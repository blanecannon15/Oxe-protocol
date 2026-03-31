FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg git git-lfs curl && rm -rf /var/lib/apt/lists/* && git lfs install

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Resolve LFS pointer for the seed database
RUN if [ "$(wc -c < voca_20k.db)" -lt 1000 ]; then \
      echo "DB is LFS pointer ($(wc -c < voca_20k.db) bytes), downloading..." && \
      for i in 1 2 3; do \
        curl -fL --retry 3 --retry-delay 5 --connect-timeout 30 --max-time 600 \
          -o voca_20k.db \
          "https://github.com/blanecannon15/Oxe-protocol/raw/main/voca_20k.db" && break; \
        echo "Attempt $i failed, retrying..." && sleep 10; \
      done; \
    fi && \
    python3 -c "import os; s=os.path.getsize('voca_20k.db'); print(f'DB size: {s/1024/1024:.0f} MB'); assert s > 1000, f'DB is only {s} bytes — LFS not resolved'"

# Volume mount for persistent DB (set RAILWAY_VOLUME_MOUNT_PATH=/data in Railway)
# On first deploy: repo DB seeds to /data/voca_20k.db
# On subsequent deploys: /data/voca_20k.db is preserved (training progress kept)
VOLUME ["/data"]

EXPOSE 7777

CMD ["python3", "oxe_server.py"]
