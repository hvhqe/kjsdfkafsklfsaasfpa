import asyncio
import os
import random
import time
import logging
from datetime import datetime
from typing import Callable, Dict, Any, Awaitable
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, BusinessMessagesDeleted, BusinessConnection,
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, TelegramObject
)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.utils.media_group import MediaGroupBuilder
from db import (
    init_db, save_message, get_message, get_media_group,
    update_message_text, upsert_connection,
    get_connections_for_owner, get_first_connection_date,
    set_auto_reply, stop_auto_reply, get_auto_reply, bump_stat,
    set_mute, remove_mute, get_mute,
    cleanup_expired_mutes, pop_expired_mutes,
    upsert_user, get_user, get_user_by_username, get_all_users,
    count_users, set_ban, is_banned, get_all_user_ids,
    get_all_connected_owner_ids,
    set_antimute, stop_antimute, is_antimute_enabled
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("aimstar-save")

BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY = os.getenv("PROXY")
ADMIN_IDS = set()
for x in os.getenv("ADMIN_IDS", "").split(","):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")


if PROXY:
    log.info(f"using proxy: {PROXY}")
    session = AiohttpSession(proxy=PROXY)
    bot = Bot(token=BOT_TOKEN, session=session)
else:
    bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()


LAUGH_SYLLABLES = ["ха", "хы", "ах", "ых", "фа", "фы", "аф", "ыф",
                   "за", "зы", "аз", "ыз", "ва", "вы", "ав", "ыв",
                   "хъ", "ъх", "фъ", "ъф"]

LAUGH_ENDING = ["ХА", "ХАХ", "ХАХА", "ХАХАХ", "АХАХ"]

RIGHTS_LABELS = {
    "can_read_messages": "Читать сообщения",
    "can_reply": "Отвечать на сообщения",
    "can_delete_sent_messages": "Удалять отправленные сообщения",
    "can_delete_all_messages": "Удалять все сообщения",
}


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


async def is_subscribed(uid: int) -> bool:
    if not CHANNEL_ID or not CHANNEL_LINK:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, uid)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning(f"subscription check failed for {uid}: {e}")
        return False


async def send_subscription_required(uid: int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="Проверить", callback_data="check_sub")]
    ])
    try:
        await bot.send_message(uid, "Подпишись на канал, чтобы пользоваться ботом.", reply_markup=kb)
    except Exception:
        pass


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if hasattr(event, "business_connection_id"):
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        target = None
        if isinstance(event, Message):
            target = event
        elif isinstance(event, CallbackQuery) and event.message:
            target = event.message

        if not target or not target.from_user:
            return await handler(event, data)

        if target.chat.type != "private":
            return await handler(event, data)

        uid = target.from_user.id

        if await is_subscribed(uid):
            return await handler(event, data)

        await send_subscription_required(uid)
        return None


