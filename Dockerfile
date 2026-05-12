FROM python:3.12-slim

WORKDIR /app

# Install Playwright Python SDK + headless Chromium + OS deps in one shot.
# `--with-deps` runs apt-get to pull Chromium's required system libs.
RUN pip install --no-cache-dir playwright==1.40.0 \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY bot.py .

CMD ["python", "-u", "bot.py"]
