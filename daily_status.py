"""
Envia un resumen diario del estado de la cartera por Telegram.
No usa la API de Claude en absoluto -> coste 0 tokens.
Solo lee portfolio_state.json y consulta precios reales via yfinance.
"""

import json
import os
import requests

STATE_PATH = os.path.join(os.path.dirname(__file__), "portfolio_state.json")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def get_price(ticker: str) -> float:
    import yfinance as yf
    t = yf.Ticker(ticker)
    price = None
    try:
        price = t.fast_info.get("lastPrice")
    except Exception:
        price = None
    if price is None:
        hist = t.history(period="5d")
        if hist.empty:
            raise ValueError(f"Sin datos para {ticker}")
        price = float(hist["Close"].iloc[-1])
    return float(price)


def build_message() -> str:
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)

    capital_inicial = 10000.0
    total = state["cash"]
    lineas = [f"Cash: {state['cash']:.2f} EUR"]

    for ticker, pos in state["positions"].items():
        try:
            price = get_price(ticker)
            valor = price * pos["shares"]
            total += valor
            variacion = (price / pos["avg_price"] - 1) * 100
            lineas.append(
                f"{ticker}: {valor:.2f} EUR ({variacion:+.1f}% desde compra)"
            )
        except Exception:
            # Fallback: si no se puede consultar el precio actual, usamos el
            # coste medio de compra para no excluir la posicion del total
            # (evita mostrar una "perdida" falsa por fallo de datos).
            valor_estimado = pos["avg_price"] * pos["shares"]
            total += valor_estimado
            lineas.append(
                f"{ticker}: ~{valor_estimado:.2f} EUR (precio actual no disponible, "
                f"usando ultimo coste conocido)"
            )

    rentabilidad = (total / capital_inicial - 1) * 100

    mensaje = (
        f"*Estado de la cartera - {state['snapshots'][-1]['date'] if state['snapshots'] else ''}*\n\n"
        f"Valor total: *{total:.2f} EUR*\n"
        f"Rentabilidad: *{rentabilidad:+.2f}%*\n\n"
        + "\n".join(lineas)
    )
    return mensaje


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    })
    resp.raise_for_status()


if __name__ == "__main__":
    mensaje = build_message()
    send_telegram(mensaje)
    print("Mensaje enviado:")
    print(mensaje)