def main_menu(connected: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="Профиль")],
        [KeyboardButton(text="Помощь")],
    ]
    if not connected:
        rows.append([KeyboardButton(text="Как подключить")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True
    )


async def is_connected(uid: int) -> bool:
    conns = await get_connections_for_owner(uid)
    return len(conns) > 0


def check_rights(rights) -> list:
    missing = []
    for field, label in RIGHTS_LABELS.items():
        if not getattr(rights, field, False):
            missing.append(label)
    return missing


def parse_duration(s: str) -> int:
    s = s.strip().lower()
    if not s:
        return 0
    unit = s[-1]
    num_part = s[:-1]
    if not num_part.isdigit():
        return 0
    n = int(num_part)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if unit not in multipliers:
        return 0
    return n * multipliers[unit]


def format_duration(seconds: int) -> str:
    if seconds >= 604800:
        return f"{seconds // 604800}н"
    if seconds >= 86400:
        return f"{seconds // 86400}д"
    if seconds >= 3600:
        return f"{seconds // 3600}ч"
    if seconds >= 60:
        return f"{seconds // 60}м"
    return f"{seconds}с"


def gen_pk_laugh(length: int = 18) -> str:
    result = []
    total = 0
    while total < length:
        syl = random.choice(LAUGH_SYLLABLES)
        result.append(syl)
        total += len(syl)
    text = "".join(result).upper()
    if random.random() < 0.4:
        text += random.choice(LAUGH_ENDING)
    return text


def extract_msg_data(msg: Message, conn_id: str, owner_id: int) -> dict:
    is_outgoing = bool(msg.from_user and msg.from_user.id == owner_id)

    if is_outgoing:
        peer_id = msg.chat.id
        peer_name = msg.chat.full_name or msg.chat.title or str(msg.chat.id)
        peer_username = msg.chat.username
    else:
        peer_id = msg.from_user.id if msg.from_user else msg.chat.id
        peer_name = msg.from_user.full_name if msg.from_user else str(msg.chat.id)
        peer_username = msg.from_user.username if msg.from_user else None

    data = {
        "business_connection_id": conn_id,
        "owner_id": owner_id,
        "chat_id": msg.chat.id,
        "message_id": msg.message_id,
        "from_user_id": peer_id,
        "from_user_name": peer_name,
        "from_username": peer_username,
        "is_outgoing": is_outgoing,
        "text": msg.text or msg.caption,
        "message_type": "text",
        "file_id": None,
        "media_group_id": msg.media_group_id,
        "has_protected_content": msg.has_protected_content,
        "date": int(msg.date.timestamp()),
    }

    if msg.photo:
        data["message_type"] = "photo"
        data["file_id"] = msg.photo[-1].file_id
    elif msg.video:
        data["message_type"] = "video"
        data["file_id"] = msg.video.file_id
    elif msg.document:
        data["message_type"] = "document"
        data["file_id"] = msg.document.file_id
    elif msg.voice:
        data["message_type"] = "voice"
        data["file_id"] = msg.voice.file_id
    elif msg.video_note:
        data["message_type"] = "video_note"
        data["file_id"] = msg.video_note.file_id
    elif msg.audio:
        data["message_type"] = "audio"
        data["file_id"] = msg.audio.file_id
    elif msg.sticker:
        data["message_type"] = "sticker"
        data["file_id"] = msg.sticker.file_id
    elif msg.animation:
        data["message_type"] = "animation"
        data["file_id"] = msg.animation.file_id

    return data


def build_header(row, title="Удалённое сообщение") -> str:
    if row["from_username"]:
        peer = f"@{row['from_username']}"
    elif row["from_user_id"]:
        peer = f"[{row['from_user_name'] or row['from_user_id']}](tg://user?id={row['from_user_id']})"
    else:
        peer = f"`{row['chat_id']}`"

    return (
        f"*{title}*\n"
        f"Чат: {peer}\n"
        f"ID: `{row['from_user_id'] or row['chat_id']}`"
    )


async def send_saved(owner_id: int, row, title="Удалённое сообщение"):
    header = build_header(row, title)
    mt = row["message_type"]
    fid = row["file_id"]
    txt = row["text"] or "(пусто)"

    try:
        if mt == "text":
            await bot.send_message(owner_id, header + f"\n\n{txt}", parse_mode="Markdown")
        elif mt == "photo":
            await bot.send_photo(owner_id, fid, caption=header, parse_mode="Markdown")
        elif mt == "video":
            await bot.send_video(owner_id, fid, caption=header, parse_mode="Markdown")
        elif mt == "document":
            await bot.send_document(owner_id, fid, caption=header, parse_mode="Markdown")
        elif mt == "voice":
            await bot.send_voice(owner_id, fid, caption=header, parse_mode="Markdown")
        elif mt == "video_note":
            await bot.send_video_note(owner_id, fid)
            await bot.send_message(owner_id, header, parse_mode="Markdown")
        elif mt == "audio":
            await bot.send_audio(owner_id, fid, caption=header, parse_mode="Markdown")
        elif mt == "sticker":
            await bot.send_sticker(owner_id, fid)
            await bot.send_message(owner_id, header, parse_mode="Markdown")
        elif mt == "animation":
            await bot.send_animation(owner_id, fid, caption=header, parse_mode="Markdown")
        else:
            await bot.send_message(owner_id, header + f"\n(тип: {mt})", parse_mode="Markdown")
    except Exception as e:
        log.exception(f"send_saved failed: {e}")


async def send_as_owner(conn_id: str, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            business_connection_id=conn_id
        )
        return True
    except Exception as e:
        log.exception(f"send_as_owner failed: {e}")
        return False


async def send_mute_card(conn_id: str, chat_id: int, secs: int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔴 Размутить",
            callback_data=f"unmute:{chat_id}"
        )]
    ])
    duration = format_duration(secs)
    text = (
        f"Вам выдан мут на: {duration}\n"
        f"Вы не можете писать в чат!\n\n"
        f"Лучший бот: @aimstarsavebot"
    )
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            business_connection_id=conn_id,
            reply_markup=kb
        )
        return sent.message_id
    except Exception as e:
        log.exception(f"send_mute_card failed: {e}")
        return None


