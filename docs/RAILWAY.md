# Railway: Hermes v2 paper worker

Crear un **servicio nuevo** desde `MoebiusSC/Hermes-v2.0-Growth-Engine` y vincular la rama `main` como origen de despliegues automáticos. El Dockerfile ejecuta `python -m hermes_trading.growth_web`: dashboard y worker paper en el mismo proceso. No reutilizar el volumen del servicio Hermes anterior.

1. Montar un volumen persistente en `/app/state` (un proceso y una réplica).
2. Mantener `HERMES_TRADING_MODE=paper` y `HERMES_GROWTH_STATE=/app/state/growth/account.json`.
3. Definir `HERMES_DASHBOARD_PASSWORD` con una contraseña única de 16–256 bytes. No ponerla en el repositorio. Railway debe pasar `PORT`; por defecto es 8080.
4. Generar un dominio HTTPS público y configurar `/health` como healthcheck. El dashboard solicita usuario `admin` y la contraseña. Una sola réplica: dos workers sobre el mismo archivo provocarían operaciones duplicadas.
5. Revisar logs de la primera observación y del siguiente cierre de 15 min. El panel muestra saldo, equity, drawdown, posición, operaciones, eventos y estado de pausa. Exportar periódicamente `account.json` durante 30 días. La cartera manual paper utiliza `/app/state/growth/manual_account.json`; respaldar también ese archivo para conservar sus operaciones.
6. Para la optimización cuantitativa autónoma, definir `HERMES_AUTOTUNE=on`. El primer estudio se programa una hora después del arranque y los cambios admitidos sobreviven los redeploys en el volumen; `growth.json` es la base fija. El panel muestra próximo ciclo, última decisión y parámetros activos. No ejecute dos réplicas ni otro optimizador sobre el mismo estado.

Si `halted` deja de ser `null`, investigar antes de cualquier reinicio o limpieza del estado. No conectar este servicio a credenciales live. La cartera manual paper acepta ETF fraccionados como simulación separada y obtiene cotizaciones públicas; no usa una cuenta paper de Alpaca. El motor autónomo sigue operando solo pares cripto spot.
