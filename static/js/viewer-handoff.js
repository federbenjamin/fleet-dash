// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{setTheme,toggleTheme,closeOverflow,toggleOverflow,overflowMenu,handoffCatalog,handoffArtifactSection,updateHandoffArtifacts,renderHandoff,loadHandoff,openHandoff,closeHandoff,changeHandoffProvider,changeHandoffModel,submitHandoff,openHandoffDestination,handoffLinksHtml});
globalThis.viewerSid=null;
// one persisted reading theme shared by chat, Markdown, and subagent views
function setTheme(light){
  ['#sview','#vbody','#sbody','#abody','#searchviewbody'].forEach(selector=>
    $(selector)?.classList.toggle('light',light));
  try{localStorage.setItem('viewer_light',light?'1':'0');}catch(e){}
}
function toggleTheme(){setTheme(!$('#sview')?.classList.contains('light'));}
(()=>{try{setTheme(localStorage.getItem('viewer_light')==='1');}catch(e){}})();

globalThis.overflowOpen=null;
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
globalThis.handoffView=null;globalThis.handoffPushed=false;globalThis.handoffOpenAfterBack=null;
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


Object.assign(globalThis,{handoffDraftPrefix});
