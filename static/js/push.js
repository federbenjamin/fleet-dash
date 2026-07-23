// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{pushSupported,pushB64,pushApi,registerFleetServiceWorker,loadPushState,currentPushSubscription,savePushSubscription,repairPushSubscription,enableFleetPush,installFleet,renamePushDevice,setPushDeviceEnabled,disconnectFleetPush,testFleetPush,replaceListedPushDevice,listedPushWorking,updateListedPushDevice,testListedPushDevice,confirmForgetPushDevice,forgetPushDevice,pushSettingsHtml,initFleetPwa});
const BRIEF_DEVICE_KEY='fleet.briefingDevice.v1';
const briefingDevice=(()=>{let value=localStorage.getItem(BRIEF_DEVICE_KEY);
  if(!value){value=(crypto.randomUUID?crypto.randomUUID():`device-${Date.now()}-${Math.random().toString(16).slice(2)}`);
    localStorage.setItem(BRIEF_DEVICE_KEY,value);}return value;})();
globalThis.pushInstallPrompt=null;globalThis.pushBusy=false;globalThis.pushLoadedAt=0;
globalThis.pushData={ok:true,configured:false,delivery:'not_configured',current_device:null};;
globalThis.pushLocal={secure:window.isSecureContext,serviceWorker:'serviceWorker' in navigator,
  notifications:'Notification' in window,push:'PushManager' in window,
  permission:'Notification' in window?Notification.permission:'unsupported',registered:false,
  error:'',notice:''};;
const pushStandalone=()=>matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
const pushPlatform=()=>navigator.userAgentData?.platform||navigator.platform||'browser';
const pushDeviceName=()=>pushData.current_device?.display_name||`Fleet on ${pushPlatform()}`;
function pushSupported(){return pushLocal.secure&&pushLocal.serviceWorker&&pushLocal.notifications&&pushLocal.push;}
function pushB64(value){
  const padding='='.repeat((4-value.length%4)%4),raw=atob((value+padding).replace(/-/g,'+').replace(/_/g,'/'));
  return Uint8Array.from([...raw].map(char=>char.charCodeAt(0)));
}
async function pushApi(path,body){
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}),data=await response.json();
  if(!response.ok||!data.ok)throw new Error(data.error||'Push setup failed');return data;
}
async function registerFleetServiceWorker(){
  if(!pushLocal.secure||!pushLocal.serviceWorker)return null;
  try{
    const registration=await navigator.serviceWorker.register('/sw.js',{scope:'/'});
    pushLocal.registered=true;return registration;
  }catch(error){pushLocal.error=`App service unavailable: ${String(error.message||error)}`;return null;}
}
async function loadPushState(force=false){
  if(!force&&Date.now()-pushLoadedAt<4000)return pushData;
  pushLocal.permission=pushLocal.notifications?Notification.permission:'unsupported';
  try{
    const response=await fetch(`/api/push/config?device=${encodeURIComponent(briefingDevice)}`,{cache:'no-store'});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(response.status===403?
      'Open Fleet once with its action-token URL on this device':(data.error||'Push status unavailable'));
    pushData=data;pushLocal.error='';pushLoadedAt=Date.now();
  }catch(error){pushLocal.error=String(error.message||error);}
  if(settingsOpen&&['notifications','devices'].includes(settingsSection))renderSettings();return pushData;
}
async function currentPushSubscription(){
  const registration=await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}
