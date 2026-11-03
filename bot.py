import os
import re
import json
import time
import asyncio
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ApplicationHandlerStop, CommandHandler,
    ContextTypes, MessageHandler, TypeHandler, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("virts-bot")

TOKEN = os.environ.get("BOT_TOKEN", "ТВОЙ_ТОКЕН")
# Telegram ID через запятую, например "123456789" или "123456789,987654321".
# Если пусто: бот отвечает всем (не рекомендуется).
ALLOWED = {
    int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",")
    if x.isdigit()
}
DATA_FILE = os.environ.get("DATA_FILE", "data.json")

URL = "https://funpay.com/chips/51/"
SERVER_NAME = "Grnd.gg Grand Mobile №01"
LIMIT = 30
CACHE_TTL = 10         # секунд: не дёргаем FunPay чаще
RATES_TTL = 3600       # секунд: курс валют обновляем раз в час
CHECK_INTERVAL = 120   # секунд: как часто проверять уведомления
ALERT_MAX_LOTS = 5     # сколько лотов показывать в одном уведомлении

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


# ---------- хранилище (фильтры и уведомления) ----------

STORE = {"chats": {}}


def load_store():
    global STORE
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fh:
            STORE = json.load(fh)
        STORE.setdefault("chats", {})
        log.info("данные загружены из %s", DATA_FILE)
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("не удалось прочитать %s, начинаю с чистого листа", DATA_FILE)


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
    return c


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


