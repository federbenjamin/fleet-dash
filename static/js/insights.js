// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{loadBudgets,budgetEditable,budgetValue,budgetForecastText,renderBudgetPanel,budgetTargetOptions,budgetSettingsHtml,editBudget,addBudget,removeBudget,saveBudgets,updateSpawnForecastDisplay,loadSpawnForecast,queueSpawnForecast,spawnForecastHtml,loadInsights,setInsightsDays,insTable,insFold,insDayLabel,insSpendChart,insBarList,insightsSection});
globalThis.budgetData={ok:true,budgets:[],forecasts:{},measurement_labels:{}};globalThis.budgetLoading=false;globalThis.budgetLoadedAt=0;
globalThis.budgetDraft=null;globalThis.spawnForecast=null;globalThis.spawnBudgetHeadroom=[];globalThis.spawnForecastTimer=null;
globalThis.spawnForecastAbort=null;globalThis.spawnForecastSequence=0;
async function loadBudgets(force=false,spawn=null){
  if(budgetLoading&&!spawn)return;
  if(!spawn&&!force&&Date.now()-budgetLoadedAt<10000)return;
  if(!spawn)budgetLoading=true;
  try{
    const query=spawn?('?'+new URLSearchParams(Object.entries(spawn).filter(([,value])=>value)).toString()):'';
    const r=await fetch('/api/budgets'+query,{cache:'no-store'}),data=await r.json();
    if(!r.ok||!data.ok)throw new Error(data.error||'Budgets unavailable');
    if(spawn){spawnForecast=data.spawn_forecast;spawnBudgetHeadroom=data.spawn_budgets||[];}
    else{budgetData=data;budgetLoadedAt=Date.now();if(budgetDraft===null)budgetDraft=(data.budgets||[]).map(budgetEditable);}
  }catch(error){if(spawn)spawnForecast={status:'error',error:String(error)};else budgetData={...budgetData,ok:false,error:String(error)};}
  finally{if(!spawn)budgetLoading=false;renderBudgetPanel();if(settingsOpen&&settingsSection==='budgets')renderSettings();if(newOpen)render(last,true);}
}
function budgetEditable(item){return{id:item.id,scope_type:item.scope_type,scope_id:item.scope_id||'',metric:item.metric,
  limit_value:item.limit_value,block_spawns:!!item.block_spawns,enabled:item.enabled!==false,label:item.label||''};}
function budgetValue(item){
  if(item.value==null)return'Unavailable';
  if(item.metric==='usd')return fmt$(item.value);
  if(item.metric==='tokens')return`${fmtTok(item.value)} tokens`;
  if(item.metric==='runtime')return`${Math.round(item.value/3600*10)/10} h`;
  return`${Math.round(item.value)} concurrent`;
}
function budgetForecastText(item){const f=(budgetData.forecasts||{})[item.id]||{};
  if(f.status!=='forecast')return`Forecast: not enough history · ${f.sample_size||0} samples`;
  const eta=f.seconds_to_limit==null?'unknown':f.seconds_to_limit<86400?`${Math.max(1,Math.round(f.seconds_to_limit/3600))} h`:`${Math.round(f.seconds_to_limit/86400)} d`;
  return`Forecast: limit in ${eta} · ${f.confidence} confidence · ${f.sample_size} samples`;}
