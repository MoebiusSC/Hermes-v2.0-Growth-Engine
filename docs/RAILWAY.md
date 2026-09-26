# Railway: Hermes v2 paper worker

Crear un **servicio nuevo** desde `MoebiusSC/Hermes-v2.0-Growth-Engine` y vincular la rama `main` como origen de despliegues automáticos. El Dockerfile ejecuta `python -m hermes_trading.growth_web`: dashboard y worker paper en el mismo proceso. No reutilizar el volumen del servicio Hermes anterior.

1. Montar un volumen persistente en `/app/state` (un proceso y una réplica).
2. Mantener `HERMES_TRADING_MODE=paper` y `HERMES_GROWTH_STATE=/app/state/growth/account.json`.
3. Definir `HERMES_DASHBOARD_PASSWORD` con una contraseña única de 16–256 bytes. No ponerla en el repositorio. Railway debe pasar `PORT`; por defecto es 8080.
4. Generar un dominio HTTPS público y configurar `/health` como healthcheck. El dashboard solicita usuario `admin` y la contraseña. Una sola réplica: dos workers sobre el mismo archivo provocarían operaciones duplicadas.
5. Revisar logs de la primera observación y del siguiente cierre de 15 min. El panel muestra saldo, equity, drawdown, posición, operaciones, eventos y estado de pausa. Exportar periódicamente `account.json` durante 30 días.

Si `halted` deja de ser `null`, investigar antes de cualquier reinicio o limpieza del estado. No conectar este servicio a credenciales live. No hay integración con la cuenta paper de Alpaca en v2, porque el motor usa solamente pares cripto spot.
