import os
import aiohttp
import aiofiles
from playwright.async_api import async_playwright

async def scrape_manhwa_images(url: str, output_dir: str):
    """
    يقوم بفتح الرابط واستخراج صور المانهوا وتحميلها في المجلد المحدد.
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    async with async_playwright() as p:
        # تشغيل المتصفح في الخلفية
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            # فتح الرابط
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # النزول لأسفل الصفحة ببطء لتحميل الصور (Lazy Loading)
            await page.evaluate("""
                async () => {
                    await new Promise((resolve, reject) => {
                        var totalHeight = 0;
                        var distance = 500;
                        var timer = setInterval(() => {
                            var scrollHeight = document.body.scrollHeight;
                            window.scrollBy(0, distance);
                            totalHeight += distance;

                            if(totalHeight >= scrollHeight){
                                clearInterval(timer);
                                resolve();
                            }
                        }, 100);
                    });
                }
            """)
            
            # الانتظار قليلاً لضمان تحميل الصور
            await page.wait_for_timeout(2000)
            
            # استخراج الصور
            images = await page.locator("img").all()
            image_urls = []
            
            for img in images:
                box = await img.bounding_box()
                # الخوارزمية: الصورة يجب أن تكون كبيرة لتكون صفحة مانهوا
                if box and box["width"] > 300 and box["height"] > 400:
                    src = await img.get_attribute("src")
                    if not src or "data:image" in src:
                        # بعض المواقع تضع الرابط الحقيقي في data-src أو داتا مشابهة
                        src = await img.get_attribute("data-src")
                        if not src:
                            src = await img.get_attribute("data-lazy-src")
                    
                    if src and src.startswith("http"):
                        image_urls.append(src)
        except Exception as e:
            print(f"Error while scraping page: {e}")
        finally:
            await browser.close()

    # تحميل الصور وتخزينها
    async with aiohttp.ClientSession() as session:
        for i, img_url in enumerate(image_urls):
            try:
                # إضافة headers لتخطي بعض حمايات المواقع البسيطة
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
                async with session.get(img_url, headers=headers) as resp:
                    if resp.status == 200:
                        file_path = os.path.join(output_dir, f"page_{i:03d}.jpg")
                        async with aiofiles.open(file_path, mode='wb') as f:
                            await f.write(await resp.read())
            except Exception as e:
                print(f"Error downloading {img_url}: {e}")
                
    return len(image_urls)