function renderBudgetPanel(){
  const el=$('#budgets');if(!el)return;
  if(budgetLoading&&!budgetData.budgets?.length){setHtml(el,'<div class="ctxload">Measuring budgets…</div>');return;}
  if(!budgetData.ok){setHtml(el,`<div class="provideralert"><b>Budgets unavailable</b> — ${esc(budgetData.error||'failed')}</div>`);return;}
  const items=budgetData.budgets||[];
  if(!items.length){setHtml(el,'<section class="budgetpanel emptybudget"><b>No budgets configured</b><span>Budgets alert only unless you explicitly enable “block future spawns.” Configure them in Settings.</span><button onclick="navigateTo(\'settings\')">Open Settings</button></section>');return;}
  setHtml(el,`<section class="budgetpanel"><div class="budgetpanelhead"><span><b>Budgets</b><small>Cumulative local usage; concurrency is current</small></span><button onclick="navigateTo('settings')">Manage</button></div>
    <div class="budgetcards">${items.map(item=>`<article class="budgetcard ${esc(item.status)}"><div><b>${esc(item.label)}</b><small>${esc(item.scope_type)}${item.scope_id?` · ${esc(item.scope_id)}`:''}</small></div>
      <strong>${esc(budgetValue(item))} <small>of ${item.metric==='usd'?fmt$(item.limit_value):item.metric==='tokens'?fmtTok(item.limit_value):Math.round(item.limit_value)}</small></strong>
      <span class="budgetmeasure">${esc(String(item.measurement_scope||'unavailable').replaceAll('_',' '))}${item.block_spawns?' · blocks future spawns':''}</span>
      <div class="budgetbar"><i style="width:${Math.round(Math.min(1,item.ratio||0)*100)}%"></i></div><p>${esc(budgetForecastText(item))}</p></article>`).join('')}</div></section>`);
}
function budgetTargetOptions(item){
  if(item.scope_type==='fleet')return'';
  if(item.scope_type==='provider')return[['claude','Claude Code'],['codex','Codex CLI']];
  if(item.scope_type==='session')return((last&&last.sessions)||[]).map(s=>[s.session_id,`${s.provider} · ${s.title||s.project}`]);
  return(workstreamData.workstreams||[]).map(w=>[w.workstream_id,w.title||w.root]);
}
function budgetSettingsHtml(){
  if(budgetDraft===null)return'<div class="ctxload">Loading budgets…</div>';
  const rows=budgetDraft.map((item,index)=>{const targets=budgetTargetOptions(item);
    return`<div class="budgetedit"><div class="budgeteditrow"><select onchange="editBudget(${index},'scope_type',this.value)">${[['fleet','Fleet'],['provider','Provider'],['workstream','Workstream'],['session','Session']].map(([v,l])=>`<option value="${v}" ${item.scope_type===v?'selected':''}>${l}</option>`).join('')}</select>
      ${targets?`<select onchange="editBudget(${index},'scope_id',this.value)"><option value="">Choose target</option>${targets.map(([v,l])=>`<option value="${esc(v)}" ${item.scope_id===v?'selected':''}>${esc(l)}</option>`).join('')}</select>`:'<span class="budgettarget">All providers and sessions</span>'}
      <button aria-label="remove budget" onclick="removeBudget(${index})">✕</button></div>
      <div class="budgeteditrow"><select onchange="editBudget(${index},'metric',this.value)">${[['usd','Exact USD'],['tokens','Tokens'],['runtime','Runtime seconds'],['concurrency','Concurrency']].map(([v,l])=>`<option value="${v}" ${item.metric===v?'selected':''}>${l}</option>`).join('')}</select>
      <input type="number" min="0.01" step="${item.metric==='usd'?'0.5':'1'}" value="${esc(String(item.limit_value))}" onchange="editBudget(${index},'limit_value',Number(this.value))">
      <label><input type="checkbox" ${item.block_spawns?'checked':''} onchange="editBudget(${index},'block_spawns',this.checked)"> block future spawns only</label></div></div>`;}).join('');
  return`<div class="budgetsettings"><div class="sethint">USD, tokens, and runtime use cumulative usage observed on this Mac. Concurrency is current. Codex stays token-only unless its protocol reports session currency. Active work is never interrupted.</div>${rows||'<div class="sethint">No budgets yet.</div>'}
    <div class="budgeteditactions"><button onclick="addBudget()">＋ Add budget</button><button class="primary" onclick="saveBudgets()">Save budgets</button></div></div>`;
}
function editBudget(index,key,value){if(!budgetDraft?.[index])return;budgetDraft[index][key]=value;
  if(key==='scope_type')budgetDraft[index].scope_id=value==='fleet'?'':value==='provider'?'claude':'';
  if(key==='scope_type'||key==='metric')renderSettings();}
function addBudget(){if(budgetDraft===null)budgetDraft=[];budgetDraft.push({scope_type:'fleet',scope_id:'',metric:'tokens',limit_value:1000000,block_spawns:false,enabled:true});renderSettings();}
function removeBudget(index){budgetDraft.splice(index,1);renderSettings();}
async function saveBudgets(){settingMessage('setmsg','saving budgets…');
  const data=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({budgets:budgetDraft})}).then(r=>r.json()).catch(error=>({ok:false,error:String(error)}));
  if(!data.ok){settingMessage('setmsg','✗ '+(data.error||'failed'));return;}
  budgetDraft=(data.budgets||[]).map(budgetEditable);settingMessage('setmsg','saved ✓');await loadBudgets(true);}
