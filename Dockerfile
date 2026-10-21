FROM node:22-bookworm-slim AS webui-builder

WORKDIR /build/webui-new
COPY webui-new/package.json webui-new/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY webui-new/ ./
RUN npm run build


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .
COPY --from=webui-builder /build/webui-new/dist ./webui-new/dist

RUN mkdir -p /app/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()"

CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
