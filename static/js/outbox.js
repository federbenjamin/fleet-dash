// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{resolvePendingReceipts,applyReceiptOutcome,outboxWhen,outboxTarget,loadOutbox,reconcileOutboxOptimistic,renderOutboxCompact,openOutbox,closeOutbox,setOutboxFilter,visibleOutboxItems,outboxRow,renderOutboxFull,mergeOutboxResult,outboxAction,confirmDeleteOutbox,localInputAt,defaultScheduleTime,scheduleButton,closeComposerMenus,syncComposerToolsOpen,toggleComposerMenu,openComposerSchedule,composerTools,canCompose,renderComposer,openSchedule,editOutbox,closeSchedule,scheduleSet,scheduleSpawnChange,scheduleUsageOptions,renderSchedule,submitSchedule});
globalThis.outboxData={ok:true,items:[],summary:{pending:0,attention:0},usage_options:[]};;
globalThis.outboxLoading=false;globalThis.outboxLoadPromise=null;globalThis.outboxLoadedAt=0;globalThis.outboxAccess='unknown';globalThis.outboxFilter='current';globalThis.scheduleView=null;
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
    <div class="freetext composer">${composerTools(s.session_id,inputId)}<textarea id="${inputId}" data-draft-key="${esc(composerDraftKey(s.session_id))}" rows="1" placeholder="${matchMedia('(pointer:coarse)').matches?'send message':`send message · return = newline · ${/Mac|iPhone|iPad|iPod/.test(navigator.platform||'')?'⌘↵':'Ctrl↵'} = send`}" autocomplete="off"
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

// ---- durable action receipts (invariant 76) -------------------------------
// A dropped connection mid-`act` used to end the story: Fleet said "delivery
// uncertain" and nothing durable said otherwise. The server now records what
// happened under the id the browser minted before the request, so a reconnected
// device can ask.
globalThis.actReceiptResolving=false;
async function resolvePendingReceipts(){
  if(actReceiptResolving||!actReceipts.length||fleetOffline)return;
  actReceiptResolving=true;
  try{
    for(const entry of actReceipts.slice()){
      let body=null;
      try{
        const response=await fetch(`/api/act-receipt?rid=${encodeURIComponent(entry.rid)}`);
        body=await response.json();
      }catch(_){return;}                      // still offline: try again next poll
      if(!body||!body.ok){forgetActReceipt(entry.rid);continue;}
      // A missing receipt is genuinely AMBIGUOUS — the request may never have
      // reached the daemon, or its receipt may have been pruned, or the ledger
      // may have refused the write while the keys still landed. Fleet must not
      // turn that into "it did not arrive": stop tracking and leave whatever the
      // user was already shown (invariant 66).
      if(!body.found){forgetActReceipt(entry.rid);continue;}
      applyReceiptOutcome(entry,body.receipt||{});
      forgetActReceipt(entry.rid);
    }
  }finally{actReceiptResolving=false;uiRefresh();}
}
function applyReceiptOutcome(entry,receipt){
  if(entry.optimisticId==null)return;
  if(receipt.state==='delivered'){
    const item=optimisticList(entry.sid).find(row=>row.id===entry.optimisticId);
    if(!item)return;
    // Fleet did reach the provider. That is NOT the same as confirmed: the
    // canonical transcript row still owns the final state (invariant 34), so
    // the row returns to waiting rather than jumping to confirmed.
    delete item.error;item.status='sending';armOptimisticTimeout(item);
    if(!paintOptimisticItem(item))uiRefresh();
    return;
  }
  if(receipt.state==='failed')
    return updateOptimistic(entry.sid,entry.optimisticId,false,
      receipt.error||'Fleet never delivered this — it can be sent again');
  // running or uncertain: the terminal may already have the keys
  markOptimisticUncertain(entry.sid,entry.optimisticId,
    receipt.error||'Delivery uncertain — check the terminal before sending again');
}

// ---- deterministic in-app briefing ---------------------------------------

Object.assign(globalThis,{outboxActions,outboxPending,outboxAttention});
