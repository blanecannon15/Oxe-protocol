FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg git git-lfs curl && rm -rf /var/lib/apt/lists/* && git lfs install

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Resolve LFS pointer for the database (Railway build context doesn't resolve LFS)
RUN if [ "$(wc -c < voca_20k.db)" -lt 1000 ]; then \
      echo "DB is LFS pointer, downloading from GitHub LFS..." && \
      git lfs pull 2>/dev/null || \
      curl -L -o voca_20k.db \
        "https://github.com/blanecannon15/Oxe-protocol/raw/main/voca_20k.db" && \
      echo "Downloaded: $(wc -c < voca_20k.db) bytes"; \
    fi

# Verify the DB is real
RUN python3 -c "import os; s=os.path.getsize('voca_20k.db'); print(f'DB size: {s/1024/1024:.0f} MB'); assert s > 1000, f'DB is only {s} bytes — LFS not resolved'"

EXPOSE 7777

CMD ["python3", "oxe_server.py"]
