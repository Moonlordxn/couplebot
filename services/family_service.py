"""Семейные сценарии CouplePet: двухэтапное подтверждение приглашений."""
import secrets
import string
from datetime import datetime, timedelta, timezone

from database.db import get_db, now_iso
from config import MAX_FAMILY_MEMBERS


def make_code(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _profile_url(user_id: int, username: str | None) -> str:
    if username:
        return f"https://t.me/{username.lstrip('@')}"
    return f"tg://user?id={user_id}"


async def get_user_family(user_id: int) -> dict | None:
    async with get_db() as db:
        cur = await db.execute(
            """SELECT f.* FROM families f JOIN family_members fm ON fm.family_id=f.id
               WHERE fm.user_id=? AND f.status='active' ORDER BY f.id DESC LIMIT 1""",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        family = dict(row)
        cur = await db.execute(
            "SELECT 1 FROM family_invites WHERE family_id=? AND status='active' AND expires_at>? LIMIT 1",
            (family["id"], now_iso()),
        )
        family["has_active_invite"] = bool(await cur.fetchone())
        cur = await db.execute(
            """SELECT fm.user_id, fm.role, fm.joined_at, u.nickname, u.username
               FROM family_members fm JOIN users u ON u.user_id=fm.user_id
               WHERE fm.family_id=? ORDER BY fm.joined_at, fm.user_id""",
            (family["id"],),
        )
        members = [dict(r) for r in await cur.fetchall()]
        for member in members:
            member["profile_url"] = _profile_url(member["user_id"], member.get("username"))
        family["members"] = members
        return family


async def get_family_by_id(family_id: int) -> dict | None:
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM families WHERE id=?", (family_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def create_family(user_id: int, name: str) -> tuple[dict | None, str | None]:
    name = (name or "").strip()[:30]
    if not name:
        return None, "Укажите название семьи."
    async with get_db() as db:
        cur = await db.execute(
            """SELECT 1 FROM family_members fm JOIN families f ON f.id=fm.family_id
               WHERE fm.user_id=? AND f.status='active' LIMIT 1""", (user_id,)
        )
        if await cur.fetchone():
            return None, "Вы уже состоите в активной семье."
        code = make_code()
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(hours=48)).isoformat()
        cur = await db.execute(
            """INSERT INTO families(name,invite_code,created_at,last_active,status)
               VALUES(?,?,?,?, 'active')""", (name, code, now.isoformat(), now.isoformat())
        )
        family_id = cur.lastrowid
        await db.execute(
            "INSERT INTO family_members(family_id,user_id,role,joined_at) VALUES(?,?,'owner',?)",
            (family_id, user_id, now.isoformat()),
        )
        await db.execute(
            """INSERT INTO family_invites(family_id,code,created_by,created_at,expires_at)
               VALUES(?,?,?,?,?)""", (family_id, code, user_id, now.isoformat(), expires)
        )
        await db.execute(
            "INSERT INTO activity_events(user_id,family_id,event_type,created_at) VALUES(?,?,?,?)",
            (user_id, family_id, "family_created", now.isoformat()),
        )
    return await get_user_family(user_id), None


async def get_or_create_family_invite(family_id: int, owner_id: int) -> str | None:
    """Возвращает действующий код либо выпускает новый после истечения старого."""
    async with get_db() as db:
        cur = await db.execute("SELECT 1 FROM family_members WHERE family_id=? AND user_id=?", (family_id, owner_id))
        if not await cur.fetchone():
            return None
        cur = await db.execute("SELECT COUNT(*) FROM family_members WHERE family_id=?", (family_id,))
        if (await cur.fetchone())[0] >= MAX_FAMILY_MEMBERS:
            return None
        cur = await db.execute("""SELECT code,expires_at FROM family_invites
            WHERE family_id=? AND status='active' ORDER BY id DESC LIMIT 1""", (family_id,))
        row = await cur.fetchone()
        now = datetime.now(timezone.utc)
        if row:
            try:
                if datetime.fromisoformat(row["expires_at"]) > now:
                    return row["code"]
            except (ValueError, TypeError):
                pass
        await db.execute("UPDATE family_invites SET status='expired' WHERE family_id=? AND status='active'", (family_id,))
        code = make_code()
        await db.execute("UPDATE families SET invite_code=?,last_active=? WHERE id=?", (code, now.isoformat(), family_id))
        await db.execute("""INSERT INTO family_invites(family_id,code,created_by,created_at,expires_at,status)
            VALUES(?,?,?,?,?,'active')""", (family_id, code, owner_id, now.isoformat(), (now + timedelta(hours=48)).isoformat()))
        return code


async def get_invite_by_code(code: str) -> dict | None:
    """Find active invite; migrate a legacy family.invite_code on demand."""
    code = (code or "").strip().upper()
    if not code:
        return None
    async with get_db() as db:
        cur = await db.execute(
            """SELECT i.*, f.name AS family_name, f.status AS family_status,
                      i.created_by AS owner_id, u.nickname AS owner_nickname, u.username AS owner_username
               FROM family_invites i JOIN families f ON f.id=i.family_id
               LEFT JOIN users u ON u.user_id=i.created_by
               WHERE UPPER(i.code)=? AND i.status='active'""", (code,)
        )
        row = await cur.fetchone()
        if row:
            return dict(row)

        # Backward compatibility: older builds stored invite_code only in families.
        cur = await db.execute(
            "SELECT * FROM families WHERE UPPER(invite_code)=? AND status='active' LIMIT 1", (code,)
        )
        family = await cur.fetchone()
        if not family:
            return None
        cur = await db.execute(
            "SELECT fm.user_id,u.nickname,u.username FROM family_members fm LEFT JOIN users u ON u.user_id=fm.user_id WHERE fm.family_id=? ORDER BY fm.joined_at LIMIT 1",
            (family['id'],),
        )
        owner = await cur.fetchone()
        if not owner:
            return None
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=48)
        cur = await db.execute(
            """INSERT INTO family_invites(family_id,code,created_by,created_at,expires_at,status)
               VALUES(?,?,?,?,?,'active')""",
            (family['id'], code, owner['user_id'], now.isoformat(), expires.isoformat()),
        )
        return {
            'id': cur.lastrowid, 'family_id': family['id'], 'code': code,
            'created_by': owner['user_id'], 'owner_id': owner['user_id'],
            'created_at': now.isoformat(), 'expires_at': expires.isoformat(),
            'status': 'active', 'family_name': family['name'], 'family_status': family['status'],
            'owner_nickname': owner['nickname'], 'owner_username': owner['username'],
        }


