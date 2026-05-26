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

SIGNAL_CHECK_INTERVAL = 60
PRICE_CHECK_INTERVAL = 15
TARGET_CHECK_INTERVAL = 30
VIP_SIGNAL_INTERVAL = 3600

ENTRY_ZONE_PERCENT = 0.3

MIN_VOLUME = 20_000_000
MIN_MARKET_CAP = 100_000_000
MIN_PRICE_CHANGE = 1.5

# کوoldown برای هر نماد (ساعت)
COOLDOWN_HOURS = 4

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ==================== Data Stores ====================

USER_SUBSCRIPTIONS = {}
USER_PAYMENTS = {}
VIP_SUBSCRIPTIONS = {}
USER_LAST_PAYMENT_ID = {}

# ✅ کلید: نماد - مقدار: زمان آخرین سیگنال
LAST_SIGNAL_TIME = {}  # {symbol: datetime}

PENDING_CONFIRMATION = {}
ACTIVE_SIGNALS = {}

# ==================== توابع کمکی ====================

def can_send_signal_for_symbol(symbol: str) -> tuple:
    """بررسی کن برای این نماد توی 4 ساعت اخیر سیگنال فرستاده شده یا نه"""
    if symbol in LAST_SIGNAL_TIME:
        time_diff = (datetime.now() - LAST_SIGNAL_TIME[symbol]).total_seconds() / 3600
        if time_diff < COOLDOWN_HOURS:
            return False, round(COOLDOWN_HOURS - time_diff, 1)
    return True, 0

def mark_signal_sent(symbol: str):
    """ثبت کن که برای این نماد سیگنال فرستاده شد"""
    LAST_SIGNAL_TIME[symbol] = datetime.now()

# ==================== CoinGecko API ====================

COINGECKO_API = "https://api.coingecko.com/api/v3"

async def fetch_all_coins():
    url = (
        f"{COINGECKO_API}/coins/markets"
        f"?vs_currency=usd"
        f"&order=market_cap_desc"
        f"&per_page=250"
        f"&page=1"
        f"&price_change_percentage=1h,24h"
        f"&sparkline=false"
    )

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"Failed to fetch coins: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"Exception fetching coins: {e}")

    return []

def analyze_coin(coin):
    price_change_1h = coin.get("price_change_percentage_1h_in_currency") or 0
    price_change_24h = coin.get("price_change_percentage_24h_in_currency") or 0
    current_price = coin.get("current_price") or 0
    symbol = (coin.get("symbol") or "N/A").upper()
    total_volume = coin.get("total_volume") or 0
    market_cap = coin.get("market_cap") or 0

    if total_volume < MIN_VOLUME:
        return None
    if market_cap < MIN_MARKET_CAP:
        return None
    if current_price <= 0:
        return None

    is_risky = abs(price_change_1h) >= 5
    strong_trend = abs(price_change_24h) >= 12

    atr_style = abs(price_change_1h) / 100 * current_price if price_change_1h != 0 else current_price * 0.01
    
    if price_change_1h > MIN_PRICE_CHANGE and price_change_24h > -3:
        return {
            "symbol": symbol,
            "signal": "LONG",
            "entry": round(current_price, 6),
            "stop_loss": round(current_price - (atr_style * 1.5), 6),
            "take_profit": round(current_price + (atr_style * 3.5), 6),
            "risk": is_risky,
            "strong": strong_trend,
            "current_price": current_price,
            "volume": total_volume,
            "change_1h": price_change_1h,
            "change_24h": price_change_24h,
            "entry_zone_low": round(current_price * (1 - ENTRY_ZONE_PERCENT/100), 6),
            "entry_zone_high": round(current_price * (1 + ENTRY_ZONE_PERCENT/100), 6),
        }

    elif price_change_1h < -MIN_PRICE_CHANGE and price_change_24h < 3:
        return {
            "symbol": symbol,
            "signal": "SHORT",
            "entry": round(current_price, 6),
            "stop_loss": round(current_price + (atr_style * 1.5), 6),
            "take_profit": round(current_price - (atr_style * 3.5), 6),
            "risk": is_risky,
            "strong": strong_trend,
            "current_price": current_price,
            "volume": total_volume,
            "change_1h": price_change_1h,
            "change_24h": price_change_24h,
            "entry_zone_low": round(current_price * (1 - ENTRY_ZONE_PERCENT/100), 6),
            "entry_zone_high": round(current_price * (1 + ENTRY_ZONE_PERCENT/100), 6),
        }

    return None

