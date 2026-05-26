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

# ⚡ تنظیمات
SIGNAL_CHECK_INTERVAL = 60      # هر 1 دقیقه
PRICE_CHECK_INTERVAL = 15       # هر 15 ثانیه
TARGET_CHECK_INTERVAL = 30      # هر 30 ثانیه
VIP_SIGNAL_INTERVAL = 3600      # هر 1 ساعت

# محدوده ورود 0.3%
ENTRY_ZONE_PERCENT = 0.3

# فیلترهای سختگیرانه برای سیگنال عالی (برمیگردونم به حالت قبلی)
MIN_VOLUME = 20_000_000      # حداقل 20 میلیون دلار
MIN_MARKET_CAP = 100_000_000 # حداقل 100 میلیون دلار
MIN_PRICE_CHANGE = 1.5       # حداقل تغییر 1.5% در 1 ساعت

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

PENDING_ENTRY_SIGNALS = {}
ACTIVE_SIGNALS = {}

# برای لاگ کردن آخرین وضعیت
LAST_SIGNAL_LOG = {}

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
    """تحلیل و تولید سیگنال با کیفیت بالا"""
    price_change_1h = coin.get("price_change_percentage_1h_in_currency") or 0
    price_change_24h = coin.get("price_change_percentage_24h_in_currency") or 0
    current_price = coin.get("current_price") or 0
    symbol = (coin.get("symbol") or "N/A").upper()
    total_volume = coin.get("total_volume") or 0
    market_cap = coin.get("market_cap") or 0

    # فیلترهای سختگیرانه برای کیفیت بالا
    if total_volume < MIN_VOLUME:
        return None
    if market_cap < MIN_MARKET_CAP:
        return None
    if current_price <= 0:
        return None

    is_risky = abs(price_change_1h) >= 5
    strong_trend = abs(price_change_24h) >= 12

    # محاسبه ATR-like برای حد ضرر و تارگت
    atr_style = abs(price_change_1h) / 100 * current_price if price_change_1h != 0 else current_price * 0.01
    
    # LONG SIGNAL (بازدهی بالاتر نیاز دارم)
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
            "market_cap": market_cap,
            "change_1h": price_change_1h,
            "change_24h": price_change_24h,
            "entry_zone_low": round(current_price * (1 - ENTRY_ZONE_PERCENT/100), 6),
            "entry_zone_high": round(current_price * (1 + ENTRY_ZONE_PERCENT/100), 6),
        }

    # SHORT SIGNAL
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
            "market_cap": market_cap,
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
        "⚡ سیگنال‌های باکیفیت برای معاملات فیوچرز\n"
        "🎯 فقط سیگنال‌های عالی ارسال می‌شوند\n"
        "✅ تایید ورود هوشمند\n"
        "💎 ایموجی‌های پریمیوم\n\n"
        "👇 یکی از گزینه‌ها رو انتخاب کن:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if query.data == "buy_subscription":
        prices = [LabeledPrice("اشتراک 1 ماهه سیگنال فوتچرز", 1)]
        await context.bot.send_invoice(
            chat_id=user_id,
            title="✨ اشتراک سیگنال فوتچرز",
            description="دریافت سیگنال‌های باکیفیت فیوچرز",
            payload=f"subscription:{user_id}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="subscription_start",
        )

    elif query.data == "buy_vip":
        prices = [LabeledPrice("اشتراک VIP فوتچرز", 2)]
        await context.bot.send_invoice(
            chat_id=user_id,
            title="🔥 اشتراک VIP فوتچرز",
            description="سیگنال‌های ویژه + اولویت",
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
        msg += f"🔹 اشتراک عادی:\n{normal_expire if normal_expire else '❌ فعال نیست'}\n\n"
        msg += f"🔸 اشتراک VIP:\n{vip_expire if vip_expire else '❌ فعال نیست'}"
        await query.edit_message_text(msg, parse_mode="Markdown")

    elif query.data.startswith("confirm_entry_"):
        symbol = query.data.replace("confirm_entry_", "")
        if user_id in PENDING_ENTRY_SIGNALS and symbol in PENDING_ENTRY_SIGNALS[user_id]:
            signal = PENDING_ENTRY_SIGNALS[user_id][symbol]
            ACTIVE_SIGNALS.setdefault(user_id, {})[symbol] = {
                **signal,
                "confirmed_at": datetime.now(),
            }
            del PENDING_ENTRY_SIGNALS[user_id][symbol]
            await query.edit_message_text(
                f"✅ **ورود {symbol} تایید شد!** ✅\n\n"
                f"📊 **نوع:** {signal['signal']}\n"
                f"🎯 **ورود:** `{signal['entry']}`\n"
                f"🛑 **SL:** `{signal['stop_loss']}`\n"
                f"🚀 **TP:** `{signal['take_profit']}`\n\n"
                f"💎 معامله فعال شد.",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text("❌ سیگنال منقضی شده.")

    elif query.data.startswith("reject_entry_"):
        symbol = query.data.replace("reject_entry_", "")
        if user_id in PENDING_ENTRY_SIGNALS and symbol in PENDING_ENTRY_SIGNALS[user_id]:
            del PENDING_ENTRY_SIGNALS[user_id][symbol]
            await query.edit_message_text(
                f"❌ **ورود {symbol} رد شد** ❌\n\n"
                f"⏳ سیگنال بسته شد.",
                parse_mode="Markdown",
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
        USER_PAYMENTS[user_id] = USER_PAYMENTS.get(user_id, 0) + payment.total_amount
        USER_LAST_PAYMENT_ID[user_id] = payment.telegram_payment_charge_id
        await update.message.reply_text(
            f"✅ **اشتراک عادی فعال شد!** ✨\n\n📅 تا تاریخ: `{USER_SUBSCRIPTIONS[user_id]}`",
            parse_mode="Markdown",
        )

    elif payment.invoice_payload.startswith("vip:"):
        VIP_SUBSCRIPTIONS[user_id] = datetime.now() + timedelta(days=30)
        USER_PAYMENTS[user_id] = USER_PAYMENTS.get(user_id, 0) + payment.total_amount
        USER_LAST_PAYMENT_ID[user_id] = payment.telegram_payment_charge_id
        await update.message.reply_text(
            f"🔥 **اشتراک VIP فعال شد!** 🔥\n\n📅 تا تاریخ: `{VIP_SUBSCRIPTIONS[user_id]}`",
            parse_mode="Markdown",
        )

async def refund_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    charge_id = None
    if context.args:
        charge_id = context.args[0]
    elif user_id in USER_LAST_PAYMENT_ID:
        charge_id = USER_LAST_PAYMENT_ID[user_id]
    if not charge_id:
        await update.message.reply_text("❌ هیچ پرداختی پیدا نشد.")
        return
    try:
        success = await context.bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
        if success:
            await update.message.reply_text("✅ ریفاند انجام شد.")
            USER_SUBSCRIPTIONS.pop(user_id, None)
            VIP_SUBSCRIPTIONS.pop(user_id, None)
            USER_LAST_PAYMENT_ID.pop(user_id, None)
            ACTIVE_SIGNALS.pop(user_id, None)
            PENDING_ENTRY_SIGNALS.pop(user_id, None)
        else:
            await update.message.reply_text("❌ ریفاند نشد.")
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ خطا:\n{e}")

# ==================== ارسال سیگنال با کیفیت ====================

async def send_initial_signal(context: CallbackContext, user_id: int, signal: dict):
    sparkles = "✨"
    direction = "📈" if signal["signal"] == "LONG" else "📉"
    
    status_tags = []
    if signal["risk"]:
        status_tags.append("🚨 #ریسکی")
    else:
        status_tags.append("✅ #استاندارد")
    
    in_zone = is_price_in_entry_zone(signal["current_price"], signal)
    if not in_zone:
        status_tags.append("⏳ #صبر")
        entry_status = "⏳ در انتظار ورود به محدوده..."
    else:
        entry_status = "✅ قیمت در محدوده ورود است!"
    
    status_text = " | ".join(status_tags)
    
    text = (
        f"{sparkles} **سیگنال فوتچرز {signal['symbol']}** {sparkles}\n\n"
        f"{direction} **نوع:** `{signal['signal']}`\n"
        f"💰 **نماد:** `{signal['symbol']}`\n\n"
        f"🎯 **نقطه ورود:** `{signal['entry']}`\n"
        f"🛑 **حد ضرر:** `{signal['stop_loss']}`\n"
        f"🚀 **هدف:** `{signal['take_profit']}`\n\n"
        f"📊 **محدوده ورود:**\n`{signal['entry_zone_low']}` ➜ `{signal['entry_zone_high']}`\n\n"
        f"💵 **قیمت لحظه‌ای:** `${signal['current_price']}`\n"
        f"{entry_status}\n\n"
        f"📈 **تغییر ۱ ساعت:** `{signal['change_1h']:+.2f}%`\n"
        f"📊 **تغییر ۲۴ ساعت:** `{signal['change_24h']:+.2f}%`\n"
        f"💎 **حجم:** `${signal['volume']:,.0f}`\n\n"
        f"🏷️ {status_text}\n\n"
        f"⏳ منتظر ورود به محدوده باشید..."
    )
    
    try:
        msg = await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
        )
        PENDING_ENTRY_SIGNALS.setdefault(user_id, {})[signal["symbol"]] = {
            **signal,
            "message_id": msg.message_id,
            "timestamp": datetime.now(),
            "in_zone": in_zone,
        }
        logger.info(f"✅ Signal sent: {signal['symbol']} {signal['signal']} to user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send signal: {e}")
        return False

# ==================== بررسی قیمت و فعال کردن دکمه ====================

async def check_price_for_entry_job(context: CallbackContext):
    coins = await fetch_all_coins()
    if not coins:
        return
    
    price_map = {(c.get("symbol") or "").upper(): (c.get("current_price") or 0) for c in coins}
    
    for user_id, signals in list(PENDING_ENTRY_SIGNALS.items()):
        for symbol, signal in list(signals.items()):
            current_price = price_map.get(symbol)
            if not current_price:
                continue
            
            in_zone = is_price_in_entry_zone(current_price, signal)
            
            if not signal.get("in_zone", False) and in_zone:
                signal["in_zone"] = True
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(f"✅ تایید ورود {symbol} ✅", callback_data=f"confirm_entry_{symbol}"),
                        InlineKeyboardButton(f"❌ رد سیگنال ❌", callback_data=f"reject_entry_{symbol}"),
                    ]
                ])
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=signal["message_id"],
                        text=(
                            f"✨ **سیگنال فوتچرز {signal['symbol']}** ✨\n\n"
                            f"📈 **نوع:** `{signal['signal']}`\n"
                            f"🎯 **ورود:** `{signal['entry']}`\n"
                            f"🛑 **SL:** `{signal['stop_loss']}`\n"
                            f"🚀 **TP:** `{signal['take_profit']}`\n\n"
                            f"💵 **قیمت لحظه‌ای:** `${current_price}`\n"
                            f"✅ **قیمت وارد محدوده ورود شد!**\n\n"
                            f"🟢 **برای ورود به معامله، دکمه تایید رو بزن:**"
                        ),
                        reply_markup=keyboard,
                        parse_mode="Markdown",
                    )
                    logger.info(f"🎯 Entry zone reached: {symbol}")
                except Exception as e:
                    logger.error(f"Edit error: {e}")
            
            # انقضای ۲ ساعته
            if (datetime.now() - signal["timestamp"]).seconds > 7200:
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=signal["message_id"],
                        text=f"⏰ **سیگنال {symbol} منقضی شد**\n\nزمان ۲ ساعت به پایان رسید.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                del PENDING_ENTRY_SIGNALS[user_id][symbol]

