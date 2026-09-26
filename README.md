# Hermes v2.0 Growth Engine

Motor experimental de **paper trading cripto spot**, derivado de `MoebiusSC/hermes-trading`. La imagen de este repositorio arranca el nuevo worker `hermes_trading.growth_run`. El worker y el dashboard originales permanecen en el código para consulta y compatibilidad, pero sus cuentas por activo y su reflexión automática **no gobiernan v2**.

## Qué hace v2

- Comparte un solo saldo simulado inicial de **50 USDT** entre BTC/USDT, ETH/USDT y SOL/USDT. No opera acciones, shorts, margen ni fondos reales.
- Clasifica velas cerradas de 1 h y 15 min como `RANGE`, `TREND_UP` o sin operación. Desactiva entradas en volatilidad alta y tendencias bajistas. Cruces RSI distintos generan señales de reversión y retroceso tendencial.
- Compara señales simultáneas y elige como máximo una posición spot. Rechaza una oportunidad si la distancia al objetivo no supera dos veces el costo total estimado.
- Calcula unidades con riesgo máximo de 0,5 % del saldo por operación, exposición máxima de 50 %, orden mínima configurable y comisión, deslizamiento y spread simulados. Los límites de pérdida diaria, semanal y drawdown se enclavan; requieren revisión humana del estado antes de reanudar.
- Genera decisiones al cierre de una vela. El backtest simula la apertura siguiente; el worker paper usa la cotización observada dentro de los 60 segundos posteriores al cierre o descarta la señal. Para un stop y objetivo tocados en la misma vela, supone primero el stop; los gaps pueden empeorar el precio.
- Guarda saldo, posiciones, señales pendientes, eventos, operaciones y curva de equity en un archivo JSON escrito atómicamente. Una pausa de datos o tres fallos consecutivos detienen nuevas entradas.
- Evalúa una cartera compartida con los mismos métodos de señales, tamaño y fills. El laboratorio compara cambios de parámetros alpha en cuatro ventanas, tres activos y costos duplicados; **solo emite una recomendación para revisión manual**.

## Inicio local

Python 3.11 o superior:

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -p 'test_*.py' -v
uv run python -m hermes_trading.growth_run backtest --days 90
uv run python -m hermes_trading.growth_run paper --once
uv run python -m hermes_trading.growth_run paper
```

`paper --once` inicia observación, consulta datos públicos y guarda el estado; las señales nuevas se consideran a partir de velas siguientes. Configure `HERMES_GROWTH_STATE` en una ruta persistente. El mismo archivo solo debe usarlo **un proceso**. `growth.json` se valida contra el estado guardado y un cambio de configuración exige migración/revisión explícita. Las credenciales del exchange no son necesarias para este worker.

Para comparar una hipótesis, copie `growth.json` y cambie solo parámetros alpha, por ejemplo `range_rsi` o `target_r`:

```bash
uv run python -m hermes_trading.growth_lab --baseline growth.json --candidate candidato.json
```

El laboratorio exige al menos 30 operaciones del candidato, tres activos operados, varios períodos positivos, mejora mediana frente a la base, drawdown similar y supervivencia con costos duplicados. Un resultado favorable **no prueba** que la ventaja vaya a persistir. Los datos históricos se descargan de fuentes públicas; compruebe cobertura y liquidez antes de interpretar resultados.

## Límites antes de usar dinero real

`HERMES_TRADING_MODE=live` se rechaza de forma explícita. El paper worker descubre stops al cerrar la vela de 15 minutos y **no coloca órdenes protectoras en un exchange**; si se corta la conexión, no hay protección real. La simulación supone fills históricos que pueden ser peores o imposibles con USDT 50, y el mínimo de orden del exchange puede variar por par.

La puerta de salida de un mes paper requiere analizar su curva, operaciones, fallos de datos, spread y costos reales, y probar fuera de muestra con la cuenta única. Antes de crear cualquier ruta live faltan escoger exchange y jurisdicción, validar mínimos y precisiones, desarrollar órdenes de protección del lado del exchange, reconciliar saldos y órdenes con idempotencia, ensayar fallos y probar con tamaños mínimos. Duplicar el capital cada mes no es una promesa ni un parámetro del algoritmo.

## Despliegue

La imagen `Dockerfile` inicia el worker v2 paper y un dashboard web de solo lectura, protegido con `HERMES_DASHBOARD_PASSWORD` (mínimo 16 bytes). La API `/api/state` también requiere la contraseña; `/health` es público para la sonda de Railway. Monte un volumen persistente en `/app/state`; vea [docs/RAILWAY.md](docs/RAILWAY.md). Este repositorio independiente no altera el deployment de `hermes-trading` original.
