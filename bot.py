import discord
from discord import app_commands
import cloudscraper
from bs4 import BeautifulSoup
import urllib.parse
import io
import zipfile
import re
import os
import json
import asyncio
import aiohttp
import threading
import logging
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

# Google auth (service account)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ImageBot")

# ─── Config ───────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN                 = os.getenv("DISCORD_TOKEN")
GDRIVE_CREDS_JSON     = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")   # full JSON string
GDRIVE_PARENT_FOLDER  = os.getenv("GDRIVE_PARENT_FOLDER_ID", "root")

MAX_IMAGES            = 300
MAX_CONCURRENT_DL     = 20
DOWNLOAD_TIMEOUT      = aiohttp.ClientTimeout(total=20, connect=8)
DISCORD_SIZE_LIMIT    = 10 * 1024 * 1024   # 10 MB per ZIP part
MIN_IMAGE_BYTES       = 512
SUPPORTED_EXTS        = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"}

# ─── Health-check server (Render) ─────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, *_):
        pass

def _start_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _Handler)
    log.info(f"Health-check server on port {port}")
    server.serve_forever()

threading.Thread(target=_start_web_server, daemon=True).start()

# ─── Google Drive helpers ─────────────────────────────────────────────────────
GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

def _get_drive_service():
    if not GDRIVE_CREDS_JSON:
        raise RuntimeError("GDRIVE_SERVICE_ACCOUNT_JSON env var is not set.")
    info = json.loads(GDRIVE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(info, scopes=GDRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def _create_drive_folder(service, name: str, parent_id: str) -> str:
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]

def _make_folder_public(service, folder_id: str):
    service.permissions().create(
        fileId=folder_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

def _upload_image_to_drive(service, folder_id, filename, content, mime_type):
    meta = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type or "image/jpeg", resumable=False)
    return service.files().create(body=meta, media_body=media, fields="id").execute()["id"]

def _mime_from_ext(ext: str) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",  ".gif": "image/gif",
        ".webp": "image/webp", ".svg": "image/svg+xml",
        ".bmp": "image/bmp",  ".avif": "image/avif",
    }.get(ext.lower(), "image/jpeg")

def _upload_all_to_drive(results: list, source_url: str) -> str:
    service = _get_drive_service()
    domain = urllib.parse.urlparse(source_url).netloc.replace("www.", "")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    folder_id = _create_drive_folder(service, f"{domain} — {ts}", GDRIVE_PARENT_FOLDER)
    _make_folder_public(service, folder_id)

    name_counts: defaultdict[str, int] = defaultdict(int)
    for index, img_url, content, content_type in results:
        if not content:
            continue
        ext = _ext_from_content_type(content_type, img_url)
        raw = os.path.basename(urllib.parse.urlparse(img_url).path)
        base = os.path.splitext(raw)[0][:60] if (raw and "." in raw) else f"image_{index:03d}"
        filename = f"{index:03d}_{base}{ext}"
        if name_counts[filename]:
            filename = f"{index:03d}_{base}_{name_counts[filename]}{ext}"
        name_counts[filename] += 1
        _upload_image_to_drive(service, folder_id, filename, content, _mime_from_ext(ext))

    return f"https://drive.google.com/drive/folders/{folder_id}"

# ─── Bot client ───────────────────────────────────────────────────────────────
class ImageScraperBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        log.info("Slash commands synced.")

client = ImageScraperBot()

@client.event
async def on_ready():
    log.info(f"Logged in as {client.user} (ID: {client.user.id})")
    await client.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="webpages for images 🔍")
    )

