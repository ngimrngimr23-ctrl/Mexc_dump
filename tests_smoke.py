#!/usr/bin/env python3
"""Прогон проверок без сети и без Telegram: python3 tests_smoke.py

Всё внешнее подменяется заглушками, поэтому запускать можно где угодно.
Каждая проверка ниже закрывает баг, который уже случался.
"""

import asyncio
import json
import os
import sys
import time
import types
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".test_state.json")
os.environ.setdefault("BOT_TOKEN", "123456789:AAFAKEfakeFAKEfakeFAKEfakeFAKEfake1")
os.environ["STATE_FILE"] = STATE
for var in ("CHAT_ID", "CHANNEL_ID", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
    os.environ.pop(var, None)
sys.path.insert(0, HERE)

import coingecko
import halal
import main
import storage
from memecoins import classify

REAL_KLINES = main.get_long_term_changes
checks = []


def check(fn):
    checks.append(fn)
    return fn


def fake_session(responses):
    """responses(url_or_args) -> (status, payload)."""
    class Resp:
        def __init__(self, status, payload):
            self.status, self._payload = status, payload
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def json(self): return self._payload
        async def text(self): return json.dumps(self._payload)
    class Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def get(self, url, params=None, headers=None, timeout=None):
            return Resp(*responses(("GET", url, params)))
        def post(self, url, json=None, headers=None, timeout=None):
            return Resp(*responses(("POST", url, json)))
    return types.SimpleNamespace(ClientSession=lambda *a, **k: Sess())


def reset():
    main.blacklist.clear(); main.allowlist.clear()
    main.meme_pairs.clear(); main.symbol_meta.clear()
    main.daily_memory.clear(); main.cg_symbols.clear()
    main.settings.update({"chat_id": 1, "channel_id": None, "skip_memes": True,
                          "use_coingecko": True, "day_drop": 0.0,
                          "day3_min_drop": 0.0, "day3_drop": 0.0,
                          "week_min_drop": 0.0, "week_drop": 0.0,
                          "month_min_drop": 0.0, "month_drop": 0.0,
                          "percent": 5.0, "hour_percent": 15.0,
                          "window_min": 5, "min_volume": 100000})
    main.cg_last_refresh = 0.0
    main.halal_ignore.clear()
    main.halal_checker.cache.clear()
    main.halal_checker.client = None
    main.settings["halal_check"] = True
    main.get_long_term_changes = REAL_KLINES
    if os.path.exists(STATE):
        os.remove(STATE)


SENT = []
async def fake_send(chat, text, **k): SENT.append(text)
main.bot.send_message = fake_send


def cand(pair="XUSDT", ch24=-5.0):
    return {"pair": pair, "price": 70.0, "max_p": 100.0, "drop": 30.0,
            "hour_drop": 30.0, "ch_24": ch24, "vol": 5e6, "is_repeat": False,
            "window_trigger": True, "hour_trigger": False}


# ------------------------------------------------- классификация мемкоинов
@check
async def test_classify_layers():
    cg = {"ZZZ", "XYZ", "BTC"}
    assert classify("PEPE", "Pepe", ["mc-trade-zone-MEME"], None, cg)[1].startswith("plate:")
    assert classify("DOGE", "Dogecoin", [], None, cg) == (True, "static")
    assert classify("ZZZ", None, [], None, cg) == (True, "coingecko")
    assert classify("QQQ", "Happy Dog Token", [], None, cg) == (True, "name:dog")
    assert classify("SOMEINU", None, [], None, cg)[1] == "ticker:INU"
    # биржа назвала монету тематической — CoinGecko не может перебить
    assert classify("XYZ", None, ["mc-trade-zone-layer2"], None, cg)[1] == "cg-конфликт-с-плашкой"
    # тарифная плашка защиты не даёт
    assert classify("ZZZ", None, ["mc-trade-zone-innovation"], None, cg)[0] is True
    # whitelist и allowlist сильнее всего
    assert classify("BTC", None, ["mc-trade-zone-MEME"], None, cg)[1] == "whitelist"
    assert classify("ZZZ", None, [], {"ZZZ"}, cg)[1] == "allowlist"


# ------------------------------------------------- exchangeInfo
GOOD_INFO = {"symbols": [
    {"symbol": "PEPEUSDT", "quoteAsset": "USDT", "baseAsset": "PEPE", "fullName": "Pepe",
     "conceptPlates": ["mc-trade-zone-MEME"], "st": False, "isSpotTradingAllowed": True},
    {"symbol": "BTCUSDT", "quoteAsset": "USDT", "baseAsset": "BTC", "fullName": "Bitcoin",
     "conceptPlates": ["mc-trade-zone-mainly"], "st": False, "isSpotTradingAllowed": True},
    {"symbol": "BTCUSDC", "quoteAsset": "USDC", "baseAsset": "BTC", "fullName": "Bitcoin",
     "conceptPlates": [], "st": False, "isSpotTradingAllowed": True},
]}


@check
async def test_exchange_info_keeps_cache_on_failure():
    reset()
    main.aiohttp = fake_session(lambda _: (200, GOOD_INFO))
    await main.refresh_exchange_info(force=True)
    assert set(main.meme_pairs) == {"PEPEUSDT"}, main.meme_pairs
    assert "BTCUSDC" not in main.symbol_meta, "не-USDT пары игнорируются"

    # каждый вид сбоя обязан сохранить прошлый кэш, иначе фильтр молча отключится
    for name, resp in (
        ("HTTP 500", lambda _: (500, {})),
        ("пустой список", lambda _: (200, {"symbols": []})),
        ("нет USDT-пар", lambda _: (200, {"symbols": [{"symbol": "AB", "quoteAsset": "BTC"}]})),
    ):
        main.aiohttp = fake_session(resp)
        await main.refresh_exchange_info(force=True)
        assert set(main.meme_pairs) == {"PEPEUSDT"}, "%s затёр кэш" % name
        assert main.info_last_error, "%s не записал ошибку" % name

    def boom(_): raise ConnectionError("нет сети")
    main.aiohttp = fake_session(boom)
    await main.refresh_exchange_info(force=True)
    assert set(main.meme_pairs) == {"PEPEUSDT"}, "обрыв сети затёр кэш"


# ------------------------------------------------- состояние
@check
async def test_state_roundtrip_and_env():
    reset()
    main.blacklist.add("FOO"); main.allowlist.add("BAR")
    main.settings["chat_id"] = 555
    main.settings["day3_min_drop"] = -9.0
    main.save_state()

    main.blacklist.clear(); main.allowlist.clear()
    main.settings["chat_id"] = None; main.settings["day3_min_drop"] = 0.0
    await main.load_state()
    assert main.blacklist == {"FOO"} and main.allowlist == {"BAR"}
    assert main.settings["chat_id"] == 555
    assert main.settings["day3_min_drop"] == -9.0, "3-дневный порог не сохранился"

    os.environ["CHAT_ID"] = "999"
    main.apply_env_overrides()
    assert main.settings["chat_id"] == 999, "env должен перебивать файл"
    os.environ["CHAT_ID"] = "мусор"
    main.apply_env_overrides()
    assert main.settings["chat_id"] == 999, "кривой CHAT_ID не должен ломать настройку"
    os.environ.pop("CHAT_ID")


@check
async def test_upstash():
    reset()
    storage.URL, storage.TOKEN = "https://demo.upstash.io", "секрет"
    assert storage.is_configured() and "секрет" not in storage.describe()

    box = {"v": None}
    def handler(call):
        _, _, body = call
        if body[0] == "GET":
            return 200, {"result": box["v"]}
        box["v"] = body[2]
        return 200, {"result": "OK"}
    storage.aiohttp = fake_session(handler)

    main.blacklist.add("UP")
    main.settings["chat_id"] = 777
    await main._save_remote(main._state_payload())
    assert box["v"] and "UP" in box["v"]

    # файл врёт — Upstash важнее
    with open(STATE, "w") as f:
        json.dump({"blacklist": ["ИЗ_ФАЙЛА"]}, f)
    main.blacklist.clear(); main.settings["chat_id"] = None
    await main.load_state()
    assert main.blacklist == {"UP"} and main.settings["chat_id"] == 777
    assert main.storage_source == "Upstash"

    # Upstash недоступен — откат на файл, ошибка записана
    def boom(call): raise ConnectionError("нет сети")
    storage.aiohttp = fake_session(boom)
    main.blacklist.clear()
    await main.load_state()
    assert main.blacklist == {"ИЗ_ФАЙЛА"}, main.blacklist
    assert main.storage_error
    storage.URL = storage.TOKEN = ""


# ------------------------------------------------- CoinGecko
@check
async def test_coingecko():
    reset()
    pages = {"n": 0, "status": 200}
    def handler(call):
        pages["n"] += 1
        if pages["status"] != 200:
            return pages["status"], {}
        if pages["n"] == 1:
            return 200, [{"symbol": "s%d" % i} for i in range(250)]
        if pages["n"] == 2:
            return 200, [{"symbol": "doge"}]
        return 200, []
    coingecko.aiohttp = fake_session(handler)
    coingecko.PAUSE = 0

    syms, err = await coingecko.fetch_meme_symbols(main.log)
    assert len(syms) == 251 and "DOGE" in syms and not err, (len(syms), err)

    # 429 и обрыв не должны обнулять прежний список
    main.cg_symbols = {"DOGE", "PEPE"}
    main.cg_last_refresh = 0.0
    pages["status"] = 429
    assert await main.refresh_coingecko(force=True) is False
    assert main.cg_symbols == {"DOGE", "PEPE"}, "429 обнулил список"
    pages["status"] = 200; pages["n"] = 0
    assert await main.refresh_coingecko(force=True) is True
    assert len(main.cg_symbols) == 251

    # раз в сутки, не чаще
    pages["n"] = 0
    assert await main.refresh_coingecko() is False and pages["n"] == 0


# ------------------------------------------------- сигналы
@check
async def test_candidates_parallel_and_isolated():
    reset()
    async def slow(sym, cur):
        await asyncio.sleep(0.2)
        return 0.0, 0.0, 0.0
    main.get_long_term_changes = slow
    SENT.clear()
    started = time.time()
    await main.process_candidates([cand("C%dUSDT" % i) for i in range(20)], Counter(), time.time())
    spent = time.time() - started
    assert len(SENT) == 20, len(SENT)
    assert spent < 1.5, "запросы свечей идут последовательно: %.1f с" % spent

    async def flaky(sym, cur):
        if sym == "BADUSDT":
            raise RuntimeError("таймаут")
        return 0.0, 0.0, 0.0
    main.get_long_term_changes = flaky
    main.daily_memory.clear(); SENT.clear()
    await main.process_candidates([cand("BADUSDT"), cand("OKUSDT")], Counter(), time.time())
    assert len(SENT) == 2, "упавшая свеча съела чужой сигнал"


@check
async def test_day_filter_off_by_zero():
    reset()
    async def zero(sym, cur): return 0.0, 0.0, 0.0
    main.get_long_term_changes = zero

    class Msg:
        async def answer(self, text, **k): self.t = text
    class Cmd:
        def __init__(self, a): self.args = a

    m = Msg(); await main.set_day_drop(m, Cmd("0"))
    assert main.settings["day_drop"] == 0.0 and "ВЫКЛЮЧЕН" in m.t
    assert main.fmt_min(0.0) == "Выкл" and main.fmt_min(-8.0) == "-8.0%"
    # монета в плюсе за сутки: раньше отсеивалась даже при нуле
    main.daily_memory.clear(); SENT.clear()
    await main.process_candidates([cand(ch24=+8.0)], Counter(), time.time())
    assert len(SENT) == 1, "при /d 0 фильтр 24ч должен быть выключен"

    await main.set_day_drop(Msg(), Cmd("5"))
    assert main.settings["day_drop"] == -5.0


@check
async def test_three_day_thresholds():
    reset()
    # свечи идут от старых к новым
    rows = [[0, str(70 + i)] for i in range(31)]
    main.aiohttp = fake_session(lambda _: (200, rows))
    c3, c7, c30 = await REAL_KLINES("XUSDT", 100.0)
    assert abs(c3 - (100 - 97) / 97 * 100) < 0.01, c3
    assert abs(c7 - (100 - 93) / 93 * 100) < 0.01, c7
    assert abs(c30 - (100 - 70) / 70 * 100) < 0.01, c30
    assert c3 < c7 < c30

    # свежий листинг: свечей меньше четырёх, деления на ноль быть не должно
    main.aiohttp = fake_session(lambda _: (200, [[0, "100"], [0, "90"]]))
    c3, c7, c30 = await REAL_KLINES("NEWUSDT", 50.0)
    assert c3 == c7 == c30 == -50.0, (c3, c7, c30)

    box = {"v": (0.0, 0.0, 0.0)}
    async def stub(sym, cur): return box["v"]
    main.get_long_term_changes = stub

    async def fires(ch3):
        box["v"] = (ch3, 0.0, 0.0)
        main.daily_memory.clear(); SENT.clear()
        await main.process_candidates([cand()], Counter(), time.time())
        return len(SENT)

    main.settings["day3_min_drop"] = -8.0
    assert await fires(-3.0) == 0, "-3% не должно проходить порог -8%"
    assert await fires(-12.0) == 1
    main.settings["day3_min_drop"] = 0.0
    assert await fires(-3.0) == 1, "ноль должен выключать порог"

    main.settings["day3_drop"] = 40.0
    assert await fires(-55.0) == 0, "-55% должно отсеиваться максимумом 40%"
    assert await fires(-20.0) == 1
    main.settings["day3_drop"] = 0.0
    assert await fires(-55.0) == 1

    box["v"] = (-17.5, 0.0, 0.0)
    main.daily_memory.clear(); SENT.clear()
    await main.process_candidates([cand()], Counter(), time.time())
    assert "За 3 дня (до дампа): <b>-17.50%</b>" in SENT[0], SENT[0]


# ------------------------------------------------- /set и канал
@check
async def test_set_roundtrip():
    reset()
    for raw, want in {"https://t.me/ch": "@ch", "t.me/ch": "@ch", "ch": "@ch",
                      "@ch": "@ch", "-1001234567890": "-1001234567890",
                      "": None}.items():
        assert main.norm_channel(raw) == want, raw

    main.settings["channel_id"] = "@ch"
    main.settings.update({"percent": 5.0, "day3_min_drop": -8.0, "day3_drop": 40.0,
                          "month_drop": 75.0, "min_volume": 300000})
    line = main.config_line()
    assert line.startswith("/set ") and "d3min=8" in line and "channel=@ch" in line

    before = {f: main.settings[f] for _, f, _ in main.SET_SPEC}
    main.settings.update({"percent": 99.0, "window_min": 99, "day3_drop": 0.0,
                          "skip_memes": False, "channel_id": None})

    class Msg:
        async def answer(self, text, **k): self.t = text
    class Cmd:
        def __init__(self, a): self.args = a

    main.aiohttp = fake_session(lambda _: (200, GOOD_INFO))
    m = Msg(); await main.set_bulk(m, Cmd(line[len("/set "):]))
    after = {f: main.settings[f] for _, f, _ in main.SET_SPEC}
    assert after == before, [(k, before[k], after[k]) for k in before if before[k] != after[k]]

    # одна опечатка не должна ронять остальную строку
    m = Msg(); await main.set_bulk(m, Cmd("p=7 t=abc zzz=1 v=500000 memes=maybe t=-5"))
    assert main.settings["percent"] == 7.0 and main.settings["min_volume"] == 500000
    assert main.settings["window_min"] == before["window_min"]
    assert "Применено (2)" in m.t and "Пропущено (4)" in m.t, m.t


# ------------------------------------------------- диагностика
@check
async def test_incoming_diagnostics():
    reset()
    seen = []
    async def handler(event, data): seen.append(event)

    class Upd:
        message = type("M", (), {"text": "/s",
                                 "from_user": type("U", (), {"id": 42})()})()

    before = main.updates_received
    await main.log_incoming(handler, Upd(), {})
    assert main.updates_received == before + 1 and len(seen) == 1
    await main.log_incoming(handler, type("E", (), {"message": None})(), {})
    assert main.updates_received == before + 2

    # упавший обработчик обязан быть погашен, иначе встанет polling
    assert await main.on_handler_error(
        type("Ev", (), {"exception": ValueError("тест")})()) is True

    cap = {}
    class M:
        chat = type("C", (), {"id": 1})()
        async def answer(self, text, **k): cap["t"] = text
    await main.status_cmd(M())
    assert "Апдейтов от Telegram: %d" % main.updates_received in cap["t"]
    assert main.version_line()


# ------------------------------------------------- кнопка «в ЧС» под сигналом
def bl_cb(markup):
    """callback_data кнопки ЧС, где бы она ни стояла."""
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data][0]


