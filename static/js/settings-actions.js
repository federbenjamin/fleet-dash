// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{promptScreenKey,ensureScreenPrompt,promptOptionKey,alwaysRow,alwaysLabel,alwaysTitle,alwaysButton,ensurePromptOptions,uiRefresh,loadNotificationPolicy,selectSettingsSection,enterSettingsRoute,leaveSettingsRoute,openSettings,closeSettings,settingsHasEditableFocus,flushFocusedSettingsRender,renderSettings,settingsSectionDescription,settingNumber,timeValue,timeMinutes,cadenceSummary,durationShort,durationParts,policyDurationField,savePolicyDuration,notificationPolicySettingsHtml,policyRuleHtml,saveGlobalPolicy,saveKindPolicy,deviceSettingsHtml,setSessionMuteQuery,sessionSettingsHtml,unmuteSettingsSession,unpinSettingsSession,appearanceSettingsHtml,budgetSectionHtml,advancedSettingsHtml,settingMessage,queueSetting,setNum,setBool,setStr,testLegacyNtfy,toggleMute,mqBlock,mqToggle,mqNav,mqOther,mqSend,elicitationBlock,elicitText,elicitBool,elicitValue,elicitSet,sendElicitation,cardPending,openSessionQ,stagingPendingBox,pendingBox,setSessionMode,setClaudePermissionMode,applyClaudePermissionMode,changeSessionModel,changeSessionEffort,saveSessionSettings,act,pendingQuestion,answerLabel,answerPreview,sendOption,toggleOpt,sendMulti,sendOther,suppressWhileAnswering,sendDismiss,focusSession,askConfirm,closeConfirm,sendInterrupt,closeWorktreeFiles,closeProviderCopy,renderCloseWorktree,confirmForceClose,closeSessionSurfaceAfterClose,executeCloseSession,sendCloseSession,stopAgentParent,copyTxt,sendPerm,imageType,chooseImages,renderImageDrafts,removeImageDraft,uploadImages,sendText,clearSentComposerCapture,queueOfflineText,removeOfflineMessage,flushOfflineMessages,slashClose,slashInput,retryCommands,slashPick});
const SETTINGS_SECTIONS=['notifications','devices','sessions','appearance','budgets','advanced'];
const SETTINGS_LABELS={notifications:'Notifications',devices:'Devices & delivery',sessions:'Sessions',
  appearance:'Appearance',budgets:'Budgets & spawning',advanced:'Advanced'};
globalThis.settingsOpen=false;globalThis.budgetSettingsOpen=false;globalThis.settingsRenderFrame=0;
globalThis.settingsRendering=false;globalThis.settingsRerenderPending=false;globalThis.settingsFocusPending=false;
globalThis.settingsMessage='';
globalThis.settingsSection=(initialDestination.route==='settings'&&SETTINGS_SECTIONS.includes(initialDestination.detail))?
  initialDestination.detail:'notifications';
globalThis.notificationPolicy={ok:true,global:null,kinds:[],muted_sessions:[],next_deliveries:[],recent_deliveries:[]};;
globalThis.notificationPolicyLoading=false;globalThis.notificationPolicyError='';
globalThis.notificationPolicySaveError='';
globalThis.globalPolicySaving=false;globalThis.globalPolicyStatus='';
globalThis.notificationGuideOpen=false;
const policyRuleOpen=new Set(),policyApplyCurrent=new Set(),policySaving=new Map();
// Coalesced: one forced paint per frame, with `after` run once it has happened
// (see scheduleRender in main.js). Anything that writes into an element the
// render creates must go through `after` rather than run on the next line.
function uiRefresh(after){scheduleRender(true,after);}
async function loadNotificationPolicy(force=false){
  if(notificationPolicyLoading)return;
  if(!force&&notificationPolicy.global)return;
  notificationPolicyLoading=true;notificationPolicyError='';if(settingsOpen&&['notifications','sessions'].includes(settingsSection))renderSettings();
  try{const response=await fetch('/api/notification-policy',{cache:'no-store'}),data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'Notification policy unavailable');
    notificationPolicy=data;
  }catch(error){notificationPolicyError=String(error.message||error);}
  finally{notificationPolicyLoading=false;if(settingsOpen&&['notifications','sessions'].includes(settingsSection))renderSettings();}
}
function selectSettingsSection(section){
  if(!SETTINGS_SECTIONS.includes(section))return;
  settingsMessage='';
  if(section!==settingsSection){settingsSection=section;settingsSectionDepth++;
    history.pushState({fdSettingsSection:section},'',`#settings/${section}`);}
  window.scrollTo({top:0,behavior:'auto'});renderSettings(true);
  if(section==='notifications')loadNotificationPolicy();
  if(section==='devices')loadPushState(true);
  if(section==='budgets')loadBudgets();
}
// Settings is an ordinary left-column DESTINATION (operator decision
// 2026-07-24): it renders beside a docked session pane like Search or
// Notifications and never covers it. enterSettingsRoute is navigateTo's
// route-specific setup; leaveSettingsRoute runs when navigation leaves.
function enterSettingsRoute(fromNavigation=true){
  const wasOpen=settingsOpen;
  settingsOpen=true;settingsMessage='';
  if(!fromNavigation){
    const destination=hashDestination();
    if(destination.route==='settings'&&SETTINGS_SECTIONS.includes(destination.detail))
      settingsSection=destination.detail;
  }else if(!location.hash.startsWith('#settings/'))
    history.replaceState({fdRoute:'settings'},'',`#settings/${settingsSection}`);
  if(!wasOpen){
    $('#settings').innerHTML='<div class="ctxload"><span class="delivery sending" aria-hidden="true">◌</span> loading settings…</div>';
    cancelAnimationFrame(settingsRenderFrame);
    settingsRenderFrame=requestAnimationFrame(()=>{
      settingsRenderFrame=0;if(settingsOpen)renderSettings(true);
    });
    loadPushState(true);
    loadNotificationPolicy(true);
    loadBudgets();
    loadWorkstreams();
  }else renderSettings(true);
}
function leaveSettingsRoute(){
  cancelAnimationFrame(settingsRenderFrame);settingsRenderFrame=0;
  settingsOpen=false;settingsSectionDepth=0;
}
function openSettings(section=settingsSection){
  if(SETTINGS_SECTIONS.includes(section))settingsSection=section;
  navigateTo('settings');
}
function closeSettings(){
  if(currentRoute==='settings')navigateTo('now');
  else leaveSettingsRoute();
}
function settingsHasEditableFocus(){const active=document.activeElement;
  if(!active||!$('#settings')?.contains(active))return false;
  if(active.matches('textarea,select,[contenteditable="true"]'))return true;
  return active.matches('input')&&!['checkbox','radio','button','submit','reset','range','color','file'].includes(active.type);}
function flushFocusedSettingsRender(){
  if(!settingsFocusPending||settingsHasEditableFocus())return;
  settingsFocusPending=false;renderSettings();
}
function renderSettings(force=false){
  if(settingsRendering){settingsRerenderPending=true;return;}
  if(!force&&settingsOpen&&settingsHasEditableFocus()){settingsFocusPending=true;return;}
  settingsRendering=true;
  const el=$('#settings');
  try{
    if(!settingsOpen){setHtml(el,'');return;}
    const sectionHtml={notifications:notificationPolicySettingsHtml,devices:deviceSettingsHtml,
      sessions:sessionSettingsHtml,appearance:appearanceSettingsHtml,budgets:budgetSectionHtml,
      advanced:advancedSettingsHtml}[settingsSection]();
    setHtml(el,`<div class="settingsapp"><nav class="settingsrail" aria-label="Settings sections">
        ${SETTINGS_SECTIONS.map(section=>`<button class="${section===settingsSection?'active':''}" aria-current="${section===settingsSection?'page':'false'}" onclick="selectSettingsSection('${section}')"><span>${esc(SETTINGS_LABELS[section])}</span></button>`).join('')}</nav>
      <div class="settingsmobile"><label>Section<select onchange="selectSettingsSection(this.value)">${SETTINGS_SECTIONS.map(section=>`<option value="${section}" ${section===settingsSection?'selected':''}>${esc(SETTINGS_LABELS[section])}</option>`).join('')}</select></label></div>
      <section class="settingscontent"><header><h2>${esc(SETTINGS_LABELS[settingsSection])}</h2><p>${esc(settingsSectionDescription(settingsSection))}</p></header>${sectionHtml}<div class="actmsg" id="setmsg">${esc(settingsMessage)}</div></section></div>`);
  }finally{
    settingsRendering=false;
    if(settingsRerenderPending){settingsRerenderPending=false;requestAnimationFrame(()=>renderSettings());}
  }
}
$('#settings').addEventListener('focusout',()=>setTimeout(flushFocusedSettingsRender,100));
function settingsSectionDescription(section){return{
  notifications:'Choose exactly which events can push, when they fire, and when they repeat.',
  devices:'Install Fleet, connect browsers, verify delivery health, and run manual legacy tests.',
  sessions:'Conversation peeks, stalled-session timing, and every persistent mute and pin.',
  appearance:'Navigation side, reading theme, and reading width on this device.',
  budgets:'Set visibility and spawn guardrails from measured local usage.',
  advanced:'Diagnostics.'}[section]||'';}
function settingNumber(st,key,step=1,min=0){return`<input type="number" value="${st[key]??''}" min="${min}" step="${step}" oninput="setNum('${key}',this.value)">`;}
function timeValue(minutes){const value=Math.max(0,Math.min(1439,Number(minutes)||0));return`${String(Math.floor(value/60)).padStart(2,'0')}:${String(value%60).padStart(2,'0')}`;}
function timeMinutes(value){const [hours,minutes]=String(value).split(':').map(Number);return hours*60+minutes;}
function cadenceSummary(rule){if(rule.mode==='off')return'No external pushes';
  const delay=rule.initial_delay_seconds?`after ${durationShort(rule.initial_delay_seconds)}`:'now';
  if(rule.mode==='once')return`Once · ${delay}`;
  if(rule.mode==='remind_once')return`${delay} · one reminder after ${durationShort(rule.repeat_interval_seconds)}`;
  return`${delay} · every ${durationShort(rule.repeat_interval_seconds)} · max ${rule.max_deliveries}`;}
function durationShort(seconds){seconds=Number(seconds)||0;return seconds%86400===0?`${seconds/86400}d`:seconds%3600===0?`${seconds/3600}h`:seconds%60===0?`${seconds/60}m`:`${seconds}s`;}
function durationParts(seconds){seconds=Math.max(0,Number(seconds)||0);
  if(seconds&&seconds%86400===0)return[seconds/86400,86400];
  if(seconds&&seconds%3600===0)return[seconds/3600,3600];
  if(seconds%60===0)return[seconds/60,60];return[seconds,1];}
function policyDurationField(rule,field,label,help,locked=false){const [amount,unit]=durationParts(rule[field]),id=`policy-${rule.kind}-${field}`,disabled=locked?'disabled':'';
  return`<label>${label}<span class="durationinput"><input id="${id}-amount" type="number" min="${field==='initial_delay_seconds'?0:1}" max="604800" value="${amount}" ${disabled} onchange="savePolicyDuration('${rule.kind}','${field}')"><select id="${id}-unit" aria-label="${esc(label)} unit" ${disabled} onchange="savePolicyDuration('${rule.kind}','${field}')">${[[1,'seconds'],[60,'minutes'],[3600,'hours'],[86400,'days']].map(([value,name])=>`<option value="${value}" ${unit===value?'selected':''}>${name}</option>`).join('')}</select></span><small class="settinghelp">${esc(help)}</small></label>`;}
function savePolicyDuration(kind,field){const id=`policy-${kind}-${field}`,amount=Number(document.getElementById(id+'-amount')?.value),unit=Number(document.getElementById(id+'-unit')?.value),seconds=Math.round(amount*unit),minimum=field==='initial_delay_seconds'?0:60;
  if(!Number.isFinite(amount)||!Number.isFinite(unit)||amount<0||seconds<minimum||seconds>604800){policySaving.set(kind,field==='initial_delay_seconds'?'error · enter 0 seconds to 7 days':'error · enter 1 minute to 7 days');renderSettings();return;}
  saveKindPolicy(kind,{[field]:seconds});}
