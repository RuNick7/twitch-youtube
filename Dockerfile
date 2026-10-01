FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Только для тестов: ffprobe проверяет ролики, собранные из фрагментов VOD.
# Рабочему образу ffmpeg не нужен: видео идёт с Twitch на YouTube как есть
FROM base AS test
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

FROM base
# yt-dlp обновляется при каждом старте: Twitch периодически ломает его
CMD ["sh", "-c", "pip install -q --no-cache-dir -U --pre 'yt-dlp[default]' || echo 'yt-dlp не обновился, работаю с текущей версией'; exec python -m app"]
