const $=q=>document.querySelector(q);
const fleetPerf=window.__fleetPerf={samples:{render_ms:[],poll_ms:[],poll_payload_bytes:[],
  input_feedback_ms:[]}};
function perfRecord(name,value){
  const list=fleetPerf.samples[name]||(fleetPerf.samples[name]=[]);
  if(Number.isFinite(value)){list.push(value);if(list.length>240)list.shift();}
}
function perfSummary(){
  const pct=(values,q)=>{const sorted=[...values].sort((a,b)=>a-b);
    return sorted.length?sorted[Math.min(sorted.length-1,Math.round((sorted.length-1)*q))]:0;};
  return Object.fromEntries(Object.entries(fleetPerf.samples).map(([name,values])=>[name,
    {count:values.length,p50:Math.round(pct(values,.5)*1000)/1000,
      p95:Math.round(pct(values,.95)*1000)/1000,last:values.at(-1)||0}]));
}
fleetPerf.summary=perfSummary;
function recordInputFeedback(started){
  perfRecord('input_feedback_ms',performance.now()-started);
}
(()=>{const m=location.search.match(/[?&]token=([0-9a-f]+)/);
  if(m){document.cookie=`act_token=${m[1]};path=/;max-age=31536000;SameSite=Lax`;
        history.replaceState(null,'',location.pathname+location.hash);}})();
const open=new Set();
const expandedPeeks=new Set();
const infoOpen=new Set(),doneOpen=new Set(),filesOpen=new Set(),stateInfoOpen=new Set();  // detail-panel fold state, survives re-renders
const routeNames={now:'Now',search:'Search',workstreams:'Workstreams',history:'History',insights:'Insights'};
const validRoutes=new Set(Object.keys(routeNames));
let currentRoute=validRoutes.has(location.hash.slice(1))?location.hash.slice(1):'now';
let nowFilter='',nowState='all',workFilter='',workState='all';
let workstreamData={ok:true,workstreams:[]},workstreamsLoading=false,workstreamsLoadedAt=0;
const SAVED_VIEW_KEY='fleet.savedViews.v1';
let savedViews=(()=>{try{
  const value=JSON.parse(localStorage.getItem(SAVED_VIEW_KEY)||'{}');
  return {now:Array.isArray(value.now)?value.now:[],
    workstreams:Array.isArray(value.workstreams)?value.workstreams:[]};
}catch(_){return {now:[],workstreams:[]};}})();
function closeMobileMore(){
  const menu=$('#mobilemore'),button=$('#bottomnav [data-route="more"]');
  if(menu)menu.classList.remove('open');
  if(button){button.classList.remove('active');button.setAttribute('aria-expanded','false');}
}
function toggleMobileMore(){
  const menu=$('#mobilemore'),button=$('#bottomnav [data-route="more"]');
  const opening=!menu.classList.contains('open');
  menu.classList.toggle('open',opening);
  button.classList.toggle('active',opening);
  button.setAttribute('aria-expanded',String(opening));
}
function applyRouteNav(route){
  document.querySelectorAll('[data-route]').forEach(button=>{
    const active=button.dataset.route===route||
      (button.dataset.route==='more'&&(route==='insights'||route==='settings'));
    button.classList.toggle('active',active);
    if(button.dataset.route!=='more'){
      if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
    }
  });
  const title=$('#mobiletitle');if(title)title.textContent=route==='settings'?'Settings':(routeNames[route]||'Now');
}
function navigateTo(route,push=true){
  closeMobileMore();
  if(route==='settings'){
    applyRouteNav('settings');
    openSettings();
    return;
  }
  if(!validRoutes.has(route))route='now';
  currentRoute=route;
  document.querySelectorAll('[data-destination]').forEach(section=>{section.hidden=section.dataset.destination!==route;});
  applyRouteNav(route);
  if(push&&location.hash!=='#'+route)history.pushState({fdRoute:route},'','#'+route);
  if(route==='insights'){loadInsights();loadBudgets();}
  if(route==='search'){loadSearchStatus(true);runSearch(true);}
  if(route==='workstreams')loadWorkstreams(true);
  if(route==='history')loadHistory(true);
  window.scrollTo({top:0,behavior:'auto'});
}
function setNowFilter(value){nowFilter=value;render(last,true);}
function setNowState(value){
  nowState=['all','needs_you','working','available'].includes(value)?value:'all';
  render(last,true);
}
async function loadWorkstreams(force=false){
  if(workstreamsLoading||(!force&&Date.now()-workstreamsLoadedAt<1800))return;
  workstreamsLoading=true;renderWorkstreams(workstreamData);
  try{
    const r=await fetch('/api/workstreams',{cache:'no-store'}),data=await r.json();
    if(!r.ok||!data.ok)throw new Error(data.error||'Workstreams unavailable');
    workstreamData=data;workstreamsLoadedAt=Date.now();
  }catch(error){workstreamData={ok:false,error:String(error),workstreams:workstreamData.workstreams||[]};}
  finally{workstreamsLoading=false;renderWorkstreams(workstreamData);if(settingsOpen&&budgetSettingsOpen)renderSettings();}
}
function setWorkFilter(value){workFilter=value;renderWorkstreams(workstreamData);}
function setWorkState(value){
  workState=['all','needs_you','working','mixed'].includes(value)?value:'all';
  renderWorkstreams(workstreamData);
}
function saveCurrentView(destination){
  if(!['now','workstreams'].includes(destination))return;
  const name=(prompt('Name this saved view')||'').trim().slice(0,40);
  if(!name)return;
  const item=destination==='now'?{name,query:nowFilter,state:nowState}:
    {name,query:workFilter,state:workState};
  const items=savedViews[destination].filter(view=>view.name.toLowerCase()!==name.toLowerCase());
  savedViews[destination]=[...items,item].slice(-12);
  localStorage.setItem(SAVED_VIEW_KEY,JSON.stringify(savedViews));
  renderSavedViews(destination);
}
function applySavedView(destination,index){
  const view=(savedViews[destination]||[])[index];if(!view)return;
  if(destination==='now'){
    nowFilter=view.query||'';nowState=view.state||'all';
    const input=$('#nowfilter');if(input)input.value=nowFilter;render(last,true);
  }else{
    workFilter=view.query||'';workState=view.state||'all';
    const input=$('#workfilter');if(input)input.value=workFilter;renderWorkstreams(workstreamData);
  }
}
function deleteSavedView(destination,index,event){
  event?.stopPropagation();savedViews[destination].splice(index,1);
  localStorage.setItem(SAVED_VIEW_KEY,JSON.stringify(savedViews));renderSavedViews(destination);
}
function renderSavedViews(destination){
  const el=$(destination==='now'?'#nowsaved':'#worksaved');if(!el)return;
  el.innerHTML=(savedViews[destination]||[]).map((view,index)=>
    `<span class="savedchip"><button onclick="applySavedView('${destination}',${index})">${esc(view.name)}</button>`+
    `<button aria-label="delete saved view ${esc(view.name)}" onclick="deleteSavedView('${destination}',${index},event)">×</button></span>`).join('');
}
function matchesNow(session){
  if(!session)return false;
  const group=session.ui_group||(session.closed_at!=null?'history':'');
  if(nowState!=='all'&&group!==nowState)return false;
  const query=nowFilter.trim().toLowerCase();
  if(!query)return true;
  return [session.title,session.name,session.project,session.branch,session.provider,
    session.reason_label,session.access_label,session.model,session.cwd]
    .filter(Boolean).join(' ').toLowerCase().includes(query);
}

// ---- background transcript search ----------------------------------------
// The daemon indexes separately from provider polling. This page only reads the
// WAL-backed index, so an old or large transcript cannot delay /api/fleet.
let searchTimer=null,searchAbort=null,searchCursor=0,searchBusy=false;
let searchItems=[],searchProjects=[],searchStatusData=null,searchStatusAt=0,searchError='';
let searchView=null;
function queueSearch(reset){clearTimeout(searchTimer);searchTimer=setTimeout(()=>runSearch(reset),180);}
function searchParams(cursor){
  const params=new URLSearchParams({q:$('#searchquery')?.value||'',
    provider:$('#searchprovider')?.value||'',kind:$('#searchkind')?.value||'',
    project:$('#searchproject')?.value||'',cursor:String(cursor||0),limit:'30'});
  return params.toString();
}
function searchHasCriteria(){return Boolean(($('#searchquery')?.value||'').trim()||
  $('#searchprovider')?.value||$('#searchkind')?.value||$('#searchproject')?.value);}
