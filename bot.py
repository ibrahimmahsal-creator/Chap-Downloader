import discord
from discord import app_commands
import cloudscraper
from bs4 import BeautifulSoup
import urllib.parse
import io
import zipfile
import re
import os
import asyncio
import aiohttp
import threading
import logging
import json
import tempfile
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

# ─── Google Drive imports (optional — only used when delivery=gdrive) ─────────
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False

# ─── Pillow (optional — used for dimension-based manhwa filter) ───────────────
try:
    from PIL import Image as PILImage
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    log.warning("Pillow not installed — dimension filter disabled. Run: pip install Pillow")

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ImageBot")

# ─── Config ───────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN                   = os.getenv("DISCORD_TOKEN")
GDRIVE_FOLDER_ID        = os.getenv("GDRIVE_FOLDER_ID", "")       # Target Drive folder (optional)
GDRIVE_SERVICE_ACCOUNT  = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON", "")  # JSON key as string or path

MAX_IMAGES          = 300
MAX_CONCURRENT_DL   = 20
DOWNLOAD_TIMEOUT    = aiohttp.ClientTimeout(total=20, connect=8)
DISCORD_SIZE_LIMIT  = 10 * 1024 * 1024   # 10 MB
MIN_IMAGE_BYTES     = 512
SUPPORTED_EXTS      = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.avif'}

# ─── Manhwa Panel Filter Constants ───────────────────────────────────────────
# URL path fragments that indicate UI/decoration assets — never manhwa panels
_UI_SKIP_WORDS = {
    "logo", "icon", "favicon", "avatar", "banner", "sprite", "button",
    "arrow", "loading", "spinner", "placeholder", "blank", "pixel",
    "tracking", "ads", "badge", "rating", "star",
    "social", "share", "facebook", "twitter", "discord", "patreon",
    "header", "footer", "nav", "menu", "sidebar", "widget",
    "bg", "background", "pattern", "texture", "watermark",
}

# HTML block-level tags whose contents are never manhwa chapter panels
_UI_ANCESTORS = {"nav", "header", "footer", "aside", "button"}

# Minimum width (px) a manhwa panel must have — filters icons / thumbnails
MINIMUM_PANEL_WIDTH = 300


def _is_ui_url(img_url: str) -> bool:
    """Return True if the URL path contains a known UI/decoration keyword."""
    path = urllib.parse.urlparse(img_url).path.lower()
    tokens = set(re.split(r'[/_\-.]', path))
    return bool(tokens & _UI_SKIP_WORDS)


def _is_manhwa_panel(data: bytes, content_type: str) -> bool:
    """
    Return True only if the image is wide enough to be a manhwa chapter panel.
    Requires Pillow; falls back to True (keep) if unavailable or unreadable.
    SVGs are always kept — they have no raster dimensions.
    """
    if not PIL_AVAILABLE:
        return True
    if "svg" in content_type.lower():
        return True
    try:
        img = PILImage.open(io.BytesIO(data))
        w, h = img.size
        return w >= MINIMUM_PANEL_WIDTH
    except Exception:
        return True   # can't read — keep it rather than silently drop

# ─── Health-check web server (for Render) ─────────────────────────────────────
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

# ─── Google Drive Helper ───────────────────────────────────────────────────────
def _get_drive_service():
    """Build and return an authenticated Google Drive service client."""
    if not GDRIVE_AVAILABLE:
        raise RuntimeError(
            "Google API libraries are not installed. "
            "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
        )
    if not GDRIVE_SERVICE_ACCOUNT:
        raise RuntimeError(
            "GDRIVE_SERVICE_ACCOUNT_JSON is not set in your .env file. "
            "Add the path to your service account JSON file or paste its contents."
        )

    # Support both: a file path  OR  the raw JSON string
    raw = GDRIVE_SERVICE_ACCOUNT.strip()
    if raw.startswith("{"):
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"]
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            raw, scopes=["https://www.googleapis.com/auth/drive"]
        )

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _upload_to_drive(buf: io.BytesIO, filename: str) -> str:
    """Upload a BytesIO buffer to Google Drive. Returns a shareable link."""
    service = _get_drive_service()
    buf.seek(0)

    file_metadata = {"name": filename}
    if GDRIVE_FOLDER_ID:
        file_metadata["parents"] = [GDRIVE_FOLDER_ID]

    media = MediaIoBaseUpload(buf, mimetype="application/zip", resumable=True)
    file = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id")
        .execute()
    )
    file_id = file.get("id")

    # Make it publicly readable (anyone with the link)
    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


