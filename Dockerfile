FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg git git-lfs && rm -rf /var/lib/apt/lists/* && git lfs install

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7777

CMD ["python3", "oxe_server.py"]
