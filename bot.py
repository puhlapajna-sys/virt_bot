import os
import re
import json
import time
import asyncio
import logging
import functools
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, ApplicationHandlerStop, CallbackQueryHandler,
    CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("virts-bot")


def parse_ids(var_name):
    raw = os.environ.get(var_name, "").replace(" ", "")
    return {int(x) for x in raw.split(",") if x.isdigit()}


TOKEN = os.environ.get("BOT_TOKEN", "ТВОЙ_ТОКЕН")
# Telegram ID через запятую. ALLOWED_IDS: кому можно пользоваться ботом
# (пусто = всем). ADMIN_IDS: админы (им доступны /stats, /stats_server и т.д.).
ALLOWED = parse_ids("ALLOWED_IDS")
ADMINS = parse_ids("ADMIN_IDS")
DATA_FILE = os.environ.get("DATA_FILE", "data.json")

URL = "https://funpay.com/chips/51/"
DEFAULT_SERVER = "Grnd.gg Grand Mobile №01"   # сервер по умолчанию
GAME_FILTER = "grand mobile"   # по этой подстроке отбираем серверы в выборе
LIMIT = 30             # сколько лотов всего доступно в списке
PAGE = 10              # сколько лотов на одной странице
SRV_PAGE = 15          # сколько серверов на одной странице выбора
MIN_BUTTONS = [0, 10, 50, 100]   # кнопки фильтра по количеству (кк)
CACHE_TTL = 10         # секунд: не дёргаем FunPay чаще
RATES_TTL = 3600       # секунд: курс валют обновляем раз в час
CHECK_INTERVAL = 120   # секунд: как часто проверять уведомления
ALERT_MAX_LOTS = 5     # сколько лотов показывать в одном уведомлении
CALC_MAX_LOTS = 6      # сколько лотов показывать в разбивке /calc

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
TIMEOUT = (5, 12)

CANDIDATES = [
    {"name": "cookie cy=rub", "cookies": {"cy": "rub"}, "params": {}},
    {"name": "cookie cy=RUB", "cookies": {"cy": "RUB"}, "params": {}},
    {"name": "cookie cy=1", "cookies": {"cy": "1"}, "params": {}},
    {"name": "cookie cy=2", "cookies": {"cy": "2"}, "params": {}},
    {"name": "param ?cy=rub", "cookies": {}, "params": {"cy": "rub"}},
    {"name": "без параметров", "cookies": {}, "params": {}},
]
_working = None
_no_rub_until = 0.0
_cache = {"t": 0.0, "data": None}
_rates = {"t": 0.0, "EUR": None, "USD": None, "src": "нет"}


# ---------- хранилище (фильтры, сервер, уведомления, пользователи) ----------

STORE = {"chats": {}, "users": {}}


def load_store():
    global STORE
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fh:
            STORE = json.load(fh)
        log.info("данные загружены из %s", DATA_FILE)
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("не удалось прочитать %s, начинаю с чистого листа", DATA_FILE)
    STORE.setdefault("chats", {})
    STORE.setdefault("users", {})
    # те, кто пользовался ботом до появления статистики (личные чаты)
    for cid in list(STORE["chats"]):
        if not cid.startswith("-") and cid not in STORE["users"]:
            STORE["users"][cid] = {"first": 0, "last": 0}


def save_store():
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(STORE, fh, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception:
        log.exception("не удалось сохранить %s", DATA_FILE)


def chat_cfg(chat_id):
    c = STORE["chats"].setdefault(str(chat_id), {})
    c.setdefault("min", 0.0)
    c.setdefault("online", False)
    c.setdefault("alert", None)
    c.setdefault("server", DEFAULT_SERVER)
    return c


def track_user(uid):
    now = int(time.time())
    key = str(uid)
    u = STORE["users"].get(key)
    if u is None:
        STORE["users"][key] = {"first": now, "last": now}
        save_store()
        return
    prev = u.get("last", 0)
    u["last"] = now
    if now - prev > 600:   # не пишем на диск при каждом сообщении
        save_store()


# ---------- утилиты ----------

def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip().lower()


def to_number(text: str) -> float:
    cleaned = re.sub(r"[^\d.,]", "", text.replace("\xa0", "").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_price(el):
    text = el.get_text(" ", strip=True).replace("\xa0", " ")
    m = re.search(r"(\d[\d ]*(?:[.,]\d+)?)\s*₽", text)
    if m:
        return to_number(m.group(1)), "₽"
    m = re.search(r"(\d[\d ]*(?:[.,]\d+)?)\s*([$€£¥₴₸])", text)
    if m:
        return to_number(m.group(1)), m.group(2)
    m = re.search(r"\d[\d ]*(?:[.,]\d+)?", text)
    return (to_number(m.group(0)), "?") if m else (0.0, "?")


def fmt_num(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:g}"


def fmt_amt(x: float) -> str:
    """Количество кк: до 2 знаков после запятой, без хвостовых нулей."""
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def is_online(item) -> bool:
    if item.get("data-online") == "1":
        return True
    if item.select_one(".media-user.online"):
        return True
    return False


def server_num(name: str):
    m = re.search(r"№\s*(\d+)", name)
    return int(m.group(1)) if m else None


def short_server(name: str) -> str:
    if GAME_FILTER in norm(name):
        m = re.search(r"№\s*\d+", name)
        if m:
            return m.group(0).replace(" ", "")
    return name if len(name) <= 24 else name[:23] + "…"


# ---------- курс валют ----------

def _rates_from_cbr():
    r = requests.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=(5, 8))
    r.raise_for_status()
    v = r.json()["Valute"]
    return {
        "EUR": v["EUR"]["Value"] / v["EUR"]["Nominal"],
        "USD": v["USD"]["Value"] / v["USD"]["Nominal"],
    }, "ЦБ РФ"


def _rates_from_erapi():
    r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=(5, 8))
    r.raise_for_status()
    rt = r.json()["rates"]
    return {"USD": rt["RUB"], "EUR": rt["RUB"] / rt["EUR"]}, "open.er-api.com"


