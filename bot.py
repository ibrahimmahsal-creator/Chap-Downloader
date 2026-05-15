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
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

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
        # Using cloudscraper to bypass simple anti-bot protections (like Cloudflare)
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
                
        # 2. Find <source> inside <picture> tags (for responsive images)
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
                    
        return list(image_urls)
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return []

@client.tree.command(name="download_images", description="Extract and download all images from a webpage (like Imageye).")
@app_commands.describe(url="The URL of the webpage to scrape")
async def download_images(interaction: discord.Interaction, url: str):
    # Defer the response since scraping and downloading might take a while
    await interaction.response.defer(thinking=True)
    
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
        
    image_urls = get_images_from_url(url)
    
    # Filter out common non-image paths or very small tracking pixels if desired
    # For now, we try to download them all
    
    if not image_urls:
        await interaction.followup.send(f"No images found on {url} or the page is protected/requires JavaScript rendering.")
        return
        
    await interaction.followup.send(f"Found {len(image_urls)} images. Downloading and packing them into a zip file...")
    
    zip_buffer = io.BytesIO()
    scraper = cloudscraper.create_scraper()
    
    with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
        count = 0
        for img_url in image_urls:
            if count >= 200:  # Hard limit to avoid giant files and memory issues
                break
                
            try:
                img_response = scraper.get(img_url, timeout=5)
                if img_response.status_code == 200:
                    content_type = img_response.headers.get('content-type', '')
                    
                    # Guess extension from content type
                    ext = '.jpg'
                    if 'png' in content_type: ext = '.png'
                    elif 'gif' in content_type: ext = '.gif'
                    elif 'webp' in content_type: ext = '.webp'
                    elif 'svg' in content_type: ext = '.svg'
                    
                    # Try to get original filename
                    parsed = urllib.parse.urlparse(img_url)
                    path_name = os.path.basename(parsed.path)
                    if path_name and '.' in path_name:
                        # Clean up query params from filename if any
                        filename = path_name
                    else:
                        filename = f"image_{count}{ext}"
                        
                    # Handle duplicate filenames in zip
                    filename = f"{count}_{filename}"
                        
                    zip_file.writestr(filename, img_response.content)
                    count += 1
            except Exception as e:
                print(f"Failed to download {img_url}: {e}")
                
    zip_buffer.seek(0)
    
    # Check size limit (Discord allows 25MB for regular users)
    size_mb = len(zip_buffer.getvalue()) / (1024 * 1024)
    if size_mb > 25:
        await interaction.channel.send(f"⚠️ The zip file is too large ({size_mb:.2f} MB) to send directly. Discord's limit is 25MB. \n\nHowever, {count} images were found. (We can implement Google Drive upload here later!)")
    else:
        file = discord.File(fp=zip_buffer, filename="extracted_images.zip")
        await interaction.channel.send(f"✅ Successfully downloaded {count} images from {url}:", file=file)

if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        print("Please set your DISCORD_TOKEN in the .env file")
    else:
        client.run(TOKEN)