function notificationPolicySettingsHtml(){
  if(notificationPolicyLoading&&!notificationPolicy.global)return'<div class="ctxload"><span class="delivery sending">◌</span> Loading notification policy…</div>';
  if(notificationPolicyError)return`<div class="pusherror">${esc(notificationPolicyError)} <button onclick="loadNotificationPolicy(true)">retry</button></div>`;
  const global=notificationPolicy.global;if(!global)return'<div class="ctxload">Notification policy unavailable</div>';
  const onRules=(notificationPolicy.kinds||[]).filter(rule=>rule.mode!=='off').length;
  const inAppRules=(notificationPolicy.kinds||[]).filter(rule=>rule.in_app_enabled!==false).length;
  const next=(notificationPolicy.next_deliveries||[])[0];
  const recent=(notificationPolicy.recent_deliveries||[])[0];
  const recentState=recent?String(recent.status||'unknown').replaceAll('_',' '):'';
  const globalDisabled=globalPolicySaving?'disabled':'';
  return`${notificationPolicySaveError?`<div class="pusherror" role="alert">${esc(notificationPolicySaveError)}</div>`:''}<div class="policyanswer"><span class="pushsignal ${global.enabled?'healthy':'off'}"></span><span><b>In app ${inAppRules}/${notificationPolicy.kinds.length} · Push ${global.enabled?'on':'off'} · ${onRules}/${notificationPolicy.kinds.length} push rules · ${pushData.enabled_devices||0} device${(pushData.enabled_devices||0)===1?'':'s'}</b><small><span>${next?`Next: ${esc(next.title)} · ${notificationTime(next.next_attempt_at)}`:global.quiet_now?`Quiet hours until ${notificationTime(global.quiet_ends_at)}`:'No delivery currently scheduled'}</span><span>${recent?`Last delivery: ${esc(recentState)} · ${esc(recent.title)} · ${notificationTime(recent.updated_at)}`:'No delivery attempt recorded yet'}</span></small></span></div>
    <details class="policyguide" ${notificationGuideOpen?'open':''} ontoggle="notificationGuideOpen=this.open"><summary><span><b>What these settings mean</b><small>In-app visibility, Web Push cadence, severity, and which rule wins</small></span><span aria-hidden="true">⌄</span></summary><div class="policyguidebody"><p><b>Show in Fleet</b> controls the Notification Center, unread/active badges, and app badge for that event type. <b>Web Push</b> is separate: cadence limits external pushes to connected devices. Turning push off does not hide in-app events; turning in-app off does not silently change push cadence.</p><div class="severityladder"><span><b>Info</b><small>Routine updates. Selecting Info includes every severity.</small></span><span><b>Warning</b><small>Needs attention or has stopped progressing.</small></span><span><b>Critical</b><small>A severe budget, repository, or scheduled-delivery failure.</small></span></div><p><b>What wins for Web Push:</b> Push off → device paused or unavailable → session mute → event snooze → quiet hours → event rule. One global push policy applies to every enabled device.</p></div></details>
    <div class="settingscard"><label class="settingsswitch"><span><b>External push notifications</b><small>Turn automatic Web Push on or off for every enabled device. In-app events stay visible.</small></span><input type="checkbox" ${global.enabled?'checked':''} ${globalDisabled} onchange="saveGlobalPolicy({enabled:this.checked})"></label>
      <label class="settingsswitch"><span><b>Quiet hours</b><small>Hold external pushes during this window and send at most one held push per event afterward.</small></span><input type="checkbox" ${global.quiet_hours_enabled?'checked':''} ${globalDisabled} onchange="saveGlobalPolicy({quiet_hours_enabled:this.checked})"></label>
      ${global.quiet_hours_enabled?`<div class="quietgrid"><label>Starts<input aria-label="Starts" type="time" value="${timeValue(global.quiet_start_minute)}" ${globalDisabled} onchange="saveGlobalPolicy({quiet_start_minute:timeMinutes(this.value)})"><small class="settinghelp">The local time when Fleet starts holding pushes.</small></label><label>Ends<input aria-label="Ends" type="time" value="${timeValue(global.quiet_end_minute)}" ${globalDisabled} onchange="saveGlobalPolicy({quiet_end_minute:timeMinutes(this.value)})"><small class="settinghelp">The local time when held events may push again.</small></label><label>Timezone<input aria-label="Timezone" value="${esc(global.timezone)}" list="fleet-timezones" ${globalDisabled} onchange="saveGlobalPolicy({timezone:this.value})"><datalist id="fleet-timezones"><option value="${esc(Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC')}"><option value="UTC"></datalist><small class="settinghelp">Interprets start and end times, including daylight-saving changes.</small></label></div>`:''}${globalPolicyStatus?`<div class="setsavestate" role="status">${esc(globalPolicyStatus)}</div>`:''}</div>
    <div class="settingssubhead"><span><b>Event rules</b><small>One global policy applies to every enabled device.</small></span><button onclick="navigateTo('notifications')">View delivery history</button></div>
    <div class="policyrules">${notificationPolicy.kinds.map(policyRuleHtml).join('')}</div>`;
}
function policyRuleHtml(rule){const open=policyRuleOpen.has(rule.kind),saving=policySaving.get(rule.kind)||'',locked=saving==='saving…',disabled=locked?'disabled':'';
  const inApp=rule.in_app_enabled!==false;
  const pushMode={off:'OFF',once:'ONCE',remind_once:'ONCE +REMINDER',repeat:'REPEAT'}[rule.mode]||rule.mode.toUpperCase();
  const note=[rule.mode==='off'?'':cadenceSummary(rule),
    rule.mode!=='off'&&rule.allow_during_quiet_hours?'quiet hours exempt':'',
    rule.active_matches?`${rule.active_matches} active`:''].filter(Boolean).join(' · ');
  return`<details class="policyrule" data-policy-kind="${esc(rule.kind)}" ${open?'open':''} ontoggle="this.open?policyRuleOpen.add('${rule.kind}'):policyRuleOpen.delete('${rule.kind}')"><summary><span class="polkind"><b>${esc(notificationKinds[rule.kind]||rule.kind)}</b><small>severity ${esc(rule.minimum_severity==='info'?'all':rule.minimum_severity)}</small></span><span class="polchips"><i class="polchip ${inApp?'on':''}">FLEET CENTER · ${inApp?'ON':'OFF'}</i><i class="polchip ${rule.mode!=='off'?'on':''}">PUSH · ${esc(pushMode)}</i></span><span class="polnote">${esc(note)}</span><em class="${saving.startsWith('error')?'error':''}">${esc(saving||'')}</em></summary><div class="policycontrols">
    <p class="policykindhelp">${esc(notificationKindDescriptions[rule.kind]||'Controls external pushes for this event type.')}</p>
    <label class="policycheck"><input aria-label="Show ${esc(notificationKinds[rule.kind]||rule.kind)} in Fleet" type="checkbox" ${inApp?'checked':''} ${disabled} onchange="saveKindPolicy('${rule.kind}',{in_app_enabled:this.checked})"><span>Show in Fleet Notification Center<small class="settinghelp">Controls in-app history, unread and active counts, the page title, and app badge for this event type. Independent of Web Push.</small></span></label>
    <label>Web Push cadence<select aria-label="Web Push cadence" ${disabled} onchange="saveKindPolicy('${rule.kind}',{mode:this.value})">${[['off','Off'],['once','Once'],['remind_once','Once + reminder'],['repeat','Repeat until resolved']].map(([value,label])=>`<option value="${value}" ${rule.mode===value?'selected':''}>${label}</option>`).join('')}</select><small class="settinghelp">How many external pushes Fleet may send while this event remains unresolved. Off affects Web Push only.</small></label>
    <label>Minimum severity<select aria-label="Minimum severity" ${disabled} onchange="saveKindPolicy('${rule.kind}',{minimum_severity:this.value})">${notificationSeverityOptions.map(([value,label])=>`<option value="${value}" ${rule.minimum_severity===value?'selected':''}>${label}</option>`).join('')}</select><small class="settinghelp">Filters this rule by Fleet-assigned urgency; it does not change sound, color, or presentation.</small></label>
    ${rule.mode!=='off'?policyDurationField(rule,'initial_delay_seconds','Initial delay','How long Fleet waits after the event begins before the first push.',locked):''}
    ${['remind_once','repeat'].includes(rule.mode)?policyDurationField(rule,'repeat_interval_seconds','Reminder interval','Time between successful pushes while the event still needs attention.',locked):''}
    ${rule.mode==='repeat'?`<label>Maximum deliveries<input aria-label="Maximum deliveries" type="number" min="1" max="100" value="${rule.max_deliveries}" ${disabled} onchange="saveKindPolicy('${rule.kind}',{max_deliveries:Number(this.value)})"><small class="settinghelp">Includes the first successful push; automatic transport retries do not count.</small></label>`:''}
    ${rule.mode!=='off'?`<label class="policycheck"><input type="checkbox" ${rule.allow_during_quiet_hours?'checked':''} ${disabled} onchange="saveKindPolicy('${rule.kind}',{allow_during_quiet_hours:this.checked})"><span>Allow during quiet hours<small class="settinghelp">Bypasses quiet hours only. Device pause, session mute, and event snooze still win.</small></span></label>`:''}
    ${rule.active_matches?`<label class="policycheck"><input type="checkbox" ${policyApplyCurrent.has(rule.kind)?'checked':''} onchange="this.checked?policyApplyCurrent.add('${rule.kind}'):policyApplyCurrent.delete('${rule.kind}')"><span>Apply this change to ${rule.active_matches} active event${rule.active_matches===1?'':'s'}<small class="settinghelp">Without this, the changed rule starts with future or revised events.</small></span></label>`:''}</div></details>`;}
async function saveGlobalPolicy(patch){const global=notificationPolicy.global;if(!global||globalPolicySaving)return;
  const previous={...global};notificationPolicySaveError='';globalPolicySaving=true;globalPolicyStatus='saving…';Object.assign(global,patch);renderSettings();
  try{const data=await pushApi('/api/notification-policy',{scope:'global',expected_revision:previous.revision,patch});notificationPolicy=data;}
  catch(error){notificationPolicy.global=previous;notificationPolicySaveError=String(error.message||error);}
  finally{globalPolicySaving=false;globalPolicyStatus=notificationPolicySaveError?'':'saved ✓';renderSettings();if(globalPolicyStatus)setTimeout(()=>{globalPolicyStatus='';if(settingsOpen&&settingsSection==='notifications')renderSettings();},1200);}}
async function saveKindPolicy(kind,patch,confirmAggressive=false){const rule=notificationPolicy.kinds.find(item=>item.kind===kind);if(!rule)return;
  if(policySaving.get(kind)==='saving…')return;
  const candidate={...rule,...patch},possible=candidate.mode==='repeat'?Math.min(candidate.max_deliveries,1+Math.floor(86400/candidate.repeat_interval_seconds)):candidate.mode==='remind_once'?2:1;
  if(possible>12&&!confirmAggressive){askConfirm('High notification cadence',`This rule can send up to ${possible} pushes per day until the event resolves.`,
      'use high cadence',()=>saveKindPolicy(kind,patch,true),true);return;}
  const previous={...rule};notificationPolicySaveError='';Object.assign(rule,patch);policySaving.set(kind,'saving…');renderSettings();
  try{const data=await pushApi('/api/notification-policy',{scope:'kind',kind,expected_revision:previous.revision,
      patch,apply_current:policyApplyCurrent.has(kind),confirm_aggressive:confirmAggressive});notificationPolicy=data;
    if(Object.prototype.hasOwnProperty.call(patch,'in_app_enabled'))void loadNotifications(true,true);
    policySaving.set(kind,'saved ✓');setTimeout(()=>{if(policySaving.get(kind)==='saved ✓'){policySaving.delete(kind);renderSettings();}},1200);}
  catch(error){Object.assign(rule,previous);policySaving.set(kind,`error · ${String(error.message||error)}`);}renderSettings();}