function renderSearchStatus(){
  const el=$('#searchstatus'),warnings=$('#searchwarnings'),s=searchStatusData;
  if(!el)return;
  if(!s){el.innerHTML='<span>Checking the local index…</span>';if(warnings)warnings.innerHTML='';return;}
  if(!s.ok){el.innerHTML=`<span class="searcherror">${esc(s.error||'Search unavailable')}</span>`;if(warnings)warnings.innerHTML='';return;}
  const warning=(s.errors||0)+(s.malformed_rows||0)+(s.unknown_rows||0)+(s.oversized_docs||0);
  el.innerHTML=`<span>${s.state==='indexing'?'Indexing':'Indexed'} ${fmtTok(s.documents||0)} items from ${s.complete_sources||0}/${s.sources||0} sources</span>
    <span class="searchprogress" aria-label="index ${s.progress_pct||0}% complete"><i style="width:${Math.max(0,Math.min(100,s.progress_pct||0))}%"></i></span>
    <span>${s.progress_pct||0}%</span>${s.pending_sources?`<span>${s.pending_sources} pending</span>`:''}${warning?`<span class="searchwarn" title="Unknown protocol rows and unreadable files stay visible here instead of disappearing">${warning} warning${warning===1?'':'s'}</span>`:''}
    ${s.last_error?`<span class="searcherror">${esc(s.last_error)}</span>`:''}`;
  if(warnings)warnings.innerHTML=(s.warnings||[]).length?`<details class="searchwarnings"><summary>Index warnings by source</summary>
    ${(s.warnings||[]).map(item=>`<div class="searchwarning"><b>${esc(item.title||item.session_id||'Transcript')}</b>
      <small>${esc([item.provider,item.source_kind].filter(Boolean).join(' · '))}</small>
      <span>${esc(item.error||[item.malformed_rows?item.malformed_rows+' malformed':'',item.unknown_rows?item.unknown_rows+' unknown':'',item.oversized_docs?item.oversized_docs+' oversized':''].filter(Boolean).join(' · ')||'Parser warning')}</span></div>`).join('')}</details>`:'';
}
async function loadSearchStatus(force){
  if(!force&&Date.now()-searchStatusAt<4000)return;
  searchStatusAt=Date.now();
  renderSearchStatus();
  try{
    const r=await fetch('/api/search/status',{cache:'no-store'}),d=await r.json();
    searchStatusData=r.ok?d:{ok:false,error:r.status===403?'Search needs this device’s action token':(d.error||'Search unavailable')};
  }catch(e){searchStatusData={ok:false,error:String(e)};}
  renderSearchStatus();
}
function searchWhen(value){
  if(!value)return'';const d=new Date(value);if(isNaN(d))return'';
  return d.toLocaleString([],{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
}
function renderSearchResults(){
  const el=$('#searchresults'),more=$('#searchmore');if(!el||!more)return;
  if(searchError){el.innerHTML=`<div class="searchempty searcherror">${esc(searchError)}</div>`;more.hidden=true;return;}
  if(searchBusy&&!searchItems.length){el.innerHTML='<div class="searchempty">Searching…</div>';more.hidden=true;return;}
  if(!searchItems.length){el.innerHTML=`<div class="searchempty">${searchHasCriteria()?
    'No indexed conversation matches these filters.':'Type a search or choose a filter.'}</div>`;more.hidden=true;return;}
  el.innerHTML=searchItems.map(item=>`<button class="searchresult" onclick="openSearchContext(${Number(item.id)})">
    <span class="searchprovider ${item.provider==='claude'?'claude':'codex'}">${item.provider==='claude'?'C':'X'}</span>
    <span class="searchcopy"><span class="searchtitle"><b>${esc(item.title||item.project||'Conversation')}</b>
      <span class="searchbadge">${esc(item.source_kind==='subagent'?'subagent':item.kind||'message')}</span></span>
      <span class="searchmeta">${esc([item.provider,item.project,item.branch,item.agent_id].filter(Boolean).join(' · '))}</span>
      <span class="searchsnippet">${esc(item.snippet||'')}</span></span>
    <span class="searchtime">${esc(searchWhen(item.timestamp||item.timestamp_epoch*1000))}</span></button>`).join('');
  more.hidden=searchCursor==null;
}
function updateSearchProjects(projects){
  searchProjects=projects||searchProjects;const select=$('#searchproject');if(!select)return;
  const current=select.value;
  select.innerHTML='<option value="">All</option>'+searchProjects.map(project=>
    `<option value="${esc(project)}">${esc(project)}</option>`).join('');
  if(searchProjects.includes(current))select.value=current;
}
async function runSearch(reset=true){
  if(!$('#searchresults'))return;
  clearTimeout(searchTimer);
  if(reset){searchCursor=0;searchItems=[];searchError='';if(searchAbort)searchAbort.abort();}
  if(reset&&!searchHasCriteria()){
    searchBusy=false;searchAbort=null;renderSearchResults();return;
  }
  if(searchBusy&&!reset)return;
  const cursor=reset?0:searchCursor;if(cursor==null)return;
  const controller=new AbortController();searchAbort=controller;searchBusy=true;renderSearchResults();
  try{
    const r=await fetch('/api/search?'+searchParams(cursor),{cache:'no-store',signal:controller.signal});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(r.status===403?'Search needs this device’s action token':(d.error||'Search failed'));
    searchError='';
    searchItems=reset?(d.results||[]):searchItems.concat(d.results||[]);
    searchCursor=d.next_cursor;updateSearchProjects(d.projects||[]);
  }catch(e){
    if(e.name==='AbortError')return;
    searchItems=[];searchCursor=null;searchError=String(e.message||e);
  }finally{if(searchAbort===controller){searchBusy=false;searchAbort=null;renderSearchResults();}}
}
function searchContextMessage(item,provider){
  const hit=item.hit?' hit':'';
  if(item.kind==='tool')return`<div class="ctool${hit}"><div class="tline"><span class="tdot">●</span> <b>Indexed tool</b>(${esc(item.text||'')})</div></div>`;
  if(item.kind==='reasoning'||item.kind==='event')return`<div class="cmsg assistant${hit}"><span class="crole">${item.kind}</span><div class="cbody mdoc">${md(item.text||'')}</div></div>`;
  const role=item.role==='user'?'user':'assistant';
  return`<div class="cmsg ${role}${hit}"><span class="crole">${role==='user'?'you':provider}</span>
    <div class="cbody ${role==='assistant'?'mdoc':''}">${role==='assistant'?md(item.text||''):'<p>'+esc(item.text||'').replace(/\n/g,'<br>')+'</p>'}</div></div>`;
}
function searchSourceAction(source){
  if(!source)return'';const sid=source.session_id||'';
  const session=((last&&last.sessions)||[]).find(item=>item.session_id===sid);
  const active=Boolean(session);
  const closed=isClosedSession(sid);
  if(source.source_kind==='artifact'&&source.artifact_path&&active)
    return`<button class="headprimary" onclick="switchSearchView('artifact')">Open artifact</button>`;
  if(source.source_kind==='subagent'&&source.agent_id&&
      (session?.agents||[]).some(agent=>agent.agent_id===source.agent_id))
    return`<button class="headprimary" onclick="switchSearchView('agent')">Open live agent</button>`;
  if(active)return`<button class="headprimary" onclick="switchSearchView('session')">Open live session</button>`;
  if(closed)return`<button class="headsecondary" onclick="switchSearchView('closed')">Open session history</button>`;
  return'';
}
async function openSearchContext(id){
  searchView={id,source:null};
  $('#searchview').style.display='flex';$('#searchviewbody').innerHTML='<div class="ctxload">Loading exact context…</div>';
  $('#searchviewtitle').innerHTML='<b>Search result</b><small>indexed transcript</small>';$('#searchviewaction').innerHTML='';
  syncOverlayHistory();
  try{
    const r=await fetch('/api/search/context?id='+encodeURIComponent(id)+'&radius=12',{cache:'no-store'}),d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Context unavailable');
    if(!searchView||searchView.id!==id)return;
    searchView={id,source:d.source};const source=d.source||{};
    $('#searchviewtitle').innerHTML=`<b>${esc(source.source_title||source.project||'Conversation')}</b>
      <small>${esc([source.provider,source.project,source.branch,source.source_kind==='subagent'?source.agent_id:''].filter(Boolean).join(' · '))}</small>`;
    $('#searchviewaction').innerHTML=searchSourceAction(source);
    $('#searchviewbody').innerHTML=`<div class="searchcontext">${(d.messages||[]).map(item=>searchContextMessage(item,source.provider||'assistant')).join('')||'<div class="ctxload">No context recorded</div>'}</div>`;
    requestAnimationFrame(()=>$('#searchviewbody .hit')?.scrollIntoView({block:'center'}));
  }catch(e){if(searchView&&searchView.id===id)$('#searchviewbody').innerHTML=`<div class="ctxload searcherror">✗ ${esc(String(e.message||e))}</div>`;}
}
function closeSearchView(){searchView=null;$('#searchview').style.display='none';$('#searchviewbody').innerHTML='';
  $('#searchviewtitle').innerHTML='';$('#searchviewaction').innerHTML='';}
function switchSearchView(kind){
  const source=searchView&&searchView.source;if(!source)return;
  closeSearchView();
  if(kind==='agent')openAgent(source.session_id,source.agent_id);
  else if(kind==='artifact'){
    const name=String(source.artifact_path||'artifact').split('/').pop();
    viewFile(source.session_id,encodeURIComponent(source.artifact_path),encodeURIComponent(name),
      'text',encodeURIComponent('Indexed artifact'));
  }
  else if(kind==='closed')openClosed(source.session_id);else openSession(source.session_id);
}
function rebuildSearch(){
  askConfirm('Rebuild the transcript index?',
    'Fleet will discard its search database and rebuild it in the background. Sessions keep working while this runs.',
    'rebuild index',async()=>{
      const button=$('#search-rebuild');if(button){button.disabled=true;button.textContent='Rebuilding…';}
      try{
        const r=await fetch('/api/search/rebuild',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
        const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'Rebuild failed');
        searchStatusAt=0;searchItems=[];await loadSearchStatus(true);await runSearch(true);
      }catch(e){searchStatusData={ok:false,error:String(e.message||e)};renderSearchStatus();}
      finally{if(button){button.disabled=false;button.textContent='Rebuild index';}}
    });
}
// input guard, two windows: a scroll gesture (touchmove OR desktop wheel/trackpad)
// holds POLL re-renders 1500ms so the scrollbox isn't replaced mid-gesture (a
// replaced node kills wheel momentum: "scroll stops after a second"); a tap
// (touchstart) holds them 800ms so the 2s tick can't detach a button between
// touch and its click event (detached node = swallowed click). User-action
// renders pass force=true and bypass the guard — tap feedback must paint
// immediately.
let lastMove=0,lastTap=0;
document.addEventListener('touchstart',()=>{lastTap=Date.now()},{passive:true});
document.addEventListener('touchmove',()=>{lastMove=Date.now()},{passive:true});
document.addEventListener('wheel',()=>{lastMove=Date.now()},{passive:true});
const touching=()=>Date.now()-lastMove<1500||Date.now()-lastTap<800;
// scrollbar auto-hide: thumbs are transparent until the element actually scrolls
// (class fades 700ms after the last scroll event). Deliberately NOT tied into
// lastMove — programmatic scrolls (sticky-bottom restores) fire scroll events
// too, and feeding those into the render guard would starve re-renders.
let sbFade;
let fstripScroll=0;   // shared so the file strip keeps its position across the md
                      // viewer ↔ full chat view switch (a fresh DOM element each side)
document.addEventListener('scroll',e=>{
  const el=e.target===document?document.documentElement:e.target;
  if(el.classList){
    el.classList.add('scrolling');
    if(el.classList.contains('fstrip'))fstripScroll=el.scrollLeft;   // remember it live
  }
  clearTimeout(sbFade);
  sbFade=setTimeout(()=>document.querySelectorAll('.scrolling').forEach(x=>x.classList.remove('scrolling')),700);
},{passive:true,capture:true});
const fmt$=v=>v==null?'unavailable':'$'+(v>=100?v.toFixed(0):v>=10?v.toFixed(1):v.toFixed(2));
const fmtTok=v=>v==null?'—':v>=1e9?(v/1e9).toFixed(2)+'B':v>=1e6?(v/1e6).toFixed(2)+'M':v>=1e3?(v/1e3).toFixed(0)+'k':v;
const fmtAge=s=>s>=86400?Math.round(s/86400)+'d':s>=3600?Math.round(s/3600)+'h':s>=60?Math.round(s/60)+'m':s+'s';
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const stateLabel={running:'Working',needs_you:'Response needed',turn_done:'Available',idle:'Available',
  stalled:'Slow',stalled_or_prompt:'Check session',dormant:'Inactive',reopenable:'Reopenable',
  stale:'Unavailable',error:'Fix needed'};

// ---- provider plan-usage header (currently Claude supplies these account gauges) ----
function usageReset(iso){
  if(!iso)return'';
  const t=Date.parse(iso);if(isNaN(t))return'';
  const secs=Math.round((t-Date.now())/1000);
  const near=secs<86400;
  const when=new Date(t).toLocaleString([],near?{hour:'numeric',minute:'2-digit'}
    :{weekday:'short',hour:'numeric',minute:'2-digit'});
  return secs>0?`resets ${when} · ${fmtAge(secs)} left`:`resets ${when}`;
}
function ugauge(label,pct,reset){
  if(pct==null)return'';
  const col=pct>=90?'var(--red)':pct>=70?'var(--amber)':'var(--green)';
  return`<div class="ugauge"><span class="ulabel">${esc(String(label||''))}</span>
    <span class="ubar"><i style="width:${Math.min(pct,100)}%;background:${col}"></i></span>
    <span class="upct">${pct}%</span>
    ${reset?`<span class="ureset">${reset}</span>`:''}</div>`;
}
function usageBar(legacy,providers){
  const el=$('#usage');
  const claude=(providers&&providers.claude)||legacy;
  const codex=providers&&providers.codex;
  // Spark has a separate preview-model allowance. Keep it in the provider/API
  // data, but omit the unused model-specific bucket from the account summary.
  const codexBuckets=(codex?.buckets||[]).filter(b=>
    !/^gpt-5\.3-codex-spark\b/i.test(String(b.label||'')));
  const claudeProfiles=claude?.profiles?.length?claude.profiles:[claude];
  const claudeHtml=claude&&claudeProfiles.some(p=>p&&(p.five_hour_pct!=null||p.weekly_pct!=null||p.email))||claude?.lifetime_tokens!=null
    ?`<div class="uprovider">${claudeProfiles.filter(Boolean).map((profile,index)=>`<div class="uaccount">
      <div class="uhead"><span class="uname">Claude Code</span>
        ${profile.email?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="uemail">${esc(profile.email)}</span></span>`:''}
        ${claude.show_active!==false&&profile.active?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="uactive">active</span></span>`:''}
        ${index===0&&claude.lifetime_tokens!=null?`<span class="useg" title="All local Claude transcripts on this Mac across profiles, including saved subagents; excludes deleted history, claude.ai, and other computers"><i class="usep" aria-hidden="true">·</i><span class="umeta">${fmtTok(claude.lifetime_tokens)} local lifetime tokens</span></span>`:''}</div>
      ${ugauge('5-hour',profile.five_hour_pct,usageReset(profile.five_hour_reset))}
      ${claude.show_week===false?'':ugauge('weekly',profile.weekly_pct,usageReset(profile.weekly_reset))}</div>`).join('')}</div>`:'';
  const codexHtml=codex&&(codexBuckets.length||codex.email||codex.plan_type||codex.lifetime_tokens!=null||codex.reset_credits||codex.error)?`<div class="uprovider">
    <div class="uhead"><span class="uname">Codex CLI</span>
      ${codex.email?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="uemail">${esc(codex.email)}</span></span>`:''}
      ${codex.plan_type?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="umeta">${esc(codex.plan_type.replaceAll('_',' '))}</span></span>`:''}
      ${codex.lifetime_tokens!=null?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="umeta">${fmtTok(codex.lifetime_tokens)} lifetime tokens</span></span>`:''}
      ${codex.reset_credits?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="umeta">${codex.reset_credits} reset credit</span></span>`:''}</div>
    ${codex.error?`<span class="umeta">${codex.stale?'stale — ':''}${esc(codex.error)}</span>`:''}
    ${codexBuckets.map(b=>ugauge(b.label,b.used_pct,usageReset(b.reset))).join('')}</div>`:'';
  if(!claudeHtml&&!codexHtml){el.className='empty';el.innerHTML='';return;}
  el.className='';
  el.innerHTML=claudeHtml+codexHtml;
}

function spark(pts,w=64,h=16){
  if(!pts||pts.length<2)return'';
  const min=Math.min(...pts),max=Math.max(...pts),r=max-min||1;
  const p=pts.map((v,i)=>`${(i/(pts.length-1)*w).toFixed(1)},${(h-2-(v-min)/r*(h-4)).toFixed(1)}`).join(' ');
  return`<svg class="spark" width="${w}" height="${h}"><polyline points="${p}" fill="none" stroke="#58a6ff" stroke-width="1.5"/></svg>`;
}

// ---- tiny markdown renderer (self-contained: no CDN on the tailnet path) ----
function mdInline(s){
  return esc(s).split(/(`[^`]*`)/).map(p=>{
    if(p.length>1&&p.startsWith('`')&&p.endsWith('`'))return'<code>'+p.slice(1,-1)+'</code>';
    return p
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
      .replace(/\*([^*\n]+)\*/g,'<i>$1</i>');
  }).join('');
}
function md(src){
  const lines=(src||'').replace(/\r/g,'').split('\n');
  let html='',i=0,m;
  const isList=l=>/^\s*([-*+]|\d+\.)\s+/.test(l);
  const cells=r=>r.replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|').map(c=>c.trim());
  const isTableSep=l=>/^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(l)&&l.includes('-');
  while(i<lines.length){
    const l=lines[i];
    if(/^\s*```/.test(l)){
      const buf=[];i++;
      while(i<lines.length&&!/^\s*```/.test(lines[i]))buf.push(lines[i++]);
      i++;html+='<pre><code>'+esc(buf.join('\n'))+'</code></pre>';continue;
    }
    if(!l.trim()){i++;continue;}
    if(m=l.match(/^(#{1,6})\s+(.*)/)){const n=m[1].length;html+=`<h${n}>${mdInline(m[2])}</h${n}>`;i++;continue;}
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)){html+='<hr>';i++;continue;}
    if(/^\s*>/.test(l)){
      const buf=[];while(i<lines.length&&/^\s*>/.test(lines[i]))buf.push(lines[i++].replace(/^\s*>\s?/,''));
      html+='<blockquote>'+md(buf.join('\n'))+'</blockquote>';continue;
    }
    if(isList(l)){
      const ord=/^\s*\d+\./.test(l),items=[];
      while(i<lines.length&&isList(lines[i])){
        let it=lines[i].replace(/^\s*([-*+]|\d+\.)\s+/,'');i++;
        while(i<lines.length&&/^\s{2,}\S/.test(lines[i])&&!isList(lines[i]))it+=' '+lines[i++].trim();
        items.push(it);
      }
      html+=(ord?'<ol>':'<ul>')+items.map(x=>'<li>'+mdInline(x)+'</li>').join('')+(ord?'</ol>':'</ul>');continue;
    }
    if(l.includes('|')&&i+1<lines.length&&isTableSep(lines[i+1])){
      const head=cells(l);i+=2;let rows='';
      while(i<lines.length&&lines[i].includes('|')&&lines[i].trim())
        rows+='<tr>'+cells(lines[i++]).map(c=>'<td>'+mdInline(c)+'</td>').join('')+'</tr>';
      html+='<table><tr>'+head.map(c=>'<th>'+mdInline(c)+'</th>').join('')+'</tr>'+rows+'</table>';continue;
    }
    const buf=[l];i++;
    while(i<lines.length&&lines[i].trim()&&!/^\s*(#{1,6}\s|```|>)/.test(lines[i])
          &&!isList(lines[i])&&!(lines[i].includes('|')&&i+1<lines.length&&isTableSep(lines[i+1])))
      buf.push(lines[i++]);
    html+='<p>'+buf.map(mdInline).join('<br>')+'</p>';
  }
  return html;
}

// ---- durable message Outbox -----------------------------------------------
let outboxData={ok:true,items:[],summary:{pending:0,attention:0},usage_options:[]};
let outboxLoading=false,outboxLoadPromise=null,outboxLoadedAt=0,outboxAccess='unknown',outboxFilter='current',scheduleView=null;
const outboxPending=new Set(['scheduled','waiting_availability','waiting_usage_reset','spawning','sending']);
const outboxAttention=new Set(['blocked','failed','confirmation_unknown']);
function outboxWhen(item){
  const value=item.sent_at||item.trigger_at||item.created_at;if(!value)return'';
  const date=new Date(value*1000);return date.toLocaleString([],{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
}
function outboxTarget(item){
  if(item.kind==='new_session')return`new ${item.target_provider||item.spawn_spec?.provider||''} session`;
  const session=((last&&last.sessions)||[]).find(row=>row.session_id===(item.destination_session_id||item.target_session_id));
  const base=session?.title||session?.project||item.destination_session_id||item.target_session_id||'missing target';
  return item.target_agent_id?`${base} · ${item.target_agent_id}`:base;
}
async function loadOutbox(force=false){
  if(outboxAccess==='denied'&&!force)return outboxData;
  if(outboxLoading){await outboxLoadPromise;return force?loadOutbox(true):outboxData;}
  if(!force&&Date.now()-outboxLoadedAt<1500)return outboxData;
  outboxLoading=true;
  outboxLoadPromise=(async()=>{try{
      const r=await fetch('/api/outbox?limit=200',{cache:'no-store'}),data=await r.json();
      if(!r.ok||!data.ok){if(r.status===403)outboxAccess='denied';throw new Error(r.status===403?'Outbox needs this device’s action token':(data.error||'Outbox unavailable'));}
      outboxAccess='allowed';outboxData=data;outboxLoadedAt=Date.now();
    }catch(error){outboxData={...outboxData,ok:false,error:String(error)};}
    finally{outboxLoading=false;outboxLoadPromise=null;renderOutboxCompact();renderOutboxFull();}})();
  await outboxLoadPromise;return outboxData;
}
function renderOutboxCompact(){
  const el=$('#outboxsummary');if(!el)return;
  const summary=outboxData.summary||last?.outbox_summary||{};
  const current=(outboxData.items||[]).filter(item=>outboxPending.has(item.state)||outboxAttention.has(item.state));
  if(!summary.pending&&!summary.attention&&!outboxData.error){el.innerHTML='';return;}
  const rows=current.slice(0,3).map(item=>`<div class="outboxmini"><span class="oboxstate">${esc(item.state_label||item.state)}</span><span class="oboxmsg">${esc(item.message||'')}</span><small>${esc(outboxWhen(item))}</small></div>`).join('');
  el.innerHTML=`<section class="outboxcompact${summary.attention?' attention':''}"><button class="outboxcompacthead" onclick="openOutbox()">
    <span><b>Message Outbox</b><small>${summary.attention?`${summary.attention} need review · `:''}${summary.pending||0} waiting to send</small></span><b class="outboxcount">Open →</b></button>${rows}</section>`;
}
function openOutbox(){
  $('#outboxview').style.display='flex';syncOverlayHistory();renderOutboxFull();loadOutbox(true);
}
function closeOutbox(){$('#outboxview').style.display='none';$('#outboxbody').innerHTML='';}
function setOutboxFilter(value){outboxFilter=value;renderOutboxFull();}
function visibleOutboxItems(){
  const items=outboxData.items||[];
  if(outboxFilter==='pending')return items.filter(item=>outboxPending.has(item.state));
  if(outboxFilter==='attention')return items.filter(item=>outboxAttention.has(item.state));
  if(outboxFilter==='sent')return items.filter(item=>item.state==='sent');
  if(outboxFilter==='all')return items;
  return items.filter(item=>outboxPending.has(item.state)||outboxAttention.has(item.state));
}
function outboxRow(item){
  const error=item.error||item.blocked_reason;
  const canEdit=item.editable,canRetry=item.retryable;
  return`<article class="outboxrow ${esc(item.state)}"><div class="outboxtop"><span class="outboxstate">${esc(item.state_label||item.state)}</span>
    <span class="outboxtime">${esc(outboxWhen(item))}</span></div><div class="outboxmessage">${esc(item.message||'')}</div>
    <div class="outboxmeta">${esc(outboxTarget(item))} · ${esc(String(item.kind||'').replaceAll('_',' '))}${item.created_zone?` · ${esc(item.created_zone)}`:''}</div>
    ${error?`<div class="outboxerror">${esc(error)}</div>`:''}<div class="outboxactions">
      ${canEdit?`<button onclick="editOutbox('${item.id}')">Edit</button><button class="primary" onclick="outboxAction('${item.id}','outbox_send_now')">Send now</button><button onclick="confirmCancelOutbox('${item.id}')">Cancel</button>`:''}
      ${canRetry?`<button class="primary" onclick="editOutbox('${item.id}','retry')">Retry / retarget</button>`:''}
    </div></article>`;
}
function renderOutboxFull(){
  const el=$('#outboxbody');if(!el||$('#outboxview').style.display!=='flex')return;
  if(outboxLoading&&!outboxData.items?.length){el.innerHTML='<div class="ctxload">Loading Outbox…</div>';return;}
  if(!outboxData.ok){el.innerHTML=`<div class="outboxempty">${esc(outboxData.error||'Outbox unavailable')}</div>`;return;}
  const items=visibleOutboxItems();
  el.innerHTML=`<div class="outboxlayout"><div class="outboxtools">${[['current','Current'],['pending','Pending'],['attention','Needs review'],['sent','Sent'],['all','All']].map(([value,label])=>
    `<button class="${outboxFilter===value?'on':''}" onclick="setOutboxFilter('${value}')">${label}</button>`).join('')}</div>
    <div class="outboxlist">${items.length?items.map(outboxRow).join(''):'<div class="outboxempty">No messages in this view.</div>'}</div></div>`;
}
function mergeOutboxResult(result){
  if(!result?.item)return;
  const items=[...(outboxData.items||[])],index=items.findIndex(item=>item.id===result.item.id);
  if(index>=0)items[index]=result.item;else items.unshift(result.item);
  outboxData={...outboxData,ok:true,items,summary:result.summary||outboxData.summary};
  renderOutboxCompact();renderOutboxFull();
}
async function outboxAction(id,type,payload={}){
  const result=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({type,outbox_id:id,...payload})}).then(r=>r.json()).catch(error=>({ok:false,error:String(error)}));
  if(!result.ok){alert(result.error||'Outbox action failed');return result;}
  mergeOutboxResult(result);
  await loadOutbox(true);return result;
}
function confirmCancelOutbox(id){askConfirm('Cancel this scheduled message?',
  'It will remain in the Outbox audit trail and will never be sent.','cancel message',()=>outboxAction(id,'outbox_cancel'));}
function localInputAt(epoch,zone){
  if(!epoch)return'';try{
    const parts=new Intl.DateTimeFormat('en-CA',{timeZone:zone||undefined,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(epoch*1000));
    const get=kind=>parts.find(part=>part.type===kind)?.value||'';
    return`${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`;
  }catch(_){return new Date(epoch*1000).toISOString().slice(0,16);}
}
function defaultScheduleTime(){const date=new Date(Date.now()+3600000),pad=value=>String(value).padStart(2,'0');date.setSeconds(0,0);
  return`${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;}
function scheduleButton(sid,inputId,agentId=''){
  return`<button class="pbtn sendoption" title="schedule or wait to send" aria-label="delivery options" onclick="openSchedule('${sid}','${inputId}','${agentId}')">⌄</button>`;
}
async function openSchedule(sid,inputId,agentId='',existing=null,spawnSpec=null,message=''){
  await loadOutbox();
  const input=inputId?document.getElementById(inputId):null;
  const source=existing||{};
  const zone=source.created_zone||Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
  scheduleView={id:source.id||null,operation:null,sid:sid||source.target_session_id||'',
    agentId:agentId||source.target_agent_id||'',inputId:inputId||null,
    kind:source.kind||((spawnSpec||source.spawn_spec)?'new_session':'at_time'),
    message:message||source.message||(input?.value||''),zone,
    localTime:source.local_time||localInputAt(source.trigger_at,zone)||defaultScheduleTime(),
    fold:source.trigger_fold,choices:null,usageKey:'',spawnSpec:spawnSpec||source.spawn_spec||null};
  if(source.usage_account_id)scheduleView.usageKey=[source.target_provider,source.usage_account_id,source.usage_window_id].join('|');
  const stacked=anyOverlay();
  $('#scheduleview').style.display='flex';$('#scheduletitle').textContent=source.id?'Edit Outbox message':
    scheduleView.kind==='new_session'?'Schedule new coding session':'Schedule message';
  if(stacked){schedulePushed=true;history.pushState({fdSchedule:1},'');}else syncOverlayHistory();
  renderSchedule();
}
function editOutbox(id,operation=null){
  const item=(outboxData.items||[]).find(row=>row.id===id);if(!item)return;
  closeOutbox();openSchedule(item.target_session_id,null,item.target_agent_id||'',item).then(()=>{scheduleView.operation=operation;renderSchedule();});
}
function closeSchedule(){$('#scheduleview').style.display='none';$('#schedulebody').innerHTML='';scheduleView=null;}
function scheduleSet(key,value){if(!scheduleView)return;scheduleView[key]=value;if(key==='sid')scheduleView.agentId='';renderSchedule();}
function scheduleUsageOptions(){return(outboxData.usage_options||[]).flatMap(account=>
  (account.windows||[]).map(window=>({value:[account.provider,account.account_id,window.id].join('|'),
    label:`${account.provider==='claude'?'Claude Code':'Codex CLI'} · ${account.label} · ${window.label} · ${usageReset(window.reset)}`})));
}
function renderSchedule(){
  const el=$('#schedulebody');if(!el||!scheduleView)return;const v=scheduleView;
  const sessions=((last&&last.sessions)||[]).filter(item=>item.access==='interactive'&&!item.read_only);
  const target=sessions.find(item=>item.session_id===v.sid);const agents=(target?.agents||[]).filter(a=>!['done','ended'].includes(a.state));
  const usage=scheduleUsageOptions();if(!v.usageKey&&usage.length)v.usageKey=usage[0].value;
  const spawn=v.spawnSpec||{};const isNew=v.kind==='new_session';
  el.innerHTML=`<div class="scheduleform">${!isNew?`<div class="schedulemode">
      ${[['at_time','At a time'],['when_available','When available'],['usage_reset','When usage resets']].map(([value,label])=>`<button class="${v.kind===value?'on':''}" onclick="scheduleSet('kind','${value}')">${label}</button>`).join('')}</div>
    <label class="nflab">exact session</label><select class="nfsel" onchange="scheduleSet('sid',this.value)">
      ${sessions.map(item=>`<option value="${esc(item.session_id)}" ${item.session_id===v.sid?'selected':''}>${esc((item.provider==='codex'?'Codex · ':'Claude · ')+(item.title||item.project))}</option>`).join('')}</select>
    ${agents.length?`<label class="nflab">target</label><select class="nfsel" onchange="scheduleSet('agentId',this.value)"><option value="">Session</option>${agents.map(agent=>`<option value="${esc(agent.agent_id)}" ${agent.agent_id===v.agentId?'selected':''}>Subagent · ${esc(agent.description||agent.agent_type||agent.agent_id)}</option>`).join('')}</select>`:''}`:
    `<label class="nflab">provider</label><select class="nfsel" onchange="scheduleView.spawnSpec.provider=this.value;renderSchedule()"><option value="claude" ${spawn.provider==='claude'?'selected':''}>Claude Code</option><option value="codex" ${spawn.provider==='codex'?'selected':''}>Codex CLI</option></select>
     <label class="nflab">directory</label><input class="nfin" value="${esc(spawn.cwd||'')}" oninput="scheduleView.spawnSpec.cwd=this.value">
     <div class="nfrow"><div class="nfcol"><label class="nflab">model</label><input class="nfin" value="${esc(spawn.model||'')}" placeholder="default" oninput="scheduleView.spawnSpec.model=this.value"></div>
     <div class="nfcol"><label class="nflab">effort</label><input class="nfin" value="${esc(spawn.effort||'')}" placeholder="default" oninput="scheduleView.spawnSpec.effort=this.value"></div></div>
     ${spawn.provider==='codex'?`<label class="nflab">mode</label><select class="nfsel" onchange="scheduleView.spawnSpec.mode=this.value"><option value="plan" ${spawn.mode==='plan'?'selected':''}>Plan</option><option value="default" ${spawn.mode==='default'?'selected':''}>Default</option></select>`:
       `<label class="nfcheck"><input type="checkbox" ${spawn.worktree?'checked':''} onchange="scheduleView.spawnSpec.worktree=this.checked;renderSchedule()"><span>new git worktree</span></label>${spawn.worktree?`<input class="nfin" value="${esc(spawn.worktree_name||'')}" placeholder="worktree name (optional)" oninput="scheduleView.spawnSpec.worktree_name=this.value">`:''}`}`}
    <label class="nflab">message</label><textarea class="nfin" maxlength="2000" oninput="scheduleView.message=this.value">${esc(v.message)}</textarea>
    ${v.kind==='at_time'||isNew?`<div class="scheduletime"><label><span class="nflab">local date and time</span><input class="nfin" type="datetime-local" value="${esc(v.localTime)}" onchange="scheduleView.localTime=this.value;scheduleView.choices=null"></label><span class="schedulezone">${esc(v.zone)}</span></div>`:''}
    ${v.kind==='usage_reset'?`<label class="nflab">account and usage window</label><select class="nfsel" onchange="scheduleView.usageKey=this.value">${usage.map(item=>`<option value="${esc(item.value)}" ${item.value===v.usageKey?'selected':''}>${esc(item.label)}</option>`).join('')}</select>${usage.length?'':'<div class="schedulewarn">No fresh reset evidence is available.</div>'}`:''}
    ${v.choices?`<div class="schedulewarn">That clock time occurs twice. Choose which occurrence:<select class="nfsel" onchange="scheduleView.fold=Number(this.value)">${v.choices.map((choice,index)=>`<option value="${choice.fold}">${index?'Second':'First'} occurrence · UTC offset ${esc(choice.offset)}</option>`).join('')}</select></div>`:''}
    <div id="schedulemsg" class="actmsg"></div><div class="schedulesubmit"><button class="pbtn" onclick="dismissOverlay()">Cancel</button><button class="pbtn send" onclick="submitSchedule()">${v.id?(v.operation?'Create retry':'Save changes'):'Schedule'}</button></div></div>`;
}
async function submitSchedule(){
  if(!scheduleView)return;const v=scheduleView;const msg=$('#schedulemsg');
  if(!v.message.trim()){msg.textContent='✗ message is required';return;}
  if(/^[\/$]/.test(v.message.trim())){msg.textContent='✗ scheduled sends support messages, not commands or skills';return;}
  const target=((last&&last.sessions)||[]).find(item=>item.session_id===v.sid);
  const payload={kind:v.kind,message:v.message,created_zone:v.zone,local_time:v.localTime,trigger_fold:v.fold,
    target_provider:target?.provider,target_session_id:v.sid,target_agent_id:v.agentId||null};
  if(v.kind==='new_session'){payload.spawn_spec=v.spawnSpec;payload.target_provider=v.spawnSpec?.provider;}
  if(v.kind==='usage_reset'){
    const [provider,account,window]=v.usageKey.split('|');payload.target_provider=provider;
    payload.usage_account_id=account;payload.usage_window_id=window;delete payload.local_time;
  }
  if(v.kind==='when_available')delete payload.local_time;
  msg.textContent='saving…';
  let body;if(!v.id)body={type:'outbox_create',...payload};
  else if(v.operation==='retry')body={type:'outbox_retry',outbox_id:v.id,patch:payload};
  else body={type:'outbox_update',outbox_id:v.id,patch:payload};
  const result=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).catch(error=>({ok:false,error:String(error)}));
  if(!result.ok){if(result.code==='ambiguous_time'){v.choices=result.choices;v.fold=result.choices?.[0]?.fold;renderSchedule();return;}msg.textContent='✗ '+(result.error||'failed');return;}
  mergeOutboxResult(result);
  const input=v.inputId&&document.getElementById(v.inputId);if(input)input.value='';
  dismissOverlay();await loadOutbox(true);render(last,true);
}

// ---- deterministic in-app briefing ---------------------------------------
const BRIEF_DEVICE_KEY='fleet.briefingDevice.v1';
const briefingDevice=(()=>{let value=localStorage.getItem(BRIEF_DEVICE_KEY);
  if(!value){value=(crypto.randomUUID?crypto.randomUUID():`device-${Date.now()}-${Math.random().toString(16).slice(2)}`);
    localStorage.setItem(BRIEF_DEVICE_KEY,value);}return value;})();
let briefingData={ok:true,sections:{attention:[],completed:[],slow:[],outcomes:[],budgets:[],measurements:[],reviewed:[]},unread:0};
let briefingLoading=false,briefingLoadPromise=null,briefingLoadedAt=0,briefingOpen=false,briefingReviewing=false;
async function loadBriefing(force=false){
  if(briefingLoading)return briefingLoadPromise;
  if(!force&&Date.now()-briefingLoadedAt<4000)return;
  briefingLoading=true;briefingLoadPromise=(async()=>{try{
    const r=await fetch(`/api/briefing?device=${encodeURIComponent(briefingDevice)}&limit=120`,{cache:'no-store'}),data=await r.json();
    if(!r.ok||!data.ok)throw new Error(data.error||'Briefing unavailable');
    briefingData=data;briefingLoadedAt=Date.now();
  }catch(error){briefingData={...briefingData,ok:false,error:String(error)};}
  finally{briefingLoading=false;briefingLoadPromise=null;renderBriefing();}})();
  return briefingLoadPromise;
}
function briefingItem(item){
  const linked=['session','repository','outbox','budget','settings'].includes(item.link_kind);
  const action=linked?`onclick="openBriefingSource(decodeURIComponent('${enc(item.link_kind)}'),decodeURIComponent('${enc(item.link_id||'')}'))"`:'';
  return`<button class="briefitem ${esc(item.severity||'info')}" ${action} ${action?'':'disabled'}>
    <span class="briefdot"></span><span><b>${esc(item.title||'Update')}</b><small>${esc(item.summary||'')}</small></span>
    ${item.provider?`<em>${esc(item.provider)}</em>`:''}</button>`;
}
function briefingBudget(item){
  const ratio=item.ratio==null?0:Math.min(1,item.ratio),scope=item.measurement_scope||'unavailable';
  return`<button class="briefbudget ${esc(item.status||'ok')}" onclick="openBriefingSource('budget','${enc(item.id)}')"><span><b>${esc(item.label)}</b><small>${esc(item.summary)}</small></span>
    <span class="budgetscope">${esc(scope.replaceAll('_',' '))}</span><span class="budgetbar"><i style="width:${Math.round(ratio*100)}%"></i></span></button>`;
}
function briefingGroup(title,items,renderer=briefingItem){
  if(!items?.length)return'';return`<section class="briefgroup"><h3>${esc(title)} <span>${items.length}</span></h3>${items.map(renderer).join('')}</section>`;
}
function openBriefingSource(kind,id){
  if(kind==='session'&&id){openSession(id);return;}
  if(kind==='repository'&&id){openRepository(id,id);return;}
  if(kind==='outbox'){openOutbox();return;}
  if(kind==='budget'){navigateTo('insights');return;}
  if(kind==='settings')navigateTo('settings');
}
async function markBriefingReviewed(){
  const cursor=briefingData.next_cursor;if(!cursor||cursor<=briefingData.review_cursor||briefingReviewing)return;
  briefingReviewing=true;try{const data=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({type:'briefing_review',device_id:briefingDevice,cursor})}).then(r=>r.json());
    if(data.ok)briefingData.review_cursor=data.cursor;
  }catch(_){}finally{briefingReviewing=false;}
}
async function toggleBriefing(){briefingOpen=!briefingOpen;renderBriefing();if(briefingOpen){await loadBriefing(true);setTimeout(markBriefingReviewed,600);}}
function renderBriefing(){
  const el=$('#briefing');if(!el)return;const d=briefingData,s=d.sections||{};
  const current=(s.attention?.length||0)+(s.slow?.length||0)+(s.budgets||[]).filter(x=>['warning','exceeded','unavailable'].includes(x.status)).length;
  const unread=(s.completed?.length||0)+(s.outcomes?.length||0)+(s.measurements?.length||0);
  const reviewed=s.reviewed?.length||0;
  if(!current&&!unread&&!reviewed&&!d.error){el.innerHTML='';return;}
  const status=d.error?'Briefing unavailable':`${current} current · ${unread} since review${reviewed?` · ${reviewed} recently reviewed`:''}${d.muted_omitted?` · ${d.muted_omitted} muted from push`:''}`;
  el.innerHTML=`<section class="briefingpanel"><button class="briefhead" onclick="toggleBriefing()"><span><b>Fleet briefing</b><small>${esc(status)}</small></span><b>${briefingOpen?'Hide':'Review'} ${briefingOpen?'↑':'→'}</b></button>
    ${briefingOpen?`<div class="briefbody">${d.error?`<div class="provideralert">${esc(d.error)}</div>`:''}
      ${briefingGroup('Needs attention now',s.attention)}${briefingGroup('Completed since last review',s.completed)}
      ${briefingGroup('Still working unusually slowly',s.slow)}${briefingGroup('Outcomes and artifacts',s.outcomes)}
      ${briefingGroup('Budgets and measurement',(s.budgets||[]).filter(x=>x.status!=='ok'),briefingBudget)}
      ${briefingGroup('Unavailable measurements',s.measurements)}${briefingGroup('Recently reviewed',s.reviewed)}</div>`:''}</section>`;
}

// Compact Markdown for card peeks. Preserve headings/emphasis/lists while
// collapsing document-scale code and tables into short, safe prose.
function peekMd(src){
  const withoutCode=String(src||'').replace(/```[^\n]*\n[\s\S]*?(?:```|$)/g,'`code block`');
  const lines=withoutCode.replace(/\r/g,'').split('\n'),out=[];
  const tableSep=l=>/^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(l)&&l.includes('-');
  for(let i=0;i<lines.length;i++){
    if(lines[i].includes('|')&&i+1<lines.length&&tableSep(lines[i+1])){
      out.push(lines[i].replace(/^\s*\||\|\s*$/g,'').split('|').map(x=>x.trim()).join(' · '));
      i+=2;
      while(i<lines.length&&lines[i].includes('|')&&lines[i].trim())i++;
      i--;
    }else out.push(lines[i]);
  }
  return md(out.join('\n'));
}

// ---- conversation context + delivered files (fetched per session on demand) ----
const ctxCache={};   // sid -> {v, messages, files} | {fetching:true}
const ctxVersion=s=>(s.convo_v||'')+':'+(s.files_n||0);
async function ensureCtx(sid,v){
  const c=ctxCache[sid];
  if(c&&(c.v===v||c.fetching))return;
  ctxCache[sid]={...(c||{}),fetching:true};
  try{
    const r=await fetch('/api/context?sid='+encodeURIComponent(sid)+'&limit=50',{cache:'no-store'});
    const d=await r.json();
    ctxCache[sid]=d.ok?{v,messages:d.messages||[],files:d.files||[],
      next_cursor:d.next_cursor,message_total:d.message_total}:{v,messages:[],files:[]};
    render(last);
  }catch(e){delete ctxCache[sid];}
}
function conversationCache(scope,sid,aid=''){
  if(scope==='closed')return closedCtx[sid];
  if(scope==='agent')return agentCache[agentCacheKey(sid,aid)];
  return ctxCache[sid];
}
function conversationEndpoint(scope,sid,aid='',cursor=null){
  const route=scope==='closed'?'/api/closed_context':scope==='agent'?'/api/agent_context':'/api/context';
  const params=new URLSearchParams({sid,limit:'50'});
  if(aid)params.set('aid',aid);
  if(cursor!=null)params.set('cursor',String(cursor));
  return route+'?'+params.toString();
}
async function loadOlderConversation(scope,sid,aid=''){
  const c=conversationCache(scope,sid,aid);
  if(!c||c.loadingOlder||c.next_cursor==null)return;
  const body=(scope==='agent'&&agentView&&agentView.sid===sid&&agentView.aid===aid)?$('#abody'):
    (scope!=='agent'&&sessionView&&sessionView.sid===sid&&Boolean(sessionView.closed)===(scope==='closed')?$('#sbody'):null);
  const old=body?{height:body.scrollHeight,top:body.scrollTop}:null;
  c.loadingOlder=true;uiRefresh();
  try{
    const response=await fetch(conversationEndpoint(scope,sid,aid,c.next_cursor),{cache:'no-store'});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'Older messages unavailable');
    c.messages=[...(data.messages||[]),...(c.messages||[])];
    c.next_cursor=data.next_cursor;c.message_total=data.message_total;c.olderError='';
  }catch(error){c.olderError=String(error.message||error);}
  finally{c.loadingOlder=false;uiRefresh();}
  if(body&&old&&document.body.contains(body))requestAnimationFrame(()=>{
    body.scrollTop=old.top+Math.max(0,body.scrollHeight-old.height);
  });
}
// "opus · high". Effort comes from the statusline side-write, so a session whose
// statusline hasn't rendered yet (or isn't installed) shows the model alone.
const modelLabel=s=>esc(s.model||s.family||'?')+(s.effort?` · ${esc(s.effort)}`:'');
function modeSelect(s,pre='msg'){
  if(!s||s.provider!=='codex')return'';
  const mode=s.collaboration_mode||'default';
  const locked=!s.capabilities?.submit||['running','needs_you','stalled'].includes(s.state);
  return`<select class="modesel" title="Codex collaboration mode — Plan enables structured questions; Default executes work"
    onclick="event.stopPropagation()" onchange="setSessionMode('${s.session_id}',this.value,'${pre}')" ${locked?'disabled':''}>
    <option value="plan" ${mode==='plan'?'selected':''}>Plan</option>
    <option value="default" ${mode==='default'?'selected':''}>Default</option></select>`;
}
const enc=v=>encodeURIComponent(v||'').replace(/'/g,'%27');  // onclick-attr-safe
const cpb=v=>`<b class="copyable" title="tap to copy" data-copy="${esc(v)}" onclick="copyTxt(event,this)">${esc(v)}</b>`;
function fchip(sid,f,cap){
  return`<button class="fchip" ${f.missing?'disabled':''} title="${esc(cap||f.path)}"
    onclick="event.stopPropagation();viewFile('${sid}','${enc(f.path)}','${enc(f.name)}','${f.kind}','${enc(cap)}')">${f.kind==='image'?'🖼':'📄'} ${esc(f.name)}${f.missing?' (gone)':''}</button>`;
}
const EVT_ICON={compact:'⧉',model:'⇄',api_error:'⚠',command:'›',qa:'☑'};
function eventRow(m,provider='claude'){
  if(m.kind==='qa'){
    const rows=(m.qa||[]).map(q=>`<div class="qarow">
      <div class="qaq">${q.header?`<span class="qah">${esc(q.header)}</span>`:''}${esc(q.q)}</div>
      <div class="qaa">${esc(q.a||'—')}</div></div>`).join('');
    return`<div class="cevt info"><div class="evline"><span class="evi">☑</span> <b>You answered ${esc(provider)}'s questions</b></div>
      <div class="qawrap">${rows}</div></div>`;
  }
  return`<div class="cevt ${m.level||'info'}">
    <div class="evline"><span class="evi">${EVT_ICON[m.kind]||'•'}</span> <b>${esc(m.title)}</b>${m.n>1?`<span class="evn">×${m.n}</span>`:''}</div>
    ${m.detail?`<div class="evd">${esc(m.detail)}</div>`:''}</div>`;
}
const optimisticMessages=new Map();
let optimisticSequence=0;
const quickResponses=new Map();
const normalizedMessage=text=>String(text||'').trim().replace(/\s+/g,' ');
function optimisticList(sid){
  if(!optimisticMessages.has(sid))optimisticMessages.set(sid,[]);
  return optimisticMessages.get(sid);
}
function canonicalCount(messages,item){
  if(item.kind==='answer')return(messages||[]).filter(message=>
    message.role==='event'&&message.kind==='qa').length;
  const wanted=normalizedMessage(item.text);
  return(messages||[]).filter(message=>message.role==='user'&&
    normalizedMessage(message.text)===wanted).length;
}
function addOptimistic(sid,text,kind='text'){
  const feedbackStarted=performance.now();
  const messages=(ctxCache[sid]&&ctxCache[sid].messages)||[];
  const item={id:++optimisticSequence,sid,text:String(text||''),kind,status:'sending',
    baseCount:canonicalCount(messages,{kind,text}),created:Date.now()};
  optimisticList(sid).push(item);
  setTimeout(()=>{
    const current=optimisticList(sid).find(entry=>entry.id===item.id);
    if(current&&current.status==='sending'){
      current.status='failed';current.error='Not confirmed after 15 seconds';uiRefresh();
    }
  },15000);
  const openConvo=sessionView?.sid===sid&&!sessionView.closed&&$('#sbody .aconvo');
  if(openConvo){
    openConvo.insertAdjacentHTML('beforeend',optimisticItemHtml(item));
    $('#sbody').scrollTop=$('#sbody').scrollHeight;
    recordInputFeedback(feedbackStarted);
    requestAnimationFrame(()=>render(last,true));
  }else{
    uiRefresh();
    recordInputFeedback(feedbackStarted);
  }
  return item.id;
}
function updateOptimistic(sid,id,ok,error,providerConfirmed=false){
  const item=optimisticList(sid).find(entry=>entry.id===id);
  if(!item)return;
  if(!ok){item.status='failed';item.error=error||'Send failed';}
  else if(providerConfirmed)item.status='confirmed';
  uiRefresh();
}
function visibleOptimistic(sid,messages){
  const list=optimisticList(sid);
  const keep=list.filter(item=>{
    if(canonicalCount(messages,item)>item.baseCount)return false;
    return true;
  });
  if(keep.length!==list.length)optimisticMessages.set(sid,keep);
  return keep;
}
function restoreOptimistic(sid,id){
  const list=optimisticList(sid),item=list.find(entry=>entry.id===id);
  optimisticMessages.set(sid,list.filter(entry=>entry.id!==id));
  if(item?.kind==='answer'){delete answered[sid];uiRefresh();return;}
  uiRefresh();
  requestAnimationFrame(()=>{
    const input=document.getElementById('sft-'+sid)||document.getElementById('vft-'+sid)||
      document.getElementById('ft-'+sid);
    if(input){input.value=item?.text||'';input.focus();}
  });
}
function optimisticItemHtml(item){
  return`<div class="cmsg user optimistic" data-optimistic-id="${item.id}">
    <span class="crole">you</span>${item.status==='sending'?`<span class="delivery sending" aria-label="sending">◌</span>`:
      item.status==='failed'?`<button class="delivery failed" title="${esc(item.error||'Send failed')} — restore" aria-label="send failed; restore message" onclick="restoreOptimistic('${item.sid}',${item.id})">!</button>`:''}
    <div class="cbody"><p>${esc(item.text).replace(/\n/g,'<br>')}</p></div></div>`;
}
function optimisticHtml(sid,messages){
  return visibleOptimistic(sid,messages).map(item=>optimisticItemHtml(item)).join('');
}
function quickResponseLabel(payload){
  if(payload.type==='permission')return `${payload.choice==='always'?'Always allow':
    payload.choice==='allow'?'Allow':payload.choice==='deny'?'Deny':'Cancel'} permission`;
  if(payload.type==='dismiss')return'Dismiss question';
  if(payload.type==='elicitation')return payload.choice==='accept'?'Submit form':
    payload.choice==='decline'?'Decline form':'Cancel form';
  return'Submit response';
}
function beginQuickResponse(sid,payload){
  const feedbackStarted=performance.now();
  const item={id:++optimisticSequence,sid,nonce:payload.nonce,text:quickResponseLabel(payload),
    status:'sending',created:Date.now()};
  quickResponses.set(sid,item);uiRefresh();recordInputFeedback(feedbackStarted);return item.id;
}
function finishQuickResponse(sid,id,ok,error){
  const item=quickResponses.get(sid);
  if(!item||item.id!==id)return;
  item.status=ok?'sent':'failed';item.error=error||'';uiRefresh();
}
function reconcileQuickResponses(f){
  const sessions=new Map(((f&&f.sessions)||[]).map(s=>[s.session_id,s]));
  const now=Date.now();
  for(const [sid,item] of quickResponses){
    const session=sessions.get(sid);
    if(!session){quickResponses.delete(sid);continue;}
    if(item.nonce&&session.pending?.nonce!==item.nonce){
      if(!item.canonicalAt){
        item.canonicalAt=now;
        if(item.status==='sending')item.status='sent';
      }else if(now-item.canonicalAt>5000)quickResponses.delete(sid);
    }else delete item.canonicalAt;
  }
}
function cardResponseFeedback(s){
  const messages=((ctxCache[s.session_id]||{}).messages)||[];
  const answers=visibleOptimistic(s.session_id,messages).filter(item=>item.kind==='answer');
  const answer=answers.length?answers[answers.length-1]:null;
  const action=quickResponses.get(s.session_id)||null;
  const item=!answer?action:!action?answer:(answer.created>=action.created?answer:action);
  if(!item)return'';
  const status=item.status==='confirmed'||item.status==='sent'?'sent':item.status;
  const verb=status==='sending'?'Submitting':status==='failed'?'Failed':'Submitted';
  const icon=status==='sending'?`<span class="delivery sending" aria-label="sending quick response">◌</span>`:
    status==='failed'?(item.kind==='answer'
      ?`<button class="delivery failed" title="${esc(item.error||'Submission failed')} — restore" aria-label="submission failed; restore response" onclick="event.stopPropagation();restoreOptimistic('${s.session_id}',${item.id})">!</button>`
      :`<span class="delivery failed" title="${esc(item.error||'Submission failed')}" aria-label="submission failed">!</span>`)
      :`<span class="delivery sent" aria-label="response submitted">✓</span>`;
  return`<div class="quickfeedback ${status}" role="status" aria-live="polite">
    <span class="qfstate">${verb}</span><span class="qftext">${esc(normalizedMessage(item.text))}</span>${icon}</div>`;
}
function convoMsgs(c,sid,includeOptimistic=true,scope='session',aid=''){
  const provider=String(sid||'').startsWith('codex:')?'codex':'claude';
  const canonical=(c.messages||[]).map(m=>{
    if(m.role==='event')return eventRow(m,provider);
    if(m.role==='tool'){
      if(m.name==='SendUserFile')
        return`<div class="ctool cfile">${(m.files||[]).map(f=>fchip(sid,f,m.caption)).join('')}
          ${m.caption?`<div class="fcap">${esc(m.caption)}</div>`:''}</div>`;
      return`<div class="ctool"><div class="tline"><span class="tdot">●</span> <b>${esc(m.name)}</b>(${esc(m.arg||'')})</div></div>`;
    }
    return`<div class="cmsg ${m.role}"><span class="crole">${m.role==='user'?'you':provider}</span>
      <div class="cbody ${m.role==='assistant'?'mdoc':''}">${m.role==='assistant'?md(m.text):'<p>'+esc(m.text).replace(/\n/g,'<br>')+'</p>'}</div></div>`;
  }).join('');
  const older=c.next_cursor!=null?`<button class="historyaction oldermsgs" onclick="loadOlderConversation('${scope}',decodeURIComponent('${enc(sid)}'),decodeURIComponent('${enc(aid)}'))"
    ${c.loadingOlder?'disabled':''}>${c.loadingOlder?'loading older messages…':'load older messages'}</button>`:'';
  const error=c.olderError?`<div class="ctxload">✗ ${esc(c.olderError)}</div>`:'';
  return older+error+canonical+(includeOptimistic?optimisticHtml(sid,c.messages||[]):'');
}
function convoBox(s,short){
  const c=ctxCache[s.session_id];
  if(!c||c.fetching&&!c.messages)return'<div class="ctxbox"><div class="ctxload">loading context…</div></div>';
  const msgs=convoMsgs(c,s.session_id);
  if(!msgs)return'';
  return`<div class="ctxbox"><div class="convo${short?' short':''}" data-sid="${s.session_id}">${msgs}</div></div>`;
}
let viewerSid=null;
// one persisted reading theme shared by chat, Markdown, and subagent views
function setTheme(light){
  $('#vbody').classList.toggle('light',light);
  $('#sbody').classList.toggle('light',light);
  $('#sevidence').classList.toggle('light',light);
  $('#abody').classList.toggle('light',light);
  $('#searchviewbody').classList.toggle('light',light);
  try{localStorage.setItem('viewer_light',light?'1':'0');}catch(e){}
}
function toggleTheme(){setTheme(!$('#vbody').classList.contains('light'));}
(()=>{try{setTheme(localStorage.getItem('viewer_light')==='1');}catch(e){}})();

let overflowOpen=null;
function closeOverflow(){
  overflowOpen=null;
  document.querySelectorAll('.ovmenu.open').forEach(el=>el.classList.remove('open'));
  document.querySelectorAll('.ovbtn[aria-expanded="true"]').forEach(el=>el.setAttribute('aria-expanded','false'));
}
function toggleOverflow(event,key){
  event.stopPropagation();
  const wasOpen=overflowOpen===key;
  closeOverflow();
  if(wasOpen)return;
  overflowOpen=key;
  event.currentTarget.setAttribute('aria-expanded','true');
  const menu=event.currentTarget.parentElement.querySelector('.ovmenu');
  if(menu)menu.classList.add('open');
}
function overflowMenu(key,s,kind='session',done=false){
  const open=overflowOpen===key;
  const label=kind==='viewer'?'viewer actions':kind==='subagent'?'subagent actions':'session actions';
  const lifecycle=kind==='session'||kind==='viewer';
  const canMode=lifecycle&&s&&s.provider==='codex';
  const mode=s?.collaboration_mode||'default';
  const modeLocked=!s?.capabilities?.submit||['running','needs_you','stalled'].includes(s?.state);
  const canStop=kind==='subagent'
    ? Boolean(!done&&s?.capabilities?.interrupt)
    : Boolean(s?.capabilities?.interrupt);
  const canClose=Boolean(lifecycle&&s?.capabilities?.close);
  const canHandoff=Boolean(s?.session_id&&['session','viewer','closed'].includes(kind));
  const canRepo=Boolean(s?.cwd&&['session','viewer','closed','subagent'].includes(kind));
  return`<span class="ovwrap">
    <button class="ovbtn" aria-label="${label}" aria-haspopup="menu" aria-expanded="${open?'true':'false'}"
      onclick="toggleOverflow(event,'${key}')">⋮</button>
    <span class="ovmenu${open?' open':''}" role="menu" onclick="event.stopPropagation()">
      ${canMode?`<span class="ovgroup"><span class="ovlabel">Codex mode</span><span class="ovseg">
        <button aria-pressed="${mode==='plan'}" ${modeLocked?'disabled':''}
          onclick="closeOverflow();setSessionMode('${s.session_id}','plan','${kind==='viewer'?'vmsg':'smsg'}')">Plan</button>
        <button aria-pressed="${mode==='default'}" ${modeLocked?'disabled':''}
          onclick="closeOverflow();setSessionMode('${s.session_id}','default','${kind==='viewer'?'vmsg':'smsg'}')">Default</button>
      </span></span><span class="ovsep"></span>`:''}
      <button class="ovitem" role="menuitem" onclick="closeOverflow();toggleTheme()">
        <span>Appearance</span><small>light / dark</small></button>
      ${canRepo?`<button class="ovitem" role="menuitem" onclick="closeOverflow();openRepository('',decodeURIComponent('${enc(s.cwd)}'))">
        <span>Repository outcome</span><small>changes · tests · PR</small></button>`:''}
      ${canHandoff?`<span class="ovsep"></span>
        <button class="ovitem" role="menuitem" onclick="closeOverflow();openHandoff(decodeURIComponent('${enc(s.session_id)}'),'claude')">
          <span>Continue in Claude</span><small>new session</small></button>
        <button class="ovitem" role="menuitem" onclick="closeOverflow();openHandoff(decodeURIComponent('${enc(s.session_id)}'),'codex')">
          <span>Continue in Codex</span><small>new session</small></button>`:''}
      ${(lifecycle||kind==='subagent')?`<span class="ovsep"></span>
        <button class="ovitem danger" role="menuitem" ${canStop?'':'disabled'}
          onclick="closeOverflow();${kind==='subagent'
            ?`stopAgentParent('${s?.session_id||''}')`
            :`sendInterrupt('${s?.session_id||''}','${kind==='viewer'?'vmsg':'smsg'}')`}">
          <span>${kind==='subagent'?'Stop parent turn':'Stop turn'}</span><small>${canStop?'active':'not running'}</small></button>`:''}
      ${lifecycle?`<button class="ovitem danger" role="menuitem" ${canClose?'':'disabled'}
          onclick="closeOverflow();sendCloseSession('${s?.session_id||''}','${kind==='viewer'?'vmsg':'smsg'}')">
          <span>Close session</span><small>${canClose?'move to history':'unavailable'}</small></button>`:''}
    </span></span>`;
}

// ---- exact provider handoff ------------------------------------------------
// A handoff creates a separate provider-native session. Fleet stores only the
// source/destination identity and delivery status; the editable body is not kept.
let handoffView=null,handoffPushed=false,handoffOpenAfterBack=null;
function handoffCatalog(provider){
  return ((((last||{}).models_by_provider||{})[provider])||[]).map(item=>
    typeof item==='string'?{id:item,name:item,efforts:[]} : item);
}
function handoffArtifactSection(){
  const selected=(handoffView?.data?.artifacts||[]).filter(item=>
    handoffView.selected.has(item.path)&&!item.missing);
  const lines=['[Selected artifact references]'];
  if(!selected.length)lines.push('(none)');
  selected.forEach(item=>lines.push(`- ${item.path}${item.caption?' — '+item.caption:''}`));
  lines.push('[End artifact references]');
  return lines.join('\n');
}
function updateHandoffArtifacts(path,checked){
  const textarea=$('#handoffpreview');
  if(textarea)handoffView.draft=textarea.value;
  if(checked)handoffView.selected.add(path);else handoffView.selected.delete(path);
  const section=handoffArtifactSection();
  const marker=/\[Selected artifact references\][\s\S]*?\[End artifact references\]/;
  handoffView.draft=marker.test(handoffView.draft)
    ?handoffView.draft.replace(marker,section):handoffView.draft+'\n\n'+section;
  if(textarea)textarea.value=handoffView.draft;
}
function renderHandoff(){
  const root=$('#handoffbody');if(!handoffView||!root)return;
  if(handoffView.loading){root.innerHTML='<div class="ctxload">Building a safe handoff preview…</div>';return;}
  if(handoffView.error&&!handoffView.data){
    root.innerHTML=`<div class="destinationempty"><span>!</span><b>Handoff unavailable</b><p>${esc(handoffView.error)}</p></div>`;return;
  }
  const d=handoffView.data,defaults=handoffView.defaults||{},provider=handoffView.target;
  const catalog=handoffCatalog(provider),picked=catalog.find(item=>item.id===defaults.model);
  const efforts=(picked?.efforts?.length?picked.efforts:((last||{}).efforts||[]));
  const artifacts=d.artifacts||[];
  root.innerHTML=`<div class="handofflayout">
    <section class="handoffeditor"><div class="handoffnotice"><b>Independent session</b><span>The source keeps running. Fleet creates one exact ${provider==='claude'?'Claude Code':'Codex CLI'} destination and sends this editable message to it.</span></div>
      <label class="handofflabel" for="handoffpreview"><span>Message to send</span><small>${handoffView.draft.length.toLocaleString()} / 30,000</small></label>
      <textarea id="handoffpreview" maxlength="30000" oninput="handoffView.draft=this.value;this.previousElementSibling.querySelector('small').textContent=this.value.length.toLocaleString()+' / 30,000'">${esc(handoffView.draft)}</textarea>
    </section>
    <aside class="handoffoptions"><h3>New coding session</h3>
      <label class="nflab">provider</label><select class="nfsel" onchange="changeHandoffProvider(this.value)">
        <option value="claude" ${provider==='claude'?'selected':''}>Claude Code</option>
        <option value="codex" ${provider==='codex'?'selected':''}>Codex CLI</option></select>
      <label class="nflab">directory</label><input class="nfin" value="${esc(defaults.cwd||'')}"
        oninput="handoffView.defaults.cwd=this.value" autocomplete="off">
      ${artifacts.length?`<label class="nflab">artifact references</label><div class="handoffartifacts">${artifacts.map(item=>
        `<label class="handoffartifact"><input type="checkbox" ${handoffView.selected.has(item.path)?'checked':''} ${item.missing?'disabled':''}
          onchange="updateHandoffArtifacts(decodeURIComponent('${enc(item.path)}'),this.checked)"><span>${esc(item.name||item.path)}${item.missing?' (gone)':''}<small>${esc(item.caption||item.path)}</small></span></label>`).join('')}</div>`:''}
      <details class="handoffadvanced" ${handoffView.advanced?'open':''} ontoggle="handoffView.advanced=this.open"><summary>Advanced session settings</summary>
        <label class="nflab">model</label><select class="nfsel" onchange="handoffView.defaults.model=this.value">
          <option value="">provider default</option>${catalog.map(item=>`<option value="${esc(item.id)}" ${defaults.model===item.id?'selected':''}>${esc(item.name||item.id)}</option>`).join('')}</select>
        <label class="nflab">effort</label><select class="nfsel" onchange="handoffView.defaults.effort=this.value">
          <option value="">provider default</option>${efforts.map(value=>`<option value="${esc(value)}" ${defaults.effort===value?'selected':''}>${esc(value)}</option>`).join('')}</select>
        ${provider==='codex'?`<label class="nflab">mode</label><select class="nfsel" onchange="handoffView.defaults.mode=this.value">
          <option value="plan" ${defaults.mode==='plan'?'selected':''}>Plan</option><option value="default" ${defaults.mode==='default'?'selected':''}>Default</option></select>`:''}
        <label class="nfcheck"><input type="checkbox" ${defaults.worktree?'checked':''}
          onchange="handoffView.defaults.worktree=this.checked;renderHandoff()"><span>start in a new Git worktree</span></label>
        ${defaults.worktree?`<label class="nflab">worktree name</label><input class="nfin" maxlength="40" value="${esc(defaults.worktree_name||'')}"
          placeholder="optional" oninput="handoffView.defaults.worktree_name=this.value">`:''}
      </details>
      <button class="pbtn send handoffsubmit" ${handoffView.busy?'disabled':''} onclick="submitHandoff(false)">${handoffView.busy?'Creating and sending…':`Start ${provider==='claude'?'Claude':'Codex'} and send`}</button>
      <div class="handoffstatus ${handoffView.error?'error':handoffView.destination?'ok':''}">${esc(handoffView.status||handoffView.error||'')}</div>
      ${handoffView.retryable&&handoffView.destination?`<button class="pbtn handoffretry" onclick="submitHandoff(true)">Retry delivery to the same session</button>`:''}
      ${handoffView.destination?`<button class="pbtn handoffretry" onclick="openHandoffDestination()">Open exact destination</button>`:''}
    </aside></div>`;
}
async function loadHandoff(){
  const view=handoffView;if(!view)return;
  view.loading=true;view.error='';renderHandoff();
  try{
    const r=await fetch(`/api/handoff?sid=${encodeURIComponent(view.sid)}&provider=${encodeURIComponent(view.target)}`,{cache:'no-store'});
    const d=await r.json();if(!r.ok||!d.ok)throw new Error(r.status===403?'This device needs Fleet’s action token to read and send handoffs':(d.error||'handoff unavailable'));
    if(handoffView!==view)return;
    view.data=d;view.draft=d.preview||'';view.defaults={...(d.defaults||{})};
    view.selected=new Set((d.artifacts||[]).filter(item=>!item.missing).map(item=>item.path));
  }catch(error){if(handoffView===view)view.error=String(error.message||error);}
  finally{if(handoffView===view){view.loading=false;renderHandoff();}}
}
function openHandoff(sid,target){
  handoffView={sid,target,data:null,draft:'',defaults:{},selected:new Set(),loading:true,
    busy:false,error:'',status:'',destination:null,retryable:false,advanced:false,
    providerDrafts:{}};
  $('#handoffview').style.display='flex';
  if(!histPushed){histPushed=true;history.pushState({fdOverlay:1},'');}
  if(!handoffPushed){handoffPushed=true;history.pushState({fdHandoff:1},'');}
  loadHandoff();
}
function closeHandoff(){
  handoffView=null;$('#handoffview').style.display='none';$('#handoffbody').innerHTML='';
}
function changeHandoffProvider(provider){
  if(!handoffView||!['claude','codex'].includes(provider))return;
  const textarea=$('#handoffpreview');if(textarea)handoffView.draft=textarea.value;
  handoffView.providerDrafts[handoffView.target]={data:handoffView.data,
    draft:handoffView.draft,defaults:{...handoffView.defaults},
    selected:new Set(handoffView.selected)};
  handoffView.target=provider;handoffView.destination=null;handoffView.retryable=false;
  const saved=handoffView.providerDrafts[provider];
  if(saved){handoffView.data=saved.data;handoffView.draft=saved.draft;
    handoffView.defaults={...saved.defaults};handoffView.selected=new Set(saved.selected);
    handoffView.error='';handoffView.loading=false;renderHandoff();}
  else loadHandoff();
}
async function submitHandoff(retry){
  if(!handoffView||handoffView.busy)return;
  const view=handoffView,textarea=$('#handoffpreview');if(textarea)view.draft=textarea.value;
  view.busy=true;view.error='';view.status=retry?'Retrying the exact destination…':'Creating one exact destination…';renderHandoff();
  const payload={type:'handoff',session_id:view.sid,provider:view.target,preview:view.draft,
    cwd:view.defaults.cwd,model:view.defaults.model,effort:view.defaults.effort,
    mode:view.defaults.mode,worktree:Boolean(view.defaults.worktree),
    worktree_name:view.defaults.worktree_name||''};
  if(retry)payload.destination_session_id=view.destination;
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();if(handoffView!==view)return;
    view.destination=d.destination_session_id||d.session_id||view.destination;
    view.retryable=Boolean(d.retryable);
    if(!r.ok||!d.ok)throw new Error(d.error||'handoff failed');
    view.status='Sent ✓ — opening the exact destination';view.error='';
    await tick();
    if(handoffView===view)setTimeout(()=>openHandoffDestination(true),150);
  }catch(error){if(handoffView===view){view.error=String(error.message||error);view.status='';}}
  finally{if(handoffView===view){view.busy=false;renderHandoff();}}
}
async function openHandoffDestination(automatic=false){
  if(!handoffView?.destination)return;
  const sid=handoffView.destination;
  await tick();
  const exists=((last||{}).sessions||[]).some(item=>item.session_id===sid)||
    isClosedSession(sid);
  if(!exists){
    handoffView.status=automatic?'Sent ✓ — destination is still starting; use Open exact destination in a moment':'Destination is still starting.';
    renderHandoff();return;
  }
  handoffOpenAfterBack=sid;
  if(handoffPushed)history.back();else{closeHandoff();primarySessionAction(sid);}
}
function handoffLinksHtml(s){
  const links=s?.handoff_links||[];if(!links.length)return'';
  return`<div class="handofflinks">${links.map(link=>`<button class="handofflink ${link.status==='delivery_failed'?'failed':''}"
    title="${esc(link.status+(link.error?' — '+link.error:''))}" onclick="event.stopPropagation();primarySessionAction(decodeURIComponent('${enc(link.session_id)}'))">${link.direction==='from'?'continued in':'continued from'} ${esc(link.provider)} · ${esc(link.status)}</button>`).join('')}</div>`;
}

// ---- repository outcomes --------------------------------------------------
// Reads are cached, bounded Git/GitHub argv probes. Mutations always use the
// revision from this editable preview and a second confirmation interstitial.
let repoView=null,repoPushed=false;
function repoSignal(state){
  return({passed:'passed',failed:'failed',running:'running',unknown:'unknown',
    not_observed:'not observed',stale:'stale',unavailable:'unavailable'})[state]||state||'not observed';
}
function updateRepoSelection(path,checked){
  if(!repoView)return;checked?repoView.selected.add(path):repoView.selected.delete(path);
  renderRepository();
}
function renderRepository(){
  const root=$('#repobody');if(!repoView||!root)return;
  if(repoView.loading&&!repoView.data){root.innerHTML='<div class="ctxload">Reading repository evidence…</div>';return;}
  if(repoView.error&&!repoView.data){root.innerHTML=`<div class="destinationempty"><span>!</span><b>Repository unavailable</b><p>${esc(repoView.error)}</p><button class="pbtn" onclick="loadRepository(true)">retry</button></div>`;return;}
  const d=repoView.data||{},actions=d.actions||{},commit=actions.commit||{},push=actions.push||{},
    create=actions.pr_create_draft||{},ready=actions.pr_mark_ready||{},pr=d.pr||{},tests=d.tests||{};
  const files=d.files||[],selected=repoView.selected||new Set();
  const checks=pr.checks||{};
  const branch=d.detached?'detached HEAD':(d.branch||'branch unavailable');
  const worktrees=d.worktrees||[d.worktree].filter(Boolean);
  root.innerHTML=`<div class="repolayout">
    <section class="reposummary">
      <div class="repostatus"><span><small>Branch</small><b>${esc(branch)}</b></span>
        <span><small>Changes</small><b class="${d.dirty?'warn':'ok'}">${d.dirty?`${files.length} file${files.length===1?'':'s'}`:'clean'}</b></span>
        <span><small>Sync</small><b>${d.ahead||0} ahead · ${d.behind||0} behind</b></span>
        <span><small>Tests/build</small><b class="${tests.state==='failed'?'bad':tests.state==='passed'?'ok':''}">${esc(repoSignal(tests.state))}</b></span>
        <span><small>Pull request</small><b>${pr.state==='ok'?`#${esc(String(pr.number))}${pr.is_draft?' · draft':' · ready'}`:esc(repoSignal(pr.state==='none'?'not_observed':pr.state))}</b></span></div>
      <div class="repoevidence"><span>Observed ${d.observed_at?fmtAge(Math.max(0,Date.now()/1000-d.observed_at))+' ago':'now'}${d.cached?' · cached':''}</span>
        ${tests.command?`<code title="${esc(tests.command)}">${esc(tests.command)}</code>`:'<span>no test/build command observed in session transcripts</span>'}
        ${pr.state==='ok'?`<span>${checks.total||0} checks · ${checks.passed||0} passed · ${checks.pending||0} pending · ${checks.failed||0} failed</span>`:''}
        ${pr.error?`<span class="repoerror">${esc(pr.error)}</span>`:''}</div>
      ${worktrees.length>1?`<label class="repolabel">Worktree<select onchange="changeRepositoryWorktree(this.value)">${worktrees.map(path=>`<option value="${esc(path)}" ${path===d.worktree?'selected':''}>${esc(path)}</option>`).join('')}</select></label>`:`<div class="repopath">${esc(d.worktree||'')}</div>`}
      ${repoView.status?`<div class="reporesult ${repoView.failed?'bad':'ok'}">${esc(repoView.status)}</div>`:''}
      ${(d.recent_actions||[]).length?`<div class="repohistory"><b>Recent actions</b>${d.recent_actions.map(item=>`<span class="${item.status==='failed'?'bad':'ok'}"><strong>${esc(String(item.kind||'').replaceAll('_',' '))}</strong><small>${esc(item.status)} · ${fmtAge(Math.max(0,Date.now()/1000-(item.finished_at||item.started_at||0)))} ago</small>${item.error?`<em>${esc(item.error)}</em>`:''}</span>`).join('')}</div>`:''}
    </section>
    <section class="repoactions">
      <article class="repoaction"><div><h3>Commit</h3><p>Review the exact files and edit the message before committing.</p></div>
        <div class="repofiles">${files.length?files.map(file=>`<label><input type="checkbox" ${selected.has(file.path)?'checked':''} ${file.conflict||file.staged?'disabled':''}
          onchange="updateRepoSelection(decodeURIComponent('${enc(file.path)}'),this.checked)"><span><code>${esc(file.status)}</code>${esc(file.path)}${file.staged?'<small>staged</small>':''}${file.untracked?'<small>new</small>':''}</span></label>`).join(''):'<span class="repoempty">Working tree is clean.</span>'}</div>
        <label class="repolabel">Commit message<textarea id="repocommit" maxlength="1000" oninput="repoView.commitMessage=this.value">${esc(repoView.commitMessage||commit.default_message||'')}</textarea></label>
        <small class="reponote">The commit includes selected files plus anything already staged in this worktree.</small>
        <button class="pbtn send" ${!commit.enabled||!selected.size||repoView.busy?'disabled':''} onclick="confirmRepoCommit()">Commit ${selected.size||''} file${selected.size===1?'':'s'}</button>
        ${!commit.enabled&&commit.reason?`<small class="repoerror">${esc(commit.reason)}</small>`:''}</article>
      <article class="repoaction"><div><h3>Push</h3><p>${push.remote?`Push ${push.ahead||0} commit${push.ahead===1?'':'s'} to ${esc(push.remote)}/${esc(push.branch||'')}.`:'No push destination is configured.'}</p></div>
        <button class="pbtn send" ${!push.enabled||repoView.busy?'disabled':''} onclick="confirmRepoPush()">Push</button>
        ${!push.enabled&&push.reason?`<small class="repoerror">${esc(push.reason)}</small>`:''}</article>
      <article class="repoaction"><div><h3>Draft pull request</h3><p>Fleet always creates a draft. It never merges.</p></div>
        <label class="repolabel">Title<input id="reprtitle" maxlength="200" value="${esc(repoView.prTitle||create.title||'')}" oninput="repoView.prTitle=this.value"></label>
        <label class="repolabel">Base branch<input id="reprbase" maxlength="200" value="${esc(repoView.prBase||create.base||'main')}" oninput="repoView.prBase=this.value"></label>
        <label class="repolabel">Body<textarea id="reprbody" maxlength="20000" oninput="repoView.prBody=this.value">${esc(repoView.prBody||create.body||'')}</textarea></label>
        <button class="pbtn send" ${!create.enabled||repoView.busy?'disabled':''} onclick="confirmRepoDraftPr()">Create draft PR</button>
        ${!create.enabled&&create.reason?`<small class="repoerror">${esc(create.reason)}</small>`:''}</article>
      <article class="repoaction"><div><h3>Ready for review</h3><p>${pr.state==='ok'?`PR #${esc(String(pr.number))} · ${esc(pr.title||'')}`:'No pull request is attached to this branch.'}</p></div>
        <button class="pbtn send" ${!ready.enabled||repoView.busy?'disabled':''} onclick="confirmRepoReady()">Mark ready</button>
        ${!ready.enabled&&ready.reason?`<small class="repoerror">${esc(ready.reason)}</small>`:''}</article>
    </section></div>`;
}
async function loadRepository(force=false){
  const view=repoView;if(!view)return;view.loading=true;view.error='';renderRepository();
  const q=new URLSearchParams({root:view.root||'',worktree:view.worktree||'',force:force?'1':'0'});
  try{const r=await fetch('/api/repo?'+q,{cache:'no-store'}),d=await r.json();
    if(repoView!==view)return;if(!r.ok||!d.ok)throw new Error(r.status===403?'This device needs Fleet’s action token for repository details':(d.error||'repository unavailable'));
    view.data=d;view.root=d.root;view.worktree=d.worktree;
    const paths=new Set((d.files||[]).filter(file=>!file.conflict).map(file=>file.path));
    if(!view.selectionInitialized){view.selected=paths;view.selectionInitialized=true;}
    else view.selected=new Set([...view.selected].filter(path=>paths.has(path)));
    if(!view.commitMessage)view.commitMessage=d.actions?.commit?.default_message||'';
    if(!view.prTitle)view.prTitle=d.actions?.pr_create_draft?.title||'';
    if(!view.prBase)view.prBase=d.actions?.pr_create_draft?.base||'main';
  }catch(error){if(repoView===view)view.error=String(error.message||error);}
  finally{if(repoView===view){view.loading=false;renderRepository();}}
}
function openRepository(root,worktree){
  const stacked=anyOverlay();repoView={root,worktree,data:null,selected:new Set(),selectionInitialized:false,
    commitMessage:'',prTitle:'',prBody:'',prBase:'',loading:true,busy:false,error:'',status:'',failed:false};
  $('#repoview').style.display='flex';
  if(stacked){repoPushed=true;history.pushState({fdRepo:1},'');}else syncOverlayHistory();
  loadRepository();
}
function closeRepository(){repoView=null;$('#repoview').style.display='none';$('#repobody').innerHTML='';}
function changeRepositoryWorktree(path){if(!repoView)return;repoView.worktree=path;repoView.data=null;
  repoView.selectionInitialized=false;repoView.commitMessage='';repoView.status='';loadRepository(true);}
async function runRepoAction(type,payload={}){
  const view=repoView;if(!view||view.busy||!view.data)return;view.busy=true;view.failed=false;
  view.status='Running '+type.replaceAll('_',' ')+'…';renderRepository();
  try{const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      type,root:view.data.root,worktree:view.data.worktree,revision:view.data.revision,...payload})});
    const d=await r.json();if(repoView!==view)return;
    if(d.snapshot)view.data=d.snapshot;
    if(!r.ok||!d.ok)throw new Error(d.error||'repository action failed');
    view.status=(d.summary||type.replaceAll('_',' ')+' completed')+' ✓';view.failed=false;
    view.selectionInitialized=false;view.selected=new Set();
    view.commitMessage='';
    await loadWorkstreams(true);
  }catch(error){if(repoView===view){view.status=String(error.message||error);view.failed=true;}}
  finally{if(repoView===view){view.busy=false;renderRepository();}}
}
function confirmRepoCommit(){
  if(!repoView?.data)return;const input=$('#repocommit');if(input)repoView.commitMessage=input.value;
  const paths=[...repoView.selected];askConfirm('Commit selected files?',
    `<b>${paths.length} file${paths.length===1?'':'s'}</b> will be staged and committed in <code>${esc(repoView.data.worktree)}</code>.<br><br>${esc(repoView.commitMessage)}`,
    'commit',()=>runRepoAction('git_commit',{paths,message:repoView.commitMessage}));
}
function confirmRepoPush(){const p=repoView?.data?.actions?.push;if(!p)return;askConfirm('Push this branch?',
  `Pushes <b>${p.ahead||0} commit${p.ahead===1?'':'s'}</b> to <code>${esc(p.remote)}/${esc(p.branch)}</code>. No force push is used.`,
  'push',()=>runRepoAction('git_push'));}
function confirmRepoDraftPr(){
  if(!repoView)return;repoView.prTitle=$('#reprtitle')?.value||repoView.prTitle;
  repoView.prBase=$('#reprbase')?.value||repoView.prBase;repoView.prBody=$('#reprbody')?.value||repoView.prBody;
  askConfirm('Create draft pull request?',`Creates a <b>draft</b> from <code>${esc(repoView.data.branch)}</code> into <code>${esc(repoView.prBase)}</code>.<br><br>${esc(repoView.prTitle)}`,
    'create draft',()=>runRepoAction('pr_create_draft',{title:repoView.prTitle,base:repoView.prBase,body:repoView.prBody}));
}
function confirmRepoReady(){const pr=repoView?.data?.pr;if(!pr)return;askConfirm('Mark pull request ready?',
  `PR <b>#${esc(String(pr.number))}</b> will leave draft state and request review. Fleet will not merge it.`,
  'mark ready',()=>runRepoAction('pr_mark_ready',{number:pr.number}));}
function terminalButton(s,card=false){
  if(!s)return'';
  const cls=`expandbtn termbtn${card?' deskonly':''}`;
  if(s.capabilities?.focus_terminal){
    const attach=s.capabilities?.focus_terminal_mode==='attach';
    const title=attach?'open a Codex TUI attached to this shared runtime':"bring this session's terminal tab to the front";
    return`<button class="${cls}" title="${esc(title)}"
      onclick="event.stopPropagation();focusSession('${s.session_id}')">${attach?'attach':'open'}</button>`;
  }
  if(s.provider==='codex'){
    const label=s.capabilities?.focus_terminal_label||(s.read_only?'view only':'no terminal');
    return`<button class="${cls}" disabled
      title="${esc(s.capabilities?.focus_terminal_reason||'Codex terminal unavailable')}">${label}</button>`;
  }
  return'';
}
let viewerChatOpen=false,viewerQOpen=true,viewerPath=null;
// one question's full interaction block (options + descriptions + Other + ✕ dismiss);
// shared by the card's amber box (pre='msg') and the viewer's docked bar (pre='vmsg')
function singleQBlock(s,p,pre){
  const sid=s.session_id;
  const q=p.questions[0],ms=q.multiSelect,n=(q.options||[]).length;
  const sel=multiSel[sid]=multiSel[sid]||new Set();
  return`<div class="ptool"><span class="ptlabel">${esc(q.header||'question')} — waiting on you</span>
      <button class="xbtn" title="${p.dismiss_action==='cancel_turn'?'dismiss by stopping this Codex turn':'dismiss — chat about this instead'}" onclick="sendDismiss('${sid}','${p.nonce}','${pre}')">✕</button></div>
    ${p.files&&p.files.length?`<div class="pfiles"><span class="plabel">read first</span>${p.files.map(f=>fchip(sid,f,f.caption)).join('')}</div>`:''}
    <div class="qtext">${esc(q.question)}</div>
    ${(q.options||[]).map((o,i)=>`<button class="optbtn ${ms&&sel.has(i+1)?'sel':''}"
        onclick="${ms?`toggleOpt('${sid}',${i+1})`:`sendOption('${sid}','${p.nonce}',[${i+1}],'${pre}')`}">
        ${esc(o.label)}${o.description?`<small>${esc(o.description)}</small>`:''}</button>`).join('')}
    ${q.allowOther!==false?`<div class="freetext"><input id="oth-${pre}-${sid}" placeholder="Other — type your own answer" ${q.secret?'type="password"':''}
      value="${esc(otherDraft[sid]||'')}" oninput="otherDraft['${sid}']=this.value"
      ${ms?'':`onkeydown="if(event.key==='Enter')sendOther('${sid}','${p.nonce}',${n},'${pre}')"`}>
      ${ms?'':`<button class="pbtn send" onclick="sendOther('${sid}','${p.nonce}',${n},'${pre}')">answer</button>`}</div>`:''}
    ${ms?`<div class="pbtns"><button class="pbtn send" onclick="sendMulti('${sid}','${p.nonce}',${n},'${pre}')">submit selection</button></div>`:''}`;
}
// The delivered-file strip: identical markup and position (docked bar, directly
// above the send box) in BOTH full-screen surfaces, so they read as one screen.
// `cur` marks the file the viewer currently shows.
function fileStrip(sid,files){
  if(!files||!files.length)return'';
  return`<div class="stripbox"><div class="fstrip">${files.map(f=>`<button class="fchip ${f.path===viewerPath?'cur':''}"
    ${f.missing?'disabled':''} title="${esc(f.caption||f.path)}"
    onclick="viewFile('${sid}','${enc(f.path)}','${enc(f.name)}','${f.kind}','${enc(f.caption)}')">${f.kind==='image'?'🖼':'📄'} ${esc(f.name)}${f.missing?' (gone)':''}</button>`).join('')}</div></div>`;
}
// horizontal scroll position survives the 2s re-render AND the viewer↔chat switch
// (fstripScroll is a shared global, updated live on scroll)
function keepStripScroll(root,fn){
  const old=root.querySelector('.fstrip');
  if(old)fstripScroll=old.scrollLeft;
  fn();
  const ns=root.querySelector('.fstrip');
  if(ns)ns.scrollLeft=fstripScroll;
}
function renderViewerBar(force){
  if(!viewerSid)return;
  const bar=$('#vact');
  const s=((last||{}).sessions||[]).find(x=>x.session_id===viewerSid);
  $('#vctrl').innerHTML=overflowMenu('viewer',s,'viewer');
  const ae=document.activeElement;
  if(ae&&ae.tagName==='INPUT'&&bar.contains(ae))return;   // don't clobber typing
  if(!force&&touching())return;                           // or a swipe/tap in flight
  const old=bar.querySelector&&bar.querySelector('.vconvo');
  const oldScroll=old?{top:old.scrollTop,atBottom:old.scrollTop+old.clientHeight>=old.scrollHeight-12}:null;
  if(s)ensureCtx(viewerSid,ctxVersion(s));
  const c=ctxCache[viewerSid];
  let h=`<div class="togbox">
      <div class="togrow">
        <button class="vchat-toggle" onclick="viewerChatOpen=!viewerChatOpen;renderViewerBar(true)">${viewerChatOpen?'▾ hide conversation':'▸ show conversation'}</button>
        <button class="expandbtn" title="open the full conversation (closes this file)"
          onclick="openSession('${viewerSid}')">⤢ full view</button>
      </div>
      ${viewerChatOpen?`<div class="togbody">${c&&!c.fetching?`<div class="convo vconvo">${convoMsgs(c,viewerSid)||'<div class="ctxload">no conversation yet</div>'}</div>`:'<div class="ctxload">loading conversation…</div>'}</div>`:''}
    </div>`;
  const p=s&&s.pending;
  if(p&&p.kind==='question'&&p.questions&&p.questions.length&&answered[viewerSid]!==p.nonce){
    h+=`<div class="togbox waiting">
      <button class="vchat-toggle" onclick="viewerQOpen=!viewerQOpen;renderViewerBar(true)">${viewerQOpen?'▾ hide question':'▸ show question — waiting on you'}</button>
      ${viewerQOpen?`<div class="togbody">${p.questions.length>1?mqBlock(s,p,'vmsg'):singleQBlock(s,p,'vmsg')}</div>`:''}
    </div>`;
  }else if(p&&answered[viewerSid]!==p.nonce){
    h+=pendingBox(s,'vmsg');
  }
  h+=`${fileStrip(viewerSid,(c&&c.files)||[])}
    ${handoffLinksHtml(s)}
    ${s&&s.read_only?`<div class="relaynote"><b>view only</b> — ${esc(s.read_only_reason||'this thread is owned by another Codex runtime')}</div>`:''}
    ${s&&s.capabilities?.submit?`<div class="freetext"><input id="vft-${viewerSid}" placeholder="send a message  ·  / or $ for commands and skills" autocomplete="off"
      oninput="slashInput('${viewerSid}','vft')" onfocus="slashInput('${viewerSid}','vft')"
      onkeydown="if(event.key==='Enter')sendText('${viewerSid}','vft','vmsg');if(event.key==='Escape')slashClose()">
      <span class="sendpair"><button class="pbtn send" onclick="sendText('${viewerSid}','vft','vmsg')">send</button>${scheduleButton(viewerSid,'vft-'+viewerSid)}</span></div>`:''}
    <div class="slashwrap" id="slash-vft-${viewerSid}"></div>
    <div class="actmsg" id="vmsg-${viewerSid}"></div>`;
  keepStripScroll(bar,()=>{bar.innerHTML=h;});
  const nw=bar.querySelector&&bar.querySelector('.vconvo');
  if(nw)nw.scrollTop=(oldScroll&&!oldScroll.atBottom)?oldScroll.top:nw.scrollHeight;
}
// the chat view's title/subtitle block for a session (title on its own line,
// project · branch · model beneath) — shared by the full chat view and the md viewer
function sessTitleBlock(s){
  if(!s)return '<b>session</b>';
  return `<b>${esc(s.title||s.project)}</b>
    <small>${esc(s.project)}${s.branch&&s.branch!=='HEAD'?` · ${esc(s.branch)}`:''}${s.family?` · ${modelLabel(s)}`:''}</small>`;
}
function viewFile(sid,ep,en,kind,ecap){
  closeSession();          // the two full-screen surfaces are mutually exclusive
  const path=decodeURIComponent(ep),name=decodeURIComponent(en),cap=decodeURIComponent(ecap||'');
  const url='/api/file?sid='+encodeURIComponent(sid)+'&p='+encodeURIComponent(path);
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  // same session heading as the chat view, then a rule, then the file name
  $('#vtitle').innerHTML=`${sessTitleBlock(s)}
    <span class="vfsep"></span>
    <span class="vfname">${kind==='image'?'🖼':'📄'} ${esc(name)}${cap?` — ${esc(cap)}`:''}</span>`;
  $('#viewer').style.display='flex';
  viewerSid=sid;viewerPath=path;syncOverlayHistory();renderViewerBar(true);
  const vb=$('#vbody');
  if(kind==='image'){vb.innerHTML=`<img src="${url}" alt="${esc(name)}">`;return;}
  vb.textContent='loading…';
  fetch(url,{cache:'no-store'}).then(async r=>{
    if(!r.ok){vb.textContent=(r.status===403?'read-only device — open the ?token= URL once to view files. ':'')+await r.text();return;}
    const t=await r.text();
    vb.innerHTML=/\.(md|markdown)$/i.test(name)?'<div class="mdoc">'+md(t)+'</div>':'<pre class="raw">'+esc(t)+'</pre>';
  }).catch(e=>{vb.textContent='✗ '+e;});
}
function closeViewer(){closeOverflow();$('#viewer').style.display='none';$('#vbody').innerHTML='';$('#vact').innerHTML='';
  $('#vctrl').innerHTML='';
  viewerSid=null;viewerPath=null;}
// ---- back-gesture / Esc closes the open full-screen overlay ----------------
// The fullscreen surfaces are mutually exclusive, so we model
// "an overlay is open" as ONE logical state: push a single history entry when we
// go from none-open to open, and the phone's back-swipe (popstate) closes it
// instead of navigating away from the dashboard. Closing via ✕/Esc calls
// history.back() so the pushed entry is consumed and history stays balanced.
let histPushed=false,schedulePushed=false;
const anyOverlay=()=>['#viewer','#sview','#aview','#settingsview','#searchview','#handoffview','#repoview','#outboxview','#scheduleview'].some(id=>$(id).style.display==='flex');
function syncOverlayHistory(){
  if(anyOverlay()&&!histPushed){histPushed=true;history.pushState({fdOverlay:1},'');}
}
window.addEventListener('popstate',()=>{
  if(schedulePushed){schedulePushed=false;closeSchedule();return;}
  if(repoPushed){repoPushed=false;closeRepository();return;}
  if(handoffPushed){
    handoffPushed=false;
    const destination=handoffOpenAfterBack;handoffOpenAfterBack=null;
    closeHandoff();
    if(destination)primarySessionAction(destination);
    return;
  }
  if(histPushed){
    histPushed=false;
    closeConfirm();closeRepository();closeHandoff();closeViewer();closeAgent();closeSession();closeSettings();closeSearchView();closeOutbox();closeSchedule();
    return;
  }
  navigateTo(validRoutes.has(location.hash.slice(1))?location.hash.slice(1):'now',false);
});
function dismissOverlay(){
  if(overflowOpen)return closeOverflow();
  if($('#confirm').style.display==='flex')return closeConfirm();   // ask first
  if(repoPushed)return history.back();
  if(handoffPushed)return history.back();
  if(schedulePushed)return history.back();
  if(histPushed)history.back();          // → popstate does the actual close
  else{closeRepository();closeHandoff();closeViewer();closeAgent();closeSession();closeSettings();closeSearchView();closeOutbox();closeSchedule();}
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')dismissOverlay();});
document.addEventListener('click',e=>{
  if(overflowOpen&&!e.target.closest('.ovwrap'))closeOverflow();
  if($('#mobilemore').classList.contains('open')&&!e.target.closest('#mobilemore')&&!e.target.closest('[data-route="more"]'))closeMobileMore();
});

// ---- full-screen session view ----------------------------------------------
// Same overlay shape as the subagent view, but this one is a real terminal
// channel: send box, question block, interrupt/mute. Ids use the `sft-`/`smsg-`
// prefixes — the card's `ft-`/`msg-` elements coexist in the DOM.
let sessionView=null;            // {sid, closed} of the open overlay
let sessQOpen=true;              // the question block inside the chat view
let sessionOpened=false;         // just-opened: force-scroll to bottom on the first render
const closedCtx={};              // sid -> {messages, info} for CLOSED sessions
let sessionEvidenceOpen=false;
const evidenceCache={};          // sid -> {events,next_cursor,loaded,loading,error}
function confidenceText(value){return({confirmed:'confirmed',inferred:'inferred',stale:'stale',unknown:'unknown'})[value]||'unknown';}
function evidenceButton(s){
  if(!s||!s.session_id)return'';
  return`<button class="evidencebtn${sessionEvidenceOpen?' on':''}" aria-label="Why here?" aria-pressed="${sessionEvidenceOpen}"
    title="Explain why this session is ${esc(s.reason_label||s.ui_group||'here')}"
    onclick="toggleSessionEvidence('${enc(s.session_id)}')">◎ <span>Why here?</span></button>`;
}
function evidenceFactsHtml(s){
  const facts=(s&&s.state_evidence)||[];
  if(!facts.length)return'<div class="evidenceempty">No state evidence recorded yet.</div>';
  return`<div class="evidencefacts">${facts.map(fact=>`<div class="evidencefact">
    <span class="evidencekind">${esc(fact.label||fact.kind||'Evidence')}</span>
    <b>${esc(fact.value||'Unavailable')}</b><small class="confidence ${esc(fact.confidence||'unknown')}">${esc(confidenceText(fact.confidence))}</small>
  </div>`).join('')}</div>`;
}
function evidenceEventHtml(event){
  const when=event.at?new Date(event.at*1000).toLocaleString():'time unavailable';
  return`<article class="evidenceevent">
    <div><b>${esc(event.reason||event.ui_group||event.normalized_state||'State changed')}</b>
      <span class="confidence ${esc(event.confidence||'unknown')}">${esc(confidenceText(event.confidence))}</span></div>
    <small>${esc(when)} · ${esc(event.ui_group||'history')} · ${esc(event.access||'view_only')}</small>
    <p>${esc(event.evidence_summary||'No evidence summary recorded.')}</p>
    <code>${esc(event.winning_rule||'placement.unknown')}</code>
  </article>`;
}
function renderEvidenceRail(s){
  const rail=$('#sevidence');if(!rail)return;
  rail.classList.toggle('open',sessionEvidenceOpen);
  if(!sessionEvidenceOpen){rail.innerHTML='';return;}
  const cache=evidenceCache[s.session_id]||{};
  const suppressed=(s.suppressed_rules||[]);
  rail.innerHTML=`<div class="evidencehead"><div><small>Why Fleet put this here</small>
      <b>${esc(s.reason_label||s.ui_group||'Unknown')}</b></div>
      <button onclick="toggleSessionEvidence('${enc(s.session_id)}')" aria-label="close state evidence">✕</button></div>
    <div class="evidencecurrent"><div class="evidencestate"><span class="chip ${esc(s.ui_group||'history')}">${esc(s.ui_group||'history')}</span>
      <span class="confidence ${esc(s.state_confidence||'unknown')}">${esc(confidenceText(s.state_confidence))}</span></div>
      <div class="evidencerule"><span>Winning rule</span><code>${esc(s.winning_rule||'placement.unknown')}</code></div>
      ${evidenceFactsHtml(s)}
      ${suppressed.length?`<details><summary>${suppressed.length} lower-priority rule${suppressed.length===1?'':'s'} suppressed</summary>
        <div class="suppressedrules">${suppressed.map(rule=>`<code>${esc(rule)}</code>`).join('')}</div></details>`:''}</div>
    <div class="evidencehistory"><h3>Placement history</h3>
      ${cache.error?`<div class="evidenceerror">${esc(cache.error)}</div>`:''}
      ${(cache.events||[]).map(evidenceEventHtml).join('')}
      ${cache.loading?'<div class="ctxload">loading evidence…</div>':''}
      ${cache.loaded&&!(cache.events||[]).length?'<div class="evidenceempty">No earlier transitions recorded.</div>':''}
      ${cache.next_cursor&&!cache.loading?`<button class="historyaction evidenceolder" onclick="loadSessionEvidence('${enc(s.session_id)}',true)">Load older</button>`:''}
    </div>`;
}
async function loadSessionEvidence(encodedSid,more=false){
  const sid=decodeURIComponent(encodedSid),cache=evidenceCache[sid]||(evidenceCache[sid]={events:[]});
  if(cache.loading||(!more&&cache.loaded))return;
  cache.loading=true;cache.error=null;
  const current=((last&&last.sessions)||[]).find(item=>item.session_id===sid)||
    closedSession(sid)||{};
  renderEvidenceRail(current);
  try{
    const cursor=more&&cache.next_cursor?'&cursor='+encodeURIComponent(cache.next_cursor):'';
    const response=await fetch('/api/evidence?sid='+encodeURIComponent(sid)+'&limit=30'+cursor,{cache:'no-store'});
    const data=await response.json();
    if(!data.ok)throw new Error(data.error||'state evidence unavailable');
    cache.events=more?[...(cache.events||[]),...(data.events||[])]:data.events||[];
    cache.next_cursor=data.next_cursor||null;cache.loaded=true;
  }catch(error){cache.error=String(error.message||error);}
  finally{cache.loading=false;}
  if(sessionView&&sessionView.sid===sid&&sessionEvidenceOpen){
    const fresh=((last&&last.sessions)||[]).find(item=>item.session_id===sid)||
      closedSession(sid)||current;
    renderEvidenceRail(fresh);
  }
}
function toggleSessionEvidence(encodedSid){
  const sid=decodeURIComponent(encodedSid);
  sessionEvidenceOpen=!sessionEvidenceOpen;
  const s=((last&&last.sessions)||[]).find(item=>item.session_id===sid)||
    closedSession(sid)||{};
  renderEvidenceRail(s);
  if(sessionEvidenceOpen)loadSessionEvidence(encodedSid);
  if(sessionView&&!sessionView.closed)renderSession(true);else if(sessionView)renderClosed(true);
}
function primarySessionAction(sid){
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(s){openSession(sid);return;}
  if(isClosedSession(sid))openClosed(sid);
}
async function markSessionRevision(payload){
  const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)});
  const d=await r.json();
  if(!d.ok)throw new Error(d.error||'failed');
  return d;
}
async function markRead(s){
  if(!s?.new_response)return;
  s.new_response=false;uiRefresh();
  try{await markSessionRevision({mark_read_session:s.session_id,revision:String(s.convo_v||'')});}
  catch(e){s.new_response=true;uiRefresh();}
}
async function markAvailable(sid,encodedRevision){
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(!s)return;
  const revision=decodeURIComponent(encodedRevision||'');
  s.reply_requested=false;
  if(s.external||s.headless||s.read_only){
    Object.assign(s,{ui_group:'history',reason_label:'External',primary_action:'view',primary_action_label:'View'});
  }else if(s.state==='dormant'){
    Object.assign(s,{ui_group:'history',reason_label:'Inactive',primary_action:'continue',primary_action_label:'Continue'});
  }else{
    Object.assign(s,{ui_group:'available',reason_label:'Available',primary_action:'continue',primary_action_label:'Continue'});
  }
  render(last,true);
  try{await markSessionRevision({mark_available_session:sid,revision});}
  catch(e){alert('mark available failed: '+e);tick();}
}
function openSession(sid){
  closeViewer();           // never stack the file viewer and the chat view
  const session=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(session?.new_response)markRead(session);
  sessionView={sid,closed:false};sessionOpened=true;sessionEvidenceOpen=false;
  $('#sview').style.display='flex';
  syncOverlayHistory();
  renderSession(true);
}
// a closed session has no process: read its transcript, offer no controls
function openClosed(sid){
  closeViewer();
  sessionView={sid,closed:true};sessionOpened=true;sessionEvidenceOpen=false;
  $('#sview').style.display='flex';
  syncOverlayHistory();
  renderClosed(true);
  if(!closedSession(sid))loadClosedMeta(sid);
}
async function loadClosedMeta(sid){
  try{
    const response=await fetch('/api/history?sid='+encodeURIComponent(sid),{cache:'no-store'});
    const data=await response.json();
    if(response.ok&&data.ok&&data.item)closedMeta.set(sid,data.item);
  }catch(_){/* the transcript endpoint still provides a read-only fallback */}
  if(sessionView&&sessionView.closed&&sessionView.sid===sid)renderClosed(true);
}
function closeSession(){
  closeOverflow();sessionView=null;sessionEvidenceOpen=false;slashClose();
  $('#sview').style.display='none';$('#sbody').innerHTML='';$('#sact').innerHTML='';$('#sctrl').innerHTML='';$('#sevidence').innerHTML='';$('#sevidence').classList.remove('open');
}
async function renderClosed(){
  if(!sessionView||!sessionView.closed)return;
  const sid=sessionView.sid;
  const body=$('#sbody');
  const meta=closedSession(sid)||{};
  $('#sctrl').innerHTML=evidenceButton(meta)+overflowMenu('session',meta,'closed');
  renderEvidenceRail(meta);
  $('#sact').innerHTML=`<div class="relaynote">this session is <b>closed</b> — its terminal is gone,
    so there is nothing to send to. The conversation is read-only.</div>
    ${handoffLinksHtml(meta)}
    ${meta.can_reopen?`<div class="freetext"><button class="pbtn send"
      onclick="reopenClosed('${sid}',this)">reopen in terminal</button></div>
      <div class="actmsg" id="reopenmsg-${sid}"></div>`:''}`;
  if(!closedCtx[sid]){
    body.innerHTML='<div class="ctxload">loading conversation…</div>';
    try{
      const r=await fetch(conversationEndpoint('closed',sid),{cache:'no-store'});
      const d=await r.json();
      closedCtx[sid]=d.ok?{messages:d.messages||[],info:d.info||{},next_cursor:d.next_cursor,
                           message_total:d.message_total}
                         :{messages:[],info:{},error:d.error||'unavailable'};
    }catch(e){closedCtx[sid]={messages:[],info:{},error:String(e)};}
    if(!sessionView||sessionView.sid!==sid)return;      // closed while fetching
  }
  const c=closedCtx[sid],info=c.info||{};
  $('#stitle2').innerHTML=`<b>${esc(meta.title||info.project||'closed session')}</b>
    <small>${esc(info.project||meta.project||'')}${info.branch&&info.branch!=='HEAD'?` · ${esc(info.branch)}`:''} · closed</small>`;
  if(c.error)body.innerHTML=`<div class="ctxload">✗ ${esc(c.error)}</div>`;
  else if(!c.messages.length)body.innerHTML='<div class="ctxload">no conversation recorded</div>';
  else body.innerHTML=`<div class="aconvo">${convoMsgs(c,sid,true,'closed')}</div>`;
  body.scrollTop=body.scrollHeight;
  if(sessionOpened){sessionOpened=false;
    requestAnimationFrame(()=>{const b=$('#sbody');b.scrollTop=b.scrollHeight;});}
}
async function reopenClosed(sid,button){
  const original=button&&button.textContent;
  if(button){button.disabled=true;button.textContent='opening…';}
  const result=await act(sid,{type:'reopen'},'reopenmsg');
  if(button){
    button.textContent=result.ok?'opened ✓':original;
    if(!result.ok)button.disabled=false;
  }
  if(result.ok)setTimeout(()=>tick(),500);
  return result;
}
function renderSession(force){
  if(!sessionView||!last)return;
  if(sessionView.closed)return renderClosed(force);
  const s=(last.sessions||[]).find(x=>x.session_id===sessionView.sid);
  if(!s){closeSession();return;}          // session died while open
  ensureCtx(s.session_id,ctxVersion(s));
  const c=ctxCache[s.session_id];
  const ae=document.activeElement;
  const typing=ae&&(ae.tagName==='INPUT')&&$('#sview').contains(ae);
  const done=['done','ended'];
  $('#stitle2').innerHTML=sessTitleBlock(s);
  $('#sctrl').innerHTML=evidenceButton(s)+terminalButton(s)+overflowMenu('session',s,'session');
  renderEvidenceRail(s);
  // A focused composer must not freeze transcript confirmation. The composer
  // itself is preserved below; only defer the body repaint during an active
  // touch gesture so mobile scrolling is not interrupted.
  if(!force&&touching())return;
  const body=$('#sbody');
  const old={top:body.scrollTop,atBottom:body.scrollTop+body.clientHeight>=body.scrollHeight-12};
  // on open, always land at the bottom (newest); otherwise stick to bottom only if already there
  const wantBottom=sessionOpened||old.atBottom;
  sessionOpened=false;
  if(!c||!c.messages)body.innerHTML='<div class="ctxload">loading conversation…</div>';
  else if(!c.messages.length)body.innerHTML='<div class="ctxload">no conversation yet</div>';
  else body.innerHTML=`<div class="aconvo">${convoMsgs(c,s.session_id)}</div>`;
  body.scrollTop=wantBottom?body.scrollHeight:old.top;
  if(typing)return;                        // never replace the input being typed into
  const p=s.pending;
  const hasQ=p&&p.kind==='question'&&p.questions&&p.questions.length&&answered[s.session_id]!==p.nonce;
  const qHtml=hasQ?`<div class="togbox waiting">
      <button class="vchat-toggle" onclick="sessQOpen=!sessQOpen;renderSession(true)">${sessQOpen?'▾ hide question':'▸ show question — waiting on you'}</button>
      ${sessQOpen?`<div class="togbody">${p.questions.length>1?mqBlock(s,p,'smsg'):singleQBlock(s,p,'smsg')}</div>`:''}
    </div>`
    :pendingBox(s,'smsg');   // permission prompts render whole
  const act=$('#sact');
  keepStripScroll(act,()=>{act.innerHTML=`
    ${qHtml}
    ${fileStrip(s.session_id,(c&&c.files)||[])}
    ${handoffLinksHtml(s)}
    ${s.read_only?`<div class="relaynote"><b>view only</b> — ${esc(s.read_only_reason||'this thread is owned by another Codex runtime')}</div>`:''}
    ${s.capabilities?.submit?`<div class="freetext"><input id="sft-${s.session_id}" placeholder="send a message  ·  / or $ for commands and skills" autocomplete="off"
      oninput="slashInput('${s.session_id}','sft')" onfocus="slashInput('${s.session_id}','sft')"
      onkeydown="if(event.key==='Enter')sendText('${s.session_id}','sft','smsg');if(event.key==='Escape')slashClose()">
      <span class="sendpair"><button class="pbtn send" onclick="sendText('${s.session_id}','sft','smsg')">send</button>${scheduleButton(s.session_id,'sft-'+s.session_id)}</span></div>`:''}
    <div class="slashwrap" id="slash-sft-${s.session_id}"></div>
    <div class="actmsg" id="smsg-${s.session_id}"></div>`;});
  // #sact just shrank #sbody — re-pin to the true bottom after layout settles
  if(wantBottom)requestAnimationFrame(()=>{const b=$('#sbody');b.scrollTop=b.scrollHeight;});
}

