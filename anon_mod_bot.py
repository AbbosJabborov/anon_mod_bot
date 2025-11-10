# anon_mod_bot.py
# Telegram moderation + anonymous posting bot
# Requires: python-telegram-bot v21+

import os, json, time, re
from datetime import datetime
from typing import Optional, Dict, Any
from telegram import Update, ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatType
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# ====== CONFIGURATION ======
BOT_TOKEN  = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",")) 
CHANNEL_ID = os.getenv("CHANNEL_ID", "@your_channel_name")
GROUP_ID = int(os.getenv("GROUP_ID", "-1001234567890"))
DB_PATH = "moderation_data.json"
MAX_TEXT_LEN = 4000

# Basic profanity filter (extend if needed)
BAD_WORDS = {"fuck", "shit", "bitch", "asshole", "bastard", "cunt", "dick"}

# ====== UTILITIES ======
def now_ts() -> int:
    return int(time.time())

def load_db() -> Dict[str, Any]:
    if not os.path.exists(DB_PATH):
        return {"muted": {}}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db: Dict[str, Any]) -> None:
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def parse_duration(s: str) -> Optional[int]:
    m = re.fullmatch(r"(\d+)\s*(m|h|d|w)", s.strip(), re.I)
    if not m: return None
    val, unit = int(m.group(1)), m.group(2).lower()
    mult = {"m":60, "h":3600, "d":86400, "w":604800}[unit]
    return val * mult

def format_time_left(seconds: int) -> str:
    if seconds <= 0: return "0s"
    days, hours = divmod(seconds, 86400)
    hours, minutes = divmod(hours, 3600)
    minutes //= 60
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    return " ".join(parts) or f"{seconds}s"

def looks_profane(text: str) -> bool:
    if not text: return False
    low = text.lower()
    return any(b in low for b in BAD_WORDS)

def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in ADMIN_IDS

# ====== MUTE SYSTEM ======
def is_muted(db: Dict[str, Any], uid: int) -> int:
    until = db.get("muted", {}).get(str(uid), 0)
    left = until - now_ts()
    return left if left > 0 else 0

def set_mute(db: Dict[str, Any], uid: int, seconds: int):
    until = now_ts() + seconds
    db.setdefault("muted", {})[str(uid)] = until
    save_db(db)
    return until

def clear_mute(db: Dict[str, Any], uid: int):
    if str(uid) in db.get("muted", {}):
        db["muted"].pop(str(uid))
        save_db(db)

# ====== PENDING STORAGE ======
PENDING: Dict[str, dict] = {}

