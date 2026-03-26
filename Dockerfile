FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Debug: check if DB is real or LFS pointer
RUN ls -lh voca_20k.db && head -1 voca_20k.db && wc -c < voca_20k.db

EXPOSE 7777

CMD ["python3", "oxe_server.py"]