async def delete_msg_as_owner(conn_id: str, chat_id: int, message_id: int):
    try:
        await bot.delete_business_messages(
            business_connection_id=conn_id,
            message_ids=[message_id]
        )
    except Exception as e:
        log.warning(f"delete_business_messages failed: {e}, fallback")
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e2:
            log.warning(f"delete_message fallback failed: {e2}")


async def mute_watcher():
    while True:
        try:
            expired = await pop_expired_mutes()
            for row in expired:
                owner_id = row["owner_id"]
                chat_id = row["chat_id"]
                card_id = row.get("card_message_id")
                conn_id = row.get("conn_id")
                log.info(f"mute expired [{owner_id}] chat={chat_id} card={card_id}")
                if card_id and conn_id:
                    try:
                        await bot.delete_business_messages(
                            business_connection_id=conn_id,
                            message_ids=[card_id]
                        )
                    except Exception as e:
                        log.warning(f"card delete failed: {e}")
                        try:
                            await bot.delete_message(chat_id=chat_id, message_id=card_id)
                        except Exception as e2:
                            log.warning(f"card delete fallback failed: {e2}")
        except Exception as e:
            log.exception(f"mute_watcher error: {e}")
        await asyncio.sleep(3)


@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if msg.chat.type != "private":
        return
    uid = msg.from_user.id
    await upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name)

    if await is_banned(uid):
        await msg.answer("Вы забанены.")
        return

    if not await is_subscribed(uid):
        await send_subscription_required(uid)
        return

    connected = await is_connected(uid)
    await msg.answer(
        "*aimstar - save*\n\n"
        "Сохраняю удалённые сообщения и даю команды прямо в чатах.\n\n"
        "Выбери пункт в меню ниже.",
        parse_mode="Markdown",
        reply_markup=main_menu(connected)
    )


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    uid = cb.from_user.id
    if await is_subscribed(uid):
        await cb.answer("Спасибо за подписку!")
        try:
            connected = await is_connected(uid)
            await bot.send_message(
                uid,
                "*aimstar - save*\n\n"
                "Сохраняю удалённые сообщения и даю команды прямо в чатах.\n\n"
                "Выбери пункт в меню ниже.",
                parse_mode="Markdown",
                reply_markup=main_menu(connected)
            )
        except Exception:
            pass
        try:
            await cb.message.delete()
        except Exception:
            pass
    else:
        await cb.answer("Ты ещё не подписался.", show_alert=True)


@dp.message(Command("auto"))
async def cmd_auto(msg: Message, command: CommandObject):
    if msg.chat.type != "private":
        return
    uid = msg.from_user.id
    await upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name)
    if await is_banned(uid):
        return
    if not command.args:
        await msg.answer("Использование: `/auto <текст>`", parse_mode="Markdown")
        return
    text = command.args
    await set_auto_reply(uid, text)
    await msg.answer(
        f"Автоответчик включён.\n\nТекст: `{text}`\n\nОтключить: `/autostop`",
        parse_mode="Markdown"
    )


@dp.message(Command("autostop"))
async def cmd_autostop(msg: Message):
    if msg.chat.type != "private":
        return
    uid = msg.from_user.id
    if await is_banned(uid):
        return
    await stop_auto_reply(uid)
    await msg.answer("Автоответчик выключен.")


@dp.message(Command("autostatus"))
async def cmd_autostatus(msg: Message):
    if msg.chat.type != "private":
        return
    uid = msg.from_user.id
    if await is_banned(uid):
        return
    auto = await get_auto_reply(uid)
    if not auto:
        await msg.answer(
            "Автоответчик выключен.\n\nВключить: `/auto <текст>`",
            parse_mode="Markdown"
        )
        return
    await msg.answer(
        f"Автоответчик включён.\n\nТекст: `{auto['text']}`\n\nОтключить: `/autostop`",
        parse_mode="Markdown"
    )


