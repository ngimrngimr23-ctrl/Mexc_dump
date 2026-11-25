"""Автопроверка монеты на халяльность через @CryptoGulfHalal_Bot.

Наш бот не может спросить другого бота: Telegram запрещает ботам писать
ботам. Поэтому проверку делает пользовательский аккаунт (юзербот) через
Telethon — отправляет тикер и читает ответ, ровно как человек руками.

Настройка (Render → Environment). Все три значения — секреты:
    TG_API_ID, TG_API_HASH  с my.telegram.org → API development tools
    TG_SESSION              строка сессии, см. tools/make_session.py
    HALAL_BOT               необязательно, по умолчанию CryptoGulfHalal_Bot
Без них проверка выключена и бот работает как раньше.

Разбор ответа намеренно строгий: монета блокируется только на «غير مباح»
(«не разрешено»). Любой непонятный ответ считается «не проверено»: сигнал
приходит с пометкой, а сырой текст ответа пишется в лог.
"""

import asyncio
import os
import re
import time

API_ID = os.environ.get("TG_API_ID", "").strip()
API_HASH = os.environ.get("TG_API_HASH", "").strip()
SESSION = os.environ.get("TG_SESSION", "").strip()
BOT = os.environ.get("HALAL_BOT", "CryptoGulfHalal_Bot").strip().lstrip("@")

FIRST_REPLY_TIMEOUT = 12   # сколько ждём первый ответ бота
NEXT_REPLY_TIMEOUT = 4     # бот может прислать «ищу…», а вердикт следом
MAX_MESSAGES = 3
MIN_INTERVAL = 1.5         # пауза между запросами, чтобы не спамить бота
TTL_KNOWN = 7 * 24 * 3600  # вердикт может поменяться — бот сам об этом предупреждает
TTL_UNKNOWN = 6 * 3600     # «не проверено» перепроверяем чаще

HARAM, HALAL, UNKNOWN = "haram", "halal", "unknown"

# Невидимые символы направления текста и татвиль — мешают сравнению строк.
_INVISIBLE = re.compile("[‎‏‪-‮⁦-⁩ـ]")
_COIN_KEYS = ("أسم العملة", "اسم العملة", "إسم العملة")
_RULING_KEY = "حكم العملة"
_LINK_KEY = "رابط الحكم"
_SOURCE_KEY = "مصدر الحكم"


def _clean(text):
    return _INVISIBLE.sub("", text or "")


def has_verdict(text):
    return _RULING_KEY in _clean(text)


def parse_reply(text, ticker):
    """Разбирает ответ бота. Возвращает dict: status, ruling, link, source, coin, note."""
    fields = {}
    for line in _clean(text).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fields[re.sub(r"\s+", " ", key).strip()] = val.strip()

    coin = next((fields[k] for k in _COIN_KEYS if fields.get(k)), "")
    ruling = re.sub(r"\s+", " ", fields.get(_RULING_KEY, "")).strip()
    link = re.search(r"https?://\S+", fields.get(_LINK_KEY, ""))
    result = {
        "ruling": ruling,
        "link": link.group(0) if link else "",
        "source": fields.get(_SOURCE_KEY, ""),
        "coin": coin,
        "note": "",
    }

    if not ruling:
        snippet = re.sub(r"\s+", " ", _clean(text)).strip()[:120]
        result.update(status=UNKNOWN, note="в ответе нет вердикта: «%s»" % snippet)
    elif coin and coin.strip().upper() != ticker.upper():
        result.update(status=UNKNOWN, note="ответ про другую монету (%s)" % coin)
    elif "غير مباح" in ruling:
        result["status"] = HARAM
    elif "مباح" in ruling:
        result["status"] = HALAL
    else:
        result.update(status=UNKNOWN, note="непонятный вердикт: %s" % ruling)
    return result


