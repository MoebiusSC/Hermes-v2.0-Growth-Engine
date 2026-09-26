const $ = id => document.getElementById(id);
const money = value => Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} USDT` : '—';
const pct = value => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(2)}%` : '—';
const utc = ms => new Date(ms).toLocaleString('es-BO', {timeZone:'UTC',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
const number = value => Number.isFinite(Number(value)) ? Number(value).toLocaleString('es-BO',{maximumFractionDigits:4}) : '—';

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
