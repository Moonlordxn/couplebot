import asyncio
import logging
from datetime import datetime, timedelta, timezone
import aiosqlite
from config import DATABASE_PATH

log = logging.getLogger(__name__)
NEEDS = {
    "hunger": (30, "🍗 Я проголодался! Покормите меня, пожалуйста."),
    "energy": (0, "😴 Я совсем устал! Уложите меня спать на 3 часа."),
    "cleanliness": (30, "🫧 Я испачкался. Пора меня помыть!"),
    "happiness": (30, "🎾 Мне скучно! Давайте поиграем!"),
}
# Голод: 100 -> 0 за 5 часов. Энергия: 100 -> 0 за 8 часов.
DECAY = {"hunger": ("last_fed_at", 180), "happiness": ("last_played_at", 1200),
         "cleanliness": ("last_washed_at", 1800)}
COOLDOWN = timedelta(hours=3)

async def send_pet_need_reminders(bot):
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""SELECT p.* FROM pets p LEFT JOIN families f ON f.id=p.family_id
            WHERE p.is_archived=0 AND (p.ownership_mode='solo' OR (p.ownership_mode='family' AND f.status='active'))""")
        pets = await cur.fetchall()

    for pet in pets:
        pet_id = pet["id"]
        current = {k: int(pet[k]) for k in NEEDS}
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            sleeping = False
            if pet["sleeping_until"]:
                try:
                    wake_at = datetime.fromisoformat(pet["sleeping_until"])
                    if wake_at.tzinfo is None: wake_at = wake_at.replace(tzinfo=timezone.utc)
                    if now < wake_at:
                        sleeping = True
                    else:
                        stamp = now.isoformat()
                        current["energy"] = 100
                        current["hunger"] = 0
                        await db.execute("""UPDATE pets SET sleeping_until=NULL, energy=100, hunger=0,
                            last_slept_at=?, last_fed_at=?, last_tick=? WHERE id=?""",
                            (stamp, stamp, stamp, pet_id))
                except (ValueError, TypeError):
                    await db.execute("UPDATE pets SET sleeping_until=NULL WHERE id=?", (pet_id,))
            if not sleeping:
                for need, (stamp_field, seconds_per_point) in DECAY.items():
                    raw = pet[stamp_field] or pet["last_tick"] or pet["created_at"]
                    try:
                        stamp = datetime.fromisoformat(raw)
                        if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
                        points = min(100, max(0, int((now - stamp).total_seconds() // seconds_per_point)))
                    except (ValueError, TypeError):
                        points = 0; stamp = now
                    if points:
                        current[need] = max(0, current[need] - points)
                        advanced = (stamp + timedelta(seconds=points * seconds_per_point)).isoformat()
                        await db.execute(f"UPDATE pets SET {need}=?, {stamp_field}=? WHERE id=?",
                                         (current[need], advanced, pet_id))
                raw_energy_stamp = pet["last_slept_at"] or pet["last_tick"] or pet["created_at"]
                try:
                    energy_stamp = datetime.fromisoformat(raw_energy_stamp)
                    if energy_stamp.tzinfo is None: energy_stamp = energy_stamp.replace(tzinfo=timezone.utc)
                    energy_points = min(100, max(0, int((now - energy_stamp).total_seconds() // 288)))
                except (ValueError, TypeError):
                    energy_points = 0; energy_stamp = now
                if energy_points:
                    current["energy"] = max(0, current["energy"] - energy_points)
                    advanced = (energy_stamp + timedelta(seconds=energy_points * 288)).isoformat()
                    await db.execute("UPDATE pets SET energy=?, last_slept_at=? WHERE id=?",
                                     (current["energy"], advanced, pet_id))
            await db.execute("UPDATE pets SET last_tick=? WHERE id=?", (now.isoformat(), pet_id))
            due = []
            for need, (threshold, message) in NEEDS.items():
                if sleeping:
                    continue
                if current[need] > threshold:
                    await db.execute("DELETE FROM pet_need_notices WHERE pet_id=? AND need=?", (pet_id, need))
                    continue
                cur = await db.execute("SELECT last_notified FROM pet_need_notices WHERE pet_id=? AND need=?", (pet_id, need))
                notice = await cur.fetchone()
                if notice:
                    try:
                        last = datetime.fromisoformat(notice["last_notified"])
                        if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
                        if now - last < COOLDOWN: continue
                    except (ValueError, TypeError): pass
                await db.execute("""INSERT INTO pet_need_notices(pet_id,need,last_notified) VALUES(?,?,?)
                    ON CONFLICT(pet_id,need) DO UPDATE SET last_notified=excluded.last_notified""",
                    (pet_id, need, now.isoformat()))
                due.append(message)
            chats = set()
            if due:
                if pet["ownership_mode"] == "family":
                    cur = await db.execute("SELECT chat_id FROM pet_chats WHERE pet_id=?", (pet_id,))
                    chats.update(int(r[0]) for r in await cur.fetchall())
                cur = await db.execute("""SELECT DISTINCT pc.chat_id FROM private_chats pc
                    JOIN members m ON m.user_id=pc.user_id WHERE m.pet_id=?""", (pet_id,))
                chats.update(int(r[0]) for r in await cur.fetchall())
                # Ensure both family owners are targeted even if chat records were created via older code.
                if pet["family_id"]:
                    cur = await db.execute("SELECT user_id FROM family_members WHERE family_id=?", (pet["family_id"],))
                    owner_ids = [int(r[0]) for r in await cur.fetchall()]
                    for uid in owner_ids:
                        cur = await db.execute("SELECT chat_id FROM private_chats WHERE user_id=?", (uid,))
                        row = await cur.fetchone()
                        if row: chats.add(int(row[0]))
            await db.commit()
        if due:
            from html import escape
            body = f"🐾 <b>{escape(str(pet['name']))}</b> нуждается в заботе:\n" + "\n".join(f"• {m}" for m in due)
            for chat_id in chats:
                try: await bot.send_message(chat_id, body, parse_mode="HTML")
                except Exception as exc: log.debug("Reminder delivery failed to %s: %s", chat_id, exc)

async def reminder_loop(bot):
    while True:
        try: await send_pet_need_reminders(bot)
        except Exception: log.exception("Pet reminder loop failed")
        await asyncio.sleep(60)