# ==================== تولید سیگنال با لاگ وضعیت ====================

async def signal_job_fast(context: CallbackContext):
    """تولید سیگنال با کیفیت بالا - با لاگ کردن وضعیت"""
    coins = await fetch_all_coins()
    if not coins:
        logger.warning("⚠️ No coins data from API")
        return
    
    # لاگ تعداد کل ارزها
    logger.info(f"📊 Checking {len(coins)} coins for signals...")
    
    # فیلتر اولیه بر اساس حجم
    high_volume_coins = [c for c in coins if (c.get("total_volume") or 0) > MIN_VOLUME]
    logger.info(f"📈 Coins with volume > ${MIN_VOLUME:,}: {len(high_volume_coins)}")
    
    signals_found = []
    
    for coin in high_volume_coins[:50]:  # چک کردن 50 تای اول
        signal = analyze_coin(coin)
        if signal:
            signals_found.append(signal)
    
    logger.info(f"🎯 Quality signals found: {len(signals_found)}")
    
    # لاگ جزئیات سیگنال‌های پیدا شده
    for sig in signals_found:
        logger.info(f"   📍 {sig['symbol']} - {sig['signal']} - 1h change: {sig['change_1h']:+.2f}%")
    
    # اگر سیگنالی پیدا نشد، لاگ بزن
    if len(signals_found) == 0:
        # پیدا کردن نزدیک‌ترین‌ها به سیگنال برای لاگ
        near_misses = []
        for coin in high_volume_coins[:20]:
            change_1h = coin.get("price_change_percentage_1h_in_currency") or 0
            if abs(change_1h) > 1.0:  # نزدیک به آستانه
                near_misses.append(f"{coin.get('symbol', '').upper()}: {change_1h:+.2f}%")
        if near_misses:
            logger.info(f"⚠️ Near misses (change >1% but <{MIN_PRICE_CHANGE}%): {', '.join(near_misses[:5])}")
        else:
            logger.info(f"⏸️ Market is calm - no strong signals. Best 1h change: {max([abs(c.get('price_change_percentage_1h_in_currency') or 0) for c in high_volume_coins[:20]], default=0):.2f}%")
    
    # ارسال سیگنال‌ها به کاربران
    now = datetime.now()
    for signal in signals_found:
        # ارسال به کاربران عادی
        for user_id, expire in list(USER_SUBSCRIPTIONS.items()):
            if expire >= now:
                await send_initial_signal(context, user_id, signal)
                await asyncio.sleep(0.3)
        
        # ارسال به VIPها
        for user_id, expire in list(VIP_SUBSCRIPTIONS.items()):
            if expire >= now:
                await send_initial_signal(context, user_id, signal)
                await asyncio.sleep(0.2)
    
    # ارسال گزارش وضعیت به ادمین (اختیاری - میتونی غیرفعال کنی)
    # برای دیباگ - هر 10 دقیقه یکبار لاگ وضعیت بازار
    current_minute = datetime.now().minute
    if current_minute % 10 == 0 and len(signals_found) == 0:
        logger.info("📢 No signals in last 10 minutes - market might be ranging")

