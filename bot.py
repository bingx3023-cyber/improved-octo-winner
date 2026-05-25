import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp
from telegram import (
    Update,
    LabeledPrice,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    CallbackContext,
    filters,
)
from telegram.error import TelegramError

# ==================== Config ====================

BOT_TOKEN = "8103126532:AAEsHu9lVck3A6B4iapFEAOVe54K9NLCxjo"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ==================== Data Stores ====================

USER_SUBSCRIPTIONS = {}   # user_id -> expiration datetime
USER_PAYMENTS = {}        # user_id -> total stars paid
VIP_SUBSCRIPTIONS = {}    # user_id -> expiration datetime
USER_LAST_PAYMENT_ID = {} # user_id -> latest payment id

LAST_SIGNALS = {}

ENTRY_UPDATE_THRESHOLD = 0.005  # 0.5%

# ==================== CoinGecko API ====================

COINGECKO_API = "https://api.coingecko.com/api/v3"


async def fetch_all_coins():
    url = (
        f"{COINGECKO_API}/coins/markets"
        f"?vs_currency=usd"
        f"&order=market_cap_desc"
        f"&per_page=250"
        f"&page=1"
        f"&price_change_percentage=1h"
    )

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    return await resp.json()

                logger.error(f"Failed to fetch coins: HTTP {resp.status}")

        except Exception as e:
            logger.error(f"Exception fetching coins: {e}")

    return []


def analyze_coin(coin):
    price_change = coin.get("price_change_percentage_1h_in_currency") or 0
    current_price = coin.get("current_price") or 0
    symbol = (coin.get("symbol") or "N/A").upper()
    total_volume = coin.get("total_volume") or 0

    is_risky = abs(price_change) >= 5

    if total_volume < 10_000_000 or current_price <= 0:
        return None

    # LONG
    if price_change > 2:
        return {
            "symbol": symbol,
            "signal": "LONG",
            "entry": round(current_price * 0.995, 6),
            "stop_loss": round(current_price * 0.98, 6),
            "take_profit": round(current_price * 1.03, 6),
            "risk": is_risky,
        }

    # SHORT
    elif price_change < -2:
        return {
            "symbol": symbol,
            "signal": "SHORT",
            "entry": round(current_price * 1.005, 6),
            "stop_loss": round(current_price * 1.02, 6),
            "take_profit": round(current_price * 0.97, 6),
            "risk": is_risky,
        }

    return None


# ==================== Telegram Handlers ====================