function deviceSettingsHtml(){const devices=pushData.devices||[],st=(last&&last.settings)||{};return`${pushSettingsHtml()}
  <div class="settingssubhead"><span><b>Connected devices</b><small>Pause applies only to the selected device. Event rules are global.</small></span></div>
  <div class="devicelist">${devices.length?devices.map(device=>{const current=device.id===briefingDevice,id=enc(device.id),name=device.display_name||'Fleet device';return`<article class="devicecard ${current?'current':''}" data-push-device="${esc(device.id)}"><span class="pushsignal ${esc(device.health||'off')}"></span><span class="deviceidentity"><b>${esc(name)}</b><small>${esc([device.platform,String(device.health||'unknown').replaceAll('_',' ')].filter(Boolean).join(' · '))}</small></span>${current?'<em>this device · controls above</em>':`<div class="devicecontrols"><input aria-label="Rename ${esc(name)}" value="${esc(name)}" maxlength="80" onchange="updateListedPushDevice(decodeURIComponent('${id}'),{display_name:this.value.trim()})"><button onclick="testListedPushDevice(decodeURIComponent('${id}'))" ${device.enabled?'':'disabled'}>Test</button><button onclick="updateListedPushDevice(decodeURIComponent('${id}'),{enabled:${device.enabled?'false':'true'}})">${device.enabled?'Pause':'Resume'}</button><button class="danger" onclick="confirmForgetPushDevice(decodeURIComponent('${id}'),decodeURIComponent('${enc(name)}'))">Remove</button><small class="devicefeedback" role="status"></small></div>`}</article>`;}).join(''):'<div class="settingsempty">No device is connected yet.</div>'}</div>
  <div class="settingscard"><div class="settingssubhead"><span><b>Legacy ntfy</b><small>Manual test delivery only. Never an automatic fallback.</small></span></div>
    <label class="settingsswitch"><span><b>Enable manual legacy tests</b><small>${st.legacy_ntfy_configured?'Configured':'Add ntfy_server and ntfy_topic to config.json first.'}</small></span><input type="checkbox" ${st.legacy_ntfy_enabled===true?'checked':''} onchange="setBool('legacy_ntfy_enabled',this.checked)"></label>
    <div class="pushactions"><button onclick="testLegacyNtfy()" ${legacyNtfyBusy||st.legacy_ntfy_enabled!==true||!st.legacy_ntfy_configured?'disabled':''}>${legacyNtfyBusy?'<span class="delivery sending">◌</span> Queuing…':'Send legacy test'}</button></div>
    ${legacyNtfyMessage?`<div class="${legacyNtfyError?'pusherror':'pushnotice'}" role="status">${esc(legacyNtfyMessage)}</div>`:''}</div>`;}
globalThis.sessionMuteQuery=localStorage.getItem('settings_mute_query')||'';
function setSessionMuteQuery(value){sessionMuteQuery=String(value||'');localStorage.setItem('settings_mute_query',sessionMuteQuery);
  const query=sessionMuteQuery.trim().toLowerCase(),rows=[...document.querySelectorAll('.mutelist article[data-search]')];let shown=0;
  rows.forEach(row=>{const visible=!query||String(row.dataset.search||'').includes(query);row.hidden=!visible;if(visible)shown++;});
  const empty=document.querySelector('.mutelist .mute-search-empty');if(empty){empty.hidden=shown>0;empty.textContent=rows.length?'No muted sessions match.':'No sessions are muted.';}}
function sessionSettingsHtml(){const st=(last&&last.settings)||{},muted=notificationPolicy.muted_sessions||[],query=sessionMuteQuery.trim().toLowerCase();
  const pins=[...pinnedSessions];
  return`<div class="settingscard"><label class="settingsswitch"><span><b>Session conversation peek</b><small>Show the newest messages, tool use, and events from full chat.</small></span><input type="checkbox" ${st.preview_sessions!==false?'checked':''} onchange="setBool('preview_sessions',this.checked)"></label>
    <label class="settingsfield"><span><b>Session peek height</b><small>Maximum visible activity lines; shorter activity shrinks the card.</small></span><span>${settingNumber(st,'preview_session_lines',1,1)} lines</span></label>
    <label class="settingsswitch"><span><b>Subagent conversation peek</b><small>Show a short latest-message preview on subagent rows.</small></span><input type="checkbox" ${st.preview_agents?'checked':''} onchange="setBool('preview_agents',this.checked)"></label>
    <label class="settingsfield"><span><b>Subagent peek height</b></span><span>${settingNumber(st,'preview_agent_lines',1,1)} lines</span></label></div>
    <div class="settingscard"><label class="settingsfield"><span><b>Stalled-session threshold</b><small>How long a working session can show no progress before Fleet marks it stalled.</small></span><span>${settingNumber(st,'stall_seconds',30,30)} seconds</span></label></div>
    <div class="settingssubhead"><span><b>Muted sessions</b><small>Muted until you manually unmute them.</small></span><em>${muted.length}</em></div>
    ${muted.length?`<label class="settingssearch"><span>Search muted sessions</span><input type="search" value="${esc(sessionMuteQuery)}" placeholder="session, project, or provider" oninput="setSessionMuteQuery(this.value)"></label>`:''}
    <div class="mutelist">${muted.map(item=>{const session=((last||{}).sessions||[]).find(s=>s.session_id===item.session_id),search=[item.session_id,item.provider,session?.title,session?.project].filter(Boolean).join(' ').toLowerCase(),visible=!query||search.includes(query);return`<article data-search="${esc(search)}" ${visible?'':'hidden'}><span><b>${esc(session?.title||session?.project||item.session_id)}</b><small>${esc(item.provider||session?.provider||'session')}</small></span><button onclick="unmuteSettingsSession(decodeURIComponent('${enc(item.session_id)}'))">Unmute</button></article>`;}).join('')}<div class="settingsempty mute-search-empty" ${muted.length&&muted.some(item=>{const session=((last||{}).sessions||[]).find(s=>s.session_id===item.session_id);return !query||[item.session_id,item.provider,session?.title,session?.project].filter(Boolean).join(' ').toLowerCase().includes(query);})?'hidden':''}>${muted.length?'No muted sessions match.':'No sessions are muted.'}</div></div>
    <div class="settingssubhead"><span><b>Pinned sessions</b><small>Right-click or long-press any card to change. Order is insertion order.</small></span><em>${pins.length}</em></div>
    <div class="mutelist pinlist">${pins.length?pins.map(sid=>{const session=((last||{}).sessions||[]).find(s=>s.session_id===sid);return`<article><span class="pinmark" aria-hidden="true">⌖</span><span><b>${esc(session?.title||session?.project||sid)}</b><small>${esc([session?.project,session?.provider].filter(Boolean).join(' · ')||'session')}</small></span><button onclick="unpinSettingsSession(decodeURIComponent('${enc(sid)}'))">Unpin</button></article>`;}).join(''):'<div class="settingsempty">No sessions are pinned.</div>'}</div>`;}
async function unpinSettingsSession(sid){await toggleSessionPin(sid);renderSettings(true);}
async function unmuteSettingsSession(sid){const previous=[...(notificationPolicy.muted_sessions||[])];notificationPolicy.muted_sessions=previous.filter(item=>item.session_id!==sid);renderSettings();
  const result=await queueSetting('mute:'+sid,{mute_session:sid,muted:false},()=>{},()=>{notificationPolicy.muted_sessions=previous;});
  if(result.ok)loadNotificationPolicy(true);}
function appearanceSettingsHtml(){const st=(last&&last.settings)||{};
  let light=false;try{light=localStorage.getItem('viewer_light')==='1';}catch(e){}
  return`
  <div class="settingscard"><div class="settingsfield"><span><b>Reading theme</b><small>Full-screen chat, file reader, and subagent views. Stays in this browser.</small></span><div class="setchoice" role="group" aria-label="Reading theme"><button aria-pressed="${!light}" onclick="setTheme(false);renderSettings(true)">Console dark</button><button aria-pressed="${light}" onclick="setTheme(true);renderSettings(true)">Paper light</button></div></div>
    <div class="settingsfield"><span><b>Desktop navigation</b><small>Mobile always uses the bottom navigation.</small></span><div class="setchoice" role="group" aria-label="Desktop navigation side"><button aria-pressed="${navSide==='left'}" onclick="setNavSide('left')">Left side</button><button aria-pressed="${navSide==='right'}" onclick="setNavSide('right')">Right side</button></div></div>
    <div class="settingsfield"><span><b>Full-screen reading width</b><small>Session chat, Markdown, and subagent conversations.</small></span><div class="setchoice" role="group" aria-label="Full-screen reading width"><button aria-pressed="${(st.reader_width||'fit')==='fit'}" onclick="setStr('reader_width','fit')">Fit the screen</button><button aria-pressed="${st.reader_width==='centered'}" onclick="setStr('reader_width','centered')">Centered</button></div></div></div>`;}
function budgetSectionHtml(){return`<div class="settingscard budgetsection">${budgetSettingsHtml()}</div>`;}
function advancedSettingsHtml(){return`
  <div class="settingscard"><div class="settingssubhead"><span><b>Diagnostics</b><small>Operational evidence contains no subscription keys or transcript content.</small></span><button onclick="window.open('/api/diagnostics','_blank','noopener')">Open diagnostics</button></div></div>`;}
