import os
import re
import asyncio
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)

TOKEN = os.environ.get("BOT_TOKEN", "ТВОЙ_ТОКЕН")
URL = "https://funpay.com/chips/51/"
SERVER_NAME = "Grnd.gg Grand Mobile №01"   # точное название сервера
LIMIT = 30                                 # сколько слотов показывать

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip().lower()


def to_number(text: str) -> float:
    """'1 000' -> 1000.0, '24.44 ₽' -> 24.44, '20 кк' -> 20.0"""
    cleaned = re.sub(r"[^\d.,]", "", text.replace("\xa0", "").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def fmt_num(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:g}"


def fetch_offers():
    r = requests.get(URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    offers = []
    for item in soup.select("a.tc-item"):
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

        amount = to_number(amount_el.get_text())   # в кк
        price = to_number(price_el.get_text())     # цена за 1кк
        if amount <= 0 or price <= 0:
            continue

        link = urljoin("https://funpay.com/", href)
        offers.append({
            "id": link,
            "seller": seller_el.get_text(strip=True),
            "amount": amount,
            "price": price,
            "link": link,
        })

    # от дешёвого к дорогому
    offers.sort(key=lambda o: (o["price"], -o["amount"]))
    return offers


def format_list(offers):
    lines = [SERVER_NAME, "№. продавец / кол-во / цена за 1кк\n"]
    for i, o in enumerate(offers[:LIMIT], 1):
        lines.append(f"{i}. {o['seller']} / {fmt_num(o['amount'])} кк / {o['price']:.2f} ₽")
    lines.append("\nЧтобы получить ссылку, напиши: 4 слот (или слот 4)")
    return "\n".join(lines)


async def virts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        offers = await asyncio.to_thread(fetch_offers)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return

    if not offers:
        await update.message.reply_text("Лоты не найдены. Попробуй /debug")
        return

    # запоминаем, что показали, чтобы номера слотов не «поехали»
    context.chat_data["shown"] = offers[:LIMIT]
    await update.message.reply_text(format_list(offers))


async def slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"(?i)^\s*(?:слот\s*(\d+)|(\d+)\s*слот)\s*$", update.message.text)
    n = int(m.group(1) or m.group(2))

    try:
        fresh = await asyncio.to_thread(fetch_offers)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return

    shown = context.chat_data.get("shown")
    if not shown:
        # списка ещё не было: берём номер из свежих данных
        context.chat_data["shown"] = shown = fresh[:LIMIT]

    if n < 1 or n > len(shown):
        await update.message.reply_text(f"Слота {n} нет. Доступно: 1–{len(shown)}")
        return

    old = shown[n - 1]
    fresh_by_id = {o["id"]: o for o in fresh}
    cur = fresh_by_id.get(old["id"])

    if cur is None:
        # слот исчез: обновляем список
        context.chat_data["shown"] = fresh[:LIMIT]
        await update.message.reply_text(
            f"Слот {n} ({old['seller']}) уже исчез с сайта. Список обновлён:\n\n"
            + format_list(fresh)
        )
        return

    text = (f"Слот {n}: {cur['seller']} / {fmt_num(cur['amount'])} кк / "
            f"{cur['price']:.2f} ₽\n{cur['link']}")
    if cur["price"] != old["price"] or cur["amount"] != old["amount"]:
        text = (f"Данные слота изменились (было {fmt_num(old['amount'])} кк / "
                f"{old['price']:.2f} ₽)\n") + text
    await update.message.reply_text(text, disable_web_page_preview=True)


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        r = await asyncio.to_thread(requests.get, URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("a.tc-item")
        servers = {}
        for it in items:
            el = it.select_one(".tc-server")
            name = el.get_text(strip=True) if el else "(нет .tc-server)"
            servers[name] = servers.get(name, 0) + 1
        text = f"HTTP {r.status_code}, лотов на странице: {len(items)}\n"
        text += "\n".join(f"{k}: {v}" for k, v in list(servers.items())[:30])
        await update.message.reply_text(text or "Пусто")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/virts: список слотов от дешёвого к дорогому\n"
        "«4 слот» или «слот 4»: ссылка на слот"
    )


app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("virts", virts))
app.add_handler(CommandHandler("debug", debug))
app.add_handler(MessageHandler(
    filters.Regex(r"(?i)^\s*(слот\s*\d+|\d+\s*слот)\s*$"), slot
))
app.run_polling()
