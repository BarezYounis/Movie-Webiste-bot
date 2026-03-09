#!/usr/bin/env python3
"""
ZedFlix → Telegram Bot (Final)
- Daily cron scheduler (always-on Railway service)
- Crawls all pages of your site automatically
- Scrapes movie details + thumbnail
- Posts thumbnail + caption, then uploads video as reply
- Downloads HLS via requests on same IP as Chrome (bypasses CDN token lock)
- Splits files > 1.8 GB one part at a time (saves disk space)
- Retries failed uploads 3 times
"""

import os
import re
import json
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
from telegram.error import TelegramError
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ─────────────────────────────────────────────
#  CONFIGURATION  (set as env vars on Railway)
# ─────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN",    "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID   = os.environ.get("CHANNEL_ID",   "@your_channel_here")
API_ID       = int(os.environ.get("API_ID",   "0"))   # from my.telegram.org
API_HASH     = os.environ.get("API_HASH",  "")         # from my.telegram.org
_pages_env  = os.environ.get("VIDEO_PAGES", "")
VIDEO_PAGES  = [u.strip() for u in _pages_env.split(",") if u.strip()]

RUN_HOUR     = int(os.environ.get("RUN_HOUR",    "2"))
RUN_MINUTE   = int(os.environ.get("RUN_MINUTE",  "0"))
DAILY_LIMIT  = int(os.environ.get("DAILY_LIMIT", "20"))   # 0 = unlimited

DOWNLOAD_FOLDER  = os.environ.get("DOWNLOAD_FOLDER", "/tmp/downloads")
UPLOADED_LOG     = os.environ.get("UPLOADED_LOG",    "/tmp/uploaded_movies.txt")
UPLOAD_DELAY     = int(os.environ.get("UPLOAD_DELAY", "5"))
PART_SIZE_BYTES  = int(os.environ.get("PART_SIZE_MB", "1900")) * 1024 * 1024

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
#  SITE CRAWLER
# ─────────────────────────────────────────────

def crawl_movie_pages(site_url: str) -> list:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    movie_urls = []
    visited    = set()
    page       = 1
    log.info(f"Crawling: {site_url}")

    while True:
        candidates = [
            site_url if page == 1 else None,
            f"{site_url}/?page={page}",
            f"{site_url}/page/{page}",
        ]
        fetched = False
        for url in candidates:
            if not url or url in visited:
                continue
            try:
                resp = session.get(url, timeout=20)
                if resp.status_code != 200:
                    continue
                visited.add(url)
                soup = BeautifulSoup(resp.text, "html.parser")
                found = 0
                for a in soup.find_all("a", href=True):
                    full = urljoin(site_url, a["href"])
                    if re.search(r'/w/movie/\d+', full) and full not in movie_urls:
                        movie_urls.append(full)
                        found += 1
                log.info(f"  Page {page}: {found} movies (total: {len(movie_urls)})")
                if found == 0:
                    log.info("  No more movies. Crawl done.")
                    return movie_urls
                fetched = True
                break
            except Exception as e:
                log.warning(f"  Could not fetch {url}: {e}")
        if not fetched:
            break
        page += 1
        time.sleep(1)

    log.info(f"Found {len(movie_urls)} movie pages total.")
    return movie_urls


# ─────────────────────────────────────────────
#  MOVIE DETAIL SCRAPER
# ─────────────────────────────────────────────