const settingQueues=new Map(),settingIntents=new Map();
function settingMessage(id,text){if(id==='setmsg')settingsMessage=String(text||'');const element=document.getElementById(id);if(element)element.textContent=text;}
function queueSetting(key,payload,onSuccess,onFailure,messageId='setmsg',refreshSection=true){
  const intent=(settingIntents.get(key)||0)+1;settingIntents.set(key,intent);
  settingMessage(messageId,'saving…');
  const prior=settingQueues.get(key)||Promise.resolve();
  const request=prior.catch(()=>{}).then(async()=>{
    const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'save failed');
    return data;
  });
  settingQueues.set(key,request);
  return request.then(data=>{
    if(settingIntents.get(key)!==intent)return data;
    onSuccess(data);
    const saved=()=>settingMessage(messageId,'saved ✓');
    if(refreshSection)uiRefresh(saved);else{render(last,true);saved();}
    return data;
  }).catch(error=>{
    if(settingIntents.get(key)===intent){onFailure();
      const failed=()=>settingMessage(messageId,'✗ '+String(error.message||error));
      if(refreshSection)uiRefresh(failed);else{render(last,true);failed();}}
    return{ok:false,error:String(error.message||error)};
  }).finally(()=>{if(settingQueues.get(key)===request)settingQueues.delete(key);});
}
async function setNum(k,v){
  const value=parseFloat(v),previous=last?.settings?.[k];
  if(!Number.isFinite(value))return{ok:false,error:'enter a number'};
  if(last?.settings)last.settings[k]=value;render(last,true);
  return queueSetting(k,{[k]:value},d=>{if(last?.settings)last.settings[k]=d[k];},()=>{
    if(last?.settings)last.settings[k]=previous;
    const field=document.querySelector(`input[oninput="setNum('${CSS.escape(k)}',this.value)"]`);
    if(field)field.value=previous??'';
  },'setmsg',false);
}
async function setBool(k,v){
  const previous=last?.settings?.[k];
  if(last?.settings)last.settings[k]=v;uiRefresh();
  return queueSetting(k,{[k]:v},d=>{if(last?.settings)last.settings[k]=d[k];},()=>{
    if(last?.settings)last.settings[k]=previous;
  });
}
async function setStr(k,v){
  const previous=last?.settings?.[k];
  if(last?.settings)last.settings[k]=v;uiRefresh();
  return queueSetting(k,{[k]:v},d=>{if(last?.settings)last.settings[k]=d[k];},()=>{
    if(last?.settings)last.settings[k]=previous;
  });
}
globalThis.legacyNtfyBusy=false;globalThis.legacyNtfyMessage='';globalThis.legacyNtfyError=false;
async function testLegacyNtfy(){
  if(legacyNtfyBusy)return;const started=performance.now();legacyNtfyBusy=true;
  legacyNtfyMessage='';legacyNtfyError=false;renderSettings();
  recordInputFeedback(started,'legacy_ntfy_test');
  try{
    const response=await fetch('/api/legacy-ntfy/test',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'Legacy test failed');
    legacyNtfyMessage='Legacy test queued';
  }catch(error){legacyNtfyMessage=String(error.message||error);legacyNtfyError=true;}
  finally{legacyNtfyBusy=false;renderSettings();}
}
async function toggleMute(sid,mute,msgId){
  const s=((last||{}).sessions||[]).find(x=>x.session_id===sid);if(!s)return;
  const previous=s.muted;s.muted=mute;uiRefresh();
  return queueSetting('mute:'+sid,{mute_session:sid,muted:mute},()=>{},()=>{s.muted=previous;},
    msgId||'msg-'+sid);
}
// multi-question asks: ONE question on screen at a time, ‹ › to move between them
// (keeps a 3-question ask from swallowing the whole screen)
function mqBlock(s,p,pre){
  const sid=s.session_id;
  let st=mqSel[sid];
  if(!st||st.nonce!==p.nonce){
    st=mqSel[sid]={nonce:p.nonce,qi:0,a:{},other:{}};
    (p.questions||[]).forEach((question,index)=>{
      if(!question.secret)st.other[index]=draftValue(questionDraftPrefix(sid,p.nonce)+`other:${index}`);
    });
  }
  const qs=p.questions;
  if(st.qi>=qs.length)st.qi=qs.length-1;
  const qi=st.qi,q=qs[qi],ms=!!q.multiSelect;
  const sel=st.a[qi]||new Set();
  const ansd=i=>((st.a[i]&&st.a[i].size)||(st.other[i]||'').trim())?1:0;
  const donecnt=qs.reduce((a,_,i)=>a+ansd(i),0);
  const oth=(st.other[qi]||'').trim();
  const picked=[...sel].sort().map(d=>(q.options[d-1]||{}).label)
    .concat(oth?['“'+oth+'”']:[]).join(', ');
  const locked=nativePromptLocked(s,p);
  return`<div class="ptool"><span class="ptlabel">multi-part question (${qs.length}) — ${esc(nativePromptLabel(s))}</span>
      <button class="xbtn" ${locked?'disabled':''} title="${p.dismiss_action==='cancel_turn'?'dismiss by stopping this Codex turn':'dismiss — chat about this instead'}" onclick="sendDismiss('${sid}','${p.nonce}','${pre}')">✕</button></div>
    ${p.files&&p.files.length?`<div class="pfiles"><span class="plabel">read first</span>${p.files.map(f=>fchip(sid,f,f.caption)).join('')}</div>`:''}
    <div class="mqnav"><button class="mqarr" ${qi===0||locked?'disabled':''} onclick="mqNav('${sid}',-1)">‹</button>
      <span class="mqpos"><b>${qi+1}</b> of ${qs.length} · ${donecnt}/${qs.length} answered</span>
      <button class="mqarr" ${qi===qs.length-1||locked?'disabled':''} onclick="mqNav('${sid}',1)">›</button></div>
    <div class="qtext"><b>${esc(q.header||'')}</b> ${esc(q.question)}${ms?' <small>(pick all that apply)</small>':''}</div>
    <div class="optgrid">${(q.options||[]).map((o,i)=>`<button class="optbtn ${sel.has(i+1)?'sel':''}${/\(recommended\)/i.test(String(o.label||''))?' rec':''}" ${locked?'disabled':''}
        onclick="mqToggle('${sid}',${qi},${i+1},${ms},${qs.length})">${esc(o.label)}${o.description?`<small>${esc(o.description)}</small>`:''}</button>`).join('')}</div>
    ${q.allowOther!==false?`<div class="freetext"><input ${q.secret?'':`data-draft-key="${esc(questionDraftPrefix(sid,p.nonce)+`other:${qi}`)}"`} ${locked?'disabled':''} placeholder="Other — type your own answer" ${q.secret?'type="password"':''} value="${esc(st.other[qi]||'')}"
      oninput="mqOther('${sid}',${qi},this.value)"></div>`:''}
    <div class="mqsum">selected: ${picked?esc(picked):'—'}</div>
    <div class="pbtns"><button class="pbtn send" ${locked?'disabled':''} onclick="mqSend('${sid}','${p.nonce}','${pre}')">submit all answers</button></div>`;
}
function mqToggle(sid,qi,d,multi,total){
  const st=mqSel[sid];if(!st)return;
  const sel=st.a[qi]||(st.a[qi]=new Set());
  if(multi){sel.has(d)?sel.delete(d):sel.add(d);}
  else{st.a[qi]=new Set([d]);st.other[qi]='';
       if(st.qi<total-1)st.qi++;}      // picked → advance, like the TUI
  uiRefresh();
}
function mqNav(sid,d){const st=mqSel[sid];if(!st)return;st.qi=Math.max(0,st.qi+d);uiRefresh();}
function mqOther(sid,qi,v){const st=mqSel[sid];if(st)st.other[qi]=v;}
function mqSend(sid,nonce,pre){
  const s=((last||{}).sessions||[]).find(x=>x.session_id===sid);
  const p=s&&s.pending;const st=mqSel[sid];
  if(!p||!st||st.nonce!==nonce)return alert('the prompt changed — refresh');
  const answers=[];
  for(let qi=0;qi<p.questions.length;qi++){
    const digits=[...(st.a[qi]||[])].sort();
    const other=(st.other[qi]||'').trim();
    if(!digits.length&&!other){st.qi=qi;uiRefresh();return alert('answer question '+(qi+1)+' first');}
    const a={multi:!!p.questions[qi].multiSelect,n_options:(p.questions[qi].options||[]).length};
    if(a.multi){if(digits.length)a.digits=digits;if(other)a.other=other;}
    else if(other)a.other=other;
    else a.digits=digits;
    answers.push(a);
  }
  return withNativeRequestLock(sid,nonce,()=>{
    const optimisticId=beginOptimisticAnswer(sid,nonce,answerPreview(sid,answers));
    return act(sid,{type:'multiq',nonce,answers},pre,optimisticId);
  });
}
function elicitationBlock(s,p,pre){
  const sid=s.session_id;
  const locked=nativeRequestLocked(sid,p.nonce);
  const draft=elicitDraft[sid]=elicitDraft[sid]||{};
  for(const field of p.fields||[])if(!field.secret&&!['select','boolean'].includes(field.type)&&draft[field.name]==null)
    draft[field.name]=draftValue(questionDraftPrefix(sid,p.nonce)+`field:${field.name}`);
  const fields=(p.fields||[]).map((f,i)=>{
    const key=enc(f.name),value=draft[f.name];
    if(f.type==='select'&&f.multiSelect)return`<div class="qtext"><b>${esc(f.label)}</b>${f.required?' *':''}</div>
      ${(f.options||[]).map(o=>`<button class="optbtn ${(value||[]).some(v=>String(v)===String(o.value))?'sel':''}"
        onclick="elicitSet('${sid}','${key}','${enc(JSON.stringify(o.value))}',true)">${esc(o.label)}</button>`).join('')}`;
    if(f.type==='select')return`<label class="qtext"><b>${esc(f.label)}</b>${f.required?' *':''}
      <select onchange="elicitValue('${sid}','${key}',this.value)"><option value="">choose…</option>
      ${(f.options||[]).map(o=>`<option value="${enc(JSON.stringify(o.value))}" ${String(value)===String(o.value)?'selected':''}>${esc(o.label)}</option>`).join('')}</select></label>`;
    if(f.type==='boolean')return`<label class="setrow"><input type="checkbox" ${value?'checked':''}
      onchange="elicitBool('${sid}','${key}',this.checked)">${esc(f.label)}</label>`;
    return`<label class="qtext"><b>${esc(f.label)}</b>${f.required?' *':''}<input ${f.secret?'type="password"':`data-draft-key="${esc(questionDraftPrefix(sid,p.nonce)+`field:${f.name}`)}"`}
      value="${esc(value??'')}" oninput="elicitText('${sid}','${key}',this.value)"></label>`;
  }).join('');
  const safeUrl=String(p.url||'').startsWith('https://')||String(p.url||'').startsWith('http://');
  return`<div class="pend"><div class="ptool"><span class="ptlabel">${esc(p.server||'MCP')} request — waiting on you</span></div>
    <div class="qtext">${esc(p.message||'')}</div>${fields}
    ${safeUrl?`<a class="jump" href="${esc(p.url)}" target="_blank" rel="noopener">open request ↗</a>`:''}
    <div class="pbtns">
      <button class="pbtn allow" ${locked?'disabled':''} onclick="sendElicitation('${sid}','${p.nonce}','accept','${pre}')">accept</button>
      <button class="pbtn deny" ${locked?'disabled':''} onclick="sendElicitation('${sid}','${p.nonce}','decline','${pre}')">decline</button>
      <button class="pbtn" ${locked?'disabled':''} onclick="sendElicitation('${sid}','${p.nonce}','cancel','${pre}')">cancel</button>
    </div><div class="actmsg" id="${pre}-${sid}"></div></div>`;
}
function elicitText(sid,key,value){(elicitDraft[sid]||(elicitDraft[sid]={}))[decodeURIComponent(key)]=value;}
function elicitBool(sid,key,value){(elicitDraft[sid]||(elicitDraft[sid]={}))[decodeURIComponent(key)]=value;}
function elicitValue(sid,key,value){
  const name=decodeURIComponent(key);if(!value)delete (elicitDraft[sid]||{})[name];
  else (elicitDraft[sid]||(elicitDraft[sid]={}))[name]=JSON.parse(decodeURIComponent(value));
}
function elicitSet(sid,key,encoded,toggle){
  const name=decodeURIComponent(key),value=JSON.parse(decodeURIComponent(encoded));
  const draft=elicitDraft[sid]||(elicitDraft[sid]={}),values=Array.isArray(draft[name])?draft[name]:[];
  const at=values.findIndex(v=>String(v)===String(value));if(at>=0)values.splice(at,1);else values.push(value);
  draft[name]=values;uiRefresh();
}
function sendElicitation(sid,nonce,choice,pre){
  const s=((last||{}).sessions||[]).find(x=>x.session_id===sid),p=s&&s.pending;
  if(!p||p.nonce!==nonce)return alert('the request changed — refresh');
  const content={...(elicitDraft[sid]||{})};
  if(choice==='accept')for(const f of p.fields||[]){
    if(f.required&&(content[f.name]==null||content[f.name]===''||(Array.isArray(content[f.name])&&!content[f.name].length)))
      return alert(`${f.label||f.name} is required`);
  }
  return withNativeRequestLock(sid,nonce,()=>act(sid,
    {type:'elicitation',nonce,choice,content:choice==='accept'?content:undefined},pre));
}
// On the CARD a question is only a SIGNAL — the option buttons, Other input and
// per-question nav ate the fleet list. Tapping it opens the full view with the
// question expanded. Permission prompts are small and stay inline (allow/deny).
function cardPending(s){
  const p=s.pending;
  if(!p||(requestKey(p)&&answered[s.session_id]===requestKey(p)))return'';
  if(s.staging_observer)return`<div class="pend qsignal stagingreadonly" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">
    <div class="ptool"><span class="ptlabel">production request · view only in staging</span></div>
    <button class="pbtn" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">view ⤢</button></div>`;
  if(s.delivery_uncertain?.nonce===p.nonce)return`<div class="pend deliveryuncertain" role="alert">
    <div class="ptool"><span class="ptlabel">delivery uncertain</span></div>
    <div class="qtext">Check the Claude terminal before doing anything else. Fleet will not retry this answer because some keys may already have landed.</div>
    <button class="pbtn" onclick="event.stopPropagation();tick(true)">refresh state</button></div>`;
  if(p.kind==='permission')return pendingBox(s,'msg');
  if(p.kind==='elicitation')return`<div class="pend qsignal" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">
    <div class="ptool"><span class="ptlabel">${esc(p.server||'MCP')} request — waiting on you</span></div>
    <button class="pbtn qanswer" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">respond ⤢</button></div>`;
  if(p.kind!=='question'||!p.questions||!p.questions.length)return'';
  const n=p.questions.length;
  const q0=p.questions[0],options=q0.options||[];
  // Console question card: the question TEXT plus a summary line — option
  // selectors never render on the card (they live in the full-view drawer).
  const rec=options.find(option=>/\(recommended\)/i.test(String(option.label||'')));
  const recLabel=rec?String(rec.label).replace(/\s*\(recommended\)\s*/i,'').trim():'';
  const summary=[`${options.length} option${options.length===1?'':'s'}`]
    .concat(recLabel?[`recommended: ${recLabel}`]:[])
    .concat(n>1?[`${n} questions`]:[]).join(' · ');
  return`<div class="pend qsignal" onclick="event.stopPropagation();openSessionQ('${s.session_id}')">
    <div class="ptool"><span class="ptlabel">◆ ${esc(n>1?`multi-part question (${n})`:(q0.header||'question'))} — ${esc(nativePromptLabel(s))}</span></div>
    <div class="qcardtext">${esc(q0.question||'')}</div>
    ${p.files&&p.files.length?`<div class="pfiles"><span class="plabel">read first</span>${p.files.map(f=>fchip(s.session_id,f,f.caption)).join('')}</div>`:''}
    <div class="qcardsummary"><span>${esc(summary)}</span><span class="qanswercue">ANSWER IN ${workspaceDockable()?'PANE':'CHAT'} →</span></div>
  </div>`;
}
function openSessionQ(sid){
  const session=((last||{}).sessions||[]).find(item=>item.session_id===sid),pending=session?.pending;
  if(pending?.kind==='question'){
    questionPanelState(sid,pending.nonce).collapsed=false;persistQuestionPanels();
  }
  openSession(sid);
}
function stagingPendingBox(s,p){
  if(p.kind==='question')return`<div class="pend stagingreadonly">
    <div class="ptool"><span class="ptlabel">production question · view only in staging</span></div>
    ${(p.questions||[]).map(q=>`<div class="qtext"><b>${esc(q.header||'Question')}</b> ${esc(q.question||'')}</div>
      ${(q.options||[]).map(option=>`<div class="optbtn" aria-disabled="true">${esc(option.label||'')}${option.description?`<small>${esc(option.description)}</small>`:''}</div>`).join('')}`).join('')}
    <div class="actmsg">Answer this request in production.</div></div>`;
  if(p.kind==='permission')return`<div class="pend stagingreadonly">
    <div class="ptool"><span class="ptlabel">production permission request · view only in staging</span></div>
    <pre>${esc(p.input_summary||'')}</pre><div class="actmsg">Decide this request in production.</div></div>`;
  return`<div class="pend stagingreadonly"><div class="ptool"><span class="ptlabel">production request · view only in staging</span></div>
    <div class="qtext">${esc(p.message||'This request can only be changed in production.')}</div></div>`;
}
// A screen-derived permission is a LABEL in the snapshot and nothing more: the
// scan may look at a terminal but may not publish one, because the snapshot is
// cached on this device by the service worker (invariant 78). So the request's
// own words are fetched per prompt, once, from the on-request screen route.
globalThis.screenPromptCache=globalThis.screenPromptCache||{};
globalThis.screenPromptLoads=globalThis.screenPromptLoads||new Set();
function promptScreenKey(s,p){return`${s.session_id}|${p&&(p.request_id||p.nonce)}`;}
async function ensureScreenPrompt(s,p){
  const key=promptScreenKey(s,p);
  if(key in screenPromptCache||screenPromptLoads.has(key))return;
  screenPromptLoads.add(key);
  try{
    const r=await fetch(`/api/screen?sid=${encodeURIComponent(s.session_id)}`,
      {headers:{'Accept':'application/json'}});
    const d=await r.json();
    // Only a capture that still shows a permission prompt describes THIS one.
    screenPromptCache[key]=d&&d.ok&&Array.isArray(d.lines)
      ?{lines:d.lines.slice(-14)}:{lines:null};
  }catch(_){screenPromptCache[key]={lines:null};}
  finally{screenPromptLoads.delete(key);uiRefresh();}
}
// Every captured permission variant puts Yes/always/No in rows 1/2/3, but row 2's
// WORDING differs sharply: a Bash prompt offers a project-wide directory grant, a
// Read prompt a session-only read, an Overwrite prompt a settings edit. One fixed
// label described all three and was honest about none, so ask the terminal what it
// actually says. Off tmux there is no answer, and the generic label is then the
// truthful one — Fleet genuinely does not know.
globalThis.promptOptionCache=globalThis.promptOptionCache||{};
globalThis.promptOptionLoads=globalThis.promptOptionLoads||new Set();
function promptOptionKey(s,p){return`${s.session_id}|${p&&(p.request_id||p.nonce)}`;}
// The always-allow row is whatever the pane says it is — its wording AND its
// key. Three captured variants offer a persistent grant on row 2, granting
// three very different things under one fixed label; a fourth offers none at
// all and puts "No" there. So the button renders only when a capture proved the
// row, and not at all off tmux where there is nothing to read: allow (row 1) and
// deny (Esc) are correct everywhere, this one never was.
function alwaysRow(s,p){
  const hit=promptOptionCache[promptOptionKey(s,p)];
  return hit&&hit.always_key?hit.always_label||'':'';
}
function alwaysLabel(s,p){
  const row=alwaysRow(s,p);
  // The row starts "Yes, and always allow …" — drop the leading Yes so the
  // button reads as an action, keeping every word that describes the grant.
  return row.replace(/^yes,\s*(and\s+)?/i,'').trim()||row;
}
function alwaysTitle(s,p){
  return `Claude's own wording for this choice: “${alwaysRow(s,p)}”`;
}
// '' until the capture lands, so the button appears a moment after the prompt
// rather than appearing wrong and correcting itself
function alwaysButton(s,p,pre,locked){
  // Codex approvals arrive as kind:'permission' too, but their `always` is a
  // documented App Server decision value, not a keystroke Fleet has to aim at a
  // row — the provider states which decisions it accepts, and that is
  // authoritative. Only Claude's digit needs the screen to prove where it goes.
  if(Array.isArray(p.decisions))
    return p.decisions.includes('always')
      ?`<button class="pbtn always" ${locked?'disabled':''}
        onclick="sendPerm('${s.session_id}','${p.nonce}','always','${pre}')">always allow</button>`:'';
  if(!alwaysRow(s,p))return'';
  return`<button class="pbtn always" ${locked?'disabled':''} title="${esc(alwaysTitle(s,p))}"
    onclick="sendPerm('${s.session_id}','${p.nonce}','always','${pre}')">${esc(alwaysLabel(s,p))}</button>`;
}
async function ensurePromptOptions(s,p){
  // Only Claude's prompt needs its rows read. A provider that states its own
  // decisions has already answered the question this fetch exists to ask, and
  // /api/prompt-options refuses a Codex thread anyway (invariant 74).
  if(!p||p.kind!=='permission'||Array.isArray(p.decisions))return;
  const key=promptOptionKey(s,p);
  if(key in promptOptionCache||promptOptionLoads.has(key))return;
  promptOptionLoads.add(key);
  try{
    const r=await fetch(`/api/prompt-options?sid=${encodeURIComponent(s.session_id)}`,
      {headers:{'Accept':'application/json'}});
    const out=await r.json();
    // Only a capture that still shows a permission prompt describes THIS one.
    promptOptionCache[key]=out&&out.ok&&out.kind==='permission'?out:{options:[]};
  }catch(_){promptOptionCache[key]={options:[]};}
  finally{promptOptionLoads.delete(key);uiRefresh();}
}
function pendingBox(s,pre='msg'){
  const p=s.pending; if(!p)return'';
  if(requestKey(p)&&answered[s.session_id]===requestKey(p))return'';   // sent: dismiss instantly
  if(s.staging_observer)return stagingPendingBox(s,p);
  if(s.delivery_uncertain?.nonce===p.nonce)return`<div class="pend deliveryuncertain" role="alert">
    <div class="ptool"><span class="ptlabel">delivery uncertain — check terminal</span></div>
    <div class="qtext">Some answer keys may already have reached Claude. Fleet has disabled retry for this request until the native prompt changes.</div>
    <div class="pbtns"><button class="pbtn" onclick="tick(true)">refresh state</button></div></div>`;
  if(p.kind==='question'){
    if(!p.questions||!p.questions.length)return'';
    return`<div class="pend">${p.questions.length>1?mqBlock(s,p,pre):singleQBlock(s,p,pre)}
      <div class="actmsg" id="${pre}-${s.session_id}"></div></div>`;
  }
  if(p.kind==='permission'){
    const locked=nativePromptLocked(s,p);
    // A prompt read off the terminal arrives seconds before Claude's hook fires,
    // so it carries no tool name or input yet. The screen text is not in the
    // fleet snapshot by design (invariant 78) — it is fetched per request, so
    // the body fills in a moment after the request itself appears.
    const fromScreen=p.source==='screen';
    if(fromScreen)void ensureScreenPrompt(s,p);
    const seen=fromScreen?screenPromptCache[promptScreenKey(s,p)]:null;
    const body=fromScreen
      ?(seen&&seen.lines?`<pre>${esc(seen.lines.join('\n'))}</pre>`
        :'<div class="ctxload">reading the request from the terminal…</div>')
      :`<pre>${esc(p.input_summary||'')}</pre>`;
    void ensurePromptOptions(s,p);
    return`<div class="pend">
      <div class="ptool">permission: ${esc(fromScreen?'seen on the terminal':p.tool)} — ${esc(nativePromptLabel(s))}</div>
      ${body}
      <div class="pbtns">
        <button class="pbtn allow" ${locked?'disabled':''} onclick="sendPerm('${s.session_id}','${p.nonce}','allow','${pre}')">allow</button>
        ${alwaysButton(s,p,pre,locked)}
        <button class="pbtn deny" ${locked?'disabled':''} onclick="sendPerm('${s.session_id}','${p.nonce}','deny','${pre}')">deny</button>
        ${(p.decisions||[]).includes('cancel')?`<button class="pbtn" ${locked?'disabled':''} onclick="sendPerm('${s.session_id}','${p.nonce}','cancel','${pre}')">cancel</button>`:''}
      </div>
      <div class="actmsg" id="${pre}-${s.session_id}"></div>
    </div>`;
  }
  if(p.kind==='elicitation')return elicitationBlock(s,p,pre);
  return'';
}
async function setSessionMode(sid,mode,pre='msg'){
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(!s||s.provider!=='codex'||providerModeActions.has(sid))return;
  const previous=s.collaboration_mode||'default';
  const message=()=>document.getElementById(pre+'-'+sid)||document.getElementById(pre);
  providerModeActions.set(sid,{kind:'mode'});s.collaboration_mode=mode;
  uiRefresh(()=>{if(message())message().textContent='changing mode…';});
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'mode',mode})});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'mode change failed');
    s.collaboration_mode=d.mode||mode;providerModeActions.delete(sid);uiRefresh();
    if(message())message().textContent=d.warning?`mode changed ✓ · ${d.warning}`:'mode changed ✓';
  }catch(e){s.collaboration_mode=previous;providerModeActions.delete(sid);uiRefresh();
    if(message())message().textContent='✗ '+String(e.message||e);}
}
function setClaudePermissionMode(sid,mode,pre='msg'){
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(!s||s.provider!=='claude'||claudePermissionLocked(s))return;
  if(mode==='bypassPermissions'){
    askConfirm('Use Bypass permissions?',
      '<b>Claude will stop asking before dangerous commands.</b> This removes almost all permission checks for this session. '
      +'Use it only in an isolated, disposable environment whose files and network access cannot cause harm.',
      'use bypass permissions',()=>applyClaudePermissionMode(s,mode,pre),true);
    return;
  }
  applyClaudePermissionMode(s,mode,pre);
}
async function applyClaudePermissionMode(s,mode,pre){
  if(providerModeActions.has(s.session_id))return;
  const previous=s.permission_mode;
  if(previous===mode)return;
  const message=()=>document.getElementById(pre+'-'+s.session_id)||document.getElementById(pre);
  providerModeActions.set(s.session_id,{kind:'permission'});s.permission_mode=mode;
  uiRefresh(()=>{if(message())message().textContent='changing permissions…';});
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:s.session_id,type:'permission_mode',mode})});
    const d=await r.json();
    if(d.code==='control_delivery_uncertain'){
      providerModeActions.delete(s.session_id);uiRefresh();
      if(message())message().textContent='⚠ '+(d.error||'Permission change unconfirmed — check Claude');
      return;
    }
    if(!r.ok||!d.ok)throw new Error(d.error||'permission mode change failed');
    s.permission_mode=d.mode||mode;providerModeActions.delete(s.session_id);uiRefresh();
    if(message())message().textContent=d.warning?`permissions changed ✓ · ${d.warning}`:'permissions changed ✓';
  }catch(e){s.permission_mode=previous;providerModeActions.delete(s.session_id);uiRefresh();
    if(message())message().textContent='✗ '+String(e.message||e);}
}
function changeSessionModel(sid,model){
  const s=((last&&last.sessions)||[]).find(item=>item.session_id===sid);
  if(!s||providerModeActions.has(sid))return;
  const current=sessionSettingValues(s);
  saveSessionSettings(s,model,repairedSessionEffort(s.provider,model,current.effort));
}
function changeSessionEffort(sid,effort){
  const s=((last&&last.sessions)||[]).find(item=>item.session_id===sid);
  if(!s||providerModeActions.has(sid))return;
  const current=sessionSettingValues(s);
  saveSessionSettings(s,current.model,effort);
}
async function saveSessionSettings(s,model,effort){
  if(!s?.capabilities?.change_model_effort||providerModeActions.has(s.session_id))return;
  const catalog=spawnCatalog(s.provider),entry=catalog.find(item=>item.id===model);
  const efforts=entry?.efforts?.length?[...entry.efforts]:spawnEfforts(s.provider,model);
  if(!entry||!efforts.includes(effort))return;
  const sid=s.session_id,version=++sessionSettingSequence;
  // CAS always starts from the newest provider snapshot. Settled action state is
  // feedback only; it must never hide or overwrite a later native TUI change.
  const previous={model:catalogModelForSession(s,catalog),effort:s.effort||''};
  const expectedModel=s.model||'';
  const expectedEffort=s.effort||'';
  providerModeActions.set(sid,{kind:'settings',version});
  // Replacing the state clears any prior saved/error feedback and repairs the
  // effort selector in the same paint as a model selection.
  sessionSettingActions.set(sid,{version,model,effort,message:'Saving…',error:false,pending:true});
  uiRefresh();
  try{
    const response=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'session_settings',model,effort,
        expected_model:expectedModel,expected_effort:expectedEffort})});
    const data=await response.json();
    if(data.code==='control_delivery_uncertain'){
      if(providerModeActions.get(sid)?.version!==version)return;
      providerModeActions.delete(sid);
      sessionSettingActions.set(sid,{version,model,effort,
        message:'Unconfirmed · '+(data.error||'check the Claude terminal'),
        error:true,warning:true,pending:false});
      uiRefresh();return;
    }
    if(!response.ok||!data.ok)throw new Error(data.error||'settings change failed');
    if(providerModeActions.get(sid)?.version!==version)return;
    const acceptedModel=String(data.model||model),acceptedEffort=String(data.effort||'');
    s.model=acceptedModel;s.effort=acceptedEffort||null;
    providerModeActions.delete(sid);
    sessionSettingActions.set(sid,{version,model:acceptedModel,effort:acceptedEffort,
      message:data.warning?`Applied ✓ · ${data.warning}`:'Saved ✓',error:false,
      warning:Boolean(data.warning),pending:false});
    uiRefresh();
  }catch(error){
    if(providerModeActions.get(sid)?.version!==version)return;
    providerModeActions.delete(sid);
    sessionSettingActions.set(sid,{version,model:previous.model,
      effort:repairedSessionEffort(s.provider,previous.model,previous.effort),
      message:'Could not save · '+String(error.message||error),error:true,pending:false});
    uiRefresh();
  }
}
// Every action that can move Claude's native surface gets a durable receipt
// (invariant 76). Mirrors Engine.ACT_RECEIPT_TYPES; read-only probes are absent
// on purpose, because replaying one costs nothing.
const RECEIPT_ACT_TYPES=new Set(['option','multiq','permission','dismiss','elicitation',
  'dismiss_then_send','send_message','text','image_text','handoff_text','relay',
  'interrupt','close','session_settings','permission_mode']);