# ====== UI BUILDERS ======
def build_delete_keyboard(original_user_id: int):
    """Create delete button with original sender's ID embedded."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{original_user_id}")]]
    )
    
def build_moderation_keyboard(key: str, profane: bool=False):
    buttons = [
        [InlineKeyboardButton("✅ Approve", callback_data=f"approve:{key}")],
        [InlineKeyboardButton("🚫 Reject", callback_data=f"reject:{key}")]
    ]
    if profane:
        buttons[0][0].text = "✅ Approve (contains profanity)"
    return InlineKeyboardMarkup(buttons)

# ====== COMMAND HANDLERS ======
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send your message anonymously. An admin will review and publish it if approved."
    )

# Handle DM anonymous message
async def dm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.chat.type != ChatType.PRIVATE:
        return

    db = load_db()
    uid = msg.from_user.id
    left = is_muted(db, uid)
    if left:
        await msg.reply_text(f"⛔ You are muted. Time left: {format_time_left(left)}")
        return

    text = (msg.text or msg.caption or "").strip()
    text = text[:MAX_TEXT_LEN] if text else ""
    prof = looks_profane(text)

    key = f"{uid}:{msg.id}"
    PENDING[key] = {"text": text, "raw_msg": msg}

    admin = next(iter(ADMIN_IDS))
    sender = f"👤 From ID: {uid}\nName: {msg.from_user.full_name}"
    if msg.from_user.username:
        sender += f"\n@{msg.from_user.username}"
    preview = f"Text:\n{text}" if text else "(no text)"
    await context.bot.send_message(admin, text=f"{sender}\n\n{preview}", reply_markup=build_moderation_keyboard(key, prof))
    await msg.reply_text("✅ Sent for moderation. Please wait for admin review.")

# Callback for moderation buttons
async def dm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer()
    data = q.data or ""
    if ":" not in data: return
    action, key = data.split(":", 1)
    item = PENDING.get(key)
    if not item:
        await q.edit_message_text("⚠️ Request already processed or expired.")
        return

    if action == "approve":
        try:
            text = item.get("text")
            raw = item.get("raw_msg")
            if raw.photo:
                await context.bot.send_photo(CHANNEL_ID, raw.photo[-1].file_id, caption=text or None)
            elif raw.video:
                await context.bot.send_video(CHANNEL_ID, raw.video.file_id, caption=text or None)
            elif raw.document:
                await context.bot.send_document(CHANNEL_ID, raw.document.file_id, caption=text or None)
            else:
                await context.bot.send_message(CHANNEL_ID, text or "(empty message)")
            await q.edit_message_text("✅ Published to channel.")
        except Exception as e:
            await q.edit_message_text(f"❌ Error publishing: {e}")
        finally:
            PENDING.pop(key, None)
    elif action == "reject":
        await q.edit_message_text("🚫 Rejected.")
        PENDING.pop(key, None)

# /anon in group
async def anon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ID:
        return
    msg = update.message
    user = msg.from_user
    text = " ".join(context.args).strip()

    if not text:
        await msg.reply_text("Usage: /anon your text")
        return

    db = load_db()
    left = is_muted(db, user.id)
    if left:
        await msg.delete()
        await context.bot.send_message(GROUP_ID, f"⛔ User muted for {format_time_left(left)}.")
        return

    if looks_profane(text):
        await msg.delete()
        admins_text = f"⚠️ Profanity detected and message deleted.\nFrom: {user.full_name} ({user.id})\nText: {text}"
        for a in ADMIN_IDS:
            await context.bot.send_message(a, admins_text)
        await context.bot.send_message(GROUP_ID, "🧹 Message removed by moderation.")
        return

    # Delete user's command message
    await msg.delete()

    reply_to_id = msg.reply_to_message.message_id if msg.reply_to_message else None

    # Send anonymous message with delete button
    sent_msg = await context.bot.send_message(
        GROUP_ID,
        f"🕵️ Anonymous:\n{text}",
        reply_to_message_id=reply_to_id,
        reply_markup=build_delete_keyboard(user.id)
    )

async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer()
    data = q.data or ""
    if not data.startswith("delete:"): return

    original_user_id = int(data.split(":", 1)[1])
    user = q.from_user

    # Only OP or admin can delete
    if user.id != original_user_id and user.id not in ADMIN_IDS:
        await q.answer("❌ You are not allowed to delete this message.", show_alert=True)
        return

    try:
        await context.bot.delete_message(q.message.chat_id, q.message.message_id)
    except Exception as e:
        await q.answer(f"❌ Failed to delete: {e}", show_alert=True)


# ====== ADMIN COMMANDS ======
async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admins only.")
        return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target:
        await update.message.reply_text("Reply to a user to mute.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /mute 1d reason")
        return
    dur = parse_duration(context.args[0])
    if not dur:
        await update.message.reply_text("Invalid duration. Examples: 10m, 2h, 1d, 1w")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason given"
    db = load_db()
    until = set_mute(db, target.id, dur)
    try:
        perms = ChatPermissions(can_send_messages=False)
        await context.bot.restrict_chat_member(chat_id=GROUP_ID, user_id=target.id, permissions=perms,
                                               until_date=datetime.utcfromtimestamp(until))
    except Exception:
        pass
    await update.message.reply_text(f"✅ {target.full_name} muted until {datetime.fromtimestamp(until)}. Reason: {reason}")

async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admins only.")
        return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target:
        await update.message.reply_text("Reply to a user to unmute.")
        return
    db = load_db()
    clear_mute(db, target.id)
    try:
        perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
        await context.bot.restrict_chat_member(chat_id=GROUP_ID, user_id=target.id, permissions=perms)
    except Exception:
        pass
    await update.message.reply_text(f"✅ {target.full_name} unmuted.")

async def modstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Admins only.")
        return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target:
        await update.message.reply_text("Reply to a user.")
        return
    db = load_db()
    left = is_muted(db, target.id)
    await update.message.reply_text(f"User: {target.full_name}\nID: {target.id}\nMuted: {'yes, ' + format_time_left(left) if left else 'no'}")

# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, dm_handler))
    app.add_handler(CallbackQueryHandler(dm_callback))
    app.add_handler(CommandHandler("anon", anon_cmd))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^delete:"))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("modstats", modstats_cmd))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
