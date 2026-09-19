import logging
import re
import sqlite3
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION — CHANGE THESE
# ============================================================

BOT_TOKEN = "8620961418:AAF8ZtsjeOLiLb2Ye6AXvR4-pBVMYpVQsfs"
ADMIN_ID = 1985033905

OFFER_TEXT = """
🎉 Paytm Cashback Offer

Get up to ₹200 cashback on your first 5 transactions,
subject to eligibility and Paytm's applicable offer terms.

To check your eligibility, please send your 10-digit mobile number.
"""

# ============================================================
# DATABASE
# ============================================================

DB_FILE = "eligibility.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            phone TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            checked_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_user(telegram_id, username, phone):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users
        (telegram_id, username, phone, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        ON CONFLICT(telegram_id)
        DO UPDATE SET
            username = excluded.username,
            phone = excluded.phone,
            status = 'pending',
            created_at = excluded.created_at,
            checked_at = NULL
    """, (telegram_id, username, phone, datetime.utcnow().isoformat()))
    conn.commit()
    cursor.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0]

def update_status(request_id, status):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET status = ?, checked_at = ? WHERE id = ?", (status, datetime.utcnow().isoformat(), request_id))
    conn.commit()
    conn.close()

def get_user(request_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, phone, status FROM users WHERE id = ?", (request_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def get_existing_user(telegram_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, phone, status FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row

# ============================================================
# KEYBOARDS
# ============================================================

def admin_keyboard(request_id):
    keyboard = [[
        InlineKeyboardButton("✅ Eligible", callback_data=f"eligible:{request_id}"),
        InlineKeyboardButton("❌ Not Eligible", callback_data=f"noteligible:{request_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)

# ============================================================
# VALIDATION
# ============================================================
PHONE_REGEX = re.compile(r"^[6-9]\d{9}$")  # Indian 10-digit mobile

def is_valid_10_digit(text: str) -> bool:
    return bool(PHONE_REGEX.fullmatch(text.strip()))

# ============================================================
# USER COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(OFFER_TEXT)

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = get_existing_user(telegram_id)
    if not user:
        await update.message.reply_text("You haven't submitted a number yet.\n\nTap /start to begin.")
        return
    request_id, phone, status = user
    if status == "pending":
        message = "⏳ Your request is still being checked.\n\nPlease wait for the eligibility result."
    elif status == "eligible":
        message = "🎉 Your number has been marked as eligible!\n\nYou can proceed with the offer subject to Paytm's applicable terms and conditions."
    elif status == "noteligible":
        message = "❌ Your number has been marked as not eligible for this offer."
    else:
        message = "Status unavailable."
    await update.message.reply_text(message)

# ============================================================
# RECEIVE PHONE NUMBER - TEXT ONLY 10 DIGIT
# ============================================================

async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    # Remove spaces if user typed like "98 76 54 32 10"
    text_clean = re.sub(r"\s+", "", text)

    if not is_valid_10_digit(text_clean):
        await update.message.reply_text(
            "❌ Invalid number.\n\nPlease enter only a valid 10-digit mobile number (e.g. 9876543210)."
        )
        return

    telegram_id = update.effective_user.id
    username = update.effective_user.username
    phone = text_clean  # now guaranteed 10 digits

    request_id = save_user(telegram_id, username, phone)

    await update.message.reply_text(
        "✅ Your mobile number has been received.\n\n"
        "⏳ We are checking your eligibility manually. "
        "You will receive the result here.",
    )

    username_text = f"@{username}" if username else "No username"
    admin_message = (
        "🔔 NEW ELIGIBILITY REQUEST\n\n"
        f"Request ID: #{request_id}\n"
        f"👤 Telegram: {username_text}\n"
        f"🆔 User ID: `{telegram_id}`\n"
        f"📱 Mobile: `{phone}`\n\n"
        "Please manually check this number and choose:"
    )
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=admin_message,
        parse_mode="Markdown",
        reply_markup=admin_keyboard(request_id)
    )

# ============================================================
# ADMIN BUTTONS
# ============================================================

async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("You are not authorized.", show_alert=True)
        return
    action, request_id = query.data.split(":")
    request_id = int(request_id)
    user = get_user(request_id)
    if not user:
        await query.edit_message_text("❌ Request not found.")
        return
    telegram_id, username, phone, old_status = user
    if action == "eligible":
        update_status(request_id, "eligible")
        await query.edit_message_text(query.message.text + "\n\n✅ MARKED AS ELIGIBLE")
        await context.bot.send_message(chat_id=telegram_id, text="🎉 Congratulations!\n\nYour number has been marked as eligible for the offer.\n\nPlease follow the offer instructions \n\n You have recieved whatsapp message on the same number from Paytm just Click on Claim Now Button after setting up UPI on Paytm and you will see the Offer select the option from the given options for activating the offer and just pay min 10rs to get offer activated on your account. \n\n If no whatsapp arrived click on the link below and signup with the same number on paytm https://p.paytm.me/xCTH/tyc933xt?referral_unique_link=k2Jj3Ea&c=GG_FBU_Constant_Ref&src=gg  \n\n if no offer shown on Paytm just tap on claim now button again or on the link again Paytm's applicable terms and conditions. Paytm's applicable terms and conditions.")
    elif action == "noteligible":
        update_status(request_id, "noteligible")
        await query.edit_message_text(query.message.text + "\n\n❌ MARKED AS NOT ELIGIBLE")
        await context.bot.send_message(chat_id=telegram_id, text="❌ Unfortunately, your number is not eligible for this offer at the moment.")

# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    logging.error("Exception while handling update:", exc_info=context.error)

# ============================================================
# MAIN
# ============================================================

def main():
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", check_status))
    # Now accept only TEXT, not CONTACT
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone))
    application.add_handler(CallbackQueryHandler(admin_decision, pattern=r"^(eligible|noteligible):\d+$"))
    application.add_error_handler(error_handler)
    print("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
