from database.db import get_db, now_iso
from datetime import datetime, timedelta, timezone


async def get_stats() -> dict:
    """Краткая статистика главного экрана админ-панели."""
    async with get_db() as db:
        async def count(sql):
            cur = await db.execute(sql)
            return (await cur.fetchone())[0]
        users_total = await count("SELECT COUNT(*) FROM users WHERE deleted_at IS NULL")
        users_blocked = await count("SELECT COUNT(*) FROM users WHERE is_blocked=1 AND deleted_at IS NULL")
        families = await count("SELECT COUNT(*) FROM families WHERE status='active'")
        pets = await count("SELECT COUNT(*) FROM pets WHERE is_archived=0")
        collection = await count("SELECT COUNT(*) FROM collection_pets")
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until > ?", (now_iso(),))
        premium = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM activity_events WHERE created_at>=?", ((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),))
        active_24h = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM activity_events WHERE created_at>=?", ((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),))
        active_7d = (await cur.fetchone())[0]
        return {"users_total":users_total,"users_blocked":users_blocked,"families":families,"pets":pets,
                "collection":collection,"active_24h":active_24h,"active_7d":active_7d,"premium":premium}


async def get_user_full(user_id: int) -> dict | None:
    """Полный профиль пользователя для админа."""
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = await cur.fetchone()
        if not user:
            return None
        user = dict(user)

        cur = await db.execute("""
            SELECT f.id, f.name, f.level, f.balance, fm.role
            FROM family_members fm
            JOIN families f ON f.id = fm.family_id
            WHERE fm.user_id = ?
        """, (user_id,))
        family = await cur.fetchone()
        user["family"] = dict(family) if family else None

        cur = await db.execute("SELECT COUNT(*) FROM collection_pets WHERE user_id = ?", (user_id,))
        user["collection_count"] = (await cur.fetchone())[0]

        return user


async def give_coins(user_id: int, amount: int) -> tuple[bool, str]:
    async with get_db() as db:
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ? AND deleted_at IS NULL", (user_id,))
        row = await cur.fetchone()
        if not row:
            return False, "Пользователь не найден"
        await db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()
        new_balance = row[0] + amount
        return True, f"Выдано {amount} монет. Новый баланс: {new_balance}"


async def set_block(user_id: int, blocked: bool) -> tuple[bool, str]:
    async with get_db() as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id = ? AND deleted_at IS NULL", (user_id,))
        if not await cur.fetchone():
            return False, "Пользователь не найден"
        await db.execute(
            "UPDATE users SET is_blocked = ? WHERE user_id = ?",
            (1 if blocked else 0, user_id)
        )
        await db.commit()
        return True, "Пользователь заблокирован" if blocked else "Пользователь разблокирован"


async def give_premium(user_id: int, days: int = 30) -> tuple[bool, str]:
    from datetime import datetime, timezone, timedelta
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    async with get_db() as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id = ? AND deleted_at IS NULL", (user_id,))
        if not await cur.fetchone():
            return False, "Пользователь не найден"
        await db.execute(
            "UPDATE users SET premium_until = ? WHERE user_id = ?",
            (until, user_id)
        )
        await db.commit()
        return True, f"Premium выдан на {days} дней (до {until[:10]})"


