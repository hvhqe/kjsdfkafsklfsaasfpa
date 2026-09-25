import aiosqlite
import time
from pathlib import Path

DB_PATH = Path("archiver.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                from_user_id INTEGER,
                from_user_name TEXT,
                from_username TEXT,
                is_outgoing INTEGER DEFAULT 0,
                text TEXT,
                message_type TEXT,
                file_id TEXT,
                media_group_id TEXT,
                has_protected_content INTEGER DEFAULT 0,
                date INTEGER,
                saved_at INTEGER DEFAULT (strftime('%s', 'now')),
                UNIQUE(chat_id, message_id)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_msg ON messages(chat_id, message_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_owner ON messages(owner_id)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                conn_id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                owner_name TEXT,
                owner_username TEXT,
                is_enabled INTEGER DEFAULT 1,
                connected_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conn_owner ON connections(owner_id)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS auto_replies (
                owner_id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS antimutes (
                owner_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                owner_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                deleted_count INTEGER DEFAULT 0,
                edited_count INTEGER DEFAULT 0,
                PRIMARY KEY (owner_id, chat_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS mutes (
                owner_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                until INTEGER NOT NULL,
                card_message_id INTEGER,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                PRIMARY KEY (owner_id, chat_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                first_seen INTEGER DEFAULT (strftime('%s', 'now')),
                last_seen INTEGER DEFAULT (strftime('%s', 'now')),
                is_banned INTEGER DEFAULT 0,
                ban_reason TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_langs (
                user_id INTEGER PRIMARY KEY,
                lang TEXT DEFAULT 'ru',
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        await db.commit()


async def save_message(data: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO messages
            (business_connection_id, owner_id, chat_id, message_id,
             from_user_id, from_user_name, from_username, is_outgoing,
             text, message_type, file_id, media_group_id,
             has_protected_content, date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["business_connection_id"],
            data["owner_id"],
            data["chat_id"],
            data["message_id"],
            data.get("from_user_id"),
            data.get("from_user_name"),
            data.get("from_username"),
            1 if data.get("is_outgoing") else 0,
            data.get("text"),
            data.get("message_type"),
            data.get("file_id"),
            data.get("media_group_id"),
            1 if data.get("has_protected_content") else 0,
            data.get("date"),
        ))
        await db.commit()


async def get_message(chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        ) as cur:
            return await cur.fetchone()


async def get_media_group(media_group_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM messages WHERE media_group_id = ? ORDER BY message_id",
            (media_group_id,)
        ) as cur:
            return await cur.fetchall()


async def update_message_text(chat_id: int, message_id: int, new_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE messages SET text = ? WHERE chat_id = ? AND message_id = ?",
            (new_text, chat_id, message_id)
        )
        await db.commit()


async def upsert_connection(conn_id: str, owner_id: int, owner_name: str,
                            owner_username: str, is_enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO connections (conn_id, owner_id, owner_name, owner_username, is_enabled)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(conn_id) DO UPDATE SET
                is_enabled = excluded.is_enabled,
                owner_name = excluded.owner_name,
                owner_username = excluded.owner_username
        """, (conn_id, owner_id, owner_name, owner_username, 1 if is_enabled else 0))
        await db.commit()


async def get_connections_for_owner(owner_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM connections WHERE owner_id = ? AND is_enabled = 1",
            (owner_id,)
        ) as cur:
            return await cur.fetchall()


async def get_first_connection_date(owner_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT MIN(connected_at) FROM connections WHERE owner_id = ?",
            (owner_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_all_connected_owner_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT owner_id FROM connections WHERE is_enabled = 1"
        ) as cur:
            return {r[0] for r in await cur.fetchall()}


async def set_auto_reply(owner_id: int, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO auto_replies (owner_id, text, enabled)
            VALUES (?, ?, 1)
            ON CONFLICT(owner_id) DO UPDATE SET
                text = excluded.text,
                enabled = 1,
                updated_at = strftime('%s', 'now')
        """, (owner_id, text))
        await db.commit()


async def stop_auto_reply(owner_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE auto_replies SET enabled = 0 WHERE owner_id = ?",
            (owner_id,)
        )
        await db.commit()


async def get_auto_reply(owner_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM auto_replies WHERE owner_id = ? AND enabled = 1",
            (owner_id,)
        ) as cur:
            return await cur.fetchone()


async def set_antimute(owner_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO antimutes (owner_id, enabled)
            VALUES (?, 1)
            ON CONFLICT(owner_id) DO UPDATE SET
                enabled = 1,
                updated_at = strftime('%s', 'now')
        """, (owner_id,))
        await db.commit()


async def stop_antimute(owner_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE antimutes SET enabled = 0 WHERE owner_id = ?",
            (owner_id,)
        )
        await db.commit()


async def is_antimute_enabled(owner_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT enabled FROM antimutes WHERE owner_id = ? AND enabled = 1",
            (owner_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row)


async def bump_stat(owner_id: int, chat_id: int, field: str):
    if field not in ("deleted_count", "edited_count"):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"""
            INSERT INTO stats (owner_id, chat_id, {field})
            VALUES (?, ?, 1)
            ON CONFLICT(owner_id, chat_id) DO UPDATE SET
                {field} = {field} + 1
        """, (owner_id, chat_id))
        await db.commit()


async def set_mute(owner_id: int, chat_id: int, until: int, card_message_id: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO mutes (owner_id, chat_id, until, card_message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(owner_id, chat_id) DO UPDATE SET
                until = excluded.until,
                card_message_id = excluded.card_message_id
        """, (owner_id, chat_id, until, card_message_id))
        await db.commit()


async def remove_mute(owner_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM mutes WHERE owner_id = ? AND chat_id = ?",
            (owner_id, chat_id)
        )
        await db.commit()


async def get_mute(owner_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM mutes WHERE owner_id = ? AND chat_id = ?",
            (owner_id, chat_id)
        ) as cur:
            return await cur.fetchone()


async def cleanup_expired_mutes():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM mutes WHERE until < ?", (int(time.time()),))
        await db.commit()


async def pop_expired_mutes():
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT m.owner_id, m.chat_id, m.card_message_id, c.conn_id
            FROM mutes m
            LEFT JOIN connections c ON c.owner_id = m.owner_id AND c.is_enabled = 1
            WHERE m.until <= ?
        """, (now,)) as cur:
            rows = await cur.fetchall()
        if rows:
            await db.execute("DELETE FROM mutes WHERE until <= ?", (now,))
            await db.commit()
    return [dict(r) for r in rows]


async def upsert_user(user_id: int, username: str, full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                last_seen = strftime('%s', 'now')
        """, (user_id, username, full_name))
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()


async def get_user_by_username(username: str):
    username = username.lstrip("@").lower()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE LOWER(username) = ?", (username,)
        ) as cur:
            return await cur.fetchone()


async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users ORDER BY first_seen DESC"
        ) as cur:
            return await cur.fetchall()


async def count_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1") as cur:
            banned = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM connections WHERE is_enabled = 1") as cur:
            conns = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM messages") as cur:
            msgs = (await cur.fetchone())[0]
    return {"total": total, "banned": banned, "connections": conns, "messages": msgs}


async def set_ban(user_id: int, banned: bool, reason: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_banned = ?, ban_reason = ? WHERE user_id = ?",
            (1 if banned else 0, reason if banned else None, user_id)
        )
        await db.commit()


async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_banned FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row[0])


async def get_all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_banned = 0") as cur:
            return [r[0] for r in await cur.fetchall()]


async def set_user_lang(user_id: int, lang: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO user_langs (user_id, lang)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                lang = excluded.lang,
                updated_at = strftime('%s', 'now')
        """, (user_id, lang))
        await db.commit()


async def get_user_lang(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT lang FROM user_langs WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else "ru"