class HalalChecker:
    def __init__(self, log):
        self.log = log
        self.client = None
        self.me = ""
        self.cache = {}            # ТИКЕР -> результат parse_reply + ts
        self.last_error = ""
        self.last_query = 0.0
        self.queries = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def is_configured():
        return bool(API_ID and API_HASH and SESSION)

    @property
    def ready(self):
        return self.client is not None

    def describe(self):
        if not self.is_configured():
            return "не настроена (нет TG_API_ID / TG_API_HASH / TG_SESSION)"
        if not self.ready:
            return "не подключена: %s" % (self.last_error or "ещё не запускалась")
        return "через %s → @%s, в кэше %d" % (self.me, BOT, len(self.cache))

    async def start(self):
        if not self.is_configured():
            self.log("халяль-проверка выключена: не заданы TG_API_ID / TG_API_HASH / TG_SESSION")
            return False
        try:
            # Импорт здесь: без настроек Telethon вообще не нужен.
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            client = TelegramClient(StringSession(SESSION), int(API_ID), API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                self.last_error = "строка сессии недействительна — получи новую"
                self.log("юзербот: %s" % self.last_error, "ERR")
                await client.disconnect()
                return False
            me = await client.get_me()
            self.client = client
            self.me = "@%s" % me.username if me.username else "id %s" % me.id
            self.last_error = ""
            self.log("юзербот подключён как %s, халяль-проверка через @%s" % (self.me, BOT), "HALAL")
            return True
        except Exception as e:
            self.last_error = repr(e)
            self.log("юзербот не подключился: %r" % e, "ERR")
            return False

    def cached(self, ticker):
        entry = self.cache.get(ticker)
        if not entry:
            return None
        ttl = TTL_UNKNOWN if entry["status"] == UNKNOWN else TTL_KNOWN
        return entry if time.time() - entry.get("ts", 0) < ttl else None

    def forget(self, ticker):
        self.cache.pop(ticker.upper(), None)

    async def check(self, ticker, fresh=False):
        """Вердикт по тикеру. Никогда не бросает исключений."""
        ticker = ticker.upper()
        if not fresh:
            hit = self.cached(ticker)
            if hit:
                return hit
        if not self.ready:
            return {"status": UNKNOWN, "note": "юзербот не подключён", "ruling": "",
                    "link": "", "source": "", "coin": ""}

        async with self._lock:
            if not fresh:
                hit = self.cached(ticker)   # пока ждали, могли уже спросить
                if hit:
                    return hit
            wait = MIN_INTERVAL - (time.time() - self.last_query)
            if wait > 0:
                await asyncio.sleep(wait)
            started = time.time()
            try:
                result = await self._ask(ticker)
            except Exception as e:
                self.last_error = repr(e)
                self.log("халяль-запрос %s упал: %r" % (ticker, e), "ERR")
                result = {"status": UNKNOWN, "note": "ошибка запроса: %r" % e, "ruling": "",
                          "link": "", "source": "", "coin": ""}
            finally:
                self.last_query = time.time()
                self.queries += 1

        result["ts"] = time.time()
        self.cache[ticker] = result
        self.log("халяль %s: %s%s (%.1f с)" % (
            ticker, result["status"],
            " — " + result["note"] if result.get("note") else "",
            time.time() - started), "HALAL")
        return result

    async def _ask(self, ticker):
        async with self.client.conversation(
                BOT, timeout=FIRST_REPLY_TIMEOUT,
                total_timeout=FIRST_REPLY_TIMEOUT + NEXT_REPLY_TIMEOUT * MAX_MESSAGES) as conv:
            await conv.send_message(ticker)
            last = None
            for i in range(MAX_MESSAGES):
                try:
                    msg = await conv.get_response(
                        timeout=FIRST_REPLY_TIMEOUT if i == 0 else NEXT_REPLY_TIMEOUT)
                except asyncio.TimeoutError:
                    break
                text = msg.raw_text or ""
                last = parse_reply(text, ticker)
                if has_verdict(text):
                    return last
            if last is None:
                return {"status": UNKNOWN, "note": "бот не ответил за %d с" % FIRST_REPLY_TIMEOUT,
                        "ruling": "", "link": "", "source": "", "coin": ""}
            return last

    def export_cache(self):
        return self.cache

    def import_cache(self, data):
        if isinstance(data, dict):
            self.cache = {str(k).upper(): v for k, v in data.items()
                          if isinstance(v, dict) and v.get("status") in (HARAM, HALAL, UNKNOWN)}
