import datetime
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Enum,
    DateTime,
    ForeignKey,
    Text,
    func,
    Table,
    Boolean
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Базовый класс для всех моделей."""
    pass


# Таблица для связи "многие-ко-многим" между Сообщениями и Тегами
message_tags_association = Table(
    'message_tags',
    Base.metadata,
    Column('message_id', Integer, ForeignKey('messages.id'), primary_key=True),
    Column('tag_id', Integer, ForeignKey('tags.id'), primary_key=True)
)


class User(Base):
    """Модель пользователя системы (клиент, менеджер, руководитель)."""
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    full_name = Column(String(255))
    username = Column(String(100), index=True, nullable=True)
    role = Column(
        Enum('client', 'partner', 'manager', 'supervisor', name='user_role_enum'),
        default='client',
        nullable=False
    )
    status = Column(
        Enum('online', 'offline', 'break', name='user_status_enum'),
        default='offline',
        nullable=False
    )

    def __repr__(self):
        return f"<User(id={self.id}, tg_id={self.telegram_id}, username='{self.username}')>"


class Dialog(Base):
    """Модель диалога между клиентом/партнером и менеджером."""
    __tablename__ = 'dialogs'

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    manager_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    
    manager_chat_id = Column(BigInteger, nullable=False)
    manager_topic_id = Column(BigInteger, nullable=False, index=True)
    partner_chat_id = Column(BigInteger, nullable=True)
    
    status = Column(
        Enum('new', 'active', 'resolved', 'escalated', 'transferred', name='dialog_status_enum'),
        default='new',
        nullable=False,
        index=True
    )
    created_at = Column(DateTime, default=func.now())
    last_client_message_at = Column(DateTime, nullable=True)

    unanswered_since = Column(DateTime, nullable=True) 
    sla_alert_sent = Column(Boolean, default=False)

    # Отношения
    client = relationship("User", foreign_keys=[client_id])
    manager = relationship("User", foreign_keys=[manager_id])
    messages = relationship("Message", back_populates="dialog", cascade="all, delete-orphan")
    notes = relationship("Note", back_populates="dialog", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Dialog(id={self.id}, client_id={self.client_id}, status='{self.status}')>"

    
class City(Base):
    """Модель городов для распределения заявок."""
    __tablename__ = 'cities'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    telegram_chat_id = Column(BigInteger, nullable=False)

    def __repr__(self):
        return f"<City(id={self.id}, name='{self.name}')>"

  

class Message(Base):
    """Модель сообщения в рамках диалога."""
    __tablename__ = 'messages'

    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey('dialogs.id'), nullable=False)
    telegram_message_id = Column(BigInteger, unique=True, nullable=False)
    text = Column(Text, nullable=True)
    # Здесь можно добавить другие поля, например, file_id, content_type и т.д.
    created_at = Column(DateTime, default=func.now())

    # Отношения
    dialog = relationship("Dialog", back_populates="messages")
    tags = relationship("Tag", secondary=message_tags_association, back_populates="messages")

    def __repr__(self):
        return f"<Message(id={self.id}, dialog_id={self.dialog_id})>"


class Note(Base):
    """Модель внутренней заметки менеджера по диалогу."""
    __tablename__ = 'notes'

    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey('dialogs.id'), nullable=False)
    author_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=func.now())

    # Отношения
    dialog = relationship("Dialog", back_populates="notes")
    author = relationship("User")

    def __repr__(self):
        return f"<Note(id={self.id}, dialog_id={self.dialog_id}, author_id={self.author_id})>"


class Tag(Base):
    """Модель тега для категоризации сообщений."""
    __tablename__ = 'tags'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)

    # Отношения
    messages = relationship("Message", secondary=message_tags_association, back_populates="tags")

    def __repr__(self):
        return f"<Tag(id={self.id}, name='{self.name}')>"
    
class Employee(Base):
    """
    Модель для ЧТЕНИЯ данных из существующей таблицы сотрудников.
    ServiceDesk Bot не управляет этой таблицей, только читает из нее.
    """
    __tablename__ = 'employees'
    __table_args__ = (
        {'schema': 'time-tracker-bot', 'extend_existing': True}
    )
    
    # Указываем только те поля, которые нам нужны для работы
    id = Column(Integer, primary_key=True)
    personal_telegram_id = Column(BigInteger, unique=True, nullable=False)
    full_name = Column(String(255))
    position = Column(String(255))
    status = Column(String(50)) # 'online', 'offline', etc.
    work_chat_id = Column(BigInteger, nullable=True)
    
    def __repr__(self):
        return f"<Employee(id={self.id}, name='{self.full_name}', status='{self.status}')>"
    
class MessageLog(Base):
    """Модель для логирования всех сообщений диалога для истории."""
    __tablename__ = 'message_logs'

    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey('dialogs.id'), nullable=False, index=True)

    # ID оригинального сообщения от клиента в его личном чате с ботом
    client_telegram_message_id = Column(BigInteger, nullable=True, index=True)
    # ID "зеркального" сообщения в чате (топике) менеджера
    manager_telegram_message_id = Column(BigInteger, nullable=True, index=True)

    sender_role = Column(String(50), nullable=False)
    sender_name = Column(String(255), nullable=False)

    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=func.now())

    is_deleted = Column(Boolean, default=False, nullable=False)
    is_edited = Column(Boolean, default=False, nullable=False)

    dialog = relationship("Dialog")

    def __repr__(self):
        return f"<MessageLog(id={self.id}, dialog_id={self.dialog_id}, from='{self.sender_role}')>"

class KnowledgeBaseEntry(Base):
    """Модель для хранения ссылок на посты в канале Базы Знаний."""
    __tablename__ = 'knowledge_base'

    id = Column(Integer, primary_key=True)
    message_id = Column(BigInteger, unique=True, nullable=False) # ID сообщения в канале
    text = Column(Text, nullable=True) # Текст для поиска
    keywords = Column(String(255), nullable=True) # Хештеги или ключевые слова
    created_at = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<KB(id={self.message_id})>"
  