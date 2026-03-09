
#!/usr/bin/env python3
"""
Telegram link-trigger video bot (trigger-only, split-upload, faster)

Changes
- Trigger-only bot: no scheduler, no startup jobs
- Normalizes Kurdfilm /view/m/... links to /w/movie/<id>
- Extracts m3u8 from page source + Chrome netlog
- Prefers media playlist; master fallback with HD selection when accessible
- Downloads via ffmpeg without re-encoding
- Mobile-safe remux for Telegram playback
- Splits large files before upload to avoid Telethon "invalid file parts" errors
- Uses max Telegram-safe upload chunking via Telethon
"""

import os
import re
import json
import time
import math
import asyncio
import logging
import subprocess
from urllib.parse import urlparse, parse_qs, unquote, urljoin

import requests
from bs4 import BeautifulSoup
import telegram
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

TL_CHANNEL = None

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@your_channel_here")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")

OWNER_USER_ID = os.environ.get("OWNER_USER_ID", "").strip()
ALLOW_ALL_USERS = os.environ.get("ALLOW_ALL_USERS", "false").strip().lower() == "true"

DOWNLOAD_FOLDER = os.environ.get("DOWNLOAD_FOLDER", "/tmp/downloads")
CHROME_BIN = os.environ.get("CHROME_BIN", "/usr/bin/google-chrome")
CHROMEDRIVER_BIN = os.environ.get("CHROMEDRIVER_BIN", "/usr/local/bin/chromedriver")
NET_LOG_PATH = os.environ.get("NET_LOG_PATH", "/tmp/chrome_netlog.json")

