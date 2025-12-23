from aiogram.fsm.state import State, StatesGroup

class ManagerFSM(StatesGroup):
    # Состояния для создания заявки
    app_selecting_direction = State()
    app_entering_last_name = State()
    app_entering_first_name = State()
    app_entering_patronymic = State()
    app_entering_datetime = State()
    app_selecting_action = State()
    app_entering_amount_to_get = State()
    app_selecting_currency_to_get = State()
    app_entering_amount_to_give = State()
    app_selecting_currency_to_give = State()
    app_entering_partner_percent = State()
    app_entering_our_percent = State()
    app_confirming_percent = State()
    app_asking_client_id = State()
    app_entering_client_id = State()
    app_confirmation = State()
    app_confirmation = State()
    app_editing_field = State()  
    app_selecting_brand = State()
    app_confirming_percent_change = State() 
    app_selecting_city = State()

    # Состояние для добавления заметки
    adding_note = State()

    # Состояние для передачи диалога
    transferring_dialog = State()
    
    # Состояние для эскалации
    escalating_dialog = State()

    # Состояние для поиска по базе знаний
    searching_kb = State()