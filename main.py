import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiohttp import web
import time
import json
import traceback
from collections import deque, Counter
from datetime import datetime, timezone
import os

import coingecko
import storage
from memecoins import classify, MEME_PLATE_MARKERS

# ================= ЛОГИ =================

def log(msg, tag="INFO"):
    """Единая точка логирования: время UTC + тег, чтобы грепать по логам Render."""
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print("%s [%s] %s" % (stamp, tag, msg), flush=True)

# ================= НАСТРОЙКИ =================
# Токен читается из переменной окружения BOT_TOKEN.
# Значение ниже оставлено запасным, чтобы не сломать текущий деплой, но оно
# лежит в открытом git — отзови его через @BotFather и задай новый через env.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8145739398:AAG3dl79hQnSsTe1KoYGt9hvaaUsR3XXllY").strip()

# Версия кода. Видна в /start, /s и в логе при старте — чтобы сразу отвечать
# на вопрос «какой код реально крутится на этом сервисе».
# Поднимать вручную при заметных правках.
CODE_VERSION = "2026-09-13 memes+cg"

# Render сам подставляет эти переменные в окружение сервиса.
RENDER_SERVICE = os.environ.get("RENDER_SERVICE_NAME", "")
RENDER_BRANCH = os.environ.get("RENDER_GIT_BRANCH", "")
RENDER_COMMIT = os.environ.get("RENDER_GIT_COMMIT", "")[:7]

STATE_FILE = os.environ.get("STATE_FILE", "filters_state.json")
EXCHANGE_INFO_URL = "https://api.mexc.com/api/v3/exchangeInfo"
KLINE_CONCURRENCY = 10   # сколько запросов свечей держать одновременно

settings = {
    "percent": 5.0,          # Порог падения в окне (%)
    "window_min": 15,        # Окно анализа (мин)
    "hour_percent": 10.0,    # Порог падения за 1 час (%)
    "check_interval": 30,    # Как часто проверять (сек)
    "min_volume": 100000,    # Мин. объем 24ч ($)
    "day_drop": 0.0,         # Порог падения за 24ч (%) (0 - выключено)
    "cooldown_min": 5,       # Минимальная пауза от спама (мин)
    "week_min_drop": 0.0,    # МИН. порог падения за 7 дней (0 - выключено)
    "week_drop": 0.0,        # МАКС. падение за 7 дней (0 - выключено)
    "month_min_drop": 0.0,   # МИН. порог падения за 30 дней (0 - выключено)
    "month_drop": 0.0,       # МАКС. падение за 30 дней (0 - выключено)
    "chat_id": None,         # ID админа (куда пишутся логи и команды)
    "channel_id": None,      # ID или юзернейм канала для дублирования сигналов
    "skip_memes": True,      # Пропускать мемкоины
    "info_refresh_min": 60,  # Как часто обновлять exchangeInfo (мин)
    "use_coingecko": True,   # Брать мемкоины ещё и из категории meme-token
    "cg_refresh_hours": 24   # Как часто обновлять список CoinGecko
}

price_history = {}
price_history_hour = {}
blacklist = set()          # ручной ЧС, базовые тикеры (BTC, а не BTCUSDT)
allowlist = set()          # ручные исключения из мем-фильтра
daily_memory = {}

symbol_meta = {}           # PAIR -> {base, plates, full_name, st, tradable}
meme_pairs = {}            # PAIR -> причина, по которой признан мемом
plate_counter = Counter()  # какие conceptPlates вообще встречаются
last_ticker = {}           # PAIR -> {price, vol}, нужен для диагностики /why
info_last_refresh = 0.0
info_last_error = ""
storage_source = "файл"    # откуда прочитано состояние — видно в /s
storage_error = ""
cg_symbols = set()         # тикеры мемкоинов из CoinGecko
cg_last_refresh = 0.0
cg_last_error = ""

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ================= СОХРАНЕНИЕ СОСТОЯНИЯ =================
# На Render диск эфемерный: файл переживает рестарт процесса, но не редеплой.
# Постоянные правки списков делай в memecoins.py — он в git.

def apply_env_overrides():
    """CHAT_ID и CHANNEL_ID из окружения важнее файла: диск на Render
    эфемерный и при редеплое filters_state.json пропадает."""
    raw = os.environ.get("CHAT_ID", "").strip()
    if raw:
        try:
            settings["chat_id"] = int(raw)
            log("chat_id взят из переменной окружения: %s" % raw)
        except ValueError:
            log("CHAT_ID=%r не число — игнорирую" % raw, "ERR")
    raw = os.environ.get("CHANNEL_ID", "").strip()
    if raw:
        settings["channel_id"] = raw
        log("channel_id взят из переменной окружения: %s" % raw)


def _read_file():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log("%s не найден" % STATE_FILE)
    except Exception as e:
        log("не смог прочитать %s: %r" % (STATE_FILE, e), "ERR")
    return None