@dp.message(F.text == "Профиль")
async def menu_profile(msg: Message):
    if msg.chat.type != "private":
        return
    uid = msg.from_user.id
    await upsert_user(uid, msg.from_user.username or "", msg.from_user.full_name)

    if await is_banned(uid):
        await msg.answer("Вы забанены.")
        return

    connected = await is_connected(uid)
    conns = await get_connections_for_owner(uid)
    first_date = await get_first_connection_date(uid)
    dt = datetime.fromtimestamp(first_date).strftime("%d.%m.%Y %H:%M") if first_date else "—"

    rights_line = ""
    if conns:
        try:
            conn = await bot.get_business_connection(conns[0]["conn_id"])
            missing = check_rights(conn.rights) if conn.rights else list(RIGHTS_LABELS.values())
            if missing:
                rights_line = "\n\n*Не хватает прав:*\n" + "\n".join(f"• {m}" for m in missing)
            else:
                rights_line = "\n\nВсе права выданы"
        except Exception:
            rights_line = "\n\nНе удалось получить права"

    auto = await get_auto_reply(uid)
    auto_line = f"\n\nАвтоответчик: `{auto['text']}`" if auto else ""
    if await is_antimute_enabled(uid):
        auto_line += "\nАнти-мут: включён"

    adm_line = "\n\nadm" if is_admin(uid) else ""

    await msg.answer(
        f"*Твой профиль*\n\n"
        f"ID: `{uid}`\n"
        f"Имя: {msg.from_user.full_name}\n"
        f"Username: @{msg.from_user.username or '—'}\n"
        f"С ботом с: `{dt}`\n"
        f"Активных подключений: `{len(conns)}`"
        f"{rights_line}{auto_line}{adm_line}",
        parse_mode="Markdown",
        reply_markup=main_menu(connected)
    )


@dp.message(F.text == "Помощь")
async def menu_help(msg: Message):
    if msg.chat.type != "private":
        return
    uid = msg.from_user.id
    if await is_banned(uid):
        return
    connected = await is_connected(uid)

    base_text = (
        "*Помощь*\n\n"
        "*Команды в личке с ботом:*\n"
        "`/auto <текст>` — включить автоответчик\n"
        "`/autostop` — выключить автоответчик\n"
        "`/autostatus` — проверить статус автоответчика\n\n"
        "*Команды в чатах с собеседниками:*\n"
        "`.haha [n]` — n сообщений пк-смеха (по умолчанию 5, максимум 20)\n\n"
        "`.spam [n] [текст]` — n раз отправить твой текст\n\n"
        "`.dice` — кинуть кубик от твоего имени\n\n"
        "`.img` — ответь на медиа, копия придёт тебе в личку\n\n"
        "`.mute [срок]` — замутить собеседника (30s, 5m, 1h, 2d, 1w)\n\n"
        "`.unmute` — снять мут\n\n"
        "`.antimute` — вкл/выкл дублирование своих сообщений"
    )

    if is_admin(uid):
        base_text += (
            "\n\n*Админ-команды:*\n"
            "`/stats` — статистика бота\n"
            "`/users` — список юзеров\n"
            "`/user <id|@username>` — инфо по юзеру\n"
            "`/ban <id|@username> [причина]` — забанить\n"
            "`/unban <id|@username>` — разбанить\n"
            "`/bans` — список забаненных\n"
            "`/broadcast <текст>` — рассылка всем"
        )

    await msg.answer(
        base_text,
        parse_mode="Markdown",
        reply_markup=main_menu(connected)
    )


@dp.callback_query(F.data.startswith("unmute:"))
async def cb_unmute(cb: CallbackQuery):
    chat_id = int(cb.data.split(":")[1])
    uid = cb.from_user.id

    conns = await get_connections_for_owner(uid)
    if not conns:
        await cb.answer("Нет активных подключений", show_alert=True)
        return

    conn_id = conns[0]["conn_id"]
    mute = await get_mute(uid, chat_id)
    if not mute:
        await cb.answer("Мут уже снят или недействителен", show_alert=True)
        return

    card_id = mute["card_message_id"] if "card_message_id" in mute.keys() else None

    await remove_mute(uid, chat_id)
    await cb.answer("Мут снят", show_alert=True)

    if card_id:
        try:
            await bot.delete_business_messages(
                business_connection_id=conn_id,
                message_ids=[card_id]
            )
        except Exception as e:
            log.warning(f"card delete failed: {e}")
            try:
                await bot.delete_message(chat_id=chat_id, message_id=card_id)
            except Exception as e2:
                log.warning(f"card delete fallback failed: {e2}")

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.message(F.text == "Как подключить")
async def menu_connect(msg: Message):
    if msg.chat.type != "private":
        return
    uid = msg.from_user.id
    if await is_banned(uid):
        return
    if await is_connected(uid):
        return
    me = await bot.get_me()
    bot_username = me.username

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Скопировать",
                copy_text={"text": f"@{bot_username}"}
            ),
            InlineKeyboardButton(
                text="Подключить",
                url="tg://settings/edit"
            )
        ]
    ])

    await msg.answer(
        "*Подключи бота за минуту*\n\n"
        "*Для работы бота нужно подключить его к аккаунту:*\n\n"
        "1. Нажми *Скопировать*\n"
        "2. Затем добавь бота в *Автоматизация чатов*\n"
        "3. Для этого нажми *Подключить*\n"
        "4. Перейди в раздел *Автоматизация чатов*\n"
        f"5. Вставь скопированное имя бота — @{bot_username}\n\n"
        "_Если раздел «Автоматизация чатов» отсутствует, "
        "обнови Telegram до последней версии._",
        parse_mode="Markdown",
        reply_markup=kb
    )


