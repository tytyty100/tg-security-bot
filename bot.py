import asyncio
import json
import logging
import os
import re
import socket
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

_original_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(*args, **kwargs):
    return [r for r in _original_getaddrinfo(*args, **kwargs) if r[0] == socket.AF_INET]


socket.getaddrinfo = _ipv4_only_getaddrinfo

from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import TOKEN

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

MUTE_MINUTES = 180  # 3 часа

DEFAULT_LEVEL = "низкая"
LEVELS = {
    "низкая": {"flood": (7, 4), "spam": (7, 5), "links": 4, "mats": 15,
               "media": (5, 10), "fwd": (5, 10), "mentions": 5},
    "средняя": {"flood": (4, 4), "spam": (5, 5), "links": 3, "mats": 8,
                "media": (4, 10), "fwd": (4, 10), "mentions": 3},
    "высокая": {"flood": (3, 4), "spam": (3, 5), "links": 2, "mats": 3,
                "media": (3, 10), "fwd": (3, 10), "mentions": 2},
}

MAT_ROOTS = (
    "хуй", "хуя", "хуе", "хуё", "хую", "хуи",
    "пизд", "бля", "бляд",
    "ебал", "ебат", "ебан", "ебну", "ебу", "ебёт", "ебут", "ёб",
    "шлюх", "пидор", "пидр",
    "мудак", "мудил", "залуп", "елд",
    "обоср", "заср", "наху", "нахре", "оху",
    "гандон", "гондон",
    "дебил", "идиот", "придур", "дурак", "козл", "козё",
    "жоп", "чмо", "хер", "курва", "сука", "сучк",
)
MAT_PATTERN = re.compile(
    "(?:{})|(?<![а-яё])манда(?![а-яё])".format("|".join(re.escape(w) for w in MAT_ROOTS)),
    re.IGNORECASE,
)


def _new_chat_config():
    return {"level": DEFAULT_LEVEL, "words": []}


chat_config = defaultdict(_new_chat_config)
history = defaultdict(lambda: defaultdict(deque))       # chat -> user -> deque[(time, message_id)]
text_history = defaultdict(lambda: defaultdict(deque))  # chat -> user -> deque[(text, time, message_id)]
media_history = defaultdict(lambda: defaultdict(deque))      # chat -> user -> deque[(time, message_id)]
forward_history = defaultdict(lambda: defaultdict(deque))    # chat -> user -> deque[(time, message_id)]

MUTE_PERMS = ChatPermissions(can_send_messages=False)
UNMUTE_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
    can_manage_topics=True,
)

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def load_settings():
    global chat_config
    chat_config = defaultdict(_new_chat_config)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for key, value in data.items():
            cid = int(key)
            if isinstance(value, str):
                chat_config[cid]["level"] = value if value in LEVELS else DEFAULT_LEVEL
            elif isinstance(value, dict):
                cfg = _new_chat_config()
                cfg["level"] = value.get("level", DEFAULT_LEVEL) if value.get("level") in LEVELS else DEFAULT_LEVEL
                cfg["words"] = [str(w).lower() for w in value.get("words", [])]
                chat_config[cid] = cfg
    except Exception:
        pass


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in chat_config.items()}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_level(chat_id: int) -> str:
    return chat_config[chat_id]["level"]


def level_desc(level: str) -> str:
    cfg = LEVELS[level]
    f_n, f_w = cfg["flood"]
    s_n, s_w = cfg["spam"]
    m_n, m_w = cfg["media"]
    fv_n, fv_w = cfg["fwd"]
    return (
        f"{level}:\n"
        f"- флуд: {f_n}+ сообщений за {f_w} сек\n"
        f"- спам: {s_n} одинаковых за {s_w} сек\n"
        f"- спам ссылками: {cfg['links']}+ ссылок\n"
        f"- мат: {cfg['mats']}+ в одном сообщении\n"
        f"- флуд медиа: {m_n}+ за {m_w} сек\n"
        f"- спам пересылками: {fv_n}+ за {fv_w} сек\n"
        f"- массовые упоминания: {cfg['mentions']}+\n"
    )


def is_group(chat) -> bool:
    return chat.type in ("group", "supergroup")


