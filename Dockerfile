# 🦅 EAGLE Panel — Railway MINI
# کل پنل فقط همین ریپوست: app.py + web/ + همین Dockerfile — بدون pages.py و main.py!
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    DATA_DIR=/data

WORKDIR /app

# وابستگی‌ها (داخل خود Dockerfile — دیگر requirements.txt لازم نیست)
RUN pip install --no-cache-dir \
      fastapi==0.109.0 \
      "uvicorn[standard]==0.27.0" \
      aiofiles==23.2.1 \
      psutil==5.9.7 \
      httpx==0.26.0 \
      python-multipart==0.0.9 \
      jdatetime==4.1.1 \
      qrcode==8.2

# کد پنل (app.py + web/)
COPY . .

# دیتای پنل (Volume ریلوی → /data)
VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "app.py"]