// ---- subagent chat overlay -------------------------------------------------
// A subagent has NO tty: its "send" box relays through the PARENT session (the
// parent forwards with SendMessage), so it is labelled as a relay, not a channel.
let agentView=null;              // {sid, aid} of the open overlay
const agentCache={};             // parent session + aid -> {v, messages, info}
const agentCacheKey=(sid,aid)=>String(sid||'')+'\0'+String(aid||'');
let agentInfoOpen2=false;        // the info dropdown INSIDE the overlay
function openAgent(sid,aid){
  agentView={sid,aid};
  agentInfoOpen2=false;
  $('#aview').style.display='flex';
  syncOverlayHistory();
  renderAgent(true);
}
function closeAgent(){
  closeOverflow();agentView=null;agentInfoOpen2=false;
  $('#aview').style.display='none';$('#abody').innerHTML='';$('#aact').innerHTML='';$('#actrl').innerHTML='';
}
function agentMeta(){
  if(!agentView||!last)return null;
  const s=(last.sessions||[]).find(x=>x.session_id===agentView.sid);
  return s?(s.agents||[]).find(a=>a.agent_id===agentView.aid):null;
}
async function ensureAgentCtx(){
  if(!agentView)return;
  const a=agentMeta(),aid=agentView.aid,key=agentCacheKey(agentView.sid,aid);
  const v=a?a.convo_v:0;
  const c=agentCache[key];
  if(c&&c.v===v)return;
  if(c&&c.fetching)return;
  agentCache[key]={...(c||{}),fetching:true};
  try{
    const r=await fetch(conversationEndpoint('agent',agentView.sid,aid),{cache:'no-store'});
    const d=await r.json();
    agentCache[key]=d.ok?{v,messages:d.messages||[],info:d.info||{},
      next_cursor:d.next_cursor,message_total:d.message_total}:
      {v,messages:[],info:{},error:d.error||'unavailable'};
  }catch(e){agentCache[key]={v,messages:[],info:{},error:String(e)};}
  renderAgent(true);
}
function renderAgent(force){
  if(!agentView)return;
  ensureAgentCtx();
  const a=agentMeta(),c=agentCache[agentCacheKey(agentView.sid,agentView.aid)];
  const info=(c&&c.info)||{};
  const done=a?['done','ended'].includes(a.state):true;
  $('#atitle').innerHTML=`<b>${esc(info.agent_type||(a&&a.agent_type)||'subagent')}</b>
    <small>${esc(info.description||(a&&a.description)||'')}</small>`;
  const par=((last&&last.sessions)||[]).find(x=>x.session_id===agentView.sid);
  $('#actrl').innerHTML=overflowMenu('subagent',par,'subagent',done);
  const ae=document.activeElement;
  const typing=ae&&ae.tagName==='INPUT'&&$('#aview').contains(ae);
  if(!force&&(typing||touching()))return;
  const body=$('#abody');
  const old={top:body.scrollTop,atBottom:body.scrollTop+body.clientHeight>=body.scrollHeight-12};
  if(!c||!c.messages){body.innerHTML='<div class="ctxload">loading conversation…</div>';}
  else if(c.error){body.innerHTML=`<div class="ctxload">✗ ${esc(c.error)}</div>`;}
  else if(!c.messages.length){body.innerHTML='<div class="ctxload">no conversation yet</div>';}
  else body.innerHTML=`<div class="aconvo">${convoMsgs(c,agentView.sid,false,'agent',agentView.aid)}</div>`;
  body.scrollTop=old.atBottom?body.scrollHeight:old.top;
  if(!typing){
    const ago=ts=>ts?fmtAge(Math.max(0,Math.round((Date.now()-Date.parse(ts))/1000)))+' ago':'?';
    const tk=info.tokens||{};
    const codex=par&&par.provider==='codex';
    $('#aact').innerHTML=`
      <div class="relaynote">${done?'this agent has finished — ':''}${codex
        ?'App Server does not accept direct input to v2 subagents. This message goes to the <b>parent thread</b> with an explicit relay instruction.'
        :'subagents have no terminal of their own: your message is typed into the <b>parent session</b>, tagged for it to forward with SendMessage'}</div>
      ${!done&&par?.capabilities?.relay_agent?`<div class="freetext"><input id="aft" placeholder="relay a message via the parent session" autocomplete="off"
        onkeydown="if(event.key==='Enter')sendRelay()">
        <span class="sendpair"><button class="pbtn send" onclick="sendRelay()">relay</button>${scheduleButton(agentView.sid,'aft',agentView.aid)}</span></div>`:''}
      <div class="actmsg" id="amsg"></div>
      <details class="dfold" ${agentInfoOpen2?'open':''} ontoggle="agentInfoOpen2=this.open">
        <summary>agent info</summary>
        <div class="kv">
          <span>agent</span>${cpb(info.agent_id||agentView.aid)}
          <span>type</span><b>${esc(info.agent_type||'?')}</b>
          <span>description</span><b>${esc(info.description||'—')}</b>
          <span>model</span><b>${esc(info.model||'?')}${info.effort?` · ${esc(info.effort)}`:''}</b>
          <span>state</span><b>${a?a.state:'closed'}${a&&!done?` · quiet ${fmtAge(a.quiet_s)}`:''}</b>
          <span>parent</span>${cpb(agentView.sid)}
          <span>started</span><b>${ago(info.started)}</b>
          <span>last activity</span><b>${ago(info.last)}</b>
          <span>tokens</span><b>${info.total_tokens==null&&tk.in==null?'unavailable':`${fmtTok(tk.in||0)} in · ${fmtTok(tk.cache_write||0)} cache write · ${fmtTok(tk.cache_read||0)} cache read · ${fmtTok(tk.out||0)} out`}</b>
          <span>cost</span><b>${info.cost==null?'unavailable':fmt$(info.cost)}</b>
        </div>
      </details>`;
  }
}
function sendRelay(){
  if(!agentView)return;
  const inp=$('#aft'),v=(inp&&inp.value||'').trim();
  if(!v)return;
  act(agentView.sid,{type:'relay',agent_id:agentView.aid,text:v},'amsg');
  if(inp)inp.value='';
}
function agentRow(a){
  const done=['done','ended'].includes(a.state);
  const signal={running:'working',stalled:'quiet — may still be working',done:'finished',
    ended:'stopped'}[a.state]||String(a.state||'unknown');
  const ago=ts=>ts?fmtAge(Math.max(0,Math.round((Date.now()-Date.parse(ts))/1000)))+' ago':'?';
  const tk=a.tokens||{};
  // tapping opens the subagent conversation
  const tap=`agentTap(event,'${a.session_id}','${a.agent_id}')`;
  return`<div class="arow ${done?'done-row':''}" style="padding-left:${12+a.depth*16}px;cursor:pointer"
    title="open this subagent's conversation" onclick="${tap}">
    <span class="tree">⎿</span>
    <span class="dot ${a.state}" role="img" aria-label="${esc(signal)}" title="${esc(signal)}"></span>
    <span class="atype">${esc(a.agent_type)}</span>
    <span class="adesc">${esc(a.description)}</span>
    <span class="amodel">${modelLabel(a)}</span>
    ${!done&&a.tok_per_s>0?`<span class="anum" title="throughput: tokens per second this agent is processing, cache reads included — a liveness signal, not output speed">${fmtTok(Math.round(a.tok_per_s))} tok/s</span>`:''}
    ${!done?spark(a.spark):''}
    <span class="anum">${fmtTok(a.total_tokens)}</span>
    <span class="acost">${fmt$(a.cost)}</span>
    <span class="aopen">›</span>
  </div>${previewAgents()&&a.last_msg?`<div class="lastmsg amsgprev" style="padding-left:${28+a.depth*16}px" onclick="${tap}">
    <span class="lmwho ${a.last_msg.role}">${a.last_msg.role==='user'?'task':'agent'}</span><span class="lmtext peekmd" style="--peek-lines:${clampA()}">${peekMd(a.last_msg.text)}</span></div>`:''}`;
}
// Pins are one shared server-side watchlist. Cards relocate to the top; they are
// never duplicated in their normal action group.
const pinnedSessions=new Set();
function syncPinnedSessions(f){
  pinnedSessions.clear();
  (((f||{}).settings||{}).pinned_sessions||[]).forEach(sid=>pinnedSessions.add(sid));
}
function agentListHtml(agents){
  return agents.map(agentRow).join('');
}
async function toggleSessionPin(sid){
  const pinned=!pinnedSessions.has(sid);
  pinned?pinnedSessions.add(sid):pinnedSessions.delete(sid);
  if(last?.settings)last.settings.pinned_sessions=[...pinnedSessions];
  render(last,true);
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pin_session:sid,pinned})});
    const d=await r.json();
    if(!d.ok)throw new Error(d.error||'failed');
    if(last?.settings)last.settings.pinned_sessions=d.pinned_sessions||[];
    syncPinnedSessions(last);render(last,true);
  }catch(e){
    pinned?pinnedSessions.delete(sid):pinnedSessions.add(sid);
    if(last?.settings)last.settings.pinned_sessions=[...pinnedSessions];
    render(last,true);alert('pin failed: '+e);
  }
}
// mobile: long-press a session header to pin; a short tap still opens its chat
let sessionPressTimer=null,sessionLongFired=false;
function sessionPressStart(sid){
  sessionLongFired=false;
  sessionPressTimer=setTimeout(()=>{sessionLongFired=true;toggleSessionPin(sid);
    if(navigator.vibrate)navigator.vibrate(15);},500);
}
function sessionPressEnd(){if(sessionPressTimer){clearTimeout(sessionPressTimer);sessionPressTimer=null;}}
function sessionTap(e,sid){
  if(sessionLongFired){sessionLongFired=false;e.stopPropagation();return;}
  openSession(sid);
}
function agentTap(e,sid,aid){
  e.stopPropagation();
  openAgent(sid,aid);
}
function renderPinned(f,predicate=()=>true){
  const el=$('#pinned');
  const sessions=(f&&f.sessions)||[],closed=(f&&f.closed)||[];
  const items=sessions.filter(s=>pinnedSessions.has(s.session_id)&&predicate(s));
  const archived=closed.filter(s=>pinnedSessions.has(s.session_id)&&predicate(s));
  if(!items.length&&!archived.length){el.className='empty';el.innerHTML='';return;}
  el.className='';
  if(!el.querySelector('.pinhdr'))el.innerHTML='<div class="pinhdr">Pinned</div><div class="pinlist"></div><div class="pinarchived"></div>';
  reconcileCards(el.querySelector('.pinlist'),items,'');
  el.querySelector('.pinarchived').innerHTML=archived.map(c=>historyRow(c,true)).join('');
}
const setg=()=>((last&&last.settings)||{});
const previewAgents=()=>!!setg().preview_agents;
const previewSessions=()=>setg().preview_sessions!==false;
const clampS=()=>Math.max(1,Math.min(6,setg().preview_session_lines??2));
const clampA=()=>Math.max(1,Math.min(6,setg().preview_agent_lines??1));
const readerWidth=()=>setg().reader_width==='centered'?'centered':'fit';
function applyReaderWidth(){document.documentElement.dataset.readerWidth=readerWidth();}

