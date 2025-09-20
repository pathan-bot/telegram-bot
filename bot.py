#!/usr/bin/env python3
import os
import logging
import threading
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import deque
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------- Config & env ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN") 
PAYMENT_URL = os.environ.get("PAYMENT_URL", "https://example.com/payment")  # placeholder
DB_FILE = os.environ.get("BOT_DB", "bot_data.db")
HEALTH_PORT = int(os.environ.get("PORT", os.environ.get("HEALTH_PORT", 10000)))

# ---------------- Health check ----------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server(port=HEALTH_PORT):
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- In-memory structures ----------------
waiting = deque()   # queue of user ids waiting
partners = {}       # partners[user_id] = partner_id
last_partner = {}   # last_partner[user_id] = last_partner_id

# ---------------- SQLite helpers ----------------
_db_lock = threading.Lock()

def init_db():
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.cursor()
        # user profiles
        cur.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                age INTEGER,
                gender TEXT,
                is_premium INTEGER DEFAULT 0,
                updated_at TEXT
            )
        """)
        # reports
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER,
                partner_id INTEGER,
                reason TEXT,
                ts TEXT
            )
        """)
        # forwards mapping for delete feature
        cur.execute("""
            CREATE TABLE IF NOT EXISTS forwards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                partner_id INTEGER,
                orig_msg_id INTEGER,
                fwd_msg_id INTEGER,
                content_type TEXT,
                ts TEXT
            )
        """)
        conn.commit()
        conn.close()

def db_set_profile(user_id: int, age=None, gender=None, is_premium=None):
    ts = datetime.utcnow().isoformat()
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM profiles WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            # update fields provided
            if age is not None:
                cur.execute("UPDATE profiles SET age=?, updated_at=? WHERE user_id=?", (age, ts, user_id))
            if gender is not None:
                cur.execute("UPDATE profiles SET gender=?, updated_at=? WHERE user_id=?", (gender, ts, user_id))
            if is_premium is not None:
                cur.execute("UPDATE profiles SET is_premium=?, updated_at=? WHERE user_id=?", (int(bool(is_premium)), ts, user_id))
        else:
            cur.execute("INSERT INTO profiles(user_id, age, gender, is_premium, updated_at) VALUES(?,?,?,?,?)",
                        (user_id, age, gender, int(bool(is_premium)) if is_premium is not None else 0, ts))
        conn.commit()
        conn.close()

