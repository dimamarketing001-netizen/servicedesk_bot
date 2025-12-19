import asyncio
import logging
from typing import Callable, Dict, Any, Awaitable

# --- ИЗМЕНЕНИЕ ЗДЕСЬ: Добавляем Router в импорт ---
from aiogram import Bot, Dispatcher, F, BaseMiddleware, Router
# --------------------------------------------------
from aiogram.types import Message, TelegramObject
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# --- ВАШИ ДАННЫЕ (из .env) ---
BOT_TOKEN='8229314742:AAHM35Yx6_t8C6qfIvALcckdO9hFqQOKpBw'
MANAGER_CHAT_ID = -1003261949871
DB_URL='mysql+aiomysql://dmitriy:yF9mO3rL7f@localhost:3306/servicedesk_bot?charset=utf8mb4'
# -----------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_pool: async_sessionmaker):
        super().__init__()
        self.session_pool = session_pool

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with self.session_pool() as session:
            data["session"] = session
            return await handler(event, data)


# === ЛОГИКА С РОУТЕРОМ ===
# Мы больше не вешаем хендлер на dp, а используем роутер, как в вашем проекте
manager_router = Router()

@manager_router.message(F.chat.id == MANAGER_CHAT_ID)
async def simple_manager_handler(message: Message, session: AsyncSession):
    log.info("--- DEBUG BOT WITH ROUTER HANDLER TRIGGERED ---")
    await message.reply("DEBUG BOT: Я вижу сообщение через РОУТЕР!")

# Создаем диспетчер
dp = Dispatcher()

# Регистрируем роутер
dp.include_router(manager_router)
# ========================


async def main():
    # Инициализация БД
    engine = create_async_engine(DB_URL)
    session_pool = async_sessionmaker(engine, expire_on_commit=False)

    bot = Bot(token=BOT_TOKEN)
    
    # Регистрация Middleware
    dp.update.middleware(DbSessionMiddleware(session_pool=session_pool))

    log.info("Starting debug bot with ROUTER...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Debug bot stopped.")