function cardCls(s){
  if(s.ui_group==='needs_you')return s.reason_label==='Fix needed'?'stalled':'needs';
  if(s.ui_group==='available')return'idle';
  if(s.ui_group==='history')return'dorm';
  return s.reason_label==='Slow'?'stalled':'';
}
// The volatile top of the card — rebuilt every poll (header, meta, peek, pending,
// running agents, the more/less toggle). No native <details> here, so replacing it
// each tick doesn't flash.
function cardTop(s){
  const isOpen=open.has(s.session_id);
  const running=s.agents.filter(a=>!['done','ended'].includes(a.state));
  const activeSession=s.ui_group==='working';
  // delivered-file chips + the session peek both need the context cache; the
  // conversation itself now lives only in the full view
  if(isOpen||(previewSessions()&&s.last_msg))ensureCtx(s.session_id,ctxVersion(s));
  const pinned=pinnedSessions.has(s.session_id);
  return`<div class="shead" title="open the full conversation" onclick="sessionTap(event,'${s.session_id}')"
      ontouchstart="sessionPressStart('${s.session_id}')" ontouchend="sessionPressEnd()" ontouchmove="sessionPressEnd()">
      <span class="chip ${s.ui_group||s.state}${s.reason_label==='Fix needed'?' problem':''}">${esc(s.reason_label||stateLabel[s.state]||s.state)}</span>
      <span class="sname">${s.title?`<span class="stitle">${esc(s.title)}</span><small>${esc(s.project)}${s.branch&&s.branch!=='HEAD'?` · ${esc(s.branch)}`:''}</small>`:`${esc(s.project)}${s.branch&&s.branch!=='HEAD'?` <small>· ${esc(s.branch)}</small>`:''}`}</span>
      <span class="m" title="session provider">${esc(s.provider||'claude')}</span>
      ${s.access==='view_only'?`<span class="accessbadge view_only">view only</span>`:''}
      ${s.new_response?`<span class="newbadge">new</span>`:''}
      <button class="primarybtn" onclick="event.stopPropagation();primarySessionAction('${s.session_id}')">${esc(s.primary_action_label||'Open')}</button>
      ${terminalButton(s,true)}
      <button class="spin${pinned?' on':''}" title="${pinned?'unpin session':'pin session to top'}"
        aria-label="${pinned?'unpin session':'pin session to top'}"
        onclick="event.stopPropagation();toggleSessionPin('${s.session_id}')">📌</button>
    </div>
    <div class="smeta">
      <div class="smeta-l">
        ${s.agents_running?`<span class="m"><b style="color:var(--green)">${s.agents_running} agent${s.agents_running>1?'s':''}</b></span>`:''}
        ${s.running?`<span class="m runskill" title="the skill or slash command this turn is running">${esc(s.running)}</span>`:''}
        ${s.compacting!=null?`<span class="m compacting" title="a compaction is running — the transcript is frozen until it finishes">⧉ compacting ${fmtAge(s.compacting)}</span>`:''}
        <span class="squiet">quiet ${fmtAge(s.quiet_s)}</span>
      </div>
      <div class="smeta-r">
        ${s.ctx_pct==null?`<span class="m">${fmtTok(s.ctx_tokens||0)} tok</span>`:`<span class="ctxwrap"><span>${s.ctx_pct}%</span><span class="ctxbar"><i style="width:${Math.min(s.ctx_pct||0,100)}%;background:${s.ctx_pct>=60?'var(--red)':s.ctx_pct>=50?'var(--amber)':'var(--blue)'}"></i></span></span>`}
        <span class="m amodel">${modelLabel(s)}</span>
      </div>
    </div>
    ${previewSessions()&&s.last_msg?`<div class="lastmsg sessionpeek${expandedPeeks.has(s.session_id)?' expanded':''}" title="open the full conversation" onclick="openSession('${s.session_id}')"><span class="lmwho ${s.last_msg.role}">${s.last_msg.role==='user'?'you':esc(s.provider||'claude')}</span><div class="peekbody"><div class="lmtext peekmd" style="--peek-lines:${clampS()}">${peekMd(s.last_msg.text)}</div><button class="peektoggle ${expandedPeeks.has(s.session_id)?'less':'more'}" type="button" aria-label="${expandedPeeks.has(s.session_id)?'collapse latest message':'expand latest message'}" onclick="event.stopPropagation();togglePeek('${s.session_id}',${expandedPeeks.has(s.session_id)?'false':'true'})">${expandedPeeks.has(s.session_id)?'Less':'...'}</button></div></div>`:''}
    ${s.error?`<div class="lastmsg"><span class="lmwho">provider</span><span class="lmtext">${esc(s.error)}</span></div>`:''}
    ${s.reply_requested?`<div class="replysignal"><span>Waiting for your reply</span><button onclick="event.stopPropagation();markAvailable('${s.session_id}','${enc(String(s.convo_v||''))}')">mark available</button></div>`:''}
    ${cardResponseFeedback(s)}
    ${cardPending(s)}
    ${activeSession&&running.length?`<div class="agents">${agentListHtml(running)}</div>`:''}
    <button class="morebtn" onclick="toggle('${s.session_id}')">${isOpen?'▾ less':'▸ more'}</button>`;
}
// The card tail ("more"): reference material with native <details> folds. It is
// rebuilt ONLY when detailSig changes (not every poll), so its open dropdowns
// don't remount every tick — that remount was the "blinking".
function cardDetail(s){
  const done=s.agents.filter(a=>['done','ended'].includes(a.state));
  const ctxFiles=((ctxCache[s.session_id]||{}).files)||[];
  return`<div class="detail">
      <div class="mutebox">
        <button class="bell ${s.muted?'muted':''}"
          onclick="event.stopPropagation();toggleMute('${s.session_id}',${s.muted?'false':'true'})">${s.muted?'🔕':'🔔'}</button>
        <span>${s.muted?'push notifications muted for this session':'notify me about this session'}</span>
      </div>
      <div class="actmsg" id="msg-${s.session_id}"></div>
      ${handoffLinksHtml(s)}
      <details class="dfold statewhy" ${stateInfoOpen.has(s.session_id)?'open':''}
        ontoggle="stateInfoOpen[this.open?'add':'delete']('${s.session_id}')">
        <summary>why this is ${esc((s.reason_label||s.ui_group||'here').toLowerCase())}</summary>
        <div class="statewhybody"><div class="evidencerule"><span>Winning rule</span><code>${esc(s.winning_rule||'placement.unknown')}</code></div>
          ${evidenceFactsHtml(s)}</div>
      </details>
      <details class="dfold" ${infoOpen.has(s.session_id)?'open':''}
        ontoggle="infoOpen[this.open?'add':'delete']('${s.session_id}')">
        <summary>session info</summary>
        <div class="kv">
          <span>session</span>${cpb(s.session_id)}
          <span>provider</span><b>${esc(s.provider||'claude')}</b>
          <span>pid</span><b>${s.pid}</b>
          <span>cwd</span>${cpb(s.cwd)}
          <span>model</span><b>${esc(s.model||'?')}${s.effort?` · ${esc(s.effort)}`:''}</b>
          ${s.provider==='codex'?`<span>mode</span><b>${esc(s.collaboration_mode||'default')}</b>`:''}
          <span>started</span><b>${s.started_ms?fmtAge(Math.round(Date.now()/1000-s.started_ms/1000))+' ago':'?'}</b>
          <span>cli status</span><b>${esc(s.reg_status||'—')}</b>
          <span>tokens in ctx</span><b>${fmtTok(s.ctx_tokens)}</b>
          <span>spend</span><b>${s.cost_source==='unavailable'?'unavailable — App Server reports tokens, not currency':`${fmt$(s.cost)} session + ${fmt$(s.agent_cost)} agents = ${fmt$(s.cost+s.agent_cost)}`}</b>
          ${s.error?`<span>provider error</span><b>${esc(s.error)}</b>`:''}
          ${!s.capabilities?.focus_terminal&&s.provider==='codex'?`<span>terminal focus</span><b>${esc(s.capabilities?.focus_terminal_reason||'unavailable')}</b>`:''}
        </div>
      </details>
      <details class="dfold" ${filesOpen.has(s.session_id)?'open':''}
        ontoggle="filesOpen[this.open?'add':'delete']('${s.session_id}')">
        <summary>${s.provider==='codex'?'changed / generated':'delivered'} files (${ctxFiles.length})</summary>
        <div class="foldscroll">${ctxFiles.length?ctxFiles.map(f=>`<div class="frow">${fchip(s.session_id,f,f.caption)}
          <span class="fmeta">${esc(f.caption||'')}</span>
          <span class="anum">${f.ts?fmtAge(Math.max(0,Math.round((Date.now()-Date.parse(f.ts))/1000)))+' ago':''}</span>
        </div>`).join(''):'<div class="dhead">none yet</div>'}</div>
      </details>
      <details class="dfold" ${doneOpen.has(s.session_id)?'open':''}
        ontoggle="doneOpen[this.open?'add':'delete']('${s.session_id}')">
        <summary>completed agents (${done.length})</summary>
        <div class="foldscroll">${done.length?agentListHtml(done):'<div class="dhead">none yet</div>'}</div>
      </details>
      ${s.bridge_url?`<div><a class="jump" href="${s.bridge_url}" target="_blank">open in claude.ai ↗</a></div>`:''}
    </div>`;
}
// what the tail depends on, EXCLUDING per-second time (started/delivered ages) so
// the panel isn't rebuilt every poll just because a clock ticked
function detailSig(s){
  const done=s.agents.filter(a=>['done','ended'].includes(a.state)).length;
  const files=((ctxCache[s.session_id]||{}).files||[]).length;
  return[s.muted,s.pid,s.model,s.effort,s.collaboration_mode,s.reg_status,s.ctx_tokens,
    s.cost==null?'na':Math.round(((s.cost||0)+(s.agent_cost||0))*100),s.error||'',done,files,
    s.winning_rule||'',s.state_confidence||'',s.provider_stale?'stale':'fresh',
    (s.handoff_links||[]).map(link=>[link.direction,link.session_id,link.status].join(':')).join(',')].join('|');
}
// used only for the (wholesale-rendered) dormant fold; live cards go through reconcileCards
function sessionCard(s){
  const isOpen=open.has(s.session_id);
  return`<div class="card ${cardCls(s)}${isOpen?' open':''}" data-sid="${s.session_id}">
    <div class="ctop">${cardTop(s)}</div>${isOpen?cardDetail(s):''}</div>`;
}
// Reconcile #sessions in place: persist each card node, rebuild only the volatile
// top every poll, and rebuild the tail only when detailSig changes. This keeps an
// expanded card's open <details> from remounting (and flashing) every tick.
function reconcileCards(container,list,emptyMessage='no live sessions'){
  if(!list.length){container.innerHTML=emptyMessage?`<div class="empty">${esc(emptyMessage)}</div>`:'';return;}
  if(container.querySelector('.empty'))container.innerHTML='';
  const seen=new Set();
  list.forEach(s=>{
    seen.add(s.session_id);
    const isOpen=open.has(s.session_id);
    let card=container.querySelector('.card[data-sid="'+s.session_id+'"]');
    if(!card){
      card=document.createElement('div');card.dataset.sid=s.session_id;
      const top=document.createElement('div');top.className='ctop';card.appendChild(top);
      container.appendChild(card);
    }
    card.className='card'+(cardCls(s)?' '+cardCls(s):'')+(isOpen?' open':'')+(pinnedSessions.has(s.session_id)?' pinned':'');
    card.querySelector('.ctop').innerHTML=cardTop(s);
    let detail=card.querySelector(':scope > .detail');
    if(isOpen){
      const sig=detailSig(s);
      if(!detail||card.dataset.dsig!==sig){
        if(detail)detail.remove();
        card.insertAdjacentHTML('beforeend',cardDetail(s));
        card.dataset.dsig=sig;
      }
    }else if(detail){detail.remove();delete card.dataset.dsig;}
  });
  [...container.children].forEach(el=>{if(el.classList.contains('card')&&!seen.has(el.dataset.sid))el.remove();});
  list.forEach((s,i)=>{
    const el=container.querySelector('.card[data-sid="'+s.session_id+'"]');
    if(el&&container.children[i]!==el)container.insertBefore(el,container.children[i]||null);
  });
}

