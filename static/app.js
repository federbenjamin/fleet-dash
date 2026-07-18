const $=q=>document.querySelector(q);
const DRAFT_STORE_KEY='fleet.drafts.v1';
const OFFLINE_MESSAGE_STORE_KEY='fleet.offlineMessages.v1';
const IMAGE_DRAFT_STORE_KEY='fleet.imageDrafts.v1';
const IMAGE_DB_NAME='fleet-images-v1',IMAGE_STORE='images';
const IMAGE_MAX_BYTES=10*1024*1024,IMAGE_MAX_COUNT=4,IMAGE_TTL_MS=24*60*60*1000;
let draftStore=(()=>{try{
  const value=JSON.parse(localStorage.getItem(DRAFT_STORE_KEY)||'{}');
  return value&&typeof value==='object'&&!Array.isArray(value)?value:{};
}catch(_){return {};}})();
function draftValue(key,fallback=''){
  return Object.prototype.hasOwnProperty.call(draftStore,key)?String(draftStore[key]):String(fallback??'');
}
function persistDrafts(){
  try{localStorage.setItem(DRAFT_STORE_KEY,JSON.stringify(draftStore));}catch(error){
    console.warn('Fleet could not persist drafts',error);
  }
}
function setDraft(key,value){
  if(!key)return;
  const text=String(value??'').slice(0,30000);
  if(text)draftStore[key]=text;else delete draftStore[key];
  const keys=Object.keys(draftStore);for(const old of keys.slice(0,Math.max(0,keys.length-200)))delete draftStore[old];
  persistDrafts();
}
function clearDraft(...keys){let changed=false;for(const key of keys){
  if(key&&Object.prototype.hasOwnProperty.call(draftStore,key)){delete draftStore[key];changed=true;}
}if(changed)persistDrafts();}
const composerDraftKey=sid=>`composer:${sid}`;
const relayDraftKey=(sid,aid)=>`relay:${sid}:${aid}`;
const questionDraftPrefix=(sid,nonce)=>`request:${sid}:${nonce}:`;
function clearDraftPrefix(prefix){
  const keys=Object.keys(draftStore).filter(key=>key.startsWith(prefix));
  clearDraft(...keys);
}
let offlineMessages=(()=>{try{
  const value=JSON.parse(localStorage.getItem(OFFLINE_MESSAGE_STORE_KEY)||'[]');
  return(Array.isArray(value)?value:[]).filter(item=>item&&
    /^[A-Za-z0-9_-]{1,100}$/.test(String(item.id||''))&&
    typeof item.sid==='string'&&item.sid.length>0&&item.sid.length<=200&&
    typeof item.text==='string'&&item.text.trim()&&item.text.length<=30000&&
    Number.isFinite(Number(item.created))).slice(-100).map(item=>({
      id:String(item.id),sid:item.sid,text:item.text,created:Number(item.created),
      imageIds:(Array.isArray(item.imageIds)?item.imageIds:[]).filter(id=>
        /^[A-Za-z0-9_-]{1,100}$/.test(String(id))).slice(0,IMAGE_MAX_COUNT).map(String),
      baseCount:Math.max(0,Number(item.baseCount)||0)}));
}catch(_){return [];}})();
function persistOfflineMessages(){
  try{
    if(offlineMessages.length)localStorage.setItem(OFFLINE_MESSAGE_STORE_KEY,JSON.stringify(offlineMessages));
    else localStorage.removeItem(OFFLINE_MESSAGE_STORE_KEY);
  }catch(error){console.warn('Fleet could not persist the offline message queue',error);}
}
function offlineMessageId(){
  if(crypto?.randomUUID)return crypto.randomUUID().replace(/-/g,'');
  return`${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`.slice(0,100);
}
let imageDrafts=(()=>{try{
  const value=JSON.parse(localStorage.getItem(IMAGE_DRAFT_STORE_KEY)||'{}');
  if(!value||typeof value!=='object'||Array.isArray(value))return{};
  return Object.fromEntries(Object.entries(value).slice(-200).map(([sid,ids])=>[
    String(sid),Array.isArray(ids)?ids.filter(id=>/^[A-Za-z0-9_-]{1,100}$/.test(String(id)))
      .slice(0,IMAGE_MAX_COUNT).map(String):[]]));
}catch(_){return {};}})();
function persistImageDrafts(){try{
  const clean=Object.fromEntries(Object.entries(imageDrafts).filter(([,ids])=>ids.length));
  imageDrafts=clean;
  if(Object.keys(clean).length)localStorage.setItem(IMAGE_DRAFT_STORE_KEY,JSON.stringify(clean));
  else localStorage.removeItem(IMAGE_DRAFT_STORE_KEY);
}catch(error){console.warn('Fleet could not persist image draft references',error);}}
const imageDraftIds=sid=>[...(imageDrafts[String(sid)]||[])];
function setImageDraftIds(sid,ids){
  const clean=[...new Set((ids||[]).map(String).filter(id=>/^[A-Za-z0-9_-]{1,100}$/.test(id)))]
    .slice(0,IMAGE_MAX_COUNT);
  if(clean.length)imageDrafts[String(sid)]=clean;else delete imageDrafts[String(sid)];
  persistImageDrafts();
}
function restoreImageDraftIds(sid,ids){setImageDraftIds(sid,[...imageDraftIds(sid),...(ids||[])]);}
let imageDbPromise=null;
function imageDb(){
  if(!('indexedDB' in window))return Promise.reject(new Error('This browser cannot persist image drafts'));
  if(imageDbPromise)return imageDbPromise;
  imageDbPromise=new Promise((resolve,reject)=>{
    const request=indexedDB.open(IMAGE_DB_NAME,1);
    request.onupgradeneeded=()=>request.result.createObjectStore(IMAGE_STORE,{keyPath:'id'});
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
  });
  return imageDbPromise;
}
async function imageStoreRequest(mode,operation){
  const db=await imageDb();
  return new Promise((resolve,reject)=>{const tx=db.transaction(IMAGE_STORE,mode),store=tx.objectStore(IMAGE_STORE);
    let request,result;try{request=operation(store);}catch(error){reject(error);return;}
    request.onsuccess=()=>{result=request.result;};request.onerror=()=>reject(request.error);
    tx.oncomplete=()=>resolve(result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
  });
}
const putImage=record=>imageStoreRequest('readwrite',store=>store.put(record));
const getImage=id=>imageStoreRequest('readonly',store=>store.get(String(id)));
const deleteImage=id=>imageStoreRequest('readwrite',store=>store.delete(String(id)));
async function deleteImages(ids){await Promise.allSettled((ids||[]).map(deleteImage));}
async function pruneImages(){
  let records=[];try{records=await imageStoreRequest('readonly',store=>store.getAll());}catch(_){return;}
  const cutoff=Date.now()-IMAGE_TTL_MS;
  await deleteImages(records.filter(record=>Number(record.created||0)<cutoff).map(record=>record.id));
}
document.addEventListener('input',event=>{
  const input=event.target,key=input?.dataset?.draftKey;
  if(key&&input.type!=='password')setDraft(key,input.value);
},true);
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
let pushActionFallback=(()=>{const value=new URLSearchParams(location.search).get('push_action');
  return ['snooze','mute'].includes(value)?value:'';})();
(()=>{const url=new URL(location.href),token=url.searchParams.get('token');
  if(token&&/^[0-9a-f]+$/.test(token))
    document.cookie=`act_token=${token};path=/;max-age=31536000;SameSite=Lax`;
  if(token||pushActionFallback){url.searchParams.delete('token');url.searchParams.delete('push_action');
    history.replaceState(history.state,'',url.pathname+url.search+url.hash);}})();
const open=new Set();
const expandedPeeks=new Set();
const infoOpen=new Set(),doneOpen=new Set(),filesOpen=new Set(),stateInfoOpen=new Set();  // detail-panel fold state, survives re-renders
const routeNames={now:'Now',notifications:'Notifications',search:'Search',workstreams:'Workstreams',history:'History',insights:'Insights'};
const validRoutes=new Set(Object.keys(routeNames));
function hashDestination(){
  const parts=location.hash.replace(/^#/,'').split('/'),route=parts[0];
  let detail=null;
  if(route==='notifications'&&parts[1]){
    try{detail=decodeURIComponent(parts.slice(1).join('/'));}catch(_){detail=null;}
  }
  return{route:validRoutes.has(route)?route:'now',detail};
}
const initialDestination=hashDestination();
if(initialDestination.detail&&!(history.state&&history.state.eventId)){
  const detailHash=`#notifications/${encodeURIComponent(initialDestination.detail)}`;
  history.replaceState({fdRoute:'notifications'},'','#notifications');
  history.pushState({fdRoute:'notifications',eventId:initialDestination.detail},'',detailHash);
}
let currentRoute=initialDestination.route,notificationDetailId=initialDestination.detail;
let nowFilter=draftValue('filter:now'),nowState='all',workFilter=draftValue('filter:workstreams'),workState='all';
const NAV_SIDE_KEY='fleet.navSide.v1';
let navSide=localStorage.getItem(NAV_SIDE_KEY)==='right'?'right':'left';
function applyNavSide(){document.documentElement.dataset.navSide=navSide;}
function setNavSide(value){
  navSide=value==='right'?'right':'left';
  localStorage.setItem(NAV_SIDE_KEY,navSide);applyNavSide();renderSettings();
}
applyNavSide();
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
  if(route!=='notifications'||!preserveNotificationDetail)notificationDetailId=null;
  currentRoute=route;
  document.querySelectorAll('[data-destination]').forEach(section=>{section.hidden=section.dataset.destination!==route;});
  applyRouteNav(route);
  if(push&&location.hash!=='#'+route)history.pushState({fdRoute:route},'','#'+route);
  if(route==='insights'){loadInsights();loadBudgets();}
  if(route==='search'){loadSearchStatus();runSearch(true);}
  if(route==='workstreams'){
    const workstreams=$('#workstreams');
    if(workstreams&&!workstreamsLoadedAt&&!workstreams.firstChild){
      workstreams.innerHTML='<div class="destinationempty"><span class="delivery sending" aria-hidden="true">◌</span><b>Grouping repositories…</b><p>Now continues polling while this page loads.</p></div>';
    }
    requestAnimationFrame(()=>{if(currentRoute==='workstreams')loadWorkstreams();});
  }
  if(route==='history')requestAnimationFrame(()=>{
    if(currentRoute==='history')loadHistory(!(historyData.items||[]).length);
  });
  if(route==='notifications'){renderNotifications();loadNotifications(true);}
  window.scrollTo({top:0,behavior:'auto'});
}
function setNowFilter(value){nowFilter=value;setDraft('filter:now',value);render(last,true);}
let nowStateFrame=0;
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
  finally{workstreamsLoading=false;renderWorkstreams(workstreamData);if(settingsOpen&&budgetSettingsOpen)renderSettings();}
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
  if(searchError&&!searchItems.length){el.innerHTML=`<div class="searchempty searcherror">${esc(searchError)} <button onclick="runSearch(true)">retry</button></div>`;more.hidden=true;return;}
  if(searchBusy&&!searchItems.length){el.innerHTML='<div class="searchempty">Searching…</div>';more.hidden=true;return;}
  if(!searchItems.length){el.innerHTML=`<div class="searchempty">${searchHasCriteria()?
    'No indexed conversation matches these filters.':'Type a search or choose a filter.'}</div>`;more.hidden=true;return;}
  el.innerHTML=(searchError?`<div class="searchempty searcherror">${esc(searchError)} <button onclick="runSearch(false)">retry page</button></div>`:'')+searchItems.map(item=>`<button class="searchresult" onclick="openSearchContext(${Number(item.id)})">
    <span class="searchprovider ${item.provider==='claude'?'claude':'codex'}">${item.provider==='claude'?'C':'X'}</span>
    <span class="searchcopy"><span class="searchtitle"><b>${esc(item.title||item.project||'Conversation')}</b>
      <span class="searchbadge">${esc(item.source_kind==='subagent'?'subagent':item.kind||'message')}</span></span>
      <span class="searchmeta">${esc([item.provider,item.project,item.branch,item.agent_id].filter(Boolean).join(' · '))}</span>
      <span class="searchsnippet">${esc(item.snippet||'')}</span></span>
    <span class="searchtime">${esc(searchWhen(item.timestamp||item.timestamp_epoch*1000))}</span></button>`).join('');
  more.hidden=searchCursor==null&&!searchBusy;
  more.disabled=searchBusy;
  more.textContent=searchBusy?'Loading more…':'Load more';
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
    if(reset)searchItems=[];
    searchCursor=cursor;searchError=String(e.message||e);
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
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const stateLabel={running:'Working',needs_you:'Response needed',turn_done:'Available',idle:'Available',
  stalled:'Slow',stalled_or_prompt:'Check session',dormant:'Inactive',reopenable:'Reopenable',
  stale:'Unavailable',blocked:'Limit reached',error:'Fix needed'};

// ---- on-demand provider plan usage ----------------------------------------
let usageOpen=false;
function closeUsage(){
  usageOpen=false;
  $('#usagepanel')?.classList.remove('open');
  $('#usagechip')?.setAttribute('aria-expanded','false');
}
function toggleUsage(){
  usageOpen=!usageOpen;
  $('#usagepanel')?.classList.toggle('open',usageOpen);
  $('#usagechip')?.setAttribute('aria-expanded',String(usageOpen));
}
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
  const el=$('#usagebody'),chip=$('#usagechip');
  if(!el||!chip)return;
  const claude=(providers&&providers.claude)||legacy;
  const codex=providers&&providers.codex;
  // Spark has a separate preview-model allowance. Keep it in the provider/API
  // data, but omit the unused model-specific bucket from the account summary.
  const codexBuckets=(codex?.buckets||[]).filter(b=>
    !/^gpt-5\.3-codex-spark\b/i.test(String(b.label||'')));
  const claudeProfiles=claude?.profiles?.length?claude.profiles:[claude];
  const visiblePercentages=[
    ...claudeProfiles.filter(Boolean).flatMap(profile=>[
      profile.five_hour_pct,claude?.show_week===false?null:profile.weekly_pct,
      claude?.show_week===false?null:profile.fable_weekly_pct]),
    ...codexBuckets.map(bucket=>bucket.used_pct),
  ].filter(value=>Number.isFinite(Number(value))).map(Number);
  const worst=visiblePercentages.length?Math.max(...visiblePercentages):null;
  const activeClaude=claudeProfiles.find(profile=>profile?.active) || claudeProfiles.find(Boolean);
  const claudeWindows=activeClaude?[activeClaude.five_hour_pct,
    claude?.show_week===false?null:activeClaude.weekly_pct]
    .filter(value=>Number.isFinite(Number(value))).map(value=>Math.round(Number(value))):[];
  const activeCodex=codexBuckets.map(bucket=>Number(bucket.used_pct))
    .filter(Number.isFinite).sort((a,b)=>b-a)[0];
  const summaries=[];
  if(claudeWindows.length)summaries.push(`Claude ${claudeWindows.join('/')}`);
  if(Number.isFinite(activeCodex))summaries.push(`Codex ${Math.round(activeCodex)}`);
  chip.classList.remove('usagewarn','usagedanger');
  if(worst>=90)chip.classList.add('usagedanger');else if(worst>=70)chip.classList.add('usagewarn');
  chip.textContent=`Usage${summaries.length?` · ${summaries.join(' · ')}`:''}`;
  chip.title='Claude active account: 5-hour/weekly · Codex: highest active non-Spark window';
  const claudeHtml=claude&&claudeProfiles.some(p=>p&&(p.five_hour_pct!=null||p.weekly_pct!=null||p.email))||claude?.lifetime_tokens!=null
    ?`<div class="uprovider">${claudeProfiles.filter(Boolean).map((profile,index)=>`<div class="uaccount">
      <div class="uhead"><span class="uname">Claude Code</span>
        ${profile.email?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="uemail">${esc(profile.email)}</span></span>`:''}
        ${claude.show_active!==false&&profile.active?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="uactive">active</span></span>`:''}
        ${index===0&&claude.lifetime_tokens!=null?`<span class="useg" title="All local Claude transcripts on this Mac across profiles, including saved subagents; excludes deleted history, claude.ai, and other computers"><i class="usep" aria-hidden="true">·</i><span class="umeta">${fmtTok(claude.lifetime_tokens)} local lifetime tokens</span></span>`:''}</div>
      ${ugauge('5-hour',profile.five_hour_pct,usageReset(profile.five_hour_reset))}
      ${claude.show_week===false?'':ugauge('weekly',profile.weekly_pct,usageReset(profile.weekly_reset))}
      ${claude.show_week===false?'':ugauge('Fable weekly',profile.fable_weekly_pct,usageReset(profile.fable_weekly_reset))}</div>`).join('')}</div>`:'';
  const codexHtml=codex&&(codexBuckets.length||codex.email||codex.plan_type||codex.lifetime_tokens!=null||codex.reset_credits||codex.error)?`<div class="uprovider">
    <div class="uhead"><span class="uname">Codex CLI</span>
      ${codex.email?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="uemail">${esc(codex.email)}</span></span>`:''}
      ${codex.plan_type?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="umeta">${esc(codex.plan_type.replaceAll('_',' '))}</span></span>`:''}
      ${codex.lifetime_tokens!=null?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="umeta">${fmtTok(codex.lifetime_tokens)} lifetime tokens</span></span>`:''}
      ${codex.reset_credits?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="umeta">${codex.reset_credits} reset credit</span></span>`:''}</div>
    ${codex.error?`<span class="umeta">${codex.stale?'stale — ':''}${esc(codex.error)}</span>`:''}
    ${codexBuckets.map(b=>ugauge(b.label,b.used_pct,usageReset(b.reset))).join('')}</div>`:'';
  if(!claudeHtml&&!codexHtml){el.className='empty';el.innerHTML='<div class="usageempty">Usage data is unavailable.</div>';return;}
  el.className='';el.innerHTML=claudeHtml+codexHtml;
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
const outboxActions=new Map();
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
  const feedbackStarted=performance.now();
  $('#outboxview').style.display='flex';syncOverlayHistory();renderOutboxFull();loadOutbox(true);
  recordInputFeedback(feedbackStarted,'outbox_open');
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
  const action=outboxActions.get(item.id)||{};
  const error=action.error||item.error||item.blocked_reason;
  const canEdit=item.editable,canRetry=item.retryable;
  return`<article class="outboxrow ${esc(item.state)}"><div class="outboxtop"><span class="outboxstate">${esc(item.state_label||item.state)}</span>
    <span class="outboxtime">${esc(outboxWhen(item))}</span></div><div class="outboxmessage">${esc(item.message||'')}</div>
    <div class="outboxmeta">${esc(outboxTarget(item))} · ${esc(String(item.kind||'').replaceAll('_',' '))}${item.created_zone?` · ${esc(item.created_zone)}`:''}</div>
    ${error?`<div class="outboxerror" role="alert">${esc(error)}</div>`:''}<div class="outboxactions">
      ${action.busy?'<span class="outboxworking" role="status"><span class="delivery sending" aria-hidden="true">◌</span> working…</span>':''}
      ${canEdit?`<button ${action.busy?'disabled':''} onclick="editOutbox('${item.id}')">Edit</button><button class="primary" ${action.busy?'disabled':''} onclick="outboxAction('${item.id}','outbox_send_now')">Send now</button><button ${action.busy?'disabled':''} onclick="confirmCancelOutbox('${item.id}')">Cancel</button>`:''}
      ${canRetry?`<button class="primary" ${action.busy?'disabled':''} onclick="editOutbox('${item.id}','retry')">Retry / retarget</button>`:''}
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
  if(outboxActions.get(id)?.busy)return{ok:false,error:'Outbox action already running'};
  const feedbackStarted=performance.now();
  outboxActions.set(id,{busy:true,error:''});renderOutboxFull();
  recordInputFeedback(feedbackStarted,'outbox_action');
  try{
    const response=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type,outbox_id:id,...payload})});
    const result=await response.json();
    if(!response.ok||!result.ok)throw new Error(result.error||'Outbox action failed');
    mergeOutboxResult(result);
    await loadOutbox(true);
    outboxActions.delete(id);renderOutboxFull();return result;
  }catch(error){
    const message=String(error.message||error);
    outboxActions.set(id,{busy:false,error:message});renderOutboxFull();
    return{ok:false,error:message};
  }
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
function closeComposerMenus(except=null){
  document.querySelectorAll('.composerplus.open').forEach(menu=>{
    if(menu===except)return;
    menu.classList.remove('open');
    menu.previousElementSibling?.setAttribute('aria-expanded','false');
    menu.closest('#sact')?.classList.remove('tools-open');
  });
}
function toggleComposerMenu(button,event){
  event?.stopPropagation();
  const menu=button?.nextElementSibling;if(!menu)return;
  const opening=!menu.classList.contains('open');
  closeComposerMenus(opening?menu:null);
  menu.classList.toggle('open',opening);button.setAttribute('aria-expanded',String(opening));
  menu.closest('#sact')?.classList.toggle('tools-open',opening);
}
function openComposerSchedule(sid,inputId,agentId=''){
  closeComposerMenus();
  openSchedule(sid,inputId,agentId);
}
function composerTools(sid,inputId){
  return`<span class="composertools"><button type="button" class="pbtn composerplusbtn" aria-label="message options" aria-haspopup="menu" aria-expanded="false" onclick="toggleComposerMenu(this,event)">＋</button>
    <span class="composerplus" role="menu">
      <label class="composerplusitem" role="menuitem" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.querySelector('input').click()}">▧ <span>Send picture</span><input type="file" accept="image/jpeg,image/png,image/gif,image/webp,image/heic,image/heif,.heic,.heif" multiple onchange="closeComposerMenus();chooseImages('${sid}',this)"></label>
      <button type="button" class="composerplusitem" role="menuitem" onclick="openComposerSchedule('${sid}','${inputId}')">◷ <span>Schedule message</span></button>
    </span></span>`;
}
async function openSchedule(sid,inputId,agentId='',existing=null,spawnSpec=null,message=''){
  const feedbackStarted=performance.now();
  const loading=loadOutbox();
  const input=inputId?document.getElementById(inputId):null;
  const source=existing||{};
  const draftKey=source.id?`schedule:${source.id}`:spawnSpec?'schedule:new-session':
    `schedule:${sid}:${agentId||'session'}`;
  const initialMessage=message||source.message||(input?.value||'');
  const scheduleSpawn=spawnSpec||source.spawn_spec?{...(spawnSpec||source.spawn_spec)}:null;
  if(scheduleSpawn){
    for(const key of ['cwd','model','effort','worktree_name'])
      scheduleSpawn[key]=draftValue(`${draftKey}:${key}`,scheduleSpawn[key]||'');
  }
  const zone=source.created_zone||Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
  scheduleView={id:source.id||null,operation:null,sid:sid||source.target_session_id||'',
    agentId:agentId||source.target_agent_id||'',inputId:inputId||null,
    kind:source.kind||((spawnSpec||source.spawn_spec)?'new_session':'at_time'),
    message:draftValue(`${draftKey}:message`,initialMessage),zone,draftKey,
    localTime:draftValue(`${draftKey}:time`,source.local_time||localInputAt(source.trigger_at,zone)||defaultScheduleTime()),
    fold:source.trigger_fold,choices:null,usageKey:'',spawnSpec:scheduleSpawn};
  if(source.usage_account_id)scheduleView.usageKey=[source.target_provider,source.usage_account_id,source.usage_window_id].join('|');
  const stacked=anyOverlay();
  $('#scheduleview').style.display='flex';$('#scheduletitle').textContent=source.id?'Edit Outbox message':
    scheduleView.kind==='new_session'?'Schedule new coding session':'Schedule message';
  if(stacked){schedulePushed=true;history.pushState({fdSchedule:1},'');}else syncOverlayHistory();
  renderSchedule();
  recordInputFeedback(feedbackStarted,'schedule_open');
  const view=scheduleView;
  await loading;
  if(scheduleView===view)renderSchedule();
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
     <label class="nflab">directory</label><input class="nfin" data-draft-key="${esc(v.draftKey+':cwd')}" value="${esc(spawn.cwd||'')}" oninput="scheduleView.spawnSpec.cwd=this.value">
     <div class="nfrow"><div class="nfcol"><label class="nflab">model</label><input class="nfin" data-draft-key="${esc(v.draftKey+':model')}" value="${esc(spawn.model||'')}" placeholder="default" oninput="scheduleView.spawnSpec.model=this.value"></div>
     <div class="nfcol"><label class="nflab">effort</label><input class="nfin" data-draft-key="${esc(v.draftKey+':effort')}" value="${esc(spawn.effort||'')}" placeholder="default" oninput="scheduleView.spawnSpec.effort=this.value"></div></div>
     ${spawn.provider==='codex'?`<label class="nflab">mode</label><select class="nfsel" onchange="scheduleView.spawnSpec.mode=this.value"><option value="plan" ${spawn.mode==='plan'?'selected':''}>Plan</option><option value="default" ${spawn.mode==='default'?'selected':''}>Default</option></select>`:
       `<label class="nfcheck"><input type="checkbox" ${spawn.worktree?'checked':''} onchange="scheduleView.spawnSpec.worktree=this.checked;renderSchedule()"><span>new git worktree</span></label>${spawn.worktree?`<input class="nfin" data-draft-key="${esc(v.draftKey+':worktree_name')}" value="${esc(spawn.worktree_name||'')}" placeholder="worktree name (optional)" oninput="scheduleView.spawnSpec.worktree_name=this.value">`:''}`}`}
    <label class="nflab">message</label><textarea class="nfin" data-draft-key="${esc(v.draftKey+':message')}" maxlength="2000" oninput="scheduleView.message=this.value">${esc(v.message)}</textarea>
    ${v.kind==='at_time'||isNew?`<div class="scheduletime"><label><span class="nflab">local date and time</span><input class="nfin" data-draft-key="${esc(v.draftKey+':time')}" type="datetime-local" value="${esc(v.localTime)}" onchange="scheduleView.localTime=this.value;scheduleView.choices=null"></label><span class="schedulezone">${esc(v.zone)}</span></div>`:''}
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
  clearDraftPrefix(v.draftKey+':');
  if(v.inputId)clearDraft(composerDraftKey(v.sid),relayDraftKey(v.sid,v.agentId));
  if(v.kind==='new_session')clearDraft('new:directory','new:worktree','new:message');
  dismissOverlay();await loadOutbox(true);render(last,true);
}

