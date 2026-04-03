# 用于云部署（Render / Railway / Fly.io 等），获得固定公网域名
FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

# 云平台会注入 PORT；本地默认 8000
EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
