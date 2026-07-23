FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py challenge_store.py ./
COPY fonts ./fonts
RUN mkdir -p assets/emoji_pins

EXPOSE 8080

CMD ["python", "bot.py"]
