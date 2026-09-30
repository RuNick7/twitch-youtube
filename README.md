# Twitch → YouTube

Бот в Telegram нарезает VOD стримера с Twitch по сменам категории, присылает сегменты на проверку и загружает одобренные на YouTube. План проекта — в [PLAN.md](PLAN.md).

Сейчас готов этап 1:

- команда `/process` для ручной обработки VOD;
- проверка кнопками в Telegram;
- скачивание только одобренных кусков с проверкой целостности;
- загрузка на YouTube с докачкой после сбоев.

Автоматический запуск после окончания стрима появится на этапе 2.

## 1. Подготовка

### Telegram

Напишите [@BotFather](https://t.me/BotFather) команду `/newbot`, придумайте имя и сохраните токен. Свой Telegram ID искать не нужно: после первого запуска бот сам пришлёт его в ответ на `/start`.

### Google Cloud

1. Откройте [console.cloud.google.com](https://console.cloud.google.com) и создайте проект.
2. «APIs & Services» → «Library» → **YouTube Data API v3** → «Enable».
3. «Google Auth Platform» (раньше называлось «OAuth consent screen») → «Get started»: название приложения, почта, тип аудитории **External**.
4. «Data Access» → «Add or remove scopes» → добавьте `https://www.googleapis.com/auth/youtube`.
5. «Audience» → **Publish app**, чтобы статус стал «In production». В режиме «Testing» доступ к YouTube слетает через 7 дней.
6. «Clients» → «Create client» → тип **TVs and Limited Input devices**. Client ID и Client secret понадобятся для `.env`.
7. Подайте заявку на аудит: [форма YouTube API](https://support.google.com/youtube/contact/yt_api_form). Пока аудит не пройден, YouTube блокирует загруженные через API ролики как приватные — для проверки работы это не мешает.

### YouTube

Подтвердите канал по телефону на [youtube.com/verify](https://www.youtube.com/verify), иначе ролики длиннее 15 минут не загрузятся.

### Стример

- Письменное разрешение на нарезки.
- Архив трансляций включён и доступен всем, а не только подписчикам.
- Если у стримера свой YouTube с Content ID — ваш канал в его списке исключений.

## 2. Установка на сервер

Docker, если ещё не установлен:

```bash
curl -fsSL https://get.docker.com | sh
```

Swap на 2 ГБ, если `swapon --show` ничего не выводит:

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

Скопируйте проект на сервер. Команда запускается на вашем компьютере из папки проекта:

```bash
rsync -av --exclude data --exclude .env --exclude __pycache__ ./ root@IP_СЕРВЕРА:/opt/twitch-youtube/
```

На сервере создайте `.env` из шаблона и заполните его:

```bash
cd /opt/twitch-youtube && cp .env.example .env && nano .env
```

Ключ для `SECRET_KEY`:

```bash
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

Запуск:

```bash
docker compose up -d --build
```

Дальше:

1. Напишите боту `/start`. Он пришлёт ваш ID. Впишите его в `TELEGRAM_OWNER_ID` и перезапустите контейнер: `docker compose up -d`.
2. Отправьте `/youtube`, откройте ссылку, введите код и выберите канал.
3. Отправьте `/process https://www.twitch.tv/videos/…` с любым VOD стримера.

## 3. Команды бота

| Команда | Что делает |
|---|---|
| `/process <ссылка на VOD>` | нарезать VOD и прислать сегменты на проверку |
| `/youtube` | подключить или переподключить YouTube-канал |
| `/status` | подключения, очередь, свободное место, версия yt-dlp |
| `/pause`, `/resume` | остановить и продолжить обработку |

Под каждым сегментом есть кнопки «Одобрить», «Отклонить» и «Смотреть на Twitch». Одобренные сегменты скачиваются и загружаются по одному. Если что-то пошло не так, бот пришлёт 🔴 и кнопку «Повторить». Если проблема не в самом сегменте (слетел доступ к YouTube, кончилось место, дневной лимит), обработка встаёт на паузу до `/resume`.

## Обслуживание

- Логи: `docker compose logs -f app`
- Обновление кода: повторите `rsync` и выполните `docker compose up -d --build`.
- yt-dlp обновляется сам при каждом старте контейнера и раз в сутки.
- Данные лежат в `./data`: база `app.db` и временные файлы `work/`. Файл сегмента удаляется сразу после загрузки.

## Тесты

```bash
python3 -m unittest discover -s tests -t .
```
