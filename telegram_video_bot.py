#!/usr/bin/env python3
"""
Video → Telegram Uploader Bot (Railway-ready)
Strategy: Use Selenium to trigger yt-dlp download WITHIN the Chrome process
by launching a local proxy, OR download segments directly via Chrome's JS fetch.

Since the m3u8 token is IP-locked to the Chrome request, we download
all HLS segments directly through Chrome using JavaScript fetch(),
then reassemble with ffmpeg.
"""

import os
import re
import json
import time
import base64
import asyncio
import logging
import subprocess
from urllib.parse import urlparse, unquote, parse_qs

import requests
import telegram
from telegram.error import TelegramError
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN",   "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID  = os.environ.get("CHANNEL_ID",  "@your_channel_here")

_pages_env  = os.environ.get("VIDEO_PAGES", "")
VIDEO_PAGES = [u.strip() for u in _pages_env.split(",") if u.strip()]

DOWNLOAD_FOLDER  = os.environ.get("DOWNLOAD_FOLDER", "/tmp/downloads")
UPLOADED_LOG     = os.environ.get("UPLOADED_LOG",    "/tmp/uploaded_videos.txt")
UPLOAD_DELAY     = int(os.environ.get("UPLOAD_DELAY", "5"))
PART_SIZE_BYTES  = int(os.environ.get("PART_SIZE_MB", "1900")) * 1024 * 1024

CAPTION_TEMPLATE = "🎬 {title}"
CHROME_BIN       = "/usr/bin/google-chrome"
CHROMEDRIVER_BIN = "/usr/local/bin/chromedriver"
NET_LOG_PATH     = "/tmp/chrome_netlog.json"

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  UPLOADED LOG
# ─────────────────────────────────────────────

def load_uploaded_log():
    if not os.path.exists(UPLOADED_LOG):
        return set()
    with open(UPLOADED_LOG) as f:
        return set(line.strip() for line in f if line.strip())

def save_to_log(url: str):
    with open(UPLOADED_LOG, "a") as f:
        f.write(url + "\n")


# ─────────────────────────────────────────────
#  PARSE NET LOG
# ─────────────────────────────────────────────

def parse_netlog(netlog_path: str):
    """
    Parse Chrome net-log for:
    - master.m3u8 URL
    - All request headers Chrome used for that URL (for reuse)
    - Title from JW ping
    Returns (m3u8_url, title, headers_dict)
    """
    if not os.path.exists(netlog_path):
        return None, "video", {}

    try:
        with open(netlog_path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        log.error(f"  Could not read net log: {e}")
        return None, "video", {}

    m3u8_url = None
    title    = "video"
    headers  = {}

    # Find master.m3u8
    m3u8_matches = re.findall(r'https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*', content)
    for url in m3u8_matches:
        if "master.m3u8" in url:
            m3u8_url = url
            log.info(f"  ✅ master.m3u8: {m3u8_url[:80]}...")
            break
    if not m3u8_url and m3u8_matches:
        m3u8_url = m3u8_matches[0]
        log.info(f"  ✅ m3u8: {m3u8_url[:80]}...")

    # Find title from JW ping
    ping_matches = re.findall(r'https?://[^\s"\'\\]*jwpltx\.com[^\s"\'\\]*ping\.gif[^\s"\'\\]*', content)
    for ping_url in ping_matches:
        parsed = urlparse(ping_url)
        params = parse_qs(parsed.query)
        if not m3u8_url:
            mu = params.get("mu", [None])[0]
            if mu:
                m3u8_url = unquote(mu)
        pt = params.get("pt", [None])[0]
        if pt:
            title = unquote(pt)
            log.info(f"  Title: {title}")
        break

    return m3u8_url, title, headers


# ─────────────────────────────────────────────
#  SELENIUM — get m3u8 + download via JS fetch
# ─────────────────────────────────────────────

def get_m3u8_and_headers_via_selenium(page_url: str):
    """
    Open page, capture m3u8 URL, and extract request headers
    that Chrome used (including cookies) via JS and performance entries.
    Returns (m3u8_url, title, request_headers, selenium_driver)
    NOTE: driver is returned OPEN so we can use it to download.
    """
    if os.path.exists(NET_LOG_PATH):
        os.remove(NET_LOG_PATH)

    chrome_options = Options()
    chrome_options.binary_location = CHROME_BIN
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--window-size=1280,720")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    chrome_options.add_argument(f"--log-net-log={NET_LOG_PATH}")
    chrome_options.add_argument("--net-log-capture-mode=IncludeSocketBytes")

    service = Service(executable_path=CHROMEDRIVER_BIN)
    driver  = webdriver.Chrome(service=service, options=chrome_options)
    log.info("  ✅ ChromeDriver started.")

    driver.get(page_url)
    log.info("  Waiting 20s for JW Player...")
    time.sleep(20)

    title = "video"
    try:
        title = driver.title.strip() or "video"
        log.info(f"  Page title: {title}")
    except Exception:
        pass

    # Get cookies from browser
    cookies = driver.get_cookies()
    cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

    driver.quit()
    log.info("  Chrome closed. Parsing net log...")

    m3u8_url, log_title, _ = parse_netlog(NET_LOG_PATH)
    if log_title != "video":
        title = log_title

    # Build headers mimicking Chrome exactly
    parsed  = urlparse(page_url)
    origin  = f"{parsed.scheme}://{parsed.netloc}"
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer":          page_url,
        "Origin":           origin,
        "Accept":           "*/*",
        "Accept-Language":  "en-US,en;q=0.9",
        "Accept-Encoding":  "gzip, deflate, br",
        "Connection":       "keep-alive",
        "Sec-Fetch-Dest":   "empty",
        "Sec-Fetch-Mode":   "cors",
        "Sec-Fetch-Site":   "cross-site",
    }
    if cookie_header:
        request_headers["Cookie"] = cookie_header
        log.info(f"  Added {len(cookies)} cookies to request headers.")

    return m3u8_url, title, request_headers


