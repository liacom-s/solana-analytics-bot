# main.py
"""
Solana Analytics Bot

Telegram bot for monitoring new Solana tokens via Birdeye WebSocket + DexScreener,
filtering potential gems / risky tokens and sending alerts to a private Telegram chat.

All sensitive data (BOT_TOKEN, CHAT_ID, BIRDEYE_API_KEY, etc.) is stored in .env
and NOT committed to the repository.
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta

# ---------- .env ----------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ---------- network / telegram ----------
import aiohttp
import websockets
from telegram import Bot
from telegram.constants import ParseMode

# ---------- CONFIG from .env ----------
MIN_LIQ            = float(os.getenv("MIN_LIQUIDITY", "0"))
MIN_FDV            = float(os.getenv("MIN_FDV", "0"))
MIN_BUYERS         = int(os.getenv("MIN_BUYERS", "0"))
MIN_SELLERS        = int(os.getenv("MIN_SELLERS", "0"))
MIN_TOKEN_AGE      = int(os.getenv("MIN_TOKEN_AGE", "0"))          # хвилини
MAX_FDV_LIQ        = float(os.getenv("MAX_FDV_LIQ_RATIO", "9999"))
MAX_DROP_5MIN      = float(os.getenv("MAX_PRICE_DROP_5MIN", "101"))
MAX_TAX            = float(os.getenv("MAX_TAX", "100"))
REQUIRE_LOGO       = os.getenv("REQUIRE_LOGO", "false").lower() in ("1","true","yes")
REQUIRE_TWITTER    = os.getenv("REQUIRE_TWITTER", "false").lower() in ("1","true","yes")

BLACKLISTED_WORDS  = [w.strip().lower() for w in os.getenv("BLACKLISTED_WORDS", "").split(",") if w.strip()]

# «сірий» список — не блокуємо, але піднімаємо пороги
GREYLIST_WORDS     = [w.strip().lower() for w in os.getenv("GREYLIST_WORDS", "ai,chatgpt,openai,meta").split(",") if w.strip()]
MIN_LIQ_GREY       = float(os.getenv("MIN_LIQUIDITY_GREY", "150000"))
MIN_FDV_GREY       = float(os.getenv("MIN_FDV_GREY", "800000"))
MIN_BUYERS_GREY    = int(os.getenv("MIN_BUYERS_GREY", "300"))

# re-check / scouting
RECHECK_ENABLED    = os.getenv("RECHECK_ENABLED", "false").lower() in ("1","true","yes")
RECHECK_DELAY      = int(os.getenv("RECHECK_DELAY", "0"))          # хвилини
SCOUT_ENABLED      = os.getenv("SCOUT_ENABLED", "false").lower() in ("1","true","yes")
SCOUT_INTERVAL_MIN = int(os.getenv("SCOUT_INTERVAL_MIN", "10"))
SCOUT_TOP          = int(os.getenv("SCOUT_TOP", "50"))

# антиспам
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "90"))
DEDUP_MINUTES      = int(os.getenv("DEDUP_MINUTES", "60"))

# misc
DEBUG              = os.getenv("DEBUG", "false").lower() in ("1","true","yes")
LOG_REASONS        = os.getenv("LOG_REASONS", "false").lower() in ("1","true","yes")

# credentials / endpoints
BOT_TOKEN          = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
CHAT_ID            = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
BIRDEYE_API_KEY    = os.getenv("BIRDEYE_API_KEY", "")
BIRDEYE_WS_URL     = os.getenv("BIRDEYE_WS_URL") or (
    f"wss://public-api.birdeye.so/socket/solana?x-api-key={BIRDEYE_API_KEY}" if BIRDEYE_API_KEY else ""
)
BIRDEYE_TOKEN_OVERVIEW = os.getenv("BIRDEYE_TOKEN_OVERVIEW", "https://public-api.birdeye.so/defi/token_overview?address=")
BIRDEYE_TOKEN_META     = os.getenv("BIRDEYE_TOKEN_META", "https://public-api.birdeye.so/defi/v3/token/meta-data/single?address=")
MEME_ENABLED       = os.getenv("MEME_PLATFORM_ENABLED", "true").lower() in ("1","true","yes")

DEXSCREENER_TRENDING   = "https://api.dexscreener.com/latest/dex/tokens/trending/solana"

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("sol-gem")

log.info(
    "Thresholds → LIQ≥%s FDV≥%s BUYERS≥%s SELLERS≥%s AGE≥%s RATIO≤%s DROP5m≤%s TAX≤%s logo=%s twitter=%s scout=%s",
    MIN_LIQ, MIN_FDV, MIN_BUYERS, MIN_SELLERS, MIN_TOKEN_AGE, MAX_FDV_LIQ, MAX_DROP_5MIN, MAX_TAX,
    REQUIRE_LOGO, REQUIRE_TWITTER, SCOUT_ENABLED
)

# ---------- TG ----------
bot = Bot(BOT_TOKEN) if BOT_TOKEN else None

async def tg_send(text: str):
    if not (bot and CHAT_ID):
        return
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    except Exception as e:
        if DEBUG:
            log.debug("TG send err: %s", e)

def telegram_send(text: str):
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(tg_send(text))
    except RuntimeError:
        asyncio.get_event_loop().run_until_complete(tg_send(text))

# ---------- STATE ----------
SEEN_ADDR: set[str] = set()
LAST_ALERT: dict[str, datetime] = {}      # addr -> datetime
WATCH: dict[str, dict] = {}               # addr -> {first_seen,last_check}

# ---------- HELPERS ----------
def to_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(str(v).replace(",", "").strip())
    except Exception:
        return default

def parse_created_at(v):
    if v is None:
        return datetime.utcnow()
    try:
        x = float(v)
        if x > 1e12:
            x /= 1000.0    # мс
        elif x > 1e10:
            x /= 1e9       # нс
        return datetime.utcfromtimestamp(x)
    except Exception:
        try:
            s = str(v).replace("Z", "+00:00")
            return datetime.fromisoformat(s).replace(tzinfo=None)
        except Exception:
            return datetime.utcnow()

# ---------- MODEL ----------
class TokenInfo:
    def __init__(
        self,
        name,
        symbol,
        address,
        liquidity,
        fdv,
        buyers,
        sellers,
        buy_tax,
        sell_tax,
        logo_url,
        twitter_url,
        timestamp,
        price_change_5m=None,
        holders=None,
    ):
        self.name = name or "Unknown"
        self.symbol = symbol or ""
        self.address = address
        self.liquidity = to_float(liquidity)
        self.fdv = to_float(fdv)
        self.buyers = int(to_float(buyers))
        self.sellers = int(to_float(sellers))
        self.buy_tax = to_float(buy_tax) if buy_tax is not None else None
        self.sell_tax = to_float(sell_tax) if sell_tax is not None else None
        self.logo_url = (logo_url or "").strip() if logo_url else ""
        self.twitter = (twitter_url or "").strip() if twitter_url else ""
        self.created_at = parse_created_at(timestamp)
        self.price_change_5m = to_float(price_change_5m) if price_change_5m is not None else None
        self.holders = int(to_float(holders)) if holders is not None else None

# ---------- HTTP CLIENT ----------
class Http:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def start(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def get_json(self, url: str) -> dict | None:
        if self.session is None:
            await self.start()
        headers = {"x-api-key": BIRDEYE_API_KEY} if BIRDEYE_API_KEY else {}
        try:
            async with self.session.get(url, headers=headers) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                return data.get("data") or data
        except Exception:
            return None

HTTP = Http()

async def get_token_info(address: str) -> TokenInfo | None:
    ov = await HTTP.get_json(f"{BIRDEYE_TOKEN_OVERVIEW}{address}")
    if not isinstance(ov, dict):
        return None
    meta = await HTTP.get_json(f"{BIRDEYE_TOKEN_META}{address}") or {}

    name   = ov.get("name") or ov.get("token_name") or meta.get("name")
    symbol = ov.get("symbol") or meta.get("symbol")

    liq    = ov.get("liquidity") or ov.get("liquidity_usd")
    fdv    = ov.get("fdv") or ov.get("market_cap") or ov.get("marketCap") or ov.get("fullyDilutedValuation")
    buyers = ov.get("buyers") or ov.get("buyersCount")
    sellers= ov.get("sellers") or ov.get("sellersCount")
    price5 = ov.get("price_change_5m") or ov.get("priceChange5m")
    holders= ov.get("holders") or meta.get("holders")

    buy_tax  = ov.get("buy_tax") or ov.get("buyTax") or ov.get("buyFee")
    sell_tax = ov.get("sell_tax") or ov.get("sellTax") or ov.get("sellFee")

    logo = (meta.get("logo") or ov.get("logo") or ov.get("logoURI") or
            (meta.get("image") if isinstance(meta.get("image"), str) else ""))
    links = meta.get("links") or ov.get("links") or {}
    twitter = (links.get("twitter") or links.get("twitter_url") or ov.get("twitter") or "")

    created = (
        ov.get("created_at") or ov.get("createdAt") or
        ov.get("time") or ov.get("timestamp") or meta.get("createdAt")
    )

    return TokenInfo(
        name, symbol, address, liq, fdv, buyers, sellers,
        buy_tax, sell_tax, logo, twitter, created, price5, holders
    )

# ---------- WORD LISTS ----------
def _word_match_any(text: str, words: list[str]) -> bool:
    if not words:
        return False
    pattern = r"\b(" + "|".join(map(re.escape, words)) + r")\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None

def name_tokens(token: TokenInfo) -> str:
    return f"{token.name} {token.symbol}".strip().lower()

def is_blacklisted_name(token: TokenInfo) -> bool:
    return _word_match_any(name_tokens(token), BLACKLISTED_WORDS)

def greylisted_hit(token: TokenInfo) -> bool:
    return _word_match_any(name_tokens(token), GREYLIST_WORDS)

# ---------- FILTERS ----------
def _reason(ok: bool, why: str):
    if LOG_REASONS and not ok:
        log.info("⛔ %s", why)
    return ok

def passes_filters(token: TokenInfo) -> bool:
    # cooldown
    last = LAST_ALERT.get(token.address)
    if last and (datetime.utcnow() - last).total_seconds() < ALERT_COOLDOWN_SEC:
        return _reason(False, f"{token.symbol} cooldown")

    if token.fdv <= 0 or token.liquidity <= 0 or token.buyers <= 0:
        return _reason(False, f"{token.symbol} empty metrics")

    # blacklist
    if is_blacklisted_name(token):
        return _reason(False, f"{token.symbol} blacklisted name")

    # greylist → higher thresholds
    if greylisted_hit(token):
        if token.liquidity < MIN_LIQ_GREY:    return _reason(False, f"{token.symbol} grey liq<{MIN_LIQ_GREY}")
        if token.fdv < MIN_FDV_GREY:          return _reason(False, f"{token.symbol} grey fdv<{MIN_FDV_GREY}")
        if token.buyers < MIN_BUYERS_GREY:    return _reason(False, f"{token.symbol} grey buyers<{MIN_BUYERS_GREY}")

    # base thresholds
    if token.liquidity < MIN_LIQ:             return _reason(False, f"{token.symbol} liq<{MIN_LIQ}")
    if token.fdv < MIN_FDV:                   return _reason(False, f"{token.symbol} fdv<{MIN_FDV}")
    if token.buyers < MIN_BUYERS:             return _reason(False, f"{token.symbol} buyers<{MIN_BUYERS}")
    if token.sellers < MIN_SELLERS:           return _reason(False, f"{token.symbol} sellers<{MIN_SELLERS}")

    minutes_age = (datetime.utcnow() - token.created_at).total_seconds() / 60.0
    if minutes_age < MIN_TOKEN_AGE:           return _reason(False, f"{token.symbol} age<{MIN_TOKEN_AGE}m")

    ratio = (token.fdv / token.liquidity) if token.liquidity > 0 else float("inf")
    if ratio > MAX_FDV_LIQ:                   return _reason(False, f"{token.symbol} fdv/liq>{MAX_FDV_LIQ}")

    if token.price_change_5m is not None and token.price_change_5m < -MAX_DROP_5MIN:
        return _reason(False, f"{token.symbol} drop5m>{MAX_DROP_5MIN}%")

    if token.buy_tax is not None and token.buy_tax > MAX_TAX:
        return _reason(False, f"{token.symbol} buy_tax>{MAX_TAX}%")
    if token.sell_tax is not None and token.sell_tax > MAX_TAX:
        return _reason(False, f"{token.symbol} sell_tax>{MAX_TAX}%")

    if REQUIRE_LOGO and not token.logo_url:
        return _reason(False, f"{token.symbol} no logo")
    if REQUIRE_TWITTER and not token.twitter:
        return _reason(False, f"{token.symbol} no twitter")

    return True

# ---------- MESSAGE ----------
def build_message(token: TokenInfo) -> str:
    age_min = int((datetime.utcnow() - token.created_at).total_seconds() / 60)
    return (
        f"🟢 *Сигнал*\n"
        f"🔥 *Токен:* {token.name} (`{token.symbol}`)\n"
        f"💧 Ліквідність: ${token.liquidity:,.0f}\n"
        f"💰 FDV: ${token.fdv:,.0f}\n"
        f"🤝 Покупців: {token.buyers} • Продавців: {token.sellers}\n"
        f"⏳ Вік: ~{age_min} хв\n"
        f"📉 5м зміна: {token.price_change_5m or 0}%\n"
        f"💱 Податок: куп. {token.buy_tax or 0}%, прод. {token.sell_tax or 0}%\n"
        f"🐦 Twitter: {token.twitter or 'нема'}"
    )

# ---------- RE-CHECK ----------
async def recheck_loop():
    if not RECHECK_ENABLED or RECHECK_DELAY <= 0:
        return
    log.info("♻️ Re-check кожні %s хв", RECHECK_DELAY)
    while True:
        try:
            now = datetime.utcnow().timestamp()
            to_check = list(WATCH.keys())
            if not to_check:
                await asyncio.sleep(RECHECK_DELAY * 60)
                continue
            for addr in to_check:
                stamp = WATCH.get(addr, {})
                last = stamp.get("last_check", 0)
                if now - last < RECHECK_DELAY * 60:
                    continue
                WATCH[addr]["last_check"] = now
                tok = await get_token_info(addr)
                if not tok:
                    continue
                if passes_filters(tok):
                    LAST_ALERT[addr] = datetime.utcnow()
                    SEEN_ADDR.add(addr)
                    telegram_send(build_message(tok))
                    WATCH.pop(addr, None)
            await asyncio.sleep(10)
        except Exception as e:
            if DEBUG:
                log.debug("recheck err: %s", e)
            await asyncio.sleep(RECHECK_DELAY * 60)

# ---------- SCOUT (Dexscreener trending) ----------
async def scout_trending_loop():
    if not SCOUT_ENABLED:
        return
    log.info("🔎 Scout: trending кожні %s хв (top %s)", SCOUT_INTERVAL_MIN, SCOUT_TOP)
    while True:
        try:
            await HTTP.start()
            async with HTTP.session.get(DEXSCREENER_TRENDING, timeout=15) as r:
                if r.status != 200:
                    await asyncio.sleep(SCOUT_INTERVAL_MIN * 60)
                    continue
                data = await r.json()
            pairs = (data.get("pairs") or [])[:SCOUT_TOP]
            for p in pairs:
                base = p.get("baseToken") or {}
                addr = base.get("address") or p.get("tokenAddress")
                if not addr or addr in SEEN_ADDR:
                    continue
                tok = await get_token_info(addr)
                if not tok:
                    continue
                if passes_filters(tok):
                    LAST_ALERT[addr] = datetime.utcnow()
                    SEEN_ADDR.add(addr)
                    telegram_send(build_message(tok))
                else:
                    WATCH.setdefault(addr, {"first_seen": datetime.utcnow().timestamp(), "last_check": 0})
            await asyncio.sleep(SCOUT_INTERVAL_MIN * 60)
        except Exception as e:
            if DEBUG:
                log.exception("scout err: %s", e)
            await asyncio.sleep(SCOUT_INTERVAL_MIN * 60)

# ---------- WS LOOP (з автоперепідпискою + heartbeat) ----------
async def ws_loop():
    # вітання
    if bot and CHAT_ID:
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text="Бот активний ✅",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    # фонові задачі
    asyncio.create_task(recheck_loop())
    asyncio.create_task(scout_trending_loop())

    if not BIRDEYE_WS_URL or not BIRDEYE_API_KEY:
        log.error("Birdeye WS/KEY відсутні — працюю тільки скаутом та re-check")
        while True:
            await asyncio.sleep(3600)

    delay, max_delay = 1, 60

    async def _subscribe(ws):
        sub = {"type": "SUBSCRIBE_TOKEN_NEW_LISTING", "meme_platform_enabled": bool(MEME_ENABLED)}
        await ws.send(json.dumps(sub))
        log.info("📩 SUBSCRIBE sent")

    async def _heartbeat(ws):
        try:
            while True:
                await asyncio.sleep(15)
                pong = await ws.ping()
                await asyncio.wait_for(pong, timeout=10)
        except Exception:
            pass  # упаде — верхній цикл перепідʼєднає

    while True:
        try:
            log.info("🔌 Connecting WS: %s", BIRDEYE_WS_URL)
            async with websockets.connect(
                BIRDEYE_WS_URL,
                subprotocols=["echo-protocol"],
                extra_headers={"Origin": "https://public-api.birdeye.so", "x-api-key": BIRDEYE_API_KEY},
                ping_interval=None,    # власний heartbeat
                ping_timeout=None,
                close_timeout=5,
                max_queue=1000,
            ) as ws:
                log.info("✅ WebSocket connected")
                await _subscribe(ws)
                hb_task = asyncio.create_task(_heartbeat(ws))

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(msg, dict):
                        continue

                    t = msg.get("type")
                    if t in ("WELCOME", "ACK", "SUBSCRIBE_TOKEN_NEW_LISTING", "SUBSCRIBED"):
                        log.info("✅ Subscribed/ACK: %s", t)
                        continue
                    if t not in ("TOKEN_NEW_LISTING", "TOKEN_NEW_LISTING_DATA"):
                        continue

                    data = msg.get("data") or {}
                    address = data.get("address") or data.get("mint") or data.get("id")
                    if not address or address in SEEN_ADDR:
                        continue

                    tok = await get_token_info(address)
                    if not tok:
                        continue

                    if passes_filters(tok):
                        LAST_ALERT[address] = datetime.utcnow()
                        SEEN_ADDR.add(address)
                        telegram_send(build_message(tok))
                    else:
                        WATCH.setdefault(address, {"first_seen": datetime.utcnow().timestamp(), "last_check": 0})

                hb_task.cancel()
                delay = 1

        except Exception as e:
            log.warning("WS error: %s", e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

# ---------- MAIN ----------
if __name__ == "__main__":
    try:
        asyncio.run(ws_loop())
    except KeyboardInterrupt:
        log.info("🛑 Зупинено")
    finally:
        try:
            asyncio.get_event_loop().run_until_complete(HTTP.stop())
        except Exception:
            pass