# ─── Scraping ─────────────────────────────────────────────────────────────────
def _scrape_image_urls(url: str) -> tuple[list[str], dict, str]:
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "DNT": "1",
    }
    try:
        resp = scraper.get(url, timeout=20, headers=headers)
        resp.raise_for_status()
    except Exception as exc:
        log.warning(f"Scrape failed for {url}: {exc}")
        return [], {}, ""

    soup = BeautifulSoup(resp.content, "html.parser")
    found: set[str] = set()

    def add(src: str):
        if not src or src.startswith("data:"):
            return
        full = urllib.parse.urljoin(url, src.strip())
        parsed_path = urllib.parse.urlparse(full).path.lower()
        ext = os.path.splitext(parsed_path)[1]
        if ext == "" or ext in SUPPORTED_EXTS:
            found.add(full)

    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
            add(img.get(attr, ""))
        for part in (img.get("srcset") or "").split(","):
            add(part.strip().split()[0] if part.strip() else "")

    for source in soup.find_all("source"):
        for part in (source.get("srcset") or "").split(","):
            add(part.strip().split()[0] if part.strip() else "")

    for tag in soup.find_all(style=True):
        for u in re.findall(r'url\([\'"]?(.*?)[\'"]?\)', tag["style"]):
            add(u)

    for style_tag in soup.find_all("style"):
        if style_tag.string:
            for u in re.findall(r'url\([\'"]?(.*?)[\'"]?\)', style_tag.string):
                add(u)

    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        if "image" in prop.lower():
            add(meta.get("content", ""))

    for link in soup.find_all("link", rel=True):
        if any("icon" in r for r in link.get("rel", [])):
            add(link.get("href", ""))

    ua = scraper.headers.get("User-Agent") or "Mozilla/5.0"
    return list(found), scraper.cookies.get_dict(), ua


def _ext_from_content_type(ct: str, fallback_url: str) -> str:
    ct = ct.lower().split(";")[0].strip()
    mapping = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/svg+xml": ".svg", "image/bmp": ".bmp",
        "image/avif": ".avif",
    }
    if ct in mapping:
        return mapping[ct]
    path = urllib.parse.urlparse(fallback_url).path.lower()
    ext = os.path.splitext(path)[1]
    return ext if ext in SUPPORTED_EXTS else ".jpg"


async def _fetch_one(session, sem, index, img_url, min_bytes):
    async with sem:
        try:
            async with session.get(img_url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True) as r:
                if r.status == 200:
                    data = await r.read()
                    if len(data) >= min_bytes:
                        return index, img_url, data, r.headers.get("Content-Type", "")
        except asyncio.TimeoutError:
            log.debug(f"Timeout: {img_url}")
        except Exception as exc:
            log.debug(f"Download error {img_url}: {exc}")
    return index, img_url, None, ""


def _build_zip(results) -> tuple[io.BytesIO, int]:
    buf = io.BytesIO()
    name_counts: defaultdict[str, int] = defaultdict(int)
    count = 0
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for index, img_url, content, content_type in results:
            if not content:
                continue
            ext = _ext_from_content_type(content_type, img_url)
            raw = os.path.basename(urllib.parse.urlparse(img_url).path)
            base = os.path.splitext(raw)[0][:60] if (raw and "." in raw) else f"image_{index:03d}"
            filename = f"{base}{ext}"
            if name_counts[filename]:
                filename = f"{base}_{name_counts[filename]}{ext}"
            name_counts[filename] += 1
            zf.writestr(f"{index:03d}_{filename}", content)
            count += 1
    buf.seek(0)
    return buf, count


def _split_zip_if_needed(results):
    buf, count = _build_zip(results)
    if len(buf.getvalue()) <= DISCORD_SIZE_LIMIT or count == 0:
        return [(buf, count)]

    file_pairs = [(i, u, c, ct) for i, u, c, ct in results if c]
    chunks, current, size = [], [], 0
    for pair in file_pairs:
        sz = len(pair[2]) + 1024
        if current and size + sz > DISCORD_SIZE_LIMIT:
            chunks.append(current)
            current, size = [pair], sz
        else:
            current.append(pair)
            size += sz
    if current:
        chunks.append(current)
    return [_build_zip(chunk) for chunk in chunks]

# ─── Commands ─────────────────────────────────────────────────────────────────