// ---- deterministic in-app briefing ---------------------------------------
const BRIEF_DEVICE_KEY='fleet.briefingDevice.v1';
const briefingDevice=(()=>{let value=localStorage.getItem(BRIEF_DEVICE_KEY);
  if(!value){value=(crypto.randomUUID?crypto.randomUUID():`device-${Date.now()}-${Math.random().toString(16).slice(2)}`);
    localStorage.setItem(BRIEF_DEVICE_KEY,value);}return value;})();
let pushInstallPrompt=null,pushBusy=false,pushLoadedAt=0;
let pushData={ok:true,configured:false,delivery:'not_configured',current_device:null};
let pushLocal={secure:window.isSecureContext,serviceWorker:'serviceWorker' in navigator,
  notifications:'Notification' in window,push:'PushManager' in window,
  permission:'Notification' in window?Notification.permission:'unsupported',registered:false,
  error:'',notice:''};
const pushStandalone=()=>matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
const pushPlatform=()=>navigator.userAgentData?.platform||navigator.platform||'browser';
const pushDeviceName=()=>pushData.current_device?.display_name||`Fleet on ${pushPlatform()}`;
function pushSupported(){return pushLocal.secure&&pushLocal.serviceWorker&&pushLocal.notifications&&pushLocal.push;}
function pushB64(value){
  const padding='='.repeat((4-value.length%4)%4),raw=atob((value+padding).replace(/-/g,'+').replace(/_/g,'/'));
  return Uint8Array.from([...raw].map(char=>char.charCodeAt(0)));
}
async function pushApi(path,body){
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}),data=await response.json();
  if(!response.ok||!data.ok)throw new Error(data.error||'Push setup failed');return data;
}
async function registerFleetServiceWorker(){
  if(!pushLocal.secure||!pushLocal.serviceWorker)return null;
  try{
    const registration=await navigator.serviceWorker.register('/sw.js',{scope:'/'});
    pushLocal.registered=true;return registration;
  }catch(error){pushLocal.error=`App service unavailable: ${String(error.message||error)}`;return null;}
}
async function loadPushState(force=false){
  if(!force&&Date.now()-pushLoadedAt<4000)return pushData;
  pushLocal.permission=pushLocal.notifications?Notification.permission:'unsupported';
  try{
    const response=await fetch(`/api/push/config?device=${encodeURIComponent(briefingDevice)}`,{cache:'no-store'});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(response.status===403?
      'Open Fleet once with its action-token URL on this device':(data.error||'Push status unavailable'));
    pushData=data;pushLocal.error='';pushLoadedAt=Date.now();
  }catch(error){pushLocal.error=String(error.message||error);}
  if(settingsOpen)renderSettings();return pushData;
}
async function currentPushSubscription(){
  const registration=await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}
async function savePushSubscription(subscription){
  const data=await pushApi('/api/push/subscription',{device_id:briefingDevice,
    display_name:pushDeviceName(),platform:pushPlatform(),permission_state:'granted',
    subscription:subscription.toJSON()});
  pushData.current_device=data.device;return data.device;
}
async function repairPushSubscription({interactive=false}={}){
  if(!pushSupported()||!pushData.configured||Notification.permission!=='granted')return false;
  let subscription=await currentPushSubscription();
  if(!subscription&&(interactive||pushData.current_device)){
    subscription=await navigator.serviceWorker.ready.then(registration=>registration.pushManager.subscribe({
      userVisibleOnly:true,applicationServerKey:pushB64(pushData.public_key)}));
  }
  if(!subscription)return false;
  await savePushSubscription(subscription);return true;
}
async function enableFleetPush(){
  if(pushBusy)return;const started=performance.now();pushBusy=true;pushLocal.error='';renderSettings();
  try{
    if(!pushLocal.secure)throw new Error('Open Fleet from its HTTPS tailnet URL');
    if(!pushSupported())throw new Error('This browser does not support Web Push');
    if(!pushData.configured)throw new Error('Fleet Web Push delivery is not configured yet');
    if(/iPhone|iPad|iPod/.test(navigator.userAgent)&&!pushStandalone())
      throw new Error('Add Fleet to the Home Screen, then enable notifications from the installed app');
    const registration=await registerFleetServiceWorker();
    if(!registration)throw new Error(pushLocal.error||'Fleet app service is unavailable');
    const permission=await Notification.requestPermission();pushLocal.permission=permission;
    if(permission!=='granted')throw new Error(permission==='denied'?
      'Notifications are blocked in browser settings':'Notification permission was not granted');
    await repairPushSubscription({interactive:true});await loadPushState(true);
  }catch(error){pushLocal.error=String(error.message||error);}
  finally{pushBusy=false;recordInputFeedback(started,'push_enable');renderSettings();}
}
async function installFleet(){
  if(!pushInstallPrompt)return;const prompt=pushInstallPrompt;pushInstallPrompt=null;
  await prompt.prompt();await prompt.userChoice.catch(()=>{});renderSettings();
}
async function renamePushDevice(value){
  const name=String(value||'').trim();if(!name||!pushData.current_device)return;
  pushBusy=true;renderSettings();try{
    const data=await pushApi('/api/push/device-settings',{device_id:briefingDevice,display_name:name});
    pushData.current_device=data.device;
  }catch(error){pushLocal.error=String(error.message||error);}
  finally{pushBusy=false;renderSettings();}
}
async function setPushDeviceEnabled(enabled){
  if(pushBusy||!pushData.current_device)return;pushBusy=true;renderSettings();try{
    const data=await pushApi('/api/push/device-settings',{device_id:briefingDevice,enabled});
    pushData.current_device=data.device;
  }catch(error){pushLocal.error=String(error.message||error);}
  finally{pushBusy=false;renderSettings();}
}
async function disconnectFleetPush(){
  if(pushBusy||!pushData.current_device)return;pushBusy=true;renderSettings();try{
    const subscription=pushSupported()?await currentPushSubscription():null;
    const data=await pushApi('/api/push/subscription',{device_id:briefingDevice,remove:true,
      permission_state:pushLocal.permission==='denied'?'denied':'expired'});
    pushData.current_device=data.device;
    if(subscription){try{await subscription.unsubscribe();}catch(_){
      pushLocal.error='Fleet delivery is disconnected; browser subscription cleanup will retry later';
    }}
  }catch(error){pushLocal.error=String(error.message||error);}
  finally{pushBusy=false;renderSettings();}
}
async function testFleetPush(){
  if(pushBusy||!pushData.current_device)return;pushBusy=true;pushLocal.error='';pushLocal.notice='';renderSettings();try{
    await pushApi('/api/push/test',{device_id:briefingDevice});
    pushLocal.notice='Test queued · delivery runs in the background';
    setTimeout(()=>loadPushState(true),750);
  }catch(error){pushLocal.error=String(error.message||error);}
  finally{pushBusy=false;renderSettings();}
}
function pushSettingsHtml(){
  const device=pushData.current_device,standalone=pushStandalone(),supported=pushSupported();
  const installState=standalone?'Installed':pushInstallPrompt?'Ready to install':'Browser tab';
  const permission=pushLocal.permission==='granted'?'Allowed':pushLocal.permission==='denied'?'Blocked':
    pushLocal.permission==='prompt'?'Not requested':'Unsupported';
  const delivery=!pushData.configured?'Server setup pending':pushData.delivery!=='ready'?
    `Worker ${String(pushData.delivery||'unavailable').replaceAll('_',' ')}`:!device?'Not connected':
    device.health==='healthy'?'Healthy':device.health==='registered'?'Registered · awaiting test':
    device.health==='disabled'?'Paused':String(device.health||'Unavailable').replaceAll('_',' ');
  const help=/iPhone|iPad|iPod/.test(navigator.userAgent)&&!standalone?
    'On iPhone or iPad: Share → Add to Home Screen. Open the installed Fleet app, then enable notifications.':
    !pushLocal.secure?'Mobile Web Push requires Fleet’s HTTPS tailnet URL. Localhost remains valid on this Mac.':
    !supported?'This browser does not expose the Service Worker, Notifications, and Push APIs together.':
    !pushData.configured?'Fleet is creating its private delivery keys. This page will update automatically.':
    pushData.delivery!=='ready'?'The delivery helper is unavailable. Persisted jobs wait and retry without delaying Fleet.':
    'Fleet sends only a minimal summary. Open Fleet for session details.';
  return`<section class="pushsetup"><div class="pushsetuphead"><span><b>Fleet app & Web Push</b><small>One installed app · per-device delivery</small></span>
    <i class="pushsignal ${esc(device?.health||(!pushData.configured?'pending':'off'))}"></i></div>
    <div class="pushrail"><span><i></i><b>App</b><small>${esc(installState)}</small></span>
      <span><i></i><b>Permission</b><small>${esc(permission)}</small></span>
      <span><i></i><b>Delivery</b><small>${esc(delivery)}</small></span></div>
    <p class="pushhelp">${esc(help)}</p>
    <div class="pushactions">
      ${pushInstallPrompt&&!standalone?'<button onclick="installFleet()">Install Fleet</button>':''}
      <button class="primary" onclick="enableFleetPush()" ${pushBusy||!supported||!pushData.configured?'disabled':''}>${pushBusy?'<span class="delivery sending">◌</span> Working…':device?'Repair subscription':'Enable notifications'}</button>
      <button onclick="testFleetPush()" ${pushBusy||!device||pushData.delivery!=='ready'?'disabled':''}>Send test</button>
    </div>
    ${device?`<div class="pushdevice"><label><span>This device</span><input value="${esc(device.display_name||'')}" maxlength="80" onchange="renamePushDevice(this.value)"></label>
      <label class="pushswitch"><input type="checkbox" ${device.enabled?'checked':''} ${device.permission_state!=='granted'?'disabled':''} onchange="setPushDeviceEnabled(this.checked)"><span>Delivery enabled</span></label>
      <button onclick="disconnectFleetPush()" ${pushBusy?'disabled':''}>Disconnect</button></div>`:''}
    ${pushLocal.error?`<div class="pusherror" role="alert">${esc(pushLocal.error)}</div>`:''}
    ${pushLocal.notice?`<div class="pushnotice" role="status">${esc(pushLocal.notice)}</div>`:''}
    <div class="sethint">Subscription endpoints and encryption keys are write-only. Fleet’s device list exposes only names, state, and health.</div></section>`;
}
async function initFleetPwa(){
  await registerFleetServiceWorker();await loadPushState(true);
  if(pushSupported()&&Notification.permission==='granted'&&pushData.current_device){
    try{await repairPushSubscription();await loadPushState(true);}catch(error){pushLocal.error=String(error.message||error);}
  }
  if(settingsOpen)renderSettings();
}
window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();pushInstallPrompt=event;if(settingsOpen)renderSettings();});
window.addEventListener('appinstalled',()=>{pushInstallPrompt=null;if(settingsOpen)renderSettings();});
navigator.serviceWorker?.addEventListener('message',event=>{if(event.data?.type==='push-subscription-change')repairPushSubscription().catch(()=>{});});
window.__fleetPush={loadPushState,enableFleetPush,repairPushSubscription};
let briefingData={ok:true,sections:{attention:[],completed:[],slow:[],outcomes:[],budgets:[],measurements:[],reviewed:[]},unread:0};
let briefingLoading=false,briefingLoadPromise=null,briefingLoadedAt=0,briefingOpen=false,briefingReviewing=false,briefingReviewError='';
async function loadBriefing(force=false){
  if(briefingLoading)return briefingLoadPromise;
  if(!force&&Date.now()-briefingLoadedAt<4000)return;
  briefingLoading=true;renderBriefing();briefingLoadPromise=(async()=>{try{
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
function safeGithubUrl(value){try{
  const url=new URL(String(value||''));
  return url.protocol==='https:'&&/^[A-Za-z0-9.-]+$/.test(url.hostname)?url.href:'';
}catch(_){return'';}}
function openGithub(value){const url=safeGithubUrl(value);if(url)window.open(url,'_blank','noopener');return Boolean(url);}
async function openBriefingSource(kind,id){
  if(kind==='session'&&id){openSession(id);return;}
  if(kind==='repository'&&id){
    let item=(workstreamData.workstreams||[]).find(row=>row.root===id);
    if(!item){await loadWorkstreams(true);item=(workstreamData.workstreams||[]).find(row=>row.root===id);}
    if(openGithub(item?.repository?.github_url))return;
    navigateTo('workstreams');return;
  }
  if(kind==='outbox'){openOutbox();return;}
  if(kind==='budget'){navigateTo('insights');return;}
  if(kind==='settings')navigateTo('settings');
}
async function markBriefingReviewed(){
  const cursor=briefingData.next_cursor;if(!cursor||cursor<=briefingData.review_cursor||briefingReviewing)return;
  briefingReviewing=true;briefingReviewError='';try{const data=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({type:'briefing_review',device_id:briefingDevice,cursor})}).then(r=>r.json());
    if(!data.ok)throw new Error(data.error||'Review marker failed');briefingData.review_cursor=data.cursor;
  }catch(error){briefingReviewError=String(error.message||error);renderBriefing();}
  finally{briefingReviewing=false;}
}
async function toggleBriefing(){briefingOpen=!briefingOpen;renderBriefing();if(briefingOpen){await loadBriefing(true);setTimeout(markBriefingReviewed,600);}}
function briefingPanelHtml(alwaysOpen=false){
  const d=briefingData,s=d.sections||{},expanded=alwaysOpen||briefingOpen;
  const current=(s.attention?.length||0)+(s.slow?.length||0)+(s.budgets||[]).filter(x=>['warning','exceeded','unavailable'].includes(x.status)).length;
  const unread=(s.completed?.length||0)+(s.outcomes?.length||0)+(s.measurements?.length||0);
  const reviewed=s.reviewed?.length||0;
  const empty=!current&&!unread&&!reviewed&&!d.error;
  const status=d.error?'Briefing unavailable':empty?`Nothing to review${briefingLoading?' · refreshing…':''}`:
    `${current} current · ${unread} since review${reviewed?` · ${reviewed} recently reviewed`:''}${d.muted_omitted?` · ${d.muted_omitted} muted from push`:''}${briefingLoading?' · refreshing…':''}`;
  return`<section class="briefingpanel notificationbrief"><button class="briefhead" onclick="${alwaysOpen?'loadBriefing(true)':'toggleBriefing()'}"><span><b>Fleet briefing</b><small>${esc(status)}</small></span><b>${alwaysOpen?'Refresh':expanded?'Hide':'Review'} ${alwaysOpen?'↻':expanded?'↑':'→'}</b></button>
    ${expanded?`<div class="briefbody">${briefingLoading?'<div class="ctxload"><span class="delivery sending" aria-hidden="true">◌</span> Refreshing briefing…</div>':''}${d.error?`<div class="provideralert">${esc(d.error)} <button onclick="loadBriefing(true)">retry</button></div>`:''}${briefingReviewError?`<div class="provideralert">${esc(briefingReviewError)} <button onclick="markBriefingReviewed()">retry review</button></div>`:''}
      ${briefingGroup('Needs attention now',s.attention)}${briefingGroup('Completed since last review',s.completed)}
      ${briefingGroup('Still working unusually slowly',s.slow)}${briefingGroup('Outcomes and artifacts',s.outcomes)}
      ${briefingGroup('Budgets and measurement',(s.budgets||[]).filter(x=>x.status!=='ok'),briefingBudget)}
      ${briefingGroup('Unavailable measurements',s.measurements)}${briefingGroup('Recently reviewed',s.reviewed)}${empty&&!briefingLoading?'<div class="destinationempty"><b>Nothing needs review</b><p>New completions and attention items will appear here.</p></div>':''}</div>`:''}</section>`;
}
function renderBriefing(){
  const el=$('#briefing');if(el)el.innerHTML=briefingPanelHtml();
  if(currentRoute==='notifications'&&notificationSection==='briefing')renderNotifications();
}

// ---- Notification Center -------------------------------------------------
const notificationKinds={question:'Question',approval:'Approval',form:'Form',reply:'Reply',
  failure:'Problem',stall:'Slow work',completion:'Completion',artifact:'Artifact',outcome:'Outcome',
  budget:'Budget',measurement:'Measurement',notification:'Notification'};
let notificationData={ok:true,events:[],delivery_problems:[],unread:0,active:0,event_cursor:0,next_cursor:null};
let notificationItems=[],notificationSection='needs',notificationLoading=false;
let notificationPollingEnabled=document.cookie.split(';').some(item=>item.trim().startsWith('act_token='));
let notificationError='',notificationLoadedAt=0,notificationAbort=null,notificationSequence=0;
let notificationDetail=null,notificationDetailLoading=false,notificationDetailError='';
let notificationActionState=pushActionFallback?{busy:false,
  message:`${pushActionFallback==='snooze'?'Snooze':'Mute'} from the notification did not complete. Review the current state and try again.`,
  error:true}:{busy:false,message:'',error:false};
pushActionFallback='';
let notificationHistoryQuery=draftValue('filter:notification-history'),notificationHistoryProvider='',notificationHistoryKind='';
let notificationHistoryWorkstream='',notificationHistorySession='',notificationHistoryAge='';
function notificationBucket(item){
  if(item.state==='snoozed')return'snoozed';
  if(item.state==='active'&&['question','approval','form','reply','stall'].includes(item.kind))return'needs';
  if(item.state==='active'&&item.kind==='failure')return'problems';
  if(item.state==='resolved'&&item.unread&&item.kind!=='failure')return'updates';
  return'history';
}
function notificationCounts(){
  const counts={needs:0,updates:0,snoozed:0,problems:(notificationData.delivery_problems||[]).length,
    briefing:0,history:0};
  notificationItems.forEach(item=>{counts[notificationBucket(item)]++;});
  return counts;
}
function updateNotificationBadges(){
  const unread=Number(notificationData.unread)||0,active=Number(notificationData.active)||0;
  const text=active&&unread?`${active}·${unread}`:String(active||unread||'');
  const desktop=$('#nav-notification-count'),mobile=$('#mobile-notification-count');
  if(desktop){desktop.textContent=text;desktop.title=`${active} active · ${unread} unread`;}
  if(mobile){mobile.textContent=String(unread||active||'');mobile.hidden=!unread&&!active;}
  if(unread&&typeof navigator.setAppBadge==='function')navigator.setAppBadge(unread).catch(()=>{});
  else if(!unread&&typeof navigator.clearAppBadge==='function')navigator.clearAppBadge().catch(()=>{});
}
let notificationReconcileBusy=false;
async function reconcileSystemNotifications(){
  if(notificationReconcileBusy||!('serviceWorker'in navigator))return;
  notificationReconcileBusy=true;
  try{
    const registration=await navigator.serviceWorker.ready,worker=registration.active;
    if(!worker)return;
    const channel=new MessageChannel();
    const ids=await new Promise(resolve=>{
      const timer=setTimeout(()=>resolve([]),1200);
      channel.port1.onmessage=event=>{clearTimeout(timer);resolve(event.data?.eventIds||[]);};
      worker.postMessage({type:'fleet-displayed-notifications'},[channel.port2]);
    });
    const resolved=[];
    await Promise.all(ids.slice(0,20).map(async id=>{
      try{
        const response=await fetch('/api/notifications?'+notificationParams(null,id),{cache:'no-store'});
        const data=await response.json(),item=(data.events||[])[0];
        if(response.ok&&data.ok&&item&&['resolved','expired'].includes(item.state))resolved.push(id);
      }catch(_){ }
    }));
    worker.postMessage({type:'fleet-notification-state',resolvedIds:resolved});
  }catch(_){ }
  finally{notificationReconcileBusy=false;}
}
function setNotificationSection(section){
  if(!['needs','updates','snoozed','problems','briefing','history'].includes(section))return;
  const started=performance.now();notificationSection=section;notificationDetailId=null;
  notificationDetail=null;notificationDetailError='';
  if(location.hash!=='#notifications')history.pushState({fdRoute:'notifications'},'','#notifications');
  renderNotifications();recordInputFeedback(started,'notifications_section');
  if(section==='briefing'){loadBriefing(true);setTimeout(markBriefingReviewed,600);}
}
function notificationParams(cursor,id){
  const params=new URLSearchParams({device:briefingDevice,limit:'60'});
  if(cursor)params.set('cursor',String(cursor));if(id)params.set('id',id);return params.toString();
}
async function loadNotifications(reset=true,force=false){
  if(notificationLoading&&!force)return;
  if(reset&&!force&&Date.now()-notificationLoadedAt<3500){
    renderNotifications();
    if(notificationDetailId&&notificationDetail?.id!==notificationDetailId)loadNotificationDetail(notificationDetailId,false);
    return;
  }
  if(!reset&&!notificationData.next_cursor)return;
  if(notificationAbort)notificationAbort.abort();
  const controller=new AbortController(),sequence=++notificationSequence;notificationAbort=controller;
  notificationLoading=true;notificationError='';renderNotifications();
  try{
    const cursor=reset?null:notificationData.next_cursor;
    const response=await fetch('/api/notifications?'+notificationParams(cursor),{cache:'no-store',signal:controller.signal});
    const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'Notifications unavailable');
    if(sequence!==notificationSequence)return;
    notificationItems=reset?(data.events||[]):notificationItems.concat(data.events||[]);
    notificationData={...data,events:notificationItems};notificationLoadedAt=Date.now();
    updateNotificationBadges();
    void reconcileSystemNotifications();
    if(notificationDetailId){
      const listed=notificationItems.find(item=>item.id===notificationDetailId);
      if(listed)notificationDetail=listed;
      else if(notificationDetail?.id!==notificationDetailId)loadNotificationDetail(notificationDetailId,false);
    }
  }catch(error){if(error.name!=='AbortError'&&sequence===notificationSequence)notificationError=String(error.message||error);}
  finally{if(sequence===notificationSequence){notificationLoading=false;notificationAbort=null;renderNotifications();}}
}
async function loadNotificationDetail(id,push=false){
  if(!id)return;const started=performance.now();notificationDetailId=id;
  if(push&&location.hash!==`#notifications/${encodeURIComponent(id)}`)
    history.pushState({fdRoute:'notifications',eventId:id},'',`#notifications/${encodeURIComponent(id)}`);
  currentRoute='notifications';notificationDetailLoading=true;notificationDetailError='';
  document.querySelectorAll('[data-destination]').forEach(section=>{section.hidden=section.dataset.destination!=='notifications';});
  applyRouteNav('notifications');renderNotifications();recordInputFeedback(started,'notification_open');
  try{
    const response=await fetch('/api/notifications?'+notificationParams(null,id),{cache:'no-store'});
    const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'Notification unavailable');
    const item=(data.events||[])[0];if(!item)throw new Error('This notification is no longer available');
    if(notificationDetailId!==id)return;notificationDetail=item;
    const index=notificationItems.findIndex(value=>value.id===id);
    if(index>=0)notificationItems[index]=item;else notificationItems.unshift(item);
    if(item.unread)void markNotificationsRead(item.sequence,false);
    void reconcileSystemNotifications();
  }catch(error){if(notificationDetailId===id)notificationDetailError=String(error.message||error);}
  finally{if(notificationDetailId===id){notificationDetailLoading=false;renderNotifications();}}
}
function openNotification(id){loadNotificationDetail(id,true);}
function closeNotificationDetail(){
  if(location.hash.startsWith('#notifications/')&&history.state?.eventId){history.back();return;}
  if(location.hash.startsWith('#notifications/'))history.replaceState({fdRoute:'notifications'},'','#notifications');
  notificationDetailId=null;notificationDetail=null;notificationDetailError='';renderNotifications();
}
async function notificationPost(path,payload,success){
  const started=performance.now();notificationActionState={busy:true,message:'Working…',error:false};
  renderNotifications();recordInputFeedback(started,'notification_action');
  try{
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'Notification action failed');
    notificationActionState={busy:false,message:success,error:false};await loadNotifications(true,true);
    if(notificationDetailId)await loadNotificationDetail(notificationDetailId,false);
  }catch(error){notificationActionState={busy:false,message:String(error.message||error),error:true};renderNotifications();}
  finally{perfRecord('notification_action_completion_ms',performance.now()-started);}
}
function snoozeNotification(id,revision,choice){
  const now=new Date(),until=new Date(now);
  if(choice==='tomorrow'){until.setDate(until.getDate()+1);until.setHours(9,0,0,0);}
  else until.setTime(now.getTime()+(choice==='hour'?3600:900)*1000);
  notificationPost('/api/notifications/snooze',{event_id:id,source_revision:revision,
    until:until.getTime()/1000},`Snoozed until ${until.toLocaleString([],choice==='tomorrow'?{weekday:'short',hour:'numeric',minute:'2-digit'}:{hour:'numeric',minute:'2-digit'})}`);
}
function wakeNotification(id,revision){notificationPost('/api/notifications/wake',
  {event_id:id,source_revision:revision},'Moved back to Needs action');}
