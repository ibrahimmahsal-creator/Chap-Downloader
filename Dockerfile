# نستخدم صورة Playwright الرسمية المجهزة بالكامل لتعمل مع بايثون والمتصفحات
FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

# تعيين مجلد العمل داخل السيرفر
WORKDIR /app

# نسخ ملف المكتبات
COPY requirements.txt .

# تثبيت المكتبات الخاصة ببايثون
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع (البوت)
COPY . .

# تشغيل البوت
CMD ["python", "main.py"]
