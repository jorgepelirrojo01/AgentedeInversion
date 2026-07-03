"""
Bot de Telegram del agente de inversion.
No usa la API de Claude para el estado/comandos normales -> coste 0 tokens.
Solo /rebalancear + /confirmar disparan una ejecucion real del agente
(via GitHub Actions), que si consume creditos de la API.

Comandos disponibles:
    /actualiza             -> estado actual + rentabilidad 24h / 7d / 30d
    /composicion           -> desglose en % de cada posicion
    /historial              -> ultimos movimientos realizados
    /comparar               -> vs. benchmark de comprar-y-mantener (VWCE.DE)
    /hora HH:MM             -> cambia la hora del aviso diario (hora de Madrid)
    /pausar                 -> silencia avisos y revisiones automaticas
    /reanudar                -> reactiva avisos y revisiones automaticas
    /rebalancear <texto>    -> pide una propuesta de cambios al agente (no ejecuta nada aun)
    /confirmar               -> ejecuta la ultima propuesta pendiente de /rebalancear
    /cancelar                 -> descarta la propuesta pendiente
    /help                     -> lista de comandos
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests

REPO_DIR = os.path.dirname(__file__)
STATE_PATH = os.path.join(REPO_DIR, "portfolio_state.json")
CONFIG_PATH = os.path.join(REPO_DIR, "bot_config.json")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")  # owner/repo, lo pone Actions solo

CAPITAL_INICIAL = 10000.0
BENCHMARK_TICKER = "VWCE.DE"   # usado por /comparar
UMBRAL_ALERTA_PCT = 5.0        # variacion en 24h que dispara una alerta automatica

# Aproximacion de zona horaria de Madrid respecto a UTC.
# CEST (marzo-octubre) = UTC+2, CET (resto del ano) = UTC+1.
# Si en algun momento notas que la hora del aviso se desfasa 1h por el
# cambio de horario, ajusta este valor manualmente a 1.
MADRID_OFFSET_HOURS = 2


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


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


def current_total_value():
    state = load_json(STATE_PATH, {})
    total = state.get("cash", 0.0)
    detalle = []
    precios_actuales = {}
    for ticker, pos in state.get("positions", {}).items():
        try:
            price = get_price(ticker)
        except Exception:
            price = pos["avg_price"]  # fallback si el precio no esta disponible
        precios_actuales[ticker] = price
        valor = price * pos["shares"]
        total += valor
        detalle.append((ticker, valor))
    return total, detalle, precios_actuales, state


def pct_change(old, new):
    if old in (None, 0):
        return None
    return (new / old - 1) * 100


def get_historical_price(ticker: str, days_ago: int) -> float:
    """Precio de cierre mas cercano a 'days_ago' dias atras, directamente de yfinance."""
    import yfinance as yf
    import pandas as pd

    period = "1mo" if days_ago <= 25 else "3mo"
    hist = yf.Ticker(ticker).history(period=period)
    if hist.empty:
        raise ValueError(f"Sin historico para {ticker}")

    target = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=days_ago))
    idx = hist.index
    idx_utc = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
    diffs = abs(idx_utc - target)  # target ya es UTC-aware, no hace falta tz_localize otra vez
    closest = diffs.argmin()
    return float(hist["Close"].iloc[closest])


def historical_portfolio_value(state, days_ago: int, precios_actuales: dict):
    """
    Valor aproximado de la cartera hace N dias: usa las posiciones ACTUALES
    (numero de participaciones de hoy) valoradas al precio de aquel momento.
    Si no se puede obtener el precio historico de un ticker (ocurre con
    algunos ETFs poco liquidos), se usa su precio ACTUAL como aproximacion
    -> se asume que "no cambio" en vez de excluirlo, que distorsionaria
    la comparacion (una posicion que desaparece del pasado infla el % de
    subida de forma artificial).
    Nota: si hubo compras/ventas entre medias, sigue siendo una aproximacion,
    no el valor exacto que tenia la cartera ese dia.
    """
    total = state.get("cash", 0.0)  # aproximamos con el cash actual
    for ticker, pos in state.get("positions", {}).items():
        try:
            price = get_historical_price(ticker, days_ago)
        except Exception:
            price = precios_actuales.get(ticker, pos["avg_price"])
        total += price * pos["shares"]
    return total


def build_message() -> str:
    total, detalle, precios_actuales, state = current_total_value()

    rentabilidad_total = pct_change(CAPITAL_INICIAL, total)

    created_at = state.get("created_at")
    dias_desde_creacion = None
    if created_at:
        dias_desde_creacion = (
            datetime.now(timezone.utc).date() - datetime.strptime(created_at, "%Y-%m-%d").date()
        ).days

    def valor_hace(dias):
        # Si la cartera no lleva tanto tiempo abierta, no tiene sentido
        # comparar contra un periodo en el que ni siquiera existia.
        if dias_desde_creacion is None or dias_desde_creacion < dias:
            return None
        return historical_portfolio_value(state, dias, precios_actuales)

    v24h = valor_hace(1)
    v7d = valor_hace(7)
    v30d = valor_hace(30)

    def fmt(r):
        return f"{r:+.2f}%" if r is not None else "sin datos suficientes (cartera muy reciente)"

    lineas = [f"Cash: {state.get('cash', 0):.2f} EUR"]
    for ticker, valor in detalle:
        lineas.append(f"{ticker}: {valor:.2f} EUR")

    mensaje = (
        f"*Estado de la cartera*\n\n"
        f"Valor total: *{total:.2f} EUR*\n"
        f"Rentabilidad total: *{fmt(rentabilidad_total)}*\n"
        f"Ultimas 24h: *{fmt(pct_change(v24h, total))}*\n"
        f"Ultima semana: *{fmt(pct_change(v7d, total))}*\n"
        f"Ultimo mes: *{fmt(pct_change(v30d, total))}*\n\n"
        + "\n".join(lineas)
    )
    return mensaje


def build_composicion_message() -> str:
    total, detalle, _, state = current_total_value()
    if total <= 0:
        return "Cartera vacia."
    lineas = []
    cash = state.get("cash", 0.0)
    lineas.append(f"Cash: {cash:.2f} EUR ({cash / total * 100:.1f}%)")
    for ticker, valor in detalle:
        lineas.append(f"{ticker}: {valor:.2f} EUR ({valor / total * 100:.1f}%)")
    return f"*Composicion de la cartera*\n\nValor total: *{total:.2f} EUR*\n\n" + "\n".join(lineas)


def build_historial_message(n: int = 8) -> str:
    state = load_json(STATE_PATH, {})
    transacciones = state.get("transactions", [])
    if not transacciones:
        return "Todavia no hay ningun movimiento registrado."
    ultimas = transacciones[-n:][::-1]  # las mas recientes primero
    lineas = []
    for tx in ultimas:
        emoji = "🟢" if tx["type"] == "buy" else "🔴"
        lineas.append(
            f"{emoji} {tx['date']} - {tx['type'].upper()} {tx['ticker']} "
            f"({tx['amount_eur']:.2f} EUR)\n   _{tx.get('reason', '')}_"
        )
    return f"*Ultimos {len(ultimas)} movimientos*\n\n" + "\n\n".join(lineas)


def build_comparar_message() -> str:
    """Compara la cartera real con lo que habria dado comprar-y-mantener el benchmark."""
    total, _, _, state = current_total_value()
    created_at = state.get("created_at")
    if not created_at:
        return "No hay fecha de inicio registrada para comparar."

    dias_transcurridos = (
        datetime.now(timezone.utc).date() - datetime.strptime(created_at, "%Y-%m-%d").date()
    ).days
    dias_transcurridos = max(dias_transcurridos, 1)

    try:
        precio_inicio = get_historical_price(BENCHMARK_TICKER, dias_transcurridos)
        precio_ahora = get_price(BENCHMARK_TICKER)
    except Exception:
        return f"No se pudo obtener el precio de {BENCHMARK_TICKER} para comparar."

    valor_benchmark = (CAPITAL_INICIAL / precio_inicio) * precio_ahora
    rentabilidad_cartera = pct_change(CAPITAL_INICIAL, total)
    rentabilidad_benchmark = pct_change(CAPITAL_INICIAL, valor_benchmark)
    diferencia = rentabilidad_cartera - rentabilidad_benchmark

    veredicto = (
        "El agente le esta ganando al mercado" if diferencia > 0
        else "El mercado (comprar y mantener) le esta ganando al agente" if diferencia < 0
        else "Empate tecnico con el mercado"
    )

    return (
        f"*Tu cartera vs. comprar-y-mantener {BENCHMARK_TICKER}*\n\n"
        f"Tu cartera: *{total:.2f} EUR* ({rentabilidad_cartera:+.2f}%)\n"
        f"Si hubieras comprado solo {BENCHMARK_TICKER} el dia 1: "
        f"*{valor_benchmark:.2f} EUR* ({rentabilidad_benchmark:+.2f}%)\n\n"
        f"Diferencia: *{diferencia:+.2f} puntos*\n"
        f"{veredicto}"
    )


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    })
    resp.raise_for_status()



def check_alerts(config):
    """Avisa si algun activo se ha movido mas de UMBRAL_ALERTA_PCT en 24h.
    Como mucho una alerta por ticker y por dia, para no ser pesado."""
    state = load_json(STATE_PATH, {})
    hoy_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    alertados_hoy = config.get("alertados_hoy", {})

    for ticker, pos in state.get("positions", {}).items():
        if alertados_hoy.get(ticker) == hoy_str:
            continue  # ya avisamos hoy de este activo
        try:
            precio_ahora = get_price(ticker)
            precio_ayer = get_historical_price(ticker, 1)
        except Exception:
            continue
        variacion = pct_change(precio_ayer, precio_ahora)
        if variacion is not None and abs(variacion) >= UMBRAL_ALERTA_PCT:
            direccion = "subido" if variacion > 0 else "caido"
            send_telegram(
                f"⚠️ *Alerta*: {ticker} ha {direccion} un *{variacion:+.2f}%* en las ultimas 24h."
            )
            alertados_hoy[ticker] = hoy_str

    config["alertados_hoy"] = alertados_hoy
    return config


def start_proposal(instrucciones: str):
    """Pide al agente una PROPUESTA de cambios, sin ejecutar nada todavia."""
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        send_telegram("No se pudo pedir la propuesta: falta configuracion de GitHub.")
        return
    config = load_json(CONFIG_PATH, {})
    config["propuesta_pendiente"] = {
        "instrucciones": instrucciones[:500],
        "fecha": datetime.now(timezone.utc).isoformat(),
    }
    save_json(CONFIG_PATH, config)

    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/workflows/agente-semanal.yml/dispatches"
    resp = requests.post(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }, json={
        "ref": "main",
        "inputs": {"mensaje": instrucciones[:500], "modo": "proponer"},
    })
    if resp.status_code == 204:
        send_telegram(
            "Pidiendo al agente una propuesta (sin ejecutar nada aun)... "
            "te mando el plan en 1-2 minutos. Cuando lo veas, responde /confirmar o /cancelar."
        )
    else:
        send_telegram(f"No se pudo pedir la propuesta (HTTP {resp.status_code}).")


def confirm_proposal():
    config = load_json(CONFIG_PATH, {})
    propuesta = config.get("propuesta_pendiente")
    if not propuesta:
        send_telegram("No hay ninguna propuesta pendiente de confirmar.")
        return
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        send_telegram("No se pudo ejecutar: falta configuracion de GitHub.")
        return

    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/workflows/agente-semanal.yml/dispatches"
    resp = requests.post(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }, json={
        "ref": "main",
        "inputs": {
            "mensaje": propuesta["instrucciones"] + " (Ya propusiste un plan para esto antes: ejecutalo ahora de verdad.)",
            "modo": "ejecutar",
        },
    })
    config["propuesta_pendiente"] = None
    save_json(CONFIG_PATH, config)

    if resp.status_code == 204:
        send_telegram("Ejecutando el plan confirmado. Te aviso con el resumen en 1-2 minutos.")
    else:
        send_telegram(f"No se pudo ejecutar (HTTP {resp.status_code}).")


def cancel_proposal():
    config = load_json(CONFIG_PATH, {})
    if config.get("propuesta_pendiente"):
        config["propuesta_pendiente"] = None
        save_json(CONFIG_PATH, config)
        send_telegram("Propuesta descartada. No se ha ejecutado nada.")
    else:
        send_telegram("No habia ninguna propuesta pendiente.")


def handle_command(text: str):
    text = text.strip()
    if text.startswith("/hora"):
        partes = text.split(maxsplit=1)
        if len(partes) == 2 and ":" in partes[1]:
            config = load_json(CONFIG_PATH, {})
            config["hora_aviso"] = partes[1].strip()
            save_json(CONFIG_PATH, config)
            send_telegram(f"Hora del aviso diario actualizada a las {partes[1].strip()} (hora de Madrid).")
        else:
            send_telegram("Uso: /hora HH:MM (ejemplo: /hora 11:00)")

    elif text.startswith("/actualiza") or text.startswith("/estado"):
        send_telegram(build_message())

    elif text.startswith("/composicion"):
        send_telegram(build_composicion_message())

    elif text.startswith("/historial"):
        send_telegram(build_historial_message())

    elif text.startswith("/comparar"):
        send_telegram(build_comparar_message())

    elif text.startswith("/pausar"):
        config = load_json(CONFIG_PATH, {})
        config["pausado"] = True
        save_json(CONFIG_PATH, config)
        send_telegram("Pausado. No recibiras avisos ni se ejecutaran revisiones automaticas hasta que mandes /reanudar.")

    elif text.startswith("/reanudar"):
        config = load_json(CONFIG_PATH, {})
        config["pausado"] = False
        save_json(CONFIG_PATH, config)
        send_telegram("Reanudado. Avisos y revisiones automaticas activos de nuevo.")

    elif text.startswith("/rebalancear"):
        instrucciones = text[len("/rebalancear"):].strip()
        if not instrucciones:
            send_telegram("Uso: /rebalancear <lo que quieras pedirle al agente>")
        else:
            start_proposal(instrucciones)

    elif text.startswith("/confirmar"):
        confirm_proposal()

    elif text.startswith("/cancelar"):
        cancel_proposal()

    elif text.startswith("/help") or text.startswith("/start"):
        send_telegram(
            "*Comandos disponibles*\n\n"
            "/actualiza - estado actual + rentabilidad 24h/7d/30d\n"
            "/composicion - desglose en % de cada posicion\n"
            "/historial - ultimos movimientos realizados\n"
            "/comparar - tu cartera vs comprar-y-mantener\n"
            "/hora HH:MM - cambia la hora del aviso diario\n"
            "/pausar - silencia avisos y revisiones automaticas\n"
            "/reanudar - reactiva avisos y revisiones automaticas\n"
            "/rebalancear <texto> - pide una propuesta al agente (no ejecuta nada)\n"
            "/confirmar - ejecuta la ultima propuesta pendiente\n"
            "/cancelar - descarta la propuesta pendiente\n"
        )


def poll_updates():
    """Consulta si hay mensajes nuevos y los procesa. Devuelve la config actualizada."""
    config = load_json(CONFIG_PATH, {"hora_aviso": "11:00", "offset": 0, "ultimo_envio": None})
    offset = config.get("offset", 0)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = requests.get(url, params={"offset": offset, "timeout": 0})
    data = resp.json()

    for update in data.get("result", []):
        offset = update["update_id"] + 1
        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", ""))
        if text and chat_id == TELEGRAM_CHAT_ID:
            handle_command(text)

    # Recargamos del disco antes de guardar el offset: handle_command puede
    # haber modificado la config (pausado, hora_aviso...) durante el bucle,
    # y no queremos pisar esos cambios con la copia vieja que teniamos en memoria.
    config = load_json(CONFIG_PATH, config)
    config["offset"] = offset
    save_json(CONFIG_PATH, config)
    return config


def check_scheduled_send(config):
    """Comprueba si toca enviar el aviso diario programado por el usuario."""
    if config.get("pausado"):
        return  # el usuario ha pedido silencio con /pausar

    hora_aviso = config.get("hora_aviso", "11:00")
    ultimo_envio = config.get("ultimo_envio")

    ahora_madrid = datetime.now(timezone.utc) + timedelta(hours=MADRID_OFFSET_HOURS)
    hoy_str = ahora_madrid.strftime("%Y-%m-%d")

    try:
        hora_objetivo = datetime.strptime(hora_aviso, "%H:%M").time()
    except ValueError:
        hora_objetivo = datetime.strptime("11:00", "%H:%M").time()

    objetivo_dt = ahora_madrid.replace(
        hour=hora_objetivo.hour, minute=hora_objetivo.minute, second=0, microsecond=0
    )
    # Ventana de 15 min para no depender de que el cron caiga justo al minuto exacto
    dentro_de_ventana = objetivo_dt <= ahora_madrid < objetivo_dt + timedelta(minutes=15)

    if dentro_de_ventana and ultimo_envio != hoy_str:
        send_telegram(build_message())
        config["ultimo_envio"] = hoy_str
        save_json(CONFIG_PATH, config)


if __name__ == "__main__":
    cfg = poll_updates()
    if not cfg.get("pausado"):
        cfg = check_alerts(cfg)
        save_json(CONFIG_PATH, cfg)  # persistimos alertados_hoy si cambio
    check_scheduled_send(cfg)