def dur_text(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин."
    h = minutes // 60
    if h == 1:
        return "1 час"
    if 2 <= h <= 4:
        return f"{h} часа"
    return f"{h} часов"


def make_mention(user) -> str:
    if user.username:
        return f'<a href="https://t.me/{user.username}">@{user.username}</a>'
    return f'<a href="tg://user?id={user.id}">{user.first_name}</a>'


async def resolve_user(chat, context, arg) -> "User | None":
    if arg.lstrip("-").isdigit():
        return (await chat.get_member(int(arg))).user
    username = None
    if arg.startswith("@"):
        username = arg
    elif "t.me/" in arg:
        u = arg.split("t.me/")[-1].split("/")[0].split("?")[0]
        if u:
            username = "@" + u
    if username:
        resolved = await context.bot.get_chat(username)
        return (await chat.get_member(resolved.id)).user
    return None


async def delete_message(chat, message_id: int):
    try:
        await chat.delete_message(message_id)
    except Exception:
        pass


async def mute_user(update: Update, user, reason: str, minutes: int = MUTE_MINUTES, delete_ids=()) -> bool:
    chat = update.effective_chat
    for mid in delete_ids:
        await delete_message(chat, mid)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    try:
        await chat.restrict_member(user.id, permissions=MUTE_PERMS, until_date=until)
    except Exception:
        return False
    mention = make_mention(user)
    text = (
        f"{mention} замучен за {reason} на {dur_text(minutes)}.\n"
        f"Размутить могут только администраторы."
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Размутить", callback_data=f"unmute:{user.id}")]]
    )
    try:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    except Exception:
        pass
    return True


def clear_tracks(chat_id: int, user_id: int):
    history[chat_id][user_id].clear()
    text_history[chat_id][user_id].clear()
    media_history[chat_id][user_id].clear()
    forward_history[chat_id][user_id].clear()


async def unmute_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user

    member = await chat.get_member(user.id)
    if member.status not in ADMIN_STATUSES:
        await query.answer("Только администраторы могут размучивать!", show_alert=True)
        return

    target_id = int(query.data.split(":")[1])
    try:
        target = await chat.get_member(target_id)
    except Exception:
        target = None

    try:
        await chat.restrict_member(target_id, permissions=UNMUTE_PERMS)
    except Exception:
        await query.answer("Не удалось размутить. Проверьте, что бот администратор.", show_alert=True)
        return

    mention = make_mention(target.user) if target else "@user"
    try:
        await query.message.edit_text(
            f"{mention} размучен.", parse_mode=ParseMode.HTML
        )
    except Exception:
        pass
    await query.answer("Готово")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not is_group(chat) or not msg or not msg.from_user or msg.from_user.is_bot:
        return

    user = msg.from_user
    member = await chat.get_member(user.id)
    if member.status in ADMIN_STATUSES:
        return

    cfg = LEVELS[get_level(chat.id)]
    flood_n, flood_w = cfg["flood"]
    spam_n, spam_w = cfg["spam"]
    link_n = cfg["links"]
    mat_n = cfg["mats"]
    media_n, media_w = cfg["media"]
    fwd_n, fwd_w = cfg["fwd"]
    mention_n = cfg["mentions"]

    now = time.time()
    ch = history[chat.id][user.id]
    ch.append((now, msg.message_id))
    while ch and ch[0][0] < now - flood_w:
        ch.popleft()

    if len(ch) >= flood_n:
        await mute_user(update, user, "флуд", delete_ids=[mid for _, mid in ch])
        clear_tracks(chat.id, user.id)
        return

    is_media = bool(
        msg.photo or msg.video or msg.audio or msg.document or msg.voice
        or msg.video_note or msg.sticker or msg.animation
    )
    if is_media:
        mh = media_history[chat.id][user.id]
        mh.append((now, msg.message_id))
        while mh and mh[0][0] < now - media_w:
            mh.popleft()
        if len(mh) >= media_n:
            await mute_user(update, user, "флуд медиа", delete_ids=[mid for _, mid in mh])
            clear_tracks(chat.id, user.id)
            return

    if msg.forward_origin:
        fh = forward_history[chat.id][user.id]
        fh.append((now, msg.message_id))
        while fh and fh[0][0] < now - fwd_w:
            fh.popleft()
        if len(fh) >= fwd_n:
            await mute_user(update, user, "спам пересылками", delete_ids=[mid for _, mid in fh])
            clear_tracks(chat.id, user.id)
            return

    txt = msg.text or msg.caption or ""
    if txt:
        link_count = txt.count("http://") + txt.count("https://") + txt.count("t.me/")
        if link_count >= link_n:
            await mute_user(update, user, "спам ссылками", delete_ids=[msg.message_id])
            clear_tracks(chat.id, user.id)
            return

        mat_count = len(MAT_PATTERN.findall(txt))
        words = chat_config[chat.id]["words"]
        if words:
            mat_count += len(
                re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE).findall(txt)
            )
        if mat_count >= mat_n:
            await mute_user(update, user, "мат", delete_ids=[msg.message_id])
            clear_tracks(chat.id, user.id)
            return

        mention_count = 0
        pings_admin = False
        mention_entities = [
            e for e in (msg.entities or []) if e.type in ("mention", "text_mention")
        ]
        if mention_entities:
            admins = await chat.get_administrators()
            admin_ids = {m.user.id for m in admins}
            admin_names = {m.user.username.lower() for m in admins if m.user.username}
            for e in mention_entities:
                if e.type == "text_mention" and e.user and e.user.id in admin_ids:
                    pings_admin = True
                    mention_count += 1
                elif e.type == "mention":
                    mention_count += 1
                    if msg.text[e.offset + 1:e.offset + e.length].lower() in admin_names:
                        pings_admin = True

        if pings_admin:
            await mute_user(update, user, "пингование администратора", delete_ids=[msg.message_id])
            clear_tracks(chat.id, user.id)
            return

        if mention_count >= mention_n:
            await mute_user(update, user, "массовые упоминания", delete_ids=[msg.message_id])
            clear_tracks(chat.id, user.id)
            return

        th = text_history[chat.id][user.id]
        th.append((txt, now, msg.message_id))
        while th and th[0][1] < now - spam_w:
            th.popleft()

        if len(th) >= spam_n and len({t for t, _, _ in th}) == 1:
            await mute_user(update, user, "спам", delete_ids=[mid for _, _, mid in th])
            clear_tracks(chat.id, user.id)


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not is_group(chat):
        await msg.reply_text("Команда /unmute работает только в группах.")
        return

    member = await chat.get_member(user.id)
    if member.status not in ADMIN_STATUSES:
        await msg.reply_text("Только администраторы могут размучивать участников.")
        return

    target_user = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user
    else:
        parts = (msg.text or msg.caption or "").strip().split()
        if len(parts) == 2:
            try:
                target_user = await resolve_user(chat, context, parts[1])
            except Exception:
                target_user = None
        else:
            await msg.reply_text("Укажите @ник, ссылку на профиль или id. Либо ответьте на сообщение.")
            return

    if target_user is None:
        await msg.reply_text("Не удалось найти пользователя.")
        return

    try:
        await chat.restrict_member(target_user.id, permissions=UNMUTE_PERMS)
    except Exception:
        await msg.reply_text("Не удалось размутить. Проверьте, что бот администратор.")
        return

    mention = make_mention(target_user)
    await msg.reply_text(f"{mention} размучен.", parse_mode=ParseMode.HTML)


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not is_group(chat):
        await msg.reply_text("Команда /mute работает только в группах.")
        return

    member = await chat.get_member(user.id)
    if member.status not in ADMIN_STATUSES:
        await msg.reply_text("Только администраторы могут мутить участников.")
        return

    parts = (msg.text or msg.caption or "").strip().split()
    target = None
    rest = parts[1:]

    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
    elif rest:
        try:
            target = await resolve_user(chat, context, rest[0])
            if target is not None:
                rest = rest[1:]
        except Exception:
            target = None
    else:
        await msg.reply_text("Использование: /mute <@ник|id|ссылка> [минуты] [причина] либо ответ на сообщение.")
        return

    if target is None:
        await msg.reply_text("Не удалось найти пользователя.")
        return

    minutes = MUTE_MINUTES
    if rest and rest[0].isdigit():
        minutes = min(int(rest[0]), 40320)
        rest = rest[1:]

    reason = " ".join(rest) if rest else "вручную"
    ok = await mute_user(update, target, reason, minutes)
    if not ok:
        await msg.reply_text("Не удалось замутить. Проверьте, что бот администратор.")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not is_group(chat):
        await msg.reply_text("Команда /settings работает только в группах.")
        return

    member = await chat.get_member(user.id)
    if member.status not in ADMIN_STATUSES:
        await msg.reply_text("Только администраторы могут менять настройки.")
        return

    level = get_level(chat.id)
    text = "Жёсткость защиты:\n\n" + "\n".join(level_desc(l) for l in LEVELS) + \
        f"\nТекущая: {level}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"Низкая {'✓' if level == 'низкая' else ''}",
            callback_data="harshness:низкая",
        ),
        InlineKeyboardButton(
            f"Средняя {'✓' if level == 'средняя' else ''}",
            callback_data="harshness:средняя",
        ),
        InlineKeyboardButton(
            f"Высокая {'✓' if level == 'высокая' else ''}",
            callback_data="harshness:высокая",
        ),
    ]])
    await msg.reply_text(text, reply_markup=kb)


