import asyncio
import os
import logging
import csv
import io
from typing import Optional, List, Dict
from datetime import datetime, timedelta
from enum import Enum

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (InlineKeyboardMarkup, InlineKeyboardButton, 
                           ReplyKeyboardRemove, BufferedInputFile)
from sqlalchemy import (select, update, delete, func, Boolean, Integer, 
                        String, Float, DateTime, Text, ForeignKey, and_)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

import FunPayAPI
from FunPayAPI.updater.events import (NewMessageEvent, NewOrderEvent, 
                                       LastChatMessageChangedEvent)
import threading

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Переменные окружения
BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("Необходимо установить переменные окружения BOT_TOKEN и DATABASE_URL")

# SQLAlchemy setup
async_engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(async_engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ==================== DATABASE MODELS ====================

class UserSettings(Base):
    __tablename__ = 'user_settings'
    
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, unique=True)
    proxy: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    golden_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    notify_new_orders: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_new_messages: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_order_delivery: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_lot_raise: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_delivery_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_response_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_raise_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_greeting_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    greeting_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True, 
                                                          default="Привет! Чем могу помочь?")
    watermark: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, 
                                                  onupdate=datetime.utcnow)


class Product(Base):
    """Товары для автовыдачи"""
    __tablename__ = 'products'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user_settings.user_id'))
    lot_title: Mapped[str] = mapped_column(String)  # Название лота для привязки
    product_text: Mapped[str] = mapped_column(Text)  # Текст товара (может содержать переменные)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AutoResponse(Base):
    """Команды автоответчика"""
    __tablename__ = 'auto_responses'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user_settings.user_id'))
    command: Mapped[str] = mapped_column(String)  # Триггерная фраза
    response: Mapped[str] = mapped_column(Text)  # Ответ
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Blacklist(Base):
    """Чёрный список пользователей"""
    __tablename__ = 'blacklist'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user_settings.user_id'))
    username: Mapped[str] = mapped_column(String)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Order(Base):
    """История заказов"""
    __tablename__ = 'orders'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user_settings.user_id'))
    order_id: Mapped[str] = mapped_column(String)
    buyer_username: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String, default="RUB")
    status: Mapped[str] = mapped_column(String, default="paid")
    auto_delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Lot(Base):
    """Лоты пользователя"""
    __tablename__ = 'lots'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user_settings.user_id'))
    lot_id: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_raised: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, 
                                                  onupdate=datetime.utcnow)


class Review(Base):
    """Отзывы"""
    __tablename__ = 'reviews'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user_settings.user_id'))
    order_id: Mapped[str] = mapped_column(String)
    stars: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MessageHistory(Base):
    """История сообщений"""
    __tablename__ = 'message_history'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user_settings.user_id'))
    chat_id: Mapped[int] = mapped_column(Integer)
    chat_name: Mapped[str] = mapped_column(String)
    sender: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    is_incoming: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db():
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ==================== FSM STATES ====================

class ProxyState(StatesGroup):
    waiting_for_proxy = State()


class GoldenKeyState(StatesGroup):
    waiting_for_key = State()


class ReplyState(StatesGroup):
    waiting_for_reply = State()


class ProductState(StatesGroup):
    waiting_for_lot_title = State()
    waiting_for_product_text = State()
    waiting_for_quantity = State()


class AutoResponseState(StatesGroup):
    waiting_for_command = State()
    waiting_for_response = State()


class BlacklistState(StatesGroup):
    waiting_for_username = State()
    waiting_for_reason = State()


class GreetingState(StatesGroup):
    waiting_for_text = State()


class WatermarkState(StatesGroup):
    waiting_for_text = State()


class ReviewReplyState(StatesGroup):
    waiting_for_reply = State()


router = Router()


# ==================== KEYBOARDS ====================

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Баланс", callback_data="check_balance"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats")
        ],
        [
            InlineKeyboardButton(text="🛍️ Мои лоты", callback_data="my_lots"),
            InlineKeyboardButton(text="📦 Заказы", callback_data="my_orders")
        ],
        [
            InlineKeyboardButton(text="⭐ Отзывы", callback_data="my_reviews"),
            InlineKeyboardButton(text="💬 Чаты", callback_data="my_chats")
        ],
        [InlineKeyboardButton(text="🤖 Автовыдача", callback_data="auto_delivery_menu")],
        [InlineKeyboardButton(text="💭 Автоответчик", callback_data="auto_response_menu")],
        [InlineKeyboardButton(text="⬆️ Автоподнятие", callback_data="auto_raise_menu")],
        [InlineKeyboardButton(text="🚷 Чёрный список", callback_data="blacklist_menu")],
        [InlineKeyboardButton(text="💾 Экспорт данных", callback_data="export_data")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu")],
        [InlineKeyboardButton(text="🔧 Настройка прокси", callback_data="set_proxy")],
        [InlineKeyboardButton(text="🔑 Ввести Golden Key", callback_data="set_golden_key")]
    ])


def get_settings_keyboard(user_settings: UserSettings):
    orders_status = "✅" if user_settings.notify_new_orders else "❌"
    messages_status = "✅" if user_settings.notify_new_messages else "❌"
    delivery_status = "✅" if user_settings.notify_order_delivery else "❌"
    raise_status = "✅" if user_settings.notify_lot_raise else "❌"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📦 Уведомления о заказах {orders_status}", 
            callback_data="toggle_orders"
        )],
        [InlineKeyboardButton(
            text=f"💬 Уведомления о сообщениях {messages_status}", 
            callback_data="toggle_messages"
        )],
        [InlineKeyboardButton(
            text=f"✅ Уведомления о выдаче {delivery_status}", 
            callback_data="toggle_delivery"
        )],
        [InlineKeyboardButton(
            text=f"⬆️ Уведомления о поднятии {raise_status}", 
            callback_data="toggle_raise"
        )],
        [InlineKeyboardButton(text="👋 Приветственное сообщение", callback_data="set_greeting")],
        [InlineKeyboardButton(text="💧 Водяной знак", callback_data="set_watermark")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])


