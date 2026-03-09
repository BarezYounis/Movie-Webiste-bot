#!/usr/bin/env python3
"""
ZedFlix → Telegram Bot (Link Trigger + Daily Scheduler)
- Keeps daily cron scheduler for VIDEO_PAGES
- Also listens for Telegram messages containing a web link
- When a link is sent to the bot, Railway starts processing it immediately
- Detects original resolution from m3u8 and prefers 1080p/720p without re-encoding
- Posts thumbnail + caption, then uploads video as reply in the target channel
"""

import os
import re
import time
import shutil
import asyncio
import logging
import subprocess
from datetime import datetime
from urllib.parse import urlparse, unquote, parse_qs, urljoin

import requests
from bs4 import BeautifulSoup
import telegram
from telethon import TelegramClient, events
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

TL_CHANNEL = None
PROCESS_QUEUE: asyncio.Queue = asyncio.Queue()
QUEUE_SET = set()
QUEUE_LOCK = asyncio.Lock()

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
BOT_TOKEN       = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID      = os.environ.get("CHANNEL_ID", "@your_channel_here")
API_ID          = int(os.environ.get("API_ID", "0"))
API_HASH        = os.environ.get("API_HASH", "")
OWNER_USER_ID   = int(os.environ.get("OWNER_USER_ID", "1166019209"))  # optional: only accept links from this Telegram user id
ALLOW_ALL_USERS = os.environ.get("ALLOW_ALL_USERS", "false").lower() == "true"

_pages_env = os.environ.get("VIDEO_PAGES", "")
VIDEO_PAGES = [u.strip() for u in _pages_env.split(",") if u.strip()]

RUN_HOUR     = int(os.environ.get("RUN_HOUR", "2"))
RUN_MINUTE   = int(os.environ.get("RUN_MINUTE", "0"))
DAILY_LIMIT  = int(os.environ.get("DAILY_LIMIT", "20"))

DOWNLOAD_FOLDER = os.environ.get("DOWNLOAD_FOLDER", "/tmp/downloads")
UPLOADED_LOG    = os.environ.get("UPLOADED_LOG", "/tmp/uploaded_movies.txt")
UPLOAD_DELAY    = int(os.environ.get("UPLOAD_DELAY", "5"))
PART_SIZE_BYTES = int(os.environ.get("PART_SIZE_MB", "1900")) * 1024 * 1024

CHROME_BIN       = "/usr/bin/google-chrome"
CHROMEDRIVER_BIN = "/usr/local/bin/chromedriver"
NET_LOG_PATH     = "/tmp/chrome_netlog.json"

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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


