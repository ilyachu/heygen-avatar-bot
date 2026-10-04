FROM python:3.11-slim

# Install system dependencies (ffmpeg is essential for Telegram round video notes)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY bot/ /app/bot/
COPY domain/ /app/domain/
COPY workers/ /app/workers/
COPY migrations/ /app/migrations/
COPY web/ /app/web/
COPY templates/ /app/templates/
COPY static/ /app/static/
COPY fixtures/ /app/fixtures/
COPY scripts/ /app/scripts/

# Prepare persistent data and temp folders
RUN mkdir -p /app/data /app/temp

# Run bot
CMD ["python", "-m", "bot.main"]
