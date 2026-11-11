from database.db import get_db, now_iso


async def ensure_user(user_id: int, username: str | None = None) -> dict:
    """Создаёт пользователя, если его ещё нет, и возвращает профиль."""
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            # Обновляем активность и username
            await db.execute(
                "UPDATE users SET last_active = ?, username = COALESCE(?, username) WHERE user_id = ?",
                (now_iso(), username, user_id)
            )
            await db.execute(
                "INSERT INTO activity_events(user_id,event_type,created_at) VALUES(?,?,?)",
                (user_id, "user_active", now_iso())
            )
            await db.commit()
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            return dict(await cur.fetchone())

        nickname = username or f"Игрок{user_id}"
        await db.execute(
            """INSERT INTO users (user_id, username, nickname, created_at, last_active)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, nickname, now_iso(), now_iso())
        )
        await db.execute(
            "INSERT INTO activity_events(user_id,event_type,created_at) VALUES(?,?,?)",
            (user_id, "user_registered", now_iso())
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return dict(await cur.fetchone())


async def get_user(user_id: int) -> dict | None:
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_nickname(user_id: int, nickname: str) -> bool:
    nickname = nickname.strip()[:20]
    if not nickname:
        return False
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET nickname = ? WHERE user_id = ?",
            (nickname, user_id)
        )
        await db.commit()
        return True


async def add_coins(user_id: int, amount: int) -> int:
    """Добавляет монеты пользователю. Возвращает новый баланс."""
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET coins = coins + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        return (await cur.fetchone())[0]
