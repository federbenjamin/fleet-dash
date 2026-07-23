const $=q=>document.querySelector(q);
const DRAFT_STORE_KEY='fleet.drafts.v1';
const OFFLINE_MESSAGE_STORE_KEY='fleet.offlineMessages.v1';
const OUTBOX_RECEIPT_STORE_KEY='fleet.outboxReceipts.v1';
const OUTBOX_RESOLVED_STORE_KEY='fleet.outboxResolved.v1';
const CONTEXT_STORE_KEY='fleet.contextCache.v1';
const IMAGE_DRAFT_STORE_KEY='fleet.imageDrafts.v1';
const QUESTION_PANEL_STORE_KEY='fleet.questionPanels.v1';
const WORKSPACE_SPLIT_STORE_KEY='fleet.workspaceSplits.v1';
const IMAGE_DB_NAME='fleet-images-v1',IMAGE_STORE='images';
const IMAGE_MAX_BYTES=10*1024*1024,IMAGE_MAX_COUNT=4,IMAGE_TTL_MS=24*60*60*1000;
let draftStore=(()=>{try{
  const value=JSON.parse(localStorage.getItem(DRAFT_STORE_KEY)||'{}');
  return value&&typeof value==='object'&&!Array.isArray(value)?value:{};
}catch(_){return {};}})();
let workspaceSplitWidths=(()=>{try{
  const value=JSON.parse(localStorage.getItem(WORKSPACE_SPLIT_STORE_KEY)||'{}');
  return Object.fromEntries(['files','subagents'].map(kind=>[kind,
    Math.max(220,Math.min(520,Number(value?.[kind])||300))]));
}catch(_){return{files:300,subagents:300};}})();
function persistWorkspaceSplits(){try{
  localStorage.setItem(WORKSPACE_SPLIT_STORE_KEY,JSON.stringify(workspaceSplitWidths));
}catch(error){console.warn('Fleet could not persist workspace divider widths',error);}}
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
      baseCount:Math.max(0,Number(item.baseCount)||0),
      dismissNonce:typeof item.dismissNonce==='string'&&item.dismissNonce.length<=500?
        item.dismissNonce:null,
      state:item.state==='confirmation_unknown'?'confirmation_unknown':'queued',
      error:typeof item.error==='string'?item.error.slice(0,500):''}));
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
let outboxReceipts=(()=>{try{
  const value=JSON.parse(localStorage.getItem(OUTBOX_RECEIPT_STORE_KEY)||'{}');
  if(!value||typeof value!=='object'||Array.isArray(value))return{};
  return Object.fromEntries(Object.entries(value).filter(([id,item])=>
    /^[A-Za-z0-9_.:-]{1,200}$/.test(id)&&item&&typeof item.sid==='string'&&
    typeof item.text==='string').slice(-100).map(([id,item])=>[id,{
      outboxId:id,sid:item.sid.slice(0,200),text:item.text.slice(0,30000),kind:'text',
      imageIds:(Array.isArray(item.imageIds)?item.imageIds:[]).filter(value=>
        /^[A-Za-z0-9_-]{1,100}$/.test(String(value))).slice(0,IMAGE_MAX_COUNT).map(String),
      imageCount:Math.max(0,Number(item.imageCount)||0),baseCount:Math.max(0,Number(item.baseCount)||0),
      created:Math.max(0,Number(item.created)||Date.now()),status:['queued','confirmed','failed'].includes(item.status)?item.status:'queued',
      queueLabel:typeof item.queueLabel==='string'?item.queueLabel.slice(0,200):'',
      queueReason:typeof item.queueReason==='string'?item.queueReason.slice(0,500):'',
      error:typeof item.error==='string'?item.error.slice(0,500):''}]))
}catch(_){return{}}})();
function persistOutboxReceipts(){try{
  const rows=Object.values(outboxReceipts).sort((a,b)=>a.created-b.created).slice(-100);
  outboxReceipts=Object.fromEntries(rows.map(item=>[item.outboxId,item]));
  if(rows.length)localStorage.setItem(OUTBOX_RECEIPT_STORE_KEY,JSON.stringify(outboxReceipts));
  else localStorage.removeItem(OUTBOX_RECEIPT_STORE_KEY);
}catch(error){console.warn('Fleet could not persist queued-send receipts',error);}}
function rememberOutboxReceipt(item){
  if(!item?.outboxId)return;
  outboxReceipts[item.outboxId]={outboxId:item.outboxId,sid:item.sid,text:item.text,kind:'text',
    imageIds:[...(item.imageIds||[])],imageCount:item.imageCount||0,baseCount:item.baseCount||0,
    created:item.created||Date.now(),status:item.status||'queued',queueLabel:item.queueLabel||'',
    queueReason:item.queueReason||'',error:item.error||''};
  persistOutboxReceipts();
}
function forgetOutboxReceipt(id){if(id&&outboxReceipts[id]){delete outboxReceipts[id];persistOutboxReceipts();}}
let resolvedOutboxReceipts=(()=>{try{
  const value=JSON.parse(localStorage.getItem(OUTBOX_RESOLVED_STORE_KEY)||'{}');
  if(!value||typeof value!=='object'||Array.isArray(value))return{};
  const cutoff=Date.now()-30*24*60*60*1000;
  return Object.fromEntries(Object.entries(value).filter(([id,at])=>
    /^[A-Za-z0-9_.:-]{1,200}$/.test(id)&&Number(at)>=cutoff).slice(-200));
}catch(_){return{};}})();
function persistResolvedOutboxReceipts(){try{
  const cutoff=Date.now()-30*24*60*60*1000;
  const rows=Object.entries(resolvedOutboxReceipts).filter(([,at])=>Number(at)>=cutoff)
    .sort((a,b)=>Number(a[1])-Number(b[1])).slice(-200);
  resolvedOutboxReceipts=Object.fromEntries(rows);
  if(rows.length)localStorage.setItem(OUTBOX_RESOLVED_STORE_KEY,JSON.stringify(resolvedOutboxReceipts));
  else localStorage.removeItem(OUTBOX_RESOLVED_STORE_KEY);
}catch(error){console.warn('Fleet could not persist resolved delivery receipts',error);}}
function resolveOutboxReceipt(id){if(!id)return;
  resolvedOutboxReceipts[id]=Date.now();forgetOutboxReceipt(id);persistResolvedOutboxReceipts();}
let persistedContexts=(()=>{try{
  const value=JSON.parse(localStorage.getItem(CONTEXT_STORE_KEY)||'{}');
  return value&&typeof value==='object'&&!Array.isArray(value)?value:{};
}catch(_){return{}}})();
const contextStoreKey=(scope,sid,aid='')=>`${scope}:${sid}:${aid}`;
function savedConversation(scope,sid,aid=''){
  const saved=persistedContexts[contextStoreKey(scope,sid,aid)];
  if(!saved||!Array.isArray(saved.messages))return null;
  return{...saved,messages:[...saved.messages],files:[...(saved.files||[])],agents:[...(saved.agents||[])],
    info:{...(saved.info||{})},stale:true};
}
function persistConversation(scope,sid,aid,cache){
  if(!cache||!Array.isArray(cache.messages))return;
  const key=contextStoreKey(scope,sid,aid),entry={v:cache.v,messages:cache.messages,
    files:cache.files||[],agents:cache.agents||[],closed:Boolean(cache.closed),
    info:cache.info||{},next_cursor:cache.next_cursor,
    message_total:cache.message_total,saved:Date.now()};
  persistedContexts[key]=entry;
  let rows=Object.entries(persistedContexts).sort((a,b)=>(b[1].saved||0)-(a[1].saved||0)).slice(0,18);
  while(rows.length){
    const value=Object.fromEntries(rows);
    try{const encoded=JSON.stringify(value);if(encoded.length<=2800000){
      localStorage.setItem(CONTEXT_STORE_KEY,encoded);persistedContexts=value;return;
    }}catch(_){}
    rows.pop();
  }
  persistedContexts={};try{localStorage.removeItem(CONTEXT_STORE_KEY);}catch(_){}
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
let pendingActToken='';
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
const open=new Set();
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
let pendingWorkspaceRoute=parseSessionHash();
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
let currentRoute=initialDestination.route,
  notificationDetailId=initialDestination.route==='notifications'?initialDestination.detail:null;
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
let searchTimer=null,searchAbort=null,searchCursor=0,searchBusy=false;
let searchItems=[],searchProjects=[],searchStatusData=null,searchStatusAt=0,searchError='';
let searchView=null;
const searchFilters={query:draftValue('filter:search'),provider:'',kind:'',project:''};
function syncSearchControls(){
  const controls={query:$('#searchquery'),provider:$('#searchprovider'),kind:$('#searchkind'),
    project:$('#searchproject')};
  for(const [key,control] of Object.entries(controls))if(control&&control.value!==searchFilters[key])
    control.value=searchFilters[key];
}
function setSearchQuery(value){
  searchFilters.query=String(value??'');
  // The delegated draft listener normally persisted this input first. Keep
  // direct callers correct without serializing localStorage twice per keypress.
  if(draftValue('filter:search')!==searchFilters.query)setDraft('filter:search',searchFilters.query);
  queueSearch(true);
}
function setSearchFilter(key,value){
  if(!['provider','kind','project'].includes(key))return;
  searchFilters[key]=String(value??'');
  // Same-document browser history can restore an older form value after a
  // search-result overlay closes. Reassert the canonical controls before the
  // dependent request so a rapid second action cannot revive stale criteria.
  syncSearchControls();runSearch(true);
}
function queueSearch(reset){clearTimeout(searchTimer);searchTimer=setTimeout(()=>runSearch(reset),180);}
function searchParams(cursor){
  const params=new URLSearchParams({q:searchFilters.query,provider:searchFilters.provider,
    kind:searchFilters.kind,project:searchFilters.project,cursor:String(cursor||0),limit:'30'});
  return params.toString();
}
function searchHasCriteria(){return Boolean(searchFilters.query.trim()||searchFilters.provider||
  searchFilters.kind||searchFilters.project);}
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
  const current=searchFilters.project;
  select.innerHTML='<option value="">All</option>'+searchProjects.map(project=>
    `<option value="${esc(project)}">${esc(project)}</option>`).join('');
  if(searchProjects.includes(current))select.value=current;
  else{searchFilters.project='';select.value='';}
}
async function runSearch(reset=true){
  if(!$('#searchresults'))return;
  syncSearchControls();
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
  if(source.source_kind==='artifact'&&source.file_id&&active)
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
    viewFile(encodeURIComponent(source.session_id),encodeURIComponent(source.file_id));
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
// replaced node kills wheel momentum: "scroll stops after a second"); a press
// holds them 800ms so the 2s tick can't detach a button between the press and
// its click event (detached node = swallowed click). User-action renders pass
// force=true and bypass the guard — tap feedback must paint immediately.
// `pointerdown` covers mouse and pen as well as touch, and it is load-bearing
// for the composer specifically: mousedown moves focus off the textarea, which
// drops the `typing` guard that was the only thing keeping #sact alive, so a
// tick landing between press and click silently swallowed the send.
let lastMove=0,lastTap=0;
document.addEventListener('touchstart',()=>{lastTap=Date.now()},{passive:true});
document.addEventListener('pointerdown',()=>{lastTap=Date.now()},{passive:true});
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
const USAGE_GAP=8;      // the small gap between the button and the panel
const USAGE_EDGE=8;     // keep this much clear of every viewport edge
// The panel's top-left pins to the Usage button's bottom-left on desktop AND
// mobile. It is position:fixed, so the coordinates are remeasured whenever the
// button can move under it (scroll, resize, keyboard-driven viewport changes).
function positionUsagePanel(){
  const panel=$('#usagepanel'),chip=$('#usagechip');
  if(!panel||!chip||!usageOpen)return;
  const button=chip.getBoundingClientRect();
  const vw=window.innerWidth,vh=window.innerHeight;
  // shrink to whatever fits to the button's right, so the left edges can line
  // up; only a panel narrower than the floor gets nudged left of the button
  const floor=Math.min(300,vw-2*USAGE_EDGE);
  const width=Math.max(floor,Math.min(520,vw-button.left-USAGE_EDGE));
  panel.style.setProperty('--usage-width',`${Math.round(width)}px`);
  const left=Math.max(USAGE_EDGE,Math.min(button.left,vw-width-USAGE_EDGE));
  const top=Math.max(USAGE_EDGE,button.bottom+USAGE_GAP);
  panel.style.setProperty('--usage-left',`${Math.round(left)}px`);
  panel.style.setProperty('--usage-top',`${Math.round(top)}px`);
  panel.style.setProperty('--usage-maxh',`${Math.max(120,Math.round(vh-top-USAGE_EDGE))}px`);
}
function closeUsage(){
  usageOpen=false;
  $('#usagepanel')?.classList.remove('open');
  $('#usagechip')?.setAttribute('aria-expanded','false');
}
function toggleUsage(){
  usageOpen=!usageOpen;
  $('#usagepanel')?.classList.toggle('open',usageOpen);
  $('#usagechip')?.setAttribute('aria-expanded',String(usageOpen));
  // measure only once it is displayed, or offsetWidth is 0
  if(usageOpen)positionUsagePanel();
}
addEventListener('scroll',()=>positionUsagePanel(),true);
addEventListener('resize',()=>positionUsagePanel());
window.visualViewport?.addEventListener('resize',()=>positionUsagePanel());
window.visualViewport?.addEventListener('scroll',()=>positionUsagePanel());
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
  const activeClaude=claudeProfiles.find(profile=>profile?.active) || claudeProfiles.find(Boolean);
  const claudeWindows=activeClaude?[activeClaude.five_hour_pct,
    claude?.show_week===false?null:activeClaude.weekly_pct]
    .filter(value=>Number.isFinite(Number(value))).map(value=>Math.round(Number(value))):[];
  const activeCodex=codexBuckets.map(bucket=>Number(bucket.used_pct))
    .filter(Number.isFinite).sort((a,b)=>b-a)[0];
  const summaries=[];
  if(claudeWindows.length)summaries.push(`Claude ${claudeWindows.join('/')}`);
  if(Number.isFinite(activeCodex))summaries.push(`Codex ${Math.round(activeCodex)}`);
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
const outboxPending=new Set(['scheduled','waiting_availability','waiting_usage_reset','waiting_provider','spawning','sending']);
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
      outboxAccess='allowed';outboxData=data;outboxLoadedAt=Date.now();reconcileOutboxOptimistic();
    }catch(error){outboxData={...outboxData,ok:false,error:String(error)};}
    finally{outboxLoading=false;outboxLoadPromise=null;renderOutboxCompact();renderOutboxFull();}})();
  await outboxLoadPromise;return outboxData;
}
function reconcileOutboxOptimistic(){
  const rows=new Map((outboxData.items||[]).map(item=>[item.id,item]));
  for(const id of Object.keys(resolvedOutboxReceipts))forgetOutboxReceipt(id);
  for(const [sid,list] of optimisticMessages)
    optimisticMessages.set(sid,list.filter(item=>!item.outboxId||!resolvedOutboxReceipts[item.outboxId]));
  for(const row of rows.values()){
    if(resolvedOutboxReceipts[row.id]||outboxReceipts[row.id]||row.origin!=='automatic_fallback'||
       ![...outboxPending,...outboxAttention].includes(row.state)||!row.target_session_id||!row.message)continue;
    outboxReceipts[row.id]={outboxId:row.id,sid:row.target_session_id,text:row.message,kind:'text',
      imageIds:[],imageCount:Math.max(0,Number(row.image_count)||0),
      baseCount:canonicalCount(ctxCache[row.target_session_id]?.messages||[],{kind:'text',text:row.message}),
      created:Number(row.created_at)*1000||Date.now(),status:outboxPending.has(row.state)?'queued':'failed',
      queueLabel:row.state_label||'Queued · waiting for session',queueReason:row.blocked_reason||'',
      error:row.error||row.blocked_reason||''};
  }
  for(const list of optimisticMessages.values())for(const item of list){
    if(!item.outboxId)continue;
    const row=rows.get(item.outboxId);if(!row)continue;
    if(outboxPending.has(row.state)){
      clearTimeout(item.confirmTimer);item.status='queued';
      item.queueReason=row.blocked_reason||row.state_label||'Queued until provider control reconnects';
      item.queueLabel=row.state==='waiting_provider'?'Queued · waiting for connection':
        'Queued · waiting for session';
    }else if(row.state==='sent'){
      clearTimeout(item.confirmTimer);item.status='confirmed';delete item.error;
      if(item.imageIds?.length){void deleteImages(item.imageIds);item.imageIds=[];}
    }else if(['blocked','failed','confirmation_unknown','cancelled'].includes(row.state)){
      clearTimeout(item.confirmTimer);item.status=row.state==='confirmation_unknown'?'uncertain':'failed';
      item.error=row.error||row.blocked_reason||(row.state==='cancelled'?'Queued send was cancelled.':'Queued send failed.');
    }
    rememberOutboxReceipt(item);
  }
  persistOutboxReceipts();
  uiRefresh();
}
function renderOutboxCompact(){
  const el=$('#outboxsummary');if(!el)return;
  const summary=outboxData.summary||last?.outbox_summary||{};
  const current=(outboxData.items||[]).filter(item=>!item.cancelled&&
    (outboxPending.has(item.state)||outboxAttention.has(item.state)));
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
  if(outboxFilter==='pending')return items.filter(item=>!item.cancelled&&outboxPending.has(item.state));
  if(outboxFilter==='attention')return items.filter(item=>!item.cancelled&&outboxAttention.has(item.state));
  if(outboxFilter==='sent')return items.filter(item=>!item.cancelled&&item.state==='sent');
  if(outboxFilter==='cancelled')return items.filter(item=>item.cancelled);
  if(outboxFilter==='all')return items;
  return items.filter(item=>!item.cancelled&&
    (outboxPending.has(item.state)||outboxAttention.has(item.state)));
}
function outboxRow(item){
  const action=outboxActions.get(item.id)||{};
  const error=action.error||item.error||item.blocked_reason;
  const canEdit=item.editable,canRetry=item.retryable;
  const rowState=item.cancelled?'cancelled':item.state,stateLabel=item.cancelled?'Cancelled':(item.state_label||item.state);
  return`<article class="outboxrow ${esc(rowState)}"><div class="outboxtop"><span class="outboxstate">${esc(stateLabel)}</span>
    <span class="outboxtime">${esc(outboxWhen(item))}</span></div><div class="outboxmessage">${esc(item.message||'')}</div>
    <div class="outboxmeta">${esc(outboxTarget(item))} · ${esc(String(item.kind||'').replaceAll('_',' '))}${item.created_zone?` · ${esc(item.created_zone)}`:''}${item.cancelled_from_label?` · was ${esc(item.cancelled_from_label)}`:''}</div>
    ${error?`<div class="outboxerror" role="alert">${esc(error)}</div>`:''}<div class="outboxactions">
      ${action.busy?'<span class="outboxworking" role="status"><span class="delivery sending" aria-hidden="true">◌</span> working…</span>':''}
      ${canEdit?`<button ${action.busy?'disabled':''} onclick="editOutbox('${item.id}')">Edit</button><button class="primary" ${action.busy?'disabled':''} onclick="outboxAction('${item.id}','outbox_send_now')">Send now</button>`:''}
      ${canRetry?`<button class="primary" ${action.busy?'disabled':''} onclick="editOutbox('${item.id}','retry')">Retry / retarget</button>`:''}
      ${item.deletable?`<button class="outboxdelete" ${action.busy?'disabled':''} onclick="confirmDeleteOutbox('${item.id}')">Delete</button>`:''}
    </div></article>`;
}
function renderOutboxFull(){
  const el=$('#outboxbody');if(!el||$('#outboxview').style.display!=='flex')return;
  if(outboxLoading&&!outboxData.items?.length){el.innerHTML='<div class="ctxload">Loading Outbox…</div>';return;}
  if(!outboxData.ok){el.innerHTML=`<div class="outboxempty">${esc(outboxData.error||'Outbox unavailable')}</div>`;return;}
  const items=visibleOutboxItems();
  el.innerHTML=`<div class="outboxlayout"><div class="outboxtools">${[['current','Current'],['pending','Pending'],['attention','Needs review'],['sent','Sent'],['cancelled','Cancelled'],['all','All']].map(([value,label])=>
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
    if(type==='outbox_delete')resolveOutboxReceipt(id);
    mergeOutboxResult(result);
    await loadOutbox(true);
    outboxActions.delete(id);renderOutboxFull();return result;
  }catch(error){
    const message=String(error.message||error);
    outboxActions.set(id,{busy:false,error:message});renderOutboxFull();
    return{ok:false,error:message};
  }
}
function confirmDeleteOutbox(id){
  const item=(outboxData.items||[]).find(row=>row.id===id),pending=item&&outboxPending.has(item.state);
  askConfirm('Delete this Outbox message?',pending?
    'It will be cancelled, moved to Cancelled, and never sent.':
    'It will move to Cancelled. Its original delivery outcome remains in the audit record.',
    'delete message',()=>outboxAction(id,'outbox_delete'));
}
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
    syncComposerToolsOpen(menu.closest('.session-composer'));
  });
}
function syncComposerToolsOpen(root){if(root)root.classList.toggle('tools-open',
  Boolean(root.querySelector('.composerplus.open,.slashmenu')));}
function toggleComposerMenu(button,event){
  event?.stopPropagation();
  const menu=button?.nextElementSibling;if(!menu)return;
  const opening=!menu.classList.contains('open');
  closeComposerMenus(opening?menu:null);
  menu.classList.toggle('open',opening);button.setAttribute('aria-expanded',String(opening));
  syncComposerToolsOpen(menu.closest('.session-composer'));
}
function openComposerSchedule(sid,inputId,agentId='',event=null){
  event?.preventDefault();event?.stopPropagation();
  openSchedule(sid,inputId,agentId);
  closeComposerMenus();
}
function composerTools(sid,inputId){
  return`<span class="composertools"><button type="button" class="pbtn composerplusbtn" aria-label="message options" aria-haspopup="menu" aria-expanded="false" onclick="toggleComposerMenu(this,event)">＋</button>
    <span class="composerplus" role="menu">
      <label class="composerplusitem composerphotoitem" role="menuitem">▧ <span>Send picture</span>
        <input id="picker-${inputId}" class="composer-file-input" type="file" aria-label="Send picture"
          accept="image/jpeg,image/png,image/gif,image/webp,image/heic,image/heif,.heic,.heif" multiple
          onchange="chooseImages('${sid}',this)" oncancel="closeComposerMenus()"></label>
      <button type="button" class="composerplusitem" role="menuitem" onclick="openComposerSchedule('${sid}','${inputId}','',event)">◷ <span>Schedule message</span></button>
    </span></span>`;
}
function canCompose(s){const caps=s?.capabilities||{};return Boolean(caps.submit||caps.queue_submit);}
function renderComposer(s,surface='session'){
  if(!canCompose(s))return'';
  const viewer=surface==='viewer',pre=viewer?'vft':'sft',msg=viewer?'vmsg':'smsg';
  const inputId=pre+'-'+s.session_id;
  return`<div class="composer-dock" data-composer-surface="${surface}">
    <div class="image-drafts" id="imgdraft-${s.session_id}"></div>
    <div class="slashwrap" id="slash-${inputId}"></div>
    <div class="actmsg" id="${msg}-${s.session_id}"></div>
    <div class="freetext composer">${composerTools(s.session_id,inputId)}<textarea id="${inputId}" data-draft-key="${esc(composerDraftKey(s.session_id))}" rows="1" placeholder="send message" autocomplete="off"
      oninput="composerInput(this,'${s.session_id}','${pre}')" onfocus="composerFocus(this,'${s.session_id}','${pre}')"
      onkeydown="composerKey(event,()=>sendText('${s.session_id}','${pre}','${msg}'));if(event.key==='Escape')slashClose()">${esc(draftValue(composerDraftKey(s.session_id)))}</textarea>
      <button class="pbtn send" onclick="sendText('${s.session_id}','${pre}','${msg}')">send</button></div></div>`;
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
    fold:source.trigger_fold,choices:null,usageKey:'',spawnSpec:scheduleSpawn,busy:false,
    expectedVersion:Math.max(0,Number(source.version)||0),
    clientRequestId:source.idempotency_key||`schedule-${offlineMessageId()}`};
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
function closeSchedule(){$('#scheduleview').style.display='none';$('#schedulebody').innerHTML='';scheduleView=null;
  // popstate can move focus to <body> before MutationObserver delivery. Apply
  // the lower-dialog focus restoration in the same close transaction.
  syncModalStack();}
function scheduleSet(key,value){if(!scheduleView)return;scheduleView[key]=value;if(key==='sid')scheduleView.agentId='';renderSchedule();}
function scheduleSpawnChange(key,value){
  if(!scheduleView?.spawnSpec)return;
  scheduleView.spawnSpec[key]=value;
  if(key==='provider'){
    scheduleView.spawnSpec.model='';scheduleView.spawnSpec.effort='';
    if(value==='codex'){
      scheduleView.spawnSpec.mode=scheduleView.spawnSpec.mode||'plan';
      scheduleView.spawnSpec.permission_mode='';scheduleView.spawnSpec.worktree=false;
      scheduleView.spawnSpec.worktree_name='';
    }else scheduleView.spawnSpec.permission_mode=scheduleView.spawnSpec.permission_mode||'default';
  }
  repairSpawnSelection(scheduleView.spawnSpec);
  renderSchedule();
}
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
    `<label class="nflab">provider</label><select class="nfsel" onchange="scheduleSpawnChange('provider',this.value)"><option value="claude" ${spawn.provider==='claude'?'selected':''}>Claude Code</option><option value="codex" ${spawn.provider==='codex'?'selected':''}>Codex CLI</option></select>
     <label class="nflab">directory</label><input class="nfin" data-draft-key="${esc(v.draftKey+':cwd')}" value="${esc(spawn.cwd||'')}" oninput="scheduleView.spawnSpec.cwd=this.value">
     <div class="nfrow"><div class="nfcol"><label class="nflab">model</label><select class="nfsel" onchange="scheduleSpawnChange('model',this.value)"><option value="">default</option>${spawnCatalog(spawn.provider).map(item=>`<option value="${esc(item.id)}" ${spawn.model===item.id?'selected':''}>${esc(item.name)}</option>`).join('')}</select></div>
     <div class="nfcol"><label class="nflab">effort</label><select class="nfsel" onchange="scheduleSpawnChange('effort',this.value)"><option value="">default</option>${spawnEfforts(spawn.provider,spawn.model).map(value=>`<option value="${esc(value)}" ${spawn.effort===value?'selected':''}>${esc(value)}</option>`).join('')}</select></div></div>
     ${spawn.provider==='codex'?`<label class="nflab">mode</label><select class="nfsel" onchange="scheduleView.spawnSpec.mode=this.value"><option value="plan" ${spawn.mode==='plan'?'selected':''}>Plan</option><option value="default" ${spawn.mode==='default'?'selected':''}>Default</option></select>`:
       `<label class="nfcheck"><input type="checkbox" ${spawn.worktree?'checked':''} onchange="scheduleView.spawnSpec.worktree=this.checked;renderSchedule()"><span>new git worktree</span></label>${spawn.worktree?`<input class="nfin" data-draft-key="${esc(v.draftKey+':worktree_name')}" value="${esc(spawn.worktree_name||'')}" placeholder="worktree name (optional)" oninput="scheduleView.spawnSpec.worktree_name=this.value">`:''}`}`}
    <label class="nflab">message</label><textarea class="nfin" data-draft-key="${esc(v.draftKey+':message')}" maxlength="2000" oninput="scheduleView.message=this.value">${esc(v.message)}</textarea>
    ${v.kind==='at_time'||isNew?`<div class="scheduletime"><label><span class="nflab">local date and time</span><input class="nfin" data-draft-key="${esc(v.draftKey+':time')}" type="datetime-local" value="${esc(v.localTime)}" onchange="scheduleView.localTime=this.value;scheduleView.choices=null"></label><span class="schedulezone">${esc(v.zone)}</span></div>`:''}
    ${v.kind==='usage_reset'?`<label class="nflab">account and usage window</label><select class="nfsel" onchange="scheduleView.usageKey=this.value">${usage.map(item=>`<option value="${esc(item.value)}" ${item.value===v.usageKey?'selected':''}>${esc(item.label)}</option>`).join('')}</select>${usage.length?'':'<div class="schedulewarn">No fresh reset evidence is available.</div>'}`:''}
    ${v.choices?`<div class="schedulewarn">That clock time occurs twice. Choose which occurrence:<select class="nfsel" onchange="scheduleView.fold=Number(this.value)">${v.choices.map((choice,index)=>`<option value="${choice.fold}">${index?'Second':'First'} occurrence · UTC offset ${esc(choice.offset)}</option>`).join('')}</select></div>`:''}
    <div id="schedulemsg" class="actmsg">${v.busy?'saving…':''}</div><div class="schedulesubmit"><button class="pbtn" ${v.busy?'disabled':''} onclick="dismissOverlay()">Cancel</button><button class="pbtn send" ${v.busy?'disabled':''} onclick="submitSchedule()">${v.busy?'Saving…':v.id?(v.operation?'Create retry':'Save changes'):'Schedule'}</button></div></div>`;
}
async function submitSchedule(){
  if(!scheduleView||scheduleView.busy)return;const v=scheduleView;const msg=$('#schedulemsg');
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
  v.busy=true;renderSchedule();
  let body;if(!v.id)body={type:'outbox_create',...payload};
  else if(v.operation==='retry')body={type:'outbox_retry',outbox_id:v.id,patch:payload};
  else body={type:'outbox_update',outbox_id:v.id,patch:payload};
  if(v.id&&v.operation!=='retry')body.patch.expected_version=v.expectedVersion;
  body.client_request_id=v.clientRequestId;
  const result=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).catch(error=>({ok:false,error:String(error)}));
  if(scheduleView!==v)return;
  if(!result.ok){v.busy=false;if(result.code==='ambiguous_time'){v.choices=result.choices;v.fold=result.choices?.[0]?.fold;renderSchedule();return;}renderSchedule();const current=$('#schedulemsg');if(current)current.textContent='✗ '+(result.error||'failed');return;}
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
  if(settingsOpen&&['notifications','devices'].includes(settingsSection))renderSettings();return pushData;
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
const listedPushBusy=new Set();
function replaceListedPushDevice(device){
  if(!device?.id)return;
  const devices=[...(pushData.devices||[])],index=devices.findIndex(item=>item.id===device.id);
  if(index>=0)devices[index]=device;else devices.push(device);
  pushData.devices=devices;
  if(pushData.current_device?.id===device.id)pushData.current_device=device;
  pushData.enabled_devices=devices.filter(item=>item.enabled===true).length;
}
function listedPushWorking(deviceId,message){
  const card=document.querySelector(`[data-push-device="${CSS.escape(deviceId)}"]`);
  if(!card)return;
  card.querySelectorAll('button,input').forEach(control=>{control.disabled=true;});
  const status=card.querySelector('.devicefeedback');if(status)status.textContent=message;
}
async function updateListedPushDevice(deviceId,patch){
  if(listedPushBusy.has(deviceId))return;
  listedPushBusy.add(deviceId);listedPushWorking(deviceId,'saving…');
  try{const data=await pushApi('/api/push/device-settings',{device_id:deviceId,...patch});
    replaceListedPushDevice(data.device);renderSettings();
    const status=document.querySelector(`[data-push-device="${CSS.escape(deviceId)}"] .devicefeedback`);
    if(status)status.textContent='saved ✓';
  }catch(error){pushLocal.error=String(error.message||error);renderSettings();}
  finally{listedPushBusy.delete(deviceId);}
}
async function testListedPushDevice(deviceId){
  if(listedPushBusy.has(deviceId))return;
  listedPushBusy.add(deviceId);listedPushWorking(deviceId,'queuing test…');
  try{await pushApi('/api/push/test',{device_id:deviceId});await loadPushState(true);
    const status=document.querySelector(`[data-push-device="${CSS.escape(deviceId)}"] .devicefeedback`);
    if(status)status.textContent='test queued ✓';
  }catch(error){pushLocal.error=String(error.message||error);renderSettings();}
  finally{listedPushBusy.delete(deviceId);}
}
function confirmForgetPushDevice(deviceId,name){
  askConfirm('Remove notification device',
    `Fleet will revoke <b>${esc(name||'this device')}</b> and suppress its queued deliveries. The browser must reconnect before it can receive another push.`,
    'remove device',()=>forgetPushDevice(deviceId),true);
}
async function forgetPushDevice(deviceId){
  if(listedPushBusy.has(deviceId))return;
  listedPushBusy.add(deviceId);listedPushWorking(deviceId,'removing…');
  try{await pushApi('/api/push/subscription',{device_id:deviceId,forget:true});
    pushData.devices=(pushData.devices||[]).filter(item=>item.id!==deviceId);
    pushData.enabled_devices=pushData.devices.filter(item=>item.enabled===true).length;
    renderSettings();
  }catch(error){pushLocal.error=String(error.message||error);renderSettings();}
  finally{listedPushBusy.delete(deviceId);}
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
  if(settingsOpen&&settingsSection==='devices')renderSettings();
}
window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();pushInstallPrompt=event;if(settingsOpen&&settingsSection==='devices')renderSettings();});
window.addEventListener('appinstalled',()=>{pushInstallPrompt=null;if(settingsOpen&&settingsSection==='devices')renderSettings();});
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
const notificationKindDescriptions={
  question:'A coding session is waiting for one answer from you.',
  approval:'A coding session is waiting for you to approve or deny an action.',
  form:'A coding session is waiting for answers to a structured set of questions.',
  reply:'A coding session finished a response and explicitly asked you to reply.',
  failure:'A session, provider, repository action, scheduled message, or push delivery needs review.',
  stall:'A working session has shown no progress for longer than the stalled-session threshold.',
  completion:'A session moved out of active work and has a completed response ready.',
  artifact:'A session delivered one or more files that are ready to open.',
  outcome:'A repository action, scheduled message, or reported repository check completed.',
  budget:'A configured cost, token, runtime, or concurrency budget reached a warning or limit.',
  measurement:'Fleet could not measure a value needed to evaluate a configured budget.',
  notification:'A Fleet system notice, including an explicit Web Push test.'};
