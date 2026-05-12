FROM python:3.11-slim-bookworm

WORKDIR /app

RUN pip install --no-cache-dir playwright==1.47.0 \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY bot.py .

CMD ["python", "-u", "bot.py"]