async def start(update: Update, context: CallbackContext):
    keyboard = [
        [
            InlineKeyboardButton(
                "💳 خرید اشتراک 1 ماهه — 1 ⭐",
                callback_data="buy_subscription",
            )
        ],
        [
            InlineKeyboardButton(
                "💳 خرید اشتراک VIP — 2 ⭐",
                callback_data="buy_vip",
            )
        ],
        [
            InlineKeyboardButton(
                "👤 نمایش اشتراک من",
                callback_data="show_subscriptions",
            )
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "سلام 👋\n"
        "به ربات سیگنال ارز دیجیتال خوش اومدی.\n\n"
        "یکی از گزینه‌های زیر رو انتخاب کن:",
        reply_markup=reply_markup,
    )


async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query

    await query.answer()

    # خرید اشتراک عادی
    if query.data == "buy_subscription":

        prices = [LabeledPrice("اشتراک 1 ماهه سیگنال", 1)]

        await context.bot.send_invoice(
            chat_id=query.from_user.id,
            title="اشتراک سیگنال ارز دیجیتال",
            description="اشتراک ۱ ماهه سیگنال عادی",
            payload=f"subscription:{query.from_user.id}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="subscription_start",
        )

    # خرید VIP
    elif query.data == "buy_vip":

        prices = [LabeledPrice("اشتراک VIP", 2)]

        await context.bot.send_invoice(
            chat_id=query.from_user.id,
            title="اشتراک VIP",
            description="اشتراک ۱ ماهه سیگنال VIP",
            payload=f"vip:{query.from_user.id}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="vip_start",
        )

    # نمایش اشتراک‌ها
    elif query.data == "show_subscriptions":

        user_id = query.from_user.id

        normal_expire = USER_SUBSCRIPTIONS.get(user_id)
        vip_expire = VIP_SUBSCRIPTIONS.get(user_id)

        msg = "📅 وضعیت اشتراک‌ها:\n\n"

        msg += (
            f"🔹 اشتراک عادی:\n"
            f"{normal_expire if normal_expire else '❌ فعال نیست'}\n\n"
        )

        msg += (
            f"🔸 اشتراک VIP:\n"
            f"{vip_expire if vip_expire else '❌ فعال نیست'}"
        )

        await query.edit_message_text(msg)


# ==================== Payment ====================

async def precheckout_callback(update: Update, context: CallbackContext):

    query = update.pre_checkout_query

    if query.invoice_payload.startswith(("subscription:", "vip:")):
        await query.answer(ok=True)

    else:
        await query.answer(
            ok=False,
            error_message="پرداخت نامعتبر",
        )


async def successful_payment_callback(
    update: Update,
    context: CallbackContext,
):

    payment = update.message.successful_payment

    user_id = update.effective_user.id

    # اشتراک عادی
    if payment.invoice_payload.startswith("subscription:"):

        USER_SUBSCRIPTIONS[user_id] = (
            datetime.now() + timedelta(days=30)
        )

        USER_PAYMENTS[user_id] = (
            USER_PAYMENTS.get(user_id, 0)
            + payment.total_amount
        )

        USER_LAST_PAYMENT_ID[user_id] = (
            payment.telegram_payment_charge_id
        )

        await update.message.reply_text(
            f"✅ اشتراک عادی فعال شد.\n\n"
            f"📅 تا تاریخ:\n"
            f"{USER_SUBSCRIPTIONS[user_id]}"
        )

    # اشتراک VIP
    elif payment.invoice_payload.startswith("vip:"):

        VIP_SUBSCRIPTIONS[user_id] = (
            datetime.now() + timedelta(days=30)
        )

        USER_PAYMENTS[user_id] = (
            USER_PAYMENTS.get(user_id, 0)
            + payment.total_amount
        )

        USER_LAST_PAYMENT_ID[user_id] = (
            payment.telegram_payment_charge_id
        )

        await update.message.reply_text(
            f"🔥 اشتراک VIP فعال شد.\n\n"
            f"📅 تا تاریخ:\n"
            f"{VIP_SUBSCRIPTIONS[user_id]}"
        )


# ==================== Refund ====================

async def refund_command(update: Update, context: CallbackContext):

    user_id = update.effective_user.id

    charge_id = None

    if context.args:
        charge_id = context.args[0]

    elif user_id in USER_LAST_PAYMENT_ID:
        charge_id = USER_LAST_PAYMENT_ID[user_id]

    if not charge_id:
        await update.message.reply_text(
            "❌ هیچ پرداختی برای ریفاند پیدا نشد."
        )
        return

    try:

        success = await context.bot.refund_star_payment(
            user_id=user_id,
            telegram_payment_charge_id=charge_id,
        )

        if success:

            await update.message.reply_text(
                "✅ ریفاند با موفقیت انجام شد."
            )

            USER_SUBSCRIPTIONS.pop(user_id, None)
            VIP_SUBSCRIPTIONS.pop(user_id, None)
            USER_LAST_PAYMENT_ID.pop(user_id, None)

        else:

            await update.message.reply_text(
                "❌ ریفاند انجام نشد."
            )

    except TelegramError as e:

        await update.message.reply_text(
            f"⚠️ خطا در ریفاند:\n{e}"
        )


# ==================== Signal Sender ====================

async def send_or_update_signal_for_user(
    context: CallbackContext,
    user_id: int,
    coins_data,
):

    now = datetime.now()

    expire = USER_SUBSCRIPTIONS.get(user_id)

    if not expire or expire < now:
        return

    user_map = LAST_SIGNALS.setdefault(user_id, {})

    sent_new = False

    for coin in coins_data:

        sig = analyze_coin(coin)

        if not sig:
            continue

        symbol = sig["symbol"]
        entry = sig["entry"]
        risk_now = sig["risk"]
        tp = sig["take_profit"]
        sl = sig["stop_loss"]
        side = sig["signal"]

        # اگر سیگنال قبلاً فعال بوده
        if symbol in user_map and user_map[symbol]["active"]:

            prev = user_map[symbol]

            # تغییر نقطه ورود
            if (
                abs(entry - prev["entry"])
                / prev["entry"]
                >= ENTRY_UPDATE_THRESHOLD
            ):

                try:

                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"⚡ {symbol}\n\n"
                            f"نقطه ورود آپدیت شد ✅\n"
                            f"Entry جدید: {entry}"
                        ),
                        reply_to_message_id=prev["message_id"],
                    )

                    prev["entry"] = entry

                except Exception as e:
                    logger.error(
                        f"Failed updating signal {symbol}: {e}"
                    )

            continue

        # ارسال سیگنال جدید
        if not sent_new:

            text = (
                f"{'🚨 #ریسکی\n\n' if risk_now else ''}"
                f"📊 سیگنال ارز دیجیتال\n\n"
                f"💰 نماد: {symbol}\n"
                f"📈 نوع: {side}\n\n"
                f"🎯 نقطه ورود: {entry}\n"
                f"🛑 حد ضرر: {sl}\n"
                f"🚀 هدف: {tp}"
            )

            try:

                msg = await context.bot.send_message(
                    chat_id=user_id,
                    text=text,
                )

                user_map[symbol] = {
                    "message_id": msg.message_id,
                    "entry": entry,
                    "risk": risk_now,
                    "signal": side,
                    "take_profit": tp,
                    "stop_loss": sl,
                    "active": True,
                }

                sent_new = True

            except Exception as e:
                logger.error(
                    f"Failed sending signal {symbol}: {e}"
                )


