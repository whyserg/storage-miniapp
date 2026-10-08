# 📁 Storage — твоё личное облако в Telegram

Мини-приложение + Telegram-бот для хранения файлов.
Файлы загружаются в GitHub-репозиторий (через GitHub Pages как CDN),
а сам интерфейс открывается прямо в Telegram как Web App.

## Структура

```
.
├── storage-miniapp.html   # само Mini App (весь интерфейс, HTML+CSS+JS)
├── telegram-storage.py    # Telegram-бот (aiogram 3.x)
├── config.py              # общие настройки
└── requirements.txt       # зависимости Python
```

## Как настроить

### 1. GitHub

1. Создай репозиторий (например `storage-miniapp`).
2. Создай GitHub token с правами `repo` (Settings → Developer settings → Personal access tokens).
3. Включи GitHub Pages (Settings → Pages → branch `main`, папка `/root`).
4. Скачай `storage-miniapp.html` и загрузи его в корень репозитория.

### 2. HTML (storage-miniapp.html)

Открой файл и замени 4 строки в начале скрипта:

```js
var GITHUB_TOKEN  = 'ghp_ВАШ_ТОКЕН';            // токен с scope: repo
var GITHUB_REPO   = 'ваш_логин/storage-miniapp'; // репозиторий
var GITHUB_BRANCH = 'main';                      // ветка
var REPO_URL      = 'https://ваш_логин.github.io/storage-miniapp'; // Pages URL без слэша
```

### 3. Telegram

1. Создай бота у [@BotFather](https://t.me/BotFather), получи токен.
2. Пропиши его в `telegram-storage.py` (строка `BOT_TOKEN`) или в переменную окружения `STORAGE_BOT_TOKEN`.
3. Пропиши URL страницы Mini App в `WEBAPP_URL` (со слэшем на конце).
4. Установи зависимости: `pip install -r requirements.txt`
5. Запусти: `python telegram-storage.py`

## Команды бота

- `/start` — приветствие + кнопка «Перейти в хранилище»
- `/help` — справка
- `/me` — информация о профиле

## Фичи Mini App

- 📂 Папки и перемещение файлов
- 🔍 Поиск по всем файлам
- ↕️ Сортировка (имя / размер / дата)
- 🎨 Три темы: системная / светлая / тёмная
- 📥 Drag & drop загрузка
- 📦 Скачивание всех файлов ZIP-архивом
- ✅ Мультивыделение (долгое нажатие)
- ♻️ Прогресс загрузки с процентами

> ⚠️ **Важно:** токен GitHub лежит в клиентском коде — это нормально для
> личного хранилища, но не публикуй токены в публичные репозитории.