def get_rates():
    now = time.time()
    if _rates["EUR"] and now - _rates["t"] < RATES_TTL:
        return _rates
    for source in (_rates_from_cbr, _rates_from_erapi):
        try:
            data, name = source()
            _rates.update(t=now, src=name, **data)
            log.info("курс обновлён (%s): EUR=%.2f USD=%.2f",
                     name, data["EUR"], data["USD"])
            return _rates
        except Exception as e:
            log.warning("курс не получен (%s): %s", source.__name__, e)
    return _rates


CUR_CODE = {"€": "EUR", "$": "USD"}


def to_rub(price: float, cur: str):
    if cur == "₽":
        return price, False
    code = CUR_CODE.get(cur)
    rate = get_rates().get(code) if code else None
    if rate:
        return price * rate, True
    return None, False


def show_price(o) -> str:
    if o["rub"] is None:
        return f"{o['price']:.2f} {o['cur']}"
    if o["converted"]:
        return f"≈{o['rub']:.1f} ₽"
    return f"{o['rub']:.2f} ₽"


# ---------- загрузка FunPay ----------

def _request(c):
    return requests.get(
        URL, headers=HEADERS, cookies=c["cookies"], params=c["params"],
        timeout=TIMEOUT,
    )


def _has_rub(html: str) -> bool:
    idx = html.find("tc-price")
    return idx != -1 and "₽" in html[idx: idx + 600]


def get_page():
    global _working, _no_rub_until
    now = time.time()

    if _working is not None:
        try:
            r = _request(_working)
            if _has_rub(r.text):
                return r, _working["name"], True
        except requests.RequestException as e:
            log.warning("рабочий способ упал: %s", e)
        _working = None

    if now < _no_rub_until:
        return _request(CANDIDATES[0]), "конвертация по курсу", False

    last = None
    for c in CANDIDATES:
        r = _request(c)
        last = (r, c["name"])
        if _has_rub(r.text):
            _working = c
            return r, c["name"], True

    _no_rub_until = now + 1800
    return last[0], "конвертация по курсу", False


def load():
    """Скачивает страницу и разбирает лоты ВСЕХ серверов (кэш на CACHE_TTL)."""
    now = time.time()
    if _cache["data"] and now - _cache["t"] < CACHE_TTL:
        return _cache["data"]

    r, method, is_rub = get_page()
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select("a.tc-item")

    offers = []
    servers = {}   # название сервера -> число лотов
    sample = ""
    for item in items:
        server_el = item.select_one(".tc-server")
        server = server_el.get_text(strip=True) if server_el else ""
        if not server:
            continue

        seller_el = item.select_one(".media-user-name")
        amount_el = item.select_one(".tc-amount")
        price_el = item.select_one(".tc-price")
        href = item.get("href")
        if not (seller_el and amount_el and price_el and href):
            continue

        if not sample:
            sample = price_el.get_text(" ", strip=True)

        amount = to_number(amount_el.get_text())
        price, cur = parse_price(price_el)
        if amount <= 0 or price <= 0:
            continue

        rub, converted = to_rub(price, cur)
        link = urljoin("https://funpay.com/", href)
        servers[server] = servers.get(server, 0) + 1
        offers.append({
            "id": link,
            "server": server,
            "skey": norm(server),
            "seller": seller_el.get_text(strip=True),
            "amount": amount,
            "price": price,
            "cur": cur,
            "rub": rub,
            "converted": converted,
            "link": link,
            "online": is_online(item),
        })

    offers.sort(key=lambda o: (
        o["rub"] if o["rub"] is not None else o["price"], -o["amount"]
    ))
    data = {
        "offers": offers,
        "servers": servers,
        "total_items": len(items),
        "status": r.status_code,
        "method": method,
        "is_rub": is_rub,
        "sample": sample,
    }
    _cache["t"] = now
    _cache["data"] = data
    log.info("страница обновлена: лотов %d, серверов %d",
             len(offers), len(servers))
    return data


def fetch_offers():
    return load()["offers"]


def for_server(offers, server):
    key = norm(server)
    return [o for o in offers if o["skey"] == key]


def server_counts():
    d = _cache["data"]
    return d["servers"] if d else {}


def server_sort_key(name):
    grand = GAME_FILTER in norm(name)
    num = server_num(name)
    return (0 if grand else 1, num if num is not None else 10 ** 6, name.lower())


