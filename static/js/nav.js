// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{perfRecord,perfSummary,recordInputFeedback,applyInstanceAuth,parseSessionHash,hashDestination,applyNavSide,setNavSide,closeMobileMore,toggleMobileMore,applyRouteNav,navigateTo,setNowFilter,setNowState,loadWorkstreams,setWorkFilter,setWorkState,saveCurrentView,applySavedView,deleteSavedView,renderSavedViews,matchesNow});
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
function recordInputFeedback(started,flow='input'){
  const elapsed=performance.now()-started;
  perfRecord('input_feedback_ms',elapsed);
  perfRecord(`feedback_${String(flow).replace(/[^a-z0-9_]+/gi,'_').toLowerCase()}_ms`,elapsed);
}
globalThis.pushActionFallback=(()=>{const value=new URLSearchParams(location.search).get('push_action');
  return ['snooze','mute'].includes(value)?value:'';})();
globalThis.pendingActToken='';
(()=>{const url=new URL(location.href),token=url.searchParams.get('token');
  if(token&&/^[0-9a-f]+$/.test(token))pendingActToken=token;
  if(token||pushActionFallback){url.searchParams.delete('token');url.searchParams.delete('push_action');
    history.replaceState(history.state,'',url.pathname+url.search+url.hash);}})();
