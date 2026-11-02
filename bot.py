import re
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = "8782025408:AAFau87lGBKO_zuLncAC7GCcdyh_de_SrmQ"
URL = "https://funpay.com/chips/51/"
SERVER_NAME = "Grand Mobile №01"   # по какой части названия сервера фильтруем
LIMIT = 15                         # сколько лотов показывать

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def to_number(text: str) -> float:
    """'1 000 000' -> 1000000.0, '12,5 ₽' -> 12.5"""
    cleaned = re.sub(r"[^\d.,]", "", text.replace("\xa0", "").replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    return float(cleaned) if cleaned else 0.0


def fetch_offers():
    r = requests.get(URL, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    offers = []
    for item in soup.select("a.tc-item"):
        server_el = item.select_one(".tc-server")
        server = server_el.get_text(strip=True) if server_el else ""

        # фильтр по нужному серверу
        if SERVER_NAME.lower() not in server.lower():
            continue

        seller_el = item.select_one(".media-user-name")
        amount_el = item.select_one(".tc-amount")
        price_el = item.select_one(".tc-price")
        if not (seller_el and amount_el and price_el):
            continue

        amount = to_number(amount_el.get_text())
        price = to_number(price_el.get_text())
        if amount <= 0:
            continue

        offers.append({
            "seller": seller_el.get_text(strip=True),
            "amount": amount,
            "price": price,
            "per_million": price / amount * 1_000_000,
            "server": server,
        })

    # сначала самые дешёвые за 1 млн
    offers.sort(key=lambda o: o["per_million"])
    return offers


async def virts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Загружаю данные...")
    try:
        offers = fetch_offers()
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return

    if not offers:
        await update.message.reply_text(
            "Лоты не найдены. Попробуй /debug, возможно, нужно поправить селекторы."
        )
        return

    lines = [f"Сервер: {SERVER_NAME}", "продавец / кол-во / цена (за 1 млн)\n"]
    for o in offers[:LIMIT]:
        lines.append(
            f"{o['seller']} / {o['amount']:,.0f} / {o['price']:.2f} ₽ "
            f"({o['per_million']:.2f} ₽)".replace(",", " ")
        )
    await update.message.reply_text("\n".join(lines))


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает, какие серверы и сколько лотов бот видит на странице."""
    try:
        r = requests.get(URL, headers=HEADERS, timeout=15)
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
    await update.message.reply_text("Команда /virts покажет список продавцов виртов.")


app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("virts", virts))
app.add_handler(CommandHandler("debug", debug))
app.run_polling()