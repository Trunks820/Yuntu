FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system dependencies & CJK fonts (for Chinese PDF export)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates fonts-noto-cjk curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY src ./src
COPY scripts ./scripts
COPY sql ./sql
COPY README.md ./

EXPOSE 6666

CMD ["python", "-m", "uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "6666"]