function updateSpawnForecastDisplay(){
  const current=document.querySelector('#newsess .spawnforecast');
  if(current)current.outerHTML=spawnForecastHtml();
}
async function loadSpawnForecast(spawn,sequence){
  const controller=new AbortController();spawnForecastAbort=controller;
  try{
    const query='?'+new URLSearchParams(Object.entries(spawn).filter(([,value])=>value)).toString();
    const response=await fetch('/api/budgets'+query,{cache:'no-store',signal:controller.signal});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'Forecast unavailable');
    if(sequence!==spawnForecastSequence)return;
    spawnForecast=data.spawn_forecast;spawnBudgetHeadroom=data.spawn_budgets||[];
  }catch(error){
    if(error.name==='AbortError'||sequence!==spawnForecastSequence)return;
    spawnForecast={status:'error',error:String(error.message||error)};
  }finally{
    if(sequence===spawnForecastSequence){spawnForecastAbort=null;updateSpawnForecastDisplay();}
  }
}
function queueSpawnForecast(delay=120){
  clearTimeout(spawnForecastTimer);spawnForecastTimer=null;
  if(spawnForecastAbort){spawnForecastAbort.abort();spawnForecastAbort=null;}
  const sequence=++spawnForecastSequence;
  spawnForecast={status:'loading'};spawnBudgetHeadroom=[];updateSpawnForecastDisplay();
  spawnForecastTimer=setTimeout(()=>{
    spawnForecastTimer=null;if(!newOpen||!newProvider)return;
    loadSpawnForecast({provider:newProvider,model:newModel,
      project:(newDir.split('/').filter(Boolean).pop()||''),cwd:newDir},sequence);
  },delay);
}
function spawnForecastHtml(){const f=spawnForecast;if(!f)return'<div class="spawnforecast">Forecast and budget headroom load from matching local history.</div>';
  if(f.status==='loading')return'<div class="spawnforecast loading"><span class="delivery sending" aria-hidden="true">◌</span> Updating forecast…</div>';
  if(f.status==='error')return`<div class="spawnforecast unavailable">${esc(f.error)}</div>`;
  if(f.status!=='forecast')return`<div class="spawnforecast">Not enough matching history · ${f.sample_size||0} sample${f.sample_size===1?'':'s'}</div>`;
  const bits=[f.median_usd!=null?`median ${fmt$(f.median_usd)}`:'currency unavailable',f.median_tokens!=null?`${fmtTok(f.median_tokens)} tokens`:null,
    f.median_runtime_seconds!=null?`${Math.round(f.median_runtime_seconds/60)} min`:null,`${f.confidence} confidence · ${f.sample_size} samples`].filter(Boolean);
  const headroom=spawnBudgetHeadroom.length?spawnBudgetHeadroom.map(item=>item.headroom==null?`${item.label}: unavailable`:
    `${item.label}: ${item.metric==='usd'?fmt$(item.headroom):item.metric==='tokens'?fmtTok(item.headroom)+' tokens':Math.round(item.headroom)} headroom${item.block_spawns&&item.status==='exceeded'?' · spawn blocked':''}`).join(' · '):'No matching budget';
  return`<div class="spawnforecast">Historical match: ${bits.map(esc).join(' · ')}<br>Budget: ${esc(headroom)}</div>`;}

