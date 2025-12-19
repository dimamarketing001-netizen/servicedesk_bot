import os
from pydantic_settings import BaseSettings, SettingsConfigDict

# 1. Получаем абсолютный путь к папке, где лежит этот файл (config.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 2. Собираем полный путь к файлу .env
ENV_FILE_PATH = os.path.join(BASE_DIR, ".env")

class Settings(BaseSettings):
    bot_token: str
    db_url: str
    message_checker_chat_id: int
    applications_channel_id: int
    escalation_channel_id: int
    knowledge_base_channel_id: int
    technical_chat_id: int
    sla_timeout_minutes: int = 5

    redis_host: str = '192.168.210.150'
    redis_port: int = 6379
    redis_db: int = 1
    redis_queue_name: str = 'main_task_queue'

    # 3. Указываем конкретный путь к файлу
    model_config = SettingsConfigDict(
        env_file=ENV_FILE_PATH, 
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()