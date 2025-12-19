"""
Настройка и запуск фоновых задач с помощью APScheduler.
ВЕРСИЯ: Только синхронизация удаления Менеджера (Client -> Manager невозможно).
"""
import logging
import asyncio
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import joinedload 

from config import Settings
from db.models import Dialog
from db import commands as db_commands

log = logging.getLogger(__name__)

async def check_manager_message_exists(
    bot: Bot,
    check_chat_id: int, # Технический чат
    original_chat_id: int, # Чат (топик) менеджера
    original_message_id: int
) -> bool:
    """
    Проверяет существование сообщения в группе менеджеров.
    """
    if not check_chat_id:
        return True 

    try:
        # Пытаемся переслать сообщение из топика в тех. чат
        test_msg = await bot.forward_message(
            chat_id=check_chat_id,
            from_chat_id=original_chat_id,
            message_id=original_message_id,
            disable_notification=True
        )
        
        # Если успешно - удаляем копию и возвращаем True
        try:
            await bot.delete_message(chat_id=check_chat_id, message_id=test_msg.message_id)
        except:
            pass
        return True 

    except TelegramBadRequest as e:
        err_msg = e.message.lower()
        # Если сообщение не найдено - значит менеджер его удалил
        if (
            "message to forward not found" in err_msg
            or "message not found" in err_msg 
            or "message_id_invalid" in err_msg
        ):
            return False 
        return True

    except Exception:
        return True


async def sync_dialogs_job(session_pool: async_sessionmaker, bot: Bot, settings: Settings):
    # log.info("Running sync_dialogs_job...")
    technical_chat_id = settings.technical_chat_id 
    
    async with session_pool() as session:
        # Получаем сообщения за последние 24 часа
        messages_to_check = await db_commands.get_live_messages_for_sync(session)
        
        if not messages_to_check:
            return

        for log_entry in messages_to_check:
            # Нас интересует ТОЛЬКО если сообщение удалил Менеджер.
            # (Проверка клиента бессмысленна из-за ограничений API)
            
            if not log_entry.manager_telegram_message_id:
                continue

            # Небольшая пауза
            await asyncio.sleep(0.05)

            try:
                dialog = await db_commands.get_dialog_by_id(session, log_entry.dialog_id)
                if not dialog or not dialog.client:
                    continue

                # Проверяем, существует ли сообщение в чате менеджера
                exists = await check_manager_message_exists(
                    bot=bot,
                    check_chat_id=technical_chat_id,
                    original_chat_id=dialog.manager_chat_id,
                    original_message_id=log_entry.manager_telegram_message_id
                )
                
                if not exists:
                    # МЕНЕДЖЕР УДАЛИЛ СООБЩЕНИЕ
                    log.info(f"[Sync] Manager deleted msg {log_entry.manager_telegram_message_id}. Deleting from client...")
                    
                    log_entry.is_deleted = True
                    await session.flush()
                    
                    # Удаляем зеркальное сообщение у клиента
                    if log_entry.client_telegram_message_id:
                        try:
                            await bot.delete_message(
                                chat_id=dialog.client.telegram_id, 
                                message_id=log_entry.client_telegram_message_id
                            )
                        except Exception as e: 
                            # Часто бывает "Message to delete not found", если клиент уже сам удалил
                            pass
                            
            except Exception as e:
                log.error(f"Error in sync loop: {e}")
                continue

        await session.commit()

async def check_sla_job(session_pool: async_sessionmaker, bot: Bot, settings: Settings):
    async with session_pool() as session:
        # Получаем диалоги, просроченные на 5 минут (из конфига)
        overdue_dialogs = await db_commands.get_overdue_dialogs(session, settings.sla_timeout_minutes)
        
        for dialog in overdue_dialogs:
            try:
                # Текст уведомления
                alert_text = (
                    f"⚠️ <b>SLA WARNING</b>\n"
                    f"Клиент ждет ответа уже более {settings.sla_timeout_minutes} минут!\n"
                    f"Первое сообщение было в: {dialog.unanswered_since.strftime('%H:%M:%S')}"
                )
                
                # Отправляем в топик менеджеру
                await bot.send_message(
                    chat_id=dialog.manager_chat_id,
                    message_thread_id=dialog.manager_topic_id,
                    text=alert_text,
                    parse_mode="HTML"
                )
                
                # Помечаем в базе, что мы уже уведомили об этой задержке
                dialog.sla_alert_sent = True
                
            except Exception as e:
                log.error(f"Failed to send SLA alert for dialog {dialog.id}: {e}")
        
        await session.commit()

def setup_scheduler(session_pool: async_sessionmaker, bot: Bot, settings: Settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        check_sla_job, 
        trigger='interval', 
        minutes=1, 
        kwargs={'session_pool': session_pool, 'bot': bot, 'settings': settings}
    )
    scheduler.add_job(
        sync_dialogs_job, 
        trigger='interval', 
        seconds=15, 
        max_instances=1, 
        kwargs={'session_pool': session_pool, 'bot': bot, 'settings': settings}
    )
    return scheduler