async function act(sid,payload,pre='msg',optimisticId=null){
  const requestStarted=performance.now();
  const receiptId=RECEIPT_ACT_TYPES.has(payload.type)?
    (payload.client_request_id||actRequestId()):null;
  const isQuick=['permission','dismiss','elicitation'].includes(payload.type);
  const quickId=isQuick?beginQuickResponse(sid,payload):null;
  const setMessage=text=>{if(pre===false||pre==null)return null;
    const el=document.getElementById(pre+'-'+sid)||document.getElementById(pre);
    if(el)el.textContent=text;return el;};
  if(fleetOffline){
    const error='Offline — your draft is saved. Reconnect before sending.';
    setMessage('✗ '+error);if(optimisticId!=null)updateOptimistic(sid,optimisticId,false,error);
    return{ok:false,error,offline:true};
  }
  if(payload.type!=='ping')setMessage('sending…');
  try{
    const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,...payload,
        ...(receiptId?{client_request_id:receiptId}:{})})});
    const d=await r.json();
    if(payload.type!=='ping')perfRecord(`native_${String(payload.type).replace(/[^a-z0-9_]+/gi,'_')}_ms`,
      performance.now()-requestStarted);
    const deliveryUncertain=d.code==='delivery_uncertain';
    if(deliveryUncertain){
      const session=((last||{}).sessions||[]).find(item=>item.session_id===sid);
      if(session)session.delivery_uncertain={nonce:payload.nonce,message:d.error};
      if(optimisticId!=null)markOptimisticUncertain(sid,optimisticId,d.error);
      uiRefresh();
    }else if(optimisticId!=null)updateOptimistic(sid,optimisticId,d.ok,d.error,
      d.ok&&['option','multiq'].includes(payload.type));
    if(optimisticId!=null&&d.queued){const item=optimisticList(sid).find(entry=>entry.id===optimisticId);
      if(item){clearTimeout(item.confirmTimer);item.status='queued';delete item.queueId;item.outboxId=d.outbox_id;
        item.queueLabel=d.message||'Queued · waiting for session';
        item.queueReason=d.queue_reason||d.message||'Waiting for the session to become available';
        rememberOutboxReceipt(item);uiRefresh();}}
    if(quickId!=null)finishQuickResponse(sid,quickId,d.ok,d.error);
    const el=setMessage(d.ok?(d.queued?(d.message||'queued'):'sent ✓'):'✗ '+(d.error||'failed'));
    if(!d.ok&&!el&&quickId==null&&optimisticId==null&&payload.type!=='focus'&&pre!==false)
      alert(d.error||'failed');
    if(d.ok&&payload.nonce&&['option','multiq','permission','dismiss','dismiss_then_send','elicitation'].includes(payload.type)){
      answered[sid]=sessionRequestKey(sid,payload.nonce);  // retain suppression through canonical QA
      clearDraftPrefix(questionDraftPrefix(sid,payload.nonce));
      delete otherDraft[sid];delete mqSel[sid];delete elicitDraft[sid];multiSel[sid]=new Set();
      uiRefresh();
    }
    return d;
  }catch(e){
    if(payload.type!=='ping')perfRecord(`native_${String(payload.type).replace(/[^a-z0-9_]+/gi,'_')}_ms`,
      performance.now()-requestStarted);
    // The response is what was lost, not necessarily the request. Remember the
    // id so a reconnected device can ask the daemon what actually happened.
    if(receiptId)rememberActReceipt({rid:receiptId,sid,type:payload.type,optimisticId});
    const nativeAnswer=['option','multiq'].includes(payload.type);
    const error=nativeAnswer?
      'Delivery uncertain — the connection dropped before Fleet received a result. Check the terminal before answering again.':
      ['send_message','dismiss_then_send'].includes(payload.type)?
      'Delivery unconfirmed — the connection dropped before Fleet received a result. Restore to send again only if it did not arrive.':String(e);
    if(optimisticId!=null){
      if(nativeAnswer)markOptimisticUncertain(sid,optimisticId,error);
      else updateOptimistic(sid,optimisticId,false,error);
    }
    if(quickId!=null)finishQuickResponse(sid,quickId,false,String(e));
    const el=setMessage('✗ '+error);
    if(!el&&quickId==null&&optimisticId==null&&payload.type!=='focus'&&pre!==false)
      alert('request failed: '+e);
    setFleetOffline(true);
    return {ok:false,error,network_error:true};
  }
}
function pendingQuestion(sid){
  const session=((last||{}).sessions||[]).find(item=>item.session_id===sid);
  return session&&session.pending&&session.pending.kind==='question'?session.pending:null;
}
function answerLabel(question,digits,other){
  if(other)return question?.secret?'(private answer)':other;
  return(digits||[]).map(d=>question?.options?.[Number(d)-1]?.label||`Option ${d}`).join(', ');
}
function answerPreview(sid,answers){
  const pending=pendingQuestion(sid),questions=(pending&&pending.questions)||[];
  return answers.map((answer,index)=>{
    const question=questions[index]||{};
    const label=answerLabel(question,answer.digits,answer.other)||'(no answer)';
    return `${question.header||question.question||`Question ${index+1}`}: ${label}`;
  }).join('\n');
}
function sendOption(sid,nonce,digits,pre){
  return withNativeRequestLock(sid,nonce,()=>{
    multiSel[sid]=new Set(digits);
    const optimisticId=beginOptimisticAnswer(sid,nonce,answerPreview(sid,[{digits}]));
    return act(sid,{type:'option',nonce,digits},pre,optimisticId);
  });
}
function toggleOpt(sid,d){
  const s=multiSel[sid];s.has(d)?s.delete(d):s.add(d);uiRefresh();
}
function sendMulti(sid,nonce,n,pre){
  const digits=[...(multiSel[sid]||[])].sort();
  const other=(otherDraft[sid]||'').trim();
  if(!digits.length&&!other)return alert('pick at least one option');
  return withNativeRequestLock(sid,nonce,()=>{
    const optimisticId=beginOptimisticAnswer(sid,nonce,answerPreview(sid,[{digits,other}]));
    return act(sid,{type:'option',nonce,digits,multi:true,n_options:n,other:other||undefined},pre,optimisticId);
  });
}
function sendOther(sid,nonce,n,pre){
  const other=(otherDraft[sid]||'').trim();
  if(!other)return alert('type your answer first');
  return withNativeRequestLock(sid,nonce,()=>{
    const optimisticId=beginOptimisticAnswer(sid,nonce,answerPreview(sid,[{other}]));
    return act(sid,{type:'option',nonce,n_options:n,other},pre,optimisticId);
  });
}
// Hide the selector the moment the action is sent, the way an option answer does
// through beginOptimisticAnswer (invariant 14). Without this a permission prompt
// stays visible and tappable for the whole injection — long enough to answer it
// twice, from one device or from two.
async function suppressWhileAnswering(sid,nonce,work){
  const had=Object.prototype.hasOwnProperty.call(answered,sid),previous=answered[sid];
  answered[sid]=sessionRequestKey(sid,nonce);uiRefresh();
  const result=await work();
  // A definite pre-delivery failure reopens the selector. Uncertainty must NOT:
  // some keys may already have landed, and re-offering the prompt invites a
  // second answer to a request the provider may consider resolved (invariant 66).
  // `duplicate` means the prompt was already answered (this device's in-flight
  // lock, or the server's answered fence). Reopening it would be a lie.
  if(result&&!result.ok&&!result.duplicate&&
      !['duplicate','delivery_uncertain','control_delivery_uncertain'].includes(result.code)){
    if(had)answered[sid]=previous;else delete answered[sid];
  }
  return result;
}
function sendDismiss(sid,nonce,pre){
  return withNativeRequestLock(sid,nonce,()=>
    suppressWhileAnswering(sid,nonce,()=>act(sid,{type:'dismiss',nonce},pre)));
}
// desktop only: pointless from the phone — it focuses a tab on the Mac
async function focusSession(sid,button=null){
  if(terminalActions.get(sid)?.busy)return;
  const feedbackStarted=performance.now(),original=button?.textContent||'Terminal';
  terminalActions.set(sid,{busy:true,ok:false,error:''});
  if(button){button.disabled=true;button.textContent='Opening…';}
  recordInputFeedback(feedbackStarted,'terminal');
  const result=await act(sid,{type:'focus'});
  terminalActions.set(sid,{busy:false,ok:!!result.ok,error:result.error||''});
  if(button&&button.isConnected){button.disabled=false;button.textContent=result.ok?'Opened ✓':'Retry';
    if(!result.ok)button.title=result.error||'Could not open terminal';}
  setTimeout(()=>{const item=terminalActions.get(sid);if(item&&!item.busy){terminalActions.delete(sid);if(last)render(last,true);}},3000);
  return result;
}

