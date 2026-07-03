"""
Ejecuta UNA sesion de revision/gestion de la cartera simulada.
Uso:
    python agent.py                          -> sesion normal de gestion
    python agent.py "mensaje personalizado"  -> instruccion especifica para esta sesion
"""

import asyncio
import sys
from claude_agent_sdk import query, ClaudeAgentOptions
from tools import investment_tools_server

SYSTEM_PROMPT = """
Eres un gestor de una cartera de inversion SIMULADA (dinero ficticio, sin riesgo real),
con fines exclusivamente educativos para aprender sobre agentes de IA e inversion.

Contexto:
- Capital inicial: 1000 EUR, aportados el 2026-07-03.
- Horizonte de evaluacion: revisiones a 1, 3 y 6 meses desde el inicio.
- Objetivo: maximizar la rentabilidad ajustada a riesgo en ese horizonte, con una
  estrategia razonable y diversificada (no apuestas todo a un solo activo).

En cada sesion debes:
1. Llamar a get_portfolio para ver el estado actual (cash, posiciones, valor total).
2. Consultar precios de los activos que te interesen con get_price.
3. Decidir si compras, vendes o mantienes. Explica SIEMPRE tu razonamiento antes de
   ejecutar una operacion (que estas comprando/vendiendo y por que).
4. Al final de la sesion, llama SIEMPRE a save_snapshot con una nota resumen de
   la decision tomada hoy, aunque sea "mantener sin cambios".

Reglas:
- No inventes precios: usa siempre get_price antes de operar.
- No gastes mas cash del disponible.
- Se conservador con el tamano de las posiciones individuales (evita concentrar
  todo el capital en un solo activo).
- Esto es una simulacion educativa, no asesoramiento financiero real.
"""


async def main():
    user_message = sys.argv[1] if len(sys.argv) > 1 else (
        "Es momento de revisar la cartera. Analiza el estado actual y decide si "
        "hay que tomar alguna accion hoy."
    )

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"investment": investment_tools_server},
        model="claude-sonnet-4-6",
        allowed_tools=[
            "mcp__investment__get_price",
            "mcp__investment__get_portfolio",
            "mcp__investment__buy",
            "mcp__investment__sell",
            "mcp__investment__save_snapshot",
        ],
    )

    async for message in query(prompt=user_message, options=options):
        # Imprime todo lo que el agente dice y hace, para llevar registro legible
        print(message)


if __name__ == "__main__":
    asyncio.run(main())