def is_price_in_entry_zone(current_price, signal):
    if signal["signal"] == "LONG":
        return current_price <= signal["entry_zone_high"]
    else:
        return current_price >= signal["entry_zone_low"]

# ==================== Telegram Handlers ====================

async def start(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("💳 اشتراک 1 ماهه — 1 ⭐", callback_data="buy_subscription")],
        [InlineKeyboardButton("🔥 اشتراک VIP — 2 ⭐", callback_data="buy_vip")],
        [InlineKeyboardButton("👤 وضعیت اشتراک من", callback_data="show_subscriptions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "✨ **ربات سیگنال فوتچرز** ✨\n\n"
        "📊 هر نماد حداکثر **هر 4 ساعت** یک سیگنال\n"
        "🎯 بدون سیگنال تکراری\n"
        "🔘 تایید/رد با ریپلای\n\n"
        "👇 انتخاب کن:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if query.data == "buy_subscription":
        prices = [LabeledPrice("اشتراک 1 ماهه", 1)]
        await context.bot.send_invoice(
            chat_id=user_id,
            title="✨ اشتراک سیگنال فوتچرز",
            description="دریافت سیگنال‌های نهایی فیوچرز",
            payload=f"subscription:{user_id}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="subscription_start",
        )

    elif query.data == "buy_vip":
        prices = [LabeledPrice("اشتراک VIP", 2)]
        await context.bot.send_invoice(
            chat_id=user_id,
            title="🔥 اشتراک VIP",
            description="سیگنال‌های ویژه",
            payload=f"vip:{user_id}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="vip_start",
        )

    elif query.data == "show_subscriptions":
        normal_expire = USER_SUBSCRIPTIONS.get(user_id)
        vip_expire = VIP_SUBSCRIPTIONS.get(user_id)
        msg = "📅 **وضعیت اشتراک‌ها:**\n\n"
        msg += f"🔹 عادی:\n{normal_expire if normal_expire else '❌ فعال نیست'}\n\n"
        msg += f"🔸 VIP:\n{vip_expire if vip_expire else '❌ فعال نیست'}"
        await query.edit_message_text(msg, parse_mode="Markdown")

    elif query.data.startswith("confirm_"):
        symbol = query.data.replace("confirm_", "")
        if user_id in PENDING_CONFIRMATION and symbol in PENDING_CONFIRMATION[user_id]:
            signal_data = PENDING_CONFIRMATION[user_id][symbol]["signal"]
            original_msg_id = PENDING_CONFIRMATION[user_id][symbol]["message_id"]
            
            ACTIVE_SIGNALS.setdefault(user_id, {})[symbol] = {
                "signal": signal_data,
                "message_id": original_msg_id,
            }
            del PENDING_CONFIRMATION[user_id][symbol]
            
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"✅ **ورود {symbol} تایید شد!** ✅\n\n"
                    f"📊 نوع: {signal_data['signal']}\n"
                    f"🎯 ورود: `{signal_data['entry']}`\n"
                    f"🛑 SL: `{signal_data['stop_loss']}`\n"
                    f"🚀 TP: `{signal_data['take_profit']}`"
                ),
                parse_mode="Markdown",
                reply_to_message_id=original_msg_id,
            )
        else:
            await query.edit_message_text("❌ سیگنال منقضی شده.")

    elif query.data.startswith("reject_"):
        symbol = query.data.replace("reject_", "")
        if user_id in PENDING_CONFIRMATION and symbol in PENDING_CONFIRMATION[user_id]:
            original_msg_id = PENDING_CONFIRMATION[user_id][symbol]["message_id"]
            del PENDING_CONFIRMATION[user_id][symbol]
            
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ **ورود {symbol} رد شد** ❌",
                reply_to_message_id=original_msg_id,
            )

# ==================== Payment ====================

async def precheckout_callback(update: Update, context: CallbackContext):
    query = update.pre_checkout_query
    if query.invoice_payload.startswith(("subscription:", "vip:")):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="پرداخت نامعتبر")