def get_reply_keyboard(chat_id: int, chat_name: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💬 Ответить",
            callback_data=f"reply_to_{chat_id}_{chat_name}"
        )]
    ])


def get_lots_keyboard(lots: List[dict]):
    buttons = []
    for lot in lots[:10]:  # Показываем максимум 10 лотов
        status = "🟢" if lot.get('is_active') else "🔴"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {lot['title'][:30]}... ({lot['price']} {lot['currency']})",
            callback_data=f"lot_info_{lot['lot_id']}"
        )])
    
    buttons.append([InlineKeyboardButton(text="🔄 Обновить лоты", callback_data="refresh_lots")])
    buttons.append([InlineKeyboardButton(text="⬆️ Поднять все лоты", callback_data="raise_all_lots")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_orders_keyboard(orders: List[Order]):
    buttons = []
    for order in orders[:10]:
        status_icon = "✅" if order.auto_delivered else "⏳"
        buttons.append([InlineKeyboardButton(
            text=f"{status_icon} #{order.order_id} - {order.buyer_username} ({order.price} {order.currency})",
            callback_data=f"order_info_{order.id}"
        )])
    
    buttons.append([
        InlineKeyboardButton(text="📅 Сегодня", callback_data="orders_today"),
        InlineKeyboardButton(text="📅 Неделя", callback_data="orders_week"),
        InlineKeyboardButton(text="📅 Месяц", callback_data="orders_month")
    ])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_reviews_keyboard(reviews: List[Review]):
    buttons = []
    for review in reviews[:10]:
        stars = "⭐" * review.stars
        replied = "✅" if review.reply else "❌"
        buttons.append([InlineKeyboardButton(
            text=f"{stars} #{review.order_id} (Ответ: {replied})",
            callback_data=f"review_info_{review.id}"
        )])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ==================== UTILITY FUNCTIONS ====================

def format_text_with_variables(text: str, variables: dict) -> str:
    """Форматирует текст с переменными"""
    for key, value in variables.items():
        text = text.replace(f"{{{key}}}", str(value))
    return text


def parse_proxy(proxy_str: str) -> dict:
    """Парсит строку прокси"""
    try:
        if "://" in proxy_str:
            protocol, rest = proxy_str.split("://", 1)
        else:
            protocol = "http"
            rest = proxy_str
        
        if "@" in rest:
            auth, host_port = rest.split("@", 1)
            user, password = auth.split(":", 1)
        else:
            user, password = None, None
            host_port = rest
        
        host, port = host_port.split(":", 1)
        return {
            "protocol": protocol,
            "host": host,
            "port": int(port),
            "user": user,
            "password": password
        }
    except:
        return None


# ==================== HANDLERS ====================

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if not user_settings:
            user_settings = UserSettings(user_id=user_id)
            session.add(user_settings)
            await session.commit()
            await session.refresh(user_settings)
    
    await message.answer(
        "👋 **Добро пожаловать в FunPay Bot!**\n\n"
        "🔧 Для начала работы необходимо настроить:\n"
        "1. 🔑 Golden Key аккаунта FunPay\n"
        "2. 🔧 Прокси (опционально)\n\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


@router.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    await message.answer("Главное меню:", reply_markup=get_main_keyboard())


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = """
🤖 **FunPay Bot - Справка**

**Основные функции:**
• 💰 Проверка баланса
• 📊 Статистика заработка
• 🛍️ Управление лотами
• 📦 История заказов
• ⭐ Работа с отзывами
• 💬 Управление чатами

**Автоматизация:**
• 🤖 Автовыдача товаров
• 💭 Автоответчик
• ⬆️ Автоподнятие лотов
• 👋 Автоприветствие

**Дополнительно:**
• 🚷 Чёрный список
• 💾 Экспорт данных
• 💧 Водяной знак
• 🔧 Настройка прокси

**Команды:**
/start - Главное меню
/menu - Открыть меню
/help - Эта справка
/cancel - Отменить действие
"""
    await message.answer(help_text, parse_mode="Markdown")


# ==================== SETTINGS HANDLERS ====================

@router.callback_query(F.data == "set_proxy")
async def callback_set_proxy(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🔧 Введите прокси в формате:\n"
        "`http://user:pass@ip:port`\n"
        "`socks5://user:pass@ip:port`\n\n"
        "Или отправьте /skip, чтобы пропустить",
        parse_mode="Markdown"
    )
    await state.set_state(ProxyState.waiting_for_proxy)


@router.message(ProxyState.waiting_for_proxy)
async def process_proxy(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    if message.text.lower() == "/skip":
        proxy = None
    else:
        proxy = message.text
        
        # Проверяем валидность прокси
        if not parse_proxy(proxy):
            await message.answer(
                "❌ Неверный формат прокси!\n"
                "Используйте: `http://user:pass@ip:port`",
                parse_mode="Markdown"
            )
            return
    
    async with async_session() as session:
        await session.execute(
            update(UserSettings)
            .where(UserSettings.user_id == user_id)
            .values(proxy=proxy)
        )
        await session.commit()
    
    await state.clear()
    await message.answer(
        f"✅ Прокси {'установлен' if proxy else 'пропущен'}!",
        reply_markup=get_main_keyboard()
    )


@router.callback_query(F.data == "set_golden_key")
async def callback_set_golden_key(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🔑 Введите Golden Key вашего аккаунта FunPay:\n\n"
        "**Как получить Golden Key:**\n"
        "1. Зайдите на funpay.com\n"
        "2. Откройте DevTools (F12)\n"
        "3. Перейдите в Application → Cookies\n"
        "4. Скопируйте значение `golden_key`",
        parse_mode="Markdown"
    )
    await state.set_state(GoldenKeyState.waiting_for_key)


@router.message(GoldenKeyState.waiting_for_key)
async def process_golden_key(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    golden_key = message.text.strip()
    
    if len(golden_key) < 20:
        await message.answer("❌ Неверный формат Golden Key!")
        return
    
    async with async_session() as session:
        await session.execute(
            update(UserSettings)
            .where(UserSettings.user_id == user_id)
            .values(golden_key=golden_key, is_active=False)
        )
        await session.commit()
    
    await state.clear()
    await message.answer(
        "✅ Golden Key установлен!\n\n"
        "⚠️ Перезапустите бота командой /start для активации",
        reply_markup=get_main_keyboard()
    )


# ==================== BALANCE & STATS ====================

@router.callback_query(F.data == "check_balance")
async def callback_check_balance(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if not user_settings or not user_settings.golden_key:
            await callback.answer("❌ Сначала настройте Golden Key!", show_alert=True)
            return
        
        await callback.answer("⏳ Получение баланса...")
        
        try:
            account = FunPayAPI.Account(user_settings.golden_key, proxy=user_settings.proxy)
            account.get()
            
            balance = account.balance
            
            text = f"💰 **Ваш баланс:**\n\n"
            for currency, amount in balance.items():
                text += f"• {currency}: **{amount}**\n"
            
            await callback.message.answer(text, parse_mode="Markdown")
            
        except Exception as e:
            logger.error(f"Ошибка при получении баланса: {e}")
            await callback.message.answer(f"❌ Ошибка: {str(e)}")


@router.callback_query(F.data == "show_stats")
async def callback_show_stats(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        # Получаем настройки
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if not user_settings or not user_settings.golden_key:
            await callback.answer("❌ Сначала настройте Golden Key!", show_alert=True)
            return
        
        # Считаем статистику
        today = datetime.utcnow().date()
        week_ago = today - timedelta(days=7)
        month_ago = today - timedelta(days=30)
        
        # Заказы за сегодня
        orders_today = await session.execute(
            select(func.count(Order.id), func.sum(Order.price))
            .where(
                Order.user_id == user_id,
                func.date(Order.created_at) == today
            )
        )
        orders_today_count, orders_today_sum = orders_today.one()
        
        # Заказы за неделю
        orders_week = await session.execute(
            select(func.count(Order.id), func.sum(Order.price))
            .where(
                Order.user_id == user_id,
                func.date(Order.created_at) >= week_ago
            )
        )
        orders_week_count, orders_week_sum = orders_week.one()
        
        # Заказы за месяц
        orders_month = await session.execute(
            select(func.count(Order.id), func.sum(Order.price))
            .where(
                Order.user_id == user_id,
                func.date(Order.created_at) >= month_ago
            )
        )
        orders_month_count, orders_month_sum = orders_month.one()
        
        # Всего заказов
        orders_total = await session.execute(
            select(func.count(Order.id), func.sum(Order.price))
            .where(Order.user_id == user_id)
        )
        orders_total_count, orders_total_sum = orders_total.one()
        
        text = f"📊 **Статистика аккаунта:**\n\n"
        text += f"**💰 Заработок:**\n"
        text += f"• Сегодня: **{orders_today_sum or 0} RUB** ({orders_today_count or 0} заказов)\n"
        text += f"• Неделя: **{orders_week_sum or 0} RUB** ({orders_week_count or 0} заказов)\n"
        text += f"• Месяц: **{orders_month_sum or 0} RUB** ({orders_month_count or 0} заказов)\n"
        text += f"• Всего: **{orders_total_sum or 0} RUB** ({orders_total_count or 0} заказов)\n\n"
        
        text += f"**⚙️ Настройки:**\n"
        text += f"• 🤖 Автовыдача: {'✅' if user_settings.auto_delivery_enabled else '❌'}\n"
        text += f"• 💭 Автоответчик: {'✅' if user_settings.auto_response_enabled else '❌'}\n"
        text += f"• ⬆️ Автоподнятие: {'✅' if user_settings.auto_raise_enabled else '❌'}\n"
        
        await callback.message.answer(text, parse_mode="Markdown")


# ==================== LOTS MANAGEMENT ====================

@router.callback_query(F.data == "my_lots")
async def callback_my_lots(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if not user_settings or not user_settings.golden_key:
            await callback.answer("❌ Сначала настройте Golden Key!", show_alert=True)
            return
        
        await callback.answer("⏳ Загрузка лотов...")
        
        try:
            account = FunPayAPI.Account(user_settings.golden_key, proxy=user_settings.proxy)
            account.get()
            
            user_info = account.get_user(account.id)
            lots = user_info.get_lots()
            
            if not lots:
                await callback.message.answer(
                    "📭 У вас нет активных лотов",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
                    ])
                )
                return
            
            lots_data = []
            for lot in lots[:10]:
                lots_data.append({
                    'lot_id': lot.id,
                    'title': lot.title,
                    'price': lot.price,
                    'currency': lot.currency,
                    'is_active': lot.is_active
                })
            
            text = f"🛍️ **Ваши лоты ({len(lots)} всего):**\n"
            await callback.message.answer(
                text,
                reply_markup=get_lots_keyboard(lots_data),
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"Ошибка при получении лотов: {e}")
            await callback.message.answer(f"❌ Ошибка: {str(e)}")


@router.callback_query(F.data == "raise_all_lots")
async def callback_raise_all_lots(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if not user_settings or not user_settings.golden_key:
            await callback.answer("❌ Сначала настройте Golden Key!", show_alert=True)
            return
        
        await callback.answer("⏳ Поднятие лотов...")
        
        try:
            account = FunPayAPI.Account(user_settings.golden_key, proxy=user_settings.proxy)
            account.get()
            
            user_info = account.get_user(account.id)
            categories = set([lot.category_id for lot in user_info.get_lots()])
            
            raised_count = 0
            for category_id in categories:
                try:
                    account.raise_lots(category_id)
                    raised_count += 1
                    await asyncio.sleep(2)  # Задержка между поднятиями
                except Exception as e:
                    logger.warning(f"Не удалось поднять лоты категории {category_id}: {e}")
            
            await callback.message.answer(
                f"✅ Поднято лотов в {raised_count} категориях!",
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"Ошибка при поднятии лотов: {e}")
            await callback.message.answer(f"❌ Ошибка: {str(e)}")


# ==================== ORDERS ====================

@router.callback_query(F.data == "my_orders")
async def callback_my_orders(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
            .limit(10)
        )
        orders = result.scalars().all()
        
        if not orders:
            await callback.message.answer(
                "📭 У вас пока нет заказов",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
                ])
            )
            return
        
        text = f"📦 **Ваши последние заказы:**\n"
        await callback.message.answer(
            text,
            reply_markup=get_orders_keyboard(orders),
            parse_mode="Markdown"
        )


# ==================== REVIEWS ====================

@router.callback_query(F.data == "my_reviews")
async def callback_my_reviews(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(Review)
            .where(Review.user_id == user_id)
            .order_by(Review.created_at.desc())
            .limit(10)
        )
        reviews = result.scalars().all()
        
        if not reviews:
            await callback.message.answer(
                "⭐ У вас пока нет отзывов",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
                ])
            )
            return
        
        text = f"⭐ **Ваши последние отзывы:**\n"
        await callback.message.answer(
            text,
            reply_markup=get_reviews_keyboard(reviews),
            parse_mode="Markdown"
        )


@router.callback_query(F.data.startswith("review_info_"))
async def callback_review_info(callback: types.CallbackQuery, state: FSMContext):
    review_id = int(callback.data.split("_")[2])
    
    async with async_session() as session:
        result = await session.execute(
            select(Review).where(Review.id == review_id)
        )
        review = result.scalar_one_or_none()
        
        if not review:
            await callback.answer("❌ Отзыв не найден!", show_alert=True)
            return
        
        stars = "⭐" * review.stars
        text = f"{stars} **Отзыв на заказ #{review.order_id}**\n\n"
        text += f"**Текст:** {review.text}\n\n"
        
        if review.reply:
            text += f"**Ваш ответ:** {review.reply}\n"
        
        buttons = []
        if not review.reply:
            buttons.append([InlineKeyboardButton(
                text="💬 Ответить на отзыв",
                callback_data=f"reply_review_{review.id}"
            )])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="my_reviews")])
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="Markdown"
        )