function muteNotification(id,revision,muted){notificationPost('/api/notifications/mute',
  {device_id:briefingDevice,event_id:id,source_revision:revision,muted},muted?'Session muted until you unmute it':'Session unmuted');}
function retryNotificationDelivery(id){notificationPost('/api/notifications/retry',
  {delivery_id:id},'Delivery queued again');}
async function markNotificationsRead(cursor=notificationData.event_cursor,renderNow=true){
  cursor=Number(cursor)||0;if(!cursor||cursor<=Number(notificationData.read_cursor||0))return;
  const started=performance.now(),previous=notificationData.read_cursor;
  notificationData.read_cursor=cursor;notificationItems.forEach(item=>{
    if(item.sequence<=cursor)item.unread=false;
  });
  const locallyRead=notificationItems.filter(item=>item.sequence<=cursor&&item.sequence>Number(previous||0)).length;
  notificationData.unread=Math.max(0,Number(notificationData.unread||0)-locallyRead);
  updateNotificationBadges();if(renderNow)renderNotifications();recordInputFeedback(started,'notifications_read');
  try{
    const response=await fetch('/api/notifications/read',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({device_id:briefingDevice,cursor})}),data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'Read state failed');notificationData.read_cursor=data.cursor;
    await loadNotifications(true,true);
  }catch(error){notificationData.read_cursor=previous;notificationError=String(error.message||error);await loadNotifications(true,true);}
  renderNotifications();
}
function notificationTime(item){const value=Number(item.changed_at||item.opened_at||item.updated_at)||0;
  return value?fmtAge(Math.max(0,Date.now()/1000-value))+' ago':'';}
