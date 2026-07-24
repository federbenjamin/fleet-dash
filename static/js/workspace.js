// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{workspaceDockable,workspaceDocked,paneSplitBounds,setPaneSplit,startPaneSplit,paneSplitKey,applyWorkspaceChrome,toggleWorkspaceExpand,rememberSessionFile,questionPanelState,persistQuestionPanels,questionScrollKey,setQuestionScrollPosition,rememberQuestionScroll,questionPanelMaxHeight,questionPanelHeight,toggleQuestionPanel,setQuestionPanelHeight,questionResizeKey,startQuestionResize,questionDrawerHtml,confidenceText,evidenceFactsHtml,evidenceEventHtml,renderEvidenceRail,loadSessionEvidence,toggleSessionEvidence,primarySessionAction,markSessionRevision,markRead,markAvailable,workspaceHash,saveWorkspaceScroll,restoreWorkspaceScroll,workspaceSplitBounds,setWorkspaceSplit,applyWorkspaceSplit,startWorkspaceSplit,workspaceSplitKey,activateWorkspaceSection,openSessionWorkspace,applyWorkspaceRoute,openSession,openClosed,exitSessionWorkspace,setSessionSection,clearWorkspaceSelection,mobileWorkspaceSwipeEnabled,workspaceHorizontalTarget,workspaceTouchPoint,finishWorkspaceTouch,loadClosedMeta,closeSession,sessionActivityHtml,renderSessionActivity,workspaceContext,workspaceAgents,renderWorkspaceChrome,renderParentWorkspaceAction,chosenWorkspaceFile,renderWorkspaceFileDocument,renderWorkspaceFiles,filteredWorkspaceAgents,setSubagentFilter,selectWorkspaceAgent,renderWorkspaceSubagents,workspaceSessionModel,renderWorkspaceDetails,renderClosedComposer,requestResumeAndSend,sendClosedResume,renderClosed,reopenClosed,renderSession,openAgent,closeAgent,agentMeta,ensureAgentCtx,renderAgent,agentRelayKey,agentRelayHtml,restoreRelay,sendRelay,agentRow});
globalThis.sessionView=null;            // one session workspace: section + optional file/agent selection
// Console two-pane shell: on wide desktops the workspace docks as a persistent
// right pane beside the queue (never a modal there); ⤢ expands it to the full
// width right of the rail; below the threshold it stays the full-screen overlay.
const PANE_SPLIT_KEY='fleet.paneSplit.v1',PANE_EXPANDED_KEY='fleet.paneExpanded.v1';
globalThis.workspacePaneWidth=(()=>{try{const value=Number(localStorage.getItem(PANE_SPLIT_KEY));
  return Number.isFinite(value)&&value>0?value:720;}catch(_){return 720;}})();
globalThis.workspaceExpanded=(()=>{try{return localStorage.getItem(PANE_EXPANDED_KEY)==='1';}catch(_){return false;}})();
function workspaceDockable(){return matchMedia('(min-width:1200px)').matches;}
function workspaceDocked(){return Boolean(sessionView)&&workspaceDockable();}
function paneSplitBounds(){
  const rail=$('#sidenav')?.getBoundingClientRect().width||136;
  return{min:650,max:Math.max(650,Math.min(1200,Math.round(innerWidth-rail-420)))};
}
function setPaneSplit(width,persist=false){
  const bounds=paneSplitBounds();
  workspacePaneWidth=Math.round(Math.max(bounds.min,Math.min(bounds.max,Number(width)||720)));
  document.documentElement.style.setProperty('--fleet-pane-width',workspacePaneWidth+'px');
  const splitter=$('#ssplit');
  if(splitter){splitter.setAttribute('aria-valuemin',String(bounds.min));
    splitter.setAttribute('aria-valuemax',String(bounds.max));
    splitter.setAttribute('aria-valuenow',String(workspacePaneWidth));}
  if(persist)try{localStorage.setItem(PANE_SPLIT_KEY,String(workspacePaneWidth));}catch(_){}
}
function startPaneSplit(event){
  if(event.pointerType==='mouse'&&event.button!==0)return;
  event.preventDefault();
  const splitter=event.currentTarget,pointerId=event.pointerId;
  splitter.classList.add('resizing');splitter.setPointerCapture?.(pointerId);
  const move=moveEvent=>{if(moveEvent.pointerId!==pointerId)return;moveEvent.preventDefault();
    setPaneSplit(innerWidth-moveEvent.clientX);};
  const end=endEvent=>{if(endEvent.pointerId!==pointerId)return;splitter.classList.remove('resizing');
    splitter.removeEventListener('pointermove',move);splitter.removeEventListener('pointerup',end);
    splitter.removeEventListener('pointercancel',end);setPaneSplit(workspacePaneWidth,true);};
  splitter.addEventListener('pointermove',move);splitter.addEventListener('pointerup',end);
  splitter.addEventListener('pointercancel',end);
}
function paneSplitKey(event){
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();const bounds=paneSplitBounds();
  const next=event.key==='Home'?bounds.min:event.key==='End'?bounds.max:
    workspacePaneWidth+(event.key==='ArrowLeft'?16:-16);   // left edge: left = wider pane
  setPaneSplit(next,true);
}
function applyWorkspaceChrome(){
  const docked=workspaceDocked(),expanded=docked&&workspaceExpanded;
  const root=document.documentElement,view=$('#sview');
  root.classList.toggle('fd-pane',docked&&!expanded);
  root.classList.toggle('fd-expanded',expanded);
  view.classList.toggle('docked',docked);
  view.setAttribute('aria-modal',docked?'false':'true');
  if(docked&&!expanded)setPaneSplit(workspacePaneWidth);
  const expand=$('#sexpand');
  if(expand){expand.hidden=!docked;expand.textContent=expanded?'⤡':'⤢';
    expand.title=expanded?'collapse to pane':'expand to full width';
    expand.setAttribute('aria-label',expanded?'collapse workspace to pane':'expand workspace to full width');}
  syncModalStack();
}
function toggleWorkspaceExpand(){
  workspaceExpanded=!workspaceExpanded;
  try{localStorage.setItem(PANE_EXPANDED_KEY,workspaceExpanded?'1':'0');}catch(_){}
  applyWorkspaceChrome();renderSession(true);
}
window.addEventListener('resize',()=>{if(sessionView)applyWorkspaceChrome();});
const workspaceScrolls=new Map();
const LAST_FILE_STORE_KEY='fleet.lastSessionFile.v1';
globalThis.lastSessionFiles=(()=>{try{const value=JSON.parse(localStorage.getItem(LAST_FILE_STORE_KEY)||'{}');
  return value&&typeof value==='object'&&!Array.isArray(value)?value:{};}catch(_){return{};}})();