def is_online(item) -> bool:
    if item.get("data-online") == "1":
        return True
    if item.select_one(".media-user.online"):
        return True
    return False


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
    now = time.time()
    if _cache["data"] and now - _cache["t"] < CACHE_TTL:
        return _cache["data"]

    r, method, is_rub = get_page()
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select("a.tc-item")

    offers = []
    sample = ""
    for item in items:
        server_el = item.select_one(".tc-server")
        server = server_el.get_text(strip=True) if server_el else ""
        if norm(server) != norm(SERVER_NAME):
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
        offers.append({
            "id": link,
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
        "total_items": len(items),
        "status": r.status_code,
        "method": method,
        "is_rub": is_rub,
        "sample": sample,
    }
    _cache["t"] = now
    _cache["data"] = data
    log.info("страница обновлена: лотов сервера %d", len(offers))
    return data


def fetch_offers():
    return load()["offers"]


# ---------- фильтры и вывод ----------

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


def format_list(offers, f):
    lines = [SERVER_NAME, filters_text(f),
             "№. продавец / кол-во / цена за 1кк\n"]
    for i, o in enumerate(offers[:LIMIT], 1):
        lines.append(
            f"{i}. {o['seller']} / {fmt_num(o['amount'])} кк / {show_price(o)}"
        )
    lines.append("\nЧтобы получить ссылку, напиши: 4 слот")
    return "\n".join(lines)


def matching_alert(offers, cfg):
    """Лоты, подходящие под порог уведомления и текущие фильтры."""
    a = cfg["alert"]
    return [
        o for o in apply_filters(offers, cfg)
        if o["rub"] is not None and o["rub"] <= a["price"]
    ]


# ---------- доступ ----------

async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED:
        return
    user = update.effective_user
    if user and user.id in ALLOWED:
        return
    msg = update.effective_message
    if msg and msg.text and msg.text.startswith("/myid"):
        return
    raise ApplicationHandlerStop


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


# ---------- команды ----------

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong (версия 7)")


async def virts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = chat_cfg(update.effective_chat.id)
    await update.message.reply_text("⏳ Загружаю...")
    all_offers = await asyncio.to_thread(fetch_offers)

    offers = apply_filters(all_offers, f)
    if not offers:
        msg = "Лоты не найдены. Попробуй /debug"
        if all_offers:
            msg = ("По текущим фильтрам лотов нет "
                   f"(всего на сервере: {len(all_offers)}).\n"
                   f"{filters_text(f)}\nСбросить: /reset")
        await update.message.reply_text(msg)
        return

    context.chat_data["shown"] = offers[:LIMIT]
    await update.message.reply_text(format_list(offers, f))


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


async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = chat_cfg(update.effective_chat.id)

    if not context.args:
        a = cfg["alert"]
        if a:
            await update.message.reply_text(
                f"🔔 Уведомления включены: лоты дешевле {fmt_num(a['price'])} ₽ "
                f"за 1кк.\n{filters_text(cfg)}\nВыключить: /alert off"
            )
        else:
            await update.message.reply_text(
                "Уведомления выключены.\nВключить: /alert 30 "
                "(писать, когда появится лот дешевле 30 ₽ за 1кк)"
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

    cfg["alert"] = {"price": price, "notified": []}
    offers = await asyncio.to_thread(fetch_offers)
    now_match = matching_alert(offers, cfg)
    cfg["alert"]["notified"] = sorted(o["id"] for o in now_match)
    save_store()

    text = (
        f"🔔 Готово! Напишу, когда появится новый лот дешевле "
        f"{fmt_num(price)} ₽ за 1кк.\n{filters_text(cfg)}\n"
        f"Проверка раз в {CHECK_INTERVAL // 60} мин."
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
    all_offers = await asyncio.to_thread(fetch_offers)

    shown = context.chat_data.get("shown")
    if not shown:
        context.chat_data["shown"] = shown = apply_filters(all_offers, f)[:LIMIT]

    if n < 1 or n > len(shown):
        await update.message.reply_text(
            f"Слота {n} нет. Доступно: 1–{len(shown)}" if shown
            else "Сначала отправь /virts"
        )
        return

    old = shown[n - 1]
    cur = {o["id"]: o for o in all_offers}.get(old["id"])

    if cur is None:
        fresh = apply_filters(all_offers, f)
        context.chat_data["shown"] = fresh[:LIMIT]
        await update.message.reply_text(
            f"Слот {n} ({old['seller']}) уже исчез с сайта. Список обновлён:\n\n"
            + format_list(fresh, f)
        )
        return

    text = (f"Слот {n}: {cur['seller']} / {fmt_num(cur['amount'])} кк / "
            f"{show_price(cur)}\n{cur['link']}")
    if cur["price"] != old["price"] or cur["amount"] != old["amount"]:
        text = (f"Данные слота изменились (было {fmt_num(old['amount'])} кк / "
                f"{show_price(old)})\n") + text
    await update.message.reply_text(text, disable_web_page_preview=True)


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Проверяю...")
    d = await asyncio.to_thread(load)
    rates = get_rates()
    online = sum(1 for o in d["offers"] if o["online"])
    alerts = sum(1 for c in STORE["chats"].values() if c.get("alert"))
    rate_line = (
        f"EUR {rates['EUR']:.2f} ₽, USD {rates['USD']:.2f} ₽ ({rates['src']})"
        if rates["EUR"] else "курс не получен"
    )
    text = (
        f"HTTP {d['status']}, лотов на странице: {d['total_items']}\n"
        f"Способ: {d['method']}\n"
        f"Рубли с сайта: {'да' if d['is_rub'] else 'нет (пересчёт по курсу)'}\n"
        f"Пример текста цены: {d['sample']}\n"
        f"Курс: {rate_line}\n"
        f"Лотов сервера: {len(d['offers'])}, онлайн: {online}\n"
        f"Доступ: {'ограничен (' + str(len(ALLOWED)) + ' ID)' if ALLOWED else 'открыт всем'}\n"
        f"Активных уведомлений: {alerts}"
    )
    await update.message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/virts: список слотов от дешёвого к дорогому\n"
        "«4 слот» или «слот 4»: ссылка на слот\n"
        "/min 50: только лоты от 50 кк (/min 0 снимает)\n"
        "/online: вкл/выкл только продавцы онлайн\n"
        "/reset: сбросить фильтры\n"
        "/alert 30: написать, когда появится лот дешевле 30 ₽ за 1кк\n"
        "/alert off: выключить уведомления\n"
        "/myid: узнать свой Telegram ID\n"
        "/ping: проверка, что бот жив"
    )


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
                        lines = [f"🔔 Лоты дешевле {fmt_num(a['price'])} ₽ за 1кк:\n"]
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
                            continue  # попробуем в следующий раз

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

app = (
    ApplicationBuilder()
    .token(TOKEN)
    .concurrent_updates(True)
    .post_init(post_init)
    .build()
)
app.add_handler(TypeHandler(Update, gate), group=-1)
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("ping", ping))
app.add_handler(CommandHandler("myid", myid))
app.add_handler(CommandHandler("virts", virts))
app.add_handler(CommandHandler("min", set_min))
app.add_handler(CommandHandler("online", toggle_online))
app.add_handler(CommandHandler("reset", reset))
app.add_handler(CommandHandler("alert", alert_cmd))
app.add_handler(CommandHandler("debug", debug))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, slot))
app.add_error_handler(on_error)
app.run_polling()
