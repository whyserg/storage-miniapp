# pip install "aiogram>=3.7,<4"

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

# ====================== НАСТРОЙКИ — ЗАМЕНИ 2 СТРОКИ ======================
BOT_TOKEN  = "8991784741:AAHZVNcPbi-ziIn_BTmGZ81KHzg3E_5DCb8"   # ← токен от @BotFather
WEBAPP_URL = "https://whyserg.github.io/storage-miniapp/"         # ← со слэшем на конце
# ========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть хранилище", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await message.answer(
        "Хранилище — личное облако в Telegram.\n\n"
        "Нажми кнопку ниже, чтобы открыть Mini App. "
        "Файлы там загружаются прямо в GitHub.",
        reply_markup=kb,
    )


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands([BotCommand(command="start", description="Открыть хранилище")])

    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())