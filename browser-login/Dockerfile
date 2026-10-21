FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DISPLAY=:99

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        xvfb \
        x11vnc \
        x11-utils \
        novnc \
        websockify \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

COPY browser_login.py entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

EXPOSE 6080 6081 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"

ENTRYPOINT ["/app/entrypoint.sh"]