@router.callback_query(F.data.startswith("reply_review_"))
async def callback_start_review_reply(callback: types.CallbackQuery, state: FSMContext):
    review_id = int(callback.data.split("_")[2])
    
    await state.update_data(review_id=review_id)
    await state.set_state(ReviewReplyState.waiting_for_reply)
    
    await callback.message.answer(
        "💬 Отправьте текст ответа на отзыв:\n\n"
        "Для отмены отправьте /cancel"
    )


@router.message(ReviewReplyState.waiting_for_reply)
async def process_review_reply(message: types.Message, state: FSMContext):
    if message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Ответ отменен")
        return
    
    user_id = message.from_user.id
    data = await state.get_data()
    review_id = data.get('review_id')
    reply_text = message.text
    
    async with async_session() as session:
        result = await session.execute(
            select(Review).where(Review.id == review_id)
        )
        review = result.scalar_one_or_none()
        
        if not review:
            await message.answer("❌ Отзыв не найден!")
            await state.clear()
            return
        
        # Получаем настройки
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if not user_settings or not user_settings.golden_key:
            await message.answer("❌ Сначала настройте Golden Key!")
            await state.clear()
            return
        
        try:
            account = FunPayAPI.Account(user_settings.golden_key, proxy=user_settings.proxy)
            account.get()
            
            # Отправляем ответ на отзыв
            account.send_review(review.order_id, reply_text)
            
            # Сохраняем ответ в БД
            await session.execute(
                update(Review)
                .where(Review.id == review_id)
                .values(reply=reply_text)
            )
            await session.commit()
            
            await message.answer(f"✅ Ответ на отзыв отправлен!")
            
        except Exception as e:
            logger.error(f"Ошибка при отправке ответа на отзыв: {e}")
            await message.answer(f"❌ Ошибка: {str(e)}")
    
    await state.clear()


