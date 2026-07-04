"""
Herramientas del agente de inversion simulado.
Todas operan sobre portfolio_state.json (estado local, sin dinero real).
"""

import json
import os
from datetime import date

from claude_agent_sdk import tool, create_sdk_mcp_server
from market import get_price as _get_price

STATE_PATH = os.path.join(os.path.dirname(__file__), "portfolio_state.json")

# Por debajo de esta cantidad de participaciones, consideramos la posicion
# cerrada (evita "dust": restos infimos que ensucian la cartera para siempre).
DUST_THRESHOLD = 1e-6


def _load_state():
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


@tool("get_price", "Consulta el precio actual de mercado de un ticker (accion, ETF o cripto, ej. AAPL, VWCE.DE, BTC-EUR)", {"ticker": str})
async def get_price(args):
    ticker = args["ticker"].upper()
    try:
        price = _get_price(ticker)
        return {"content": [{"type": "text", "text": f"{ticker}: {price:.2f} (moneda nativa del activo, revisa si coincide con EUR)"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error obteniendo precio de {ticker}: {e}"}]}


@tool("get_portfolio", "Devuelve el estado actual de la cartera: cash disponible, posiciones abiertas y valor total estimado", {})
async def get_portfolio(args):
    state = _load_state()
    total = state["cash"]
    lines = [f"Cash disponible: {state['cash']:.2f} EUR", "Posiciones:"]
    if not state["positions"]:
        lines.append("  (ninguna)")
    for ticker, pos in state["positions"].items():
        try:
            price = _get_price(ticker)
            value = price * pos["shares"]
            total += value
            lines.append(f"  {ticker}: {pos['shares']:.6f} uds @ coste medio {pos['avg_price']:.2f} | precio actual {price:.2f} | valor {value:.2f} EUR")
        except Exception as e:
            lines.append(f"  {ticker}: error al valorar ({e})")
    lines.append(f"VALOR TOTAL ESTIMADO: {total:.2f} EUR")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool("buy", "Compra un ticker gastando una cantidad en EUR del cash disponible", {"ticker": str, "amount_eur": float, "reason": str})
async def buy(args):
    state = _load_state()
    ticker = args["ticker"].upper()
    try:
        amount = float(args["amount_eur"])
    except (TypeError, ValueError):
        return {"content": [{"type": "text", "text": "El importe debe ser un numero."}]}
    reason = args.get("reason", "")

    if amount <= 0:
        return {"content": [{"type": "text", "text": "El importe de compra debe ser positivo."}]}
    if amount > state["cash"]:
        return {"content": [{"type": "text", "text": f"Fondos insuficientes. Cash disponible: {state['cash']:.2f} EUR"}]}

    try:
        price = _get_price(ticker)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"No se pudo obtener precio de {ticker}: {e}"}]}

    shares = amount / price
    pos = state["positions"].get(ticker, {"shares": 0.0, "avg_price": 0.0})
    new_shares = pos["shares"] + shares
    pos["avg_price"] = (pos["avg_price"] * pos["shares"] + price * shares) / new_shares
    pos["shares"] = new_shares
    state["positions"][ticker] = pos
    state["cash"] -= amount

    state["transactions"].append({
        "date": str(date.today()),
        "type": "buy",
        "ticker": ticker,
        "amount_eur": amount,
        "price": price,
        "shares": shares,
        "reason": reason,
    })
    _save_state(state)
    return {"content": [{"type": "text", "text": f"Compra ejecutada: {shares:.6f} uds de {ticker} a {price:.2f} ({amount:.2f} EUR). Cash restante: {state['cash']:.2f} EUR"}]}


@tool("sell", "Vende parte o toda la posicion de un ticker. Usa shares=-1 para vender toda la posicion.", {"ticker": str, "shares": float, "reason": str})
async def sell(args):
    state = _load_state()
    ticker = args["ticker"].upper()
    try:
        shares_to_sell = float(args["shares"])
    except (TypeError, ValueError):
        return {"content": [{"type": "text", "text": "La cantidad debe ser un numero."}]}
    reason = args.get("reason", "")

    pos = state["positions"].get(ticker)
    if not pos:
        return {"content": [{"type": "text", "text": f"No tienes ninguna posicion en {ticker}."}]}

    # shares = -1 (o cualquier negativo) significa "vender todo"
    if shares_to_sell < 0:
        shares_to_sell = pos["shares"]

    if shares_to_sell <= 0:
        return {"content": [{"type": "text", "text": "La cantidad a vender debe ser positiva (o -1 para vender todo)."}]}
    if shares_to_sell > pos["shares"] + DUST_THRESHOLD:
        return {"content": [{"type": "text", "text": f"No hay suficientes unidades de {ticker} (tienes {pos['shares']:.6f})."}]}

    try:
        price = _get_price(ticker)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"No se pudo obtener precio de {ticker}: {e}"}]}

    shares_to_sell = min(shares_to_sell, pos["shares"])  # no vender mas de lo que hay
    proceeds = shares_to_sell * price
    pos["shares"] -= shares_to_sell
    if pos["shares"] <= DUST_THRESHOLD:
        del state["positions"][ticker]
    else:
        state["positions"][ticker] = pos
    state["cash"] += proceeds

    state["transactions"].append({
        "date": str(date.today()),
        "type": "sell",
        "ticker": ticker,
        "amount_eur": proceeds,
        "price": price,
        "shares": shares_to_sell,
        "reason": reason,
    })
    _save_state(state)
    return {"content": [{"type": "text", "text": f"Venta ejecutada: {shares_to_sell:.6f} uds de {ticker} a {price:.2f} ({proceeds:.2f} EUR). Cash actual: {state['cash']:.2f} EUR"}]}


@tool("save_snapshot", "Guarda una foto del valor total de la cartera en la fecha actual, con una nota breve sobre la decision de hoy", {"note": str})
async def save_snapshot(args):
    state = _load_state()
    total = state["cash"]
    positions_snapshot = {}
    for ticker, pos in state["positions"].items():
        try:
            price = _get_price(ticker)
            value = price * pos["shares"]
            total += value
            positions_snapshot[ticker] = {"shares": pos["shares"], "price": price, "value": value}
        except Exception:
            positions_snapshot[ticker] = {"shares": pos["shares"], "price": None, "value": None}

    snapshot = {
        "date": str(date.today()),
        "total_value": total,
        "cash": state["cash"],
        "positions": positions_snapshot,
        "note": args.get("note", ""),
    }
    state["snapshots"].append(snapshot)
    _save_state(state)
    return {"content": [{"type": "text", "text": f"Snapshot guardado. Valor total: {total:.2f} EUR"}]}


investment_tools_server = create_sdk_mcp_server(
    name="investment-tools",
    tools=[get_price, get_portfolio, buy, sell, save_snapshot],
)