# ==================== Jobs ====================

async def signal_job(context: CallbackContext):

    coins = await fetch_all_coins()

    if not coins:
        return

    for user_id in list(USER_SUBSCRIPTIONS.keys()):

        await send_or_update_signal_for_user(
            context,
            user_id,
            coins,
        )


async def check_targets_job(context: CallbackContext):

    coins = await fetch_all_coins()

    if not coins:
        return

    price_map = {
        (c.get("symbol") or "").upper():
        (c.get("current_price") or 0)
        for c in coins
    }

    for user_id, symbols in list(LAST_SIGNALS.items()):

        for symbol, info in list(symbols.items()):

            if not info.get("active"):
                continue

            price = price_map.get(symbol)

            if not price:
                continue

            side = info.get("signal")
            tp = info.get("take_profit")

            touched = (
                (side == "LONG" and price >= tp)
                or
                (side == "SHORT" and price <= tp)
            )

            if touched:

                try:

                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🎯 تارگت {symbol} تاچ شد ✅",
                        reply_to_message_id=info["message_id"],
                    )

                    info["active"] = False

                except Exception as e:
                    logger.error(
                        f"Failed sending TP touched {symbol}: {e}"
                    )


# ==================== VIP Signals ====================

async def vip_signal_job(context: CallbackContext):

    coins = await fetch_all_coins()

    if not coins:
        return

    best = max(
        coins,
        key=lambda c: (
            c.get("total_volume", 0)
            * abs(
                c.get(
                    "price_change_percentage_1h_in_currency"
                ) or 0
            )
        ),
    )

    sig = analyze_coin(best)

    if not sig:
        return

    text = (
        f"🚀 سیگنال VIP\n\n"
        f"💰 نماد: {sig['symbol']}\n"
        f"📈 نوع: {sig['signal']}\n\n"
        f"🎯 ورود: {sig['entry']}\n"
        f"🛑 حد ضرر: {sig['stop_loss']}\n"
        f"🚀 تارگت: {sig['take_profit']}"
    )

    now = datetime.now()

    for user_id, expire in list(VIP_SUBSCRIPTIONS.items()):

        if expire >= now:

            try:

                await context.bot.send_message(
                    chat_id=user_id,
                    text=text,
                )

            except Exception as e:
                logger.error(
                    f"Failed sending VIP signal: {e}"
                )


# ==================== Main ====================

def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refund", refund_command))

    # Buttons
    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    # Payments
    app.add_handler(
        PreCheckoutQueryHandler(
            precheckout_callback
        )
    )

    app.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            successful_payment_callback,
        )
    )

    # Jobs
    app.job_queue.run_repeating(
        signal_job,
        interval=300,
        first=10,
    )

    app.job_queue.run_repeating(
        check_targets_job,
        interval=60,
        first=30,
    )

    app.job_queue.run_repeating(
        vip_signal_job,
        interval=18000,
        first=20,
    )

    logger.info("Bot started successfully")

    app.run_polling()


if __name__ == "__main__":
    main()