async def harshness_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = query.message.chat
    member = await chat.get_member(query.from_user.id)
    if member.status not in ADMIN_STATUSES:
        await query.answer("Только администраторы могут менять настройки!", show_alert=True)
        return

    level = query.data.split(":", 1)[1]
    chat_config[chat.id]["level"] = level
    save_settings()

    text = "Жёсткость защиты:\n\n" + "\n".join(level_desc(l) for l in LEVELS) + \
        f"\nТекущая: {level}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"Низкая {'✓' if level == 'низкая' else ''}",
            callback_data="harshness:низкая",
        ),
        InlineKeyboardButton(
            f"Средняя {'✓' if level == 'средняя' else ''}",
            callback_data="harshness:средняя",
        ),
        InlineKeyboardButton(
            f"Высокая {'✓' if level == 'высокая' else ''}",
            callback_data="harshness:высокая",
        ),
    ]])
    try:
        await query.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await query.answer(f"Уровень: {level}")


async def banword_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not is_group(chat):
        await msg.reply_text("Команда /banword работает только в группах.")
        return

    member = await chat.get_member(user.id)
    if member.status not in ADMIN_STATUSES:
        await msg.reply_text("Только администраторы могут добавлять слова.")
        return

    parts = (msg.text or "").strip().split()
    if len(parts) < 2:
        await msg.reply_text("Использование: /banword <слово>")
        return

    word = parts[1].lower()
    words = chat_config[chat.id]["words"]
    if word in words:
        await msg.reply_text("Это слово уже в бан-списке.")
        return

    words.append(word)
    save_settings()
    await msg.reply_text(f"Слово «{word}» добавлено в бан-список ({len(words)} слов).")