def check_urls(markup):
    """{текст кнопки: ссылка} для всех кнопок-ссылок."""
    return {b.text: b.url for row in markup.inline_keyboard for b in row if b.url}


def check_url(markup):
    urls = list(check_urls(markup).values())
    return urls[0] if urls else None


@check
async def test_blacklist_button():
    reset()
    kb = main.alert_keyboard("TICS")
    assert bl_cb(kb) == "bl:add:TICS"
    assert len(bl_cb(main.alert_keyboard("A" * 20)).encode()) <= 64

    # кнопки проверки открывают внешних ботов с вписанным тикером
    urls = check_urls(kb)
    assert urls == {
        "🕌 Проверка халяль": "https://t.me/CryptoGulfHalal_Bot?text=TICS",
        "📊 Проверка маркетов": "https://t.me/SpreaderApp?text=TICS",
    }, urls
    assert len(check_urls(main.alert_keyboard("TICS", in_blacklist=True))) == 2, \
        "после нажатия ЧС кнопки проверки пропали"
    saved = list(main.CHECK_BOTS)
    main.CHECK_BOTS[:] = [(label, "") if "маркетов" in label else (label, b) for label, b in saved]
    assert list(check_urls(main.alert_keyboard("TICS"))) == ["🕌 Проверка халяль"], \
        "пустое имя бота должно убирать только его кнопку"
    main.CHECK_BOTS[:] = [(label, "") for label, _ in saved]
    assert not check_urls(main.alert_keyboard("TICS")), "без ботов кнопок проверки быть не должно"
    assert bl_cb(main.alert_keyboard("TICS")) == "bl:add:TICS", "кнопка ЧС должна остаться"
    main.CHECK_BOTS[:] = saved

    # кнопка уходит только админу: в канале её могли бы нажать подписчики
    sent = []
    async def capture(chat, text, **k): sent.append((chat, k.get("reply_markup")))
    async def zero(sym, cur): return 0.0, 0.0, 0.0
    main.bot.send_message = capture
    main.get_long_term_changes = zero
    main.settings["chat_id"] = 111
    main.settings["channel_id"] = "@chan"
    await main.process_candidates([cand("TICSUSDT")], Counter(), time.time())
    main.bot.send_message = fake_send
    by_chat = dict(sent)
    assert by_chat[111] is not None, "у админа нет кнопки"
    assert by_chat["@chan"] is None, "кнопка попала в канал"
    assert bl_cb(by_chat[111]) == "bl:add:TICS"
    assert check_url(by_chat[111]), "у админа нет кнопки проверки"

    class Chat:
        def __init__(self, cid): self.id = cid
    class Msg:
        def __init__(self, cid): self.chat, self.markup = Chat(cid), None
        async def edit_reply_markup(self, reply_markup=None): self.markup = reply_markup
    class CB:
        def __init__(self, data, cid=111):
            self.data, self.message = data, Msg(cid)
            self.from_user = type("U", (), {"id": 42})()
            self.toast = None
        async def answer(self, text=None, show_alert=False): self.toast = text

    cb = CB("bl:add:TICS")
    await main.on_blacklist_button(cb)
    assert "TICS" in main.blacklist, "кнопка не добавила в ЧС"
    assert bl_cb(cb.message.markup) == "bl:del:TICS"
    assert check_url(cb.message.markup), "кнопка проверки пропала после нажатия ЧС"
    assert "добавлена" in cb.toast
    with open(STATE) as f:
        assert "TICS" in json.load(f)["blacklist"], "ЧС после кнопки не сохранился"

    cb = CB("bl:del:TICS")
    await main.on_blacklist_button(cb)
    assert "TICS" not in main.blacklist, "откат не сработал"
    assert bl_cb(cb.message.markup) == "bl:add:TICS"

    # чужой чат (например, пересланное сообщение) ЧС не меняет
    cb = CB("bl:add:TICS", cid=999)
    await main.on_blacklist_button(cb)
    assert "TICS" not in main.blacklist and "только из чата админа" in cb.toast

    # мусор в callback_data
    for junk in ("bl:add:", "bl:add:ti cs", "bl:zap:TICS", "bl:add:TICS:x", "bl:add:" + "A" * 30):
        cb = CB(junk)
        await main.on_blacklist_button(cb)
        assert not main.blacklist, "мусор %r изменил ЧС" % junk

    # если сообщение слишком старое и кнопку не обновить, ЧС всё равно меняется
    class OldMsg(Msg):
        async def edit_reply_markup(self, reply_markup=None):
            raise RuntimeError("message is too old")
    cb = CB("bl:add:OLD"); cb.message = OldMsg(111)
    await main.on_blacklist_button(cb)
    assert "OLD" in main.blacklist


