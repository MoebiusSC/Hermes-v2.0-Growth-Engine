const $ = id => document.getElementById(id);
const money = value => Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} USDT` : '—';
const pct = value => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(2)}%` : '—';
const utc = ms => new Date(ms).toLocaleString('es-BO', {timeZone:'UTC',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
const number = value => Number.isFinite(Number(value)) ? Number(value).toLocaleString('es-BO',{maximumFractionDigits:4}) : '—';
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
  const x = i => 10 + i * 680 / Math.max(points.length - 1, 1);
  const y = value => 215 - (value - min) * 185 / Math.max(max - min, 0.000001);
  const values = points.map(p => Number(p.equity));
  const d = values.map((v,i) => `${i ? 'L' : 'M'}${x(i).toFixed(2)} ${y(v).toFixed(2)}`).join(' ');
  const area = document.createElementNS(ns,'path');
  area.setAttribute('d', `${d} L${x(values.length-1)} 225 L10 225 Z`);
  area.setAttribute('fill',color === '#70e1cd' ? '#70e1cd13' : '#ff879213');
  const path = document.createElementNS(ns,'path');
  path.setAttribute('d',d); path.setAttribute('stroke',color); path.setAttribute('stroke-width','2.5');
  path.setAttribute('stroke-linecap','round'); path.setAttribute('stroke-linejoin','round'); path.setAttribute('fill','none');
  svg.append(area,path);
  for (const v of [min,(min+max)/2,max]) {
    const grid = document.createElementNS(ns,'line'); const yy = y(v);
    grid.setAttribute('x1','10'); grid.setAttribute('x2','690'); grid.setAttribute('y1',yy); grid.setAttribute('y2',yy);
    grid.setAttribute('stroke','#263746'); grid.setAttribute('stroke-dasharray','3 6'); svg.insertBefore(grid,area);
  }
}

function drawCurve(points) {
  const svg=$('chart'); svg.replaceChildren();
  const clean=points.filter(p => Number.isFinite(Number(p.equity)));
  if (clean.length < 2) {
    const label=document.createElementNS('http://www.w3.org/2000/svg','text');
    label.setAttribute('x','20');label.setAttribute('y','120');label.setAttribute('fill','#92a8b4');
    label.textContent='La curva aparecerá después del primer ciclo';svg.append(label);return;
  }
  const values=clean.map(p=>Number(p.equity)), low=Math.min(...values), high=Math.max(...values);
  const pad=Math.max((high-low)*.12,0.03);
  line(svg,clean,low-pad,high+pad,values.at(-1)>=values[0]?'#70e1cd':'#ff8792');
  $('chart-start').textContent=utc(Date.parse(clean[0].ts));
  $('chart-end').textContent=utc(Date.parse(clean.at(-1).ts));
  $('chart-range').textContent=`${number(low)} – ${number(high)} USDT`;
}

function renderRows(data) {
  const tbody=$('trade-rows');tbody.replaceChildren();
  if (!data.trades.length) {
    const row=tbody.insertRow(),cell=row.insertCell();cell.colSpan=5;cell.className='empty';cell.textContent='Aún no hay operaciones cerradas';
  } else for (const trade of data.trades.slice(0,12)) {
    const row=tbody.insertRow();
    for (const value of [utc(trade.closed_ms),trade.asset,trade.regime,trade.reason,`${trade.pnl>=0?'+':''}${Number(trade.pnl).toFixed(4)} USDT`]) {
      const cell=row.insertCell();cell.textContent=value;
      if (cell.cellIndex===4) cell.className=trade.pnl>=0?'positive':'negative';
    }
  }
  const events=$('events');events.replaceChildren();
  if (!data.events.length) {const p=document.createElement('p');p.className='empty';p.textContent='Sin eventos todavía';events.append(p);return;}
  for (const event of data.events.slice(0,7)) {
    const row=document.createElement('div');row.className='row';
    const label=document.createElement('span');label.textContent=`${utc(event.ts)} · ${event.event}`;
    const value=document.createElement('strong');value.textContent=event.reason||event.asset||event.error||'—';
    row.append(label,value);events.append(row);
  }
}

function render(data) {
  if (!data.ready) { $('status').textContent=data.message||'Esperando datos';$('status-dot').className='dot warn';return; }
  const m=data.metrics, age=Date.now()-data.updated_at*1000, halted=m.halted;
  $('status').textContent=halted?`Pausado: ${halted}`:age>300000?'Datos sin actualizar':'Paper en observación';
  $('status-dot').className=halted?'dot bad':age>300000?'dot warn':'dot';
  const banner=$('banner');banner.style.display=halted?'block':'none';
  banner.textContent=halted?`Entradas detenidas por ${halted}. Revise los datos y el estado persistente antes de reanudar.`:'';
  $('equity').textContent=money(m.equity);$('capital').textContent=`Capital inicial: ${money(data.initial_capital)}`;
  $('return').textContent=pct(m.realised_return);$('return').className=`value ${m.realised_return>=0?'positive':'negative'}`;
  $('drawdown').textContent=pct(m.max_drawdown);$('trades-count').textContent=m.n;
  $('win-rate').textContent=`Tasa de acierto: ${pct(m.win_rate)}`;
  $('position').textContent=data.position?`${data.position.asset} · ${data.position.regime}`:'Sin posición';
  $('entry-stop').textContent=data.position?`${number(data.position.entry)} / ${number(data.position.stop)}`:'—';
  $('target').textContent=data.position?`${number(data.position.target)} USDT`:'—';
  $('cash').textContent=money(m.cash);$('risk').textContent=pct(data.risk.risk_per_trade);
  $('exposure').textContent=pct(data.risk.max_exposure);$('pf').textContent=m.profit_factor===null?'—':number(m.profit_factor);
  $('updated').textContent=`Actualizado: ${utc(data.updated_at*1000)} UTC`;
  const opt=data.optimizer||{};
  $('optimizer-status').textContent=!opt.enabled?'Desactivado':opt.running?'Evaluando histórico':opt.active_change?'Cambio en observación':'Activo · esperando ciclo';
  $('optimizer-next').textContent=opt.next_due_ms?`${utc(opt.next_due_ms)} UTC`:'—';
  $('optimizer-last').textContent=opt.last_decision?`${opt.last_decision.event||'evaluación'} · ${opt.last_decision.reason||opt.last_decision.change?.field||'—'}`:'Aún sin evaluación';
  $('optimizer-alpha').textContent=data.alpha?`RSI rango ${data.alpha.range_rsi} · RSI tendencia ${data.alpha.trend_rsi} · objetivo ${data.alpha.target_r}R · stop ${data.alpha.stop_atr} ATR`:'—';
  currentV2=data;renderComparison();
  drawCurve(data.curve);renderRows(data);
}

async function refresh() {
  try {
    const response=await fetch('/api/state',{cache:'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    $('status').textContent='Sin conexión al estado';$('status-dot').className='dot bad';
  }
}
refresh();setInterval(refresh,30000);

const manualMoney = value => Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} USD` : '—';
const manualUnits = value => Number(value).toFixed(12).replace(/\.?0+$/,'');
let manualAccount=null, manualQuote=null, manualBusy=false;

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
      `${manualUnits(order.qty)} × ${number(order.price)}`,order.pnl===null?'—':manualMoney(order.pnl)]);
}

async function loadManual() {
  try {
    const response=await fetch('/api/manual/state',{cache:'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderManual(await response.json());
  } catch (error) { $('manual-message').textContent=`No se pudo cargar la cuenta manual (${error.message}).`; }
}

async function loadQuote(asset=$('manual-asset').value) {
  manualQuote=null;$('manual-submit').disabled=true;
  $('manual-quote').textContent=`Consultando ${asset}…`;
  try {
    const response=await fetch(`/api/manual/quote?asset=${encodeURIComponent(asset)}`,{cache:'no-store'});
    const data=await response.json();
    if (asset!==$('manual-asset').value) return;
    if (!response.ok) throw new Error(data.error||`HTTP ${response.status}`);
    manualQuote=data;
    $('manual-quote').textContent=`${asset}: ${manualMoney(data.price)} · ${data.source} · ${utc(data.asof*1000)} UTC · ${data.tradable?'Órdenes paper disponibles':'Mercado cerrado o dato antiguo'}`;
    $('manual-submit').disabled=!data.tradable||manualBusy;
    await loadManual();
  } catch (error) {
    if (asset===$('manual-asset').value) $('manual-quote').textContent=`Cotización no disponible: ${error.message}`;
  }
}

$('manual-asset').addEventListener('change',()=>loadQuote());
$('manual-side').addEventListener('change',()=>{
  $('manual-amount-label').firstChild.textContent=$('manual-side').value==='buy'?'Monto total en USD':'Cantidad de unidades a vender';
  $('manual-amount').value='';
});
$('manual-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const asset=$('manual-asset').value,side=$('manual-side').value;
  const amount=Number($('manual-amount').value);
  if (!manualAccount||!manualQuote?.tradable||manualQuote.asset!==asset||manualBusy||!Number.isFinite(amount)||amount<=0) return;
  if (side==='buy'&&(amount<5||amount>manualAccount.cash+1e-8)) {
    $('manual-message').textContent='Compra mínima 5 USD; comprueba el saldo.';return;
  }
  if (side==='sell'&&amount>(manualAccount.positions[asset]?.qty??0)+1e-10) {
    $('manual-message').textContent='No tienes suficientes unidades para vender.';return;
  }
  const unit=side==='buy'?'USD de tu saldo manual':'unidades';
  const message=`¿Registrar ${side==='buy'?'COMPRA':'VENTA'} simulada de ${amount} ${unit} de ${asset}?\nCotización de referencia ${manualMoney(manualQuote.price)} (${manualQuote.source}). El servidor actualizará el precio y aplicará costos antes de registrar la orden. No moverá dinero real.`;
  if (!window.confirm(message)) return;
  manualBusy=true;$('manual-submit').disabled=true;
  $('manual-message').textContent='Registrando orden paper…';
  try {
    const id=crypto.randomUUID().replaceAll('-','');
    const response=await fetch('/api/manual/order',{method:'POST',headers:{'Content-Type':'application/json','X-Hermes-Action':'manual-paper'},
      body:JSON.stringify({id,asset,side,amount})});
    const data=await response.json();
    if (!response.ok) throw new Error(data.error||`HTTP ${response.status}`);
    const order=data.order;
    $('manual-message').textContent=`Orden paper registrada: ${side==='buy'?'compra':'venta'} ${manualUnits(order.qty)} ${asset} a ${manualMoney(order.price)}. Comisión simulada: ${manualMoney(order.fee)}.`;
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
setInterval(async()=>{
  if (manualBusy) return;
  await Promise.all([loadQuote(),...Object.keys(manualAccount?.positions||{}).filter(a=>a!==$('manual-asset').value)
    .map(a=>fetch(`/api/manual/quote?asset=${encodeURIComponent(a)}`,{cache:'no-store'}).catch(()=>null))]);
  await loadManual();
},60000);