@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.chat.type != "private" or not is_admin(msg.from_user.id):
        return
    c = await count_users()
    await msg.answer(
        f"*Статистика бота*\n\n"
        f"Юзеров всего: `{c['total']}`\n"
        f"Забанено: `{c['banned']}`\n"
        f"Активных подключений: `{c['connections']}`\n"
        f"Сообщений в БД: `{c['messages']}`",
        parse_mode="Markdown"
    )


@dp.message(Command("users"))
async def cmd_users(msg: Message):
    if msg.chat.type != "private" or not is_admin(msg.from_user.id):
        return
    users = await get_all_users()
    if not users:
        await msg.answer("Пользователей нет.")
        return

    connected_ids = await get_all_connected_owner_ids()

    lines = [f"*Юзеры бота ({len(users)}):*\n"]
    for u in users[:100]:
        conn_mark = "✅" if u["user_id"] in connected_ids else "❌"
        ban_mark = "🚫" if u["is_banned"] else ""
        un = f"@{u['username']}" if u["username"] else "—"
        parts = [conn_mark]
        if ban_mark:
            parts.append(ban_mark)
        parts.append(f"`{u['user_id']}`")
        parts.append(un)
        parts.append(f"— {u['full_name'] or '—'}")
        lines.append(" ".join(parts))
    if len(users) > 100:
        lines.append(f"\n...и ещё {len(users) - 100}")
    await msg.answer("\n".join(lines), parse_mode="Markdown")


async def resolve_target(arg: str):
    if not arg:
        return None
    arg = arg.strip()
    if arg.lstrip("-").isdigit():
        return await get_user(int(arg))
    if arg.startswith("@"):
        return await get_user_by_username(arg)
    return await get_user_by_username(arg)


@dp.message(Command("user"))
async def cmd_user(msg: Message, command: CommandObject):
    if msg.chat.type != "private" or not is_admin(msg.from_user.id):
        return
    if not command.args:
        await msg.answer("Использование: `/user <id|@username>`", parse_mode="Markdown")
        return
    u = await resolve_target(command.args.strip().split()[0])
    if not u:
        await msg.answer("Юзер не найден.")
        return
    conns = await get_connections_for_owner(u["user_id"])
    await msg.answer(
        f"*Юзер*\n\n"
        f"ID: `{u['user_id']}`\n"
        f"Username: @{u['username'] or '—'}\n"
        f"Имя: {u['full_name'] or '—'}\n"
        f"Забанен: `{'да' if u['is_banned'] else 'нет'}`\n"
        f"Причина бана: {u['ban_reason'] or '—'}\n"
        f"Активных подключений: `{len(conns)}`\n"
        f"Первый визит: `{datetime.fromtimestamp(u['first_seen']).strftime('%d.%m.%Y %H:%M')}`\n"
        f"Последний визит: `{datetime.fromtimestamp(u['last_seen']).strftime('%d.%m.%Y %H:%M')}`",
        parse_mode="Markdown"
    )


