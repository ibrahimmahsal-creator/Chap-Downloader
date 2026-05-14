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
            
            # استنساخ خوارزمية (Imageye) لسحب جميع الصور الممكنة في الصفحة
            image_urls = await page.evaluate("""
                () => {
                    let urls = [];
                    
                    // 1. استخراج كل صور <img>
                    let imgs = document.querySelectorAll('img');
                    for (let img of imgs) {
                        let src = img.getAttribute('data-src') || 
                                  img.getAttribute('data-lazy-src') || 
                                  img.getAttribute('data-original') || 
                                  img.src;
                                  
                        if (!src || src.startsWith('data:image')) continue;
                        
                        let w = img.naturalWidth || img.width || img.clientWidth || 0;
                        let h = img.naturalHeight || img.height || img.clientHeight || 0;
                        
                        // المشكلة كانت في الـ Lazy Loading الذي يجعل الأبعاد 0. 
                        // لذلك سنعتمد على اسم الكلاس أو مسار الرابط أيضاً!
                        let isMangaImg = img.className.includes('wp-manga') || 
                                         img.className.includes('page-break') ||
                                         img.className.includes('reader') ||
                                         img.className.includes('chapter-image') ||
                                         src.includes('/chapter/') ||
                                         src.includes('/manga/') ||
                                         src.includes('/uploads/');
                                         
                        if (isMangaImg || (w >= 100 && h >= 20)) {
                            urls.push(src);
                        }
                    }
                    
                    // 2. استخراج صور خلفيات CSS (كما تفعل أداة Imageye)
                    let allElements = document.querySelectorAll('*');
                    for (let el of allElements) {
                        let style = window.getComputedStyle(el);
                        let bg = style.backgroundImage;
                        if (bg && bg !== 'none' && bg.includes('url(')) {
                            let match = bg.match(/url\(['"]?(.*?)['"]?\)/);
                            if (match && match[1] && !match[1].startsWith('data:image')) {
                                let w = el.clientWidth || 0;
                                let h = el.clientHeight || 0;
                                if (w >= 100 && h >= 20) {
                                    urls.push(match[1]);
                                }
                            }
                        }
                    }
                    
                    // تنظيف وتحويل جميع الروابط لتكون كاملة
                    let finalUrls = urls.map(src => {
                        if (src.startsWith('http')) return src;
                        if (src.startsWith('//')) return window.location.protocol + src;
                        if (src.startsWith('/')) return window.location.origin + src;
                        return window.location.origin + '/' + src;
                    });
                    
                    // إزالة التكرارات مع الحفاظ على الترتيب
                    return [...new Set(finalUrls)];
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
