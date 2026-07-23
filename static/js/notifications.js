// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{loadBriefing,briefingItem,briefingBudget,briefingGroup,safeGithubUrl,openGithub,openBriefingSource,markBriefingReviewed,toggleBriefing,briefingPanelHtml,renderBriefing,notificationBucket,notificationCounts,updateNotificationBadges,reconcileSystemNotifications,setNotificationSection,notificationParams,loadNotifications,loadNotificationDetail,openNotification,closeNotificationDetail,renderNotificationActionFeedback,notificationPost,snoozeNotification,wakeNotification,muteNotification,retryNotificationDelivery,markNotificationsRead,notificationTime,notificationRow,deliveryProblemRow,notificationDetailHtml,renderNotifications,peekMd});
globalThis.briefingData={ok:true,sections:{attention:[],completed:[],slow:[],outcomes:[],budgets:[],measurements:[],reviewed:[]},unread:0};;
globalThis.briefingLoading=false;globalThis.briefingLoadPromise=null;globalThis.briefingLoadedAt=0;globalThis.briefingOpen=false;globalThis.briefingReviewing=false;globalThis.briefingReviewError='';
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
globalThis.notificationData={ok:true,events:[],delivery_problems:[],unread:0,active:0,event_cursor:0,next_cursor:null};;
globalThis.notificationItems=[];globalThis.notificationSection='needs';globalThis.notificationLoading=false;
globalThis.notificationPollingEnabled=false;
globalThis.notificationError='';globalThis.notificationLoadedAt=0;globalThis.notificationAbort=null;globalThis.notificationSequence=0;
globalThis.notificationDetail=null;globalThis.notificationDetailLoading=false;globalThis.notificationDetailError='';
const notificationActionStates=new Map();globalThis.notificationActionGeneration=0;
if(pushActionFallback&&notificationDetailId)notificationActionStates.set(notificationDetailId,{busy:false,
  message:`${pushActionFallback==='snooze'?'Snooze':'Mute'} from the notification did not complete. Review the current state and try again.`,
  error:true,generation:0});
pushActionFallback='';
globalThis.notificationHistoryQuery=draftValue('filter:notification-history');globalThis.notificationHistoryProvider='';globalThis.notificationHistoryKind='';
globalThis.notificationHistoryWorkstream='';globalThis.notificationHistorySession='';globalThis.notificationHistoryAge='';
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
globalThis.notificationReconcileBusy=false;
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

Object.assign(globalThis,{notificationKinds,notificationKindDescriptions,notificationSeverityOptions,notificationActionStates});