// in-app interstitial — a native confirm() is easy to dismiss by reflex on a phone,
// and stopping a turn is destructive (the work in flight is lost)
globalThis.confirmYes=null;
function askConfirm(title,body,confirmLabel,onYes,danger=false){
  closeOverflow();
  confirmYes=onYes;
  $('#confirm').innerHTML=`<div class="cfbox${danger?' cfhigh':''}">
    <div class="cftitle">${esc(title)}</div>
    <div class="cfbody">${body}</div>
    <div class="cfbtns">
      <button class="pbtn" onclick="closeConfirm()">cancel</button>
      <button class="pbtn cfgo${danger?' danger':''}" onclick="const f=confirmYes;closeConfirm();f&&f()">${esc(confirmLabel)}</button>
    </div></div>`;
  $('#confirm').style.display='flex';
}
function closeConfirm(){confirmYes=null;$('#confirm').style.display='none';$('#confirm').innerHTML='';}
function sendInterrupt(sid,pre='msg'){
  askConfirm('Stop this turn?',
    'Sends <b>Esc</b> to the session. Whatever it is doing right now is abandoned — '
    +'including any subagents it has running.',
    'stop the turn',
    ()=>act(sid,{type:'interrupt'},pre));
}
function closeWorktreeFiles(preview){
  const dirty=(preview.dirty_files||[]).map(item=>`<li><span>${esc(item.category||'changed')}</span> ${esc(item.path||'')}</li>`).join('');
  const ignored=(preview.ignored_files||[]).map(path=>`<li><span>ignored</span> ${esc(path)}</li>`).join('');
  const dirtyMore=preview.dirty_files_truncated?`<li>…and more changed paths (${preview.dirty_total} total)</li>`:'';
  const ignoredMore=preview.ignored_files_truncated?`<li>…and more ignored paths (${preview.ignored_count} total)</li>`:'';
  return dirty||ignored?`<ul class="closefiles">${dirty}${dirtyMore}${ignored}${ignoredMore}</ul>`:'';
}
function closeProviderCopy(s,active){
  const provider=s.provider==='codex'?'The Codex thread will be archived.':
    'The registered Claude process will end. Its iTerm tab stays open.';
  return provider+(active?' The current turn and every subagent under it will stop first.':'')+
    ' The conversation remains available in <b>Session history</b>.';
}
function renderCloseWorktree(s,pre,preview,active){
  const shared=(preview.shared_sessions||[]).map(item=>esc(item.title||item.session_id)).join(', ');
  const reason=shared?`<div class="closeblock">Removal is blocked while this worktree is also used by: <b>${shared}</b>.</div>`:
    preview.reason?`<div class="closeblock">${esc(preview.reason)}</div>`:'';
  const path=esc(preview.worktree||s.cwd||'');
  $('#confirm').innerHTML=`<div class="cfbox closechoice">
    <div class="cftitle">Close this session?</div>
    <div class="cfbody">${closeProviderCopy(s,active)}<div class="closepath"><b>Secondary worktree</b>${path}</div>
      ${reason}${closeWorktreeFiles(preview)}</div>
    <div class="closechoices">
      <button class="pbtn" onclick="closeConfirm()">cancel</button>
      <button class="pbtn cfgo" onclick="executeCloseSession('${s.session_id}','${pre}','preserve')">close · preserve worktree</button>
      <button class="pbtn remover" ${preview.remove_allowed?'':'disabled'}
        onclick="executeCloseSession('${s.session_id}','${pre}','remove')">close · remove clean worktree</button>
      ${preview.force_remove_allowed?`<button class="pbtn force" onclick="confirmForceClose('${s.session_id}','${pre}')">force remove dirty worktree</button>`:''}
    </div></div>`;
  $('#confirm').style.display='flex';
}
function confirmForceClose(sid,pre){
  const preview=closePreviewCache.get(sid);if(!preview)return;
  askConfirm('Force remove dirty worktree?',
    '<b>This permanently deletes every listed worktree file, including ignored files.</b> The Git branch survives.'+
    closeWorktreeFiles(preview),
    'close and force remove',()=>executeCloseSession(sid,pre,'force_remove'),true);
}
const closePreviewCache=new Map();
async function closeSessionSurfaceAfterClose(){
  closeConfirm();
  if(histPushed){
    await new Promise(resolve=>{
      window.addEventListener('popstate',()=>resolve(),{once:true});
      history.back();
    });
  }else{closeViewer();closeSession();}
}
async function executeCloseSession(sid,pre,cleanup){
  const preview=closePreviewCache.get(sid);const destructive=cleanup!=='preserve';
  let providerClosed=false;
  $('#confirm').innerHTML=`<div class="cfbox"><div class="cftitle">Closing session…</div>
    <div class="cfbody"><span class="delivery sending" aria-hidden="true">◌</span> ${destructive?'Closing the provider before removing the worktree.':'Preserving the worktree.'}</div></div>`;
  try{
    const closeResponse=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'close',cleanup_ticket:destructive?preview?.cleanup_ticket:undefined})});
    const closed=await closeResponse.json();
    if(!closeResponse.ok||!closed.ok)throw new Error(closed.error||'session close failed');
    providerClosed=true;
    if(!destructive){closePreviewCache.delete(sid);await closeSessionSurfaceAfterClose();setTimeout(()=>tick(),0);return;}
    $('#confirm .cfbody').innerHTML='<span class="delivery sending" aria-hidden="true">◌</span> Session closed. Rechecking the worktree before removal…';
    const cleanupResponse=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'worktree_cleanup',cleanup_ticket:preview.cleanup_ticket,
                           force:cleanup==='force_remove'})});
    const result=await cleanupResponse.json();
    closePreviewCache.delete(sid);await closeSessionSurfaceAfterClose();setTimeout(()=>tick(),0);
    if(!cleanupResponse.ok||!result.ok){
      $('#confirm').innerHTML=`<div class="cfbox"><div class="cftitle">Session closed · worktree preserved</div>
        <div class="cfbody">${esc(result.error||'Cleanup failed')}<div class="closepath">${esc(result.worktree||preview.worktree||'')}</div></div>
        <div class="cfbtns"><button class="pbtn" onclick="closeConfirm()">close</button></div></div>`;
      $('#confirm').style.display='flex';
      return;
    }
    closeConfirm();
  }catch(error){
    if(providerClosed){
      closePreviewCache.delete(sid);await closeSessionSurfaceAfterClose();setTimeout(()=>tick(),0);
    }
    $('#confirm').innerHTML=`<div class="cfbox"><div class="cftitle">${providerClosed?'Session closed · worktree preserved':'Could not close session'}</div>
      <div class="cfbody">${esc(String(error.message||error))}${providerClosed?`<div class="closepath">${esc(preview?.worktree||'')}</div>`:''}</div><div class="cfbtns">
      <button class="pbtn" onclick="closeConfirm()">close</button></div></div>`;
    $('#confirm').style.display='flex';
  }
}
async function sendCloseSession(sid,pre='smsg'){
  const s=((last&&last.sessions)||[]).find(x=>x.session_id===sid);
  if(!s||!s.capabilities?.close)return alert('This session cannot be closed here.');
  const active=['running','stalled','needs_you','stalled_or_prompt'].includes(s.state);
  closeOverflow();
  $('#confirm').innerHTML='<div class="cfbox"><div class="cftitle">Checking worktree…</div><div class="cfbody"><span class="delivery sending" aria-hidden="true">◌</span> Looking for files that closing could remove.</div></div>';
  $('#confirm').style.display='flex';
  try{
    const response=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,type:'close_preview'})});
    const preview=await response.json();
    if(!response.ok||!preview.ok)throw new Error(preview.error||'worktree check failed');
    if(preview.secondary_worktree){closePreviewCache.set(sid,preview);renderCloseWorktree(s,pre,preview,active);return;}
    closeConfirm();
    askConfirm('Close this session?',closeProviderCopy(s,active),active?'stop and close':'close session',
      ()=>executeCloseSession(sid,pre,'preserve'));
  }catch(error){
    closeConfirm();
    askConfirm('Close this session?',closeProviderCopy(s,active)+
      `<div class="closeblock">Fleet could not inspect the worktree: ${esc(String(error.message||error))}. Closing will preserve it.</div>`,
      active?'stop and close':'close session',()=>executeCloseSession(sid,pre,'preserve'));
  }
}
// A subagent has NO terminal: the only way to stop it is to Esc its PARENT, which
// ends the parent's whole turn and every other agent under it. Say so plainly.
function stopAgentParent(sid,pre='amsg'){
  askConfirm('Stop this subagent?',
    'A subagent has no terminal of its own — the only way to stop it is to send <b>Esc</b> '
    +'to its <b>parent session</b>. That ends the parent\'s entire turn and kills '
    +'<b>every other subagent</b> it is running, not just this one.',
    'stop parent turn',
    ()=>act(sid,{type:'interrupt'},pre));
}
function copyTxt(ev,el){
  ev.stopPropagation();
  const original=el.textContent;el.textContent='copying…';
  if(!navigator.clipboard){el.textContent='copy unavailable';setTimeout(()=>{el.textContent=original},1200);return;}
  navigator.clipboard.writeText(el.dataset.copy).then(()=>{
    el.textContent='copied ✓';setTimeout(()=>{el.textContent=original},900);
  }).catch(()=>{el.textContent='copy failed';setTimeout(()=>{el.textContent=original},1200);});
}
function sendPerm(sid,nonce,choice,pre='msg'){
  return withNativeRequestLock(sid,nonce,()=>
    suppressWhileAnswering(sid,nonce,()=>act(sid,{type:'permission',nonce,choice},pre)));
}
function imageType(file){
  const mime=String(file?.type||'').toLowerCase();
  if(['image/jpeg','image/png','image/gif','image/webp','image/heic','image/heif'].includes(mime))return mime;
  const ext=String(file?.name||'').toLowerCase().split('.').pop();
  return ext==='heic'?'image/heic':ext==='heif'?'image/heif':'';
}
async function chooseImages(sid,input){
  closeComposerMenus();
  const message=document.getElementById('smsg-'+sid)||document.getElementById('vmsg-'+sid),
    current=imageDraftIds(sid);
  const files=[...(input?.files||[])];if(input)input.value='';
  if(current.length>=IMAGE_MAX_COUNT){if(message)message.textContent='Remove an image before adding another.';return;}
  let added=0;
  for(const file of files.slice(0,IMAGE_MAX_COUNT-current.length)){
    const type=imageType(file);
    if(!type){if(message)message.textContent='Use JPEG, PNG, GIF, WebP, HEIC, or HEIF images.';continue;}
    if(!file.size||file.size>IMAGE_MAX_BYTES){if(message)message.textContent='Each image must be 10 MB or smaller.';continue;}
    const id=offlineMessageId();
    try{await putImage({id,sid:String(sid),name:String(file.name||'image').slice(0,120),
      type,size:file.size,created:Date.now(),blob:file});current.push(id);added++;}
    catch(error){if(message)message.textContent='Could not save this image on the device.';console.warn(error);break;}
  }
  setImageDraftIds(sid,current);await renderImageDrafts(sid);
  if(message&&added)message.textContent=`${added} image${added===1?'':'s'} attached`;
  void pruneImages();
}
async function renderImageDrafts(sid){
  const target=document.getElementById('imgdraft-'+sid),ids=imageDraftIds(sid);if(!target)return;
  const anchor=captureReadingAnchor();
  if(!ids.length){target.innerHTML='';restoreReadingAnchor(anchor);return;}
  const records=(await Promise.all(ids.map(id=>getImage(id).catch(()=>null)))).filter(Boolean);
  if(!document.getElementById('imgdraft-'+sid))return;
  const missing=ids.filter(id=>!records.some(record=>record.id===id));
  if(missing.length)setImageDraftIds(sid,ids.filter(id=>!missing.includes(id)));
  target.innerHTML=records.map(record=>`<span class="image-draft">🖼 <span>${esc(record.name||'image')}</span><small>${Math.max(1,Math.round(record.size/1024))} KB</small><button type="button" aria-label="remove ${esc(record.name||'image')}" onclick="removeImageDraft(decodeURIComponent('${enc(sid)}'),'${record.id}')">×</button></span>`).join('');
  restoreReadingAnchor(anchor);
}
async function removeImageDraft(sid,id){
  setImageDraftIds(sid,imageDraftIds(sid).filter(value=>value!==id));await deleteImage(id).catch(()=>{});
  await renderImageDrafts(sid);
}
async function uploadImages(sid,imageIds){
  const uploadIds=[];
  for(const id of imageIds){
    const record=await getImage(id).catch(()=>null);
    if(!record)return{ok:false,error:'An attached image is no longer stored on this device.'};
    try{
      const url=`/api/upload-image?sid=${encodeURIComponent(sid)}&id=${encodeURIComponent(id)}&name=${encodeURIComponent(record.name||'image')}`;
      const response=await fetch(url,{method:'POST',headers:{'Content-Type':record.type},body:record.blob});
      const result=await response.json();
      if(!response.ok||!result.ok)return{ok:false,error:result.error||'Image upload failed'};
      uploadIds.push(result.upload_id);
    }catch(error){setFleetOffline(true);return{ok:false,error:String(error),network_error:true};}
  }
  return{ok:true,uploadIds};
}
async function sendText(sid,ftPre='ft',msgPre='msg'){
  const inp=document.getElementById(ftPre+'-'+sid);
  const v=(inp&&inp.value||'').trim(),imageIds=imageDraftIds(sid);
  if(!v&&!imageIds.length)return;
  const text=v||(imageIds.length===1?'Please inspect the attached image.':'Please inspect the attached images.');
  const cmd=(v.startsWith('/')||v.startsWith('$'))?(cmdCache[sid]||[]).find(c=>c.name===v.split(/\s+/)[0]):null;
  const el=document.getElementById(msgPre+'-'+sid)||document.getElementById(msgPre);
  if(imageIds.length&&(v.startsWith('/')||v.startsWith('$'))){if(el)el.textContent='Send commands and images separately.';return;}
  if(fleetOffline){
    if(v.startsWith('/')||v.startsWith('$')){
      if(el)el.textContent='offline — command draft saved; reconnect to run it';
      return;
    }
    const queued=queueOfflineText(sid,text,imageIds,null,pendingQuestion(sid)?.nonce||null);
    if(!queued){if(el)el.textContent='offline queue is full — draft kept here';return;}
    slashClose();closeComposerMenus();if(inp){inp.value='';resizeComposer(inp);}clearDraft(composerDraftKey(sid));setImageDraftIds(sid,[]);void renderImageDrafts(sid);
    if(el)el.textContent='queued offline — sends automatically after reconnection';
    return;
  }
  if(cmd&&cmd.danger&&!confirm(`${cmd.name} destroys this session's conversation state.\n\n${cmd.desc}\n\nSend it?`))return;
  slashClose();closeComposerMenus();
  const pending=pendingQuestion(sid);
  if(pending){
    let uploadIds=[];
    if(imageIds.length){
      if(el)el.textContent='uploading images…';
      const uploaded=await uploadImages(sid,imageIds);
      if(!uploaded.ok){
        if(uploaded.network_error&&offlineMessages.length<100){
          const queued=queueOfflineText(sid,text,imageIds,null,pending.nonce);
          if(queued){clearSentComposerCapture(sid,ftPre,v,imageIds);
            if(el)el.textContent='queued offline — dismisses the question, then sends after reconnection';}
          return;
        }
        if(el)el.textContent='✗ '+uploaded.error;return;
      }
      uploadIds=uploaded.uploadIds;
    }
    if(el)el.textContent='dismissing question…';
    const result=await act(sid,{type:'dismiss_then_send',nonce:pending.nonce,text,
      upload_ids:uploadIds,client_request_id:'send-'+offlineMessageId()},msgPre);
    if(!result.ok)return result;
    clearSentComposerCapture(sid,ftPre,v,imageIds);
    const optimisticId=addOptimistic(sid,text,'text',result.queued?'queued':'sending',null,null,imageIds);
    const item=optimisticList(sid).find(entry=>entry.id===optimisticId);
    if(item&&result.queued){item.outboxId=result.outbox_id;
      item.queueLabel=result.message||'Queued · waiting for session';
      item.queueReason=result.queue_reason||'Question dismissed; waiting for the session to become available';
      rememberOutboxReceipt(item);uiRefresh();}
    if(!result.queued&&imageIds.length)void deleteImages(imageIds);
    return result;
  }
  if(cmd&&['action','skill'].includes(cmd.execution)){
    if(commandSendLocks.has(sid))return;
    commandSendLocks.add(sid);if(el)el.textContent='sending command…';
    const payload=cmd.execution==='action'?{type:cmd.action}:
      {type:'skill',name:cmd.name,args:v.slice(cmd.name.length).trim()};
    const result=await act(sid,payload,msgPre);
    commandSendLocks.delete(sid);
    if(result.ok&&inp&&inp.value.trim()===v){inp.value='';resizeComposer(inp);clearDraft(composerDraftKey(sid));}
    else if(!result.ok)setDraft(composerDraftKey(sid),inp?.value||v);
    return result;
  }
  if(inp){inp.value='';resizeComposer(inp);}clearDraft(composerDraftKey(sid));setImageDraftIds(sid,[]);void renderImageDrafts(sid); // sending is the only automatic clear
  const optimisticId=addOptimistic(sid,text,'text','sending',null,null,imageIds);
  const clientRequestId='send-'+offlineMessageId();
  const directOnly=/^[\/$]/.test(v);
  if(!imageIds.length)return act(sid,{type:directOnly?'text':'send_message',text,
    client_request_id:clientRequestId},msgPre,optimisticId);
  if(el)el.textContent='uploading images…';
  const uploaded=await uploadImages(sid,imageIds);
  if(!uploaded.ok){
    if(uploaded.network_error&&offlineMessages.length<100){
      queueOfflineText(sid,text,imageIds,optimisticId);if(el)el.textContent='queued offline — sends automatically after reconnection';return;
    }
    updateOptimistic(sid,optimisticId,false,uploaded.error);if(el)el.textContent='✗ '+uploaded.error;return;
  }
  const result=await act(sid,{type:'send_message',text,upload_ids:uploaded.uploadIds,
    client_request_id:clientRequestId},msgPre,optimisticId);
  if(result.ok&&!result.queued)void deleteImages(imageIds);
}