async def cancel_family_invite(family_id: int, owner_id: int) -> tuple[bool, str]:
    """Revoke active family invite and any pending join requests."""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT 1 FROM family_members WHERE family_id=? AND user_id=?", (family_id, owner_id)
        )
        if not await cur.fetchone():
            return False, "Отменить приглашение может только участник этой семьи."
        cur = await db.execute(
            "SELECT id FROM family_invites WHERE family_id=? AND status='active'", (family_id,)
        )
        invite_ids = [int(r['id']) for r in await cur.fetchall()]
        if not invite_ids:
            return False, "Активного приглашения нет."
        placeholders = ','.join('?' for _ in invite_ids)
        await db.execute(
            f"UPDATE family_invites SET status='revoked' WHERE id IN ({placeholders})", invite_ids
        )
        await db.execute(
            f"UPDATE family_join_requests SET status='cancelled',decided_at=? WHERE invite_id IN ({placeholders}) AND status IN ('awaiting_invitee','awaiting_owner')",
            [now_iso(), *invite_ids],
        )
        await db.execute("UPDATE families SET invite_code=?,last_active=? WHERE id=?", (make_code(), now_iso(), family_id))
        return True, "Приглашение отменено. Старый код и ссылка больше не работают."


async def cancel_join_request(request_id: int, requester_id: int) -> tuple[bool, str]:
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE family_join_requests SET status='cancelled',decided_at=? WHERE id=? AND requester_id=? AND status IN ('awaiting_invitee','awaiting_owner')",
            (now_iso(), request_id, requester_id),
        )
        if cur.rowcount:
            return True, "Ваш запрос на вступление отменён."
        return False, "Запрос уже обработан или не найден."