async def get_recent_users(limit: int = 10) -> list[dict]:
    async with get_db() as db:
        cur = await db.execute("""
            SELECT user_id, username, nickname, level, coins, last_active, is_blocked
            FROM users
            WHERE deleted_at IS NULL
            ORDER BY last_active DESC
            LIMIT ?
        """, (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def get_families(limit: int = 30, include_archived: bool = False, offset: int = 0) -> list[dict]:
    async with get_db() as db:
        where = "" if include_archived else "WHERE f.status='active'"
        cur = await db.execute(f"""SELECT f.id,f.name,f.status,f.created_at,f.last_active,
            (SELECT COUNT(*) FROM family_members fm WHERE fm.family_id=f.id) AS members_count,
            (SELECT p.name FROM pets p WHERE p.family_id=f.id ORDER BY p.id DESC LIMIT 1) AS pet_name,
            (SELECT p.level FROM pets p WHERE p.family_id=f.id ORDER BY p.id DESC LIMIT 1) AS pet_level
            FROM families f {where} ORDER BY f.id DESC LIMIT ? OFFSET ?""", (limit, offset))
        return [dict(r) for r in await cur.fetchall()]


async def get_family_details(family_id: int) -> dict | None:
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM families WHERE id=?", (family_id,))
        row = await cur.fetchone()
        if not row:
            return None
        result = dict(row)
        cur = await db.execute("""SELECT u.user_id,u.username,u.nickname,fm.role,fm.joined_at
            FROM family_members fm LEFT JOIN users u ON u.user_id=fm.user_id
            WHERE fm.family_id=? ORDER BY fm.joined_at""", (family_id,))
        result["members"] = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute("SELECT * FROM pets WHERE family_id=? ORDER BY id DESC", (family_id,))
        result["pets"] = [dict(r) for r in await cur.fetchall()]
        return result


async def search_user(query: str) -> dict | None:
    query = (query or "").strip().lstrip("@")
    async with get_db() as db:
        if query.isdigit():
            cur = await db.execute("SELECT * FROM users WHERE user_id=? AND deleted_at IS NULL", (int(query),))
        else:
            cur = await db.execute("SELECT * FROM users WHERE lower(username)=lower(?) AND deleted_at IS NULL LIMIT 1", (query,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def archive_family(family_id: int, admin_id: int, reason: str = "admin_deleted"):
    async with get_db() as db:
        cur = await db.execute("SELECT status FROM families WHERE id=?", (family_id,))
        family_row = await cur.fetchone()
        if not family_row or family_row[0] != "active":
            return False, [], None
        cur = await db.execute("SELECT user_id FROM family_members WHERE family_id=?", (family_id,))
        members = [r[0] for r in await cur.fetchall()]
        cur = await db.execute("SELECT name FROM pets WHERE family_id=? AND is_archived=0 ORDER BY id DESC LIMIT 1", (family_id,))
        pet = await cur.fetchone()
        pet_name = pet[0] if pet else None
        now = now_iso()
        await db.execute("UPDATE families SET status='archived',archived_at=?,archived_reason=?,deleted_at=? WHERE id=?", (now, reason, now, family_id))
        await db.execute("UPDATE pets SET is_archived=1,archived_at=? WHERE family_id=? AND is_archived=0", (now, family_id))
        await db.execute("UPDATE family_invites SET status='revoked' WHERE family_id=? AND status='active'", (family_id,))
        await db.execute("UPDATE family_join_requests SET status='rejected',decided_at=? WHERE family_id=? AND status IN ('awaiting_invitee','awaiting_owner')", (now, family_id))
        await db.execute("INSERT INTO admin_action_log(admin_id,action,target_type,target_id,reason,created_at) VALUES(?,?,?,?,?,?)", (admin_id, "archive_family", "family", str(family_id), reason, now))
        await db.execute("INSERT INTO activity_events(user_id,family_id,event_type,created_at) VALUES(?,?,?,?)", (admin_id, family_id, "family_archived", now))
        return True, members, pet_name


async def delete_user_profile(user_id: int, admin_id: int, reason: str = "admin_deleted"):
    """Анонимизирует профиль, архивируя связанные питомцев/семьи; история остаётся."""
    async with get_db() as db:
        cur = await db.execute("SELECT deleted_at FROM users WHERE user_id=?", (user_id,))
        user_row = await cur.fetchone()
        if not user_row or user_row[0] is not None:
            return False, [], []
        cur = await db.execute("SELECT family_id FROM family_members WHERE user_id=?", (user_id,))
        family_ids = [r[0] for r in await cur.fetchall()]
        affected_members = []
        now = now_iso()
        for family_id in family_ids:
            cur = await db.execute("SELECT user_id FROM family_members WHERE family_id=?", (family_id,))
            affected_members.extend(r[0] for r in await cur.fetchall())
            await db.execute("UPDATE families SET status='archived',archived_at=?,archived_reason='profile_deleted',deleted_at=? WHERE id=?", (now, now, family_id))
            await db.execute("UPDATE pets SET is_archived=1,archived_at=? WHERE family_id=? AND is_archived=0", (now, family_id))
            await db.execute("UPDATE family_invites SET status='revoked' WHERE family_id=? AND status='active'", (family_id,))
            await db.execute("UPDATE family_join_requests SET status='rejected',decided_at=? WHERE family_id=? AND status IN ('awaiting_invitee','awaiting_owner')", (now, family_id))
            await db.execute("INSERT INTO admin_action_log(admin_id,action,target_type,target_id,reason,created_at) VALUES(?,?,?,?,?,?)", (admin_id, "archive_family", "family", str(family_id), "profile_deleted", now))
        cur = await db.execute("SELECT id FROM pets p JOIN members m ON m.pet_id=p.id WHERE m.user_id=? AND p.family_id IS NULL AND p.is_archived=0", (user_id,))
        pet_ids = [r[0] for r in await cur.fetchall()]
        for pet_id in pet_ids:
            await db.execute("UPDATE pets SET is_archived=1,archived_at=? WHERE id=?", (now, pet_id))
        await db.execute("DELETE FROM collection_pets WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM private_chats WHERE user_id=?", (user_id,))
        await db.execute("UPDATE users SET username=NULL,nickname='Удалённый пользователь',is_blocked=1,private_started=0,deleted_at=? WHERE user_id=?", (now, user_id))
        await db.execute("INSERT INTO admin_action_log(admin_id,action,target_type,target_id,reason,created_at) VALUES(?,?,?,?,?,?)", (admin_id, "anonymize_user", "user", str(user_id), reason, now))
        await db.execute("INSERT INTO activity_events(user_id,event_type,created_at) VALUES(?,?,?)", (admin_id, "user_profile_deleted", now))
        return True, sorted(set(affected_members)), pet_ids


async def get_period_stats(days: int) -> dict:
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with get_db() as db:
        async def scalar(sql, args=(since,)):
            cur = await db.execute(sql, args)
            return (await cur.fetchone())[0]
        return {
            "active_users": await scalar("SELECT COUNT(DISTINCT user_id) FROM activity_events WHERE created_at>=? AND user_id IS NOT NULL"),
            "registrations": await scalar("SELECT COUNT(*) FROM users WHERE created_at>=? AND deleted_at IS NULL"),
            "families": await scalar("SELECT COUNT(*) FROM families WHERE created_at>=?"),
            "pets": await scalar("SELECT COUNT(*) FROM pets WHERE created_at>=?"),
            "care_actions": await scalar("SELECT COUNT(*) FROM action_log WHERE created_at>=?"),
            "groups": await scalar("SELECT COUNT(DISTINCT chat_id) FROM activity_events WHERE created_at>=? AND chat_id<0"),
        }


async def get_full_stats() -> dict:
    async with get_db() as db:
        queries = {
            "users": "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL",
            "families_active": "SELECT COUNT(*) FROM families WHERE status='active'",
            "families_archived": "SELECT COUNT(*) FROM families WHERE status='archived'",
            "pets_active": "SELECT COUNT(*) FROM pets WHERE is_archived=0",
            "pets_archived": "SELECT COUNT(*) FROM pets WHERE is_archived=1",
            "solo_pets": "SELECT COUNT(*) FROM pets WHERE ownership_mode='solo' AND is_archived=0",
            "family_pets": "SELECT COUNT(*) FROM pets WHERE ownership_mode='family' AND is_archived=0",
            "groups": "SELECT COUNT(DISTINCT chat_id) FROM pet_chats",
            "care_actions": "SELECT COUNT(*) FROM action_log",
            "invites_created": "SELECT COUNT(*) FROM family_invites",
            "invites_used": "SELECT COUNT(*) FROM family_invites WHERE status='used'",
            "collection_entries": "SELECT COUNT(*) FROM collection_pets",
            "blocked_users": "SELECT COUNT(*) FROM users WHERE is_blocked=1 AND deleted_at IS NULL",
            "join_requests_rejected": "SELECT COUNT(*) FROM family_join_requests WHERE status='rejected'",
            "join_requests_pending": "SELECT COUNT(*) FROM family_join_requests WHERE status IN ('awaiting_invitee','awaiting_owner')",
        }
        result = {}
        for key, sql in queries.items():
            cur = await db.execute(sql)
            result[key] = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until>? AND deleted_at IS NULL", (now_iso(),))
        result["premium_users"] = (await cur.fetchone())[0]
        return result


async def get_admin_log(limit: int = 20) -> list[dict]:
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM admin_action_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def get_weekly_activity_by_day() -> list[dict]:
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=6)).date().isoformat()
    async with get_db() as db:
        cur = await db.execute("""SELECT substr(created_at,1,10) AS day,
            COUNT(DISTINCT user_id) AS users FROM activity_events
            WHERE created_at>=? AND user_id IS NOT NULL GROUP BY substr(created_at,1,10) ORDER BY day""", (since,))
        return [dict(r) for r in await cur.fetchall()]


async def log_admin_action(admin_id: int, action: str, target_type: str, target_id: str, reason: str | None = None):
    async with get_db() as db:
        await db.execute("INSERT INTO admin_action_log(admin_id,action,target_type,target_id,reason,created_at) VALUES(?,?,?,?,?,?)",
                         (admin_id, action, target_type, str(target_id), reason, now_iso()))