function clearSentComposerCapture(sid,ftPre,text,imageIds=[]){
  const current=document.getElementById(ftPre+'-'+sid);
  if(current&&current.value.trim()===text){
    current.value='';resizeComposer(current);clearDraft(composerDraftKey(sid));
  }
  if(imageIds.length){
    const sent=new Set(imageIds),remaining=imageDraftIds(sid).filter(id=>!sent.has(id));
    setImageDraftIds(sid,remaining);void renderImageDrafts(sid);
  }
}

globalThis.offlineFlushBusy=false;
const commandSendLocks=new Set();
function queueOfflineText(sid,text,imageIds=[],existingOptimisticId=null,dismissNonce=null){
  if(offlineMessages.length>=100)return null;
  const messages=(ctxCache[sid]&&ctxCache[sid].messages)||[];
  const entry={id:offlineMessageId(),sid,text:String(text),imageIds:[...(imageIds||[])],created:Date.now(),state:'queued',
    dismissNonce:typeof dismissNonce==='string'?dismissNonce:null,
    baseCount:canonicalCount(messages,{kind:'text',text})};
  offlineMessages.push(entry);persistOfflineMessages();
  const existing=existingOptimisticId!=null;
  // Do not call optimisticList before linking an existing row: that function
  // materializes every unlinked queue record and would create a duplicate.
  let item=existingOptimisticId==null?null:optimisticBucket(sid).find(candidate=>candidate.id===existingOptimisticId);
  if(item){clearTimeout(item.confirmTimer);item.queueId=entry.id;item.status='queued';
    item.queueLabel='Queued offline';item.queueReason=entry.dismissNonce?
      'Dismisses the open question, then sends after reconnection':'Sends automatically after reconnection';delete item.error;}
  else{optimisticList(sid);item=optimisticList(sid).find(candidate=>candidate.queueId===entry.id);}
  if(item){
    const openConvo=sessionView?.sid===sid&&!sessionView.closed&&$('#sbody .aconvo');
    if(openConvo){
      const current=existing?openConvo.querySelector(`[data-optimistic-id="${item.id}"]`):null;
      if(current)current.outerHTML=optimisticItemHtml(item);
      else openConvo.insertAdjacentHTML('beforeend',optimisticItemHtml(item));
      $('#sbody').scrollTop=$('#sbody').scrollHeight;}
  }
  uiRefresh();
  return entry;
}
function removeOfflineMessage(id){
  const length=offlineMessages.length;
  offlineMessages=offlineMessages.filter(item=>item.id!==id);
  if(offlineMessages.length!==length)persistOfflineMessages();
}
async function flushOfflineMessages(){
  if(fleetOffline||offlineFlushBusy||!offlineMessages.length)return;
  offlineFlushBusy=true;
  try{
    while(!fleetOffline&&offlineMessages.length){
      const queued=offlineMessages[0];
      if(queued.state==='confirmation_unknown')break;
      const item=optimisticList(queued.sid).find(candidate=>candidate.queueId===queued.id);
      if(!item){removeOfflineMessage(queued.id);continue;}
      item.baseCount=canonicalCount((ctxCache[queued.sid]?.messages)||[],item);
      item.status='sending';delete item.error;armOptimisticTimeout(item);uiRefresh();
      let payload={type:queued.dismissNonce?'dismiss_then_send':'send_message',text:queued.text,
        client_request_id:'offline-'+queued.id};
      if(queued.dismissNonce)payload.nonce=queued.dismissNonce;
      if(queued.imageIds?.length){
        const uploaded=await uploadImages(queued.sid,queued.imageIds);
        if(uploaded.network_error){clearTimeout(item.confirmTimer);item.status='queued';delete item.error;uiRefresh();break;}
        if(!uploaded.ok){removeOfflineMessage(queued.id);clearTimeout(item.confirmTimer);item.status='failed';item.error=uploaded.error;uiRefresh();continue;}
        payload={type:queued.dismissNonce?'dismiss_then_send':'send_message',text:queued.text,
          nonce:queued.dismissNonce||undefined,upload_ids:uploaded.uploadIds,
          client_request_id:'offline-'+queued.id};
      }
      const result=await act(queued.sid,payload,false,item.id);
      if(result.offline){
        clearTimeout(item.confirmTimer);item.status='queued';delete item.error;uiRefresh();break;
      }
      if(result.network_error){
        clearTimeout(item.confirmTimer);item.status='failed';
        item.error='Delivery unconfirmed — the connection dropped while sending. Fleet did not retry; check the session before dismissing this receipt.';
        queued.state='confirmation_unknown';queued.error=item.error;persistOfflineMessages();
        uiRefresh();break;
      }
      removeOfflineMessage(queued.id);
      if(!result.ok){
        clearTimeout(item.confirmTimer);item.status='failed';item.error=result.error||'Send rejected';uiRefresh();
      }
      if(result.ok&&!result.queued&&queued.imageIds?.length)void deleteImages(queued.imageIds);
    }
  }finally{offlineFlushBusy=false;}
}