def db_get_profile(user_id: int):
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.cursor()
        cur.execute("SELECT age, gender, is_premium, updated_at FROM profiles WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {"age": row[0], "gender": row[1], "is_premium": bool(row[2]), "updated_at": row[3]}
        return {"age": None, "gender": None, "is_premium": False, "updated_at": None}

def db_add_report(reporter_id: int, partner_id: int, reason: str = ""):
    ts = datetime.utcnow().isoformat()
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.cursor()
        cur.execute("INSERT INTO reports(reporter_id, partner_id, reason, ts) VALUES(?,?,?,?)",
                    (reporter_id, partner_id, reason, ts))
        conn.commit()
        conn.close()

def db_add_forward(user_id:int, partner_id:int, orig_msg_id:int, fwd_msg_id:int, content_type:str):
    ts = datetime.utcnow().isoformat()
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.cursor()
        cur.execute("INSERT INTO forwards(user_id, partner_id, orig_msg_id, fwd_msg_id, content_type, ts) VALUES(?,?,?,?,?,?)",
                    (user_id, partner_id, orig_msg_id, fwd_msg_id, content_type, ts))
        conn.commit()
        conn.close()

def db_get_last_forward(user_id:int):
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.cursor()
        cur.execute("SELECT id, fwd_msg_id, partner_id FROM forwards WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row  # None or (id, fwd_msg_id, partner_id)

def db_delete_forward_record(record_id:int):
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.cursor()
        cur.execute("DELETE FROM forwards WHERE id=?", (record_id,))
        conn.commit()
        conn.close()

# ---------------- Utility ----------------
def is_premium_user(user_id:int):
    profile = db_get_profile(user_id)
    return profile.get("is_premium", False)

# ---------------- Commands & Handlers ----------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # show the 6-button inline keyboard
    keyboard = [
        [InlineKeyboardButton("💬 Chat", callback_data="chat"),
         InlineKeyboardButton("❌ Leave chat", callback_data="leave")],
        [InlineKeyboardButton("⚠ Report", callback_data="report"),
         InlineKeyboardButton("🔎 Search by gender", callback_data="search_gender")],
        [InlineKeyboardButton("⚙ Settings", callback_data="settings"),
         InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # prefer reply_text if message exists
    if update.message:
        await update.message.reply_text("👋 Welcome! Choose an option:", reply_markup=reply_markup)
    else:
        # fallback
        await context.bot.send_message(chat_id=update.effective_chat.id, text="👋 Welcome! Choose an option:", reply_markup=reply_markup)

async def chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    # avoid admin or group misuse - only private chat ideally
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please use this bot in private chat (one-to-one).")
        return

    if user in partners:
        await update.message.reply_text("⚠ You are already in a chat. Use /exit to leave.")
        return
    if user in waiting:
        await update.message.reply_text("⏳ You are already waiting...")
        return

    # try to find compatible waiting partner (basic FIFO)
    # (premium gender search not applied here)
    if waiting:
        partner = waiting.popleft()
        if partner == user:
            waiting.appendleft(partner)
            await update.message.reply_text("⏳ Waiting for a partner...")
            return
        partners[user] = partner
        partners[partner] = user
        last_partner[user] = partner
        last_partner[partner] = user
        await context.bot.send_message(chat_id=user, text="✅ Partner found! Say hi 👋")
        await context.bot.send_message(chat_id=partner, text="✅ Partner found! Say hi 👋")
    else:
        waiting.append(user)
        await update.message.reply_text("⏳ Waiting for a partner...")

async def exit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    if user in waiting:
        try:
            waiting.remove(user)
        except ValueError:
            pass
        await update.message.reply_text("⛔ You left the queue.")
        return
    if user in partners:
        partner = partners.get(user)
        # cleanup
        try:
            del partners[partner]
        except KeyError:
            pass
        try:
            del partners[user]
        except KeyError:
            pass
        last_partner[user] = partner
        if partner:
            try:
                await context.bot.send_message(chat_id=partner, text="⚠ Your partner left the chat.")
            except Exception:
                pass
        await update.message.reply_text("❌ You left the chat.")
        return
    await update.message.reply_text("You are not in a chat or queue.")

async def forward_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # handle text, photo, sticker forwarding; store mapping for delete
    user = update.effective_user.id
    if user not in partners:
        return
    partner = partners.get(user)
    if not partner:
        return

    try:
        if update.message.text:
            sent = await context.bot.send_message(chat_id=partner, text=update.message.text)
            db_add_forward(user, partner, update.message.message_id, sent.message_id, "text")
        elif update.message.photo:
            file_id = update.message.photo[-1].file_id
            sent = await context.bot.send_photo(chat_id=partner, photo=file_id, caption=update.message.caption)
            db_add_forward(user, partner, update.message.message_id, sent.message_id, "photo")
        elif update.message.sticker:
            sent = await context.bot.send_sticker(chat_id=partner, sticker=update.message.sticker.file_id)
            db_add_forward(user, partner, update.message.message_id, sent.message_id, "sticker")
        else:
            sent = await context.bot.send_message(chat_id=partner, text="📨 (Message forwarded)")
            db_add_forward(user, partner, update.message.message_id, sent.message_id, "other")
    except Exception as e:
        logger.exception("Forwarding failed: %s", e)

# ---------------- Callback (inline buttons) ----------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user.id

    if query.data == "chat":
        # start pairing flow
        await query.edit_message_text("🔍 Searching for a partner... Please run /chat to start pairing in private chat.")
        return

    if query.data == "leave":
        await query.edit_message_text("❌ Use /exit in private chat to leave chat or queue.")
        return

    if query.data == "report":
        # if user in partner, report them
        partner = partners.get(user)
        if partner:
            db_add_report(user, partner, reason="Reported via button")
            await query.edit_message_text("⚠ Your request to report is saved. We'll verify and take action soon. Enjoy our services.")
        else:
            await query.edit_message_text("⚠ You are not in an active chat to report.")
        return

    if query.data == "search_gender":
        await query.edit_message_text("🔎 Search by gender is a premium feature. Please purchase premium.")
        return

    if query.data == "settings":
        # show quick settings instructions
        txt = (
            "⚙ Settings - quick commands:\n"
            "/profile - show your profile\n"
            "/set age <number> - set your age\n"
            "/set gender <male/female/other> - set gender\n"
            "/set premium on|off - toggle premium (admin only in real world)\n"
        )
        await query.edit_message_text(txt)
        return

    if query.data == "help":
        await query.edit_message_text("❓ Help section: For now, commands: /start /chat /exit /profile /set /delete_last /rules /report")

# ---------------- Additional commands (profile, set, report, delete) ----------------
async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    prof = db_get_profile(user)
    text = f"👤 Profile:\nAge: {prof.get('age')}\nGender: {prof.get('gender')}\nPremium: {prof.get('is_premium')}\nLast updated: {prof.get('updated_at')}"
    await update.message.reply_text(text)

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # usage: /set field value
    user = update.effective_user.id
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /set <field> <value>\nFields: age gender premium")
        return
    field = args[0].lower()
    value = " ".join(args[1:]).strip()
    if field == "age":
        try:
            age = int(value)
            db_set_profile(user, age=age)
            await update.message.reply_text(f"✅ Your age is set to {age}")
        except ValueError:
            await update.message.reply_text("Please provide a valid age number.")
    elif field == "gender":
        gender = value.lower()
        db_set_profile(user, gender=gender)
        await update.message.reply_text(f"✅ Your gender is set to {gender}")
    elif field == "premium":
        if value.lower() in ("on", "1", "true", "yes"):
            db_set_profile(user, is_premium=1)
            await update.message.reply_text("✅ Premium flag set to ON (for testing).")
        else:
            db_set_profile(user, is_premium=0)
            await update.message.reply_text("✅ Premium flag set to OFF.")
    else:
        await update.message.reply_text("Unknown field. Allowed: age, gender, premium")

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "📜 Chat Rules:\n"
        "a) Avoid sharing personal details\n"
        "b) Abusing other users is not allowed\n"
        "c) Sexual content is not allowed\n"
        "d) Sending links is not allowed\n"
        "e) Sending spam/fraud messages are not allowed"
    )
    await update.message.reply_text(txt)

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    partner = partners.get(user)
    if partner:
        reason = " ".join(context.args) if context.args else ""
        db_add_report(user, partner, reason)
        await update.message.reply_text("✅ Your request to report is saved. We'll verify and take action soon. Enjoy our services.")
    else:
        await update.message.reply_text("You are not in an active chat to report.")

async def delete_last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    row = db_get_last_forward(user)
    if not row:
        await update.message.reply_text("No forwarded messages found to delete.")
        return
    record_id, fwd_msg_id, partner_id = row
    try:
        # try deleting from partner chat (bot sent message)
        await context.bot.delete_message(chat_id=partner_id, message_id=fwd_msg_id)
    except Exception as e:
        logger.warning("Delete forwarded message failed: %s", e)
        await update.message.reply_text("Could not delete the forwarded message (maybe already deleted).")
        # still remove mapping
        db_delete_forward_record(record_id)
        return
    # removed successfully
    db_delete_forward_record(record_id)
    await update.message.reply_text("✅ Your last forwarded message has been deleted.")

async def previous_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # premium only
    user = update.effective_user.id
    if not is_premium_user(user):
        await update.message.reply_text("🔄 This is a premium feature. Please purchase premium.")
        return
    partner = last_partner.get(user)
    if not partner:
        await update.message.reply_text("No previous partner found.")
        return
    await update.message.reply_text(f"Your previous partner id: {partner} (use /chat to try to reconnect)")

async def payment_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💳 Payment support coming soon! {PAYMENT_URL}")

# ---------------- Error handler ----------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Exception while handling an update: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_user:
            await context.bot.send_message(chat_id=update.effective_user.id, text="⚠ An error occurred. Please try again later.")
    except Exception:
        pass

# ---------------- Main ----------------
def main():
    init_db()
    # start health server
    threading.Thread(target=start_health_server, daemon=True).start()

    # Build application with reasonable timeouts (request_kwargs)
    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers (commands + utility)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("chat", chat_cmd))
    app.add_handler(CommandHandler("exit", exit_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("delete_last", delete_last_cmd))
    app.add_handler(CommandHandler("previous", previous_cmd))
    app.add_handler(CommandHandler("payment", payment_cmd))
    app.add_handler(CommandHandler("paysupport", payment_cmd))
    app.add_handler(CommandHandler("help", lambda u,c: u.message.reply_text("❓ Help coming soon!")))

    # Inline callback
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Forward content handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_messages))
    app.add_handler(MessageHandler(filters.PHOTO, forward_messages))
    app.add_handler(MessageHandler(filters.Sticker.ALL, forward_messages))

    # Error handler
    app.add_error_handler(error_handler)

    print("🤖 Bot started polling...")
    app.run_polling()

if __name__ == "__main__":
    main()

