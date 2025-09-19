from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
import logging
from collections import deque
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters
)

# --- Config ---
BOT_TOKEN = "7995697835:AAHCYXhis8B7LzuFODcB6IvRNs51idEjWM4"  # <-- ensure this token is your bot token

waiting = deque()
partners = {}
last_partner = {}

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💬 Chat", callback_data="chat"),
         InlineKeyboardButton("❌ Leave Chat", callback_data="leave")],
        [InlineKeyboardButton("⚠ Report", callback_data="report")],
        [InlineKeyboardButton("⚙ Settings", callback_data="settings"),
         InlineKeyboardButton("💎 Premium", callback_data="premium")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👋 Welcome! Choose an option:", reply_markup=reply_markup)

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.chat_id
    if user in partners:
        await update.message.reply_text("⚠ You are already in a chat. Use /exit to leave.")
        return
    if user in waiting:
        await update.message.reply_text("⏳ You are already waiting...")
        return
    if waiting:
        partner = waiting.popleft()
        partners[user] = partner
        partners[partner] = user
        last_partner[user] = partner
        last_partner[partner] = user
        await context.bot.send_message(chat_id=user, text="✅ Partner found! Say hi 👋")
        await context.bot.send_message(chat_id=partner, text="✅ Partner found! Say hi 👋")
    else:
        waiting.append(user)
        await update.message.reply_text("⏳ Waiting for a partner...")

async def exit_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.chat_id
    if user in waiting:
        waiting.remove(user)
        await update.message.reply_text("⛔ You left the queue.")
        return
    if user in partners:
        partner = partners[user]
        del partners[user]
        del partners[partner]
        await update.message.reply_text("❌ You left the chat.")
        await context.bot.send_message(chat_id=partner, text="⚠ Your partner left.")
        return
    await update.message.reply_text("You are not in a chat.")

async def forward_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.chat_id
    if user in partners:
        partner = partners[user]
        if update.message.text:
            await context.bot.send_message(chat_id=partner, text=update.message.text)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "chat":
        await query.edit_message_text("🔍 Searching for a partner...")
    elif query.data == "leave":
        await query.edit_message_text("❌ You left the chat.")
    elif query.data == "report":
        await query.edit_message_text("⚠ Report sent to admin.")
    elif query.data == "settings":
        await query.edit_message_text("⚙ Settings (Coming soon).")
    elif query.data == "premium":
        await query.edit_message_text("💎 Premium coming soon!")
 async def start_health_server():
    server = HTTPServer(('0.0.0.0', 10000), HealthCheckHandler)
    server.serve_forever()

# --- Main ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chat", chat))
    app.add_handler(CommandHandler("exit", exit_chat))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_messages))
    threading.Thread(target=start_health_server, daemon=True).start()

    print("🤖 Bot started polling...")
    app.run_polling()

if __name__ == "__main__":
    main()

