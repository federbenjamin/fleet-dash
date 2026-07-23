// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{syncPinnedSessions,agentListHtml,activeSubagents,activeSubagentCard,renderActiveSubagents,toggleSessionPin,pinFeedbackHtml,sessionPressStart,sessionPressEnd,sessionTap,sessionHeaderKey,agentTap,cardAgentTap,renderPinned,applyReaderWidth,cardCls,cardUsesFixedPeekHeight,cardFrame,cardTop,cardAgentList,cardDetail,detailSig,sessionCard,cardTopFocusAnchor,restoreCardTopFocus,reconcileCards});
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
globalThis.sessionPressTimer=null;globalThis.sessionPressTarget=null;globalThis.sessionPressSid=null;globalThis.sessionLongFired=false;
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

Object.assign(globalThis,{pinnedSessions,pinActions,cardAgentTapAttr,setg,previewAgents,previewSessions,clampS,clampA,readerWidth,multiSel,mqSel,otherDraft,elicitDraft,answered});