# ─── Bot Client ───────────────────────────────────────────────────────────────
class ImageScraperBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
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

# ─── Scraping Logic ───────────────────────────────────────────────────────────
def _scrape_image_urls(url: str) -> tuple[list[str], dict, str]:
    """
    Synchronous scrape — runs in a thread pool so it won't block the event loop.
    Returns (image_url_list, cookies_dict, user_agent_string).
    """
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
    # Use a dict as an ordered set — keys are URLs, insertion order = DOM order
    found: dict[str, None] = {}

    def add(src: str, skip_ui_check: bool = False):
        if not src or src.startswith("data:"):
            return
        full = urllib.parse.urljoin(url, src.strip())
        # ── Layer 1: URL keyword blacklist ──
        if not skip_ui_check and _is_ui_url(full):
            return
        parsed_path = urllib.parse.urlparse(full).path.lower()
        ext = os.path.splitext(parsed_path)[1]
        if ext == "" or ext in SUPPORTED_EXTS:
            found[full] = None   # preserves first-seen order, deduplicates

    def _in_ui_ancestor(tag) -> bool:
        """Return True if any ancestor tag is a navigation/layout element."""
        for parent in tag.parents:
            if getattr(parent, "name", None) in _UI_ANCESTORS:
                return True
        return False

    # 1. <img> tags — skip those inside nav/header/footer/aside (Layer 2: DOM context)
    for img in soup.find_all("img"):
        if _in_ui_ancestor(img):
            continue
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
            add(img.get(attr, ""))
        srcset = img.get("srcset", "")
        if srcset:
            for part in srcset.split(","):
                add(part.strip().split()[0])

    # 2. <source srcset> inside <picture> — only if not in a UI ancestor
    for source in soup.find_all("source"):
        if _in_ui_ancestor(source):
            continue
        srcset = source.get("srcset", "")
        for part in srcset.split(","):
            add(part.strip().split()[0])

    # NOTE: CSS backgrounds, <style> blocks, Open Graph meta, and favicon links
    # are intentionally excluded — they are never manhwa chapter panel images.

    ua = scraper.headers.get("User-Agent") or "Mozilla/5.0"
    cookies = scraper.cookies.get_dict()
    return list(found.keys()), cookies, ua  # keys() preserves DOM insertion order


def _ext_from_content_type(ct: str, fallback_url: str) -> str:
    ct = ct.lower().split(";")[0].strip()
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "image/bmp": ".bmp",
        "image/avif": ".avif",
    }
    if ct in mapping:
        return mapping[ct]
    path = urllib.parse.urlparse(fallback_url).path.lower()
    ext = os.path.splitext(path)[1]
    return ext if ext in SUPPORTED_EXTS else ".jpg"


async def _fetch_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    index: int,
    img_url: str,
    min_bytes: int,
) -> tuple[int, str, bytes | None, str]:
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


def _build_zip(results, zip_compression=zipfile.ZIP_DEFLATED) -> tuple[io.BytesIO, int]:
    """Pack downloaded images into a ZIP buffer. Returns (buffer, count)."""
    buf = io.BytesIO()
    name_counts: defaultdict[str, int] = defaultdict(int)
    count = 0
    with zipfile.ZipFile(buf, "w", compression=zip_compression, compresslevel=6) as zf:
        for index, img_url, content, content_type in results:
            if not content:
                continue
            # ── Layer 3: Dimension filter — reject images too narrow to be panels ──
            if not _is_manhwa_panel(content, content_type):
                log.debug(f"Skipped (too small): {img_url}")
                continue
            ext = _ext_from_content_type(content_type, img_url)
            raw_name = os.path.basename(urllib.parse.urlparse(img_url).path)
            if raw_name and "." in raw_name:
                base = os.path.splitext(raw_name)[0][:60]
            else:
                base = f"image_{index:03d}"
            filename = f"{base}{ext}"
            if name_counts[filename]:
                filename = f"{base}_{name_counts[filename]}{ext}"
            name_counts[filename] += 1
            zf.writestr(f"{index:03d}_{filename}", content)
            count += 1
    buf.seek(0)
    return buf, count