async def unbanword_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not is_group(chat):
        await msg.reply_text("Команда /unbanword работает только в группах.")
        return

    member = await chat.get_member(user.id)
    if member.status not in ADMIN_STATUSES:
        await msg.reply_text("Только администраторы могут удалять слова.")
        return

    parts = (msg.text or "").strip().split()
    if len(parts) < 2:
        await msg.reply_text("Использование: /unbanword <слово>")
        return

    word = parts[1].lower()
    words = chat_config[chat.id]["words"]
    if word not in words:
        await msg.reply_text("Такого слова нет в бан-списке.")
        return

    words.remove(word)
    save_settings()
    await msg.reply_text(f"Слово «{word}» удалено из бан-списка.")


async def words_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    words = chat_config[chat.id]["words"]
    if not words:
        await msg.reply_text("Бан-список пуст. Добавьте слово командой /banword <слово>")
        return
    await msg.reply_text("Бан-слова:\n" + "\n".join(f"- {w}" for w in words))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if update.effective_chat.type != "private":
        return
    text = (
        "Привет! Я бот-защитник групп. \n\n"
        "Слежу за порядком, удаляю сообщения нарушителей и мучу их на 3 часа:\n"
        "- флуд, спам одинаковыми сообщениями\n"
        "- спам ссылками и массовыми упоминаниями\n"
        "- пингование администраторов\n"
        "- мат (включая ваши бан-слова)\n"
        "- флуд медиа и спам пересылками\n\n"
        "Команды для администраторов:\n"
        "/settings - настройка жёсткости (низкая/средняя/высокая)\n"
        "/banword <слово> - добавить бан-слово\n"
        "/unbanword <слово> - убрать бан-слово\n"
        "/words - список бан-слов\n"
        "/mute <@ник|id|ссылка> [минуты] [причина] - ручной мут (или ответом)\n"
        "/unmute <@ник|id|ссылка> - размут (или ответом)\n\n"
        "Добавьте меня в группу администратором, и я начну работать."
    )
    await msg.reply_text(text)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Ошибка при обработке: %s", context.error)


def main():
    logging.basicConfig(
        filename=os.path.join(BASE_DIR, "bot.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    load_settings()
    app = Application.builder().token(TOKEN).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("banword", banword_cmd))
    app.add_handler(CommandHandler("unbanword", unbanword_cmd))
    app.add_handler(CommandHandler("words", words_cmd))
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(unmute_cb, pattern=r"^unmute:\d+$"))
    app.add_handler(CallbackQueryHandler(harshness_cb, pattern=r"^harshness:"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    logging.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