def probe_video_info(path: str) -> dict:
    """Read final video metadata so Telegram mobile gets correct dimensions."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,sample_aspect_ratio,display_aspect_ratio:format=duration",
        "-of", "default=noprint_wrappers=1",
        path,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
        width = int(float(data.get("width", "0") or 0))
        height = int(float(data.get("height", "0") or 0))
        duration = int(float(data.get("duration", "0") or 0))
        sar = data.get("sample_aspect_ratio", "1:1") or "1:1"
        dar = data.get("display_aspect_ratio", "")
        return {
            "width": width,
            "height": height,
            "duration": duration,
            "sar": sar,
            "dar": dar,
        }
    except Exception as e:
        log.warning(f"Could not probe video metadata for {path}: {e}")
        return {"width": 0, "height": 0, "duration": 0, "sar": "1:1", "dar": ""}


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def extract_first_url(text: str) -> str | None:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip(".,)") if m else None


def is_allowed_sender(sender_id: int | None) -> bool:
    if ALLOW_ALL_USERS:
        return True
    if OWNER_USER_ID and sender_id == OWNER_USER_ID:
        return True
    return False


async def enqueue_url(url: str, source_chat_id: int | None = None, source_message_id: int | None = None):
    async with QUEUE_LOCK:
        if url in QUEUE_SET:
            return False
        QUEUE_SET.add(url)
        await PROCESS_QUEUE.put({
            "url": url,
            "source_chat_id": source_chat_id,
            "source_message_id": source_message_id,
        })
        return True


async def dequeue_done(url: str):
    async with QUEUE_LOCK:
        QUEUE_SET.discard(url)


# ─────────────────────────────────────────────
#  MOVIE DETAIL SCRAPER
# ─────────────────────────────────────────────

def scrape_movie_details(page_url: str) -> dict:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    details = {
        "title": "Unknown",
        "year": "",
        "duration": "",
        "category": "",
        "thumbnail_url": "",
        "page_url": page_url,
    }

    try:
        resp = session.get(page_url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for sel in ["h1.movie-title", "h1", ".title", "[class*='title']"]:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                text = re.sub(r'\s*[\|\-–]\s*.*$', '', el.get_text(strip=True)).strip()
                if text:
                    details["title"] = text
                    break

        if details["title"] == "Unknown":
            t = soup.find("title")
            if t:
                text = re.sub(r'\s*[\|\-–]\s*.*$', '', t.get_text(strip=True)).strip()
                if text:
                    details["title"] = text

        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            details["thumbnail_url"] = og["content"]
        else:
            for sel in [".poster img", ".cover img", "[class*='poster'] img", "[class*='cover'] img"]:
                img = soup.select_one(sel)
                if img:
                    src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                    if src:
                        details["thumbnail_url"] = urljoin(page_url, src)
                        break

        full_text = soup.get_text(" ", strip=True)

        m = re.search(r'\b(19[5-9]\d|20[0-3]\d)\b', full_text)
        if m:
            details["year"] = m.group(1)

        m = (
            re.search(r'\b(\d{1,2}:\d{2}:\d{2})\b', full_text)
            or re.search(r'\b(\d{2,3})\s*(?:min|دەقیقە|دقیقه)', full_text, re.IGNORECASE)
        )
        if m:
            details["duration"] = m.group(1)

        for sel in [".genre a", ".category a", "[class*='genre'] a", "[class*='category'] a", ".tags a"]:
            els = soup.select(sel)
            if els:
                cats = [e.get_text(strip=True) for e in els[:3] if e.get_text(strip=True)]
                if cats:
                    details["category"] = " · ".join(cats)
                    break

        log.info(f"  Scraped: {details['title']} ({details['year']}) [{details['duration']}] {details['category']}")
    except Exception as e:
        log.error(f"  Scrape error: {e}")

    return details



def format_caption(details: dict) -> str:
    lines = [f"🎬 *{details['title']}*"]
    if details["year"]:
        lines.append(f"📅 {details['year']}")
    if details["duration"]:
        lines.append(f"⏱ {details['duration']}")
    if details["category"]:
        lines.append(f"🎭 {details['category']}")
    lines.append(f"🔗 [Watch on ZedFlix]({details['page_url']})")
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  SELENIUM — capture m3u8 URL + session headers
# ─────────────────────────────────────────────

def parse_netlog(netlog_path: str):
    if not os.path.exists(netlog_path):
        return None
    try:
        with open(netlog_path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        log.error(f"  Net log read error: {e}")
        return None

    for url in re.findall(r'https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*', content):
        if "master.m3u8" in url:
            log.info(f"  ✅ master.m3u8: {url[:80]}...")
            return url

    matches = re.findall(r'https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*', content)
    if matches:
        log.info(f"  ✅ m3u8: {matches[0][:80]}...")
        return matches[0]

    for ping_url in re.findall(r'https?://[^\s"\'\\]*jwpltx\.com[^\s"\'\\]*ping\.gif[^\s"\'\\]*', content):
        mu = parse_qs(urlparse(ping_url).query).get("mu", [None])[0]
        if mu:
            url = unquote(mu)
            log.info(f"  ✅ m3u8 from ping: {url[:80]}...")
            return url

    return None



def get_m3u8_and_headers(page_url: str):
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
    driver = webdriver.Chrome(service=service, options=chrome_options)
    log.info("  ✅ Chrome started.")

    cookies = []
    try:
        driver.get(page_url)
        log.info("  Waiting 20s for JW Player...")
        time.sleep(20)
        cookies = driver.get_cookies()
        log.info(f"  Captured {len(cookies)} cookies.")
    except Exception as e:
        log.error(f"  Selenium error: {e}")
    finally:
        driver.quit()
        log.info("  Chrome closed. Parsing net log...")

    m3u8_url = parse_netlog(NET_LOG_PATH)
    parsed = urlparse(page_url)
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": page_url,
        "Origin": f"{parsed.scheme}://{parsed.netloc}",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str

    return m3u8_url, headers


# ─────────────────────────────────────────────
#  DOWNLOAD HLS — choose best real HD variant
# ─────────────────────────────────────────────

def parse_m3u8(content: str, base_url: str):
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    if "#EXT-X-STREAM-INF" in content:
        variants = []
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue

            bw_match = re.search(r'BANDWIDTH=(\d+)', line)
            res_match = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
            frame_match = re.search(r'FRAME-RATE=([\d.]+)', line)

            next_url = None
            for j in range(i + 1, len(lines)):
                if not lines[j].startswith("#"):
                    next_url = lines[j]
                    break

            if not next_url:
                continue

            variants.append({
                "bandwidth": int(bw_match.group(1)) if bw_match else 0,
                "width": int(res_match.group(1)) if res_match else 0,
                "height": int(res_match.group(2)) if res_match else 0,
                "frame_rate": float(frame_match.group(1)) if frame_match else 0.0,
                "url": next_url if next_url.startswith("http") else urljoin(base_url, next_url),
                "raw": line,
            })
        return variants

    segments = []
    for line in lines:
        if not line.startswith("#"):
            segments.append(line if line.startswith("http") else urljoin(base_url, line))
    return segments



def choose_best_hd_variant(variants: list[dict]) -> dict | None:
    if not variants:
        return None

    variants = sorted(
        variants,
        key=lambda v: (v.get("height", 0), v.get("width", 0), v.get("bandwidth", 0), v.get("frame_rate", 0.0)),
        reverse=True,
    )

    hd_1080 = [v for v in variants if v.get("height", 0) >= 1080]
    if hd_1080:
        return hd_1080[0]

    hd_720 = [v for v in variants if 720 <= v.get("height", 0) < 1080]
    if hd_720:
        return hd_720[0]

    return variants[0]



def download_hls(m3u8_url: str, title: str, headers: dict) -> str | None:
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    safe = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "video"
    segs_dir = os.path.join(DOWNLOAD_FOLDER, f"{safe}_segs")
    os.makedirs(segs_dir, exist_ok=True)
    output = os.path.join(DOWNLOAD_FOLDER, f"{safe}.mp4")

    session = requests.Session()
    session.headers.update(headers)

    log.info("  Fetching master m3u8...")
    try:
        r = session.get(m3u8_url, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log.error(f"  Master m3u8 fetch failed: {e}")
        shutil.rmtree(segs_dir, ignore_errors=True)
        return None

    playlist = parse_m3u8(r.text, m3u8_url)

    if playlist and isinstance(playlist[0], dict):
        best_variant = choose_best_hd_variant(playlist)
        if not best_variant:
            log.error("  No playable variant found in master playlist.")
            shutil.rmtree(segs_dir, ignore_errors=True)
            return None

        media_url = best_variant["url"]
        log.info(
            f"  Selected variant: {best_variant.get('width', 0)}x{best_variant.get('height', 0)} | "
            f"{best_variant.get('bandwidth', 0)} bps"
        )

        try:
            r = session.get(media_url, timeout=30)
            r.raise_for_status()
            segments = parse_m3u8(r.text, media_url)
        except Exception as e:
            log.error(f"  Media playlist fetch failed: {e}")
            shutil.rmtree(segs_dir, ignore_errors=True)
            return None
    else:
        segments = playlist
        log.info("  Master playlist not found; using direct media playlist.")

    if not segments:
        log.error("  No segments found in playlist.")
        shutil.rmtree(segs_dir, ignore_errors=True)
        return None

    log.info(f"  Downloading {len(segments)} segments...")
    seg_files = []
    for idx, seg_url in enumerate(segments):
        seg_path = os.path.join(segs_dir, f"seg_{idx:05d}.ts")
        if os.path.exists(seg_path):
            seg_files.append(seg_path)
            continue
        for attempt in range(3):
            try:
                r = session.get(seg_url, timeout=60)
                r.raise_for_status()
                with open(seg_path, "wb") as f:
                    f.write(r.content)
                seg_files.append(seg_path)
                break
            except Exception as e:
                if attempt == 2:
                    log.warning(f"  Skipping segment {idx} after 3 failures: {e}")
        if idx % 50 == 0:
            log.info(f"  Progress: {idx}/{len(segments)} segments")

    if not seg_files:
        log.error("  All segment downloads failed.")
        shutil.rmtree(segs_dir, ignore_errors=True)
        return None

    log.info(f"  Downloaded {len(seg_files)}/{len(segments)} segments. Merging without re-encoding...")
    concat = os.path.join(segs_dir, "concat.txt")
    with open(concat, "w") as f:
        for sf in seg_files:
            f.write(f"file '{sf}'\n")

    # Try to normalize sample aspect ratio at metadata level only (no re-encode).
    # This helps Telegram mobile avoid zoomed playback when HLS segments carry bad SAR.
    remux_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c", "copy",
        "-bsf:v", "h264_metadata=sample_aspect_ratio=1/1",
        "-movflags", "+faststart",
        output,
    ]
    res = subprocess.run(remux_cmd, capture_output=True, text=True)

    if res.returncode != 0:
        log.warning("  SAR metadata rewrite failed; falling back to plain remux.")
        res = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat,
                "-map", "0:v:0",
                "-map", "0:a?",
                "-c", "copy",
                "-movflags", "+faststart",
                output,
            ],
            capture_output=True,
            text=True,
        )

    shutil.rmtree(segs_dir, ignore_errors=True)
    log.info("  Segments cleaned up.")

    if res.returncode != 0:
        log.error(f"  ffmpeg error: {res.stderr[-300:]}")
        return None

    info = probe_video_info(output)
    log.info(
        f"  ✅ {output} ({os.path.getsize(output)/1024/1024:.1f} MB) | "
        f"{info.get('width', 0)}x{info.get('height', 0)} | SAR {info.get('sar', '')} | DAR {info.get('dar', '')}"
    )
    return output


# ─────────────────────────────────────────────
#  TELEGRAM SENDS
# ─────────────────────────────────────────────

async def post_thumbnail(tg_bot, details: dict) -> int | None:
    caption = format_caption(details)
    try:
        if details["thumbnail_url"]:
            r = requests.get(details["thumbnail_url"], timeout=15)
            r.raise_for_status()
            msg = await tg_bot.send_photo(chat_id=CHANNEL_ID, photo=r.content, caption=caption, parse_mode="Markdown")
        else:
            msg = await tg_bot.send_message(chat_id=CHANNEL_ID, text=caption, parse_mode="Markdown")
        log.info(f"  ✅ Thumbnail posted (msg_id={msg.message_id})")
        return msg.message_id
    except Exception as e:
        log.error(f"  Thumbnail post failed: {e}")
        return None


async def upload_video(tl_client, input_path: str, title: str, reply_to: int | None) -> bool:
    file_size = os.path.getsize(input_path)
    log.info(f"  Uploading {os.path.basename(input_path)} ({file_size/1024/1024:.1f} MB) via Telethon...")

    TWO_GB = 2 * 1024 * 1024 * 1024

    async def _upload_one(path: str, caption: str, reply_id: int | None) -> bool:
        info = probe_video_info(path)
        width = max(1, int(info.get("width", 0) or 0))
        height = max(1, int(info.get("height", 0) or 0))
        duration = max(0, int(info.get("duration", 0) or 0))
        attributes = [
            DocumentAttributeVideo(
                duration=duration,
                w=width,
                h=height,
                supports_streaming=True,
            )
        ]

        log.info(
            f"  Upload metadata: {width}x{height} | duration={duration}s | "
            f"SAR {info.get('sar', '')} | DAR {info.get('dar', '')}"
        )

        for attempt in range(1, 4):
            try:
                log.info(f"  Telethon upload attempt {attempt}/3: {os.path.basename(path)}")
                await tl_client.send_file(
                    TL_CHANNEL,
                    path,
                    caption=caption,
                    reply_to=reply_id,
                    supports_streaming=True,
                    force_document=False,
                    mime_type="video/mp4",
                    attributes=attributes,
                    progress_callback=lambda c, t: log.info(f"    Upload progress: {c/t*100:.1f}%") if t and c % (50 * 1024 * 1024) < 1 * 1024 * 1024 else None,
                )
                log.info(f"  ✅ Uploaded: {os.path.basename(path)}")
                return True
            except Exception as e:
                log.error(f"  Upload error (attempt {attempt}): {e}")
                if attempt < 3:
                    await asyncio.sleep(30 * attempt)
        return False

    if file_size <= TWO_GB:
        ok = await _upload_one(input_path, f"🎬 {title}", reply_to)
        try:
            os.remove(input_path)
        except Exception:
            pass
        return ok

    log.info(f"  {file_size/1024/1024/1024:.2f} GB > 2 GB — splitting one part at a time...")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True,
        text=True,
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        return await _upload_one(input_path, f"🎬 {title}", reply_to)

    num_parts = max(2, -(-file_size // PART_SIZE_BYTES))
    part_duration = duration / num_parts
    base = os.path.splitext(input_path)[0]
    success = True

    for i in range(num_parts):
        part_path = f"{base}_part{i+1}of{num_parts}.mp4"
        log.info(f"  Creating part {i+1}/{num_parts}...")
        res = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(i * part_duration),
                "-i", input_path,
                "-t", str(part_duration),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                part_path,
            ],
            capture_output=True,
            text=True,
        )

        if res.returncode != 0:
            log.error(f"  ffmpeg error: {res.stderr[-200:]}")
            success = False
            continue

        log.info(f"  Part {i+1}: {os.path.getsize(part_path)/1024/1024:.1f} MB")
        ok = await _upload_one(part_path, f"🎬 {title}\n📦 Part {i+1}/{num_parts}", reply_to)
        if not ok:
            success = False
        try:
            os.remove(part_path)
        except Exception:
            pass
        if i < num_parts - 1:
            await asyncio.sleep(UPLOAD_DELAY)

    try:
        os.remove(input_path)
    except Exception:
        pass
    return success


# ─────────────────────────────────────────────
#  PROCESSING
# ─────────────────────────────────────────────

async def process_movie(tg_bot, tl_client, page_url: str) -> bool:
    log.info(f"\n{'=' * 60}")
    log.info(f"Processing: {page_url}")

    details = scrape_movie_details(page_url)
    m3u8_url, headers = get_m3u8_and_headers(page_url)
    if not m3u8_url:
        log.error("  No m3u8 found. Skipping.")
        return False

    thumb_msg_id = await post_thumbnail(tg_bot, details)
    await asyncio.sleep(2)

    local_path = download_hls(m3u8_url, details["title"], headers)
    if not local_path:
        log.error("  Download failed.")
        return False

    success = await upload_video(tl_client, local_path, details["title"], thumb_msg_id)
    if success:
        save_to_log(page_url)
        log.info(f"  ✅ Done: {details['title']}")
    return success


async def process_queue_worker(tg_bot, tl_client):
    log.info("Link queue worker started.")
    while True:
        item = await PROCESS_QUEUE.get()
        url = item["url"]
        chat_id = item.get("source_chat_id")
        try:
            if chat_id:
                await tg_bot.send_message(chat_id=chat_id, text=f"✅ Started processing:\n{url}")

            ok = await process_movie(tg_bot, tl_client, url)

            if chat_id:
                if ok:
                    await tg_bot.send_message(chat_id=chat_id, text=f"🎉 Done:\n{url}")
                else:
                    await tg_bot.send_message(chat_id=chat_id, text=f"❌ Failed to process:\n{url}")
        except Exception as e:
            log.error(f"Queue worker error for {url}: {e}")
            if chat_id:
                try:
                    await tg_bot.send_message(chat_id=chat_id, text=f"❌ Error while processing:\n{url}\n\n{e}")
                except Exception:
                    pass
        finally:
            await dequeue_done(url)
            PROCESS_QUEUE.task_done()
            await asyncio.sleep(UPLOAD_DELAY)


# ─────────────────────────────────────────────
#  DAILY JOB
# ─────────────────────────────────────────────

async def run_daily_job():
    log.info(f"\n{'#' * 60}")
    log.info(f"Daily job @ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    uploaded = load_uploaded_log()
    new_movies = [u for u in VIDEO_PAGES if u not in uploaded]
    log.info(f"Queued: {len(VIDEO_PAGES)} | New: {len(new_movies)} | Already done: {len(uploaded)}")

    if DAILY_LIMIT > 0 and len(new_movies) > DAILY_LIMIT:
        log.info(f"Daily limit: processing {DAILY_LIMIT} of {len(new_movies)}.")
        new_movies = new_movies[:DAILY_LIMIT]

    queued = 0
    for page_url in new_movies:
        try:
            added = await enqueue_url(page_url)
            if added:
                queued += 1
        except Exception as e:
            log.error(f"  Error queueing {page_url}: {e}")

    log.info(f"Daily job queued: {queued}/{len(new_movies)} movies.")


async def scheduler_loop():
    await run_daily_job()
    while True:
        now = datetime.utcnow()
        secs = (((RUN_HOUR - now.hour) % 24) * 3600 + ((RUN_MINUTE - now.minute) % 60) * 60 - now.second)
        if secs <= 0:
            secs += 86400
        log.info(f"Next scheduled run in {secs // 3600}h {(secs % 3600) // 60}m")
        await asyncio.sleep(secs)
        await run_daily_job()


# ─────────────────────────────────────────────
#  TELEGRAM LINK LISTENER
# ─────────────────────────────────────────────

async def setup_link_listener(tg_bot, tl_client):
    @tl_client.on(events.NewMessage(incoming=True))
    async def handle_new_message(event):
        try:
            if not event.is_private:
                return

            sender_id = event.sender_id
            if not is_allowed_sender(sender_id):
                await event.reply("❌ You are not allowed to use this bot.")
                return

            text = event.raw_text or ""
            url = extract_first_url(text)
            if not url:
                await event.reply("Send me a web link and I will start processing it on Railway.")
                return

            uploaded = load_uploaded_log()
            if url in uploaded:
                await event.reply("ℹ️ This link was already processed before.")
                return

            added = await enqueue_url(url, source_chat_id=event.chat_id, source_message_id=event.id)
            if not added:
                await event.reply("⏳ This link is already in the queue.")
                return

            queue_size = PROCESS_QUEUE.qsize()
            await event.reply(f"📥 Link received and queued. Queue size: {queue_size}\n\n{url}")
        except Exception as e:
            log.error(f"Telegram handler error: {e}")
            try:
                await event.reply(f"❌ Error: {e}")
            except Exception:
                pass


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

async def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise SystemExit("❌ Set BOT_TOKEN env var.")
    if CHANNEL_ID == "@your_channel_here":
        raise SystemExit("❌ Set CHANNEL_ID env var.")
    if not ALLOW_ALL_USERS and not OWNER_USER_ID:
        raise SystemExit("❌ Set OWNER_USER_ID or ALLOW_ALL_USERS=true.")

    tg_bot = telegram.Bot(token=BOT_TOKEN)
    tl_client = TelegramClient("bot_session", API_ID, API_HASH)
    await tl_client.start(bot_token=BOT_TOKEN)
    log.info("Telethon client connected.")

    global TL_CHANNEL
    try:
        chan = CHANNEL_ID
        if isinstance(chan, str) and chan.lstrip("-").isdigit():
            chan = int(chan)
        TL_CHANNEL = await tl_client.get_entity(chan)
        log.info(f"Resolved channel: {TL_CHANNEL.id}")
    except Exception as e:
        log.error(f"Could not resolve channel: {e}")
        TL_CHANNEL = int(str(CHANNEL_ID).replace("-100", "").replace("-", ""))
        log.info(f"Using raw channel ID: {TL_CHANNEL}")

    await setup_link_listener(tg_bot, tl_client)

    log.info(f"Bot started. Scheduled daily at {RUN_HOUR:02d}:{RUN_MINUTE:02d} UTC.")
    log.info(f"Channel: {CHANNEL_ID} | Movies queued: {len(VIDEO_PAGES)} | Daily limit: {DAILY_LIMIT}")
    log.info("Send a web link to the bot in Telegram private chat to start processing immediately.")

    worker_task = asyncio.create_task(process_queue_worker(tg_bot, tl_client))
    scheduler_task = asyncio.create_task(scheduler_loop())

    try:
        await tl_client.run_until_disconnected()
    finally:
        worker_task.cancel()
        scheduler_task.cancel()
        await asyncio.gather(worker_task, scheduler_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
