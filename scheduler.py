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

async def send_sla_alerts(bot: Bot, dialog, text: str, escalation_chat_id: int):
    """Отправляет уведомление и в топик, и в канал эскалации."""
    # 1. В топик менеджера
    try:
        await bot.send_message(
            chat_id=dialog.manager_chat_id,
            message_thread_id=dialog.manager_topic_id,
            text=text,
            parse_mode="HTML"
        )
    except Exception as e:
        log.error(f"SLA Topic Alert Error: {e}")

    # 2. В канал эскалации (руководству)
    try:
        # Добавим ссылку на топик для руководства
        chat_id_clean = str(dialog.manager_chat_id).replace("-100", "")
        link = f"\n\n🔗 <a href='https://t.me/c/{chat_id_clean}/{dialog.manager_topic_id}'>Перейти к диалогу</a>"
        await bot.send_message(
            chat_id=escalation_chat_id,
            text=text + link,
            parse_mode="HTML"
        )
    except Exception as e:
        log.error(f"SLA Escalation Group Alert Error: {e}")

async def check_sla_job(session_pool: async_sessionmaker, bot: Bot, settings):
    async with session_pool() as session:
        now = datetime.now()
        dialogs = await db_commands.get_all_overdue_dialogs(session)

        for dialog in dialogs:
            # Считаем, сколько клиент ждет (в минутах)
            wait_time = (now - dialog.unanswered_since).total_seconds() / 60

            if dialog.manager:
                name = dialog.manager.full_name or "Без имени"
                # Если в базе всё еще вопросики, а юзернейма нет, выведем хотя бы ID
                if "?" in name and not dialog.manager.username:
                    manager_info = f"Менеджер ID:{dialog.manager.id}"
                elif dialog.manager.username:
                    manager_info = f"@{dialog.manager.username}"
                else:
                    manager_info = name
            else:
                manager_info = "Не назначен"
            
            # --- СЦЕНАРИЙ 1: ПЕРВОЕ НАРУШЕНИЕ (5 мин по умолчанию) ---
            if wait_time >= settings.sla_timeout_minutes and not dialog.sla_alert_sent:
                alert_text = (  
                    f"⏰ <b>SLA WARNING</b>\n"
                    f"Диалог: #{dialog.id}\n"
                    f"Менеджер: {manager_info}\n"
                    f"⚠️ Ожидание: <b>{int(wait_time)} мин.</b>"
                )
                await send_sla_alerts(bot, dialog, alert_text, settings.escalation_channel_id)
                
                # Обновляем статус в БД
                dialog.sla_alert_sent = True
                dialog.sla_last_alert_at = now
                
                await db_commands.log_sla_violation(
                    session, dialog.id, dialog.manager_id, 'initial', int(wait_time)
                )

            # --- СЦЕНАРИЙ 2: ПОВТОРНОЕ НАРУШЕНИЕ (Через 3 мин после первого и далее каждую минуту) ---
            elif dialog.sla_alert_sent:
                # Сколько прошло с момента последнего уведомления
                time_since_last_alert = (now - dialog.sla_last_alert_at).total_seconds() / 60
                
                # Если прошло более 3-х минут с ПЕРВОГО аларма, начинаем долбить каждую минуту
                if wait_time >= (settings.sla_timeout_minutes + 3) and time_since_last_alert >= 1:
                    alert_text = (
                        f"🚨 <b>SLA ESCALATION (Критическое)</b>\n"
                        f"Диалог: #{dialog.id}\n"
                        f"Менеджер игнорирует ответ!\n"
                        f"🔥 Суммарное ожидание: <b>{int(wait_time)} мин.</b>"
                    )
                    await send_sla_alerts(bot, dialog, alert_text, settings.escalation_channel_id)
                    
                    dialog.sla_last_alert_at = now # Обновляем время, чтобы сработать через минуту
                    
                    await db_commands.log_sla_violation(
                        session, dialog.id, dialog.manager_id, 'repeated', int(wait_time)
                    )

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