M3U8_WAIT_SECONDS = int(os.environ.get("M3U8_WAIT_SECONDS", "12"))
MAX_SINGLE_UPLOAD_MB = int(os.environ.get("MAX_SINGLE_UPLOAD_MB", "1900"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

link_queue: asyncio.Queue = asyncio.Queue()
queue_seen = set()

def is_allowed_user(user_id: int) -> bool:
    return ALLOW_ALL_USERS or bool(OWNER_USER_ID and str(user_id) == OWNER_USER_ID)

def normalize_input_url(url: str) -> str:
    m = re.search(r'https?://kurdfilm\.krd/view/m/(\d+)', url, re.IGNORECASE)
    if m:
        return f"https://kurdfilm.krd/w/movie/{m.group(1)}"
    m = re.search(r'https?://kurdfilm\.krd/w/movie/(\d+)', url, re.IGNORECASE)
    if m:
        return f"https://kurdfilm.krd/w/movie/{m.group(1)}"
    return url

def extract_urls(text: str):
    if not text:
        return []
    raw = re.findall(r'https?://\S+', text)
    return [normalize_input_url(u.rstrip(").,]}>")) for u in raw]

def sanitize_filename(name: str) -> str:
    safe = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return safe or "video"

def ffmpeg_headers_blob(headers: dict) -> str:
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items() if v)

def resolve_url(base_url: str, maybe_relative: str) -> str:
    return maybe_relative if maybe_relative.startswith(("http://", "https://")) else urljoin(base_url, maybe_relative)

def scrape_movie_details(page_url: str) -> dict:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    details = {
        "title": "Unknown", "year": "", "duration": "", "category": "",
        "thumbnail_url": "", "page_url": page_url,
    }
    try:
        resp = session.get(page_url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for sel in ["h1.movie-title", "h1", ".title", "[class*='title']"]:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                text = re.sub(r'\s*[\|\-–]\s*.*$', "", el.get_text(strip=True)).strip()
                if text:
                    details["title"] = text
                    break
        if details["title"] == "Unknown":
            t = soup.find("title")
            if t:
                text = re.sub(r'\s*[\|\-–]\s*.*$', "", t.get_text(strip=True)).strip()
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
        m = (re.search(r'\b(\d{1,2}:\d{2}:\d{2})\b', full_text) or
             re.search(r'\b(\d{2,3})\s*(?:min|دەقیقە|دقیقه)', full_text, re.IGNORECASE))
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
    lines.append(f"🔗 [Watch on site]({details['page_url']})")
    return "\n".join(lines)

def find_player_referer(driver, page_url: str) -> str:
    try:
        iframes = driver.find_elements("tag name", "iframe")
        for iframe in iframes:
            src = iframe.get_attribute("src")
            if src and src.startswith("http"):
                return src
    except Exception:
        pass
    return page_url

def extract_m3u8_from_html(html: str, base_url: str):
    urls = re.findall(r'https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*', html)
    resolved, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            resolved.append(u)
    rels = re.findall(r'["\']([^"\']+\.m3u8[^"\']*)["\']', html)
    for r in rels:
        full = resolve_url(base_url, r)
        if full not in seen:
            seen.add(full)
            resolved.append(full)
    return resolved

def parse_netlog_urls(netlog_path: str):
    if not os.path.exists(netlog_path):
        return []
    try:
        with open(netlog_path, "r", errors="replace") as f:
            content = f.read()
    except Exception as e:
        log.error(f"  Net log read error: {e}")
        return []
    urls = re.findall(r'https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*', content)
    if urls:
        seen, ordered = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                ordered.append(u)
        return ordered
    for ping_url in re.findall(r'https?://[^\s"\'\\]*jwpltx\.com[^\s"\'\\]*ping\.gif[^\s"\'\\]*', content):
        mu = parse_qs(urlparse(ping_url).query).get("mu", [None])[0]
        if mu:
            return [unquote(mu)]
    return []

def choose_playlists(urls):
    if not urls:
        return None, None
    media_candidates = [u for u in urls if "master.m3u8" not in u.lower()]
    master_candidates = [u for u in urls if "master.m3u8" in u.lower()]
    media_url = media_candidates[-1] if media_candidates else None
    master_url = master_candidates[-1] if master_candidates else None
    if media_url:
        log.info(f"  ✅ media m3u8: {media_url[:100]}...")
    if master_url:
        log.info(f"  ✅ master m3u8: {master_url[:100]}...")
    return media_url, master_url

def get_m3u8_candidates_and_headers(page_url: str):
    page_url = normalize_input_url(page_url)
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
    chrome_options.add_argument(f"--user-agent={USER_AGENT}")
    chrome_options.add_argument(f"--log-net-log={NET_LOG_PATH}")
    chrome_options.add_argument("--net-log-capture-mode=IncludeSocketBytes")

    service = Service(executable_path=CHROMEDRIVER_BIN)
    driver = webdriver.Chrome(service=service, options=chrome_options)
    log.info("  ✅ Chrome started.")

    cookies = []
    referer_url = page_url
    html_urls = []

    try:
        driver.get(page_url)
        log.info(f"  Waiting {M3U8_WAIT_SECONDS}s for player...")
        time.sleep(M3U8_WAIT_SECONDS)
        cookies = driver.get_cookies()
        referer_url = find_player_referer(driver, page_url)
        try:
            html = driver.page_source or ""
            html_urls = extract_m3u8_from_html(html, page_url)
            if html_urls:
                log.info(f"  Found {len(html_urls)} m3u8 URL(s) in page source.")
        except Exception as e:
            log.warning(f"  HTML m3u8 extraction failed: {e}")
        log.info(f"  Captured {len(cookies)} cookies.")
        log.info(f"  Player referer: {referer_url}")
    except Exception as e:
        log.error(f"  Selenium error: {e}")
    finally:
        driver.quit()
        log.info("  Chrome closed. Parsing net log...")

    netlog_urls = parse_netlog_urls(NET_LOG_PATH)
    all_urls, seen = [], set()
    for u in html_urls + netlog_urls:
        if u not in seen:
            seen.add(u)
            all_urls.append(u)

    media_url, master_url = choose_playlists(all_urls)
    parsed = urlparse(referer_url)
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": referer_url,
        "Origin": f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str
    return media_url, master_url, headers

def parse_master_variants(master_text: str, master_url: str):
    variants = []
    lines = master_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            width = height = bandwidth = 0
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            if m:
                width, height = int(m.group(1)), int(m.group(2))
            m = re.search(r"BANDWIDTH=(\d+)", line)
            if m:
                bandwidth = int(m.group(1))
            if i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt and not nxt.startswith("#"):
                    variants.append({
                        "width": width, "height": height, "bandwidth": bandwidth,
                        "url": resolve_url(master_url, nxt),
                    })
    return variants

def choose_best_hd_variant(variants):
    if not variants:
        return None
    variants = sorted(variants, key=lambda v: (v.get("height", 0), v.get("bandwidth", 0)), reverse=True)
    for target in (1080, 720):
        candidates = [v for v in variants if v.get("height", 0) == target]
        if candidates:
            return sorted(candidates, key=lambda v: v.get("bandwidth", 0), reverse=True)[0]
    return variants[0]

def fetch_master_and_choose_variant(master_url: str, headers: dict):
    session = requests.Session()
    session.headers.update(headers)
    try:
        r = session.get(master_url, timeout=30)
        r.raise_for_status()
        variants = parse_master_variants(r.text, master_url)
        chosen = choose_best_hd_variant(variants)
        if chosen:
            log.info(
                "  Selected variant: %sx%s | %.2f Mbps",
                chosen.get("width", 0), chosen.get("height", 0),
                chosen.get("bandwidth", 0) / 1000000 if chosen.get("bandwidth", 0) else 0,
            )
        return chosen
    except Exception as e:
        log.warning(f"  Master playlist parse failed: {e}")
        return None

def download_hls_ffmpeg(m3u8_url: str, title: str, headers: dict) -> str | None:
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    safe = sanitize_filename(title)
    output = os.path.join(DOWNLOAD_FOLDER, f"{safe}.mp4")
    header_blob = ffmpeg_headers_blob(headers)
    cmd = [
        "ffmpeg", "-y",
        "-rw_timeout", "30000000",
        "-headers", header_blob,
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        output,
    ]
    log.info("  Downloading via ffmpeg...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        log.error(f"  ffmpeg download failed: {res.stderr[-700:]}")
        return None
    if not os.path.exists(output) or os.path.getsize(output) == 0:
        log.error("  ffmpeg produced no output file.")
        return None
    log.info(f"  ✅ Downloaded via ffmpeg: {output} ({os.path.getsize(output)/1024/1024:.1f} MB)")
    return output

def probe_video(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,codec_name",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"width": 0, "height": 0, "duration": 0, "codec_name": ""}
    try:
        data = json.loads(res.stdout)
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        duration = stream.get("duration") or fmt.get("duration") or 0
        return {
            "width": int(float(stream.get("width", 0) or 0)),
            "height": int(float(stream.get("height", 0) or 0)),
            "duration": int(float(duration or 0)),
            "codec_name": stream.get("codec_name", ""),
        }
    except Exception:
        return {"width": 0, "height": 0, "duration": 0, "codec_name": ""}

def normalize_mp4_for_mobile(input_path: str) -> str:
    meta = probe_video(input_path)
    codec = (meta.get("codec_name") or "").lower()
    temp_out = os.path.splitext(input_path)[0] + "_mobile.mp4"
    if codec == "h264":
        cmd = [
            "ffmpeg", "-y", "-i", input_path, "-map", "0",
            "-c", "copy",
            "-bsf:v", "h264_metadata=sample_aspect_ratio=1/1",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            temp_out,
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", input_path, "-map", "0",
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            temp_out,
        ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not os.path.exists(temp_out) or os.path.getsize(temp_out) == 0:
        log.warning("  Mobile-safe remux failed; using original file.")
        try:
            if os.path.exists(temp_out):
                os.remove(temp_out)
        except Exception:
            pass
        return input_path
    try:
        os.remove(input_path)
    except Exception:
        pass
    log.info("  ✅ Mobile-safe remux completed.")
    return temp_out

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

async def notify_user(app: Application | None, chat_id: int | None, text: str):
    if app is None or chat_id is None:
        return
    try:
        await app.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        log.warning(f"  Notify failed: {e}")

def split_video_by_duration(input_path: str, max_bytes: int) -> list[str]:
    size = os.path.getsize(input_path)
    if size <= max_bytes:
        return [input_path]

    meta = probe_video(input_path)
    duration = max(int(meta.get("duration", 0)), 1)
    parts = max(2, math.ceil(size / max_bytes))
    segment_duration = max(1, math.ceil(duration / parts))

    base, ext = os.path.splitext(input_path)
    outputs = []

    log.info(f"  File exceeds safe upload size; splitting into {parts} part(s)...")
    for i in range(parts):
        part_path = f"{base}_part{i+1}of{parts}{ext}"
        start = i * segment_duration
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", input_path,
            "-t", str(segment_duration),
            "-c", "copy",
            "-movflags", "+faststart",
            part_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not os.path.exists(part_path) or os.path.getsize(part_path) == 0:
            raise RuntimeError(f"ffmpeg split failed for part {i+1}: {res.stderr[-300:]}")
        outputs.append(part_path)

    try:
        os.remove(input_path)
    except Exception:
        pass
    return outputs

async def upload_one_file(tl_client, path: str, caption: str, reply_to: int | None) -> bool:
    meta = probe_video(path)
    width = meta.get("width", 0)
    height = meta.get("height", 0)
    duration = meta.get("duration", 0)
    size_mb = os.path.getsize(path) / 1024 / 1024

    log.info(f"  Uploading {os.path.basename(path)} ({size_mb:.1f} MB) | {width}x{height} | {duration}s")
    try:
        await tl_client.send_file(
            TL_CHANNEL,
            path,
            caption=caption,
            reply_to=reply_to,
            supports_streaming=True,
            part_size_kb=512,
            attributes=[
                DocumentAttributeVideo(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True,
                )
            ],
        )
        log.info(f"  ✅ Uploaded: {os.path.basename(path)}")
        return True
    except Exception as e:
        log.error(f"  Upload error: {e}")
        return False
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

async def upload_video(tl_client, input_path: str, title: str, reply_to: int | None) -> bool:
    max_bytes = MAX_SINGLE_UPLOAD_MB * 1024 * 1024
    try:
        parts = split_video_by_duration(input_path, max_bytes)
    except Exception as e:
        log.error(f"  Split failed: {e}")
        return False

    if len(parts) == 1:
        return await upload_one_file(tl_client, parts[0], f"🎬 {title}", reply_to)

    ok_all = True
    for idx, part in enumerate(parts, start=1):
        ok = await upload_one_file(
            tl_client,
            part,
            f"🎬 {title}\n📦 Part {idx}/{len(parts)}",
            reply_to,
        )
        if not ok:
            ok_all = False
            break
    return ok_all

async def process_movie(tg_bot, tl_client, page_url: str, app: Application | None = None, notify_chat_id: int | None = None) -> bool:
    page_url = normalize_input_url(page_url)
    log.info(f"\n{'='*60}")
    log.info(f"Processing: {page_url}")

    details = scrape_movie_details(page_url)
    media_url, master_url, headers = get_m3u8_candidates_and_headers(page_url)
    if not media_url and not master_url:
        log.error("  No m3u8 found. Skipping.")
        await notify_user(app, notify_chat_id, "❌ Could not detect a playable stream for this link.")
        return False

    chosen_url = media_url
    if master_url:
        chosen = fetch_master_and_choose_variant(master_url, headers)
        if chosen and chosen.get("url"):
            chosen_url = chosen["url"]
    if not chosen_url and master_url:
        chosen_url = master_url
    if not chosen_url:
        log.error("  No playable HLS URL chosen.")
        await notify_user(app, notify_chat_id, "❌ Could not choose a playable HLS stream.")
        return False

    await notify_user(app, notify_chat_id, "⏳ Processing started...")
    thumb_msg_id = await post_thumbnail(tg_bot, details)
    await asyncio.sleep(1)

    local_path = download_hls_ffmpeg(chosen_url, details["title"], headers)
    if not local_path and media_url and chosen_url != media_url:
        log.warning("  Retry with browser-captured media playlist...")
        local_path = download_hls_ffmpeg(media_url, details["title"], headers)
    if not local_path and master_url and chosen_url != master_url:
        log.warning("  Retry with master playlist...")
        local_path = download_hls_ffmpeg(master_url, details["title"], headers)
    if not local_path:
        log.error("  Download failed.")
        await notify_user(app, notify_chat_id, "❌ Download failed. The site blocked replay or returned a protected stream.")
        return False

    local_path = normalize_mp4_for_mobile(local_path)
    success = await upload_video(tl_client, local_path, details["title"], thumb_msg_id)

    if success:
        log.info(f"  ✅ Done: {details['title']}")
        await notify_user(app, notify_chat_id, f"✅ Finished: {details['title']}")
    else:
        await notify_user(app, notify_chat_id, f"❌ Upload failed: {details['title']}")
    return success

async def enqueue_link(url: str, source_chat_id: int | None = None):
    url = normalize_input_url(url)
    key = (url, source_chat_id)
    if key in queue_seen:
        return False
    queue_seen.add(key)
    await link_queue.put({"url": url, "chat_id": source_chat_id})
    return True

async def link_queue_worker(app: Application, tg_bot, tl_client):
    log.info("Link queue worker started.")
    while True:
        job = await link_queue.get()
        url = job["url"]
        chat_id = job.get("chat_id")
        key = (url, chat_id)
        try:
            await process_movie(tg_bot, tl_client, url, app=app, notify_chat_id=chat_id)
        except Exception as e:
            log.error(f"Queue worker error for {url}: {e}")
            await notify_user(app, chat_id, f"❌ Error while processing:\n{url}\n\n{e}")
        finally:
            queue_seen.discard(key)
            link_queue.task_done()

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_chat or not update.message:
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if update.effective_chat.type != "private":
        return
    if not is_allowed_user(user_id):
        await update.message.reply_text("❌ You are not allowed to use this bot.")
        return
    urls = extract_urls(update.message.text or "")
    if not urls:
        await update.message.reply_text("Send a web link in private chat.")
        return
    log.info(f"Telegram link received from {user_id}: {urls}")
    added = 0
    for url in urls:
        ok = await enqueue_link(url, chat_id)
        if ok:
            log.info(f"Queued from Telegram: {url}")
            added += 1
    if added:
        await update.message.reply_text(f"✅ Queued {added} link(s).")
    else:
        await update.message.reply_text("ℹ️ That link is already being processed.")

async def main():
    global TL_CHANNEL
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Set BOT_TOKEN env var.")
        raise SystemExit(1)
    if CHANNEL_ID == "@your_channel_here":
        print("❌ Set CHANNEL_ID env var.")
        raise SystemExit(1)
    if API_ID == 0 or not API_HASH:
        print("❌ Set API_ID and API_HASH env vars.")
        raise SystemExit(1)
    if not ALLOW_ALL_USERS and not OWNER_USER_ID:
        print("❌ Set OWNER_USER_ID or ALLOW_ALL_USERS=true.")
        raise SystemExit(1)

    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    tg_bot = telegram.Bot(token=BOT_TOKEN)

    tl_client = TelegramClient("bot_session", API_ID, API_HASH)
    await tl_client.start(bot_token=BOT_TOKEN)
    log.info("Telethon client connected.")

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

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    worker_task = asyncio.create_task(link_queue_worker(app, tg_bot, tl_client))
    log.info("Bot started in trigger-only mode.")
    log.info("Send a web link to the bot in Telegram private chat to start processing.")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        worker_task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await tl_client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