function notificationRow(item){
  const bucket=notificationBucket(item),id=enc(item.id),meta=[notificationKinds[item.kind]||item.kind,item.provider,
    notificationTime(item)].filter(Boolean).join(' · ');
  return`<button class="notificationrow ${esc(bucket)} ${item.unread?'unread':''}" onclick="openNotification(decodeURIComponent('${id}'))">
    <span class="notificationspine"></span><span class="notificationcopy"><span><b>${esc(item.title||'Fleet update')}</b>${item.unread?'<i aria-label="unread"></i>':''}</span>
    <small>${esc(item.summary||'')}</small><em>${esc(meta)}</em></span><span class="notificationchev">›</span></button>`;
}
function deliveryProblemRow(item){
  const pending=['queued','sending','retrying'].includes(item.status);
  const label=item.status==='subscription_expired'?'Reconnect '+(item.display_name||'device'):
    pending?'Delivery retry pending':'Delivery failed';
  return`<article class="notificationrow problems deliveryproblem"><span class="notificationspine"></span><span class="notificationcopy"><span><b>${esc(label)}</b></span>
    <small>${item.status==='subscription_expired'?'The browser subscription expired. Reconnect before retrying.':pending?'A retry is queued; the problem clears after a confirmed delivery.':`Attempt ${item.attempt||0}${item.remote_status?` · HTTP ${item.remote_status}`:''}`}</small>
    <em>${esc([item.platform,notificationTime(item)].filter(Boolean).join(' · '))}</em></span><span class="deliveryactions">${item.can_retry?`<button onclick="retryNotificationDelivery(decodeURIComponent('${enc(item.id)}'))">Retry</button>`:pending?'<span>Waiting</span>':`<button onclick="navigateTo('settings')">Reconnect</button>`}</span></article>`;
}
function notificationDetailHtml(item){
  if(notificationDetailLoading)return'<div class="notificationdetailstate"><span class="delivery sending">◌</span> Checking current state…</div>';
  if(notificationDetailError)return`<div class="notificationdetailstate bad">${esc(notificationDetailError)}<button onclick="loadNotificationDetail(decodeURIComponent('${enc(notificationDetailId)}'))">Retry</button></div>`;
  if(!item)return'<div class="notificationdetailstate"><b>Select an event</b><span>Open a row to review its current state and safe actions.</span></div>';
  const active=item.state==='active',snoozed=item.state==='snoozed',resolved=!active&&!snoozed;
  const source=(item.payload||{}).link_kind,sourceId=(item.payload||{}).link_id||item.session_id;
  const id=enc(item.id),revision=enc(item.source_revision),sourceKind=enc(source||'');
  const currentSession=((last||{}).sessions||[]).find(value=>value.session_id===item.session_id);
  const currentRequest=active&&['question','approval','form'].includes(item.kind)&&currentSession?.pending&&
    String(currentSession.pending.nonce||'')===String(item.source_revision||'')?
    `<section class="notificationrequest"><h3>Respond here</h3>${pendingBox(currentSession,'nmsg')}</section>`:'';
  return`<div class="notificationdetailhead"><button aria-label="close notification detail" onclick="closeNotificationDetail()">×</button><span class="notificationkind">${esc(notificationKinds[item.kind]||item.kind)}</span>
    <h2>${esc(item.title||'Fleet update')}</h2><p>${esc(item.summary||'')}</p></div>
    <dl class="notificationfacts"><div><dt>State</dt><dd>${esc(item.state)}</dd></div><div><dt>Opened</dt><dd>${esc(notificationTime({...item,changed_at:item.opened_at}))}</dd></div>
    ${item.provider?`<div><dt>Provider</dt><dd>${esc(item.provider)}</dd></div>`:''}${item.session_id?`<div><dt>Session</dt><dd>${esc(item.session_id)}</dd></div>`:''}</dl>
    ${currentRequest}${notificationActionState.message?`<div class="notificationactionmsg ${notificationActionState.error?'bad':''}" role="status">${notificationActionState.busy?'<span class="delivery sending">◌</span> ':''}${esc(notificationActionState.message)}</div>`:''}
    <div class="notificationactions">
      ${active?`<button onclick="snoozeNotification(decodeURIComponent('${id}'),decodeURIComponent('${revision}'),'quarter')">Snooze 15m</button><button onclick="snoozeNotification(decodeURIComponent('${id}'),decodeURIComponent('${revision}'),'hour')">1 hour</button><button onclick="snoozeNotification(decodeURIComponent('${id}'),decodeURIComponent('${revision}'),'tomorrow')">Tomorrow</button>`:''}
      ${snoozed?`<button class="primary" onclick="wakeNotification(decodeURIComponent('${id}'),decodeURIComponent('${revision}'))">Wake now</button>`:''}
      ${item.session_id&&(!resolved||item.muted)?`<button onclick="muteNotification(decodeURIComponent('${id}'),decodeURIComponent('${revision}'),${item.muted?'false':'true'})">${item.muted?'Unmute session':'Mute session'}</button>`:''}
      ${item.session_id&&active?`<button class="primary" onclick="openSession(decodeURIComponent('${enc(item.session_id)}'))">${['question','approval','form','reply'].includes(item.kind)?'Open request':'Open session'}</button>`:''}
      ${source?`<button onclick="openBriefingSource(decodeURIComponent('${sourceKind}'),decodeURIComponent('${enc(sourceId||'')}'))">Open source</button>`:''}
    </div>${resolved?'<div class="notificationreadonly">This event is resolved. Its history is read-only.</div>':''}`;
}
function renderNotifications(){
  const list=$('#notifications'),detail=$('#notificationdetail'),status=$('#notificationstatus');if(!list||!detail||!status)return;
  const counts=notificationCounts();document.querySelectorAll('[data-notification-section]').forEach(button=>{
    const section=button.dataset.notificationSection;button.classList.toggle('active',section===notificationSection);
    button.textContent=button.textContent.split(' · ')[0]+(section==='briefing'?'':` · ${counts[section]||0}`);
  });
  if(notificationSection==='history'){
    const workstreams=[...new Set(notificationItems.map(item=>item.workstream_id).filter(Boolean))].sort();
    const sessions=[...new Set(notificationItems.map(item=>item.session_id).filter(Boolean))].sort();
    status.innerHTML=`<label class="notificationsearch"><span>⌕</span><input data-draft-key="filter:notification-history" value="${esc(notificationHistoryQuery)}" placeholder="Filter history" oninput="notificationHistoryQuery=this.value;renderNotifications()"></label>
      <select aria-label="notification provider" onchange="notificationHistoryProvider=this.value;renderNotifications()"><option value="">All providers</option>${['claude','codex'].map(value=>`<option value="${value}" ${notificationHistoryProvider===value?'selected':''}>${value}</option>`).join('')}</select>
      <select aria-label="notification kind" onchange="notificationHistoryKind=this.value;renderNotifications()"><option value="">All kinds</option>${Object.entries(notificationKinds).map(([value,label])=>`<option value="${value}" ${notificationHistoryKind===value?'selected':''}>${esc(label)}</option>`).join('')}</select>
      <select aria-label="notification workstream" onchange="notificationHistoryWorkstream=this.value;renderNotifications()"><option value="">All workstreams</option>${workstreams.map(value=>`<option value="${esc(value)}" ${notificationHistoryWorkstream===value?'selected':''}>${esc(value)}</option>`).join('')}</select>
      <select aria-label="notification session" onchange="notificationHistorySession=this.value;renderNotifications()"><option value="">All sessions</option>${sessions.map(value=>`<option value="${esc(value)}" ${notificationHistorySession===value?'selected':''}>${esc(value)}</option>`).join('')}</select>
      <select aria-label="notification age" onchange="notificationHistoryAge=this.value;renderNotifications()"><option value="">Any time</option><option value="86400" ${notificationHistoryAge==='86400'?'selected':''}>Last 24 hours</option><option value="604800" ${notificationHistoryAge==='604800'?'selected':''}>Last 7 days</option><option value="2592000" ${notificationHistoryAge==='2592000'?'selected':''}>Last 30 days</option></select>
      ${notificationError?`<button onclick="loadNotifications(true,true)">${esc(notificationError)} · Retry</button>`:''}`;
  }else status.innerHTML=`<span>${notificationData.active||0} active</span><span>${notificationData.unread||0} unread on this device</span>${notificationLoading?'<span><i class="delivery sending">◌</i> Refreshing</span>':''}${notificationError?`<button onclick="loadNotifications(true,true)">${esc(notificationError)} · Retry</button>`:''}`;
  if(notificationLoading&&!notificationItems.length&&notificationSection!=='briefing')
    list.innerHTML='<div class="notificationdetailstate"><span class="delivery sending">◌</span> Loading notifications…</div>';
  else if(notificationSection==='briefing')list.innerHTML=briefingPanelHtml(true);
  else if(notificationSection==='problems'){
    const events=notificationItems.filter(item=>notificationBucket(item)==='problems');
    list.innerHTML=events.map(notificationRow).join('')+(notificationData.delivery_problems||[]).map(deliveryProblemRow).join('')||'<div class="destinationempty"><span>✓</span><b>No current problems</b><p>Provider and delivery failures will stay visible here until recovered.</p></div>';
  }else{
    let items=notificationItems.filter(item=>notificationBucket(item)===notificationSection);
    if(notificationSection==='history'){
      const query=notificationHistoryQuery.trim().toLowerCase(),age=Number(notificationHistoryAge)||0;
      items=items.filter(item=>(!notificationHistoryProvider||item.provider===notificationHistoryProvider)&&
        (!notificationHistoryKind||item.kind===notificationHistoryKind)&&
        (!notificationHistoryWorkstream||item.workstream_id===notificationHistoryWorkstream)&&
        (!notificationHistorySession||item.session_id===notificationHistorySession)&&
        (!age||Date.now()/1000-Number(item.changed_at||item.opened_at||0)<=age)&&
        (!query||[item.title,item.summary,item.provider,item.kind,item.session_id,item.workstream_id].filter(Boolean).join(' ').toLowerCase().includes(query)));
    }
    const empty={needs:['⌁','Nothing needs action','Questions, approvals, reply requests, and slow work land here.'],updates:['✓','No unread updates','Completions and outcomes move here until marked read.'],snoozed:['◷','Nothing snoozed','Snoozed work stays visible and can be woken early.'],history:['↺','No matching history','Resolved and read events remain available here.']}[notificationSection];
    list.innerHTML=items.map(notificationRow).join('')||`<div class="destinationempty"><span>${empty[0]}</span><b>${empty[1]}</b><p>${empty[2]}</p></div>`;
  }
  detail.innerHTML=notificationDetailHtml(notificationDetail);
  detail.classList.toggle('open',Boolean(notificationDetailId));
  const more=$('#notificationmore');if(more)more.hidden=!notificationData.next_cursor||notificationSection==='briefing';
  updateNotificationBadges();
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
const CLAUDE_PERMISSION_LABELS={default:'Manual',acceptEdits:'Accept edits',plan:'Plan',auto:'Auto',
  dontAsk:"Don't ask",bypassPermissions:'Bypass permissions'};
const claudePermissionLabel=mode=>CLAUDE_PERMISSION_LABELS[mode]||'Detecting…';
const providerModeActions=new Map();
function claudePermissionLocked(s){return !s?.capabilities?.change_permission_mode||providerModeActions.has(s?.session_id);}
function claudePermissionSelect(s,pre='msg'){
  if(!s||s.provider!=='claude')return'';
  const current=s.permission_mode||'';const available=new Set(s.permission_modes||[]);
  const locked=claudePermissionLocked(s);
  const option=(mode,label,extra='')=>`<option value="${mode}" ${current===mode?'selected':''}
    ${!available.has(mode)||locked?'disabled':''}>${label}${extra}</option>`;
  return`<select class="modesel permissionselect" title="Claude permission mode"
    onclick="event.stopPropagation()" onchange="setClaudePermissionMode('${s.session_id}',this.value,'${pre}')"
    ${locked?'disabled':''}>
    ${current?'':`<option selected disabled>Detecting…</option>`}
    ${option('default','Manual')}${option('auto','Auto',available.has('auto')?'':' — unavailable')}
    ${option('acceptEdits','Accept edits')}${option('plan','Plan')}
    <optgroup label="Advanced"><option value="dontAsk" disabled>Don't ask — new sessions only</option>
      ${available.has('bypassPermissions')?option('bypassPermissions','Bypass permissions'):''}</optgroup>
    ${current==='dontAsk'?`<option value="dontAsk" selected disabled>Don't ask — startup mode</option>`:''}
    </select>`;
}
function modeSelect(s,pre='msg'){
  if(!s||s.provider!=='codex')return'';
  const mode=s.collaboration_mode||'default';
  const locked=!s.capabilities?.submit||['running','needs_you','stalled'].includes(s.state)||providerModeActions.has(s.session_id);
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
const nativeRequestLocks=new Set();
const nativeRequestKey=(sid,nonce)=>String(sid)+'\0'+String(nonce||'');
function nativeRequestLocked(sid,nonce){return nativeRequestLocks.has(nativeRequestKey(sid,nonce));}
async function withNativeRequestLock(sid,nonce,work){
  const key=nativeRequestKey(sid,nonce);if(nativeRequestLocks.has(key))return{ok:false,duplicate:true};
  nativeRequestLocks.add(key);uiRefresh();
  try{return await work();}
  finally{nativeRequestLocks.delete(key);uiRefresh();}
}
const normalizedMessage=text=>String(text||'').trim().replace(/\s+/g,' ');
function optimisticBucket(sid){
  if(!optimisticMessages.has(sid))optimisticMessages.set(sid,[]);
  return optimisticMessages.get(sid);
}
function optimisticList(sid){
  const list=optimisticBucket(sid);
  for(const queued of offlineMessages){
    if(queued.sid!==sid||list.some(item=>item.queueId===queued.id))continue;
    list.push({id:++optimisticSequence,queueId:queued.id,sid,text:queued.text,kind:'text',
      imageIds:[...(queued.imageIds||[])],imageCount:(queued.imageIds||[]).length,
      status:'queued',baseCount:queued.baseCount,created:queued.created});
  }
  return list;
}
function canonicalCount(messages,item){
  if(item.kind==='answer')return(messages||[]).filter(message=>
    message.role==='event'&&message.kind==='qa').length;
  const wanted=normalizedMessage(item.text);
  return(messages||[]).filter(message=>message.role==='user'&&
    normalizedMessage(message.text)===wanted).length;
}
function armOptimisticTimeout(item){
  clearTimeout(item.confirmTimer);
  item.confirmTimer=setTimeout(()=>{
    const current=optimisticList(item.sid).find(entry=>entry.id===item.id);
    if(current&&current.status==='sending'){
      current.status='failed';current.error='Not confirmed after 15 seconds';uiRefresh();
    }
  },15000);
}
function addOptimistic(sid,text,kind='text',status='sending',queueId=null,baseCount=null,imageIds=[]){
  const feedbackStarted=performance.now();
  const messages=(ctxCache[sid]&&ctxCache[sid].messages)||[];
  const item={id:++optimisticSequence,queueId,sid,text:String(text||''),kind,status,
    imageIds:[...(imageIds||[])],imageCount:(imageIds||[]).length,
    baseCount:baseCount==null?canonicalCount(messages,{kind,text}):baseCount,created:Date.now()};
  optimisticBucket(sid).push(item);
  if(status==='sending')armOptimisticTimeout(item);
  const openConvo=sessionView?.sid===sid&&!sessionView.closed&&$('#sbody .aconvo');
  if(openConvo){
    openConvo.insertAdjacentHTML('beforeend',optimisticItemHtml(item));
    $('#sbody').scrollTop=$('#sbody').scrollHeight;
    recordInputFeedback(feedbackStarted,'send');
    requestAnimationFrame(()=>render(last,true));
  }else{
    uiRefresh();
    recordInputFeedback(feedbackStarted,'send');
  }
  return item.id;
}
function composerKey(event,send){
  const mac=/Mac|iPhone|iPad|iPod/.test(navigator.platform||'');
  if(event.key!=='Enter'||(mac?!event.metaKey:!event.ctrlKey))return;
  event.preventDefault();
  send();
}
function resizeComposer(input){
  if(!input)return;
  input.style.height='auto';
  const limit=matchMedia('(pointer:coarse)').matches?112:140;
  const height=Math.min(Math.max(input.scrollHeight,42),limit);
  input.style.height=height+'px';
  input.style.overflowY=input.scrollHeight>limit?'auto':'hidden';
}
function composerInput(input,sid,pre){
  resizeComposer(input);
  slashInput(sid,pre);
}
function composerFocus(input,sid,pre){
  input?.closest('#sact')?.classList.add('composer-active');
  resizeComposer(input);
  slashInput(sid,pre);
  requestAnimationFrame(syncVisualViewport);
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
    if(item.status==='queued')return true;
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
  if(item?.imageIds?.length)restoreImageDraftIds(sid,item.imageIds);
  uiRefresh();
  requestAnimationFrame(()=>{
    const input=document.getElementById('sft-'+sid)||document.getElementById('vft-'+sid)||
      document.getElementById('ft-'+sid);
    if(input){input.value=item?.text||'';setDraft(composerDraftKey(sid),input.value);input.focus();}
  });
}
function optimisticItemHtml(item){
  return`<div class="cmsg user optimistic" data-optimistic-id="${item.id}">
    <span class="crole">you</span>${item.status==='queued'?`<span class="delivery queued" aria-label="queued offline" title="queued until Fleet reconnects">↥</span>`:
      item.status==='sending'?`<span class="delivery sending" aria-label="sending">◌</span>`:
      item.status==='failed'?`<button class="delivery failed" title="${esc(item.error||'Send failed')} — restore" aria-label="send failed; restore message" onclick="restoreOptimistic('${item.sid}',${item.id})">!</button>`:''}
    <div class="cbody">${item.imageCount?`<div class="image-receipt">🖼 ${item.imageCount} image${item.imageCount===1?'':'s'}</div>`:''}<p>${esc(item.text).replace(/\n/g,'<br>')}</p></div></div>`;
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
  quickResponses.set(sid,item);uiRefresh();recordInputFeedback(feedbackStarted,'quick_response');return item.id;
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
  const queuedMessages=visibleOptimistic(s.session_id,messages).filter(item=>item.queueId);
  const queued=queuedMessages.length?queuedMessages[queuedMessages.length-1]:null;
  const action=quickResponses.get(s.session_id)||null;
  const item=[answer,queued,action].filter(Boolean).sort((a,b)=>a.created-b.created).at(-1);
  if(!item)return'';
  const status=item.status==='confirmed'||item.status==='sent'?'sent':item.status;
  const verb=status==='queued'?'Queued offline':status==='sending'?(item.kind==='text'?'Sending':'Submitting'):
    status==='failed'?'Failed':'Submitted';
  const icon=status==='queued'?`<span class="delivery queued" aria-label="queued offline">↥</span>`:
    status==='sending'?`<span class="delivery sending" aria-label="sending quick response">◌</span>`:
    status==='failed'?(item.kind==='answer'||item.kind==='text'
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
  const canClaudeMode=lifecycle&&s&&s.provider==='claude'&&!s.provisional;
  const mode=s?.collaboration_mode||'default';
  const modeLocked=!s?.capabilities?.submit||['running','needs_you','stalled'].includes(s?.state)||providerModeActions.has(s?.session_id);
  const permissionModes=new Set(s?.permission_modes||[]);
  const permissionLocked=claudePermissionLocked(s);
  const permissionButton=(value,label,shown=true)=>shown?`<button aria-pressed="${s?.permission_mode===value}"
    ${permissionLocked||!permissionModes.has(value)?'disabled':''}
    onclick="closeOverflow();setClaudePermissionMode('${s?.session_id||''}','${value}','${kind==='viewer'?'vmsg':'smsg'}')">${label}</button>`:'';
  const canStop=kind==='subagent'
    ? Boolean(!done&&s?.capabilities?.interrupt)
    : Boolean(s?.capabilities?.interrupt);
  const canClose=Boolean(lifecycle&&s?.capabilities?.close);
  const canHandoff=Boolean(!s?.staging_observer&&s?.session_id&&['session','viewer','closed'].includes(kind));
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
      ${canClaudeMode?`<span class="ovgroup"><span class="ovlabel">Claude permissions · ${esc(claudePermissionLabel(s.permission_mode))}</span>
        <span class="ovseg permissionseg">${permissionButton('default','Manual')}${permissionButton('auto','Auto')}
          ${permissionButton('acceptEdits','Accept edits')}${permissionButton('plan','Plan')}</span>
        <span class="ovlabel">Advanced</span><span class="ovseg permissionseg">
          <button disabled title="Claude Code exposes this only at startup">Don't ask · new session</button>
          ${permissionButton('bypassPermissions','Bypass permissions',permissionModes.has('bypassPermissions'))}</span>
        ${permissionLocked?`<small class="ovhint">${s?.permission_mode?"Available only while Claude is idle":"Waiting for Claude to report its mode"}</small>`:''}
      </span><span class="ovsep"></span>`:''}
      <button class="ovitem" role="menuitem" onclick="closeOverflow();toggleTheme()">
        <span>Appearance</span><small>light / dark</small></button>
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
const handoffDraftPrefix=(sid,target)=>`handoff:${sid}:${target}:`;
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
  setDraft(handoffDraftPrefix(handoffView.sid,handoffView.target)+'message',handoffView.draft);
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
      <textarea id="handoffpreview" data-draft-key="${esc(handoffDraftPrefix(handoffView.sid,provider)+'message')}" maxlength="30000" oninput="handoffView.draft=this.value;this.previousElementSibling.querySelector('small').textContent=this.value.length.toLocaleString()+' / 30,000'">${esc(handoffView.draft)}</textarea>
    </section>
    <aside class="handoffoptions"><h3>New coding session</h3>
      <label class="nflab">provider</label><select class="nfsel" onchange="changeHandoffProvider(this.value)">
        <option value="claude" ${provider==='claude'?'selected':''}>Claude Code</option>
        <option value="codex" ${provider==='codex'?'selected':''}>Codex CLI</option></select>
      <label class="nflab">directory</label><input class="nfin" data-draft-key="${esc(handoffDraftPrefix(handoffView.sid,provider)+'cwd')}" value="${esc(defaults.cwd||'')}"
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
        ${defaults.worktree?`<label class="nflab">worktree name</label><input class="nfin" data-draft-key="${esc(handoffDraftPrefix(handoffView.sid,provider)+'worktree')}" maxlength="40" value="${esc(defaults.worktree_name||'')}"
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
    const prefix=handoffDraftPrefix(view.sid,view.target);
    view.data=d;view.draft=draftValue(prefix+'message',d.preview||'');view.defaults={...(d.defaults||{})};
    view.defaults.cwd=draftValue(prefix+'cwd',view.defaults.cwd||'');
    view.defaults.worktree_name=draftValue(prefix+'worktree',view.defaults.worktree_name||'');
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
    clearDraftPrefix(handoffDraftPrefix(view.sid,view.target));
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

const terminalActions=new Map();
function terminalButton(s,card=false){
  if(!s)return'';
  const cls=`expandbtn termbtn${card?' deskonly':''}`;
  if(s.capabilities?.focus_terminal){
    const attach=s.capabilities?.focus_terminal_mode==='attach';
    const title=attach?'open a Codex TUI attached to this shared runtime':"bring this session's terminal tab to the front";
    const action=terminalActions.get(s.session_id)||{};
    return`<button class="${cls}" title="${esc(title)}"
      ${action.busy?'disabled':''} onclick="event.stopPropagation();focusSession('${s.session_id}',this)">${action.busy?'Opening…':action.ok?'Opened ✓':attach?'Attach':'Terminal'}</button>`;
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
  const otherKey=questionDraftPrefix(sid,p.nonce)+'other:0';
  if(otherDraft[sid]==null)otherDraft[sid]=q.secret?'':draftValue(otherKey);
  const sel=multiSel[sid]=multiSel[sid]||new Set();
  const locked=nativeRequestLocked(sid,p.nonce);
  return`<div class="ptool"><span class="ptlabel">${esc(q.header||'question')} — waiting on you</span>
      <button class="xbtn" ${locked?'disabled':''} title="${p.dismiss_action==='cancel_turn'?'dismiss by stopping this Codex turn':'dismiss — chat about this instead'}" onclick="sendDismiss('${sid}','${p.nonce}','${pre}')">✕</button></div>
    ${p.files&&p.files.length?`<div class="pfiles"><span class="plabel">read first</span>${p.files.map(f=>fchip(sid,f,f.caption)).join('')}</div>`:''}
    <div class="qtext">${esc(q.question)}</div>
    ${(q.options||[]).map((o,i)=>`<button class="optbtn ${ms&&sel.has(i+1)?'sel':''}" ${locked?'disabled':''}
        onclick="${ms?`toggleOpt('${sid}',${i+1})`:`sendOption('${sid}','${p.nonce}',[${i+1}],'${pre}')`}">
        ${esc(o.label)}${o.description?`<small>${esc(o.description)}</small>`:''}</button>`).join('')}
    ${q.allowOther!==false?`<div class="freetext"><input id="oth-${pre}-${sid}" ${q.secret?'':`data-draft-key="${esc(otherKey)}"`} ${locked?'disabled':''} placeholder="Other — type your own answer" ${q.secret?'type="password"':''}
      value="${esc(otherDraft[sid]||'')}" oninput="otherDraft['${sid}']=this.value"
      ${ms?'':`onkeydown="if(event.key==='Enter')sendOther('${sid}','${p.nonce}',${n},'${pre}')"`}>
      ${ms?'':`<button class="pbtn send" ${locked?'disabled':''} onclick="sendOther('${sid}','${p.nonce}',${n},'${pre}')">answer</button>`}</div>`:''}
    ${ms?`<div class="pbtns"><button class="pbtn send" ${locked?'disabled':''} onclick="sendMulti('${sid}','${p.nonce}',${n},'${pre}')">submit selection</button></div>`:''}`;
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
  if(ae&&['INPUT','TEXTAREA'].includes(ae.tagName)&&bar.contains(ae))return; // don't clobber typing
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
    ${s&&s.capabilities?.submit?`<div class="freetext composer"><textarea id="vft-${viewerSid}" data-draft-key="${esc(composerDraftKey(viewerSid))}" rows="2" placeholder="send message" autocomplete="off"
      oninput="composerInput(this,'${viewerSid}','vft')" onfocus="resizeComposer(this);slashInput('${viewerSid}','vft')"
      onkeydown="composerKey(event,()=>sendText('${viewerSid}','vft','vmsg'));if(event.key==='Escape')slashClose()">${esc(draftValue(composerDraftKey(viewerSid)))}</textarea>
      <span class="sendpair"><button class="pbtn send" onclick="sendText('${viewerSid}','vft','vmsg')">send</button>${scheduleButton(viewerSid,'vft-'+viewerSid)}</span></div>`:''}
    <div class="slashwrap" id="slash-vft-${viewerSid}"></div>
    <div class="actmsg" id="vmsg-${viewerSid}"></div>`;
  keepStripScroll(bar,()=>{bar.innerHTML=h;});
  const nw=bar.querySelector&&bar.querySelector('.vconvo');
  if(nw)nw.scrollTop=(oldScroll&&!oldScroll.atBottom)?oldScroll.top:nw.scrollHeight;
}
// Full chat headers identify the conversation. Operational metadata lives in
// the status strip above the composer, where it can update independently.
function sessTitleBlock(s){
  if(!s)return '<b>session</b>';
  return `<b>${esc(s.title||s.project||'session')}</b>`;
}
function viewFile(sid,ep,en,kind,ecap){
  closeSession();          // the two full-screen surfaces are mutually exclusive
  const path=decodeURIComponent(ep),name=decodeURIComponent(en),cap=decodeURIComponent(ecap||'');
  const url='/api/file?sid='+encodeURIComponent(sid)+'&p='+encodeURIComponent(path);
  // The file viewer is a reading surface: filename and file actions only.
  $('#vtitle').innerHTML=`<span class="vfname">${kind==='image'?'🖼':'📄'} ${esc(name)}${cap?` — ${esc(cap)}`:''}</span>`;
  $('#viewer').style.display='flex';
  viewerSid=sid;viewerPath=path;syncOverlayHistory();
  const vb=$('#vbody');
  requestAnimationFrame(()=>{if(viewerSid===sid&&viewerPath===path)renderViewerBar(true);});
  if(kind==='image'){vb.innerHTML=`<div class="ctxload">loading image…</div><img hidden src="${url}" alt="${esc(name)}"
    onload="this.hidden=false;this.previousElementSibling?.remove()"
    onerror="this.previousElementSibling.textContent='✗ image unavailable';this.remove()">`;return;}
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
let histPushed=false,schedulePushed=false,settingsPushed=false;
const anyOverlay=()=>['#viewer','#sview','#aview','#settingsview','#searchview','#handoffview','#outboxview','#scheduleview'].some(id=>$(id).style.display==='flex');
function syncOverlayHistory(){
  if(anyOverlay()&&!histPushed){histPushed=true;history.pushState({fdOverlay:1},'');}
}
window.addEventListener('popstate',()=>{
  if(settingsPushed){settingsPushed=false;closeSettings();return;}
  if(schedulePushed){schedulePushed=false;closeSchedule();return;}
  if(handoffPushed){
    handoffPushed=false;
    const destination=handoffOpenAfterBack;handoffOpenAfterBack=null;
    closeHandoff();
    if(destination)primarySessionAction(destination);
    return;
  }
  if(histPushed){
    histPushed=false;
    closeConfirm();closeHandoff();closeViewer();closeAgent();closeSession();closeSettings();closeSearchView();closeOutbox();closeSchedule();
    return;
  }
  const destination=hashDestination();notificationDetailId=destination.detail;
  if(!notificationDetailId){notificationDetail=null;notificationDetailError='';}
  navigateTo(destination.route,false,Boolean(notificationDetailId));
});
function dismissOverlay(){
  if(usageOpen)return closeUsage();
  if(overflowOpen)return closeOverflow();
  if(document.querySelector('.composerplus.open'))return closeComposerMenus();
  if($('#confirm').style.display==='flex')return closeConfirm();   // ask first
  if(settingsPushed)return history.back();
  if(handoffPushed)return history.back();
  if(schedulePushed)return history.back();
  if(histPushed)history.back();          // → popstate does the actual close
  else{closeHandoff();closeViewer();closeAgent();closeSession();closeSettings();closeSearchView();closeOutbox();closeSchedule();}
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')dismissOverlay();});
document.addEventListener('click',e=>{
  if(usageOpen&&!e.target.closest('#usagepanel')&&!e.target.closest('#usagechip'))closeUsage();
  if(overflowOpen&&!e.target.closest('.ovwrap'))closeOverflow();
  if(!e.target.closest('.composertools'))closeComposerMenus();
  if($('#mobilemore').classList.contains('open')&&!e.target.closest('#mobilemore')&&!e.target.closest('[data-route="more"]'))closeMobileMore();
});

// iOS resizes the visual viewport independently from the fixed layout viewport.
// Keep full-screen reading surfaces fitted to the pixels above the keyboard so
// the dashboard beneath them can never peek through.
let visualViewportBaseline=Math.max(1,Math.round(
  window.visualViewport?.height||window.innerHeight||document.documentElement.clientHeight||1));
function syncVisualViewport(){
  const viewport=window.visualViewport;
  const height=Math.max(1,Math.round(viewport?.height||window.innerHeight));
  const top=Math.max(0,Math.round(viewport?.offsetTop||0));
  const layoutHeight=Math.max(height,window.innerHeight||0,document.documentElement.clientHeight||0);
  const focused=document.activeElement;
  const editingOverlay=focused&&['INPUT','TEXTAREA','SELECT'].includes(focused.tagName)&&
    Boolean(focused.closest('#sview,#aview,#viewer'));
  if(!editingOverlay)visualViewportBaseline=Math.max(height,layoutHeight);
  const keyboardOpen=editingOverlay&&Math.max(
    layoutHeight-height-top,visualViewportBaseline-height-top)>80;
  document.documentElement.style.setProperty('--fleet-visual-height',height+'px');
  document.documentElement.style.setProperty('--fleet-visual-top',top+'px');
  document.documentElement.classList.toggle('keyboard-open',keyboardOpen);
}
window.visualViewport?.addEventListener('resize',syncVisualViewport);
window.visualViewport?.addEventListener('scroll',syncVisualViewport);
window.addEventListener('orientationchange',()=>requestAnimationFrame(syncVisualViewport));
document.addEventListener('focusout',event=>{
  if(!event.target.closest?.('#sact .composer'))return;
  setTimeout(()=>{
    if(!document.activeElement?.closest?.('#sact .composer'))$('#sact').classList.remove('composer-active');
    syncVisualViewport();
  },0);
},true);
let chatTouch=null;
document.addEventListener('touchstart',event=>{
  const input=document.activeElement;
  if(!event.target.closest?.('#sbody')||!input?.matches?.('#sact .composer textarea'))return chatTouch=null;
  const touch=event.touches?.[0];
  chatTouch=touch?{x:touch.clientX,y:touch.clientY,input}:null;
},{passive:true,capture:true});
document.addEventListener('touchmove',event=>{
  if(!chatTouch)return;
  const touch=event.touches?.[0];if(!touch)return;
  const dx=Math.abs(touch.clientX-chatTouch.x),dy=Math.abs(touch.clientY-chatTouch.y);
  if(dy<10||dy<=dx)return;
  chatTouch.input.blur();closeComposerMenus();chatTouch=null;
  requestAnimationFrame(syncVisualViewport);
},{passive:true,capture:true});
document.addEventListener('touchend',()=>{chatTouch=null;},{passive:true,capture:true});
syncVisualViewport();

// ---- full-screen session view ----------------------------------------------
// Same overlay shape as the subagent view, but this one is a real terminal
// channel: send box, question block, interrupt/mute. Ids use the `sft-`/`smsg-`
// prefixes — the card's `ft-`/`msg-` elements coexist in the DOM.
const statusExpanded=new Set(),statusCostsOpen=new Set();
function statusGraphPoint(value){
  const n=Number(value)||0;
  if(n>20000)return['█','hot'];if(n>15000)return['▇','warm'];if(n>10000)return['▆','warm'];
  if(n>7500)return['▅','warn'];if(n>5000)return['▄','warn'];if(n>2500)return['▃','cool'];
  if(n>1000)return['▂','cool'];return['▁','cool'];
}
function statusLineHtml(status,key){
  if(!status||typeof status!=='object')return'';
  const id=String(key||'status'),expanded=statusExpanded.has(id),costOpen=statusCostsOpen.has(id);
  const branch=status.branch&&status.branch!=='HEAD'?String(status.branch):'';
  const git=[];
  if(branch){
    let label='⎇ '+branch;
    if(Number.isFinite(status.ahead)&&status.ahead>0)label+=` ↑${status.ahead}`;
    if(Number.isFinite(status.behind)&&status.behind>0)label+=` ↓${status.behind}`;
    git.push(`<span>${esc(label)}</span>`);
  }
  if(status.worktree_label)git.push(`<span title="${esc(status.worktree||'')}">${esc(status.worktree_label)}</span>`);
  const model=[];
  if(status.model)model.push(`<span>${esc(status.model)}${status.effort?` · ${esc(status.effort)}`:''}</span>`);
  const context=[];
  if(Number.isFinite(status.context_pct))context.push(`Ctx: ${status.context_pct}%`);
  if(Number.isFinite(status.compact_remaining))context.push(`→${fmtTok(status.compact_remaining)}`);
  if(context.length)model.push(`<span>${esc(context.join('  '))}</span>`);
  const cache=[];
  if(Number.isFinite(status.cache_read_pct)){
    const tier=status.cache_read_pct>=90?'good':status.cache_read_pct>=75?'warn':status.cache_read_pct>=50?'warm':'hot';
    cache.push(`<span class="${tier}">♻ ${status.cache_read_pct}%</span>`);
  }
  if(Number.isFinite(status.cache_write)){
    let cw=`✎ ${fmtTok(status.cache_write)}`;
    if(Number(status.cache_write_spikes)>0)cw+=` · spikes ${status.cache_write_spikes}`;
    if(Number(status.cache_write_peak)>0)cw+=` · peak ${fmtTok(status.cache_write_peak)}`;
    cache.push(`<span>${esc(cw)}</span>`);
  }
  const breakdown=Array.isArray(status.cost_breakdown)?status.cost_breakdown:[];
  let cost='';
  if(Number.isFinite(status.tree_cost)){
    const prefix=status.cost_scope==='estimated'?'~':'';
    const label=status.cost_label==='agent'?'agent':'tree';
    const delta=Number.isFinite(status.turn_cost)?` · +${fmt$(status.turn_cost)}`:'';
    const summary=`${label} ${prefix}${fmt$(status.tree_cost)}${delta}`;
    cost=breakdown.length>1?`<details class="status-cost" ${costOpen?'open':''}
      ontoggle="statusCostToggle('${enc(id)}',this.open)"><summary>${esc(summary)}</summary>
      <div class="status-cost-breakdown">${breakdown.map(item=>`<span>${esc(item.label||item.kind||'Usage')}<b>${esc(prefix+fmt$(item.cost))}</b></span>`).join('')}
      ${status.cost_breakdown_omitted?`<small>+${status.cost_breakdown_omitted} more</small>`:''}</div></details>`:
      `<span class="status-tree-cost">${esc(summary)}</span>`;
    cache.push(cost);
  }
  const history=(status.cache_write_history||[]).filter(Number.isFinite).slice(-50);
  const graph=history.map(value=>{const [glyph,tier]=statusGraphPoint(value);return`<i class="${tier}">${glyph}</i>`;}).join('');
  const primary=git.length?`<div class="status-primary">${git.join('<em>│</em>')}</div>`:'';
  const secondary=model.length?`<div class="status-secondary">${model.join('<em>│</em>')}</div>`:'';
  const details=(cache.length||graph)?`<div class="status-details">
    ${cache.length?`<div class="status-cache">${cache.join('<em>│</em>')}</div>`:''}
    ${graph?`<div class="status-graph" aria-label="Cache write history: ${esc(history.join(', '))}"><b>CW</b>${graph}</div>`:''}
  </div>`:'';
  if(!primary&&!secondary&&!details)return'';
  return`<section class="statusstrip${expanded?' expanded':''}${status.frozen?' frozen':''}" data-status-key="${esc(id)}">
    ${primary}${secondary}
    ${details?`<button class="status-expand" aria-label="${expanded?'collapse':'expand'} status details" aria-expanded="${expanded}"
      onclick="toggleStatusDetails('${enc(id)}')">${expanded?'hide usage details':'usage details'}</button>${details}`:''}
  </section>`;
}
function statusCostToggle(encodedKey,isOpen){
  const key=decodeURIComponent(encodedKey);if(isOpen)statusCostsOpen.add(key);else statusCostsOpen.delete(key);
}
function toggleStatusDetails(encodedKey){
  const key=decodeURIComponent(encodedKey);if(statusExpanded.has(key))statusExpanded.delete(key);else statusExpanded.add(key);
  if(key.startsWith('agent:'))renderAgent(true);else if(sessionView?.closed)renderClosed(true);else renderSession(true);
}
function refreshStatusStrip(hostSelector,status,key){
  const current=document.querySelector(hostSelector+' .statusstrip');
  if(current)current.outerHTML=statusLineHtml(status,key);
}
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
  if(spawnProvisional&&spawnProvisional.id===sid){openSession(sid);return;}
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
  const session=((last&&last.sessions)||[]).find(x=>x.session_id===sid)||
    (spawnProvisional&&spawnProvisional.id===sid?provisionalSessionObject():null);
  if(session?.new_response)markRead(session);
  sessionView={sid,closed:false};sessionOpened=true;sessionEvidenceOpen=false;
  $('#sview').style.display='flex';
  syncVisualViewport();
  $('#stitle2').innerHTML=sessTitleBlock(session);
  const body=$('#sbody');body.innerHTML='<div class="ctxload">loading conversation…</div>';
  $('#sactivity').innerHTML='';delete $('#sactivity').dataset.renderKey;
  delete body.dataset.renderKey;
  syncOverlayHistory();
  requestAnimationFrame(()=>{if(sessionView?.sid===sid&&!sessionView.closed)renderSession(true);});
}
// a closed session has no process: read its transcript, offer no controls
function openClosed(sid){
  closeViewer();
  sessionView={sid,closed:true};sessionOpened=true;sessionEvidenceOpen=false;
  $('#sview').style.display='flex';
  syncVisualViewport();
  const body=$('#sbody');body.innerHTML='<div class="ctxload">loading conversation…</div>';
  $('#sactivity').innerHTML='';delete $('#sactivity').dataset.renderKey;
  delete body.dataset.renderKey;
  syncOverlayHistory();
  requestAnimationFrame(()=>{if(sessionView?.sid===sid&&sessionView.closed)renderClosed(true);});
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
  closeComposerMenus();
  $('#sview').style.display='none';$('#sbody').innerHTML='';delete $('#sbody').dataset.renderKey;
  $('#sactivity').innerHTML='';delete $('#sactivity').dataset.renderKey;
  $('#sact').innerHTML='';$('#sact').classList.remove('session-composer','composer-active','tools-open');
  $('#sctrl').innerHTML='';$('#sevidence').innerHTML='';$('#sevidence').classList.remove('open');
}
function sessionActivityHtml(s){
  const mainWorking=['running','stalled'].includes(s.state);
  const agents=(s.agents||[]).filter(agent=>!['done','ended'].includes(agent.state));
  if(!mainWorking&&!agents.length)return'';
  const mainSlow=s.state==='stalled';
  const signals=[mainWorking?`<span class="worksignal"><i class="workpulse${mainSlow?' slow':''}" aria-hidden="true"></i>${mainSlow?'Main session is slow':'Main session working'}</span>`:'',
    agents.length?`<span class="worksignal"><i class="worksubicon" aria-hidden="true">⇶</i>${agents.length} active subagent${agents.length===1?'':'s'}</span>`:''].filter(Boolean);
  const rows=[];
  if(mainWorking)rows.push(`<span><b>Main session</b><small>${mainSlow?'Slow — may still be working':'Working'}</small></span>`);
  for(const agent of agents)rows.push(`<span><b>${esc(agent.description||agent.agent_type||agent.agent_id||'Subagent')}</b><small>${agent.state==='stalled'?'Slow — may still be working':'Working'}</small></span>`);
  return`<div class="workactivity" role="status" aria-live="polite"><details><summary>${signals.join('<em>│</em>')}</summary>
    <div class="workdetails">${rows.join('')}</div></details></div>`;
}
function renderSessionActivity(s){
  const host=$('#sactivity');if(!host)return;
  const html=sessionActivityHtml(s),key=[s.state,...(s.agents||[]).map(agent=>
    `${agent.agent_id||''}:${agent.state||''}:${agent.description||agent.agent_type||''}`)].join('|');
  if(host.dataset.renderKey===key&&Boolean(host.innerHTML)===Boolean(html))return;
  host.innerHTML=html;host.dataset.renderKey=key;
}
async function renderClosed(){
  if(!sessionView||!sessionView.closed)return;
  const sid=sessionView.sid;
  const body=$('#sbody');
  const meta=closedSession(sid)||{};
  $('#sctrl').innerHTML=evidenceButton(meta)+overflowMenu('session',meta,'closed');
  renderEvidenceRail(meta);
  const closedActions=status=>`<div class="relaynote">this session is <b>closed</b> — its terminal is gone,
      so there is nothing to send to. The conversation is read-only.</div>
      ${statusLineHtml(status,'closed:'+sid)}
      ${handoffLinksHtml(meta)}
      ${meta.can_reopen?`<div class="freetext"><button class="pbtn send"
        onclick="reopenClosed('${sid}',this)">reopen in terminal</button></div>
        <div class="actmsg" id="reopenmsg-${sid}"></div>`:''}`;
  $('#sact').classList.remove('session-composer','composer-active','tools-open');
  $('#sact').innerHTML=closedActions(meta.status_line);
  if(!closedCtx[sid]){
    closedCtx[sid]={fetching:true,messages:[],info:{}};
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
  $('#sact').innerHTML=closedActions(info.status_line||meta.status_line);
  $('#stitle2').innerHTML=`<b>${esc(meta.title||info.project||'closed session')}</b>`;
  if(c.fetching){body.innerHTML='<div class="ctxload">loading conversation…</div>';return;}
  const old={top:body.scrollTop,atBottom:body.scrollTop+body.clientHeight>=body.scrollHeight-12};
  const wantBottom=sessionOpened||old.atBottom;sessionOpened=false;
  const optimistic=visibleOptimistic(sid,c.messages||[]).map(item=>`${item.id}:${item.status}:${item.error||''}`).join('|');
  const bodyKey=`closed:${c.messages?.length??-1}:${c.next_cursor??''}:${c.olderError||''}:${c.error||''}:${optimistic}`;
  if(body.dataset.renderKey!==bodyKey){
    if(c.error)body.innerHTML=`<div class="ctxload">✗ ${esc(c.error)}</div>`;
    else if(!c.messages.length)body.innerHTML='<div class="ctxload">no conversation recorded</div>';
    else body.innerHTML=`<div class="aconvo">${convoMsgs(c,sid,true,'closed')}</div>`;
    body.dataset.renderKey=bodyKey;
    body.scrollTop=wantBottom?body.scrollHeight:old.top;
  }else if(wantBottom)body.scrollTop=body.scrollHeight;
  if(wantBottom)requestAnimationFrame(()=>{const b=$('#sbody');b.scrollTop=b.scrollHeight;});
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
  const s=(last.sessions||[]).find(x=>x.session_id===sessionView.sid)||
    (spawnProvisional&&spawnProvisional.id===sessionView.sid?provisionalSessionObject():null);
  if(!s){closeSession();return;}          // session died while open
  if(s.provisional)return renderProvisionalSession(s);
  renderSessionActivity(s);
  ensureCtx(s.session_id,ctxVersion(s));
  const c=ctxCache[s.session_id];
  const ae=document.activeElement;
  const typing=ae&&['INPUT','TEXTAREA'].includes(ae.tagName)&&$('#sview').contains(ae);
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
  const optimisticItems=visibleOptimistic(s.session_id,(c&&c.messages)||[]);
  const optimisticRevision=optimisticItems.map(item=>`${item.id}:${item.status}:${item.imageCount||0}:${item.error||''}`).join('|');
  const bodyKey=`session:${c?.v??'loading'}:${c?.messages?.length??-1}:${c?.next_cursor??''}:${c?.olderError||''}:${optimisticRevision}`;
  if(body.dataset.renderKey!==bodyKey){
    const optimisticOnly=optimisticItems.map(item=>optimisticItemHtml(item)).join('');
    if((!c||!c.messages)&&optimisticOnly)body.innerHTML=`<div class="aconvo">${optimisticOnly}</div>`;
    else if(!c||!c.messages)body.innerHTML='<div class="ctxload">loading conversation…</div>';
    else if(!c.messages.length&&optimisticOnly)body.innerHTML=`<div class="aconvo">${optimisticOnly}</div>`;
    else if(!c.messages.length)body.innerHTML='<div class="ctxload">no conversation yet</div>';
    else body.innerHTML=`<div class="aconvo">${convoMsgs(c,s.session_id)}</div>`;
    body.dataset.renderKey=bodyKey;
    body.scrollTop=wantBottom?body.scrollHeight:old.top;
  }else if(wantBottom)body.scrollTop=body.scrollHeight;
  if(typing){
    refreshStatusStrip('#sact',s.status_line,'session:'+s.session_id);
    return;                                // never replace the input being typed into
  }
  const p=s.pending;
  const hasQ=p&&p.kind==='question'&&p.questions&&p.questions.length&&answered[s.session_id]!==p.nonce;
  const qHtml=hasQ?`<div class="togbox waiting">
      <button class="vchat-toggle" onclick="sessQOpen=!sessQOpen;renderSession(true)">${sessQOpen?'▾ hide question':'▸ show question — waiting on you'}</button>
      ${sessQOpen?`<div class="togbody">${p.questions.length>1?mqBlock(s,p,'smsg'):singleQBlock(s,p,'smsg')}</div>`:''}
    </div>`
    :pendingBox(s,'smsg');   // permission prompts render whole
  const act=$('#sact');
  act.classList.remove('tools-open');
  act.classList.toggle('session-composer',Boolean(s.capabilities?.submit));
  if(!s.capabilities?.submit)act.classList.remove('composer-active');
  keepStripScroll(act,()=>{act.innerHTML=`
    <div class="session-context">${qHtml}
      ${fileStrip(s.session_id,(c&&c.files)||[])}
      ${handoffLinksHtml(s)}
      ${s.read_only?`<div class="relaynote"><b>view only</b> — ${esc(s.read_only_reason||'this thread is owned by another Codex runtime')}</div>`:''}
      ${statusLineHtml(s.status_line,'session:'+s.session_id)}</div>
    <div class="composer-dock">${s.capabilities?.submit?`<div class="freetext composer"><textarea id="sft-${s.session_id}" data-draft-key="${esc(composerDraftKey(s.session_id))}" rows="2" placeholder="send message" autocomplete="off"
      oninput="composerInput(this,'${s.session_id}','sft')" onfocus="composerFocus(this,'${s.session_id}','sft')"
      onkeydown="composerKey(event,()=>sendText('${s.session_id}','sft','smsg'));if(event.key==='Escape')slashClose()">${esc(draftValue(composerDraftKey(s.session_id)))}</textarea>
      <span class="sendpair">${composerTools(s.session_id,'sft-'+s.session_id)}<button class="pbtn send" onclick="sendText('${s.session_id}','sft','smsg')">send</button></span></div><div class="image-drafts" id="imgdraft-${s.session_id}"></div>`:''}
      <div class="slashwrap" id="slash-sft-${s.session_id}"></div>
      <div class="actmsg" id="smsg-${s.session_id}"></div></div>`;});
  if(s.capabilities?.submit){resizeComposer(document.getElementById('sft-'+s.session_id));void renderImageDrafts(s.session_id);}
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
  $('#atitle').innerHTML='<b>subagent</b>';
  const body=$('#abody');body.innerHTML='<div class="ctxload">loading conversation…</div>';
  delete body.dataset.renderKey;
  syncOverlayHistory();
  requestAnimationFrame(()=>{if(agentView?.sid===sid&&agentView?.aid===aid)renderAgent(true);});
}
function closeAgent(){
  closeOverflow();agentView=null;agentInfoOpen2=false;
  $('#aview').style.display='none';$('#abody').innerHTML='';delete $('#abody').dataset.renderKey;
  $('#aact').innerHTML='';$('#actrl').innerHTML='';
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
  const typing=ae&&['INPUT','TEXTAREA'].includes(ae.tagName)&&$('#aview').contains(ae);
  if(!force&&touching())return;
  const body=$('#abody');
  const old={top:body.scrollTop,atBottom:body.scrollTop+body.clientHeight>=body.scrollHeight-12};
  const bodyKey=`agent:${c?.v??'loading'}:${c?.messages?.length??-1}:${c?.next_cursor??''}:${c?.olderError||''}:${c?.error||''}`;
  if(body.dataset.renderKey!==bodyKey){
    if(!c||!c.messages){body.innerHTML='<div class="ctxload">loading conversation…</div>';}
    else if(c.error){body.innerHTML=`<div class="ctxload">✗ ${esc(c.error)}</div>`;}
    else if(!c.messages.length){body.innerHTML='<div class="ctxload">no conversation yet</div>';}
    else body.innerHTML=`<div class="aconvo">${convoMsgs(c,agentView.sid,false,'agent',agentView.aid)}</div>`;
    body.dataset.renderKey=bodyKey;
    body.scrollTop=old.atBottom?body.scrollHeight:old.top;
  }
  if(!typing){
    const ago=ts=>ts?fmtAge(Math.max(0,Math.round((Date.now()-Date.parse(ts))/1000)))+' ago':'?';
    const tk=info.tokens||{};
    const codex=par&&par.provider==='codex';
    $('#aact').innerHTML=`
      <div class="relaynote">${done?'this agent has finished — ':''}${codex
        ?'App Server does not accept direct input to v2 subagents. This message goes to the <b>parent thread</b> with an explicit relay instruction.'
        :'subagents have no terminal of their own: your message is typed into the <b>parent session</b>, tagged for it to forward with SendMessage'}</div>
      ${statusLineHtml(info.status_line,'agent:'+agentView.sid+':'+agentView.aid)}
      ${!done&&par?.capabilities?.relay_agent?`<div class="freetext composer"><textarea id="aft" data-draft-key="${esc(relayDraftKey(agentView.sid,agentView.aid))}" rows="2" placeholder="relay via parent  ·  Return newline  ·  ⌘/Ctrl+Return relay" autocomplete="off"
        onkeydown="composerKey(event,sendRelay)">${esc(draftValue(relayDraftKey(agentView.sid,agentView.aid)))}</textarea>
        <span class="sendpair"><button class="pbtn send" onclick="sendRelay()">relay</button>${scheduleButton(agentView.sid,'aft',agentView.aid)}</span></div>`:''}
      ${agentRelayHtml(agentView.sid,agentView.aid)}
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
  }else refreshStatusStrip('#aact',info.status_line,'agent:'+agentView.sid+':'+agentView.aid);
}
const agentRelays=new Map();
function agentRelayKey(sid,aid){return agentCacheKey(sid,aid);}
function agentRelayHtml(sid,aid){
  const item=agentRelays.get(agentRelayKey(sid,aid));if(!item)return'';
  if(item.status==='failed')return`<div class="quickfeedback failed" role="alert"><span class="qfstate">Relay failed</span><span class="qftext">${esc(item.error||'Request failed')}</span><button onclick="restoreRelay()">restore</button></div>`;
  return`<div class="quickfeedback ${item.status}" role="status"><span class="qfstate">${item.status==='sending'?'Relaying':'Relayed'}</span><span class="qftext">${esc(item.text)}</span>${item.status==='sending'?'<span class="delivery sending" aria-hidden="true">◌</span>':'<span class="delivery sent">✓</span>'}</div>`;
}
function restoreRelay(){
  if(!agentView)return;const key=agentRelayKey(agentView.sid,agentView.aid),item=agentRelays.get(key);
  agentRelays.delete(key);setDraft(relayDraftKey(agentView.sid,agentView.aid),item?.text||'');renderAgent(true);
  requestAnimationFrame(()=>{const input=$('#aft');if(input){input.value=item?.text||'';input.focus();}});
}
async function sendRelay(){
  if(!agentView)return;
  const inp=$('#aft'),v=(inp&&inp.value||'').trim();
  if(!v)return;
  const sid=agentView.sid,aid=agentView.aid,key=agentRelayKey(sid,aid);
  if(agentRelays.get(key)?.status==='sending')return;
  const feedbackStarted=performance.now();
  if(inp)inp.value='';clearDraft(relayDraftKey(sid,aid));agentRelays.set(key,{text:v,status:'sending',error:''});renderAgent(true);
  recordInputFeedback(feedbackStarted,'relay');
  const result=await act(sid,{type:'relay',agent_id:aid,text:v},'amsg');
  const current=agentRelays.get(key);if(!current)return;
  current.status=result.ok?'sent':'failed';current.error=result.error||'';renderAgent(true);
  if(result.ok)setTimeout(()=>{if(agentRelays.get(key)===current){agentRelays.delete(key);renderAgent(true);}},5000);
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
const pinActions=new Map();
function syncPinnedSessions(f){
  pinnedSessions.clear();
  (((f||{}).settings||{}).pinned_sessions||[]).forEach(sid=>pinnedSessions.add(sid));
}
function agentListHtml(agents){
  return agents.map(agentRow).join('');
}
function activeSubagents(f,applyQuery=false){
  const query=applyQuery?nowFilter.trim().toLowerCase():'';
  return((f&&f.sessions)||[]).flatMap(parent=>(parent.agents||[])
    .filter(agent=>!['done','ended'].includes(agent.state))
    .filter(agent=>!query||[
      parent.title,parent.project,parent.branch,parent.provider,parent.cwd,
      agent.agent_type,agent.description,agent.model,agent.effort,agent.state,
      agent.last_msg&&agent.last_msg.text,
    ].filter(Boolean).join(' ').toLowerCase().includes(query))
    .map(agent=>({parent,agent})));
}
function activeSubagentCard(parent,agent){
  const signal={running:'Working',stalled:'Slow — check progress'}[agent.state]||String(agent.state||'Active');
  const latest=agent.last_msg&&agent.last_msg.text?String(agent.last_msg.text):
    `quiet ${fmtAge(Math.max(0,Number(agent.quiet_s??parent.quiet_s??0)))}`;
  const breadcrumb=[parent.project,parent.title,parent.branch&&parent.branch!=='HEAD'?parent.branch:null]
    .filter(Boolean).join(' · ');
  return`<button class="activeagentcard" onclick="openAgent(decodeURIComponent('${enc(parent.session_id)}'),decodeURIComponent('${enc(agent.agent_id)}'))">
    <span class="dot ${esc(agent.state||'running')}" aria-hidden="true"></span>
    <span class="activeagentmain"><b>${esc(agent.description||agent.agent_type||agent.agent_id)}</b>
      <small>${esc(breadcrumb)}</small><em>${esc(latest)}</em></span>
    <span class="activeagentmeta"><b>${esc(signal)}</b><small>${esc(agent.agent_type||'subagent')} · ${esc(modelLabel(agent))}</small></span>
    <span class="aopen">›</span></button>`;
}
function renderActiveSubagents(f){
  const el=$('#subagents');if(!el)return;
  const items=activeSubagents(f,true);
  el.className='queue subagentqueue';
  el.innerHTML=`<div class="queuehead"><b>Active subagents · ${items.length}</b><span>children working across parent sessions</span></div>
    <div class="activeagentlist">${items.length?items.map(({parent,agent})=>activeSubagentCard(parent,agent)).join(''):
      '<div class="queueempty">No active subagents match this filter.</div>'}</div>`;
}
async function toggleSessionPin(sid){
  if(pinActions.get(sid)?.busy)return;
  const previous=[...pinnedSessions];
  const pinned=!pinnedSessions.has(sid);
  pinActions.set(sid,{busy:true,pinned,error:''});
  pinned?pinnedSessions.add(sid):pinnedSessions.delete(sid);
  if(last?.settings)last.settings.pinned_sessions=[...pinnedSessions];
  render(last,true);
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pin_session:sid,pinned})});
    const d=await r.json();
    if(!d.ok)throw new Error(d.error||'failed');
    if(last?.settings)last.settings.pinned_sessions=d.pinned_sessions||[];
    syncPinnedSessions(last);pinActions.delete(sid);render(last,true);
  }catch(e){
    pinnedSessions.clear();previous.forEach(item=>pinnedSessions.add(item));
    pinActions.set(sid,{busy:false,pinned,error:String(e.message||e)});
    if(last?.settings)last.settings.pinned_sessions=[...pinnedSessions];
    render(last,true);
  }
}
function pinFeedbackHtml(sid){
  const item=pinActions.get(sid);if(!item)return'';
  if(item.busy)return`<div class="quickfeedback" role="status"><span class="qfstate">${item.pinned?'Pinning':'Unpinning'}</span><span class="delivery sending" aria-hidden="true">◌</span></div>`;
  return`<div class="quickfeedback failed" role="alert"><span class="qfstate">Pin failed</span><span class="qftext">${esc(item.error||'Could not save pin')}</span><button onclick="event.stopPropagation();toggleSessionPin(decodeURIComponent('${enc(sid)}'))">retry</button></div>`;
}
// mobile: long-press a session header to pin; a short tap still opens its chat
let sessionPressTimer=null,sessionPressTarget=null,sessionLongFired=false;
function sessionPressStart(sid,target){
  sessionPressEnd();sessionPressTarget=target||null;
  if(sessionPressTarget)sessionPressTarget.classList.add('pinpress');
  sessionLongFired=false;
  sessionPressTimer=setTimeout(()=>{sessionLongFired=true;toggleSessionPin(sid);
    if(navigator.vibrate)navigator.vibrate(15);sessionPressEnd();},500);
}
function sessionPressEnd(){
  if(sessionPressTimer){clearTimeout(sessionPressTimer);sessionPressTimer=null;}
  if(sessionPressTarget){sessionPressTarget.classList.remove('pinpress');sessionPressTarget=null;}
}
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
  const liveById=new Map(sessions.map(item=>[item.session_id,item]));
  const closedById=new Map(closed.map(item=>[item.session_id,item]));
  const items=[...pinnedSessions].map(sid=>liveById.get(sid)||closedById.get(sid))
    .filter(item=>item&&predicate(item));
  if(!items.length){el.className='empty';el.innerHTML='';return;}
  el.className='';
  if(!el.querySelector('.pinhdr'))el.innerHTML='<div class="pinhdr">Pinned</div><div class="pinlist"></div>';
  const list=el.querySelector('.pinlist'),seen=new Set();
  items.forEach(item=>{
    const sid=String(item.session_id||'');seen.add(sid);
    let slot=[...list.children].find(child=>child.dataset.pinSid===sid);
    if(!slot){slot=document.createElement('div');slot.className='pinslot';slot.dataset.pinSid=sid;list.appendChild(slot);}
    const kind=liveById.has(sid)?'live':'closed';
    if(slot.dataset.pinKind!==kind){slot.innerHTML='';slot.dataset.pinKind=kind;}
    if(kind==='live')reconcileCards(slot,[item],'');
    else slot.innerHTML=historyRow(item,true);
  });
  [...list.children].forEach(slot=>{if(!seen.has(slot.dataset.pinSid))slot.remove();});
  items.forEach((item,index)=>{
    const slot=[...list.children].find(child=>child.dataset.pinSid===String(item.session_id||''));
    if(slot&&list.children[index]!==slot)list.insertBefore(slot,list.children[index]||null);
  });
}
const setg=()=>((last&&last.settings)||{});
const previewAgents=()=>!!setg().preview_agents;
const previewSessions=()=>setg().preview_sessions!==false;
const clampS=()=>Math.max(1,Math.min(6,setg().preview_session_lines??2));
const clampA=()=>Math.max(1,Math.min(6,setg().preview_agent_lines??1));
const readerWidth=()=>setg().reader_width==='centered'?'centered':'fit';
function applyReaderWidth(){document.documentElement.dataset.readerWidth=readerWidth();}

