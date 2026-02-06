FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir yt-dlp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY cookies.txt /app/cookies.txt
ENV YTDLP_COOKIES_PATH=/app/cookies.txt

ENV PORT=8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]