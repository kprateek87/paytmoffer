
import os
import re
import sqlite3
import logging
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "1985033905"))
DB_PATH = os.environ.get("DB_PATH", "customers.db")

PHONE_PATTERN = re.compile(r"^[6-9][0-9]{9}$")


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.execute("PRAGMA busy_timeout = 15000")
    return db


def init_db():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                chat_id INTEGER PRIMARY KEY,
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)


def save_customer(chat_id, phone):
    with connect_db() as db:
        db.execute("""
            INSERT INTO customers (chat_id, phone, status, updated_at)
            VALUES (?, ?, 'pending', CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                phone = excluded.phone,
                status = 'pending',
                updated_at = CURRENT_TIMESTAMP
        """, (chat_id, phone))


def get_customer(chat_id):
    with connect_db() as db:
        return db.execute(
            "SELECT phone, status FROM customers WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()


def update_status(chat_id, expected_status, new_status):
    """Prevent two admin clicks from processing the same request."""
    with connect_db() as db:
        cursor = db.execute("""
            UPDATE customers
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = ? AND status = ?
        """, (new_status, chat_id, expected_status))
        return cursor.rowcount == 1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["awaiting_consent"] = True

    message = (
        "🎉 Welcome to Cashback Eligibility Review!\n\n"
        "Hello and welcome! 👋\n\n"
        "You can submit a request to have your eligibility "
        "for an offer reviewed manually.\n\n"
        "✨ How It Works\n\n"
        "1️⃣ Provide your 10-digit Indian mobile number.\n"
        "2️⃣ An authorized reviewer will assess your request.\n"
        "3️⃣ You will receive a message with the review outcome.\n\n"
        "🔒 Privacy & Security\n"
        "Your number will be shared with the designated reviewer "
        "for this request. Do not send OTPs, UPI PINs, passwords, "
        "or banking credentials.\n\n"
        "📌 Submitting a request does not guarantee cashback. "
        "This bot does not itself activate any Paytm offer.\n\n"
        "Do you consent to sharing your mobile number with the "
        "reviewer for this eligibility review?"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "I Agree & Continue", callback_data="consent:yes"
            ),
            InlineKeyboardButton(
                "Cancel", callback_data="consent:no"
            ),
        ]
    ])

    await update.effective_message.reply_text(
        message, reply_markup=keyboard
    )


async def handle_consent(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    if query.data == "consent:no":
        context.user_data.clear()
        await query.edit_message_text(
            "No problem. Your request has not been submitted. "
            "You can use /start if you change your mind."
        )
        return

    if query.data == "consent:yes":
        context.user_data["awaiting_phone"] = True
        context.user_data.pop("awaiting_consent", None)
        await query.edit_message_text(
            "Thank you for your consent.\n\n"
            "Please enter your 10-digit Indian mobile number "
            "starting with 6, 7, 8, or 9."
        )


async def receive_phone(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not context.user_data.get("awaiting_phone"):
        await update.message.reply_text(
            "Please use /start to begin or submit a new request."
        )
        return

    phone = update.message.text.strip()

    if not PHONE_PATTERN.fullmatch(phone):
        await update.message.reply_text(
            "❌ Invalid number format.\n\n"
            "Please enter exactly 10 digits, starting with "
            "6, 7, 8, or 9. Do not include +91 or spaces."
        )
        return

    user = update.effective_user
    chat_id = update.effective_chat.id

    save_customer(chat_id, phone)
    context.user_data["awaiting_phone"] = False

    await update.message.reply_text(
        "✅ Your request has been submitted for manual review.\n\n"
        "You will receive an update after the reviewer has "
        "checked your request."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Eligible", callback_data=f"review:eligible:{chat_id}"
            ),
            InlineKeyboardButton(
                "❌ Not Eligible", callback_data=f"review:rejected:{chat_id}"
            ),
        ]
    ])

    admin_message = (
        "📋 NEW ELIGIBILITY REVIEW\n\n"
        f"Name: {user.full_name}\n"
        f"Telegram ID: {user.id}\n"
        f"Mobile: {phone}\n"
        f"Request ID: {chat_id}\n\n"
        "Verify the request using your authorized process "
        "before selecting an outcome."
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_message,
            reply_markup=keyboard,
        )
    except Exception:
        logging.exception("Admin notification failed")
        await update.message.reply_text(
            "Your request is saved, but the reviewer could not "
            "be notified. Please contact support."
        )


async def handle_review(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        await query.answer(
            "You are not authorized to review requests.",
            show_alert=True,
        )
        return

    await query.answer()

    try:
        _, decision, chat_id_text = query.data.split(":")
        customer_id = int(chat_id_text)
    except (ValueError, AttributeError):
        await query.edit_message_text("Invalid review request.")
        return

    customer = get_customer(customer_id)

    if not customer:
        await query.edit_message_text("Customer record not found.")
        return

    phone, status = customer

    if status != "pending":
        await query.edit_message_text(
            f"This request has already been reviewed: {status}."
        )
        return

    if decision == "eligible":
        new_status = "eligible"
        customer_message = (
            "Thank you. Your request has been reviewed and marked "
            "eligible by the reviewer.\n\n"
            "Please verify the offer availability and terms through "
            "an official, authorized channel. This message does not "
            "confirm that cashback has been credited or activated."
        )
    elif decision == "rejected":
        new_status = "not_eligible"
        customer_message = (
            "Thank you for your time.\n\n"
            "Based on the current review, we cannot confirm your "
            "eligibility for this offer.\n\n"
            "If you believe this outcome is incorrect, you may "
            "submit a new review request using /start and provide "
            "another number you are authorized to use."
        )
    else:
        await query.edit_message_text("Unknown review decision.")
        return

    changed = update_status(customer_id, "pending", new_status)

    if not changed:
        await query.edit_message_text(
            "This request has already been processed."
        )
        return

    try:
        await context.bot.send_message(
            chat_id=customer_id,
            text=customer_message,
        )
        await query.edit_message_text(
            f"Review saved: {new_status}\nMobile: {phone}\n"
            "Customer notification sent."
        )
    except Exception:
        logging.exception("Customer notification failed")
        await query.edit_message_text(
            f"Review saved: {new_status}\nMobile: {phone}\n"
            "However, Telegram could not deliver the notification."
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Your current submission flow has been cancelled. "
        "Use /start whenever you want to begin again."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Your Telegram numeric ID is: {update.effective_user.id}"
    )


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
):
    logging.error(
        "Unhandled bot error",
        exc_info=context.error,
    )


def main():
    if not BOT_TOKEN.strip():
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    if ADMIN_ID <= 0:
        raise RuntimeError("Set a valid ADMIN_ID.")

    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(
        CallbackQueryHandler(handle_consent, pattern=r"^consent:")
    )
    app.add_handler(
        CallbackQueryHandler(handle_review, pattern=r"^review:")
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)
    )
    app.add_error_handler(error_handler)

    logging.info("Eligibility review bot is starting.")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
