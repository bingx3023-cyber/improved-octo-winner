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

# ⚡ تنظیمات سرعت بالا برای فوتچرز
SIGNAL_CHECK_INTERVAL = 60      # هر 1 دقیقه سیگنال جدید بررسی بشه
PRICE_CHECK_INTERVAL = 15       # هر 15 ثانیه قیمت برای ورود بررسی بشه
TARGET_CHECK_INTERVAL = 30      # هر 30 ثانیه تارگت و حد ضرر چک بشه
VIP_SIGNAL_INTERVAL = 3600      # هر 1 ساعت سیگنال VIP

# محدوده مجاز برای ورود (درصد اختلاف از نقطه ورود)
ENTRY_ZONE_PERCENT = 0.3  # 0.3% بالاتر یا پایین‌تر از نقطه ورود

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

# سیگنال‌های در انتظار ورود به محدوده
PENDING_ENTRY_SIGNALS = {}  # user_id -> {symbol: signal_data}

# سیگنال‌های فعال (تایید شده)
ACTIVE_SIGNALS = {}       # user_id -> {symbol: signal_data}

# ==================== CoinGecko API ====================

COINGECKO_API = "https://api.coingecko.com/api/v3"

async def fetch_all_coins():
    """دریافت لیست رمزارزها از CoinGecko"""
    url = (
        f"{COINGECKO_API}/coins/markets"
        f"?vs_currency=usd"
        f"&order=market_cap_desc"
        f"&per_page=100"
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
    """تحلیل و تولید سیگنال از روی داده‌های هر رمزارز"""
    price_change_1h = coin.get("price_change_percentage_1h_in_currency") or 0
    price_change_24h = coin.get("price_change_percentage_24h_in_currency") or 0
    current_price = coin.get("current_price") or 0
    symbol = (coin.get("symbol") or "N/A").upper()
    total_volume = coin.get("total_volume") or 0
    market_cap = coin.get("market_cap") or 0

    # فیلترهای کیفیت برای فوتچرز
    if total_volume < 20_000_000:  # حداقل حجم 20 میلیون دلار
        return None
    if market_cap < 100_000_000:   # حداقل مارکت کپ 100 میلیون دلار
        return None
    if current_price <= 0:
        return None

    is_risky = abs(price_change_1h) >= 5
    strong_trend = abs(price_change_24h) >= 10

    # محاسبه حد ضرر و تارگت بر اساس نوسان
    atr_style = abs(price_change_1h) / 100 * current_price if price_change_1h != 0 else current_price * 0.01
    
    # LONG SIGNAL
    if price_change_1h > 1.5 and price_change_24h > -5:
        return {
            "symbol": symbol,
            "signal": "LONG",
            "entry": round(current_price, 6),
            "stop_loss": round(current_price - (atr_style * 1.5), 6),
            "take_profit": round(current_price + (atr_style * 3), 6),
            "risk": is_risky,
            "strong": strong_trend,
            "current_price": current_price,
            "volume": total_volume,
            "entry_zone_low": round(current_price * (1 - ENTRY_ZONE_PERCENT/100), 6),
            "entry_zone_high": round(current_price * (1 + ENTRY_ZONE_PERCENT/100), 6),
        }

    # SHORT SIGNAL
    elif price_change_1h < -1.5 and price_change_24h < 5:
        return {
            "symbol": symbol,
            "signal": "SHORT",
            "entry": round(current_price, 6),
            "stop_loss": round(current_price + (atr_style * 1.5), 6),
            "take_profit": round(current_price - (atr_style * 3), 6),
            "risk": is_risky,
            "strong": strong_trend,
            "current_price": current_price,
            "volume": total_volume,
            "entry_zone_low": round(current_price * (1 - ENTRY_ZONE_PERCENT/100), 6),
            "entry_zone_high": round(current_price * (1 + ENTRY_ZONE_PERCENT/100), 6),
        }

    return None

def is_price_in_entry_zone(current_price, signal):
    """بررسی اینکه قیمت فعلی در محدوده ورود هست یا نه"""
    if signal["signal"] == "LONG":
        return current_price <= signal["entry_zone_high"]
    else:  # SHORT
        return current_price >= signal["entry_zone_low"]

# ==================== Telegram Handlers ====================

async def start(update: Update, context: CallbackContext):
    """دستور start"""
    keyboard = [
        [InlineKeyboardButton("💳 اشتراک 1 ماهه — 1 ⭐", callback_data="buy_subscription")],
        [InlineKeyboardButton("🔥 اشتراک VIP — 2 ⭐", callback_data="buy_vip")],
        [InlineKeyboardButton("👤 وضعیت اشتراک من", callback_data="show_subscriptions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "✨ **ربات سیگنال فوتچرز** ✨\n\n"
        "⚡ سیگنال‌های لحظه‌ای برای معاملات فیوچرز\n"
        "🎯 با تایید ورود هوشمند\n"
        "✅ وقتی قیمت به محدوده ورود رسید، دکمه تایید فعال میشه\n"
        "💎 ایموجی‌های پریمیوم تلگرام\n\n"
        "👇 یکی از گزینه‌ها رو انتخاب کن:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

async def button_handler(update: Update, context: CallbackContext):
    """مدیریت دکمه‌های شیشه‌ای"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    # خرید اشتراک
    if query.data == "buy_subscription":
        prices = [LabeledPrice("اشتراک 1 ماهه سیگنال فوتچرز", 1)]
        await context.bot.send_invoice(
            chat_id=user_id,
            title="✨ اشتراک سیگنال فوتچرز",
            description="دریافت سیگنال‌های لحظه‌ای برای معاملات فیوچرز",
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
            description="سیگنال‌های ویژه + الert های پیشرفته",
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

    # ✅ تایید ورود سیگنال
    elif query.data.startswith("confirm_entry_"):
        parts = query.data.split("_")
        if len(parts) >= 3:
            symbol = parts[2]
            
            if user_id in PENDING_ENTRY_SIGNALS and symbol in PENDING_ENTRY_SIGNALS[user_id]:
                signal = PENDING_ENTRY_SIGNALS[user_id][symbol]
                
                # انتقال به سیگنال فعال
                ACTIVE_SIGNALS.setdefault(user_id, {})[symbol] = {
                    **signal,
                    "confirmed_at": datetime.now(),
                }
                
                # حذف از انتظار
                del PENDING_ENTRY_SIGNALS[user_id][symbol]
                
                await query.edit_message_text(
                    f"✅ **ورود {symbol} تایید شد!** ✅\n\n"
                    f"📊 **نوع:** {signal['signal']}\n"
                    f"🎯 **نقطه ورود:** `{signal['entry']}`\n"
                    f"🛑 **حد ضرر:** `{signal['stop_loss']}`\n"
                    f"🚀 **هدف:** `{signal['take_profit']}`\n\n"
                    f"💎 معامله فعال شد. ربات تارگت و حد ضرر رو بررسی میکنه.",
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text("❌ این سیگنال منقضی شده یا وجود ندارد.")
    
    # ❌ رد سیگنال
    elif query.data.startswith("reject_entry_"):
        parts = query.data.split("_")
        if len(parts) >= 3:
            symbol = parts[2]
            
            if user_id in PENDING_ENTRY_SIGNALS and symbol in PENDING_ENTRY_SIGNALS[user_id]:
                del PENDING_ENTRY_SIGNALS[user_id][symbol]
                
                await query.edit_message_text(
                    f"❌ **ورود {symbol} رد شد** ❌\n\n"
                    f"⏳ سیگنال بسته شد. برای سیگنال بعدی منتظر بمون.",
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text("❌ این سیگنال منقضی شده یا وجود ندارد.")

# ==================== Payment ====================

async def precheckout_callback(update: Update, context: CallbackContext):
    """تایید پیش‌پرداخت"""
    query = update.pre_checkout_query
    if query.invoice_payload.startswith(("subscription:", "vip:")):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="پرداخت نامعتبر")

async def successful_payment_callback(update: Update, context: CallbackContext):
    """پس از پرداخت موفق"""
    payment = update.message.successful_payment
    user_id = update.effective_user.id

    if payment.invoice_payload.startswith("subscription:"):
        USER_SUBSCRIPTIONS[user_id] = datetime.now() + timedelta(days=30)
        USER_PAYMENTS[user_id] = USER_PAYMENTS.get(user_id, 0) + payment.total_amount
        USER_LAST_PAYMENT_ID[user_id] = payment.telegram_payment_charge_id

        await update.message.reply_text(
            "✅ **اشتراک عادی فعال شد!** ✨\n\n"
            f"📅 تا تاریخ: `{USER_SUBSCRIPTIONS[user_id]}`\n\n"
            "⚡ از الان سیگنال‌های فوتچرز رو دریافت می‌کنی.",
            parse_mode="Markdown",
        )

    elif payment.invoice_payload.startswith("vip:"):
        VIP_SUBSCRIPTIONS[user_id] = datetime.now() + timedelta(days=30)
        USER_PAYMENTS[user_id] = USER_PAYMENTS.get(user_id, 0) + payment.total_amount
        USER_LAST_PAYMENT_ID[user_id] = payment.telegram_payment_charge_id

        await update.message.reply_text(
            "🔥 **اشتراک VIP فعال شد!** 🔥\n\n"
            f"📅 تا تاریخ: `{VIP_SUBSCRIPTIONS[user_id]}`\n\n"
            "💎 سیگنال‌های ویژه + اولویت دریافت.",
            parse_mode="Markdown",
        )

# ==================== Refund ====================

async def refund_command(update: Update, context: CallbackContext):
    """بازگشت وجه"""
    user_id = update.effective_user.id
    charge_id = None

    if context.args:
        charge_id = context.args[0]
    elif user_id in USER_LAST_PAYMENT_ID:
        charge_id = USER_LAST_PAYMENT_ID[user_id]

    if not charge_id:
        await update.message.reply_text("❌ هیچ پرداختی برای ریفاند پیدا نشد.")
        return

    try:
        success = await context.bot.refund_star_payment(
            user_id=user_id,
            telegram_payment_charge_id=charge_id,
        )

        if success:
            await update.message.reply_text("✅ ریفاند با موفقیت انجام شد.")
            USER_SUBSCRIPTIONS.pop(user_id, None)
            VIP_SUBSCRIPTIONS.pop(user_id, None)
            USER_LAST_PAYMENT_ID.pop(user_id, None)
            ACTIVE_SIGNALS.pop(user_id, None)
            PENDING_ENTRY_SIGNALS.pop(user_id, None)
        else:
            await update.message.reply_text("❌ ریفاند انجام نشد.")
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ خطا در ریفاند:\n{e}")

# ==================== Signal Sender با وضعیت #صبر ====================

async def send_initial_signal(context: CallbackContext, user_id: int, signal: dict):
    """ارسال سیگنال اولیه با وضعیت #صبر یا #ریسکی"""
    
    sparkles = "✨"
    direction = "📈" if signal["signal"] == "LONG" else "📉"
    strong_emoji = "💪" if signal.get("strong") else "⚡"
    
    # تعیین وضعیت سیگنال
    status_tags = []
    if signal["risk"]:
        status_tags.append("🚨 #ریسکی")
    else:
        status_tags.append("✅ #استاندارد")
    
    # بررسی قیمت فعلی نسبت به محدوده ورود
    in_zone = is_price_in_entry_zone(signal["current_price"], signal)
    if not in_zone:
        status_tags.append("⏳ #صبر")
        entry_status = "⏳ **در انتظار ورود به محدوده...**"
    else:
        entry_status = "✅ **قیمت در محدوده ورود است!**"
    
    status_text = " | ".join(status_tags)
    
    text = (
        f"{sparkles} **سیگنال فوتچرز {signal['symbol']}** {sparkles}\n\n"
        f"{direction} **نوع:** `{signal['signal']}`\n"
        f"💰 **نماد:** `{signal['symbol']}`\n\n"
        f"🎯 **نقطه ورود:** `{signal['entry']}`\n"
        f"🛑 **حد ضرر (SL):** `{signal['stop_loss']}`\n"
        f"🚀 **هدف (TP):** `{signal['take_profit']}`\n\n"
        f"📊 **محدوده مجاز ورود:**\n"
        f"`{signal['entry_zone_low']}` ➜ `{signal['entry_zone_high']}`\n\n"
        f"💵 **قیمت لحظه‌ای:** `${signal['current_price']}`\n"
        f"{entry_status}\n\n"
        f"🏷️ **وضعیت:** {status_text}\n\n"
        f"💎 **حجم ۲۴h:** `${signal['volume']:,.0f}`\n\n"
        f"⏳ منتظر بمانید تا قیمت وارد محدوده شود...\n"
        f"سپس دکمه تایید فعال می‌شود."
    )
    
    try:
        msg = await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            disable_notification=False,
        )
        
        # ذخیره در انتظار ورود
        PENDING_ENTRY_SIGNALS.setdefault(user_id, {})[signal["symbol"]] = {
            **signal,
            "message_id": msg.message_id,
            "timestamp": datetime.now(),
            "in_zone": in_zone,
        }
        
        return True
    except Exception as e:
        logger.error(f"Failed sending signal to {user_id}: {e}")
        return False

# ==================== بررسی قیمت و فعال کردن دکمه تایید ====================

async def check_price_for_entry_job(context: CallbackContext):
    """بررسی قیمت هر 15 ثانیه و فعال کردن دکمه تایید وقتی قیمت وارد محدوده شد"""
    coins = await fetch_all_coins()
    if not coins:
        return
    
    price_map = {(c.get("symbol") or "").upper(): (c.get("current_price") or 0) for c in coins}
    
    for user_id, signals in list(PENDING_ENTRY_SIGNALS.items()):
        for symbol, signal in list(signals.items()):
            current_price = price_map.get(symbol)
            if not current_price or current_price <= 0:
                continue
            
            in_zone = is_price_in_entry_zone(current_price, signal)
            
            # اگه قبلاً تو محدوده نبود و الان وارد شد → فعال کردن دکمه تایید
            if not signal.get("in_zone", False) and in_zone:
                signal["in_zone"] = True
                signal["current_price_at_entry"] = current_price
                
                # دکمه‌های تایید و رد
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(f"✅ تایید ورود {symbol} ✅", callback_data=f"confirm_entry_{symbol}"),
                        InlineKeyboardButton(f"❌ رد سیگنال ❌", callback_data=f"reject_entry_{symbol}"),
                    ]
                ])
                
                # ویرایش پیام اصلی و افزودن دکمه‌ها
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=signal["message_id"],
                        text=(
                            f"✨ **سیگنال فوتچرز {signal['symbol']}** ✨\n\n"
                            f"📈 **نوع:** `{signal['signal']}`\n"
                            f"💰 **نماد:** `{signal['symbol']}`\n\n"
                            f"🎯 **نقطه ورود:** `{signal['entry']}`\n"
                            f"🛑 **حد ضرر:** `{signal['stop_loss']}`\n"
                            f"🚀 **هدف:** `{signal['take_profit']}`\n\n"
                            f"💵 **قیمت لحظه‌ای:** `${current_price}`\n"
                            f"✅ **قیمت وارد محدوده ورود شد!**\n\n"
                            f"🟢 **برای ورود به معامله، دکمه تایید رو بزن:**"
                        ),
                        reply_markup=keyboard,
                        parse_mode="Markdown",
                    )
                    logger.info(f"Entry zone reached for {symbol} - user {user_id}")
                except Exception as e:
                    logger.error(f"Failed to edit message for entry: {e}")
            
            # اگه قیمت از محدوده خارج شد و قبلاً تو محدوده بود → برگردوندن به حالت #صبر
            elif signal.get("in_zone", False) and not in_zone:
                signal["in_zone"] = False
                
                # برگردوندن به حالت بدون دکمه
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=signal["message_id"],
                        text=(
                            f"✨ **سیگنال فوتچرز {signal['symbol']}** ✨\n\n"
                            f"📈 **نوع:** `{signal['signal']}`\n"
                            f"💰 **نماد:** `{signal['symbol']}`\n\n"
                            f"🎯 **نقطه ورود:** `{signal['entry']}`\n"
                            f"🛑 **حد ضرر:** `{signal['stop_loss']}`\n"
                            f"🚀 **هدف:** `{signal['take_profit']}`\n\n"
                            f"📊 **محدوده مجاز ورود:**\n"
                            f"`{signal['entry_zone_low']}` ➜ `{signal['entry_zone_high']}`\n\n"
                            f"💵 **قیمت لحظه‌ای:** `${current_price}`\n"
                            f"⏳ **قیمت از محدوده خارج شد - #صبر**\n\n"
                            f"منتظر برگشت به محدوده باشید...",
                        ),
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error(f"Failed to revert message: {e}")
            
            # انقضای سیگنال بعد از 2 ساعت
            if (datetime.now() - signal["timestamp"]).seconds > 7200:
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=signal["message_id"],
                        text=(
                            f"⏰ **سیگنال {symbol} منقضی شد**\n\n"
                            f"زمان ۲ ساعته انتظار به پایان رسید.\n"
                            f"برای سیگنال بعدی منتظر بمونید."
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                del PENDING_ENTRY_SIGNALS[user_id][symbol]

# ==================== Signal Generation Job ====================

async def signal_job_fast(context: CallbackContext):
    """تولید و ارسال سیگنال هر 1 دقیقه"""
    coins = await fetch_all_coins()
    if not coins:
        return
    
    filtered_coins = [c for c in coins if (c.get("total_volume") or 0) > 20_000_000]
    
    signals_sent_this_round = set()
    
    for coin in filtered_coins[:15]:
        signal = analyze_coin(coin)
        if not signal:
            continue
        
        signal_key = f"{signal['symbol']}_{signal['signal']}"
        if signal_key in signals_sent_this_round:
            continue
        signals_sent_this_round.add(signal_key)
        
        now = datetime.now()
        
        # ارسال به کاربران عادی
        for user_id, expire in list(USER_SUBSCRIPTIONS.items()):
            if expire >= now:
                # بررسی نکن که سیگنال قبلاً فرستاده شده
                await send_initial_signal(context, user_id, signal)
                await asyncio.sleep(0.2)
        
        # ارسال به VIPها
        for user_id, expire in list(VIP_SUBSCRIPTIONS.items()):
            if expire >= now:
                await send_initial_signal(context, user_id, signal)
                await asyncio.sleep(0.1)
    
    logger.info(f"Signal job completed - sent {len(signals_sent_this_round)} signals")

# ==================== Check Targets & Stop Loss ====================

async def check_targets_job(context: CallbackContext):
    """بررسی تارگت و حد ضرر سیگنال‌های فعال"""
    coins = await fetch_all_coins()
    if not coins:
        return
    
    price_map = {(c.get("symbol") or "").upper(): (c.get("current_price") or 0) for c in coins}
    
    for user_id, signals in list(ACTIVE_SIGNALS.items()):
        for symbol, signal in list(signals.items()):
            current_price = price_map.get(symbol)
            if not current_price or current_price <= 0:
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
                profit_emoji = "✅" if profit_pct > 0 else "⚠️"
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🎯 **تارگت {symbol} تاچ شد!** {profit_emoji}\n\n"
                            f"📈 **سود:** `+{profit_pct:.2f}%`\n"
                            f"💰 **قیمت فعلی:** `${current_price}`\n"
                            f"🎯 **تارگت:** `${tp}`\n\n"
                            f"✨ معامله با موفقیت بسته شد."
                        ),
                        parse_mode="Markdown",
                        reply_to_message_id=signal.get("message_id"),
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
                            f"💰 **قیمت فعلی:** `${current_price}`\n"
                            f"🛑 **حد ضرر:** `${sl}`\n\n"
                            f"⚡ معامله بسته شد."
                        ),
                        parse_mode="Markdown",
                        reply_to_message_id=signal.get("message_id"),
                    )
                    del ACTIVE_SIGNALS[user_id][symbol]
                except Exception as e:
                    logger.error(f"SL error: {e}")