function cardCls(s){
  if(s.ui_group==='needs_you')return ['Fix needed','Limit reached'].includes(s.reason_label)?'stalled':'needs';
  if(s.ui_group==='available')return'idle';
  if(s.ui_group==='history')return'dorm';
  return s.reason_label==='Slow'?'stalled':'';
}
// Ordinary collapsed cards use one stable frame whose height follows the
// session-peek line preference. Anything that adds an actionable/volatile row
// stays content-sized so a fixed frame can never hide a control.
function cardUsesFixedPeekHeight(s){
  if(s.provisional||open.has(s.session_id)||expandedPeeks.has(s.session_id))return false;
  const pending=s.pending&&(!s.pending.nonce||answered[s.session_id]!==s.pending.nonce);
  const running=s.ui_group==='working'&&(s.agents||[]).some(a=>!['done','ended'].includes(a.state));
  const answerFeedback=optimisticList(s.session_id).some(item=>item.kind==='answer'||item.queueId);
  return!pending&&!s.error&&!s.reply_requested&&!running&&!pinActions.has(s.session_id)&&
    !quickResponses.has(s.session_id)&&!answerFeedback;
}
function cardFrame(s){
  return{fixed:cardUsesFixedPeekHeight(s),lines:previewSessions()?clampS():0};
}
// The volatile top of the card — rebuilt every poll (header, meta, peek, pending,
// running agents, the more/less toggle). No native <details> here, so replacing it
// each tick doesn't flash.
function cardTop(s){
  if(s.provisional)return provisionalCardTop(s);
  const isOpen=open.has(s.session_id);
  const running=s.agents.filter(a=>!['done','ended'].includes(a.state));
  const activeSession=s.ui_group==='working';
  const showPrimary=!(s.provider==='claude'&&(!s.primary_action||['open','continue','view'].includes(s.primary_action)));
  // delivered-file chips + the session peek both need the context cache; the
  // conversation itself now lives only in the full view
  if(isOpen||(previewSessions()&&s.last_msg))ensureCtx(s.session_id,ctxVersion(s));
  const pinned=pinnedSessions.has(s.session_id);
  return`<div class="shead" title="open the full conversation" onclick="sessionTap(event,'${s.session_id}')"
      ontouchstart="sessionPressStart('${s.session_id}',this)" ontouchend="sessionPressEnd()" ontouchmove="sessionPressEnd()">
      <span class="chip ${s.ui_group||s.state}${s.reason_label==='Fix needed'?' problem':''}">${esc(s.reason_label||stateLabel[s.state]||s.state)}</span>
      <span class="sname">${s.title?`<span class="stitle">${esc(s.title)}</span><small>${esc(s.project)}${s.branch&&s.branch!=='HEAD'?` · ${esc(s.branch)}`:''}</small>`:`${esc(s.project)}${s.branch&&s.branch!=='HEAD'?` <small>· ${esc(s.branch)}</small>`:''}`}</span>
      <span class="m" title="session provider">${esc(s.provider||'claude')}</span>
      ${s.access==='view_only'?`<span class="accessbadge view_only">view only</span>`:''}
      ${s.new_response?`<span class="newbadge">new</span>`:''}
      ${showPrimary?`<button class="primarybtn" onclick="event.stopPropagation();primarySessionAction('${s.session_id}')">${esc(s.primary_action_label||'Open')}</button>`:''}
      ${terminalButton(s,true)}
      <button class="spin${pinned?' on':''}" ${pinActions.get(s.session_id)?.busy?'disabled':''} title="${pinned?'unpin session':'pin session'}"
        aria-label="${pinned?'unpin session':'pin session'}"
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
    ${pinFeedbackHtml(s.session_id)}
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
          ${s.provider==='claude'?`<span>permission mode</span><span class="permissiondetail">
            <b>${esc(claudePermissionLabel(s.permission_mode))}</b>${claudePermissionSelect(s,'msg')}</span>`:''}
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
  return[s.muted,s.pid,s.model,s.effort,s.collaboration_mode,s.permission_mode,
    (s.permission_modes||[]).join(','),Boolean(s.capabilities?.change_permission_mode),s.reg_status,s.ctx_tokens,
    s.cost==null?'na':Math.round(((s.cost||0)+(s.agent_cost||0))*100),s.error||'',done,files,
    s.winning_rule||'',s.state_confidence||'',s.provider_stale?'stale':'fresh',
    (s.handoff_links||[]).map(link=>[link.direction,link.session_id,link.status].join(':')).join(',')].join('|');
}
// used only for the (wholesale-rendered) dormant fold; live cards go through reconcileCards
function sessionCard(s){
  const isOpen=open.has(s.session_id);
  const frame=cardFrame(s);
  return`<div class="card ${cardCls(s)}${isOpen?' open':''}${frame.fixed?' fixedpeek':''}" data-sid="${s.session_id}"
    style="--session-card-lines:${frame.lines}">
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
    const frame=cardFrame(s);
    card.className='card'+(cardCls(s)?' '+cardCls(s):'')+(isOpen?' open':'')+
      (pinnedSessions.has(s.session_id)?' pinned':'')+(frame.fixed?' fixedpeek':'');
    card.style.setProperty('--session-card-lines',String(frame.lines));
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
let settingsOpen=false,budgetSettingsOpen=false,settingsReturnState=null,settingsRenderFrame=0;
function uiRefresh(){render(last,true);if(settingsOpen)renderSettings();}
function openSettings(){
  if(settingsOpen)return;
  const stacked=anyOverlay();
  settingsReturnState=sessionView?{sid:sessionView.sid,closed:sessionView.closed,
    scrollTop:$('#sbody')?.scrollTop||0,evidenceOpen:sessionEvidenceOpen}:null;
  settingsOpen=true;
  $('#settingsview').style.display='flex';
  $('#settings').scrollTop=0;
  $('#settings').innerHTML='<div class="ctxload"><span class="delivery sending" aria-hidden="true">◌</span> loading settings…</div>';
  cancelAnimationFrame(settingsRenderFrame);
  settingsRenderFrame=requestAnimationFrame(()=>{
    settingsRenderFrame=0;if(settingsOpen)renderSettings();
  });
  loadPushState(true);
  loadBudgets();
  loadWorkstreams();
  if(stacked){settingsPushed=true;history.pushState({fdSettings:1},'');}
  else syncOverlayHistory();
}
function closeSettings(){
  const restore=settingsReturnState;settingsReturnState=null;
  cancelAnimationFrame(settingsRenderFrame);settingsRenderFrame=0;
  settingsOpen=false;
  $('#settingsview').style.display='none';
  $('#settings').innerHTML='';
  applyRouteNav(currentRoute);
  if(restore&&sessionView&&sessionView.sid===restore.sid&&sessionView.closed===restore.closed){
    sessionEvidenceOpen=restore.evidenceOpen;
    const body=$('#sbody');if(body)body.scrollTop=restore.scrollTop;
    requestAnimationFrame(()=>{if(sessionView&&sessionView.sid===restore.sid){
      const current=$('#sbody');if(current)current.scrollTop=restore.scrollTop;
    }});
  }
}
function renderSettings(){
  const el=$('#settings');
  if(!settingsOpen){el.innerHTML='';return;}
  const st=(last&&last.settings)||{};
  const num=(k,step)=>`<input type="number" value="${st[k]??''}" min="0" step="${step}"
    onchange="setNum('${k}',this.value)">`;
  el.innerHTML=`<div class="setpanel">${pushSettingsHtml()}<div class="dhead">Legacy ntfy</div>
    <label class="setrow"><input type="checkbox" ${st.legacy_ntfy_enabled===true?'checked':''}
      onchange="setBool('legacy_ntfy_enabled',this.checked)">Enable manual legacy tests</label>
    <div class="sethint">Manual test delivery only. Fleet never sends automatic, fallback, or duplicate ntfy alerts.${st.legacy_ntfy_configured?'':' Add ntfy_server and ntfy_topic to config.json first.'}</div>
    <div class="pushactions" style="margin-top:8px"><button onclick="testLegacyNtfy()"
      ${legacyNtfyBusy||st.legacy_ntfy_enabled!==true||!st.legacy_ntfy_configured?'disabled':''}>${legacyNtfyBusy?'<span class="delivery sending">◌</span> Queuing…':'Send legacy test'}</button></div>
    ${legacyNtfyMessage?`<div class="${legacyNtfyError?'pusherror':'pushnotice'}" role="status">${esc(legacyNtfyMessage)}</div>`:''}
    <div class="dhead" style="margin-top:12px">session state</div>
    <label class="setrow">Mark a running session stalled after <span class="setnum">${num('stall_seconds',30)} seconds without progress</span></label>
    <div class="setnum" style="padding:6px 0 2px">per-session mute: tap the 🔔 on a card</div>
    <div class="dhead" style="margin-top:12px">desktop navigation</div>
    <div class="setchoice" role="group" aria-label="Desktop navigation side">
      <button aria-pressed="${navSide==='left'}" onclick="setNavSide('left')">Left side</button>
      <button aria-pressed="${navSide==='right'}" onclick="setNavSide('right')">Right side</button>
    </div>
    <div class="sethint">Saved on this browser. Mobile keeps the bottom navigation.</div>
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
const settingQueues=new Map(),settingIntents=new Map();
function settingMessage(id,text){const element=document.getElementById(id);if(element)element.textContent=text;}
function queueSetting(key,payload,onSuccess,onFailure,messageId='setmsg'){
  const intent=(settingIntents.get(key)||0)+1;settingIntents.set(key,intent);
  settingMessage(messageId,'saving…');
  const prior=settingQueues.get(key)||Promise.resolve();
  const request=prior.catch(()=>{}).then(async()=>{
    const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'save failed');
    return data;
  });
  settingQueues.set(key,request);
  return request.then(data=>{
    if(settingIntents.get(key)!==intent)return data;
    onSuccess(data);uiRefresh();settingMessage(messageId,'saved ✓');return data;
  }).catch(error=>{
    if(settingIntents.get(key)===intent){onFailure();uiRefresh();settingMessage(messageId,'✗ '+String(error.message||error));}
    return{ok:false,error:String(error.message||error)};
  }).finally(()=>{if(settingQueues.get(key)===request)settingQueues.delete(key);});
}
async function setNum(k,v){
  const value=parseFloat(v),previous=last?.settings?.[k];
  if(last?.settings)last.settings[k]=value;uiRefresh();
  return queueSetting(k,{[k]:value},d=>{if(last?.settings)last.settings[k]=d[k];},()=>{
    if(last?.settings)last.settings[k]=previous;
  });
}
async function setBool(k,v){
  const previous=last?.settings?.[k];
  if(last?.settings)last.settings[k]=v;uiRefresh();
  return queueSetting(k,{[k]:v},d=>{if(last?.settings)last.settings[k]=d[k];},()=>{
    if(last?.settings)last.settings[k]=previous;
  });
}
async function setStr(k,v){
  const previous=last?.settings?.[k];
  if(last?.settings)last.settings[k]=v;uiRefresh();
  return queueSetting(k,{[k]:v},d=>{if(last?.settings)last.settings[k]=d[k];},()=>{
    if(last?.settings)last.settings[k]=previous;
  });
}
let legacyNtfyBusy=false,legacyNtfyMessage='',legacyNtfyError=false;
async function testLegacyNtfy(){
  if(legacyNtfyBusy)return;const started=performance.now();legacyNtfyBusy=true;
  legacyNtfyMessage='';legacyNtfyError=false;renderSettings();
  recordInputFeedback(started,'legacy_ntfy_test');
  try{
    const response=await fetch('/api/legacy-ntfy/test',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'Legacy test failed');
    legacyNtfyMessage='Legacy test queued';
  }catch(error){legacyNtfyMessage=String(error.message||error);legacyNtfyError=true;}
  finally{legacyNtfyBusy=false;renderSettings();}
}
async function toggleMute(sid,mute){
  const s=((last||{}).sessions||[]).find(x=>x.session_id===sid);if(!s)return;
  const previous=s.muted;s.muted=mute;uiRefresh();
  return queueSetting('mute:'+sid,{mute_session:sid,muted:mute},()=>{},()=>{s.muted=previous;},
    'msg-'+sid);
}
// multi-question asks: ONE question on screen at a time, ‹ › to move between them
// (keeps a 3-question ask from swallowing the whole screen)
function mqBlock(s,p,pre){
  const sid=s.session_id;
  let st=mqSel[sid];
  if(!st||st.nonce!==p.nonce){
    st=mqSel[sid]={nonce:p.nonce,qi:0,a:{},other:{}};
    (p.questions||[]).forEach((question,index)=>{
      if(!question.secret)st.other[index]=draftValue(questionDraftPrefix(sid,p.nonce)+`other:${index}`);
    });
  }
  const qs=p.questions;
  if(st.qi>=qs.length)st.qi=qs.length-1;
  const qi=st.qi,q=qs[qi],ms=!!q.multiSelect;
  const sel=st.a[qi]||new Set();
  const ansd=i=>((st.a[i]&&st.a[i].size)||(st.other[i]||'').trim())?1:0;
  const donecnt=qs.reduce((a,_,i)=>a+ansd(i),0);
  const oth=(st.other[qi]||'').trim();
  const picked=[...sel].sort().map(d=>(q.options[d-1]||{}).label)
    .concat(oth?['“'+oth+'”']:[]).join(', ');
  const locked=nativeRequestLocked(sid,p.nonce);
  return`<div class="ptool"><span class="ptlabel">multi-part question (${qs.length}) — waiting on you</span>
      <button class="xbtn" ${locked?'disabled':''} title="${p.dismiss_action==='cancel_turn'?'dismiss by stopping this Codex turn':'dismiss — chat about this instead'}" onclick="sendDismiss('${sid}','${p.nonce}','${pre}')">✕</button></div>
    ${p.files&&p.files.length?`<div class="pfiles"><span class="plabel">read first</span>${p.files.map(f=>fchip(sid,f,f.caption)).join('')}</div>`:''}
    <div class="mqnav"><button class="mqarr" ${qi===0||locked?'disabled':''} onclick="mqNav('${sid}',-1)">‹</button>
      <span class="mqpos"><b>${qi+1}</b> of ${qs.length} · ${donecnt}/${qs.length} answered</span>
      <button class="mqarr" ${qi===qs.length-1||locked?'disabled':''} onclick="mqNav('${sid}',1)">›</button></div>
    <div class="qtext"><b>${esc(q.header||'')}</b> ${esc(q.question)}${ms?' <small>(pick all that apply)</small>':''}</div>
    ${(q.options||[]).map((o,i)=>`<button class="optbtn ${sel.has(i+1)?'sel':''}" ${locked?'disabled':''}
        onclick="mqToggle('${sid}',${qi},${i+1},${ms},${qs.length})">${esc(o.label)}${o.description?`<small>${esc(o.description)}</small>`:''}</button>`).join('')}
    ${q.allowOther!==false?`<div class="freetext"><input ${q.secret?'':`data-draft-key="${esc(questionDraftPrefix(sid,p.nonce)+`other:${qi}`)}"`} ${locked?'disabled':''} placeholder="Other — type your own answer" ${q.secret?'type="password"':''} value="${esc(st.other[qi]||'')}"
      oninput="mqOther('${sid}',${qi},this.value)"></div>`:''}
    <div class="mqsum">selected: ${picked?esc(picked):'—'}</div>
    <div class="pbtns"><button class="pbtn send" ${locked?'disabled':''} onclick="mqSend('${sid}','${p.nonce}','${pre}')">submit all answers</button></div>`;
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
  return withNativeRequestLock(sid,nonce,()=>{
    const optimisticId=addOptimistic(sid,answerPreview(sid,answers),'answer');
    return act(sid,{type:'multiq',nonce,answers},pre,optimisticId);
  });
}
function elicitationBlock(s,p,pre){
  const sid=s.session_id;
  const locked=nativeRequestLocked(sid,p.nonce);
  const draft=elicitDraft[sid]=elicitDraft[sid]||{};
  for(const field of p.fields||[])if(!field.secret&&!['select','boolean'].includes(field.type)&&draft[field.name]==null)
    draft[field.name]=draftValue(questionDraftPrefix(sid,p.nonce)+`field:${field.name}`);
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
    return`<label class="qtext"><b>${esc(f.label)}</b>${f.required?' *':''}<input ${f.secret?'type="password"':`data-draft-key="${esc(questionDraftPrefix(sid,p.nonce)+`field:${f.name}`)}"`}
      value="${esc(value??'')}" oninput="elicitText('${sid}','${key}',this.value)"></label>`;
  }).join('');
  const safeUrl=String(p.url||'').startsWith('https://')||String(p.url||'').startsWith('http://');
  return`<div class="pend"><div class="ptool"><span class="ptlabel">${esc(p.server||'MCP')} request — waiting on you</span></div>
    <div class="qtext">${esc(p.message||'')}</div>${fields}
    ${safeUrl?`<a class="jump" href="${esc(p.url)}" target="_blank" rel="noopener">open request ↗</a>`:''}
    <div class="pbtns">
      <button class="pbtn allow" ${locked?'disabled':''} onclick="sendElicitation('${sid}','${p.nonce}','accept','${pre}')">accept</button>
      <button class="pbtn deny" ${locked?'disabled':''} onclick="sendElicitation('${sid}','${p.nonce}','decline','${pre}')">decline</button>
      <button class="pbtn" ${locked?'disabled':''} onclick="sendElicitation('${sid}','${p.nonce}','cancel','${pre}')">cancel</button>
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
  return withNativeRequestLock(sid,nonce,()=>act(sid,
    {type:'elicitation',nonce,choice,content:choice==='accept'?content:undefined},pre));
}
// On the CARD a question is only a SIGNAL — the option buttons, Other input and
// per-question nav ate the fleet list. Tapping it opens the full view with the
// question expanded. Permission prompts are small and stay inline (allow/deny).
function cardPending(s){
  const p=s.pending;
  if(!p||(p.nonce&&answered[s.session_id]===p.nonce))return'';
  if(s.staging_observer)return`<div class="pend qsignal stagingreadonly" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">
    <div class="ptool"><span class="ptlabel">production request · view only in staging</span></div>
    <button class="pbtn" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">view ⤢</button></div>`;
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
function stagingPendingBox(s,p){
  if(p.kind==='question')return`<div class="pend stagingreadonly">
    <div class="ptool"><span class="ptlabel">production question · view only in staging</span></div>
    ${(p.questions||[]).map(q=>`<div class="qtext"><b>${esc(q.header||'Question')}</b> ${esc(q.question||'')}</div>
      ${(q.options||[]).map(option=>`<div class="optbtn" aria-disabled="true">${esc(option.label||'')}${option.description?`<small>${esc(option.description)}</small>`:''}</div>`).join('')}`).join('')}
    <div class="actmsg">Answer this request in production.</div></div>`;
  if(p.kind==='permission')return`<div class="pend stagingreadonly">
    <div class="ptool"><span class="ptlabel">production permission request · view only in staging</span></div>
    <pre>${esc(p.input_summary||'')}</pre><div class="actmsg">Decide this request in production.</div></div>`;
  return`<div class="pend stagingreadonly"><div class="ptool"><span class="ptlabel">production request · view only in staging</span></div>
    <div class="qtext">${esc(p.message||'This request can only be changed in production.')}</div></div>`;
}
function pendingBox(s,pre='msg'){
  const p=s.pending; if(!p)return'';
  if(p.nonce&&answered[s.session_id]===p.nonce)return'';   // sent: dismiss instantly
  if(s.staging_observer)return stagingPendingBox(s,p);
  if(p.kind==='question'){
    if(!p.questions||!p.questions.length)return'';
    return`<div class="pend">${p.questions.length>1?mqBlock(s,p,pre):singleQBlock(s,p,pre)}
      <div class="actmsg" id="${pre}-${s.session_id}"></div></div>`;
  }
  if(p.kind==='permission'){
    const locked=nativeRequestLocked(s.session_id,p.nonce);
    return`<div class="pend">
      <div class="ptool">permission: ${esc(p.tool)} — waiting on you</div>
      <pre>${esc(p.input_summary||'')}</pre>
      <div class="pbtns">
        <button class="pbtn allow" ${locked?'disabled':''} onclick="sendPerm('${s.session_id}','${p.nonce}','allow','${pre}')">allow</button>
        <button class="pbtn always" ${locked?'disabled':''} onclick="sendPerm('${s.session_id}','${p.nonce}','always','${pre}')">always allow</button>
        <button class="pbtn deny" ${locked?'disabled':''} onclick="sendPerm('${s.session_id}','${p.nonce}','deny','${pre}')">deny</button>
        ${(p.decisions||[]).includes('cancel')?`<button class="pbtn" ${locked?'disabled':''} onclick="sendPerm('${s.session_id}','${p.nonce}','cancel','${pre}')">cancel</button>`:''}
      </div>
      <div class="actmsg" id="${pre}-${s.session_id}"></div>
    </div>`;
  }
  if(p.kind==='elicitation')return elicitationBlock(s,p,pre);
  return'';
}
async function setSessionMode(sid,mode,pre='msg'){
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(!s||s.provider!=='codex'||providerModeActions.has(sid))return;
  const previous=s.collaboration_mode||'default';
  providerModeActions.set(sid,{kind:'mode'});s.collaboration_mode=mode;uiRefresh();
  const message=()=>document.getElementById(pre+'-'+sid)||document.getElementById(pre);
  if(message())message().textContent='changing mode…';
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'mode',mode})});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'mode change failed');
    s.collaboration_mode=d.mode||mode;providerModeActions.delete(sid);uiRefresh();
    if(message())message().textContent='mode changed ✓';
  }catch(e){s.collaboration_mode=previous;providerModeActions.delete(sid);uiRefresh();
    if(message())message().textContent='✗ '+String(e.message||e);}
}
function setClaudePermissionMode(sid,mode,pre='msg'){
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(!s||s.provider!=='claude'||claudePermissionLocked(s))return;
  if(mode==='bypassPermissions'){
    askConfirm('Use Bypass permissions?',
      '<b>Claude will stop asking before dangerous commands.</b> This removes almost all permission checks for this session. '
      +'Use it only in an isolated, disposable environment whose files and network access cannot cause harm.',
      'use bypass permissions',()=>applyClaudePermissionMode(s,mode,pre),true);
    return;
  }
  applyClaudePermissionMode(s,mode,pre);
}
async function applyClaudePermissionMode(s,mode,pre){
  if(providerModeActions.has(s.session_id))return;
  const previous=s.permission_mode;
  if(previous===mode)return;
  providerModeActions.set(s.session_id,{kind:'permission'});s.permission_mode=mode;uiRefresh();
  const message=()=>document.getElementById(pre+'-'+s.session_id)||document.getElementById(pre);
  if(message())message().textContent='changing permissions…';
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:s.session_id,type:'permission_mode',mode})});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'permission mode change failed');
    s.permission_mode=d.mode||mode;providerModeActions.delete(s.session_id);uiRefresh();
    if(message())message().textContent='permissions changed ✓';
  }catch(e){s.permission_mode=previous;providerModeActions.delete(s.session_id);uiRefresh();
    if(message())message().textContent='✗ '+String(e.message||e);}
}
async function act(sid,payload,pre='msg',optimisticId=null){
  const requestStarted=performance.now();
  const isQuick=['permission','dismiss','elicitation'].includes(payload.type);
  const quickId=isQuick?beginQuickResponse(sid,payload):null;
  const setMessage=text=>{if(pre===false||pre==null)return null;
    const el=document.getElementById(pre+'-'+sid)||document.getElementById(pre);
    if(el)el.textContent=text;return el;};
  if(fleetOffline){
    const error='Offline — your draft is saved. Reconnect before sending.';
    setMessage('✗ '+error);if(optimisticId!=null)updateOptimistic(sid,optimisticId,false,error);
    return{ok:false,error,offline:true};
  }
  if(payload.type!=='ping')setMessage('sending…');
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,...payload})});
    const d=await r.json();
    if(payload.type!=='ping')perfRecord(`native_${String(payload.type).replace(/[^a-z0-9_]+/gi,'_')}_ms`,
      performance.now()-requestStarted);
    if(optimisticId!=null)updateOptimistic(sid,optimisticId,d.ok,d.error,
      d.ok&&['option','multiq'].includes(payload.type));
    if(quickId!=null)finishQuickResponse(sid,quickId,d.ok,d.error);
    const el=setMessage(d.ok?'sent ✓':'✗ '+(d.error||'failed'));
    if(!d.ok&&!el&&quickId==null&&payload.type!=='focus'&&pre!==false)alert(d.error||'failed');
    if(d.ok&&payload.nonce&&['option','multiq','permission','dismiss','elicitation'].includes(payload.type)){
      answered[sid]=payload.nonce;      // hide the selector NOW, don't wait for the poll
      clearDraftPrefix(questionDraftPrefix(sid,payload.nonce));
      delete otherDraft[sid];delete mqSel[sid];delete elicitDraft[sid];multiSel[sid]=new Set();
      uiRefresh();
    }
    return d;
  }catch(e){
    if(payload.type!=='ping')perfRecord(`native_${String(payload.type).replace(/[^a-z0-9_]+/gi,'_')}_ms`,
      performance.now()-requestStarted);
    if(optimisticId!=null)updateOptimistic(sid,optimisticId,false,String(e));
    if(quickId!=null)finishQuickResponse(sid,quickId,false,String(e));
    const el=setMessage('✗ '+e);
    if(!el&&quickId==null&&payload.type!=='focus'&&pre!==false)alert('request failed: '+e);
    setFleetOffline(true);
    return {ok:false,error:String(e),network_error:true};
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
  return withNativeRequestLock(sid,nonce,()=>{
    const optimisticId=addOptimistic(sid,answerPreview(sid,[{digits}]),'answer');
    return act(sid,{type:'option',nonce,digits},pre,optimisticId);
  });
}
function toggleOpt(sid,d){
  const s=multiSel[sid];s.has(d)?s.delete(d):s.add(d);uiRefresh();
}
function sendMulti(sid,nonce,n,pre){
  const digits=[...(multiSel[sid]||[])].sort();
  const other=(otherDraft[sid]||'').trim();
  if(!digits.length&&!other)return alert('pick at least one option');
  return withNativeRequestLock(sid,nonce,()=>{
    const optimisticId=addOptimistic(sid,answerPreview(sid,[{digits,other}]),'answer');
    return act(sid,{type:'option',nonce,digits,multi:true,n_options:n,other:other||undefined},pre,optimisticId);
  });
}
function sendOther(sid,nonce,n,pre){
  const other=(otherDraft[sid]||'').trim();
  if(!other)return alert('type your answer first');
  return withNativeRequestLock(sid,nonce,()=>{
    const optimisticId=addOptimistic(sid,answerPreview(sid,[{other}]),'answer');
    return act(sid,{type:'option',nonce,n_options:n,other},pre,optimisticId);
  });
}
function sendDismiss(sid,nonce,pre){
  return withNativeRequestLock(sid,nonce,()=>act(sid,{type:'dismiss',nonce},pre));
}
// desktop only: pointless from the phone — it focuses a tab on the Mac
async function focusSession(sid,button=null){
  if(terminalActions.get(sid)?.busy)return;
  const feedbackStarted=performance.now(),original=button?.textContent||'Terminal';
  terminalActions.set(sid,{busy:true,ok:false,error:''});
  if(button){button.disabled=true;button.textContent='Opening…';}
  recordInputFeedback(feedbackStarted,'terminal');
  const result=await act(sid,{type:'focus'});
  terminalActions.set(sid,{busy:false,ok:!!result.ok,error:result.error||''});
  if(button&&button.isConnected){button.disabled=false;button.textContent=result.ok?'Opened ✓':'Retry';
    if(!result.ok)button.title=result.error||'Could not open terminal';}
  setTimeout(()=>{const item=terminalActions.get(sid);if(item&&!item.busy){terminalActions.delete(sid);if(last)render(last,true);}},3000);
  return result;
}