const actCookieName=instance=>`act_token_${instance?.mode==='staging'?'staging':'production'}`;
function applyInstanceAuth(instance){
  const cookieName=actCookieName(instance);
  if(pendingActToken){
    document.cookie=`${cookieName}=${pendingActToken};path=/;max-age=31536000;SameSite=Lax`;
    pendingActToken='';
  }
  return document.cookie.split(';').some(item=>item.trim().startsWith(cookieName+'='));
}
const openCards=new Set();
const expandedPeeks=new Set();
const infoOpen=new Set(),doneOpen=new Set(),filesOpen=new Set(),stateInfoOpen=new Set();  // detail-panel fold state, survives re-renders
const routeNames={now:'Now',notifications:'Notifications',search:'Search',workstreams:'Workstreams',history:'History',insights:'Insights',settings:'Settings'};
const validRoutes=new Set(Object.keys(routeNames));
const workspaceSections=new Set(['chat','files','subagents','details']);
function parseSessionHash(){
  const parts=location.hash.replace(/^#/,'').split('/');
  if(parts[0]!=='session'||!parts[1])return null;
  try{
    const sid=decodeURIComponent(parts[1]),section=workspaceSections.has(parts[2])?parts[2]:'chat';
    let item=parts[3]?decodeURIComponent(parts.slice(3).join('/')):null;
    if(section==='files'&&item&&!/^[0-9a-f]{24}$/.test(item))item=null;
    if(section==='subagents'&&item&&!/^agent-[A-Za-z0-9_-]{1,64}$/.test(item))item=null;
    if(!['files','subagents'].includes(section))item=null;
    return{sid,section,item};
  }catch(_){return null;}
}
globalThis.pendingWorkspaceRoute=parseSessionHash();
function hashDestination(){
  const parts=location.hash.replace(/^#/,'').split('/'),route=parts[0];
  let detail=null;
  if((route==='notifications'||route==='settings')&&parts[1]){
    try{detail=decodeURIComponent(parts.slice(1).join('/'));}catch(_){detail=null;}
  }
  return{route:validRoutes.has(route)?route:'now',detail};
}
const initialDestination=hashDestination();
if(initialDestination.route==='notifications'&&initialDestination.detail&&
    !(history.state&&history.state.eventId)){
  const detailHash=`#notifications/${encodeURIComponent(initialDestination.detail)}`;
  history.replaceState({fdRoute:'notifications'},'','#notifications');
  history.pushState({fdRoute:'notifications',eventId:initialDestination.detail},'',detailHash);
}
globalThis.currentRoute=initialDestination.route;globalThis.notificationDetailId=initialDestination.route==='notifications'?initialDestination.detail:null;
globalThis.nowFilter=draftValue('filter:now');globalThis.nowState='all';globalThis.workFilter=draftValue('filter:workstreams');globalThis.workState='all';
const NAV_SIDE_KEY='fleet.navSide.v1';
globalThis.navSide=localStorage.getItem(NAV_SIDE_KEY)==='right'?'right':'left';
function applyNavSide(){document.documentElement.dataset.navSide=navSide;}
function setNavSide(value){
  navSide=value==='right'?'right':'left';
  localStorage.setItem(NAV_SIDE_KEY,navSide);applyNavSide();renderSettings();
}
applyNavSide();
globalThis.workstreamData={ok:true,workstreams:[]};globalThis.workstreamsLoading=false;globalThis.workstreamsLoadedAt=0;
const SAVED_VIEW_KEY='fleet.savedViews.v1';
globalThis.savedViews=(()=>{try{
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
      (button.dataset.route==='more'&&(route==='history'||route==='insights'||route==='settings'));
    button.classList.toggle('active',active);
    if(button.dataset.route!=='more'){
      if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
    }
  });
  const title=$('#mobiletitle');if(title)title.textContent=route==='settings'?'Settings':(routeNames[route]||'Now');
}
function navigateTo(route,push=true,preserveNotificationDetail=false){
  closeMobileMore();
  if(route==='settings'){
    applyRouteNav('settings');
    openSettings();
    return;
  }
  if(!validRoutes.has(route))route='now';
  // choosing a rail destination returns an expanded workspace to pane mode so
  // the destination is actually visible beside it (prototype behavior)
  if(push&&globalThis.sessionView&&globalThis.workspaceExpanded&&workspaceDocked())toggleWorkspaceExpand();
  if(route!=='notifications'||!preserveNotificationDetail)notificationDetailId=null;
  currentRoute=route;
  document.querySelectorAll('[data-destination]').forEach(section=>{section.hidden=section.dataset.destination!==route;});
  applyRouteNav(route);
  if(push&&location.hash!=='#'+route)history.pushState({fdRoute:route},'','#'+route);
  if(route==='workstreams'){
    const workstreams=$('#workstreams');
    if(workstreams&&!workstreamsLoadedAt&&!workstreams.firstChild){
      workstreams.innerHTML='<div class="destinationempty"><span class="delivery sending" aria-hidden="true">◌</span><b>Grouping repositories…</b><p>Now continues polling while this page loads.</p></div>';
    }
  }
  // Destination visibility and selected navigation are the tap's immediate
  // feedback. Paint them before route-specific rendering or fetch setup so a
  // cached notification/search result list cannot delay the pressed response.
  requestAnimationFrame(()=>{
    if(currentRoute!==route)return;
    if(route==='insights'){loadInsights();loadBudgets();}
    else if(route==='search'){loadSearchStatus();runSearch(true);}
    else if(route==='workstreams')loadWorkstreams();
    // Revisiting History must not mean "load the next 100 rows". The explicit
    // Show more control owns pagination; navigation only seeds the first page.
    else if(route==='history'&&!historyLoadedAt&&!historyLoading)loadHistory(true);
    else if(route==='notifications'){renderNotifications();loadNotifications(true);}
  });
  window.scrollTo({top:0,behavior:'auto'});
}
function setNowFilter(value){nowFilter=value;setDraft('filter:now',value);render(last,true);}
globalThis.nowStateFrame=0;
function setNowState(value){
  nowState=['all','needs_you','working','available','subagents'].includes(value)?value:'all';
  document.querySelectorAll('[data-now-filter]').forEach(button=>{
    const active=button.dataset.nowFilter===nowState;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
  });
  if(nowStateFrame)cancelAnimationFrame(nowStateFrame);
  nowStateFrame=requestAnimationFrame(()=>{nowStateFrame=0;render(last,true);});
}
async function loadWorkstreams(force=false){
  if(workstreamsLoading||(!force&&Date.now()-workstreamsLoadedAt<1800))return;
  workstreamsLoading=true;renderWorkstreams(workstreamData);
  try{
    const r=await fetch('/api/workstreams',{cache:'no-store'}),data=await r.json();
    if(!r.ok||!data.ok)throw new Error(data.error||'Workstreams unavailable');
    workstreamData=data;workstreamsLoadedAt=Date.now();
  }catch(error){workstreamData={ok:false,error:String(error),workstreams:workstreamData.workstreams||[]};}
  finally{workstreamsLoading=false;renderWorkstreams(workstreamData);if(settingsOpen&&settingsSection==='budgets')renderSettings();}
}
function setWorkFilter(value){workFilter=value;setDraft('filter:workstreams',value);renderWorkstreams(workstreamData);}
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
    setDraft('filter:now',nowFilter);
    const input=$('#nowfilter');if(input)input.value=nowFilter;render(last,true);
  }else{
    workFilter=view.query||'';workState=view.state||'all';
    setDraft('filter:workstreams',workFilter);
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
  if(nowState==='subagents')return false;
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

Object.assign(globalThis,{fleetPerf,actCookieName,openCards,expandedPeeks,infoOpen,doneOpen,filesOpen,stateInfoOpen,routeNames,validRoutes,workspaceSections,initialDestination,NAV_SIDE_KEY,SAVED_VIEW_KEY});
