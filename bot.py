import discord
from discord import app_commands
import cloudscraper
from bs4 import BeautifulSoup
import urllib.parse
import io
import zipfile
import re
import os
import sys
import json
import asyncio
import aiohttp
import threading
import logging
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

# ─── Playwright (optional — JS-rendered page fallback) ───────────────────────
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ─── Pillow (optional — used for dimension filter & smart stitch) ─────────────
try:
    from PIL import Image as PILImage
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ImageBot")

if not PIL_AVAILABLE:
    log.warning("Pillow not installed — dimension filter disabled. Run: pip install Pillow")

# ─── Config ───────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

MAX_IMAGES         = 300
MAX_CONCURRENT_DL  = 20
DOWNLOAD_TIMEOUT   = aiohttp.ClientTimeout(total=20, connect=8)
DISCORD_SIZE_LIMIT = 10 * 1024 * 1024   # 10 MB — split ZIPs above this
MIN_IMAGE_BYTES    = 512
SUPPORTED_EXTS     = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.avif'}

# ─── Manhwa Panel Filter Constants ────────────────────────────────────────────
_UI_SKIP_WORDS = {
    "logo", "icon", "favicon", "avatar", "banner", "sprite", "button",
    "arrow", "loading", "spinner", "placeholder", "blank", "pixel",
    "tracking", "ads", "badge", "rating", "star",
    "social", "share", "facebook", "twitter", "discord", "patreon",
    "header", "footer", "nav", "menu", "sidebar", "widget",
    "bg", "background", "pattern", "texture", "watermark",
}
_UI_ANCESTORS        = {"nav", "header", "footer", "aside", "button"}
MINIMUM_PANEL_WIDTH  = 450
MINIMUM_PANEL_HEIGHT = 450


def _is_ui_url(img_url: str) -> bool:
    path   = urllib.parse.urlparse(img_url).path.lower()
    tokens = set(re.split(r'[/_\-.]', path))
    return bool(tokens & _UI_SKIP_WORDS)