// in-app interstitial — a native confirm() is easy to dismiss by reflex on a phone,
// and stopping a turn is destructive (the work in flight is lost)
let confirmYes=null;
function askConfirm(title,body,confirmLabel,onYes,danger=false){
  closeOverflow();
  confirmYes=onYes;
  $('#confirm').innerHTML=`<div class="cfbox${danger?' cfhigh':''}">
    <div class="cftitle">${esc(title)}</div>
    <div class="cfbody">${body}</div>
    <div class="cfbtns">
      <button class="pbtn" onclick="closeConfirm()">cancel</button>
      <button class="pbtn cfgo${danger?' danger':''}" onclick="const f=confirmYes;closeConfirm();f&&f()">${esc(confirmLabel)}</button>
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
function closeWorktreeFiles(preview){
  const dirty=(preview.dirty_files||[]).map(item=>`<li><span>${esc(item.category||'changed')}</span> ${esc(item.path||'')}</li>`).join('');
  const ignored=(preview.ignored_files||[]).map(path=>`<li><span>ignored</span> ${esc(path)}</li>`).join('');
  const dirtyMore=preview.dirty_files_truncated?`<li>…and more changed paths (${preview.dirty_total} total)</li>`:'';
  const ignoredMore=preview.ignored_files_truncated?`<li>…and more ignored paths (${preview.ignored_count} total)</li>`:'';
  return dirty||ignored?`<ul class="closefiles">${dirty}${dirtyMore}${ignored}${ignoredMore}</ul>`:'';
}
function closeProviderCopy(s,active){
  const provider=s.provider==='codex'?'The Codex thread will be archived.':
    'The registered Claude process will end. Its iTerm tab stays open.';
  return provider+(active?' The current turn and every subagent under it will stop first.':'')+
    ' The conversation remains available in <b>Session history</b>.';
}
function renderCloseWorktree(s,pre,preview,active){
  const shared=(preview.shared_sessions||[]).map(item=>esc(item.title||item.session_id)).join(', ');
  const reason=shared?`<div class="closeblock">Removal is blocked while this worktree is also used by: <b>${shared}</b>.</div>`:
    preview.reason?`<div class="closeblock">${esc(preview.reason)}</div>`:'';
  const path=esc(preview.worktree||s.cwd||'');
  $('#confirm').innerHTML=`<div class="cfbox closechoice">
    <div class="cftitle">Close this session?</div>
    <div class="cfbody">${closeProviderCopy(s,active)}<div class="closepath"><b>Secondary worktree</b>${path}</div>
      ${reason}${closeWorktreeFiles(preview)}</div>
    <div class="closechoices">
      <button class="pbtn" onclick="closeConfirm()">cancel</button>
      <button class="pbtn cfgo" onclick="executeCloseSession('${s.session_id}','${pre}','preserve')">close · preserve worktree</button>
      <button class="pbtn remover" ${preview.remove_allowed?'':'disabled'}
        onclick="executeCloseSession('${s.session_id}','${pre}','remove')">close · remove clean worktree</button>
      ${preview.force_remove_allowed?`<button class="pbtn force" onclick="confirmForceClose('${s.session_id}','${pre}')">force remove dirty worktree</button>`:''}
    </div></div>`;
  $('#confirm').style.display='flex';
}
function confirmForceClose(sid,pre){
  const preview=closePreviewCache.get(sid);if(!preview)return;
  askConfirm('Force remove dirty worktree?',
    '<b>This permanently deletes every listed worktree file, including ignored files.</b> The Git branch survives.'+
    closeWorktreeFiles(preview),
    'close and force remove',()=>executeCloseSession(sid,pre,'force_remove'),true);
}
const closePreviewCache=new Map();
async function closeSessionSurfaceAfterClose(){
  closeConfirm();
  if(histPushed){
    await new Promise(resolve=>{
      window.addEventListener('popstate',()=>resolve(),{once:true});
      history.back();
    });
  }else{closeViewer();closeSession();}
}
async function executeCloseSession(sid,pre,cleanup){
  const preview=closePreviewCache.get(sid);const destructive=cleanup!=='preserve';
  let providerClosed=false;
  $('#confirm').innerHTML=`<div class="cfbox"><div class="cftitle">Closing session…</div>
    <div class="cfbody"><span class="delivery sending" aria-hidden="true">◌</span> ${destructive?'Closing the provider before removing the worktree.':'Preserving the worktree.'}</div></div>`;
  try{
    const closeResponse=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'close',cleanup_ticket:destructive?preview?.cleanup_ticket:undefined})});
    const closed=await closeResponse.json();
    if(!closeResponse.ok||!closed.ok)throw new Error(closed.error||'session close failed');
    providerClosed=true;
    if(!destructive){closePreviewCache.delete(sid);await closeSessionSurfaceAfterClose();setTimeout(()=>tick(),0);return;}
    $('#confirm .cfbody').innerHTML='<span class="delivery sending" aria-hidden="true">◌</span> Session closed. Rechecking the worktree before removal…';
    const cleanupResponse=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'worktree_cleanup',cleanup_ticket:preview.cleanup_ticket,
                           force:cleanup==='force_remove'})});
    const result=await cleanupResponse.json();
    closePreviewCache.delete(sid);await closeSessionSurfaceAfterClose();setTimeout(()=>tick(),0);
    if(!cleanupResponse.ok||!result.ok){
      $('#confirm').innerHTML=`<div class="cfbox"><div class="cftitle">Session closed · worktree preserved</div>
        <div class="cfbody">${esc(result.error||'Cleanup failed')}<div class="closepath">${esc(result.worktree||preview.worktree||'')}</div></div>
        <div class="cfbtns"><button class="pbtn" onclick="closeConfirm()">close</button></div></div>`;
      $('#confirm').style.display='flex';
      return;
    }
    closeConfirm();
  }catch(error){
    if(providerClosed){
      closePreviewCache.delete(sid);await closeSessionSurfaceAfterClose();setTimeout(()=>tick(),0);
    }
    $('#confirm').innerHTML=`<div class="cfbox"><div class="cftitle">${providerClosed?'Session closed · worktree preserved':'Could not close session'}</div>
      <div class="cfbody">${esc(String(error.message||error))}${providerClosed?`<div class="closepath">${esc(preview?.worktree||'')}</div>`:''}</div><div class="cfbtns">
      <button class="pbtn" onclick="closeConfirm()">close</button></div></div>`;
    $('#confirm').style.display='flex';
  }
}
async function sendCloseSession(sid,pre='smsg'){
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(!s||!s.capabilities?.close)return alert('This session cannot be closed here.');
  const active=['running','stalled','needs_you','stalled_or_prompt'].includes(s.state);
  closeOverflow();
  $('#confirm').innerHTML='<div class="cfbox"><div class="cftitle">Checking worktree…</div><div class="cfbody"><span class="delivery sending" aria-hidden="true">◌</span> Looking for files that closing could remove.</div></div>';
  $('#confirm').style.display='flex';
  try{
    const response=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'close_preview'})});
    const preview=await response.json();
    if(!response.ok||!preview.ok)throw new Error(preview.error||'worktree check failed');
    if(preview.secondary_worktree){closePreviewCache.set(sid,preview);renderCloseWorktree(s,pre,preview,active);return;}
    closeConfirm();
    askConfirm('Close this session?',closeProviderCopy(s,active),active?'stop and close':'close session',
      ()=>executeCloseSession(sid,pre,'preserve'));
  }catch(error){
    closeConfirm();
    askConfirm('Close this session?',closeProviderCopy(s,active)+
      `<div class="closeblock">Fleet could not inspect the worktree: ${esc(String(error.message||error))}. Closing will preserve it.</div>`,
      active?'stop and close':'close session',()=>executeCloseSession(sid,pre,'preserve'));
  }
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
  const original=el.textContent;el.textContent='copying…';
  if(!navigator.clipboard){el.textContent='copy unavailable';setTimeout(()=>{el.textContent=original},1200);return;}
  navigator.clipboard.writeText(el.dataset.copy).then(()=>{
    el.textContent='copied ✓';setTimeout(()=>{el.textContent=original},900);
  }).catch(()=>{el.textContent='copy failed';setTimeout(()=>{el.textContent=original},1200);});
}
function sendPerm(sid,nonce,choice,pre='msg'){
  return withNativeRequestLock(sid,nonce,()=>act(sid,{type:'permission',nonce,choice},pre));
}
function imageType(file){
  const mime=String(file?.type||'').toLowerCase();
  if(['image/jpeg','image/png','image/gif','image/webp','image/heic','image/heif'].includes(mime))return mime;
  const ext=String(file?.name||'').toLowerCase().split('.').pop();
  return ext==='heic'?'image/heic':ext==='heif'?'image/heif':'';
}
async function chooseImages(sid,input){
  const message=document.getElementById('smsg-'+sid),current=imageDraftIds(sid);
  const files=[...(input?.files||[])];if(input)input.value='';
  if(current.length>=IMAGE_MAX_COUNT){if(message)message.textContent='Remove an image before adding another.';return;}
  let added=0;
  for(const file of files.slice(0,IMAGE_MAX_COUNT-current.length)){
    const type=imageType(file);
    if(!type){if(message)message.textContent='Use JPEG, PNG, GIF, WebP, HEIC, or HEIF images.';continue;}
    if(!file.size||file.size>IMAGE_MAX_BYTES){if(message)message.textContent='Each image must be 10 MB or smaller.';continue;}
    const id=offlineMessageId();
    try{await putImage({id,sid:String(sid),name:String(file.name||'image').slice(0,120),
      type,size:file.size,created:Date.now(),blob:file});current.push(id);added++;}
    catch(error){if(message)message.textContent='Could not save this image on the device.';console.warn(error);break;}
  }
  setImageDraftIds(sid,current);await renderImageDrafts(sid);
  if(message&&added)message.textContent=`${added} image${added===1?'':'s'} attached`;
  void pruneImages();
}
async function renderImageDrafts(sid){
  const target=document.getElementById('imgdraft-'+sid),ids=imageDraftIds(sid);if(!target)return;
  if(!ids.length){target.innerHTML='';return;}
  const records=(await Promise.all(ids.map(id=>getImage(id).catch(()=>null)))).filter(Boolean);
  if(!document.getElementById('imgdraft-'+sid))return;
  const missing=ids.filter(id=>!records.some(record=>record.id===id));
  if(missing.length)setImageDraftIds(sid,ids.filter(id=>!missing.includes(id)));
  target.innerHTML=records.map(record=>`<span class="image-draft">🖼 <span>${esc(record.name||'image')}</span><small>${Math.max(1,Math.round(record.size/1024))} KB</small><button type="button" aria-label="remove ${esc(record.name||'image')}" onclick="removeImageDraft(decodeURIComponent('${enc(sid)}'),'${record.id}')">×</button></span>`).join('');
}
async function removeImageDraft(sid,id){
  setImageDraftIds(sid,imageDraftIds(sid).filter(value=>value!==id));await deleteImage(id).catch(()=>{});
  await renderImageDrafts(sid);
}
async function uploadImages(sid,imageIds){
  const uploadIds=[];
  for(const id of imageIds){
    const record=await getImage(id).catch(()=>null);
    if(!record)return{ok:false,error:'An attached image is no longer stored on this device.'};
    try{
      const url=`/api/upload-image?sid=${encodeURIComponent(sid)}&id=${encodeURIComponent(id)}&name=${encodeURIComponent(record.name||'image')}`;
      const response=await fetch(url,{method:'POST',headers:{'Content-Type':record.type},body:record.blob});
      const result=await response.json();
      if(!response.ok||!result.ok)return{ok:false,error:result.error||'Image upload failed'};
      uploadIds.push(result.upload_id);
    }catch(error){setFleetOffline(true);return{ok:false,error:String(error),network_error:true};}
  }
  return{ok:true,uploadIds};
}
async function sendText(sid,ftPre='ft',msgPre='msg'){
  const inp=document.getElementById(ftPre+'-'+sid);
  const v=(inp&&inp.value||'').trim(),imageIds=imageDraftIds(sid);
  if(!v&&!imageIds.length)return;
  const text=v||(imageIds.length===1?'Please inspect the attached image.':'Please inspect the attached images.');
  const cmd=(v.startsWith('/')||v.startsWith('$'))?(cmdCache[sid]||[]).find(c=>c.name===v.split(/\s+/)[0]):null;
  const el=document.getElementById(msgPre+'-'+sid)||document.getElementById(msgPre);
  if(imageIds.length&&(v.startsWith('/')||v.startsWith('$'))){if(el)el.textContent='Send commands and images separately.';return;}
  if(fleetOffline){
    if(v.startsWith('/')||v.startsWith('$')){
      if(el)el.textContent='offline — command draft saved; reconnect to run it';
      return;
    }
    const queued=queueOfflineText(sid,text,imageIds);
    if(!queued){if(el)el.textContent='offline queue is full — draft kept here';return;}
    slashClose();closeComposerMenus();if(inp){inp.value='';resizeComposer(inp);}clearDraft(composerDraftKey(sid));setImageDraftIds(sid,[]);void renderImageDrafts(sid);
    if(el)el.textContent='queued offline — sends automatically after reconnection';
    return;
  }
  if(cmd&&cmd.danger&&!confirm(`${cmd.name} destroys this session's conversation state.\n\n${cmd.desc}\n\nSend it?`))return;
  slashClose();closeComposerMenus();
  if(inp){inp.value='';resizeComposer(inp);}clearDraft(composerDraftKey(sid));setImageDraftIds(sid,[]);void renderImageDrafts(sid); // sending is the only automatic clear
  if(cmd&&cmd.execution==='action')return act(sid,{type:cmd.action},msgPre);
  if(cmd&&cmd.execution==='skill')return act(sid,{type:'skill',name:cmd.name,args:v.slice(cmd.name.length).trim()},msgPre);
  const optimisticId=addOptimistic(sid,text,'text','sending',null,null,imageIds);
  if(!imageIds.length)return act(sid,{type:'text',text},msgPre,optimisticId);
  if(el)el.textContent='uploading images…';
  const uploaded=await uploadImages(sid,imageIds);
  if(!uploaded.ok){
    if(uploaded.network_error&&offlineMessages.length<100){
      queueOfflineText(sid,text,imageIds,optimisticId);if(el)el.textContent='queued offline — sends automatically after reconnection';return;
    }
    updateOptimistic(sid,optimisticId,false,uploaded.error);if(el)el.textContent='✗ '+uploaded.error;return;
  }
  const result=await act(sid,{type:'image_text',text,upload_ids:uploaded.uploadIds},msgPre,optimisticId);
  if(result.ok)void deleteImages(imageIds);
}

