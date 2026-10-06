// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{actionSession,actionBaseMatches,actionKindMatches,actionMatches,openInboxAction,dismissInboxAction,setActionKind,actionIcon,renderActionInbox,filteredWorkstreams,toggleWorkstream,workstreamSessionRow,workstreamLiveRow,renderWorkstreams,renderQueue,toggleHistory,closedSession,isClosedSession,historyParams,renderHistoryDestination,loadHistory,historyItems,matchesHistoryFilter,filteredHistory,spawnCatalog,spawnEfforts,repairSpawnSelection,spawnSnapshot,provisionalSessionObject,sessionsWithProvisional,provisionalCardTop,renderProvisionalSession,restoreSpawnForm,keepWaitingForSpawn,retrySpawn,changeNewProvider,changeNewDirectory,changeNewModel,changeNewEffort,changeNewMode,changeNewPermission,changeNewWorktree,renderNewSectionNow,openNewSessionComposer,renderNewSessionPane,newSection,spawnChips,newSessionFormHtml,persistQuickSpawns,quickSpawnKey,recordQuickSpawn,quickSpawnList,applyQuickSpawn,toggleQuickSpawnPin,doSpawn,startSpawn,doScheduleNew,checkSpawn,historyCount,historyRow,toggle});
// The flat session list is Search TYPE=SESSION; these globals feed it from the
// search query/provider/access controls (runSessionSearch keeps them in step).
globalThis.historyFilter='';globalThis.historyAccess='all';globalThis.historyProvider='all';globalThis.historyProject='all';
globalThis.historyData={ok:true,items:[],next_cursor:0,total:0};;
globalThis.historyLoading=false;globalThis.historyLoadedAt=0;globalThis.historyAbort=null;
globalThis.closedIds=new Set();
const closedMeta=new Map();
const historyInfoOpen=new Set();
const workstreamOpen=new Set();
globalThis.actionKind='all';

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
async function dismissInboxAction(encodedActionId,encodedSid){
  const actionId=decodeURIComponent(encodedActionId),sid=decodeURIComponent(encodedSid);
  const action=((last&&last.actions)||[]).find(item=>item.action_id===actionId);
  if(!action||action.session_id!==sid||!action.dismissible)return;
  const button=document.querySelector(`[data-dismiss-action="${CSS.escape(actionId)}"]`);
  if(button){button.disabled=true;button.textContent='Dismissing…';}
  try{
    const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({dismiss_action:{session_id:sid,action_id:actionId}})});
    const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'dismiss failed');
    last.actions=(last.actions||[]).filter(item=>item.action_id!==actionId);
    if(last.settings)last.settings.dismissed_actions=data.dismissed_actions||{};
    const session=(last.sessions||[]).find(item=>item.session_id===sid);
    if(session){session.attention_dismissed=true;session.unresolved_attention=true;
      if(session.ui_group!=='history'){const viewOnly=session.access==='view_only'||session.read_only||session.headless;
        Object.assign(session,{ui_group:'available',reason_label:'Available',
          primary_action:viewOnly?'view':'continue',primary_action_label:viewOnly?'View':'Continue'});}}
    render(last,true);
  }catch(error){alert('dismiss failed: '+String(error.message||error));tick(true);}
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
  if(!candidates.length){setClass(el,'');setHtml(el,'');return visibleSessionIds;}
  setClass(el,'actioninbox');
  setHtml(el,`<div class="actionhead"><div><b>Needs you · ${actions.length}</b></div>
    <div class="actionfilters">${[['all','All'],['requests','Requests'],['approvals','Approvals'],
      ['problems','Problems'],['budgets','Budgets']].map(([value,label])=>
      `<button class="${actionKind===value?'active':''}" onclick="setActionKind('${value}')">${label}</button>`).join('')}</div></div>
    <div class="actionrows">${actions.length?actions.map(action=>{
      const session=actionSession(action),encoded=enc(action.action_id),encodedSid=enc(action.session_id||'');
      const identityTitle=session?.title||action.title||'',identityProject=session?.project||action.project||'';
      const displayRequest=identityTitle||action.request;
      const contextSignal=action.kind==='reply'?action.context:action.request;
      const displayContext=identityTitle?[identityProject,contextSignal].filter(Boolean).join(' · '):action.context;
      const age=Math.max(0,Math.round(((f&&f.t)||Date.now()/1000)-(action.created_at||0)));
      return`<div class="actionrow ${esc(action.kind)} ${esc(action.status||'')}" data-action-id="${esc(action.action_id)}" data-action-sid="${esc(action.session_id||'')}">
        <button class="actionopen" onclick="openInboxAction(decodeURIComponent('${encoded}'))">
          <span class="actionglyph">${actionIcon(action.kind)}</span><span class="actioncopy"><span class="actionrequest">${esc(displayRequest)}</span>
          ${displayContext?`<span class="actioncontext">${esc(displayContext)}</span>`:''}
          <span class="actionmeta"><strong>${esc(action.reason||'Needs review')}</strong> · ${esc(action.provider||'fleet')} · ${esc(action.access_label||'Review')} · ${fmtAge(age)} ago</span></span></button>
        <span class="actioncontrols"><span class="actiondelivery">${esc(action.delivery_state||'Review')}</span>
          ${action.dismissible?`<button class="actiondismiss" data-dismiss-action="${esc(action.action_id)}" onclick="dismissInboxAction('${encoded}','${encodedSid}')">Dismiss</button>`:''}
          ${session?.muted?'<span class="actionmuted" title="session notifications muted">🔕</span>':''}</span>
        ${session?cardResponseFeedback(session):''}
        ${session?pinFeedbackHtml(session.session_id):''}
      </div>`;}).join(''):`<div class="actionempty">No ${esc(actionKind==='all'?'matching':actionKind)} actions.</div>`}</div>`);
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
function workstreamLiveRow(session){
  const sid=String(session.session_id||''),encoded=enc(sid);
  const title=session.title||session.name||session.project||'Session';
  return`<div class="worklive" role="button" tabindex="0" onclick="primarySessionAction(decodeURIComponent('${encoded}'))"
      onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
    <span class="workstate ${esc(session.ui_group||'working')}"></span>
    <b>${esc(title)}</b>
    ${session.branch?`<span class="worklivebranch">${esc(session.branch)}</span>`:''}
    <span class="worklivemeta">${esc([session.provider||'claude',session.reason_label||''].filter(Boolean).join(' · '))}</span>
  </div>`;
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
    const live=(item.sessions||[]).filter(session=>(session.ui_group||'history')!=='history');
    const liveBranches=new Map();
    live.forEach(session=>{if(session.branch)liveBranches.set(session.branch,(liveBranches.get(session.branch)||0)+1);});
    const otherBranches=(item.branches||[]).filter(branch=>!liveBranches.has(branch)).length;
    const pills=[['needs_you','needs you'],['working','working'],['available','available'],['history','history']]
      .filter(([key])=>counts[key]).map(([key,label])=>`<span class="workpill ${key}">${counts[key]} ${label}</span>`).join('');
    const cost=item.cost_scope==='unavailable'?'cost unavailable':
      `${fmt$(item.cost)}${item.cost_scope==='partial'?' <i>partial</i>':''}`;
    const context=item.context_tokens==null?'context unavailable':`${fmtTok(item.context_tokens)} context now`;
    return`<section class="workstream ${item.missing?'missing':''}" data-workstream-id="${esc(item.workstream_id)}">
      <div class="workheadrow">
        <button class="workhead" aria-expanded="${expanded}" onclick="toggleWorkstream(decodeURIComponent('${id}'))">
          <span class="workkind ${item.kind==='git'?'git':''}">${item.kind==='git'?'GIT':item.kind==='unknown'?'?':'DIR'}</span>
          <span class="worktitle"><b>${esc(item.title)}${item.missing?' <i>· missing</i>':''}${item.stale?' <i>· stale</i>':''}</b>
            <small>${esc(item.root)} · ${(item.providers||[]).map(esc).join(' · ')||'provider unavailable'}</small></span>
        </button>
        <span class="workheadside">${pills||'<span class="workpill">no sessions</span>'}
          ${safeGithubUrl(repository.github_url)?`<a class="repoopen" href="${esc(safeGithubUrl(repository.github_url))}" target="_blank" rel="noopener">GitHub ↗</a>`:''}
          <button class="workchev" aria-expanded="${expanded}" aria-label="${expanded?'collapse':'expand'} workstream detail" onclick="toggleWorkstream(decodeURIComponent('${id}'))">${expanded?'▴':'▾'}</button></span>
      </div>
      <div class="workbody">
        <div class="workbranches">${[...liveBranches.entries()].map(([branch,count])=>
            `<span class="workbranch"><code>${esc(branch)}</code> · ${count} session${count===1?'':'s'}</span>`).join('')}
          ${otherBranches?`<span class="workbranchother">${otherBranches} other branch${otherBranches===1?'':'es'} only in history</span>`:
            liveBranches.size?'':'<span class="workbranchother">branch unavailable</span>'}</div>
        ${live.map(workstreamLiveRow).join('')}
        <div class="workoutcome"><b>Latest</b><span>${esc(item.latest_outcome||'No outcome recorded')}</span></div>
        <div class="worksignals"><span>${cost.startsWith('cost')?esc(cost):cost}</span><span>${esc(context)}</span>
          <span>Changes <b>${esc(summary.changed_files||'not observed')}</b></span><span>Tests <b>${esc(String(summary.tests||'not observed').replaceAll('_',' '))}</b></span><span>PR <b>${esc(String(summary.pull_request||'not observed').replaceAll('_',' '))}</b></span><span>Budget <b>${esc(String(item.budget_state||'not_configured').replaceAll('_',' '))}</b></span></div>
      </div>
      ${expanded?`<div class="workdetail"><div class="worktrees"><b>Worktrees</b>${(item.worktrees||[]).map(path=>`<code>${esc(path)}</code>`).join('')}</div>
        <div class="worksessions">${(item.sessions||[]).map(workstreamSessionRow).join('')}</div></div>`:''}</section>`;
  }).join('');
}
function renderQueue(el,list,title,subtitle,kind,keepEmpty=false){
  if(!list.length&&!keepEmpty){
    if(el.className||el.firstChild){setHtml(el,'');setClass(el,'');}
    return;
  }
  setClass(el,`queue ${kind}`);
  if(!el.querySelector('.queuehead'))setHtml(el,'<div class="queuehead"><b></b></div><div class="queuelist"></div>');
  setText(el.querySelector('.queuehead b'),`${title} · ${list.length}`);
  reconcileCards(el.querySelector('.queuelist'),list,
    keepEmpty?'No sessions are ready for another message.':'');
}
function toggleHistory(encoded){
  const sid=decodeURIComponent(encoded);
  historyInfoOpen.has(sid)?historyInfoOpen.delete(sid):historyInfoOpen.add(sid);
  render(last,true);   // pinned history rows live on the Now surface
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
  if(historyProject!=='all')params.set('project',historyProject);
  return params.toString();
}
function renderHistoryDestination(){
  // History is decommissioned: the flat session list renders inside Search
  // under TYPE=SESSION (invariant 31/55 successor surface).
  if(currentRoute==='search'&&searchFilters.kind==='session')renderSearchResults();
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
function historyItems(f){
  const live=(f.sessions||[]).filter(s=>s.ui_group==='history'&&!pinnedSessions.has(s.session_id));
  const closed=(historyData.items||[]).filter(s=>!pinnedSessions.has(s.session_id));
  return [...live,...closed].sort((a,b)=>(b.activity_at||0)-(a.activity_at||0));
}
function matchesHistoryFilter(item){
  const query=historyFilter.trim().toLowerCase();
  const accessOk=historyAccess==='all'||item.primary_action===historyAccess;
  const providerOk=historyProvider==='all'||(item.provider||'claude')===historyProvider;
  const projectOk=historyProject==='all'||(item.project||'')===historyProject;
  const hay=[item.title,item.name,item.project,item.branch,item.provider,item.reason_label,
    item.access_label,item.state,item.reg_status].filter(Boolean).join(' ').toLowerCase();
  return accessOk&&providerOk&&projectOk&&(!query||hay.includes(query));
}
function filteredHistory(f){
  return historyItems(f).filter(matchesHistoryFilter);
}

// ---- new session -----------------------------------------------------------
// The spawn form takes the session pane (mockup 8c): #newsess in Now keeps only
// the entry button, and on spawn the pane becomes the provisional session's
// chat. Form state lives in globals: the 2s poll re-renders the pane, so
// anything held only in the DOM (typed path) would be wiped mid-use. There is
// deliberately NO initial-message field (Console locked decision) — the first
// message is typed into the live chat, or into the Schedule overlay.
globalThis.newOpen=false;globalThis.newProvider='claude';globalThis.newDir=draftValue('new:directory');globalThis.newModel='';globalThis.newEffort='';globalThis.newMode='plan';globalThis.newPermissionMode='default';globalThis.newWt=true;globalThis.newWtName=draftValue('new:worktree');globalThis.spawnWait=null;globalThis.spawnMsg='';
globalThis.spawnProvisional=null;
// Quick-spawn recents: device-local, right-click (or long-press context menu)
// pins a row. Only non-secret spawn configuration is stored.
const QUICK_SPAWN_KEY='fleet.quickSpawns.v1';
globalThis.quickSpawns=(()=>{try{const value=JSON.parse(localStorage.getItem(QUICK_SPAWN_KEY)||'[]');
  return Array.isArray(value)?value.slice(0,12):[];}catch(_){return[];}})();
function persistQuickSpawns(){try{localStorage.setItem(QUICK_SPAWN_KEY,JSON.stringify(quickSpawns.slice(0,12)));}catch(_){}}
function quickSpawnKey(item){return`${item.provider}|${item.cwd}`;}
function recordQuickSpawn(spec){
  if(!spec?.cwd)return;
  const key=`${spec.provider}|${spec.cwd}`;
  const prior=quickSpawns.find(item=>quickSpawnKey(item)===key);
  const entry={provider:spec.provider,cwd:spec.cwd,model:spec.model||'',effort:spec.effort||'',
    mode:spec.mode||'',permission_mode:spec.permission_mode||'',worktree:Boolean(spec.worktree),
    pinned:Boolean(prior?.pinned),at:Date.now()};
  quickSpawns=[entry,...quickSpawns.filter(item=>quickSpawnKey(item)!==key)].slice(0,12);
  persistQuickSpawns();
}
function quickSpawnList(){return[...quickSpawns].sort((a,b)=>(b.pinned-a.pinned)||(b.at-a.at)).slice(0,4);}
function applyQuickSpawn(key){
  const item=quickSpawns.find(row=>quickSpawnKey(row)===key);if(!item)return;
  newProvider=item.provider==='codex'?'codex':'claude';newDir=item.cwd;
  newMode=item.mode||'plan';newPermissionMode=item.permission_mode||'default';newWt=item.worktree!==false;
  setDraft('new:directory',newDir);
  const repaired=repairSpawnSelection({provider:newProvider,model:item.model||'',effort:item.effort||''});
  newModel=repaired.model;newEffort=repaired.effort;
  spawnForecast=null;queueSpawnForecast(0);renderNewSectionNow();
}
function toggleQuickSpawnPin(key,event){
  event?.preventDefault();
  const item=quickSpawns.find(row=>quickSpawnKey(row)===key);if(!item)return;
  item.pinned=!item.pinned;persistQuickSpawns();renderNewSectionNow();
}
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
    worktree_name:newProvider==='claude'?newWtName:'',message:''};
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
  return`<div class="cmain"><div class="shead" title="open startup details" onclick="sessionTap(event,'${s.session_id}')">
      <span class="chip ${failed?'problem':'working'}">${failed?'Start failed':'Starting'}</span>
      <span class="sname"><span class="stitle">New coding session</span><small>${esc(s.project)} · ${esc(s.provider)}</small></span>
      <span class="m amodel">${modelLabel(s)}</span>
    </div>
    ${p?.spec.message?`<div class="lastmsg"><span class="peekwho">you ·</span><span class="lmtext">${esc(p.spec.message)}</span><span class="delivery ${failed?'failed':'sending'}" aria-label="${failed?'start failed':'starting session'}">${failed?'!':'◌'}</span></div>`:''}
    <div class="spawncardstate ${failed?'failed':''}">${failed?esc(p.error||'Session did not start'):`<span class="delivery sending" aria-hidden="true">◌</span> ${esc(p?.status==='discovering'?'Finding the new session…':'Starting session…')}`}</div>
    ${failed&&p?.canRetry?`<div class="spawncardactions"><button class="pbtn send" onclick="event.stopPropagation();retrySpawn()">retry</button><button class="pbtn" onclick="event.stopPropagation();restoreSpawnForm()">restore form</button></div>`:''}</div>`;
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
  newWtName=p.spec.worktree_name;
  setDraft('new:directory',newDir);setDraft('new:worktree',newWtName);
  const sid=p.id;spawnProvisional=null;spawnWait=null;spawnMsg='';
  if(sessionView?.sid===sid)closeSession();
  openNewSessionComposer();
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
  newProvider=value==='codex'?'codex':'claude';newModel='';newEffort='';spawnForecast=null;queueSpawnForecast(0);renderNewSectionNow();
}
function changeNewDirectory(value){newDir=value;setDraft('new:directory',value);queueSpawnForecast(120);}
function changeNewModel(value){newModel=value;if(newEffort&&!spawnEfforts(newProvider,newModel).includes(newEffort))newEffort='';queueSpawnForecast(0);renderNewSectionNow();}
function changeNewEffort(value){newEffort=value;renderNewSectionNow();}
function changeNewMode(value){newMode=value==='default'?'default':'plan';renderNewSectionNow();}
function changeNewPermission(value){newPermissionMode=value;renderNewSectionNow();}
function changeNewWorktree(value){newWt=value;renderNewSectionNow();}
function renderNewSectionNow(){
  if(sessionView?.newForm){renderNewSessionPane(true);return;}
  const root=$('#newsess');if(root)root.innerHTML=newSection();
}
function openNewSessionComposer(){
  // The spawn form fills the session pane (docked beside the queue on wide
  // desktops, full-screen on mobile). Opening paints only the pane — never a
  // synchronous full-fleet render (invariant 55).
  const feedbackStarted=performance.now();
  newOpen=true;
  if(!sessionView?.newForm){
    const returnHash=sessionView?.returnHash||(parseSessionHash()?'#now':(location.hash||'#now'));
    sessionView={sid:null,newForm:true,section:'chat',returnHash,historyDepth:0};
    agentView=null;viewerSid=null;viewerPath=null;
  }
  const view=$('#sview');
  view.style.display='flex';view.classList.add('newform-view');
  applyWorkspaceChrome();activateWorkspaceSection();syncVisualViewport();syncOverlayHistory();
  renderNewSessionPane(true);
  recordInputFeedback(feedbackStarted,'new_session');
}
function renderNewSessionPane(force=false){
  if(!sessionView?.newForm)return;
  const view=$('#sview');view.classList.add('newform-view');
  $('#stitle2').innerHTML='<span class="sesstitle"><b>New session</b><small>exact provider spawn · opens in its own terminal</small></span>';
  $('#sctrl').innerHTML='';$('#sactivity').innerHTML='';
  $('#sfilecount').textContent='';$('#sagentcount').textContent='';
  const ae=document.activeElement;
  if(!force&&ae&&['INPUT','SELECT','TEXTAREA'].includes(ae.tagName)&&view.contains(ae))return;
  const body=$('#sbody'),keepTop=body.scrollTop;
  body.innerHTML=`<div class="newpane">${newSessionFormHtml()}</div>`;
  body.scrollTop=keepTop;
  delete body.dataset.renderKey;delete body.dataset.canonicalKey;
  const act=$('#sact');
  act.classList.remove('session-composer','composer-active','tools-open','question-present');
  act.innerHTML='';
}
function newSection(){
  return`<button class="newbtn" onclick="openNewSessionComposer()">+ new coding session</button>
    ${spawnMsg?`<div class="actmsg spawnbanner">${esc(spawnMsg)}</div>`:''}
    <div class="dsep"></div>`;
}
function spawnChips(label,options,current,handler){
  return`<div class="nfgroup"><span class="nfglabel">${esc(label)}</span><div class="nfchips">${options.map(option=>{
    const [value,text,cls]=option;
    return`<button class="nfchip${current===value?' on':''}${cls?' '+cls:''}" aria-pressed="${current===value}"
      onclick="${handler}(decodeURIComponent('${enc(value)}'))">${esc(text)}</button>`;}).join('')}</div></div>`;
}
function newSessionFormHtml(){
  const dirs=(last&&last.recent_dirs)||[];
  const staging=last?.instance?.mode==='staging',stagingSource=last?.instance?.source_root||'';
  if(staging&&stagingSource)newDir=stagingSource;
  if(!newDir&&dirs.length)newDir=dirs[0].path;   // the newest directory the server has seen
  const catalog=spawnCatalog(newProvider);
  const models=catalog.length?catalog.map(m=>m.id):
    (newProvider==='claude'?((last&&last.models)||[]):[]);
  const efforts=spawnEfforts(newProvider,newModel);
  if(newOpen&&!spawnForecast&&!spawnForecastTimer)queueSpawnForecast();
  const cur=dirs.find(d=>d.path===newDir);
  const untrusted=!staging&&newDir&&(!cur||!cur.trusted);
  const recents=quickSpawnList();
  return`<div class="newform">
    ${recents.length?`<div class="nfgroup"><div class="nfglabelrow"><span class="nfglabel">Quick spawn · recents</span><span class="nfhint">right-click a row to pin it</span></div>
      <div class="quickspawns">${recents.map(item=>{const key=enc(quickSpawnKey(item));
        return`<button class="quickspawn${item.pinned?' pinned':''}" onclick="applyQuickSpawn(decodeURIComponent('${key}'))"
          oncontextmenu="toggleQuickSpawnPin(decodeURIComponent('${key}'),event)">
          ${item.pinned?'<span class="qspin">⌖</span>':''}<b>${esc(item.cwd.split('/').filter(Boolean).pop()||item.cwd)}</b>
          <small>${esc([item.provider,item.model||'default model',item.effort,item.provider==='codex'?item.mode:item.permission_mode].filter(Boolean).join(' · '))}</small>
          <span class="qsfill">Fill form →</span></button>`;}).join('')}</div></div><div class="nfsep"></div>`:''}
    ${staging?`<div class="nfwarn"><b>Isolated staging worktree</b><br>Fleet will create a new disposable branch and worktree from the staging checkout. Production sessions remain view only.</div>`:`
    <div class="nfgroup"><span class="nfglabel">Directory</span>
      <input class="nfin nfdir" data-draft-key="new:directory" placeholder="type a path (must be under ~)" value="${esc(newDir)}"
        oninput="changeNewDirectory(this.value)" autocomplete="off">
      <div class="nfchips nfdirs">${dirs.slice(0,6).map(d=>`<button class="nfchip${d.path===newDir?' on':''}"
        onclick="changeNewDirectory(decodeURIComponent('${enc(d.path)}'));renderNewSectionNow()">${esc(d.path.replace(/^\/Users\/[^/]+/,'~'))}${d.trusted?'':' ⚠'}</button>`).join('')}</div></div>`}
    ${newProvider==='claude'&&untrusted?`<div class="nfwarn">⚠ this folder isn't trusted yet — Claude Code will ask
      “do you trust the files in this folder?” at startup, and only your Mac can answer it.</div>`:''}
    ${spawnChips('Provider',[['claude','claude'],['codex','codex']],newProvider,'changeNewProvider')}
    ${spawnChips('Model',[['','default'],...models.map(m=>[m,m])],newModel,'changeNewModel')}
    ${spawnChips('Effort',[['','default'],...efforts.map(e=>[e,e])],newEffort,'changeNewEffort')}
    ${newProvider==='codex'?spawnChips('Mode',[['plan','Plan'],['default','Default']],newMode,'changeNewMode'):''}
    ${newProvider==='claude'?spawnChips('Permission mode',[['default','Manual'],['auto','Auto'],['acceptEdits','Accept edits'],['plan','Plan'],['dontAsk',"Don't ask",'advanced']],newPermissionMode,'changeNewPermission'):''}
    ${newProvider==='claude'&&!staging?`<div class="nfgroup"><span class="nfglabel">Worktree</span>
      <label class="nfcheck"><input type="checkbox" ${newWt?'checked':''}
        onchange="changeNewWorktree(this.checked)"><span>create linked git worktree</span></label>
      ${newWt?`<input class="nfin" data-draft-key="new:worktree" placeholder="worktree name (optional)" value="${esc(newWtName)}"
        oninput="newWtName=this.value" autocomplete="off">`:''}</div>`:''}
    ${spawnForecastHtml()}
    <div class="nfactions"><button class="pbtn send nfgo" onclick="doSpawn()">Spawn session</button>
      <button class="pbtn sendoption nfgo" onclick="doScheduleNew()">Schedule session</button></div>
    ${spawnMsg?`<div class="actmsg">${esc(spawnMsg)}</div>`:''}
  </div>`;
}
async function doSpawn(){
  if(!newDir){spawnMsg='✗ pick a directory first';renderNewSectionNow();return;}
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
    clearDraft('new:directory','new:worktree');
    recordQuickSpawn(spec);
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
  if(!newDir){spawnMsg='✗ pick a directory first';renderNewSectionNow();return;}
  const spec={provider:newProvider,cwd:newDir,model:newModel,effort:newEffort,mode:newMode,
    permission_mode:newProvider==='claude'?newPermissionMode:'',
    worktree:newProvider==='claude'&&newWt,worktree_name:newProvider==='claude'?newWtName:''};
  // The scheduled message is typed in the Schedule overlay (the form has no
  // initial-message field).
  openSchedule('',null,'',null,spec,'');
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
function historyCount(f){
  const live=(f.sessions||[]).filter(item=>item.ui_group==='history'&&
    !pinnedSessions.has(item.session_id)&&matchesHistoryFilter(item)).length;
  const total=live+Number(historyData.total||0);
  return`${total} session${total===1?'':'s'}`;
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

function toggle(sid){openCards.has(sid)?openCards.delete(sid):openCards.add(sid);render(last,true);}

// ---- budgets and forecasts ------------------------------------------------

Object.assign(globalThis,{closedMeta,historyInfoOpen,workstreamOpen});