# ─────────────────────────────────────────────
#  DOWNLOAD VIA REQUESTS (same IP as Chrome)
# ─────────────────────────────────────────────

def parse_m3u8_segments(m3u8_content: str, base_url: str) -> list:
    """Parse an m3u8 playlist and return list of absolute segment URLs."""
    segments = []
    lines    = m3u8_content.splitlines()

    # If this is a master playlist, find the best stream playlist URL
    if "#EXT-X-STREAM-INF" in m3u8_content:
        best_bandwidth = 0
        best_url       = None
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                bw = int(bw_match.group(1)) if bw_match else 0
                if bw >= best_bandwidth and i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line and not next_line.startswith("#"):
                        best_bandwidth = bw
                        best_url       = next_line
        if best_url:
            if not best_url.startswith("http"):
                best_url = base_url.rsplit("/", 1)[0] + "/" + best_url
            return best_url  # Return URL string, not list, to signal re-fetch needed
        return []

    # Regular media playlist — extract .ts or segment URLs
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            if line.startswith("http"):
                segments.append(line)
            else:
                segments.append(base_url.rsplit("/", 1)[0] + "/" + line)

    return segments


def download_hls_with_requests(m3u8_url: str, title: str, headers: dict) -> str | None:
    """
    Download HLS stream using requests (runs in same process/IP as the bot).
    Fetches master m3u8 → media m3u8 → all .ts segments → merges with ffmpeg.
    """
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    safe_title   = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "video"
    segments_dir = os.path.join(DOWNLOAD_FOLDER, f"{safe_title}_segments")
    os.makedirs(segments_dir, exist_ok=True)
    output_path  = os.path.join(DOWNLOAD_FOLDER, f"{safe_title}.mp4")

    session = requests.Session()
    session.headers.update(headers)

    log.info(f"  Fetching master m3u8...")
    try:
        resp = session.get(m3u8_url, timeout=30)
        resp.raise_for_status()
        master_content = resp.text
    except Exception as e:
        log.error(f"  Failed to fetch m3u8: {e}")
        return None

    # Parse master → get media playlist URL
    result = parse_m3u8_segments(master_content, m3u8_url)

    if isinstance(result, str):
        # Got a media playlist URL back — fetch it
        media_url = result
        log.info(f"  Fetching media playlist: {media_url[:80]}...")
        try:
            resp = session.get(media_url, timeout=30)
            resp.raise_for_status()
            segments = parse_m3u8_segments(resp.text, media_url)
        except Exception as e:
            log.error(f"  Failed to fetch media playlist: {e}")
            return None
    else:
        segments = result
        media_url = m3u8_url

    if not segments:
        log.error("  No segments found in playlist.")
        return None

    log.info(f"  Downloading {len(segments)} segments...")

    segment_files = []
    for idx, seg_url in enumerate(segments):
        seg_path = os.path.join(segments_dir, f"seg_{idx:05d}.ts")
        if os.path.exists(seg_path):
            segment_files.append(seg_path)
            continue
        try:
            r = session.get(seg_url, timeout=60)
            r.raise_for_status()
            with open(seg_path, "wb") as f:
                f.write(r.content)
            segment_files.append(seg_path)
            if idx % 20 == 0:
                log.info(f"  Progress: {idx}/{len(segments)} segments")
        except Exception as e:
            log.warning(f"  Segment {idx} failed: {e}")

    if not segment_files:
        log.error("  No segments downloaded.")
        return None

    log.info(f"  Downloaded {len(segment_files)}/{len(segments)} segments. Merging...")

    # Write concat list for ffmpeg
    concat_file = os.path.join(segments_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for sf in segment_files:
            f.write(f"file '{sf}'\n")

    # Merge with ffmpeg
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        output_path
    ]
    log.info(f"  Merging with ffmpeg...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        log.error(f"  ffmpeg merge error: {result.stderr[-500:]}")
        return None

    # Cleanup segments
    import shutil
    shutil.rmtree(segments_dir, ignore_errors=True)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    log.info(f"  ✅ Merged: {output_path} ({size_mb:.1f} MB)")
    return output_path


# ─────────────────────────────────────────────
#  SPLIT WITH FFMPEG
# ─────────────────────────────────────────────

def split_video(input_path: str) -> list:
    file_size = os.path.getsize(input_path)
    if file_size <= PART_SIZE_BYTES:
        log.info(f"  Single upload ({file_size/1024/1024:.1f} MB).")
        return [input_path]

    log.info(f"  {file_size/1024/1024/1024:.2f} GB — splitting...")

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        return [input_path]

    num_parts     = max(2, -(-file_size // PART_SIZE_BYTES))
    part_duration = duration / num_parts
    base          = os.path.splitext(input_path)[0]
    part_paths    = []

    for i in range(num_parts):
        part_path = f"{base}_part{i+1}of{num_parts}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(i * part_duration),
            "-i", input_path,
            "-t", str(part_duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            part_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            part_paths.append(part_path)
            log.info(f"  Part {i+1}/{num_parts}: {os.path.getsize(part_path)/1024/1024:.1f} MB")
        else:
            log.error(f"  ffmpeg split error: {result.stderr[-200:]}")

    os.remove(input_path)
    return part_paths


# ─────────────────────────────────────────────
#  TELEGRAM UPLOAD
# ─────────────────────────────────────────────

async def upload_part(bot, local_path: str, caption: str) -> bool:
    filename  = os.path.basename(local_path)
    file_size = os.path.getsize(local_path)
    log.info(f"  Uploading {filename} ({file_size/1024/1024:.1f} MB)...")

    if file_size > 2 * 1024 * 1024 * 1024:
        log.error("  Exceeds 2 GB. Skipping.")
        return False

    for attempt in range(1, 4):  # 3 attempts
        try:
            log.info(f"  Upload attempt {attempt}/3...")
            with open(local_path, "rb") as f:
                await bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=3600,   # 1 hour
                    write_timeout=3600,  # 1 hour
                    connect_timeout=120,
                    pool_timeout=3600,
                )
            log.info(f"  ✅ Uploaded: {filename}")
            return True
        except TelegramError as e:
            log.error(f"  Telegram error (attempt {attempt}): {e}")
            if attempt < 3:
                wait = 30 * attempt
                log.info(f"  Retrying in {wait}s...")
                await asyncio.sleep(wait)

    log.error(f"  ❌ All upload attempts failed for {filename}")
    return False


async def upload_video(bot, local_path: str, title: str, page_url: str) -> bool:
    parts   = split_video(local_path)
    total   = len(parts)
    success = True

    for idx, part_path in enumerate(parts, 1):
        caption = (
            f"🎬 {title}\n📦 Part {idx}/{total}"
            if total > 1
            else CAPTION_TEMPLATE.format(title=title, url=page_url)
        )
        ok = await upload_part(bot, part_path, caption)
        if not ok:
            success = False
        try:
            os.remove(part_path)
        except Exception:
            pass
        if idx < total:
            await asyncio.sleep(UPLOAD_DELAY)

    return success


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

async def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Set BOT_TOKEN env var.")
        return
    if CHANNEL_ID == "@your_channel_here":
        print("❌ Set CHANNEL_ID env var.")
        return
    if not VIDEO_PAGES:
        print("❌ Set VIDEO_PAGES env var.")
        return

    uploaded = load_uploaded_log()
    log.info(f"Bot starting. {len(VIDEO_PAGES)} page(s) queued, {len(uploaded)} already done.")

    bot = telegram.Bot(token=BOT_TOKEN)

    for page_url in VIDEO_PAGES:
        if page_url in uploaded:
            log.info(f"Skipping: {page_url}")
            continue

        log.info(f"\n{'='*60}")
        log.info(f"Processing: {page_url}")

        # 1. Get m3u8 URL + session headers from Chrome
        m3u8_url, title, headers = get_m3u8_and_headers_via_selenium(page_url)
        if not m3u8_url:
            log.error("  No m3u8 found. Skipping.")
            continue

        # 2. Download HLS using same headers (same IP, same session context)
        local_path = download_hls_with_requests(m3u8_url, title, headers)
        if not local_path or not os.path.exists(local_path):
            log.error("  Download failed. Skipping.")
            continue

        # 3. Upload to Telegram (split if > 1.9 GB)
        success = await upload_video(bot, local_path, title, page_url)
        if success:
            save_to_log(page_url)

        await asyncio.sleep(UPLOAD_DELAY)

    log.info("\n✅ All done!")


if __name__ == "__main__":
    asyncio.run(main())
