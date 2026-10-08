# pip install "aiogram>=3.7,<4"

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    CallbackQuery,
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
    format="%(asctime)s %(levelname)s [%(name)s]: %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()

START_TEXT = (
    "✨ <b>Добро пожаловать в твоё личное облако!</b>\n\n"
    "Этот бот даёт тебе собственное хранилище файлов прямо в Telegram — "
    "с удобным интерфейсом, который открывается по кнопке ниже.\n\n"
    "📦 <b>Что умеет хранилище:</b>\n"
    "• Загружать любые файлы — фото, видео, документы, архивы\n"
    "• Раскладывать их по папкам и перемещать между ними\n"
    "• Искать файлы по названию и сортировать по дате, размеру и имени\n"
    "• Скачивать всё одним ZIP-архивом\n"
    "• Работать в тёмной и светлой теме — как удобнее\n\n"
    "🚀 <b>Как начать:</b>\n"
    "Просто нажми кнопку <b>«Перейти в хранилище»</b> ниже — "
    "и всё готово. Файлы сохраняются в твой GitHub-репозиторий, "
    "так что они не потеряются даже после перезапуска бота.\n\n"
    "💡 <i>Подсказка: файлы можно загружать не только по кнопке — "
    "просто перетащи их в окно хранилища.</i>"
)

HELP_TEXT = (
    "🆘 <b>Помощь</b>\n\n"
    "📁 <b>/start</b> — открыть хранилище и начать работу\n"
    "👤 <b>/me</b> — узнать информацию о своём профиле\n\n"
    "Если что-то не работает, проверь:\n"
    "• что ссылка на хранилище (WEBAPP_URL) доступна\n"
    "• что токен GitHub и репозиторий в файле <code>storage-miniapp.html</code> настроены верно"
)

ME_TEXT = (
    "👤 <b>Твой профиль</b>\n\n"
    "Имя: {name}\n"
    "Никнейм: @{username}\n"
    "ID: <code>{user_id}</code>\n"
    "Язык: {lang}\n"
    "Тема: {theme}"
)


def webapp_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📁 Перейти в хранилище",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    ]])


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(START_TEXT, reply_markup=webapp_kb())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, reply_markup=webapp_kb())


@router.message(Command("me"))
async def cmd_me(message: Message):
    user = message.from_user
    await message.answer(
        ME_TEXT.format(
            name=user.full_name or "—",
            username=user.username or "—",
            user_id=user.id,
            lang=user.language_code or "—",
            theme=message.from_user.language_code or "—",
        ),
        reply_markup=webapp_kb(),
    )


@router.message(F.text)
async def fallback_text(message: Message):
    """Любое другое сообщение — мило отвечаем и ведём к хранилищу."""
    await message.answer(
        "🤔 Не понял команду. Воспользуйся кнопками ниже или напиши /help.",
        reply_markup=webapp_kb(),
    )


@router.errors()
async def errors_handler(event) -> bool:
    logger.exception("Unhandled error", exc_info=event.exception)
    return True


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.errors.register(errors_handler)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть хранилище"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="me", description="Мой профиль"),
        ],
        scope=BotCommandScopeDefault(),
    )

    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Бот запущен. Ожидаю команды...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())