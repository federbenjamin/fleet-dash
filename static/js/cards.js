// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{activeToolChip,screenBlockNotice,syncPinnedSessions,agentListHtml,activeSubagents,activeSubagentCard,renderActiveSubagents,toggleSessionPin,pinFeedbackHtml,sessionPressStart,sessionPressEnd,sessionTap,sessionHeaderKey,agentTap,cardAgentTap,renderPinned,applyReaderWidth,cardCls,cardUsesFixedPeekHeight,cardFrame,cardMetaRail,cardTop,cardAgentList,sessionCard,cardTopFocusAnchor,restoreCardTopFocus,reconcileCards});
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
  setClass(el,'queue subagentqueue');
  setHtml(el,`<div class="queuehead"><b>Active subagents · ${items.length}</b><span>children working across parent sessions</span></div>
    <div class="activeagentlist">${items.length?items.map(({parent,agent})=>activeSubagentCard(parent,agent)).join(''):
      '<div class="queueempty">No active subagents match this filter.</div>'}</div>`);
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
  if(!items.length){setClass(el,'empty');setHtml(el,'');return;}
  setClass(el,'');
  if(!el.querySelector('.pinhdr'))setHtml(el,'<div class="pinhdr">Pinned</div><div class="pinlist"></div>');
  const list=el.querySelector('.pinlist'),seen=new Set();
  items.forEach(item=>{
    const sid=String(item.session_id||'');seen.add(sid);
    let slot=[...list.children].find(child=>child.dataset.pinSid===sid);
    if(!slot){slot=document.createElement('div');slot.className='pinslot';slot.dataset.pinSid=sid;list.appendChild(slot);}
    const kind=liveById.has(sid)?'live':'closed';
    if(slot.dataset.pinKind!==kind){setHtml(slot,'');slot.dataset.pinKind=kind;}
    if(kind==='live')reconcileCards(slot,[item],'');
    else setHtml(slot,historyRow(item,true));
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
  const pending=s.pending&&(!requestKey(s.pending)||answered[s.session_id]!==requestKey(s.pending));
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
// The Console card's meta strip: status dot+label, model, context bar, quiet
// time, live agent count. One DOM node, two presentations: the desktop right
// rail, and on mobile an inline status row directly under the header (10a) —
// `.cmain{display:contents}` lets flex `order` interleave it there. Alert
// states (stalled / limit) darken the rail surface and redden the quiet clock.
// "stalled" means frozen mid-tool (invariant 7), but until now the card never
// said WHICH tool — so a session wedged on a hung command and one running a slow
// build looked identical. This comes from the transcript fold, not a screen read,
// so it works on sessions outside tmux too.
function activeToolChip(s){
  const t=s.active_tool;if(!t||!t.name)return'';
  const age=t.seconds==null?'':` · ${fmtAge(t.seconds)}`;
  const more=t.count>1?` +${t.count-1}`:'';
  const stalled=s.state==='stalled';
  return`<span class="mrow ctool${stalled?' crit':''}" title="${stalled?
    'this turn has been inside this tool call with no result':
    'the tool call this turn is waiting on'}">${esc(t.name)}${more}${age}</span>`;
}
function cardMetaRail(s){
  const cls=cardCls(s);
  const tone=cls==='needs'?'amber':cls==='stalled'?'red':
    s.ui_group==='working'?'green':'dim';
  const label=String(s.reason_label||stateLabel[s.state]||s.state||'').toLowerCase();
  const running=(s.agents||[]).filter(a=>!terminalAgentStates.has(a.state)).length;
  const ctx=s.ctx_pct==null?
    (s.provider==='codex'||!s.ctx_tokens?'':`<span class="mrow">${fmtTok(s.ctx_tokens)} tok</span>`):
    `<span class="mrow">ctx ${s.ctx_pct}%<span class="railbar"><i class="${s.ctx_pct>=90?'crit':s.ctx_pct>=70?'warn':''}" style="width:${Math.min(s.ctx_pct||0,100)}%"></i></span></span>`;
  return`<div class="cmeta${cls==='stalled'?' alert':''}" role="button" tabindex="-1"
      onclick="sessionTap(event,'${s.session_id}')">
      <span class="mrow cstat ${tone}"><i class="mdot"></i>${esc(label)}</span>
      <span class="mrow cmodel">${modelLabel(s)}</span>
      ${ctx}
      ${s.quiet_s!=null&&s.ui_group!=='available'?`<span class="mrow cquiet${cls==='stalled'?' crit':''}">quiet ${fmtAge(s.quiet_s)}</span>`:''}
      ${s.compacting!=null?`<span class="mrow ccompact" title="${s.compacting_source==='screen'?
        'seen on the terminal — Fleet has been watching it compact for at least this long, and it may have started earlier':
        'a compaction is running — the transcript is frozen until it finishes'}">⧉ compacting ${
        s.compacting_source==='screen'?'≥':''}${fmtAge(s.compacting)}</span>`:''}
      ${activeToolChip(s)}
      ${s.running?`<span class="mrow runskill" title="the skill or slash command this turn is running">${esc(s.running)}</span>`:''}
      ${running?`<span class="mrow cagents">${running} agent${running>1?'s':''}</span>`:''}
    </div>`;
}
// The volatile top of the card — rebuilt every poll (header, peek, pending,
// running agents, meta rail). No native <details> here, so replacing it each
// tick doesn't flash. Pin = right-click (desktop) / long-press (mobile); the
// ⌖ marker shows pinned state. Terminal and navigation actions live in the
// session workspace, never as card buttons.
function cardTop(s){
  if(s.provisional)return provisionalCardTop(s);
  const navigationOnly=!s.primary_action||['open','continue','view'].includes(s.primary_action);
  const showPrimary=s.ui_group!=='needs_you'&&!(navigationOnly&&['claude','codex'].includes(s.provider));
  // A card needs the conversation for exactly one thing: confirming an
  // outstanding optimistic receipt (invariant 34). The peek is `s.last_msg` from
  // the poll, and the file/agent folds this used to feed are gone — so a card
  // with nothing in flight fetches nothing. Measured on production: 49 of 49
  // sessions fetched /api/context every time their conversation moved, and none
  // of them needed it.
  // A card WITH a pending request keeps fetching, because answering it inline
  // creates a receipt whose baseCount must be computed against real messages —
  // from an empty cache an identical earlier answer would confirm it instantly.
  if(s.pending||optimisticList(s.session_id).length)ensureCtx(s.session_id,ctxVersion(s));
  const pinned=pinnedSessions.has(s.session_id);
  const questionCard=cardCls(s)==='needs'&&s.pending&&s.pending.kind!=='permission';
  const headRight=questionCard?
    `<span class="cwaiting">WAITING ${fmtAge(s.quiet_s||0)}</span>`:'';
  return`<div class="cmain">
    <div class="shead${sessionPressSid===s.session_id?' pinpress':''}" role="button" tabindex="0" aria-label="Open chat: ${esc(s.title||s.project||'session')}"
      title="open the full conversation" onclick="sessionTap(event,'${s.session_id}')" onkeydown="sessionHeaderKey(event,'${s.session_id}')"
      oncontextmenu="event.preventDefault();toggleSessionPin('${s.session_id}')"
      ontouchstart="sessionPressStart('${s.session_id}',this)" ontouchend="sessionPressEnd()" ontouchmove="sessionPressEnd()">
      ${s.new_response?'<span class="newdot" role="img" aria-label="new response" title="new response"></span>':''}
      <span class="sname"><span class="stitle">${esc(s.title||s.project||'session')}</span>
        <small>${esc(s.project)}${s.branch&&s.branch!=='HEAD'?` · ${esc(s.branch)}`:''} · ${esc(s.provider||'claude')}</small></span>
      ${s.access==='view_only'?`<span class="accessbadge view_only">view only</span>`:''}
      ${pinned?`<span class="pinmark" title="pinned — right-click or long-press to unpin">⌖ pinned</span>`:''}
      ${headRight}
      ${showPrimary?`<button class="primarybtn" onclick="event.stopPropagation();primarySessionAction('${s.session_id}')">${esc(s.primary_action_label||'Open')}</button>`:''}
    </div>
    ${previewSessions()&&s.last_msg?`<div class="lastmsg sessionpeek${expandedPeeks.has(s.session_id)?' expanded':''}" title="${expandedPeeks.has(s.session_id)?'full peek exposed':'latest message'}" onclick="togglePeekFromTap(event,'${s.session_id}',${expandedPeeks.has(s.session_id)?'true':'false'})">${s.last_msg.role==='user'?'<span class="peekwho">you ·</span>':''}<div class="peekbody"><div class="lmtext peekmd" style="--peek-lines:${clampS()}">${peekMd(s.last_msg.text)}</div><button class="peektoggle ${expandedPeeks.has(s.session_id)?'less':'more'}" type="button" aria-label="${expandedPeeks.has(s.session_id)?'collapse latest message':'expand latest message'}" onclick="event.stopPropagation();togglePeek('${s.session_id}',${expandedPeeks.has(s.session_id)?'false':'true'})">${expandedPeeks.has(s.session_id)?'Less':'...'}</button></div></div>`:''}
    ${s.error?`<div class="lastmsg carderror"><span class="peekwho">provider ·</span><span class="lmtext">${esc(s.error)}</span></div>`:''}
    ${screenBlockNotice(s)}
    ${s.reply_requested&&!s.staging_observer?`<div class="replysignal"><span>Waiting for your reply</span><button onclick="event.stopPropagation();markAvailable('${s.session_id}','${enc(String(s.convo_v||''))}')">mark available</button></div>`:''}
    ${pinFeedbackHtml(s.session_id)}
    ${cardResponseFeedback(s)}
    ${cardPending(s)}
    ${cardAgentList(s)}
    </div>
    ${questionCard?'':cardMetaRail(s)}`;
}
// A session sitting on a dialog Fleet must not answer (invariant 78). Before
// the scan could look at a terminal this rendered as an ordinary idle session
// with no explanation, and the spawn simply appeared to do nothing. Fleet says
// what is on screen and stops there: accepting folder trust is the user's call
// and always the Mac's (invariant 21).
function screenBlockNotice(s){
  if(s.screen_state!=='trust')return'';
  return`<div class="lastmsg screenblock" role="status"><span class="peekwho">terminal ·</span><span class="lmtext">Waiting on Claude's folder-trust prompt. Answer it in the terminal — Fleet never accepts trust for you.</span></div>`;
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
// The old card "more" tail (cardDetail/detailSig) is gone: the Console
// redesign moved every fold it held — mute, handoff links, state evidence,
// delivered files, completed agents — into the session workspace. It had
// been unreachable since, and was the only reason a card read the
// conversation cache.
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
// Reconcile #sessions in place: persist each card node and rebuild only its
// volatile top every poll, so a card is never torn down and remounted (which is
// what used to flash). A focused header control is semantically restored because
// innerHTML necessarily replaces that node; otherwise a poll immediately after
// closing chat drops keyboard focus onto <body>.
function reconcileCards(container,list,emptyMessage='no live sessions'){
  if(!list.length){setHtml(container,emptyMessage?`<div class="empty">${esc(emptyMessage)}</div>`:'');return;}
  if(container.querySelector('.empty'))setHtml(container,'');
  const seen=new Set();
  list.forEach(s=>{
    seen.add(s.session_id);
    let card=container.querySelector('.card[data-sid="'+s.session_id+'"]');
    if(!card){
      card=document.createElement('div');card.dataset.sid=s.session_id;
      const top=document.createElement('div');top.className='ctop';card.appendChild(top);
      container.appendChild(card);
      // setHtml memoises the last string it wrote here; appending a card out of
      // band makes that memo a lie, and a later setHtml(container,'') would then
      // be skipped and leave the card rendered in two places at once.
      container.__setHtml=undefined;
    }
    const frame=cardFrame(s);
    setClass(card,'card'+(cardCls(s)?' '+cardCls(s):'')+
      (pinnedSessions.has(s.session_id)?' pinned':'')+(frame.fixed?' fixedpeek':'')+
      (sessionView?.sid===s.session_id&&workspaceDocked()?' paneopen':''));
    // Rewriting identical HTML is what made untouched cards re-layout — and
    // move under your finger — on every 2s poll and every user action. The
    // string is cheap; the innerHTML parse plus relayout of 48 cards is not.
    // Skipping an unchanged write also preserves focus and scroll for free.
    const top=card.querySelector('.ctop'),html=cardTop(s);
    const changed=top.__setHtml!==html;
    if(changed){
      const focusAnchor=cardTopFocusAnchor(top);
      setHtml(top,html);restoreCardTopFocus(top,focusAnchor);
    }
    // --session-card-lines is the CONFIGURED maximum here; measurePeekOverflow
    // replaces it with the content-hugging minimum (invariant 45/57). Writing
    // the maximum back on every poll made every card grow to the clamp and
    // shrink again — so it is written only when the peek could actually have
    // changed: a new card, a settings change, or a rebuilt .ctop.
    if(changed||card.__frameLines!==frame.lines){
      card.__frameLines=frame.lines;
      card.style.setProperty('--session-card-lines',String(frame.lines));
    }
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
