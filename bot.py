import os
import re
import time
import asyncio
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("virts-bot")

TOKEN = os.environ.get("BOT_TOKEN", "ТВОЙ_ТОКЕН")
URL = "https://funpay.com/chips/51/"
SERVER_NAME = "Grnd.gg Grand Mobile №01"
LIMIT = 30
CACHE_TTL = 10  # секунд: не дёргаем сайт чаще

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
TIMEOUT = (5, 12)  # (подключение, чтение)

CANDIDATES = [
    {"name": "cookie cy=rub", "cookies": {"cy": "rub"}, "params": {}},
    {"name": "cookie cy=RUB", "cookies": {"cy": "RUB"}, "params": {}},
    {"name": "cookie cy=1", "cookies": {"cy": "1"}, "params": {}},
    {"name": "cookie cy=2", "cookies": {"cy": "2"}, "params": {}},
    {"name": "param ?cy=rub", "cookies": {}, "params": {"cy": "rub"}},
    {"name": "без параметров", "cookies": {}, "params": {}},
]
_working = None
_cache = {"t": 0.0, "data": None}


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


def _request(c):
    return requests.get(
        URL, headers=HEADERS, cookies=c["cookies"], params=c["params"],
        timeout=TIMEOUT,
    )


def _has_rub(html: str) -> bool:
    """Быстрая проверка без полного парсинга страницы."""
    idx = html.find("tc-price")
    return idx != -1 and "₽" in html[idx: idx + 600]


def get_page():
    global _working
    if _working is not None:
        try:
            r = _request(_working)
            if _has_rub(r.text):
                return r, _working["name"], True
        except requests.RequestException as e:
            log.warning("рабочий способ упал: %s", e)
        _working = None

    last = None
    for c in CANDIDATES:
        r = _request(c)
        last = (r, c["name"])
        if _has_rub(r.text):
            _working = c
            return r, c["name"], True
    return last[0], last[1], False


def load():
    """Скачивает и разбирает страницу один раз, результат кэшируется."""
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

        link = urljoin("https://funpay.com/", href)
        offers.append({
            "id": link,
            "seller": seller_el.get_text(strip=True),
            "amount": amount,
            "price": price,
            "cur": cur,
            "link": link,
            "online": is_online(item),
        })

    offers.sort(key=lambda o: (o["price"], -o["amount"]))
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


def get_filters(context):
    return context.chat_data.setdefault("filters", {"min": 0.0, "online": False})


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
            f"{i}. {o['seller']} / {fmt_num(o['amount'])} кк / "
            f"{o['price']:.2f} {o['cur']}"
        )
    if offers and offers[0]["cur"] != "₽":
        lines.append(
            f"\n⚠️ Сайт отдал цены в {offers[0]['cur']}, а не в рублях. "
            "Выключи VPN на время запуска бота или запусти его на сервере "
            "с российским IP."
        )
    lines.append("\nЧтобы получить ссылку, напиши: 4 слот")
    return "\n".join(lines)


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong (версия 5)")


async def virts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = get_filters(context)
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
    f = get_filters(context)
    if not context.args:
        await update.message.reply_text(
            "Напиши число, например: /min 50 (лоты от 50 кк). /min 0 снимает фильтр."
        )
        return
    f["min"] = max(to_number(context.args[0]), 0.0)
    await update.message.reply_text(
        f"Готово. {filters_text(f)}\nОбнови список: /virts"
    )


async def toggle_online(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = get_filters(context)
    f["online"] = not f["online"]
    await update.message.reply_text(
        f"Только онлайн: {'включено' if f['online'] else 'выключено'}.\n"
        "Обнови список: /virts"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["filters"] = {"min": 0.0, "online": False}
    await update.message.reply_text("Фильтры сброшены. Обнови список: /virts")


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
    f = get_filters(context)
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
            f"{cur['price']:.2f} {cur['cur']}\n{cur['link']}")
    if cur["price"] != old["price"] or cur["amount"] != old["amount"]:
        text = (f"Данные слота изменились (было {fmt_num(old['amount'])} кк / "
                f"{old['price']:.2f} {old['cur']})\n") + text
    await update.message.reply_text(text, disable_web_page_preview=True)


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Проверяю...")
    d = await asyncio.to_thread(load)
    online = sum(1 for o in d["offers"] if o["online"])
    text = (
        f"HTTP {d['status']}, лотов на странице: {d['total_items']}\n"
        f"Способ: {d['method']}\n"
        f"Рубли получены: {'да' if d['is_rub'] else 'НЕТ'}\n"
        f"Пример текста цены: {d['sample']}\n"
        f"Лотов сервера: {len(d['offers'])}, онлайн: {online}"
    )
    await update.message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/virts: список слотов от дешёвого к дорогому\n"
        "«4 слот» или «слот 4»: ссылка на слот\n"
        "/min 50: только лоты от 50 кк (/min 0 снимает)\n"
        "/online: вкл/выкл только продавцы онлайн\n"
        "/reset: сбросить фильтры\n"
        "/ping: проверка, что бот жив"
    )


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка в обработчике", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"Ошибка бота: {type(context.error).__name__}: {context.error}"
            )
        except Exception:
            pass


app = (
    ApplicationBuilder()
    .token(TOKEN)
    .concurrent_updates(True)
    .build()
)
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("ping", ping))
app.add_handler(CommandHandler("virts", virts))
app.add_handler(CommandHandler("min", set_min))
app.add_handler(CommandHandler("online", toggle_online))
app.add_handler(CommandHandler("reset", reset))
app.add_handler(CommandHandler("debug", debug))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, slot))
app.add_error_handler(on_error)
app.run_polling()
