FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg git git-lfs && rm -rf /var/lib/apt/lists/* && git lfs install

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Resolve LFS pointers (DB file may be a pointer in the build context)
RUN git lfs pull || echo "LFS pull skipped (no remote or already resolved)"

# Verify the DB is real, not a pointer
RUN python3 -c "import os; s=os.path.getsize('voca_20k.db'); print(f'DB size: {s/1024/1024:.0f} MB'); assert s > 1000, f'DB is only {s} bytes — LFS not resolved'"

EXPOSE 7777

CMD ["python3", "oxe_server.py"]