const multiSel={};   // sessionId -> Set of chosen digits (multiSelect questions)
const mqSel={};      // sessionId -> {nonce, qi, a:{qIdx:Set(digits)}, other:{qIdx:text}}
const otherDraft={}; // sessionId -> single-question "Other" draft (survives re-renders)
const elicitDraft={}; // sessionId -> field values for MCP elicitation forms
const answered={};   // sessionId -> nonce already sent: hide the selector instantly
let settingsOpen=false,budgetSettingsOpen=false;
function uiRefresh(){render(last,true);if(viewerSid)renderViewerBar(true);
  if(sessionView)renderSession(true);if(agentView)renderAgent(true);if(settingsOpen)renderSettings();}
function openSettings(){
  settingsOpen=true;
  $('#settingsview').style.display='flex';
  $('#settings').scrollTop=0;
  renderSettings();
  loadBudgets();
  loadWorkstreams();
  syncOverlayHistory();
}
function closeSettings(){
  settingsOpen=false;
  $('#settingsview').style.display='none';
  $('#settings').innerHTML='';
  applyRouteNav(currentRoute);
}
function renderSettings(){
  const el=$('#settings');
  if(!settingsOpen){el.innerHTML='';return;}
  const nt=(last&&last.notify)||{};
  const st=(last&&last.settings)||{};
  const num=(k,step)=>`<input type="number" value="${st[k]??''}" min="0" step="${step}"
    onchange="setNum('${k}',this.value)">`;
  const row=(k,lbl,tail)=>`<label class="setrow"><input type="checkbox" ${nt[k]!==false?'checked':''}
    onchange="setNotify('${k}',this.checked)">${lbl}${tail?`<span class="setnum"> — ${tail}</span>`:''}</label>`;
  el.innerHTML=`<div class="setpanel"><div class="dhead">push notifications (ntfy)</div>
    ${row('needs_you','waiting on you',`after ${num('awaiting_input_notify_seconds',30)} s blocked`)}
    ${row('stall','session stalled',`frozen > ${num('stall_seconds',30)} s (also drives the chip)`)}
    ${row('spend','spend threshold',`every $ ${num('spend_threshold_usd',1)}`)}
    ${row('fleet_quiet','fleet gone quiet',`idle ${num('fleet_quiet_minutes',1)} min first (0 = right away)`)}
    ${row('scheduled_digest','daily briefing push','off by default')}
    <div class="digestsettings"><label><span>Daily time</span><input type="time" value="${esc(st.digest_schedule_time||'09:00')}"
      onchange="setStr('digest_schedule_time',this.value)"></label><label><span>IANA timezone</span><input value="${esc(st.digest_schedule_zone||Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC')}"
      onchange="setStr('digest_schedule_zone',this.value)"></label></div>
    <div class="setnum" style="padding:6px 0 2px">tap-target for pushes (opens on tap; blank = none)</div>
    <div class="freetext" style="margin-top:0"><input placeholder="https://your-mac.tailnet.ts.net"
      value="${esc(st.dashboard_url||'')}" onchange="setStr('dashboard_url',this.value)"></div>
    <div class="setnum" style="padding:6px 0 2px">per-session mute: tap the 🔔 on a card</div>
    <div class="dhead" style="margin-top:12px">full-screen reading width</div>
    <div class="setchoice" role="group" aria-label="Full-screen reading width">
      <button aria-pressed="${(st.reader_width||'fit')==='fit'}"
        onclick="setStr('reader_width','fit')">Fit the screen</button>
      <button aria-pressed="${st.reader_width==='centered'}"
        onclick="setStr('reader_width','centered')">Centered · fixed width</button>
    </div>
    <div class="sethint">Applies to session chat, Markdown, and subagent conversations.</div>
    <div class="dhead" style="margin-top:12px">conversation peek</div>
    <label class="setrow"><input type="checkbox" ${st.preview_sessions!==false?'checked':''}
      onchange="setBool('preview_sessions',this.checked)">on session cards<span class="setnum">
      — ${num('preview_session_lines',1)} line(s)</span></label>
    <label class="setrow"><input type="checkbox" ${st.preview_agents?'checked':''}
      onchange="setBool('preview_agents',this.checked)">on subagent rows<span class="setnum">
      — ${num('preview_agent_lines',1)} line(s)</span></label>
    <details class="budgetsettingsfold" ${budgetSettingsOpen?'open':''} ontoggle="budgetSettingsOpen=this.open"><summary>Budgets and spawn limits</summary>${budgetSettingsHtml()}</details>
    <div class="actmsg" id="setmsg"></div></div>`;
}
async function setNotify(k,v){
  const msg=$('#setmsg');
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({notify:{[k]:v}})});
    const d=await r.json();
    if(d.ok){if(last)last.notify=d.notify;if(msg)msg.textContent='saved ✓';}
    else if(msg)msg.textContent='✗ '+(d.error||'failed');
  }catch(e){if(msg)msg.textContent='✗ '+e;}
}
async function setNum(k,v){
  const msg=$('#setmsg');
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({[k]:parseFloat(v)})});
    const d=await r.json();
    if(d.ok){if(last&&last.settings)last.settings[k]=d[k];if(msg)msg.textContent='saved ✓';
      uiRefresh();}   // a peek-line change must repaint the cards, not wait for the poll
    else if(msg)msg.textContent='✗ '+(d.error||'failed');
  }catch(e){if(msg)msg.textContent='✗ '+e;}
}
async function setBool(k,v){
  const msg=$('#setmsg');
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({[k]:v})});
    const d=await r.json();
    if(d.ok){if(last&&last.settings)last.settings[k]=d[k];if(msg)msg.textContent='saved ✓';uiRefresh();}
    else if(msg)msg.textContent='✗ '+(d.error||'failed');
  }catch(e){if(msg)msg.textContent='✗ '+e;}
}
async function setStr(k,v){
  const msg=$('#setmsg');
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({[k]:v})});
    const d=await r.json();
    if(d.ok){if(last&&last.settings)last.settings[k]=d[k];if(msg)msg.textContent='saved ✓';uiRefresh();}
    else if(msg)msg.textContent='✗ '+(d.error||'failed');
  }catch(e){if(msg)msg.textContent='✗ '+e;}
}
async function toggleMute(sid,mute){
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mute_session:sid,muted:mute})});
    const d=await r.json();
    if(!d.ok)return alert(d.error||'failed');
    const s=((last||{}).sessions||[]).find(x=>x.session_id===sid);
    if(s)s.muted=mute;
    uiRefresh();
  }catch(e){alert(e);}
}
// multi-question asks: ONE question on screen at a time, ‹ › to move between them
// (keeps a 3-question ask from swallowing the whole screen)
function mqBlock(s,p,pre){
  const sid=s.session_id;
  let st=mqSel[sid];
  if(!st||st.nonce!==p.nonce)st=mqSel[sid]={nonce:p.nonce,qi:0,a:{},other:{}};
  const qs=p.questions;
  if(st.qi>=qs.length)st.qi=qs.length-1;
  const qi=st.qi,q=qs[qi],ms=!!q.multiSelect;
  const sel=st.a[qi]||new Set();
  const ansd=i=>((st.a[i]&&st.a[i].size)||(st.other[i]||'').trim())?1:0;
  const donecnt=qs.reduce((a,_,i)=>a+ansd(i),0);
  const oth=(st.other[qi]||'').trim();
  const picked=[...sel].sort().map(d=>(q.options[d-1]||{}).label)
    .concat(oth?['“'+oth+'”']:[]).join(', ');
  return`<div class="ptool"><span class="ptlabel">multi-part question (${qs.length}) — waiting on you</span>
      <button class="xbtn" title="${p.dismiss_action==='cancel_turn'?'dismiss by stopping this Codex turn':'dismiss — chat about this instead'}" onclick="sendDismiss('${sid}','${p.nonce}','${pre}')">✕</button></div>
    ${p.files&&p.files.length?`<div class="pfiles"><span class="plabel">read first</span>${p.files.map(f=>fchip(sid,f,f.caption)).join('')}</div>`:''}
    <div class="mqnav"><button class="mqarr" ${qi===0?'disabled':''} onclick="mqNav('${sid}',-1)">‹</button>
      <span class="mqpos"><b>${qi+1}</b> of ${qs.length} · ${donecnt}/${qs.length} answered</span>
      <button class="mqarr" ${qi===qs.length-1?'disabled':''} onclick="mqNav('${sid}',1)">›</button></div>
    <div class="qtext"><b>${esc(q.header||'')}</b> ${esc(q.question)}${ms?' <small>(pick all that apply)</small>':''}</div>
    ${(q.options||[]).map((o,i)=>`<button class="optbtn ${sel.has(i+1)?'sel':''}"
        onclick="mqToggle('${sid}',${qi},${i+1},${ms},${qs.length})">${esc(o.label)}${o.description?`<small>${esc(o.description)}</small>`:''}</button>`).join('')}
    ${q.allowOther!==false?`<div class="freetext"><input placeholder="Other — type your own answer" ${q.secret?'type="password"':''} value="${esc(st.other[qi]||'')}"
      oninput="mqOther('${sid}',${qi},this.value)"></div>`:''}
    <div class="mqsum">selected: ${picked?esc(picked):'—'}</div>
    <div class="pbtns"><button class="pbtn send" onclick="mqSend('${sid}','${p.nonce}','${pre}')">submit all answers</button></div>`;
}
function mqToggle(sid,qi,d,multi,total){
  const st=mqSel[sid];if(!st)return;
  const sel=st.a[qi]||(st.a[qi]=new Set());
  if(multi){sel.has(d)?sel.delete(d):sel.add(d);}
  else{st.a[qi]=new Set([d]);st.other[qi]='';
       if(st.qi<total-1)st.qi++;}      // picked → advance, like the TUI
  uiRefresh();
}
function mqNav(sid,d){const st=mqSel[sid];if(!st)return;st.qi=Math.max(0,st.qi+d);uiRefresh();}
function mqOther(sid,qi,v){const st=mqSel[sid];if(st)st.other[qi]=v;}
function mqSend(sid,nonce,pre){
  const s=((last||{}).sessions||[]).find(x=>x.session_id===sid);
  const p=s&&s.pending;const st=mqSel[sid];
  if(!p||!st||st.nonce!==nonce)return alert('the prompt changed — refresh');
  const answers=[];
  for(let qi=0;qi<p.questions.length;qi++){
    const digits=[...(st.a[qi]||[])].sort();
    const other=(st.other[qi]||'').trim();
    if(!digits.length&&!other){st.qi=qi;uiRefresh();return alert('answer question '+(qi+1)+' first');}
    const a={multi:!!p.questions[qi].multiSelect,n_options:(p.questions[qi].options||[]).length};
    if(a.multi){if(digits.length)a.digits=digits;if(other)a.other=other;}
    else if(other)a.other=other;
    else a.digits=digits;
    answers.push(a);
  }
  const optimisticId=addOptimistic(sid,answerPreview(sid,answers),'answer');
  act(sid,{type:'multiq',nonce,answers},pre,optimisticId);
}
function elicitationBlock(s,p,pre){
  const sid=s.session_id;
  const draft=elicitDraft[sid]=elicitDraft[sid]||{};
  const fields=(p.fields||[]).map((f,i)=>{
    const key=enc(f.name),value=draft[f.name];
    if(f.type==='select'&&f.multiSelect)return`<div class="qtext"><b>${esc(f.label)}</b>${f.required?' *':''}</div>
      ${(f.options||[]).map(o=>`<button class="optbtn ${(value||[]).some(v=>String(v)===String(o.value))?'sel':''}"
        onclick="elicitSet('${sid}','${key}','${enc(JSON.stringify(o.value))}',true)">${esc(o.label)}</button>`).join('')}`;
    if(f.type==='select')return`<label class="qtext"><b>${esc(f.label)}</b>${f.required?' *':''}
      <select onchange="elicitValue('${sid}','${key}',this.value)"><option value="">choose…</option>
      ${(f.options||[]).map(o=>`<option value="${enc(JSON.stringify(o.value))}" ${String(value)===String(o.value)?'selected':''}>${esc(o.label)}</option>`).join('')}</select></label>`;
    if(f.type==='boolean')return`<label class="setrow"><input type="checkbox" ${value?'checked':''}
      onchange="elicitBool('${sid}','${key}',this.checked)">${esc(f.label)}</label>`;
    return`<label class="qtext"><b>${esc(f.label)}</b>${f.required?' *':''}<input ${f.secret?'type="password"':''}
      value="${esc(value??'')}" oninput="elicitText('${sid}','${key}',this.value)"></label>`;
  }).join('');
  const safeUrl=String(p.url||'').startsWith('https://')||String(p.url||'').startsWith('http://');
  return`<div class="pend"><div class="ptool"><span class="ptlabel">${esc(p.server||'MCP')} request — waiting on you</span></div>
    <div class="qtext">${esc(p.message||'')}</div>${fields}
    ${safeUrl?`<a class="jump" href="${esc(p.url)}" target="_blank" rel="noopener">open request ↗</a>`:''}
    <div class="pbtns">
      <button class="pbtn allow" onclick="sendElicitation('${sid}','${p.nonce}','accept','${pre}')">accept</button>
      <button class="pbtn deny" onclick="sendElicitation('${sid}','${p.nonce}','decline','${pre}')">decline</button>
      <button class="pbtn" onclick="sendElicitation('${sid}','${p.nonce}','cancel','${pre}')">cancel</button>
    </div><div class="actmsg" id="${pre}-${sid}"></div></div>`;
}
function elicitText(sid,key,value){(elicitDraft[sid]||(elicitDraft[sid]={}))[decodeURIComponent(key)]=value;}
function elicitBool(sid,key,value){(elicitDraft[sid]||(elicitDraft[sid]={}))[decodeURIComponent(key)]=value;}
function elicitValue(sid,key,value){
  const name=decodeURIComponent(key);if(!value)delete (elicitDraft[sid]||{})[name];
  else (elicitDraft[sid]||(elicitDraft[sid]={}))[name]=JSON.parse(decodeURIComponent(value));
}
function elicitSet(sid,key,encoded,toggle){
  const name=decodeURIComponent(key),value=JSON.parse(decodeURIComponent(encoded));
  const draft=elicitDraft[sid]||(elicitDraft[sid]={}),values=Array.isArray(draft[name])?draft[name]:[];
  const at=values.findIndex(v=>String(v)===String(value));if(at>=0)values.splice(at,1);else values.push(value);
  draft[name]=values;uiRefresh();
}
function sendElicitation(sid,nonce,choice,pre){
  const s=((last||{}).sessions||[]).find(x=>x.session_id===sid),p=s&&s.pending;
  if(!p||p.nonce!==nonce)return alert('the request changed — refresh');
  const content={...(elicitDraft[sid]||{})};
  if(choice==='accept')for(const f of p.fields||[]){
    if(f.required&&(content[f.name]==null||content[f.name]===''||(Array.isArray(content[f.name])&&!content[f.name].length)))
      return alert(`${f.label||f.name} is required`);
  }
  act(sid,{type:'elicitation',nonce,choice,content:choice==='accept'?content:undefined},pre);
}
// On the CARD a question is only a SIGNAL — the option buttons, Other input and
// per-question nav ate the fleet list. Tapping it opens the full view with the
// question expanded. Permission prompts are small and stay inline (allow/deny).
function cardPending(s){
  const p=s.pending;
  if(!p||(p.nonce&&answered[s.session_id]===p.nonce))return'';
  if(p.kind==='permission')return pendingBox(s,'msg');
  if(p.kind==='elicitation')return`<div class="pend qsignal" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">
    <div class="ptool"><span class="ptlabel">${esc(p.server||'MCP')} request — waiting on you</span></div>
    <button class="pbtn qanswer" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">respond ⤢</button></div>`;
  if(p.kind!=='question'||!p.questions||!p.questions.length)return'';
  const n=p.questions.length;
  const label=n>1?`multi-part question (${n})`:(p.questions[0].header||'question');
  return`<div class="pend qsignal" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">
    <div class="ptool"><span class="ptlabel">${esc(label)} — waiting on you</span></div>
    ${p.files&&p.files.length?`<div class="pfiles"><span class="plabel">read first</span>${p.files.map(f=>fchip(s.session_id,f,f.caption)).join('')}</div>`:''}
    <button class="pbtn qanswer" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">answer ⤢</button>
  </div>`;
}
function openSessionQ(sid){sessQOpen=true;openSession(sid);}
function pendingBox(s,pre='msg'){
  const p=s.pending; if(!p)return'';
  if(p.nonce&&answered[s.session_id]===p.nonce)return'';   // sent: dismiss instantly
  if(p.kind==='question'){
    if(!p.questions||!p.questions.length)return'';
    return`<div class="pend">${p.questions.length>1?mqBlock(s,p,pre):singleQBlock(s,p,pre)}
      <div class="actmsg" id="${pre}-${s.session_id}"></div></div>`;
  }
  if(p.kind==='permission'){
    return`<div class="pend">
      <div class="ptool">permission: ${esc(p.tool)} — waiting on you</div>
      <pre>${esc(p.input_summary||'')}</pre>
      <div class="pbtns">
        <button class="pbtn allow" onclick="sendPerm('${s.session_id}','${p.nonce}','allow','${pre}')">allow</button>
        <button class="pbtn always" onclick="sendPerm('${s.session_id}','${p.nonce}','always','${pre}')">always allow</button>
        <button class="pbtn deny" onclick="sendPerm('${s.session_id}','${p.nonce}','deny','${pre}')">deny</button>
        ${(p.decisions||[]).includes('cancel')?`<button class="pbtn" onclick="sendPerm('${s.session_id}','${p.nonce}','cancel','${pre}')">cancel</button>`:''}
      </div>
      <div class="actmsg" id="${pre}-${s.session_id}"></div>
    </div>`;
  }
  if(p.kind==='elicitation')return elicitationBlock(s,p,pre);
  return'';
}
async function setSessionMode(sid,mode,pre='msg'){
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(!s||s.provider!=='codex')return;
  const previous=s.collaboration_mode||'default';
  s.collaboration_mode=mode;uiRefresh();
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'mode',mode})});
    const d=await r.json();
    if(!d.ok){s.collaboration_mode=previous;uiRefresh();alert(d.error||'mode change failed');return;}
    s.collaboration_mode=d.mode||mode;uiRefresh();
  }catch(e){s.collaboration_mode=previous;uiRefresh();alert('mode change failed: '+e);}
}
async function act(sid,payload,pre='msg',optimisticId=null){
  const isQuick=['permission','dismiss','elicitation'].includes(payload.type);
  const quickId=isQuick?beginQuickResponse(sid,payload):null;
  const setMessage=text=>{const el=document.getElementById(pre+'-'+sid);
    if(el)el.textContent=text;return el;};
  if(payload.type!=='ping')setMessage('sending…');
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,...payload})});
    const d=await r.json();
    if(optimisticId!=null)updateOptimistic(sid,optimisticId,d.ok,d.error,
      d.ok&&['option','multiq'].includes(payload.type));
    if(quickId!=null)finishQuickResponse(sid,quickId,d.ok,d.error);
    const el=setMessage(d.ok?'sent ✓':'✗ '+(d.error||'failed'));
    if(!d.ok&&!el&&quickId==null)alert(d.error||'failed');
    if(d.ok&&payload.nonce&&['option','multiq','permission','dismiss','elicitation'].includes(payload.type)){
      answered[sid]=payload.nonce;      // hide the selector NOW, don't wait for the poll
      delete otherDraft[sid];delete mqSel[sid];delete elicitDraft[sid];multiSel[sid]=new Set();
      uiRefresh();
    }
    return d;
  }catch(e){
    if(optimisticId!=null)updateOptimistic(sid,optimisticId,false,String(e));
    if(quickId!=null)finishQuickResponse(sid,quickId,false,String(e));
    const el=setMessage('✗ '+e);
    if(!el&&quickId==null)alert('request failed: '+e);
    return {ok:false,error:String(e)};
  }
}
function pendingQuestion(sid){
  const session=((last||{}).sessions||[]).find(item=>item.session_id===sid);
  return session&&session.pending&&session.pending.kind==='question'?session.pending:null;
}
function answerLabel(question,digits,other){
  if(other)return question?.secret?'(private answer)':other;
  return(digits||[]).map(d=>question?.options?.[Number(d)-1]?.label||`Option ${d}`).join(', ');
}
function answerPreview(sid,answers){
  const pending=pendingQuestion(sid),questions=(pending&&pending.questions)||[];
  return answers.map((answer,index)=>{
    const question=questions[index]||{};
    const label=answerLabel(question,answer.digits,answer.other)||'(no answer)';
    return `${question.header||question.question||`Question ${index+1}`}: ${label}`;
  }).join('\n');
}
function sendOption(sid,nonce,digits,pre){
  const optimisticId=addOptimistic(sid,answerPreview(sid,[{digits}]),'answer');
  act(sid,{type:'option',nonce,digits},pre,optimisticId);
}
function toggleOpt(sid,d){
  const s=multiSel[sid];s.has(d)?s.delete(d):s.add(d);uiRefresh();
}
function sendMulti(sid,nonce,n,pre){
  const digits=[...(multiSel[sid]||[])].sort();
  const other=(otherDraft[sid]||'').trim();
  if(!digits.length&&!other)return alert('pick at least one option');
  const optimisticId=addOptimistic(sid,answerPreview(sid,[{digits,other}]),'answer');
  act(sid,{type:'option',nonce,digits,multi:true,n_options:n,other:other||undefined},pre,optimisticId);
}
function sendOther(sid,nonce,n,pre){
  const other=(otherDraft[sid]||'').trim();
  if(!other)return alert('type your answer first');
  const optimisticId=addOptimistic(sid,answerPreview(sid,[{other}]),'answer');
  act(sid,{type:'option',nonce,n_options:n,other},pre,optimisticId);
}
function sendDismiss(sid,nonce,pre){
  act(sid,{type:'dismiss',nonce},pre);
}
// desktop only: pointless from the phone — it focuses a tab on the Mac
function focusSession(sid){act(sid,{type:'focus'});}