# ------------------------------------------------- халяль-проверка
# Ответ бота ровно в том виде, в каком его прислали (с двойными пробелами).
HARAM_REPLY = """أسم العملة: MBL
حكم العملة:  غير مباح ❌
رابط الحكم:  https://t.me/CrypoIslam/322
مصدر الحكم: كريبتو اسلام
تحذير: يرجى التحقق من الرابط، قد يكون تغير الحكم"""


@check
async def test_halal_parse():
    r = halal.parse_reply(HARAM_REPLY, "MBL")
    assert r["status"] == halal.HARAM, r
    assert r["link"] == "https://t.me/CrypoIslam/322" and r["coin"] == "MBL"
    assert r["source"] == "كريبتو اسلام"
    assert halal.parse_reply(HARAM_REPLY, "mbl")["status"] == halal.HARAM

    # невидимые символы направления текста не должны ломать разбор
    rtl = HARAM_REPLY.replace("غير", "‏غير‎").replace("حكم", "‫حكم")
    assert halal.parse_reply(rtl, "MBL")["status"] == halal.HARAM

    ok = HARAM_REPLY.replace("غير مباح ❌", "مباح ✅")
    assert halal.parse_reply(ok, "MBL")["status"] == halal.HALAL

    # всё непонятное — «не проверено», а не харам
    doubtful = HARAM_REPLY.replace("غير مباح ❌", "مشبوه ⚠️")
    r = halal.parse_reply(doubtful, "MBL")
    assert r["status"] == halal.UNKNOWN and "مشبوه" in r["note"]
    r = halal.parse_reply("لم يتم العثور على العملة", "QQQ")
    assert r["status"] == halal.UNKNOWN and "нет вердикта" in r["note"]
    r = halal.parse_reply(HARAM_REPLY, "TICS")
    assert r["status"] == halal.UNKNOWN and "другую монету" in r["note"], \
        "ответ про чужую монету не должен блокировать нашу"