// ---- slash-command autocomplete -------------------------------------------
// Menu INSERTS (never sends): most skills take args, and it keeps the send path
// — with its destructive-command confirm — as the single way anything fires.
const cmdCache={};          // sessionId -> [{name,desc,scope,danger}]
const cmdLoads={},cmdErrors={};
globalThis.slashBox=null;          // id of the open menu's container, or null
function slashClose(){
  if(!slashBox)return;
  const el=document.getElementById(slashBox);
  if(el){const root=el.closest('.session-composer');el.innerHTML='';syncComposerToolsOpen(root);}
  slashBox=null;
}
async function slashInput(sid,pre){
  const inp=document.getElementById(pre+'-'+sid);
  const v=(inp&&inp.value)||'';
  // menu lives while the text is a single command/skill token; a space starts args
  if((!v.startsWith('/')&&!v.startsWith('$'))||/\s/.test(v))return slashClose();
  const box=document.getElementById('slash-'+pre+'-'+sid);if(!box)return;
  slashBox='slash-'+pre+'-'+sid;
  if(!cmdCache[sid]){
    if(!cmdLoads[sid]){
      box.innerHTML='<div class="slashmenu"><div class="slashempty"><span class="delivery sending" aria-hidden="true">◌</span> loading commands…</div></div>';
      syncComposerToolsOpen(box.closest('.session-composer'));
      cmdLoads[sid]=(async()=>{try{
        const r=await fetch('/api/commands?sid='+encodeURIComponent(sid),{cache:'no-store'});
        const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'commands unavailable');
        cmdCache[sid]=d.commands||[];delete cmdErrors[sid];
      }catch(e){cmdErrors[sid]=String(e.message||e);}
      finally{delete cmdLoads[sid];}})();
    }
    await cmdLoads[sid];
    const current=document.getElementById(pre+'-'+sid);
    if(!current||current.value!==v)return;
  }
  if(cmdErrors[sid]){box.innerHTML=`<div class="slashmenu"><div class="slashempty">${esc(cmdErrors[sid])} <button onclick="retryCommands('${sid}','${pre}')">retry</button></div></div>`;return;}
  const q=v.slice(1).toLowerCase();
  const hits=cmdCache[sid].filter(c=>c.name[0]===v[0]&&c.name.slice(1).toLowerCase().includes(q))
    .sort((a,b)=>(a.name.slice(1).toLowerCase().startsWith(q)?0:1)-(b.name.slice(1).toLowerCase().startsWith(q)?0:1))
    .slice(0,40);
  box.innerHTML=hits.length?`<div class="slashmenu">${hits.map(c=>`
    <button class="slashrow" onmousedown="event.preventDefault()" onclick="slashPick('${sid}','${pre}','${enc(c.name)}')">
      <span class="scmd">${esc(c.name)}${c.danger?' <span class="sdanger">destructive</span>':''}</span>
      <span class="sdesc">${esc(c.desc||'')}</span>
      <span class="sscope">${esc(c.scope)}</span>
    </button>`).join('')}</div>`
    :`<div class="slashmenu"><div class="slashempty">no command matches “${esc(v)}”</div></div>`;
  syncComposerToolsOpen(box.closest('.session-composer'));
}
function retryCommands(sid,pre){delete cmdCache[sid];delete cmdErrors[sid];slashInput(sid,pre);}
function slashPick(sid,pre,name){
  const inp=document.getElementById(pre+'-'+sid);
  if(!inp)return;
  inp.value=decodeURIComponent(name)+' ';   // trailing space: args go right here
  setDraft(composerDraftKey(sid),inp.value);
  slashClose();
  inp.focus();
}

Object.assign(globalThis,{RECEIPT_ACT_TYPES,SETTINGS_SECTIONS,SETTINGS_LABELS,policyRuleOpen,policyApplyCurrent,policySaving,settingQueues,settingIntents,closePreviewCache,commandSendLocks,cmdCache,cmdLoads,cmdErrors});