// in-app interstitial — a native confirm() is easy to dismiss by reflex on a phone,
// and stopping a turn is destructive (the work in flight is lost)
let confirmYes=null;
function askConfirm(title,body,confirmLabel,onYes){
  closeOverflow();
  confirmYes=onYes;
  $('#confirm').innerHTML=`<div class="cfbox">
    <div class="cftitle">${esc(title)}</div>
    <div class="cfbody">${body}</div>
    <div class="cfbtns">
      <button class="pbtn" onclick="closeConfirm()">cancel</button>
      <button class="pbtn cfgo" onclick="const f=confirmYes;closeConfirm();f&&f()">${esc(confirmLabel)}</button>
    </div></div>`;
  $('#confirm').style.display='flex';
}
function closeConfirm(){confirmYes=null;$('#confirm').style.display='none';$('#confirm').innerHTML='';}
function sendInterrupt(sid,pre='msg'){
  askConfirm('Stop this turn?',
    'Sends <b>Esc</b> to the session. Whatever it is doing right now is abandoned — '
    +'including any subagents it has running.',
    'stop the turn',
    ()=>act(sid,{type:'interrupt'},pre));
}
function sendCloseSession(sid,pre='smsg'){
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(!s||!s.capabilities?.close)return alert('This session cannot be closed here.');
  const active=['running','stalled','needs_you','stalled_or_prompt'].includes(s.state);
  const provider=s.provider==='codex'
    ?'The Codex thread will be archived.'
    :'The registered Claude process will end. Its iTerm tab stays open.';
  const activeBody=active
    ?' The current turn and every subagent under it will stop first.'
    :'';
  askConfirm('Close this session?',
    provider+activeBody+' The conversation remains available in <b>Session history</b>.',
    active?'stop and close':'close session',
    async()=>{
      const result=await act(sid,{type:'close'},pre);
      if(result?.ok){dismissOverlay();setTimeout(()=>tick(),0);}
    });
}
// A subagent has NO terminal: the only way to stop it is to Esc its PARENT, which
// ends the parent's whole turn and every other agent under it. Say so plainly.
function stopAgentParent(sid,pre='amsg'){
  askConfirm('Stop this subagent?',
    'A subagent has no terminal of its own — the only way to stop it is to send <b>Esc</b> '
    +'to its <b>parent session</b>. That ends the parent\'s entire turn and kills '
    +'<b>every other subagent</b> it is running, not just this one.',
    'stop parent turn',
    ()=>act(sid,{type:'interrupt'},pre));
}
function copyTxt(ev,el){
  ev.stopPropagation();
  if(!navigator.clipboard)return;
  navigator.clipboard.writeText(el.dataset.copy).then(()=>{
    const o=el.textContent;el.textContent='copied ✓';
    setTimeout(()=>{el.textContent=o},900);   // UX flash only
  });
}
function sendPerm(sid,nonce,choice,pre='msg'){
  act(sid,{type:'permission',nonce,choice},pre);
}
function sendText(sid,ftPre='ft',msgPre='msg'){
  const inp=document.getElementById(ftPre+'-'+sid);
  const v=(inp&&inp.value||'').trim();
  if(!v)return;
  const cmd=(v.startsWith('/')||v.startsWith('$'))?(cmdCache[sid]||[]).find(c=>c.name===v.split(/\s+/)[0]):null;
  if(cmd&&cmd.danger&&!confirm(`${cmd.name} destroys this session's conversation state.\n\n${cmd.desc}\n\nSend it?`))return;
  slashClose();
  if(inp)inp.value='';        // clear NOW: the round-trip is the terminal's, not yours
  if(cmd&&cmd.execution==='action')return act(sid,{type:cmd.action},msgPre);
  if(cmd&&cmd.execution==='skill')return act(sid,{type:'skill',name:cmd.name,args:v.slice(cmd.name.length).trim()},msgPre);
  const optimisticId=addOptimistic(sid,v,'text');
  act(sid,{type:'text',text:v},msgPre,optimisticId);
}