class FakeConv:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def send_message(self, text): self.sent.append(text)
    async def get_response(self, timeout=None):
        if not self.replies:
            raise asyncio.TimeoutError()
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return types.SimpleNamespace(raw_text=item)


class FakeClient:
    def __init__(self, script):
        self.script, self.asked = script, []
    def conversation(self, bot, **kw):
        conv = FakeConv(self.script.get(len(self.asked), []))
        self.asked.append(bot)
        return conv


@check
async def test_halal_checker():
    checker = halal.HalalChecker(main.log)
    saved = halal.MIN_INTERVAL
    halal.MIN_INTERVAL = 0
    try:
        # сначала «ищу…», вердикт следующим сообщением
        checker.client = FakeClient({0: ["🔎 جاري البحث...", HARAM_REPLY]})
        v = await checker.check("MBL")
        assert v["status"] == halal.HARAM, v
        assert checker.client.asked == [halal.BOT]

        # повторный запрос берётся из кэша, бот не дёргается
        v = await checker.check("mbl")
        assert v["status"] == halal.HARAM and len(checker.client.asked) == 1

        # бот молчит — «не проверено», без исключения
        checker.client = FakeClient({0: []})
        v = await checker.check("SILENT")
        assert v["status"] == halal.UNKNOWN and "не ответил" in v["note"]

        # обрыв связи — тоже без исключения
        checker.client = FakeClient({0: [ConnectionError("нет сети")]})
        v = await checker.check("BROKEN")
        assert v["status"] == halal.UNKNOWN and "ошибка" in v["note"]

        # «не проверено» живёт в кэше меньше, чем вердикт
        checker.cache["OLDU"] = {"status": halal.UNKNOWN, "ts": time.time() - halal.TTL_UNKNOWN - 1}
        checker.cache["OLDH"] = {"status": halal.HARAM, "ts": time.time() - halal.TTL_UNKNOWN - 1}
        assert checker.cached("OLDU") is None and checker.cached("OLDH")

        # без клиента — честное «не подключён»
        checker.client = None
        v = await checker.check("NOPE")
        assert v["status"] == halal.UNKNOWN and "не подключён" in v["note"]
    finally:
        halal.MIN_INTERVAL = saved


