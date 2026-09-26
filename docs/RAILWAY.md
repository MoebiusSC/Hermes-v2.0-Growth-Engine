# Railway: Hermes v2 paper worker

Crear un **servicio nuevo** desde este repositorio. El Dockerfile ejecuta `python -m hermes_trading.growth_run paper`; no expone dashboard ni necesita puerto HTTP. No reutilizar el volumen del servicio Hermes anterior.

1. Montar un volumen persistente en `/app/state` (un proceso y una réplica).
2. Mantener `HERMES_TRADING_MODE=paper` y `HERMES_GROWTH_STATE=/app/state/growth/account.json`.
3. Revisar logs de la primera observación y del siguiente cierre de 15 min. El JSON impreso incluye saldo, estado de pausa y métricas.
4. Exportar periódicamente `account.json` para analizar equity y operaciones durante 30 días. Si `halted` deja de ser `null`, investigar antes de cualquier reinicio o limpieza del estado.

No conectar este servicio a credenciales live. No hay integración con la cuenta paper de Alpaca en v2, porque el motor usa solamente pares cripto spot.
