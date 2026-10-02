FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.txt .
# Сегменты VOD идут на YouTube как есть, а ffmpeg (из imageio-ffmpeg, ~80 МБ)
# нужен только для вертикальных Shorts из клипов
RUN pip install --no-cache-dir -r requirements.txt

# Только для тестов: ffprobe проверяет загруженные ролики. Код приложения тест
# подключает томом, поэтому этот слой не пересобирается при каждой правке
FROM base AS test
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

FROM base
COPY app ./app
# yt-dlp обновляется при каждом старте: Twitch периодически ломает его
CMD ["sh", "-c", "pip install -q --no-cache-dir -U --pre 'yt-dlp[default]' || echo 'yt-dlp не обновился, работаю с текущей версией'; exec python -m app"]