let offlineFlushBusy=false;
function queueOfflineText(sid,text,imageIds=[],existingOptimisticId=null){
  if(offlineMessages.length>=100)return null;
  const messages=(ctxCache[sid]&&ctxCache[sid].messages)||[];
  const entry={id:offlineMessageId(),sid,text:String(text),imageIds:[...(imageIds||[])],created:Date.now(),
    baseCount:canonicalCount(messages,{kind:'text',text})};
  offlineMessages.push(entry);persistOfflineMessages();
  const existing=existingOptimisticId!=null;
  // Do not call optimisticList before linking an existing row: that function
  // materializes every unlinked queue record and would create a duplicate.
  let item=existingOptimisticId==null?null:optimisticBucket(sid).find(candidate=>candidate.id===existingOptimisticId);
  if(item){clearTimeout(item.confirmTimer);item.queueId=entry.id;item.status='queued';delete item.error;}
  else{optimisticList(sid);item=optimisticList(sid).find(candidate=>candidate.queueId===entry.id);}
  if(item){
    const openConvo=sessionView?.sid===sid&&!sessionView.closed&&$('#sbody .aconvo');
    if(openConvo){
      const current=existing?openConvo.querySelector(`[data-optimistic-id="${item.id}"]`):null;
      if(current)current.outerHTML=optimisticItemHtml(item);
      else openConvo.insertAdjacentHTML('beforeend',optimisticItemHtml(item));
      $('#sbody').scrollTop=$('#sbody').scrollHeight;}
  }
  uiRefresh();
  return entry;
}
function removeOfflineMessage(id){
  const length=offlineMessages.length;
  offlineMessages=offlineMessages.filter(item=>item.id!==id);
  if(offlineMessages.length!==length)persistOfflineMessages();
}
async function flushOfflineMessages(){
  if(fleetOffline||offlineFlushBusy||!offlineMessages.length)return;
  offlineFlushBusy=true;
  try{
    while(!fleetOffline&&offlineMessages.length){
      const queued=offlineMessages[0];
      const item=optimisticList(queued.sid).find(candidate=>candidate.queueId===queued.id);
      if(!item){removeOfflineMessage(queued.id);continue;}
      item.baseCount=canonicalCount((ctxCache[queued.sid]?.messages)||[],item);
      item.status='sending';delete item.error;armOptimisticTimeout(item);uiRefresh();
      let payload={type:'text',text:queued.text};
      if(queued.imageIds?.length){
        const uploaded=await uploadImages(queued.sid,queued.imageIds);
        if(uploaded.network_error){clearTimeout(item.confirmTimer);item.status='queued';delete item.error;uiRefresh();break;}
        if(!uploaded.ok){removeOfflineMessage(queued.id);clearTimeout(item.confirmTimer);item.status='failed';item.error=uploaded.error;uiRefresh();continue;}
        payload={type:'image_text',text:queued.text,upload_ids:uploaded.uploadIds};
      }
      const result=await act(queued.sid,payload,false,item.id);
      if(result.offline){
        clearTimeout(item.confirmTimer);item.status='queued';delete item.error;uiRefresh();break;
      }
      removeOfflineMessage(queued.id);
      if(result.network_error){
        clearTimeout(item.confirmTimer);item.status='failed';
        item.error='Connection dropped while sending. Fleet did not retry because delivery is unknown; restore to send again.';
        uiRefresh();break;
      }
      if(!result.ok){
        clearTimeout(item.confirmTimer);item.status='failed';item.error=result.error||'Send rejected';uiRefresh();
      }
      if(result.ok&&queued.imageIds?.length)void deleteImages(queued.imageIds);
    }
  }finally{offlineFlushBusy=false;}
}

// ---- slash-command autocomplete -------------------------------------------
// Menu INSERTS (never sends): most skills take args, and it keeps the send path
// — with its destructive-command confirm — as the single way anything fires.
const cmdCache={};          // sessionId -> [{name,desc,scope,danger}]
const cmdLoads={},cmdErrors={};
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
  const box=document.getElementById('slash-'+pre+'-'+sid);if(!box)return;
  slashBox='slash-'+pre+'-'+sid;
  if(!cmdCache[sid]){
    if(!cmdLoads[sid]){
      box.innerHTML='<div class="slashmenu"><div class="slashempty"><span class="delivery sending" aria-hidden="true">◌</span> loading commands…</div></div>';
      cmdLoads[sid]=(async()=>{try{
        const r=await fetch('/api/commands?sid='+encodeURIComponent(sid),{cache:'no-store'});
        const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'commands unavailable');
        cmdCache[sid]=d.commands||[];delete cmdErrors[sid];
      }catch(e){cmdErrors[sid]=String(e.message||e);}
      finally{delete cmdLoads[sid];}})();
    }
    await cmdLoads[sid];
    const current=document.getElementById(pre+'-'+sid);
    if(!current||current.value!==v)return;
  }
  if(cmdErrors[sid]){box.innerHTML=`<div class="slashmenu"><div class="slashempty">${esc(cmdErrors[sid])} <button onclick="retryCommands('${sid}','${pre}')">retry</button></div></div>`;return;}
  const q=v.slice(1).toLowerCase();
  const hits=cmdCache[sid].filter(c=>c.name[0]===v[0]&&c.name.slice(1).toLowerCase().includes(q))
    .sort((a,b)=>(a.name.slice(1).toLowerCase().startsWith(q)?0:1)-(b.name.slice(1).toLowerCase().startsWith(q)?0:1))
    .slice(0,40);
  box.innerHTML=hits.length?`<div class="slashmenu">${hits.map(c=>`
    <button class="slashrow" onmousedown="event.preventDefault()" onclick="slashPick('${sid}','${pre}','${enc(c.name)}')">
      <span class="scmd">${esc(c.name)}${c.danger?' <span class="sdanger">destructive</span>':''}</span>
      <span class="sdesc">${esc(c.desc||'')}</span>
      <span class="sscope">${esc(c.scope)}</span>
    </button>`).join('')}</div>`
    :`<div class="slashmenu"><div class="slashempty">no command matches “${esc(v)}”</div></div>`;
}
function retryCommands(sid,pre){delete cmdCache[sid];delete cmdErrors[sid];slashInput(sid,pre);}
function slashPick(sid,pre,name){
  const inp=document.getElementById(pre+'-'+sid);
  if(!inp)return;
  inp.value=decodeURIComponent(name)+' ';   // trailing space: args go right here
  setDraft(composerDraftKey(sid),inp.value);
  slashClose();
  inp.focus();
}
let historyFilter=draftValue('filter:history'),historyAccess='all',historyProvider='all';
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
        ${session?`<button class="spin actionpin" ${pinActions.get(session.session_id)?.busy?'disabled':''}
          title="pin session" aria-label="pin session"
          onclick="event.stopPropagation();toggleSessionPin(decodeURIComponent('${enc(session.session_id)}'))">📌</button>`:''}
        ${session?.muted?'<span class="actionmuted" title="session notifications muted">🔕</span>':''}
        ${session?cardResponseFeedback(session):''}
        ${session?pinFeedbackHtml(session.session_id):''}
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
        ${safeGithubUrl(repository.github_url)?`<a class="repoopen" href="${esc(safeGithubUrl(repository.github_url))}" target="_blank" rel="noopener">GitHub ↗</a>`:''}</div>
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
  historyFilter=value;setDraft('filter:history',value);clearTimeout(historyFilterTimer);
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
let newOpen=false,newProvider='claude',newDir=draftValue('new:directory'),newModel='',newEffort='',newMode='plan',newPermissionMode='default',newWt=true,
  newWtName=draftValue('new:worktree'),newMessage=draftValue('new:message'),spawnWait=null,spawnMsg='';
