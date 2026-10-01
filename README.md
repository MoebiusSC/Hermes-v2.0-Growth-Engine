# Hermes v2.0 Growth Engine

Motor experimental de **paper trading cripto spot**, derivado de `MoebiusSC/hermes-trading`. La imagen de este repositorio arranca el nuevo worker `hermes_trading.growth_run`. El worker y el dashboard originales permanecen en el código para consulta y compatibilidad, pero sus cuentas por activo y su reflexión automática **no gobiernan v2**.

## Qué hace v2

- Comparte un solo saldo simulado inicial de **50 USDT** entre BTC/USDT, ETH/USDT, SOL/USDT, XRP/USDT y LINK/USDT. No opera acciones, shorts, margen ni fondos reales. En un estado persistente anterior, XRP y LINK se incorporan solo tras verificar datos recientes, cartera plana y disponibilidad de ambos pares en Alpaca paper.
- Clasifica velas cerradas de 1 h y 15 min como `RANGE`, `TREND_UP` o sin operación. Desactiva entradas en volatilidad alta y tendencias bajistas. Cruces RSI distintos generan señales de reversión y retroceso tendencial.
- Compara señales simultáneas y admite hasta tres posiciones spot bajo un presupuesto global de riesgo y exposición. Rechaza una oportunidad si la distancia al objetivo no supera dos veces el costo total estimado.
- Calcula unidades con riesgo máximo de 0,5 % del saldo por operación, exposición máxima de 50 %, orden mínima configurable y comisión, deslizamiento y spread simulados. Los límites de pérdida diaria, semanal y drawdown se enclavan; requieren revisión humana del estado antes de reanudar.
- Genera decisiones al cierre de una vela. El backtest simula la apertura siguiente; el worker paper usa la cotización observada dentro de los 60 segundos posteriores al cierre o descarta la señal. Para un stop y objetivo tocados en la misma vela, supone primero el stop; los gaps pueden empeorar el precio.
- Guarda saldo, posiciones, señales pendientes, eventos, operaciones y curva de equity en un archivo JSON escrito atómicamente. Una pausa de datos o tres fallos consecutivos detienen nuevas entradas.
- Espera hasta dos minutos por una vela recién cerrada antes de enclavar una pausa de datos y valida todos los activos antes de avanzar los cursores. Repara huecos con velas reales del mismo proveedor (máximo siete días). Las pausas de datos pueden recuperarse con posiciones abiertas tras revisar su historial y reconciliar la propiedad y las órdenes en Alpaca paper. Si se descubre un stop u objetivo pasado, sale con cotización actual y registra el motivo; nunca inventa una ejecución histórica ni reproduce entradas atrasadas. El dashboard muestra activo, fuente y hueco. Los límites de pérdida y las discrepancias del broker siguen enclavados.
- Evalúa una cartera compartida con los mismos métodos de señales, tamaño y fills. El laboratorio compara cambios de parámetros alpha en cuatro ventanas, tres activos y costos duplicados. El worker usa además DSR, PBO y remuestreo por bloques para aplicar **un solo cambio acotado** de forma autónoma cuando `HERMES_AUTOTUNE=on`; nunca ajusta capital, riesgo ni costos.

## Inicio local