def scrape_movie_details(page_url: str) -> dict:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    details = {"title": "Unknown", "year": "", "duration": "", "category": "", "thumbnail_url": "", "page_url": page_url}

    try:
        resp = session.get(page_url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Title
        for sel in ["h1.movie-title", "h1", ".title", "[class*='title']"]:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                text = re.sub(r'\s*[\|\-–]\s*.*$', '', el.get_text(strip=True)).strip()
                if text:
                    details["title"] = text
                    break
        # Fallback to <title> tag
        if details["title"] == "Unknown":
            t = soup.find("title")
            if t:
                text = re.sub(r'\s*[\|\-–]\s*.*$', '', t.get_text(strip=True)).strip()
                if text:
                    details["title"] = text

        # Thumbnail — og:image is most reliable
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

        # Year
        m = re.search(r'\b(19[5-9]\d|20[0-3]\d)\b', full_text)
        if m:
            details["year"] = m.group(1)

        # Duration
        m = (re.search(r'\b(\d{1,2}:\d{2}:\d{2})\b', full_text) or
             re.search(r'\b(\d{2,3})\s*(?:min|دەقیقە|دقیقه)', full_text, re.IGNORECASE))
        if m:
            details["duration"] = m.group(1)

        # Category
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
    if details["year"]:      lines.append(f"📅 {details['year']}")
    if details["duration"]:  lines.append(f"⏱ {details['duration']}")
    if details["category"]:  lines.append(f"🎭 {details['category']}")
    lines.append(f"🔗 [Watch on ZedFlix]({details['page_url']})")
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  SELENIUM — capture m3u8 URL + session headers
#  KEY: we keep the headers from Chrome's session
#  so that requests downloads from the SAME IP
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

    # Prefer master.m3u8
    for url in re.findall(r'https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*', content):
        if "master.m3u8" in url:
            log.info(f"  ✅ master.m3u8: {url[:80]}...")
            return url

    # Fallback: any m3u8
    matches = re.findall(r'https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*', content)
    if matches:
        log.info(f"  ✅ m3u8: {matches[0][:80]}...")
        return matches[0]

    # Fallback: JW ping mu= param
    for ping_url in re.findall(r'https?://[^\s"\'\\]*jwpltx\.com[^\s"\'\\]*ping\.gif[^\s"\'\\]*', content):
        mu = parse_qs(urlparse(ping_url).query).get("mu", [None])[0]
        if mu:
            url = unquote(mu)
            log.info(f"  ✅ m3u8 from ping: {url[:80]}...")
            return url

    return None


def get_m3u8_and_headers(page_url: str):
    """
    Opens page in headless Chrome, waits for JW Player,
    captures m3u8 URL from net-log + session cookies.
    Returns (m3u8_url, headers_dict) where headers match Chrome's session.
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
    # Write ALL network traffic to file — most reliable capture method
    chrome_options.add_argument(f"--log-net-log={NET_LOG_PATH}")
    chrome_options.add_argument("--net-log-capture-mode=IncludeSocketBytes")

    service = Service(executable_path=CHROMEDRIVER_BIN)
    driver  = webdriver.Chrome(service=service, options=chrome_options)
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

    parsed     = urlparse(page_url)
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

    # These headers MUST match what Chrome sent — the CDN validates Referer/Origin
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer":         page_url,
        "Origin":          f"{parsed.scheme}://{parsed.netloc}",
        "Accept":          "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection":      "keep-alive",
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "cross-site",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str

    return m3u8_url, headers


# ─────────────────────────────────────────────
#  DOWNLOAD HLS — uses requests with Chrome headers
#  This runs on the SAME server IP as Chrome,
#  so the CDN signed token is valid
# ─────────────────────────────────────────────

def parse_m3u8(content: str, base_url: str):
    """Parse m3u8 content. Returns media playlist URL (str) or segment list (list)."""
    if "#EXT-X-STREAM-INF" in content:
        best_bw  = 0
        best_url = None
        lines    = content.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                m  = re.search(r'BANDWIDTH=(\d+)', line)
                bw = int(m.group(1)) if m else 0
                if bw >= best_bw and i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if nxt and not nxt.startswith("#"):
                        best_bw  = bw
                        best_url = nxt
        if best_url:
            if not best_url.startswith("http"):
                best_url = base_url.rsplit("/", 1)[0] + "/" + best_url
            return best_url  # string = need to fetch this playlist
        return []

    segments = []
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            segments.append(line if line.startswith("http") else base_url.rsplit("/", 1)[0] + "/" + line)
    return segments


def download_hls(m3u8_url: str, title: str, headers: dict) -> str | None:
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    safe       = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "video"
    segs_dir   = os.path.join(DOWNLOAD_FOLDER, f"{safe}_segs")
    os.makedirs(segs_dir, exist_ok=True)
    output     = os.path.join(DOWNLOAD_FOLDER, f"{safe}.mp4")

    session = requests.Session()
    session.headers.update(headers)

    # Fetch master playlist
    log.info("  Fetching master m3u8...")
    try:
        r = session.get(m3u8_url, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log.error(f"  Master m3u8 fetch failed: {e}")
        shutil.rmtree(segs_dir, ignore_errors=True)
        return None

    result = parse_m3u8(r.text, m3u8_url)

    # If master playlist, fetch media playlist
    if isinstance(result, str):
        media_url = result
        log.info(f"  Fetching media playlist: {media_url[:80]}...")
        try:
            r = session.get(media_url, timeout=30)
            r.raise_for_status()
            segments = parse_m3u8(r.text, media_url)
        except Exception as e:
            log.error(f"  Media playlist fetch failed: {e}")
            shutil.rmtree(segs_dir, ignore_errors=True)
            return None
    else:
        segments = result

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
            except Exception:
                if attempt == 2:
                    log.warning(f"  Skipping segment {idx} after 3 failures")
        if idx % 50 == 0:
            log.info(f"  Progress: {idx}/{len(segments)} segments")

    log.info(f"  Downloaded {len(seg_files)}/{len(segments)} segments. Merging...")

    concat = os.path.join(segs_dir, "concat.txt")
    with open(concat, "w") as f:
        for sf in seg_files:
            f.write(f"file '{sf}'\n")

    res = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat, "-c", "copy", output],
        capture_output=True, text=True
    )
    shutil.rmtree(segs_dir, ignore_errors=True)
    log.info("  Segments cleaned up.")

    if res.returncode != 0:
        log.error(f"  ffmpeg error: {res.stderr[-300:]}")
        return None

    log.info(f"  ✅ {output} ({os.path.getsize(output)/1024/1024:.1f} MB)")
    return output


# ─────────────────────────────────────────────
#  TELEGRAM — post thumbnail then video reply
# ─────────────────────────────────────────────

async def post_thumbnail(tg_bot, details: dict) -> int | None:
    """Post thumbnail + caption using python-telegram-bot (no size limit for photos)."""
    caption = format_caption(details)
    try:
        if details["thumbnail_url"]:
            r = requests.get(details["thumbnail_url"], timeout=15)
            r.raise_for_status()
            msg = await tg_bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=r.content,
                caption=caption,
                parse_mode="Markdown",
            )
        else:
            msg = await tg_bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode="Markdown",
            )
        log.info(f"  ✅ Thumbnail posted (msg_id={msg.message_id})")
        return msg.message_id
    except Exception as e:
        log.error(f"  Thumbnail post failed: {e}")
        return None


async def upload_video(tl_client, input_path: str, title: str, reply_to: int | None) -> bool:
    """
    Upload video using Telethon (MTProto) — supports files up to 2 GB natively.
    No splitting needed for files under 2 GB.
    For files over 2 GB, splits one part at a time.
    """
    file_size = os.path.getsize(input_path)
    log.info(f"  Uploading {os.path.basename(input_path)} ({file_size/1024/1024:.1f} MB) via Telethon...")

    TWO_GB = 2 * 1024 * 1024 * 1024

    async def _upload_one(path: str, caption: str, reply_id: int | None) -> bool:
        for attempt in range(1, 4):
            try:
                log.info(f"  Telethon upload attempt {attempt}/3: {os.path.basename(path)}")
                await tl_client.send_file(
                    CHANNEL_ID,
                    path,
                    caption=caption,
                    reply_to=reply_id,
                    supports_streaming=True,
                    progress_callback=lambda c, t: log.info(f"    Upload progress: {c/t*100:.1f}%") if t and c % (50*1024*1024) < 1*1024*1024 else None,
                )
                log.info(f"  ✅ Uploaded: {os.path.basename(path)}")
                return True
            except Exception as e:
                log.error(f"  Upload error (attempt {attempt}): {e}")
                if attempt < 3:
                    await asyncio.sleep(30 * attempt)
        return False

    # File fits in one upload
    if file_size <= TWO_GB:
        ok = await _upload_one(input_path, f"🎬 {title}", reply_to)
        try:
            os.remove(input_path)
        except Exception:
            pass
        return ok

    # File > 2 GB — split one part at a time
    log.info(f"  {file_size/1024/1024/1024:.2f} GB > 2 GB — splitting one part at a time...")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_path],
        capture_output=True, text=True
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        return await _upload_one(input_path, f"🎬 {title}", reply_to)

    num_parts     = max(2, -(-file_size // PART_SIZE_BYTES))
    part_duration = duration / num_parts
    base          = os.path.splitext(input_path)[0]
    success       = True

    for i in range(num_parts):
        part_path = f"{base}_part{i+1}of{num_parts}.mp4"
        log.info(f"  Creating part {i+1}/{num_parts}...")
        res = subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(i * part_duration),
            "-i", input_path,
            "-t", str(part_duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            part_path
        ], capture_output=True, text=True)

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
#  PROCESS ONE MOVIE
# ─────────────────────────────────────────────

async def process_movie(tg_bot, tl_client, page_url: str) -> bool:
    log.info(f"\n{'='*60}")
    log.info(f"Processing: {page_url}")

    # 1. Scrape details + thumbnail
    details = scrape_movie_details(page_url)

    # 2. Get m3u8 URL + session headers via Chrome
    m3u8_url, headers = get_m3u8_and_headers(page_url)
    if not m3u8_url:
        log.error("  No m3u8 found. Skipping.")
        return False

    # 3. Post thumbnail with movie details caption (python-telegram-bot)
    thumb_msg_id = await post_thumbnail(tg_bot, details)
    await asyncio.sleep(2)

    # 4. Download video on server
    local_path = download_hls(m3u8_url, details["title"], headers)
    if not local_path:
        log.error("  Download failed.")
        return False

    # 5. Upload video via Telethon (supports up to 2 GB)
    success = await upload_video(tl_client, local_path, details["title"], thumb_msg_id)

    if success:
        save_to_log(page_url)
        log.info(f"  ✅ Done: {details['title']}")
    return success


# ─────────────────────────────────────────────
#  DAILY JOB
# ─────────────────────────────────────────────

async def run_daily_job(tg_bot, tl_client):
    log.info(f"\n{'#'*60}")
    log.info(f"Daily job @ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    uploaded   = load_uploaded_log()
    new_movies = [u for u in VIDEO_PAGES if u not in uploaded]
    log.info(f"Queued: {len(VIDEO_PAGES)} | New: {len(new_movies)} | Already done: {len(uploaded)}")

    if DAILY_LIMIT > 0 and len(new_movies) > DAILY_LIMIT:
        log.info(f"Daily limit: processing {DAILY_LIMIT} of {len(new_movies)}.")
        new_movies = new_movies[:DAILY_LIMIT]

    success = 0
    for page_url in new_movies:
        try:
            if await process_movie(tg_bot, tl_client, page_url):
                success += 1
        except Exception as e:
            log.error(f"  Error processing {page_url}: {e}")
        await asyncio.sleep(UPLOAD_DELAY)

    log.info(f"Daily job done: {success}/{len(new_movies)} movies uploaded.")


# ─────────────────────────────────────────────
#  SCHEDULER — always-on, fires daily
# ─────────────────────────────────────────────

async def scheduler():
    tg_bot  = telegram.Bot(token=BOT_TOKEN)
    # Telethon client — uses MTProto for large file uploads (up to 2 GB)
    tl_client = TelegramClient("bot_session", API_ID, API_HASH)
    await tl_client.start(bot_token=BOT_TOKEN)
    log.info("Telethon client connected.")

    log.info(f"Bot started. Scheduled daily at {RUN_HOUR:02d}:{RUN_MINUTE:02d} UTC.")
    log.info(f"Channel: {CHANNEL_ID} | Movies queued: {len(VIDEO_PAGES)} | Daily limit: {DAILY_LIMIT}")

    # Run immediately on startup
    await run_daily_job(tg_bot, tl_client)

    while True:
        now = datetime.utcnow()
        secs = (((RUN_HOUR - now.hour) % 24) * 3600
                + ((RUN_MINUTE - now.minute) % 60) * 60
                - now.second)
        if secs <= 0:
            secs += 86400
        log.info(f"Next run in {secs // 3600}h {(secs % 3600) // 60}m")
        await asyncio.sleep(secs)
        await run_daily_job(tg_bot, tl_client)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Set BOT_TOKEN env var.")
        exit(1)
    if CHANNEL_ID == "@your_channel_here":
        print("❌ Set CHANNEL_ID env var.")
        exit(1)
    if not VIDEO_PAGES:
        print("❌ Set VIDEO_PAGES env var (comma-separated URLs).")
        exit(1)
    asyncio.run(scheduler())
