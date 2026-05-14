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
            
            # استخراج الصور باستخدام جافاسكريبت لضمان دقة أعلى وتخطي مشاكل الـ CSS
            image_urls = await page.evaluate("""
                () => {
                    let urls = [];
                    let imgs = document.querySelectorAll('img');
                    for (let img of imgs) {
                        // محاولة جلب الرابط الحقيقي (لتخطي الـ Lazy Loading)
                        let src = img.getAttribute('data-src') || 
                                  img.getAttribute('data-lazy-src') || 
                                  img.getAttribute('data-original') || 
                                  img.src;
                                  
                        if (!src || src.startsWith('data:image')) continue;
                        
                        // بعض المواقع تقطع المانهوا لشرائح صغيرة، لذلك سنقلل الحد الأدنى
                        let isBig = (img.naturalWidth > 200 && img.naturalHeight > 150) || 
                                    (img.width > 200 && img.height > 150);
                                    
                        // بعض المواقع تستخدم كلاسات محددة لصور الفصول
                        let isChapterImg = img.className.includes('wp-manga') || 
                                           img.className.includes('page-break') ||
                                           img.className.includes('reader');
                        
                        if (isBig || isChapterImg) {
                            if (src.startsWith('http')) {
                                urls.push(src);
                            } else if (src.startsWith('//')) {
                                urls.push(window.location.protocol + src);
                            } else if (src.startsWith('/')) {
                                urls.push(window.location.origin + src);
                            }
                        }
                    }
                    return [...new Set(urls)]; // إزالة التكرار
                }
            """)
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