// ---- slash-command autocomplete -------------------------------------------
// Menu INSERTS (never sends): most skills take args, and it keeps the send path
// — with its destructive-command confirm — as the single way anything fires.
const cmdCache={};          // sessionId -> [{name,desc,scope,danger}]
let slashBox=null;          // id of the open menu's container, or null
function slashClose(){
  if(!slashBox)return;
  const el=document.getElementById(slashBox);
  if(el)el.innerHTML='';
  slashBox=null;
}
async function slashInput(sid,pre){
  const inp=document.getElementById(pre+'-'+sid);
  const v=(inp&&inp.value)||'';
  // menu lives while the text is a single command/skill token; a space starts args
  if((!v.startsWith('/')&&!v.startsWith('$'))||/\s/.test(v))return slashClose();
  if(!cmdCache[sid]){
    cmdCache[sid]=[];       // in-flight guard: one fetch per session
    try{
      const r=await fetch('/api/commands?sid='+encodeURIComponent(sid),{cache:'no-store'});
      const d=await r.json();
      cmdCache[sid]=d.commands||[];
    }catch(e){cmdCache[sid]=[];}
  }
  const q=v.slice(1).toLowerCase();
  const hits=cmdCache[sid].filter(c=>c.name[0]===v[0]&&c.name.slice(1).toLowerCase().includes(q))
    .sort((a,b)=>(a.name.slice(1).toLowerCase().startsWith(q)?0:1)-(b.name.slice(1).toLowerCase().startsWith(q)?0:1))
    .slice(0,40);
  const box=document.getElementById('slash-'+pre+'-'+sid);
  if(!box)return;
  slashBox='slash-'+pre+'-'+sid;
  box.innerHTML=hits.length?`<div class="slashmenu">${hits.map(c=>`
    <button class="slashrow" onmousedown="event.preventDefault()" onclick="slashPick('${sid}','${pre}','${enc(c.name)}')">
      <span class="scmd">${esc(c.name)}${c.danger?' <span class="sdanger">destructive</span>':''}</span>
      <span class="sdesc">${esc(c.desc||'')}</span>
      <span class="sscope">${esc(c.scope)}</span>
    </button>`).join('')}</div>`
    :`<div class="slashmenu"><div class="slashempty">no command matches “${esc(v)}”</div></div>`;
}
function slashPick(sid,pre,name){
  const inp=document.getElementById(pre+'-'+sid);
  if(!inp)return;
  inp.value=decodeURIComponent(name)+' ';   // trailing space: args go right here
  slashClose();
  inp.focus();
}
let historyFilter='',historyAccess='all',historyProvider='all';
let historyData={ok:true,items:[],next_cursor:0,total:0};
let historyLoading=false,historyLoadedAt=0,historyAbort=null,historyFilterTimer=null;
let closedIds=new Set();
const closedMeta=new Map();
const historyInfoOpen=new Set();
const actionSelected=new Set(),workstreamOpen=new Set();
let actionKind='all',actionBulkBusy=false;

function actionSession(action){
  return ((last&&last.sessions)||[]).find(item=>item.session_id===action.session_id)||null;
}
function actionBaseMatches(action){
  const session=actionSession(action);
  if(action.kind==='budget'&&(nowState!=='all'||nowFilter.trim()))return false;
  if(action.kind!=='budget'&&(!session||pinnedSessions.has(action.session_id)||!matchesNow(session)))return false;
  return true;
}
function actionKindMatches(action){
  if(actionKind==='requests'&&!['question','form','reply'].includes(action.kind))return false;
  if(actionKind==='approvals'&&action.kind!=='approval')return false;
  if(actionKind==='outcomes'&&action.kind!=='outcome')return false;
  if(actionKind==='problems'&&!['problem','attention'].includes(action.kind))return false;
  if(actionKind==='budgets'&&action.kind!=='budget')return false;
  return true;
}
function actionMatches(action){return actionBaseMatches(action)&&actionKindMatches(action);}
function openInboxAction(actionId){
  const action=((last&&last.actions)||[]).find(item=>item.action_id===actionId);if(!action)return;
  if(action.kind==='budget'){navigateTo('insights');return;}
  ['question','form','approval'].includes(action.kind)?openSessionQ(action.session_id):
    primarySessionAction(action.session_id);
}
function setActionKind(value){
  actionKind=['all','requests','approvals','outcomes','problems','budgets'].includes(value)?value:'all';
  render(last,true);
}
function toggleActionSelection(actionId,checked){
  checked?actionSelected.add(actionId):actionSelected.delete(actionId);renderActionInbox(last);
}
function toggleVisibleActions(checked){
  const visible=((last&&last.actions)||[]).filter(action=>actionMatches(action)&&(action.safe_bulk||[]).length);
  visible.forEach(item=>checked?actionSelected.add(item.action_id):actionSelected.delete(item.action_id));
  renderActionInbox(last);
}
async function bulkTriage(operation){
  if(actionBulkBusy)return;
  const selected=((last&&last.actions)||[]).filter(item=>actionSelected.has(item.action_id));
  const eligible=selected.filter(item=>(item.safe_bulk||[]).includes(operation));
  if(!eligible.length)return;
  actionBulkBusy=true;renderActionInbox(last);
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({bulk_triage:{operation,items:eligible.map(item=>({
        action_id:item.action_id,session_id:item.session_id,revision:String(item.revision||'')}))}})});
    const data=await r.json();if(!data.ok)throw new Error(data.error||'bulk update failed');
    eligible.forEach(item=>actionSelected.delete(item.action_id));await tick();
  }catch(error){alert('bulk action failed: '+error);}
  finally{actionBulkBusy=false;renderActionInbox(last);}
}
function actionIcon(kind){return {question:'?',form:'≡',approval:'!',reply:'↩',problem:'×',
  attention:'!',outcome:'✓',budget:'$'}[kind]||'•';}
function renderActionInbox(f){
  const el=$('#actioninbox');if(!el)return new Set();
  const candidates=((f&&f.actions)||[]).filter(actionBaseMatches);
  const actions=candidates.filter(actionKindMatches);
  const visibleSessionIds=new Set(actions.map(action=>action.session_id).filter(Boolean));
  const activeIds=new Set(((f&&f.actions)||[]).map(item=>item.action_id));
  [...actionSelected].forEach(id=>{if(!activeIds.has(id))actionSelected.delete(id);});
  if(!candidates.length){el.className='';el.innerHTML='';return visibleSessionIds;}
  const selected=actions.filter(item=>actionSelected.has(item.action_id));
  const eligible=operation=>selected.filter(item=>(item.safe_bulk||[]).includes(operation)).length;
  const selectable=actions.filter(item=>(item.safe_bulk||[]).length);
  const allSelected=selectable.length>0&&selectable.every(item=>actionSelected.has(item.action_id));
  el.className='actioninbox';
  el.innerHTML=`<div class="actionhead"><label><input type="checkbox" aria-label="select visible actions"
      ${allSelected?'checked':''} ${selectable.length?'':'disabled'} onchange="toggleVisibleActions(this.checked)"><span><b>Needs you</b><small>Action inbox · ${actions.length} item${actions.length===1?' needs':'s need'} review</small></span></label>
    <div class="actionfilters">${[['all','All'],['requests','Requests'],['approvals','Approvals'],
      ['outcomes','Outcomes'],['problems','Problems'],['budgets','Budgets']].map(([value,label])=>
      `<button class="${actionKind===value?'active':''}" onclick="setActionKind('${value}')">${label}</button>`).join('')}</div></div>
    ${selected.length?`<div class="bulkbar"><b>${selected.length} selected</b>
      ${eligible('mark_read')?`<button onclick="bulkTriage('mark_read')">Review ${eligible('mark_read')}</button>`:''}
      ${eligible('mark_available')?`<button onclick="bulkTriage('mark_available')">Mark available ${eligible('mark_available')}</button>`:''}
      ${eligible('mute')?`<button onclick="bulkTriage('mute')">Mute ${eligible('mute')}</button>`:''}
      ${eligible('dismiss')?`<button onclick="bulkTriage('dismiss')">Dismiss ${eligible('dismiss')}</button>`:''}
      ${actionBulkBusy?'<span>updating…</span>':''}</div>`:''}
    <div class="actionrows">${actions.length?actions.map(action=>{
      const session=actionSession(action),encoded=enc(action.action_id);
      const identityTitle=session?.title||action.title||'',identityProject=session?.project||action.project||'';
      const displayRequest=identityTitle||action.request;
      const contextSignal=action.kind==='reply'?action.context:action.request;
      const displayContext=identityTitle?[identityProject,contextSignal].filter(Boolean).join(' · '):action.context;
      const age=Math.max(0,Math.round(((f&&f.t)||Date.now()/1000)-(action.created_at||0)));
      const selectable=(action.safe_bulk||[]).length;
      return`<div class="actionrow ${esc(action.kind)} ${esc(action.status||'')}" data-action-id="${esc(action.action_id)}" data-action-sid="${esc(action.session_id||'')}">
        <label class="actioncheck" onclick="event.stopPropagation()">${selectable?`<input type="checkbox" aria-label="select ${esc(action.request)}"
          ${actionSelected.has(action.action_id)?'checked':''} onchange="toggleActionSelection(decodeURIComponent('${encoded}'),this.checked)">`:''}</label>
        <button class="actionopen" onclick="openInboxAction(decodeURIComponent('${encoded}'))">
          <span class="actionglyph">${actionIcon(action.kind)}</span><span class="actioncopy"><span class="actionrequest">${esc(displayRequest)}</span>
          ${displayContext?`<span class="actioncontext">${esc(displayContext)}</span>`:''}
          <span class="actionmeta"><strong>${esc(action.reason||'Needs review')}</strong> · ${esc(action.provider||'fleet')} · ${esc(action.access_label||'Review')} · ${fmtAge(age)} ago</span></span>
          <span class="actiondelivery">${esc(action.delivery_state||'Review')}</span></button>
        <button class="primarybtn" onclick="openInboxAction(decodeURIComponent('${encoded}'))">${esc(action.primary_action_label||'Review')}</button>
        ${session?.muted?'<span class="actionmuted" title="session notifications muted">🔕</span>':''}
        ${session?cardResponseFeedback(session):''}
      </div>`;}).join(''):`<div class="actionempty">No ${esc(actionKind==='all'?'matching':actionKind)} actions.</div>`}</div>`;
  return visibleSessionIds;
}

