#!/usr/bin/env python3
"""
Video Scraper → Telegram Uploader Bot
Railway-ready: downloads on server, splits files > 1.9 GB, uploads all parts.
"""

import os
import re
import json
import time
import glob
import asyncio
import logging
import subprocess
from urllib.parse import urlparse, unquote, parse_qs

import yt_dlp
import telegram
from telegram.error import TelegramError
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ─────────────────────────────────────────────
#  CONFIGURATION  (override via env vars on Railway)
# ─────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN",   "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID  = os.environ.get("CHANNEL_ID",  "@your_channel_here")

# Comma-separated list of page URLs (set as env var on Railway)
# e.g.  VIDEO_PAGES=https://kurdfilm.krd/w/movie/111,https://kurdfilm.krd/w/movie/222
_pages_env  = os.environ.get("VIDEO_PAGES", "")
VIDEO_PAGES = [u.strip() for u in _pages_env.split(",") if u.strip()]

DOWNLOAD_FOLDER  = os.environ.get("DOWNLOAD_FOLDER", "/tmp/downloads")
UPLOADED_LOG     = os.environ.get("UPLOADED_LOG",    "/tmp/uploaded_videos.txt")
UPLOAD_DELAY     = int(os.environ.get("UPLOAD_DELAY", "5"))          # seconds between uploads
PART_SIZE_BYTES  = int(os.environ.get("PART_SIZE_MB", "1900")) * 1024 * 1024  # default 1.9 GB
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", "chromedriver")

CAPTION_TEMPLATE = "🎬 {title}"

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
#  SELENIUM — intercept m3u8
# ─────────────────────────────────────────────

def extract_m3u8_from_ping_url(ping_url: str):
    parsed = urlparse(ping_url)
    params = parse_qs(parsed.query)
    mu = params.get("mu", [None])[0]
    return unquote(mu) if mu else None


def extract_title_from_ping_url(ping_url: str):
    parsed = urlparse(ping_url)
    params = parse_qs(parsed.query)
    pt = params.get("pt", [None])[0]
    return unquote(pt) if pt else "video"


def get_m3u8_via_selenium(page_url: str):
    """
    Launch headless Chrome on the server, wait for JW Player to fire its
    ping request, and extract the m3u8 stream URL + video title.
    """
    log.info(f"  Launching headless Chrome for: {page_url}")

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1280,720")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    try:
        service = Service(CHROMEDRIVER_PATH)
        driver  = webdriver.Chrome(service=service, options=chrome_options)
    except Exception as e:
        log.error(f"  ChromeDriver failed to start: {e}")
        return None, None

    m3u8_url = None
    title    = "video"

    try:
        driver.get(page_url)
        log.info("  Waiting 10s for JW Player to initialize...")
        time.sleep(10)

        logs = driver.get_log("performance")

        # Primary: JW Player ping.gif with mu= param
        for entry in logs:
            msg     = json.loads(entry["message"])
            method  = msg.get("message", {}).get("method", "")
            if method != "Network.requestWillBeSent":
                continue
            req_url = msg["message"]["params"]["request"]["url"]
            if "jwpltx.com" in req_url and "ping.gif" in req_url and "mu=" in req_url:
                extracted = extract_m3u8_from_ping_url(req_url)
                if extracted:
                    m3u8_url = extracted
                    title    = extract_title_from_ping_url(req_url)
                    log.info(f"  ✅ m3u8 found via JW ping: {m3u8_url[:80]}...")
                    log.info(f"  Title: {title}")
                    break

        # Fallback: any .m3u8 network request
        if not m3u8_url:
            for entry in logs:
                msg    = json.loads(entry["message"])
                method = msg.get("message", {}).get("method", "")
                if method != "Network.requestWillBeSent":
                    continue
                req_url = msg["message"]["params"]["request"]["url"]
                if ".m3u8" in req_url:
                    m3u8_url = req_url
                    log.info(f"  ✅ m3u8 found via fallback: {m3u8_url[:80]}...")
                    break

        if title == "video":
            try:
                title = driver.title.strip() or "video"
            except Exception:
                pass

    except Exception as e:
        log.error(f"  Selenium error: {e}")
    finally:
        driver.quit()

    return m3u8_url, title


# ─────────────────────────────────────────────
#  DOWNLOAD
# ─────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def download_m3u8(m3u8_url: str, title: str) -> str | None:
    """Download HLS stream with yt-dlp directly on the server."""
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    safe_title      = sanitize_filename(title) or "video"
    output_template = os.path.join(DOWNLOAD_FOLDER, f"{safe_title}.%(ext)s")

    ydl_opts = {
        "outtmpl":             output_template,
        "format":              "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet":               False,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        },
    }

    log.info(f"  Downloading with yt-dlp on server...")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([m3u8_url])

        # Locate downloaded file
        for f in os.listdir(DOWNLOAD_FOLDER):
            full = os.path.join(DOWNLOAD_FOLDER, f)
            if safe_title in f and f.endswith(".mp4"):
                log.info(f"  Downloaded: {full} ({os.path.getsize(full)/1024/1024:.1f} MB)")
                return full

        # Broader search
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

