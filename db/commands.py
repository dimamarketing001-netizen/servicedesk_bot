"""
Функции для взаимодействия с базой данных (CRUD-операции).
"""
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from aiogram.types import User as AiogramUser
from sqlalchemy import select, func, and_, or_, case
from db.models import User, Dialog, Note, Employee, MessageLog, KnowledgeBaseEntry
import re

async def add_or_update_kb_entry(session: AsyncSession, message_id: int, text: str):
    """
    Сохраняет пост. Извлекает хештеги для поиска.
    """
    # Ищем хештеги (слова с решеткой в начале)
    hashtags = re.findall(r"#\w+", text)
    
    # Если хештегов нет, сохраняем пустую строку, чтобы не было ошибки NoneType
    if hashtags:
        keywords_str = " ".join(hashtags).lower() # "#крипта #btc"
    else:
        keywords_str = ""

    # Проверяем, есть ли запись
    stmt = select(KnowledgeBaseEntry).where(KnowledgeBaseEntry.message_id == message_id)
    result = await session.execute(stmt)
    entry = result.scalar_one_or_none()

    if not entry:
        entry = KnowledgeBaseEntry(
            message_id=message_id, 
            text=text, 
            keywords=keywords_str
        )
        session.add(entry)
    else:
        entry.text = text
        entry.keywords = keywords_str
    
    await session.flush()
    print(f"✅ [DB] Saved Entry {message_id}: Keywords='{keywords_str}'")

async def search_knowledge_base(session: AsyncSession, query: str) -> list[KnowledgeBaseEntry]:
    """
    Поиск. Если ввели 'биток', ищем '%#биток%'. Если '#биток', ищем '%#биток%'.
    """
    clean_query = query.strip().lower()
    
    # Если пользователь не ввел #, добавляем его сами
    if not clean_query.startswith("#"):
        search_term = f"%#{clean_query}%"
    else:
        search_term = f"%{clean_query}%"

    print(f"🔍 [DB] Searching for LIKE '{search_term}'")

    stmt = (
        select(KnowledgeBaseEntry)
        .where(KnowledgeBaseEntry.keywords.like(search_term))
        .limit(5)
    )
    result = await session.execute(stmt)
    found = result.scalars().all()
    print(f"📊 [DB] Found {len(found)} results")
    return found

async def get_live_messages_for_sync(session: AsyncSession) -> list[MessageLog]:
    """
    Выбирает сообщения для проверки.
    Берем ВСЕ активные сообщения за последние 24 часа.
    """
    time_threshold = datetime.now() - timedelta(hours=24)

    stmt = (
        select(MessageLog)
        .join(Dialog, MessageLog.dialog_id == Dialog.id)
        .where(
            MessageLog.is_deleted == False,
            MessageLog.created_at >= time_threshold
        )
        .order_by(MessageLog.created_at.desc()) 
        .limit(500)
    )
    result = await session.execute(stmt)
    return result.scalars().all()

# --- Остальной код без изменений (оставляем старый) ---
async def update_message_log_entry(session: AsyncSession, log_id: int, new_text: str):
    log_entry = await session.get(MessageLog, log_id)
    if log_entry:
        log_entry.text = new_text
        log_entry.is_edited = True
        await session.flush()
        
async def add_message_to_log(
    session: AsyncSession,
    *,
    dialog_id: int,
    sender_role: str,
    sender_name: str,
    text: str,
    client_telegram_message_id: int | None = None,
    manager_telegram_message_id: int | None = None
) -> MessageLog:
    log_entry = MessageLog(
        dialog_id=dialog_id,
        sender_role=sender_role,
        sender_name=sender_name,
        text=text,
        client_telegram_message_id=client_telegram_message_id,
        manager_telegram_message_id=manager_telegram_message_id,
        is_deleted=False,
        is_edited=False
    )
    session.add(log_entry)
    await session.flush()
    return log_entry

async def mark_message_as_deleted(session: AsyncSession, message_id: int):
    stmt = select(MessageLog).where(
        or_(
            MessageLog.client_telegram_message_id == message_id,
            MessageLog.manager_telegram_message_id == message_id
        )
    )
    result = await session.execute(stmt)
    log_entry = result.scalar_one_or_none()
    if log_entry:
        log_entry.is_deleted = True
        await session.flush()
        return True
    return False

async def get_full_history_for_client(session: AsyncSession, client_id: int) -> list[MessageLog]:
    """
    Получает ПОЛНУЮ историю сообщений клиента по ВСЕМ его диалогам.
    Позволяет видеть переписку со всеми предыдущими менеджерами.
    """
    stmt = (
        select(MessageLog)
        .join(Dialog, MessageLog.dialog_id == Dialog.id)
        .where(
            Dialog.client_id == client_id,
            MessageLog.is_deleted == False
        )
        .order_by(MessageLog.created_at.asc()) 
    )
    result = await session.execute(stmt)
    return result.scalars().all()

async def get_or_create_user(session: AsyncSession, aiogram_user: AiogramUser, role: str = 'client') -> User:
    stmt = select(User).where(User.telegram_id == aiogram_user.id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            telegram_id=aiogram_user.id,
            full_name=aiogram_user.full_name,
            username=aiogram_user.username,
            role=role
        )
        session.add(user)
        await session.flush()
    else:
        has_changes = False
        if user.full_name != aiogram_user.full_name:
            user.full_name = aiogram_user.full_name
            has_changes = True
        if user.username != aiogram_user.username:
            user.username = aiogram_user.username
            has_changes = True
        if user.role == 'client' and role == 'manager':
            user.role = 'manager'
            has_changes = True
        if has_changes:
            await session.flush()
    return user