class StubChecker:
    def __init__(self, verdicts): self.verdicts, self.asked = verdicts, []
    @staticmethod
    def is_configured(): return True
    ready = True
    cache = {}
    def cached(self, t):
        v = self.verdicts.get(t.upper())
        return dict(v, ts=time.time()) if v else None
    async def check(self, t, fresh=False):
        self.asked.append(t)
        return self.verdicts.get(t, {"status": halal.UNKNOWN, "note": "бот <молчит> & всё"})
    def export_cache(self): return {}
    def import_cache(self, d): pass
    def describe(self): return "заглушка"


@check
async def test_halal_blocks_alerts():
    reset()
    async def zero(sym, cur): return 0.0, 0.0, 0.0
    main.get_long_term_changes = zero
    real = main.halal_checker
    main.halal_checker = StubChecker({
        "MBL": {"status": halal.HARAM, "link": "https://t.me/CrypoIslam/322"},
        "GOOD": {"status": halal.HALAL},
    })
    try:
        SENT.clear()
        stats = Counter()
        await main.process_candidates(
            [cand("MBLUSDT"), cand("GOODUSDT"), cand("ODDUSDT")], stats, time.time())
        assert "MBL" in main.blacklist, "харам-монета не попала в ЧС"
        assert stats["харам"] == 1
        texts = "\n".join(SENT)
        assert "ДАМП: <code>MBL</code>" not in texts, "по харам-монете пришёл сигнал"
        good = [t for t in SENT if "<code>GOOD</code>" in t][0]
        assert "Халяль: <b>مباح ✅</b>" in good
        odd = [t for t in SENT if "<code>ODD</code>" in t][0]
        assert "не проверено" in odd, "непонятный ответ должен пропускать сигнал с пометкой"
        assert "&lt;молчит&gt; &amp;" in odd, "текст ответа не экранирован для HTML"
        with open(STATE) as f:
            assert "MBL" in json.load(f)["blacklist"]

        # ты вернул MBL из ЧС — проверка больше не должна её блокировать
        class Msg:
            async def answer(self, text, **k): self.t = text
        class Cmd:
            def __init__(self, a): self.args = a
        await main.del_blacklist(Msg(), Cmd("MBL"))
        assert "MBL" not in main.blacklist and "MBL" in main.halal_ignore
        main.daily_memory.clear(); SENT.clear()
        await main.process_candidates([cand("MBLUSDT")], Counter(), time.time())
        assert len(SENT) == 1 and "не проверяется" in SENT[0], SENT
        assert "MBL" not in main.blacklist

        # а /b снова отдаёт монету под проверку
        await main.add_blacklist(Msg(), Cmd("MBL"))
        assert "MBL" not in main.halal_ignore

        # выключили проверку — сигналы без вопросов к боту
        main.settings["halal_check"] = False
        main.halal_checker.asked.clear(); main.daily_memory.clear(); SENT.clear()
        await main.process_candidates([cand("ODDUSDT")], Counter(), time.time())
        assert len(SENT) == 1 and not main.halal_checker.asked and "Халяль" not in SENT[0]
    finally:
        main.halal_checker = real

    # без настроек юзербота сигналы идут как раньше
    reset()
    main.get_long_term_changes = zero
    SENT.clear()
    await main.process_candidates([cand("ODDUSDT")], Counter(), time.time())
    assert len(SENT) == 1 and "Халяль" not in SENT[0]


@check
async def test_halal_state_persists():
    reset()
    main.halal_ignore.add("MBL")
    main.halal_checker.cache["XYZ"] = {"status": halal.HARAM, "ts": time.time()}
    main.settings["halal_check"] = False
    main.save_state()
    main.halal_ignore.clear(); main.halal_checker.cache.clear()
    main.settings["halal_check"] = True
    await main.load_state()
    assert main.halal_ignore == {"MBL"}
    assert main.halal_checker.cached("XYZ")["status"] == halal.HARAM
    assert main.settings["halal_check"] is False

async def main_run():
    failed = []
    for fn in checks:
        name = fn.__name__.replace("test_", "")
        try:
            await fn()
            print("  ок   %s" % name)
        except Exception as e:
            failed.append((name, e))
            print("  СБОЙ %s: %r" % (name, e))
    if os.path.exists(STATE):
        os.remove(STATE)
    print()
    if failed:
        print("ПРОВАЛЕНО %d из %d" % (len(failed), len(checks)))
        return 1
    print("ВСЕ %d ПРОВЕРОК ПРОЙДЕНЫ" % len(checks))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_run()))