function rememberSessionFile(sid,fileId){
  if(!sid||!/^[0-9a-f]{24}$/.test(String(fileId||'')))return;
  lastSessionFiles[sid]=fileId;
  const rows=Object.entries(lastSessionFiles).slice(-200);lastSessionFiles=Object.fromEntries(rows);
  try{localStorage.setItem(LAST_FILE_STORE_KEY,JSON.stringify(lastSessionFiles));}catch(_){}
}
globalThis.sessionOpened=false;         // just-opened: force-scroll to bottom on the first render
globalThis.questionResizeActive=null;
const questionScrollPositions=new Map();
globalThis.questionPanelStore=(()=>{try{
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
  event.preventDefault();drawer.style.maxHeight='none';   // content-fit sizing yields to the explicit drag
  const startY=event.clientY,startHeight=drawer.getBoundingClientRect().height;
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
  const label=p.questions.length>1?`Multi-part question · ${(mqSel[sid]?.qi||0)+1} of ${p.questions.length} — ${nativePromptLabel(s)}`:
    `${p.questions[0].header||'Question'} — ${nativePromptLabel(s)}`;
  // A drawer the user never resized hugs its content (max-height); an explicit
  // resize switches to the persisted fixed height (invariant 60 mechanics).
  const sizing=state.height?`style="height:${height}px"`:`style="max-height:${height}px"`;
  return`<section class="question-drawer${collapsed?' collapsed':''}" data-question-key="${esc(questionPanelKey(sid,p.nonce))}"
      ${collapsed?'':sizing}>
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
globalThis.sessionEvidenceOpen=false;
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
  $('#sview').style.display='flex';applyWorkspaceChrome();activateWorkspaceSection();syncVisualViewport();
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
  requestAnimationFrame(()=>{if(sessionView?.sid===sid){renderSession(true);restoreWorkspaceScroll();
    if(workspaceDocked()&&last)render(last,true);}});   // repaint cards: pane selection highlight
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
globalThis.workspaceTouch=null;
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
  $('#sview').style.display='none';applyWorkspaceChrome();
  $('#sbody').innerHTML='';delete $('#sbody').dataset.renderKey;
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
  $('#stitle2').innerHTML=`${sessTitleBlock(s)}
    ${!sessionView.closed&&s.ui_group==='needs_you'?'<span class="chip needs_you">Needs you</span>':''}
    ${sessionView.closed?'<small class="closedbadge">Closed · saved workspace</small>':''}`;
  // Terminal lives behind the ⋮ menu (Console locked decision); only Codex's
  // narrowly proved exact-terminal Open stays as a button left of ⋮ (invariant 30).
  $('#sctrl').innerHTML=(s.provider==='codex'?terminalButton(s):'')+
    overflowMenu('session',s,sessionView.closed?'closed':'session');
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
globalThis.agentView=null;              // {sid, aid} of the open overlay
const agentCache={};             // parent session + aid -> {v, messages, info}
const agentCacheKey=(sid,aid)=>String(sid||'')+'\0'+String(aid||'');
globalThis.agentInfoOpen2=false;        // the info dropdown INSIDE the overlay
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

Object.assign(globalThis,{workspaceScrolls,LAST_FILE_STORE_KEY,questionScrollPositions,questionPanelKey,closedCtx,reopenedSessions,evidenceCache,WORKSPACE_SWIPE_MIN_PX,WORKSPACE_EDGE_SWIPE_PX,terminalAgentStates,closedResumeRequests,closedResumeWarned,agentCache,agentCacheKey,agentRelays});
