# 🤖 Image Scraper Bot

A powerful Discord image scraper — works like the Imageye Chrome extension but right inside Discord. Scrapes images from any webpage and delivers them as a `.zip` file.

---

## ✨ Features

| Feature | Details |
|---|---|
| **Smart scraping** | `<img>`, `<picture>`, CSS backgrounds, Open Graph, meta tags, `<link>` icons |
| **Cloudflare bypass** | Uses `cloudscraper` with a realistic browser fingerprint |
| **Concurrent downloads** | Up to 20 parallel downloads with a semaphore to avoid hammering servers |
| **Auto-split ZIP** | If the ZIP exceeds Discord's 25 MB limit it's automatically split into multiple parts |
| **Type filter** | Filter by jpg / png / gif / webp / svg / avif |
| **Min-size filter** | Skip tiny tracker pixels & placeholders (default 5 KB) |
| **Filename deduplication** | No more overwritten files inside the ZIP |
| **Preview command** | See images inline before committing to a full download |
| **Progress embeds** | Live status messages with rich Discord embeds |
| **Render-ready** | Includes a health-check HTTP server for Render.com free hosting |

---

## 🚀 Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure your token
Create a `.env` file in the same directory:
```
DISCORD_TOKEN=your_discord_bot_token_here
```

### 3. Run
```bash
python bot.py
```

---

## 📖 Commands

### `/download_images`
Scrape and download all images from a webpage into a ZIP file.

| Option | Description | Default |
|---|---|---|
| `url` | The page to scrape *(required)* | — |
| `min_size` | Skip images below this many KB | `5` |
| `image_type` | Only download this format (jpg/png/gif/webp/svg/avif) | All types |
| `max_count` | Maximum images to download | `100` (max 300) |

### `/preview_images`
Preview the first few images from a URL directly in Discord — no ZIP needed.

| Option | Description | Default |
|---|---|---|
| `url` | The page to preview *(required)* | — |
| `count` | How many images to preview | `5` (max 10) |

### `/bot_info`
Show a help embed with all commands and features.

---

## 🐛 Bugs Fixed vs Original

- `threading` was used but never imported → **crash on startup**
- `aiohttp` missing from `requirements.txt` → **install failure**
- No semaphore on concurrent downloads → **could fire 200 simultaneous requests**
- Tiny tracker pixels (1×1 px) were included in ZIP
- ZIP too large → user got nothing; now **auto-splits into multiple ZIPs**
- Filename collisions inside ZIP were silently overwritten
- `scraper.headers.get('User-Agent')` used wrong API; now uses correct attribute
- No ZIP compression → unnecessarily large files; now uses `ZIP_DEFLATE`
- Only one slash command; now has **3 commands** with proper option choices

---

## ☁️ Deploying to Render

1. Push your code to a GitHub repo.
2. Create a new **Web Service** on [render.com](https://render.com).
3. Set **Start Command** to `python bot.py`.
4. Add `DISCORD_TOKEN` as an environment variable.
5. Render's health check will hit the built-in HTTP server automatically.