async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> Optional[User]:
    return await session.get(User, telegram_id)

async def set_manager_status(session: AsyncSession, user_id: int, status: str):
    user = await session.get(User, user_id)
    if user and user.role in ('manager', 'supervisor'):
        user.status = status
        await session.flush()

async def find_free_manager(session: AsyncSession, exclude_telegram_id: int | None = None) -> Optional[Employee]:
    """
    Ищет свободного менеджера.
    Логика: берет ВСЕХ онлайн менеджеров, сортирует по нагрузке,
    а затем Python-кодом исключает того, кто передает диалог.
    Это гарантирует, что диалог не вернется к отправителю.
    """
    # 1. Подзапрос: считаем активные диалоги
    subquery = (
        select(Dialog.manager_id, func.count(Dialog.id).label('active_dialogs_count'))
        .join(User, User.id == Dialog.manager_id)
        .where(Dialog.status == 'active')
        .group_by(Dialog.manager_id)
        .subquery()
    )

    # 2. Запрос: все онлайн менеджеры с рабочим чатом
    stmt = (
        select(Employee)
        .outerjoin(User, Employee.personal_telegram_id == User.telegram_id)
        .outerjoin(subquery, User.id == subquery.c.manager_id)
        .where(
            Employee.position == 'Чат менеджер',
            Employee.status == 'online',
            Employee.work_chat_id.isnot(None)
        )
        # Сортировка: от менее загруженных к более загруженным
        .order_by(func.coalesce(subquery.c.active_dialogs_count, 0).asc())
    )
    
    result = await session.execute(stmt)
    candidates = result.scalars().all()

    # 3. Фильтрация на уровне Python (Самая надежная)
    for employee in candidates:
        # Если передали ID для исключения и он совпадает - пропускаем этого сотрудника
        if exclude_telegram_id is not None and employee.personal_telegram_id == exclude_telegram_id:
            continue
        
        # Возвращаем первого подходящего (он будет самым свободным из-за сортировки выше)
        return employee

    return None

async def find_last_dialog_for_client(session: AsyncSession, client_id: int) -> Optional[Dialog]:
    stmt = (
        select(Dialog)
        .where(Dialog.client_id == client_id)
        .order_by(Dialog.created_at.desc())
        .options(joinedload(Dialog.manager))
    )
    result = await session.execute(stmt)
    return result.scalars().first()

async def find_dialog_by_topic(session: AsyncSession, topic_id: int) -> Optional[Dialog]:
    stmt = select(Dialog).where(Dialog.manager_topic_id == topic_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
    
async def get_dialog_by_id(session: AsyncSession, dialog_id: int) -> Optional[Dialog]:
    stmt = (
        select(Dialog)
        .where(Dialog.id == dialog_id)
        .options(joinedload(Dialog.client))
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

async def create_dialog(session: AsyncSession, client_id: int, manager_id: int, manager_chat_id: int, topic_id: int) -> Dialog:
    new_dialog = Dialog(
        client_id=client_id,
        manager_id=manager_id,
        manager_chat_id=manager_chat_id,
        manager_topic_id=topic_id,
        status='active'
    )
    session.add(new_dialog)
    await session.flush()
    return new_dialog

async def update_dialog_status(session: AsyncSession, dialog_id: int, new_status: str):
    dialog = await session.get(Dialog, dialog_id)
    if dialog:
        dialog.status = new_status
        await session.flush()

async def update_dialog_last_client_message_time(session: AsyncSession, dialog_id: int, timestamp: datetime):
    dialog = await session.get(Dialog, dialog_id)
    if dialog:
        dialog.last_client_message_at = timestamp
        await session.flush()

async def get_log_entry_by_client_msg_id(session: AsyncSession, client_msg_id: int) -> Optional[MessageLog]:
    stmt = select(MessageLog).where(MessageLog.client_telegram_message_id == client_msg_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

async def get_log_entry_by_manager_msg_id(session: AsyncSession, manager_msg_id: int) -> Optional[MessageLog]:
    stmt = select(MessageLog).where(MessageLog.manager_telegram_message_id == manager_msg_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

async def update_log_text(session: AsyncSession, log_entry: MessageLog, new_text: str):
    log_entry.text = new_text
    log_entry.is_edited = True
    await session.flush()

async def create_note(session: AsyncSession, dialog_id: int, author_id: int, text: str) -> Note:
    new_note = Note(
        dialog_id=dialog_id,
        author_id=author_id,
        text=text
    )
    session.add(new_note)
    await session.flush()
    return new_note

async def get_notes_by_dialog(session: AsyncSession, dialog_id: int) -> list[Note]:
    """Получает список всех заметок по конкретному диалогу."""
    stmt = (
        select(Note)
        .where(Note.dialog_id == dialog_id)
        .order_by(Note.created_at.asc())
        .options(joinedload(Note.author))
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_all_notes_for_client(session: AsyncSession, dialog_id: int) -> list[Note]:
    """
    Получает ВСЕ заметки по клиенту, зная ID любого его диалога.
    Работает через JOIN: Note -> Dialog -> Client_ID.
    """
    # 1. Узнаем ID клиента через текущий диалог
    current_dialog = await session.get(Dialog, dialog_id)
    if not current_dialog:
        return []
    
    client_id = current_dialog.client_id

    # 2. Ищем все заметки, которые привязаны к ЛЮБОМУ диалогу этого клиента
    stmt = (
        select(Note)
        .join(Dialog, Note.dialog_id == Dialog.id)
        .where(Dialog.client_id == client_id)
        .order_by(Note.created_at.asc())
        .options(joinedload(Note.author))
    )
    result = await session.execute(stmt)
    return result.scalars().all()