Python 3.11 o superior:

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -p 'test_*.py' -v
uv run python -m hermes_trading.growth_run backtest --days 90
uv run python -m hermes_trading.growth_run paper --once
uv run python -m hermes_trading.growth_run paper
```

`paper --once` inicia observación, consulta datos públicos y guarda el estado; las señales nuevas se consideran a partir de velas siguientes. Configure `HERMES_GROWTH_STATE` en una ruta persistente. El mismo archivo solo debe usarlo **un proceso**. `growth.json` se valida contra el estado guardado: únicamente la expansión documentada de tres a cinco activos puede migrarse automáticamente después de las comprobaciones; otros cambios requieren migración/revisión explícita. Las credenciales del exchange no son necesarias para este worker.

Para comparar una hipótesis, copie `growth.json` y cambie solo parámetros alpha, por ejemplo `range_rsi` o `target_r`:

```bash
uv run python -m hermes_trading.growth_lab --baseline growth.json --candidate candidato.json
```

El laboratorio exige al menos 30 operaciones del candidato, tres activos operados, 75 % de ventanas positivas, mejora mediana frente a la base, drawdown similar, supervivencia con costos a 2× y 3×, DSR ≥95 %, PBO ≤20 % y estrés mediante remuestreo por bloques. El contador de ensayos se reserva antes de investigar y sobrevive a los reinicios. Un resultado favorable **no prueba** que la ventaja vaya a persistir. Los datos históricos se descargan de fuentes públicas; compruebe cobertura y liquidez antes de interpretar resultados.

## Optimización automática y comparación

Con `HERMES_AUTOTUNE=on`, la primera evaluación comienza aproximadamente una hora después de arrancar. Después prueba **un candidato predefinido por ciclo** con un cambio pequeño en RSI, stop ATR u objetivo. Compara la cartera completa durante cuatro ventanas cronológicas de 30 días con igual capital y costos, comprueba un mínimo de tres monedas operadas, un mínimo de 30 operaciones, el drawdown, costos duplicados y una mejora de al menos 0,25 puntos porcentuales con cinco operaciones en la ventana más reciente. Exige velas cerradas, cobertura completa y ausencia de huecos en ambas temporalidades. La promoción requiere evidencia de todos los controles de [Validation Engine V1](docs/QUANT_VALIDATION.md), configuración sin cambios desde el estudio y costes de salida al cerrar cada ventana. El DSR usa retornos diarios y el total persistente de ensayos; el PBO compara una familia local de tres configuraciones con los mismos datos. Las ventanas son históricas y pueden reutilizarse entre ciclos; no sustituyen la observación prospectiva. Si aprueba, aplica el cambio solo estando sin posición, sin señal pendiente y sin pausa de riesgo. El intervalo normal es de siete días; un rechazo o error de datos permite otro intento al día siguiente con el siguiente candidato. Las investigaciones corren en segundo plano para no bloquear el worker.

La configuración base (`growth.json`) y todos los parámetros de capital, riesgo y costos quedan fijos en el estado. Las decisiones y sus motivos quedan en `account.json` y el dashboard. Después de 14 días y 10 operaciones cerradas bajo un cambio aplicado, el worker revierte los parámetros anteriores si la equity cae más de 0,5 % respecto del momento de aplicación; de lo contrario confirma el cambio. Esa pérdida posterior es un freno conservador, **no una prueba causal** de que el cambio fuese perjudicial. Una pausa por límites de riesgo nunca se libera automáticamente. El estudio histórico puede concluir que ningún candidato supera la base, por lo que activar la optimización no garantiza cambios ni rentabilidad.

El dashboard permite cargar en el navegador el JSON de `/api/state` del Hermes original para comparar **solo su cartera cripto** con V2 en el período de fechas común. Muestra retorno porcentual y máximo drawdown; el archivo original no sale del navegador. Hermes original y V2 tienen diferentes activos, capitales y reglas, por lo que la comparación observacional no demuestra superioridad estadística. Para una comparación experimental estricta harían falta iguales activos, períodos, costos y capitales en un backtest común.

## Operación manual paper

El dashboard protegido permite comprar y vender manualmente BTC, ETH, SOL, DOGE, BNB, XRP, LINK, AVAX, ADA, SUI, LTC y PAXG (pares USDT en el panel); los ETF SPY, VOO, QQQ; y acciones estadounidenses como AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AMD, KO y JPM. «Otra acción» acepta un ticker válido de EE. UU., que debe tener cotización disponible y, en Alpaca, estar habilitado para órdenes fraccionarias. El bot autónomo y su comparación cuantitativa usan BTC, ETH, SOL, XRP y LINK cuando la migración de la cartera existente haya concluido. El modo «Simulación local» usa **otra cartera simulada** con 50 USD iniciales, persistida en `manual_account.json` al lado de `account.json` en el mismo volumen. Sus órdenes nunca cambian el saldo, las métricas ni la optimización del motor autónomo. Las compras usan un presupuesto en USD (mínimo 5 USD) y las ventas usan unidades existentes. No hay apalancamiento ni ventas en corto.

El simulador local obtiene cotización nueva antes de cada orden: velas públicas de Binance spot con respaldo de Kraken para cripto y velas de 1 minuto de Yahoo Finance para acciones y ETF. Rechaza precios inválidos o antiguos; las acciones y ETF solo aceptan órdenes durante el horario regular de Nueva York con datos intradía recientes (máximo 20 minutos de retraso). Para cripto el límite es 2 minutos. Si ninguno de los proveedores ofrece una cotización válida para un par, no admite la operación. Aplica un deslizamiento simulado de 0,04 % en cripto y 0,05 % en acciones/ETF y una comisión simulada de 0,1 % en cripto. Estas son estimaciones locales. Las solicitudes requieren contraseña, origen coincidente y confirmación en el navegador. Cada orden tiene identificador para no duplicarla en un reintento. **El modo local no llama a ninguna API de órdenes externa.**

## Alpaca paper: una cuenta compartida

Se puede vincular **una cuenta Alpaca paper compartida**, independiente de la simulación local: órdenes manuales de los activos anteriores cuando Alpaca los ofrezca y reflejo de entradas y salidas cripto del bot V2. Debe iniciar vacía con aproximadamente **50 USD de equity**. El efectivo y equity de Alpaca son comunes; cada símbolo se asigna exclusivamente al operador manual o al bot. El capital de Alpaca no se importa a la curva cuantitativa de comparación, que conserva la cartera interna de V2. Acciones y ETF fraccionarios se compran por `notional` USD y se venden por `qty`; cripto usa pares `/USD` dentro de Alpaca (el panel local conserva `/USDT`). La pantalla consulta el activo en Alpaca y bloquea la orden si no está listado, no es negociable o una acción no es fraccionaria. La oferta de criptomonedas varía por cuenta y jurisdicción; aparecer en el selector **no** garantiza que Alpaca admita su compra. Las órdenes de mercado pueden quedar pendientes o ejecutarse a otro precio.

La conexión está **desactivada por defecto**. Configure en Railway `HERMES_ALPACA_MANUAL_KEY` y `HERMES_ALPACA_MANUAL_SECRET` como variables privadas con claves **paper** de la misma cuenta; después `HERMES_ALPACA_PAPER=on`. No copie estas claves al repositorio, al navegador ni a `HERMES_DASHBOARD_PASSWORD`. La URL de órdenes está fijada en `https://paper-api.alpaca.markets`; el código no puede recibir una URL live por configuración. Si faltan claves, el panel indica pendiente y sigue funcionando el simulador local.