const notificationSeverityOptions=[
  ['info','All events (Info, Warning, or Critical)'],
  ['warning','Warning or Critical'],
  ['critical','Critical only']];
let notificationData={ok:true,events:[],delivery_problems:[],unread:0,active:0,event_cursor:0,next_cursor:null};
let notificationItems=[],notificationSection='needs',notificationLoading=false;
let notificationPollingEnabled=false;
let notificationError='',notificationLoadedAt=0,notificationAbort=null,notificationSequence=0;
let notificationDetail=null,notificationDetailLoading=false,notificationDetailError='';
const notificationActionStates=new Map();let notificationActionGeneration=0;
if(pushActionFallback&&notificationDetailId)notificationActionStates.set(notificationDetailId,{busy:false,
  message:`${pushActionFallback==='snooze'?'Snooze':'Mute'} from the notification did not complete. Review the current state and try again.`,
  error:true,generation:0});
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
function renderNotificationActionFeedback(key){
  // Snooze/wake/mute are initiated in the open detail pane. Updating all
  // notification filters and up to 60 list rows just to paint "Working…"
  // delays first feedback on mobile. Delivery retries still need their list
  // row rebuilt, so keep the full fallback for non-detail action keys.
  if(key===notificationDetailId){
    const detail=$('#notificationdetail');
    if(detail){detail.innerHTML=notificationDetailHtml(notificationDetail);
      detail.classList.toggle('open',Boolean(notificationDetailId));return;}
  }
  renderNotifications();
}
async function notificationPost(key,path,payload,success){
  const previous=notificationActionStates.get(key);if(previous?.busy)return;
  const started=performance.now(),generation=++notificationActionGeneration;
  notificationActionStates.set(key,{busy:true,message:'Working…',error:false,generation});
  renderNotificationActionFeedback(key);recordInputFeedback(started,'notification_action');
  let completionRecorded=false;
  try{
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'Notification action failed');
    if(notificationActionStates.get(key)?.generation!==generation)return;
    notificationActionStates.set(key,{busy:false,message:success,error:false,generation});renderNotificationActionFeedback(key);
    perfRecord('notification_action_completion_ms',performance.now()-started);completionRecorded=true;
    // Provider acceptance is the local completion point. Refresh canonical
    // history in the background so a slow list read cannot hold the control.
    void (async()=>{await loadNotifications(true,true);
      if(notificationDetailId)await loadNotificationDetail(notificationDetailId,false);})();
  }catch(error){if(notificationActionStates.get(key)?.generation===generation){
    notificationActionStates.set(key,{busy:false,message:String(error.message||error),error:true,generation});renderNotificationActionFeedback(key);}}
  finally{if(!completionRecorded)perfRecord('notification_action_completion_ms',performance.now()-started);}
}
function snoozeNotification(id,revision,choice){
  const now=new Date(),until=new Date(now);
  if(choice==='tomorrow'){until.setDate(until.getDate()+1);until.setHours(9,0,0,0);}
  else until.setTime(now.getTime()+(choice==='hour'?3600:900)*1000);
  notificationPost(id,'/api/notifications/snooze',{event_id:id,source_revision:revision,
    until:until.getTime()/1000},`Snoozed until ${until.toLocaleString([],choice==='tomorrow'?{weekday:'short',hour:'numeric',minute:'2-digit'}:{hour:'numeric',minute:'2-digit'})}`);
}
function wakeNotification(id,revision){notificationPost(id,'/api/notifications/wake',
  {event_id:id,source_revision:revision},'Moved back to Needs action');}
function muteNotification(id,revision,muted){notificationPost(id,'/api/notifications/mute',
  {device_id:briefingDevice,event_id:id,source_revision:revision,muted},muted?'Session muted until you unmute it':'Session unmuted');}
