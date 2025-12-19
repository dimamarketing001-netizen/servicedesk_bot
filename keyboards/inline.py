"""
Модуль для создания и управления Inline-клавиатурами.

Здесь определяются фабрики CallbackData для структурирования данных
и функции-конструкторы для генерации клавиатур.
"""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton


# --- Фабрики CallbackData ---

class ManagerCallback(CallbackData, prefix="mgr"):
    """
    Фабрика для колбэков, связанных с действиями менеджера в "пульте управления".
    - 'action': Конкретное действие (например, 'resolve', 'add_note').
    - 'dialog_id': ID диалога, к которому относится действие.
    """
    action: str
    dialog_id: int


class CsatCallback(CallbackData, prefix="csat"):
    """
    Фабрика для колбэков опроса удовлетворенности клиента (CSAT).
    - 'dialog_id': ID диалога, который оценивается.
    - 'rating': Оценка, поставленная клиентом (например, от 1 до 5).
    """
    dialog_id: int
    rating: int


# --- Функции-конструкторы клавиатур ---

def get_manager_control_panel(dialog_id: int) -> InlineKeyboardMarkup:
    """
    Создает "пульт управления" для менеджера в теме диалога.

    :param dialog_id: ID диалога для включения в callback_data.
    :return: Объект InlineKeyboardMarkup.
    """
    builder = InlineKeyboardBuilder()

    # Добавляем кнопки в соответствии с ТЗ
    builder.button(
        text="✅ Решено",
        callback_data=ManagerCallback(action="resolve", dialog_id=dialog_id)
    )
    builder.button(
        text="📝 Создать Заявку",
        callback_data=ManagerCallback(action="create_app", dialog_id=dialog_id)
    )
    builder.button(
        text="📌 Заметка",
        callback_data=ManagerCallback(action="add_note", dialog_id=dialog_id)
    )
    builder.button(
        text="🔄 Передать",
        callback_data=ManagerCallback(action="transfer", dialog_id=dialog_id)
    )
    builder.button(
        text="📚 База Знаний",
        callback_data=ManagerCallback(action="kb_search", dialog_id=dialog_id)
    )
    builder.button(
        text="🆘 Эскалация",
        callback_data=ManagerCallback(action="escalate", dialog_id=dialog_id)
    )

    # Устанавливаем расположение кнопок: по 2 в ряд
    builder.adjust(2)

    return builder.as_markup()


def get_csat_keyboard(dialog_id: int) -> InlineKeyboardMarkup:
    """
    Создает клавиатуру для оценки качества обслуживания клиентом.

    :param dialog_id: ID диалога, который оценивается.
    :return: Объект InlineKeyboardMarkup.
    """
    builder = InlineKeyboardBuilder()

    ratings = {
        "😞 Плохо": 1,
        "😐 Нормально": 3,
        "😄 Отлично": 5
    }

    for text, rating in ratings.items():
        builder.button(
            text=text,
            callback_data=CsatCallback(dialog_id=dialog_id, rating=rating)
        )
    
    # Располагаем кнопки в один ряд
    builder.adjust(3)
    
    return builder.as_markup()


def get_confirmation_keyboard(action: str, target_id: int) -> InlineKeyboardMarkup:
    """
    Создает универсальную клавиатуру подтверждения (Да/Нет).
    Пример использования: подтверждение передачи диалога.

    :param action: Строка, идентифицирующая действие (например, 'confirm_transfer').
    :param target_id: ID объекта, к которому применяется действие.
    :return: Объект InlineKeyboardMarkup.
    """
    # Для этого можно создать отдельный CallbackData или использовать существующий
    # с дополнительным полем. Для простоты пока оставим как пример.
    # class ConfirmCallback(CallbackData, prefix="confirm"):
    #     decision: bool
    #     action: str
    #     target_id: int
    
    builder = InlineKeyboardBuilder()
    
    builder.button(text="✅ Да, подтвердить", callback_data=f"confirm:{action}:{target_id}:yes")
    builder.button(text="❌ Нет, отмена", callback_data=f"confirm:{action}:{target_id}:no")
    
    return builder.as_markup()

def get_app_step_keyboard(extra_buttons: dict = None) -> InlineKeyboardMarkup:
    """
    Генерирует клавиатуру для шага создания заявки.
    Всегда добавляет кнопки 'Пауза' и 'Отмена'.
    
    :param extra_buttons: Словарь {'Текст кнопки': 'callback_data'} для выбора (например, валюты).
    """
    builder = InlineKeyboardBuilder()

    # 1. Кнопки выбора (если есть)
    if extra_buttons:
        for text, data in extra_buttons.items():
            builder.button(text=text, callback_data=data)
        
        # Адаптация сетки: если кнопок много (валюты) - по 2, иначе по 1
        if len(extra_buttons) > 4:
            builder.adjust(2)
        else:
            builder.adjust(1)

    # 2. Управляющие кнопки (всегда отдельным рядом внизу)
    control_row = [
        InlineKeyboardButton(text="⏸ Пауза (Чат)", callback_data="app_pause"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="app_cancel")
    ]
    
    # Добавляем ряд управления. 
    # Если были кнопки выбора, row() добавит новый ряд под ними.
    builder.row(*control_row)

    return builder.as_markup()