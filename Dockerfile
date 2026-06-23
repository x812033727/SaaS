# SaaS MVP — 正式部署映像（多 worker，gunicorn + UvicornWorker）
# 多 worker 水平擴展請搭配 PostgreSQL（FOR UPDATE 行鎖）+ Redis 限流後端，
# 詳見 README「Production / 多 worker 部署」。
FROM python:3.12-slim AS base

# - PYTHONDONTWRITEBYTECODE：不寫 .pyc，映像更乾淨
# - PYTHONUNBUFFERED：stdout/stderr 即時輸出（容器日誌即時可見）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Rich Menu 自動印字需中文字型（Pillow 由 pip 裝，字型來自系統套件）。
# fonts-wqy-zenhei 體積小（~2MB）且涵蓋繁簡中文，符合 slim 映像精神；缺字型時
# 程式會靜默回退純色底圖（不影響啟動）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# 先只複製套件中繼資料 + 原始碼以利 layer 快取（psycopg[binary]/cryptography 皆有 wheel，
# 無需系統編譯工具）。安裝含 prod 額外相依（gunicorn + psycopg + redis）。
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[prod]"

# 進入點 + 排程腳本
COPY docker/entrypoint.sh docker/scheduler.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/scheduler.sh

# 以非 root 執行（資安最佳實務）
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# 容器層級健康檢查打 /healthz（DB SELECT 1 + 生效限流後端）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

ENTRYPOINT ["entrypoint.sh"]
# 預設啟動 web（多 worker）；scheduler 服務在 compose 以 'scheduler' 參數覆寫。
CMD ["web"]