async def successful_payment_callback(update: Update, context: CallbackContext):
    payment = update.message.successful_payment
    user_id = update.effective_user.id

    if payment.invoice_payload.startswith("subscription:"):
        USER_SUBSCRIPTIONS[user_id] = datetime.now() + timedelta(days=30)
        await update.message.reply_text(f"✅ اشتراک عادی فعال شد تا {USER_SUBSCRIPTIONS[user_id]}")
    elif payment.invoice_payload.startswith("vip:"):
        VIP_SUBSCRIPTIONS[user_id] = datetime.now() + timedelta(days=30)
        await update.message.reply_text(f"🔥 اشتراک VIP فعال شد تا {VIP_SUBSCRIPTIONS[user_id]}")

async def refund_command(update: Update, context: CallbackContext):
    await update.message.reply_text("برای ریفاند با پشتیبانی تماس بگیرید.")

# ==================== ارسال سیگنال نهایی ====================

async def send_final_signal(context: CallbackContext, user_id: int, signal: dict):
    direction = "📈" if signal["signal"] == "LONG" else "📉"
    risk_tag = "🚨 #ریسکی" if signal["risk"] else "✅ #استاندارد"
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ تایید ورود {signal['symbol']} ✅", callback_data=f"confirm_{signal['symbol']}"),
            InlineKeyboardButton(f"❌ رد سیگنال ❌", callback_data=f"reject_{signal['symbol']}"),
        ]
    ])
    
    text = (
        f"✨ **سیگنال نهایی {signal['symbol']}** ✨\n\n"
        f"{direction} نوع: **{signal['signal']}**\n"
        f"💰 نماد: `{signal['symbol']}`\n\n"
        f"🎯 ورود: `{signal['entry']}`\n"
        f"🛑 SL: `{signal['stop_loss']}`\n"
        f"🚀 TP: `{signal['take_profit']}`\n\n"
        f"📊 محدوده ورود:\n`{signal['entry_zone_low']}` ➜ `{signal['entry_zone_high']}`\n\n"
        f"💵 قیمت لحظه‌ای: `${signal['current_price']}`\n"
        f"📈 1h: `{signal['change_1h']:+.2f}%` | 24h: `{signal['change_24h']:+.2f}%`\n"
        f"💎 حجم: `${signal['volume']:,.0f}`\n\n"
        f"🏷️ {risk_tag}\n\n"
        f"⏳ منتظر ورود به محدوده `{signal['entry_zone_low']}` تا `{signal['entry_zone_high']}`...\n\n"
        f"🔘 وقتی وارد شد، دکمه تایید رو بزن."
    )
    
    try:
        msg = await context.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        
        PENDING_CONFIRMATION.setdefault(user_id, {})[signal["symbol"]] = {
            "signal": signal,
            "message_id": msg.message_id,
            "timestamp": datetime.now(),
        }
        
        logger.info(f"✅ Signal: {signal['symbol']} {signal['signal']} entry:{signal['entry']}")
        return True
    except Exception as e:
        logger.error(f"Send error: {e}")
        return False

# ==================== بررسی قیمت و اطلاع ====================

async def check_price_for_entry_job(context: CallbackContext):
    coins = await fetch_all_coins()
    if not coins:
        return
    
    price_map = {(c.get("symbol") or "").upper(): (c.get("current_price") or 0) for c in coins}
    
    for user_id, signals in list(PENDING_CONFIRMATION.items()):
        for symbol, data in list(signals.items()):
            signal = data["signal"]
            original_msg_id = data["message_id"]
            current_price = price_map.get(symbol)
            
            if not current_price:
                continue
            
            in_zone = is_price_in_entry_zone(current_price, signal)
            already_notified = data.get("notified", False)
            
            if in_zone and not already_notified:
                data["notified"] = True
                
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🟢 **قیمت {symbol} وارد محدوده شد!** 🟢\n\n"
                        f"💰 قیمت فعلی: `${current_price}`\n"
                        f"✅ دکمه تایید رو بزن."
                    ),
                    parse_mode="Markdown",
                    reply_to_message_id=original_msg_id,
                )
                logger.info(f"🟢 Entry zone: {symbol}")
            
            if (datetime.now() - data["timestamp"]).seconds > 7200:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⏰ سیگنال {symbol} منقضی شد.",
                    reply_to_message_id=original_msg_id,
                )
                del PENDING_CONFIRMATION[user_id][symbol]

# ==================== تولید سیگنال (بدون تکرار) ====================

