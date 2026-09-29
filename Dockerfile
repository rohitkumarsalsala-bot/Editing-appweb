FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-ai.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-ai.txt
COPY . .
ENV PORT=8000 WHISPER_MODEL=small
CMD ["sh","-c","gunicorn -w 1 --threads 8 -t 1800 -b 0.0.0.0:${PORT} server:app"]