async def request_join_family(user_id: int, code: str) -> tuple[dict | None, str | None]:
    invite = await get_invite_by_code(code)
    if not invite:
        return None, "Приглашение не найдено или уже недействительно."
    try:
        if datetime.fromisoformat(invite["expires_at"]) <= datetime.now(timezone.utc):
            return None, "Срок действия приглашения истёк. Попросите создать новое."
    except (ValueError, TypeError):
        return None, "Приглашение имеет некорректный срок действия."
    if invite["family_status"] != "active":
        return None, "Эта семья больше не активна."
    if invite["created_by"] == user_id:
        return None, "Нельзя вступить в собственную семью по своей ссылке."
    async with get_db() as db:
        cur = await db.execute(
            """SELECT 1 FROM family_members fm JOIN families f ON f.id=fm.family_id
               WHERE fm.user_id=? AND f.status='active' LIMIT 1""", (user_id,)
        )
        if await cur.fetchone():
            return None, "Вы уже состоите в другой семье."
        cur = await db.execute(
            "SELECT COUNT(*) FROM family_members WHERE family_id=?", (invite["family_id"],)
        )
        if (await cur.fetchone())[0] >= MAX_FAMILY_MEMBERS:
            return None, "В этой семье уже два участника."
        cur = await db.execute(
            """SELECT id,status FROM family_join_requests WHERE family_id=? AND requester_id=?
               AND status IN ('awaiting_invitee','awaiting_owner') ORDER BY id DESC LIMIT 1""",
            (invite["family_id"], user_id),
        )
        existing = await cur.fetchone()
        if existing:
            if existing["status"] == "awaiting_owner":
                return None, "Вы уже подтвердили приглашение. Ожидайте решения владельца семьи."
            return {"request_id": existing["id"], **invite}, None
        now = datetime.now(timezone.utc)
        expires = min(datetime.fromisoformat(invite["expires_at"]), now + timedelta(hours=24)).isoformat()
        cur = await db.execute(
            """INSERT INTO family_join_requests(invite_id,family_id,requester_id,status,created_at,expires_at)
               VALUES(?,?,?,'awaiting_invitee',?,?)""",
            (invite["id"], invite["family_id"], user_id, now.isoformat(), expires),
        )
        request_id = cur.lastrowid
        await db.execute(
            "INSERT INTO activity_events(user_id,family_id,event_type,created_at) VALUES(?,?,?,?)",
            (user_id, invite["family_id"], "family_invite_opened", now.isoformat()),
        )
    return {"request_id": request_id, **invite}, None


async def confirm_invitee(request_id: int, user_id: int) -> tuple[dict | None, str | None]:
    async with get_db() as db:
        cur = await db.execute(
            """SELECT r.*, f.name AS family_name, f.status AS family_status,
                      fm.user_id AS owner_id, u.nickname AS owner_nickname, u.username AS owner_username
               FROM family_join_requests r JOIN families f ON f.id=r.family_id
               JOIN family_members fm ON fm.family_id=f.id
               JOIN users u ON u.user_id=fm.user_id
               WHERE r.id=? AND r.requester_id=? AND r.status='awaiting_invitee'""",
            (request_id, user_id),
        )
        row = await cur.fetchone()
        if not row:
            return None, "Запрос уже обработан или не найден."
        request = dict(row)
        if datetime.fromisoformat(request["expires_at"]) <= datetime.now(timezone.utc):
            await db.execute("UPDATE family_join_requests SET status='expired' WHERE id=?", (request_id,))
            return None, "Срок действия запроса истёк."
        await db.execute("UPDATE family_join_requests SET status='awaiting_owner' WHERE id=?", (request_id,))
        await db.execute(
            "INSERT INTO activity_events(user_id,family_id,event_type,created_at) VALUES(?,?,?,?)",
            (user_id, request["family_id"], "family_invitee_confirmed", now_iso()),
        )
        return request, None