# ==================== AUTO DELIVERY ====================

@router.callback_query(F.data == "auto_delivery_menu")
async def callback_auto_delivery_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if not user_settings:
            await callback.answer("❌ Ошибка!", show_alert=True)
            return
        
        # Получаем товары
        products_result = await session.execute(
            select(Product).where(
                Product.user_id == user_id,
                Product.is_active == True
            )
        )
        products = products_result.scalars().all()
        
        status = "✅ Включена" if user_settings.auto_delivery_enabled else "❌ Выключена"
        text = f"🤖 **Автовыдача товаров**\n\n"
        text += f"**Статус:** {status}\n"
        text += f"**Товаров:** {len(products)}\n\n"
        
        if products:
            text += "**Список товаров:**\n"
            for product in products[:5]:
                text += f"• {product.lot_title[:30]}... (x{product.quantity})\n"
        
        buttons = [
            [InlineKeyboardButton(
                text=f"{'Выключить' if user_settings.auto_delivery_enabled else 'Включить'} автовыдачу",
                callback_data="toggle_auto_delivery"
            )],
            [InlineKeyboardButton(text="➕ Добавить товар", callback_data="add_product")],
            [InlineKeyboardButton(text="📋 Список товаров", callback_data="list_products")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
        ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="Markdown"
        )


@router.callback_query(F.data == "toggle_auto_delivery")
async def callback_toggle_auto_delivery(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if user_settings:
            user_settings.auto_delivery_enabled = not user_settings.auto_delivery_enabled
            await session.commit()
            
            status = "включена" if user_settings.auto_delivery_enabled else "выключена"
            await callback.answer(f"✅ Автовыдача {status}!", show_alert=True)
            await callback_auto_delivery_menu(callback)


@router.callback_query(F.data == "add_product")
async def callback_add_product(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "➕ **Добавление товара**\n\n"
        "Введите название лота (точно как на FunPay):",
        parse_mode="Markdown"
    )
    await state.set_state(ProductState.waiting_for_lot_title)


@router.message(ProductState.waiting_for_lot_title)
async def process_product_lot_title(message: types.Message, state: FSMContext):
    await state.update_data(lot_title=message.text)
    await state.set_state(ProductState.waiting_for_product_text)
    
    await message.answer(
        "Введите текст товара (будет отправлен покупателю):\n\n"
        "**Доступные переменные:**\n"
        "• `{buyer}` - имя покупателя\n"
        "• `{order_id}` - ID заказа\n"
        "• `{lot_name}` - название лота\n",
        parse_mode="Markdown"
    )


@router.message(ProductState.waiting_for_product_text)
async def process_product_text(message: types.Message, state: FSMContext):
    await state.update_data(product_text=message.text)
    await state.set_state(ProductState.waiting_for_quantity)
    
    await message.answer("Введите количество товара (число):")


@router.message(ProductState.waiting_for_quantity)
async def process_product_quantity(message: types.Message, state: FSMContext):
    try:
        quantity = int(message.text)
    except ValueError:
        await message.answer("❌ Введите корректное число!")
        return
    
    user_id = message.from_user.id
    data = await state.get_data()
    
    async with async_session() as session:
        product = Product(
            user_id=user_id,
            lot_title=data['lot_title'],
            product_text=data['product_text'],
            quantity=quantity
        )
        session.add(product)
        await session.commit()
    
    await state.clear()
    await message.answer(
        f"✅ Товар добавлен!\n"
        f"**Лот:** {data['lot_title']}\n"
        f"**Количество:** {quantity}",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


# ==================== AUTO RESPONSE ====================

@router.callback_query(F.data == "auto_response_menu")
async def callback_auto_response_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if not user_settings:
            await callback.answer("❌ Ошибка!", show_alert=True)
            return
        
        # Получаем команды
        commands_result = await session.execute(
            select(AutoResponse).where(
                AutoResponse.user_id == user_id,
                AutoResponse.is_active == True
            )
        )
        commands = commands_result.scalars().all()
        
        status = "✅ Включен" if user_settings.auto_response_enabled else "❌ Выключен"
        text = f"💭 **Автоответчик**\n\n"
        text += f"**Статус:** {status}\n"
        text += f"**Команд:** {len(commands)}\n\n"
        
        if commands:
            text += "**Список команд:**\n"
            for cmd in commands[:5]:
                text += f"• `{cmd.command}` → {cmd.response[:20]}...\n"
        
        buttons = [
            [InlineKeyboardButton(
                text=f"{'Выключить' if user_settings.auto_response_enabled else 'Включить'} автоответчик",
                callback_data="toggle_auto_response"
            )],
            [InlineKeyboardButton(text="➕ Добавить команду", callback_data="add_auto_response")],
            [InlineKeyboardButton(text="📋 Список команд", callback_data="list_auto_responses")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
        ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="Markdown"
        )


@router.callback_query(F.data == "toggle_auto_response")
async def callback_toggle_auto_response(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if user_settings:
            user_settings.auto_response_enabled = not user_settings.auto_response_enabled
            await session.commit()
            
            status = "включен" if user_settings.auto_response_enabled else "выключен"
            await callback.answer(f"✅ Автоответчик {status}!", show_alert=True)
            await callback_auto_response_menu(callback)


@router.callback_query(F.data == "add_auto_response")
async def callback_add_auto_response(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "➕ **Добавление команды**\n\n"
        "Введите триггерную фразу (команду):"
    )
    await state.set_state(AutoResponseState.waiting_for_command)


@router.message(AutoResponseState.waiting_for_command)
async def process_auto_response_command(message: types.Message, state: FSMContext):
    await state.update_data(command=message.text.lower())
    await state.set_state(AutoResponseState.waiting_for_response)
    
    await message.answer(
        "Введите ответ на эту команду:\n\n"
        "**Доступные переменные:**\n"
        "• `{user}` - имя пользователя\n"
        "• `{chat}` - название чата\n",
        parse_mode="Markdown"
    )


@router.message(AutoResponseState.waiting_for_response)
async def process_auto_response_response(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    
    async with async_session() as session:
        auto_response = AutoResponse(
            user_id=user_id,
            command=data['command'],
            response=message.text
        )
        session.add(auto_response)
        await session.commit()
    
    await state.clear()
    await message.answer(
        f"✅ Команда добавлена!\n"
        f"**Команда:** `{data['command']}`\n"
        f"**Ответ:** {message.text[:50]}...",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


# ==================== BLACKLIST ====================

@router.callback_query(F.data == "blacklist_menu")
async def callback_blacklist_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(Blacklist).where(Blacklist.user_id == user_id)
        )
        blacklist = result.scalars().all()
        
        text = f"🚷 **Чёрный список**\n\n"
        text += f"**Пользователей:** {len(blacklist)}\n\n"
        
        if blacklist:
            text += "**Список:**\n"
            for user in blacklist[:10]:
                reason = user.reason or "Не указана"
                text += f"• `{user.username}` - {reason[:30]}\n"
        
        buttons = [
            [InlineKeyboardButton(text="➕ Добавить в ЧС", callback_data="add_to_blacklist")],
            [InlineKeyboardButton(text="🗑️ Очистить ЧС", callback_data="clear_blacklist")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
        ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="Markdown"
        )


@router.callback_query(F.data == "add_to_blacklist")
async def callback_add_to_blacklist(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "➕ **Добавление в чёрный список**\n\n"
        "Введите имя пользователя:"
    )
    await state.set_state(BlacklistState.waiting_for_username)


@router.message(BlacklistState.waiting_for_username)
async def process_blacklist_username(message: types.Message, state: FSMContext):
    await state.update_data(username=message.text)
    await state.set_state(BlacklistState.waiting_for_reason)
    
    await message.answer("Введите причину (или отправьте /skip):")


@router.message(BlacklistState.waiting_for_reason)
async def process_blacklist_reason(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    
    reason = None if message.text.lower() == "/skip" else message.text
    
    async with async_session() as session:
        blacklist_entry = Blacklist(
            user_id=user_id,
            username=data['username'],
            reason=reason
        )
        session.add(blacklist_entry)
        await session.commit()
    
    await state.clear()
    await message.answer(
        f"✅ Пользователь `{data['username']}` добавлен в чёрный список!",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "clear_blacklist")
async def callback_clear_blacklist(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        await session.execute(
            delete(Blacklist).where(Blacklist.user_id == user_id)
        )
        await session.commit()
    
    await callback.answer("✅ Чёрный список очищен!", show_alert=True)
    await callback_blacklist_menu(callback)


# ==================== SETTINGS TOGGLES ====================

@router.callback_query(F.data == "settings_menu")
async def callback_settings_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if not user_settings:
            await callback.answer("❌ Ошибка загрузки настроек!", show_alert=True)
            return
        
        text = f"⚙️ **Настройки уведомлений:**\n"
        
        await callback.message.edit_text(
            text,
            reply_markup=get_settings_keyboard(user_settings),
            parse_mode="Markdown"
        )


@router.callback_query(F.data == "toggle_orders")
async def callback_toggle_orders(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if user_settings:
            user_settings.notify_new_orders = not user_settings.notify_new_orders
            await session.commit()
            
            status = "включены" if user_settings.notify_new_orders else "выключены"
            await callback.answer(f"✅ Уведомления о заказах {status}!", show_alert=True)
            await callback_settings_menu(callback)


@router.callback_query(F.data == "toggle_messages")
async def callback_toggle_messages(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if user_settings:
            user_settings.notify_new_messages = not user_settings.notify_new_messages
            await session.commit()
            
            status = "включены" if user_settings.notify_new_messages else "выключены"
            await callback.answer(f"✅ Уведомления о сообщениях {status}!", show_alert=True)
            await callback_settings_menu(callback)


@router.callback_query(F.data == "toggle_delivery")
async def callback_toggle_delivery(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if user_settings:
            user_settings.notify_order_delivery = not user_settings.notify_order_delivery
            await session.commit()
            
            status = "включены" if user_settings.notify_order_delivery else "выключены"
            await callback.answer(f"✅ Уведомления о выдаче {status}!", show_alert=True)
            await callback_settings_menu(callback)


@router.callback_query(F.data == "toggle_raise")
async def callback_toggle_raise(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if user_settings:
            user_settings.notify_lot_raise = not user_settings.notify_lot_raise
            await session.commit()
            
            status = "включены" if user_settings.notify_lot_raise else "выключены"
            await callback.answer(f"✅ Уведомления о поднятии {status}!", show_alert=True)
            await callback_settings_menu(callback)


@router.callback_query(F.data == "set_greeting")
async def callback_set_greeting(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "👋 Введите текст приветственного сообщения:\n\n"
        "**Доступные переменные:**\n"
        "• `{user}` - имя пользователя\n"
        "• `{chat}` - название чата\n",
        parse_mode="Markdown"
    )
    await state.set_state(GreetingState.waiting_for_text)


@router.message(GreetingState.waiting_for_text)
async def process_greeting_text(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    async with async_session() as session:
        await session.execute(
            update(UserSettings)
            .where(UserSettings.user_id == user_id)
            .values(
                greeting_text=message.text,
                auto_greeting_enabled=True
            )
        )
        await session.commit()
    
    await state.clear()
    await message.answer(
        "✅ Приветственное сообщение установлено!",
        reply_markup=get_main_keyboard()
    )


@router.callback_query(F.data == "set_watermark")
async def callback_set_watermark(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "💧 Введите текст водяного знака (будет добавляться к исходящим сообщениям):"
    )
    await state.set_state(WatermarkState.waiting_for_text)


@router.message(WatermarkState.waiting_for_text)
async def process_watermark_text(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    async with async_session() as session:
        await session.execute(
            update(UserSettings)
            .where(UserSettings.user_id == user_id)
            .values(watermark=message.text)
        )
        await session.commit()
    
    await state.clear()
    await message.answer(
        "✅ Водяной знак установлен!",
        reply_markup=get_main_keyboard()
    )


# ==================== EXPORT DATA ====================

@router.callback_query(F.data == "export_data")
async def callback_export_data(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        # Получаем все заказы
        orders_result = await session.execute(
            select(Order).where(Order.user_id == user_id)
        )
        orders = orders_result.scalars().all()
        
        if not orders:
            await callback.answer("❌ Нет данных для экспорта!", show_alert=True)
            return
        
        # Создаем CSV
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Заголовки
        writer.writerow(['ID заказа', 'Покупатель', 'Описание', 'Цена', 'Валюта', 
                        'Статус', 'Автовыдача', 'Дата'])
        
        # Данные
        for order in orders:
            writer.writerow([
                order.order_id,
                order.buyer_username,
                order.description,
                order.price,
                order.currency,
                order.status,
                'Да' if order.auto_delivered else 'Нет',
                order.created_at.strftime('%Y-%m-%d %H:%M:%S')
            ])
        
        # Отправляем файл
        output.seek(0)
        file = BufferedInputFile(
            output.getvalue().encode('utf-8'),
            filename=f"funpay_orders_{user_id}_{datetime.now().strftime('%Y%m%d')}.csv"
        )
        
        await callback.message.answer_document(
            document=file,
            caption=f"📊 Экспорт данных: {len(orders)} заказов"
        )


# ==================== NAVIGATION ====================

@router.callback_query(F.data == "back_to_main")
async def callback_back_to_main(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )


@router.callback_query(F.data.startswith("reply_to_"))
async def callback_start_reply(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    chat_id = parts[2]
    chat_name = "_".join(parts[3:])
    
    await state.update_data(chat_id=chat_id, chat_name=chat_name)
    await state.set_state(ReplyState.waiting_for_reply)
    
    await callback.message.answer(
        f"💬 Отправьте сообщение для ответа в чат **{chat_name}**:\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="Markdown"
    )


@router.message(ReplyState.waiting_for_reply)
async def process_reply(message: types.Message, state: FSMContext):
    if message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Ответ отменен")
        return
    
    user_id = message.from_user.id
    data = await state.get_data()
    chat_id = data.get('chat_id')
    chat_name = data.get('chat_name')
    
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        
        if not user_settings or not user_settings.golden_key:
            await message.answer("❌ Сначала настройте Golden Key!")
            await state.clear()
            return
        
        try:
            account = FunPayAPI.Account(user_settings.golden_key, proxy=user_settings.proxy)
            account.get()
            
            # Добавляем водяной знак если есть
            message_text = message.text
            if user_settings.watermark:
                message_text = f"{user_settings.watermark}\n{message_text}"
            
            # Отправляем сообщение
            account.send_message(int(chat_id), message_text, chat_name)
            
            # Сохраняем в историю
            msg_history = MessageHistory(
                user_id=user_id,
                chat_id=int(chat_id),
                chat_name=chat_name,
                sender=user_settings.username or "me",
                text=message_text,
                is_incoming=False
            )
            session.add(msg_history)
            await session.commit()
            
            await message.answer(
                f"✅ Сообщение отправлено в чат **{chat_name}**!", 
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения: {e}")
            await message.answer(f"❌ Ошибка: {str(e)}")
    
    await state.clear()


# ==================== FUNPAY LISTENER ====================

async def funpay_listener(user_id: int, golden_key: str, proxy: Optional[str], bot: Bot):
    """Слушатель событий FunPay"""
    try:
        account = FunPayAPI.Account(golden_key, proxy=proxy)
        account.get()
        
        runner = FunPayAPI.Runner(account)
        
        logger.info(f"Запущен слушатель FunPay для пользователя {user_id}")
        
        for event in runner.listen(requests_delay=4):
            async with async_session() as session:
                result = await session.execute(
                    select(UserSettings).where(UserSettings.user_id == user_id)
                )
                user_settings = result.scalar_one_or_none()
                
                if not user_settings:
                    break
                
                # Обработка нового заказа
                if isinstance(event, NewOrderEvent):
                    order = event.order
                    
                    # Сохраняем заказ в БД
                    db_order = Order(
                        user_id=user_id,
                        order_id=order.id,
                        buyer_username=order.buyer_username,
                        description=order.description,
                        price=order.price,
                        currency="RUB",
                        status="paid"
                    )
                    session.add(db_order)
                    await session.commit()
                    
                    # Автовыдача
                    if user_settings.auto_delivery_enabled:
                        products_result = await session.execute(
                            select(Product).where(
                                Product.user_id == user_id,
                                Product.is_active == True,
                                Product.lot_title.ilike(f"%{order.description}%")
                            )
                        )
                        products = products_result.scalars().all()
                        
                        if products:
                            try:
                                chat = account.get_chat_by_name(order.buyer_username, True)
                                
                                for product in products:
                                    # Форматируем текст с переменными
                                    variables = {
                                        'buyer': order.buyer_username,
                                        'order_id': order.id,
                                        'lot_name': order.description
                                    }
                                    text = format_text_with_variables(product.product_text, variables)
                                    
                                    # Добавляем водяной знак
                                    if user_settings.watermark:
                                        text = f"{user_settings.watermark}\n{text}"
                                    
                                    account.send_message(chat.id, text)
                                    
                                    # Уменьшаем количество
                                    product.quantity -= 1
                                    if product.quantity <= 0:
                                        product.is_active = False
                                    
                                    await session.commit()
                                
                                db_order.auto_delivered = True
                                await session.commit()
                                
                                if user_settings.notify_order_delivery:
                                    await bot.send_message(
                                        user_id,
                                        f"✅ Автовыдача выполнена для заказа #{order.id}!",
                                        parse_mode="Markdown"
                                    )
                            
                            except Exception as e:
                                logger.error(f"Ошибка автовыдачи: {e}")
                    
                    # Уведомление о заказе
                    if user_settings.notify_new_orders:
                        text = f"📦 **Новый заказ!**\n\n"
                        text += f"• **ID:** {order.id}\n"
                        text += f"• **Покупатель:** {order.buyer_username}\n"
                        text += f"• **Описание:** {order.description}\n"
                        text += f"• **Сумма:** {order.price}\n"
                        
                        try:
                            await bot.send_message(
                                user_id,
                                text,
                                parse_mode="Markdown",
                                reply_markup=get_reply_keyboard(order.buyer_username, order.buyer_username)
                            )
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления о заказе: {e}")
                
                # Обработка нового сообщения
                elif isinstance(event, NewMessageEvent):
                    if event.message.author_id != account.id:
                        msg = event.message
                        
                        # Проверяем чёрный список
                        blacklist_result = await session.execute(
                            select(Blacklist).where(
                                Blacklist.user_id == user_id,
                                Blacklist.username == msg.author
                            )
                        )
                        is_blacklisted = blacklist_result.scalar_one_or_none() is not None
                        
                        # Автоответчик
                        if user_settings.auto_response_enabled and not is_blacklisted:
                            message_text = str(msg).strip().lower()
                            
                            commands_result = await session.execute(
                                select(AutoResponse).where(
                                    AutoResponse.user_id == user_id,
                                    AutoResponse.is_active == True,
                                    AutoResponse.command == message_text
                                )
                            )
                            commands = commands_result.scalars().all()
                            
                            if commands:
                                try:
                                    chat = account.get_chat_by_name(msg.author, True)
                                    
                                    for cmd in commands:
                                        variables = {
                                            'user': msg.author,
                                            'chat': msg.chat_name
                                        }
                                        response_text = format_text_with_variables(cmd.response, variables)
                                        
                                        if user_settings.watermark:
                                            response_text = f"{user_settings.watermark}\n{response_text}"
                                        
                                        account.send_message(chat.id, response_text)
                                    
                                    await asyncio.sleep(1)
                                except Exception as e:
                                    logger.error(f"Ошибка автоответа: {e}")
                        
                        # Автоприветствие (для новых чатов)
                        if user_settings.auto_greeting_enabled and not is_blacklisted:
                            # Здесь можно добавить логику проверки нового чата
                            pass
                        
                        # Сохраняем сообщение в историю
                        msg_history = MessageHistory(
                            user_id=user_id,
                            chat_id=msg.chat_id,
                            chat_name=msg.chat_name,
                            sender=msg.author,
                            text=str(msg),
                            is_incoming=True
                        )
                        session.add(msg_history)
                        await session.commit()
                        
                        # Уведомление о сообщении
                        if user_settings.notify_new_messages and not is_blacklisted:
                            text = f"💬 **Новое сообщение!**\n\n"
                            text += f"• **От:** {msg.author}\n"
                            text += f"• **Чат:** {msg.chat_name}\n"
                            text += f"• **Текст:** {msg.text}\n"
                            
                            try:
                                await bot.send_message(
                                    user_id,
                                    text,
                                    parse_mode="Markdown",
                                    reply_markup=get_reply_keyboard(msg.chat_id, msg.chat_name)
                                )
                            except Exception as e:
                                logger.error(f"Ошибка отправки уведомления о сообщении: {e}")
    
    except Exception as e:
        logger.error(f"Критическая ошибка в слушателе FunPay для пользователя {user_id}: {e}")


async def start_funpay_listeners(bot: Bot):
    """Запускает слушателей FunPay для всех активных пользователей"""
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(
                UserSettings.golden_key.isnot(None),
                UserSettings.is_active == True
            )
        )
        active_users = result.scalars().all()
        
        for user in active_users:
            asyncio.create_task(
                funpay_listener(user.user_id, user.golden_key, user.proxy, bot)
            )
            logger.info(f"Запущен слушатель для пользователя {user.user_id}")


async def activate_users():
    """Активирует всех пользователей с настроенным Golden Key"""
    async with async_session() as session:
        await session.execute(
            update(UserSettings)
            .where(UserSettings.golden_key.isnot(None))
            .values(is_active=True)
        )
        await session.commit()
        logger.info("Активированы все пользователи с настроенным Golden Key")


# ==================== MAIN ====================

async def main():
    # Инициализируем БД
    await init_db()
    
    # Активируем пользователей
    await activate_users()
    
    # Создаем бота
    bot = Bot(token=BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    
    # Регистрируем роутер
    dp.include_router(router)
    
    # Запускаем слушателей FunPay
    await start_funpay_listeners(bot)
    
    # Запускаем polling
    logger.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
