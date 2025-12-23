import asyncio
import sys
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import delete
from config import settings
from db.models import City

async def seed_cities():
    # Создаем движок
    engine = create_async_engine(settings.db_url)
    session_pool = async_sessionmaker(engine, expire_on_commit=False)

    cities_data = [
        ('г. Екатеринбург', -4994118335), ('г. Иваново', -4884992909),
        ('г. Казань', -5025318327), ('г. Кострома', -4998251659),
        ('г. Москва', -5082346600), ('г. Нижний Новгород', -5085602853),
        ('г. Нижний Тагил', -5071982445), ('г. Новосибирск', -5060371022),
        ('г. Омск', -5038368702), ('г. Пермь', -4832743871),
        ('г. Самара', -5018177957), ('г. Санкт-Петербург', -5051094242),
        ('г. Тверь', -5097117317), ('г. Тольятти', -5076445428),
        ('г. Тула', -5084869531), ('г. Тюмень', -5035132557),
        ('г. Челябинск', -5071640706), ('г. Ярославль', -5002072096),
        ('г. Сургут', -4998502918), ('г. Уфа', -5058933665),
        ('г. Сочи', -5060292520)
    ]

    try:
        async with session_pool() as session:
            # Очищаем таблицу
            await session.execute(delete(City))
            
            # Добавляем города
            for name, chat_id in cities_data:
                city = City(name=name, telegram_chat_id=chat_id)
                session.add(city)
            
            await session.commit()
            print("DONE: Data saved to database successfully.") # Только английский для терминала
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        await engine.dispose() # Закрываем соединение правильно

if __name__ == "__main__":
    asyncio.run(seed_cities())