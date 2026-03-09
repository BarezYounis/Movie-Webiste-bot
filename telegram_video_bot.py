#!/usr/bin/env python3
"""
Video → Telegram Uploader Bot (Railway-ready)
Captures JW Player m3u8 URL + session cookies from Chrome, passes them to yt-dlp.
"""

import os
import re
import json
import time
import asyncio
import logging
import subprocess
import tempfile
from urllib.parse import urlparse, unquote, parse_qs

import yt_dlp
import telegram
from telegram.error import TelegramError
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ─────────────────────────────────────────────
#  CONFIGURATION  (set as env vars on Railway)
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
COOKIES_FILE     = "/tmp/chrome_cookies.txt"

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

def parse_netlog_for_m3u8(netlog_path: str):
    """Parse Chrome net-log file for m3u8 URL and title."""
    if not os.path.exists(netlog_path):
        log.error(f"  Net log not found: {netlog_path}")
        return None, "video"

    try:
        with open(netlog_path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        log.error(f"  Could not read net log: {e}")
        return None, "video"

    m3u8_url = None
    title    = "video"

    # Find master.m3u8 URLs
    m3u8_matches = re.findall(r'https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*', content)
    for url in m3u8_matches:
        if "master.m3u8" in url:
            m3u8_url = url
            log.info(f"  ✅ master.m3u8: {m3u8_url[:80]}...")
            break
    if not m3u8_url and m3u8_matches:
        m3u8_url = m3u8_matches[0]
        log.info(f"  ✅ m3u8: {m3u8_url[:80]}...")

    # Find title from JW Player ping
    ping_matches = re.findall(r'https?://[^\s"\'\\]*jwpltx\.com[^\s"\'\\]*ping\.gif[^\s"\'\\]*', content)
    for ping_url in ping_matches:
        parsed = urlparse(ping_url)
        params = parse_qs(parsed.query)
        if not m3u8_url:
            mu = params.get("mu", [None])[0]
            if mu:
                m3u8_url = unquote(mu)
                log.info(f"  ✅ m3u8 from JW ping: {m3u8_url[:80]}...")
        pt = params.get("pt", [None])[0]
        if pt:
            title = unquote(pt)
            log.info(f"  Title: {title}")
        break

    return m3u8_url, title


def cookies_to_netscape(cookies: list, domain: str) -> str:
    """Convert Selenium cookies to Netscape format for yt-dlp."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        cookie_domain = c.get("domain", domain)
        if not cookie_domain.startswith("."):
            cookie_domain = "." + cookie_domain
        secure    = "TRUE" if c.get("secure", False) else "FALSE"
        http_only = "TRUE"
        expiry    = str(int(c.get("expiry", 9999999999)))
        name      = c.get("name", "")
        value     = c.get("value", "")
        path      = c.get("path", "/")
        lines.append(f"{cookie_domain}\t{http_only}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
    return "\n".join(lines)


def get_m3u8_via_selenium(page_url: str):
    """
    Open page in headless Chrome, capture net-log for m3u8 URL,
    export session cookies for authenticated download.
    Returns (m3u8_url, title, referer, cookies_file_path).
    """
    log.info(f"  Chrome: {CHROME_BIN} exists={os.path.exists(CHROME_BIN)}")
    log.info(f"  ChromeDriver: {CHROMEDRIVER_BIN} exists={os.path.exists(CHROMEDRIVER_BIN)}")

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

    try:
        service = Service(executable_path=CHROMEDRIVER_BIN)
        driver  = webdriver.Chrome(service=service, options=chrome_options)
        log.info("  ✅ ChromeDriver started.")
    except Exception as e:
        log.error(f"  ChromeDriver failed: {e}")
        return None, None, None, None

    title   = "video"
    referer = page_url
    cookies_path = None

    try:
        driver.get(page_url)
        log.info("  Waiting 20s for JW Player...")
        time.sleep(20)

        try:
            title = driver.title.strip() or "video"
            log.info(f"  Page title: {title}")
        except Exception:
            pass

        # Export cookies in Netscape format
        try:
            cookies = driver.get_cookies()
            log.info(f"  Captured {len(cookies)} cookies.")
            domain  = urlparse(page_url).netloc
            netscape_cookies = cookies_to_netscape(cookies, domain)
            with open(COOKIES_FILE, "w") as f:
                f.write(netscape_cookies)
            cookies_path = COOKIES_FILE
            log.info(f"  Cookies saved to {COOKIES_FILE}")
        except Exception as e:
            log.warning(f"  Could not export cookies: {e}")

    except Exception as e:
        log.error(f"  Selenium page load error: {e}")
    finally:
        driver.quit()
        log.info("  Chrome closed. Parsing net log...")

    m3u8_url, log_title = parse_netlog_for_m3u8(NET_LOG_PATH)
    if log_title != "video":
        title = log_title

    return m3u8_url, title, referer, cookies_path


# ─────────────────────────────────────────────
#  DOWNLOAD
# ─────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def download_m3u8(m3u8_url: str, title: str, referer: str, cookies_path: str):
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    safe_title      = sanitize_filename(title) or "video"
    output_template = os.path.join(DOWNLOAD_FOLDER, f"{safe_title}.%(ext)s")

    ydl_opts = {
        "outtmpl":             output_template,
        "format":              "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet":               False,
        # Pass the same headers Chrome used
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer":    referer,
            "Origin":     f"{urlparse(referer).scheme}://{urlparse(referer).netloc}",
        },
    }

    # Pass cookies if available
    if cookies_path and os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path
        log.info(f"  Using cookies file: {cookies_path}")

    log.info(f"  Downloading: {title}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([m3u8_url])

        for f in os.listdir(DOWNLOAD_FOLDER):
            full = os.path.join(DOWNLOAD_FOLDER, f)
            if safe_title in f and f.endswith(".mp4"):
                log.info(f"  ✅ {full} ({os.path.getsize(full)/1024/1024:.1f} MB)")
                return full
        for f in os.listdir(DOWNLOAD_FOLDER):
            full = os.path.join(DOWNLOAD_FOLDER, f)
            if safe_title[:10] in f:
                return full

    except Exception as e:
        log.error(f"  yt-dlp error: {e}")

    return None


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
        log.error("  Could not get duration. Uploading as-is.")
        return [input_path]

    num_parts     = max(2, -(-file_size // PART_SIZE_BYTES))
    part_duration = duration / num_parts
    base          = os.path.splitext(input_path)[0]
    part_paths    = []

    for i in range(num_parts):
        start     = i * part_duration
        part_path = f"{base}_part{i+1}of{num_parts}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", input_path,
            "-t", str(part_duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            part_path
        ]
        log.info(f"  Creating part {i+1}/{num_parts}...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            part_paths.append(part_path)
            log.info(f"  Part {i+1}: {os.path.getsize(part_path)/1024/1024:.1f} MB")
        else:
            log.error(f"  ffmpeg error: {result.stderr[-300:]}")

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
        log.error("  Exceeds 2 GB limit. Skipping.")
        return False

    try:
        with open(local_path, "rb") as f:
            await bot.send_video(
                chat_id=CHANNEL_ID,
                video=f,
                caption=caption,
                supports_streaming=True,
                read_timeout=600,
                write_timeout=600,
                connect_timeout=60,
            )
        log.info(f"  ✅ Uploaded: {filename}")
        return True
    except TelegramError as e:
        log.error(f"  Telegram error: {e}")
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
        print("❌ Set BOT_TOKEN env var on Railway.")
        return
    if CHANNEL_ID == "@your_channel_here":
        print("❌ Set CHANNEL_ID env var on Railway.")
        return
    if not VIDEO_PAGES:
        print("❌ Set VIDEO_PAGES env var on Railway.")
        return

    uploaded = load_uploaded_log()
    log.info(f"Bot starting. {len(VIDEO_PAGES)} page(s) queued, {len(uploaded)} already done.")

    bot = telegram.Bot(token=BOT_TOKEN)

    for page_url in VIDEO_PAGES:
        if page_url in uploaded:
            log.info(f"Skipping (already done): {page_url}")
            continue

        log.info(f"\n{'='*60}")
        log.info(f"Processing: {page_url}")

        m3u8_url, title, referer, cookies_path = get_m3u8_via_selenium(page_url)
        if not m3u8_url:
            log.error("  No m3u8 found. Skipping.")
            continue

        local_path = download_m3u8(m3u8_url, title, referer, cookies_path)
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
