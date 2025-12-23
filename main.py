import asyncio
import logging
from datetime import datetime, date, timedelta
from typing import Callable, Dict, Any, Awaitable, Generator
import uuid
import redis.asyncio as redis
import json
from telegram import InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest 
from aiogram import types, Bot, Dispatcher, F, BaseMiddleware
from aiogram.fsm.context import FSMContext  
from aiogram.fsm.storage.memory import MemoryStorage 
from aiogram.types import Message, CallbackQuery, TelegramObject, User as AiogramUser, InlineQueryResultArticle, InputTextMessageContent, SwitchInlineQueryChosenChat
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from config import settings
from db import commands as db_commands
from db.models import User, Dialog, Base
from keyboards.inline import ManagerCallback, get_manager_control_panel, get_app_step_keyboard
from scheduler import setup_scheduler
from states.manager_states import ManagerFSM 

from aiogram.enums import ContentType
from aiogram.types import Update

class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_pool: async_sessionmaker):
        super().__init__()
        self.session_pool = session_pool
    async def __call__(self, handler, event, data):
        async with self.session_pool() as session:
            data["session"] = session
            return await handler(event, data)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

dp = Dispatcher()

redis_client = redis.Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    db=settings.redis_db,
    decode_responses=True
)

BRANDS = ["KeineExchange", "ftCash", "BitRocket", "AvanChange", "CoinsBlack", "DocrtorBit", "FOEX", "DIMMAR", "SberBit", "ArkedUSDT", "MULTIKASSA", "Fox", "ZombieCash", "AWX"]
CURRENCIES = ["Tether (TRC-20)", "Tether (ERC-20)", "Tether (BEP20)", "Bitcoin", "Litecoin", "Ethereum (ERC-20)", "Tron (TRX)", "USD Coin (ERC-20)", "USD Coin (TRC-20)", "Рубль (RUB)"]

async def ask_for_datetime(message: Message, state: FSMContext, error: bool = False):
    prompt = "Введите дату и время встречи (пример: `19.09.2025 15:00`) или выберите быстрый вариант:"
    if error: prompt = "❌ Неверный формат.\n" + prompt
    
    await state.update_data(last_prompt=prompt)
    
    date_btns = {"Сегодня": "set_date_today", "Завтра": "set_date_tomorrow", "Послезавтра": "set_date_day_after"}
    kb = get_app_step_keyboard(date_btns)
    
    await edit_or_send_message(message, state, text=prompt, reply_markup=kb, parse_mode="Markdown")

async def edit_or_send_message(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup=None,
    is_callback: bool = False,
    parse_mode: str = "HTML"  # <--- ДОБАВЛЕНО ЭТО
):
    """
    Управляет сообщениями бота в FSM: редактирует или отправляет новые,
    очищая предыдущие сообщения. Поддерживает форматирование.
    """
    data = await state.get_data()
    last_bot_message_id = data.get('last_bot_message_id')
    chat_id = message.chat.id

    # Удаляем предыдущее сообщение бота, если оно было
    if last_bot_message_id:
        try:
            await message.bot.delete_message(chat_id, last_bot_message_id)
        except Exception:
            pass 

    # Если это не колбэк (т.е. текст от юзера), удаляем его сообщение
    if not is_callback:
        try:
            await message.delete()
        except Exception:
            pass

    # Отправляем новое сообщение с переданным parse_mode
    new_msg = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    
    await state.update_data(last_bot_message_id=new_msg.message_id)

def build_keyboard_for_app(items: list, items_per_row: int = 2) -> InlineKeyboardMarkup:
    """Строит инлайн-клавиатуру для шагов создания заявки."""
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.button(text=item, callback_data=item)
    builder.adjust(items_per_row)
    return builder.as_markup()

def format_application_summary(data: dict) -> str:
    """Форматирует собранные данные по заявке в итоговое сообщение."""
    summary = "<b>✅ Новая заявка</b>\n\n"
    summary += f"<b>Город:</b> {data.get('city_name', 'Не указан')}\n"
    summary += f"<b>Тип:</b> {data.get('type', 'Не указан')}\n"
    summary += f"<b>Направление:</b> {data.get('direction', 'Не указано')}\n"
    summary += f"<b>Бренд:</b> {data.get('brand', 'Не указан')}\n"
    summary += f"<b>ФИО клиента:</b> {data.get('last_name', '')} {data.get('first_name', '')} {data.get('patronymic', '')}\n"
    summary += f"<b>Время встречи:</b> {data.get('datetime', 'Не указано')}\n\n"
    
    if data.get('action') == 'Принять':
        summary += f"<b>Принять:</b> {data.get('amount_to_get', '')} {data.get('currency_to_get', '')}\n"
    elif data.get('action') == 'Выдать':
        summary += f"<b>Выдать:</b> {data.get('amount_to_give', '')} {data.get('currency_to_give', '')}\n"

    if data.get('type') == 'Партнерская':
        summary += f"<b>Процент партнера:</b> {data.get('partner_percent', '')}%\n"
        summary += f"<b>Наш процент:</b> {data.get('our_percent', '')}%\n"
        summary += f"<b>Общий процент: {data.get('total_percent', '')}%</b>\n"
    else: # Частная
        summary += f"<b>Наш процент:</b> {data.get('our_percent', '')}%\n"

    if data.get('client_id'):
        summary += f"<b>ID клиента:</b> {data.get('client_id')}"
        
    return summary

def format_summary_for_client(data: dict) -> str:
    """
    Форматирует данные по заявке в краткое сообщение для клиента.
    """
    summary = "<b>Детали вашей встречи:</b>\n\n"
    summary += f"<b>Город:</b> {data.get('city_name', 'Не указан')}\n"
    
    # Собираем ФИО
    full_name = f"{data.get('last_name', '')} {data.get('first_name', '')} {data.get('patronymic', '')}".strip()
    if full_name:
        summary += f"<b>ФИО:</b> {full_name}\n"
    
    # Добавляем информацию о действии (Принять/Выдать)
    if data.get('action') == 'Принять':
        summary += f"<b>Примем:</b> {data.get('amount_to_get', '')} {data.get('currency_to_get', '')}\n"
    elif data.get('action') == 'Выдать':
        summary += f"<b>Выдадим:</b> {data.get('amount_to_give', '')} {data.get('currency_to_give', '')}\n"

    # Добавляем время встречи
    if data.get('datetime'):
        summary += f"<b>Время встречи:</b> {data.get('datetime')}"
        
    return summary

def split_text(text: str, chunk_size: int = 4000) -> Generator[str, None, None]:
    """
    Простая функция-генератор для разбиения длинного текста на части
    заданного размера, не разрывая слова.
    """
    if len(text) <= chunk_size:
        yield text
        return
    
    last_space = text.rfind(' ', 0, chunk_size)
    if last_space == -1: # Если нет пробелов (одно длинное слово)
        last_space = chunk_size
        
    yield text[:last_space]
    