@dp.message(Command("ban"))
async def cmd_ban(msg: Message, command: CommandObject):
    if msg.chat.type != "private" or not is_admin(msg.from_user.id):
        return
    if not command.args:
        await msg.answer("Использование: `/ban <id|@username> [причина]`", parse_mode="Markdown")
        return
    parts = command.args.split(maxsplit=1)
    target = await resolve_target(parts[0])
    if not target:
        await msg.answer("Юзер не найден.")
        return
    if is_admin(target["user_id"]):
        await msg.answer("Нельзя забанить администратора.")
        return
    if target["is_banned"]:
        await msg.answer(f"`{target['user_id']}` уже забанен.")
        return
    reason = parts[1] if len(parts) > 1 else None
    await set_ban(target["user_id"], True, reason)
    await msg.answer(f"Забанен `{target['user_id']}` (@{target['username'] or '—'})")
    try:
        await bot.send_message(
            target["user_id"],
            f"Вы забанены в aimstar - save.\nПричина: {reason or 'не указана'}"
        )
    except Exception:
        pass


@dp.message(Command("unban"))
async def cmd_unban(msg: Message, command: CommandObject):
    if msg.chat.type != "private" or not is_admin(msg.from_user.id):
        return
    if not command.args:
        await msg.answer("Использование: `/unban <id|@username>`", parse_mode="Markdown")
        return
    target_arg = command.args.strip().split()[0]
    target = await resolve_target(target_arg)
    if not target:
        await msg.answer("Юзер не найден.")
        return
    if not target["is_banned"]:
        await msg.answer(f"`{target['user_id']}` не забанен.")
        return
    await set_ban(target["user_id"], False)
    await msg.answer(f"Разбанен `{target['user_id']}` (@{target['username'] or '—'})")
    try:
        await bot.send_message(target["user_id"], "Вы разбанены в aimstar - save.")
    except Exception:
        pass


@dp.message(Command("bans"))
async def cmd_bans(msg: Message):
    if msg.chat.type != "private" or not is_admin(msg.from_user.id):
        return
    users = await get_all_users()
    banned = [u for u in users if u["is_banned"]]
    if not banned:
        await msg.answer("Забаненных нет.")
        return
    lines = [f"*Забаненные ({len(banned)}):*\n"]
    for u in banned:
        lines.append(
            f"• `{u['user_id']}` @{u['username'] or '—'} — {u['ban_reason'] or 'без причины'}"
        )
    await msg.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("broadcast"))
async def cmd_broadcast(msg: Message, command: CommandObject):
    if msg.chat.type != "private" or not is_admin(msg.from_user.id):
        return
    if not command.args:
        await msg.answer("Использование: `/broadcast <текст>`", parse_mode="Markdown")
        return
    text = command.args
    user_ids = await get_all_user_ids()
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await msg.answer(f"Рассылка отправлена.\nУспешно: `{sent}`\nОшибок: `{failed}`",
                     parse_mode="Markdown")


@dp.business_connection()
async def on_business_connection(conn: BusinessConnection):
    owner_id = conn.user.id
    await upsert_connection(
        conn.id, owner_id,
        conn.user.full_name,
        conn.user.username or "",
        conn.is_enabled
    )
    await upsert_user(owner_id, conn.user.username or "", conn.user.full_name)

    if not conn.is_enabled:
        log.info(f"business disconnected: id={conn.id} owner={owner_id}")
        return

    log.info(f"business connected: id={conn.id} owner={owner_id}")

    if await is_banned(owner_id):
        log.warning(f"banned user connected: {owner_id}")
        return

    missing = check_rights(conn.rights) if conn.rights else list(RIGHTS_LABELS.values())

    if missing:
        text = (
            "*Недостаточно разрешений*\n\n"
            "Бот подключён, но не все права выданы.\n\n"
            "*Не хватает:*\n"
            + "\n".join(f"• {m}" for m in missing)
            + "\n\n*Как выдать:*\n"
            "Настройки → Telegram Business → Чат-боты → "
            "aimstarsavebot → включи все галочки."
        )
        log.warning(f"missing rights for {owner_id}: {missing}")
    else:
        text = (
            "*Бизнес-аккаунт подключён*\n\n"
            "Все необходимые права выданы."
        )

    try:
        await bot.send_message(
            owner_id, text,
            parse_mode="Markdown",
            reply_markup=main_menu(True)
        )
    except Exception as e:
        log.exception(f"failed to notify connect: {e}")