def _split_zip_if_needed(results, limit=DISCORD_SIZE_LIMIT):
    """
    If total zip exceeds Discord's limit, split into multiple ZIPs of ≤ limit bytes.
    Returns list of (BytesIO, count) tuples.
    """
    buf, count = _build_zip(results)
    if len(buf.getvalue()) <= limit or count == 0:
        return [(buf, count)]

    file_pairs = [
        (index, img_url, content, content_type)
        for index, img_url, content, content_type in results
        if content
    ]

    chunks = []
    current_chunk = []
    current_size = 0
    OVERHEAD = 1024

    for pair in file_pairs:
        size = len(pair[2]) + OVERHEAD
        if current_chunk and current_size + size > limit:
            chunks.append(current_chunk)
            current_chunk = [pair]
            current_size = size
        else:
            current_chunk.append(pair)
            current_size += size

    if current_chunk:
        chunks.append(current_chunk)

    return [_build_zip(chunk) for chunk in chunks]


# ─── Slash Commands ───────────────────────────────────────────────────────────

@client.tree.command(
    name="download_images",
    description="Scrape & download all images from any webpage — choose ZIP or Google Drive delivery.",
)
@app_commands.describe(
    url="The full URL of the webpage to scrape",
    delivery="How to receive the images: ZIP attached here, or a Google Drive link",
    min_size="Ignore images smaller than this many KB (default: 5)",
    image_type="Only download this type (leave blank for all)",
    max_count="Maximum number of images to include (default: 100, max: 300)",
)
@app_commands.choices(
    delivery=[
        app_commands.Choice(name="📦 ZIP file (attach here)",    value="zip"),
        app_commands.Choice(name="☁️ Google Drive (shared link)", value="gdrive"),
    ],
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
    delivery: str = "zip",
    min_size: int = 5,
    image_type: str = "all",
    max_count: int = 100,
):
    await interaction.response.defer(thinking=True)

    # ── Validate Google Drive availability early ──
    if delivery == "gdrive":
        if not GDRIVE_AVAILABLE:
            await interaction.followup.send(
                "❌ **Google Drive not available.** The required libraries aren't installed.\n"
                "Run: `pip install google-auth google-auth-oauthlib google-api-python-client`"
            )
            return
        if not GDRIVE_SERVICE_ACCOUNT:
            await interaction.followup.send(
                "❌ **Google Drive not configured.**\n"
                "Set `GDRIVE_SERVICE_ACCOUNT_JSON` in your `.env` file with the path to "
                "(or contents of) your Google Service Account JSON key."
            )
            return

    # ── Normalise URL ──
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # ── Clamp options ──
    min_bytes   = max(0, min_size) * 1024
    max_count   = max(1, min(max_count, MAX_IMAGES))
    type_filter = None if image_type == "all" else image_type

    # ── Scrape ──
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

    # ── Type filter ──
    if type_filter:
        image_urls = [u for u in image_urls if u.lower().endswith(type_filter)]
        if not image_urls:
            await interaction.followup.send(
                f"⚠️ Found images on the page, but none matched the **{type_filter}** filter."
            )
            return

    urls_to_dl = image_urls[:max_count]

    delivery_label = "☁️ Google Drive" if delivery == "gdrive" else "📦 ZIP"
    progress_embed = discord.Embed(
        title="⏳ Downloading Images…",
        description=(
            f"Found **{len(image_urls)}** image URL(s){' (type filtered)' if type_filter else ''}.\n"
            f"Downloading up to **{len(urls_to_dl)}** concurrently…\n"
            f"Delivery: **{delivery_label}**"
        ),
        color=discord.Color.blurple(),
    )
    progress_msg = await interaction.followup.send(embed=progress_embed, wait=True)

    # ── Async Download ──
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

    # ── Check success ──
    successful = sum(1 for _, _, content, _ in results if content)

    if successful == 0:
        embed = discord.Embed(
            title="⚠️ Download Failed",
            description=(
                "Successfully scraped URL(s) but couldn't download any images.\n\n"
                "They might be:\n"
                "• Served behind authentication\n"
                "• Protected with hotlink prevention\n"
                f"• Smaller than your min-size filter (**{min_size} KB**)"
            ),
            color=discord.Color.orange(),
        )
        await progress_msg.edit(embed=embed)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # DELIVERY: Google Drive
    # ══════════════════════════════════════════════════════════════════════════
    if delivery == "gdrive":
        # Build a single ZIP (no 25 MB split needed — Drive has no limit)
        uploading_embed = discord.Embed(
            title="☁️ Uploading to Google Drive…",
            description=f"Packing **{successful}** images and uploading. This may take a moment…",
            color=discord.Color.og_blurple(),
        )
        await progress_msg.edit(embed=uploading_embed)

        try:
            buf, count = await asyncio.to_thread(_build_zip, results)
            zip_size   = len(buf.getvalue())

            # Generate a clean filename from the URL hostname
            hostname  = urllib.parse.urlparse(url).netloc.replace("www.", "")
            safe_name = re.sub(r"[^\w\-.]", "_", hostname)
            filename  = f"{safe_name}_images.zip"

            drive_link = await asyncio.to_thread(_upload_to_drive, buf, filename)

        except Exception as exc:
            log.error(f"Google Drive upload failed: {exc}")
            err_embed = discord.Embed(
                title="❌ Google Drive Upload Failed",
                description=(
                    f"**Error:** {exc}\n\n"
                    "Make sure your Service Account JSON is correct and the target "
                    "folder (if set) has been shared with the service account email."
                ),
                color=discord.Color.red(),
            )
            await progress_msg.edit(embed=err_embed)
            return

        result_embed = discord.Embed(
            title="✅ Uploaded to Google Drive!",
            color=discord.Color.green(),
        )
        result_embed.add_field(name="🌐 Source",        value=url,                                  inline=False)
        result_embed.add_field(name="🖼️ Images Found",  value=str(len(image_urls)),                 inline=True)
        result_embed.add_field(name="⬇️ Downloaded",    value=str(successful),                      inline=True)
        result_embed.add_field(name="📦 ZIP Size",       value=f"{zip_size/1024/1024:.2f} MB",       inline=True)
        result_embed.add_field(name="☁️ Drive Link",    value=f"[Click to open]({drive_link})",     inline=False)
        if type_filter:
            result_embed.add_field(name="🔍 Filter", value=type_filter, inline=True)
        result_embed.set_footer(text="Uploaded via Google Drive Service Account")

        await progress_msg.edit(embed=result_embed)
        # Also send the raw link as a plain message so it's easy to copy
        await interaction.followup.send(f"☁️ **Google Drive link:** {drive_link}")
        return

    # ══════════════════════════════════════════════════════════════════════════
    # DELIVERY: ZIP (attach to Discord)
    # ══════════════════════════════════════════════════════════════════════════
    zip_parts = await asyncio.to_thread(_split_zip_if_needed, results)
    successful = sum(c for _, c in zip_parts)

    size_total = sum(len(b.getvalue()) for b, _ in zip_parts)
    result_embed = discord.Embed(
        title="✅ Images Ready!",
        color=discord.Color.green(),
    )
    result_embed.add_field(name="🌐 Source",         value=url,                                  inline=False)
    result_embed.add_field(name="🖼️ Images Found",   value=str(len(image_urls)),                 inline=True)
    result_embed.add_field(name="⬇️ Downloaded",     value=str(successful),                      inline=True)
    result_embed.add_field(name="📦 ZIP Size",        value=f"{size_total/1024/1024:.2f} MB",     inline=True)
    if type_filter:
        result_embed.add_field(name="🔍 Filter", value=type_filter, inline=True)
    if len(zip_parts) > 1:
        result_embed.add_field(
            name="⚡ Split ZIPs",
            value=f"File was too large — split into **{len(zip_parts)}** ZIPs",
            inline=False,
        )
    result_embed.set_footer(text="Images packed with ZIP_DEFLATE compression")

    files = [
        discord.File(buf, filename=f"images_part{i+1}.zip" if len(zip_parts) > 1 else "images.zip")
        for i, (buf, _) in enumerate(zip_parts)
    ]

    await progress_msg.edit(embed=result_embed)
    for i in range(0, len(files), 10):
        await interaction.followup.send(files=files[i:i+10])


