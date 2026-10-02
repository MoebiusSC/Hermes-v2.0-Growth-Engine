const $ = id => document.getElementById(id);
const money = value => Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} USDT` : '—';
const pct = value => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(2)}%` : '—';
const utc = ms => new Date(ms).toLocaleString('es-BO', {timeZone:'UTC',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
const number = value => Number.isFinite(Number(value)) ? Number(value).toLocaleString('es-BO',{maximumFractionDigits:4}) : '—';
const pnlMoney = value => Number.isFinite(Number(value)) ? `${Number(value)>=0?'+':''}${Number(value).toFixed(4)} USDT` : '—';
let originalExport=null, currentV2=null;

function series(points, valueOf) {
  return (points||[]).map(p=>({ts:Date.parse(p.ts),equity:Number(valueOf(p))}))
    .filter(p=>Number.isFinite(p.ts)&&Number.isFinite(p.equity)&&p.equity>0).sort((a,b)=>a.ts-b.ts);
}

function periodMetrics(points) {
  const initial=points[0].equity, last=points.at(-1).equity;
  let peak=initial, drawdown=0;
  for (const p of points) {peak=Math.max(peak,p.equity);drawdown=Math.max(drawdown,1-p.equity/peak);}
  return {return:last/initial-1,drawdown};
}

function renderComparison() {
  const tbody=$('comparison-rows');tbody.replaceChildren();
  if (!originalExport||!currentV2) return;
  const old=series(originalExport.equity,p=>p.crypto?.realized+p.crypto?.unrealized);
  const modern=series(currentV2.curve,p=>p.equity);
  if (old.length<2||modern.length<2) {$('comparison-status').textContent='Faltan puntos de equity cripto para comparar.';return;}
  const start=Math.max(old[0].ts,modern[0].ts),end=Math.min(old.at(-1).ts,modern.at(-1).ts);
  const a=old.filter(p=>p.ts>=start&&p.ts<=end),b=modern.filter(p=>p.ts>=start&&p.ts<=end);
  if (a.length<2||b.length<2) {$('comparison-status').textContent='No hay un período común con suficientes datos.';return;}
  $('comparison-status').textContent=`Período común: ${utc(start)} – ${utc(end)} UTC`;
  for (const [label,points] of [['Hermes · cripto',a],['Hermes V2',b]]) {
    const m=periodMetrics(points),row=tbody.insertRow();
    for (const value of [label,pct(m.return),pct(m.drawdown),String(points.length)]) row.insertCell().textContent=value;
  }
}

$('original-file').addEventListener('change',async event=>{
  const file=event.target.files?.[0];if(!file)return;
  try {
    const data=JSON.parse(await file.text());
    if (!Array.isArray(data.equity)) throw new Error('El archivo no contiene la curva equity de Hermes.');
    originalExport=data;renderComparison();
  } catch (error) {originalExport=null;$('comparison-rows').replaceChildren();$('comparison-status').textContent=String(error.message);}
});

function line(svg, points, min, max, color) {
  const ns = 'http://www.w3.org/2000/svg';
  const x = i => 76 + i * 608 / Math.max(points.length - 1, 1);
  const y = value => 205 - (value - min) * 175 / Math.max(max - min, 0.000001);
  const values = points.map(p => Number(p.equity));
  const d = values.map((v,i) => `${i ? 'L' : 'M'}${x(i).toFixed(2)} ${y(v).toFixed(2)}`).join(' ');
  const area = document.createElementNS(ns,'path');
  area.setAttribute('d', `${d} L${x(values.length-1)} 205 L76 205 Z`);
  area.setAttribute('fill',color === '#70e1cd' ? '#70e1cd13' : '#ff879213');
  const path = document.createElementNS(ns,'path');
  path.setAttribute('d',d); path.setAttribute('stroke',color); path.setAttribute('stroke-width','2.5');
  path.setAttribute('stroke-linecap','round'); path.setAttribute('stroke-linejoin','round'); path.setAttribute('fill','none');
  svg.append(area,path);
  for (const v of [min,(min+max)/2,max]) {
    const grid = document.createElementNS(ns,'line'); const yy = y(v);
    grid.setAttribute('x1','76'); grid.setAttribute('x2','684'); grid.setAttribute('y1',yy); grid.setAttribute('y2',yy);
    grid.setAttribute('stroke','#263746'); grid.setAttribute('stroke-dasharray','3 6'); svg.insertBefore(grid,area);
    const tick=document.createElementNS(ns,'text');tick.setAttribute('x','68');tick.setAttribute('y',yy+4);
    tick.setAttribute('text-anchor','end');tick.textContent=Number(v).toFixed(2);svg.append(tick);
  }
  const marker=document.createElementNS(ns,'circle');marker.setAttribute('cx',x(values.length-1));
  marker.setAttribute('cy',y(values.at(-1)));marker.setAttribute('r','4');marker.setAttribute('fill',color);
  const title=document.createElementNS(ns,'title');title.textContent=`Último capital: ${values.at(-1).toFixed(2)} USDT`;
  marker.append(title);svg.append(marker);
}

function drawCurve(points) {
  const svg=$('chart'); svg.replaceChildren();
  const clean=points.filter(p => Number.isFinite(Number(p.equity)));
  if (clean.length < 2) {
    const label=document.createElementNS('http://www.w3.org/2000/svg','text');
    label.setAttribute('x','20');label.setAttribute('y','120');label.setAttribute('fill','#92a8b4');
    label.textContent='La curva aparecerá después del primer ciclo';svg.append(label);
    $('chart-start').textContent='—';$('chart-end').textContent='—';$('chart-range').textContent='—';return;
  }
  const values=clean.map(p=>Number(p.equity)), low=Math.min(...values), high=Math.max(...values);
  const pad=Math.max((high-low)*.12,0.03);
  line(svg,clean,low-pad,high+pad,values.at(-1)>=values[0]?'#70e1cd':'#ff8792');
  $('chart-start').textContent=utc(Date.parse(clean[0].ts));
  $('chart-end').textContent=utc(Date.parse(clean.at(-1).ts));
  $('chart-range').textContent=`Último: ${values.at(-1).toFixed(2)} USDT · escala USDT`;
}

function renderAssets(data) {
  const tbody=$('asset-rows');tbody.replaceChildren();
  const assets=Array.isArray(data.assets)?data.assets:[];
  const positions=(Array.isArray(data.positions)?data.positions:[]).filter(p=>p&&typeof p.asset==='string');
  const byAsset=Object.fromEntries(positions.map(p=>[p.asset,p]));
  $('asset-count').textContent=`${assets.length} monedas · ${positions.length} activas`;
  for (const asset of assets) {
    const pos=byAsset[asset],active=Boolean(pos);
    const row=tbody.insertRow();row.insertCell().textContent=asset;
    const status=row.insertCell(),pill=document.createElement('span');
    pill.className=`state-pill${active?' active':''}`;pill.textContent=active?'Activa':'Sin operación';status.append(pill);
    row.insertCell().textContent=active?number(pos.qty):'—';
    const bar=data.last_bar?.[asset];row.insertCell().textContent=Number.isFinite(bar)?`${utc(bar)} UTC`:'Sin datos';
    row.insertCell().textContent=String(data.metrics?.trades_by_asset?.[asset]??0);
  }
}

function renderAssetPerformance(data) {
  const container=$('asset-performance');container.replaceChildren();
  const assets=Array.isArray(data.assets)?data.assets:[];
  const metrics=data.metrics?.asset_metrics||{};
  for (const asset of assets) {
    const item=metrics[asset]||{trades:0,pnl:0,last_pnl:null,last_closed_ms:null};
    const last=Number(item.last_pnl), total=Number(item.pnl??0), hasLast=Number.isFinite(last)&&Number(item.trades)>0;
    const card=document.createElement('article');card.className='asset-performance-card';
    const head=document.createElement('div');head.className='asset-performance-head';
    const symbol=document.createElement('div');symbol.className='asset-performance-symbol';symbol.textContent=asset.replace('/USDT','');
    const count=document.createElement('span');count.className='state-pill';count.textContent=`${item.trades??0} cierres`;
    head.append(symbol,count);

    const result=document.createElement('div');result.className='asset-performance-result';
    result.textContent=hasLast?pnlMoney(last):'Sin operaciones cerradas';
    if (hasLast) result.classList.add(last>=0?'positive':'negative');

    const meta=document.createElement('div');meta.className='asset-performance-meta';
    const totalRow=document.createElement('div');
    const totalLabel=document.createElement('span');totalLabel.textContent='Acumulado';
    const totalValue=document.createElement('strong');totalValue.textContent=pnlMoney(total);
    totalValue.className=total>=0?'positive':'negative';totalRow.append(totalLabel,totalValue);
    const dateRow=document.createElement('div');
    const dateLabel=document.createElement('span');dateLabel.textContent='Último cierre';
    const dateValue=document.createElement('strong');dateValue.textContent=Number.isFinite(Number(item.last_closed_ms))?`${utc(Number(item.last_closed_ms))} UTC`:'—';
    dateRow.append(dateLabel,dateValue);
    meta.append(totalRow,dateRow);
    card.append(head,result,meta);container.append(card);
  }
}

function renderStrategies(data) {
  const tbody=$('strategy-rows');tbody.replaceChildren();
  const rows=data.metrics?.strategy_metrics||{};
  const labels={hermes_core:'Hermes Core',sui_ema_26_55:'SUI EMA 26/55'};
  for (const name of ['hermes_core','sui_ema_26_55']) {
    const item=rows[name]||{trades:0,pnl:0,return:0,win_rate:0,profit_factor:null,max_drawdown:0};
    const pnl=Number(item.pnl??0);
    const row=tbody.insertRow();
    const values=[labels[name]||name,String(item.trades??0),
      (pnl>=0?'+':'')+pnl.toFixed(4)+' USDT',
      pct(item.return??0),pct(item.win_rate??0),
      item.profit_factor===null?'—':number(item.profit_factor),pct(item.max_drawdown??0)];
    values.forEach((value,index)=>{const cell=row.insertCell();cell.textContent=value;
      if(index===2||index===3) cell.className=pnl>=0?'positive':'negative';});
  }
}

function renderReadiness(data) {
  const readiness=data.live_readiness||{};
  const ready=Boolean(readiness.ready), blockers=Number(readiness.blocking_count??0);
  const pill=$('live-readiness-pill');
  pill.textContent=ready?'Criterios completos':'PAPER ONLY';
  pill.className=`gate-pill ${ready?'gate-pass':'gate-pending'}`;
  $('live-readiness-headline').textContent=ready?
    'Criterios para considerar un micro-live completados':
    `${blockers} criterio${blockers===1?'':'s'} todavía no cumple${blockers===1?'':'n'}`;
  $('live-readiness-summary').textContent=(readiness.note||'Solo diagnóstico.')+
    ' Los umbrales del panel son puertas técnicas; no activan órdenes reales automáticamente.';
  const groups=$('live-readiness-groups');groups.replaceChildren();
  const label={PASS:'Cumple',PENDING:'Pendiente',FAIL:'No cumple',UNMEASURED:'No medible'};
  const cls={PASS:'gate-pass',PENDING:'gate-pending',FAIL:'gate-fail',UNMEASURED:'gate-unmeasured'};
  for(const section of readiness.sections||[]) {
    const card=document.createElement('div');card.className='readiness-group';
    const title=document.createElement('h4');title.textContent=section.label;card.append(title);
    for(const gate of section.gates||[]) {
      const row=document.createElement('div');row.className='readiness-gate';
      const name=document.createElement('div');name.textContent=gate.label;
      const badge=document.createElement('span');badge.className=`gate-pill ${cls[gate.status]||'gate-unmeasured'}`;
      badge.textContent=label[gate.status]||gate.status;
      const target=document.createElement('div');target.className='target';
      target.textContent=`Actual: ${gate.value??'—'} · objetivo: ${gate.target||'—'}`;
      row.append(name,badge,target);card.append(row);
    }
    groups.append(card);
  }
}

function renderValidation(data) {
  const tbody=$('validation-rows');tbody.replaceChildren();
  const statuses={PASS:'Pasa',FAIL:'Rechaza',INSUFFICIENT:'Muestra insuficiente',PENDING:'Pendiente',OBSERVED:'Observado'};
  const states={PAPER_LEGACY:'Paper · validación pendiente',PAPER_OBSERVATION:'Observación paper',
    VALIDATED:'Histórico validado · aplicación pendiente',REJECTED:'Rechazado',CANDIDATE:'Candidato'};
  const status=gate=>gate?.status?(statuses[gate.status]||gate.status):'Sin evidencia';
  const probability=gate=>Number.isFinite(gate?.probability)?`${pct(gate.probability)} · ${status(gate)}`:status(gate);
  for(const item of data.validation?.strategies||[]) {
    const v=item.validation||{},w=v.walk_forward;
    const values=[item.strategy,states[item.state]||item.state,status(v.leakage),
      w?`${w.positive_folds}/${w.folds} positivas · ${status(w)}`:'Sin evidencia',
      probability(v.dsr),probability(v.pbo),status(v.costs),status(v.bootstrap),status(v.paper)];
    const row=tbody.insertRow();values.forEach(value=>row.insertCell().textContent=value);
  }
  const latest=data.optimizer?.last_assessment, v=latest?.validation;
  $('validation-detail').textContent=v?.version?
    `${v.version} · ${v.start} → ${v.end} · ${v.dsr?.n_obs??'—'} días · motivo: ${latest.reason||'—'}`:
    'Las configuraciones existentes siguen en paper. Aún no hay una evaluación con los nuevos controles.';
}

function renderRows(data) {
  const tbody=$('trade-rows');tbody.replaceChildren();
  const trades=Array.isArray(data.trades)?data.trades:[];
  const eventRows=Array.isArray(data.events)?data.events:[];
  if (!trades.length) {
    const row=tbody.insertRow(),cell=row.insertCell();cell.colSpan=6;cell.className='empty';cell.textContent='Aún no hay operaciones cerradas';
  } else for (const trade of trades.slice(0,12)) {
    const row=tbody.insertRow();
    for (const value of [utc(trade.closed_ms),trade.asset,trade.strategy||'hermes_core',trade.regime,trade.reason,`${trade.pnl>=0?'+':''}${Number(trade.pnl).toFixed(4)} USDT`]) {
      const cell=row.insertCell();cell.textContent=value;
      if (cell.cellIndex===5) cell.className=trade.pnl>=0?'positive':'negative';
    }
  }
  const events=$('events');events.replaceChildren();
  if (!eventRows.length) {const p=document.createElement('p');p.className='empty';p.textContent='Sin eventos todavía';events.append(p);return;}
  for (const event of eventRows.slice(0,7)) {
    const row=document.createElement('div');row.className='row';
    const label=document.createElement('span');label.textContent=`${utc(event.ts)} · ${event.event}`;
    const value=document.createElement('strong');value.textContent=event.reason||event.asset||event.error||'—';
    row.append(label,value);events.append(row);
  }
}

function render(data) {
  if (!data||!data.ready) { $('status').textContent=data?.message||'Esperando datos';$('status-dot').className='dot warn';return; }
  const m=data.metrics||{}, age=Date.now()-Number(data.updated_at||0)*1000, halted=m.halted;
  const market=m.market_data||{}, waiting=market.status==='waiting', problems=market.problems||[];
  $('status').textContent=halted?`Pausado: ${halted}`:waiting?'Esperando publicación de velas':age>300000?'Datos sin actualizar':'Paper en observación';
  $('status-dot').className=halted?'dot bad':waiting||age>300000?'dot warn':'dot';
  const banner=$('banner');banner.style.display=halted||waiting?'block':'none';
  const detail=problems.map(p=>`${p.asset}${p.source?` (${p.source})`:''}: ${p.reason}${p.first_missing_bar?` desde ${utc(p.first_missing_bar)} UTC`:''}`).join('; ');
  const dataPause=['market_data_gap','stale_or_unsynchronized_market_data','consecutive_data_errors','missing_current_quote'].includes(halted);
  banner.textContent=waiting&&!halted?'Esperando velas completas para todos los activos. Se comprobarán de nuevo en el siguiente ciclo.':
    halted?`Entradas detenidas por ${halted}. ${dataPause?'Recuperación automática tras verificar velas, posiciones y Alpaca paper.':'Revise el estado persistente antes de reanudar.'}${detail?` ${detail}.`:''}${market.recovery?` Reconciliación: ${market.recovery}.`:''}`:'';
  $('equity').textContent=money(m.equity);$('capital').textContent=`Capital inicial: ${money(data.initial_capital)}`;
  $('return').textContent=pct(m.realised_return);$('return').className=`value ${m.realised_return>=0?'positive':'negative'}`;
  $('drawdown').textContent=pct(m.max_drawdown);$('trades-count').textContent=m.n;
  $('win-rate').textContent=`Tasa de acierto: ${pct(m.win_rate)}`;
  const rawPositions=Array.isArray(data.positions)?data.positions:(data.position?[data.position]:[]);
  const positions=rawPositions.filter(p=>p&&typeof p.asset==='string');
  const maxPositions=Number(m.max_positions??data.risk?.max_positions??0);
  $('position').textContent=positions.length?`${positions.length} / ${maxPositions||'—'} · ${positions.map(p=>p.asset.replace('/USDT','')).join(', ')}`:'Sin posiciones';
  $('entry-stop').textContent=positions.length?positions.map(p=>`${p.asset.replace('/USDT','')} ${number(p.entry)}/${number(p.stop)}`).join(' · '):'—';
  $('target').textContent=positions.length?positions.map(p=>`${p.asset.replace('/USDT','')} ${number(p.target)}`).join(' · '):'—';
  const risk=data.risk||{}, initial=Math.max(Number(data.initial_capital)||0,0.000001), equity=Math.max(Number(m.equity)||0,0.000001);
  $('cash').textContent=money(m.cash);$('risk').textContent=pct(risk.risk_per_trade);
  $('global-risk').textContent=`${pct((Number(m.open_risk)||0)/initial)} usado / ${pct(risk.max_portfolio_risk)} máx.`;
  $('risk-usage').textContent=pct(m.risk_utilization);
  $('exposure').textContent=`${pct((Number(m.gross_exposure)||0)/equity)} usado / ${pct(risk.max_total_exposure)} máx.`;
  $('pf').textContent=m.profit_factor===null?'—':number(m.profit_factor);
  $('updated').textContent=`Actualizado: ${utc(data.updated_at*1000)} UTC`;
  const opt=data.optimizer||{};
  $('optimizer-status').textContent=!opt.enabled?'Desactivado':opt.running?'Evaluando histórico':opt.active_change?'Cambio en observación':'Activo · esperando ciclo';
  $('optimizer-next').textContent=opt.next_due_ms?`${utc(opt.next_due_ms)} UTC`:'—';
  $('optimizer-last').textContent=opt.last_decision?`${opt.last_decision.event||'evaluación'} · ${opt.last_decision.reason||opt.last_decision.change?.field||'—'}`:'Aún sin evaluación';
  $('optimizer-alpha').textContent=data.alpha?`RSI rango ${data.alpha.range_rsi} · RSI tendencia ${data.alpha.trend_rsi} · objetivo ${data.alpha.target_r}R · stop ${data.alpha.stop_atr} ATR`:'—';
  $('optimizer-trials').textContent=String(opt.trial_count??0)+(opt.legacy_trial_count_is_lower_bound?' · historial previo parcial':'');
  currentV2=data;
  const sections=[
    ['comparación',()=>renderComparison()],
    ['curva',()=>drawCurve(Array.isArray(data.curve)?data.curve:[])],
    ['activos',()=>renderAssets(data)],
    ['resultado por moneda',()=>renderAssetPerformance(data)],
    ['estrategias',()=>renderStrategies(data)],
    ['preparación live',()=>renderReadiness(data)],
    ['validación',()=>renderValidation(data)],
    ['historial',()=>renderRows(data)],
  ];
  const failures=[];
  for (const [name,fn] of sections) {
    try { fn(); } catch (error) { failures.push(`${name}: ${error?.message||error}`); console.error(error); }
  }
  if (failures.length) {
    banner.style.display='block';
    banner.textContent=`Estado conectado, pero hubo un error visual: ${failures.join(' · ')}`;
  }
}

async function refresh() {
  let data;
  try {
    const response=await fetch('/api/state',{cache:'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    data=await response.json();
  } catch (error) {
    $('status').textContent=`Sin conexión al estado: ${error?.message||error}`;$('status-dot').className='dot bad';
    return;
  }
  try {
    render(data);
  } catch (error) {
    console.error(error);
    $('status').textContent='Estado conectado · error de visualización';$('status-dot').className='dot warn';
    const banner=$('banner');banner.style.display='block';banner.textContent=`Error de visualización: ${error?.message||error}`;
  }
}
refresh();setInterval(refresh,30000);

const manualMoney = value => Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} USD` : '—';
const manualUnits = value => Number(value).toFixed(12).replace(/\.?0+$/,'');
let manualAccount=null, manualQuote=null, manualBusy=false;
const paperMode=()=>$('manual-mode').value==='alpaca';
const manualStateUrl=()=>paperMode()?'/api/alpaca/manual/state':'/api/manual/state';
const selectedManualAsset=()=>$('manual-asset').value==='__stock__'
  ? $('manual-custom-ticker').value.trim().toUpperCase():$('manual-asset').value;
const manualCanTrade=()=>Boolean(manualQuote?.tradable&&(!paperMode()||manualQuote.broker_available));

function manualRow(tbody, values) {
  const row=tbody.insertRow();
  for (const value of values) row.insertCell().textContent=value;
}

function renderManual(account) {
  manualAccount=account;
  $('manual-cash').textContent=manualMoney(account.cash);
  $('manual-equity').textContent=manualMoney(account.equity);
  const positions=$('manual-positions');positions.replaceChildren();
  const entries=Object.entries(account.positions);
  if (!entries.length) manualRow(positions,['Sin posiciones','—','—','—','—']);
  for (const [asset,pos] of entries) {
    const mark=account.marks[asset];
    const value=pos.qty*(mark?.price??pos.cost/pos.qty);
    manualRow(positions,[asset,manualUnits(pos.qty),manualMoney(pos.cost),manualMoney(value),
      `${manualMoney(value-pos.cost)} · ${mark?utc(mark.asof*1000)+' UTC':'sin cotización'}`]);
  }
  const orders=$('manual-orders');orders.replaceChildren();
  if (!account.orders.length) manualRow(orders,['Sin órdenes','—','—','—','—']);
  for (const order of account.orders.slice(-12).reverse())
    manualRow(orders,[utc(order.ts*1000),order.side==='buy'?'Compra':'Venta',order.asset,
      order.qty?`${manualUnits(order.qty)} × ${number(order.price)}`:(order.status||'pendiente'),
      order.pnl===null?(order.status||'—'):manualMoney(order.pnl)]);
}

async function loadManual() {
  const url=manualStateUrl();
  try {
    const response=await fetch(url,{cache:'no-store'});
    const data=await response.json();
    if (url!==manualStateUrl()) return;
    if (!response.ok) throw new Error(data.error||`HTTP ${response.status}`);
    if (!data.ready && paperMode()) {
      manualAccount=null;$('manual-cash').textContent='—';$('manual-equity').textContent='—';
      $('manual-submit').disabled=true;
      $('manual-message').textContent=data.message||'Alpaca paper pendiente de configuración';return;
    }
    renderManual(data);
    $('manual-submit').disabled=manualBusy||!manualCanTrade()||Boolean(data.pending||data.halted);
    if (data.halted) $('manual-message').textContent=`Alpaca paper detenido: ${data.halted}`;
    if (data.pending) $('manual-message').textContent='Orden pendiente de ejecución o reconciliación en Alpaca.';
  } catch (error) {manualAccount=null;$('manual-submit').disabled=true;
    $('manual-message').textContent=`Cuenta manual: ${error.message}`;}
}

async function loadQuote(asset=selectedManualAsset()) {
  manualQuote=null;$('manual-submit').disabled=true;
  if (!asset || !/^(?:[A-Z]{1,6}(?:[.-][A-Z])?|[A-Z]{2,5}\/USDT)$/.test(asset)) {
    $('manual-quote').textContent='Escribe un ticker de acción válido, por ejemplo DIS o BRK.B.';return;
  }
  const broker=paperMode();
  $('manual-quote').textContent=`Consultando ${asset}…`;
  try {
    const response=await fetch(`/api/manual/quote?asset=${encodeURIComponent(asset)}`,{cache:'no-store'});
    const data=await response.json();
    if (asset!==selectedManualAsset()||broker!==paperMode()) return;
    if (!response.ok) throw new Error(data.error||`HTTP ${response.status}`);
    if (broker) {
      const check=await fetch(`/api/alpaca/manual/asset?asset=${encodeURIComponent(asset)}`,{cache:'no-store'});
      const availability=await check.json();
      if (asset!==selectedManualAsset()||broker!==paperMode()) return;
      if (!check.ok) throw new Error(availability.error||`Alpaca HTTP ${check.status}`);
      data.broker_available=availability.available;
      data.broker_reason=availability.reason;
    }
    manualQuote=data;
    $('manual-quote').textContent=`${asset}: ${manualMoney(data.price)} · ${data.source} · ${utc(data.asof*1000)} UTC · ${!data.tradable?'Mercado cerrado o dato antiguo':broker&&!data.broker_available?data.broker_reason:'Órdenes paper disponibles'}`;
    $('manual-submit').disabled=!manualCanTrade()||manualBusy||!manualAccount||Boolean(manualAccount.pending||manualAccount.halted);
    await loadManual();
  } catch (error) {
    if (asset===selectedManualAsset()&&broker===paperMode()) $('manual-quote').textContent=`Activo no disponible: ${error.message}`;
  }
}

$('manual-asset').addEventListener('change',()=>{
  $('manual-custom-label').hidden=$('manual-asset').value!=='__stock__';
  $('manual-custom-ticker').value='';loadQuote();
});
$('manual-custom-ticker').addEventListener('change',()=>loadQuote());
$('manual-mode').addEventListener('change',()=>{
  manualAccount=null;$('manual-message').textContent='';
  $('manual-rules').textContent=paperMode()
    ? 'Alpaca paper compartida: cripto mínima 10 USD, acciones y ETF fraccionarios desde 5 USD. El activo debe estar negociable en Alpaca; las acciones operan en horario regular de Nueva York. Se reserva 2 % del efectivo común. Solo puedes vender unidades manuales.'
    : 'Simulación local: compra mínima 5 USD. Las acciones y ETF operan en horario regular de Nueva York; las criptomonedas requieren cotización reciente. Solo puedes vender unidades propias.';
  loadManual();loadQuote();
});
$('manual-side').addEventListener('change',()=>{
  $('manual-amount-label').firstChild.textContent=$('manual-side').value==='buy'?'Monto total en USD':'Cantidad de unidades a vender';
  $('manual-amount').value='';
});
$('manual-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const asset=selectedManualAsset(),side=$('manual-side').value;
  const amount=Number($('manual-amount').value);
  if (!manualAccount||manualAccount.pending||manualAccount.halted||!manualCanTrade()||manualQuote.asset!==asset||manualBusy||!Number.isFinite(amount)||amount<=0) return;
  const broker=paperMode(),minimum=broker&&asset.includes('/')?10:5;
  if (side==='buy'&&(amount<minimum||amount>manualAccount.cash*(broker ? .98 : 1)+1e-8)) {
    $('manual-message').textContent=`Compra mínima ${minimum} USD; comprueba el saldo disponible.`;return;
  }
  if (side==='sell'&&amount>(manualAccount.positions[asset]?.qty??0)+1e-10) {
    $('manual-message').textContent='No tienes suficientes unidades para vender.';return;
  }
  const unit=side==='buy'?`USD del saldo ${broker?'compartido':'manual'}`:'unidades';
  const message=`¿${broker?'Enviar a Alpaca paper':'Registrar'} ${side==='buy'?'COMPRA':'VENTA'} de ${amount} ${unit} de ${asset}?\nCotización orientativa ${manualMoney(manualQuote.price)} (${manualQuote.source}). ${broker?'Alpaca determinará el fill de mercado, que puede diferir de esta cotización.':'El simulador aplicará costos.'} No moverá dinero real.`;
  if (!window.confirm(message)) return;
  manualBusy=true;$('manual-submit').disabled=true;
  $('manual-message').textContent='Registrando orden paper…';
  try {
    const id=crypto.randomUUID().replaceAll('-','');
    const response=await fetch(broker?'/api/alpaca/manual/order':'/api/manual/order',{method:'POST',headers:{'Content-Type':'application/json','X-Hermes-Action':'manual-paper'},
      body:JSON.stringify({id,asset,side,amount})});
    const data=await response.json();
    if (!response.ok) throw new Error(data.error||`HTTP ${response.status}`);
    const order=data.order;
    $('manual-message').textContent=broker
      ? `Alpaca paper: orden ${order.status||'enviada'} (${order.id}). ${order.qty?manualUnits(order.qty)+' unidades ejecutadas':'Consulta el historial para confirmar el fill'}.`
      : `Orden local registrada: ${side==='buy'?'compra':'venta'} ${manualUnits(order.qty)} ${asset} a ${manualMoney(order.price)}. Comisión simulada: ${manualMoney(order.fee)}.`;
    $('manual-amount').value='';
    await loadManual();
  } catch (error) {
    $('manual-message').textContent=`Orden no confirmada: ${error.message}. Consulta el historial antes de repetirla.`;
    await loadManual();
  } finally {
    manualBusy=false;await loadQuote(asset);
  }
});

loadManual();loadQuote();
async function loadAutoPaper() {
  try {
    const response=await fetch('/api/alpaca/auto/state',{cache:'no-store'}),data=await response.json();
    if (!response.ok) throw new Error(data.error||`HTTP ${response.status}`);
    $('alpaca-auto-status').textContent=data.ready?(data.halted?`Detenido: ${data.halted}`:
      data.pending?'Orden pendiente':Array.isArray(data.skipped_assets)&&data.skipped_assets.length?`Omitiendo ${data.skipped_assets.join(', ')} manual`:
      data.cursor===null?'Esperando cartera interna sin posición':`Conectado · cuenta ••••${data.account_suffix}`):data.message;
    $('alpaca-auto-balance').textContent=data.ready?`${manualMoney(data.equity)} / ${manualMoney(data.cash)}`:'—';
    $('alpaca-auto-positions').textContent=data.ready?(data.positions.map(p=>`${p.asset} ${manualUnits(p.qty)}`).join(', ')||'Sin posiciones'):'—';
    $('alpaca-auto-last').textContent=data.last_order?`${data.last_order.side} ${data.last_order.asset} · ${data.last_order.status}`:'—';
  } catch (error) {$('alpaca-auto-status').textContent=`Sin conexión: ${error.message}`;}
}
loadAutoPaper();setInterval(loadAutoPaper,60000);
setInterval(async()=>{
  if (manualBusy) return;
  await Promise.all([loadQuote(),...Object.keys(manualAccount?.positions||{}).filter(a=>a!==selectedManualAsset())
    .map(a=>fetch(`/api/manual/quote?asset=${encodeURIComponent(a)}`,{cache:'no-store'}).catch(()=>null))]);
  await loadManual();
},60000);