async def load_state():
    """Состояние берём из Upstash, если он настроен, иначе из файла.
    Файл остаётся запасным вариантом, если Redis недоступен."""
    global storage_source, storage_error

    data = None
    from_file = False
    if storage.is_configured():
        data, storage_error = await storage.load(log)
        if data is not None:
            storage_source = "Upstash"
        elif storage_error:
            log("Upstash недоступен — пробую локальный файл", "WARN")
            storage_source = "файл (Upstash с ошибкой)"
        else:
            storage_source = "Upstash"
    if data is None:
        data = _read_file()
        from_file = data is not None
        if from_file and storage_source == "файл":
            storage_source = "файл"

    if data is None:
        log("состояние пустое — стартую с чистыми списками")
        return

    blacklist.update(str(x).upper() for x in data.get("blacklist", []))
    allowlist.update(str(x).upper() for x in data.get("allowlist", []))
    for key in ("skip_memes", "use_coingecko"):
        if key in data:
            settings[key] = bool(data[key])
    if data.get("cg_symbols"):
        global cg_symbols, cg_last_refresh
        cg_symbols = set(data["cg_symbols"])
        cg_last_refresh = float(data.get("cg_last_refresh") or 0)
        log("CoinGecko: из кэша поднято %d тикеров (обновлялось %s)"
            % (len(cg_symbols), _ago(cg_last_refresh)))
    for key in ("chat_id", "channel_id"):
        if data.get(key):
            settings[key] = data[key]
    log("состояние загружено из «%s»: ЧС=%d, allowlist=%d, skip_memes=%s"
        % (storage_source, len(blacklist), len(allowlist), settings["skip_memes"]))

    if from_file and storage.is_configured() and not storage_error:
        # Первый запуск с Upstash: переносим то, что накопилось в файле.
        log("переношу состояние из файла в Upstash")
        storage_error = await storage.save(_state_payload(), log)


def _state_payload():
    return {
        "blacklist": sorted(blacklist),
        "allowlist": sorted(allowlist),
        "skip_memes": settings["skip_memes"],
        "chat_id": settings["chat_id"],
        "channel_id": settings["channel_id"],
        "use_coingecko": settings["use_coingecko"],
        "cg_symbols": sorted(cg_symbols),
        "cg_last_refresh": cg_last_refresh,
    }


async def _save_remote(payload):
    global storage_error
    storage_error = await storage.save(payload, log)


def save_state():
    """Пишем в файл всегда, в Upstash — фоном, чтобы не тормозить команды."""
    payload = _state_payload()
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("не смог сохранить %s: %r" % (STATE_FILE, e), "ERR")

    if storage.is_configured():
        try:
            asyncio.get_running_loop().create_task(_save_remote(payload))
        except RuntimeError:
            # Вне цикла событий (например, в тестах) — остаётся только файл.
            pass


def norm_base(arg):
    """'btc', 'BTCUSDT', ' btc ' -> 'BTC'"""
    coin = (arg or "").strip().upper()
    return coin[:-4] if coin.endswith("USDT") and len(coin) > 4 else coin

# ================= TELEGRAM UI =================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    settings["chat_id"] = message.chat.id
    save_state()
    await message.answer(
        "🚀 <b>Бот-сканер MEXC запущен</b>\n"
        f"<code>{version_line()}</code>\n\n"
        f"📉 Порог окна: <b>{settings['percent']}%</b>\n"
        f"⏱ Порог за 1 час: <b>{settings['hour_percent']}%</b>\n"
        f"📅 Порог 24ч: <b>{fmt_min(settings['day_drop'])}</b>\n"
        f"📆 Мин. порог 7д: <b>{settings['week_min_drop']}%</b>\n"
        f"🗓 Мин. порог 30д: <b>{settings['month_min_drop']}%</b>\n"
        f"📆 Фильтр макс 7д: <b>{fmt_max(settings['week_drop'])}</b>\n"
        f"🗓 Фильтр макс 30д: <b>{fmt_max(settings['month_drop'])}</b>\n"
        f"💰 Мин. объём: <b>{settings['min_volume']:,}$</b>\n"
        f"🎭 Мем-фильтр: <b>{'ВКЛ' if settings['skip_memes'] else 'выкл'}</b> (мемов в базе: {len(meme_pairs)})\n"
        f"📢 Канал: <b>{settings['channel_id'] or 'Не задан'}</b>\n\n"
        "⚙️ <b>Команды:</b>\n"
        "/p 5 — % падения в окне\n"
        "/ph 8 — % падения за 1 час\n"
        "/d 5 — мин. % падения за 24ч\n"
        "/wmin 10 — мин. % падения за 7 дней (0=выкл)\n"
        "/mmin 20 — мин. % падения за 30 дней (0=выкл)\n"
        "/w 30 — скрыть, если упала >30% за 7 дней (0=выкл)\n"
        "/m 50 — скрыть, если упала >50% за 30 дней (0=выкл)\n"
        "/t 10 — окно (мин)\n"
        "/v 200000 — объем $\n"
        "/b BTC — в ЧС\n"
        "/unb BTC — убрать из ЧС\n"
        "/bl — показать ЧС и исключения\n"
        "/memes on|off — фильтр мемкоинов\n"
        "/allow PEPE — считать монету НЕ мемом\n"
        "/plates — категории монет по данным MEXC\n"
        "/cg on|off|refresh — второй источник мемов (CoinGecko)\n"
        "/why DOGE — почему монета проходит/не проходит\n"
        "/channel @имя_канала — куда дублировать сигналы (пусто = выкл)\n"
        "/s — статус"
        , parse_mode="HTML")

@dp.message(Command("channel"))
async def set_channel(message: types.Message, command: CommandObject):
    if command.args:
        settings["channel_id"] = command.args
        save_state()
        await message.answer(f"✅ Канал для сигналов установлен: <b>{command.args}</b>\n<i>Не забудь сделать бота администратором в этом канале!</i>", parse_mode="HTML")
    else:
        settings["channel_id"] = None
        save_state()
        await message.answer("✅ Дублирование в канал <b>ОТКЛЮЧЕНО</b>", parse_mode="HTML")