async function savePushSubscription(subscription){
  const data=await pushApi('/api/push/subscription',{device_id:briefingDevice,
    display_name:pushDeviceName(),platform:pushPlatform(),permission_state:'granted',
    subscription:subscription.toJSON()});
  pushData.current_device=data.device;return data.device;
}
async function repairPushSubscription({interactive=false}={}){
  if(!pushSupported()||!pushData.configured||Notification.permission!=='granted')return false;
  let subscription=await currentPushSubscription();
  if(!subscription&&(interactive||pushData.current_device)){
    subscription=await navigator.serviceWorker.ready.then(registration=>registration.pushManager.subscribe({
      userVisibleOnly:true,applicationServerKey:pushB64(pushData.public_key)}));
  }
  if(!subscription)return false;
  await savePushSubscription(subscription);return true;
}
async function enableFleetPush(){
  if(pushBusy)return;const started=performance.now();pushBusy=true;pushLocal.error='';renderSettings();
  try{
    if(!pushLocal.secure)throw new Error('Open Fleet from its HTTPS tailnet URL');
    if(!pushSupported())throw new Error('This browser does not support Web Push');
    if(!pushData.configured)throw new Error('Fleet Web Push delivery is not configured yet');
    if(/iPhone|iPad|iPod/.test(navigator.userAgent)&&!pushStandalone())
      throw new Error('Add Fleet to the Home Screen, then enable notifications from the installed app');
    const registration=await registerFleetServiceWorker();
    if(!registration)throw new Error(pushLocal.error||'Fleet app service is unavailable');
    const permission=await Notification.requestPermission();pushLocal.permission=permission;
    if(permission!=='granted')throw new Error(permission==='denied'?
      'Notifications are blocked in browser settings':'Notification permission was not granted');
    await repairPushSubscription({interactive:true});await loadPushState(true);
  }catch(error){pushLocal.error=String(error.message||error);}
  finally{pushBusy=false;recordInputFeedback(started,'push_enable');renderSettings();}
}
async function installFleet(){
  if(!pushInstallPrompt)return;const prompt=pushInstallPrompt;pushInstallPrompt=null;
  await prompt.prompt();await prompt.userChoice.catch(()=>{});renderSettings();
}
async function renamePushDevice(value){
  const name=String(value||'').trim();if(!name||!pushData.current_device)return;
  pushBusy=true;renderSettings();try{
    const data=await pushApi('/api/push/device-settings',{device_id:briefingDevice,display_name:name});
    pushData.current_device=data.device;
  }catch(error){pushLocal.error=String(error.message||error);}
  finally{pushBusy=false;renderSettings();}
}
async function setPushDeviceEnabled(enabled){
  if(pushBusy||!pushData.current_device)return;pushBusy=true;renderSettings();try{
    const data=await pushApi('/api/push/device-settings',{device_id:briefingDevice,enabled});
    pushData.current_device=data.device;
  }catch(error){pushLocal.error=String(error.message||error);}
  finally{pushBusy=false;renderSettings();}
}
async function disconnectFleetPush(){
  if(pushBusy||!pushData.current_device)return;pushBusy=true;renderSettings();try{
    const subscription=pushSupported()?await currentPushSubscription():null;
    const data=await pushApi('/api/push/subscription',{device_id:briefingDevice,remove:true,
      permission_state:pushLocal.permission==='denied'?'denied':'expired'});
    pushData.current_device=data.device;
    if(subscription){try{await subscription.unsubscribe();}catch(_){
      pushLocal.error='Fleet delivery is disconnected; browser subscription cleanup will retry later';
    }}
  }catch(error){pushLocal.error=String(error.message||error);}
  finally{pushBusy=false;renderSettings();}
}
async function testFleetPush(){
  if(pushBusy||!pushData.current_device)return;pushBusy=true;pushLocal.error='';pushLocal.notice='';renderSettings();try{
    await pushApi('/api/push/test',{device_id:briefingDevice});
    pushLocal.notice='Test queued · delivery runs in the background';
    setTimeout(()=>loadPushState(true),750);
  }catch(error){pushLocal.error=String(error.message||error);}
  finally{pushBusy=false;renderSettings();}
}
const listedPushBusy=new Set();
function replaceListedPushDevice(device){
  if(!device?.id)return;
  const devices=[...(pushData.devices||[])],index=devices.findIndex(item=>item.id===device.id);
  if(index>=0)devices[index]=device;else devices.push(device);
  pushData.devices=devices;
  if(pushData.current_device?.id===device.id)pushData.current_device=device;
  pushData.enabled_devices=devices.filter(item=>item.enabled===true).length;
}
function listedPushWorking(deviceId,message){
  const card=document.querySelector(`[data-push-device="${CSS.escape(deviceId)}"]`);
  if(!card)return;
  card.querySelectorAll('button,input').forEach(control=>{control.disabled=true;});
  const status=card.querySelector('.devicefeedback');if(status)status.textContent=message;
}
async function updateListedPushDevice(deviceId,patch){
  if(listedPushBusy.has(deviceId))return;
  listedPushBusy.add(deviceId);listedPushWorking(deviceId,'saving…');
  try{const data=await pushApi('/api/push/device-settings',{device_id:deviceId,...patch});
    replaceListedPushDevice(data.device);renderSettings();
    const status=document.querySelector(`[data-push-device="${CSS.escape(deviceId)}"] .devicefeedback`);
    if(status)status.textContent='saved ✓';
  }catch(error){pushLocal.error=String(error.message||error);renderSettings();}
  finally{listedPushBusy.delete(deviceId);}
}
async function testListedPushDevice(deviceId){
  if(listedPushBusy.has(deviceId))return;
  listedPushBusy.add(deviceId);listedPushWorking(deviceId,'queuing test…');
  try{await pushApi('/api/push/test',{device_id:deviceId});await loadPushState(true);
    const status=document.querySelector(`[data-push-device="${CSS.escape(deviceId)}"] .devicefeedback`);
    if(status)status.textContent='test queued ✓';
  }catch(error){pushLocal.error=String(error.message||error);renderSettings();}
  finally{listedPushBusy.delete(deviceId);}
}
function confirmForgetPushDevice(deviceId,name){
  askConfirm('Remove notification device',
    `Fleet will revoke <b>${esc(name||'this device')}</b> and suppress its queued deliveries. The browser must reconnect before it can receive another push.`,
    'remove device',()=>forgetPushDevice(deviceId),true);
}
async function forgetPushDevice(deviceId){
  if(listedPushBusy.has(deviceId))return;
  listedPushBusy.add(deviceId);listedPushWorking(deviceId,'removing…');
  try{await pushApi('/api/push/subscription',{device_id:deviceId,forget:true});
    pushData.devices=(pushData.devices||[]).filter(item=>item.id!==deviceId);
    pushData.enabled_devices=pushData.devices.filter(item=>item.enabled===true).length;
    renderSettings();
  }catch(error){pushLocal.error=String(error.message||error);renderSettings();}
  finally{listedPushBusy.delete(deviceId);}
}
function pushSettingsHtml(){
  const device=pushData.current_device,standalone=pushStandalone(),supported=pushSupported();
  const installState=standalone?'Installed':pushInstallPrompt?'Ready to install':'Browser tab';
  const permission=pushLocal.permission==='granted'?'Allowed':pushLocal.permission==='denied'?'Blocked':
    pushLocal.permission==='prompt'?'Not requested':'Unsupported';
  const delivery=!pushData.configured?'Server setup pending':pushData.delivery!=='ready'?
    `Worker ${String(pushData.delivery||'unavailable').replaceAll('_',' ')}`:!device?'Not connected':
    device.health==='healthy'?'Healthy':device.health==='registered'?'Registered · awaiting test':
    device.health==='disabled'?'Paused':String(device.health||'Unavailable').replaceAll('_',' ');
  const help=/iPhone|iPad|iPod/.test(navigator.userAgent)&&!standalone?
    'On iPhone or iPad: Share → Add to Home Screen. Open the installed Fleet app, then enable notifications.':
    !pushLocal.secure?'Mobile Web Push requires Fleet’s HTTPS tailnet URL. Localhost remains valid on this Mac.':
    !supported?'This browser does not expose the Service Worker, Notifications, and Push APIs together.':
    !pushData.configured?'Fleet is creating its private delivery keys. This page will update automatically.':
    pushData.delivery!=='ready'?'The delivery helper is unavailable. Persisted jobs wait and retry without delaying Fleet.':
    'Fleet sends only a minimal summary. Open Fleet for session details.';
  return`<section class="pushsetup"><div class="pushsetuphead"><span><b>Fleet app & Web Push</b><small>One installed app · per-device delivery</small></span>
    <i class="pushsignal ${esc(device?.health||(!pushData.configured?'pending':'off'))}"></i></div>
    <div class="pushrail"><span><i></i><b>App</b><small>${esc(installState)}</small></span>
      <span><i></i><b>Permission</b><small>${esc(permission)}</small></span>
      <span><i></i><b>Delivery</b><small>${esc(delivery)}</small></span></div>
    <p class="pushhelp">${esc(help)}</p>
    <div class="pushactions">
      ${pushInstallPrompt&&!standalone?'<button onclick="installFleet()">Install Fleet</button>':''}
      <button class="primary" onclick="enableFleetPush()" ${pushBusy||!supported||!pushData.configured?'disabled':''}>${pushBusy?'<span class="delivery sending">◌</span> Working…':device?'Repair subscription':'Enable notifications'}</button>
      <button onclick="testFleetPush()" ${pushBusy||!device||pushData.delivery!=='ready'?'disabled':''}>Send test</button>
    </div>
    ${device?`<div class="pushdevice"><label><span>This device</span><input value="${esc(device.display_name||'')}" maxlength="80" onchange="renamePushDevice(this.value)"></label>
      <label class="pushswitch"><input type="checkbox" ${device.enabled?'checked':''} ${device.permission_state!=='granted'?'disabled':''} onchange="setPushDeviceEnabled(this.checked)"><span>Delivery enabled</span></label>
      <button onclick="disconnectFleetPush()" ${pushBusy?'disabled':''}>Disconnect</button></div>`:''}
    ${pushLocal.error?`<div class="pusherror" role="alert">${esc(pushLocal.error)}</div>`:''}
    ${pushLocal.notice?`<div class="pushnotice" role="status">${esc(pushLocal.notice)}</div>`:''}
    <div class="sethint">Subscription endpoints and encryption keys are write-only. Fleet’s device list exposes only names, state, and health.</div></section>`;
}
async function initFleetPwa(){
  await registerFleetServiceWorker();await loadPushState(true);
  if(pushSupported()&&Notification.permission==='granted'&&pushData.current_device){
    try{await repairPushSubscription();await loadPushState(true);}catch(error){pushLocal.error=String(error.message||error);}
  }
  if(settingsOpen&&settingsSection==='devices')renderSettings();
}
window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();pushInstallPrompt=event;if(settingsOpen&&settingsSection==='devices')renderSettings();});
window.addEventListener('appinstalled',()=>{pushInstallPrompt=null;if(settingsOpen&&settingsSection==='devices')renderSettings();});
navigator.serviceWorker?.addEventListener('message',event=>{if(event.data?.type==='push-subscription-change')repairPushSubscription().catch(()=>{});});
window.__fleetPush={loadPushState,enableFleetPush,repairPushSubscription};

Object.assign(globalThis,{BRIEF_DEVICE_KEY,briefingDevice,pushStandalone,pushPlatform,pushDeviceName,listedPushBusy});