@client.tree.command(
    name="download_images",
    description="Scrape & download all images from a webpage — ZIP or Google Drive.",
)
@app_commands.describe(
    url="The full URL of the webpage to scrape",
    min_size="Ignore images smaller than this many KB (default: 5)",
    image_type="Only download this format (leave blank for all)",
    max_count="Maximum images to download (default: 100, max: 300)",
    upload_to_drive="Upload images to Google Drive and get a shareable folder link",
)
@app_commands.choices(
    image_type=[
        app_commands.Choice(name="All types",  value="all"),
        app_commands.Choice(name="JPEG / JPG", value=".jpg"),
        app_commands.Choice(name="PNG",        value=".png"),
        app_commands.Choice(name="GIF",        value=".gif"),
        app_commands.Choice(name="WebP",       value=".webp"),
        app_commands.Choice(name="SVG",        value=".svg"),
        app_commands.Choice(name="AVIF",       value=".avif"),
    ]
)
async def download_images(
    interaction: discord.Interaction,
    url: str,
    min_size: int = 5,
    image_type: str = "all",
    max_count: int = 100,
    upload_to_drive: bool = False,
):
    await interaction.response.defer(thinking=True)

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    min_bytes   = max(0, min_size) * 1024
    max_count   = max(1, min(max_count, MAX_IMAGES))
    type_filter = None if image_type == "all" else image_type

    if upload_to_drive and not GDRIVE_CREDS_JSON:
        await interaction.followup.send(
            "❌ Google Drive is not configured on this bot.\n"
            "The bot owner must set `GDRIVE_SERVICE_ACCOUNT_JSON` and `GDRIVE_PARENT_FOLDER_ID`."
        )
        return

    log.info(f"Scraping: {url}")
    image_urls, cookies, user_agent = await asyncio.to_thread(_scrape_image_urls, url)

    if not image_urls:
        embed = discord.Embed(
            title="❌ No Images Found",
            description=(
                f"Couldn't find any images on **{url}**.\n\n"
                "Possible reasons:\n"
                "• The page renders images with JavaScript\n"
                "• The site blocks scrapers\n"
                "• The URL leads to a non-HTML resource"
            ),
            color=discord.Color.red(),
        )
        await interaction.followup.send(embed=embed)
        return

    if type_filter:
        image_urls = [u for u in image_urls if u.lower().endswith(type_filter)]
        if not image_urls:
            await interaction.followup.send(
                f"⚠️ Found images on the page, but none matched the **{type_filter}** filter."
            )
            return

    urls_to_dl = image_urls[:max_count]
    destination = "📁 Google Drive" if upload_to_drive else "📦 ZIP file"

    progress_embed = discord.Embed(
        title="⏳ Downloading Images…",
        description=(
            f"Found **{len(image_urls)}** image URL(s).\n"
            f"Downloading up to **{len(urls_to_dl)}** → {destination}"
        ),
        color=discord.Color.blurple(),
    )
    progress_msg = await interaction.followup.send(embed=progress_embed, wait=True)

    sem = asyncio.Semaphore(MAX_CONCURRENT_DL)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_DL, ssl=False)
    async with aiohttp.ClientSession(
        cookies=cookies,
        headers={
            "User-Agent": user_agent,
            "Referer": url,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
        connector=connector,
    ) as session:
        tasks = [_fetch_one(session, sem, i, u, min_bytes) for i, u in enumerate(urls_to_dl)]
        results = await asyncio.gather(*tasks)

    successful = sum(1 for _, _, c, _ in results if c)

    if successful == 0:
        embed = discord.Embed(
            title="⚠️ Download Failed",
            description=(
                "Scraped URL(s) but couldn't download any images.\n\n"
                "• Protected with hotlink prevention\n"
                "• Served behind authentication\n"
                f"• All smaller than the **{min_size} KB** min-size filter"
            ),
            color=discord.Color.orange(),
        )
        await progress_msg.edit(embed=embed)
        return

    # ── Google Drive path ──
    if upload_to_drive:
        await progress_msg.edit(embed=discord.Embed(
            title="☁️ Uploading to Google Drive…",
            description=f"Uploading **{successful}** images. This may take a moment…",
            color=discord.Color.blurple(),
        ))
        try:
            folder_url = await asyncio.to_thread(_upload_all_to_drive, results, url)
        except Exception as exc:
            log.error(f"Drive upload failed: {exc}")
            await progress_msg.edit(embed=discord.Embed(
                title="❌ Google Drive Upload Failed",
                description=f"```{exc}```\nCheck service account credentials and parent folder ID.",
                color=discord.Color.red(),
            ))
            return

        result_embed = discord.Embed(title="✅ Uploaded to Google Drive!", color=discord.Color.green())
        result_embed.add_field(name="🌐 Source",       value=url,                    inline=False)
        result_embed.add_field(name="🖼️ Found",        value=str(len(image_urls)),   inline=True)
        result_embed.add_field(name="☁️ Uploaded",     value=str(successful),        inline=True)
        result_embed.add_field(name="📂 Drive Folder", value=f"[Open in Google Drive]({folder_url})", inline=False)
        if type_filter:
            result_embed.add_field(name="🔍 Filter", value=type_filter, inline=True)
        result_embed.set_footer(text="Folder visibility: anyone with the link can view")
        await progress_msg.edit(embed=result_embed)

    # ── ZIP path ──
    else:
        zip_parts  = await asyncio.to_thread(_split_zip_if_needed, results)
        size_total = sum(len(b.getvalue()) for b, _ in zip_parts)

        result_embed = discord.Embed(title="✅ Images Ready!", color=discord.Color.green())
        result_embed.add_field(name="🌐 Source",     value=url,                    inline=False)
        result_embed.add_field(name="🖼️ Found",      value=str(len(image_urls)),   inline=True)
        result_embed.add_field(name="⬇️ Downloaded", value=str(successful),        inline=True)
        result_embed.add_field(name="📦 ZIP Size",   value=f"{size_total/1024/1024:.2f} MB", inline=True)
        if type_filter:
            result_embed.add_field(name="🔍 Filter", value=type_filter, inline=True)
        if len(zip_parts) > 1:
            result_embed.add_field(
                name="⚡ Split ZIPs",
                value=f"Too large — split into **{len(zip_parts)}** ZIPs",
                inline=False,
            )
        result_embed.set_footer(text="Tip: use upload_to_drive: True to bypass the 10 MB ZIP limit")
        await progress_msg.edit(embed=result_embed)

        files = [
            discord.File(buf, filename=f"images_part{i+1}.zip" if len(zip_parts) > 1 else "images.zip")
            for i, (buf, _) in enumerate(zip_parts)
        ]
        for i in range(0, len(files), 10):
            await interaction.followup.send(files=files[i:i+10])


@client.tree.command(
    name="preview_images",
    description="Preview the first few images from a webpage directly in Discord.",
)
@app_commands.describe(url="The URL to preview images from", count="How many to preview (1–10)")
async def preview_images(interaction: discord.Interaction, url: str, count: int = 5):
    await interaction.response.defer(thinking=True)
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    count = max(1, min(count, 10))

    image_urls, cookies, user_agent = await asyncio.to_thread(_scrape_image_urls, url)
    if not image_urls:
        await interaction.followup.send("❌ No images found on that page.")
        return

    sem = asyncio.Semaphore(10)
    connector = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(
        cookies=cookies, headers={"User-Agent": user_agent, "Referer": url}, connector=connector,
    ) as session:
        results = await asyncio.gather(*[
            _fetch_one(session, sem, i, u, MIN_IMAGE_BYTES) for i, u in enumerate(image_urls[:count])
        ])

    files = [
        discord.File(io.BytesIO(c), filename=f"preview_{i:02d}{_ext_from_content_type(ct, u)}")
        for i, u, c, ct in results if c
    ]
    if not files:
        await interaction.followup.send("⚠️ Found image URLs but couldn't download any for preview.")
        return

    embed = discord.Embed(
        title=f"🖼️ Preview — {url}",
        description=f"Showing **{len(files)}** of **{len(image_urls)}** images.\nUse `/download_images` to get them all.",
        color=discord.Color.blurple(),
    )
    await interaction.followup.send(embed=embed, files=files[:10])


@client.tree.command(name="bot_info", description="Show commands and setup info.")
async def bot_info(interaction: discord.Interaction):
    drive_status = "✅ Connected" if GDRIVE_CREDS_JSON else "❌ Not configured (set GDRIVE_SERVICE_ACCOUNT_JSON)"
    embed = discord.Embed(
        title="🤖 Image Scraper Bot",
        description="Scrapes and downloads all images from any webpage.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📥 /download_images",
        value=(
            "`url` — page to scrape *(required)*\n"
            "`min_size` — skip images below X KB *(default 5)*\n"
            "`image_type` — filter by format\n"
            "`max_count` — cap at N images *(default 100, max 300)*\n"
            "`upload_to_drive` — upload to Google Drive instead of ZIP"
        ),
        inline=False,
    )
    embed.add_field(name="👁️ /preview_images", value="Preview images inline without downloading.", inline=False)
    embed.add_field(
        name="⚡ Features",
        value=(
            "• Cloudflare bypass · concurrent downloads\n"
            "• Scrapes `<img>`, `<picture>`, CSS, OG tags\n"
            "• Auto-splits ZIP > 10 MB\n"
            "• **Google Drive upload** with public folder link\n"
            "• Deduplicates filenames, filters by type & size"
        ),
        inline=False,
    )
    embed.add_field(name="☁️ Google Drive", value=drive_status, inline=False)
    embed.set_footer(text="discord.py · cloudscraper · aiohttp · google-api-python-client")
    await interaction.response.send_message(embed=embed)


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        log.error("DISCORD_TOKEN is not set in .env")
    else:
        client.run(TOKEN, log_handler=None)