@dp.message(Command("p"))
async def set_percent(message: types.Message, command: CommandObject):
    try:
        val = float(command.args.replace(',', '.'))
        settings["percent"] = val
        await message.answer(f"✅ Порог падения: <b>{val}%</b>", parse_mode="HTML")
    except: await message.answer("❌ Ошибка. Пример: /p 7.5")

@dp.message(Command("ph"))
async def set_hour_percent(message: types.Message, command: CommandObject):
    try:
        val = float(command.args.replace(',', '.'))
        settings["hour_percent"] = val
        await message.answer(f"✅ Порог падения за 1 час: <b>{val}%</b>", parse_mode="HTML")
    except: await message.answer("❌ Ошибка. Пример: /ph 8")

@dp.message(Command("d"))
async def set_day_drop(message: types.Message, command: CommandObject):
    try:
        val = float(command.args.replace(',', '.'))
        settings["day_drop"] = -abs(val) if val != 0 else 0.0
        if val == 0:
            await message.answer(
                "✅ Фильтр 24ч <b>ВЫКЛЮЧЕН</b>\n"
                "<i>Теперь ловятся и ножи на монетах, которые за сутки в плюсе.</i>",
                parse_mode="HTML")
        else:
            await message.answer(f"✅ Фильтр 24ч: монета должна быть в минусе минимум на "
                                 f"<b>{settings['day_drop']}%</b> за 24ч", parse_mode="HTML")
    except: await message.answer("❌ Ошибка. Пример: /d 5 (для отключения введи /d 0)")

@dp.message(Command("wmin"))
async def set_week_min_drop(message: types.Message, command: CommandObject):
    try:
        val = float(command.args.replace(',', '.'))
        settings["week_min_drop"] = -abs(val) if val != 0 else 0.0
        if val == 0:
            await message.answer("✅ Мин. порог падения за 7 дней <b>ВЫКЛЮЧЕН</b>", parse_mode="HTML")
        else:
            await message.answer(f"✅ Порог за 7 дней: монета должна упасть минимум на <b>{settings['week_min_drop']}%</b>", parse_mode="HTML")
    except: await message.answer("❌ Ошибка. Пример: /wmin 10")

@dp.message(Command("mmin"))
async def set_month_min_drop(message: types.Message, command: CommandObject):
    try:
        val = float(command.args.replace(',', '.'))
        settings["month_min_drop"] = -abs(val) if val != 0 else 0.0
        if val == 0:
            await message.answer("✅ Мин. порог падения за 30 дней <b>ВЫКЛЮЧЕН</b>", parse_mode="HTML")
        else:
            await message.answer(f"✅ Порог за 30 дней: монета должна упасть минимум на <b>{settings['month_min_drop']}%</b>", parse_mode="HTML")
    except: await message.answer("❌ Ошибка. Пример: /mmin 20")

@dp.message(Command("w"))
async def set_week_drop(message: types.Message, command: CommandObject):
    try:
        val = abs(float(command.args.replace(',', '.')))
        settings["week_drop"] = val
        if val == 0:
            await message.answer("✅ Макс. фильтр 7 дней <b>ВЫКЛЮЧЕН</b>", parse_mode="HTML")
        else:
            await message.answer(f"✅ Макс. фильтр 7 дней: скрывать монеты, упавшие больше чем на <b>-{val}%</b>", parse_mode="HTML")
    except: await message.answer("❌ Ошибка. Пример: /w 30 (для отключения введи /w 0)")

@dp.message(Command("m"))
async def set_month_drop(message: types.Message, command: CommandObject):
    try:
        val = abs(float(command.args.replace(',', '.')))
        settings["month_drop"] = val
        if val == 0:
            await message.answer("✅ Макс. фильтр 30 дней <b>ВЫКЛЮЧЕН</b>", parse_mode="HTML")
        else:
            await message.answer(f"✅ Макс. фильтр 30 дней: скрывать монеты, упавшие больше чем на <b>-{val}%</b>", parse_mode="HTML")
    except: await message.answer("❌ Ошибка. Пример: /m 50 (для отключения введи /m 0)")

@dp.message(Command("t"))
async def set_time(message: types.Message, command: CommandObject):
    if command.args and command.args.isdigit():
        settings["window_min"] = int(command.args)
        await message.answer(f"✅ Окно: <b>{command.args} мин</b>", parse_mode="HTML")

@dp.message(Command("v"))
async def set_volume(message: types.Message, command: CommandObject):
    if command.args and command.args.isdigit():
        settings["min_volume"] = int(command.args)
        await message.answer(f"✅ Объём: <b>{settings['min_volume']:,}$</b>", parse_mode="HTML")

def fmt_min(val):
    """Порог «минимум столько-то падения»: 0 означает выключено."""
    return "Выкл" if val == 0 else "%s%%" % val


def fmt_max(val):
    """'Выкл' или '-30.0%' — вынесено из f-строк ради Python < 3.12."""
    return "Выкл" if val == 0 else "-%s%%" % val