@client.tree.command(
    name="preview_images",
    description="Preview the first few images found on a webpage without downloading.",
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

    preview_urls = image_urls[:count]

    sem = asyncio.Semaphore(10)
    connector = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(
        cookies=cookies,
        headers={"User-Agent": user_agent, "Referer": url},
        connector=connector,
    ) as session:
        tasks = [_fetch_one(session, sem, i, u, MIN_IMAGE_BYTES) for i, u in enumerate(preview_urls)]
        results = await asyncio.gather(*tasks)

    files = []
    for index, img_url, content, content_type in results:
        if content:
            ext = _ext_from_content_type(content_type, img_url)
            files.append(discord.File(io.BytesIO(content), filename=f"preview_{index:02d}{ext}"))

    if not files:
        await interaction.followup.send("⚠️ Found image URLs but couldn't download any for preview.")
        return

    embed = discord.Embed(
        title=f"🖼️ Image Preview — {url}",
        description=f"Showing **{len(files)}** of **{len(image_urls)}** images found.\nUse `/download_images` to get them all.",
        color=discord.Color.blurple(),
    )
    await interaction.followup.send(embed=embed, files=files[:10])


@client.tree.command(
    name="bot_info",
    description="Show information and usage tips for the Image Scraper Bot.",
)
async def bot_info(interaction: discord.Interaction):
    gdrive_status = "✅ Configured" if (GDRIVE_AVAILABLE and GDRIVE_SERVICE_ACCOUNT) else "❌ Not configured"

    embed = discord.Embed(
        title="🤖 Image Scraper Bot",
        description="A powerful Discord bot that extracts and downloads all images from any webpage.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📥 /download_images",
        value=(
            "Scrape + download all images.\n"
            "`url` — page to scrape *(required)*\n"
            "`delivery` — **📦 ZIP** (attach here) or **☁️ Google Drive** (link)\n"
            "`min_size` — skip images below X KB *(default 5)*\n"
            "`image_type` — filter by format (jpg/png/gif/webp/svg/avif)\n"
            "`max_count` — cap images to download *(default 100, max 300)*"
        ),
        inline=False,
    )
    embed.add_field(
        name="👁️ /preview_images",
        value="Preview the first few images from a page directly in Discord (no ZIP needed).",
        inline=False,
    )
    embed.add_field(
        name="ℹ️ /bot_info",
        value="Shows this help message.",
        inline=False,
    )
    embed.add_field(
        name="⚡ Features",
        value=(
            "• Bypasses basic Cloudflare protection\n"
            "• Scrapes `<img>`, `<picture>`, CSS backgrounds, Open Graph & meta tags\n"
            "• Concurrent downloads with rate limiting\n"
            "• **ZIP delivery**: auto-splits if over 25 MB\n"
            "• **Google Drive delivery**: no size limit, shareable link\n"
            "• Deduplicates filenames\n"
            "• Filters by type & minimum size"
        ),
        inline=False,
    )
    embed.add_field(
        name="☁️ Google Drive Status",
        value=gdrive_status,
        inline=True,
    )
    embed.set_footer(text="Built with discord.py · cloudscraper · aiohttp · BeautifulSoup4")
    await interaction.response.send_message(embed=embed)


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        log.error("DISCORD_TOKEN is not set. Add it to your .env file.")
    else:
        client.run(TOKEN, log_handler=None)
