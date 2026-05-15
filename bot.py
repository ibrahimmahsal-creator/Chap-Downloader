import tempfile
import discord
from discord.ext import commands
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
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# Dummy web server for Render health checks running in a separate thread
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is awake and running!")

def start_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    print(f"Dummy web server listening on port {port} for Render")
    server.serve_forever()

# Start the web server immediately in the background
threading.Thread(target=start_web_server, daemon=True).start()

class ImageScraperBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Sync the slash commands with Discord
        await self.tree.sync()

client = ImageScraperBot()

@client.event
async def on_ready():
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print('Bot is ready to scrape images!')
    print('------')

def get_images_from_url(url):
    try:
        # Using cloudscraper to bypass simple anti-bot protections
        scraper = cloudscraper.create_scraper()
        response = scraper.get(url, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        image_urls = set()
        
        # 1. Find all <img> tags
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src') or img.get('data-original') or img.get('data-lazy-src')
            if src:
                image_urls.add(urllib.parse.urljoin(url, src))
                
        # 2. Find <source> inside <picture> tags
        for source in soup.find_all('source'):
            srcset = source.get('srcset')
            if srcset:
                urls = [u.strip().split(' ')[0] for u in srcset.split(',')]
                for u in urls:
                    if u:
                        image_urls.add(urllib.parse.urljoin(url, u))
                        
        # 3. Find background images in inline CSS styles
        for tag in soup.find_all(style=True):
            style = tag.get('style')
            urls = re.findall(r'url\([\'"]?(.*?)[\'"]?\)', style)
            for u in urls:
                if u and not u.startswith('data:'):
                    image_urls.add(urllib.parse.urljoin(url, u))
                    
        return list(image_urls), scraper.cookies.get_dict(), scraper.headers.get('User-Agent', '')
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return [], {}, ""

@client.tree.command(name="download_images", description="Extract and download all images from a webpage (like Imageye).")
@app_commands.describe(url="The URL of the webpage to scrape")
async def download_images(interaction: discord.Interaction, url: str):
    # Defer the response since scraping and downloading might take a while
    await interaction.response.defer(thinking=True)
    
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
        
    image_urls, cookies, user_agent = await asyncio.to_thread(get_images_from_url, url)
    
    if not image_urls:
        await interaction.followup.send(f"No images found on {url} or the page is protected/requires JavaScript rendering.")
        return
        
    await interaction.followup.send(f"Found {len(image_urls)} images. Downloading concurrently and packing into a zip...")
    
    # We will limit to 200 images max
    urls_to_download = list(image_urls)[:200]
    
    # Use a temporary directory to save files to the disk instead of RAM
    with tempfile.TemporaryDirectory() as temp_dir:
        
        async def fetch_and_save(session, index, img_url):
            try:
                async with session.get(img_url, timeout=15) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')
                        ext = '.jpg'
                        if 'png' in content_type: ext = '.png'
                        elif 'gif' in content_type: ext = '.gif'
                        elif 'webp' in content_type: ext = '.webp'
                        elif 'svg' in content_type: ext = '.svg'
                        
                        parsed = urllib.parse.urlparse(img_url)
                        path_name = os.path.basename(parsed.path)
                        filename = path_name if path_name and '.' in path_name else f"image_{index}{ext}"
                        filename = f"{index:03d}_{filename}"
                        filepath = os.path.join(temp_dir, filename)
                        
                        # Write the file to disk in chunks to save memory
                        with open(filepath, 'wb') as f:
                            async for chunk in response.content.iter_chunked(1024):
                                f.write(chunk)
                        return True
            except Exception as e:
                print(f"Failed to download {img_url}: {e}")
            return False

        # Download everything purely async and save to temp_dir
        async with aiohttp.ClientSession(cookies=cookies, headers={'User-Agent': user_agent}) as session:
            tasks = [fetch_and_save(session, i, u) for i, u in enumerate(urls_to_download)]
            results = await asyncio.gather(*tasks)

        # Count how many images successfully downloaded
        count = sum(results)
        
        if count == 0:
            await interaction.followup.send(f"⚠️ Failed to download any images from {url}. They might be protected or broken links.")
            return

        # Create the zip file directly on the disk
        zip_path = os.path.join(temp_dir, "extracted_images.zip")
        with zipfile.ZipFile(zip_path, 'w') as zip_file:
            for root, _, files in os.walk(temp_dir):
                for file in files:
                    if file != "extracted_images.zip":
                        zip_file.write(os.path.join(root, file), arcname=file)
                        
        # Check size limit (Discord allows 25MB for regular users)
        size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        
        if size_mb > 25:
            await interaction.followup.send(f"⚠️ The zip file is too large ({size_mb:.2f} MB) to send directly. Discord's limit is 25MB. \n\nHowever, {count} images were successfully scraped.")
        else:
            # Send the zip file from the disk
            file = discord.File(fp=zip_path, filename="extracted_images.zip")
            await interaction.followup.send(f"✅ Successfully downloaded {count} images from {url}:", file=file)

if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        print("Please set your DISCORD_TOKEN in the .env file")
    else:
        client.run(TOKEN)