# ==================== بررسی تارگت و حد ضرر ====================

async def check_targets_job(context: CallbackContext):
    coins = await fetch_all_coins()
    if not coins:
        return
    
    price_map = {(c.get("symbol") or "").upper(): (c.get("current_price") or 0) for c in coins}
    
    for user_id, signals in list(ACTIVE_SIGNALS.items()):
        for symbol, signal in list(signals.items()):
            current_price = price_map.get(symbol)
            if not current_price:
                continue
            
            signal_type = signal["signal"]
            tp = signal["take_profit"]
            sl = signal["stop_loss"]
            entry = signal["entry"]
            
            if signal_type == "LONG":
                hit_tp = current_price >= tp
                hit_sl = current_price <= sl
                profit_pct = ((current_price - entry) / entry) * 100
            else:
                hit_tp = current_price <= tp
                hit_sl = current_price >= sl
                profit_pct = ((entry - current_price) / entry) * 100
            
            if hit_tp:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🎯 **تارگت {symbol} تاچ شد!** ✅\n\n"
                            f"📈 **سود:** `+{profit_pct:.2f}%`\n"
                            f"💰 **قیمت:** `${current_price}`\n\n"
                            f"✨ معامله بسته شد."
                        ),
                        parse_mode="Markdown",
                    )
                    del ACTIVE_SIGNALS[user_id][symbol]
                except Exception as e:
                    logger.error(f"TP error: {e}")
                    
            elif hit_sl:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🛑 **حد ضرر {symbol} فعال شد!**\n\n"
                            f"📉 **ضرر:** `{profit_pct:.2f}%`\n"
                            f"💰 **قیمت:** `${current_price}`\n\n"
                            f"⚡ معامله بسته شد."
                        ),
                        parse_mode="Markdown",
                    )
                    del ACTIVE_SIGNALS[user_id][symbol]
                except Exception as e:
                    logger.error(f"SL error: {e}")

