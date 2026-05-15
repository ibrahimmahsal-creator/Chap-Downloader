# Image Downloader Bot (Imageye Clone for Discord)

This is a Discord bot that works like the "Image Downloader - Imageye" Chrome extension. Given a URL, the bot scrapes the webpage, extracts all the images (from `<img>` tags, `<picture>`, and inline CSS background images), downloads them, and sends them to the channel as a `.zip` file.

## Features
- **Smart Image Extraction**: Parses standard image tags, responsive sources, and CSS backgrounds.
- **Bypasses Basic Protection**: Uses `cloudscraper` to bypass simple anti-bot mechanisms like Cloudflare.
- **ZIP Compression**: Packages all images into a zip file for easy downloading.
- **Discord Slash Commands**: Uses modern `/` commands to interact.

## Setup Instructions

1. Ensure you have Python installed on your system.
2. Open a terminal in this directory and install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Open the `.env` file and replace `your_discord_bot_token_here` with your actual Discord Bot Token.
4. Run the bot:
   ```bash
   python bot.py
   ```

## Usage

In Discord, type the following command:
```
/download_images url: <your_website_url>
```

The bot will scrape the site, download the images, and send you a `.zip` file. Note that Discord has a 25 MB limit for attachments; if the images exceed this size, the bot will notify you.