def version_line():
    """Короткая строка «что именно запущено»."""
    parts = [CODE_VERSION]
    if RENDER_SERVICE:
        parts.append("сервис %s" % RENDER_SERVICE)
    if RENDER_BRANCH:
        parts.append("ветка %s" % RENDER_BRANCH)
    if RENDER_COMMIT:
        parts.append("коммит %s" % RENDER_COMMIT)
    return " | ".join(parts)


def _ago(stamp):
    if not stamp:
        return "никогда"
    mins = int((time.time() - stamp) / 60)
    return "%d ч назад" % (mins // 60) if mins >= 120 else "%d мин назад" % mins


def info_age():
    if not info_last_refresh:
        return "ещё не обновлялось"
    return _ago(info_last_refresh)


@dp.message(Command("b"))
async def add_blacklist(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("❌ Пример: /b DOGE")
    base = norm_base(command.args)
    blacklist.add(base)
    allowlist.discard(base)
    save_state()
    log("ручной ЧС += %s (всего %d)" % (base, len(blacklist)), "CMD")
    await message.answer(f"🚫 <b>{base}</b> в ЧС", parse_mode="HTML")


@dp.message(Command("unb"))
async def del_blacklist(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("❌ Пример: /unb DOGE")
    base = norm_base(command.args)
    if base in blacklist:
        blacklist.discard(base)
        save_state()
        log("ручной ЧС -= %s (всего %d)" % (base, len(blacklist)), "CMD")
        await message.answer(f"✅ <b>{base}</b> убрана из ЧС", parse_mode="HTML")
    else:
        await message.answer(f"ℹ️ <b>{base}</b> и так не в ЧС", parse_mode="HTML")


@dp.message(Command("bl"))
async def show_blacklist(message: types.Message):
    bl = ", ".join(sorted(blacklist)) or "пусто"
    al = ", ".join(sorted(allowlist)) or "пусто"
    await message.answer(
        f"🚫 <b>Ручной ЧС</b> ({len(blacklist)}):\n{bl[:1500]}\n\n"
        f"✅ <b>Исключения из мем-фильтра</b> ({len(allowlist)}):\n{al[:1500]}\n\n"
        f"🎭 Мемов по данным биржи и эвристикам: <b>{len(meme_pairs)}</b> (см. /plates)",
        parse_mode="HTML")


@dp.message(Command("memes"))
async def toggle_memes(message: types.Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg in ("on", "вкл", "1"):
        settings["skip_memes"] = True
    elif arg in ("off", "выкл", "0"):
        settings["skip_memes"] = False
    else:
        return await message.answer(
            f"🎭 Мем-фильтр: <b>{'ВКЛ' if settings['skip_memes'] else 'выкл'}</b>\n"
            f"Мемов найдено: <b>{len(meme_pairs)}</b>\n"
            "Переключить: /memes on | /memes off", parse_mode="HTML")
    save_state()
    log("мем-фильтр -> %s" % settings["skip_memes"], "CMD")
    await message.answer(
        f"✅ Мем-фильтр <b>{'ВКЛЮЧЕН' if settings['skip_memes'] else 'ВЫКЛЮЧЕН'}</b> "
        f"(монет под фильтром: {len(meme_pairs)})", parse_mode="HTML")


@dp.message(Command("allow"))
async def allow_coin(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("❌ Пример: /allow PEPE — монета перестанет считаться мемом")
    base = norm_base(command.args)
    allowlist.add(base)
    blacklist.discard(base)
    meme_pairs.pop(base + "USDT", None)
    save_state()
    log("allowlist += %s" % base, "CMD")
    await message.answer(f"✅ <b>{base}</b> больше не считается мемом", parse_mode="HTML")


@dp.message(Command("unallow"))
async def unallow_coin(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("❌ Пример: /unallow PEPE")
    base = norm_base(command.args)
    allowlist.discard(base)
    save_state()
    await refresh_exchange_info(force=True)
    await message.answer(f"✅ <b>{base}</b> убрана из исключений", parse_mode="HTML")


@dp.message(Command("cg"))
async def toggle_cg(message: types.Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg in ("on", "вкл", "1"):
        settings["use_coingecko"] = True
    elif arg in ("off", "выкл", "0"):
        settings["use_coingecko"] = False
    elif arg in ("refresh", "обнови"):
        await message.answer("⏳ Обновляю список CoinGecko…")
        await refresh_coingecko(force=True)
        await refresh_exchange_info(force=True)
        return await message.answer(
            f"✅ Тикеров из CoinGecko: <b>{len(cg_symbols)}</b>\n"
            f"Всего мемов: <b>{len(meme_pairs)}</b>\n"
            f"Ошибка: {cg_last_error or 'нет'}", parse_mode="HTML")
    else:
        return await message.answer(
            f"🦎 CoinGecko: <b>{'ВКЛ' if settings['use_coingecko'] else 'выкл'}</b>\n"
            f"Тикеров в списке: <b>{len(cg_symbols)}</b>\n"
            f"Обновлялось: {_ago(cg_last_refresh)}\n"
            f"Ошибка: {cg_last_error or 'нет'}\n\n"
            "/cg on | /cg off | /cg refresh", parse_mode="HTML")
    save_state()
    await refresh_exchange_info(force=True)
    await message.answer(
        f"✅ CoinGecko <b>{'ВКЛЮЧЕН' if settings['use_coingecko'] else 'ВЫКЛЮЧЕН'}</b>\n"
        f"Мемов теперь: <b>{len(meme_pairs)}</b>", parse_mode="HTML")


@dp.message(Command("plates"))
async def show_plates(message: types.Message):
    if not plate_counter:
        return await message.answer(
            f"⚠️ Данные exchangeInfo ещё не загружены.\nОшибка: {info_last_error or 'нет'}")
    matched = sorted(p for p in plate_counter if any(m in p.lower() for m in MEME_PLATE_MARKERS))
    top = "\n".join(f"{n:>5} — {p}" for p, n in plate_counter.most_common(25))
    await message.answer(
        f"📚 <b>Категории MEXC</b> (обновлено {info_age()})\n"
        f"<pre>{top}</pre>\n"
        f"🎭 Распознано как мем-зона: <b>{', '.join(matched) or 'НИЧЕГО'}</b>\n"
        f"Всего мемов: <b>{len(meme_pairs)}</b>",
        parse_mode="HTML")


@dp.message(Command("why"))
async def why_coin(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("❌ Пример: /why DOGE")
    base = norm_base(command.args)
    pair = base + "USDT"
    meta = symbol_meta.get(pair)
    tick = last_ticker.get(pair)

    lines = [f"🔍 <b>{pair}</b>"]
    if meta:
        lines.append(f"📛 Название: {meta['full_name'] or '—'}")
        lines.append(f"🏷 Плашки: {', '.join(meta['plates']) or 'нет'}")
        lines.append(f"⚠️ ST: {'да' if meta['st'] else 'нет'}")
        lines.append(f"💱 Торгуется: {'да' if meta['tradable'] else 'НЕТ'}")
    else:
        lines.append("❓ Пары нет в exchangeInfo (не листингована или кэш не загружен)")

    is_meme, reason = classify(
        base,
        meta["full_name"] if meta else None,
        meta["plates"] if meta else None,
        allowlist,
        cg_symbols)
    lines.append(f"🎭 Мем: <b>{'ДА' if is_meme else 'нет'}</b> (причина: {reason})")
    lines.append(f"🚫 В ручном ЧС: {'да' if base in blacklist else 'нет'}")
    lines.append(f"✅ В исключениях: {'да' if base in allowlist else 'нет'}")
    if tick:
        lines.append(f"💰 Объём 24ч: {int(tick['vol']):,}$ (мин. {settings['min_volume']:,}$)")
        lines.append(f"💵 Цена: {tick['price']}")
    else:
        lines.append("💰 В последнем скане не встречалась")

    if base in blacklist:
        verdict = "ПРОПУСКАЕТСЯ — ручной ЧС"
    elif meta and not meta["tradable"]:
        verdict = "ПРОПУСКАЕТСЯ — не торгуется"
    elif settings["skip_memes"] and is_meme:
        verdict = "ПРОПУСКАЕТСЯ — мемкоин (%s)" % reason
    elif tick and tick["vol"] < settings["min_volume"]:
        verdict = "ПРОПУСКАЕТСЯ — объём ниже минимума"
    else:
        verdict = "СКАНИРУЕТСЯ"
    lines.append(f"\n📌 Итог: <b>{verdict}</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("s"))
async def status_cmd(message: types.Message):
    await message.answer(
        "📊 <b>Статус</b>\n"
        f"📉 Окно: {settings['percent']}% ({settings['window_min']}м)\n"
        f"⏱ За 1 час: {settings['hour_percent']}%\n"
        f"📅 24ч (мин): {fmt_min(settings['day_drop'])}\n"
        f"📆 7 дней (мин): {settings['week_min_drop']}%\n"
        f"🗓 30 дней (мин): {settings['month_min_drop']}%\n"
        f"📆 7 дней (макс): {fmt_max(settings['week_drop'])}\n"
        f"🗓 30 дней (макс): {fmt_max(settings['month_drop'])}\n"
        f"💰 Объём: {settings['min_volume']:,}$\n"
        f"📢 Канал: {settings['channel_id'] or 'Не задан'}\n"
        f"🛑 В памяти дампов: {len(daily_memory)}\n\n"
        f"🎭 Мем-фильтр: {'ВКЛ' if settings['skip_memes'] else 'выкл'}\n"
        f"🚫 Мемов найдено: {len(meme_pairs)}\n"
        f"🦎 CoinGecko: {'ВКЛ' if settings['use_coingecko'] else 'выкл'}, "
        f"тикеров {len(cg_symbols)}, {_ago(cg_last_refresh)}\n"
        f"📚 Пар в exchangeInfo: {len(symbol_meta)}\n"
        f"🕒 Обновлено: {info_age()}\n"
        f"❗ Ошибка API: {info_last_error or 'нет'}\n"
        f"🛠 Ручной ЧС: {len(blacklist)} | Исключения: {len(allowlist)}\n"
        f"🧩 Версия: {version_line()}\n"
        f"💾 Хранилище: {storage.describe()}\n"
        f"📥 Прочитано из: {storage_source}\n"
        f"❗ Ошибка хранилища: {storage_error or 'нет'}"
        , parse_mode="HTML")

# ================= API & LOGIC =================

async def fetch_prices():
    url = "https://api.mexc.com/api/v3/ticker/24hr"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200: return await response.json()
    except Exception as e:
        log("ошибка /ticker/24hr: %r" % e, "ERR")
    return []

# Функция для запроса свечей (7 и 30 дней)
async def get_long_term_changes(symbol, current_price):
    url = f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval=1d&limit=31"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if not data: return 0.0, 0.0
                    # Если монета новая, берем самую старую доступную свечу
                    idx_7 = -8 if len(data) >= 8 else 0
                    idx_30 = -31 if len(data) >= 31 else 0
                    p_7 = float(data[idx_7][1])   # Цена открытия 7 дней назад
                    p_30 = float(data[idx_30][1]) # Цена открытия 30 дней назад
                    c_7 = ((current_price - p_7) / p_7) * 100
                    c_30 = ((current_price - p_30) / p_30) * 100
                    return c_7, c_30
    except:
        pass
    return 0.0, 0.0

async def refresh_coingecko(force=False):
    """Раз в сутки тянет категорию meme-token. Возвращает True, если список
    изменился и монеты надо переклассифицировать."""
    global cg_symbols, cg_last_refresh, cg_last_error

    if not settings["use_coingecko"]:
        return False
    if not force and (time.time() - cg_last_refresh) < settings["cg_refresh_hours"] * 3600:
        return False

    symbols, err = await coingecko.fetch_meme_symbols(log)
    cg_last_error = err

    if not symbols:
        # Ничего не пришло — оставляем прошлый список, иначе мем-фильтр
        # молча похудеет на тысячу тикеров.
        log("CoinGecko: список не обновлён, работаю на прежнем (%d тикеров)"
            % len(cg_symbols), "WARN")
        return False

    added = len(symbols - cg_symbols)
    removed = len(cg_symbols - symbols)
    changed = bool(added or removed)
    cg_symbols = symbols
    cg_last_refresh = time.time()
    if changed:
        log("CoinGecko: список обновлён — стало %d тикеров (+%d, -%d)"
            % (len(cg_symbols), added, removed))
        save_state()
    return changed


async def refresh_exchange_info(force=False):
    """Тянет метаданные символов у MEXC: категории (conceptPlates), полное имя,
    метку ST и флаг торгуемости. При ошибке остаётся на прошлом кэше."""
    global info_last_refresh, info_last_error

    if not force and (time.time() - info_last_refresh) < settings["info_refresh_min"] * 60:
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(EXCHANGE_INFO_URL, timeout=30) as resp:
                if resp.status != 200:
                    info_last_error = "HTTP %s" % resp.status
                    log("exchangeInfo вернул HTTP %s — работаю на старом кэше (%d пар)"
                        % (resp.status, len(symbol_meta)), "ERR")
                    return
                data = await resp.json()
    except Exception as e:
        info_last_error = repr(e)
        log("exchangeInfo недоступен: %r — работаю на старом кэше (%d пар)"
            % (e, len(symbol_meta)), "ERR")
        return

    symbols = data.get("symbols") or []
    if not symbols:
        info_last_error = "пустой список symbols"
        log("exchangeInfo вернул пустой список symbols — кэш не трогаю", "ERR")
        return

    meta, memes = {}, {}
    plates_cnt, status_cnt, reasons, bad = Counter(), Counter(), Counter(), 0
    for s in symbols:
        try:
            if s.get("quoteAsset") != "USDT":
                continue
            pair = s["symbol"]
            base = (s.get("baseAsset") or pair[:-4]).upper()
            plates = [p for p in (s.get("conceptPlates") or []) if p]
            full_name = s.get("fullName") or ""

            status_cnt[str(s.get("status"))] += 1
            for p in plates:
                plates_cnt[p] += 1
            if not plates:
                plates_cnt["<без плашки>"] += 1

            meta[pair] = {
                "base": base,
                "plates": plates,
                "full_name": full_name,
                "st": bool(s.get("st")),
                "tradable": bool(s.get("isSpotTradingAllowed", True)),
            }
            is_meme, reason = classify(base, full_name, plates, allowlist, cg_symbols)
            if is_meme:
                memes[pair] = reason
                reasons[reason.split(":")[0]] += 1
        except Exception as e:
            bad += 1
            log("не разобрал символ %r: %r" % (s.get("symbol"), e), "ERR")

    if not meta and symbol_meta:
        # Ответ разобрался, но USDT-пар в нём нет — скорее всего изменился формат.
        # Затирать рабочий кэш нельзя: молча отключится мем-фильтр.
        info_last_error = "ни одной USDT-пары в ответе"
        log("exchangeInfo: разобрано 0 USDT-пар при %d символах в ответе — "
            "кэш (%d пар) сохранён. Похоже, изменился формат ответа MEXC."
            % (len(symbols), len(symbol_meta)), "ERR")
        return

    symbol_meta.clear()
    symbol_meta.update(meta)
    meme_pairs.clear()
    meme_pairs.update(memes)
    plate_counter.clear()
    plate_counter.update(plates_cnt)
    info_last_refresh = time.time()
    info_last_error = ""

    log("USDT-пар %d, мемов %d, битых символов %d" % (len(meta), len(memes), bad))
    log("статусы symbol.status: %s"
        % ", ".join("%s=%d" % kv for kv in status_cnt.most_common()), "PLATES")
    log("категории (топ-40): %s"
        % ", ".join("%s=%d" % kv for kv in plates_cnt.most_common(40)), "PLATES")

    matched = sorted(p for p in plates_cnt if any(m in p.lower() for m in MEME_PLATE_MARKERS))
    if matched:
        log("мем-зона распознана по плашкам: %s" % ", ".join(matched), "MEME")
    else:
        log("мем-плашка в conceptPlates НЕ найдена. Работают только статический список "
            "и эвристики. Посмотри строку [PLATES] выше и добавь нужный слаг в "
            "MEME_PLATE_MARKERS в memecoins.py", "WARN")

    log("причины срабатывания: %s"
        % (", ".join("%s=%d" % kv for kv in reasons.most_common()) or "нет"), "MEME")
    sample = ", ".join("%s(%s)" % (p[:-4], r) for p, r in sorted(memes.items())[:40])
    log("примеры мемов: %s" % (sample or "нет"), "MEME")

    # Монеты, которые биржа мемами НЕ помечала: только они могут быть
    # ложным срабатыванием, поэтому печатаем их полностью.
    guessed = sorted("%s(%s)" % (pr[:-4], rs) for pr, rs in memes.items()
                     if not rs.startswith("plate:"))
    if guessed:
        shown = guessed[:60]
        tail = " …и ещё %d" % (len(guessed) - len(shown)) if len(guessed) > len(shown) else ""
        log("не по плашке биржи (%d): %s%s — лишнее убирается командой /allow ТИКЕР"
            % (len(guessed), ", ".join(shown), tail), "CHECK")


async def process_candidates(candidates, stats, now):
    """Догружает 7/30-дневную историю параллельно, фильтрует и рассылает сигналы."""
    if not candidates:
        return

    sem = asyncio.Semaphore(KLINE_CONCURRENCY)

    async def fetch(cand):
        async with sem:
            return await get_long_term_changes(cand["pair"], cand["max_p"])

    started = time.time()
    results = await asyncio.gather(*(fetch(c) for c in candidates),
                                   return_exceptions=True)
    if len(candidates) > 1:
        log("свечей запрошено %d за %.1f с (параллельно, до %d разом)"
            % (len(candidates), time.time() - started, KLINE_CONCURRENCY), "DEBUG")

    for cand, res in zip(candidates, results):
        pair = cand["pair"]
        if isinstance(res, Exception):
            log("свечи для %s не пришли: %r — считаю 7д/30д нулями" % (pair, res), "ERR")
            ch_7, ch_30 = 0.0, 0.0
        else:
            ch_7, ch_30 = res

        if settings["week_min_drop"] != 0 and ch_7 > settings["week_min_drop"]:
            continue  # Упала недостаточно за неделю
        if settings["month_min_drop"] != 0 and ch_30 > settings["month_min_drop"]:
            continue  # Упала недостаточно за месяц
        if settings["week_drop"] > 0 and ch_7 < -settings["week_drop"]:
            continue  # Упала слишком сильно за неделю (отсев)
        if settings["month_drop"] > 0 and ch_30 < -settings["month_drop"]:
            continue  # Упала слишком сильно за месяц

        price = cand["price"]
        daily_memory[pair] = {
            "time": daily_memory[pair]["time"] if pair in daily_memory else now,
            "price": price,
            "last_msg": now,
        }

        label = "🔥 <b>ПОВТОРНЫЙ ДАМП (x2)</b>\n" if cand["is_repeat"] else ""
        if cand["window_trigger"] and cand["hour_trigger"]:
            trigger_label = "⚡ Триггер: окно + 1 час\n"
        elif cand["hour_trigger"]:
            trigger_label = "⚡ Триггер: падение за 1 час\n"
        else:
            trigger_label = "⚡ Триггер: окно\n"

        base_coin = pair[:-4]
        drop, hour_drop = cand["drop"], cand["hour_drop"]
        ch_24, vol, max_p = cand["ch_24"], cand["vol"], cand["max_p"]

        alert_text = (
            f"🚨 <b>ДАМП: <code>{base_coin}</code></b>\n{label}{trigger_label}"
            f"📉 В окне: <b>-{drop:.2f}%</b>\n"
            f"⏱ За 1 час: <b>-{hour_drop:.2f}%</b>\n"
            f"📊 За 24 часа: <b>{ch_24:.2f}%</b>\n"
            f"📆 За 7 дней (до дампа): <b>{ch_7:.2f}%</b>\n"
            f"🗓 За 30 дней (до дампа): <b>{ch_30:.2f}%</b>\n"
            f"💵 Было (пик): <code>{max_p}</code>\n"
            f"💸 Стало (тек): <code>{price}</code>\n"
            f"💰 Объём: <b>{int(vol):,}$</b>"
        )

        stats["сигналы"] += 1
        log("СИГНАЛ %s: окно -%.2f%%, час -%.2f%%, 24ч %.2f%%, объём %d$"
            % (base_coin, drop, hour_drop, ch_24, int(vol)), "ALERT")
        try:
            await bot.send_message(settings["chat_id"], alert_text, parse_mode="HTML")
        except Exception as e:
            log("не отправилось админу: %r" % e, "ERR")

        if settings["channel_id"]:
            try:
                await bot.send_message(settings["channel_id"], alert_text, parse_mode="HTML")
            except Exception as e:
                log("не отправилось в канал %s: %r" % (settings["channel_id"], e), "ERR")


async def parser_task():
    log("--- Фоновый парсер запущен ---")
    while True:
        try:
            if settings["chat_id"]:
                if await refresh_coingecko():
                    await refresh_exchange_info(force=True)
                else:
                    await refresh_exchange_info()
                data = await fetch_prices()
                if not data:
                    log("пустой ответ /ticker/24hr — пропускаю цикл", "ERR")
                now = time.time()
                stats = Counter()
                candidates = []
                max_pts = int((settings["window_min"] * 60) / settings["check_interval"])
                max_pts_hour = max(1, int(3600 / settings["check_interval"]))
                cooldown_sec = settings["cooldown_min"] * 60

                for item in data:
                    pair = item['symbol']
                    stats["всего"] += 1
                    if not pair.endswith("USDT"):
                        continue
                    stats["usdt"] += 1
                    base = pair[:-4]

                    # Цену и объём читаем до фильтров: даже отсеянная монета
                    # должна быть видна в /why с актуальными цифрами.
                    try:
                        vol = float(item['quoteVolume'])
                        price = float(item['lastPrice'])
                        ch_24 = float(item['priceChangePercent']) * 100
                    except Exception:
                        stats["битые"] += 1
                        continue
                    last_ticker[pair] = {"price": price, "vol": vol}

                    if pair in blacklist or base in blacklist:
                        stats["ручной_чс"] += 1
                        continue

                    meta = symbol_meta.get(pair)
                    if meta and not meta["tradable"]:
                        stats["неторгуемые"] += 1
                        continue
                    if settings["skip_memes"]:
                        reason = meme_pairs.get(pair)
                        if reason is None and meta is None:
                            # пары нет в exchangeInfo — классифицируем по тикеру
                            is_meme, r = classify(base, allowlist=allowlist,
                                                  cg_symbols=cg_symbols)
                            if is_meme:
                                reason = meme_pairs[pair] = r + "|нет-в-exchangeInfo"
                                log("мем без метаданных: %s (%s)" % (base, r), "MEME")
                        if reason:
                            stats["мемы"] += 1
                            continue

                    if vol < settings["min_volume"]:
                        stats["объём_мал"] += 1
                        continue
                    stats["анализ"] += 1
                    if meta and meta["st"]:
                        stats["st_в_анализе"] += 1

                    if pair not in price_history or price_history[pair].maxlen != max_pts:
                        price_history[pair] = deque(maxlen=max_pts)
                    if pair not in price_history_hour or price_history_hour[pair].maxlen != max_pts_hour:
                        price_history_hour[pair] = deque(maxlen=max_pts_hour)

                    history = price_history[pair]
                    history_hour = price_history_hour[pair]

                    drop = 0.0
                    if len(history) > 0:
                        max_p = max(history)
                        drop = ((max_p - price) / max_p) * 100
                    else:
                        max_p = price

                    hour_drop = 0.0
                    if len(history_hour) > 0:
                        max_p_hour = max(history_hour)
                        hour_drop = ((max_p_hour - price) / max_p_hour) * 100

                    # Очистка старой памяти (старше 24ч)
                    if pair in daily_memory and (now - daily_memory[pair]["time"]) >= 86400:
                        del daily_memory[pair]

                    window_trigger = drop >= settings["percent"]
                    hour_trigger = hour_drop >= settings["hour_percent"]

                    # Проверка базовых условий (срабатывает окно ИЛИ часовой порог)
                    day_ok = settings["day_drop"] == 0 or ch_24 <= settings["day_drop"]
                    if (window_trigger or hour_trigger) and day_ok:
                        should_alert = True
                        is_repeat = False

                        if pair in daily_memory:
                            if (now - daily_memory[pair]["last_msg"]) < cooldown_sec:
                                should_alert = False
                            else:
                                # Условие x2
                                req_drop = settings["percent"] * 2
                                threshold = daily_memory[pair]["price"] * (1 - (req_drop / 100))
                                if price <= threshold:
                                    is_repeat = True
                                else:
                                    should_alert = False

                        if should_alert:
                            # Свечи за 7/30 дней не запрашиваем здесь: при низком
                            # пороге кандидатов десятки, и последовательные запросы
                            # растянули бы цикл. Копим и забираем разом после цикла.
                            candidates.append({
                                "pair": pair, "price": price, "max_p": max_p,
                                "drop": drop, "hour_drop": hour_drop, "ch_24": ch_24,
                                "vol": vol, "is_repeat": is_repeat,
                                "window_trigger": window_trigger,
                                "hour_trigger": hour_trigger,
                            })

                    history.append(price)
                    history_hour.append(price)

                await process_candidates(candidates, stats, now)

                log("usdt=%d мемы=%d чс=%d неторг=%d объём_мал=%d битые=%d "
                    "анализ=%d (из них с меткой ST %d) сигналы=%d"
                    % (stats["usdt"], stats["мемы"], stats["ручной_чс"],
                       stats["неторгуемые"], stats["объём_мал"], stats["битые"],
                       stats["анализ"], stats["st_в_анализе"], stats["сигналы"]), "SCAN")

        except Exception as e:
            log("ошибка парсера: %r" % e, "ERR")
            traceback.print_exc()
        await asyncio.sleep(settings["check_interval"])

# ================= WEB & RUN =================

async def handle_ping(request): return web.Response(text="OK", status=200)

async def main():
    log("=== старт бота ===")
    log("версия кода: %s" % version_line())
    log("хранилище: %s" % storage.describe())
    await load_state()
    apply_env_overrides()
    await refresh_coingecko()
    if settings["chat_id"]:
        log("chat_id восстановлен (%s) — сканирование начнётся сразу"
            % settings["chat_id"])
    else:
        log("chat_id не задан — жду команду /start, до неё парсер простаивает", "WARN")
    await refresh_exchange_info(force=True)

    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    await site.start()

    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(parser_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
