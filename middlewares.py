from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ChatType, ChatMemberStatus
from config import OWNER_ID, REQUIRED_CHANNEL
from database.db import get_db, now_iso


class PrivateSubscriptionMiddleware(BaseMiddleware):
    """Логирует активность, применяет блокировку и требует подписку только в ЛС."""
    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            chat = event.chat
            user = event.from_user
            exempt = bool(event.text and event.text.startswith("/start"))
        elif isinstance(event, CallbackQuery):
            chat = event.message.chat if event.message else None
            user = event.from_user
            exempt = event.data == "subscription:check"
        else:
            return await handler(event, data)
        if not chat or not user or user.is_bot:
            return await handler(event, data)

        # События используются для суточной, недельной и общей аналитики.
        try:
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO activity_events(user_id,event_type,chat_id,created_at) VALUES(?,?,?,?)",
                    (user.id, "interaction", chat.id, now_iso()),
                )
                await db.execute("UPDATE users SET last_active=? WHERE user_id=?", (now_iso(), user.id))
                cur = await db.execute("SELECT is_blocked FROM users WHERE user_id=?", (user.id,))
                row = await cur.fetchone()
                blocked = bool(row and row[0])
        except Exception:
            blocked = False
        if blocked and user.id != OWNER_ID:
            if isinstance(event, Message):
                await event.answer("⛔ Ваш доступ к CouplePet ограничен администратором.")
            else:
                await event.answer("Доступ ограничен администратором", show_alert=True)
            return None

        if chat.type != ChatType.PRIVATE or user.id == OWNER_ID or exempt:
            return await handler(event, data)
        bot = data.get("bot")
        try:
            member = await bot.get_chat_member(REQUIRED_CHANNEL, user.id)
            subscribed = member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
            if member.status == ChatMemberStatus.RESTRICTED:
                subscribed = bool(member.is_member)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Не удалось проверить подписку user_id=%s channel=%s", user.id, REQUIRED_CHANNEL)
            subscribed = False
        if subscribed:
            return await handler(event, data)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
            [InlineKeyboardButton(text="🔄 Проверить подписку", callback_data="subscription:check")],
        ])
        notice = "🔒 Для использования CouplePet подпишитесь на канал @couplepets_updates и нажмите «Проверить подписку»."
        if isinstance(event, Message):
            await event.answer(notice, reply_markup=markup)
        else:
            await event.answer("Сначала подпишитесь на канал", show_alert=True)
            if event.message:
                await event.message.answer(notice, reply_markup=markup)
        return None
