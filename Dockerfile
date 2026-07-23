# Multi-stage: build Discord Activity (Vite) + run Discord bot + serve Activity/API.

FROM node:20-bookworm-slim AS activity-build
WORKDIR /src
COPY activity/client/package.json activity/client/package-lock.json ./
RUN npm ci
COPY activity/client/ ./
# Baked into the JS bundle at build time (also set as Render env / Docker build-arg).
ARG VITE_DISCORD_CLIENT_ID
ENV VITE_DISCORD_CLIENT_ID=$VITE_DISCORD_CLIENT_ID
RUN if [ -z "$VITE_DISCORD_CLIENT_ID" ]; then echo "WARN: VITE_DISCORD_CLIENT_ID empty at build"; fi
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    ACTIVITY_STATIC_DIR=/app/activity_dist

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cache-bust when Python sources change (Render sometimes reuses stale layers).
ARG SOURCE_DATE=2026-07-23-html-controls
RUN echo "build $SOURCE_DATE"
COPY bot.py challenge_store.py activity_http.py ./
COPY fonts ./fonts
COPY --from=activity-build /src/dist ./activity_dist
RUN mkdir -p assets/emoji_pins

EXPOSE 8080
CMD ["python", "-u", "bot.py"]
