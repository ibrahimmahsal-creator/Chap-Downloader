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
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ImageBot")

# ─── Config ───────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

MAX_IMAGES          = 300          # hard cap per request
MAX_CONCURRENT_DL   = 20           # simultaneous downloads (semaphore)
DOWNLOAD_TIMEOUT    = aiohttp.ClientTimeout(total=20, connect=8)
DISCORD_SIZE_LIMIT  = 25 * 1024 * 1024   # 25 MB
MIN_IMAGE_BYTES     = 512          # ignore tiny tracker pixels / placeholders
SUPPORTED_EXTS      = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.avif'}

# ─── Health-check web server (for Render) ─────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, *_):   # silence HTTP access logs
        pass

def _start_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _Handler)
    log.info(f"Health-check server on port {port}")
    server.serve_forever()

threading.Thread(target=_start_web_server, daemon=True).start()

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
    found: set[str] = set()

    def add(src: str):
        if not src or src.startswith("data:"):
            return
        full = urllib.parse.urljoin(url, src.strip())
        # Basic sanity: must look like an image URL or have no extension (lazy-load)
        parsed_path = urllib.parse.urlparse(full).path.lower()
        ext = os.path.splitext(parsed_path)[1]
        if ext == "" or ext in SUPPORTED_EXTS:
            found.add(full)

    # 1. <img src / data-src / data-original / data-lazy-src / data-srcset>
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
            add(img.get(attr, ""))
        # srcset on <img>
        srcset = img.get("srcset", "")
        if srcset:
            for part in srcset.split(","):
                add(part.strip().split()[0])

    # 2. <source srcset> inside <picture>
    for source in soup.find_all("source"):
        srcset = source.get("srcset", "")
        for part in srcset.split(","):
            add(part.strip().split()[0])

    # 3. Inline CSS background-image
    for tag in soup.find_all(style=True):
        for u in re.findall(r'url\([\'"]?(.*?)[\'"]?\)', tag["style"]):
            add(u)

    # 4. <style> blocks
    for style_tag in soup.find_all("style"):
        if style_tag.string:
            for u in re.findall(r'url\([\'"]?(.*?)[\'"]?\)', style_tag.string):
                add(u)

    # 5. Open Graph / Twitter card meta images
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        if "image" in prop.lower():
            add(meta.get("content", ""))

    # 6. <link rel="icon"> / apple-touch-icon
    for link in soup.find_all("link", rel=True):
        if any("icon" in r for r in link.get("rel", [])):
            add(link.get("href", ""))

    # Retrieve UA that cloudscraper actually used
    ua = scraper.headers.get("User-Agent") or "Mozilla/5.0"
    cookies = scraper.cookies.get_dict()

    return list(found), cookies, ua


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
    # Fall back to URL extension
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
            ext = _ext_from_content_type(content_type, img_url)
            raw_name = os.path.basename(urllib.parse.urlparse(img_url).path)
            if raw_name and "." in raw_name:
                base = os.path.splitext(raw_name)[0][:60]   # cap length
            else:
                base = f"image_{index:03d}"
            filename = f"{base}{ext}"
            # Deduplicate filenames
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
    # Try full zip first
    buf, count = _build_zip(results)
    if len(buf.getvalue()) <= limit or count == 0:
        return [(buf, count)]

    # Split: build individual file pairs and chunk them
    file_pairs = []
    for index, img_url, content, content_type in results:
        if content:
            file_pairs.append((index, img_url, content, content_type))

    chunks = []
    current_chunk = []
    current_size = 0
    OVERHEAD = 1024  # ZIP header overhead estimate per file

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
    description="Scrape & download all images from any webpage. Sends a ZIP file.",
)
@app_commands.describe(
    url="The full URL of the webpage to scrape",
    min_size="Ignore images smaller than this many KB (default: 5)",
    image_type="Only download this type (leave blank for all)",
    max_count="Maximum number of images to include (default: 100, max: 300)",
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
):
    await interaction.response.defer(thinking=True)

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

    progress_embed = discord.Embed(
        title="⏳ Downloading Images…",
        description=(
            f"Found **{len(image_urls)}** image URL(s){' (type filtered)' if type_filter else ''}.\n"
            f"Downloading up to **{len(urls_to_dl)}** concurrently…"
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

    # ── Build ZIP(s) ──
    zip_parts = await asyncio.to_thread(_split_zip_if_needed, results)

    successful = sum(c for _, c in zip_parts)

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

    # ── Send ──
    size_total = sum(len(b.getvalue()) for b, _ in zip_parts)
    result_embed = discord.Embed(
        title="✅ Images Ready!",
        color=discord.Color.green(),
    )
    result_embed.add_field(name="🌐 Source", value=url, inline=False)
    result_embed.add_field(name="🖼️ Images Found",     value=str(len(image_urls)),    inline=True)
    result_embed.add_field(name="⬇️ Downloaded",        value=str(successful),         inline=True)
    result_embed.add_field(name="📦 ZIP Size",          value=f"{size_total/1024/1024:.2f} MB", inline=True)
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
    # Discord allows multiple files per message (up to 10)
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
    embed = discord.Embed(
        title="🤖 Image Scraper Bot",
        description="A powerful Discord bot that extracts and downloads all images from any webpage.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📥 /download_images",
        value=(
            "Scrape + download all images as a **ZIP file**.\n"
            "`url` — page to scrape *(required)*\n"
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
            "• Auto-splits ZIP if over 25 MB\n"
            "• Deduplicates filenames\n"
            "• Filters by type & minimum size"
        ),
        inline=False,
    )
    embed.set_footer(text="Built with discord.py · cloudscraper · aiohttp · BeautifulSoup4")
    await interaction.response.send_message(embed=embed)


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        log.error("DISCORD_TOKEN is not set. Add it to your .env file.")
    else:
        client.run(TOKEN, log_handler=None)
