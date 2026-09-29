# Hermes V2: contexto para agentes de código

Este repositorio es `MoebiusSC/Hermes-v2.0-Growth-Engine`. Es un proyecto separado de `MoebiusSC/hermes-trading` (Hermes original). Trabaja sobre la rama o worktree de V2 y no cambies ni despliegues el proyecto original por accidente.

## Comprobación local

- Python 3.11+ y `uv`.
- `uv sync --frozen`
- `uv run python -m unittest discover -s tests -p 'test_*.py' -v`
- `uv run python -m hermes_trading.growth_run paper --once` consulta datos públicos y guarda estado; establece `HERMES_GROWTH_STATE` a una ruta de prueba fuera del volumen de producción si lo ejecutas.

## Mapa del proyecto

- `hermes_trading/growth.py`: cartera y decisiones paper internas de V2.
- `hermes_trading/growth_run.py`: worker y optimización autónoma.
- `hermes_trading/growth_optimizer.py`, `growth_lab.py`: validación cuantitativa de candidatos.
- `hermes_trading/growth_web.py`, `growth_dashboard.html`, `growth_dashboard.js`: API y dashboard protegidos.
- `hermes_trading/manual_paper.py`: simulador manual local, independiente del bot; catálogo de criptos, ETF y acciones, incluidas acciones por ticker.
- `hermes_trading/alpaca_paper_bridge.py`: cuenta Alpaca paper compartida por órdenes manuales y reflejo del bot.
- `docs/RAILWAY.md`: configuración y despliegue. `README.md`: comportamiento, límites y comandos.

## Estado y seguridad de órdenes

- La curva de comparación Hermes vs. V2 usa la cartera interna; el efectivo y equity de Alpaca mezclan operaciones manuales y del bot, así que no representan la rentabilidad aislada del bot.
- Las acciones y algunas monedas adicionales son para órdenes manuales. El bot autónomo puede migrar de BTC/ETH/SOL a BTC/ETH/SOL/XRP/LINK solo tras comprobar datos recientes y disponibilidad de ambos pares en Alpaca paper, conservando el estado anterior si alguna comprobación falla.
- La conexión Alpaca apunta únicamente a `https://paper-api.alpaca.markets`. Las claves están en variables privadas de Railway `HERMES_ALPACA_MANUAL_KEY` y `HERMES_ALPACA_MANUAL_SECRET`; no las copies al código, al chat, a pruebas ni a commits. `HERMES_ALPACA_PAPER=on` habilita el puente en producción.
- `alpaca_shared.json` en el volumen persiste la identidad de cuenta, propiedad por símbolo, cursor y órdenes pendientes. El bot nunca debe vender unidades manuales. Las operaciones manuales nunca deben vender unidades del bot. No borres ni restaures ese archivo sin reconciliar posiciones y órdenes en Alpaca.
- `account.json` y `manual_account.json` también viven en `/app/state/growth`. Ejecuta una sola réplica de este servicio sobre el volumen. El worker paper no habilita dinero real; `HERMES_TRADING_MODE=live` se rechaza.
- Mantén los identificadores de orden y el registro de intención antes de enviar órdenes; trata respuestas perdidas, fills parciales y discrepancias de broker de forma conservadora.

Antes de cambiar la lógica de ejecución, revisa `README.md`, el puente Alpaca y las pruebas correspondientes. Para una modificación del algoritmo, conserva una comparación reproducible con la misma base, costos, períodos y reglas de riesgo.