def split_video(input_path: str) -> list[str]:
    """
    Split a video into parts of PART_SIZE_BYTES using ffmpeg (stream copy, no re-encode).
    Returns a list of part file paths in order.
    If the file is small enough, returns [input_path] unchanged.
    """
    file_size = os.path.getsize(input_path)
    if file_size <= PART_SIZE_BYTES:
        log.info(f"  File fits in one part ({file_size/1024/1024:.1f} MB). No split needed.")
        return [input_path]

    log.info(f"  File is {file_size/1024/1024/1024:.2f} GB — splitting into ~{PART_SIZE_BYTES/1024/1024/1024:.1f} GB parts...")

    # Get video duration in seconds
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        log.error("  Could not probe video duration. Uploading as-is.")
        return [input_path]

    # Estimate how many parts needed
    num_parts   = max(2, -(-file_size // PART_SIZE_BYTES))  # ceiling division
    part_duration = duration / num_parts

    base        = os.path.splitext(input_path)[0]
    part_paths  = []

    for i in range(num_parts):
        start      = i * part_duration
        part_path  = f"{base}_part{i+1}of{num_parts}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", input_path,
            "-t", str(part_duration),
            "-c", "copy",           # stream copy — fast, no quality loss
            "-avoid_negative_ts", "make_zero",
            part_path
        ]
        log.info(f"  Creating part {i+1}/{num_parts}...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error(f"  ffmpeg error: {result.stderr[-300:]}")
        else:
            part_paths.append(part_path)
            log.info(f"  Part {i+1}: {os.path.getsize(part_path)/1024/1024:.1f} MB")

    # Remove original large file to free space
    os.remove(input_path)
    log.info(f"  Removed original file to free disk space.")
    return part_paths


# ─────────────────────────────────────────────
#  TELEGRAM UPLOAD
# ─────────────────────────────────────────────

async def upload_part(bot, local_path: str, caption: str) -> bool:
    """Upload a single file to Telegram."""
    filename  = os.path.basename(local_path)
    file_size = os.path.getsize(local_path)
    log.info(f"  Uploading {filename} ({file_size/1024/1024:.1f} MB)...")

    if file_size > 2 * 1024 * 1024 * 1024:
        log.error(f"  Part exceeds 2 GB Telegram limit ({file_size/1024**3:.2f} GB). Skipping.")
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
    """Split if needed, then upload all parts."""
    parts   = split_video(local_path)
    total   = len(parts)
    success = True

    for idx, part_path in enumerate(parts, 1):
        if total > 1:
            caption = f"🎬 {title}\n📦 Part {idx}/{total}"
        else:
            caption = CAPTION_TEMPLATE.format(title=title, url=page_url)

        ok = await upload_part(bot, part_path, caption)
        if not ok:
            success = False

        # Clean up part after upload
        try:
            os.remove(part_path)
        except Exception:
            pass

        if idx < total:
            log.info(f"  Waiting {UPLOAD_DELAY}s before next part...")
            await asyncio.sleep(UPLOAD_DELAY)

    return success


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

async def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Set BOT_TOKEN as an environment variable on Railway.")
        return
    if CHANNEL_ID == "@your_channel_here":
        print("❌ Set CHANNEL_ID as an environment variable on Railway.")
        return
    if not VIDEO_PAGES:
        print("❌ Set VIDEO_PAGES as a comma-separated env var on Railway.")
        return

    log.info(f"Bot starting. {len(VIDEO_PAGES)} page(s) to process.")
    uploaded = load_uploaded_log()
    log.info(f"Already uploaded: {len(uploaded)} page(s).")

    bot = telegram.Bot(token=BOT_TOKEN)

    for page_url in VIDEO_PAGES:
        if page_url in uploaded:
            log.info(f"Skipping (already done): {page_url}")
            continue

        log.info(f"\n{'='*60}")
        log.info(f"Processing: {page_url}")

        # 1. Intercept m3u8
        m3u8_url, title = get_m3u8_via_selenium(page_url)
        if not m3u8_url:
            log.error("  No m3u8 found. Skipping.")
            continue

        # 2. Download on server
        local_path = download_m3u8(m3u8_url, title)
        if not local_path or not os.path.exists(local_path):
            log.error("  Download failed. Skipping.")
            continue

        # 3. Upload (with auto-split if > 1.9 GB)
        success = await upload_video(bot, local_path, title, page_url)

        if success:
            save_to_log(page_url)
            log.info(f"  ✅ Done: {page_url}")
        else:
            log.error(f"  ❌ Upload failed: {page_url}")

        log.info(f"  Waiting {UPLOAD_DELAY}s before next video...")
        await asyncio.sleep(UPLOAD_DELAY)

    log.info("\n✅ All videos processed!")


if __name__ == "__main__":
    asyncio.run(main())
