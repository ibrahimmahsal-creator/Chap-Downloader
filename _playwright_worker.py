#!/usr/bin/env python3
"""
Standalone Playwright scraper — called as a subprocess by bot.py.
Usage: python _playwright_worker.py <url>
Output: JSON to stdout  {"urls": [...], "cookies": {...}}
"""
import asyncio, json, sys, re, os, urllib.parse

SUPPORTED_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.avif'}
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_UI_SKIP = {
    "logo","icon","favicon","avatar","banner","sprite","button","arrow",
    "loading","spinner","placeholder","blank","pixel","tracking","ads",
    "badge","rating","star","social","share","facebook","twitter",
    "discord","patreon","header","footer","nav","menu","sidebar",
    "widget","bg","background","pattern","texture","watermark",
}

IMG_RE = re.compile(
    r'https?://[^\s\'"<>]+\.(?:jpg|jpeg|png|webp|gif|avif|bmp)(?:\?[^\s\'"<>]*)?',
    re.IGNORECASE,
)

def _is_ui(url):
    tokens = set(re.split(r'[/_\-.]', urllib.parse.urlparse(url).path.lower()))
    return bool(tokens & _UI_SKIP)

def _add(url, store):
    if not url or url.startswith("data:") or _is_ui(url):
        return
    ext = os.path.splitext(urllib.parse.urlparse(url).path.lower())[1]
    if ext == "" or ext in SUPPORTED_EXTS:
        store[url] = None

async def scrape(url):
    from playwright.async_api import async_playwright
    from bs4 import BeautifulSoup

    intercepted, json_imgs = {}, {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=DEFAULT_UA,
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await ctx.new_page()

        def on_request(req):
            if req.resource_type == "image":
                _add(req.url, intercepted)

        async def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    text = await resp.text()
                    
                    # Specialized Shinigami API parsing
                    if "shngm.io/v1/chapter/detail" in resp.url:
                        data = json.loads(text)
                        api_data = data.get("data", {})
                        base = api_data.get("base_url", "https://assets.shngm.id")
                        chap = api_data.get("chapter", {})
                        path = chap.get("path", "")
                        for p in chap.get("data", []):
                            _add(f"{base}{path}{p}", json_imgs)

                    for m in IMG_RE.finditer(text):
                        _add(m.group(0), json_imgs)
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="load", timeout=25_000)
        except Exception as e:
            print(f"goto: {e}", file=sys.stderr)

        await asyncio.sleep(5)

        try:
            await page.evaluate("""async () => {
                await new Promise(r => {
                    const total = document.body.scrollHeight;
                    let done = 0;
                    const step = Math.max(400, Math.floor(total/15));
                    const t = setInterval(() => {
                        window.scrollBy(0, step); done += step;
                        if (done >= total) { clearInterval(t); r(); }
                    }, 150);
                });
            }""")
            await asyncio.sleep(3)
        except Exception:
            pass

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Scan <script> tags for embedded image URLs
        for tag in soup.find_all("script"):
            for m in IMG_RE.finditer(tag.string or ""):
                _add(m.group(0), json_imgs)

        # DOM <img> tags
        dom = {}
        for img in soup.find_all("img"):
            for attr in ("src","data-src","data-original","data-lazy-src","data-url"):
                val = img.get(attr,"")
                if val:
                    _add(urllib.parse.urljoin(url, val.strip()), dom)

        cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
        await browser.close()

    all_urls = {}
    for d in (json_imgs, intercepted, dom):
        all_urls.update(d)

    return {"urls": list(all_urls), "cookies": cookies}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"urls": [], "cookies": {}}))
        sys.exit(0)
    try:
        result = asyncio.run(scrape(sys.argv[1]))
    except Exception as e:
        print(f"worker error: {e}", file=sys.stderr)
        result = {"urls": [], "cookies": {}}
    print(json.dumps(result))