def grand_names(servers):
    return sorted(
        (n for n in servers if GAME_FILTER in norm(n)), key=server_sort_key
    )


def all_names(servers):
    return sorted(servers, key=server_sort_key)


# ---------- фильтры ----------

def apply_filters(offers, f):
    res = offers
    if f["min"] > 0:
        res = [o for o in res if o["amount"] >= f["min"]]
    if f["online"]:
        res = [o for o in res if o["online"]]
    return res


def filters_text(f):
    parts = []
    if f["min"] > 0:
        parts.append(f"от {fmt_num(f['min'])} кк")
    if f["online"]:
        parts.append("только онлайн")
    return "Фильтры: " + ", ".join(parts) if parts else "Фильтры: нет"


def matching_alert(offers, cfg):
    a = cfg["alert"]
    srv = for_server(offers, a.get("server", DEFAULT_SERVER))
    return [
        o for o in apply_filters(srv, cfg)
        if o["rub"] is not None and o["rub"] <= a["price"]
    ]


# ---------- список с кнопками ----------

async def refresh_shown(context, f):
    """Скачивает свежие данные и обновляет список, который видит пользователь."""
    all_offers = await asyncio.to_thread(fetch_offers)
    srv = for_server(all_offers, f["server"])
    offers = apply_filters(srv, f)
    context.chat_data["shown"] = offers[:LIMIT]
    context.chat_data["total"] = len(srv)
    context.chat_data["page"] = 0
    return all_offers


