import discord
from discord.ext import commands
import os
import shutil
import asyncio
from aiohttp import web
from discord import app_commands
from scraper import scrape_manhwa_images
from drive_uploader import upload_folder_to_drive

# إعدادات البوت (تم إلغاء الحاجة لقراءة الرسائل)
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# أزرار الاختيار
class DeliveryMethodView(discord.ui.View):
    def __init__(self, url: str, chapter_name: str):
        super().__init__(timeout=300)
        self.url = url
        self.chapter_name = chapter_name

    @discord.ui.button(label="ZIP File", style=discord.ButtonStyle.primary, emoji="📦")
    async def btn_zip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⏳ جاري سحب الصور وضغطها، يرجى الانتظار...", ephemeral=True)
        
        folder_name = f"downloads/{self.chapter_name}"
        zip_name = f"{folder_name}.zip"
        
        try:
            # سحب الصور
            count = await scrape_manhwa_images(self.url, folder_name)
            
            if count == 0:
                await interaction.followup.send("❌ لم أتمكن من إيجاد أي صور للمانهوا في هذا الرابط.")
                return
                
            # ضغط المجلد
            shutil.make_archive(folder_name, 'zip', folder_name)
            
            # التأكد من الحجم قبل الإرسال (Discord Limit: 25MB)
            file_size_mb = os.path.getsize(zip_name) / (1024 * 1024)
            if file_size_mb > 25:
                await interaction.followup.send(f"⚠️ حجم الملف ({file_size_mb:.1f} MB) يتجاوز حد الديسكورد المسموح (25MB). يرجى الضغط على زر Google Drive بدلاً من ذلك.")
            else:
                file = discord.File(zip_name)
                await interaction.followup.send(content=f"✅ تم تحميل **{count}** صورة بنجاح!", file=file)
                
        except Exception as e:
            await interaction.followup.send(f"❌ حدث خطأ: {e}")
        finally:
            # تنظيف الملفات
            shutil.rmtree(folder_name, ignore_errors=True)
            if os.path.exists(zip_name):
                os.remove(zip_name)

    @discord.ui.button(label="Google Drive", style=discord.ButtonStyle.success, emoji="☁️")
    async def btn_drive(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⏳ جاري سحب الصور ورفعها على درايف، يرجى الانتظار...", ephemeral=True)
        
        folder_name = f"downloads/{self.chapter_name}"
        
        try:
            # سحب الصور
            count = await scrape_manhwa_images(self.url, folder_name)
            
            if count == 0:
                await interaction.followup.send("❌ لم أتمكن من إيجاد أي صور للمانهوا في هذا الرابط.")
                return
                
            # الرفع على درايف بخيط منفصل (Thread) لعدم إيقاف البوت
            loop = asyncio.get_event_loop()
            drive_link = await loop.run_in_executor(None, upload_folder_to_drive, folder_name, self.chapter_name)
            
            await interaction.followup.send(f"✅ تم سحب **{count}** صورة والرفع بنجاح!\nرابط درايف: {drive_link}")
            
        except Exception as e:
            await interaction.followup.send(f"❌ حدث خطأ أثناء الرفع: {str(e)}")
        finally:
            # تنظيف الملفات
            shutil.rmtree(folder_name, ignore_errors=True)

@bot.tree.command(name="download", description="تحميل فصل مانهوا من موقع")
@app_commands.describe(url="رابط الفصل", chapter_name="اسم الفصل (اختياري)")
async def download(interaction: discord.Interaction, url: str, chapter_name: str = "Manhwa_Chapter"):
    """
    أمر التحميل: /download [url] [chapter_name]
    """
    view = DeliveryMethodView(url, chapter_name)
    await interaction.response.send_message(f"📥 تم استلام الرابط! كيف تريد استلام الفصل **{chapter_name}**؟", view=view)

@bot.tree.command(name="ping", description="فحص سرعة استجابة البوت")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 بونج! سرعة الاتصال: `{latency}ms`")

@bot.tree.command(name="help", description="عرض أوامر وطريقة استخدام البوت")
async def help_cmd(interaction: discord.Interaction):
    help_text = (
        "**📚 قائمة أوامر بوت المانهوا:**\n\n"
        "📥 `/download [url] [chapter_name]` : لتحميل أي فصل مانهوا واختيار (ZIP / Google Drive).\n"
        "🏓 `/ping` : لعرض سرعة استجابة البوت.\n"
        "ℹ️ `/help` : لعرض هذه القائمة."
    )
    await interaction.response.send_message(help_text)

async def handle(request):
    return web.Response(text="Discord Bot is running smoothly!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Dummy web server started on port {port}")

@bot.event
async def on_ready():
    print(f"✅ Bot is logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ تم تفعيل أوامر السلاش ({len(synced)} command)")
    except Exception as e:
        print(f"❌ خطأ في السلاش كوماند: {e}")
    await start_dummy_server()

if __name__ == '__main__':
    # جلب التوكن من متغيرات البيئة (مهم جداً للسيرفرات مثل Render)
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_DISCORD_TOKEN_HERE")
    
    if BOT_TOKEN == "YOUR_DISCORD_BOT_TOKEN_HERE" or not BOT_TOKEN:
        print("❌ يرجى تعيين BOT_TOKEN كمتغير بيئة (Environment Variable) أو وضعه في الكود مباشرة للتجربة المحلية!")
    else:
        bot.run(BOT_TOKEN)