async def decide_join_request(request_id: int, owner_id: int, approve: bool) -> tuple[dict | None, str | None]:
    async with get_db() as db:
        cur = await db.execute(
            """SELECT r.*, f.name AS family_name, f.status AS family_status,
                      i.id AS invite_id
               FROM family_join_requests r JOIN families f ON f.id=r.family_id
               JOIN family_invites i ON i.id=r.invite_id
               JOIN family_members fm ON fm.family_id=f.id AND fm.user_id=?
               WHERE r.id=? AND r.status='awaiting_owner'""", (owner_id, request_id)
        )
        row = await cur.fetchone()
        if not row:
            return None, "Запрос не найден или у вас нет прав."
        request = dict(row)
        if request["family_status"] != "active":
            return None, "Семья больше не активна."
        try:
            if datetime.fromisoformat(request["expires_at"]) <= datetime.now(timezone.utc):
                await db.execute("UPDATE family_join_requests SET status='expired' WHERE id=?", (request_id,))
                return None, "Срок действия запроса истёк."
        except (ValueError, TypeError):
            return None, "Некорректный срок действия запроса."
        cur = await db.execute("SELECT COUNT(*) FROM family_members WHERE family_id=?", (request["family_id"],))
        if (await cur.fetchone())[0] >= MAX_FAMILY_MEMBERS:
            return None, "В семье уже два участника."
        if approve:
            cur = await db.execute(
                """SELECT 1 FROM family_members fm JOIN families f ON f.id=fm.family_id
                   WHERE fm.user_id=? AND f.status='active' LIMIT 1""", (request["requester_id"],)
            )
            if await cur.fetchone():
                return None, "Приглашённый уже состоит в другой семье."
            await db.execute(
                "INSERT INTO family_members(family_id,user_id,role,joined_at) VALUES(?,?,'owner',?)",
                (request["family_id"], request["requester_id"], now_iso()),
            )
            await db.execute("UPDATE family_invites SET status='used' WHERE id=?", (request["invite_id"],))
            await db.execute("UPDATE family_join_requests SET status='approved',decided_at=? WHERE id=?", (now_iso(), request_id))
            await db.execute(
                "INSERT INTO activity_events(user_id,family_id,event_type,created_at) VALUES(?,?,?,?)",
                (request["requester_id"], request["family_id"], "family_joined", now_iso()),
            )
        else:
            await db.execute("UPDATE family_join_requests SET status='rejected',decided_at=? WHERE id=?", (now_iso(), request_id))
        return request, None


async def create_family_pet(family_id: int, creator_id: int, name: str, species: str) -> tuple[int | None, str | None]:
    name = (name or "Пушистик").strip()[:20]
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM family_members WHERE family_id=?", (family_id,)
        )
        if (await cur.fetchone())[0] != 2:
            return None, "Сначала оба участника должны подтвердить создание семьи."
        cur = await db.execute(
            "SELECT 1 FROM family_members WHERE family_id=? AND user_id=?", (family_id, creator_id)
        )
        if not await cur.fetchone():
            return None, "Вы не являетесь владельцем этой семьи."
        cur = await db.execute(
            "SELECT id FROM pets WHERE family_id=? AND is_archived=0 LIMIT 1", (family_id,)
        )
        if await cur.fetchone():
            return None, "У семьи уже есть общий питомец."
        code = make_code()
        now = now_iso()
        cur = await db.execute(
            """INSERT INTO pets(name,species,rarity,invite_code,created_at,last_tick,
               last_fed_at,last_played_at,last_washed_at,last_slept_at,family_id,ownership_mode)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,'family')""",
            (name, species, "Обычный", code, now, now, now, now, now, now, family_id),
        )
        pet_id = cur.lastrowid
        cur = await db.execute("SELECT user_id,username FROM family_members fm JOIN users u USING(user_id) WHERE family_id=?", (family_id,))
        for member in await cur.fetchall():
            await db.execute(
                "INSERT INTO members(pet_id,user_id,username,joined_at) VALUES(?,?,?,?)",
                (pet_id, member["user_id"], member["username"], now),
            )
        await db.execute(
            "INSERT INTO activity_events(user_id,family_id,pet_id,event_type,created_at) VALUES(?,?,?,?,?)",
            (creator_id, family_id, pet_id, "family_pet_created", now),
        )
        return pet_id, None


async def get_family_pet(family_id: int) -> dict | None:
    async with get_db() as db:
        cur = await db.execute(
            """SELECT p.* FROM pets p JOIN families f ON f.id=p.family_id
               WHERE p.family_id=? AND p.is_archived=0 AND p.ownership_mode='family'
               AND f.status='active' ORDER BY p.id DESC LIMIT 1""", (family_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def connect_family_to_chat(family_id: int, user_id: int, chat_id: int) -> tuple[bool, str]:
    async with get_db() as db:
        cur = await db.execute("SELECT 1 FROM family_members WHERE family_id=? AND user_id=?", (family_id, user_id))
        if not await cur.fetchone():
            return False, "Подключить семейного питомца может только владелец семьи."
        cur = await db.execute("SELECT id FROM pets WHERE family_id=? AND is_archived=0 LIMIT 1", (family_id,))
        pet = await cur.fetchone()
        if not pet:
            return False, "Сначала создайте общего питомца в меню семьи."
        await db.execute("INSERT OR IGNORE INTO pet_chats(pet_id,chat_id) VALUES(?,?)", (pet[0], chat_id))
        await db.execute(
            "INSERT INTO activity_events(user_id,family_id,pet_id,event_type,chat_id,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, family_id, pet[0], "family_pet_connected_to_group", chat_id, now_iso()),
        )
        return True, "Семейный питомец подключён к этой беседе."
