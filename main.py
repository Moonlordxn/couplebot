import asyncio
import random
import secrets
import string
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, CallbackQuery, LabeledPrice,
                           PreCheckoutQuery, FSInputFile)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN as TOKEN, DATABASE_PATH as DB_PATH, ASSETS_DIR, BASE_DIR
from database.db import init_db as new_init_db, now_iso
from handlers import setup_routers

dp = Dispatcher(storage=MemoryStorage())

SPECIES = {
    "cat": ("🐱", "Котик"), "dog": ("🐶", "Щенок"),
    "bunny": ("🐰", "Зайчик"), "fox": ("🦊", "Лисёнок"),
    "panda": ("🐼", "Пандочка"), "dragon": ("🐉", "Дракончик"),
    "unicorn": ("🦄", "Единорожка"), "penguin": ("🐧", "Пингвинёнок"),
}

def get_pet_photo(species: str, stage: str = "baby") -> FSInputFile | None:
    """Возвращает фото питомца, если файл существует."""
    path = ASSETS_DIR / f"{species}_baby.jpg"
    if path.exists():
        return FSInputFile(path)
    return None

RARITIES = ["Обычный", "Необычный", "Редкий", "Эпический", "Легендарный", "Мифический"]
RARITY_WEIGHTS = [50, 25, 15, 7, 2, 1]

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def make_code(length=8):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def clamp(value):
    return max(0, min(100, int(value)))


def stage_for_level(level):
    if level >= 50:
        return "Взрослый"
    if level >= 20:
        return "Подросток"
    if level >= 5:
        return "Малыш"
    return "Кроха"


