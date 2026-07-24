import './state-store.js';
import './nav.js';
import './search.js';
import './ui-utils.js';
import './outbox.js';
import './push.js';
import './notifications.js';
import './context.js';
import './viewer-handoff.js';
import './overlays.js';
import './workspace.js';
import './cards.js';
import './settings-actions.js';
import './history-spawn.js';
import './insights.js';
// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{applyInstance,setFleetOffline,render,scheduleRender,flushScheduledRender,tick,scheduleFleetPoll});
globalThis.last=null;
// One paint per frame. A single answer tap used to call render() four or more
// times, and every completed /api/context fetch called it again, so N sessions
// moving in one poll meant N full rebuilds. Callers coalesce here instead; the
// frame's `force` values union, so an explicit user action still overrides the
// touch/scroll render guard even if a background fetch scheduled first.
// rAF is when the browser paints anyway, so nothing is delayed — only repeated.
globalThis.renderFrame=0;globalThis.renderForce=false;globalThis.renderAfter=[];
function scheduleRender(force,after){
  if(typeof after==='function')renderAfter.push(after);
  if(force)renderForce=true;
  if(renderFrame)return;
  renderFrame=requestAnimationFrame(flushScheduledRender);
}
function flushScheduledRender(){
  renderFrame=0;
  const force=renderForce,callbacks=renderAfter;
  renderForce=false;renderAfter=[];
  render(last,force);
  if(typeof settingsOpen!=='undefined'&&settingsOpen)renderSettings();
  // run AFTER the paint: these write into elements the render just created
  // (inline status messages) or measure when the UI actually changed
  for(const callback of callbacks){try{callback();}catch(error){console.error(error);}}
}
globalThis.pollSequence=0;globalThis.pollApplied=0;globalThis.pollController=null;globalThis.pollInFlight=null;globalThis.pollTimer=null;
globalThis.fleetOffline=false;
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
  const nowLabels={all:'All',needs_you:'Needs you',working:'Working',available:'Avail',subagents:'Subagents'};
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
    if(currentRoute==='search'&&searchFilters.kind==='session')renderSearchResults();
    if(currentRoute==='workstreams')renderWorkstreams(workstreamData);
    if(currentRoute==='notifications')renderNotifications();
    renderSavedViews('now');
    renderBudgetPanel();
    $('#rollup').innerHTML=insightsSection();
  }
  checkSpawn(f);
  // Pass `force` through: a pointerdown keeps touching() true for 800ms, so a
  // user action inside the pane (answering a question, sending) would otherwise
  // have its own repaint deferred by the very tap that requested it.
  renderSession(force);
  if(pendingWorkspaceRoute){const route=pendingWorkspaceRoute;pendingWorkspaceRoute=null;applyWorkspaceRoute(route);}
  schedulePeekOverflow();
  applyRouteNav(currentRoute);
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
    if(currentRoute==='search'&&searchFilters.kind==='session'&&
      Date.now()-historyLoadedAt>5000&&!historyLoading)loadHistory(true);
    if(!fleetOffline){void flushOfflineMessages();void resolvePendingReceipts();}
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
