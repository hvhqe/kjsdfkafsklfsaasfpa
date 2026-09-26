import asyncio
import os
import io
import random
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Callable, Dict, Any, Awaitable
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, BusinessMessagesDeleted, BusinessConnection,
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, TelegramObject, BufferedInputFile
)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.utils.media_group import MediaGroupBuilder
from PIL import Image, ImageDraw, ImageFont
import google.generativeai as genai
from deep_translator import GoogleTranslator
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
    set_antimute, stop_antimute, is_antimute_enabled,
    set_swmute, remove_swmute, is_swmuted,
    queue_swmute_delete, pop_swmute_queue
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("aimstar-save")

BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY = os.getenv("PROXY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_IDS = set()
for x in os.getenv("ADMIN_IDS", "").split(","):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")

MSK = ZoneInfo("Europe/Moscow")

if GEMINI_KEY:
    try:
        genai.configure(api_key=GEMINI_KEY)
        gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    except Exception as e:
        log.warning(f"gemini init failed: {e}")
        gemini_model = None
else:
    gemini_model = None


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

MEMES = [
    "https://i.imgflip.com/1bij.jpg",
    "https://i.imgflip.com/1bip.jpg",
    "https://i.imgflip.com/1bgw.jpg",
    "https://i.imgflip.com/1bh3.jpg",
    "https://i.imgflip.com/1bhf.jpg",
    "https://i.imgflip.com/1bhm.jpg",
    "https://i.imgflip.com/1bik.jpg",
    "https://i.imgflip.com/26am.jpg",
    "https://i.imgflip.com/1otk96.jpg",
]

LEET_MAP = {
    "а": "4", "б": "6", "в": "8", "г": "9", "д": "d", "е": "3",
    "з": "3", "и": "u", "к": "k", "л": "l", "м": "m", "н": "h",
    "о": "0", "п": "n", "р": "p", "с": "s", "т": "7", "у": "y",
    "ф": "f", "х": "x", "ц": "c", "ч": "4", "ш": "w", "щ": "w",
    "ъ": "", "ы": "b", "ь": "", "э": "3", "ю": "10", "я": "9",
}

RIGHTS_LABELS = {
    "can_read_messages": "Читать сообщения",
    "can_reply": "Отвечать на сообщения",
    "can_delete_sent_messages": "Удалять отправленные сообщения",
    "can_delete_all_messages": "Удалять все сообщения",
}

SW_MAP = {
    "q": "й", "w": "ц", "e": "у", "r": "к", "t": "е", "y": "н", "u": "г",
    "i": "ш", "o": "щ", "p": "з", "[": "х", "]": "ъ", "a": "ф", "s": "ы",
    "d": "в", "f": "а", "g": "п", "h": "р", "j": "о", "k": "л", "l": "д",
    ";": "ж", "'": "э", "z": "я", "x": "ч", "c": "с", "v": "м", "b": "и",
    "n": "т", "m": "ь", ",": "б", ".": "ю",
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t", "н": "y", "г": "u",
    "ш": "i", "щ": "o", "з": "p", "х": "[", "ъ": "]", "ф": "a", "ы": "s",
    "в": "d", "а": "f", "п": "g", "р": "h", "о": "j", "л": "k", "д": "l",
    "ж": ";", "э": "'", "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b",
    "т": "n", "ь": "m", "б": ",", "ю": ".",
}


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def to_leet(text: str) -> str:
    return "".join(LEET_MAP.get(ch, ch) for ch in text.lower())


def to_sw(text: str) -> str:
    return "".join(SW_MAP.get(ch, ch) for ch in text)


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


async def send_as_owner(conn_id: str, chat_id: int, text: str, parse_mode: str = None) -> bool:
    try:
        kwargs = {
            "chat_id": chat_id,
            "text": text,
            "business_connection_id": conn_id,
        }
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        await bot.send_message(**kwargs)
        return True
    except Exception as e:
        log.exception(f"send_as_owner failed: {e}")
        return False


async def send_photo_as_owner(conn_id: str, chat_id: int, photo, caption: str = None):
    try:
        kwargs = {
            "chat_id": chat_id,
            "photo": photo,
            "business_connection_id": conn_id,
        }
        if caption:
            kwargs["caption"] = caption
        await bot.send_photo(**kwargs)
        return True
    except Exception as e:
        log.exception(f"send_photo_as_owner failed: {e}")
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


async def download_photo_as_bytes(file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    return buf.getvalue()


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


async def swmute_watcher():
    while True:
        try:
            batch = await pop_swmute_queue()
            if batch:
                grouped: dict = {}
                for item in batch:
                    key = (item["conn_id"], item["chat_id"])
                    grouped.setdefault(key, []).append(item["message_id"])

                for (conn_id, chat_id), ids in grouped.items():
                    for i in range(0, len(ids), 100):
                        chunk = ids[i:i+100]
                        try:
                            await bot.delete_business_messages(
                                business_connection_id=conn_id,
                                message_ids=chunk
                            )
                        except Exception as e:
                            log.warning(f"swmute batch delete failed: {e}")
        except Exception as e:
            log.exception(f"swmute_watcher error: {e}")
        await asyncio.sleep(0.3)


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
    dt = datetime.fromtimestamp(first_date, MSK).strftime("%d.%m.%Y %H:%M") if first_date else "—"

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
        "*В личке с ботом:*\n"
        "`/auto <текст>` — включить автоответчик\n"
        "`/autostop` — выключить автоответчик\n"
        "`/autostatus` — статус автоответчика\n\n"
        "*В чатах с собеседниками:*\n"
        "*Спам:*\n"
        "`.haha [n]` — n сообщений пк-смеха\n"
        "`.spam [n] [текст]` — n раз текст\n"
        "`.flood <текст>` — каждое слово отдельно\n"
        "`.type <текст>` — по одному слову с задержкой\n\n"
        "*Текст:*\n"
        "`.rev <текст>` — задом наперёд\n"
        "`.leet <текст>` — l33t\n"
        "`.sw <текст>` — смена раскладки\n"
        "`.bold <текст>` — *жирный*\n"
        "`.italic <текст>` — _курсив_\n"
        "`.mono <текст>` — `моноширинный`\n"
        "`.line <текст>` — подчёркнутый\n"
        "`.crossed <текст>` — зачёркнутый\n"
        "`.hidden <текст>` — скрытый\n"
        "`.quote <текст>` — цитата\n"
        "`.code <текст>` — блок кода\n\n"
        "*Утилиты:*\n"
        "`.ai <вопрос>` — спросить Gemini\n"
        "`.tl <текст>` — перевести на русский\n"
        "`.short <url>` — сократить ссылку\n"
        "`.info` — инфо о собеседнике\n\n"
        "*Игры:*\n"
        "`.rps` — камень-ножницы-бумага\n"
        "`.flip` — орёл/решка\n"
        "`.duel` — дуэль\n"
        "`.xox` — крестики-нолики\n"
        "`.streak` — серия\n\n"
        "*Медиа:*\n"
        "`.meme` — случайный мем\n"
        "`.wtm <текст>` — водяной знак (ответь на фото)\n"
        "`.memz <текст>` — чёрные поля с текстом (ответь на фото)\n\n"
        "*Модерация:*\n"
        "`.mute [срок]` — мут на время\n"
        "`.swmute` — постоянный мут\n"
        "`.unmute` — снять мут\n"
        "`.antimute` — дублирование своих сообщений\n"
        "`.dice` — кубик\n"
        "`.img` — копия медиа в личку"
    )

    if is_admin(uid):
        base_text += (
            "\n\n*Админ-команды:*\n"
            "`/stats` — статистика\n"
            "`/users` — список юзеров\n"
            "`/user <id|@username>` — инфо\n"
            "`/ban <id|@username> [причина]` — забанить\n"
            "`/unban <id|@username>` — разбанить\n"
            "`/bans` — забаненные\n"
            "`/broadcast <текст>` — рассылка"
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
    await remove_swmute(uid, chat_id)
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
        f"Первый визит: `{datetime.fromtimestamp(u['first_seen'], MSK).strftime('%d.%m.%Y %H:%M')}`\n"
        f"Последний визит: `{datetime.fromtimestamp(u['last_seen'], MSK).strftime('%d.%m.%Y %H:%M')}`",
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
        if await is_swmuted(owner_id, msg.chat.id):
            await queue_swmute_delete(owner_id, msg.chat.id, msg.message_id, conn_id)
            return

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

    # ---------- спам ----------

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

    if cmd == ".flood":
        arg = text[len(".flood"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        for w in arg.split():
            ok = await send_as_owner(conn_id, msg.chat.id, w)
            if not ok:
                break
            await asyncio.sleep(0.15)
        return

    if cmd == ".type":
        arg = text[len(".type"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        for w in arg.split():
            ok = await send_as_owner(conn_id, msg.chat.id, w)
            if not ok:
                break
            await asyncio.sleep(0.7)
        return

    # ---------- текст ----------

    if cmd == ".rev":
        arg = text[len(".rev"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, arg[::-1])
        return

    if cmd == ".leet":
        arg = text[len(".leet"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, to_leet(arg))
        return

    if cmd == ".sw":
        arg = text[len(".sw"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, to_sw(arg))
        return

    if cmd == ".bold":
        arg = text[len(".bold"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, f"*{arg}*", parse_mode="Markdown")
        return

    if cmd == ".italic":
        arg = text[len(".italic"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, f"_{arg}_", parse_mode="Markdown")
        return

    if cmd == ".mono":
        arg = text[len(".mono"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, f"`{arg}`", parse_mode="Markdown")
        return

    if cmd == ".line":
        arg = text[len(".line"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, f"__{arg}__", parse_mode="Markdown")
        return

    if cmd == ".crossed":
        arg = text[len(".crossed"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, f"~{arg}~", parse_mode="Markdown")
        return

    if cmd == ".hidden":
        arg = text[len(".hidden"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, f"||{arg}||", parse_mode="Markdown")
        return

    if cmd == ".quote":
        arg = text[len(".quote"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, f"> {arg}", parse_mode="Markdown")
        return

    if cmd == ".code":
        arg = text[len(".code"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        await send_as_owner(conn_id, msg.chat.id, f"```\n{arg}\n```", parse_mode="Markdown")
        return

    # ---------- утилиты ----------

    if cmd == ".ai":
        arg = text[len(".ai"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        if not gemini_model:
            await bot.send_message(owner_id, "Gemini не настроен (нет GEMINI_API_KEY).")
            return
        try:
            resp = await asyncio.to_thread(gemini_model.generate_content, arg)
            answer = (resp.text or "").strip()
            if not answer:
                answer = "(пусто)"
            if len(answer) > 4000:
                answer = answer[:4000] + "..."
            await send_as_owner(conn_id, msg.chat.id, answer)
        except Exception as e:
            log.exception(f".ai failed: {e}")
            await bot.send_message(owner_id, f"Ошибка Gemini: {e}")
        return

    if cmd == ".tl":
        arg = text[len(".tl"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        try:
            translated = await asyncio.to_thread(
                GoogleTranslator(source="auto", target="ru").translate, arg
            )
            await send_as_owner(conn_id, msg.chat.id, translated)
        except Exception as e:
            log.exception(f".tl failed: {e}")
            await bot.send_message(owner_id, f"Ошибка перевода: {e}")
        return

    if cmd == ".short":
        arg = text[len(".short"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not arg:
            return
        try:
            import urllib.parse
            import urllib.request
            url = f"https://is.gd/create.php?format=simple&url={urllib.parse.quote(arg)}"
            with urllib.request.urlopen(url, timeout=5) as r:
                short = r.read().decode()
            await send_as_owner(conn_id, msg.chat.id, short)
        except Exception as e:
            log.exception(f".short failed: {e}")
            await bot.send_message(owner_id, f"Ошибка сокращения: {e}")
        return

    if cmd == ".info":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        peer_name = msg.chat.full_name or msg.chat.title or "—"
        peer_username = f"@{msg.chat.username}" if msg.chat.username else "—"
        peer_id = msg.chat.id
        text_out = (
            f"Инфо о собеседнике:\n"
            f"ID: {peer_id}\n"
            f"Имя: {peer_name}\n"
            f"Username: {peer_username}"
        )
        await bot.send_message(owner_id, text_out)
        return

    # ---------- игры ----------

    if cmd == ".rps":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        choice = random.choice(["камень", "ножницы", "бумага"])
        await send_as_owner(conn_id, msg.chat.id, f"🪨✂️📄 {choice}")
        return

    if cmd == ".flip":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        result = random.choice(["орёл", "решка"])
        await send_as_owner(conn_id, msg.chat.id, f"🪙 {result}")
        return

    if cmd == ".duel":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        result = random.choice([
            "ты выжил. противник убит",
            "ты убит. противник выжил",
            "оба выжили",
            "оба убиты",
        ])
        await send_as_owner(conn_id, msg.chat.id, f"🔫 {result}")
        return

    if cmd == ".xox":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        result = random.choice(["X победил", "O победил", "ничья"])
        await send_as_owner(conn_id, msg.chat.id, f"❌⭕ {result}")
        return

    if cmd == ".streak":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        n = random.randint(1, 100)
        await send_as_owner(conn_id, msg.chat.id, f"🔥 серия: {n}")
        return

    # ---------- медиа ----------

    if cmd == ".meme":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        try:
            url = random.choice(MEMES)
            await bot.send_photo(
                chat_id=msg.chat.id,
                photo=url,
                business_connection_id=conn_id
            )
        except Exception as e:
            log.exception(f"meme failed: {e}")
        return

    if cmd == ".wtm":
        arg = text[len(".wtm"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not msg.reply_to_message or not msg.reply_to_message.photo:
            await bot.send_message(owner_id, "Ответь на фото командой .wtm <текст>")
            return
        if not arg:
            return
        try:
            raw = await download_photo_as_bytes(msg.reply_to_message.photo[-1].file_id)
            img = Image.open(io.BytesIO(raw)).convert("RGBA")
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(20, img.width // 20))
            except Exception:
                font = ImageFont.load_default()
            w, h = img.size
            bbox = draw.textbbox((0, 0), arg, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            x = w - tw - 20
            y = h - th - 30
            draw.text((x+2, y+2), arg, font=font, fill=(0, 0, 0, 200))
            draw.text((x, y), arg, font=font, fill=(255, 255, 255, 255))
            out = io.BytesIO()
            img.convert("RGB").save(out, format="JPEG", quality=85)
            out.seek(0)
            file = BufferedInputFile(out.read(), filename="wtm.jpg")
            await send_photo_as_owner(conn_id, msg.chat.id, file)
        except Exception as e:
            log.exception(f".wtm failed: {e}")
            await bot.send_message(owner_id, f"Ошибка .wtm: {e}")
        return

    if cmd == ".memz":
        arg = text[len(".memz"):].strip()
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if not msg.reply_to_message or not msg.reply_to_message.photo:
            await bot.send_message(owner_id, "Ответь на фото командой .memz <текст>")
            return
        try:
            raw = await download_photo_as_bytes(msg.reply_to_message.photo[-1].file_id)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            try:
                font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(24, img.width // 15))
            except Exception:
                font = ImageFont.load_default()
            bbox = ImageDraw.Draw(img).textbbox((0, 0), arg or " ", font=font)
            th = bbox[3] - bbox[1]
            pad = th + 40
            new = Image.new("RGB", (img.width, img.height + pad * 2), (0, 0, 0))
            new.paste(img, (0, pad))
            draw = ImageDraw.Draw(new)
            if arg:
                bbox2 = draw.textbbox((0, 0), arg, font=font)
                tw = bbox2[2] - bbox2[0]
                draw.text(((img.width - tw) // 2, 10), arg, font=font, fill=(255, 255, 255))
            out = io.BytesIO()
            new.save(out, format="JPEG", quality=85)
            out.seek(0)
            file = BufferedInputFile(out.read(), filename="memz.jpg")
            await send_photo_as_owner(conn_id, msg.chat.id, file)
        except Exception as e:
            log.exception(f".memz failed: {e}")
            await bot.send_message(owner_id, f"Ошибка .memz: {e}")
        return

    # ---------- модерация ----------

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

    if cmd == ".swmute":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        if await is_swmuted(owner_id, msg.chat.id):
            await remove_swmute(owner_id, msg.chat.id)
            await send_as_owner(conn_id, msg.chat.id, "Постоянный мут снят.")
        else:
            await set_swmute(owner_id, msg.chat.id)
            await send_as_owner(
                conn_id, msg.chat.id,
                "Вам выдан постоянный мут.\nВы не можете писать в чат!\n\nЛучший бот: @aimstarsavebot"
            )
        return

    if cmd == ".unmute":
        await delete_msg_as_owner(conn_id, msg.chat.id, msg.message_id)
        mute = await get_mute(owner_id, msg.chat.id)
        card_id = None
        if mute and "card_message_id" in mute.keys():
            card_id = mute["card_message_id"]
        await remove_mute(owner_id, msg.chat.id)
        await remove_swmute(owner_id, msg.chat.id)
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

    editor = msg.from_user
    if editor:
        username = f"@{editor.username}" if editor.username else editor.full_name
        editor_id = editor.id
    else:
        username = "unknown"
        editor_id = "—"

    header = (
        f"{username} ({editor_id})\n"
        f"отредактировал свое сообщение\n"
        f"было:\n"
        f"{old_text}\n\n"
        f"стало:\n"
        f"{new_text}"
    )
    try:
        await bot.send_message(owner_id, header)
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
    asyncio.create_task(swmute_watcher())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())