# ==================== VIP Signals ====================

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
    best_signal = candidates[0][1]
    
    vip_text = (
        f"👑 **سیگنال ویژه VIP** 👑\n\n"
        f"✨ **{best_signal['symbol']} - {best_signal['signal']}** ✨\n\n"
        f"🎯 **ورود:** `{best_signal['entry']}`\n"
        f"🛑 **SL:** `{best_signal['stop_loss']}`\n"
        f"🚀 **TP:** `{best_signal['take_profit']}`\n\n"
        f"📊 **حجم:** `${best_signal['volume']:,.0f}`\n"
        f"📈 **تغییر ۱ ساعت:** `{best_signal['change_1h']:+.2f}%`\n\n"
        f"🌟 VIP only"
    )
    
    now = datetime.now()
    for user_id, expire in list(VIP_SUBSCRIPTIONS.items()):
        if expire >= now:
            try:
                await context.bot.send_message(chat_id=user_id, text=vip_text, parse_mode="Markdown")
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"VIP error: {e}")

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
    
    logger.info("✅ ربات با کیفیت بالا اجرا شد")
    logger.info(f"📊 فیلترها: حجم>${MIN_VOLUME:,} | مارکت کپ>${MIN_MARKET_CAP:,} | تغییر>{MIN_PRICE_CHANGE}%")
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