Antes de colocar cualquier orden, el servidor comprueba la cuenta paper, el saldo en efectivo, la disponibilidad del activo, la ausencia de órdenes abiertas y las unidades atribuidas al origen de la venta. Exige al menos 10 USD para cripto, 5 USD para acciones/ETF fraccionarios y reserva un 2 % de efectivo en compras. Registra cada intento con `client_order_id` en el archivo persistente `alpaca_shared.json`; un reintento reconcilia primero con Alpaca. Si el bot ya tiene una posición interna al activar el espejo, espera a estar plano. Si una moneda pertenece al operador manual, el bot omite la entrada y la salida de esa operación; el panel informa el motivo. Ante saldo inicial distinto, posiciones extrañas, una orden parcial o falta de correspondencia, suspende los envíos afectados e informa el motivo. Los stops siguen siendo decisiones del worker a cierres de 15 minutos; cripto no tiene bracket de protección del lado de Alpaca. No conecta ninguna cuenta real.

La cartera local de 50 USD y su historial permanecen disponibles con el selector «Simulación local». El modo «Alpaca paper» muestra saldo y equity comunes, pero posiciones y órdenes manuales atribuidas; la sección del bot muestra su propia posición atribuida. El equity total de Alpaca no representa el rendimiento aislado del bot. No se traspasan posiciones locales ni las del Hermes original a la cuenta paper.

## Límites antes de usar dinero real

`HERMES_TRADING_MODE=live` se rechaza de forma explícita. El paper worker descubre stops al cerrar la vela de 15 minutos y **no coloca órdenes protectoras en un exchange**; si se corta la conexión, no hay protección real. La simulación supone fills históricos que pueden ser peores o imposibles con USDT 50, y el mínimo de orden del exchange puede variar por par.

La puerta de salida de un mes paper requiere analizar su curva, operaciones, fallos de datos, spread y costos reales, y probar fuera de muestra con la cuenta única. Antes de crear cualquier ruta live faltan escoger exchange y jurisdicción, validar mínimos y precisiones, desarrollar órdenes de protección del lado del exchange, reconciliar saldos y órdenes con idempotencia, ensayar fallos y probar con tamaños mínimos. Duplicar el capital cada mes no es una promesa ni un parámetro del algoritmo.

## Despliegue

La imagen `Dockerfile` inicia el worker v2 paper y un dashboard protegido con `HERMES_DASHBOARD_PASSWORD` (mínimo 16 bytes). La API `/api/state` requiere contraseña; `/api/manual/order` registra órdenes solo en la simulación independiente; `/health` es público para la sonda de Railway. Monte un volumen persistente en `/app/state`; vea [docs/RAILWAY.md](docs/RAILWAY.md). Este repositorio independiente no altera el deployment de `hermes-trading` original.