globalThis.insightsDays=7;
const insightsCache={};   // days -> {t, data, fetching}
globalThis.insightsSequence=0;
function loadInsights(force){
  const days=insightsDays,c=insightsCache[days];
  if(!force&&c&&(c.fetching||(c.data&&Date.now()-c.t<60000)))return;
  const sequence=++insightsSequence;
  insightsCache[days]={...(c||{}),fetching:true,error:''};
  fetch('/api/insights?days='+days,{cache:'no-store'}).then(async r=>{
    const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'Insights unavailable');return d;
  }).then(d=>{
    insightsCache[days]={t:Date.now(),data:d,fetching:false,error:''};
    if(insightsDays===days&&sequence===insightsSequence)render(last,true);
  }).catch(error=>{
    insightsCache[days]={...(insightsCache[days]||{}),fetching:false,error:String(error.message||error)};
    if(insightsDays===days&&sequence===insightsSequence)render(last,true);
  });
}
function setInsightsDays(n){insightsDays=n;render(last,true);loadInsights();}
function insTable(heads,rows){
  if(!rows||!rows.length)return'<div class="setnum" style="padding:2px 8px 8px">no data in window</div>';
  return`<table><tr>${heads.map(h=>`<th${h[1]?' class="r"':''}>${h[0]}</th>`).join('')}</tr>${rows.join('')}</table>`;
}
const insOpen=new Set();   // per-subsection fold state, survives re-renders
function insFold(key,title,inner){
  return`<details class="insfold" ${insOpen.has(key)?'open':''} ontoggle="insOpen[this.open?'add':'delete']('${key}')">
    <summary>${title}</summary>${inner}</details>`;
}
const MONTHS=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
function insDayLabel(day,previous){
  const [,month,date]=String(day).split('-');
  const label=Number(date)===1||!previous||String(previous).slice(0,7)!==String(day).slice(0,7)?
    `${MONTHS[Number(month)-1]||''} ${Number(date)}`:String(Number(date));
  return label;
}
function insSpendChart(d){
  // agents = ledger $ per day; sessions = measured usage_stats $ per day.
  // These are different measures than the lifetime session totals above —
  // the legend says so instead of pretending they reconcile.
  const days={};
  (d.token_mix||[]).forEach(x=>{(days[x.day]=days[x.day]||{day:x.day,sessions:0,agents:0}).sessions=
    (x.input||0)+(x.write||0)+(x.read||0)+(x.output||0);});
  (d.by_day||[]).forEach(x=>{(days[x.day]=days[x.day]||{day:x.day,sessions:0,agents:0}).agents=x.cost||0;});
  const series=Object.values(days).sort((a,b)=>a.day<b.day?-1:1);
  if(!series.length)return`<div class="inscard inschart"><span class="inslabel">$ BY DAY</span>
    <div class="settingsempty">No measured spend in this window.</div></div>`;
  const peak=Math.max(...series.map(x=>x.sessions+x.agents),0.01);
  const every=Math.max(1,Math.ceil(series.length/10));
  return`<div class="inscard inschart"><div class="inscharthead"><span class="inslabel">$ BY DAY</span>
      <span class="inslegend"><i class="agent"></i> agents · <i class="sess"></i> sessions (measured that day)</span></div>
    <div class="insbars">${series.map(x=>`<div class="insbarcol" title="${esc(x.day)} · agents ${fmt$(x.agents)} · sessions ${fmt$(x.sessions)}">
      <i class="agent" style="height:${Math.round(x.agents/peak*100)}%"></i>
      <i class="sess" style="height:${Math.round(x.sessions/peak*100)}%"></i></div>`).join('')}</div>
    <div class="insdays">${series.map((x,i)=>`<span>${i%every?'':esc(insDayLabel(x.day,series[i-1]&&series[i-1].day))}</span>`).join('')}</div></div>`;
}
function insBarList(rows){
  if(!rows.length)return'<div class="settingsempty">No data in window.</div>';
  const peak=Math.max(...rows.map(x=>x.value),0.01);
  return rows.map(x=>`<div class="insrow ${x.cls||''}"><div class="insrowhead"><span>${esc(x.name)}</span><span>${fmt$(x.value)}</span></div>
    <div class="insmeter"><i style="width:${Math.round(x.value/peak*100)}%"></i></div></div>`).join('');
}
function insightsSection(){
  const c=insightsCache[insightsDays],d=c&&c.data;
  let body='<div class="ctxload">crunching…</div>';
  if(d&&d.ok){
    const mixTot=(d.token_mix||[]).reduce((a,x)=>({input:a.input+x.input,write:a.write+x.write,
      read:a.read+x.read,output:a.output+x.output}),{input:0,write:0,read:0,output:0});
    const mixRow=x=>`<tr><td>${x.day}</td><td class="r">${x.input.toFixed(2)}</td><td class="r">${x.write.toFixed(2)}</td><td class="r">${x.read.toFixed(2)}</td><td class="r">${x.output.toFixed(2)}</td><td class="r"><b>${(x.input+x.write+x.read+x.output).toFixed(2)}</b></td></tr>`;
    const runs=(d.agents||[]).reduce((total,agent)=>total+(agent.runs||0),0);
    const topPeak=Math.max(...(d.top_sessions||[]).map(s=>s.cost||0),0.01);
    body=`<div class="inshead"><div class="insstats">
        <span><b>${fmt$(d.totals.agent_cost)}</b> agents</span>
        <span><b>${fmt$(d.totals.session_cost)}</b> sessions in window <small>lifetime $</small></span>
        <span class="bust"><b>~${fmt$(d.totals.bust_cost||0)}</b> cache busts re-paid</span>
        <span class="insruns">agent runs ${runs}</span></div>
      <div class="inswin">${[7,30,90].map(n=>`<button class="${insightsDays===n?'cur':''}" onclick="setInsightsDays(${n})">${n}D</button>`).join('')}</div></div>
      <div class="insgrid">${insSpendChart(d)}
        <div class="inscard"><span class="inslabel">BY MODEL <small>agents + sessions $</small></span>
          ${insBarList((d.models||[]).slice(0,6).map(m=>({name:m.name,value:(m.agents||0)+(m.sessions||0),cls:'model'})))}
          <span class="inslabel skills">BY SKILL</span>
          ${insBarList((d.skills||[]).slice(0,6).map(s=>({name:s.name,value:s.cost||0})))}</div></div>
      <div class="inscard instop"><span class="inslabel">TOP SESSIONS · LIFETIME $</span>
        ${(d.top_sessions||[]).length?(d.top_sessions||[]).map(s=>`<div class="instoprow"><b>${esc(s.title||'?')}</b><span>${esc(s.project||'')}</span>
          <div class="insmeter"><i style="width:${Math.round((s.cost||0)/topPeak*100)}%"></i></div><em>${fmt$(s.cost||0)}</em></div>`).join(''):'<div class="settingsempty">No sessions in window.</div>'}</div>`
      +insFold('cache',`cache invalidations — est ${fmt$(d.totals.bust_cost||0)} re-paid in window`,
        insTable([['cause'],['events',1],['tokens re-paid',1],['est $',1]],
          (d.cache_busts||[]).map(x=>`<tr><td>${esc(x.name)}</td><td class="r">${x.events}</td><td class="r">${fmtTok(x.tokens)}</td><td class="r">${x.cost.toFixed(2)}</td></tr>`)))
      +insFold('mix','$ by token class per day — where the money actually goes',
        insTable([['day'],['uncached $',1],['cache write $',1],['cache read $',1],['output $',1],['total',1]],
          (d.token_mix||[]).length?[mixRow({day:'window',...mixTot})].concat(d.token_mix.map(mixRow)):[]))
      +insFold('agents','by agent type',
        insTable([['agent'],['runs',1],['total $',1],['avg $',1],['cache hit',1]],
          d.agents.map(a=>`<tr><td>${esc(a.name)}</td><td class="r">${a.runs}</td><td class="r">${a.cost.toFixed(2)}</td><td class="r">${a.avg.toFixed(3)}</td><td class="r">${a.cache_pct}%</td></tr>`)))
      +insFold('skills','by skill — attributed cost of turns run while the skill was active',
        insTable([['skill'],['uses',1],['total $',1],['avg $/use',1]],
          d.skills.map(s=>`<tr><td>${esc(s.name)}</td><td class="r">${s.uses}</td><td class="r">${s.cost.toFixed(2)}</td><td class="r">${s.avg.toFixed(3)}</td></tr>`)))
      +insFold('tools','by tool — context injected by tool results (est tokens)',
        insTable([['tool'],['uses',1],['tokens in',1],['avg/use',1]],
          d.tools.map(x=>`<tr><td>${esc(x.name)}</td><td class="r">${x.uses}</td><td class="r">${fmtTok(x.tokens)}</td><td class="r">${fmtTok(x.avg_tokens)}</td></tr>`)))
      +insFold('models','by model — agents vs sessions split',
        insTable([['model'],['agents $',1],['sessions $',1]],
          d.models.map(m=>`<tr><td>${esc(m.name)}</td><td class="r">${m.agents.toFixed(2)}</td><td class="r">${m.sessions.toFixed(2)}</td></tr>`)))
      +insFold('projects','by project (sessions active in window; lifetime $)',
        insTable([['project'],['agents $',1],['sessions $',1]],
          d.projects.map(p=>`<tr><td>${esc(p.name)}</td><td class="r">${p.agents.toFixed(2)}</td><td class="r">${p.sessions.toFixed(2)}</td></tr>`)));
  }else if(c?.error){body=`<div class="ctxload" role="alert">✗ ${esc(c.error)} <button onclick="loadInsights(true)">retry</button></div>`;}
  else if(d&&!d.ok){body=`<div class="ctxload">✗ ${esc(d.error||'failed')}</div>`;}
  return`<div class="insightspanel">${body}</div>`;
}


Object.assign(globalThis,{insightsCache,insOpen});
