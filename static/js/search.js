// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{syncSearchControls,showSearchHistory,setSearchQuery,setSearchFilter,setSearchAccess,queueSearch,searchParams,searchHasCriteria,renderSearchStatus,loadSearchStatus,searchWhen,renderSearchResults,runSessionSearch,sessionResultRow,renderSessionResults,updateSearchProjects,runSearch,searchContextMessage,searchSourceAction,openSearchContext,closeSearchView,switchSearchView,rebuildSearch});
globalThis.searchTimer=null;globalThis.searchAbort=null;globalThis.searchCursor=0;globalThis.searchBusy=false;
globalThis.searchItems=[];globalThis.searchProjects=[];globalThis.searchStatusData=null;globalThis.searchStatusAt=0;globalThis.searchError='';
globalThis.searchView=null;
// TYPE=SESSION replaces the old History destination: an empty query lists every
// session chronologically with access chips. Legacy #history deep links land here.
globalThis.searchAccess='all';globalThis.sessionSearchSignature='';
const searchFilters={query:draftValue('filter:search'),provider:'',
  kind:location.hash.replace(/^#/,'').split('/')[0]==='history'?'session':'',project:''};
function syncSearchControls(){
  const controls={query:$('#searchquery'),provider:$('#searchprovider'),kind:$('#searchkind'),
    project:$('#searchproject')};
  for(const [key,control] of Object.entries(controls))if(control&&control.value!==searchFilters[key])
    control.value=searchFilters[key];
  const history=$('#searchhistory');if(history){const active=searchFilters.kind==='session'&&searchAccess==='all';
    history.classList.toggle('active',active);history.setAttribute('aria-pressed',String(active));}
}
function showSearchHistory(){searchFilters.kind='session';searchAccess='all';syncSearchControls();runSearch(true);}
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
function setSearchAccess(value){
  searchAccess=['all','continue','view','reopen'].includes(value)?value:'all';
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
  if(searchFilters.kind==='session'){
    // Session results come from the durable ledger, not the transcript index —
    // index progress is irrelevant here. Show the flat-list count instead.
    el.innerHTML=`<span>${historyCount(last||{sessions:[]})}</span><span>${searchFilters.query.trim()||searchAccess!=='all'||searchFilters.provider?'filtered':'no query — chronological'}</span>`;
    if(warnings)warnings.innerHTML='';return;
  }
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
  const chips=$('#searchsessionchips');
  if(chips){
    chips.hidden=searchFilters.kind!=='session';
    chips.querySelectorAll('[data-search-access]').forEach(button=>{
      const active=button.dataset.searchAccess===searchAccess;
      button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));
    });
  }
  if(searchFilters.kind==='session')return renderSessionResults(el,more);
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
// ---- TYPE=SESSION: the flat chronological session list (History replacement) --
function sessionResultRow(item){
  const sid=String(item.session_id||''),encoded=enc(sid);
  const isClosed=item.closed_at!=null&&!item.capabilities;
  const badge=isClosed?'closed':(item.reason_label==='External'||item.primary_action==='view')?'external':'inactive';
  const activity=item.activity_at||item.last_seen||item.closed_at||((last&&last.t)||0);
  const title=item.title||item.name||item.project||'Session';
  const meta=[item.project,item.branch&&item.branch!=='HEAD'?item.branch:'',item.provider||'claude',
    item.primary_action==='view'?'view only':''].filter(Boolean).join(' · ');
  const canReopen=isClosed&&item.can_reopen;
  return`<div class="searchresult sessionresult" data-history-sid="${esc(sid)}" role="button" tabindex="0"
      onclick="${isClosed?`openClosed(decodeURIComponent('${encoded}'))`:`primarySessionAction(decodeURIComponent('${encoded}'))`}"
      onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
    <span class="sessionbadge ${esc(badge)}">${badge==='closed'?'Closed':badge==='external'?'External':'Inactive'}</span>
    <span class="searchcopy"><span class="searchtitle"><b>${esc(title)}</b></span>
      <span class="searchmeta">${esc(meta)}</span></span>
    <span class="sessionresultside">
      ${isClosed?`<button class="historyaction" onclick="event.stopPropagation();openClosed(decodeURIComponent('${encoded}'))">View</button>
        ${canReopen?`<button class="historyaction" ${reopenedSessions.has(sid)?'disabled':''} onclick="event.stopPropagation();reopenClosed(decodeURIComponent('${encoded}'),this)">${reopenedSessions.has(sid)?'opened ✓':'Reopen'}</button>`:''}`
        :`<button class="historyaction" onclick="event.stopPropagation();primarySessionAction(decodeURIComponent('${encoded}'))">${esc(item.primary_action_label||'View')}</button>`}
      <button class="spin sessionpin${pinnedSessions.has(sid)?' on':''}" ${pinActions.get(sid)?.busy?'disabled':''} aria-label="${pinnedSessions.has(sid)?'unpin session':'pin session'}"
        title="${pinnedSessions.has(sid)?'unpin session':'pin session'}"
        onclick="event.stopPropagation();toggleSessionPin(decodeURIComponent('${encoded}'))">⌖</button>
      <span class="searchtime">${fmtAge(Math.max(0,Math.round(((last&&last.t)||Date.now()/1000)-activity)))} ago</span>
    </span>
  </div>`;
}
function renderSessionResults(el,more){
  const items=filteredHistory(last||{sessions:[]});
  renderSearchStatus();
  if(!items.length&&!historyLoading){
    el.innerHTML=historyData.ok?
      '<div class="searchempty">No matching sessions. Inactive and closed sessions appear here.</div>':
      `<div class="searchempty searcherror">${esc(historyData.error||'Session history unavailable')} <button onclick="loadHistory(true)">retry</button></div>`;
    more.hidden=true;return;
  }
  el.innerHTML=items.map(sessionResultRow).join('')+
    (historyLoading?'<div class="ctxload">loading sessions…</div>':'')+
    (!historyLoading&&!historyData.ok?`<div class="searchempty searcherror">✗ ${esc(historyData.error||'session history unavailable')} <button onclick="loadHistory(${items.length?'false':'true'})">retry</button></div>`:'');
  more.hidden=historyLoading||historyData.next_cursor==null;
  more.disabled=historyLoading;
  const remaining=Math.min(100,Math.max(0,Number(historyData.total||0)-(historyData.items||[]).length));
  more.textContent=historyLoading?'Loading more…':`Show ${remaining} more`;
}
async function runSessionSearch(reset){
  clearTimeout(searchTimer);
  if(searchAbort){searchAbort.abort();searchAbort=null;searchBusy=false;}
  historyFilter=searchFilters.query;
  historyProvider=searchFilters.provider||'all';
  historyProject=searchFilters.project||'all';
  historyAccess=searchAccess;
  const signature=historyParams(0);
  // Revisiting the destination re-renders the cached list; only changed
  // criteria refetch page 1, and only Show more loads the next 100 rows.
  if(reset&&historyLoadedAt&&sessionSearchSignature===signature){renderSearchResults();return;}
  sessionSearchSignature=signature;
  await loadHistory(reset);
}
async function runSearch(reset=true){
  if(!$('#searchresults'))return;
  syncSearchControls();
  if(searchFilters.kind==='session')return runSessionSearch(reset);
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

Object.assign(globalThis,{searchFilters});