@dp.business_message()
async def on_business_message(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return

    if msg.from_user and msg.from_user.is_bot:
        return

    conn = await bot.get_business_connection(conn_id)
    owner_id = conn.user.id

    if await is_banned(owner_id):
        return

    is_outgoing = msg.from_user and msg.from_user.id == owner_id
    text = (msg.text or msg.caption or "").strip()

    if is_outgoing and text.startswith("."):
        await handle_dot_command(msg, text, conn_id, owner_id)
        return

    if not is_outgoing:
        mute = await get_mute(owner_id, msg.chat.id)
        if mute:
            if mute["until"] <= int(time.time()):
                await remove_mute(owner_id, msg.chat.id)
            else:
                await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
                return

        auto = await get_auto_reply(owner_id)
        if auto and auto["enabled"]:
            await asyncio.sleep(1)
            await send_as_owner(conn_id, msg.chat.id, auto["text"])

    if is_outgoing and text and not text.startswith("."):
        if await is_antimute_enabled(owner_id):
            await asyncio.sleep(0.1)
            try:
                await send_as_owner(conn_id, msg.chat.id, text)
            except Exception as e:
                log.exception(f"antimute dup failed: {e}")

    if (not is_outgoing) and msg.has_protected_content:
        return

    data = extract_msg_data(msg, conn_id, owner_id)
    await save_message(data)


async def handle_dot_command(msg: Message, text: str, conn_id: str, owner_id: int):
    parts = text.split(maxsplit=2)
    cmd = parts[0].lower()

    if cmd == ".haha":
        n = 5
        if len(parts) > 1 and parts[1].strip().isdigit():
            n = max(1, min(int(parts[1].strip()), 20))
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        for _ in range(n):
            ok = await send_as_owner(conn_id, msg.chat.id, gen_pk_laugh(random.randint(14, 22)))
            if not ok:
                break
            await asyncio.sleep(0.15)
        return

    if cmd == ".spam":
        n = 5
        spam_text = ""
        if len(parts) >= 3:
            if parts[1].strip().isdigit():
                n = max(1, min(int(parts[1].strip()), 20))
                spam_text = parts[2]
            else:
                spam_text = " ".join(parts[1:])
        elif len(parts) == 2:
            spam_text = parts[1]
        if not spam_text:
            await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
            return
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        for _ in range(n):
            ok = await send_as_owner(conn_id, msg.chat.id, spam_text)
            if not ok:
                break
            await asyncio.sleep(0.15)
        return

    if cmd == ".dice":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        try:
            await bot.send_dice(msg.chat.id, business_connection_id=conn_id)
        except Exception as e:
            log.exception(f"dice failed: {e}")
        return

    if cmd == ".img":
        if not msg.reply_to_message:
            await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
            return
        replied = msg.reply_to_message
        file_id = None
        mtype = None
        if replied.photo:
            file_id = replied.photo[-1].file_id
            mtype = "photo"
        elif replied.video:
            file_id = replied.video.file_id
            mtype = "video"
        elif replied.video_note:
            file_id = replied.video_note.file_id
            mtype = "video_note"
        elif replied.voice:
            file_id = replied.voice.file_id
            mtype = "voice"
        elif replied.document:
            file_id = replied.document.file_id
            mtype = "document"
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not file_id:
            return
        header = (
            f"*Секретное медиа*\n"
            f"Чат: `{msg.chat.id}`\n"
            f"Тип: `{mtype}`"
        )
        try:
            if mtype == "photo":
                await bot.send_photo(owner_id, file_id, caption=header, parse_mode="Markdown")
            elif mtype == "video":
                await bot.send_video(owner_id, file_id, caption=header, parse_mode="Markdown")
            elif mtype == "video_note":
                await bot.send_video_note(owner_id, file_id)
                await bot.send_message(owner_id, header, parse_mode="Markdown")
            elif mtype == "voice":
                await bot.send_voice(owner_id, file_id, caption=header, parse_mode="Markdown")
            elif mtype == "document":
                await bot.send_document(owner_id, file_id, caption=header, parse_mode="Markdown")
        except Exception as e:
            log.exception(f"img failed: {e}")
        return

    if cmd == ".mute":
        arg = parts[1] if len(parts) > 1 else ""
        secs = parse_duration(arg)
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if secs <= 0:
            try:
                await bot.send_message(
                    owner_id,
                    "Укажи срок мута. Пример: `.mute 1h`\n\n"
                    "Доступные единицы:\n"
                    "• `s` — секунды\n"
                    "• `m` — минуты\n"
                    "• `h` — часы\n"
                    "• `d` — дни\n"
                    "• `w` — недели",
                    parse_mode="Markdown"
                )
            except Exception as e:
                log.exception(f"mute hint failed: {e}")
            return
        until = int(time.time()) + secs
        card_id = await send_mute_card(conn_id, msg.chat.id, secs)
        await set_mute(owner_id, msg.chat.id, until, card_id)
        log.info(f".mute [{owner_id}] chat={msg.chat.id} secs={secs} card={card_id}")
        return

    if cmd == ".unmute":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        mute = await get_mute(owner_id, msg.chat.id)
        card_id = None
        if mute and "card_message_id" in mute.keys():
            card_id = mute["card_message_id"]
        await remove_mute(owner_id, msg.chat.id)
        if card_id:
            try:
                await bot.delete_business_messages(
                    business_connection_id=conn_id,
                    message_ids=[card_id]
                )
            except Exception as e:
                log.warning(f"card delete failed: {e}")
                try:
                    await bot.delete_message(chat_id=msg.chat.id, message_id=card_id)
                except Exception as e2:
                    log.warning(f"card delete fallback failed: {e2}")
        log.info(f".unmute [{owner_id}] chat={msg.chat.id}")
        return

    if cmd == ".antimute":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if await is_antimute_enabled(owner_id):
            await stop_antimute(owner_id)
            await send_as_owner(conn_id, msg.chat.id, "Анти-мут выключен.")
        else:
            await set_antimute(owner_id)
            await send_as_owner(conn_id, msg.chat.id, "Анти-мут включён.")
        return


@dp.edited_business_message()
async def on_edited_business_message(msg: Message):
    conn_id = msg.business_connection_id
    if not conn_id:
        return

    conn = await bot.get_business_connection(conn_id)
    owner_id = conn.user.id

    if await is_banned(owner_id):
        return

    row = await get_message(msg.chat.id, msg.message_id)
    old_text = row["text"] if row else "(нет в базе)"

    new_text = msg.text or msg.caption or "(пусто)"
    await update_message_text(msg.chat.id, msg.message_id, new_text)
    await bump_stat(owner_id, msg.chat.id, "edited_count")

    header = (
        f"*Сообщение отредактировано*\n"
        f"Чат: `{msg.chat.id}`\n"
        f"ID: `{msg.message_id}`"
    )
    try:
        await bot.send_message(
            owner_id,
            header + f"\n\n*Было:*\n{old_text}\n\n*Стало:*\n{new_text}",
            parse_mode="Markdown"
        )
    except Exception as e:
        log.exception(f"failed to notify edit: {e}")


@dp.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted):
    conn_id = event.business_connection_id
    conn = await bot.get_business_connection(conn_id)
    owner_id = conn.user.id

    if await is_banned(owner_id):
        return

    chat_id = event.chat.id

    albums: dict = {}
    singles: list = []

    for msg_id in event.message_ids:
        row = await get_message(chat_id, msg_id)
        if not row:
            continue
        if row["is_outgoing"]:
            continue
        if row["media_group_id"]:
            albums.setdefault(row["media_group_id"], []).append(msg_id)
        else:
            singles.append(msg_id)

    for mg_id, _ in albums.items():
        group = await get_media_group(mg_id)
        if not group:
            continue
        try:
            builder = MediaGroupBuilder(
                caption=build_header(group[0], title="Удалённый альбом")
            )
            for row in group:
                if row["message_type"] == "photo":
                    builder.add_photo(media=row["file_id"])
                elif row["message_type"] == "video":
                    builder.add_video(media=row["file_id"])
                elif row["message_type"] == "document":
                    builder.add_document(media=row["file_id"])

            if builder._media:
                await bot.send_media_group(owner_id, media=builder.build())
        except Exception as e:
            log.exception(f"album send failed: {e}")
        await bump_stat(owner_id, chat_id, "deleted_count")
        await asyncio.sleep(0.3)

    for msg_id in singles:
        row = await get_message(chat_id, msg_id)
        if row:
            await send_saved(owner_id, row, title="Удалённое сообщение")
            await bump_stat(owner_id, chat_id, "deleted_count")
            await asyncio.sleep(0.3)


async def main():
    await init_db()
    await cleanup_expired_mutes()
    log.info(f"db ready, admins: {ADMIN_IDS}")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"delete_webhook failed: {e}")

    dp.message.middleware(SubscriptionMiddleware())
    dp.callback_query.middleware(SubscriptionMiddleware())

    asyncio.create_task(mute_watcher())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())