function retryNotificationDelivery(id){notificationPost('delivery:'+id,'/api/notifications/retry',
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
function notificationTime(item){const value=Number(typeof item==='number'?item:
  item?.changed_at||item?.opened_at||item?.updated_at)||0;if(!value)return'';
  const delta=Date.now()/1000-value;return delta<0?'in '+fmtAge(-delta):fmtAge(delta)+' ago';}
function notificationRow(item){
  const bucket=notificationBucket(item),id=enc(item.id),meta=[notificationKinds[item.kind]||item.kind,item.provider,
    notificationTime(item)].filter(Boolean).join(' · ');
  return`<button class="notificationrow ${esc(bucket)} ${item.unread?'unread':''}" onclick="openNotification(decodeURIComponent('${id}'))">
    <span class="notificationspine"></span><span class="notificationcopy"><span><b>${esc(item.title||'Fleet update')}</b>${item.unread?'<i aria-label="unread"></i>':''}</span>
    <small>${esc(item.summary||'')}</small><em>${esc(meta)}</em></span><span class="notificationchev">›</span></button>`;
}
function deliveryProblemRow(item){
  const pending=['queued','sending','retrying'].includes(item.status);
  const action=notificationActionStates.get('delivery:'+item.id)||{};
  const label=item.status==='subscription_expired'?'Reconnect '+(item.display_name||'device'):
    pending?'Delivery retry pending':'Delivery failed';
  return`<article class="notificationrow problems deliveryproblem"><span class="notificationspine"></span><span class="notificationcopy"><span><b>${esc(label)}</b></span>
    <small>${item.status==='subscription_expired'?'The browser subscription expired. Reconnect before retrying.':pending?'A retry is queued; the problem clears after a confirmed delivery.':`Attempt ${item.attempt||0}${item.remote_status?` · HTTP ${item.remote_status}`:''}`}</small>
    <em>${esc([item.platform,notificationTime(item)].filter(Boolean).join(' · '))}</em></span><span class="deliveryactions">${action.message?`<span role="status">${action.busy?'◌ ':''}${esc(action.message)}</span>`:''}${item.can_retry?`<button ${action.busy?'disabled':''} onclick="retryNotificationDelivery(decodeURIComponent('${enc(item.id)}'))">Retry</button>`:pending?'<span>Waiting</span>':`<button onclick="navigateTo('settings')">Reconnect</button>`}</span></article>`;
}
function notificationDetailHtml(item){
  if(notificationDetailLoading)return'<div class="notificationdetailstate"><span class="delivery sending">◌</span> Checking current state…</div>';
  if(notificationDetailError)return`<div class="notificationdetailstate bad">${esc(notificationDetailError)}<button onclick="loadNotificationDetail(decodeURIComponent('${enc(notificationDetailId)}'))">Retry</button></div>`;
  if(!item)return'<div class="notificationdetailstate"><b>Select an event</b><span>Open a row to review its current state and safe actions.</span></div>';
  const active=item.state==='active',snoozed=item.state==='snoozed',resolved=!active&&!snoozed;
  const action=notificationActionStates.get(item.id)||{};
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
    ${currentRequest}${action.message?`<div class="notificationactionmsg ${action.error?'bad':''}" role="status">${action.busy?'<span class="delivery sending">◌</span> ':''}${esc(action.message)}</div>`:''}
    <div class="notificationactions">
      ${active?`<button ${action.busy?'disabled':''} onclick="snoozeNotification(decodeURIComponent('${id}'),decodeURIComponent('${revision}'),'quarter')">Snooze 15m</button><button ${action.busy?'disabled':''} onclick="snoozeNotification(decodeURIComponent('${id}'),decodeURIComponent('${revision}'),'hour')">1 hour</button><button ${action.busy?'disabled':''} onclick="snoozeNotification(decodeURIComponent('${id}'),decodeURIComponent('${revision}'),'tomorrow')">Tomorrow</button>`:''}
      ${snoozed?`<button class="primary" ${action.busy?'disabled':''} onclick="wakeNotification(decodeURIComponent('${id}'),decodeURIComponent('${revision}'))">Wake now</button>`:''}
      ${item.session_id&&(!resolved||item.muted)?`<button ${action.busy?'disabled':''} onclick="muteNotification(decodeURIComponent('${id}'),decodeURIComponent('${revision}'),${item.muted?'false':'true'})">${item.muted?'Unmute session':'Mute session'}</button>`:''}
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
function messageFingerprint(message){
  if(message&&message.id!=null)return`id:${message.id}`;
  if(message&&message.uuid!=null)return`uuid:${message.uuid}`;
  return JSON.stringify(message||null);
}
function mergeFreshConversation(previous,fresh){
  const latest=[...(fresh.messages||[])],old=[...(previous?.messages||[])];
  if(!old.length)return{...fresh,messages:latest};
  const freshStart=Number(fresh.message_total)-latest.length;
  const oldStart=Number(previous.next_cursor);
  if(Number.isFinite(freshStart)&&Number.isFinite(oldStart)&&freshStart>=oldStart){
    const prefixLength=Math.min(old.length,Math.max(0,freshStart-oldStart));
    return{...fresh,messages:[...old.slice(0,prefixLength),...latest],next_cursor:oldStart};
  }
  const oldKeys=old.map(messageFingerprint),freshKeys=latest.map(messageFingerprint);
  let overlap=Math.min(oldKeys.length,freshKeys.length);
  while(overlap>0&&oldKeys.slice(-overlap).some((key,index)=>key!==freshKeys[index]))overlap--;
  return{...fresh,messages:overlap?[...old,...latest.slice(overlap)]:latest,
    next_cursor:overlap?previous.next_cursor:fresh.next_cursor};
}
async function ensureCtx(sid,v){
  if(!ctxCache[sid])ctxCache[sid]=savedConversation('session',sid)||undefined;
  const c=ctxCache[sid];
  if(c&&((c.v===v&&!c.stale)||c.fetching||
      (c.error&&(fleetOffline||Date.now()<Number(c.retryAt||0)))))return;
  ctxCache[sid]={...(c||{}),fetching:true};
  try{
    const r=await fetch('/api/context?sid='+encodeURIComponent(sid)+'&limit=50',{cache:'no-store'});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Conversation unavailable');
    ctxCache[sid]=mergeFreshConversation(c,{v,messages:d.messages||[],files:d.files||[],
      next_cursor:d.next_cursor,message_total:d.message_total});
    delete ctxCache[sid].stale;delete ctxCache[sid].error;delete ctxCache[sid].retryAt;
    persistConversation('session',sid,'',ctxCache[sid]);
    render(last);
  }catch(e){ctxCache[sid]={...(c||{}),v,fetching:false,stale:Boolean(c),
    messages:c?.messages||[],files:c?.files||[],error:String(e.message||e),retryAt:Date.now()+5000};render(last);}
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
    persistConversation(scope,sid,aid,c);
  }catch(error){c.olderError=String(error.message||error);}
  finally{c.loadingOlder=false;uiRefresh();}
  if(body&&old&&document.body.contains(body))requestAnimationFrame(()=>{
    body.scrollTop=old.top+Math.max(0,body.scrollHeight-old.height);
  });
}
// "opus · high". Effort comes from the statusline side-write, so a session whose
// statusline hasn't rendered yet (or isn't installed) shows the model alone.
const modelLabel=s=>{
  const saved=sessionSettingActions.get(s?.session_id);
  const optimistic=sessionSettingActionPending(s?.session_id,saved)?saved:null;
  const model=optimistic?.model||s.model||s.family||'?';
  const effort=optimistic&&Object.prototype.hasOwnProperty.call(optimistic,'effort')
    ?optimistic.effort:s.effort;
  return esc(model)+(effort?` · ${esc(effort)}`:'');
};
const CLAUDE_PERMISSION_LABELS={default:'Manual',acceptEdits:'Accept edits',plan:'Plan',auto:'Auto',
  dontAsk:"Don't ask",bypassPermissions:'Bypass permissions'};
const claudePermissionLabel=mode=>CLAUDE_PERMISSION_LABELS[mode]||'Detecting…';
const providerModeActions=new Map();
const sessionSettingActions=new Map();
let sessionSettingSequence=0;
function sessionSettingActionPending(sid,action=sessionSettingActions.get(sid)){
  const providerAction=providerModeActions.get(sid);
  return Boolean(action?.pending&&providerAction?.kind==='settings'&&
    providerAction.version===action.version);
}
function reconcileSessionActions(f){
  const live=new Set((f?.sessions||[]).map(session=>session.session_id));
  for(const sid of sessionSettingActions.keys())if(!live.has(sid))sessionSettingActions.delete(sid);
  for(const sid of providerModeActions.keys())if(!live.has(sid))providerModeActions.delete(sid);
}
function catalogModelForSession(s,catalog=spawnCatalog(s?.provider)){
  const current=String(s?.model||'').toLowerCase();
  const family=String(s?.family||'').toLowerCase();
  return catalog.find(item=>item.id===s?.model)?.id||
    catalog.find(item=>current&&current.includes(String(item.id).toLowerCase()))?.id||
    catalog.find(item=>family===String(item.id).toLowerCase())?.id||catalog[0]?.id||'';
}
function repairedSessionEffort(provider,model,current){
  const efforts=spawnEfforts(provider,model);
  if(efforts.includes(current))return current;
  return efforts.includes('medium')?'medium':efforts.includes('high')?'high':efforts[0]||'';
}
function sessionSettingValues(s){
  const saved=sessionSettingActions.get(s?.session_id);
  const model=catalogModelForSession(s);
  if(sessionSettingActionPending(s?.session_id,saved))return{model:saved.model,effort:saved.effort};
  return{model,effort:repairedSessionEffort(s?.provider,model,s?.effort||'')};
}
function sessionSettingsControls(s){
  if(!s?.capabilities?.model_effort_settings)return'';
  const catalog=spawnCatalog(s.provider),values=sessionSettingValues(s);
  if(!catalog.length)return'';
  const action=sessionSettingActions.get(s.session_id);
  const locked=!s.capabilities?.change_model_effort||providerModeActions.has(s.session_id);
  const efforts=spawnEfforts(s.provider,values.model);
  const feedback=action?.message||s.capabilities?.change_model_effort_reason||'';
  return`<span class="ovgroup ovsettings"><span class="ovlabel">Model and effort</span>
    <label><span>Model</span><select aria-label="Session model" ${locked?'disabled':''}
      onchange="changeSessionModel('${s.session_id}',this.value)">
      ${catalog.map(item=>`<option value="${esc(item.id)}" ${values.model===item.id?'selected':''}>${esc(item.name||item.id)}</option>`).join('')}
    </select></label>
    <label><span>Effort</span><select aria-label="Session effort" ${locked||!efforts.length?'disabled':''}
      onchange="changeSessionEffort('${s.session_id}',this.value)">
      ${efforts.map(value=>`<option value="${esc(value)}" ${values.effort===value?'selected':''}>${esc(value)}</option>`).join('')}
    </select></label>
    ${feedback?`<small class="ovhint settingsfeedback${action?.error?' error':action?.warning?' warning':''}" role="status">${esc(feedback)}</small>`:''}
  </span><span class="ovsep"></span>`;
}
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
  return`<button class="fchip" ${f.missing||!f.file_id?'disabled':''} title="${esc(cap||f.name||'file')}"
    onclick="event.stopPropagation();viewFile('${enc(sid)}','${enc(f.file_id)}')">${f.kind==='image'?'🖼':'📄'} ${esc(f.name)}${f.missing?' (gone)':''}</button>`;
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
function nativePromptCapabilityLocked(s,p){
  const capability=p?.kind==='permission'?'decide_approval':'answer_structured';
  return s?.capabilities?.[capability]!==true;
}
function nativePromptLocked(s,p){
  return nativeRequestLocked(s?.session_id,p?.nonce)||nativePromptCapabilityLocked(s,p);
}
function nativePromptLabel(s,fallback='waiting on you'){
  return nativePromptCapabilityLocked(s,s?.pending)?
    (s?.capabilities?.answer_reason||"Waiting for Claude's native prompt state"):fallback;
}
async function withNativeRequestLock(sid,nonce,work){
  const key=nativeRequestKey(sid,nonce);if(nativeRequestLocks.has(key))return{ok:false,duplicate:true};
  nativeRequestLocks.add(key);
  try{return await work();}
  finally{nativeRequestLocks.delete(key);uiRefresh();}
}
function beginOptimisticAnswer(sid,nonce,text){
  answered[sid]=nonce;
  return addOptimistic(sid,text,'answer');
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
      status:queued.state==='confirmation_unknown'?'uncertain':'queued',queueLabel:'Queued offline',
      queueReason:queued.state==='confirmation_unknown'?'Delivery unconfirmed; restore only after checking the session':
        queued.dismissNonce?'Dismisses the open question, then sends after reconnection':
        'Sends automatically after reconnection',
      error:queued.error||'',
      baseCount:queued.baseCount,created:queued.created});
  }
  for(const receipt of Object.values(outboxReceipts)){
    if(receipt.sid!==sid||list.some(item=>item.outboxId===receipt.outboxId))continue;
    list.push({id:++optimisticSequence,...receipt,imageIds:[...(receipt.imageIds||[])]});
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
const OPTIMISTIC_CONFIRM_MS=15000;
function expireOptimistic(item,now=Date.now()){
  if(!item||item.status!=='sending'||!Number.isFinite(item.confirmDeadline)||
     now<item.confirmDeadline)return false;
  clearTimeout(item.confirmTimer);item.confirmTimer=null;
  item.status='failed';item.error='Not confirmed after 15 seconds';
  return true;
}
function paintOptimisticItem(item){
  const rows=[...document.querySelectorAll(`.optimistic[data-optimistic-id="${item.id}"]`)];
  rows.forEach(row=>{row.outerHTML=optimisticItemHtml(item);});
  return rows.length>0;
}
function scheduleOptimisticTimeout(item){
  clearTimeout(item.confirmTimer);
  item.confirmTimer=setTimeout(()=>{
    const current=optimisticList(item.sid).find(entry=>entry.id===item.id);
    if(!current||current.status!=='sending')return;
    if(!expireOptimistic(current))return scheduleOptimisticTimeout(current);
    // The full fleet render can be expensive on a phone. Update the open
    // receipt synchronously; the next normal render reconciles every surface.
    if(!paintOptimisticItem(current))uiRefresh();
  },Math.max(0,item.confirmDeadline-Date.now()));
}
function armOptimisticTimeout(item){
  item.confirmDeadline=Date.now()+OPTIMISTIC_CONFIRM_MS;
  scheduleOptimisticTimeout(item);
}
function addOptimistic(sid,text,kind='text',status='sending',queueId=null,baseCount=null,imageIds=[]){
  const feedbackStarted=performance.now();
  const messages=(ctxCache[sid]&&ctxCache[sid].messages)||[];
  const item={id:++optimisticSequence,queueId,sid,text:String(text||''),kind,status,
    imageIds:[...(imageIds||[])],imageCount:(imageIds||[]).length,
    baseCount:baseCount==null?canonicalCount(messages,{kind,text}):baseCount,created:Date.now()};
  optimisticBucket(sid).push(item);
  // Native answers have their own request result and may legitimately need more
  // than 15 seconds of TUI key sequencing. Keep their spinner until that result
  // fails or the canonical QA event replaces the receipt.
  if(status==='sending'&&kind!=='answer')armOptimisticTimeout(item);
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
const SESSION_TAIL_THRESHOLD=120;
let sessionFollowTail=true,sessionTailObserver=null,sessionReadingIntentRevision=0;
function sessionTailTarget(body){
  const rows=[...(body?.querySelectorAll('.aconvo > *')||[])];
  return rows.findLast?.(row=>!(row.matches('.optimistic[data-delivery-status="failed"],.optimistic[data-delivery-status="uncertain"]')))||
    [...rows].reverse().find(row=>!(row.matches('.optimistic[data-delivery-status="failed"],.optimistic[data-delivery-status="uncertain"]')))||null;
}
function sessionNearTail(body,threshold=SESSION_TAIL_THRESHOLD){
  if(!body)return false;
  const target=sessionTailTarget(body),bodyRect=body.getBoundingClientRect();
  if(!target)return body.scrollHeight-body.scrollTop-body.clientHeight<=threshold;
  return target.getBoundingClientRect().bottom-bodyRect.bottom<=threshold;
}
function pinSessionTail(body=$('#sbody')){
  if(!body)return;
  const ignored=body.querySelector('.aconvo > .optimistic[data-delivery-status="failed"],.aconvo > .optimistic[data-delivery-status="uncertain"]');
  if(!ignored){body.scrollTop=body.scrollHeight;return;}
  const target=sessionTailTarget(body);if(!target){body.scrollTop=body.scrollHeight;return;}
  const bodyRect=body.getBoundingClientRect(),targetRect=target.getBoundingClientRect();
  body.scrollTop=Math.max(0,body.scrollTop+targetRect.bottom-bodyRect.bottom+8);
}
function scheduleSessionTailPin(){
  requestAnimationFrame(()=>{if(!sessionFollowTail)return;pinSessionTail();
    requestAnimationFrame(()=>{if(sessionFollowTail)pinSessionTail();});});
}
function observeSessionTail(){
  sessionTailObserver?.disconnect();sessionTailObserver=null;
  const body=$('#sbody'),convo=body?.querySelector('.aconvo');if(!convo||!('ResizeObserver' in window))return;
  sessionTailObserver=new ResizeObserver(()=>{if(sessionFollowTail)scheduleSessionTailPin();});
  sessionTailObserver.observe(convo);
}
function activeReadingBody(){
  if($('#sview')?.style.display==='flex')return $('#sbody');
  return null;
}
function readingBlocks(body){
  if(!body)return[];
  return body.id==='sbody'?[...body.querySelectorAll('.aconvo > *')]:
    [...body.querySelectorAll('.mdoc > *, :scope > pre.raw, :scope > img')];
}
function captureReadingAnchor(body=activeReadingBody()){
  if(!body)return null;
  let pinned;
  if(body.id==='sbody'){
    pinned=sessionFollowTail&&sessionNearTail(body);
    // Follow-tail is an explicit reading position, not a permanent session
    // flag. If geometry proves the reader is away from the tail before a
    // layout mutation, preserve that reading anchor instead of snapping back.
    if(sessionFollowTail&&!pinned){sessionFollowTail=false;sessionReadingIntentRevision++;}
  }else pinned=body.scrollHeight-body.scrollTop-body.clientHeight<=16;
  const intentRevision=body.id==='sbody'?sessionReadingIntentRevision:null;
  if(pinned)return{body,pinned:true,intentRevision};
  const rect=body.getBoundingClientRect();
  const element=readingBlocks(body).find(row=>row.getBoundingClientRect().bottom>rect.top+1);
  return{body,pinned:false,element,offset:element?element.getBoundingClientRect().top-rect.top:0,
    scrollTop:body.scrollTop,intentRevision};
}
const readingAnchorRevisions=new WeakMap();
function restoreReadingAnchor(anchor){
  if(!anchor)return;
  const revision=(readingAnchorRevisions.get(anchor.body)||0)+1;
  readingAnchorRevisions.set(anchor.body,revision);
  const apply=()=>{if(!anchor.body.isConnected||readingAnchorRevisions.get(anchor.body)!==revision)return;
    // A delayed layout callback must never undo a newer reader gesture. This
    // is especially important when message growth and desktop wheel scrolling
    // land in adjacent animation frames.
    if(anchor.body.id==='sbody'&&anchor.intentRevision!==sessionReadingIntentRevision)return;
    if(anchor.pinned){if(anchor.body.id==='sbody'){sessionFollowTail=true;pinSessionTail(anchor.body);}
      else anchor.body.scrollTop=anchor.body.scrollHeight;return;}
    if(anchor.element?.isConnected){const rect=anchor.body.getBoundingClientRect();
      anchor.body.scrollTop+=anchor.element.getBoundingClientRect().top-rect.top-anchor.offset;}
    else anchor.body.scrollTop=anchor.scrollTop;
  };
  // Class and visual-viewport changes are laid out synchronously. Correct the
  // scroll position in that same frame so a later resize cannot capture an
  // already-shifted viewport as its new anchor, then repeat after paint for
  // WebKit's delayed visualViewport geometry.
  apply();requestAnimationFrame(apply);
}
function resizeComposer(input,preserveAnchor=true){
  if(!input)return;
  const style=getComputedStyle(input),base=Math.max(1,parseFloat(style.getPropertyValue('--composer-control-height'))||44);
  const lineHeight=Math.max(16,parseFloat(style.lineHeight)||22);
  const lines=Math.min(4,(String(input.value||'').match(/\n/g)||[]).length+1);
  const height=Math.round(base+(lines-1)*lineHeight);
  const anchor=preserveAnchor&&Math.abs(input.getBoundingClientRect().height-height)>.5?captureReadingAnchor():null;
  input.style.height=height+'px';
  input.style.overflowY=input.scrollHeight>height?'auto':'hidden';
  restoreReadingAnchor(anchor);
}
function composerInput(input,sid,pre){
  resizeComposer(input);
  slashInput(sid,pre);
}
function composerFocus(input,sid,pre){
  const anchor=captureReadingAnchor();
  input?.closest('.session-composer')?.classList.add('composer-active');
  resizeComposer(input,false);
  slashInput(sid,pre);
  restoreReadingAnchor(anchor);requestAnimationFrame(syncVisualViewport);
}
function updateOptimistic(sid,id,ok,error,providerConfirmed=false){
  const item=optimisticList(sid).find(entry=>entry.id===id);
  if(!item)return;
  let painted=false;
  if(!ok){clearTimeout(item.confirmTimer);item.confirmTimer=null;
    item.status='failed';item.error=error||'Send failed';painted=paintOptimisticItem(item);}
  else if(providerConfirmed){clearTimeout(item.confirmTimer);item.confirmTimer=null;
    delete item.confirmDeadline;item.providerConfirmed=true;}
  if(!painted)uiRefresh();
}
function markOptimisticUncertain(sid,id,error){
  const item=optimisticList(sid).find(entry=>entry.id===id);if(!item)return;
  clearTimeout(item.confirmTimer);item.confirmTimer=null;item.status='uncertain';
  item.error=error||'Delivery uncertain — check the terminal';
  if(!paintOptimisticItem(item))uiRefresh();
}
function visibleOptimistic(sid,messages){
  const list=optimisticList(sid);
  const keep=list.filter(item=>{
    // Timers may be throttled while a mobile browser is backgrounded. The
    // absolute deadline makes the next poll or view render self-heal.
    expireOptimistic(item);
    if(canonicalCount(messages,item)>item.baseCount){
      clearTimeout(item.confirmTimer);item.confirmTimer=null;
      if(item.queueId)removeOfflineMessage(item.queueId);
      if(item.outboxId)forgetOutboxReceipt(item.outboxId);
      if(item.imageIds?.length)void deleteImages(item.imageIds);
      return false;
    }
    if(item.status==='queued')return true;
    return true;
  });
  if(keep.length!==list.length)optimisticMessages.set(sid,keep);
  return keep;
}
function restoreOptimistic(sid,id){
  const list=optimisticList(sid),item=list.find(entry=>entry.id===id);
  if(item){clearTimeout(item.confirmTimer);item.confirmTimer=null;}
  optimisticMessages.set(sid,list.filter(entry=>entry.id!==id));
  if(item?.queueId)removeOfflineMessage(item.queueId);
  if(item?.outboxId)resolveOutboxReceipt(item.outboxId);
  if(item?.kind==='answer'){delete answered[sid];uiRefresh();return;}
  if(item?.imageIds?.length)restoreImageDraftIds(sid,item.imageIds);
  uiRefresh();
  requestAnimationFrame(()=>{
    const input=document.getElementById('sft-'+sid)||document.getElementById('vft-'+sid)||
      document.getElementById('ft-'+sid);
    if(input){input.value=item?.text||'';setDraft(composerDraftKey(sid),input.value);input.focus();}
  });
}
function dismissOptimistic(sid,id){
  const list=optimisticList(sid),item=list.find(entry=>entry.id===id);if(!item)return;
  clearTimeout(item.confirmTimer);item.confirmTimer=null;
  optimisticMessages.set(sid,list.filter(entry=>entry.id!==id));
  if(item.queueId)removeOfflineMessage(item.queueId);
  if(item.outboxId)resolveOutboxReceipt(item.outboxId);
  if(item.imageIds?.length)void deleteImages(item.imageIds);
  if(item.kind==='answer'&&item.status==='failed')delete answered[sid];
  uiRefresh();
}
function optimisticItemHtml(item){
  const queueLabel=item.queueLabel||'Queued · waiting for session';
  const delivery=item.status==='queued'?`<span class="delivery queued" aria-label="${item.queueId?'queued offline':'message queued'}">↥</span><span class="deliverylabel" role="status" title="${esc(item.queueReason||queueLabel)}">${esc(queueLabel)}</span>`:
    item.status==='sending'?`<span class="delivery sending" aria-label="sending">◌</span>`:
    item.status==='confirmed'&&item.kind==='text'&&item.outboxId?`<span class="delivery sent" aria-hidden="true">✓</span><span class="deliverylabel" role="status">Sent</span>`:
    item.status==='uncertain'?`<span class="delivery failed" aria-hidden="true">!</span><span class="deliverylabel" role="status">${esc(item.error||'Delivery uncertain — check terminal')}</span><button class="deliveryresolve" aria-label="dismiss uncertain message receipt" onclick="dismissOptimistic('${item.sid}',${item.id})">dismiss</button>`:
    item.status==='failed'?`<span class="delivery failed" aria-hidden="true">!</span><span class="deliverylabel" role="status">${esc(item.error||'Send failed')}</span><span class="deliveryactions"><button aria-label="send failed; restore message" onclick="restoreOptimistic('${item.sid}',${item.id})">restore</button><button aria-label="dismiss failed message receipt" onclick="dismissOptimistic('${item.sid}',${item.id})">dismiss</button></span>`:'';
  return`<div class="cmsg user optimistic" data-optimistic-id="${item.id}" data-delivery-status="${esc(item.status)}">
    <span class="crole">you</span>${delivery}
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
  const queuedMessages=visibleOptimistic(s.session_id,messages).filter(item=>item.status==='queued');
  const queued=queuedMessages.length?queuedMessages[queuedMessages.length-1]:null;
  const action=quickResponses.get(s.session_id)||null;
  const item=[answer,queued,action].filter(Boolean).sort((a,b)=>a.created-b.created).at(-1);
  if(!item)return'';
  const status=item.status==='confirmed'||item.status==='sent'?'sent':item.status;
  const verb=status==='queued'?(item.queueLabel||'Queued'):status==='sending'?
    (item.kind==='text'?'Sending':item.providerConfirmed?'Submitted':'Submitting'):
    status==='failed'?'Failed':'Submitted';
  const icon=status==='queued'?`<span class="delivery queued" aria-label="message queued">↥</span>`:
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
  ['#sview','#vbody','#sbody','#abody','#searchviewbody'].forEach(selector=>
    $(selector)?.classList.toggle('light',light));
  try{localStorage.setItem('viewer_light',light?'1':'0');}catch(e){}
}
function toggleTheme(){setTheme(!$('#sview')?.classList.contains('light'));}
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
        ${permissionLocked?`<small class="ovhint">${esc(s?.capabilities?.change_permission_mode_reason||
          (s?.permission_mode?"Available only while Claude is idle":"Waiting for Claude to report its mode"))}</small>`:''}
      </span><span class="ovsep"></span>`:''}
      ${kind==='session'?sessionSettingsControls(s):''}
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
  // Replacing an open <details> can dispatch a late toggle from the detached
  // element. Preserve the live state before replacement and ignore detached
  // toggle events so changing model/worktree never collapses Advanced.
  if(root.querySelector('.handoffadvanced')?.open)handoffView.advanced=true;
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
      <details class="handoffadvanced" ${handoffView.advanced?'open':''} ontoggle="if(this.isConnected)handoffView.advanced=this.open"><summary>Advanced session settings</summary>
        <label class="nflab">model</label><select class="nfsel" onchange="changeHandoffModel(this.value)">
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
    const repaired=repairSpawnSelection({provider:view.target,...view.defaults});
    view.defaults.model=repaired.model;view.defaults.effort=repaired.effort;
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
function changeHandoffModel(model){
  if(!handoffView)return;
  handoffView.defaults.model=model;
  if(handoffView.defaults.effort&&!spawnEfforts(handoffView.target,model).includes(handoffView.defaults.effort))
    handoffView.defaults.effort='';
  renderHandoff();
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
    if(d.code==='delivery_uncertain'){
      view.error=d.error||'Delivery unconfirmed — check the destination terminal.';
      view.status='Delivery unconfirmed · Fleet will not retry automatically';
      return;
    }
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
  return`<div class="handofflinks">${links.map(link=>`<button class="handofflink ${['delivery_failed','confirmation_unknown'].includes(link.status)?'failed':''}"
    title="${esc(link.status+(link.error?' — '+link.error:''))}" onclick="event.stopPropagation();primarySessionAction(decodeURIComponent('${enc(link.session_id)}'))">${link.direction==='from'?'continued in':'continued from'} ${esc(link.provider)} · ${esc(link.status)}</button>`).join('')}</div>`;
}

const terminalActions=new Map();
function terminalButton(s,card=false){
  if(!s)return'';
  const cls=`expandbtn termbtn${card?' deskonly':''}`;
  if(s.provider==='codex'){
    if(!s.capabilities?.focus_terminal||s.capabilities?.focus_terminal_mode!=='focus')return'';
    const action=terminalActions.get(s.session_id)||{};
    return`<button class="${cls}" title="bring this session's terminal tab to the front"
      ${action.busy?'disabled':''} onclick="event.stopPropagation();focusSession('${s.session_id}',this)">${action.busy?'Opening…':action.ok?'Opened ✓':'Open'}</button>`;
  }
  if(s.capabilities?.focus_terminal){
    const action=terminalActions.get(s.session_id)||{};
    return`<button class="${cls}" title="bring this session's terminal tab to the front"
      ${action.busy?'disabled':''} onclick="event.stopPropagation();focusSession('${s.session_id}',this)">${action.busy?'Opening…':action.ok?'Opened ✓':'Terminal'}</button>`;
  }
  return'';
}
let viewerQOpen=true,viewerPath=null;
// one question's full interaction block (options + descriptions + Other + ✕ dismiss);
// shared by the card's amber box (pre='msg') and the viewer's docked bar (pre='vmsg')
function singleQBlock(s,p,pre){
  const sid=s.session_id;
  const q=p.questions[0],ms=q.multiSelect,n=(q.options||[]).length;
  const otherKey=questionDraftPrefix(sid,p.nonce)+'other:0';
  if(otherDraft[sid]==null)otherDraft[sid]=q.secret?'':draftValue(otherKey);
  const sel=multiSel[sid]=multiSel[sid]||new Set();
  const locked=nativePromptLocked(s,p);
  return`<div class="ptool"><span class="ptlabel">${esc(q.header||'question')} — ${esc(nativePromptLabel(s))}</span>
      <button class="xbtn" ${locked?'disabled':''} title="${p.dismiss_action==='cancel_turn'?'dismiss by stopping this Codex turn':'dismiss — chat about this instead'}" onclick="sendDismiss('${sid}','${p.nonce}','${pre}')">✕</button></div>
    ${p.files&&p.files.length?`<div class="pfiles"><span class="plabel">read first</span>${p.files.map(f=>fchip(sid,f,f.caption)).join('')}</div>`:''}
    <div class="qtext">${esc(q.question)}</div>
    ${(q.options||[]).map((o,i)=>`<button class="optbtn ${sel.has(i+1)?'sel':''}" ${locked?'disabled':''}
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
  const stripName=name=>{const chars=Array.from(String(name||''));return esc(chars.length>40?`${chars.slice(0,40).join('')}…`:chars.join(''));};
  return`<div class="stripbox"><div class="fstrip">${files.map(f=>`<button class="fchip ${f.file_id===sessionView?.fileId?'cur':''}"
    ${f.missing||!f.file_id?'disabled':''} title="${esc(f.caption||f.name)}" aria-label="${esc(f.name)}${f.missing?' (gone)':''}"
    onclick="viewFile('${enc(sid)}','${enc(f.file_id)}')">${f.kind==='image'?'🖼':'📄'} ${stripName(f.name)}${f.missing?' (gone)':''}</button>`).join('')}</div></div>`;
}
function viewerSurfaceBar(sid,files){
  return`<div class="surfacebar viewer-surfacebar"><div class="surfacebar-main">${fileStrip(sid,files)||'<span class="surfacebar-empty">Current file</span>'}</div>
    <button class="pbtn surfacebar-action chatjump" onclick="openSession('${sid}')">chat</button></div>`;
}
function latestFileButton(sid,files){
  const f=(files||[])[0];if(!f)return'';
  return`<button class="pbtn surfacebar-action latestfile" ${f.missing?'disabled':''} title="${esc(f.caption||f.path)}"
    onclick="viewFile('${enc(sid)}','${enc(f.file_id)}')">${f.kind==='image'?'🖼':'📄'} <span>${esc(f.name)}</span></button>`;
}
function sessionSurfaceBar(s,files){
  const status=statusLineHtml(s.status_line,'session:'+s.session_id),latest=latestFileButton(s.session_id,files);
  if(!status&&!latest)return'';
  return`<div class="surfacebar session-surfacebar"><div class="surfacebar-main">${status}</div>${latest}</div>`;
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
function keepSessionActionScroll(root,fn){
  const contextTop=root.querySelector('.session-context')?.scrollTop||0;
  const question=root.querySelector('.question-scroll');
  if(question?.dataset.scrollKey)setQuestionScrollPosition(
    question.dataset.scrollKey,question.scrollTop);
  keepStripScroll(root,fn);
  const context=root.querySelector('.session-context');if(context)context.scrollTop=contextTop;
  const nextQuestion=root.querySelector('.question-scroll');
  if(nextQuestion?.dataset.scrollKey)nextQuestion.scrollTop=
    questionScrollPositions.get(nextQuestion.dataset.scrollKey)||0;
}
function renderViewerBar(force){
  if(sessionView?.section==='files')renderWorkspaceFiles(force);
}
// Full chat headers identify the conversation. Operational metadata lives in
// the status strip above the composer, where it can update independently.
function sessTitleBlock(s){
  if(!s)return '<b>session</b>';
  return `<b>${esc(s.title||s.project||'session')}</b>`;
}
function viewerFormat(name,kind){
  if(kind==='image')return'image';
  const ext=(String(name||'').match(/\.([^.]+)$/)||[])[1]?.toLowerCase()||'';
  if(['md','markdown'].includes(ext))return'markdown';
  if(['html','htm'].includes(ext))return'html';
  if(ext==='pdf')return'pdf';
  if(ext==='json')return'json';
  return'text';
}
function sandboxedHtmlDocument(source){
  const parsed=new DOMParser().parseFromString(String(source||''),'text/html');
  parsed.querySelectorAll('script[src],iframe,frame,object,embed,meta,base,link').forEach(node=>node.remove());
  parsed.querySelectorAll('*').forEach(element=>{
    for(const attr of [...element.attributes]){
      const key=attr.name.toLowerCase(),value=attr.value.trim().toLowerCase();
      if(['srcdoc','action','formaction','target','ping'].includes(key))
        element.removeAttribute(attr.name);
      else if(['href','xlink:href','srcset'].includes(key))element.removeAttribute(attr.name);
      else if(['src','poster','data'].includes(key)&&!value.startsWith('data:'))
        element.removeAttribute(attr.name);
    }
  });
  const styles=[...parsed.querySelectorAll('style')].map(node=>node.outerHTML).join('');
  parsed.querySelectorAll('style').forEach(node=>node.remove());
  const headScripts=[...parsed.head.querySelectorAll('script:not([src])')].map(node=>node.outerHTML).join('');
  parsed.head.querySelectorAll('script:not([src])').forEach(node=>node.remove());
  return`<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; media-src data: blob:; font-src data: blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'">
    ${styles}${headScripts}</head><body>${parsed.body.innerHTML}</body></html>`;
}
function showViewerFrame(body,className,title,{src='',srcdoc='',sandbox=true}={}){
  body.classList.add('frameview');
  const frame=document.createElement('iframe');frame.className=`fileframe ${className}`;
  frame.title=title;if(sandbox!==false)frame.setAttribute('sandbox',sandbox===true?'':sandbox);frame.referrerPolicy='no-referrer';
  if(srcdoc)frame.srcdoc=srcdoc;else frame.src=src;
  body.replaceChildren(frame);
}
function showJsonDocument(body,text){
  body.replaceChildren();
  const pre=document.createElement('pre');pre.className='raw jsondoc';
  try{pre.textContent=JSON.stringify(JSON.parse(text),null,2);}
  catch(error){
    const message=document.createElement('div');message.className='fileerror';
    message.textContent=`Invalid JSON — ${error.message}`;body.append(message);pre.textContent=text;
  }
  body.append(pre);
}
function viewFile(encodedSid,encodedFileId){
  const sid=decodeURIComponent(encodedSid),fileId=decodeURIComponent(encodedFileId||'');
  if(!/^[0-9a-f]{24}$/.test(fileId))return;
  seedTargetedWorkspaceHistory(sid,'files');
  openSessionWorkspace(sid,'files',fileId,true);
}
function seedTargetedWorkspaceHistory(sid,section){
  if(!sessionView||sessionView.sid!==sid)openSessionWorkspace(sid,'chat',null,true);
  if(sessionView?.section!==section)openSessionWorkspace(sid,section,null,true);
}
function closeViewer(){viewerSid=null;viewerPath=null;}
// ---- back-gesture / Esc closes the open full-screen overlay ----------------
// The fullscreen surfaces are mutually exclusive, so we model
// "an overlay is open" as ONE logical state: push a single history entry when we
// go from none-open to open, and the phone's back-swipe (popstate) closes it
// instead of navigating away from the dashboard. Closing via ✕/Esc calls
// history.back() so the pushed entry is consumed and history stays balanced.
let histPushed=false,schedulePushed=false,settingsPushed=false,settingsSectionDepth=0;
const fullscreenOverlaySelectors=['#sview','#settingsview','#searchview','#handoffview','#outboxview','#scheduleview'];
const anyOverlay=()=>fullscreenOverlaySelectors.some(id=>$(id).style.display==='flex');
function syncOverlayHistory(){
  if(anyOverlay()&&!histPushed){histPushed=true;history.pushState({fdOverlay:1},'');}
}
window.addEventListener('popstate',()=>{
  const destination=hashDestination();
  if(settingsOpen&&settingsSectionDepth>0&&destination.route==='settings'){
    settingsSection=SETTINGS_SECTIONS.includes(destination.detail)?destination.detail:'notifications';
    settingsSectionDepth=Math.max(0,settingsSectionDepth-1);renderSettings(true);return;
  }
  if(settingsPushed){settingsPushed=false;closeSettings();return;}
  if(schedulePushed){schedulePushed=false;closeSchedule();return;}
  if(handoffPushed){
    handoffPushed=false;
    const destination=handoffOpenAfterBack;handoffOpenAfterBack=null;
    closeHandoff();
    if(destination)primarySessionAction(destination);
    return;
  }
  const workspace=parseSessionHash();
  if(workspace){applyWorkspaceRoute(workspace);return;}
  if(histPushed){
    histPushed=false;
    closeConfirm();closeHandoff();closeViewer();closeAgent();closeSession();closeSettings();closeSearchView();closeOutbox();closeSchedule();
    return;
  }
  if(sessionView)closeSession();
  notificationDetailId=destination.route==='notifications'?destination.detail:'';
  if(!notificationDetailId){notificationDetail=null;notificationDetailError='';}
  navigateTo(destination.route,false,Boolean(notificationDetailId));
});
function dismissOverlay(){
  if(usageOpen)return closeUsage();
  if(overflowOpen)return closeOverflow();
  if(document.querySelector('.composerplus.open'))return closeComposerMenus();
  if($('#confirm').style.display==='flex')return closeConfirm();   // ask first
  if(settingsOpen&&settingsSectionDepth)return history.go(-(settingsSectionDepth+1));
  if(settingsPushed)return history.back();
  if(handoffPushed)return history.back();
  if(schedulePushed)return history.back();
  if(sessionView)return history.back();
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

// Full-screen surfaces are real, stack-aware dialogs. Their markup predates the
// modal controller, so semantics and focus ownership are applied centrally.
const modalDefinitions=[
  ['sview','stitle2','Session workspace'],['settingsview','settitle','Settings'],
  ['searchview','searchviewtitle','Search result'],['handoffview','handofftitle','Continue in another session'],
  ['outboxview',null,'Message Outbox'],['scheduleview','scheduletitle','Schedule message'],
  ['confirm',null,'Confirmation']];
const modalOpeners=new WeakMap(),modalOpenState=new WeakSet();let modalLastClosedOpener=null;
function modalOpener(element){
  if(!element||element===document.body)return null;
  const card=element.closest?.('[data-sid]');
  return{element,id:element.id||'',sid:card?.dataset.sid||'',aria:element.getAttribute?.('aria-label')||''};
}
function resolveModalOpener(record){
  if(!record)return null;
  if(record.element?.isConnected)return record.element;
  if(record.id){const found=document.getElementById(record.id);if(found)return found;}
  if(record.sid&&record.aria){
    return[...document.querySelectorAll('[data-sid]')].find(card=>card.dataset.sid===record.sid)
      ?.querySelector(`[aria-label="${CSS.escape(record.aria)}"]`)||null;
  }
  return null;
}
function modalVisible(root){return root&&root.style.display==='flex';}
function modalStack(){return modalDefinitions.map(([id])=>document.getElementById(id)).filter(modalVisible)
  .sort((a,b)=>(Number(getComputedStyle(a).zIndex)||0)-(Number(getComputedStyle(b).zIndex)||0));}
function modalFocusable(root){return[...root.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')]
  .filter(element=>!element.hidden&&element.getClientRects().length);}
function syncModalStack(){
  const stack=modalStack(),top=stack.at(-1)||null;
  for(const [id] of modalDefinitions){
    const root=document.getElementById(id),visible=stack.includes(root);
    if(visible&&!modalOpenState.has(root)){
      modalOpenState.add(root);modalOpeners.set(root,modalOpener(document.activeElement));
    }else if(!visible&&modalOpenState.has(root)){
      modalOpenState.delete(root);modalLastClosedOpener=modalOpeners.get(root)||modalLastClosedOpener;
    }
    root.inert=visible&&root!==top;
    root.setAttribute('aria-hidden',visible&&root===top?'false':'true');
  }
  for(const id of ['appshell','bottomnav','mobilemore','usagepanel']){
    const root=document.getElementById(id);if(root)root.inert=Boolean(top);
  }
  if(top)requestAnimationFrame(()=>{
    if(modalStack().at(-1)!==top||top.contains(document.activeElement))return;
    const restore=resolveModalOpener(modalLastClosedOpener);
    ((restore&&top.contains(restore)&&!restore.closest('[inert]'))?restore:
      (modalFocusable(top)[0]||top)).focus({preventScroll:true});modalLastClosedOpener=null;
  });
  else requestAnimationFrame(()=>{
    const restore=resolveModalOpener(modalLastClosedOpener);modalLastClosedOpener=null;
    if(restore&&!restore.closest('[inert]')&&!restore.closest(fullscreenOverlaySelectors.join(',')))
      restore.focus({preventScroll:true});
  });
}
for(const [id,labelledBy,label] of modalDefinitions){
  const root=document.getElementById(id);root.setAttribute('role','dialog');root.setAttribute('aria-modal','true');
  root.tabIndex=-1;if(labelledBy)root.setAttribute('aria-labelledby',labelledBy);else root.setAttribute('aria-label',label);
  root.setAttribute('aria-hidden','true');
  new MutationObserver(syncModalStack).observe(root,{attributes:true,attributeFilter:['style']});
}
document.addEventListener('keydown',event=>{
  if(event.key!=='Tab')return;const top=modalStack().at(-1);if(!top)return;
  const focusable=modalFocusable(top);if(!focusable.length){event.preventDefault();top.focus();return;}
  const first=focusable[0],lastItem=focusable.at(-1),active=document.activeElement;
  if(!top.contains(active)||(event.shiftKey&&active===first)||(!event.shiftKey&&active===lastItem)){
    event.preventDefault();(event.shiftKey?lastItem:first).focus();
  }
},true);

// iOS resizes the visual viewport independently from the fixed layout viewport.
// Keep full-screen reading surfaces fitted to the pixels above the keyboard so
// the dashboard beneath them can never peek through.
let visualViewportBaseline=Math.max(1,Math.round(
  window.visualViewport?.height||window.innerHeight||document.documentElement.clientHeight||1));
function syncVisualViewport(){
  const readingAnchor=captureReadingAnchor();
  const viewport=globalThis.__fleetVisualViewportOverride||window.visualViewport;
  const height=Math.max(1,Math.round(viewport?.height||window.innerHeight));
  const top=Math.max(0,Math.round(viewport?.offsetTop||0));
  const layoutHeight=Math.max(height,window.innerHeight||0,document.documentElement.clientHeight||0);
  const focused=document.activeElement;
  const editingOverlay=focused&&['INPUT','TEXTAREA','SELECT'].includes(focused.tagName)&&
    Boolean(focused.closest(fullscreenOverlaySelectors.join(',')));
  if(!editingOverlay)visualViewportBaseline=Math.max(height,layoutHeight);
  const keyboardOpen=Math.max(layoutHeight-height-top,visualViewportBaseline-height-top)>80;
  document.documentElement.style.setProperty('--fleet-visual-height',height+'px');
  document.documentElement.style.setProperty('--fleet-visual-top',top+'px');
  document.documentElement.classList.toggle('keyboard-open',keyboardOpen);
  const compact=matchMedia('(pointer:coarse)').matches||innerWidth<=720;
  for(const selector of fullscreenOverlaySelectors){const root=$(selector);if(compact){
    root.style.setProperty('inset','auto 0px');root.style.setProperty('top',top+'px');
    root.style.setProperty('bottom','auto');root.style.setProperty('height',height+'px');
  }else for(const property of ['inset','top','bottom','height'])root.style.removeProperty(property);}
  restoreReadingAnchor(readingAnchor);
}
window.visualViewport?.addEventListener('resize',syncVisualViewport);
window.visualViewport?.addEventListener('scroll',syncVisualViewport);
window.addEventListener('orientationchange',()=>requestAnimationFrame(syncVisualViewport));
document.addEventListener('focusin',event=>{
  if(event.target.closest?.(fullscreenOverlaySelectors.join(',')))requestAnimationFrame(syncVisualViewport);
},true);
document.addEventListener('focusout',event=>{
  if(!event.target.closest?.('.session-composer .composer'))return;
  setTimeout(()=>{
    const anchor=captureReadingAnchor();
    if(!document.activeElement?.closest?.('.session-composer .composer'))
      event.target.closest?.('.session-composer')?.classList.remove('composer-active');
    syncVisualViewport();restoreReadingAnchor(anchor);
  },0);
},true);
let chatTouch=null;
let sessionScrollIntentAt=0;
const noteSessionScrollIntent=event=>{
  if(event.target?.closest?.('#sbody')){
    sessionScrollIntentAt=Date.now();
    sessionReadingIntentRevision++;
  }
};
document.addEventListener('wheel',noteSessionScrollIntent,{passive:true,capture:true});
document.addEventListener('touchmove',noteSessionScrollIntent,{passive:true,capture:true});
document.addEventListener('pointerdown',noteSessionScrollIntent,{passive:true,capture:true});
document.addEventListener('keydown',event=>{
  if(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(event.key)&&
      event.target?.closest?.('#sview')){
    sessionScrollIntentAt=Date.now();
    sessionReadingIntentRevision++;
  }
},true);
document.addEventListener('touchstart',event=>{
  const input=document.activeElement;
  if(!event.target.closest?.('#sbody,#vbody')||
      !input?.matches?.('.session-composer .composer textarea'))return chatTouch=null;
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
$('#sbody')?.addEventListener('scroll',()=>{
  // Resizing images/messages also emits scroll events on mobile. Only an
  // actual reader gesture may disengage follow-tail; layout growth is handled
  // by the ResizeObserver and must remain pinned.
  if(Date.now()-sessionScrollIntentAt<1200)sessionFollowTail=sessionNearTail($('#sbody'));
},{passive:true});
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
  if(current){const anchor=captureReadingAnchor();current.outerHTML=statusLineHtml(status,key);
    restoreReadingAnchor(anchor);}
}
let sessionView=null;            // one session workspace: section + optional file/agent selection
const workspaceScrolls=new Map();
const LAST_FILE_STORE_KEY='fleet.lastSessionFile.v1';
let lastSessionFiles=(()=>{try{const value=JSON.parse(localStorage.getItem(LAST_FILE_STORE_KEY)||'{}');
  return value&&typeof value==='object'&&!Array.isArray(value)?value:{};}catch(_){return{};}})();
function rememberSessionFile(sid,fileId){
  if(!sid||!/^[0-9a-f]{24}$/.test(String(fileId||'')))return;
  lastSessionFiles[sid]=fileId;
  const rows=Object.entries(lastSessionFiles).slice(-200);lastSessionFiles=Object.fromEntries(rows);
  try{localStorage.setItem(LAST_FILE_STORE_KEY,JSON.stringify(lastSessionFiles));}catch(_){}
}
let sessionOpened=false;         // just-opened: force-scroll to bottom on the first render
let questionResizeActive=null;
const questionScrollPositions=new Map();
let questionPanelStore=(()=>{try{
  const value=JSON.parse(localStorage.getItem(QUESTION_PANEL_STORE_KEY)||'{}');
  if(!value||typeof value!=='object'||Array.isArray(value))return{};
  return Object.fromEntries(Object.entries(value).slice(-100).map(([key,state])=>[String(key),{
    height:Math.max(0,Math.min(2000,Number(state?.height)||0)),collapsed:Boolean(state?.collapsed)}]));
}catch(_){return{};}})();
const questionPanelKey=(sid,nonce)=>`${sid}:${nonce}`;
function questionPanelState(sid,nonce){
  const key=questionPanelKey(sid,nonce);
  return questionPanelStore[key]||(questionPanelStore[key]={height:0,collapsed:false});
}
function persistQuestionPanels(){try{
  const entries=Object.entries(questionPanelStore).slice(-100);questionPanelStore=Object.fromEntries(entries);
  localStorage.setItem(QUESTION_PANEL_STORE_KEY,JSON.stringify(questionPanelStore));
}catch(error){console.warn('Fleet could not persist question panel size',error);}}
function questionScrollKey(sid,p){
  const state=mqSel[sid],part=state&&state.nonce===p.nonce?state.qi:0;
  return`${sid}:${p.nonce}:${part}`;
}
function setQuestionScrollPosition(key,top){
  if(!key)return;
  questionScrollPositions.delete(key);
  questionScrollPositions.set(key,Math.max(0,Number(top)||0));
  while(questionScrollPositions.size>200){
    questionScrollPositions.delete(questionScrollPositions.keys().next().value);
  }
}
function rememberQuestionScroll(encodedKey,top){
  setQuestionScrollPosition(decodeURIComponent(encodedKey),top);
}
function questionPanelMaxHeight(){
  const view=$('#sview')?.getBoundingClientRect(),head=$('#shead2')?.getBoundingClientRect();
  const dock=$('#sact .composer-dock')?.getBoundingClientRect();
  const extras=$('#sact .session-extras')?.getBoundingClientRect();
  return Math.max(180,Math.floor((view?.height||innerHeight)-(head?.height||52)-
    (dock?.height||76)-(extras?.height||0)-18));
}
function questionPanelHeight(sid,nonce){
  const state=questionPanelState(sid,nonce),fallback=Math.min(420,Math.round(innerHeight*.44));
  return Math.max(180,Math.min(questionPanelMaxHeight(),state.height||fallback));
}
function toggleQuestionPanel(encodedSid,encodedNonce){
  const sid=decodeURIComponent(encodedSid),nonce=decodeURIComponent(encodedNonce);
  const state=questionPanelState(sid,nonce);state.collapsed=!state.collapsed;
  if(!state.collapsed&&!state.height)state.height=Math.min(420,Math.round(innerHeight*.44));
  persistQuestionPanels();renderSession(true);
}
function setQuestionPanelHeight(sid,nonce,height){
  const state=questionPanelState(sid,nonce),max=questionPanelMaxHeight();
  state.height=Math.max(180,Math.min(max,Math.round(height)));state.collapsed=false;
  persistQuestionPanels();renderSession(true);
}
function questionResizeKey(event,encodedSid,encodedNonce){
  if(!['ArrowUp','ArrowDown','Home','End'].includes(event.key))return;
  event.preventDefault();const sid=decodeURIComponent(encodedSid),nonce=decodeURIComponent(encodedNonce);
  const state=questionPanelState(sid,nonce);
  if(event.key==='Home'){state.collapsed=true;persistQuestionPanels();renderSession(true);return;}
  if(event.key==='End'){setQuestionPanelHeight(sid,nonce,questionPanelMaxHeight());return;}
  const height=questionPanelHeight(sid,nonce)+(event.key==='ArrowUp'?40:-40);
  if(height<=190&&event.key==='ArrowDown'){state.collapsed=true;persistQuestionPanels();renderSession(true);return;}
  setQuestionPanelHeight(sid,nonce,height);
}
function startQuestionResize(event,encodedSid,encodedNonce){
  if(event.pointerType==='mouse'&&event.button!==0)return;
  const sid=decodeURIComponent(encodedSid),nonce=decodeURIComponent(encodedNonce);
  const drawer=event.currentTarget.closest('.question-drawer'),state=questionPanelState(sid,nonce);
  if(!drawer||state.collapsed)return;
  event.preventDefault();const startY=event.clientY,startHeight=drawer.getBoundingClientRect().height;
  const max=questionPanelMaxHeight(),pointerId=event.pointerId;
  questionResizeActive={sid,nonce,pointerId};drawer.classList.add('resizing');
  event.currentTarget.setPointerCapture?.(pointerId);
  const move=moveEvent=>{
    if(moveEvent.pointerId!==pointerId)return;moveEvent.preventDefault();
    const height=Math.max(72,Math.min(max,startHeight+startY-moveEvent.clientY));
    state.height=Math.round(height);drawer.style.height=`${height}px`;
    drawer.classList.toggle('collapse-ready',height<=112);
  };
  const finish=endEvent=>{
    if(endEvent.pointerId!==pointerId)return;
    window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',finish);
    window.removeEventListener('pointercancel',finish);questionResizeActive=null;
    state.collapsed=state.height<=112;
    state.height=state.collapsed?Math.max(180,Math.min(max,Math.round(startHeight))):
      Math.max(180,state.height);
    persistQuestionPanels();
    if(state.collapsed){renderSession(true);return;}
    // An ordinary resize already has the correct live DOM. Rebuilding the
    // entire action dock here briefly detaches the drawer, flickers on slower
    // devices, and can discard its reading position between pointerup and the
    // next paint.
    drawer.classList.remove('resizing','collapse-ready');
    drawer.style.height=`${state.height}px`;
    const resizer=drawer.querySelector('.question-resizer');
    if(resizer){resizer.setAttribute('aria-valuenow',String(state.height));
      resizer.setAttribute('aria-valuetext',`${state.height} pixels`);}
  };
  window.addEventListener('pointermove',move,{passive:false});window.addEventListener('pointerup',finish);
  window.addEventListener('pointercancel',finish);
}
function questionDrawerHtml(s,p,pre){
  const sid=s.session_id,state=questionPanelState(sid,p.nonce),collapsed=state.collapsed;
  const height=questionPanelHeight(sid,p.nonce),scrollKey=questionScrollKey(sid,p);
  const label=p.questions.length>1?`Multi-part question · ${(mqSel[sid]?.qi||0)+1} of ${p.questions.length}`:
    `${p.questions[0].header||'Question'} · waiting on you`;
  return`<section class="question-drawer${collapsed?' collapsed':''}" data-question-key="${esc(questionPanelKey(sid,p.nonce))}"
      ${collapsed?'':`style="height:${height}px"`}>
    <div class="question-resizer" role="separator" tabindex="0" aria-label="Resize question panel"
      title="Drag to resize · Home collapses · End expands" aria-orientation="horizontal" aria-valuemin="0"
      aria-valuemax="${questionPanelMaxHeight()}" aria-valuenow="${collapsed?0:height}"
      aria-valuetext="${collapsed?'collapsed':`${height} pixels`}"
      onpointerdown="startQuestionResize(event,'${enc(sid)}','${enc(p.nonce)}')"
      onkeydown="questionResizeKey(event,'${enc(sid)}','${enc(p.nonce)}')"><i></i></div>
    <button class="question-drawer-toggle" aria-expanded="${!collapsed}"
      onclick="toggleQuestionPanel('${enc(sid)}','${enc(p.nonce)}')"><span>${esc(label)}</span><small>${collapsed?'expand':'collapse'}</small></button>
    ${collapsed?'':`<div class="question-scroll" data-scroll-key="${esc(scrollKey)}"
      onscroll="rememberQuestionScroll('${enc(scrollKey)}',this.scrollTop)">${pendingBox(s,pre)}</div>`}
  </section>`;
}
const closedCtx={};              // sid -> {messages, info} for CLOSED sessions
const reopenedSessions=new Set();// successful reopen feedback survives poll rerenders
let sessionEvidenceOpen=false;
const evidenceCache={};          // sid -> {events,next_cursor,loaded,loading,error}
function confidenceText(value){return({confirmed:'confirmed',inferred:'inferred',stale:'stale',unknown:'unknown'})[value]||'unknown';}
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
  if(sessionView?.sid===sid&&sessionView.section==='details')renderWorkspaceDetails(workspaceSessionModel(),workspaceContext());
  try{
    const cursor=more&&cache.next_cursor?'&cursor='+encodeURIComponent(cache.next_cursor):'';
    const response=await fetch('/api/evidence?sid='+encodeURIComponent(sid)+'&limit=30'+cursor,{cache:'no-store'});
    const data=await response.json();
    if(!data.ok)throw new Error(data.error||'state evidence unavailable');
    cache.events=more?[...(cache.events||[]),...(data.events||[])]:data.events||[];
    cache.next_cursor=data.next_cursor||null;cache.loaded=true;
  }catch(error){cache.error=String(error.message||error);}
  finally{cache.loading=false;}
  if(sessionView&&sessionView.sid===sid&&sessionView.section==='details')
    renderWorkspaceDetails(workspaceSessionModel(),workspaceContext());
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
function workspaceHash(sid,section='chat',item=null){
  return`#session/${encodeURIComponent(sid)}/${section}${item?'/'+encodeURIComponent(item):''}`;
}
function saveWorkspaceScroll(){
  if(!sessionView)return;
  const selector={chat:'#sbody',files:'#vbody',subagents:'#abody',details:'#spanel-details'}[sessionView.section];
  const node=$(selector);if(node)workspaceScrolls.set(`${sessionView.sid}:${sessionView.section}:${sessionView.fileId||sessionView.agentId||''}`,node.scrollTop);
}
function restoreWorkspaceScroll(){
  if(!sessionView)return;
  const selector={chat:'#sbody',files:'#vbody',subagents:'#abody',details:'#spanel-details'}[sessionView.section];
  const node=$(selector),key=`${sessionView.sid}:${sessionView.section}:${sessionView.fileId||sessionView.agentId||''}`;
  if(node&&workspaceScrolls.has(key))requestAnimationFrame(()=>{node.scrollTop=workspaceScrolls.get(key)||0;});
}
function workspaceSplitBounds(browser){
  const width=Math.max(0,browser?.getBoundingClientRect().width||$('#sworkspace')?.clientWidth||innerWidth);
  return{min:220,max:Math.max(220,Math.min(520,width-320))};
}
function setWorkspaceSplit(kind,width,persist=false){
  if(!['files','subagents'].includes(kind))return;
  const browser=$(`#s${kind==='files'?'file':'agent'}browser`);if(!browser)return;
  const bounds=workspaceSplitBounds(browser),value=Math.round(Math.max(bounds.min,Math.min(bounds.max,Number(width)||300)));
  workspaceSplitWidths[kind]=value;browser.style.setProperty('--workspace-list-width',`${value}px`);
  const divider=browser.querySelector('.workspacedivider');
  if(divider){divider.setAttribute('aria-valuemin',String(bounds.min));divider.setAttribute('aria-valuemax',String(bounds.max));divider.setAttribute('aria-valuenow',String(value));}
  if(persist)persistWorkspaceSplits();
}
function applyWorkspaceSplit(kind){setWorkspaceSplit(kind,workspaceSplitWidths[kind]||300,false);}
function startWorkspaceSplit(event,kind){
  if(matchMedia('(max-width:720px)').matches||(event.pointerType==='mouse'&&event.button!==0))return;
  const divider=event.currentTarget,browser=divider.closest('.workspacebrowser');if(!browser)return;
  event.preventDefault();const startX=event.clientX,startWidth=browser.querySelector('aside')?.getBoundingClientRect().width||300;
  divider.classList.add('resizing');divider.setPointerCapture?.(event.pointerId);
  const move=moveEvent=>{if(moveEvent.pointerId!==event.pointerId)return;moveEvent.preventDefault();setWorkspaceSplit(kind,startWidth+moveEvent.clientX-startX);};
  const end=endEvent=>{if(endEvent.pointerId!==event.pointerId)return;divider.classList.remove('resizing');
    divider.removeEventListener('pointermove',move);divider.removeEventListener('pointerup',end);divider.removeEventListener('pointercancel',end);persistWorkspaceSplits();};
  divider.addEventListener('pointermove',move);divider.addEventListener('pointerup',end);divider.addEventListener('pointercancel',end);
}
function workspaceSplitKey(event,kind){
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  const browser=event.currentTarget.closest('.workspacebrowser'),bounds=workspaceSplitBounds(browser),current=workspaceSplitWidths[kind]||300;
  event.preventDefault();const next=event.key==='Home'?bounds.min:event.key==='End'?bounds.max:current+(event.key==='ArrowRight'?16:-16);
  setWorkspaceSplit(kind,next,true);
}
function activateWorkspaceSection(){
  if(!sessionView)return;
  for(const section of workspaceSections){
    const active=section===sessionView.section,tab=$(`#stab-${section}`),panel=$(`#spanel-${section}`);
    if(tab){tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;}
    if(panel)panel.hidden=!active;
  }
  $('#sview').dataset.section=sessionView.section;
  if(['files','subagents'].includes(sessionView.section))applyWorkspaceSplit(sessionView.section);
}
function openSessionWorkspace(sid,section='chat',item=null,push=true){
  if(!sid)return;
  section=workspaceSections.has(section)?section:'chat';
  const liveSession=((last&&last.sessions)||[]).find(x=>x.session_id===sid)||
    (spawnProvisional&&spawnProvisional.id===sid?provisionalSessionObject():null);
  const closed=!liveSession&&(isClosedSession(sid)||closedMeta.has(sid)||String(sid).startsWith('codex:'));
  if(liveSession?.new_response)markRead(liveSession);
  const same=sessionView?.sid===sid,target=workspaceHash(sid,section,item);
  const replacingOpenSession=!same&&Boolean(sessionView);
  let returnHash=same?sessionView.returnHash:
    (parseSessionHash()?'#now':(location.hash||'#now'));
  let historyDepth=same?Number(sessionView.historyDepth||0):
    (replacingOpenSession?Number(sessionView.historyDepth||0):0);
  if(!push&&history.state?.fdWorkspace){
    returnHash=history.state.returnHash||returnHash;
    historyDepth=Math.max(0,Number(history.state.depth)||0);
  }else if(push&&location.hash!==target)historyDepth++;
  if(same)saveWorkspaceScroll();
  const previousSection=same?sessionView.section:null;
  sessionView={...(same?sessionView:{}),sid,closed,lastGood:liveSession||(same?sessionView.lastGood:null),
    missingSince:null,section,fileId:section==='files'?item:null,agentId:section==='subagents'?item:null,
    fileExplicit:section==='files'?Boolean(item):false,agentExplicit:section==='subagents'?Boolean(item):false,
    agentFilter:section==='subagents'&&previousSection!=='subagents'?'active':(same?sessionView.agentFilter:'active'),
    returnHash,historyDepth};
  agentView=sessionView.agentId?{sid,aid:sessionView.agentId}:null;
  viewerSid=section==='files'?sid:null;viewerPath=sessionView.fileId;
  if(!same){sessionOpened=true;sessionFollowTail=true;sessionReadingIntentRevision++;
    sessionEvidenceOpen=false;$('#sbody').innerHTML='<div class="ctxload">loading conversation…</div>';
    $('#sactivity').innerHTML='';delete $('#sactivity').dataset.renderKey;
    delete $('#sbody').dataset.renderKey;delete $('#sbody').dataset.canonicalKey;}
  $('#sview').style.display='flex';activateWorkspaceSection();syncVisualViewport();
  const routeState={fdWorkspace:1,sid,section,item,returnHash,depth:historyDepth};
  // Switching from one session to another replaces the open workspace route.
  // Otherwise closing the second chat exposes the first (often already-closed)
  // chat instead of the dashboard.
  if(push&&location.hash!==target){
    if(replacingOpenSession)history.replaceState(routeState,'',target);
    else history.pushState(routeState,'',target);
  }
  else if(!push&&location.hash!==target)history.replaceState(routeState,'',target);
  if(closed&&!closedSession(sid))loadClosedMeta(sid);
  requestAnimationFrame(()=>{if(sessionView?.sid===sid){renderSession(true);restoreWorkspaceScroll();}});
}
function applyWorkspaceRoute(route){
  pendingWorkspaceRoute=null;openSessionWorkspace(route.sid,route.section,route.item,false);
}
function openSession(sid){openSessionWorkspace(sid,'chat',null,true);}
function openClosed(sid){openSessionWorkspace(sid,'chat',null,true);}
function exitSessionWorkspace(){
  if(!sessionView)return;
  const returnHash=sessionView.returnHash||'#now';
  closeSession();
  // The close control always returns to the dashboard destination. Traversing
  // browser history can instead reveal the chat that this one replaced.
  const route=returnHash.replace(/^#/,'').split('/')[0];
  history.replaceState({fdRoute:validRoutes.has(route)?route:'now'},'',returnHash);
  navigateTo(validRoutes.has(route)?route:'now',false);
}
function setSessionSection(section){
  if(!sessionView||!workspaceSections.has(section))return;
  openSessionWorkspace(sessionView.sid,section,null,true);
}
function clearWorkspaceSelection(){
  if(!sessionView||!['files','subagents'].includes(sessionView.section))return;
  saveWorkspaceScroll();sessionView.fileId=null;sessionView.agentId=null;agentView=null;viewerPath=null;
  sessionView.fileExplicit=false;sessionView.agentExplicit=false;
  history.replaceState({fdWorkspace:1,sid:sessionView.sid,section:sessionView.section,
    returnHash:sessionView.returnHash,depth:sessionView.historyDepth},'',
    workspaceHash(sessionView.sid,sessionView.section));
  renderSession(true);
}
$('#stabs')?.addEventListener('keydown',event=>{
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  const sections=[...workspaceSections],current=Math.max(0,sections.indexOf(sessionView?.section||'chat'));
  const index=event.key==='Home'?0:event.key==='End'?sections.length-1:
    (current+(event.key==='ArrowRight'?1:-1)+sections.length)%sections.length;
  event.preventDefault();setSessionSection(sections[index]);requestAnimationFrame(()=>$(`#stab-${sections[index]}`)?.focus());
});
const WORKSPACE_SWIPE_MIN_PX=56,WORKSPACE_EDGE_SWIPE_PX=24;
let workspaceTouch=null;
function mobileWorkspaceSwipeEnabled(){
  return Boolean(sessionView&&$('#sview')?.style.display!=='none'&&matchMedia('(max-width:720px)').matches);
}
function workspaceHorizontalTarget(target){
  if(!(target instanceof Element))return false;
  if(target.closest('input,textarea,select,[contenteditable="true"],iframe,embed,object'))return true;
  for(let node=target;node&&node!==$('#sview');node=node.parentElement){
    const style=getComputedStyle(node),overflow=style.overflowX;
    if(['auto','scroll'].includes(overflow)&&node.scrollWidth>node.clientWidth+1)return true;
  }
  return false;
}
function workspaceTouchPoint(event){return event.touches?.[0]||event.changedTouches?.[0]||null;}
$('#sview')?.addEventListener('touchstart',event=>{
  if(!mobileWorkspaceSwipeEnabled()||event.touches?.length!==1)return workspaceTouch=null;
  const point=workspaceTouchPoint(event);if(!point)return workspaceTouch=null;
  workspaceTouch={x:point.clientX,y:point.clientY,lastX:point.clientX,lastY:point.clientY,
    edge:point.clientX<=WORKSPACE_EDGE_SWIPE_PX,ignored:workspaceHorizontalTarget(event.target),axis:null};
},{passive:true,capture:true});
$('#sview')?.addEventListener('touchmove',event=>{
  if(!workspaceTouch||event.touches?.length!==1)return;
  const point=workspaceTouchPoint(event);if(!point)return;
  workspaceTouch.lastX=point.clientX;workspaceTouch.lastY=point.clientY;
  const dx=point.clientX-workspaceTouch.x,dy=point.clientY-workspaceTouch.y;
  if(!workspaceTouch.axis&&Math.max(Math.abs(dx),Math.abs(dy))>=10){
    if(Math.abs(dx)>Math.abs(dy)*1.2)workspaceTouch.axis='x';
    else if(Math.abs(dy)>Math.abs(dx)*1.2)workspaceTouch.axis='y';
  }
  if(workspaceTouch.axis!=='x')return;
  const edgeExit=workspaceTouch.edge&&dx>0;
  if(edgeExit||!workspaceTouch.ignored)event.preventDefault();
},{passive:false,capture:true});
function finishWorkspaceTouch(event){
  if(!workspaceTouch)return;
  const gesture=workspaceTouch;workspaceTouch=null;
  const point=workspaceTouchPoint(event),endX=point?.clientX??gesture.lastX,endY=point?.clientY??gesture.lastY;
  const dx=endX-gesture.x,dy=endY-gesture.y;
  if(gesture.axis!=='x'||Math.abs(dx)<WORKSPACE_SWIPE_MIN_PX||Math.abs(dx)<=Math.abs(dy)*1.2)return;
  if(gesture.edge&&dx>0){exitSessionWorkspace();return;}
  if(gesture.ignored||!sessionView)return;
  const sections=[...workspaceSections],current=sections.indexOf(sessionView.section);
  const next=current+(dx<0?1:-1);
  if(next>=0&&next<sections.length)setSessionSection(sections[next]);
}
$('#sview')?.addEventListener('touchend',finishWorkspaceTouch,{passive:true,capture:true});
$('#sview')?.addEventListener('touchcancel',()=>{workspaceTouch=null;},{passive:true,capture:true});
async function loadClosedMeta(sid){
  try{
    const response=await fetch('/api/history?sid='+encodeURIComponent(sid),{cache:'no-store'});
    const data=await response.json();
    if(response.ok&&data.ok&&data.item)closedMeta.set(sid,data.item);
  }catch(_){/* the transcript endpoint still provides a read-only fallback */}
  if(sessionView&&sessionView.closed&&sessionView.sid===sid)renderClosed(true);
}
function closeSession(){
  saveWorkspaceScroll();closeOverflow();sessionView=null;agentView=null;viewerSid=null;viewerPath=null;
  sessionEvidenceOpen=false;slashClose();
  sessionTailObserver?.disconnect();sessionTailObserver=null;
  closeComposerMenus();
  $('#sview').style.display='none';$('#sbody').innerHTML='';delete $('#sbody').dataset.renderKey;
  $('#sactivity').innerHTML='';delete $('#sactivity').dataset.renderKey;
  $('#sact').innerHTML='';$('#sact').classList.remove('session-composer','composer-active','tools-open','question-present');
  $('#sctrl').innerHTML='';$('#sdetails').innerHTML='';$('#sfilelist').innerHTML='';
  $('#sagentlist').innerHTML='';$('#vbody').innerHTML='';$('#abody').innerHTML='';
  syncModalStack();
}
function sessionActivityHtml(s){
  const mainWorking=['running','stalled'].includes(s.state);
  if(!mainWorking)return'';
  const mainSlow=s.state==='stalled';
  return`<div class="mainworkingrow${mainSlow?' slow':''}" role="status" aria-live="polite">
    <span class="crole">${esc(s.provider||'agent')}</span><span class="cbody"><i class="workpulse${mainSlow?' slow':''}" aria-hidden="true"></i>
      <span>Main agent working</span></span></div>`;
}
function renderSessionActivity(s){
  const host=$('#sactivity');if(!host)return;
  const html=sessionActivityHtml(s),key=s.state;
  if(host.dataset.renderKey===key&&Boolean(host.innerHTML)===Boolean(html))return;
  host.innerHTML=html;host.dataset.renderKey=key;
}
function workspaceContext(){
  if(!sessionView)return null;
  return sessionView.closed?closedCtx[sessionView.sid]:ctxCache[sessionView.sid];
}
function workspaceAgents(s,c){return[...((s&&s.agents)||(c&&c.agents)||[])];}
function renderWorkspaceChrome(s,c){
  if(!sessionView)return;
  const files=(c&&c.files)||[],agents=workspaceAgents(s,c);
  $('#stitle2').innerHTML=`${sessTitleBlock(s)}${sessionView.closed?'<small class="closedbadge">Closed · saved workspace</small>':''}`;
  $('#sctrl').innerHTML=terminalButton(s)+overflowMenu('session',s,sessionView.closed?'closed':'session');
  $('#sfilecount').textContent=files.length?String(files.length):'';
  const activeAgents=agents.filter(agent=>!terminalAgentStates.has(agent.state));
  $('#sagentcount').textContent=activeAgents.length?String(activeAgents.length):'';
  activateWorkspaceSection();
}
function renderParentWorkspaceAction(s,c,{pending=true,showFiles=false,note=''}={}){
  const act=$('#sact');if(!act)return;
  const focused=document.activeElement;
  if(focused&&['INPUT','TEXTAREA'].includes(focused.tagName)&&act.contains(focused)){
    refreshStatusStrip('#sact',s.status_line,'session:'+s.session_id);return;
  }
  const p=pending?s.pending:null;
  const hasQ=p&&p.kind==='question'&&p.questions&&p.questions.length&&answered[s.session_id]!==p.nonce;
  const qHtml=hasQ?questionDrawerHtml(s,p,'smsg'):(p?pendingBox(s,'smsg'):'');
  act.classList.remove('tools-open');act.classList.toggle('session-composer',canCompose(s));
  act.classList.toggle('question-present',Boolean(hasQ));
  if(!canCompose(s))act.classList.remove('composer-active');
  keepSessionActionScroll(act,()=>{act.innerHTML=`<div class="session-context">${qHtml}
      ${note?`<div class="relaynote">${note}</div>`:''}
      <div class="session-extras">${handoffLinksHtml(s)}
        ${s.read_only?`<div class="relaynote"><b>view only</b> — ${esc(s.read_only_reason||'this thread is owned by another Codex runtime')}</div>`:''}
      ${sessionSurfaceBar(s,showFiles?(c&&c.files)||[]:[])}</div></div>
    ${renderComposer(s,'session')}`;});
  if(canCompose(s)){resizeComposer(document.getElementById('sft-'+s.session_id));void renderImageDrafts(s.session_id);}
}
function chosenWorkspaceFile(files){
  if(!files.length)return null;
  return files.find(file=>file.file_id===sessionView.fileId)||
    files.find(file=>file.file_id===lastSessionFiles[sessionView.sid])||files[0];
}
function renderWorkspaceFileDocument(file){
  const body=$('#vbody');if(!body||!file)return;
  const key=`${sessionView.sid}:${file.file_id}:${file.missing?'missing':'available'}`;
  if(body.dataset.renderKey===key)return;
  body.dataset.renderKey=key;body.classList.remove('frameview');body.replaceChildren();
  const format=viewerFormat(file.name,file.kind),url='/api/file?sid='+encodeURIComponent(sessionView.sid)+'&fid='+encodeURIComponent(file.file_id);
  $('#vtitle').innerHTML=`<span class="vfname">${format==='image'?'🖼':'📄'} ${esc(file.name)}${file.caption?` <small>${esc(file.caption)}</small>`:''}</span>`;
  if(file.missing){body.innerHTML='<div class="workspaceempty"><b>File unavailable</b><p>The retained transcript names this file, but neither the file nor its saved delivery copy remains.</p></div>';return;}
  if(format==='image'){body.innerHTML=`<div class="ctxload">loading image…</div><img hidden src="${url}" alt="${esc(file.name)}"
    onload="this.hidden=false;this.previousElementSibling?.remove()" onerror="this.previousElementSibling.textContent='✗ image unavailable';this.remove()">`;return;}
  if(format==='pdf'){showViewerFrame(body,'pdfpreview',`PDF preview: ${file.name}`,{src:url,sandbox:false});return;}
  body.textContent='loading…';
  fetch(url,{cache:'no-store'}).then(async response=>{
    if(!response.ok){body.textContent=(response.status===403?'read-only device — open the token URL once. ':'')+await response.text();return;}
    const text=await response.text();
    if(!sessionView||sessionView.sid!==file.session_id||sessionView.fileId!==file.file_id)return;
    if(sessionView.fileId!==file.file_id)return;
    if(format==='html')showViewerFrame(body,'htmlpreview',`HTML preview: ${file.name}`,{srcdoc:sandboxedHtmlDocument(text),sandbox:'allow-scripts'});
    else if(format==='json')showJsonDocument(body,text);
    else body.innerHTML=format==='markdown'?'<div class="mdoc">'+md(text)+'</div>':'<pre class="raw">'+esc(text)+'</pre>';
  }).catch(error=>{if(sessionView?.fileId===file.file_id)body.textContent='✗ '+error;});
}
function renderWorkspaceFiles(force=false){
  if(!sessionView||sessionView.section!=='files')return;
  const c=workspaceContext(),files=(c&&c.files)||[],list=$('#sfilelist');
  if(!c||c.fetching&&!c.messages){list.innerHTML='<div class="ctxload">loading file inventory…</div>';return;}
  if(!files.length){list.innerHTML='<div class="workspaceempty"><b>No retained files</b><p>This session has no validated file records.</p></div>';
    $('#vtitle').textContent='Files';$('#vbody').innerHTML='<div class="workspaceempty"><b>Nothing to preview</b></div>';
    $('#sfilebrowser').classList.remove('has-selection');return;}
  const selected=chosenWorkspaceFile(files);sessionView.fileId=selected?.file_id||null;viewerPath=sessionView.fileId;
  if(selected)rememberSessionFile(sessionView.sid,selected.file_id);
  const keepListTop=list.scrollTop;  // #sfilelist is itself the scroll container; innerHTML swap resets it every poll
  list.innerHTML=`<header class="workspaceasidehead"><b>${files.length} file${files.length===1?'':'s'}</b><small>session-owned inventory</small></header>
    <div class="workspacelist">${files.map(file=>`<button class="workspaceitem ${file.file_id===selected?.file_id?'selected':''}" ${file.missing||!file.file_id?'disabled':''}
      onclick="viewFile('${enc(sessionView.sid)}','${enc(file.file_id)}')"><span>${file.kind==='image'?'🖼':'📄'}</span><b>${esc(file.name)}</b><small>${esc(file.caption||'')}</small></button>`).join('')}</div>`;
  list.scrollTop=keepListTop;
  $('#sfilebrowser').classList.toggle('has-selection',Boolean(sessionView.fileExplicit));
  if(selected)renderWorkspaceFileDocument({...selected,session_id:sessionView.sid});
  renderParentWorkspaceAction(workspaceSessionModel(),c,{pending:true});
}
const terminalAgentStates=new Set(['done','ended','cancelled','failed','error']);
function filteredWorkspaceAgents(agents,filter){
  if(filter==='all')return agents.map(agent=>({agent,ancestor:false}));
  const keep=new Set(),stack=[];
  agents.forEach((agent,index)=>{const depth=Math.max(0,Number(agent.depth)||0);stack.length=depth;
    if(!terminalAgentStates.has(agent.state)){keep.add(index);stack.forEach(parent=>keep.add(parent));}
    stack[depth]=index;});
  return agents.map((agent,index)=>({agent,ancestor:keep.has(index)&&terminalAgentStates.has(agent.state)}))
    .filter((_,index)=>keep.has(index));
}
function setSubagentFilter(filter){
  if(!sessionView||sessionView.section!=='subagents')return;
  sessionView.agentFilter=filter==='all'?'all':'active';renderWorkspaceSubagents(true);
}
function selectWorkspaceAgent(encodedSid,encodedAid){
  const sid=decodeURIComponent(encodedSid),aid=decodeURIComponent(encodedAid);
  if(!/^agent-[A-Za-z0-9_-]{1,64}$/.test(aid))return;
  seedTargetedWorkspaceHistory(sid,'subagents');
  openSessionWorkspace(sid,'subagents',aid,true);
}
function renderWorkspaceSubagents(force=false){
  if(!sessionView||sessionView.section!=='subagents')return;
  const s=workspaceSessionModel(),c=workspaceContext(),agents=workspaceAgents(s,c),filter=sessionView.agentFilter||'active';
  const visible=filteredWorkspaceAgents(agents,filter);
  $('#sagentfilters').innerHTML=`<button class="${filter==='active'?'active':''}" aria-pressed="${filter==='active'}" onclick="setSubagentFilter('active')">Active</button>
    <button class="${filter==='all'?'active':''}" aria-pressed="${filter==='all'}" onclick="setSubagentFilter('all')">All</button>`;
  $('#sagentlist').innerHTML=agents.length?`<div class="workspacelist">${visible.length?visible.map(({agent,ancestor})=>{
    const terminal=terminalAgentStates.has(agent.state),latest=agent.last_msg?.text||(!terminal?`quiet ${fmtAge(Math.max(0,Number(agent.quiet_s)||0))}`:'');
    const desc=esc(ancestor?'parent of active subagent':latest);
    return`<button class="workspaceitem agentworkspaceitem ${agent.agent_id===sessionView.agentId?'selected':''} ${ancestor?'ancestor':''}" style="--agent-depth:${Math.max(0,Number(agent.depth)||0)}"
      onclick="selectWorkspaceAgent('${enc(sessionView.sid)}','${enc(agent.agent_id)}')"><span class="dot ${esc(agent.state||'running')}"></span><b>${esc(agent.description||agent.agent_type||agent.agent_id)}</b>
      <small><span class="amodel">${modelLabel(agent)}</span>${desc?` · ${desc}`:''}</small></button>`;}).join(''):'<div class="workspaceempty"><b>No active subagents</b><p>Choose All to read completed or cancelled work.</p></div>'}</div>`:
    '<div class="workspaceempty"><b>No retained subagents</b><p>No validated subagent records are available for this session.</p></div>';
  const selected=agents.find(agent=>agent.agent_id===sessionView.agentId)||null;
  if(!selected){sessionView.agentId=null;agentView=null;$('#sagentbrowser').classList.remove('has-selection');
    $('#atitle').textContent='Subagents';$('#abody').innerHTML='<div class="workspaceempty"><b>Select a subagent to read its conversation</b><p>The composer still targets the parent session.</p></div>';
    if(sessionView.closed)renderClosedComposer(s,c);else renderParentWorkspaceAction(s,c,{pending:false});return;}
  agentView={sid:sessionView.sid,aid:selected.agent_id};$('#sagentbrowser').classList.add('has-selection');renderAgent(force);
}
function workspaceSessionModel(){
  if(!sessionView)return{};
  const live=((last&&last.sessions)||[]).find(item=>item.session_id===sessionView.sid);
  if(live)return live;
  const c=closedCtx[sessionView.sid]||{},info=c.info||{},meta=closedSession(sessionView.sid)||{};
  return{...meta,...info,session_id:sessionView.sid,provider:meta.provider||(sessionView.sid.startsWith('codex:')?'codex':'claude'),
    title:meta.title||info.title||info.project||'closed session',state:'closed',closed:true,agents:c.agents||[],capabilities:{}};
}
function renderWorkspaceDetails(s,c){
  if(!sessionView||sessionView.section!=='details')return;
  const cache=evidenceCache[s.session_id]||{},status=s.status_line||c?.info?.status_line;
  $('#sdetailindex').innerHTML=['overview','placement','notifications','continuation'].map(id=>`<a href="#detail-${id}" onclick="event.preventDefault();document.getElementById('detail-${id}').scrollIntoView({behavior:'smooth',block:'start'})">${id[0].toUpperCase()+id.slice(1)}</a>`).join('');
  $('#sdetails').innerHTML=`<section class="detailsection" id="detail-overview"><h2>Overview</h2><div class="kv">
      <span>provider</span><b>${esc(s.provider||'claude')}</b><span>session ID</span>${cpb(s.session_id)}
      <span>status</span><b>${esc(s.state||'closed')}</b><span>model</span><b>${esc(s.model||'?')}</b>
      <span>effort</span><b>${esc(s.effort||'—')}</b><span>mode</span><b>${esc(s.collaboration_mode||'—')}</b>
      <span>permission</span><b>${esc(s.permission_mode?claudePermissionLabel(s.permission_mode):'—')}</b>
      <span>started</span><b>${s.started_ms?fmtAge(Math.max(0,Math.round(Date.now()/1000-s.started_ms/1000)))+' ago':'unavailable'}</b>
      <span>context</span><b>${s.ctx_tokens==null?'unavailable':s.ctx_window?`${fmtTok(s.ctx_tokens)} / ${fmtTok(s.ctx_window)} (${s.ctx_pct}%)`:fmtTok(s.ctx_tokens)}</b>
      <span>spend</span><b>${s.cost==null?'unavailable':`${fmt$(s.cost)} session + ${fmt$(s.agent_cost||0)} agents`}</b>
      ${s.error?`<span>errors</span><b>${esc(s.error)}</b>`:''}</div>${statusLineHtml(status,'details:'+s.session_id)}</section>
    <section class="detailsection" id="detail-placement"><h2>Placement</h2><div class="evidencerule"><span>Current placement</span><b>${esc(s.reason_label||s.ui_group||'History')}</b><code>${esc(s.winning_rule||'placement.unknown')}</code></div>
      ${evidenceFactsHtml(s)}<div class="evidencehistory">${cache.error?`<div class="evidenceerror">${esc(cache.error)}</div>`:''}${(cache.events||[]).map(evidenceEventHtml).join('')}
      ${cache.loading?'<div class="ctxload">loading placement history…</div>':''}${cache.loaded&&!(cache.events||[]).length?'<div class="evidenceempty">No earlier transitions recorded.</div>':''}
      ${cache.next_cursor&&!cache.loading?`<button class="historyaction" onclick="loadSessionEvidence('${enc(s.session_id)}',true)">Load older</button>`:''}</div></section>
    <section class="detailsection" id="detail-notifications"><h2>Notifications</h2><div class="mutebox"><button class="bell ${s.muted?'muted':''}" ${sessionView.closed?'disabled':''}
      onclick="toggleMute('${s.session_id}',${s.muted?'false':'true'},'dmsg-${s.session_id}')">${s.muted?'🔕':'🔔'}</button><span>${sessionView.closed?'Notification controls are unavailable for a closed session.':s.muted?'Push notifications muted for this session.':'Notifications follow your session policy.'}</span></div><div class="actmsg" id="dmsg-${s.session_id}"></div></section>
    <section class="detailsection" id="detail-continuation"><h2>Continuation</h2>${handoffLinksHtml(s)}
      ${sessionView.closed?`<p>${s.can_resume_and_send?'The first text send resumes this exact saved session.':esc(s.resume_disabled_reason||'This saved session cannot be resumed.')}</p>`:''}
      ${s.bridge_url?`<a class="jump" href="${esc(s.bridge_url)}" target="_blank" rel="noreferrer">open external session ↗</a>`:''}
      ${s.can_reopen?`<button class="pbtn" onclick="reopenClosed('${s.session_id}',this)">reopen in terminal</button>`:''}</section>`;
  if(!cache.loaded&&!cache.loading)loadSessionEvidence(enc(s.session_id));
  if(sessionView.closed)renderClosedComposer(s,c);else renderParentWorkspaceAction(s,c,{pending:true});
}
const closedResumeRequests=new Map(),closedResumeWarned=new Set();
function renderClosedComposer(s,c){
  const act=$('#sact'),can=Boolean(s.can_resume_and_send),reason=s.resume_disabled_reason||'This saved session cannot be resumed.';
  const draft=draftValue(composerDraftKey(s.session_id));
  act.classList.toggle('session-composer',can);act.classList.remove('question-present','tools-open');
  act.innerHTML=`<div class="session-context"><div class="relaynote"><b>closed session</b> — ${can?'Your first text send resumes this exact session. Images and scheduling stay disabled until it is live.':esc(reason)}</div>
    ${handoffLinksHtml(s)}${statusLineHtml(s.status_line||c?.info?.status_line,'closed:'+s.session_id)}</div><div class="freetext composer"><textarea id="closedft-${esc(s.session_id)}" data-draft-key="${esc(composerDraftKey(s.session_id))}" rows="2" ${can?'':'disabled'}
      placeholder="${can?'resume and send to this exact session':esc(reason)}" oninput="setDraft('${esc(composerDraftKey(s.session_id))}',this.value)" onkeydown="composerKey(event,()=>requestResumeAndSend('${enc(s.session_id)}'))">${esc(draft)}</textarea>
      <button class="pbtn send" ${can?'':'disabled'} onclick="requestResumeAndSend('${enc(s.session_id)}')">Resume & send</button></div><div class="actmsg" id="smsg"></div>`;
}
function requestResumeAndSend(encodedSid){
  const sid=decodeURIComponent(encodedSid),input=document.getElementById('closedft-'+sid),text=(input?.value||'').trim();if(!text)return;
  let request=closedResumeRequests.get(sid);
  if(!request||request.text!==text)request={id:'resume-'+offlineMessageId(),text};closedResumeRequests.set(sid,request);
  if(!closedResumeWarned.has(request.id)){
    closedResumeWarned.add(request.id);askConfirm('Resume this exact session?',
      'Fleet will reopen the saved session, wait until it accepts input, and deliver this text exactly once. Images and scheduling remain off until the session is live.',
      'resume and send',()=>sendClosedResume(sid,request));return;
  }
  sendClosedResume(sid,request);
}
async function sendClosedResume(sid,request){
  const input=document.getElementById('closedft-'+sid),button=input?.parentElement?.querySelector('button');
  if(button){button.disabled=true;button.textContent='Resuming…';}
  const result=await act(sid,{type:'resume_and_send',text:request.text,client_request_id:request.id},'smsg');
  if(result.ok){clearDraft(composerDraftKey(sid));if(input)input.value='';
    rememberOutboxReceipt({outboxId:result.outbox_id,sid,text:request.text,status:'queued',queueLabel:'Resuming session',queueReason:'Waiting for the exact saved session to accept input',created:Date.now()});
    closedResumeRequests.delete(sid);renderClosed(true);setTimeout(()=>tick(true),500);
  }else{closedResumeRequests.delete(sid);if(button){button.disabled=false;button.textContent='Resume & send';}}
}
async function renderClosed(){
  if(!sessionView||!sessionView.closed)return;
  const sid=sessionView.sid;
  const body=$('#sbody'),meta=closedSession(sid)||{};
  if(!closedCtx[sid]){
    const saved=savedConversation('closed',sid);
    closedCtx[sid]={...(saved||{messages:[],info:{}}),fetching:true};
    if(!saved)body.innerHTML='<div class="ctxload">loading conversation…</div>';
    try{
      const r=await fetch(conversationEndpoint('closed',sid),{cache:'no-store'});
      const d=await r.json();
      if(!r.ok||!d.ok)throw new Error(d.error||'unavailable');
      closedCtx[sid]=mergeFreshConversation(saved,{messages:d.messages||[],files:d.files||[],agents:d.agents||[],
        closed:true,info:d.info||{},next_cursor:d.next_cursor,message_total:d.message_total});
      persistConversation('closed',sid,'',closedCtx[sid]);
    }catch(e){closedCtx[sid]={...(saved||{messages:[],info:{}}),fetching:false,
      stale:Boolean(saved),error:String(e.message||e)};}
    if(!sessionView||sessionView.sid!==sid)return;      // closed while fetching
  }
  const c=closedCtx[sid],info=c.info||{};
  const s={...meta,...info,session_id:sid,closed:true,state:'closed',agents:c.agents||[],
    provider:meta.provider||(sid.startsWith('codex:')?'codex':'claude'),capabilities:{}};
  renderWorkspaceChrome(s,c);
  if(sessionView.section==='files')return renderWorkspaceFiles(true);
  if(sessionView.section==='subagents')return renderWorkspaceSubagents(true);
  if(sessionView.section==='details')return renderWorkspaceDetails(s,c);
  renderClosedComposer(s,c);
  if(c.fetching){body.innerHTML='<div class="ctxload">loading conversation…</div>';return;}
  const old={top:body.scrollTop,atBottom:body.scrollTop+body.clientHeight>=body.scrollHeight-12};
  const wantBottom=sessionOpened||old.atBottom;sessionOpened=false;
  const optimistic=visibleOptimistic(sid,c.messages||[]).map(item=>`${item.id}:${item.status}:${item.error||''}`).join('|');
  const bodyKey=`closed:${c.messages?.length??-1}:${c.next_cursor??''}:${c.olderError||''}:${c.error||''}:${optimistic}`;
  if(body.dataset.renderKey!==bodyKey){
    if(c.error&&!c.messages.length)body.innerHTML=`<div class="ctxload">✗ ${esc(c.error)}</div>`;
    else if(!c.messages.length)body.innerHTML='<div class="ctxload">no conversation recorded</div>';
    else body.innerHTML=`${c.stale?'<div class="ctxload">offline · showing saved conversation</div>':''}<div class="aconvo">${convoMsgs(c,sid,true,'closed')}</div>`;
    body.dataset.renderKey=bodyKey;
    body.scrollTop=wantBottom?body.scrollHeight:old.top;
  }else if(wantBottom)body.scrollTop=body.scrollHeight;
  // Finish the initial closed-chat pin before yielding back to input. An
  // animation-frame callback races the reader: on mobile it can run after an
  // immediate scroll and yank the archive back to the bottom.
  if(wantBottom)queueMicrotask(()=>{const b=$('#sbody');if(b)b.scrollTop=b.scrollHeight;});
}
async function reopenClosed(sid,button){
  const original=button&&button.textContent;
  if(button){button.disabled=true;button.textContent='opening…';}
  const result=await act(sid,{type:'reopen'},'reopenmsg');
  if(button){
    button.textContent=result.ok?'opened ✓':original;
    if(!result.ok)button.disabled=false;
  }
  if(result.ok){reopenedSessions.add(sid);renderHistoryDestination();setTimeout(()=>tick(),500);}
  return result;
}
function renderSession(force){
  if(!sessionView||!last)return;
  if(sessionView.closed)return renderClosed(force);
  const current=(last.sessions||[]).find(x=>x.session_id===sessionView.sid)||
    (spawnProvisional&&spawnProvisional.id===sessionView.sid?provisionalSessionObject():null);
  if(current){sessionView.lastGood=current;sessionView.missingSince=null;}
  else if(closedIds.has(sessionView.sid)){
    sessionView={...sessionView,closed:true};sessionOpened=true;renderClosed(force);return;
  }else if(!sessionView.lastGood){closeSession();return;}
  else if(!sessionView.missingSince)sessionView.missingSince=Date.now();
  const missing=!current;
  const s=current||{...sessionView.lastGood,state:'reconnecting',reason_label:'Reconnecting',
    capabilities:{...(sessionView.lastGood.capabilities||{}),submit:false,
      queue_submit:Boolean(sessionView.lastGood.capabilities?.queue_submit)}};
  if(s.provisional)return renderProvisionalSession(s);
  if(missing){
    const elapsed=Math.max(0,Date.now()-sessionView.missingSince),host=$('#sactivity');
    host.innerHTML=`<div class="sessionactivity"><span class="worksignal"><i class="workpulse slow" aria-hidden="true"></i>${elapsed<30000?'Reconnecting to session':'Session unavailable · saved chat retained'}</span></div>`;
    host.dataset.renderKey=`missing:${elapsed<30000?'reconnecting':'unavailable'}`;
  }else renderSessionActivity(s);
  ensureCtx(s.session_id,ctxVersion(s));
  const c=ctxCache[s.session_id];
  renderWorkspaceChrome(s,c);
  if(sessionView.section==='files')return renderWorkspaceFiles(force);
  if(sessionView.section==='subagents')return renderWorkspaceSubagents(force);
  if(sessionView.section==='details')return renderWorkspaceDetails(s,c);
  const ae=document.activeElement;
  const typing=ae&&['INPUT','TEXTAREA'].includes(ae.tagName)&&$('#sview').contains(ae);
  const done=['done','ended'];
  // A focused composer must not freeze transcript confirmation. The composer
  // itself is preserved below; only defer the body repaint during an active
  // touch gesture so mobile scrolling is not interrupted.
  if(questionResizeActive||(!force&&touching()))return;
  const body=$('#sbody');
  const old={top:body.scrollTop};
  const opened=sessionOpened;
  sessionOpened=false;
  const optimisticItems=visibleOptimistic(s.session_id,(c&&c.messages)||[]);
  const optimisticRevision=optimisticItems.map(item=>`${item.id}:${item.status}:${item.imageCount||0}:${item.error||''}`).join('|');
  const canonicalKey=`session:${c?.v??'loading'}:${c?.messages?.length??-1}:${c?.next_cursor??''}:${c?.olderError||''}`;
  const bodyKey=`${canonicalKey}:${optimisticRevision}`;
  const receiptOnlyChange=Boolean(body.dataset.canonicalKey===canonicalKey&&body.dataset.renderKey!==bodyKey);
  const wantBottom=opened||(sessionFollowTail&&!receiptOnlyChange);
  if(body.dataset.renderKey!==bodyKey){
    const optimisticOnly=optimisticItems.map(item=>optimisticItemHtml(item)).join('');
    if((!c||!c.messages)&&optimisticOnly)body.innerHTML=`<div class="aconvo">${optimisticOnly}</div>`;
    else if(!c||!c.messages)body.innerHTML='<div class="ctxload">loading conversation…</div>';
    else if(!c.messages.length&&optimisticOnly)body.innerHTML=`<div class="aconvo">${optimisticOnly}</div>`;
    else if(!c.messages.length)body.innerHTML='<div class="ctxload">no conversation yet</div>';
    else body.innerHTML=`<div class="aconvo">${convoMsgs(c,s.session_id)}</div>`;
    body.dataset.renderKey=bodyKey;body.dataset.canonicalKey=canonicalKey;
    if(wantBottom)pinSessionTail(body);else body.scrollTop=old.top;
  }else if(wantBottom)pinSessionTail(body);
  observeSessionTail();
  if(typing){
    refreshStatusStrip('#sact',s.status_line,'session:'+s.session_id);
    return;                                // never replace the input being typed into
  }
  const layoutAnchor=captureReadingAnchor(body);
  renderParentWorkspaceAction(s,c,{pending:true,showFiles:true});restoreReadingAnchor(layoutAnchor);
  // #sact just shrank #sbody — re-pin to the true bottom after layout settles
  if(wantBottom){sessionFollowTail=true;scheduleSessionTailPin();}
}

// ---- subagent chat overlay -------------------------------------------------
// A subagent has NO tty: its "send" box relays through the PARENT session (the
// parent forwards with SendMessage), so it is labelled as a relay, not a channel.
let agentView=null;              // {sid, aid} of the open overlay
const agentCache={};             // parent session + aid -> {v, messages, info}
const agentCacheKey=(sid,aid)=>String(sid||'')+'\0'+String(aid||'');
let agentInfoOpen2=false;        // the info dropdown INSIDE the overlay
function openAgent(sid,aid){
  seedTargetedWorkspaceHistory(sid,'subagents');
  openSessionWorkspace(sid,'subagents',aid,true);
}
function closeAgent(){
  closeOverflow();agentView=null;agentInfoOpen2=false;
}
function agentMeta(){
  if(!agentView)return null;
  return workspaceAgents(workspaceSessionModel(),workspaceContext()).find(a=>a.agent_id===agentView.aid)||null;
}
async function ensureAgentCtx(){
  if(!agentView)return;
  const a=agentMeta(),aid=agentView.aid,key=agentCacheKey(agentView.sid,aid);
  const v=a?a.convo_v:0;
  if(!agentCache[key])agentCache[key]=savedConversation('agent',agentView.sid,aid)||undefined;
  const c=agentCache[key];
  if(c&&c.v===v&&!c.stale)return;
  if(c&&(c.fetching||(c.error&&(fleetOffline||Date.now()<Number(c.retryAt||0)))))return;
  agentCache[key]={...(c||{}),fetching:true};
  try{
    const r=await fetch(conversationEndpoint('agent',agentView.sid,aid),{cache:'no-store'});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'unavailable');
    agentCache[key]=mergeFreshConversation(c,{v,messages:d.messages||[],info:d.info||{},
      next_cursor:d.next_cursor,message_total:d.message_total});
    delete agentCache[key].stale;delete agentCache[key].error;delete agentCache[key].retryAt;
    persistConversation('agent',agentView.sid,aid,agentCache[key]);
  }catch(e){agentCache[key]={...(c||{messages:[],info:{}}),v,fetching:false,
    stale:Boolean(c),error:String(e.message||e),retryAt:Date.now()+5000};}
  renderAgent(true);
}
function renderAgent(force){
  if(!agentView||sessionView?.section!=='subagents')return;
  ensureAgentCtx();
  const a=agentMeta(),c=agentCache[agentCacheKey(agentView.sid,agentView.aid)];
  const info=(c&&c.info)||{};
  const done=a?terminalAgentStates.has(a.state):true;
  $('#atitle').innerHTML=`<b>${esc(info.agent_type||(a&&a.agent_type)||'subagent')}</b>
    <small>${esc(info.description||(a&&a.description)||'')}</small>`;
  const par=workspaceSessionModel();
  const ae=document.activeElement;
  const typing=ae&&['INPUT','TEXTAREA'].includes(ae.tagName)&&$('#sact').contains(ae);
  if(!force&&touching())return;
  const body=$('#abody');
  const old={top:body.scrollTop,atBottom:body.scrollTop+body.clientHeight>=body.scrollHeight-12};
  const bodyKey=`agent:${c?.v??'loading'}:${c?.messages?.length??-1}:${c?.next_cursor??''}:${c?.olderError||''}:${c?.error||''}`;
  if(body.dataset.renderKey!==bodyKey){
    if(!c||!c.messages){body.innerHTML='<div class="ctxload">loading conversation…</div>';}
    else if(c.error&&!c.messages.length){body.innerHTML=`<div class="ctxload">✗ ${esc(c.error)}</div>`;}
    else if(!c.messages.length){body.innerHTML='<div class="ctxload">no conversation yet</div>';}
    else body.innerHTML=`${c.stale?'<div class="ctxload">offline · showing saved conversation</div>':''}<div class="aconvo">${convoMsgs(c,agentView.sid,false,'agent',agentView.aid)}</div>`;
    body.dataset.renderKey=bodyKey;
    body.scrollTop=old.atBottom?body.scrollHeight:old.top;
  }
  if(!typing){
    const parentWaiting=par?.state==='needs_you'||Boolean(par?.pending);
    const codex=par?.provider==='codex';
    const reason=sessionView.closed?'This saved parent session is closed.':done?
      `This agent is ${esc(a?.state||'finished')} and remains readable, but cannot receive relays.`:
      parentWaiting?'The parent is waiting for your input. Relaying now could answer the parent request instead.':
      codex?'The message is sent to the parent thread with an explicit relay instruction.':'The message is typed into the parent session and tagged for SendMessage.';
    $('#sact').classList.remove('session-composer','question-present','tools-open');
    $('#sact').innerHTML=`<div class="session-context"><div class="relaynote">${reason}</div>
      ${parentWaiting?`<button class="pbtn" onclick="setSessionSection('chat')">Jump to parent request</button>`:''}
      ${statusLineHtml(info.status_line,'agent:'+agentView.sid+':'+agentView.aid)}</div>
      ${!sessionView.closed&&!done&&!parentWaiting&&par?.capabilities?.relay_agent?`<div class="freetext composer relay-composer"><textarea id="aft" data-draft-key="${esc(relayDraftKey(agentView.sid,agentView.aid))}" rows="2" placeholder="relay via parent  ·  ⌘/Ctrl+Return relay" autocomplete="off"
        oninput="setDraft('${esc(relayDraftKey(agentView.sid,agentView.aid))}',this.value)" onkeydown="composerKey(event,sendRelay)">${esc(draftValue(relayDraftKey(agentView.sid,agentView.aid)))}</textarea>
        <button class="pbtn send" onclick="sendRelay()">Relay</button></div>`:''}
      ${agentRelayHtml(agentView.sid,agentView.aid)}<div class="actmsg" id="amsg"></div>`;
  }else refreshStatusStrip('#sact',info.status_line,'agent:'+agentView.sid+':'+agentView.aid);
}
const agentRelays=new Map();
function agentRelayKey(sid,aid){return agentCacheKey(sid,aid);}
function agentRelayHtml(sid,aid){
  const item=agentRelays.get(agentRelayKey(sid,aid));if(!item)return'';
  if(item.status==='uncertain')return`<div class="quickfeedback failed" role="alert"><span class="qfstate">Relay unconfirmed</span><span class="qftext">${esc(item.error||'Check the parent Claude terminal before doing anything else.')}</span></div>`;
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
  current.status=result.ok?'sent':result.code==='delivery_uncertain'?'uncertain':'failed';
  current.error=result.error||'';renderAgent(true);
  if(result.ok)setTimeout(()=>{if(agentRelays.get(key)===current){agentRelays.delete(key);renderAgent(true);}},5000);
}
function agentRow(a,buildTap){
  const done=['done','ended'].includes(a.state);
  const signal={running:'working',stalled:'quiet — may still be working',done:'finished',
    ended:'stopped'}[a.state]||String(a.state||'unknown');
  const ago=ts=>ts?fmtAge(Math.max(0,Math.round((Date.now()-Date.parse(ts))/1000)))+' ago':'?';
  const tk=a.tokens||{};
  // tapping opens the subagent conversation — the caller chooses the destination
  // (the completed fold keeps the standalone overlay; the card list routes into
  // the session workspace's Subagents section)
  const tap=buildTap?buildTap(a):`agentTap(event,'${a.session_id}','${a.agent_id}')`;
  return`<div class="arow ${done?'done-row':''}" style="padding-left:${12+a.depth*16}px;cursor:pointer"
    title="open this subagent's conversation" onclick="${tap}">
    <span class="tree">⎿</span>
    <span class="dot ${a.state}" role="img" aria-label="${esc(signal)}" title="${esc(signal)}"></span>
    <span class="atype">${esc(a.agent_type)}</span>
    <span class="adesc">${esc(a.description)}</span>
    ${!done?spark(a.spark):''}
    <span class="amodel">${modelLabel(a)}</span>
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
function agentListHtml(agents,buildTap){
  return agents.map(agent=>agentRow(agent,buildTap)).join('');
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
let sessionPressTimer=null,sessionPressTarget=null,sessionPressSid=null,sessionLongFired=false;
function sessionPressStart(sid,target){
  sessionPressEnd();sessionPressSid=sid;sessionPressTarget=target||null;
  if(sessionPressTarget)sessionPressTarget.classList.add('pinpress');
  sessionLongFired=false;
  sessionPressTimer=setTimeout(()=>{sessionLongFired=true;toggleSessionPin(sid);
    if(navigator.vibrate)navigator.vibrate(15);sessionPressEnd();},500);
}
function sessionPressEnd(){
  if(sessionPressTimer){clearTimeout(sessionPressTimer);sessionPressTimer=null;}
  if(sessionPressTarget){sessionPressTarget.classList.remove('pinpress');sessionPressTarget=null;}
  sessionPressSid=null;
}
function sessionTap(e,sid){
  if(sessionLongFired){sessionLongFired=false;e.stopPropagation();return;}
  openSession(sid);
}
function sessionHeaderKey(event,sid){
  if(event.target!==event.currentTarget||!['Enter',' '].includes(event.key))return;
  event.preventDefault();openSession(sid);
}
function agentTap(e,sid,aid){
  e.stopPropagation();
  openAgent(sid,aid);
}
// The session card's running-agent list routes into the session workspace's
// Subagents section with that agent selected, rather than the standalone
// overlay. Every row in that list is non-terminal, so the section's default
// Active filter (invariant 37) always has it visible.
function cardAgentTap(e,encodedSid,encodedAid){
  e.stopPropagation();
  selectWorkspaceAgent(encodedSid,encodedAid);
}
const cardAgentTapAttr=a=>`cardAgentTap(event,'${enc(a.session_id)}','${enc(a.agent_id)}')`;
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
  if(s.provisional||expandedPeeks.has(s.session_id))return false;
  const pending=s.pending&&(!s.pending.nonce||answered[s.session_id]!==s.pending.nonce);
  // the running-agent list renders on any card with live agents, so the fixed
  // frame must lift wherever it appears or the list is clipped (invariant 45)
  const running=(s.agents||[]).some(a=>!terminalAgentStates.has(a.state));
  const answerFeedback=optimisticList(s.session_id).some(item=>item.kind==='answer'||item.status==='queued');
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
  const navigationOnly=!s.primary_action||['open','continue','view'].includes(s.primary_action);
  const showPrimary=s.ui_group!=='needs_you'&&!(navigationOnly&&['claude','codex'].includes(s.provider));
  // delivered-file chips + the session peek both need the context cache; the
  // conversation itself now lives only in the full view
  if(previewSessions()&&s.last_msg)ensureCtx(s.session_id,ctxVersion(s));
  const pinned=pinnedSessions.has(s.session_id);
  return`<div class="shead${sessionPressSid===s.session_id?' pinpress':''}" role="button" tabindex="0" aria-label="Open chat: ${esc(s.title||s.project||'session')}"
      title="open the full conversation" onclick="sessionTap(event,'${s.session_id}')" onkeydown="sessionHeaderKey(event,'${s.session_id}')"
      ontouchstart="sessionPressStart('${s.session_id}',this)" ontouchend="sessionPressEnd()" ontouchmove="sessionPressEnd()">
      ${s.new_response?'<span class="newdot" role="img" aria-label="new response" title="new response"></span>':''}
      <span class="chip ${s.ui_group||s.state}${s.reason_label==='Fix needed'?' problem':''}">${esc(s.reason_label||stateLabel[s.state]||s.state)}</span>
      <span class="sname">${s.title?`<span class="stitle">${esc(s.title)}</span><small>${esc(s.project)}${s.branch&&s.branch!=='HEAD'?` · ${esc(s.branch)}`:''}</small>`:`${esc(s.project)}${s.branch&&s.branch!=='HEAD'?` <small>· ${esc(s.branch)}</small>`:''}`}</span>
      ${s.access==='view_only'?`<span class="accessbadge view_only">view only</span>`:''}
      <span class="m" title="session provider">${esc(s.provider||'claude')}</span>
      ${showPrimary?`<button class="primarybtn" onclick="event.stopPropagation();primarySessionAction('${s.session_id}')">${esc(s.primary_action_label||'Open')}</button>`:''}
      ${terminalButton(s,true)}
      <button class="spin${pinned?' on':''}" ${pinActions.get(s.session_id)?.busy?'disabled':''} title="${pinned?'unpin session':'pin session'}"
        aria-label="${pinned?'unpin session':'pin session'}"
        onclick="event.stopPropagation();toggleSessionPin('${s.session_id}')">📌</button>
    </div>
    <div class="smeta" role="button" tabindex="0" aria-label="Open chat: ${esc(s.title||s.project||'session')}"
      title="open the full conversation" onclick="sessionTap(event,'${s.session_id}')" onkeydown="sessionHeaderKey(event,'${s.session_id}')">
      <div class="smeta-l">
        ${s.agents_running?`<span class="m"><b style="color:var(--green)">${s.agents_running} agent${s.agents_running>1?'s':''}</b></span>`:''}
        ${s.running?`<span class="m runskill" title="the skill or slash command this turn is running">${esc(s.running)}</span>`:''}
        ${s.compacting!=null?`<span class="m compacting" title="a compaction is running — the transcript is frozen until it finishes">⧉ compacting ${fmtAge(s.compacting)}</span>`:''}
        ${!['available','needs_you'].includes(s.ui_group)?`<span class="squiet">quiet ${fmtAge(s.quiet_s)}</span>`:''}
      </div>
      <div class="smeta-r">
        ${s.ctx_pct==null?(s.provider==='codex'?'':`<span class="m">${fmtTok(s.ctx_tokens||0)} tok</span>`):`<span class="ctxwrap"><span>${s.ctx_pct}%</span><span class="ctxbar"><i style="width:${Math.min(s.ctx_pct||0,100)}%;background:${s.ctx_pct>=60?'var(--red)':s.ctx_pct>=50?'var(--amber)':'var(--blue)'}"></i></span></span>`}
        <span class="m amodel">${modelLabel(s)}</span>
      </div>
    </div>
    ${previewSessions()&&s.last_msg?`<div class="lastmsg sessionpeek${expandedPeeks.has(s.session_id)?' expanded':''}" title="${expandedPeeks.has(s.session_id)?'full peek exposed':'latest message'}" onclick="togglePeekFromTap(event,'${s.session_id}',${expandedPeeks.has(s.session_id)?'true':'false'})"><span class="lmwho ${s.last_msg.role}">${s.last_msg.role==='user'?'you':esc(s.provider||'claude')}</span><div class="peekbody"><div class="lmtext peekmd" style="--peek-lines:${clampS()}">${peekMd(s.last_msg.text)}</div><button class="peektoggle ${expandedPeeks.has(s.session_id)?'less':'more'}" type="button" aria-label="${expandedPeeks.has(s.session_id)?'collapse latest message':'expand latest message'}" onclick="event.stopPropagation();togglePeek('${s.session_id}',${expandedPeeks.has(s.session_id)?'false':'true'})">${expandedPeeks.has(s.session_id)?'Less':'...'}</button></div></div>`:''}
    ${s.error?`<div class="lastmsg"><span class="lmwho">provider</span><span class="lmtext">${esc(s.error)}</span></div>`:''}
    ${s.reply_requested&&!s.staging_observer?`<div class="replysignal"><span>Waiting for your reply</span><button onclick="event.stopPropagation();markAvailable('${s.session_id}','${enc(String(s.convo_v||''))}')">mark available</button></div>`:''}
    ${pinFeedbackHtml(s.session_id)}
    ${cardResponseFeedback(s)}
    ${cardPending(s)}
    ${cardAgentList(s)}`;
}
// The card's running-subagent list: every non-terminal agent, in engine tree
// order so agentRow's depth indentation still describes the hierarchy — never
// sorted and never capped. It renders on any card with live agents, not just
// Working ones, so background work stays visible while you triage.
function cardAgentList(s){
  const running=(s.agents||[]).filter(agent=>!terminalAgentStates.has(agent.state));
  if(!running.length)return'';
  return`<div class="agents" aria-label="Active subagents">${agentListHtml(running,cardAgentTapAttr)}</div>`;
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
  const frame=cardFrame(s);
  return`<div class="card ${cardCls(s)}${frame.fixed?' fixedpeek':''}" data-sid="${s.session_id}"
    style="--session-card-lines:${frame.lines}">
    <div class="ctop">${cardTop(s)}</div></div>`;
}
function cardTopFocusAnchor(top){
  const focused=document.activeElement;
  if(!focused||!top.contains(focused))return null;
  const controls=[...top.querySelectorAll('button,a[href],input,select,textarea,[tabindex]')];
  return{id:focused.id||'',aria:focused.getAttribute('aria-label')||'',
    title:focused.getAttribute('title')||'',name:focused.getAttribute('name')||'',
    tag:focused.tagName,text:['BUTTON','A'].includes(focused.tagName)?focused.textContent.trim():'',
    index:controls.indexOf(focused)};
}
function restoreCardTopFocus(top,anchor){
  if(!anchor)return;
  const controls=[...top.querySelectorAll('button,a[href],input,select,textarea,[tabindex]')];
  let target=anchor.id?controls.find(item=>item.id===anchor.id):null;
  if(!target&&anchor.aria)target=controls.find(item=>item.getAttribute('aria-label')===anchor.aria);
  if(!target&&anchor.title)target=controls.find(item=>item.getAttribute('title')===anchor.title);
  if(!target&&anchor.name)target=controls.find(item=>item.getAttribute('name')===anchor.name);
  if(!target&&anchor.text)target=controls.find(item=>item.tagName===anchor.tag&&item.textContent.trim()===anchor.text);
  if(!target&&anchor.index>=0)target=controls[anchor.index];
  if(target&&!target.disabled&&!target.closest('[inert]')&&target.getClientRects().length)
    target.focus({preventScroll:true});
}
// Reconcile #sessions in place: persist each card node, rebuild only the volatile
// top every poll, and rebuild the tail only when detailSig changes. This keeps an
// expanded card's open <details> from remounting (and flashing) every tick. A
// focused header control is semantically restored because innerHTML necessarily
// replaces that node; otherwise a poll immediately after closing chat drops
// keyboard focus onto <body>.
function reconcileCards(container,list,emptyMessage='no live sessions'){
  if(!list.length){container.innerHTML=emptyMessage?`<div class="empty">${esc(emptyMessage)}</div>`:'';return;}
  if(container.querySelector('.empty'))container.innerHTML='';
  const seen=new Set();
  list.forEach(s=>{
    seen.add(s.session_id);
    let card=container.querySelector('.card[data-sid="'+s.session_id+'"]');
    if(!card){
      card=document.createElement('div');card.dataset.sid=s.session_id;
      const top=document.createElement('div');top.className='ctop';card.appendChild(top);
      container.appendChild(card);
    }
    const frame=cardFrame(s);
    card.className='card'+(cardCls(s)?' '+cardCls(s):'')+
      (pinnedSessions.has(s.session_id)?' pinned':'')+(frame.fixed?' fixedpeek':'');
    card.style.setProperty('--session-card-lines',String(frame.lines));
    const top=card.querySelector('.ctop'),focusAnchor=cardTopFocusAnchor(top);
    top.innerHTML=cardTop(s);restoreCardTopFocus(top,focusAnchor);
    const detail=card.querySelector(':scope > .detail');if(detail)detail.remove();delete card.dataset.dsig;
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
const SETTINGS_SECTIONS=['notifications','devices','sessions','appearance','budgets','advanced'];
const SETTINGS_LABELS={notifications:'Notifications',devices:'Devices & delivery',sessions:'Sessions',
  appearance:'Appearance',budgets:'Budgets & spawning',advanced:'Advanced'};
let settingsOpen=false,budgetSettingsOpen=false,settingsReturnState=null,settingsRenderFrame=0;
let settingsRendering=false,settingsRerenderPending=false,settingsFocusPending=false;
let settingsMessage='';
let settingsSection=(initialDestination.route==='settings'&&SETTINGS_SECTIONS.includes(initialDestination.detail))?
  initialDestination.detail:'notifications';
let notificationPolicy={ok:true,global:null,kinds:[],muted_sessions:[],next_deliveries:[],recent_deliveries:[]};
let notificationPolicyLoading=false,notificationPolicyError='';
let notificationPolicySaveError='';
let globalPolicySaving=false,globalPolicyStatus='';
let notificationGuideOpen=false;
const policyRuleOpen=new Set(),policyApplyCurrent=new Set(),policySaving=new Map();
function uiRefresh(){render(last,true);if(settingsOpen)renderSettings();}
async function loadNotificationPolicy(force=false){
  if(notificationPolicyLoading)return;
  if(!force&&notificationPolicy.global)return;
  notificationPolicyLoading=true;notificationPolicyError='';if(settingsOpen&&['notifications','sessions'].includes(settingsSection))renderSettings();
  try{const response=await fetch('/api/notification-policy',{cache:'no-store'}),data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'Notification policy unavailable');
    notificationPolicy=data;
  }catch(error){notificationPolicyError=String(error.message||error);}
  finally{notificationPolicyLoading=false;if(settingsOpen&&['notifications','sessions'].includes(settingsSection))renderSettings();}
}
function selectSettingsSection(section){
  if(!SETTINGS_SECTIONS.includes(section))return;
  settingsMessage='';
  if(section!==settingsSection){settingsSection=section;settingsSectionDepth++;
    history.pushState({fdSettingsSection:section},'',`#settings/${section}`);}
  $('#settings').scrollTop=0;renderSettings(true);
  if(section==='notifications')loadNotificationPolicy();
  if(section==='devices')loadPushState(true);
  if(section==='budgets')loadBudgets();
}
function openSettings(section=settingsSection){
  if(settingsOpen)return;
  if(SETTINGS_SECTIONS.includes(section))settingsSection=section;settingsSectionDepth=0;
  const stacked=anyOverlay();
  settingsReturnState=sessionView?{sid:sessionView.sid,closed:sessionView.closed,
    scrollTop:$('#sbody')?.scrollTop||0,evidenceOpen:sessionEvidenceOpen}:null;
  settingsOpen=true;settingsMessage='';
  $('#settingsview').style.display='flex';
  $('#settings').scrollTop=0;
  $('#settings').innerHTML='<div class="ctxload"><span class="delivery sending" aria-hidden="true">◌</span> loading settings…</div>';
  cancelAnimationFrame(settingsRenderFrame);
  settingsRenderFrame=requestAnimationFrame(()=>{
    settingsRenderFrame=0;if(settingsOpen)renderSettings(true);
  });
  loadPushState(true);
  loadNotificationPolicy(true);
  loadBudgets();
  loadWorkstreams();
  if(stacked){settingsPushed=true;history.pushState({fdSettings:1},'');}
  else syncOverlayHistory();
  history.replaceState(history.state,'',`#settings/${settingsSection}`);
}
function closeSettings(){
  const restore=settingsReturnState;settingsReturnState=null;
  cancelAnimationFrame(settingsRenderFrame);settingsRenderFrame=0;
  settingsOpen=false;settingsSectionDepth=0;
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
function settingsHasEditableFocus(){const active=document.activeElement;
  if(!active||!$('#settings')?.contains(active))return false;
  if(active.matches('textarea,select,[contenteditable="true"]'))return true;
  return active.matches('input')&&!['checkbox','radio','button','submit','reset','range','color','file'].includes(active.type);}
function flushFocusedSettingsRender(){
  if(!settingsFocusPending||settingsHasEditableFocus())return;
  settingsFocusPending=false;renderSettings();
}
function renderSettings(force=false){
  if(settingsRendering){settingsRerenderPending=true;return;}
  if(!force&&settingsOpen&&settingsHasEditableFocus()){settingsFocusPending=true;return;}
  settingsRendering=true;
  const el=$('#settings');
  try{
    if(!settingsOpen){el.innerHTML='';return;}
    const sectionHtml={notifications:notificationPolicySettingsHtml,devices:deviceSettingsHtml,
      sessions:sessionSettingsHtml,appearance:appearanceSettingsHtml,budgets:budgetSectionHtml,
      advanced:advancedSettingsHtml}[settingsSection]();
    el.innerHTML=`<div class="settingsapp"><nav class="settingsrail" aria-label="Settings sections">
        ${SETTINGS_SECTIONS.map(section=>`<button class="${section===settingsSection?'active':''}" aria-current="${section===settingsSection?'page':'false'}" onclick="selectSettingsSection('${section}')"><span>${esc(SETTINGS_LABELS[section])}</span></button>`).join('')}</nav>
      <div class="settingsmobile"><label>Section<select onchange="selectSettingsSection(this.value)">${SETTINGS_SECTIONS.map(section=>`<option value="${section}" ${section===settingsSection?'selected':''}>${esc(SETTINGS_LABELS[section])}</option>`).join('')}</select></label></div>
      <section class="settingscontent"><header><h2>${esc(SETTINGS_LABELS[settingsSection])}</h2><p>${esc(settingsSectionDescription(settingsSection))}</p></header>${sectionHtml}<div class="actmsg" id="setmsg">${esc(settingsMessage)}</div></section></div>`;
  }finally{
    settingsRendering=false;
    if(settingsRerenderPending){settingsRerenderPending=false;requestAnimationFrame(()=>renderSettings());}
  }
}
$('#settings').addEventListener('focusout',()=>setTimeout(flushFocusedSettingsRender,100));
function settingsSectionDescription(section){return{
  notifications:'Choose exactly which events can push, when they fire, and when they repeat.',
  devices:'Install Fleet, connect browsers, and verify delivery health.',
  sessions:'Control stalled-session timing and every persistent session mute.',
  appearance:'Tune navigation, reading width, and conversation previews.',
  budgets:'Set visibility and spawn guardrails from measured local usage.',
  advanced:'Diagnostics and manual-only legacy delivery controls.'}[section]||'';}
function settingNumber(st,key,step=1,min=0){return`<input type="number" value="${st[key]??''}" min="${min}" step="${step}" oninput="setNum('${key}',this.value)">`;}
function timeValue(minutes){const value=Math.max(0,Math.min(1439,Number(minutes)||0));return`${String(Math.floor(value/60)).padStart(2,'0')}:${String(value%60).padStart(2,'0')}`;}
function timeMinutes(value){const [hours,minutes]=String(value).split(':').map(Number);return hours*60+minutes;}
function cadenceSummary(rule){if(rule.mode==='off')return'No external pushes';
  const delay=rule.initial_delay_seconds?`after ${durationShort(rule.initial_delay_seconds)}`:'now';
  if(rule.mode==='once')return`Once · ${delay}`;
  if(rule.mode==='remind_once')return`${delay} · one reminder after ${durationShort(rule.repeat_interval_seconds)}`;
  return`${delay} · every ${durationShort(rule.repeat_interval_seconds)} · max ${rule.max_deliveries}`;}
function durationShort(seconds){seconds=Number(seconds)||0;return seconds%86400===0?`${seconds/86400}d`:seconds%3600===0?`${seconds/3600}h`:seconds%60===0?`${seconds/60}m`:`${seconds}s`;}
function durationParts(seconds){seconds=Math.max(0,Number(seconds)||0);
  if(seconds&&seconds%86400===0)return[seconds/86400,86400];
  if(seconds&&seconds%3600===0)return[seconds/3600,3600];
  if(seconds%60===0)return[seconds/60,60];return[seconds,1];}
function policyDurationField(rule,field,label,help,locked=false){const [amount,unit]=durationParts(rule[field]),id=`policy-${rule.kind}-${field}`,disabled=locked?'disabled':'';
  return`<label>${label}<span class="durationinput"><input id="${id}-amount" type="number" min="${field==='initial_delay_seconds'?0:1}" max="604800" value="${amount}" ${disabled} onchange="savePolicyDuration('${rule.kind}','${field}')"><select id="${id}-unit" aria-label="${esc(label)} unit" ${disabled} onchange="savePolicyDuration('${rule.kind}','${field}')">${[[1,'seconds'],[60,'minutes'],[3600,'hours'],[86400,'days']].map(([value,name])=>`<option value="${value}" ${unit===value?'selected':''}>${name}</option>`).join('')}</select></span><small class="settinghelp">${esc(help)}</small></label>`;}
function savePolicyDuration(kind,field){const id=`policy-${kind}-${field}`,amount=Number(document.getElementById(id+'-amount')?.value),unit=Number(document.getElementById(id+'-unit')?.value),seconds=Math.round(amount*unit),minimum=field==='initial_delay_seconds'?0:60;
  if(!Number.isFinite(amount)||!Number.isFinite(unit)||amount<0||seconds<minimum||seconds>604800){policySaving.set(kind,field==='initial_delay_seconds'?'error · enter 0 seconds to 7 days':'error · enter 1 minute to 7 days');renderSettings();return;}
  saveKindPolicy(kind,{[field]:seconds});}
function cadenceStrip(rule){if(rule.mode==='off')return'<span class="cadenceoff">Off</span>';
  const count=rule.mode==='once'?1:rule.mode==='remind_once'?2:Math.min(5,rule.max_deliveries);
  return`<span class="cadencestrip">${Array.from({length:count},(_,index)=>`<i class="${index?'repeat':''}"></i>${index<count-1?'<b></b>':''}`).join('')}</span>`;}
function notificationPolicySettingsHtml(){
  if(notificationPolicyLoading&&!notificationPolicy.global)return'<div class="ctxload"><span class="delivery sending">◌</span> Loading notification policy…</div>';
  if(notificationPolicyError)return`<div class="pusherror">${esc(notificationPolicyError)} <button onclick="loadNotificationPolicy(true)">retry</button></div>`;
  const global=notificationPolicy.global;if(!global)return'<div class="ctxload">Notification policy unavailable</div>';
  const onRules=(notificationPolicy.kinds||[]).filter(rule=>rule.mode!=='off').length;
  const inAppRules=(notificationPolicy.kinds||[]).filter(rule=>rule.in_app_enabled!==false).length;
  const next=(notificationPolicy.next_deliveries||[])[0];
  const recent=(notificationPolicy.recent_deliveries||[])[0];
  const recentState=recent?String(recent.status||'unknown').replaceAll('_',' '):'';
  const globalDisabled=globalPolicySaving?'disabled':'';
  return`${notificationPolicySaveError?`<div class="pusherror" role="alert">${esc(notificationPolicySaveError)}</div>`:''}<div class="policyanswer"><span class="pushsignal ${global.enabled?'healthy':'off'}"></span><span><b>In app ${inAppRules}/${notificationPolicy.kinds.length} · Push ${global.enabled?'on':'off'} · ${onRules}/${notificationPolicy.kinds.length} push rules · ${pushData.enabled_devices||0} device${(pushData.enabled_devices||0)===1?'':'s'}</b><small><span>${next?`Next: ${esc(next.title)} · ${notificationTime(next.next_attempt_at)}`:global.quiet_now?`Quiet hours until ${notificationTime(global.quiet_ends_at)}`:'No delivery currently scheduled'}</span><span>${recent?`Last delivery: ${esc(recentState)} · ${esc(recent.title)} · ${notificationTime(recent.updated_at)}`:'No delivery attempt recorded yet'}</span></small></span></div>
    <details class="policyguide" ${notificationGuideOpen?'open':''} ontoggle="notificationGuideOpen=this.open"><summary><span><b>What these settings mean</b><small>In-app visibility, Web Push cadence, severity, and which rule wins</small></span><span aria-hidden="true">⌄</span></summary><div class="policyguidebody"><p><b>Show in Fleet</b> controls the Notification Center, unread/active badges, and app badge for that event type. <b>Web Push</b> is separate: cadence limits external pushes to connected devices. Turning push off does not hide in-app events; turning in-app off does not silently change push cadence.</p><div class="severityladder"><span><b>Info</b><small>Routine updates. Selecting Info includes every severity.</small></span><span><b>Warning</b><small>Needs attention or has stopped progressing.</small></span><span><b>Critical</b><small>A severe budget, repository, or scheduled-delivery failure.</small></span></div><p><b>What wins for Web Push:</b> Push off → device paused or unavailable → session mute → event snooze → quiet hours → event rule. One global push policy applies to every enabled device.</p></div></details>
    <div class="settingscard"><label class="settingsswitch"><span><b>External push notifications</b><small>Turn automatic Web Push on or off for every enabled device. In-app events stay visible.</small></span><input type="checkbox" ${global.enabled?'checked':''} ${globalDisabled} onchange="saveGlobalPolicy({enabled:this.checked})"></label>
      <label class="settingsswitch"><span><b>Quiet hours</b><small>Hold external pushes during this window and send at most one held push per event afterward.</small></span><input type="checkbox" ${global.quiet_hours_enabled?'checked':''} ${globalDisabled} onchange="saveGlobalPolicy({quiet_hours_enabled:this.checked})"></label>
      ${global.quiet_hours_enabled?`<div class="quietgrid"><label>Starts<input aria-label="Starts" type="time" value="${timeValue(global.quiet_start_minute)}" ${globalDisabled} onchange="saveGlobalPolicy({quiet_start_minute:timeMinutes(this.value)})"><small class="settinghelp">The local time when Fleet starts holding pushes.</small></label><label>Ends<input aria-label="Ends" type="time" value="${timeValue(global.quiet_end_minute)}" ${globalDisabled} onchange="saveGlobalPolicy({quiet_end_minute:timeMinutes(this.value)})"><small class="settinghelp">The local time when held events may push again.</small></label><label>Timezone<input aria-label="Timezone" value="${esc(global.timezone)}" list="fleet-timezones" ${globalDisabled} onchange="saveGlobalPolicy({timezone:this.value})"><datalist id="fleet-timezones"><option value="${esc(Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC')}"><option value="UTC"></datalist><small class="settinghelp">Interprets start and end times, including daylight-saving changes.</small></label></div>`:''}${globalPolicyStatus?`<div class="setsavestate" role="status">${esc(globalPolicyStatus)}</div>`:''}</div>
    <div class="settingssubhead"><span><b>Event rules</b><small>One global policy applies to every enabled device.</small></span><button onclick="dismissOverlay();setTimeout(()=>navigateTo('notifications'),0)">View delivery history</button></div>
    <div class="policyrules">${notificationPolicy.kinds.map(policyRuleHtml).join('')}</div>`;
}
function policyRuleHtml(rule){const open=policyRuleOpen.has(rule.kind),saving=policySaving.get(rule.kind)||'',locked=saving==='saving…',disabled=locked?'disabled':'';
  const max=rule.mode==='once'?1:rule.mode==='remind_once'?2:rule.max_deliveries;
  const inApp=rule.in_app_enabled!==false;
  return`<details class="policyrule" data-policy-kind="${esc(rule.kind)}" ${open?'open':''} ontoggle="this.open?policyRuleOpen.add('${rule.kind}'):policyRuleOpen.delete('${rule.kind}')"><summary><span><b>${esc(notificationKinds[rule.kind]||rule.kind)}</b><small>In app ${inApp?'on':'off'} · Push: ${esc(cadenceSummary(rule))}${rule.active_matches?` · ${rule.active_matches} active`:''}</small></span>${cadenceStrip({...rule,max_deliveries:max})}<em class="${saving.startsWith('error')?'error':''}">${esc(saving||'')}</em></summary><div class="policycontrols">
    <p class="policykindhelp">${esc(notificationKindDescriptions[rule.kind]||'Controls external pushes for this event type.')}</p>
    <label class="policycheck"><input aria-label="Show ${esc(notificationKinds[rule.kind]||rule.kind)} in Fleet" type="checkbox" ${inApp?'checked':''} ${disabled} onchange="saveKindPolicy('${rule.kind}',{in_app_enabled:this.checked})"><span>Show in Fleet Notification Center<small class="settinghelp">Controls in-app history, unread and active counts, the page title, and app badge for this event type. Independent of Web Push.</small></span></label>
    <label>Web Push cadence<select aria-label="Web Push cadence" ${disabled} onchange="saveKindPolicy('${rule.kind}',{mode:this.value})">${[['off','Off'],['once','Once'],['remind_once','Once + reminder'],['repeat','Repeat until resolved']].map(([value,label])=>`<option value="${value}" ${rule.mode===value?'selected':''}>${label}</option>`).join('')}</select><small class="settinghelp">How many external pushes Fleet may send while this event remains unresolved. Off affects Web Push only.</small></label>
    <label>Minimum severity<select aria-label="Minimum severity" ${disabled} onchange="saveKindPolicy('${rule.kind}',{minimum_severity:this.value})">${notificationSeverityOptions.map(([value,label])=>`<option value="${value}" ${rule.minimum_severity===value?'selected':''}>${label}</option>`).join('')}</select><small class="settinghelp">Filters this rule by Fleet-assigned urgency; it does not change sound, color, or presentation.</small></label>
    ${rule.mode!=='off'?policyDurationField(rule,'initial_delay_seconds','Initial delay','How long Fleet waits after the event begins before the first push.',locked):''}
    ${['remind_once','repeat'].includes(rule.mode)?policyDurationField(rule,'repeat_interval_seconds','Reminder interval','Time between successful pushes while the event still needs attention.',locked):''}
    ${rule.mode==='repeat'?`<label>Maximum deliveries<input aria-label="Maximum deliveries" type="number" min="1" max="100" value="${rule.max_deliveries}" ${disabled} onchange="saveKindPolicy('${rule.kind}',{max_deliveries:Number(this.value)})"><small class="settinghelp">Includes the first successful push; automatic transport retries do not count.</small></label>`:''}
    ${rule.mode!=='off'?`<label class="policycheck"><input type="checkbox" ${rule.allow_during_quiet_hours?'checked':''} ${disabled} onchange="saveKindPolicy('${rule.kind}',{allow_during_quiet_hours:this.checked})"><span>Allow during quiet hours<small class="settinghelp">Bypasses quiet hours only. Device pause, session mute, and event snooze still win.</small></span></label>`:''}
    ${rule.active_matches?`<label class="policycheck"><input type="checkbox" ${policyApplyCurrent.has(rule.kind)?'checked':''} onchange="this.checked?policyApplyCurrent.add('${rule.kind}'):policyApplyCurrent.delete('${rule.kind}')"><span>Apply this change to ${rule.active_matches} active event${rule.active_matches===1?'':'s'}<small class="settinghelp">Without this, the changed rule starts with future or revised events.</small></span></label>`:''}</div></details>`;}
async function saveGlobalPolicy(patch){const global=notificationPolicy.global;if(!global||globalPolicySaving)return;
  const previous={...global};notificationPolicySaveError='';globalPolicySaving=true;globalPolicyStatus='saving…';Object.assign(global,patch);renderSettings();
  try{const data=await pushApi('/api/notification-policy',{scope:'global',expected_revision:previous.revision,patch});notificationPolicy=data;}
  catch(error){notificationPolicy.global=previous;notificationPolicySaveError=String(error.message||error);}
  finally{globalPolicySaving=false;globalPolicyStatus=notificationPolicySaveError?'':'saved ✓';renderSettings();if(globalPolicyStatus)setTimeout(()=>{globalPolicyStatus='';if(settingsOpen&&settingsSection==='notifications')renderSettings();},1200);}}
async function saveKindPolicy(kind,patch,confirmAggressive=false){const rule=notificationPolicy.kinds.find(item=>item.kind===kind);if(!rule)return;
  if(policySaving.get(kind)==='saving…')return;
  const candidate={...rule,...patch},possible=candidate.mode==='repeat'?Math.min(candidate.max_deliveries,1+Math.floor(86400/candidate.repeat_interval_seconds)):candidate.mode==='remind_once'?2:1;
  if(possible>12&&!confirmAggressive){askConfirm('High notification cadence',`This rule can send up to ${possible} pushes per day until the event resolves.`,
      'use high cadence',()=>saveKindPolicy(kind,patch,true),true);return;}
  const previous={...rule};notificationPolicySaveError='';Object.assign(rule,patch);policySaving.set(kind,'saving…');renderSettings();
  try{const data=await pushApi('/api/notification-policy',{scope:'kind',kind,expected_revision:previous.revision,
      patch,apply_current:policyApplyCurrent.has(kind),confirm_aggressive:confirmAggressive});notificationPolicy=data;
    if(Object.prototype.hasOwnProperty.call(patch,'in_app_enabled'))void loadNotifications(true,true);
    policySaving.set(kind,'saved ✓');setTimeout(()=>{if(policySaving.get(kind)==='saved ✓'){policySaving.delete(kind);renderSettings();}},1200);}
  catch(error){Object.assign(rule,previous);policySaving.set(kind,`error · ${String(error.message||error)}`);}renderSettings();}
function deviceSettingsHtml(){const devices=pushData.devices||[];return`${pushSettingsHtml()}
  <div class="settingssubhead"><span><b>Connected devices</b><small>Pause applies only to the selected device. Event rules are global.</small></span></div>
  <div class="devicelist">${devices.length?devices.map(device=>{const current=device.id===briefingDevice,id=enc(device.id),name=device.display_name||'Fleet device';return`<article class="devicecard ${current?'current':''}" data-push-device="${esc(device.id)}"><span class="pushsignal ${esc(device.health||'off')}"></span><span class="deviceidentity"><b>${esc(name)}</b><small>${esc([device.platform,String(device.health||'unknown').replaceAll('_',' ')].filter(Boolean).join(' · '))}</small></span>${current?'<em>this device · controls above</em>':`<div class="devicecontrols"><input aria-label="Rename ${esc(name)}" value="${esc(name)}" maxlength="80" onchange="updateListedPushDevice(decodeURIComponent('${id}'),{display_name:this.value.trim()})"><button onclick="testListedPushDevice(decodeURIComponent('${id}'))" ${device.enabled?'':'disabled'}>Test</button><button onclick="updateListedPushDevice(decodeURIComponent('${id}'),{enabled:${device.enabled?'false':'true'}})">${device.enabled?'Pause':'Resume'}</button><button class="danger" onclick="confirmForgetPushDevice(decodeURIComponent('${id}'),decodeURIComponent('${enc(name)}'))">Remove</button><small class="devicefeedback" role="status"></small></div>`}</article>`;}).join(''):'<div class="settingsempty">No device is connected yet.</div>'}</div>`;}
let sessionMuteQuery=localStorage.getItem('settings_mute_query')||'';
function setSessionMuteQuery(value){sessionMuteQuery=String(value||'');localStorage.setItem('settings_mute_query',sessionMuteQuery);
  const query=sessionMuteQuery.trim().toLowerCase(),rows=[...document.querySelectorAll('.mutelist article[data-search]')];let shown=0;
  rows.forEach(row=>{const visible=!query||String(row.dataset.search||'').includes(query);row.hidden=!visible;if(visible)shown++;});
  const empty=document.querySelector('.mutelist .mute-search-empty');if(empty){empty.hidden=shown>0;empty.textContent=rows.length?'No muted sessions match.':'No sessions are muted.';}}
function sessionSettingsHtml(){const st=(last&&last.settings)||{},muted=notificationPolicy.muted_sessions||[],query=sessionMuteQuery.trim().toLowerCase();
  return`<div class="settingscard"><label class="settingsfield"><span><b>Stalled-session threshold</b><small>How long a working session can show no progress before Fleet marks it stalled.</small></span><span>${settingNumber(st,'stall_seconds',30,30)} seconds</span></label></div>
    <div class="settingssubhead"><span><b>Muted sessions</b><small>Muted until you manually unmute them.</small></span><em>${muted.length}</em></div>
    ${muted.length?`<label class="settingssearch"><span>Search muted sessions</span><input type="search" value="${esc(sessionMuteQuery)}" placeholder="session, project, or provider" oninput="setSessionMuteQuery(this.value)"></label>`:''}
    <div class="mutelist">${muted.map(item=>{const session=((last||{}).sessions||[]).find(s=>s.session_id===item.session_id),search=[item.session_id,item.provider,session?.title,session?.project].filter(Boolean).join(' ').toLowerCase(),visible=!query||search.includes(query);return`<article data-search="${esc(search)}" ${visible?'':'hidden'}><span><b>${esc(session?.title||session?.project||item.session_id)}</b><small>${esc(item.provider||session?.provider||'session')}</small></span><button onclick="unmuteSettingsSession(decodeURIComponent('${enc(item.session_id)}'))">Unmute</button></article>`;}).join('')}<div class="settingsempty mute-search-empty" ${muted.length&&muted.some(item=>{const session=((last||{}).sessions||[]).find(s=>s.session_id===item.session_id);return !query||[item.session_id,item.provider,session?.title,session?.project].filter(Boolean).join(' ').toLowerCase().includes(query);})?'hidden':''}>${muted.length?'No muted sessions match.':'No sessions are muted.'}</div></div>`;}
async function unmuteSettingsSession(sid){const previous=[...(notificationPolicy.muted_sessions||[])];notificationPolicy.muted_sessions=previous.filter(item=>item.session_id!==sid);renderSettings();
  const result=await queueSetting('mute:'+sid,{mute_session:sid,muted:false},()=>{},()=>{notificationPolicy.muted_sessions=previous;});
  if(result.ok)loadNotificationPolicy(true);}
function appearanceSettingsHtml(){const st=(last&&last.settings)||{};return`
  <div class="settingscard"><div class="settingsfield"><span><b>Desktop navigation</b><small>Mobile always uses the bottom navigation.</small></span><div class="setchoice" role="group" aria-label="Desktop navigation side"><button aria-pressed="${navSide==='left'}" onclick="setNavSide('left')">Left side</button><button aria-pressed="${navSide==='right'}" onclick="setNavSide('right')">Right side</button></div></div>
    <div class="settingsfield"><span><b>Full-screen reading width</b><small>Session chat, Markdown, and subagent conversations.</small></span><div class="setchoice" role="group" aria-label="Full-screen reading width"><button aria-pressed="${(st.reader_width||'fit')==='fit'}" onclick="setStr('reader_width','fit')">Fit the screen</button><button aria-pressed="${st.reader_width==='centered'}" onclick="setStr('reader_width','centered')">Centered</button></div></div>
    <label class="settingsswitch"><span><b>Session conversation peek</b><small>Fixed card height follows this line count.</small></span><input type="checkbox" ${st.preview_sessions!==false?'checked':''} onchange="setBool('preview_sessions',this.checked)"></label>
    <label class="settingsfield"><span><b>Session peek height</b><small>Collapsed conversation rows.</small></span><span>${settingNumber(st,'preview_session_lines',1,1)} lines</span></label>
    <label class="settingsswitch"><span><b>Subagent conversation peek</b><small>Show a short latest-message preview on subagent rows.</small></span><input type="checkbox" ${st.preview_agents?'checked':''} onchange="setBool('preview_agents',this.checked)"></label>
    <label class="settingsfield"><span><b>Subagent peek height</b></span><span>${settingNumber(st,'preview_agent_lines',1,1)} lines</span></label></div>`;}
function budgetSectionHtml(){return`<div class="settingscard budgetsection">${budgetSettingsHtml()}</div>`;}
function advancedSettingsHtml(){const st=(last&&last.settings)||{};return`
  <div class="settingscard"><div class="settingssubhead"><span><b>Legacy ntfy</b><small>Manual test delivery only. Never an automatic fallback.</small></span></div>
    <label class="settingsswitch"><span><b>Enable manual legacy tests</b><small>${st.legacy_ntfy_configured?'Configured':'Add ntfy_server and ntfy_topic to config.json first.'}</small></span><input type="checkbox" ${st.legacy_ntfy_enabled===true?'checked':''} onchange="setBool('legacy_ntfy_enabled',this.checked)"></label>
    <div class="pushactions"><button onclick="testLegacyNtfy()" ${legacyNtfyBusy||st.legacy_ntfy_enabled!==true||!st.legacy_ntfy_configured?'disabled':''}>${legacyNtfyBusy?'<span class="delivery sending">◌</span> Queuing…':'Send legacy test'}</button></div>
    ${legacyNtfyMessage?`<div class="${legacyNtfyError?'pusherror':'pushnotice'}" role="status">${esc(legacyNtfyMessage)}</div>`:''}</div>
  <div class="settingscard"><div class="settingssubhead"><span><b>Diagnostics</b><small>Operational evidence contains no subscription keys or transcript content.</small></span><button onclick="window.open('/api/diagnostics','_blank','noopener')">Open diagnostics</button></div></div>`;}
const settingQueues=new Map(),settingIntents=new Map();
function settingMessage(id,text){if(id==='setmsg')settingsMessage=String(text||'');const element=document.getElementById(id);if(element)element.textContent=text;}
function queueSetting(key,payload,onSuccess,onFailure,messageId='setmsg',refreshSection=true){
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
    onSuccess(data);refreshSection?uiRefresh():render(last,true);settingMessage(messageId,'saved ✓');return data;
  }).catch(error=>{
    if(settingIntents.get(key)===intent){onFailure();refreshSection?uiRefresh():render(last,true);settingMessage(messageId,'✗ '+String(error.message||error));}
    return{ok:false,error:String(error.message||error)};
  }).finally(()=>{if(settingQueues.get(key)===request)settingQueues.delete(key);});
}
async function setNum(k,v){
  const value=parseFloat(v),previous=last?.settings?.[k];
  if(!Number.isFinite(value))return{ok:false,error:'enter a number'};
  if(last?.settings)last.settings[k]=value;render(last,true);
  return queueSetting(k,{[k]:value},d=>{if(last?.settings)last.settings[k]=d[k];},()=>{
    if(last?.settings)last.settings[k]=previous;
    const field=document.querySelector(`input[oninput="setNum('${CSS.escape(k)}',this.value)"]`);
    if(field)field.value=previous??'';
  },'setmsg',false);
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
async function toggleMute(sid,mute,msgId){
  const s=((last||{}).sessions||[]).find(x=>x.session_id===sid);if(!s)return;
  const previous=s.muted;s.muted=mute;uiRefresh();
  return queueSetting('mute:'+sid,{mute_session:sid,muted:mute},()=>{},()=>{s.muted=previous;},
    msgId||'msg-'+sid);
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
  const locked=nativePromptLocked(s,p);
  return`<div class="ptool"><span class="ptlabel">multi-part question (${qs.length}) — ${esc(nativePromptLabel(s))}</span>
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
    const optimisticId=beginOptimisticAnswer(sid,nonce,answerPreview(sid,answers));
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
  if(s.delivery_uncertain?.nonce===p.nonce)return`<div class="pend deliveryuncertain" role="alert">
    <div class="ptool"><span class="ptlabel">delivery uncertain</span></div>
    <div class="qtext">Check the Claude terminal before doing anything else. Fleet will not retry this answer because some keys may already have landed.</div>
    <button class="pbtn" onclick="event.stopPropagation();tick(true)">refresh state</button></div>`;
  if(p.kind==='permission')return pendingBox(s,'msg');
  if(p.kind==='elicitation')return`<div class="pend qsignal" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">
    <div class="ptool"><span class="ptlabel">${esc(p.server||'MCP')} request — waiting on you</span></div>
    <button class="pbtn qanswer" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">respond ⤢</button></div>`;
  if(p.kind!=='question'||!p.questions||!p.questions.length)return'';
  const n=p.questions.length;
  const label=n>1?`multi-part question (${n})`:(p.questions[0].header||'question');
  return`<div class="pend qsignal" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">
    <div class="ptool"><span class="ptlabel">${esc(label)} — ${esc(nativePromptLabel(s))}</span></div>
    ${p.files&&p.files.length?`<div class="pfiles"><span class="plabel">read first</span>${p.files.map(f=>fchip(s.session_id,f,f.caption)).join('')}</div>`:''}
    <button class="pbtn qanswer" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">answer ⤢</button>
  </div>`;
}
function openSessionQ(sid){
  const session=((last||{}).sessions||[]).find(item=>item.session_id===sid),pending=session?.pending;
  if(pending?.kind==='question'){
    questionPanelState(sid,pending.nonce).collapsed=false;persistQuestionPanels();
  }
  openSession(sid);
}
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
  if(s.delivery_uncertain?.nonce===p.nonce)return`<div class="pend deliveryuncertain" role="alert">
    <div class="ptool"><span class="ptlabel">delivery uncertain — check terminal</span></div>
    <div class="qtext">Some answer keys may already have reached Claude. Fleet has disabled retry for this request until the native prompt changes.</div>
    <div class="pbtns"><button class="pbtn" onclick="tick(true)">refresh state</button></div></div>`;
  if(p.kind==='question'){
    if(!p.questions||!p.questions.length)return'';
    return`<div class="pend">${p.questions.length>1?mqBlock(s,p,pre):singleQBlock(s,p,pre)}
      <div class="actmsg" id="${pre}-${s.session_id}"></div></div>`;
  }
  if(p.kind==='permission'){
    const locked=nativePromptLocked(s,p);
    return`<div class="pend">
      <div class="ptool">permission: ${esc(p.tool)} — ${esc(nativePromptLabel(s))}</div>
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
    if(message())message().textContent=d.warning?`mode changed ✓ · ${d.warning}`:'mode changed ✓';
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
    if(d.code==='control_delivery_uncertain'){
      providerModeActions.delete(s.session_id);uiRefresh();
      if(message())message().textContent='⚠ '+(d.error||'Permission change unconfirmed — check Claude');
      return;
    }
    if(!r.ok||!d.ok)throw new Error(d.error||'permission mode change failed');
    s.permission_mode=d.mode||mode;providerModeActions.delete(s.session_id);uiRefresh();
    if(message())message().textContent=d.warning?`permissions changed ✓ · ${d.warning}`:'permissions changed ✓';
  }catch(e){s.permission_mode=previous;providerModeActions.delete(s.session_id);uiRefresh();
    if(message())message().textContent='✗ '+String(e.message||e);}
}
function changeSessionModel(sid,model){
  const s=((last&&last.sessions)||[]).find(item=>item.session_id===sid);
  if(!s||providerModeActions.has(sid))return;
  const current=sessionSettingValues(s);
  saveSessionSettings(s,model,repairedSessionEffort(s.provider,model,current.effort));
}
function changeSessionEffort(sid,effort){
  const s=((last&&last.sessions)||[]).find(item=>item.session_id===sid);
  if(!s||providerModeActions.has(sid))return;
  const current=sessionSettingValues(s);
  saveSessionSettings(s,current.model,effort);
}
async function saveSessionSettings(s,model,effort){
  if(!s?.capabilities?.change_model_effort||providerModeActions.has(s.session_id))return;
  const catalog=spawnCatalog(s.provider),entry=catalog.find(item=>item.id===model);
  const efforts=entry?.efforts?.length?[...entry.efforts]:spawnEfforts(s.provider,model);
  if(!entry||!efforts.includes(effort))return;
  const sid=s.session_id,version=++sessionSettingSequence;
  // CAS always starts from the newest provider snapshot. Settled action state is
  // feedback only; it must never hide or overwrite a later native TUI change.
  const previous={model:catalogModelForSession(s,catalog),effort:s.effort||''};
  const expectedModel=s.model||'';
  const expectedEffort=s.effort||'';
  providerModeActions.set(sid,{kind:'settings',version});
  // Replacing the state clears any prior saved/error feedback and repairs the
  // effort selector in the same paint as a model selection.
  sessionSettingActions.set(sid,{version,model,effort,message:'Saving…',error:false,pending:true});
  uiRefresh();
  try{
    const response=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'session_settings',model,effort,
        expected_model:expectedModel,expected_effort:expectedEffort})});
    const data=await response.json();
    if(data.code==='control_delivery_uncertain'){
      if(providerModeActions.get(sid)?.version!==version)return;
      providerModeActions.delete(sid);
      sessionSettingActions.set(sid,{version,model,effort,
        message:'Unconfirmed · '+(data.error||'check the Claude terminal'),
        error:true,warning:true,pending:false});
      uiRefresh();return;
    }
    if(!response.ok||!data.ok)throw new Error(data.error||'settings change failed');
    if(providerModeActions.get(sid)?.version!==version)return;
    const acceptedModel=String(data.model||model),acceptedEffort=String(data.effort||'');
    s.model=acceptedModel;s.effort=acceptedEffort||null;
    providerModeActions.delete(sid);
    sessionSettingActions.set(sid,{version,model:acceptedModel,effort:acceptedEffort,
      message:data.warning?`Applied ✓ · ${data.warning}`:'Saved ✓',error:false,
      warning:Boolean(data.warning),pending:false});
    uiRefresh();
  }catch(error){
    if(providerModeActions.get(sid)?.version!==version)return;
    providerModeActions.delete(sid);
    sessionSettingActions.set(sid,{version,model:previous.model,
      effort:repairedSessionEffort(s.provider,previous.model,previous.effort),
      message:'Could not save · '+String(error.message||error),error:true,pending:false});
    uiRefresh();
  }
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
    const deliveryUncertain=d.code==='delivery_uncertain';
    if(deliveryUncertain){
      const session=((last||{}).sessions||[]).find(item=>item.session_id===sid);
      if(session)session.delivery_uncertain={nonce:payload.nonce,message:d.error};
      if(optimisticId!=null)markOptimisticUncertain(sid,optimisticId,d.error);
      uiRefresh();
    }else if(optimisticId!=null)updateOptimistic(sid,optimisticId,d.ok,d.error,
      d.ok&&['option','multiq'].includes(payload.type));
    if(optimisticId!=null&&d.queued){const item=optimisticList(sid).find(entry=>entry.id===optimisticId);
      if(item){clearTimeout(item.confirmTimer);item.status='queued';delete item.queueId;item.outboxId=d.outbox_id;
        item.queueLabel=d.message||'Queued · waiting for session';
        item.queueReason=d.queue_reason||d.message||'Waiting for the session to become available';
        rememberOutboxReceipt(item);uiRefresh();}}
    if(quickId!=null)finishQuickResponse(sid,quickId,d.ok,d.error);
    const el=setMessage(d.ok?(d.queued?(d.message||'queued'):'sent ✓'):'✗ '+(d.error||'failed'));
    if(!d.ok&&!el&&quickId==null&&optimisticId==null&&payload.type!=='focus'&&pre!==false)
      alert(d.error||'failed');
    if(d.ok&&payload.nonce&&['option','multiq','permission','dismiss','dismiss_then_send','elicitation'].includes(payload.type)){
      answered[sid]=payload.nonce;      // retain immediate nonce suppression through canonical QA
      clearDraftPrefix(questionDraftPrefix(sid,payload.nonce));
      delete otherDraft[sid];delete mqSel[sid];delete elicitDraft[sid];multiSel[sid]=new Set();
      uiRefresh();
    }
    return d;
  }catch(e){
    if(payload.type!=='ping')perfRecord(`native_${String(payload.type).replace(/[^a-z0-9_]+/gi,'_')}_ms`,
      performance.now()-requestStarted);
    const nativeAnswer=['option','multiq'].includes(payload.type);
    const error=nativeAnswer?
      'Delivery uncertain — the connection dropped before Fleet received a result. Check the terminal before answering again.':
      ['send_message','dismiss_then_send'].includes(payload.type)?
      'Delivery unconfirmed — the connection dropped before Fleet received a result. Restore to send again only if it did not arrive.':String(e);
    if(optimisticId!=null){
      if(nativeAnswer)markOptimisticUncertain(sid,optimisticId,error);
      else updateOptimistic(sid,optimisticId,false,error);
    }
    if(quickId!=null)finishQuickResponse(sid,quickId,false,String(e));
    const el=setMessage('✗ '+error);
    if(!el&&quickId==null&&optimisticId==null&&payload.type!=='focus'&&pre!==false)
      alert('request failed: '+e);
    setFleetOffline(true);
    return {ok:false,error,network_error:true};
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
    multiSel[sid]=new Set(digits);
    const optimisticId=beginOptimisticAnswer(sid,nonce,answerPreview(sid,[{digits}]));
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
    const optimisticId=beginOptimisticAnswer(sid,nonce,answerPreview(sid,[{digits,other}]));
    return act(sid,{type:'option',nonce,digits,multi:true,n_options:n,other:other||undefined},pre,optimisticId);
  });
}
function sendOther(sid,nonce,n,pre){
  const other=(otherDraft[sid]||'').trim();
  if(!other)return alert('type your answer first');
  return withNativeRequestLock(sid,nonce,()=>{
    const optimisticId=beginOptimisticAnswer(sid,nonce,answerPreview(sid,[{other}]));
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
  closeComposerMenus();
  const message=document.getElementById('smsg-'+sid)||document.getElementById('vmsg-'+sid),
    current=imageDraftIds(sid);
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
  const anchor=captureReadingAnchor();
  if(!ids.length){target.innerHTML='';restoreReadingAnchor(anchor);return;}
  const records=(await Promise.all(ids.map(id=>getImage(id).catch(()=>null)))).filter(Boolean);
  if(!document.getElementById('imgdraft-'+sid))return;
  const missing=ids.filter(id=>!records.some(record=>record.id===id));
  if(missing.length)setImageDraftIds(sid,ids.filter(id=>!missing.includes(id)));
  target.innerHTML=records.map(record=>`<span class="image-draft">🖼 <span>${esc(record.name||'image')}</span><small>${Math.max(1,Math.round(record.size/1024))} KB</small><button type="button" aria-label="remove ${esc(record.name||'image')}" onclick="removeImageDraft(decodeURIComponent('${enc(sid)}'),'${record.id}')">×</button></span>`).join('');
  restoreReadingAnchor(anchor);
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
    const queued=queueOfflineText(sid,text,imageIds,null,pendingQuestion(sid)?.nonce||null);
    if(!queued){if(el)el.textContent='offline queue is full — draft kept here';return;}
    slashClose();closeComposerMenus();if(inp){inp.value='';resizeComposer(inp);}clearDraft(composerDraftKey(sid));setImageDraftIds(sid,[]);void renderImageDrafts(sid);
    if(el)el.textContent='queued offline — sends automatically after reconnection';
    return;
  }
  if(cmd&&cmd.danger&&!confirm(`${cmd.name} destroys this session's conversation state.\n\n${cmd.desc}\n\nSend it?`))return;
  slashClose();closeComposerMenus();
  const pending=pendingQuestion(sid);
  if(pending){
    let uploadIds=[];
    if(imageIds.length){
      if(el)el.textContent='uploading images…';
      const uploaded=await uploadImages(sid,imageIds);
      if(!uploaded.ok){
        if(uploaded.network_error&&offlineMessages.length<100){
          const queued=queueOfflineText(sid,text,imageIds,null,pending.nonce);
          if(queued){clearSentComposerCapture(sid,ftPre,v,imageIds);
            if(el)el.textContent='queued offline — dismisses the question, then sends after reconnection';}
          return;
        }
        if(el)el.textContent='✗ '+uploaded.error;return;
      }
      uploadIds=uploaded.uploadIds;
    }
    if(el)el.textContent='dismissing question…';
    const result=await act(sid,{type:'dismiss_then_send',nonce:pending.nonce,text,
      upload_ids:uploadIds,client_request_id:'send-'+offlineMessageId()},msgPre);
    if(!result.ok)return result;
    clearSentComposerCapture(sid,ftPre,v,imageIds);
    const optimisticId=addOptimistic(sid,text,'text',result.queued?'queued':'sending',null,null,imageIds);
    const item=optimisticList(sid).find(entry=>entry.id===optimisticId);
    if(item&&result.queued){item.outboxId=result.outbox_id;
      item.queueLabel=result.message||'Queued · waiting for session';
      item.queueReason=result.queue_reason||'Question dismissed; waiting for the session to become available';
      rememberOutboxReceipt(item);uiRefresh();}
    if(!result.queued&&imageIds.length)void deleteImages(imageIds);
    return result;
  }
  if(cmd&&['action','skill'].includes(cmd.execution)){
    if(commandSendLocks.has(sid))return;
    commandSendLocks.add(sid);if(el)el.textContent='sending command…';
    const payload=cmd.execution==='action'?{type:cmd.action}:
      {type:'skill',name:cmd.name,args:v.slice(cmd.name.length).trim()};
    const result=await act(sid,payload,msgPre);
    commandSendLocks.delete(sid);
    if(result.ok&&inp&&inp.value.trim()===v){inp.value='';resizeComposer(inp);clearDraft(composerDraftKey(sid));}
    else if(!result.ok)setDraft(composerDraftKey(sid),inp?.value||v);
    return result;
  }
  if(inp){inp.value='';resizeComposer(inp);}clearDraft(composerDraftKey(sid));setImageDraftIds(sid,[]);void renderImageDrafts(sid); // sending is the only automatic clear
  const optimisticId=addOptimistic(sid,text,'text','sending',null,null,imageIds);
  const clientRequestId='send-'+offlineMessageId();
  const directOnly=/^[\/$]/.test(v);
  if(!imageIds.length)return act(sid,{type:directOnly?'text':'send_message',text,
    client_request_id:clientRequestId},msgPre,optimisticId);
  if(el)el.textContent='uploading images…';
  const uploaded=await uploadImages(sid,imageIds);
  if(!uploaded.ok){
    if(uploaded.network_error&&offlineMessages.length<100){
      queueOfflineText(sid,text,imageIds,optimisticId);if(el)el.textContent='queued offline — sends automatically after reconnection';return;
    }
    updateOptimistic(sid,optimisticId,false,uploaded.error);if(el)el.textContent='✗ '+uploaded.error;return;
  }
  const result=await act(sid,{type:'send_message',text,upload_ids:uploaded.uploadIds,
    client_request_id:clientRequestId},msgPre,optimisticId);
  if(result.ok&&!result.queued)void deleteImages(imageIds);
}

function clearSentComposerCapture(sid,ftPre,text,imageIds=[]){
  const current=document.getElementById(ftPre+'-'+sid);
  if(current&&current.value.trim()===text){
    current.value='';resizeComposer(current);clearDraft(composerDraftKey(sid));
  }
  if(imageIds.length){
    const sent=new Set(imageIds),remaining=imageDraftIds(sid).filter(id=>!sent.has(id));
    setImageDraftIds(sid,remaining);void renderImageDrafts(sid);
  }
}

let offlineFlushBusy=false;
const commandSendLocks=new Set();
function queueOfflineText(sid,text,imageIds=[],existingOptimisticId=null,dismissNonce=null){
  if(offlineMessages.length>=100)return null;
  const messages=(ctxCache[sid]&&ctxCache[sid].messages)||[];
  const entry={id:offlineMessageId(),sid,text:String(text),imageIds:[...(imageIds||[])],created:Date.now(),state:'queued',
    dismissNonce:typeof dismissNonce==='string'?dismissNonce:null,
    baseCount:canonicalCount(messages,{kind:'text',text})};
  offlineMessages.push(entry);persistOfflineMessages();
  const existing=existingOptimisticId!=null;
  // Do not call optimisticList before linking an existing row: that function
  // materializes every unlinked queue record and would create a duplicate.
  let item=existingOptimisticId==null?null:optimisticBucket(sid).find(candidate=>candidate.id===existingOptimisticId);
  if(item){clearTimeout(item.confirmTimer);item.queueId=entry.id;item.status='queued';
    item.queueLabel='Queued offline';item.queueReason=entry.dismissNonce?
      'Dismisses the open question, then sends after reconnection':'Sends automatically after reconnection';delete item.error;}
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
      if(queued.state==='confirmation_unknown')break;
      const item=optimisticList(queued.sid).find(candidate=>candidate.queueId===queued.id);
      if(!item){removeOfflineMessage(queued.id);continue;}
      item.baseCount=canonicalCount((ctxCache[queued.sid]?.messages)||[],item);
      item.status='sending';delete item.error;armOptimisticTimeout(item);uiRefresh();
      let payload={type:queued.dismissNonce?'dismiss_then_send':'send_message',text:queued.text,
        client_request_id:'offline-'+queued.id};
      if(queued.dismissNonce)payload.nonce=queued.dismissNonce;
      if(queued.imageIds?.length){
        const uploaded=await uploadImages(queued.sid,queued.imageIds);
        if(uploaded.network_error){clearTimeout(item.confirmTimer);item.status='queued';delete item.error;uiRefresh();break;}
        if(!uploaded.ok){removeOfflineMessage(queued.id);clearTimeout(item.confirmTimer);item.status='failed';item.error=uploaded.error;uiRefresh();continue;}
        payload={type:queued.dismissNonce?'dismiss_then_send':'send_message',text:queued.text,
          nonce:queued.dismissNonce||undefined,upload_ids:uploaded.uploadIds,
          client_request_id:'offline-'+queued.id};
      }
      const result=await act(queued.sid,payload,false,item.id);
      if(result.offline){
        clearTimeout(item.confirmTimer);item.status='queued';delete item.error;uiRefresh();break;
      }
      if(result.network_error){
        clearTimeout(item.confirmTimer);item.status='failed';
        item.error='Delivery unconfirmed — the connection dropped while sending. Fleet did not retry; check the session before dismissing this receipt.';
        queued.state='confirmation_unknown';queued.error=item.error;persistOfflineMessages();
        uiRefresh();break;
      }
      removeOfflineMessage(queued.id);
      if(!result.ok){
        clearTimeout(item.confirmTimer);item.status='failed';item.error=result.error||'Send rejected';uiRefresh();
      }
      if(result.ok&&!result.queued&&queued.imageIds?.length)void deleteImages(queued.imageIds);
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
  if(el){const root=el.closest('.session-composer');el.innerHTML='';syncComposerToolsOpen(root);}
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
      syncComposerToolsOpen(box.closest('.session-composer'));
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
  syncComposerToolsOpen(box.closest('.session-composer'));
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
const workstreamOpen=new Set();
let actionKind='all';

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
  actionKind=['all','requests','approvals','problems','budgets'].includes(value)?value:'all';
  render(last,true);
}
function actionIcon(kind){return {question:'?',form:'≡',approval:'!',reply:'↩',problem:'×',
  attention:'!',outcome:'✓',budget:'$'}[kind]||'•';}
function renderActionInbox(f){
  const el=$('#actioninbox');if(!el)return new Set();
  const candidates=((f&&f.actions)||[]).filter(actionBaseMatches);
  const actions=candidates.filter(actionKindMatches);
  const visibleSessionIds=new Set(actions.map(action=>action.session_id).filter(Boolean));
  if(!candidates.length){el.className='';el.innerHTML='';return visibleSessionIds;}
  el.className='actioninbox';
  el.innerHTML=`<div class="actionhead"><div><b>Needs you · ${actions.length}</b></div>
    <div class="actionfilters">${[['all','All'],['requests','Requests'],['approvals','Approvals'],
      ['problems','Problems'],['budgets','Budgets']].map(([value,label])=>
      `<button class="${actionKind===value?'active':''}" onclick="setActionKind('${value}')">${label}</button>`).join('')}</div></div>
    <div class="actionrows">${actions.length?actions.map(action=>{
      const session=actionSession(action),encoded=enc(action.action_id);
      const identityTitle=session?.title||action.title||'',identityProject=session?.project||action.project||'';
      const displayRequest=identityTitle||action.request;
      const contextSignal=action.kind==='reply'?action.context:action.request;
      const displayContext=identityTitle?[identityProject,contextSignal].filter(Boolean).join(' · '):action.context;
      const age=Math.max(0,Math.round(((f&&f.t)||Date.now()/1000)-(action.created_at||0)));
      return`<div class="actionrow ${esc(action.kind)} ${esc(action.status||'')}" data-action-id="${esc(action.action_id)}" data-action-sid="${esc(action.session_id||'')}">
        <button class="actionopen" onclick="openInboxAction(decodeURIComponent('${encoded}'))">
          <span class="actionglyph">${actionIcon(action.kind)}</span><span class="actioncopy"><span class="actionrequest">${esc(displayRequest)}</span>
          ${displayContext?`<span class="actioncontext">${esc(displayContext)}</span>`:''}
          <span class="actionmeta"><strong>${esc(action.reason||'Needs review')}</strong> · ${esc(action.provider||'fleet')} · ${esc(action.access_label||'Review')} · ${fmtAge(age)} ago</span></span>
          <span class="actiondelivery">${esc(action.delivery_state||'Review')}</span></button>
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
  if(!el.querySelector('.queuehead'))el.innerHTML='<div class="queuehead"><b></b></div><div class="queuelist"></div>';
  el.querySelector('.queuehead b').textContent=`${title} · ${list.length}`;
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
function spawnCatalog(provider){
  return ((((last||{}).models_by_provider||{})[provider])||[]).map(item=>
    typeof item==='string'?{id:item,name:item,efforts:[]}:
      {id:String(item.id||''),name:String(item.name||item.id||''),efforts:Array.isArray(item.efforts)?item.efforts:[]})
    .filter(item=>item.id);
}
function spawnEfforts(provider,model){
  const catalog=spawnCatalog(provider),picked=catalog.find(item=>item.id===model);
  if(picked?.efforts?.length)return[...picked.efforts];
  // A provider default still has provider-specific constraints. Offering the
  // fleet-wide effort list here can create combinations no model accepts.
  if(catalog.length)return[...new Set(catalog.flatMap(item=>item.efforts||[]))];
  return[...((last&&last.efforts)||[])];
}
function repairSpawnSelection(spec){
  const catalog=spawnCatalog(spec.provider),models=new Set(catalog.map(item=>item.id));
  if(spec.model&&!models.has(spec.model))spec.model='';
  const efforts=new Set(spawnEfforts(spec.provider,spec.model));
  if(spec.effort&&!efforts.has(spec.effort))spec.effort='';
  return spec;
}
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
  $('#sctrl').innerHTML='';
  const message=p.spec.message?`<div class="cmsg user optimistic"><span class="crole">you</span>
      <span class="delivery ${failed?'failed':'sending'}" aria-label="${failed?'start failed':'starting session'}">${failed?'!':'◌'}</span>
      <div class="cbody"><p>${esc(p.spec.message).replace(/\n/g,'<br>')}</p></div></div>`:'';
  $('#sbody').innerHTML=`<div class="aconvo">${message}<div class="spawnstage ${failed?'failed':''}">
    ${failed?'!':`<span class="delivery sending" aria-hidden="true">◌</span>`}
    <div><b>${failed?'Session did not start':p.status==='discovering'?'Finding the new session…':'Starting session…'}</b>
    <span>${failed?esc(p.error||'Startup failed'):'Your message is saved here while Fleet waits for the exact native session.'}</span></div></div></div>`;
  $('#sact').classList.remove('session-composer','composer-active','tools-open','question-present');
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
  newProvider=value;newModel='';newEffort='';spawnForecast=null;queueSpawnForecast(0);renderNewSectionNow();
}
function changeNewDirectory(value){newDir=value;setDraft('new:directory',value);queueSpawnForecast(120);}
function changeNewModel(value){newModel=value;if(newEffort&&!spawnEfforts(newProvider,newModel).includes(newEffort))newEffort='';queueSpawnForecast(0);renderNewSectionNow();}
function changeNewWorktree(value){newWt=value;renderNewSectionNow();}
function renderNewSectionNow(){const root=$('#newsess');if(root)root.innerHTML=newSection();}
function openNewSessionComposer(scroll=false){
  newOpen=true;
  // Opening one local form must not synchronously rebuild every session card.
  // Commit the pressed/open state first; forecast work remains asynchronous.
  renderNewSectionNow();
  if(scroll)requestAnimationFrame(()=>$('#newsess')?.scrollIntoView({behavior:'smooth'}));
}
function newSection(){
  const dirs=(last&&last.recent_dirs)||[];
  const staging=last?.instance?.mode==='staging',stagingSource=last?.instance?.source_root||'';
  if(staging&&stagingSource)newDir=stagingSource;
  if(!newDir&&dirs.some(d=>d.path===DEFAULT_DIR))newDir=DEFAULT_DIR;   // the usual repo
  const catalog=spawnCatalog(newProvider);
  const models=catalog.length?catalog.map(m=>m.id):
    (newProvider==='claude'?((last&&last.models)||[]):[]);
  const picked=catalog.find(m=>m.id===newModel);
  const efforts=spawnEfforts(newProvider,newModel);
  if(newOpen&&!spawnForecast&&!spawnForecastTimer)queueSpawnForecast();
  if(!newOpen)
    return`<button class="newbtn" onclick="openNewSessionComposer()">+ new coding session</button>
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
      onchange="changeNewWorktree(this.checked)"><span>new git worktree</span></label>
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
        ${canReopen?`<button class="historyaction" ${reopenedSessions.has(sid)?'disabled':''} onclick="event.stopPropagation();reopenClosed(decodeURIComponent('${encoded}'),this)">${reopenedSessions.has(sid)?'opened ✓':'Reopen'}</button>`:''}`
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
function togglePeekFromTap(event,sid,expanded){
  if(expanded||!event.currentTarget.classList.contains('truncated'))return;
  event.preventDefault();
  togglePeek(sid,true);
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
let pollSequence=0,pollApplied=0,pollController=null,pollInFlight=null,pollTimer=null;
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
  reconcileSessionActions(f);
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
  const runtimeWarnings=Object.entries(f.providers||{}).flatMap(([provider,value])=>{
    const runtime=value?.runtime;if(!runtime)return[];
    const rows=[];
    if(runtime.mode==='migrating'&&!['committed','isolated'].includes(runtime.phase)){
      const detail=runtime.error||runtime.blockers?.join(', ')||'verifying the shared runtime';
      rows.push(`<div class="provideralert"><b>${esc(provider)} runtime migration · ${esc(runtime.phase)}</b> — ${esc(detail)}. Claude Code remains available.</div>`);
    }
    if(runtime.launcher?.state==='repair_needed')rows.push(`<div class="provideralert"><b>codex terminal routing needs repair</b> — ${esc(runtime.launcher.error||'open a new shell after Fleet repairs the managed launcher')}</div>`);
    return rows;
  }).join('');
  const ledgerProblem=f.ledger&&f.ledger.ok===false?
    `<div class="provideralert"><b>Local data recovered</b> — ${esc(f.ledger.error||'Fleet started a clean local ledger after a storage failure.')}${f.ledger.quarantine?` Preserved as <code>${esc(f.ledger.quarantine)}</code>.`:''}</div>`:'';
  $('#providerstate').innerHTML=ledgerProblem+runtimeWarnings+providerProblems.map(([provider,value])=>
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
  renderSession();
  if(pendingWorkspaceRoute){const route=pendingWorkspaceRoute;pendingWorkspaceRoute=null;applyWorkspaceRoute(route);}
  schedulePeekOverflow();
  applyRouteNav(settingsOpen?'settings':currentRoute);
  const titleCount=Math.max(Number(t.needs_me)||0,Number(notificationData.active)||0,Number(notificationData.unread)||0);
  document.title=(titleCount?`(${titleCount}) `:'')+instanceName;
  perfRecord('render_ms',performance.now()-renderStarted);
}

async function tick(force=false){
  if(pollInFlight&&!force)return pollInFlight;
  if(force&&pollController)pollController.abort('superseded');
  const pollStarted=performance.now(),sequence=++pollSequence;
  const controller=new AbortController();pollController=controller;
  let timedOut=false;
  const timeout=setTimeout(()=>{timedOut=true;controller.abort('timeout');},
    Math.max(250,Number(globalThis.__fleetPollTimeoutMs)||8000));
  const request=(async()=>{try{
    const r=await fetch('/api/fleet',{cache:'no-store',signal:controller.signal});
    const payload=Number(r.headers.get('X-Fleet-Payload-Bytes')||r.headers.get('Content-Length'));
    if(Number.isFinite(payload))perfRecord('poll_payload_bytes',payload);
    const next=await r.json();
    if(!r.ok)throw new Error(next.error||`Fleet returned ${r.status}`);
    if(sequence<pollApplied||sequence!==pollSequence)return;
    pollApplied=sequence;last=next;
    notificationPollingEnabled=applyInstanceAuth(last.instance);
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
  }catch(e){if(sequence===pollSequence&&(e.name!=='AbortError'||timedOut)){
    if(timedOut)console.warn('Fleet poll timed out; keeping the last usable screen');
    else if(e instanceof TypeError&&/fetch|network/i.test(String(e.message||e)))
      console.warn('Fleet poll failed; keeping the last usable screen',e);
    else console.error('Fleet Dash render/poll failed',e);
    setFleetOffline(true);
  }}finally{clearTimeout(timeout);if(pollController===controller)pollController=null;
    if(sequence===pollSequence)perfRecord('poll_ms',performance.now()-pollStarted);}})();
  pollInFlight=request;
  try{return await request;}finally{if(pollInFlight===request)pollInFlight=null;}
}
function scheduleFleetPoll(delay=2000){
  clearTimeout(pollTimer);pollTimer=setTimeout(async()=>{await tick();scheduleFleetPoll();},delay);
}
$('#nowfilter').value=nowFilter;
syncSearchControls();
$('#workfilter').value=workFilter;
navigateTo(currentRoute,false,Boolean(notificationDetailId));
tick().finally(()=>{
  fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"type":"ping"}'})
    .then(r=>{notificationPollingEnabled=r.status!==403;$('#notoken').style.display=r.status===403?'block':'none';
      if(notificationPollingEnabled)loadNotifications(true);}).catch(()=>{});
  const start=()=>initFleetPwa();
  if('requestIdleCallback' in window)requestIdleCallback(start,{timeout:2000});
  else setTimeout(start,250);
  scheduleFleetPoll();
});
window.addEventListener('online',()=>tick(true));
setInterval(()=>{if(currentRoute==='search')loadSearchStatus();},5000);
setInterval(()=>{if(currentRoute==='workstreams')loadWorkstreams();},8000);