# ==================== VIP Signals ====================

async def vip_signal_job(context: CallbackContext):
    """سیگنال ویژه VIP"""
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
        f"💎 **امتیاز ویژه:** {'🔥 روند بسیار قوی' if best_signal.get('strong') else '⚡ سیگنال استاندارد'}\n"
        f"📊 **حجم:** `${best_signal['volume']:,.0f}`\n\n"
        f"🌟 این سیگنال فقط برای VIPها ارسال شده."
    )
    
    now = datetime.now()
    for user_id, expire in list(VIP_SUBSCRIPTIONS.items()):
        if expire >= now:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=vip_text,
                    parse_mode="Markdown",
                )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"VIP error: {e}")

# ==================== Main ====================

def main():
    """اجرای اصلی ربات"""
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Command Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refund", refund_command))
    
    # Callback Query Handlers
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Payment Handlers
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    
    # Job Queue
    app.job_queue.run_repeating(signal_job_fast, interval=SIGNAL_CHECK_INTERVAL, first=5)
    app.job_queue.run_repeating(check_price_for_entry_job, interval=PRICE_CHECK_INTERVAL, first=3)
    app.job_queue.run_repeating(check_targets_job, interval=TARGET_CHECK_INTERVAL, first=10)
    app.job_queue.run_repeating(vip_signal_job, interval=VIP_SIGNAL_INTERVAL, first=30)
    
    logger.info("✅ ربات فوتچرز با سیستم تایید هوشمند اجرا شد")
    logger.info(f"⚡ چک سیگنال: {SIGNAL_CHECK_INTERVAL}s | چک قیمت: {PRICE_CHECK_INTERVAL}s | چک تارگت: {TARGET_CHECK_INTERVAL}s")
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
