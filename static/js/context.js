// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{messageFingerprint,mergeFreshConversation,ensureCtx,conversationCache,conversationEndpoint,loadOlderConversation,sessionSettingActionPending,reconcileSessionActions,catalogModelForSession,repairedSessionEffort,sessionSettingValues,sessionSettingsControls,claudePermissionLocked,claudePermissionSelect,modeSelect,fchip,eventRow,nativeRequestLocked,nativePromptCapabilityLocked,nativePromptLocked,nativePromptLabel,withNativeRequestLock,beginOptimisticAnswer,optimisticBucket,optimisticList,canonicalCount,expireOptimistic,paintOptimisticItem,scheduleOptimisticTimeout,armOptimisticTimeout,addOptimistic,composerKey,sessionTailTarget,sessionNearTail,pinSessionTail,scheduleSessionTailPin,observeSessionTail,activeReadingBody,readingBlocks,captureReadingAnchor,restoreReadingAnchor,resizeComposer,composerInput,composerFocus,updateOptimistic,markOptimisticUncertain,visibleOptimistic,restoreOptimistic,dismissOptimistic,optimisticItemHtml,optimisticHtml,quickResponseLabel,beginQuickResponse,finishQuickResponse,reconcileQuickResponses,cardResponseFeedback,convoMsgs,convoBox});
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
globalThis.sessionSettingSequence=0;
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
globalThis.optimisticSequence=0;
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
globalThis.sessionFollowTail=true;globalThis.sessionTailObserver=null;globalThis.sessionReadingIntentRevision=0;
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

Object.assign(globalThis,{ctxCache,ctxVersion,modelLabel,CLAUDE_PERMISSION_LABELS,claudePermissionLabel,providerModeActions,sessionSettingActions,enc,cpb,EVT_ICON,optimisticMessages,quickResponses,nativeRequestLocks,nativeRequestKey,normalizedMessage,OPTIMISTIC_CONFIRM_MS,SESSION_TAIL_THRESHOLD,readingAnchorRevisions});
