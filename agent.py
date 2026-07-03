"""
Ejecuta UNA sesion de revision/gestion de la cartera simulada.
Uso:
    python agent.py                          -> sesion normal de gestion
    python agent.py "mensaje personalizado"  -> instruccion especifica para esta sesion

Variables de entorno opcionales:
    AGENT_MODE=proponer   -> el agente SOLO propone cambios, no compra/vende de verdad
    AGENT_MODE=ejecutar   -> (por defecto) el agente puede comprar/vender de verdad
    RESPETAR_PAUSA=1      -> si la cartera esta en pausa (bot_config.json), no hace nada
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID -> si estan presentes, manda el resumen final por Telegram
"""

import asyncio
import json
import os
import sys

from claude_agent_sdk import query, ClaudeAgentOptions
from tools import investment_tools_server

AGENT_MODE = os.environ.get("AGENT_MODE", "ejecutar")
RESPETAR_PAUSA = os.environ.get("RESPETAR_PAUSA") == "1"

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "bot_config.json")

SYSTEM_PROMPT_BASE = """
Eres un gestor de una cartera de inversion SIMULADA (dinero ficticio, sin riesgo real),
con fines exclusivamente educativos para aprender sobre agentes de IA e inversion.

Contexto:
- Capital inicial: 10000 EUR, aportados el 2026-07-03.
- Horizonte de evaluacion: revisiones a 1, 3 y 6 meses desde el inicio.
- Objetivo: maximizar la rentabilidad ajustada a riesgo en ese horizonte, con una
  estrategia razonable y diversificada (no apuestas todo a un solo activo).

En cada sesion debes:
1. Llamar a get_portfolio para ver el estado actual (cash, posiciones, valor total).
2. Consultar precios de los activos que te interesen con get_price.
3. Explica SIEMPRE tu razonamiento antes de proponer o ejecutar una operacion.

Reglas:
- No inventes precios: usa siempre get_price antes de operar.
- No gastes mas cash del disponible.
- Se conservador con el tamano de las posiciones individuales (evita concentrar
  todo el capital en un solo activo).
- Esto es una simulacion educativa, no asesoramiento financiero real.
- Termina SIEMPRE tu respuesta con un resumen breve y claro (2-4 lineas) en texto
  plano, apto para mandarse tal cual por un mensaje de Telegram.
"""

MODO_PROPONER_EXTRA = """
MODO PROPUESTA (IMPORTANTE):
No tienes disponibles las herramientas de compra/venta en esta sesion.
Tu trabajo es analizar la situacion y PROPONER en texto que operaciones
harias y por que, con importes aproximados, pero SIN ejecutarlas.
No llames a save_snapshot tampoco: no ha cambiado nada todavia.
"""

MODO_EJECUTAR_EXTRA = """
4. Al final de la sesion, llama SIEMPRE a save_snapshot con una nota resumen de
   la decision tomada hoy, aunque sea "mantener sin cambios".
"""


def cartera_pausada() -> bool:
    if not os.path.exists(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        return bool(config.get("pausado"))
    except Exception:
        return False


def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"No se pudo enviar el resumen por Telegram: {e}")


async def main():
    if RESPETAR_PAUSA and cartera_pausada():
        print("Cartera en pausa (/pausar activo). No se ejecuta nada esta vez.")
        return

    user_message = sys.argv[1] if len(sys.argv) > 1 else (
        "Es momento de revisar la cartera. Analiza el estado actual y decide si "
        "hay que tomar alguna accion hoy."
    )

    if AGENT_MODE == "proponer":
        system_prompt = SYSTEM_PROMPT_BASE + MODO_PROPONER_EXTRA
        allowed_tools = [
            "mcp__investment__get_price",
            "mcp__investment__get_portfolio",
        ]
        prefijo_telegram = "*Propuesta del agente* (aun no ejecutada)\n\n"
    else:
        system_prompt = SYSTEM_PROMPT_BASE + MODO_EJECUTAR_EXTRA
        allowed_tools = [
            "mcp__investment__get_price",
            "mcp__investment__get_portfolio",
            "mcp__investment__buy",
            "mcp__investment__sell",
            "mcp__investment__save_snapshot",
        ]
        prefijo_telegram = "*Resumen de la revision*\n\n"

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model="claude-sonnet-4-6",
        mcp_servers={"investment": investment_tools_server},
        allowed_tools=allowed_tools,
    )

    ultimo_texto = ""
    async for message in query(prompt=user_message, options=options):
        print(message)
        # Vamos guardando el ultimo texto de respuesta del asistente para
        # mandarlo por Telegram al terminar
        content = getattr(message, "content", None)
        if content:
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    ultimo_texto = text

    if ultimo_texto:
        send_telegram(prefijo_telegram + ultimo_texto[:3500])


if __name__ == "__main__":
    asyncio.run(main())
