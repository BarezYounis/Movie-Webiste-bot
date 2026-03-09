#!/usr/bin/env python3
"""
Video → Telegram Uploader Bot (Railway-ready)
Key fix: creates one split part at a time, uploads it, deletes it before next part.
This keeps disk usage at ~2x part size max instead of full file + all parts.
"""

import os
import re
import json
import time
import shutil
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
PART_SIZE_BYTES  = int(os.environ.get("PART_SIZE_MB", "1800")) * 1024 * 1024  # 1.8 GB default

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
#  SELENIUM — capture m3u8 + cookies
# ─────────────────────────────────────────────

def parse_netlog(netlog_path: str):
    if not os.path.exists(netlog_path):
        return None, "video"
    try:
        with open(netlog_path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        log.error(f"  Could not read net log: {e}")
        return None, "video"

    m3u8_url = None
    title    = "video"

    m3u8_matches = re.findall(r'https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*', content)
    for url in m3u8_matches:
        if "master.m3u8" in url:
            m3u8_url = url
            log.info(f"  ✅ master.m3u8: {m3u8_url[:80]}...")
            break
    if not m3u8_url and m3u8_matches:
        m3u8_url = m3u8_matches[0]
        log.info(f"  ✅ m3u8: {m3u8_url[:80]}...")

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
            log.info(f"  Title from ping: {title}")
        break

    return m3u8_url, title


def get_m3u8_and_headers_via_selenium(page_url: str):
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

    title = "video"
    cookies = []
    try:
        driver.get(page_url)
        log.info("  Waiting 20s for JW Player...")
        time.sleep(20)
        try:
            title = driver.title.strip() or "video"
            log.info(f"  Page title: {title}")
        except Exception:
            pass
        cookies = driver.get_cookies()
        log.info(f"  Captured {len(cookies)} cookies.")
    except Exception as e:
        log.error(f"  Selenium error: {e}")
    finally:
        driver.quit()
        log.info("  Chrome closed.")

    m3u8_url, log_title = parse_netlog(NET_LOG_PATH)
    if log_title != "video":
        title = log_title

    parsed = urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer":         page_url,
        "Origin":          origin,
        "Accept":          "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection":      "keep-alive",
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "cross-site",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    return m3u8_url, title, headers


# ─────────────────────────────────────────────
#  DOWNLOAD HLS SEGMENTS
# ─────────────────────────────────────────────

def parse_m3u8_segments(m3u8_content: str, base_url: str):
    """Returns list of segment URLs, or a string URL if it's a master playlist."""
    if "#EXT-X-STREAM-INF" in m3u8_content:
        best_bw  = 0
        best_url = None
        lines    = m3u8_content.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                bw = int(bw_match.group(1)) if bw_match else 0
                if bw >= best_bw and i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if nxt and not nxt.startswith("#"):
                        best_bw  = bw
                        best_url = nxt
        if best_url:
            if not best_url.startswith("http"):
                best_url = base_url.rsplit("/", 1)[0] + "/" + best_url
            return best_url  # signal to re-fetch
        return []

    segments = []
    for line in m3u8_content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            if line.startswith("http"):
                segments.append(line)
            else:
                segments.append(base_url.rsplit("/", 1)[0] + "/" + line)
    return segments


def download_hls_with_requests(m3u8_url: str, title: str, headers: dict):
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    safe_title   = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "video"
    segments_dir = os.path.join(DOWNLOAD_FOLDER, f"{safe_title}_segments")
    os.makedirs(segments_dir, exist_ok=True)
    output_path  = os.path.join(DOWNLOAD_FOLDER, f"{safe_title}.mp4")

    session = requests.Session()
    session.headers.update(headers)

    log.info("  Fetching master m3u8...")
    try:
        resp = session.get(m3u8_url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"  Failed to fetch m3u8: {e}")
        return None

    result = parse_m3u8_segments(resp.text, m3u8_url)

    if isinstance(result, str):
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

    if not segments:
        log.error("  No segments found.")
        return None

    log.info(f"  Downloading {len(segments)} segments...")
    segment_files = []
    failed = 0
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
        except Exception as e:
            log.warning(f"  Segment {idx} failed: {e}")
            failed += 1
        if idx % 50 == 0:
            log.info(f"  Progress: {idx}/{len(segments)} segments")

    log.info(f"  Downloaded {len(segment_files)}/{len(segments)} segments ({failed} failed). Merging...")

    concat_file = os.path.join(segments_dir, "concat.txt")
    with open(concat_file, "w") as f:
        for sf in segment_files:
            f.write(f"file '{sf}'\n")

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", output_path]
    result = subprocess.run(cmd, capture_output=True, text=True)

    # Clean up segments immediately to free disk space
    shutil.rmtree(segments_dir, ignore_errors=True)
    log.info("  Segments cleaned up.")

    if result.returncode != 0:
        log.error(f"  ffmpeg merge error: {result.stderr[-500:]}")
        return None

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    log.info(f"  ✅ Merged: {output_path} ({size_mb:.1f} MB)")
    return output_path


# ─────────────────────────────────────────────
#  UPLOAD WITH ONE-AT-A-TIME SPLITTING
#  Creates one part → uploads it → deletes it → creates next part
#  Max disk usage = original file + 1 part at a time
# ─────────────────────────────────────────────

async def upload_part(bot, local_path: str, caption: str) -> bool:
    filename  = os.path.basename(local_path)
    file_size = os.path.getsize(local_path)
    log.info(f"  Uploading {filename} ({file_size/1024/1024:.1f} MB)...")

    if file_size > 2 * 1024 * 1024 * 1024:
        log.error("  Exceeds 2 GB limit. Skipping.")
        return False

    for attempt in range(1, 4):
        try:
            log.info(f"  Upload attempt {attempt}/3...")
            with open(local_path, "rb") as f:
                await bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=3600,
                    write_timeout=3600,
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


async def upload_video(bot, input_path: str, title: str, page_url: str) -> bool:
    file_size = os.path.getsize(input_path)

    # Small enough to upload directly
    if file_size <= PART_SIZE_BYTES:
        log.info(f"  Single upload ({file_size/1024/1024:.1f} MB).")
        caption = CAPTION_TEMPLATE.format(title=title, url=page_url)
        ok = await upload_part(bot, input_path, caption)
        try:
            os.remove(input_path)
        except Exception:
            pass
        return ok

    # Need to split — get duration first
    log.info(f"  {file_size/1024/1024/1024:.2f} GB — will split one part at a time...")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        log.error("  Could not probe duration. Attempting direct upload.")
        caption = CAPTION_TEMPLATE.format(title=title, url=page_url)
        return await upload_part(bot, input_path, caption)

    num_parts     = max(2, -(-file_size // PART_SIZE_BYTES))
    part_duration = duration / num_parts
    base          = os.path.splitext(input_path)[0]
    success       = True

    log.info(f"  Splitting into {num_parts} parts of ~{part_duration/60:.1f} min each.")

    for i in range(num_parts):
        part_path = f"{base}_part{i+1}of{num_parts}.mp4"
        caption   = f"🎬 {title}\n📦 Part {i+1}/{num_parts}"

        # Create this part only
        log.info(f"  Creating part {i+1}/{num_parts}...")
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
        if result.returncode != 0:
            log.error(f"  ffmpeg error: {result.stderr[-300:]}")
            success = False
            continue

        part_size = os.path.getsize(part_path)
        log.info(f"  Part {i+1}/{num_parts}: {part_size/1024/1024:.1f} MB")

        # Upload this part immediately
        ok = await upload_part(bot, part_path, caption)
        if not ok:
            success = False

        # Delete part right away to free disk
        try:
            os.remove(part_path)
            log.info(f"  Deleted part {i+1} from disk.")
        except Exception:
            pass

        if i < num_parts - 1:
            await asyncio.sleep(UPLOAD_DELAY)

    # Delete original file
    try:
        os.remove(input_path)
        log.info("  Deleted original file from disk.")
    except Exception:
        pass

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

        m3u8_url, title, headers = get_m3u8_and_headers_via_selenium(page_url)
        if not m3u8_url:
            log.error("  No m3u8 found. Skipping.")
            continue

        local_path = download_hls_with_requests(m3u8_url, title, headers)
        if not local_path or not os.path.exists(local_path):
            log.error("  Download failed. Skipping.")
            continue

        success = await upload_video(bot, local_path, title, page_url)
        if success:
            save_to_log(page_url)

        await asyncio.sleep(UPLOAD_DELAY)

    log.info("\n✅ All done!")


if __name__ == "__main__":
    asyncio.run(main())