async def signal_job_fast(context: CallbackContext):
    coins = await fetch_all_coins()
    if not coins:
        return
    
    high_volume_coins = [c for c in coins if (c.get("total_volume") or 0) > MIN_VOLUME]
    
    signals_found = []
    for coin in high_volume_coins[:50]:
        signal = analyze_coin(coin)
        if signal:
            signals_found.append(signal)
    
    now = datetime.now()
    
    for signal in signals_found:
        symbol = signal["symbol"]
        
        # ✅ بررسی کن برای این نماد توی 4 ساعت اخیر سیگنال فرستاده شده یا نه
        can_send, wait_hours = can_send_signal_for_symbol(symbol)
        
        if not can_send:
            logger.info(f"⏸️ Skipping {symbol} - cooldown {wait_hours}h left")
            continue
        
        # ارسال به کاربران
        for user_id, expire in list(USER_SUBSCRIPTIONS.items()):
            if expire >= now:
                await send_final_signal(context, user_id, signal)
                await asyncio.sleep(0.2)
        
        for user_id, expire in list(VIP_SUBSCRIPTIONS.items()):
            if expire >= now:
                await send_final_signal(context, user_id, signal)
                await asyncio.sleep(0.1)
        
        # ثبت زمان آخرین سیگنال برای این نماد
        mark_signal_sent(symbol)
        
        logger.info(f"📤 Sent signal for {symbol}")

# ==================== بررسی تارگت و حد ضرر ====================

async def check_targets_job(context: CallbackContext):
    coins = await fetch_all_coins()
    if not coins:
        return
    
    price_map = {(c.get("symbol") or "").upper(): (c.get("current_price") or 0) for c in coins}
    
    for user_id, signals in list(ACTIVE_SIGNALS.items()):
        for symbol, data in list(signals.items()):
            signal = data["signal"]
            original_msg_id = data["message_id"]
            current_price = price_map.get(symbol)
            
            if not current_price:
                continue
            
            tp = signal["take_profit"]
            sl = signal["stop_loss"]
            entry = signal["entry"]
            
            if signal["signal"] == "LONG":
                hit_tp = current_price >= tp
                hit_sl = current_price <= sl
                profit_pct = ((current_price - entry) / entry) * 100
            else:
                hit_tp = current_price <= tp
                hit_sl = current_price >= sl
                profit_pct = ((entry - current_price) / entry) * 100
            
            if hit_tp:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎯 **تارگت {symbol} تاچ شد!** ✅\n\n📈 سود: `+{profit_pct:.2f}%`",
                    parse_mode="Markdown",
                    reply_to_message_id=original_msg_id,
                )
                del ACTIVE_SIGNALS[user_id][symbol]
            elif hit_sl:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🛑 **حد ضرر {symbol} فعال شد!**\n\n📉 ضرر: `{profit_pct:.2f}%`",
                    parse_mode="Markdown",
                    reply_to_message_id=original_msg_id,
                )
                del ACTIVE_SIGNALS[user_id][symbol]

# ==================== VIP ====================

async def vip_signal_job(context: CallbackContext):
    coins = await fetch_all_coins()
    if not coins:
        return
    
    candidates = []
    for coin in coins:
        sig = analyze_coin(coin)
        if sig:
            score = (sig["volume"] / 1_000_000) * (3 if sig["strong"] else 1)
            candidates.append((score, sig))
    
    if not candidates:
        return
    
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]
    
    can_send, _ = can_send_signal_for_symbol(best["symbol"])
    if not can_send:
        return
    
    vip_text = f"👑 VIP: {best['symbol']} {best['signal']}\n🎯 ورود: {best['entry']}\n🛑 SL: {best['stop_loss']}\n🚀 TP: {best['take_profit']}"
    
    now = datetime.now()
    for user_id, expire in list(VIP_SUBSCRIPTIONS.items()):
        if expire >= now:
            await context.bot.send_message(chat_id=user_id, text=vip_text)
    
    mark_signal_sent(best["symbol"])

# ==================== Main ====================

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refund", refund_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    
    app.job_queue.run_repeating(signal_job_fast, interval=SIGNAL_CHECK_INTERVAL, first=5)
    app.job_queue.run_repeating(check_price_for_entry_job, interval=PRICE_CHECK_INTERVAL, first=3)
    app.job_queue.run_repeating(check_targets_job, interval=TARGET_CHECK_INTERVAL, first=10)
    app.job_queue.run_repeating(vip_signal_job, interval=VIP_SIGNAL_INTERVAL, first=30)
    
    logger.info("=" * 50)
    logger.info(f"✅ ربات اجرا شد - هر نماد هر {COOLDOWN_HOURS} ساعت یک بار")
    logger.info("=" * 50)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