async def forward_message_to_client(bot: Bot, client_tg_id: int, message: Message) -> Message | None:
    """
    Универсальная функция для отправки любого контента клиенту.
    Возвращает объект отправленного сообщения или None в случае ошибки.
    """
    try:
        # Копируем сообщение как есть и возвращаем его
        sent_message = await bot.copy_message(
            chat_id=client_tg_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
        return sent_message
    except Exception as e:
        error_text = f"❌ **Ошибка доставки!**\nКлиент мог заблокировать бота.\nПричина: `{type(e).__name__}: {e}`"
        # Отправляем ответ в чат менеджеру, чтобы он был в курсе проблемы
        await message.reply(error_text, parse_mode="Markdown")
        # Возвращаем None, чтобы вызывающая функция знала об ошибке
        return None

  
# === ЛОГИКА ДЛЯ КЛИЕНТОВ (без изменений) ===
@dp.message(F.chat.type == "private")
async def handle_client_message(message: Message, session: AsyncSession, bot: Bot):
    log.info(f"[CLIENT HANDLER] Received message from user {message.from_user.id}")

    user = await db_commands.get_or_create_user(session, message.from_user)
    if not user:
        await message.answer("Произошла ошибка при регистрации.")
        return

    dialog = await db_commands.find_last_dialog_for_client(session, user.id)
    dialog_id_to_update = None

    # --- СЦЕНАРИЙ 1: ДИАЛОГ УЖЕ ЕСТЬ ---
    if dialog:
        dialog_id_to_update = dialog.id
        
        # 1. Попытка восстановить/переоткрыть топик
        if dialog.status in ('resolved', 'transferred'):
            try:
                await bot.reopen_forum_topic(chat_id=dialog.manager_chat_id, message_thread_id=dialog.manager_topic_id)
                
                # Отправляем новый пульт
                manager_info = f"@{dialog.manager.username}" if dialog.manager and dialog.manager.username else "текущему менеджеру"
                reopen_text = (
                    f"🔄 <b>Диалог возобновлен клиентом!</b>\n"
                    f"Клиент: {user.full_name}\n"
                    f"Ответственный: {manager_info}\n"
                    f"👇 <i>Используйте меню ниже для управления</i>"
                )
                control_panel_msg = await bot.send_message(
                    chat_id=dialog.manager_chat_id, 
                    message_thread_id=dialog.manager_topic_id, 
                    text=reopen_text,
                    reply_markup=get_manager_control_panel(dialog.id),
                    parse_mode="HTML"
                )
                await bot.pin_chat_message(
                    chat_id=dialog.manager_chat_id,
                    message_id=control_panel_msg.message_id,
                    disable_notification=True
                )
            except TelegramBadRequest as e:
                # Если ошибка не "thread not found", то просто логируем. 
                # Если "not found", она обработается ниже при отправке сообщения.
                if "message thread not found" not in e.message.lower():
                    log.warning(f"Не удалось переоткрыть топик: {e}")
            except Exception as e:
                log.error(f"Ошибка при возобновлении темы: {e}")

        # Обновляем статус
        await db_commands.update_dialog_status(session, dialog_id=dialog.id, new_status='active')

        # 2. Попытка отправить сообщение менеджеру
        # ЗДЕСЬ ДОБАВЛЕНА ЗАЩИТА ОТ УДАЛЕННОГО ТОПИКА
        try:
            manager_message = await send_message_to_manager(
                bot, 
                chat_id=dialog.manager_chat_id,
                topic_id=dialog.manager_topic_id, 
                from_user=message.from_user, 
                message=message
            )
        except TelegramBadRequest as e:
            # Если топик удален вручную ("message thread not found")
            if "message thread not found" in e.message.lower():
                log.warning(f"Топик {dialog.manager_topic_id} не найден. Создаю новый...")
                
                # Создаем новый топик
                user_display_name = user.full_name or f"User {user.telegram_id}"
                new_topic = await bot.create_forum_topic(
                    chat_id=dialog.manager_chat_id, 
                    name=f"🗣️ {user_display_name} (Restored)"
                )
                
                # Обновляем ID топика в БД
                dialog.manager_topic_id = new_topic.message_thread_id
                await session.flush()
                
                # Отправляем сообщение в НОВЫЙ топик
                manager_message = await send_message_to_manager(
                    bot, 
                    chat_id=dialog.manager_chat_id,
                    topic_id=new_topic.message_thread_id, 
                    from_user=message.from_user, 
                    message=message
                )
                
                # Сразу кидаем туда пульт управления
                manager_greeting = (f"⚠️ <b>Топик был восстановлен</b> (старый был удален)\n"
                                    f"Клиент: {user.full_name}")
                control_panel_message = await bot.send_message(
                    chat_id=dialog.manager_chat_id, 
                    message_thread_id=new_topic.message_thread_id, 
                    text=manager_greeting, 
                    reply_markup=get_manager_control_panel(dialog.id),
                    parse_mode="HTML"
                )
                await bot.pin_chat_message(
                    chat_id=dialog.manager_chat_id,
                    message_id=control_panel_message.message_id,
                    disable_notification=True
                )
            else:
                # Если ошибка другая - прокидываем её дальше
                raise e

        # Логируем
        log_text = ""
        if message.text:
            log_text = message.text
        elif message.caption:
            log_text = f"[{message.content_type}] {message.caption}"
        else:
            log_text = f"[{message.content_type}]"
            
        if log_text:
            await db_commands.add_message_to_log(
                session=session,
                dialog_id=dialog_id_to_update,
                sender_role='client',
                sender_name=message.from_user.full_name,
                text=log_text.strip(),
                client_telegram_message_id=message.message_id,           
                manager_telegram_message_id=manager_message.message_id   
            )

    # --- СЦЕНАРИЙ 2: НОВЫЙ ДИАЛОГ ---
    else:
        # (Код без изменений, как был раньше)
        free_employee = await db_commands.find_free_manager(session)
        
        if not free_employee or not free_employee.work_chat_id:
            log.warning("Не найдены свободные менеджеры с назначенным рабочим чатом.")
            await message.answer("К сожалению, сейчас все менеджеры заняты. Попробуйте позже.")
            return
            
        manager_work_chat_id = free_employee.work_chat_id
        
        full_name_parts = free_employee.full_name.split()
        first_name = full_name_parts[0] if full_name_parts else free_employee.full_name
        last_name = " ".join(full_name_parts[1:]) if len(full_name_parts) > 1 else None
        manager_aiogram_user = AiogramUser(id=free_employee.personal_telegram_id, is_bot=False, first_name=first_name, last_name=last_name, full_name=free_employee.full_name) 
        manager_user = await db_commands.get_or_create_user(session, manager_aiogram_user, role='manager')
        
        try:
            user_display_name = user.full_name or f"User {user.telegram_id}"
            
            topic = await bot.create_forum_topic(
                chat_id=manager_work_chat_id, 
                name=f"🗣️ {user_display_name}"
            )

            try:
                temp_msg = await bot.send_message(chat_id=manager_work_chat_id, message_thread_id=topic.message_thread_id, text=".")
                await bot.delete_message(chat_id=manager_work_chat_id, message_id=temp_msg.message_id)
            except:
                pass

        except Exception as e:
            log.error(f"Failed to create topic in chat {manager_work_chat_id}: {e}") 
            await message.answer("Произошла техническая ошибка. Пожалуйста, сообщите администратору.")
            return
        
        new_dialog = await db_commands.create_dialog(
            session, 
            client_id=user.id, 
            manager_id=manager_user.id, 
            manager_chat_id=manager_work_chat_id,
            topic_id=topic.message_thread_id
        )
        dialog_id_to_update = new_dialog.id
        
        manager_greeting = (f"❗️ Новое обращение от клиента: {user.full_name}\n👤 @{user.username if user.username else 'N/A'}\n✅ Назначен ответственный: {manager_user.full_name}")
        
        manager_message = await send_message_to_manager(
            bot, 
            chat_id=manager_work_chat_id, 
            topic_id=topic.message_thread_id, 
            from_user=message.from_user, 
            message=message
        )

        log_text = ""
        if message.text:
            log_text = message.text
        elif message.caption:
            log_text = f"[{message.content_type}] {message.caption}"
        else:
            log_text = f"[{message.content_type}]"
            
        if log_text:
            await db_commands.add_message_to_log(
                session=session,
                dialog_id=dialog_id_to_update,
                sender_role='client',
                sender_name=message.from_user.full_name,
                text=log_text.strip(),
                client_telegram_message_id=message.message_id,
                manager_telegram_message_id=manager_message.message_id
            )
        
        control_panel_message = await bot.send_message(
            chat_id=manager_work_chat_id, 
            message_thread_id=topic.message_thread_id, 
            text=manager_greeting, 
            reply_markup=get_manager_control_panel(new_dialog.id)
        )
        
        try:
            await bot.pin_chat_message(
                chat_id=manager_work_chat_id,
                message_id=control_panel_message.message_id,
                disable_notification=True
            )
        except Exception as e:
            log.error(f"Could not pin message in topic {topic.message_thread_id}: {e}")

    if dialog_id_to_update:
        await db_commands.update_dialog_last_client_message_time(session, dialog_id=dialog_id_to_update, timestamp=datetime.now())
    await session.commit()

async def send_message_to_manager(bot: Bot, chat_id: int, topic_id: int, from_user: AiogramUser, message: Message) -> Message:
    user_info = f"👤 <b>{from_user.full_name}</b> (@{from_user.username if from_user.username else 'N/A'}):\n\n"
    if message.text:
        return await bot.send_message(chat_id=chat_id, message_thread_id=topic_id, text=f"{user_info}{message.text}", parse_mode="HTML")
    else:
        return await bot.copy_message(chat_id=chat_id, message_thread_id=topic_id, from_chat_id=message.chat.id, message_id=message.message_id, caption=f"{user_info}{message.caption or ''}", parse_mode="HTML")

# ==========================================
# === ОБРАБОТКА РЕДАКТИРОВАНИЯ СООБЩЕНИЙ ===
# ==========================================

# 1. КЛИЕНТ изменил сообщение
@dp.edited_message(F.chat.type == "private")
async def handle_client_edited_message(message: Message, session: AsyncSession, bot: Bot):
    # Ищем запись в БД по ID сообщения клиента
    log_entry = await db_commands.get_log_entry_by_client_msg_id(session, message.message_id)
    if not log_entry:
        return

    # Получаем диалог, чтобы знать куда отправлять уведомление
    dialog = await db_commands.get_dialog_by_id(session, log_entry.dialog_id)
    if not dialog or not dialog.manager_chat_id:
        return

    # Определяем новый текст
    new_text = message.text or message.caption or "[Медиа]"

    # Обновляем БД
    await db_commands.update_log_text(session, log_entry, new_text)
    await session.commit()

    # ТЗ: "отправить новое измененое сообщение в ответ на старое с пометкой"
    notification_text = f"✏️ <b>Клиент изменил сообщение:</b>\n\n{new_text}"
    
    try:
        await bot.send_message(
            chat_id=dialog.manager_chat_id,
            message_thread_id=dialog.manager_topic_id,
            text=notification_text,
            reply_to_message_id=log_entry.manager_telegram_message_id, # Отвечаем на исходное
            parse_mode="HTML"
        )
    except Exception as e:
        log.error(f"Failed to notify manager about edit: {e}")


# 2. МЕНЕДЖЕР изменил сообщение
@dp.edited_message(
    F.chat.type.in_({"group", "supergroup"}),
    F.message_thread_id,
    F.from_user.is_bot == False
)
async def handle_manager_edited_message(message: Message, session: AsyncSession, bot: Bot):
    # Ищем запись в БД по ID сообщения менеджера
    log_entry = await db_commands.get_log_entry_by_manager_msg_id(session, message.message_id)
    if not log_entry:
        return

    dialog = await db_commands.get_dialog_by_id(session, log_entry.dialog_id)
    if not dialog:
        return

    # Получаем данные клиента
    client_user = await session.get(User, dialog.client_id)
    if not client_user:
        return

    # ТЗ: "у клиента оно должно просто поменяется"
    new_text = message.text or message.caption
    
    # Обновляем БД
    if new_text:
        await db_commands.update_log_text(session, log_entry, new_text)
        await session.commit()

    try:
        # Если это текст
        if message.text:
            await bot.edit_message_text(
                chat_id=client_user.telegram_id,
                message_id=log_entry.client_telegram_message_id,
                text=message.text
            )
        # Если это подпись к медиа (фото/видео)
        elif message.caption:
            await bot.edit_message_caption(
                chat_id=client_user.telegram_id,
                message_id=log_entry.client_telegram_message_id,
                caption=message.caption
            )
    except Exception as e:
        log.warning(f"Failed to edit message for client {client_user.telegram_id}: {e}")

# === ЛОГИКА ДЛЯ МЕНЕДЖЕРОВ (ОТВЕТ КЛИЕНТУ) ===
@dp.message(
    F.chat.type.in_({"supergroup", "group"}), # Работаем только в группах
    F.message_thread_id,                      # Только если сообщение внутри ТОПИКА
    F.from_user.is_bot == False,              # Игнорируем сообщения от ботов
    F.content_type.in_({                      # Реагируем на контент
        ContentType.TEXT, ContentType.PHOTO, ContentType.DOCUMENT,
        ContentType.VOICE, ContentType.VIDEO, ContentType.STICKER
    }),
    StateFilter(None)                         # Только если менеджер не в FSM (не создает заявку)
)
async def handle_manager_reply_to_client(message: Message, session: AsyncSession, bot: Bot, state: FSMContext):
    """
    Обрабатывает ответ менеджера из топика и пересылает его клиенту.
    Автоматически возобновляет диалог, если он был закрыт.
    """
    if message.from_user and message.from_user.is_bot:
        return

    # Игнорируем команды
    if message.text and message.text.startswith('/'):
        return

    # 1. Находим диалог по ID топика
    dialog = await db_commands.find_dialog_by_topic(session, message.message_thread_id)
    if not dialog:
        return

    # === НОВАЯ ЛОГИКА ВОЗОБНОВЛЕНИЯ ===
    if dialog.status in ('resolved', 'transferred'):
        # 1. Меняем статус на active
        await db_commands.update_dialog_status(session, dialog.id, 'active')
        
        # 2. Пытаемся технически открыть топик (если он был закрыт галочкой)
        try:
            await bot.reopen_forum_topic(chat_id=dialog.manager_chat_id, message_thread_id=dialog.manager_topic_id)
        except Exception:
            pass # Если уже открыт или ошибка, не страшно

        # 3. Отправляем и закрепляем новый пульт управления
        try:
            reopen_text = (
                f"🔄 <b>Диалог возобновлен менеджером!</b>\n"
                f"👤 Ответственный: @{message.from_user.username}\n"
                f"👇 <i>Панель управления возвращена:</i>"
            )
            
            control_panel_msg = await bot.send_message(
                chat_id=dialog.manager_chat_id,
                message_thread_id=dialog.manager_topic_id,
                text=reopen_text,
                reply_markup=get_manager_control_panel(dialog.id),
                parse_mode="HTML"
            )
            
            await bot.pin_chat_message(
                chat_id=dialog.manager_chat_id,
                message_id=control_panel_msg.message_id,
                disable_notification=True
            )
        except Exception as e:
            log.warning(f"Не удалось вернуть панель управления при ответе менеджера: {e}")

    # Если статус какой-то другой странный (не active, но и не resolved/transferred), блокируем
    elif dialog.status != 'active':
        try:
            await message.reply(f"⚠️ Невозможно отправить сообщение. Статус диалога: `{dialog.status}`.")
        except:
            pass
        return
    # ===================================

    # 2. Получаем данные клиента из БД
    client_user = await session.get(User, dialog.client_id)
    if not client_user:
        await message.reply(f"❌ Ошибка: Не удалось найти профиль клиента ID={dialog.client_id}!")
        return

    # 3. Пересылаем сообщение клиенту
    sent_to_client_message = await forward_message_to_client(bot, client_user.telegram_id, message)

    if not sent_to_client_message:
        return

    # 4. Логируем сообщение в БД
    log_text = ""
    if message.text:
        log_text = message.text
    elif message.caption:
        log_text = f"[{message.content_type}] {message.caption}"
    else:
        log_text = f"[{message.content_type}]"

    manager_user = await db_commands.get_or_create_user(session, message.from_user, 'manager')
    
    await db_commands.add_message_to_log(
        session=session,
        dialog_id=dialog.id,
        sender_role='manager',
        sender_name=manager_user.full_name,
        text=log_text.strip(),
        client_telegram_message_id=sent_to_client_message.message_id, 
        manager_telegram_message_id=message.message_id                
    )
    await db_commands.reset_sla_status(session, dialog.id)

    await session.commit()

@dp.edited_message()
async def on_message_edited_or_deleted(message: types.Message, session: AsyncSession):
    """
    Срабатывает, когда сообщение редактируется. В некоторых случаях (когда сообщение
    "удаляется для всех"), оно сначала редактируется на "Сообщение удалено",
    а потом приходит событие о фактическом удалении.
    Этот хендлер - дополнительная попытка отловить такие события.
    """
    # Этот хендлер больше для будущего, основная логика ниже.
    pass

@dp.update()
async def on_raw_update(update: Update, session: AsyncSession, bot: Bot):
    update_dict = update.model_dump(exclude_none=True)

    # 1. Обычное удаление (одиночное) - часто бывает когда менеджер удаляет сообщение
    if 'message_delete' in update_dict:
        chat_id = update_dict['message_delete']['chat']['id']
        message_id = update_dict['message_delete']['message_id']
        
        # Если это чат менеджеров (группа), значит менеджер удалил сообщение
        # Можно попытаться найти его в БД и удалить у клиента сразу
        async with session.begin():
            log_entry = await db_commands.get_log_entry_by_manager_msg_id(session, message_id)
            if log_entry and not log_entry.is_deleted:
                log_entry.is_deleted = True
                # Получаем диалог, чтобы узнать ID клиента
                dialog = await db_commands.get_dialog_by_id(session, log_entry.dialog_id)
                if dialog and dialog.client:
                    try:
                        await bot.delete_message(chat_id=dialog.client.telegram_id, message_id=log_entry.client_telegram_message_id)
                        log.info(f"Instant delete sync: Removed message from client {dialog.client.telegram_id}")
                    except Exception as e:
                        log.warning(f"Failed instant delete: {e}")

    # Сценарий 1: Массовое удаление (самый частый и надежный)
    if 'message_delete_bulk' in update_dict:
        chat_id = update_dict['message_delete_bulk']['chat']['id']
        message_ids = update_dict['message_delete_bulk']['message_ids']
        
        log.info(f"{len(message_ids)} messages were deleted in bulk in chat {chat_id}.")
        
        # Используем транзакцию для массового обновления
        async with session.begin():
            for message_id in message_ids:
                await db_commands.mark_message_as_deleted(session, message_id)
        
        # session.commit() здесь не нужен, т.к. `async with session.begin()` делает это автоматически
        log.info(f"Finished marking {len(message_ids)} bulk-deleted messages.")



@dp.callback_query(ManagerCallback.filter(F.action == "resolve"))
async def resolve_dialog_callback(query: CallbackQuery, callback_data: ManagerCallback, session: AsyncSession, bot: Bot):
    dialog = await db_commands.get_dialog_by_id(session, callback_data.dialog_id)
    if not dialog or dialog.status == 'resolved': await query.answer("Диалог уже был решен.", show_alert=True); return
    await db_commands.update_dialog_status(session, dialog.id, 'resolved')
    try: await bot.close_forum_topic(chat_id=dialog.manager_chat_id, message_thread_id=dialog.manager_topic_id)
    except Exception as e: logging.error(f"Could not close topic {dialog.manager_topic_id}: {e}")
    client_user: User = await session.get(User, dialog.client_id)
    try: await bot.send_message(chat_id=client_user.telegram_id, text="Благодарим за обращение! Ваш вопрос решен.")
    except Exception as e: logging.warning(f"Could not send CSAT to client {client_user.telegram_id} (bot might be blocked): {e}")
    await query.message.edit_text(f"{query.message.text}\n\n✅ **Диалог завершен менеджером @{query.from_user.username}**", parse_mode="Markdown")
    await query.answer("Диалог успешно завершен.")
    await session.commit()

@dp.callback_query(ManagerCallback.filter(F.action == "transfer"))
async def transfer_dialog_callback(query: CallbackQuery, callback_data: ManagerCallback, session: AsyncSession, bot: Bot):
    await query.answer("Ищу менеджера...")

    # 1. Получаем текущий диалог и клиента
    old_dialog = await db_commands.get_dialog_by_id(session, callback_data.dialog_id)
    if not old_dialog:
        await query.message.answer("⚠️ Ошибка: диалог не найден.")
        return
        
    client_user = await session.get(User, old_dialog.client_id)
    if not client_user:
        await query.message.answer("⚠️ Ошибка: клиент не найден.")
        return

    # 2. Ищем НОВОГО менеджера, исключая СЕБЯ (query.from_user.id)
    # Используем обновленную функцию из db/commands.py
    new_manager_employee = await db_commands.find_free_manager(
        session, 
        exclude_telegram_id=query.from_user.id 
    )
    
    if not new_manager_employee:
        await query.message.answer("⚠️ Некого выбрать: другие менеджеры заняты или оффлайн.")
        return
    
    if not new_manager_employee.work_chat_id:
        await query.message.answer(f"⚠️ Ошибка: у менеджера {new_manager_employee.full_name} нет рабочего чата.")
        return

    # 3. Создаем User для нового менеджера (если его еще нет в таблице users)
    new_manager_aiogram_user = AiogramUser(
        id=new_manager_employee.personal_telegram_id,
        is_bot=False,
        first_name=new_manager_employee.full_name.split()[0],
        full_name=new_manager_employee.full_name
    )
    new_manager_user = await db_commands.get_or_create_user(session, new_manager_aiogram_user, 'manager')

    # 4. Создаем новый топик в чате НОВОГО менеджера
    try:
        user_display_name = client_user.full_name or f"User {client_user.telegram_id}"
        # В названии топика пишем от кого пришло
        new_topic = await bot.create_forum_topic(
            chat_id=new_manager_employee.work_chat_id,
            name=f"➡️ От {query.from_user.full_name}: {user_display_name}"
        )
    except Exception as e:
        log.error(f"Error creating topic: {e}")
        await query.message.answer("❌ Техническая ошибка при создании топика.")
        return
    
    # === БЛОК: ПЕРЕДАЧА ЗАМЕТОК ===
    # Получаем все заметки по клиенту
    client_notes = await db_commands.get_all_notes_for_client(session, old_dialog.id)
    
    if client_notes:
        notes_text = "📝 <b>ВАЖНЫЕ ЗАМЕТКИ ПО КЛИЕНТУ:</b>\n\n"
        for note in client_notes:
            author = note.author.full_name if note.author else "System"
            notes_text += f"📌 <b>{author}:</b> {note.text}\n"
        
        try:
            await bot.send_message(
                chat_id=new_manager_employee.work_chat_id,
                message_thread_id=new_topic.message_thread_id,
                text=notes_text,
                parse_mode="HTML"
            )
        except Exception as e:
            log.error(f"Failed to send notes during transfer: {e}")
    # ==============================

    # 5. ГЕНЕРАЦИЯ ПОЛНОЙ ИСТОРИИ
    # Запрашиваем историю по ID клиента -> получим сообщения из всех предыдущих диалогов
    full_history = await db_commands.get_full_history_for_client(session, client_user.id)
    
    if full_history:
        history_text = "📜 <b>ИСТОРИЯ ПЕРЕПИСКИ</b>\n"
        history_text += "<i>(Хронология общения с разными менеджерами)</i>\n\n"
        
        for msg in full_history:
            # Формируем заголовок: Имя (Роль)
            if msg.sender_role == 'client':
                header = f"👤 <b>Клиент ({msg.sender_name})</b>"
            else:
                header = f"👨‍💻 <b>Support ({msg.sender_name})</b>"
            
            # Добавляем текст сообщения
            history_text += f"{header}:\n{msg.text}\n\n"
        
        # Отправляем историю кусками (чтобы не превысить лимит 4096 символов)
        for chunk in split_text(history_text, 3800): 
            try:
                await bot.send_message(
                    chat_id=new_manager_employee.work_chat_id,
                    message_thread_id=new_topic.message_thread_id,
                    text=chunk.strip(),
                    parse_mode="HTML"
                )
                await asyncio.sleep(0.3) # Анти-флуд
            except Exception as e:
                log.warning(f"History send error: {e}")
    else:
        await bot.send_message(
            chat_id=new_manager_employee.work_chat_id,
            message_thread_id=new_topic.message_thread_id,
            text="<i>История переписки пуста.</i>",
            parse_mode="HTML"
        )

    # 6. Закрываем старый диалог (ставим статус transferred)
    await db_commands.update_dialog_status(session, old_dialog.id, 'transferred')
    try:
        # Переименовываем старый топик
        await bot.edit_forum_topic(
            chat_id=old_dialog.manager_chat_id,
            message_thread_id=old_dialog.manager_topic_id,
            name=f"✅ Передан -> {new_manager_employee.full_name}"
        )
        # Закрываем старый топик
        await bot.close_forum_topic(
            chat_id=old_dialog.manager_chat_id,
            message_thread_id=old_dialog.manager_topic_id
        )
    except Exception:
        pass

    # 7. Создаем запись нового диалога в БД
    new_dialog = await db_commands.create_dialog(
        session,
        client_id=client_user.id,
        manager_id=new_manager_user.id,
        manager_chat_id=new_manager_employee.work_chat_id,
        topic_id=new_topic.message_thread_id,
    )
    new_dialog.status = 'active'
    await session.flush()
    
    # 8. Отправляем пульт управления новому менеджеру
    manager_greeting = (f"❗️ <b>Диалог передан вам!</b>\n"
                        f"Отправил: @{query.from_user.username}\n"
                        f"Клиент: {client_user.full_name}\n"
                        f"✅ Вы назначены ответственным.")
                        
    control_msg = await bot.send_message(
        chat_id=new_manager_employee.work_chat_id,
        message_thread_id=new_topic.message_thread_id,
        text=manager_greeting,
        reply_markup=get_manager_control_panel(new_dialog.id),
        parse_mode="HTML"
    )
    
    # Пытаемся закрепить (с защитой от ошибок)
    try:
        await bot.pin_chat_message(
            chat_id=new_manager_employee.work_chat_id,
            message_id=control_msg.message_id,
            disable_notification=True
        )
    except Exception:
        pass

    # Уведомляем того, кто передал
    await query.message.answer(f"✅ Успешно передано менеджеру {new_manager_employee.full_name}")
    await session.commit()

# === ФУНКЦИОНАЛ: ЗАМЕТКИ ===
@dp.callback_query(ManagerCallback.filter(F.action == "add_note"))
async def start_add_note(query: CallbackQuery, callback_data: ManagerCallback, state: FSMContext, session: AsyncSession):
    await query.answer()
    
    # Ищем заметки по всему клиенту
    notes = await db_commands.get_all_notes_for_client(session, callback_data.dialog_id)
    
    text = "📝 <b>История заметок по клиенту:</b>\n\n"
    if notes:
        for note in notes:
            author = note.author.full_name if note.author else "System"
            date = note.created_at.strftime("%d.%m")
            # Жирный шрифт для автора и даты
            text += f"🔹 <b>{date} ({author}):</b> {note.text}\n"
    else:
        text += "<i>Заметок пока нет.</i>\n"
        
    text += "\n✍️ <b>Введите текст новой заметки:</b>"
    
    # Добавляем кнопку отмены
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Закрыть", callback_data="cancel_note")
    
    # ВАЖНО: parse_mode="HTML" включает жирный шрифт
    sent_msg = await query.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    
    # Сохраняем данные
    await state.update_data(
        dialog_id=callback_data.dialog_id, 
        note_message_id=sent_msg.message_id # Запоминаем ID, чтобы удалить потом
    )
    await state.set_state(ManagerFSM.adding_note)

@dp.message(StateFilter(ManagerFSM.adding_note))
async def save_note_handler(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    data = await state.get_data()
    dialog_id = data.get('dialog_id')
    note_msg_id = data.get('note_message_id') # ID сообщения с историей заметок
    
    if not dialog_id:
        await message.answer("❌ Ошибка контекста.")
        await state.clear()
        return

    manager_user = await db_commands.get_or_create_user(session, message.from_user, 'manager')

    await db_commands.create_note(
        session,
        dialog_id=dialog_id,
        author_id=manager_user.id,
        text=message.text
    )
    await session.commit()
    
    # Удаляем сообщение с инструкцией и историей (чтобы не захламлять чат)
    if note_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=note_msg_id)
        except:
            pass
            
    # Удаляем сообщение, которое написал менеджер (сам текст заметки),
    # так как мы подтверждаем сохранение отдельным сообщением.
    try:
        await message.delete()
    except:
        pass
    
    # Отправляем подтверждение
    await message.answer(f"✅ <b>Заметка сохранена:</b>\n{message.text}", parse_mode="HTML")
    
    await state.clear()

# === ФУНКЦИОНАЛ: БАЗА ЗНАНИЙ ===
@dp.callback_query(ManagerCallback.filter(F.action == "kb_search"))
async def start_kb_search(query: CallbackQuery, callback_data: ManagerCallback, state: FSMContext):
    print("🔘 [UI] Нажата кнопка 'База Знаний'")
    await query.answer()
    
    # Сохраняем ID диалога (на всякий случай)
    await state.update_data(dialog_id=callback_data.dialog_id)
    
    text = (
        "📚 <b>Поиск по Базе Знаний</b>\n\n"
        "Введите <b>#хештег</b> или ключевое слово.\n"
        "Например: <code>Верификация</code>"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Выйти из поиска", callback_data="cancel_kb_search")
    
    # Отправляем НОВОЕ сообщение вниз
    msg = await query.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    
    # Запоминаем ID сообщения, чтобы кнопка "Выйти" удалила его
    await state.update_data(search_message_id=msg.message_id)
    
    # Включаем режим поиска
    await state.set_state(ManagerFSM.searching_kb)

@dp.callback_query(F.data == "cancel_note")
async def cancel_note_handler(query: CallbackQuery, state: FSMContext, bot: Bot):
    """Закрывает режим заметок и удаляет сообщение."""
    data = await state.get_data()
    note_msg_id = data.get('note_message_id')
    
    await state.clear()
    
    # Удаляем сообщение с историей заметок
    if note_msg_id:
        try:
            await bot.delete_message(chat_id=query.message.chat.id, message_id=note_msg_id)
        except:
            pass
    # На случай если ID не сохранился, пробуем удалить то, на которое нажали
    try:
        await query.message.delete()
    except:
        pass

    await query.answer("Отменено")

@dp.message(StateFilter(ManagerFSM.searching_kb))
async def perform_kb_search(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    print(f"📨 [Search] Получен запрос: '{message.text}'")
    
    # Игнорируем команды
    if message.text.startswith("/"):
        return

    search_query = message.text.strip()
    
    # Удаляем сообщение менеджера (запрос), чтобы было чисто
    try:
        await message.delete()
    except:
        pass

    # Ищем в БД
    results = await db_commands.search_knowledge_base(session, search_query)
    
    # Кнопка выхода нужна в любом случае
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Выйти из поиска", callback_data="cancel_kb_search")

    if not results:
        print("❌ [Search] Ничего не найдено")
        # Удаляем старое меню поиска, чтобы прислать новое с ошибкой
        data = await state.get_data()
        old_msg_id = data.get('search_message_id')
        if old_msg_id:
            try: await bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
            except: pass

        new_msg = await message.answer(
            f"😔 По запросу '<b>{search_query}</b>' ничего не найдено.\nПопробуйте другой запрос.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.update_data(search_message_id=new_msg.message_id)
        return

    # Если нашли
    print(f"✅ [Search] Найдено {len(results)} записей. Пересылаем...")
    
    # Удаляем меню поиска перед отправкой результатов
    data = await state.get_data()
    old_msg_id = data.get('search_message_id')
    if old_msg_id:
        try: await bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
        except: pass

    # Пересылаем сообщения
    await message.answer(f"🔎 <b>Результаты ({len(results)} шт.):</b>", parse_mode="HTML")
    
    for entry in results:
        try:
            await bot.forward_message(
                chat_id=message.chat.id,
                message_thread_id=message.message_thread_id,
                from_chat_id=settings.knowledge_base_channel_id,
                message_id=entry.message_id
            )
            await asyncio.sleep(0.2)
        except Exception as e:
            log.error(f"Forward error: {e}")

    # Отправляем новое меню поиска вниз
    final_msg = await message.answer(
        "👇 Введите следующий запрос или нажмите Выйти:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.update_data(search_message_id=final_msg.message_id)

# ==========================================
# === УПРАВЛЕНИЕ ПРОЦЕССОМ ЗАЯВКИ (ПАУЗА) ===
# ==========================================

@dp.callback_query(F.data == "app_pause", StateFilter(ManagerFSM))
async def app_pause_handler(query: CallbackQuery, state: FSMContext, bot: Bot):
    """Ставит заявку на паузу."""
    current_state = await state.get_state()
    
    # Сохраняем состояние
    await state.update_data(saved_state=current_state)
    
    # Сбрасываем состояние, чтобы работал обычный чат
    await state.set_state(None)
    
    text = (
        "⏸ <b>Заявка на паузе.</b>\n\n"
        "Режим FSM отключен. Вы можете писать клиенту в этот чат.\n"
        "Чтобы вернуться к заполнению, нажмите кнопку ниже."
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Продолжить заполнение", callback_data="app_resume")
    
    await query.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await query.answer("На паузе")

@dp.callback_query(F.data == "app_resume")
async def app_resume_handler(query: CallbackQuery, state: FSMContext, bot: Bot, session: AsyncSession): 
    """Возобновляет заявку и восстанавливает клавиатуру."""
    data = await state.get_data()
    saved_state_str = data.get('saved_state')
    last_prompt = data.get('last_prompt') or "▶️ Продолжаем ввод данных."
    
    if not saved_state_str:
        await query.message.edit_text("❌ Ошибка восстановления. Начните заново.")
        return

    # Восстанавливаем состояние
    await state.set_state(saved_state_str)
    
    # === ВОССТАНОВЛЕНИЕ КЛАВИАТУРЫ ===
    # Определяем, какие кнопки нужно показать для этого шага
    kb = None
    
    if saved_state_str == ManagerFSM.app_selecting_direction.state:
        kb = get_app_step_keyboard({"Обратная": "Обратная", "Прямая": "Прямая"})
    
    elif saved_state_str == ManagerFSM.app_selecting_city.state:
        cities = await db_commands.get_all_cities(session)
        city_btns = {city.name: f"city_id:{city.id}" for city in cities}
        kb = get_app_step_keyboard(city_btns)
        
    elif saved_state_str == ManagerFSM.app_selecting_action.state:
        kb = get_app_step_keyboard({"Принять": "Принять", "Выдать": "Выдать"})
        
    elif saved_state_str in (ManagerFSM.app_selecting_currency_to_get.state, ManagerFSM.app_selecting_currency_to_give.state):
        curr_btns = {c: c for c in CURRENCIES}
        kb = get_app_step_keyboard(curr_btns)
        
    elif saved_state_str == ManagerFSM.app_asking_client_id.state:
        kb = get_app_step_keyboard({"Да": "Да", "Нет": "Нет"})

    elif saved_state_str == ManagerFSM.app_entering_datetime.state:
        date_btns = {"Сегодня": "set_date_today", "Завтра": "set_date_tomorrow", "Послезавтра": "set_date_day_after"}
        kb = get_app_step_keyboard(date_btns)

    else:
        # Для текстовых полей (Фамилия, Сумма и т.д.) только кнопки управления
        kb = get_app_step_keyboard()

    await query.message.edit_text(text=last_prompt, reply_markup=kb, parse_mode="HTML")
    await query.answer("Продолжаем...")


@dp.callback_query(F.data == "app_cancel")
async def app_cancel_btn_handler(query: CallbackQuery, state: FSMContext):
    await state.clear()
    await query.message.edit_text("❌ Создание заявки отменено.")
    await query.answer()

# === ФУНКЦИОНАЛ: ЭСКАЛАЦИЯ ===
@dp.callback_query(ManagerCallback.filter(F.action == "escalate"))
async def start_escalation(query: CallbackQuery, callback_data: ManagerCallback, state: FSMContext):
    await query.answer()
    
    text = (
        "🆘 <b>Эскалация проблемы</b>\n\n"
        "Опишите причину эскалации и проблему. Это сообщение будет отправлено руководителям."
    )
    
    await state.update_data(dialog_id=callback_data.dialog_id)
    await edit_or_send_message(query.message, state, text=text, is_callback=True)
    await state.set_state(ManagerFSM.escalating_dialog)


@dp.message(StateFilter(ManagerFSM.escalating_dialog))
async def perform_escalation(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    data = await state.get_data()
    dialog_id = data.get('dialog_id')
    reason = message.text
    
    dialog = await db_commands.get_dialog_by_id(session, dialog_id)
    if not dialog:
        await message.answer("Ошибка: диалог не найден.")
        await state.clear()
        return

    # 1. Формируем ссылку на текущий топик, чтобы супервайзер мог перейти
    # Формат ссылки на топик: https://t.me/c/ID_ЧАТА/ID_ТОПИКА
    chat_id_str = str(dialog.manager_chat_id).replace("-100", "")
    topic_link = f"https://t.me/c/{chat_id_str}/{dialog.manager_topic_id}"
    
    # 2. Сообщение для канала эскалации
    alert_text = (
        f"🚨 <b>НОВАЯ ЭСКАЛАЦИЯ</b>\n\n"
        f"👨‍💻 <b>Менеджер:</b> @{message.from_user.username}\n"
        f"👤 <b>Клиент:</b> {dialog.client.full_name} (ID: {dialog.client.telegram_id})\n"
        f"📝 <b>Причина:</b> {reason}\n\n"
        f"🔗 <a href='{topic_link}'>Перейти к диалогу</a>"
    )
    
    try:
        # Отправляем в канал эскалации (указан в config.py)
        await bot.send_message(
            chat_id=settings.escalation_channel_id,
            text=alert_text,
            parse_mode="HTML"
        )
        
        # 3. Обновляем статус диалога
        await db_commands.update_dialog_status(session, dialog_id, 'escalated')
        
        await message.answer("✅ <b>Эскалация отправлена!</b> Руководители уведомлены.")
        
    except Exception as e:
        log.error(f"Failed to send escalation: {e}")
        await message.answer(f"❌ Не удалось отправить эскалацию. Проверьте настройки ID канала.\nОшибка: {e}")
        
    await state.clear()

# =================================================================
# === FSM-ЛОГИКА СОЗДАНИЯ ЗАЯВКИ (ФИНАЛЬНАЯ ВЕРСИЯ С ЧИСТЫМ UX) ===
# =================================================================

# --- 0. Обработчик отмены ---
@dp.message(Command("cancel"), StateFilter(ManagerFSM))
async def cancel_fsm(message: Message, state: FSMContext):
    data = await state.get_data()
    last_bot_message_id = data.get('last_bot_message_id')
    if last_bot_message_id:
        try:
            await message.bot.delete_message(message.chat.id, last_bot_message_id)
        except Exception:
            pass
    await state.clear()
    await message.answer("Действие отменено.")


# --- 1. Старт FSM (без выбора типа) ---
@dp.callback_query(ManagerCallback.filter(F.action == "create_app"))
async def start_create_application(query: CallbackQuery, callback_data: ManagerCallback, state: FSMContext):
    await query.answer()
    await state.clear()
    await state.update_data(dialog_id=callback_data.dialog_id, type='Частная', brand='VIP-Obmen')
    
    prompt = "Шаг 1: Выберите направление заявки:"
    await state.update_data(last_prompt=prompt)
    
    # Кнопки
    buttons = {"Обратная": "Обратная", "Прямая": "Прямая"}
    kb = get_app_step_keyboard(buttons)
    
    await edit_or_send_message(query.message, state, text=prompt, reply_markup=kb, is_callback=True)
    await state.set_state(ManagerFSM.app_selecting_direction)


# --- 2. Выбор направления ---
@dp.callback_query(StateFilter(ManagerFSM.app_selecting_direction))
async def app_select_direction(query: CallbackQuery, state: FSMContext):
    await query.answer()
    
    # Получаем старые данные ДО обновления
    data = await state.get_data()
    old_direction = data.get('direction')
    new_direction = query.data
    
    # Сохраняем новое направление
    await state.update_data(direction=new_direction)
    
    # ЛОГИКА: Если это редактирование И направление изменилось
    if data.get('editing_mode') and old_direction != new_direction:
        
        # Включаем режим "Цепочка обновлений"
        await state.update_data(chain_update=True)
        
        # Определяем, какую сумму переспрашивать (зависит от действия)
        action = data.get('action') # 'Принять' или 'Выдать'
        
        if action == 'Принять':
            prompt = f"⚠️ Направление изменилось на '{new_direction}'.\nВведите новую <b>сумму принятия</b>:"
            next_state = ManagerFSM.app_entering_amount_to_get
        elif action == 'Выдать':
            prompt = f"⚠️ Направление изменилось на '{new_direction}'.\nВведите новую <b>сумму выдачи</b>:"
            next_state = ManagerFSM.app_entering_amount_to_give
        else:
            # Если действия вдруг нет (маловероятно при редактировании), просим выбрать действие
            prompt = f"⚠️ Направление изменилось. Что нужно сделать?"
            next_state = ManagerFSM.app_selecting_action
            kb = get_app_step_keyboard({"Принять": "Принять", "Выдать": "Выдать"})
            await state.update_data(last_prompt=prompt)
            await query.message.edit_text(prompt, reply_markup=kb, parse_mode="HTML")
            await state.set_state(next_state)
            return

        # Запоминаем вопрос и переходим к вводу суммы
        await state.update_data(last_prompt=prompt)
        kb = get_app_step_keyboard() # Текстовый ввод
        
        # Отправляем сообщение
        await query.message.edit_text(prompt, reply_markup=kb, parse_mode="HTML")
        await state.set_state(next_state)
        return

    # --- Стандартная логика (если не редактирование или направление не менялось) ---
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(query.message, state, edit_mode=True)
        return

    prompt = "Шаг 2: Введите фамилию клиента:"
    await state.update_data(last_prompt=prompt)
    kb = get_app_step_keyboard()
    await edit_or_send_message(query.message, state, text=prompt, reply_markup=kb, is_callback=True)
    await state.set_state(ManagerFSM.app_entering_last_name)


# --- 3. Ввод Фамилии ---
@dp.message(StateFilter(ManagerFSM.app_entering_last_name))
async def app_enter_last_name(message: Message, state: FSMContext):
    await state.update_data(last_name=message.text)
    try: await message.delete() 
    except: pass

    data = await state.get_data()
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(message, state)
        return

    prompt = "Шаг 3: Введите имя клиента:"
    await state.update_data(last_prompt=prompt)
    
    kb = get_app_step_keyboard()
    await edit_or_send_message(message, state, text=prompt, reply_markup=kb)
    await state.set_state(ManagerFSM.app_entering_first_name)


# --- 4. Ввод Имени ---
@dp.message(StateFilter(ManagerFSM.app_entering_first_name))
async def app_enter_first_name(message: Message, state: FSMContext):
    await state.update_data(first_name=message.text)
    try: await message.delete()
    except: pass
    
    data = await state.get_data()
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(message, state)
        return

    prompt = "Шаг 4: Введите отчество клиента:"
    await state.update_data(last_prompt=prompt)
    
    kb = get_app_step_keyboard()
    await edit_or_send_message(message, state, text=prompt, reply_markup=kb)
    await state.set_state(ManagerFSM.app_entering_patronymic)


# --- 5. Ввод Отчества и запрос Даты ---
@dp.message(StateFilter(ManagerFSM.app_entering_patronymic))
async def app_enter_patronymic(message: Message, state: FSMContext):
    await state.update_data(patronymic=message.text)
    try: await message.delete()
    except: pass

    data = await state.get_data()
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(message, state)
        return

    # Вызов функции даты (см. ниже)
    await ask_for_datetime(message, state)
    await state.set_state(ManagerFSM.app_entering_datetime)

@dp.callback_query(F.data.startswith("set_date_"), StateFilter(ManagerFSM.app_entering_datetime))
async def suggest_date_from_button(query: CallbackQuery, state: FSMContext):
    """
    Ловит нажатие на кнопку быстрой даты и предлагает ее в тексте сообщения,
    чтобы менеджер мог ее скопировать/отредактировать и отправить.
    """
    await query.answer()
    
    today = date.today()
    if query.data == "set_date_today":
        selected_date = today
    elif query.data == "set_date_tomorrow":
        selected_date = today + timedelta(days=1)
    else: # day_after
        selected_date = today + timedelta(days=2)

    date_text = selected_date.strftime('%d.%m.%Y 00:00')

    # Мы не переходим на следующий шаг, а редактируем сообщение,
    # предлагая менеджеру готовую дату для отправки.
    prompt = (
        f"Предложенная дата: `{date_text}`\n\n"
        "Скопируйте ее, при необходимости измените время и отправьте следующим сообщением."
    )
    
    # Редактируем сообщение и убираем кнопки
    await query.message.edit_text(prompt, reply_markup=None, parse_mode="Markdown")


# --- 6. Ввод Даты ---
@dp.message(StateFilter(ManagerFSM.app_entering_datetime))
async def app_enter_datetime(message: Message, state: FSMContext, session: AsyncSession):
    try:
        datetime.strptime(message.text, '%d.%m.%Y %H:%M')
    except ValueError:
        try: await message.delete()
        except: pass
        await ask_for_datetime(message, state, error=True)
        return

    await state.update_data(datetime=message.text)
    try: await message.delete()
    except: pass

    data = await state.get_data()
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(message, state)
        return

    # Загружаем города из БД
    cities = await db_commands.get_all_cities(session)
    
    prompt = "Шаг 6: Выберите город:"
    await state.update_data(last_prompt=prompt)
    
    # В callback_data кладем ID города
    city_btns = {city.name: f"city_id:{city.id}" for city in cities}
    kb = get_app_step_keyboard(city_btns)
    
    await edit_or_send_message(message, state, text=prompt, reply_markup=kb)
    await state.set_state(ManagerFSM.app_selecting_city)

@dp.callback_query(StateFilter(ManagerFSM.app_selecting_city), F.data.startswith("city_id:"))
async def app_select_city(query: CallbackQuery, state: FSMContext, session: AsyncSession):
    city_id = int(query.data.split(":")[1])
    city = await db_commands.get_city_by_id(session, city_id)
    
    if not city:
        await query.answer("Ошибка: город не найден.")
        return

    await query.answer()
    # Сохраняем ID для робота и Name для текста
    await state.update_data(city_id=city.id, city_name=city.name)
    
    data = await state.get_data()
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(query.message, state, edit_mode=True)
        return

    prompt = "Шаг 7: Что нужно сделать?"
    await state.update_data(last_prompt=prompt)
    kb = get_app_step_keyboard({"Принять": "Принять", "Выдать": "Выдать"})
    
    await query.message.edit_text(text=prompt, reply_markup=kb, parse_mode="HTML")
    await state.set_state(ManagerFSM.app_selecting_action)

# --- 7. Выбор действия (Принять/Выдать) ---
@dp.callback_query(StateFilter(ManagerFSM.app_selecting_action))
async def app_select_action(query: CallbackQuery, state: FSMContext):
    await query.answer()
    
    # Получаем старые данные
    data = await state.get_data()
    old_action = data.get('action')
    new_action = query.data
    
    await state.update_data(action=new_action)
    
    # ЛОГИКА: Если редактируем И действие изменилось (Принять <-> Выдать)
    if data.get('editing_mode') and old_action != new_action:
        # Включаем цепочку обновлений
        await state.update_data(chain_update=True)
        
        if new_action == 'Принять':
            prompt = f"⚠️ Действие изменилось на 'Принять'.\nВведите <b>сумму принятия</b>:"
            next_state = ManagerFSM.app_entering_amount_to_get
        else:
            prompt = f"⚠️ Действие изменилось на 'Выдать'.\nВведите <b>сумму выдачи</b>:"
            next_state = ManagerFSM.app_entering_amount_to_give
            
        await state.update_data(last_prompt=prompt)
        kb = get_app_step_keyboard()
        await query.message.edit_text(prompt, reply_markup=kb, parse_mode="HTML")
        await state.set_state(next_state)
        return

    # Стандартная логика (если не меняли или это первое создание)
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(query.message, state, edit_mode=True)
        return

    if new_action == 'Принять':
        prompt = "Шаг 7: Сколько нужно принять?"
        next_state = ManagerFSM.app_entering_amount_to_get
    else:
        prompt = "Шаг 7: Сколько нужно выдать?"
        next_state = ManagerFSM.app_entering_amount_to_give
        
    await state.update_data(last_prompt=prompt)
    kb = get_app_step_keyboard()
    await edit_or_send_message(query.message, state, text=prompt, reply_markup=kb, is_callback=True)
    await state.set_state(next_state)

# --- Далее все хендлеры по аналогии... ---
# (Полный код для всех оставшихся хендлеров будет ниже, чтобы не разрывать блок)

@dp.message(StateFilter(ManagerFSM.app_entering_amount_to_get))
async def app_enter_amount_get(message: Message, state: FSMContext):
    await state.update_data(amount_to_get=message.text)
    try: await message.delete()
    except: pass

    data = await state.get_data()
    
    # === ИСПРАВЛЕНИЕ ЗДЕСЬ ===
    # Выходим из редактирования, только если это НЕ цепочка обновлений.
    # Если chain_update=True, мы должны пойти дальше выбирать валюту.
    if data.get('editing_mode') and not data.get('chain_update'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(message, state)
        return
    # =========================

    prompt = "Шаг 8: Выберите валюту принятия:"
    await state.update_data(last_prompt=prompt)
    
    curr_btns = {c: c for c in CURRENCIES}
    kb = get_app_step_keyboard(curr_btns)
    
    await edit_or_send_message(message, state, text=prompt, reply_markup=kb)
    await state.set_state(ManagerFSM.app_selecting_currency_to_get)

@dp.message(StateFilter(ManagerFSM.app_entering_amount_to_give))
async def app_enter_amount_give(message: Message, state: FSMContext):
    # 1. Сохраняем введенную сумму
    await state.update_data(amount_to_give=message.text)
    
    # Удаляем сообщение пользователя для чистоты чата
    try: await message.delete()
    except: pass

    data = await state.get_data()
    
    # 2. Логика выхода при редактировании
    # Если мы в режиме редактирования, НО это не цепочка обновлений (смена действия или суммы+валюты),
    # то сразу выходим. Иначе - идем выбирать валюту.
    if data.get('editing_mode') and not data.get('chain_update'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(message, state)
        return

    # 3. Переход к следующему шагу (Валюта)
    prompt = "Шаг 8: Выберите валюту выдачи:"
    await state.update_data(last_prompt=prompt)
    
    # Генерируем кнопки валют
    curr_btns = {c: c for c in CURRENCIES}
    kb = get_app_step_keyboard(curr_btns)
    
    await edit_or_send_message(message, state, text=prompt, reply_markup=kb)
    await state.set_state(ManagerFSM.app_selecting_currency_to_give)

@dp.callback_query(StateFilter(ManagerFSM.app_confirming_percent_change))
async def app_handle_percent_change_choice(query: CallbackQuery, state: FSMContext):
    await query.answer()
    
    # Выключаем режим цепочки, так как это последний шаг цепочки
    await state.update_data(chain_update=False)
    # Выключаем режим редактирования (мы вернемся в него автоматически, показав summary)
    await state.update_data(editing_mode=False)

    if query.data == "yes_change_perc":
        # Если ДА -> Просим ввести процент
        prompt = "Введите новый процент:"
        await state.update_data(last_prompt=prompt)
        kb = get_app_step_keyboard()
        
        # Ставим editing_mode=True, чтобы после ввода процента нас вернуло на экран подтверждения
        await state.update_data(editing_mode=True) 
        
        await query.message.edit_text(prompt, reply_markup=kb, parse_mode="HTML")
        await state.set_state(ManagerFSM.app_entering_our_percent)
    else:
        # Если НЕТ -> Сразу показываем итог
        await display_confirmation_screen(query.message, state, edit_mode=True)

@dp.callback_query(StateFilter(ManagerFSM.app_selecting_currency_to_get))
async def app_select_currency_get(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(currency_to_get=query.data)
    
    data = await state.get_data()
    
    # ЕСЛИ ЭТО ЦЕПОЧКА ОБНОВЛЕНИЙ -> Спрашиваем про процент
    if data.get('chain_update'):
        prompt = "Нужно ли изменить процент?"
        await state.update_data(last_prompt=prompt)
        kb = get_app_step_keyboard({"Да": "yes_change_perc", "Нет": "no_change_perc"})
        
        await query.message.edit_text(prompt, reply_markup=kb, parse_mode="HTML")
        await state.set_state(ManagerFSM.app_confirming_percent_change)
        return

    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(query.message, state, edit_mode=True)
        return

    prompt = "Шаг 9: Введите наш процент:"
    await state.update_data(last_prompt=prompt)
    kb = get_app_step_keyboard()
    await edit_or_send_message(query.message, state, text=prompt, reply_markup=kb, is_callback=True)
    await state.set_state(ManagerFSM.app_entering_our_percent)

@dp.callback_query(StateFilter(ManagerFSM.app_selecting_currency_to_give))
async def app_select_currency_give(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(currency_to_give=query.data)
    
    data = await state.get_data()
    
    if data.get('chain_update'):
        prompt = "Нужно ли изменить процент?"
        await state.update_data(last_prompt=prompt)
        kb = get_app_step_keyboard({"Да": "yes_change_perc", "Нет": "no_change_perc"})
        
        await query.message.edit_text(prompt, reply_markup=kb, parse_mode="HTML")
        await state.set_state(ManagerFSM.app_confirming_percent_change)
        return

    # Стандартная логика
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(query.message, state, edit_mode=True)
        return

    prompt = "Шаг 9: Введите наш процент:"
    await state.update_data(last_prompt=prompt)
    kb = get_app_step_keyboard()
    await edit_or_send_message(query.message, state, text=prompt, reply_markup=kb, is_callback=True)
    await state.set_state(ManagerFSM.app_entering_our_percent)
    
@dp.callback_query(StateFilter(ManagerFSM.app_selecting_currency_to_give))
async def app_select_currency_give(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.update_data(currency_to_give=query.data)
    
    data = await state.get_data()
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(query.message, state, edit_mode=True)
        return

    prompt = "Шаг 9: Введите наш процент:"
    await state.update_data(last_prompt=prompt)
    
    kb = get_app_step_keyboard()
    await edit_or_send_message(query.message, state, text=prompt, reply_markup=kb, is_callback=True)
    await state.set_state(ManagerFSM.app_entering_our_percent)

@dp.message(StateFilter(ManagerFSM.app_entering_our_percent))
async def app_enter_our_percent(message: Message, state: FSMContext):
    await state.update_data(our_percent=message.text)
    try: await message.delete()
    except: pass

    data = await state.get_data()
    # === ИСПРАВЛЕНИЕ: Выход при редактировании ===
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
        await display_confirmation_screen(message, state)
        return
    # ============================================

    prompt = "Указать ID клиента?"
    await state.update_data(last_prompt=prompt)
    
    kb = get_app_step_keyboard({"Да": "Да", "Нет": "Нет"})
    await edit_or_send_message(message, state, text=prompt, reply_markup=kb)
    await state.set_state(ManagerFSM.app_asking_client_id)
        
@dp.callback_query(StateFilter(ManagerFSM.app_asking_client_id))
async def app_ask_client_id(query: CallbackQuery, state: FSMContext):
    await query.answer()
    if query.data == 'Да':
        prompt = "Введите ID клиента:"
        await state.update_data(last_prompt=prompt)
        
        kb = get_app_step_keyboard()
        await edit_or_send_message(query.message, state, text=prompt, reply_markup=kb, is_callback=True)
        await state.set_state(ManagerFSM.app_entering_client_id)
    else:
        await state.update_data(client_id=None)
        await display_confirmation_screen(query.message, state, edit_mode=True)

@dp.message(StateFilter(ManagerFSM.app_entering_client_id))
async def app_enter_client_id(message: Message, state: FSMContext):
    await state.update_data(client_id=message.text)
    try: await message.delete()
    except: pass
    
    data = await state.get_data()
    # === ИСПРАВЛЕНИЕ: Сбрасываем флаг ===
    if data.get('editing_mode'):
        await state.update_data(editing_mode=False)
    # ====================================
    
    await display_confirmation_screen(message, state)


async def display_confirmation_screen(message: Message, state: FSMContext, edit_mode: bool = False):
    """Отображает итоговую информацию и кнопки 'Подтвердить' / 'Изменить'."""
    data = await state.get_data()
    summary = format_application_summary(data)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data="confirm_deal")
    builder.button(text="✏️ Изменить", callback_data="edit_deal")
    
    if edit_mode:
        # Если сообщение было с кнопками (callback), его нужно редактировать
        await message.edit_text(summary, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        # Если это был текстовый ответ от пользователя, отправляем новое сообщение
        await message.answer(summary, reply_markup=builder.as_markup(), parse_mode="HTML")

    await state.set_state(ManagerFSM.app_confirmation)

# --- Обработчик для состояния подтверждения ---
@dp.callback_query(StateFilter(ManagerFSM.app_confirmation))
async def app_confirmation_handler(query: CallbackQuery, state: FSMContext, bot: Bot, session: AsyncSession):
    await query.answer()
    
    if query.data == "confirm_deal":
        data = await state.get_data()
        
        # --- 1. Подготовка данных для Redis (как в вашем примере) ---
        # Добавляем информацию о создателе
        data['creator_user_id'] = query.from_user.id
        data['creator_username'] = query.from_user.username
        
        # Убираем лишние данные, которые не нужны бэкенду
        data.pop('editing_mode', None)
        
        # --- 2. Отправка в Redis ---
        try:
            await redis_client.rpush(settings.redis_queue_name, json.dumps(data))
            log.info(f"Заявка успешно отправлена в очередь Redis '{settings.redis_queue_name}'")
        except Exception as e:
            log.error(f"Ошибка отправки заявки в Redis: {e}")
            await query.message.edit_text("❌ Критическая ошибка: не удалось отправить заявку в обработку. Свяжитесь с администратором.")
            await state.clear()
            return

        # --- 3. Отправка уведомлений (клиенту и в канал) ---
        summary_for_manager = format_application_summary(data)
        summary_for_client = format_summary_for_client(data)
        
        # Отправка клиенту
        dialog_id = data.get('dialog_id')
        dialog = await db_commands.get_dialog_by_id(session, dialog_id)
        if dialog and dialog.client:
            try:
                await bot.send_message(
                    chat_id=dialog.client.telegram_id, 
                    text=summary_for_client, # <-- Используем новую переменную
                    parse_mode="HTML"
                )
            except Exception as e:
                log.warning(f"Не удалось отправить копию заявки клиенту {dialog.client.telegram_id}: {e}")
        
        # Отправка в канал заявок (остается без изменений, с полной информацией)
        try:
            await bot.send_message(
                chat_id=settings.applications_channel_id, 
                text=summary_for_manager, # <-- Используем полную версию для менеджеров
                parse_mode="HTML"
            )
        except Exception as e:
            log.error(f"Не удалось отправить копию заявки в канал {settings.applications_channel_id}: {e}")

        # --- 4. Завершение ---
        await query.message.edit_text("✅ Заявка успешно создана и отправлена в обработку!")
        await state.clear()

    elif query.data == "edit_deal":
        # Показываем меню с полями для редактирования
        builder = InlineKeyboardBuilder()
        fields = {
            "edit_direction": "Направление",
            "edit_city": "📍 Город",
            "edit_last_name": "Фамилия",
            "edit_first_name": "Имя",
            "edit_patronymic": "Отчество",
            "edit_datetime": "Время встречи",
            "edit_action": "Действие (Принять/Выдать)",
            "edit_amount_currency": "Сумма и Валюта",
            "edit_percents": "Проценты",
            "edit_client_id": "ID клиента"
        }
        for cb_data, text in fields.items():
             builder.button(text=text, callback_data=cb_data)
        
        builder.button(text="⬅️ Назад", callback_data="back_to_confirmation")
        builder.adjust(2)

        await query.message.edit_text("Какое поле вы хотите изменить?", reply_markup=builder.as_markup())
        await state.set_state(ManagerFSM.app_editing_field)

# --- Обработчик для выбора поля для редактирования ---
@dp.callback_query(StateFilter(ManagerFSM.app_editing_field))
async def app_select_field_to_edit(query: CallbackQuery, state: FSMContext, session: AsyncSession):
    await query.answer()
    
    if query.data == "back_to_confirmation":
        await display_confirmation_screen(query.message, state, edit_mode=True)
        return

    # Получаем текущие данные, чтобы понять контекст (Принять или Выдать)
    data = await state.get_data()
    current_action = data.get('action') # 'Принять' или 'Выдать'

    # Подготовка кнопок
    brand_btns = {b: b for b in BRANDS}
    direction_btns = {"Обратная": "Обратная", "Прямая": "Прямая"}
    action_btns = {"Принять": "Принять", "Выдать": "Выдать"}

    # --- ЛОГИКА ДЛЯ СЛОЖНЫХ ПОЛЕЙ ---
    
    # 1. СУММА И ВАЛЮТА (Запускаем цепочку обновлений)
    if query.data == "edit_amount_currency":
        await state.update_data(editing_mode=True, chain_update=True) # <-- Включаем цепочку
        
        if current_action == 'Принять':
            prompt = "Введите новую сумму принятия:"
            target_state = ManagerFSM.app_entering_amount_to_get
        else: # Выдать
            prompt = "Введите новую сумму выдачи:"
            target_state = ManagerFSM.app_entering_amount_to_give
            
        await state.update_data(last_prompt=prompt)
        await query.message.edit_text(prompt, reply_markup=get_app_step_keyboard(), parse_mode="HTML")
        await state.set_state(target_state)
        return

    # 2. ПРОЦЕНТЫ
    if query.data == "edit_percents":
        prompt = "Введите новый процент:"
        target_state = ManagerFSM.app_entering_our_percent
        # Тут цепочка не нужна, просто меняем и выходим
        await state.update_data(editing_mode=True, last_prompt=prompt)
        await query.message.edit_text(prompt, reply_markup=get_app_step_keyboard(), parse_mode="HTML")
        await state.set_state(target_state)
        return

    # 3. ID КЛИЕНТА
    if query.data == "edit_client_id":
        prompt = "Введите новый ID клиента:"
        target_state = ManagerFSM.app_entering_client_id
        await state.update_data(editing_mode=True, last_prompt=prompt)
        await query.message.edit_text(prompt, reply_markup=get_app_step_keyboard(), parse_mode="HTML")
        await state.set_state(target_state)
        return
    
        
    if query.data == "edit_city":
        cities = await db_commands.get_all_cities(session) # Теперь сессия есть!
        prompt = "Выберите новый город:"
        kb = get_app_step_keyboard({city.name: f"city_id:{city.id}" for city in cities})
        await state.update_data(editing_mode=True, last_prompt=prompt)
        await query.message.edit_text(prompt, reply_markup=kb, parse_mode="HTML")
        await state.set_state(ManagerFSM.app_selecting_city)
        return

  
    # --- КАРТА ДЛЯ ПРОСТЫХ ПОЛЕЙ ---
    field_map = {
        "edit_brand": (
            ManagerFSM.app_selecting_brand, 
            "Выберите новый бренд:", 
            get_app_step_keyboard(brand_btns)
        ),
        "edit_direction": (
            ManagerFSM.app_selecting_direction, 
            "Выберите направление:", 
            get_app_step_keyboard(direction_btns)
        ),
        "edit_last_name": (
            ManagerFSM.app_entering_last_name, 
            "Введите новую фамилию:", 
            get_app_step_keyboard()
        ),
        "edit_first_name": (
            ManagerFSM.app_entering_first_name, 
            "Введите новое имя:", 
            get_app_step_keyboard()
        ),
        "edit_patronymic": (
            ManagerFSM.app_entering_patronymic, 
            "Введите новое отчество:", 
            get_app_step_keyboard()
        ),
        "edit_datetime": (
            ManagerFSM.app_entering_datetime, 
            "Введите новую дату и время:", 
            get_app_step_keyboard({"Сегодня": "set_date_today", "Завтра": "set_date_tomorrow"})
        ),
        "edit_action": (
            ManagerFSM.app_selecting_action, 
            "Что нужно сделать?", 
            get_app_step_keyboard(action_btns)
        ),
    }

    if query.data in field_map:
        target_state, prompt, markup = field_map[query.data]
        
        await state.update_data(editing_mode=True, field_to_edit=query.data, last_prompt=prompt) 
        await query.message.edit_text(prompt, reply_markup=markup, parse_mode="HTML")
        await state.set_state(target_state)
    else:
        await query.message.answer("Редактирование этого поля пока не реализовано.")

# === ИНДЕКСАЦИЯ БАЗЫ ЗНАНИЙ (ПО ХЕШТЕГАМ) ===
@dp.message(F.chat.id == settings.knowledge_base_channel_id)
@dp.edited_message(F.chat.id == settings.knowledge_base_channel_id)
@dp.channel_post(F.chat.id == settings.knowledge_base_channel_id)
@dp.edited_channel_post(F.chat.id == settings.knowledge_base_channel_id)
async def index_kb_content(message: Message, session: AsyncSession):
    text_content = message.text or message.caption or ""
    if not text_content: return

    # Если в тексте нет #, игнорируем (или сохраняем, но искать не по чему)
    if "#" not in text_content:
        print(f"⚠️ [Index] Post {message.message_id} ignored (no hashtags)")
        return

    try:
        await db_commands.add_or_update_kb_entry(session, message.message_id, text_content)
        await session.commit()
    except Exception as e:
        log.error(f"KB Index Error: {e}")

@dp.callback_query(F.data == "cancel_kb_search")
async def cancel_kb_search_handler(query: CallbackQuery, state: FSMContext, bot: Bot):
    print("🔘 [UI] Нажата кнопка 'Выйти из поиска'")
    
    data = await state.get_data()
    search_msg_id = data.get('search_message_id')
    
    # 1. Сбрасываем состояние
    await state.clear()
    
    # 2. Удаляем сообщение с меню поиска
    if search_msg_id:
        try:
            await bot.delete_message(chat_id=query.message.chat.id, message_id=search_msg_id)
        except Exception as e:
            print(f"⚠️ Не удалось удалить сообщение по ID: {e}")
            # Если не вышло по ID, пробуем удалить то, на которое нажали
            try:
                await query.message.delete()
            except:
                pass
    else:
        try:
            await query.message.delete()
        except:
            pass

    await query.answer("Поиск закрыт")

# === ВРЕМЕННЫЙ ОТЛАДЧИК ===
@dp.channel_post()
@dp.edited_channel_post()
async def debug_channel_id(message: Message):
    print(f"\n📢 ПОЙМАН ПОСТ В КАНАЛЕ!")
    print(f"ID Канала (Real): {message.chat.id}")
    print(f"ID в Config (Settings): {settings.knowledge_base_channel_id}")
    
    if message.chat.id != settings.knowledge_base_channel_id:
        print("❌ ОШИБКА: ID не совпадают! Бот игнорирует этот канал.")
        print(f"👉 Укажите в .env этот ID: {message.chat.id}")
    else:
        print("✅ ID совпадают. Индексация должна работать.")

# === ФУНКЦИЯ ЗАПУСКА main ===
async def main():
    log.info("Starting bot... (FSM DISABLED)")
    engine = create_async_engine(settings.db_url, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_pool = async_sessionmaker(engine, expire_on_commit=False)
    bot = Bot(token=settings.bot_token)
    dp.update.middleware(DbSessionMiddleware(session_pool=session_pool))
    
    scheduler = setup_scheduler(session_pool, bot, settings)
    scheduler.start()

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        if 'scheduler' in locals(): scheduler.shutdown()
        if 'redis_client' in locals(): await redis_client.close()
        await engine.dispose()
        log.info("Bot stopped.")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped by user.")