"""Единый конфиг для Telegram-бота и Mini App.

Все настройки в одном месте. Значения по умолчанию можно
переопределить переменными окружения:
  STORAGE_BOT_TOKEN
  STORAGE_WEBAPP_URL
"""
import os

BOT_TOKEN = os.getenv("STORAGE_BOT_TOKEN", "8991784741:AAHZVNcPbi-ziIn_BTmGZ81KHzg3E_5DCb8")
WEBAPP_URL = os.getenv("STORAGE_WEBAPP_URL", "https://whyserg.github.io/storage-miniapp/")

# Лимит размера файла (байт) — GitHub API принимает файлы до 100 МБ,
# но для стабильности ставим 50 МБ.
MAX_FILE_SIZE = 50 * 1024 * 1024

ALLOWED_EXTENSIONS_TXT = None  # None = любые файлы разрешены