let spawnProvisional=null;
const DEFAULT_DIR='/Users/benjaminfeder/Programming/Quirk';
function spawnSnapshot(){
  return{provider:newProvider,cwd:newDir,model:newModel,effort:newEffort,mode:newMode,
    permission_mode:newProvider==='claude'?newPermissionMode:'',
    worktree:newProvider==='claude'&&newWt,
    worktree_name:newProvider==='claude'?newWtName:'',message:newMessage.trim()};
}
function provisionalSessionObject(){
  const p=spawnProvisional;if(!p)return null;
  const failed=p.status==='failed';
  return{session_id:p.id,provider:p.spec.provider,project:p.spec.cwd.split('/').filter(Boolean).pop()||'new session',
    title:'New coding session',cwd:p.spec.cwd,branch:p.spec.worktree_name||'',model:p.spec.model,
    effort:p.spec.effort,collaboration_mode:p.spec.mode,permission_mode:p.spec.permission_mode,
    permission_modes:['default','acceptEdits','plan'],ui_group:failed?'needs_you':'working',
    reason_label:failed?'Start failed':'Starting',state:failed?'idle':'running',reg_status:'starting',
    quiet_s:0,ctx_tokens:0,ctx_pct:null,total_tokens:0,cost:null,agent_cost:null,
    agents:[],agents_running:0,agents_total:0,last_msg:null,pending:null,provisional:true,
    error:p.error||'',capabilities:{submit:false,focus_terminal:false},access:'interactive'};
}
function sessionsWithProvisional(f){
  const sessions=[...((f&&f.sessions)||[])],provisional=provisionalSessionObject();
  if(provisional&&!sessions.some(item=>item.session_id===provisional.session_id))sessions.push(provisional);
  return sessions;
}
function provisionalCardTop(s){
  const p=spawnProvisional,failed=p?.status==='failed';
  return`<div class="shead" title="open startup details" onclick="sessionTap(event,'${s.session_id}')">
      <span class="chip ${failed?'problem':'working'}">${failed?'Start failed':'Starting'}</span>
      <span class="sname"><span class="stitle">New coding session</span><small>${esc(s.project)} · ${esc(s.provider)}</small></span>
      <span class="m amodel">${modelLabel(s)}</span>
    </div>
    ${p?.spec.message?`<div class="lastmsg"><span class="lmwho user">you</span><span class="lmtext">${esc(p.spec.message)}</span><span class="delivery ${failed?'failed':'sending'}" aria-label="${failed?'start failed':'starting session'}">${failed?'!':'◌'}</span></div>`:''}
    <div class="spawncardstate ${failed?'failed':''}">${failed?esc(p.error||'Session did not start'):`<span class="delivery sending" aria-hidden="true">◌</span> ${esc(p?.status==='discovering'?'Finding the new session…':'Starting session…')}`}</div>
    ${failed&&p?.canRetry?`<div class="spawncardactions"><button class="pbtn send" onclick="event.stopPropagation();retrySpawn()">retry</button><button class="pbtn" onclick="event.stopPropagation();restoreSpawnForm()">restore form</button></div>`:''}`;
}
function renderProvisionalSession(s){
  const p=spawnProvisional;if(!p)return;
  const failed=p.status==='failed';
  $('#stitle2').innerHTML=`<b>New coding session</b><small>${esc(s.project)} · ${esc(s.provider)}${s.model?` · ${esc(s.model)}`:''}</small>`;
  $('#sctrl').innerHTML='';renderEvidenceRail({});
  const message=p.spec.message?`<div class="cmsg user optimistic"><span class="crole">you</span>
      <span class="delivery ${failed?'failed':'sending'}" aria-label="${failed?'start failed':'starting session'}">${failed?'!':'◌'}</span>
      <div class="cbody"><p>${esc(p.spec.message).replace(/\n/g,'<br>')}</p></div></div>`:'';
  $('#sbody').innerHTML=`<div class="aconvo">${message}<div class="spawnstage ${failed?'failed':''}">
    ${failed?'!':`<span class="delivery sending" aria-hidden="true">◌</span>`}
    <div><b>${failed?'Session did not start':p.status==='discovering'?'Finding the new session…':'Starting session…'}</b>
    <span>${failed?esc(p.error||'Startup failed'):'Your message is saved here while Fleet waits for the exact native session.'}</span></div></div></div>`;
  $('#sact').classList.remove('session-composer','composer-active','tools-open');
  $('#sact').innerHTML=failed?`<div class="spawnrecovery">
    ${p.canRetry?`<button class="pbtn send" onclick="retrySpawn()">retry same session setup</button><button class="pbtn" onclick="restoreSpawnForm()">restore setup</button>`:
      p.serverSessionId?`<button class="pbtn send" onclick="keepWaitingForSpawn()">keep waiting</button>`:
      `<button class="pbtn" onclick="restoreSpawnForm()">restore setup</button>`}
    <span>${p.canRetry?'No native session was created.':p.serverSessionId?'Fleet has the exact session ID and can keep looking.':'The request outcome is unknown, so Fleet will not risk creating a duplicate.'}</span></div>`:'';
  requestAnimationFrame(()=>{const body=$('#sbody');body.scrollTop=body.scrollHeight;});
}
function restoreSpawnForm(){
  const p=spawnProvisional;if(!p)return;
  newProvider=p.spec.provider;newDir=p.spec.cwd;newModel=p.spec.model;newEffort=p.spec.effort;
  newMode=p.spec.mode;newPermissionMode=p.spec.permission_mode||'default';newWt=p.spec.worktree;
  newWtName=p.spec.worktree_name;newMessage=p.spec.message;
  setDraft('new:directory',newDir);setDraft('new:worktree',newWtName);setDraft('new:message',newMessage);
  const sid=p.id;spawnProvisional=null;spawnWait=null;newOpen=true;spawnMsg='';
  if(sessionView?.sid===sid)closeSession();
  render(last,true);
}
function keepWaitingForSpawn(){
  if(!spawnProvisional?.serverSessionId)return;
  spawnProvisional.status='discovering';spawnProvisional.error='';
  spawnWait={sessionId:spawnProvisional.serverSessionId,until:Date.now()+120000,
    provider:spawnProvisional.spec.provider,initialMessage:spawnProvisional.spec.message,
    provisionalId:spawnProvisional.id};render(last,true);
}
function retrySpawn(){
  if(!spawnProvisional?.canRetry)return;
  startSpawn(spawnProvisional.spec,spawnProvisional);
}
function changeNewProvider(value){
  newProvider=value;newModel='';spawnForecast=null;queueSpawnForecast(0);render(last,true);
}
function changeNewDirectory(value){newDir=value;setDraft('new:directory',value);queueSpawnForecast(120);}
function changeNewModel(value){newModel=value;queueSpawnForecast(0);}
function newSection(){
  const dirs=(last&&last.recent_dirs)||[];
  const staging=last?.instance?.mode==='staging',stagingSource=last?.instance?.source_root||'';
  if(staging&&stagingSource)newDir=stagingSource;
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
  const untrusted=!staging&&newDir&&(!cur||!cur.trusted);
  return`<div class="newform">
    <div class="nfhead">new session <button class="xbtn" onclick="newOpen=false;render(last,true)">✕</button></div>
    <label class="nflab">provider</label>
    <select class="nfsel" onchange="changeNewProvider(this.value)">
      <option value="claude" ${newProvider==='claude'?'selected':''}>Claude Code</option>
      <option value="codex" ${newProvider==='codex'?'selected':''}>Codex CLI</option>
    </select>
    ${staging?`<div class="nfwarn"><b>Isolated staging worktree</b><br>Fleet will create a new disposable branch and worktree from the staging checkout. Production sessions remain view only.</div>`:`
    <label class="nflab">directory</label>
    <select class="nfsel" onchange="changeNewDirectory(this.value)">
      <option value="">— pick a recent directory —</option>
      ${dirs.map(d=>`<option value="${esc(d.path)}" ${d.path===newDir?'selected':''}>${esc(d.path.replace(/^\/Users\/[^/]+/,'~'))}${d.trusted?'':' ⚠ untrusted'}</option>`).join('')}
    </select>
    <input class="nfin" data-draft-key="new:directory" placeholder="…or type a path (must be under ~)" value="${esc(dirs.some(d=>d.path===newDir)?'':newDir)}"
      oninput="changeNewDirectory(this.value)" autocomplete="off">`}
    ${newProvider==='claude'&&untrusted?`<div class="nfwarn">⚠ this folder isn't trusted yet — Claude Code will ask
      “do you trust the files in this folder?” at startup, and only your Mac can answer it.</div>`:''}
    <div class="nfrow">
      <div class="nfcol"><label class="nflab">model</label>
        <select class="nfsel" onchange="changeNewModel(this.value)">
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
      ${newProvider==='claude'?`<div class="nfcol"><label class="nflab">permission mode</label>
        <select class="nfsel" onchange="newPermissionMode=this.value">
          <option value="default" ${newPermissionMode==='default'?'selected':''}>Manual</option>
          <option value="auto" ${newPermissionMode==='auto'?'selected':''}>Auto</option>
          <option value="acceptEdits" ${newPermissionMode==='acceptEdits'?'selected':''}>Accept edits</option>
          <option value="plan" ${newPermissionMode==='plan'?'selected':''}>Plan</option>
          <optgroup label="Advanced"><option value="dontAsk" ${newPermissionMode==='dontAsk'?'selected':''}>Don't ask</option></optgroup>
        </select></div>`:''}
    </div>
    ${newProvider==='claude'&&!staging?`<label class="nfcheck"><input type="checkbox" ${newWt?'checked':''}
      onchange="newWt=this.checked;render(last,true)"><span>new git worktree</span></label>
    ${newWt?`<input class="nfin" data-draft-key="new:worktree" placeholder="worktree name (optional)" value="${esc(newWtName)}"
      oninput="newWtName=this.value" autocomplete="off">`:''}`:''}
    <label class="nflab">initial message <span style="text-transform:none;letter-spacing:0">(optional now, required to schedule)</span></label>
    <textarea class="nfin nfmessage" data-draft-key="new:message" maxlength="2000" placeholder="What should this session work on?" oninput="newMessage=this.value">${esc(newMessage)}</textarea>
    ${spawnForecastHtml()}
    <div class="nfactions"><button class="pbtn send nfgo" onclick="doSpawn()">start session ▸</button>
      <button class="pbtn sendoption nfgo" onclick="doScheduleNew()">schedule session</button></div>
    ${spawnMsg?`<div class="actmsg">${esc(spawnMsg)}</div>`:''}
  </div>
  <div class="dsep"></div>`;
}
async function doSpawn(){
  if(!newDir){spawnMsg='✗ pick a directory first';render(last,true);return;}
  if(spawnProvisional){openSession(spawnProvisional.id);return;}
  const feedbackStarted=performance.now();
  const spec=spawnSnapshot();
  const id='spawn-'+(globalThis.crypto?.randomUUID?.()||String(Date.now()));
  spawnProvisional={id,spec,status:'starting',error:'',canRetry:false,serverSessionId:null};
  spawnMsg='';newOpen=false;
  openSession(id);render(last,true);
  recordInputFeedback(feedbackStarted,'spawn');
  await startSpawn(spec,spawnProvisional);
}
async function startSpawn(spec,provisional){
  if(!provisional||spawnProvisional!==provisional)return;
  provisional.status='starting';provisional.error='';provisional.canRetry=false;render(last,true);
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type:'spawn',provider:spec.provider,cwd:spec.cwd,model:spec.model,effort:spec.effort,mode:spec.mode,
                           permission_mode:spec.permission_mode,
                           worktree:spec.worktree,worktree_name:spec.worktree_name,initial_text:spec.message||undefined})});
    const d=await r.json();
    if(spawnProvisional!==provisional)return;
    if(!r.ok||!d.ok){provisional.status='failed';provisional.error=d.error||'Session could not be started';
      provisional.canRetry=true;render(last,true);return;}
    clearDraft('new:directory','new:worktree','new:message');newMessage='';
    if(d.staging_workspace?.cwd)provisional.spec={...provisional.spec,cwd:d.staging_workspace.cwd,
      worktree:true,worktree_name:d.staging_workspace.worktree_name||''};
    provisional.serverSessionId=d.session_id;provisional.status='discovering';provisional.trustPrompt=!!d.trust_prompt;
    // Both providers return the exact native session identity. Never guess by cwd:
    // a sibling session in the same repo must not be opened by mistake.
    spawnWait={sessionId:d.session_id,until:Date.now()+120000,provider:spec.provider,
      initialMessage:spec.message,provisionalId:provisional.id};render(last,true);
  }catch(e){if(spawnProvisional===provisional){provisional.status='failed';
    provisional.error='Fleet lost the startup response: '+String(e.message||e);provisional.canRetry=false;render(last,true);}}
}
function doScheduleNew(){
  if(!newDir){spawnMsg='✗ pick a directory first';render(last,true);return;}
  if(!newMessage.trim()){spawnMsg='✗ add the message this new session should receive';render(last,true);return;}
  const spec={provider:newProvider,cwd:newDir,model:newModel,effort:newEffort,mode:newMode,
    permission_mode:newProvider==='claude'?newPermissionMode:'',
    worktree:newProvider==='claude'&&newWt,worktree_name:newProvider==='claude'?newWtName:''};
  openSchedule('',null,'',null,spec,newMessage);
}
// a spawned session only enters the fleet once it writes a transcript
async function checkSpawn(f){
  if(!spawnWait)return;
  if(Date.now()>spawnWait.until){
    if(spawnProvisional&&spawnProvisional.id===spawnWait.provisionalId){
      spawnProvisional.status='failed';spawnProvisional.error='The native session started, but Fleet could not discover it within two minutes.';
      spawnProvisional.canRetry=false;
    }
    spawnWait=null;render(last,true);return;
  }
  const s=(f.sessions||[]).find(x=>x.session_id===spawnWait.sessionId);
  if(s){const waiting=spawnWait,provisional=spawnProvisional;spawnWait=null;spawnMsg='';
    const wasOpen=sessionView?.sid===waiting.provisionalId;
    let optimisticId=null;
    if(waiting.initialMessage)optimisticId=addOptimistic(s.session_id,waiting.initialMessage,'text');
    spawnProvisional=null;
    if(wasOpen){sessionView={sid:s.session_id,closed:false};sessionOpened=false;}
    render(last,true);
    if(waiting.provider==='claude'&&waiting.initialMessage){
      const delivered=await act(s.session_id,{type:'text',text:waiting.initialMessage},'spawnmsg',optimisticId);
      if(!delivered.ok)spawnMsg='✗ session started, but the initial message failed: '+(delivered.error||'failed');
    }else if(optimisticId)updateOptimistic(s.session_id,optimisticId,true,'',true);
    if(!wasOpen)openSession(s.session_id);
  }
}
function historySection(f){
  const items=historyItems(f);
  if(!items.length&&!historyLoading&&historyData.ok&&historyData.next_cursor==null)
    return'<div class="destinationempty"><span>↺</span><b>No session history</b><p>Inactive and closed sessions will appear here.</p></div>';
  return`<div class="historybox">
    <div class="historycount">${historyCount(f)}</div>
    <div class="historytools">
      <div class="freetext"><input data-draft-key="filter:history" placeholder="Filter by title, project, branch, provider, or state"
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
  if(!items.length&&!historyLoading)return`<div class="empty">${historyData.ok?'no matches':
    `${esc(historyData.error||'history unavailable')} <button onclick="loadHistory(true)">retry</button>`}</div>`;
  return items.map(item=>historyRow(item)).join('')+
    (historyLoading?'<div class="ctxload">loading history…</div>':'')+
    (!historyLoading&&!historyData.ok?`<div class="ctxload searcherror">✗ ${esc(historyData.error||'history unavailable')} <button onclick="loadHistory(${items.length?'false':'true'})">retry</button></div>`:'')+
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
      <button class="spin${pinnedSessions.has(sid)?' on':''}" ${pinActions.get(sid)?.busy?'disabled':''} aria-label="${pinnedSessions.has(sid)?'unpin session':'pin session'}"
        title="${pinnedSessions.has(sid)?'unpin session':'pin session'}"
        onclick="event.stopPropagation();toggleSessionPin(decodeURIComponent('${encoded}'))">📌</button>
    </div>
    ${pinFeedbackHtml(sid)}
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
let spawnForecastAbort=null,spawnForecastSequence=0;
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

let insightsDays=7;
const insightsCache={};   // days -> {t, data, fetching}
let insightsSequence=0;
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
  }else if(c?.error){body=`<div class="ctxload" role="alert">✗ ${esc(c.error)} <button onclick="loadInsights(true)">retry</button></div>`;}
  else if(d&&!d.ok){body=`<div class="ctxload">✗ ${esc(d.error||'failed')}</div>`;}
  return`<div class="insightspanel">${body}</div>`;
}

let last=null;
let pollSequence=0,pollApplied=0,pollController=null;
let fleetOffline=false;
function applyInstance(instance){
  const staging=instance?.mode==='staging',name=instance?.name||(staging?'Fleet Staging':'Fleet Dash');
  document.documentElement.dataset.instance=staging?'staging':'production';
  const banner=$('#instancebanner');
  if(banner){banner.hidden=!staging;banner.textContent=staging?
    'STAGING · production sessions are view only · controls work only on staging test sessions':'';}
  const brand=document.querySelector('.brand b');if(brand)brand.textContent=name;
  const apple=document.querySelector('meta[name="apple-mobile-web-app-title"]');if(apple)apple.content=name;
  return name;
}
function setFleetOffline(offline){
  fleetOffline=Boolean(offline);document.documentElement.dataset.offline=fleetOffline?'true':'false';
  const stale=$('#stale');if(!stale)return;
  if(fleetOffline){
    stale.textContent='offline — showing the last local snapshot; drafts are saved and messages can queue until reconnection';
    stale.style.display='block';
  }
}
function render(f,force){
  if(!f||!f.sessions)return;
  const renderStarted=performance.now();
  const instanceName=applyInstance(f.instance);
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
  const activeAgentCount=activeSubagents(f).length;
  const provisional=provisionalSessionObject();
  const nowCounts={needs_you:(t.needs_me||0)+(provisional?.ui_group==='needs_you'?1:0),
    working:(t.busy||0)+(provisional?.ui_group==='working'?1:0),available:t.available||0,
    subagents:activeAgentCount};
  const nowLabels={all:'All',needs_you:'Needs you',working:'Working',available:'Available',subagents:'Subagents'};
  document.querySelectorAll('[data-now-filter]').forEach(button=>{
    const active=button.dataset.nowFilter===nowState;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
    button.textContent=nowLabels[button.dataset.nowFilter]+(button.dataset.nowFilter==='all'?'':` · ${nowCounts[button.dataset.nowFilter]||0}`);
  });
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
    if(nowState==='subagents'){
      ['#pinned','#actioninbox','#needsyou','#outboxsummary','#working','#sessions'].forEach(selector=>{
        const element=$(selector);if(element){element.innerHTML='';element.className=selector==='#pinned'?'empty':'';}
      });
      renderActiveSubagents(f);
    }else{
      const subagents=$('#subagents');if(subagents){subagents.innerHTML='';subagents.className='';}
      const unpinned=sessionsWithProvisional(f).filter(s=>!pinnedSessions.has(s.session_id)&&matchesNow(s));
      const inboxSessionIds=new Set((f.actions||[]).filter(action=>!pinnedSessions.has(action.session_id))
        .map(action=>action.session_id));
      renderPinned(f,matchesNow);
      const visibleInboxSessionIds=renderActionInbox(f);
      renderQueue($('#needsyou'),unpinned.filter(s=>s.ui_group==='needs_you'&&!visibleInboxSessionIds.has(s.session_id)),
        'Needs You','sessions waiting for your response','needs');
      renderOutboxCompact();
      renderQueue($('#working'),unpinned.filter(s=>s.ui_group==='working'),
        'Working','turns in progress','working');
      renderQueue($('#sessions'),unpinned.filter(s=>s.ui_group==='available'&&!inboxSessionIds.has(s.session_id)),
        'Available','ready for another message','available',true);
    }
    if(!typingNew)$('#newsess').innerHTML=newSection();
    if(!typingHistory)$('#history').innerHTML=historySection(f);
    if(currentRoute==='workstreams')renderWorkstreams(workstreamData);
    if(currentRoute==='notifications')renderNotifications();
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
  const titleCount=Math.max(Number(t.needs_me)||0,Number(notificationData.active)||0,Number(notificationData.unread)||0);
  document.title=(titleCount?`(${titleCount}) `:'')+instanceName;
  perfRecord('render_ms',performance.now()-renderStarted);
}

async function tick(){
  const pollStarted=performance.now(),sequence=++pollSequence;
  if(pollController)pollController.abort();
  const controller=new AbortController();pollController=controller;
  try{
    const r=await fetch('/api/fleet',{cache:'no-store',signal:controller.signal});
    const payload=Number(r.headers.get('X-Fleet-Payload-Bytes')||r.headers.get('Content-Length'));
    if(Number.isFinite(payload))perfRecord('poll_payload_bytes',payload);
    const next=await r.json();
    if(!r.ok)throw new Error(next.error||`Fleet returned ${r.status}`);
    if(sequence<pollApplied||sequence!==pollSequence)return;
    pollApplied=sequence;last=next;
    if(last.page_v){if(window.__pv&&window.__pv!==last.page_v)return location.reload();window.__pv=last.page_v;}
    setFleetOffline(r.headers.get('X-Fleet-Offline')==='1');
    if(!fleetOffline)$('#stale').style.display='none';
    try{render(last);}
    catch(renderError){
      console.error('Fleet Dash render failed; server remains reachable',renderError);
      const stale=$('#stale');
      stale.textContent='display error — server is still reachable; showing the last usable screen';
      stale.style.display='block';
      return;
    }
    if(currentRoute==='now'||$('#outboxview').style.display==='flex')loadOutbox();
    if(notificationPollingEnabled)loadNotifications(true);
    if(currentRoute==='notifications'&&notificationSection==='briefing')loadBriefing();
    if(currentRoute==='insights')loadBudgets();
    if(currentRoute==='history'&&Date.now()-historyLoadedAt>5000&&!historyLoading)loadHistory(true);
    if(!fleetOffline)void flushOfflineMessages();
  }catch(e){if(e.name!=='AbortError'&&sequence===pollSequence){console.error('Fleet Dash render/poll failed',e);setFleetOffline(true);}}
  finally{if(pollController===controller)pollController=null;if(sequence===pollSequence)perfRecord('poll_ms',performance.now()-pollStarted);}
}
$('#nowfilter').value=nowFilter;
$('#searchquery').value=draftValue('filter:search');
$('#workfilter').value=workFilter;
navigateTo(currentRoute,false,Boolean(notificationDetailId));
tick().finally(()=>{
  const start=()=>initFleetPwa();
  if('requestIdleCallback' in window)requestIdleCallback(start,{timeout:2000});
  else setTimeout(start,250);
});
setInterval(tick,2000);
window.addEventListener('online',()=>tick());
setInterval(()=>{if(currentRoute==='search')loadSearchStatus();},5000);
setInterval(()=>{if(currentRoute==='workstreams')loadWorkstreams();},8000);
fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"type":"ping"}'})
  .then(r=>{notificationPollingEnabled=r.status!==403;$('#notoken').style.display=r.status===403?'block':'none';
    if(notificationPollingEnabled)loadNotifications(true);}).catch(()=>{});