def build_view(context, f):
    """Возвращает (текст, клавиатура) для текущей страницы списка."""
    shown = context.chat_data.get("shown") or []
    total = context.chat_data.get("total", 0)
    pages = max(1, -(-len(shown) // PAGE))
    page = min(max(context.chat_data.get("page", 0), 0), pages - 1)
    context.chat_data["page"] = page

    if not shown:
        text = (f"{f['server']}\n{filters_text(f)}\n\n"
                f"По текущим фильтрам лотов нет (всего на сервере: {total}).")
    else:
        start = page * PAGE
        chunk = shown[start:start + PAGE]
        lines = [f["server"], filters_text(f), f"Страница {page + 1}/{pages}\n",
                 "№. продавец / кол-во / цена за 1кк\n"]
        for i, o in enumerate(chunk, start + 1):
            lines.append(
                f"{i}. {o['seller']} / {fmt_num(o['amount'])} кк / {show_price(o)}"
            )
        lines.append("\nНажми номер слота, чтобы получить ссылку.")
        text = "\n".join(lines)

    rows = []

    # номера слотов текущей страницы (по 5 в ряд)
    if shown:
        start = page * PAGE
        nums = [
            InlineKeyboardButton(str(i), callback_data=f"s:{i}")
            for i in range(start + 1, start + len(shown[start:start + PAGE]) + 1)
        ]
        for k in range(0, len(nums), 5):
            rows.append(nums[k:k + 5])

    # переключение страниц
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"p:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="n"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"p:{page + 1}"))
        rows.append(nav)

    # фильтр по количеству
    rows.append([
        InlineKeyboardButton(
            ("✅ " if f["min"] == v else "") + ("Все" if v == 0 else f"от {v}"),
            callback_data=f"m:{v}",
        )
        for v in MIN_BUTTONS
    ])

    # онлайн и обновление
    rows.append([
        InlineKeyboardButton(
            "🟢 Онлайн: вкл" if f["online"] else "⚪ Онлайн: выкл",
            callback_data="o",
        ),
        InlineKeyboardButton("🔄 Обновить", callback_data="r"),
    ])

    # выбор сервера
    rows.append([
        InlineKeyboardButton(
            f"🌐 Сервер: {short_server(f['server'])}", callback_data="srv"
        )
    ])

    return text, InlineKeyboardMarkup(rows)


# ---------- выбор сервера ----------

def set_picker(context, names, all_flag=False, custom=False, page=0):
    context.chat_data["srv"] = {
        "names": names, "all": all_flag, "custom": custom, "page": page,
    }


def default_picker_names(servers):
    """Серверы Grand Mobile; если таких нет, все серверы."""
    names = grand_names(servers)
    return names, bool(names)


def build_server_picker(context, f):
    st = context.chat_data["srv"]
    names = st["names"]
    counts = server_counts()
    pages = max(1, -(-len(names) // SRV_PAGE))
    page = min(max(st["page"], 0), pages - 1)
    st["page"] = page
    chunk = names[page * SRV_PAGE:(page + 1) * SRV_PAGE]

    if st["custom"]:
        what = "Найдено несколько серверов"
    elif st["all"]:
        what = "Все серверы FunPay"
    else:
        what = "Серверы Grand Mobile"
    text = (f"🌐 {what}\nСейчас: {f['server']}\n"
            f"Всего: {len(names)}, страница {page + 1}/{pages}\n"
            "(в скобках число лотов)")

    rows, row = [], []
    cur_key = norm(f["server"])
    for k, name in enumerate(chunk):
        idx = page * SRV_PAGE + k
        label = (("✅ " if norm(name) == cur_key else "")
                 + f"{short_server(name)} ({counts.get(name, 0)})")
        row.append(InlineKeyboardButton(label, callback_data=f"sv:{idx}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"sp:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="n"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"sp:{page + 1}"))
        rows.append(nav)

    if not st["custom"]:
        rows.append([InlineKeyboardButton(
            "🎮 Только Grand Mobile" if st["all"] else "🌍 Все серверы",
            callback_data="sa",
        )])
    rows.append([InlineKeyboardButton("↩ К списку", callback_data="r")])

    return text, InlineKeyboardMarkup(rows)


async def safe_edit(message, text, markup):
    try:
        await message.edit_text(text, reply_markup=markup,
                                disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def send_long(msg, lines, limit=3800):
    """Длинный текст: первый кусок правит сообщение msg, остальные идут новыми."""
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    if not chunks:
        chunks = ["Пусто"]
    await safe_edit(msg, chunks[0], None)
    for c in chunks[1:]:
        await msg.reply_text(c, disable_web_page_preview=True)


async def resolve_slot(context, f, n):
    """Возвращает (статус, текст). Статусы: ok, gone, range."""
    all_offers = await asyncio.to_thread(fetch_offers)
    srv = for_server(all_offers, f["server"])

    shown = context.chat_data.get("shown")
    if not shown:
        context.chat_data["shown"] = shown = apply_filters(srv, f)[:LIMIT]
        context.chat_data["total"] = len(srv)
        context.chat_data["page"] = 0

    if n < 1 or n > len(shown):
        return "range", (
            f"Слота {n} нет. Доступно: 1–{len(shown)}" if shown
            else "Сначала отправь /virts"
        )

    old = shown[n - 1]
    cur = {o["id"]: o for o in all_offers}.get(old["id"])

    if cur is None:
        fresh = apply_filters(srv, f)
        context.chat_data["shown"] = fresh[:LIMIT]
        context.chat_data["total"] = len(srv)
        context.chat_data["page"] = 0
        return "gone", f"Слот {n} ({old['seller']}) уже исчез с сайта."

    text = (f"Слот {n}: {cur['seller']} / {fmt_num(cur['amount'])} кк / "
            f"{show_price(cur)}\n{cur['link']}")
    if cur["price"] != old["price"] or cur["amount"] != old["amount"]:
        text = (f"Данные слота изменились (было {fmt_num(old['amount'])} кк / "
                f"{show_price(old)})\n") + text
    return "ok", text


# ---------- доступ ----------

def is_admin(user_id) -> bool:
    return user_id in ADMINS


def permitted(user_id) -> bool:
    if not ALLOWED:
        return True
    return user_id in ALLOWED or user_id in ADMINS


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or not permitted(user.id):
        msg = update.effective_message
        # пока админ не настроен, /myid доступен всем (чтобы узнать свой ID)
        if (user is not None and not ADMINS and msg and msg.text
                and msg.text.startswith("/myid")):
            return
        raise ApplicationHandlerStop
    track_user(user.id)


def admin_only(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if ADMINS:
            if user and user.id in ADMINS:
                return await fn(update, context)
            return  # не админам молча не отвечаем
        await update.effective_message.reply_text(
            "Команды админа выключены: задай на хостинге переменную ADMIN_IDS "
            "со своим Telegram ID (узнать его можно командой /myid)."
        )
    return wrapper


# ---------- тексты ----------

START_TEXT = (
    "Привет! Я показываю самые дешёвые вирты на FunPay (GTA 5 RP, Grand Mobile) "
    "в рублях, от дешёвого к дорогому.\n\n"
    "Команды:\n"
    "/virts: список лотов с кнопками\n"
    "/server: выбрать сервер\n"
    "/calc: калькулятор (сколько кк за сумму или сколько стоят N кк)\n"
    "/min 50: только лоты от 50 кк\n"
    "/online: только продавцы онлайн\n"
    "/reset: сбросить фильтры\n"
    "/alert 30: уведомлять, когда появится лот дешевле 30 ₽ за 1кк\n"
    "/help: подробная инструкция"
)

HELP_TEXT = (
    "📖 Как пользоваться ботом\n\n"
    "Бот показывает лоты с вирт на FunPay (GTA 5 RP, Grand Mobile) в рублях, "
    "от дешёвого к дорогому.\n\n"
    "▶ Список лотов\n"
    "/virts открывает список с кнопками:\n"
    "• номер слота: пришлю ссылку на лот;\n"
    "• ◀ ▶: страницы;\n"
    "• «от 10 / 50 / 100»: скрыть мелкие лоты;\n"
    "• «Онлайн»: только продавцы онлайн;\n"
    "• 🔄: обновить список;\n"
    "• 🌐 Сервер: сменить сервер.\n"
    "Ссылку на слот можно получить и текстом: «4 слот» или «слот 4».\n\n"
    "🌐 Сервер\n"
    "/server: выбрать сервер кнопками\n"
    "/server 5: сразу сервер №05\n"
    "/server текст: поиск по названию\n\n"
    "🔎 Фильтры (сохраняются)\n"
    "/min 50: только лоты от 50 кк (/min 0 снимает)\n"
    "/online: включить/выключить «только онлайн»\n"
    "/reset: сбросить оба фильтра\n\n"
    "🧮 Калькулятор\n"
    "/calc 500: сколько кк можно купить на 500 ₽\n"
    "/calc 20кк: сколько стоят 20 кк\n"
    "Считает по лотам выбранного сервера с учётом фильтров. Каждый продавец "
    "это отдельный заказ.\n\n"
    "🔔 Уведомления\n"
    "/alert 30: напишу, когда на выбранном сервере появится новый лот "
    "дешевле 30 ₽ за 1кк (проверка раз в пару минут)\n"
    "/alert: посмотреть, включены ли уведомления\n"
    "/alert off: выключить\n"
    "Уведомления следят за тем сервером, который был выбран при /alert.\n\n"
    "ℹ️ Цены с пометкой ≈ пересчитаны в рубли по курсу, на сайте они могут "
    "отличаться на 1–2 ₽."
)

ADMIN_TEXT = (
    "🛠 Команды админа\n\n"
    "/stats: сколько людей пользуется ботом\n"
    "/stats_server: самый дешёвый слот на каждом сервере Grand Mobile "
    "(учитывает твои /min и /online)\n"
    "/debug: состояние бота: курс, лоты, доступ\n"
    "/ping: проверка, что бот жив\n"
    "/myid: твой Telegram ID"
)

CALC_HELP = (
    "🧮 Калькулятор\n\n"
    "/calc 500: сколько кк можно купить на 500 ₽\n"
    "/calc 20кк: сколько стоят 20 кк\n\n"
    "Считает по лотам выбранного сервера (учитывает /min и /online), "
    "начиная с самых дешёвых."
)


# ---------- команды ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = HELP_TEXT
    user = update.effective_user
    if user and is_admin(user.id):
        text += "\n\n🛠 Команды админа: /admin"
    await update.message.reply_text(text)


@admin_only
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(ADMIN_TEXT)


@admin_only
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong (версия 10)")


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMINS and user.id not in ADMINS:
        return
    text = f"Твой Telegram ID: {user.id}"
    if not ADMINS:
        text += ("\n\nЧтобы стать админом, добавь этот ID в переменную "
                 "ADMIN_IDS на хостинге и перезапусти бота.")
    await update.message.reply_text(text)


async def show_list_new(update: Update, context: ContextTypes.DEFAULT_TYPE, f):
    msg = await update.message.reply_text("⏳ Загружаю...")
    await refresh_shown(context, f)
    text, markup = build_view(context, f)
    await safe_edit(msg, text, markup)


async def virts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = chat_cfg(update.effective_chat.id)
    await show_list_new(update, context, f)


async def server_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = chat_cfg(update.effective_chat.id)
    msg = await update.message.reply_text("⏳ Загружаю серверы...")
    d = await asyncio.to_thread(load)
    servers = d["servers"]
    query = " ".join(context.args).strip()

    if not query:
        names, is_grand = default_picker_names(servers)
        if not is_grand:
            names = all_names(servers)
        set_picker(context, names, all_flag=not is_grand)
        text, markup = build_server_picker(context, f)
        await safe_edit(msg, text, markup)
        return

    if query.isdigit():
        n = int(query)
        matches = [x for x in grand_names(servers) if server_num(x) == n]
    else:
        q = norm(query)
        matches = [x for x in all_names(servers) if q in norm(x)]

    if not matches:
        await safe_edit(msg, f"Сервер «{query}» не найден. Список: /server", None)
        return

    if len(matches) == 1:
        f["server"] = matches[0]
        save_store()
        await refresh_shown(context, f)
        text, markup = build_view(context, f)
        await safe_edit(msg, text, markup)
        return

    set_picker(context, matches, custom=True)
    text, markup = build_server_picker(context, f)
    await safe_edit(msg, text, markup)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    f = chat_cfg(update.effective_chat.id)

    try:
        if data == "n":
            await q.answer()
            return

        # ----- выбор сервера -----
        if data == "srv":
            await q.answer()
            d = await asyncio.to_thread(load)
            names, is_grand = default_picker_names(d["servers"])
            if not is_grand:
                names = all_names(d["servers"])
            set_picker(context, names, all_flag=not is_grand)
            t, markup = build_server_picker(context, f)
            await safe_edit(q.message, t, markup)
            return

        if data == "sa":
            st = context.chat_data.get("srv")
            if not st:
                await q.answer("Список устарел, отправь /server заново.",
                               show_alert=True)
                return
            await q.answer()
            d = await asyncio.to_thread(load)
            if st["all"]:
                names, is_grand = default_picker_names(d["servers"])
                if not is_grand:
                    names = all_names(d["servers"])
                set_picker(context, names, all_flag=not is_grand)
            else:
                set_picker(context, all_names(d["servers"]), all_flag=True)
            t, markup = build_server_picker(context, f)
            await safe_edit(q.message, t, markup)
            return

        if data.startswith("sp:"):
            st = context.chat_data.get("srv")
            if not st:
                await q.answer("Список устарел, отправь /server заново.",
                               show_alert=True)
                return
            st["page"] = int(data[3:])
            await q.answer()
            t, markup = build_server_picker(context, f)
            await safe_edit(q.message, t, markup)
            return

        if data.startswith("sv:"):
            st = context.chat_data.get("srv")
            idx = int(data[3:])
            if not st or idx >= len(st["names"]):
                await q.answer("Список устарел, отправь /server заново.",
                               show_alert=True)
                return
            f["server"] = st["names"][idx]
            save_store()
            await q.answer(f"Сервер: {short_server(f['server'])}")
            await refresh_shown(context, f)
            t, markup = build_view(context, f)
            await safe_edit(q.message, t, markup)
            return

        # ----- ссылка на слот -----
        if data.startswith("s:"):
            n = int(data[2:])
            if not context.chat_data.get("shown"):
                await q.answer("Список устарел, отправь /virts заново.",
                               show_alert=True)
                return
            status, text = await resolve_slot(context, f, n)
            if status == "ok":
                await q.answer()
                await q.message.reply_text(text, disable_web_page_preview=True)
            elif status == "gone":
                await q.answer(text + " Список обновлён.", show_alert=True)
                t, markup = build_view(context, f)
                await safe_edit(q.message, t, markup)
            else:
                await q.answer(text, show_alert=True)
            return

        # ----- переключение страницы (без перезагрузки данных) -----
        if data.startswith("p:"):
            if not context.chat_data.get("shown"):
                await q.answer("Список устарел, отправь /virts заново.",
                               show_alert=True)
                return
            context.chat_data["page"] = int(data[2:])
            await q.answer()
            t, markup = build_view(context, f)
            await safe_edit(q.message, t, markup)
            return

        # ----- фильтры и обновление: перезагружаем данные -----
        if data.startswith("m:"):
            f["min"] = float(data[2:])
            save_store()
        elif data == "o":
            f["online"] = not f["online"]
            save_store()
        elif data != "r":
            await q.answer()
            return

        await q.answer()
        await refresh_shown(context, f)
        t, markup = build_view(context, f)
        await safe_edit(q.message, t, markup)

    except Exception:
        log.exception("ошибка в обработке кнопки %s", data)
        try:
            await q.answer("Ошибка, попробуй ещё раз.", show_alert=True)
        except Exception:
            pass


async def set_min(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = chat_cfg(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text(
            "Напиши число, например: /min 50 (лоты от 50 кк). /min 0 снимает фильтр."
        )
        return
    f["min"] = max(to_number(context.args[0]), 0.0)
    save_store()
    await update.message.reply_text(
        f"Готово. {filters_text(f)}\nОбнови список: /virts"
    )


async def toggle_online(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = chat_cfg(update.effective_chat.id)
    f["online"] = not f["online"]
    save_store()
    await update.message.reply_text(
        f"Только онлайн: {'включено' if f['online'] else 'выключено'}.\n"
        "Обнови список: /virts"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = chat_cfg(update.effective_chat.id)
    f["min"] = 0.0
    f["online"] = False
    save_store()
    await update.message.reply_text("Фильтры сброшены. Обнови список: /virts")


# ---------- калькулятор ----------

def parse_calc(text: str):
    """Возвращает ('amount', кк) или ('money', рубли) либо None."""
    t = text.lower().replace(",", ".").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:кк|kk)", t)
    if m:
        return "amount", float(m.group(1))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:₽|руб\.?|р\.?|rub)?", t)
    if m:
        return "money", float(m.group(1))
    return None


def plan_amount(lots, need):
    taken, remaining = [], need
    for o in lots:
        if remaining <= 1e-9:
            break
        take = min(remaining, o["amount"])
        taken.append((o, take, take * o["rub"]))
        remaining -= take
    return taken


def plan_money(lots, budget):
    taken, left = [], budget
    for o in lots:
        if left <= 0.005:
            break
        take = min(left / o["rub"], o["amount"])
        cost = take * o["rub"]
        taken.append((o, take, cost))
        left -= cost
    return taken


async def calc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = chat_cfg(update.effective_chat.id)
    parsed = parse_calc(" ".join(context.args)) if context.args else None
    if not parsed or parsed[1] <= 0:
        await update.message.reply_text(CALC_HELP)
        return
    mode, value = parsed

    msg = await update.message.reply_text("⏳ Считаю...")
    all_offers = await asyncio.to_thread(fetch_offers)
    lots = [
        o for o in apply_filters(for_server(all_offers, f["server"]), f)
        if o["rub"] is not None
    ]
    lots.sort(key=lambda o: (o["rub"], -o["amount"]))
    if not lots:
        await safe_edit(
            msg,
            f"{f['server']}\n{filters_text(f)}\n\nПодходящих лотов нет. "
            "Сбросить фильтры: /reset", None,
        )
        return

    if mode == "amount":
        taken = plan_amount(lots, value)
        title = f"Купить {fmt_amt(value)} кк:"
    else:
        taken = plan_money(lots, value)
        title = f"На {value:.2f} ₽ можно купить:"

    got = sum(t[1] for t in taken)
    cost = sum(t[2] for t in taken)
    approx = "≈" if any(t[0]["converted"] for t in taken) else ""

    lines = [f"🧮 {f['server']}", filters_text(f), "", title]
    for i, (o, take, c) in enumerate(taken[:CALC_MAX_LOTS], 1):
        tilde = "≈" if o["converted"] else ""
        lines.append(
            f"{i}. {o['seller']}: {fmt_amt(take)} кк × {tilde}{o['rub']:.2f} ₽ "
            f"= {tilde}{c:.2f} ₽\n{o['link']}"
        )
    if len(taken) > CALC_MAX_LOTS:
        lines.append(f"…и ещё {len(taken) - CALC_MAX_LOTS} лотов")

    if got > 0:
        lines.append(
            f"\nИтого: {fmt_amt(got)} кк за {approx}{cost:.2f} ₽ "
            f"(в среднем {approx}{cost / got:.2f} ₽ за 1кк)"
        )

    if mode == "amount":
        short = value - got
        if short > 1e-6:
            lines.append(
                f"⚠️ Не хватает {fmt_amt(short)} кк: на сервере (с учётом "
                f"фильтров) доступно только {fmt_amt(got)} кк."
            )
        elif len(taken) > 1:
            single = next((o for o in lots if o["amount"] >= value), None)
            if single:
                tilde = "≈" if single["converted"] else ""
                lines.append(
                    f"\nОдним заказом у одного продавца: {single['seller']}, "
                    f"{tilde}{value * single['rub']:.2f} ₽ "
                    f"({show_price(single)} за 1кк)\n{single['link']}"
                )
    else:
        left = value - cost
        if left > 0.01:
            lines.append(
                f"⚠️ Лотов не хватило: останется {left:.2f} ₽ "
                "(предложение на сервере закончилось)."
            )

    lines.append("\nРасчёт по текущим лотам, каждый продавец это отдельный заказ.")
    await send_long(msg, lines)


# ---------- уведомления ----------

async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = chat_cfg(update.effective_chat.id)

    if not context.args:
        a = cfg["alert"]
        if a:
            await update.message.reply_text(
                f"🔔 Уведомления включены: {a.get('server', DEFAULT_SERVER)}, "
                f"лоты дешевле {fmt_num(a['price'])} ₽ за 1кк.\n"
                f"{filters_text(cfg)}\nВыключить: /alert off"
            )
        else:
            await update.message.reply_text(
                "Уведомления выключены.\nВключить: /alert 30 "
                "(писать, когда на выбранном сервере появится лот "
                "дешевле 30 ₽ за 1кк)"
            )
        return

    arg = context.args[0].lower()
    if arg in ("off", "выкл", "0"):
        cfg["alert"] = None
        save_store()
        await update.message.reply_text("🔕 Уведомления выключены.")
        return

    price = to_number(arg)
    if price <= 0:
        await update.message.reply_text("Напиши цену числом, например: /alert 30")
        return

    cfg["alert"] = {"price": price, "notified": [], "server": cfg["server"]}
    offers = await asyncio.to_thread(fetch_offers)
    now_match = matching_alert(offers, cfg)
    cfg["alert"]["notified"] = sorted(o["id"] for o in now_match)
    save_store()

    text = (
        f"🔔 Готово! Сервер: {cfg['server']}.\n"
        f"Напишу, когда появится новый лот дешевле {fmt_num(price)} ₽ за 1кк.\n"
        f"{filters_text(cfg)}\nПроверка раз в {CHECK_INTERVAL // 60} мин."
    )
    if now_match:
        text += (f"\n\nСейчас таких лотов уже {len(now_match)} "
                 "(они есть в /virts, о них повторно не напишу).")
    await update.message.reply_text(text)


def parse_slot(text: str):
    t = text.lower().strip()
    if "слот" not in t or len(t) > 20:
        return None
    m = re.search(r"\d+", t)
    return int(m.group()) if m else None


async def slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = parse_slot(update.message.text or "")
    if n is None:
        return
    f = chat_cfg(update.effective_chat.id)
    status, text = await resolve_slot(context, f, n)
    await update.message.reply_text(text, disable_web_page_preview=True)
    if status == "gone":
        t, markup = build_view(context, f)
        await update.message.reply_text(t, reply_markup=markup,
                                        disable_web_page_preview=True)


# ---------- команды админа ----------

@admin_only
async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = chat_cfg(update.effective_chat.id)
    await update.message.reply_text("⏳ Проверяю...")
    d = await asyncio.to_thread(load)
    rates = get_rates()
    cur_offers = for_server(d["offers"], f["server"])
    online = sum(1 for o in cur_offers if o["online"])
    alerts = sum(1 for c in STORE["chats"].values() if c.get("alert"))
    rate_line = (
        f"EUR {rates['EUR']:.2f} ₽, USD {rates['USD']:.2f} ₽ ({rates['src']})"
        if rates["EUR"] else "курс не получен"
    )
    access = f"ограничен ({len(ALLOWED)} ID)" if ALLOWED else "открыт всем"
    text = (
        f"HTTP {d['status']}, лотов на странице: {d['total_items']}\n"
        f"Способ: {d['method']}\n"
        f"Рубли с сайта: {'да' if d['is_rub'] else 'нет (пересчёт по курсу)'}\n"
        f"Пример текста цены: {d['sample']}\n"
        f"Курс: {rate_line}\n"
        f"Серверов на сайте: {len(d['servers'])}, "
        f"из них Grand Mobile: {len(grand_names(d['servers']))}\n"
        f"Текущий сервер: {f['server']}\n"
        f"Лотов сервера: {len(cur_offers)}, онлайн: {online}\n"
        f"Доступ: {access}, админов: {len(ADMINS)}\n"
        f"Активных уведомлений: {alerts}"
    )
    await update.message.reply_text(text)


@admin_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    users = STORE["users"].values()
    total = len(STORE["users"])
    day = sum(1 for u in users if u.get("last", 0) >= now - 86400)
    week = sum(1 for u in users if u.get("last", 0) >= now - 7 * 86400)
    new_day = sum(1 for u in users if u.get("first", 0) >= now - 86400)
    alerts = sum(1 for c in STORE["chats"].values() if c.get("alert"))
    await update.message.reply_text(
        "📊 Статистика бота\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"🟢 Активны за 24 часа: {day}\n"
        f"📅 Активны за 7 дней: {week}\n"
        f"🆕 Новых за 24 часа: {new_day}\n"
        f"🔔 С включёнными уведомлениями: {alerts}\n\n"
        "Активность считается с момента обновления бота."
    )


@admin_only
async def stats_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = chat_cfg(update.effective_chat.id)
    msg = await update.message.reply_text("⏳ Считаю...")
    d = await asyncio.to_thread(load)
    names = grand_names(d["servers"])
    if not names:
        await safe_edit(msg, "Серверов Grand Mobile не найдено. Проверь /debug.",
                        None)
        return

    by_server = {}
    for o in d["offers"]:
        by_server.setdefault(o["skey"], []).append(o)

    rows, empty = [], []
    for name in names:
        lots = [
            o for o in apply_filters(by_server.get(norm(name), []), f)
            if o["rub"] is not None
        ]
        if lots:
            rows.append((name, min(lots, key=lambda o: (o["rub"], -o["amount"]))))
        else:
            empty.append(name)
    rows.sort(key=lambda r: r[1]["rub"])

    lines = ["🏷 Самые дешёвые слоты, Grand Mobile",
             filters_text(f), "(от дешёвого к дорогому)\n"]
    for i, (name, o) in enumerate(rows):
        mark = "🏆 " if i == 0 else ""
        lines.append(
            f"{mark}{short_server(name)}: {show_price(o)} / "
            f"{fmt_num(o['amount'])} кк / {o['seller']}\n{o['link']}\n"
        )
    if empty:
        lines.append("Нет подходящих лотов: "
                     + ", ".join(short_server(n) for n in empty))
    await send_long(msg, lines)


# ---------- фоновая проверка уведомлений ----------

async def alert_loop(app):
    await asyncio.sleep(15)
    while True:
        try:
            chats = {cid: c for cid, c in STORE["chats"].items() if c.get("alert")}
            if chats:
                offers = await asyncio.to_thread(fetch_offers)
                changed = False
                for cid, cfg in chats.items():
                    a = cfg["alert"]
                    match = matching_alert(offers, cfg)
                    ids = {o["id"] for o in match}
                    old = set(a.get("notified", []))
                    new = [o for o in match if o["id"] not in old]

                    if new:
                        lines = [
                            f"🔔 {a.get('server', DEFAULT_SERVER)}: лоты дешевле "
                            f"{fmt_num(a['price'])} ₽ за 1кк:\n"
                        ]
                        for o in new[:ALERT_MAX_LOTS]:
                            lines.append(
                                f"{o['seller']} / {fmt_num(o['amount'])} кк / "
                                f"{show_price(o)}\n{o['link']}\n"
                            )
                        if len(new) > ALERT_MAX_LOTS:
                            lines.append(f"…и ещё {len(new) - ALERT_MAX_LOTS}. См. /virts")
                        try:
                            await app.bot.send_message(
                                int(cid), "\n".join(lines),
                                disable_web_page_preview=True,
                            )
                        except Exception:
                            log.exception("не удалось отправить уведомление в %s", cid)
                            continue

                    if ids != old:
                        a["notified"] = sorted(ids)
                        changed = True
                if changed:
                    save_store()
        except Exception:
            log.exception("ошибка в проверке уведомлений")
        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app):
    app.create_task(alert_loop(app))


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка в обработчике", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"Ошибка бота: {type(context.error).__name__}: {context.error}"
            )
        except Exception:
            pass


load_store()
if not ALLOWED:
    log.warning("ALLOWED_IDS не задан: бот отвечает всем!")
if not ADMINS:
    log.warning("ADMIN_IDS не задан: команды админа выключены.")

app = (
    ApplicationBuilder()
    .token(TOKEN)
    .concurrent_updates(True)
    .post_init(post_init)
    .build()
)
app.add_handler(TypeHandler(Update, gate), group=-1)
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_cmd))
app.add_handler(CommandHandler("virts", virts))
app.add_handler(CommandHandler("server", server_cmd))
app.add_handler(CommandHandler("calc", calc_cmd))
app.add_handler(CommandHandler("min", set_min))
app.add_handler(CommandHandler("online", toggle_online))
app.add_handler(CommandHandler("reset", reset))
app.add_handler(CommandHandler("alert", alert_cmd))
app.add_handler(CommandHandler("admin", admin_cmd))
app.add_handler(CommandHandler("ping", ping))
app.add_handler(CommandHandler("myid", myid))
app.add_handler(CommandHandler("debug", debug))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CommandHandler("stats_server", stats_server))
app.add_handler(CallbackQueryHandler(on_button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, slot))
app.add_error_handler(on_error)
app.run_polling()
