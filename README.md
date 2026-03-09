# Video Bot — Railway Deployment Guide

## What it does
1. Opens each video page in headless Chrome on the server
2. Intercepts the JW Player `.m3u8` stream URL
3. Downloads the full video with `yt-dlp` directly on the server
4. Splits files larger than **1.9 GB** into parts using `ffmpeg`
5. Uploads each part to your Telegram channel
6. Tracks uploaded pages so nothing is re-uploaded

---

## Files
| File | Purpose |
|------|---------|
| `telegram_video_bot.py` | Main bot script |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Installs Chrome + ChromeDriver + ffmpeg |
| `railway.toml` | Railway build/deploy config |

---

## Deploy to Railway

### Step 1 — Push to GitHub
Create a new GitHub repo and push all 4 files:
```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### Step 2 — Create Railway project
1. Go to https://railway.app → **New Project**
2. Choose **Deploy from GitHub repo**
3. Select your repo — Railway will auto-detect the Dockerfile

### Step 3 — Set environment variables
In Railway → your service → **Variables**, add:

| Variable | Value |
|----------|-------|
| `BOT_TOKEN` | Your Telegram bot token from @BotFather |
| `CHANNEL_ID` | `@yourchannel` or `-100xxxxxxxxxx` |
| `VIDEO_PAGES` | Comma-separated URLs, e.g. `https://kurdfilm.krd/w/movie/111,https://kurdfilm.krd/w/movie/222` |
| `PART_SIZE_MB` | `1900` (default — max part size before splitting) |
| `UPLOAD_DELAY` | `5` (seconds between uploads) |

### Step 4 — Deploy
Click **Deploy**. Railway will:
- Build the Docker image (installs Chrome, ChromeDriver, ffmpeg)
- Run the bot once
- Exit when all videos are processed

### Step 5 — Re-run for new videos
To process new videos:
1. Update `VIDEO_PAGES` in Railway Variables to add new URLs
2. Go to **Deployments** → click **Redeploy**

---

## Tips
- Railway free tier has **500 hours/month** and **1 GB RAM** — enough for most videos
- Upgrade to Pro ($5/mo) for more RAM if processing very large files
- Logs are visible in Railway → your service → **Logs** tab
- Already-uploaded URLs are stored in `/tmp/uploaded_videos.txt` on the server
  (resets between deployments — that's fine, just don't re-add old URLs to VIDEO_PAGES)

---

## Telegram Setup
1. Message **@BotFather** → `/newbot` → follow steps → copy the token
2. Add your bot as an **Admin** to your channel (with permission to post)
3. For private channels, use the numeric ID format: `-100xxxxxxxxxx`
   (forward any message from your channel to @userinfobot to get the ID)