def _is_manhwa_panel(data: bytes, content_type: str) -> bool:
    if not PIL_AVAILABLE:
        return True
    if "svg" in content_type.lower():
        return True
    try:
        img = PILImage.open(io.BytesIO(data))
        w, h = img.size
        return w >= MINIMUM_PANEL_WIDTH and h >= MINIMUM_PANEL_HEIGHT
    except Exception:
        return True


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

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _parse_soup_for_images(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Extract image URLs from a BeautifulSoup document."""
    found: dict[str, None] = {}

    def add(src: str):
        if not src or src.startswith("data:"):
            return
        full = urllib.parse.urljoin(base_url, src.strip())
        if _is_ui_url(full):
            return
        parsed_path = urllib.parse.urlparse(full).path.lower()
        ext = os.path.splitext(parsed_path)[1]
        if ext == "" or ext in SUPPORTED_EXTS:
            found[full] = None

    def _in_ui_ancestor(tag) -> bool:
        for parent in tag.parents:
            if getattr(parent, "name", None) in _UI_ANCESTORS:
                return True
        return False

    for img in soup.find_all("img"):
        if _in_ui_ancestor(img):
            continue
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
            add(img.get(attr, ""))
        srcset = img.get("srcset", "")
        if srcset:
            for part in srcset.split(","):
                add(part.strip().split()[0])

    for source in soup.find_all("source"):
        if _in_ui_ancestor(source):
            continue
        srcset = source.get("srcset", "")
        for part in srcset.split(","):
            add(part.strip().split()[0])

    return list(found.keys())


def _scrape_static(url: str) -> tuple[list[str], dict, str]:
    """Fast static HTML scrape via cloudscraper."""
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
        log.warning(f"Static scrape failed for {url}: {exc}")
        return [], {}, DEFAULT_UA

    soup    = BeautifulSoup(resp.content, "html.parser")
    urls    = _parse_soup_for_images(soup, url)
    ua      = scraper.headers.get("User-Agent") or DEFAULT_UA
    cookies = scraper.cookies.get_dict()
    return urls, cookies, ua


async def _scrape_playwright(url: str) -> tuple[list[str], dict, str]:
    """
    Runs _playwright_worker.py in a subprocess with a hard 55-second OS-level
    timeout. proc.kill() is the only reliable way to stop Playwright on Windows
    when asyncio.wait_for cancellation doesn't propagate to the browser process.
    """
    if not PLAYWRIGHT_AVAILABLE:
        log.warning("Playwright not installed. Run: pip install playwright && python -m playwright install chromium")
        return [], {}, DEFAULT_UA

    log.info(f"Static scrape found nothing — trying Playwright subprocess for {url}")
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_playwright_worker.py")

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, worker, url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=55)
        except asyncio.TimeoutError:
            log.warning(f"Playwright subprocess timed out for {url} — killing")
            try:
                proc.kill()
            except Exception:
                pass
            await proc.wait()
            return [], {}, DEFAULT_UA

        if stderr:
            log.debug(f"Playwright worker: {stderr.decode(errors='replace')[:300]}")

        if not stdout or proc.returncode != 0:
            log.warning(f"Playwright worker exit code {proc.returncode}")
            return [], {}, DEFAULT_UA

        data    = json.loads(stdout.decode())
        urls    = data.get("urls", [])
        cookies = data.get("cookies", {})
        log.info(f"Playwright subprocess found {len(urls)} image URL(s)")
        return urls, cookies, DEFAULT_UA

    except Exception as exc:
        log.error(f"Playwright subprocess error: {exc}")
        return [], {}, DEFAULT_UA


_MANGADEX_UUID_RE = re.compile(
    r'/chapter/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
    re.IGNORECASE,
)

_SHINIGAMI_UUID_RE = re.compile(
    r'shinigami\.asia/chapter/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
    re.IGNORECASE,
)


async def _scrape_shinigami_api(url: str) -> tuple[list[str], dict, str]:
    """
    Shinigami.asia stores chapter pages in a JSON API at:
      GET https://shinigami.asia/api/chapter/<uuid>/images
    Returns a list of image objects with a `url` (or `src`) field.
    Falls back to scraping the __NEXT_DATA__ / window.__data__ JSON blob
    embedded in the page's <script> tags if the API endpoint fails.
    """
    m = _SHINIGAMI_UUID_RE.search(url)
    if not m:
        return [], {}, DEFAULT_UA

    chapter_id = m.group(1)
    log.info(f"Shinigami.asia detected — chapter {chapter_id}")

    headers = {
        "User-Agent": DEFAULT_UA,
        "Referer": url,
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # ── Attempt 1: JSON API endpoint ────────────────────────────────────────
    for api_tmpl in (
        f"https://shinigami.asia/api/chapter/{chapter_id}/images",
        f"https://shinigami.asia/api/chapters/{chapter_id}",
    ):
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(api_tmpl, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        # data may be a list of URLs, list of objects, or a dict with an images key
                        imgs = _extract_shinigami_urls(data, url)
                        if imgs:
                            log.info(f"Shinigami API ({api_tmpl}): {len(imgs)} pages")
                            return imgs, {}, DEFAULT_UA
        except Exception as exc:
            log.debug(f"Shinigami API attempt failed ({api_tmpl}): {exc}")

    # ── Attempt 2: parse __NEXT_DATA__ from raw HTML ─────────────────────
    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        resp = scraper.get(url, timeout=20, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        # Next.js embeds all page data in <script id="__NEXT_DATA__">
        next_data_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if next_data_tag and next_data_tag.string:
            next_data = json.loads(next_data_tag.string)
            imgs = _dig_for_image_urls(next_data, url)
            if imgs:
                log.info(f"Shinigami __NEXT_DATA__: {len(imgs)} pages")
                return imgs, scraper.cookies.get_dict(), DEFAULT_UA

        # Generic: scan all <script> tags for JSON arrays of image URLs
        img_re = re.compile(
            r'https?://[^\s\'"<>]+\.(?:jpg|jpeg|png|webp|gif|avif)(?:\?[^\s\'"<>]*)?',
            re.IGNORECASE,
        )
        found: dict[str, None] = {}
        for script in soup.find_all("script"):
            for m2 in img_re.finditer(script.string or ""):
                u = m2.group(0)
                if not _is_ui_url(u):
                    found[u] = None
        if found:
            log.info(f"Shinigami inline script: {len(found)} pages")
            return list(found), scraper.cookies.get_dict(), DEFAULT_UA

    except Exception as exc:
        log.warning(f"Shinigami HTML fallback error: {exc}")

    log.warning(f"Shinigami: all methods failed for {url}")
    return [], {}, DEFAULT_UA


def _extract_shinigami_urls(data, base_url: str) -> list[str]:
    """Pull image URLs out of whatever shape the Shinigami API returns."""
    urls: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str) and item.startswith("http"):
                urls.append(item)
            elif isinstance(item, dict):
                for key in ("url", "src", "image", "imageUrl", "img"):
                    val = item.get(key, "")
                    if val and isinstance(val, str) and val.startswith("http"):
                        urls.append(val)
                        break
    elif isinstance(data, dict):
        # look for a nested list of pages
        for key in ("images", "pages", "data", "chapter", "results"):
            sub = data.get(key)
            if sub:
                urls = _extract_shinigami_urls(sub, base_url)
                if urls:
                    break
    return urls


def _dig_for_image_urls(obj, base_url: str, depth: int = 0) -> list[str]:
    """Recursively search a parsed JSON object for arrays of image URLs."""
    if depth > 12:
        return []
    img_re = re.compile(
        r'https?://[^\s\'"<>]+\.(?:jpg|jpeg|png|webp|gif|avif)(?:\?[^\s\'"<>]*)?',
        re.IGNORECASE,
    )
    if isinstance(obj, str):
        if img_re.match(obj) and not _is_ui_url(obj):
            return [obj]
        return []
    if isinstance(obj, list):
        results: list[str] = []
        for item in obj:
            results.extend(_dig_for_image_urls(item, base_url, depth + 1))
        if len(results) > 3:   # looks like a real page list
            return results
        return results
    if isinstance(obj, dict):
        # prioritise keys that sound like page/image lists
        for key in ("images", "pages", "data", "dataSaver", "chapter", "results", "content"):
            sub = obj.get(key)
            if sub:
                found = _dig_for_image_urls(sub, base_url, depth + 1)
                if len(found) > 3:
                    return found
        # fall back to scanning all values
        all_found: list[str] = []
        for v in obj.values():
            all_found.extend(_dig_for_image_urls(v, base_url, depth + 1))
        return all_found
    return []


async def _scrape_mangadex_api(url: str) -> tuple[list[str], dict, str]:
    """
    For URLs containing a MangaDex chapter UUID (e.g. shinigami.asia, mangadex.org,
    or any scanlation site that re-uses MangaDex chapter IDs), fetch image URLs
    directly from the MangaDex at-home API — no browser needed.
    """
    m = _MANGADEX_UUID_RE.search(url)
    if not m:
        return [], {}, DEFAULT_UA

    chapter_id = m.group(1)
    api_url    = f"https://api.mangadex.org/at-home/server/{chapter_id}"
    log.info(f"Trying MangaDex at-home API for chapter {chapter_id}")

    try:
        async with aiohttp.ClientSession(headers={"User-Agent": DEFAULT_UA}) as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning(f"MangaDex API returned {resp.status}")
                    return [], {}, DEFAULT_UA
                data = await resp.json()
    except Exception as exc:
        log.warning(f"MangaDex API error: {exc}")
        return [], {}, DEFAULT_UA

    base_url   = data.get("baseUrl", "")
    ch         = data.get("chapter", {})
    hash_val   = ch.get("hash", "")
    pages      = ch.get("data", [])          # high quality
    if not pages:
        pages  = ch.get("dataSaver", [])     # compressed fallback

    if not base_url or not hash_val or not pages:
        log.warning("MangaDex API response missing expected fields")
        return [], {}, DEFAULT_UA

    image_urls = [f"{base_url}/data/{hash_val}/{p}" for p in pages]
    log.info(f"MangaDex API: found {len(image_urls)} pages")
    return image_urls, {}, DEFAULT_UA


async def _scrape_image_urls(url: str) -> tuple[list[str], dict, str]:
    """
    Scraping pipeline (fastest → slowest):
    1. Static HTML scrape (cloudscraper + BeautifulSoup)  — ~1s
    2. MangaDex at-home API (for any URL with a MangaDex UUID) — ~1s
    3. Playwright headless browser subprocess              — ~20-55s
    """
    # Step 1: static
    urls, cookies, ua = await asyncio.to_thread(_scrape_static, url)
    if urls:
        log.info(f"Static scrape found {len(urls)} image URL(s).")
        return urls, cookies, ua

    # Step 2a: Shinigami.asia direct API (handles JS-rendered chapter readers)
    if "shinigami.asia" in url:
        urls, cookies, ua = await _scrape_shinigami_api(url)
        if urls:
            return urls, cookies, ua

    # Step 2b: MangaDex direct API
    urls, cookies, ua = await _scrape_mangadex_api(url)
    if urls:
        return urls, cookies, ua

    # Step 3: Playwright headless browser (last resort)
    return await _scrape_playwright(url)



def _ext_from_content_type(ct: str, fallback_url: str) -> str:
    ct = ct.lower().split(";")[0].strip()
    mapping = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/svg+xml": ".svg",
        "image/bmp": ".bmp", "image/avif": ".avif",
    }
    if ct in mapping:
        return mapping[ct]
    path = urllib.parse.urlparse(fallback_url).path.lower()
    ext  = os.path.splitext(path)[1]
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
    buf         = io.BytesIO()
    name_counts: defaultdict[str, int] = defaultdict(int)
    count = 0
    with zipfile.ZipFile(buf, "w", compression=zip_compression, compresslevel=6) as zf:
        for index, img_url, content, content_type in results:
            if not content:
                continue
            if not _is_manhwa_panel(content, content_type):
                log.debug(f"Skipped (too small): {img_url}")
                continue
            ext      = _ext_from_content_type(content_type, img_url)
            raw_name = os.path.basename(urllib.parse.urlparse(img_url).path)
            base     = os.path.splitext(raw_name)[0][:60] if (raw_name and "." in raw_name) else f"image_{index:03d}"
            filename = f"{base}{ext}"
            if name_counts[filename]:
                filename = f"{base}_{name_counts[filename]}{ext}"
            name_counts[filename] += 1
            zf.writestr(f"{index:03d}_{filename}", content)
            count += 1
    buf.seek(0)
    return buf, count


def _split_zip(results, limit=DISCORD_SIZE_LIMIT) -> list[tuple[io.BytesIO, int]]:
    """Build ZIP and split into ≤ limit-byte chunks if needed."""
    buf, count = _build_zip(results)
    if len(buf.getvalue()) <= limit or count == 0:
        return [(buf, count)]

    file_pairs = [(i, u, c, ct) for i, u, c, ct in results if c]
    chunks: list[list] = []
    current_chunk: list = []
    current_size = 0
    OVERHEAD = 1024

    for pair in file_pairs:
        size = len(pair[2]) + OVERHEAD
        if current_chunk and current_size + size > limit:
            chunks.append(current_chunk)
            current_chunk = [pair]
            current_size  = size
        else:
            current_chunk.append(pair)
            current_size += size

    if current_chunk:
        chunks.append(current_chunk)

    return [_build_zip(chunk) for chunk in chunks]


def _split_stitch_zip(zip_buf: io.BytesIO, limit: int = DISCORD_SIZE_LIMIT) -> list[io.BytesIO]:
    """
    Split a stitch ZIP (containing JPEG strips) into multiple ZIPs each ≤ limit bytes.
    Reads the already-created strips and repacks them — no re-stitching needed.
    """
    zip_buf.seek(0)
    if len(zip_buf.getvalue()) <= limit:
        zip_buf.seek(0)
        return [zip_buf]

    # Extract all strips in sorted order
    strips: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(zip_buf, "r") as zf:
        for name in sorted(zf.namelist()):
            strips.append((name, zf.read(name)))

    parts: list[io.BytesIO] = []
    cur_buf  = io.BytesIO()
    cur_zf   = zipfile.ZipFile(cur_buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1)
    cur_size = 0
    OVERHEAD = 512

    for name, data in strips:
        size = len(data) + OVERHEAD
        if cur_size > 0 and cur_size + size > limit:
            cur_zf.close()
            cur_buf.seek(0)
            parts.append(cur_buf)
            cur_buf  = io.BytesIO()
            cur_zf   = zipfile.ZipFile(cur_buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1)
            cur_size = 0
        cur_zf.writestr(name, data)
        cur_size += size

    cur_zf.close()
    cur_buf.seek(0)
    parts.append(cur_buf)
    return parts


def _smart_stitch(results, max_strip_height: int = 15000) -> tuple[io.BytesIO, int]:
    """
    Fast vertical stitch: decode each image only once, check dimensions inline,
    process strip-by-strip to keep RAM low, save at JPEG quality 85.
    Returns (zip_buffer, strip_count).
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow is required for Smart Stitch. Run: pip install Pillow")

    # Sort by DOM index (reading order)
    sorted_items = sorted(
        [(idx, data, ct) for idx, _url, data, ct in results if data],
        key=lambda x: x[0],
    )

    # Decode + dimension filter in one pass (no double-decode)
    panels: list[tuple[PILImage.Image, int, int]] = []   # (img, w, h)
    for idx, data, ct in sorted_items:
        if "svg" in ct.lower():
            continue
        try:
            img = PILImage.open(io.BytesIO(data))
            img.load()
            w, h = img.size
            if w < MINIMUM_PANEL_WIDTH or h < MINIMUM_PANEL_HEIGHT:
                log.debug(f"Stitch skip (too small): {w}×{h}")
                continue
            if img.mode != "RGB":
                img = img.convert("RGB")
            panels.append((img, w, h))
        except Exception as exc:
            log.debug(f"Stitch decode error: {exc}")

    if not panels:
        raise ValueError("No valid panels to stitch after filtering.")

    # Pick target width = most common panel width
    widths   = [w for _, w, _ in panels]
    target_w = max(set(widths), key=widths.count)
    all_same_width = all(w == target_w for _, w, _ in panels)

    # Pack into strips and encode strip-by-strip (saves RAM)
    zip_buf      = io.BytesIO()
    strip_index  = 0
    strip_groups_meta: list[int] = []   # panel counts per strip

    current_imgs: list[PILImage.Image] = []
    current_h = 0

    def _flush_strip(group: list[PILImage.Image], s_idx: int, total_strips_hint: int):
        total_h = sum(img.size[1] for img in group)
        canvas  = PILImage.new("RGB", (target_w, total_h))
        y = 0
        for img in group:
            # Resize only if needed (BILINEAR is 4× faster than LANCZOS)
            if img.size[0] != target_w:
                ratio = target_w / img.size[0]
                new_h = max(1, int(img.size[1] * ratio))
                img   = img.resize((target_w, new_h), PILImage.BILINEAR)
            canvas.paste(img, (0, y))
            y += img.size[1]
        img_buf = io.BytesIO()
        # quality=85 is visually indistinguishable and ~40% faster than 95
        canvas.save(img_buf, format="JPEG", quality=85, optimize=False)
        img_buf.seek(0)
        return img_buf

    # We won't know total strips until we finish, so write to a temp list first
    strip_bufs: list[io.BytesIO] = []

    for img, w, h in panels:
        if current_imgs and current_h + h > max_strip_height:
            strip_bufs.append(_flush_strip(current_imgs, len(strip_bufs), 0))
            current_imgs = [img]
            current_h    = h
        else:
            current_imgs.append(img)
            current_h   += h

    if current_imgs:
        strip_bufs.append(_flush_strip(current_imgs, len(strip_bufs), 0))

    strip_count = len(strip_bufs)
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for i, sbuf in enumerate(strip_bufs, start=1):
            name = f"strip_{i:03d}_of_{strip_count:03d}.jpg"
            zf.writestr(name, sbuf.read())
            log.info(f"Stitch: packed strip {i}/{strip_count}")

    zip_buf.seek(0)
    return zip_buf, strip_count


# ─── Slash Commands ───────────────────────────────────────────────────────────

@client.tree.command(
    name="download_images",
    description="Scrape & download all manhwa images from any webpage.",
)
@app_commands.describe(
    url="The full URL of the webpage to scrape",
    delivery="How to receive the images: ZIP (auto-split if > 10 MB) or Smart Stitch",
    min_size="Ignore images smaller than this many KB (default: 5)",
    image_type="Only download this type (leave blank for all)",
    max_count="Maximum number of images to include (default: 300, max: 300)",
)
@app_commands.choices(
    delivery=[
        app_commands.Choice(name="📦 ZIP file (split if > 10 MB)",    value="zip"),
        app_commands.Choice(name="🧵 Smart Stitch (stitched strips)", value="stitch"),
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
    max_count: int = 300,
):
    await interaction.response.defer(thinking=True)

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    min_bytes   = max(0, min_size) * 1024
    max_count   = max(1, min(max_count, MAX_IMAGES))
    type_filter = None if image_type == "all" else image_type

    log.info(f"Scraping: {url}")
    image_urls, cookies, user_agent = await _scrape_image_urls(url)

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

    urls_to_dl    = image_urls[:max_count]
    delivery_label = "🧵 Smart Stitch" if delivery == "stitch" else "📦 ZIP"
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

    sem = asyncio.Semaphore(MAX_CONCURRENT_DL)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_DL, ssl=False)
    async with aiohttp.ClientSession(
        cookies=cookies,
        headers={
            "User-Agent": user_agent,
            "Referer":    url,
            "Accept":     "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
        connector=connector,
    ) as session:
        tasks   = [_fetch_one(session, sem, i, u, min_bytes) for i, u in enumerate(urls_to_dl)]
        results = await asyncio.gather(*tasks)

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
    # DELIVERY: Smart Stitch
    # ══════════════════════════════════════════════════════════════════════════
    if delivery == "stitch":
        stitching_embed = discord.Embed(
            title="🧵 Stitching Images…",
            description=(
                f"Downloaded **{successful}** panels.\n"
                "Stitching into strips of ≤ 15 000 px height… this may take a moment."
            ),
            color=discord.Color.blurple(),
        )
        await progress_msg.edit(embed=stitching_embed)

        try:
            zip_buf, strip_count = await asyncio.to_thread(_smart_stitch, results)
            zip_size = len(zip_buf.getvalue())
        except Exception as exc:
            log.error(f"Smart Stitch failed: {exc}")
            await progress_msg.edit(embed=discord.Embed(
                title="❌ Smart Stitch Failed",
                description=f"**Error:** {exc}",
                color=discord.Color.red(),
            ))
            return

        result_embed = discord.Embed(title="✅ Smart Stitch Ready!", color=discord.Color.green())
        result_embed.add_field(name="🌐 Source",            value=url,                             inline=False)
        result_embed.add_field(name="🖼️ Panels Downloaded", value=str(successful),                 inline=True)
        result_embed.add_field(name="🧵 Strips",            value=str(strip_count),                inline=True)
        result_embed.add_field(name="📊 Strip Height",      value="≤ 15 000 px each",              inline=True)
        result_embed.add_field(name="📦 ZIP Size",          value=f"{zip_size/1024/1024:.2f} MB",  inline=True)
        if type_filter:
            result_embed.add_field(name="🔍 Filter", value=type_filter, inline=True)
        result_embed.set_footer(text="Panels stitched at JPEG quality 85 · 15 000 px max strip height · split at 10 MB")
        await progress_msg.edit(embed=result_embed)

        # Split the stitch ZIP by strip (not by raw images) and send each part
        stitch_parts = await asyncio.to_thread(_split_stitch_zip, zip_buf)
        n = len(stitch_parts)
        for i, part_buf in enumerate(stitch_parts):
            fname = f"stitched_part{i+1:02d}_of_{n:02d}.zip" if n > 1 else "stitched.zip"
            await interaction.followup.send(file=discord.File(part_buf, filename=fname))
        return

    # ══════════════════════════════════════════════════════════════════════════
    # DELIVERY: ZIP (split at 10 MB)
    # ══════════════════════════════════════════════════════════════════════════
    zip_parts = await asyncio.to_thread(_split_zip, results)
    successful = sum(c for _, c in zip_parts)
    size_total = sum(len(b.getvalue()) for b, _ in zip_parts)

    result_embed = discord.Embed(title="✅ Images Ready!", color=discord.Color.green())
    result_embed.add_field(name="🌐 Source",       value=url,                              inline=False)
    result_embed.add_field(name="🖼️ Images Found", value=str(len(image_urls)),             inline=True)
    result_embed.add_field(name="⬇️ Downloaded",   value=str(successful),                  inline=True)
    result_embed.add_field(name="📦 ZIP Size",      value=f"{size_total/1024/1024:.2f} MB", inline=True)
    if type_filter:
        result_embed.add_field(name="🔍 Filter", value=type_filter, inline=True)
    if len(zip_parts) > 1:
        result_embed.add_field(
            name="⚡ Split ZIPs",
            value=f"ZIP exceeded 10 MB — split into **{len(zip_parts)}** parts",
            inline=False,
        )
    result_embed.set_footer(text="Images packed with ZIP_DEFLATE compression · split at 10 MB")

    files = [
        discord.File(buf, filename=f"images_part{i+1:02d}.zip" if len(zip_parts) > 1 else "images.zip")
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

    image_urls, cookies, user_agent = await _scrape_image_urls(url)
    if not image_urls:
        await interaction.followup.send("❌ No images found on that page.")
        return

    sem = asyncio.Semaphore(10)
    connector = aiohttp.TCPConnector(limit=10, ssl=False)
    async with aiohttp.ClientSession(
        cookies=cookies,
        headers={"User-Agent": user_agent, "Referer": url},
        connector=connector,
    ) as session:
        tasks   = [_fetch_one(session, sem, i, u, MIN_IMAGE_BYTES) for i, u in enumerate(image_urls[:count])]
        results = await asyncio.gather(*tasks)

    files = [
        discord.File(io.BytesIO(content), filename=f"preview_{index:02d}{_ext_from_content_type(ct, img_url)}")
        for index, img_url, content, ct in results if content
    ]
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
        description="A powerful Discord bot that extracts and downloads all manhwa images from any webpage.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📥 /download_images",
        value=(
            "Scrape + download all manhwa panel images.\n"
            "`url` — page to scrape *(required)*\n"
            "`delivery` — **📦 ZIP** (auto-split at 10 MB) or **🧵 Smart Stitch**\n"
            "`min_size` — skip images below X KB *(default 5)*\n"
            "`image_type` — filter by format (jpg/png/gif/webp/svg/avif)\n"
            "`max_count` — cap images to download *(default 300, max 300)*"
        ),
        inline=False,
    )
    embed.add_field(
        name="👁️ /preview_images",
        value="Preview the first few images from a page directly in Discord.",
        inline=False,
    )
    embed.add_field(
        name="⚡ Features",
        value=(
            "• Bypasses basic Cloudflare protection\n"
            "• Scrapes `<img>` & `<picture>` tags in DOM order\n"
            "• Concurrent downloads with rate limiting\n"
            "• Manhwa filter: skips UI images, icons & small thumbnails\n"
            "• **ZIP**: auto-splits into parts if > 10 MB\n"
            "• **Smart Stitch**: stitches panels into ≤ 15 000 px tall strips\n"
            "• Deduplicates filenames"
        ),
        inline=False,
    )
    embed.set_footer(text="Built with discord.py · cloudscraper · aiohttp · BeautifulSoup4 · Pillow · Playwright")
    await interaction.response.send_message(embed=embed)


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        log.error("DISCORD_TOKEN is not set. Add it to your .env file.")
    else:
        client.run(TOKEN, log_handler=None)