async def apply_decay(pet_id):
    """Обновляет потребности питомца и завершает трёхчасовой сон."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""SELECT last_fed_at,last_played_at,last_washed_at,last_slept_at,
            last_tick,created_at,sleeping_until FROM pets WHERE id=? AND is_archived=0""", (pet_id,))
        row = await cur.fetchone()
        if not row:
            return
        now = datetime.now(timezone.utc)

        # Пока питомец спит, уход недоступен. По окончании 3 часов он
        # просыпается полностью отдохнувшим, но с голодом 0/100.
        sleeping_until = row["sleeping_until"]
        if sleeping_until:
            try:
                wake_at = datetime.fromisoformat(sleeping_until)
                if wake_at.tzinfo is None:
                    wake_at = wake_at.replace(tzinfo=timezone.utc)
                if now < wake_at:
                    return
                stamp = now.isoformat()
                await db.execute("""UPDATE pets SET sleeping_until=NULL, energy=100, hunger=0,
                    last_slept_at=?, last_fed_at=?, last_tick=? WHERE id=?""",
                    (stamp, stamp, stamp, pet_id))
                await db.commit()
                return
            except (ValueError, TypeError):
                await db.execute("UPDATE pets SET sleeping_until=NULL WHERE id=?", (pet_id,))

        def get_stamp(field):
            raw = row[field] or row["last_tick"] or row["created_at"]
            try:
                value = datetime.fromisoformat(raw)
                return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            except (ValueError, TypeError):
                return now

        # Голод падает с 100 до 0 за 5 часов; остальные показатели
        # сохраняют постепенное снижение. Энергия — за 8 часов.
        decay_rules = (
            ("hunger", "last_fed_at", 180),       # 1 пункт каждые 180 секунд
            ("happiness", "last_played_at", 1200),
            ("cleanliness", "last_washed_at", 1800),
        )
        for value_col, stamp_col, seconds_per_point in decay_rules:
            stamp = get_stamp(stamp_col)
            points = min(100, max(0, int((now - stamp).total_seconds() // seconds_per_point)))
            if points:
                advanced = (stamp + __import__("datetime").timedelta(seconds=points * seconds_per_point)).isoformat()
                await db.execute(f"UPDATE pets SET {value_col}=MAX(0,{value_col}-?), {stamp_col}=? WHERE id=?",
                                 (points, advanced, pet_id))

        energy_stamp = get_stamp("last_slept_at")
        energy_points = min(100, max(0, int((now - energy_stamp).total_seconds() // 288)))
        if energy_points:
            advanced = (energy_stamp + __import__("datetime").timedelta(seconds=energy_points * 288)).isoformat()
            await db.execute("UPDATE pets SET energy=MAX(0,energy-?), last_slept_at=? WHERE id=?",
                             (energy_points, advanced, pet_id))
        await db.execute("UPDATE pets SET last_tick=? WHERE id=?", (now_iso(), pet_id))
        await db.commit()


async def fetch_pet_by_id(pet_id):
    await apply_decay(pet_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM pets WHERE id=? AND is_archived=0", (pet_id,))
        return await cur.fetchone()


async def pet_for_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""SELECT p.id FROM pets p JOIN members m ON p.id=m.pet_id
            LEFT JOIN families f ON f.id=p.family_id
            WHERE m.user_id=? AND p.is_archived=0
              AND (p.family_id IS NULL OR f.status='active')
            ORDER BY CASE WHEN p.ownership_mode='family' THEN 0 ELSE 1 END, p.id DESC LIMIT 1""", (user_id,))
        row = await cur.fetchone()
    return await fetch_pet_by_id(row[0]) if row else None


async def solo_pet_for_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""SELECT p.id FROM pets p JOIN members m ON p.id=m.pet_id
            WHERE m.user_id=? AND p.ownership_mode='solo' AND p.is_archived=0
            ORDER BY p.id DESC LIMIT 1""", (user_id,))
        row = await cur.fetchone()
    return await fetch_pet_by_id(row[0]) if row else None


async def pet_for_chat(chat_id):
    """В группах доступны только питомцы активных семей.

    Личный питомец никогда не должен становиться общим из-за старой записи
    pet_chats или случайного подключения.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""SELECT p.id FROM pets p
            JOIN pet_chats c ON p.id=c.pet_id
            JOIN families f ON f.id=p.family_id
            WHERE c.chat_id=? AND p.is_archived=0
              AND p.ownership_mode='family' AND f.status='active'
            ORDER BY p.id DESC LIMIT 1""", (chat_id,))
        row = await cur.fetchone()
    return await fetch_pet_by_id(row[0]) if row else None


async def resolve_pet(message):
    if message.chat.type != "private":
        return await pet_for_chat(message.chat.id)
    return await pet_for_user(message.from_user.id)


def progress_bar(value):
    filled = round(value / 10)
    return "🟩" * filled + "⬜" * (10 - filled)


async def get_pet_owners(pet_id: int) -> list[str]:
    """Имена владельцев в виде ссылок на Telegram-профили."""
    from html import escape
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""SELECT m.user_id, u.nickname, COALESCE(u.username,m.username) AS username
            FROM members m LEFT JOIN users u ON u.user_id=m.user_id
            WHERE m.pet_id=? ORDER BY m.joined_at""", (pet_id,))
        rows = await cur.fetchall()
    result = []
    for row in rows:
        name = escape(row["nickname"] or row["username"] or str(row["user_id"]))
        url = f"https://t.me/{row['username']}" if row["username"] else f"tg://user?id={row['user_id']}"
        result.append(f'<a href="{escape(url, quote=True)}">{name}</a>')
    return result


def pet_text(pet, owners: list[str] | None = None):
    from html import escape
    emoji, species_name = SPECIES.get(pet["species"], ("🐾", "Питомец"))
    pet_name = escape(str(pet["name"]))
    owners_line = ""
    if owners:
        if len(owners) == 1:
            owners_line = f"👤 Хозяин: <b>{owners[0]}</b>\n"
        else:
            owners_line = f"👥 Хозяева: <b>{' и '.join(owners)}</b>\n"

    sleep_line = ""
    if pet["sleeping_until"]:
        try:
            wake_at = datetime.fromisoformat(pet["sleeping_until"])
            if wake_at.tzinfo is None:
                wake_at = wake_at.replace(tzinfo=timezone.utc)
            remaining = max(0, int((wake_at - datetime.now(timezone.utc)).total_seconds()))
            hours, remainder = divmod(remaining, 3600)
            minutes = remainder // 60
            sleep_line = f"\n💤 Спит · проснётся через {hours} ч. {minutes} мин.\n"
        except (ValueError, TypeError):
            sleep_line = "\n💤 Питомец спит.\n"

    return (
        f"{emoji} <b>{pet_name}</b> — {species_name}\n"
        f"{owners_line}{sleep_line}"
        f"💎 Редкость: <b>{pet['rarity']}</b>\n"
        f"🌱 {pet['stage']} · Уровень {pet['level']}/100\n"
        f"⭐ Опыт: {pet['experience']} · 🪙 Монеты: {pet['coins']}\n\n"
        f"🍗 Сытость: {pet['hunger']}/100\n{progress_bar(pet['hunger'])}\n"
        f"💗 Счастье: {pet['happiness']}/100\n{progress_bar(pet['happiness'])}\n"
        f"⚡ Энергия: {pet['energy']}/100\n{progress_bar(pet['energy'])}\n"
        f"🫧 Чистота: {pet['cleanliness']}/100\n{progress_bar(pet['cleanliness'])}"
    )


async def send_pet_status(target, pet, extra_text: str = "", reply_markup=None):
    """Отправляет статус питомца с картинкой (если есть)."""
    owners = await get_pet_owners(pet["id"])
    text = (extra_text + "\n\n" if extra_text else "") + pet_text(pet, owners)
    photo = get_pet_photo(pet["species"])
    if photo:
        await target.answer_photo(
            photo=photo,
            caption=text,
            parse_mode="HTML",
            reply_markup=reply_markup or pet_keyboard()
        )
    else:
        await target.answer(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup or pet_keyboard()
        )



def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Мой профиль", callback_data="nav:profile")
    kb.button(text="👨‍👩‍👧 Семья", callback_data="nav:family")
    kb.button(text="🐾 Мой питомец", callback_data="menu:pet")
    kb.button(text="📖 Как играть", callback_data="menu:help")
    kb.adjust(2, 1)
    return kb.as_markup()


def pet_keyboard(prefix="act"):
    kb = InlineKeyboardBuilder()
    kb.button(text="🍖 Покормить", callback_data=f"{prefix}:feed")
    kb.button(text="🎾 Поиграть", callback_data=f"{prefix}:play")
    kb.button(text="🛁 Помыть", callback_data=f"{prefix}:wash")
    kb.button(text="😴 Сон", callback_data=f"{prefix}:sleep")
    kb.button(text="🔄 Обновить", callback_data=f"{prefix}:status")
    kb.button(text="🏡 Главное меню", callback_data="menu:home")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def back_keyboard(target="home"):
    kb = InlineKeyboardBuilder()
    kb.button(text="🏡 Главное меню", callback_data=f"menu:{target}")
    return kb.as_markup()



async def create_pet(name, species, user_id, username, chat_id=None):
    code = make_code()
    rarity = random.choices(RARITIES[:5], weights=[55, 25, 13, 5, 2], k=1)[0]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""SELECT p.id FROM members m
            JOIN pets p ON p.id = m.pet_id
            WHERE m.user_id = ? AND p.ownership_mode = 'solo'
              AND p.is_archived = 0 LIMIT 1""", (user_id,))
        if await cur.fetchone():
            return None, "У вас уже есть личный питомец. Откройте /status."
        stamp = now_iso()
        cur = await db.execute("""INSERT INTO pets(name,species,rarity,invite_code,created_at,last_tick,
            last_fed_at,last_played_at,last_washed_at,last_slept_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (name, species, rarity, code, stamp, stamp, stamp, stamp, stamp, stamp))
        pet_id = cur.lastrowid
        await db.execute("INSERT INTO members VALUES(?,?,?,?)",
                         (pet_id, user_id, username, now_iso()))
        if chat_id is not None:
            await db.execute("INSERT INTO pet_chats VALUES(?,?)", (pet_id, chat_id))
        await db.execute("INSERT INTO activity_events(user_id,pet_id,event_type,created_at) VALUES(?,?,?,?)",
                         (user_id, pet_id, "solo_pet_created", now_iso()))
        await db.commit()
    return (pet_id, code, rarity), None


# Старые start/help перенесены в handlers/start.py


@dp.message(Command("create"))
async def create_cmd(message: Message):
    from services.user_service import ensure_user
    await ensure_user(message.from_user.id, message.from_user.username)
    if message.chat.type != "private":
        return await message.answer("Личного питомца можно создать только в личном чате с ботом. Семейного питомца подключают к беседе через /connect_family.")
    args = (message.text or "").split(maxsplit=2)
    name = args[1][:20] if len(args) > 1 else "Пушистик"
    species = args[2].lower() if len(args) > 2 else random.choice(list(SPECIES))
    if species not in SPECIES:
        return await message.answer("Не знаю такой вид. Доступно: " + ", ".join(SPECIES))
    result, error = await create_pet(name, species, message.from_user.id,
        message.from_user.username, None)
    if error:
        return await message.answer(error, reply_markup=main_menu())
    _, code, rarity = result
    emoji, species_name = SPECIES[species]
    from html import escape
    safe_name = escape(name)
    caption = (
        f"🎉 <b>Ура! Ваш личный питомец появился!</b>\n\n{emoji} <b>{safe_name}</b> — {species_name}\n"
        f"💎 Редкость: {rarity}\n\n"
        "🐾 Это ваш личный питомец. Его нельзя превратить в семейного. "
        "Для совместного выращивания создайте отдельную семью через раздел «Семья»."
    )
    photo = get_pet_photo(species)
    if photo:
        await message.answer_photo(photo=photo, caption=caption, parse_mode="HTML", reply_markup=pet_keyboard("soloact"))
    else:
        await message.answer(caption, parse_mode="HTML", reply_markup=pet_keyboard("soloact"))



@dp.message(Command("join"))
async def join_cmd(message: Message):
    await message.answer(
        "🔒 Приглашение по коду личного питомца отключено.\n\n"
        "Личный питомец остаётся личным и не может стать общим. "
        "Чтобы выращивать питомца вместе, создайте семью в разделе «Семья».",
        reply_markup=main_menu(),
    )


@dp.message(Command("status"))
async def status_cmd(message: Message):
    pet = await resolve_pet(message)
    if not pet:
        return await message.answer("Питомца пока нет. Создайте его: <code>/create ИМЯ</code>",
                                    reply_markup=main_menu())
    await send_pet_status(message, pet)



@dp.message(Command("solo_status"))
async def solo_status_cmd(message: Message):
    if message.chat.type != "private":
        return await message.answer("Личный питомец доступен в личном чате с ботом.")
    pet = await solo_pet_for_user(message.from_user.id)
    if not pet:
        return await message.answer("Личного питомца нет. Создайте его командой /create.")
    await send_pet_status(message, pet, reply_markup=pet_keyboard("soloact"))


async def solo_action_by_message(message: Message, action: str):
    if message.chat.type != "private":
        return await message.answer("Личный питомец доступен только в личном чате с ботом.")
    pet = await solo_pet_for_user(message.from_user.id)
    if not pet:
        return await message.answer("Личного питомца нет. Создайте его командой /create.")
    ok, result = await perform_action(pet, message.from_user.id, action)
    if not ok:
        return await message.answer(result)
    pet = await fetch_pet_by_id(pet["id"])
    await send_pet_status(message, pet, extra_text=result, reply_markup=pet_keyboard("soloact"))


@dp.message(Command("solo_feed"))
async def solo_feed_cmd(message: Message): await solo_action_by_message(message, "feed")

@dp.message(Command("solo_play"))
async def solo_play_cmd(message: Message): await solo_action_by_message(message, "play")

@dp.message(Command("solo_wash"))
async def solo_wash_cmd(message: Message): await solo_action_by_message(message, "wash")

@dp.message(Command("solo_sleep"))
async def solo_sleep_cmd(message: Message): await solo_action_by_message(message, "sleep")


@dp.message(Command("code"))
async def code_cmd(message: Message):
    await message.answer("Код приглашения личного питомца больше не используется. Откройте /family, чтобы пригласить человека в семью.")


async def perform_action(pet, user_id, action, chat_id=None):
    changes = {
        "feed": ("hunger", 60, "🍖 Питомец вкусно поел!"),
        "play": ("happiness", 35, "🎾 Вы отлично поиграли!"),
        "wash": ("cleanliness", 45, "🛁 Теперь питомец чистенький!"),
        "sleep": ("energy", 100, "😴 Питомец лёг спать на 3 часа! Я разбужу вас, когда он проснётся."),
    }
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT ownership_mode,family_id,is_archived FROM pets WHERE id=?", (pet["id"],))
        pet_meta = await cur.fetchone()
        if not pet_meta or pet_meta["is_archived"]:
            return False, "Питомец недоступен."
        if pet_meta["ownership_mode"] == "family" and pet_meta["family_id"] is not None:
            cur = await db.execute("SELECT 1 FROM family_members WHERE family_id=? AND user_id=?", (pet_meta["family_id"], user_id))
        else:
            cur = await db.execute("SELECT 1 FROM members WHERE pet_id=? AND user_id=?", (pet["id"], user_id))
        if not await cur.fetchone():
            return False, "⛔ Это не ваш питомец! Заведите своего семейного питомца, чтобы ухаживать за ним вместе."
        cur = await db.execute("SELECT created_at FROM action_log WHERE pet_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                               (pet["id"], user_id))
        row = await cur.fetchone()
        if row:
            try:
                if (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() < 5:
                    return False, "Подожди несколько секунд перед следующим действием."
            except ValueError:
                pass
        field, amount, result = changes[action]
        # Проверяем актуальные показатели из БД, а не устаревший объект pet.
        cur = await db.execute(
            "SELECT hunger, happiness, cleanliness, energy, sleeping_until FROM pets WHERE id=?",
            (pet["id"],)
        )
        needs = await cur.fetchone()
        if not needs:
            return False, "Питомец недоступен."

        if needs["sleeping_until"]:
            try:
                wake_at = datetime.fromisoformat(needs["sleeping_until"])
                if wake_at.tzinfo is None:
                    wake_at = wake_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < wake_at:
                    return False, "💤 Питомец крепко спит. Дайте ему отдохнуть — он проснётся через 3 часа после укладывания."
            except (ValueError, TypeError):
                pass

        if action == "sleep":
            if int(needs["energy"]) > 0:
                return False, "😴 Питомец пока не устал. Укладывать его спать нужно, когда энергия достигнет 0/100."
            now_dt = datetime.now(timezone.utc)
            wake_at = (now_dt + __import__("datetime").timedelta(hours=3)).isoformat()
            now = now_dt.isoformat()
            await db.execute("UPDATE pets SET sleeping_until=?, last_slept_at=?, last_tick=? WHERE id=?",
                             (wake_at, now, now, pet["id"]))
            await db.execute("INSERT INTO action_log(pet_id,user_id,action,created_at) VALUES(?,?,?,?)",
                             (pet["id"], user_id, action, now))
            await db.execute("UPDATE users SET last_active=? WHERE user_id=?", (now, user_id))
            await db.commit()
            return True, "😴 Питомец лёг спать на 3 часа! Во время сна его нельзя кормить или тревожить. После пробуждения он будет очень голоден."

        if int(needs["energy"]) <= 0:
            return False, "🥱 Питомец слишком устал и не хочет ничего делать. Сначала уложите его спать на 3 часа!"

        full_messages = {
            "feed": ("hunger", "🍗 Питомец уже сыт! Он пока не хочет есть."),
            "play": ("happiness", "🎾 Питомец уже счастлив и пока не хочет играть."),
            "wash": ("cleanliness", "🫧 Питомец уже чистенький! Купание пока не требуется."),
        }
        need_field, full_message = full_messages[action]
        if int(needs[need_field]) >= 100:
            return False, full_message

        now = now_iso()
        timestamp_field = {"feed":"last_fed_at", "play":"last_played_at", "wash":"last_washed_at"}[action]
        await db.execute(f"UPDATE pets SET {field}=MIN(100,{field}+?), experience=experience+10, coins=coins+3, {timestamp_field}=?, last_tick=? WHERE id=?",
                         (amount, now, now, pet["id"]))
        await db.execute("INSERT INTO action_log(pet_id,user_id,action,created_at) VALUES(?,?,?,?)",
                         (pet["id"], user_id, action, now))
        await db.execute("UPDATE users SET last_active=? WHERE user_id=?", (now, user_id))
        await db.execute("INSERT INTO activity_events(user_id,family_id,pet_id,event_type,chat_id,created_at) VALUES(?,?,?,?,?,?)",
                         (user_id, pet["family_id"], pet["id"], f"pet_{action}", chat_id, now))
        cur = await db.execute("SELECT experience,level FROM pets WHERE id=?", (pet["id"],))
        xp, level = await cur.fetchone()
        leveled = False
        while level < 100 and xp >= level * 50:
            xp -= level * 50
            level += 1
            leveled = True
        await db.execute("UPDATE pets SET experience=?,level=?,stage=? WHERE id=?",
                         (xp, level, stage_for_level(level), pet["id"]))
        await db.commit()
    return True, result + ("\n🎉 Новый уровень!" if leveled else "")


async def action_by_message(message, action):
    pet = await resolve_pet(message)
    if not pet:
        return await message.answer("Сначала создайте питомца: /create", reply_markup=main_menu())
    ok, result = await perform_action(pet, message.from_user.id, action,
                                      message.chat.id if message.chat.type != "private" else None)
    if not ok:
        return await message.answer(result)
    pet = await fetch_pet_by_id(pet["id"])
    await send_pet_status(message, pet, extra_text=result)
    await notify_other_owner(message.bot, pet["id"], message.from_user.id, action)


async def notify_other_owner(bot, pet_id: int, actor_id: int, action: str):
    from html import escape
    action_labels = {
        "feed": "покормил питомца",
        "play": "поиграл с питомцем",
        "wash": "помыл питомца",
        "sleep": "уложил питомца спать",
    }
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""SELECT m.user_id FROM members m
            WHERE m.pet_id=? AND m.user_id<>?""", (pet_id, actor_id))
        partners = await cur.fetchall()
        cur = await db.execute("SELECT name FROM pets WHERE id=?", (pet_id,))
        pet_row = await cur.fetchone()
        cur = await db.execute("SELECT nickname,username FROM users WHERE user_id=?", (actor_id,))
        actor_row = await cur.fetchone()
    pet_name = escape(str(pet_row["name"])) if pet_row else "питомцем"
    actor_name = escape((actor_row["nickname"] or actor_row["username"] or str(actor_id)) if actor_row else str(actor_id))
    actor_url = f"https://t.me/{actor_row['username']}" if actor_row and actor_row["username"] else f"tg://user?id={actor_id}"
    actor_link = f'<a href="{escape(actor_url, quote=True)}">{actor_name}</a>'
    action_text = action_labels.get(action, "позаботился о")
    text = f"💗 {actor_link} {action_text}: <b>{pet_name}</b>!"
    for partner in partners:
        try:
            await bot.send_message(partner["user_id"], text, parse_mode="HTML")
        except Exception:
            pass


@dp.message(Command("feed"))
async def feed_cmd(message: Message):
    await action_by_message(message, "feed")


@dp.message(Command("play"))
async def play_cmd(message: Message):
    await action_by_message(message, "play")


@dp.message(Command("wash"))
async def wash_cmd(message: Message):
    await action_by_message(message, "wash")


@dp.message(Command("sleep"))
async def sleep_cmd(message: Message):
    await action_by_message(message, "sleep")


@dp.callback_query(
    F.data.startswith("menu:"),
    ~F.data.in_({"menu:profile", "menu:family", "menu:home", "menu:help"})
)
async def menu_callback(callback: CallbackQuery):
    section = callback.data.split(":", 1)[1]
    await callback.answer()
    if section == "pet":
        pet = await pet_for_chat(callback.message.chat.id) if callback.message.chat.type != "private" else await pet_for_user(callback.from_user.id)
        if not pet:
            return await callback.message.answer("🐣 Питомца пока нет. Создай его командой <code>/create ИМЯ cat</code>.",
                                                 parse_mode="HTML", reply_markup=main_menu())
        return await send_pet_status(callback.message, pet)

    elif section == "invite":
        pet = await pet_for_user(callback.from_user.id)
        if not pet:
            return await callback.message.answer("Сначала создай питомца: <code>/create ИМЯ</code>",
                                                 parse_mode="HTML", reply_markup=main_menu())
        text = "Личного питомца нельзя превратить в семейного. Создайте семью в разделе «Семья», чтобы пригласить второго владельца."
        markup = back_keyboard()
    elif section == "help":
        text = ("📖 <b>Как играть</b>\n\n"
                "🐾 <b>Личный питомец</b>\nСоздайте его командой /create и ухаживайте самостоятельно.\n\n"
                "💞 <b>Семейный питомец</b>\nОткройте /family, создайте семью и пригласите партнёра. После подтверждения обоих участников создайте нового общего питомца.\n\n"
                "🍖 Уход: /feed, /play, /wash, /sleep.\n"
                "🏡 Для управления семейным питомцем в группе используйте /connect_family.\n\n"
                "Личного питомца нельзя превратить в семейного.")
        markup = back_keyboard()
    else:
        return
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)


@dp.callback_query(F.data.startswith("act:"))
@dp.callback_query(F.data.startswith("soloact:"))
async def action_callback(callback: CallbackQuery):
    # Telegram requires every callback query to be acknowledged promptly.
    # Acknowledge first so a database/photo/notification error cannot leave
    # the inline button spinning forever.
    try:
        await callback.answer("Обрабатываю действие…")
    except Exception:
        logging.getLogger(__name__).exception("Не удалось подтвердить callback query")

    try:
        if not callback.message:
            return
        is_solo = callback.data.startswith("soloact:")
        action = callback.data.split(":", 1)[1]
        if is_solo and callback.message.chat.type != "private":
            await callback.message.answer("Личный питомец доступен только в личном чате.")
            return

        if callback.message.chat.type != "private":
            pet = await pet_for_chat(callback.message.chat.id)
        elif is_solo:
            pet = await solo_pet_for_user(callback.from_user.id)
        else:
            pet = await pet_for_user(callback.from_user.id)

        if not pet:
            await callback.message.answer("Сначала создайте питомца командой /create")
            return

        prefix = "soloact" if is_solo else "act"
        if action == "status":
            pet = await fetch_pet_by_id(pet["id"])
            await send_pet_status(callback.message, pet, reply_markup=pet_keyboard(prefix))
            return

        if action not in {"feed", "play", "wash", "sleep"}:
            await callback.message.answer("Неизвестное действие. Обновите меню питомца.")
            return

        ok, result = await perform_action(
            pet, callback.from_user.id, action,
            callback.message.chat.id if callback.message.chat.type != "private" else None
        )
        if not ok:
            await callback.message.answer(result)
            return

        pet = await fetch_pet_by_id(pet["id"])
        await send_pet_status(
            callback.message, pet, extra_text=result,
            reply_markup=pet_keyboard(prefix)
        )
        if not is_solo:
            try:
                await notify_other_owner(callback.bot, pet["id"], callback.from_user.id, action)
            except Exception:
                logging.getLogger(__name__).exception("Не удалось уведомить второго владельца о действии с питомцем")
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "Ошибка обработки действия питомца: user_id=%s callback_data=%s",
            callback.from_user.id, callback.data
        )
        try:
            await callback.message.answer(
                "⚠️ Не удалось выполнить действие с питомцем. Ошибка записана в журнал бота; "
                "проверьте логи запуска и отправьте их разработчику."
            )
        except Exception:
            logging.getLogger(__name__).exception("Не удалось отправить сообщение об ошибке callback")



async def main():
    # Новая инициализация БД (с миграцией)
    await new_init_db()
    # Подключаем новые роутеры (профиль, семья, старт)
    from middlewares import PrivateSubscriptionMiddleware
    dp.message.outer_middleware(PrivateSubscriptionMiddleware())
    dp.callback_query.outer_middleware(PrivateSubscriptionMiddleware())
    dp.include_router(setup_routers())
    bot = Bot(TOKEN)
    from services.reminder_service import reminder_loop
    reminder_task = asyncio.create_task(reminder_loop(bot))
    print("CouplePet is running...")
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