function filteredWorkstreams(f){
  const query=workFilter.trim().toLowerCase();
  return ((f&&f.workstreams)||[]).filter(item=>{
    const stateOk=workState==='all'||(workState==='mixed'?(item.providers||[]).length>1:
      (item.counts&&item.counts[workState]>0));
    const hay=[item.title,item.root,...(item.branches||[]),...(item.providers||[]),
      ...(item.worktrees||[]),...(item.sessions||[]).flatMap(s=>[s.title,s.name,s.project,s.branch,s.provider])]
      .filter(Boolean).join(' ').toLowerCase();
    return stateOk&&(!query||hay.includes(query));
  });
}
function toggleWorkstream(id){workstreamOpen.has(id)?workstreamOpen.delete(id):workstreamOpen.add(id);renderWorkstreams(workstreamData);}
function workstreamSessionRow(session){
  const sid=String(session.session_id||''),encoded=enc(sid),closed=session.closed_at!=null;
  const title=session.title||session.name||session.project||'Session';
  return`<div class="worksession"><span class="workstate ${esc(session.ui_group||'history')}"></span>
    <span class="worksessioncopy"><b>${esc(title)}</b><small>${esc(session.reason_label||'History')} · ${esc(session.provider||'claude')}${session.branch?` · ${esc(session.branch)}`:''}</small></span>
    <button class="historyaction" onclick="${closed?`openClosed(decodeURIComponent('${encoded}'))`:`primarySessionAction(decodeURIComponent('${encoded}'))`}">${esc(session.primary_action_label||'View')}</button></div>`;
}
function renderWorkstreams(f){
  const el=$('#workstreams');if(!el)return;
  document.querySelectorAll('[data-work-filter]').forEach(button=>{
    const active=button.dataset.workFilter===workState;button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
  renderSavedViews('workstreams');
  if(workstreamsLoading&&!(f&&f.workstreams&&f.workstreams.length)){
    el.innerHTML='<div class="destinationempty"><span>⌘</span><b>Grouping repositories…</b><p>Now continues polling while this page loads.</p></div>';return;
  }
  const staleAlert=f&&f.ok===false?
    `<div class="provideralert"><b>Workstreams stale</b> — ${esc(f.error||'repository grouping failed')}. Showing the last successful grouping.</div>`:'';
  const items=filteredWorkstreams(f);
  if(!items.length){el.innerHTML=staleAlert+'<div class="destinationempty"><span>⌘</span><b>No matching workstreams</b><p>Repositories and project folders appear when Fleet observes a session.</p></div>';return;}
  el.innerHTML=staleAlert+items.map(item=>{
    const id=enc(item.workstream_id),expanded=workstreamOpen.has(item.workstream_id),counts=item.counts||{};
    const summary=item.repo_summary||{},repository=item.repository||{};
    const stateCounts=[['needs_you','needs you'],['working','working'],['available','available'],['history','history']]
      .filter(([key])=>counts[key]).map(([key,label])=>`<span class="wcount ${key}"><b>${counts[key]}</b> ${label}</span>`).join('');
    const cost=item.cost_scope==='unavailable'?'cost unavailable':
      `${fmt$(item.cost)}${item.cost_scope==='partial'?' partial':''}`;
    const context=item.context_tokens==null?'context unavailable':`${fmtTok(item.context_tokens)} context now`;
    return`<section class="workstream ${item.missing?'missing':''}" data-workstream-id="${esc(item.workstream_id)}">
      <button class="workhead" onclick="toggleWorkstream(decodeURIComponent('${id}'))">
        <span class="workkind">${item.kind==='git'?'git':item.kind==='unknown'?'?':'dir'}</span><span class="worktitle"><b>${esc(item.title)}${item.missing?' · missing':''}${item.stale?' · stale':''}</b><small>${esc(item.root)}</small></span>
        <span class="workcounts">${stateCounts||'<span class="wcount">no sessions</span>'}</span><span class="chev">${expanded?'⌃':'⌄'}</span></button>
      <div class="worksummary"><span>${(item.providers||[]).map(esc).join(' · ')||'provider unavailable'}</span>
        <span>${(item.branches||[]).map(branch=>`<code>${esc(branch)}</code>`).join(' ')||'branch unavailable'}</span>
        <span>${esc(cost)} · ${esc(context)}</span></div>
      <div class="workoutcome"><b>Latest</b><span>${esc(item.latest_outcome||'No outcome recorded')}</span></div>
      <div class="worksignals"><span>Changes <b>${esc(summary.changed_files||'not observed')}</b></span><span>Tests <b>${esc(String(summary.tests||'not observed').replaceAll('_',' '))}</b></span><span>PR <b>${esc(String(summary.pull_request||'not observed').replaceAll('_',' '))}</b></span><span>Budget <b>${esc(String(item.budget_state||'not_configured').replaceAll('_',' '))}</b></span>
        ${item.kind==='git'?`<button class="repoopen" onclick="openRepository(decodeURIComponent('${enc(item.root)}'),decodeURIComponent('${enc(repository.worktree||item.worktree||item.root)}'))">Repository</button>`:''}</div>
      ${expanded?`<div class="workdetail"><div class="worktrees"><b>Worktrees</b>${(item.worktrees||[]).map(path=>`<code>${esc(path)}</code>`).join('')}</div>
        <div class="worksessions">${(item.sessions||[]).map(workstreamSessionRow).join('')}</div></div>`:''}</section>`;
  }).join('');
}
function renderQueue(el,list,title,subtitle,kind,keepEmpty=false){
  if(!list.length&&!keepEmpty){
    if(el.className||el.firstChild){el.innerHTML='';el.className='';}
    return;
  }
  el.className=`queue ${kind}`;
  if(!el.querySelector('.queuehead'))el.innerHTML='<div class="queuehead"><b></b><span></span></div><div class="queuelist"></div>';
  el.querySelector('.queuehead b').textContent=`${title} · ${list.length}`;
  el.querySelector('.queuehead span').textContent=subtitle;
  reconcileCards(el.querySelector('.queuelist'),list,
    keepEmpty?'No sessions are ready for another message.':'');
}
function toggleHistory(encoded){
  const sid=decodeURIComponent(encoded);
  historyInfoOpen.has(sid)?historyInfoOpen.delete(sid):historyInfoOpen.add(sid);
  updateHistoryRows();
}
function setHistoryFilter(kind,value){
  if(kind==='access')historyAccess=value;else historyProvider=value;
  loadHistory(true);
}
function closedSession(sid){
  return ((last&&last.closed)||[]).find(item=>item.session_id===sid)||
    (historyData.items||[]).find(item=>item.session_id===sid)||closedMeta.get(sid)||null;
}
function isClosedSession(sid){return Boolean(closedSession(sid)||closedIds.has(sid));}
function historyParams(cursor){
  const params=new URLSearchParams({cursor:String(cursor||0),limit:'100'});
  if(historyFilter.trim())params.set('q',historyFilter.trim());
  if(historyAccess!=='all')params.set('access',historyAccess);
  if(historyProvider!=='all')params.set('provider',historyProvider);
  return params.toString();
}
function renderHistoryDestination(){
  const el=$('#history');if(!el||!last)return;
  const focused=document.activeElement;
  if(focused&&focused.tagName==='INPUT'&&el.contains(focused)){
    updateHistoryRows();
    const count=el.querySelector('.historycount');if(count)count.textContent=historyCount(last);
  }else el.innerHTML=historySection(last);
}
async function loadHistory(reset=false){
  if(reset&&historyAbort)historyAbort.abort();
  if(historyLoading&&!reset)return;
  const cursor=reset?0:historyData.next_cursor;
  if(cursor==null)return;
  const controller=new AbortController();historyAbort=controller;historyLoading=true;
  renderHistoryDestination();
  try{
    const response=await fetch('/api/history?'+historyParams(cursor),
      {cache:'no-store',signal:controller.signal});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'History unavailable');
    const items=data.items||[];items.forEach(item=>closedMeta.set(item.session_id,item));
    historyData={ok:true,items:reset?items:[...(historyData.items||[]),...items],
      next_cursor:data.next_cursor,total:Number(data.total||0)};
    historyLoadedAt=Date.now();
  }catch(error){
    if(error.name==='AbortError')return;
    historyData={...historyData,ok:false,error:String(error.message||error)};
  }finally{
    if(historyAbort===controller){historyAbort=null;historyLoading=false;renderHistoryDestination();}
  }
}
function queueHistoryFilter(value){
  historyFilter=value;clearTimeout(historyFilterTimer);
  historyFilterTimer=setTimeout(()=>loadHistory(true),180);
}
function historyItems(f){
  const live=(f.sessions||[]).filter(s=>s.ui_group==='history'&&!pinnedSessions.has(s.session_id));
  const closed=(historyData.items||[]).filter(s=>!pinnedSessions.has(s.session_id));
  return [...live,...closed].sort((a,b)=>(b.activity_at||0)-(a.activity_at||0));
}
function matchesHistoryFilter(item){
  const query=historyFilter.trim().toLowerCase();
  const accessOk=historyAccess==='all'||item.primary_action===historyAccess;
  const providerOk=historyProvider==='all'||(item.provider||'claude')===historyProvider;
  const hay=[item.title,item.name,item.project,item.branch,item.provider,item.reason_label,
    item.access_label,item.state,item.reg_status].filter(Boolean).join(' ').toLowerCase();
  return accessOk&&providerOk&&(!query||hay.includes(query));
}
function filteredHistory(f){
  return historyItems(f).filter(matchesHistoryFilter);
}
function updateHistoryRows(){
  const el=document.getElementById('historyrows');
  if(el&&last)el.innerHTML=historyRows(filteredHistory(last));
}
function filterChips(kind,current,items){
  return`<div class="filterline"><span class="filterlabel">${kind}</span>${items.map(([value,label])=>
    `<button class="filterchip${current===value?' on':''}" aria-pressed="${current===value}"
      onclick="setHistoryFilter('${kind.toLowerCase()}','${value}')">${label}</button>`).join('')}</div>`;
}

// ---- new session -----------------------------------------------------------
// form state lives in globals: the 2s poll re-renders this section, so anything
// held only in the DOM (typed path, status line) would be wiped mid-use
let newOpen=false,newProvider='claude',newDir='',newModel='',newEffort='',newMode='plan',newWt=true,newWtName='',newMessage='',spawnWait=null,spawnMsg='';
const DEFAULT_DIR='/Users/benjaminfeder/Programming/Quirk';
function newSection(){
  const dirs=(last&&last.recent_dirs)||[];
  if(!newDir&&dirs.some(d=>d.path===DEFAULT_DIR))newDir=DEFAULT_DIR;   // the usual repo
  const catalog=(((last&&last.models_by_provider)||{})[newProvider])||[];
  const models=catalog.length?catalog.map(m=>m.id):
    (newProvider==='claude'?((last&&last.models)||[]):[]);
  const picked=catalog.find(m=>m.id===newModel);
  const efforts=(picked&&picked.efforts&&picked.efforts.length)?picked.efforts:
    ((last&&last.efforts)||[]);
  if(newOpen&&!spawnForecast&&!spawnForecastTimer)queueSpawnForecast();
  if(!newOpen)
    return`<button class="newbtn" onclick="newOpen=true;render(last,true)">+ new coding session</button>
      ${spawnMsg?`<div class="actmsg spawnbanner">${esc(spawnMsg)}</div>`:''}
      <div class="dsep"></div>`;
  const cur=dirs.find(d=>d.path===newDir);
  const untrusted=newDir&&(!cur||!cur.trusted);
  return`<div class="newform">
    <div class="nfhead">new session <button class="xbtn" onclick="newOpen=false;render(last,true)">✕</button></div>
    <label class="nflab">provider</label>
    <select class="nfsel" onchange="newProvider=this.value;newModel='';spawnForecast=null;queueSpawnForecast();render(last,true)">
      <option value="claude" ${newProvider==='claude'?'selected':''}>Claude Code</option>
      <option value="codex" ${newProvider==='codex'?'selected':''}>Codex CLI</option>
    </select>
    <label class="nflab">directory</label>
    <select class="nfsel" onchange="newDir=this.value;spawnForecast=null;queueSpawnForecast();render(last,true)">
      <option value="">— pick a recent directory —</option>
      ${dirs.map(d=>`<option value="${esc(d.path)}" ${d.path===newDir?'selected':''}>${esc(d.path.replace(/^\/Users\/[^/]+/,'~'))}${d.trusted?'':' ⚠ untrusted'}</option>`).join('')}
    </select>
    <input class="nfin" placeholder="…or type a path (must be under ~)" value="${esc(dirs.some(d=>d.path===newDir)?'':newDir)}"
      oninput="newDir=this.value;spawnForecast=null;queueSpawnForecast()" autocomplete="off">
    ${newProvider==='claude'&&untrusted?`<div class="nfwarn">⚠ this folder isn't trusted yet — Claude Code will ask
      “do you trust the files in this folder?” at startup, and only your Mac can answer it.</div>`:''}
    <div class="nfrow">
      <div class="nfcol"><label class="nflab">model</label>
        <select class="nfsel" onchange="newModel=this.value;spawnForecast=null;queueSpawnForecast()">
          <option value="">default</option>
          ${models.map(m=>`<option value="${m}" ${m===newModel?'selected':''}>${m}</option>`).join('')}
        </select></div>
      <div class="nfcol"><label class="nflab">effort</label>
        <select class="nfsel" onchange="newEffort=this.value">
          <option value="">default</option>
          ${efforts.map(e=>`<option value="${e}" ${e===newEffort?'selected':''}>${e}</option>`).join('')}
        </select></div>
      ${newProvider==='codex'?`<div class="nfcol"><label class="nflab">mode</label>
        <select class="nfsel" onchange="newMode=this.value">
          <option value="plan" ${newMode==='plan'?'selected':''}>Plan</option>
          <option value="default" ${newMode==='default'?'selected':''}>Default</option>
        </select></div>`:''}
    </div>
    ${newProvider==='claude'?`<label class="nfcheck"><input type="checkbox" ${newWt?'checked':''}
      onchange="newWt=this.checked;render(last,true)"><span>new git worktree</span></label>
    ${newWt?`<input class="nfin" placeholder="worktree name (optional)" value="${esc(newWtName)}"
      oninput="newWtName=this.value" autocomplete="off">`:''}`:''}
    <label class="nflab">initial message <span style="text-transform:none;letter-spacing:0">(optional now, required to schedule)</span></label>
    <textarea class="nfin nfmessage" maxlength="2000" placeholder="What should this session work on?" oninput="newMessage=this.value">${esc(newMessage)}</textarea>
    ${spawnForecastHtml()}
    <div class="nfactions"><button class="pbtn send nfgo" onclick="doSpawn()">start session ▸</button>
      <button class="pbtn sendoption nfgo" onclick="doScheduleNew()">schedule session</button></div>
    ${spawnMsg?`<div class="actmsg">${esc(spawnMsg)}</div>`:''}
  </div>
  <div class="dsep"></div>`;
}
async function doSpawn(){
  if(!newDir){spawnMsg='✗ pick a directory first';render(last,true);return;}
  spawnMsg='starting…';render(last,true);
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type:'spawn',provider:newProvider,cwd:newDir,model:newModel,effort:newEffort,mode:newMode,
                           worktree:newWt,worktree_name:newWtName,initial_text:newMessage||undefined})});
    const d=await r.json();
    if(!d.ok){spawnMsg='✗ '+(d.error||'failed');render(last,true);return;}
    spawnMsg=d.trust_prompt
      ? '⚠ started — but it is waiting on the trust prompt on your Mac ("do you trust the files in this folder?")'
      : 'started ✓ — opening it here as soon as it appears…';
    // Both providers return the exact native session identity. Never guess by cwd:
    // a sibling session in the same repo must not be opened by mistake.
    spawnWait={sessionId:d.session_id,until:Date.now()+120000,provider:newProvider,initialMessage:newMessage};
    newMessage='';
    newOpen=false;render(last,true);
  }catch(e){spawnMsg='✗ '+e;render(last,true);}
}
function doScheduleNew(){
  if(!newDir){spawnMsg='✗ pick a directory first';render(last,true);return;}
  if(!newMessage.trim()){spawnMsg='✗ add the message this new session should receive';render(last,true);return;}
  const spec={provider:newProvider,cwd:newDir,model:newModel,effort:newEffort,mode:newMode,
    worktree:newProvider==='claude'&&newWt,worktree_name:newProvider==='claude'?newWtName:''};
  openSchedule('',null,'',null,spec,newMessage);
}
// a spawned session only enters the fleet once it writes a transcript
async function checkSpawn(f){
  if(!spawnWait)return;
  if(Date.now()>spawnWait.until){spawnWait=null;spawnMsg='';return;}
  const s=(f.sessions||[]).find(x=>x.session_id===spawnWait.sessionId);
  if(s){const waiting=spawnWait;spawnWait=null;spawnMsg='';
    if(waiting.provider==='claude'&&waiting.initialMessage){
      const delivered=await act(s.session_id,{type:'text',text:waiting.initialMessage},'spawnmsg');
      if(!delivered.ok)spawnMsg='✗ session started, but the initial message failed: '+(delivered.error||'failed');
    }
    openSession(s.session_id);}
}
function historySection(f){
  const items=historyItems(f);
  if(!items.length&&!historyLoading&&historyData.ok&&historyData.next_cursor==null)
    return'<div class="destinationempty"><span>↺</span><b>No session history</b><p>Inactive and closed sessions will appear here.</p></div>';
  return`<div class="historybox">
    <div class="historycount">${historyCount(f)}</div>
    <div class="historytools">
      <div class="freetext"><input placeholder="Filter by title, project, branch, provider, or state"
        value="${esc(historyFilter)}" oninput="queueHistoryFilter(this.value)"></div>
      ${filterChips('Access',historyAccess,[['all','All'],['continue','Continue'],['view','View only'],['reopen','Reopen']])}
      ${filterChips('Provider',historyProvider,[['all','All'],['claude','Claude'],['codex','Codex']])}
    </div>
    <div id="historyrows">${historyRows(filteredHistory(f))}</div>
  </div>`;
}
function historyCount(f){
  const live=(f.sessions||[]).filter(item=>item.ui_group==='history'&&
    !pinnedSessions.has(item.session_id)&&matchesHistoryFilter(item)).length;
  const total=live+Number(historyData.total||0);
  return`${total} session${total===1?'':'s'}`;
}
function historyRows(items){
  if(!items.length&&!historyLoading)return`<div class="empty">${historyData.ok?'no matches':esc(historyData.error||'history unavailable')}</div>`;
  return items.map(item=>historyRow(item)).join('')+
    (historyLoading?'<div class="ctxload">loading history…</div>':'')+
    (!historyLoading&&historyData.next_cursor!=null?`<button class="newbtn" onclick="loadHistory(false)">
      show ${Math.min(100,Math.max(0,historyData.total-(historyData.items||[]).length))} more</button>`:'');
}
function historyRow(item,pinnedView=false){
  const sid=String(item.session_id||''),encoded=enc(sid);
  const isClosed=item.closed_at!=null&&!item.capabilities;
  const open=historyInfoOpen.has(sid);
  const activity=item.activity_at||item.last_seen||item.closed_at||((last&&last.t)||0);
  const title=item.title||item.name||item.project||'Session';
  const action=item.primary_action_label||'View';
  const provider=item.provider||'claude';
  const raw=item.state||'closed';
  const canReopen=isClosed&&item.can_reopen;
  return`<div class="historyrow" data-history-sid="${esc(sid)}">
    <div class="historymain" onclick="toggleHistory('${encoded}')">
      <span class="historyreason">${esc(item.reason_label||'Inactive')}</span>
      <span class="historyname"><b>${esc(title)}</b><small>${esc(item.project||'')}${item.branch&&item.branch!=='HEAD'?` · ${esc(item.branch)}`:''} · ${esc(provider)}</small></span>
      <span class="historyage">${fmtAge(Math.max(0,Math.round(((last&&last.t)||Date.now()/1000)-activity)))} ago</span>
      ${isClosed?`<button class="historyaction" onclick="event.stopPropagation();openClosed(decodeURIComponent('${encoded}'))">View</button>
        ${canReopen?`<button class="historyaction" onclick="event.stopPropagation();reopenClosed(decodeURIComponent('${encoded}'),this)">Reopen</button>`:''}`
        :`<button class="historyaction" onclick="event.stopPropagation();primarySessionAction(decodeURIComponent('${encoded}'))">${esc(action)}</button>`}
      <button class="spin${pinnedSessions.has(sid)?' on':''}" aria-label="${pinnedSessions.has(sid)?'unpin session':'pin session to top'}"
        title="${pinnedSessions.has(sid)?'unpin session':'pin session to top'}"
        onclick="event.stopPropagation();toggleSessionPin(decodeURIComponent('${encoded}'))">📌</button>
    </div>
    ${open?`<div class="historydetail"><div class="kv">
      <span>session</span>${cpb(sid)}
      <span>provider</span><b>${esc(provider)}</b>
      <span>access</span><b>${esc(item.access_label||'View only')}</b>
      <span>provider state</span><b>${esc(raw)}</b>
      <span>placement rule</span><b><code>${esc(item.winning_rule||'placement.unknown')}</code></b>
      <span>confidence</span><b>${esc(confidenceText(item.state_confidence))}</b>
      <span>cwd</span>${cpb(item.cwd||'?')}
      <span>branch</span><b>${esc(item.branch||'—')}</b>
      <span>model</span><b>${esc(item.model||'?')}</b>
      ${item.error?`<span>error</span><b>${esc(item.error)}</b>`:''}
      ${item.bridge_url?`<span>link</span><b><a class="jump" href="${esc(item.bridge_url)}" target="_blank">open in claude.ai ↗</a></b>`:''}
    </div>${evidenceFactsHtml(item)}</div>`:''}
  </div>`;
}

function toggle(sid){open.has(sid)?open.delete(sid):open.add(sid);render(last,true);}
function togglePeek(sid,expanded){
  expanded?expandedPeeks.add(sid):expandedPeeks.delete(sid);
  render(last,true);
}
let peekMeasurePending=false;
function schedulePeekOverflow(){
  if(peekMeasurePending)return;
  peekMeasurePending=true;
  requestAnimationFrame(()=>{
    peekMeasurePending=false;
    document.querySelectorAll('.sessionpeek').forEach(row=>{
      row.classList.remove('truncated');
      if(row.classList.contains('expanded'))return;
      const body=row.querySelector('.peekmd');
      if(body&&body.scrollHeight>body.clientHeight+1)row.classList.add('truncated');
    });
  });
}
window.addEventListener('resize',schedulePeekOverflow);

// ---- budgets and forecasts ------------------------------------------------
let budgetData={ok:true,budgets:[],forecasts:{},measurement_labels:{}},budgetLoading=false,budgetLoadedAt=0;
let budgetDraft=null,spawnForecast=null,spawnBudgetHeadroom=[],spawnForecastTimer=null;
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
  finally{if(!spawn)budgetLoading=false;renderBudgetPanel();if(settingsOpen)renderSettings();if(newOpen)render(last,true);}
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
  if(budgetLoading&&!budgetData.budgets?.length){el.innerHTML='<div class="ctxload">Measuring budgets…</div>';return;}
  if(!budgetData.ok){el.innerHTML=`<div class="provideralert"><b>Budgets unavailable</b> — ${esc(budgetData.error||'failed')}</div>`;return;}
  const items=budgetData.budgets||[];
  if(!items.length){el.innerHTML='<section class="budgetpanel emptybudget"><b>No budgets configured</b><span>Budgets alert only unless you explicitly enable “block future spawns.” Configure them in Settings.</span><button onclick="navigateTo(\'settings\')">Open Settings</button></section>';return;}
  el.innerHTML=`<section class="budgetpanel"><div class="budgetpanelhead"><span><b>Budgets</b><small>Cumulative local usage; concurrency is current</small></span><button onclick="navigateTo('settings')">Manage</button></div>
    <div class="budgetcards">${items.map(item=>`<article class="budgetcard ${esc(item.status)}"><div><b>${esc(item.label)}</b><small>${esc(item.scope_type)}${item.scope_id?` · ${esc(item.scope_id)}`:''}</small></div>
      <strong>${esc(budgetValue(item))} <small>of ${item.metric==='usd'?fmt$(item.limit_value):item.metric==='tokens'?fmtTok(item.limit_value):Math.round(item.limit_value)}</small></strong>
      <span class="budgetmeasure">${esc(String(item.measurement_scope||'unavailable').replaceAll('_',' '))}${item.block_spawns?' · blocks future spawns':''}</span>
      <div class="budgetbar"><i style="width:${Math.round(Math.min(1,item.ratio||0)*100)}%"></i></div><p>${esc(budgetForecastText(item))}</p></article>`).join('')}</div></section>`;
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
  if(key==='scope_type'){budgetDraft[index].scope_id=value==='fleet'?'':value==='provider'?'claude':'';}renderSettings();}
function addBudget(){if(budgetDraft===null)budgetDraft=[];budgetDraft.push({scope_type:'fleet',scope_id:'',metric:'tokens',limit_value:1000000,block_spawns:false,enabled:true});renderSettings();}
function removeBudget(index){budgetDraft.splice(index,1);renderSettings();}
async function saveBudgets(){const msg=$('#setmsg');if(msg)msg.textContent='saving budgets…';
  const data=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({budgets:budgetDraft})}).then(r=>r.json()).catch(error=>({ok:false,error:String(error)}));
  if(!data.ok){if(msg)msg.textContent='✗ '+(data.error||'failed');return;}
  budgetDraft=(data.budgets||[]).map(budgetEditable);if(msg)msg.textContent='saved ✓';await loadBudgets(true);}
function queueSpawnForecast(){clearTimeout(spawnForecastTimer);spawnForecastTimer=setTimeout(()=>{
  spawnForecastTimer=null;if(!newOpen||!newProvider)return;loadBudgets(false,{provider:newProvider,model:newModel,project:(newDir.split('/').filter(Boolean).pop()||''),cwd:newDir});},250);}
function spawnForecastHtml(){const f=spawnForecast;if(!f)return'<div class="spawnforecast">Forecast and budget headroom load from matching local history.</div>';
  if(f.status==='error')return`<div class="spawnforecast unavailable">${esc(f.error)}</div>`;
  if(f.status!=='forecast')return`<div class="spawnforecast">Not enough matching history · ${f.sample_size||0} sample${f.sample_size===1?'':'s'}</div>`;
  const bits=[f.median_usd!=null?`median ${fmt$(f.median_usd)}`:'currency unavailable',f.median_tokens!=null?`${fmtTok(f.median_tokens)} tokens`:null,
    f.median_runtime_seconds!=null?`${Math.round(f.median_runtime_seconds/60)} min`:null,`${f.confidence} confidence · ${f.sample_size} samples`].filter(Boolean);
  const headroom=spawnBudgetHeadroom.length?spawnBudgetHeadroom.map(item=>item.headroom==null?`${item.label}: unavailable`:
    `${item.label}: ${item.metric==='usd'?fmt$(item.headroom):item.metric==='tokens'?fmtTok(item.headroom)+' tokens':Math.round(item.headroom)} headroom${item.block_spawns&&item.status==='exceeded'?' · spawn blocked':''}`).join(' · '):'No matching budget';
  return`<div class="spawnforecast">Historical match: ${bits.map(esc).join(' · ')}<br>Budget: ${esc(headroom)}</div>`;}

let insightsDays=7;
const insightsCache={};   // days -> {t, data, fetching}
function loadInsights(force){
  const c=insightsCache[insightsDays];
  if(!force&&c&&(c.fetching||(c.data&&Date.now()-c.t<60000)))return;
  insightsCache[insightsDays]={...(c||{}),fetching:true};
  fetch('/api/insights?days='+insightsDays,{cache:'no-store'}).then(r=>r.json()).then(d=>{
    insightsCache[insightsDays]={t:Date.now(),data:d};
    render(last,true);
  }).catch(()=>{delete insightsCache[insightsDays];});
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
function insightsSection(){
  const c=insightsCache[insightsDays],d=c&&c.data;
  let body='<div class="ctxload">crunching…</div>';
  if(d&&d.ok){
    const mixTot=(d.token_mix||[]).reduce((a,x)=>({input:a.input+x.input,write:a.write+x.write,
      read:a.read+x.read,output:a.output+x.output}),{input:0,write:0,read:0,output:0});
    const mixRow=x=>`<tr><td>${x.day}</td><td class="r">${x.input.toFixed(2)}</td><td class="r">${x.write.toFixed(2)}</td><td class="r">${x.read.toFixed(2)}</td><td class="r">${x.output.toFixed(2)}</td><td class="r"><b>${(x.input+x.write+x.read+x.output).toFixed(2)}</b></td></tr>`;
    body=`<div class="insbar">${[7,30,90].map(n=>`<button class="mqarr${insightsDays===n?' cur':''}" onclick="setInsightsDays(${n})">${n}d</button>`).join('')}
        <span class="setnum">agents ${fmt$(d.totals.agent_cost)} · sessions active in window ${fmt$(d.totals.session_cost)} (lifetime $) · cache busts ~${fmt$(d.totals.bust_cost||0)}</span></div>`
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
      +insFold('models','by model',
        insTable([['model'],['agents $',1],['sessions $',1]],
          d.models.map(m=>`<tr><td>${esc(m.name)}</td><td class="r">${m.agents.toFixed(2)}</td><td class="r">${m.sessions.toFixed(2)}</td></tr>`)))
      +insFold('projects','by project (sessions active in window; lifetime $)',
        insTable([['project'],['agents $',1],['sessions $',1]],
          d.projects.map(p=>`<tr><td>${esc(p.name)}</td><td class="r">${p.agents.toFixed(2)}</td><td class="r">${p.sessions.toFixed(2)}</td></tr>`)))
      +insFold('bydayy','agent $ by day',
        insTable([['day'],['$',1]],d.by_day.map(x=>`<tr><td>${x.day}</td><td class="r">${x.cost.toFixed(2)}</td></tr>`)))
      +insFold('topsess','top sessions (lifetime $)',
        insTable([['session'],['project'],['$',1]],
          d.top_sessions.map(s=>`<tr><td>${esc(s.title||'?')}</td><td>${esc(s.project||'')}</td><td class="r">${s.cost.toFixed(2)}</td></tr>`)));
  }else if(d&&!d.ok){body=`<div class="ctxload">✗ ${esc(d.error||'failed')}</div>`;}
  return`<div class="insightspanel">${body}</div>`;
}

let last=null;
function render(f,force){
  if(!f||!f.sessions)return;
  const renderStarted=performance.now();
  closedIds=new Set(f.closed_ids||[]);
  reconcileQuickResponses(f);
  applyReaderWidth();
  syncPinnedSessions(f);
  const t=f.totals;
  const outSummary=f.outbox_summary||{};
  const navCount=(t.needs_me||0)+(outSummary.pending||0)+(outSummary.attention||0);
  $('#nav-now-count').textContent=navCount||'';
  $('#nav-now-count').title=`${t.needs_me||0} need you · ${outSummary.pending||0} outbox pending · ${outSummary.attention||0} outbox need review`;
  const outChip=$('#outboxchip');if(outChip)outChip.textContent=`Outbox${outSummary.pending||outSummary.attention?` · ${(outSummary.pending||0)+(outSummary.attention||0)}`:''}`;
  document.querySelectorAll('[data-now-filter]').forEach(button=>{
    const active=button.dataset.nowFilter===nowState;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
  $('#totals').innerHTML=`<span><b${t.needs_me?' style="color:var(--amber)"':''}>${t.needs_me}</b> need you</span>
    <span><b>${t.busy}</b> working</span>
    <span><b>${t.available||0}</b> available <i class="sep">·</i>
      <b>${t.agents_running}</b> subagent${t.agents_running===1?'':'s'}</span>`;
  usageBar(f.usage,f.provider_usage);
  const providerProblems=Object.entries(f.providers||{}).filter(([,value])=>value&&value.ok===false);
  const ledgerProblem=f.ledger&&f.ledger.ok===false?
    `<div class="provideralert"><b>Local data recovered</b> — ${esc(f.ledger.error||'Fleet started a clean local ledger after a storage failure.')}${f.ledger.quarantine?` Preserved as <code>${esc(f.ledger.quarantine)}</code>.`:''}</div>`:'';
  $('#providerstate').innerHTML=ledgerProblem+providerProblems.map(([provider,value])=>
    `<div class="provideralert"><b>${esc(provider)} unavailable</b> — ${esc(value.error||'provider connection failed')}. Showing last known session placement when available.</div>`).join('');
  const ae=document.activeElement;
  const typingHistory=ae&&ae.tagName==='INPUT'&&$('#history').contains(ae);
  const typingNew=ae&&(ae.tagName==='INPUT'||ae.tagName==='SELECT'||ae.tagName==='TEXTAREA')&&$('#newsess').contains(ae);
  if(force||!touching()){
    const unpinned=f.sessions.filter(s=>!pinnedSessions.has(s.session_id)&&matchesNow(s));
    const inboxSessionIds=new Set((f.actions||[]).filter(action=>!pinnedSessions.has(action.session_id))
      .map(action=>action.session_id));
    renderPinned(f,matchesNow);
    const visibleInboxSessionIds=renderActionInbox(f);
    renderQueue($('#needsyou'),unpinned.filter(s=>s.ui_group==='needs_you'&&!visibleInboxSessionIds.has(s.session_id)),
      'Needs You','sessions waiting for your response','needs');
    renderBriefing();
    renderOutboxCompact();
    renderQueue($('#working'),unpinned.filter(s=>s.ui_group==='working'),
      'Working','turns in progress','working');
    renderQueue($('#sessions'),unpinned.filter(s=>s.ui_group==='available'&&!inboxSessionIds.has(s.session_id)),
      'Available','ready for another message','available',true);
    if(!typingNew)$('#newsess').innerHTML=newSection();
    if(!typingHistory)$('#history').innerHTML=historySection(f);
    if(currentRoute==='workstreams')renderWorkstreams(workstreamData);
    renderSavedViews('now');
    renderBudgetPanel();
    $('#rollup').innerHTML=insightsSection();
  }
  checkSpawn(f);
  renderViewerBar();
  renderAgent();
  renderSession();
  schedulePeekOverflow();
  applyRouteNav(settingsOpen?'settings':currentRoute);
  document.title=(t.needs_me?`(${t.needs_me}) `:'')+'Fleet View';
  perfRecord('render_ms',performance.now()-renderStarted);
}

async function tick(){
  const pollStarted=performance.now();
  try{
    const r=await fetch('/api/fleet',{cache:'no-store'});
    const payload=Number(r.headers.get('X-Fleet-Payload-Bytes')||r.headers.get('Content-Length'));
    if(Number.isFinite(payload))perfRecord('poll_payload_bytes',payload);
    last=await r.json();
    if(last.page_v){if(window.__pv&&window.__pv!==last.page_v)return location.reload();window.__pv=last.page_v;}
    $('#stale').style.display='none';
    render(last);
    if(currentRoute==='now'||$('#outboxview').style.display==='flex')loadOutbox();
    if(currentRoute==='now')loadBriefing();
    if(currentRoute==='insights')loadBudgets();
    if(currentRoute==='history'&&Date.now()-historyLoadedAt>5000&&!historyLoading)loadHistory(true);
  }catch(e){console.error('Fleet Dash render/poll failed',e);$('#stale').style.display='block';}
  finally{perfRecord('poll_ms',performance.now()-pollStarted);}
}
navigateTo(currentRoute,false);
tick();setInterval(tick,2000);
setInterval(()=>{if(currentRoute==='search')loadSearchStatus();},5000);
setInterval(()=>{if(currentRoute==='workstreams')loadWorkstreams();},8000);
fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"type":"ping"}'})
  .then(r=>{$('#notoken').style.display=r.status===403?'block':'none';}).catch(()=>{});
