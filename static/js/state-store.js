// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{persistWorkspaceSplits,draftValue,persistDrafts,setDraft,clearDraft,clearDraftPrefix,persistOfflineMessages,offlineMessageId,persistOutboxReceipts,rememberOutboxReceipt,forgetOutboxReceipt,persistResolvedOutboxReceipts,resolveOutboxReceipt,savedConversation,persistConversation,persistImageDrafts,setImageDraftIds,restoreImageDraftIds,imageDb,imageStoreRequest,deleteImages,pruneImages});
const $=q=>document.querySelector(q);
const DRAFT_STORE_KEY='fleet.drafts.v1';
const OFFLINE_MESSAGE_STORE_KEY='fleet.offlineMessages.v1';
const OUTBOX_RECEIPT_STORE_KEY='fleet.outboxReceipts.v1';
const OUTBOX_RESOLVED_STORE_KEY='fleet.outboxResolved.v1';
const CONTEXT_STORE_KEY='fleet.contextCache.v1';
const IMAGE_DRAFT_STORE_KEY='fleet.imageDrafts.v1';
const QUESTION_PANEL_STORE_KEY='fleet.questionPanels.v1';
const WORKSPACE_SPLIT_STORE_KEY='fleet.workspaceSplits.v1';
const IMAGE_DB_NAME='fleet-images-v1',IMAGE_STORE='images';
const IMAGE_MAX_BYTES=10*1024*1024,IMAGE_MAX_COUNT=4,IMAGE_TTL_MS=24*60*60*1000;
globalThis.draftStore=(()=>{try{
  const value=JSON.parse(localStorage.getItem(DRAFT_STORE_KEY)||'{}');
  return value&&typeof value==='object'&&!Array.isArray(value)?value:{};
}catch(_){return {};}})();
globalThis.workspaceSplitWidths=(()=>{try{
  const value=JSON.parse(localStorage.getItem(WORKSPACE_SPLIT_STORE_KEY)||'{}');
  return Object.fromEntries(['files','subagents'].map(kind=>[kind,
    Math.max(220,Math.min(520,Number(value?.[kind])||300))]));
}catch(_){return{files:300,subagents:300};}})();
function persistWorkspaceSplits(){try{
  localStorage.setItem(WORKSPACE_SPLIT_STORE_KEY,JSON.stringify(workspaceSplitWidths));
}catch(error){console.warn('Fleet could not persist workspace divider widths',error);}}
function draftValue(key,fallback=''){
  return Object.prototype.hasOwnProperty.call(draftStore,key)?String(draftStore[key]):String(fallback??'');
}
function persistDrafts(){
  try{localStorage.setItem(DRAFT_STORE_KEY,JSON.stringify(draftStore));}catch(error){
    console.warn('Fleet could not persist drafts',error);
  }
}
function setDraft(key,value){
  if(!key)return;
  const text=String(value??'').slice(0,30000);
  if(text)draftStore[key]=text;else delete draftStore[key];
  const keys=Object.keys(draftStore);for(const old of keys.slice(0,Math.max(0,keys.length-200)))delete draftStore[old];
  persistDrafts();
}
function clearDraft(...keys){let changed=false;for(const key of keys){
  if(key&&Object.prototype.hasOwnProperty.call(draftStore,key)){delete draftStore[key];changed=true;}
}if(changed)persistDrafts();}
const composerDraftKey=sid=>`composer:${sid}`;
const relayDraftKey=(sid,aid)=>`relay:${sid}:${aid}`;
const questionDraftPrefix=(sid,nonce)=>`request:${sid}:${nonce}:`;
function clearDraftPrefix(prefix){
  const keys=Object.keys(draftStore).filter(key=>key.startsWith(prefix));
  clearDraft(...keys);
}
globalThis.offlineMessages=(()=>{try{
  const value=JSON.parse(localStorage.getItem(OFFLINE_MESSAGE_STORE_KEY)||'[]');
  return(Array.isArray(value)?value:[]).filter(item=>item&&
    /^[A-Za-z0-9_-]{1,100}$/.test(String(item.id||''))&&
    typeof item.sid==='string'&&item.sid.length>0&&item.sid.length<=200&&
    typeof item.text==='string'&&item.text.trim()&&item.text.length<=30000&&
    Number.isFinite(Number(item.created))).slice(-100).map(item=>({
      id:String(item.id),sid:item.sid,text:item.text,created:Number(item.created),
      imageIds:(Array.isArray(item.imageIds)?item.imageIds:[]).filter(id=>
        /^[A-Za-z0-9_-]{1,100}$/.test(String(id))).slice(0,IMAGE_MAX_COUNT).map(String),
      baseCount:Math.max(0,Number(item.baseCount)||0),
      dismissNonce:typeof item.dismissNonce==='string'&&item.dismissNonce.length<=500?
        item.dismissNonce:null,
      state:item.state==='confirmation_unknown'?'confirmation_unknown':'queued',
      error:typeof item.error==='string'?item.error.slice(0,500):''}));
}catch(_){return [];}})();
function persistOfflineMessages(){
  try{
    if(offlineMessages.length)localStorage.setItem(OFFLINE_MESSAGE_STORE_KEY,JSON.stringify(offlineMessages));
    else localStorage.removeItem(OFFLINE_MESSAGE_STORE_KEY);
  }catch(error){console.warn('Fleet could not persist the offline message queue',error);}
}
function offlineMessageId(){
  if(crypto?.randomUUID)return crypto.randomUUID().replace(/-/g,'');
  return`${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`.slice(0,100);
}
globalThis.outboxReceipts=(()=>{try{
  const value=JSON.parse(localStorage.getItem(OUTBOX_RECEIPT_STORE_KEY)||'{}');
  if(!value||typeof value!=='object'||Array.isArray(value))return{};
  return Object.fromEntries(Object.entries(value).filter(([id,item])=>
    /^[A-Za-z0-9_.:-]{1,200}$/.test(id)&&item&&typeof item.sid==='string'&&
    typeof item.text==='string').slice(-100).map(([id,item])=>[id,{
      outboxId:id,sid:item.sid.slice(0,200),text:item.text.slice(0,30000),kind:'text',
      imageIds:(Array.isArray(item.imageIds)?item.imageIds:[]).filter(value=>
        /^[A-Za-z0-9_-]{1,100}$/.test(String(value))).slice(0,IMAGE_MAX_COUNT).map(String),
      imageCount:Math.max(0,Number(item.imageCount)||0),baseCount:Math.max(0,Number(item.baseCount)||0),
      created:Math.max(0,Number(item.created)||Date.now()),status:['queued','confirmed','failed'].includes(item.status)?item.status:'queued',
      queueLabel:typeof item.queueLabel==='string'?item.queueLabel.slice(0,200):'',
      queueReason:typeof item.queueReason==='string'?item.queueReason.slice(0,500):'',
      error:typeof item.error==='string'?item.error.slice(0,500):''}]))
}catch(_){return{}}})();
function persistOutboxReceipts(){try{
  const rows=Object.values(outboxReceipts).sort((a,b)=>a.created-b.created).slice(-100);
  outboxReceipts=Object.fromEntries(rows.map(item=>[item.outboxId,item]));
  if(rows.length)localStorage.setItem(OUTBOX_RECEIPT_STORE_KEY,JSON.stringify(outboxReceipts));
  else localStorage.removeItem(OUTBOX_RECEIPT_STORE_KEY);
}catch(error){console.warn('Fleet could not persist queued-send receipts',error);}}
function rememberOutboxReceipt(item){
  if(!item?.outboxId)return;
  outboxReceipts[item.outboxId]={outboxId:item.outboxId,sid:item.sid,text:item.text,kind:'text',
    imageIds:[...(item.imageIds||[])],imageCount:item.imageCount||0,baseCount:item.baseCount||0,
    created:item.created||Date.now(),status:item.status||'queued',queueLabel:item.queueLabel||'',
    queueReason:item.queueReason||'',error:item.error||''};
  persistOutboxReceipts();
}
function forgetOutboxReceipt(id){if(id&&outboxReceipts[id]){delete outboxReceipts[id];persistOutboxReceipts();}}
globalThis.resolvedOutboxReceipts=(()=>{try{
  const value=JSON.parse(localStorage.getItem(OUTBOX_RESOLVED_STORE_KEY)||'{}');
  if(!value||typeof value!=='object'||Array.isArray(value))return{};
  const cutoff=Date.now()-30*24*60*60*1000;
  return Object.fromEntries(Object.entries(value).filter(([id,at])=>
    /^[A-Za-z0-9_.:-]{1,200}$/.test(id)&&Number(at)>=cutoff).slice(-200));
}catch(_){return{};}})();
function persistResolvedOutboxReceipts(){try{
  const cutoff=Date.now()-30*24*60*60*1000;
  const rows=Object.entries(resolvedOutboxReceipts).filter(([,at])=>Number(at)>=cutoff)
    .sort((a,b)=>Number(a[1])-Number(b[1])).slice(-200);
  resolvedOutboxReceipts=Object.fromEntries(rows);
  if(rows.length)localStorage.setItem(OUTBOX_RESOLVED_STORE_KEY,JSON.stringify(resolvedOutboxReceipts));
  else localStorage.removeItem(OUTBOX_RESOLVED_STORE_KEY);
}catch(error){console.warn('Fleet could not persist resolved delivery receipts',error);}}
function resolveOutboxReceipt(id){if(!id)return;
  resolvedOutboxReceipts[id]=Date.now();forgetOutboxReceipt(id);persistResolvedOutboxReceipts();}
globalThis.persistedContexts=(()=>{try{
  const value=JSON.parse(localStorage.getItem(CONTEXT_STORE_KEY)||'{}');
  return value&&typeof value==='object'&&!Array.isArray(value)?value:{};
}catch(_){return{}}})();
const contextStoreKey=(scope,sid,aid='')=>`${scope}:${sid}:${aid}`;
function savedConversation(scope,sid,aid=''){
  const saved=persistedContexts[contextStoreKey(scope,sid,aid)];
  if(!saved||!Array.isArray(saved.messages))return null;
  return{...saved,messages:[...saved.messages],files:[...(saved.files||[])],agents:[...(saved.agents||[])],
    info:{...(saved.info||{})},stale:true};
}
function persistConversation(scope,sid,aid,cache){
  if(!cache||!Array.isArray(cache.messages))return;
  const key=contextStoreKey(scope,sid,aid),entry={v:cache.v,messages:cache.messages,
    files:cache.files||[],agents:cache.agents||[],closed:Boolean(cache.closed),
    info:cache.info||{},next_cursor:cache.next_cursor,
    message_total:cache.message_total,saved:Date.now()};
  persistedContexts[key]=entry;
  let rows=Object.entries(persistedContexts).sort((a,b)=>(b[1].saved||0)-(a[1].saved||0)).slice(0,18);
  while(rows.length){
    const value=Object.fromEntries(rows);
    try{const encoded=JSON.stringify(value);if(encoded.length<=2800000){
      localStorage.setItem(CONTEXT_STORE_KEY,encoded);persistedContexts=value;return;
    }}catch(_){}
    rows.pop();
  }
  persistedContexts={};try{localStorage.removeItem(CONTEXT_STORE_KEY);}catch(_){}
}
globalThis.imageDrafts=(()=>{try{
  const value=JSON.parse(localStorage.getItem(IMAGE_DRAFT_STORE_KEY)||'{}');
  if(!value||typeof value!=='object'||Array.isArray(value))return{};
  return Object.fromEntries(Object.entries(value).slice(-200).map(([sid,ids])=>[
    String(sid),Array.isArray(ids)?ids.filter(id=>/^[A-Za-z0-9_-]{1,100}$/.test(String(id)))
      .slice(0,IMAGE_MAX_COUNT).map(String):[]]));
}catch(_){return {};}})();
function persistImageDrafts(){try{
  const clean=Object.fromEntries(Object.entries(imageDrafts).filter(([,ids])=>ids.length));
  imageDrafts=clean;
  if(Object.keys(clean).length)localStorage.setItem(IMAGE_DRAFT_STORE_KEY,JSON.stringify(clean));
  else localStorage.removeItem(IMAGE_DRAFT_STORE_KEY);
}catch(error){console.warn('Fleet could not persist image draft references',error);}}
const imageDraftIds=sid=>[...(imageDrafts[String(sid)]||[])];
function setImageDraftIds(sid,ids){
  const clean=[...new Set((ids||[]).map(String).filter(id=>/^[A-Za-z0-9_-]{1,100}$/.test(id)))]
    .slice(0,IMAGE_MAX_COUNT);
  if(clean.length)imageDrafts[String(sid)]=clean;else delete imageDrafts[String(sid)];
  persistImageDrafts();
}
function restoreImageDraftIds(sid,ids){setImageDraftIds(sid,[...imageDraftIds(sid),...(ids||[])]);}
globalThis.imageDbPromise=null;
function imageDb(){
  if(!('indexedDB' in window))return Promise.reject(new Error('This browser cannot persist image drafts'));
  if(imageDbPromise)return imageDbPromise;
  imageDbPromise=new Promise((resolve,reject)=>{
    const request=indexedDB.open(IMAGE_DB_NAME,1);
    request.onupgradeneeded=()=>request.result.createObjectStore(IMAGE_STORE,{keyPath:'id'});
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
  });
  return imageDbPromise;
}
async function imageStoreRequest(mode,operation){
  const db=await imageDb();
  return new Promise((resolve,reject)=>{const tx=db.transaction(IMAGE_STORE,mode),store=tx.objectStore(IMAGE_STORE);
    let request,result;try{request=operation(store);}catch(error){reject(error);return;}
    request.onsuccess=()=>{result=request.result;};request.onerror=()=>reject(request.error);
    tx.oncomplete=()=>resolve(result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
  });
}
const putImage=record=>imageStoreRequest('readwrite',store=>store.put(record));
const getImage=id=>imageStoreRequest('readonly',store=>store.get(String(id)));
const deleteImage=id=>imageStoreRequest('readwrite',store=>store.delete(String(id)));
async function deleteImages(ids){await Promise.allSettled((ids||[]).map(deleteImage));}
async function pruneImages(){
  let records=[];try{records=await imageStoreRequest('readonly',store=>store.getAll());}catch(_){return;}
  const cutoff=Date.now()-IMAGE_TTL_MS;
  await deleteImages(records.filter(record=>Number(record.created||0)<cutoff).map(record=>record.id));
}
document.addEventListener('input',event=>{
  const input=event.target,key=input?.dataset?.draftKey;
  if(key&&input.type!=='password')setDraft(key,input.value);
},true);

Object.assign(globalThis,{$,DRAFT_STORE_KEY,OFFLINE_MESSAGE_STORE_KEY,OUTBOX_RECEIPT_STORE_KEY,OUTBOX_RESOLVED_STORE_KEY,CONTEXT_STORE_KEY,IMAGE_DRAFT_STORE_KEY,QUESTION_PANEL_STORE_KEY,WORKSPACE_SPLIT_STORE_KEY,IMAGE_DB_NAME,IMAGE_STORE,IMAGE_MAX_BYTES,IMAGE_MAX_COUNT,IMAGE_TTL_MS,composerDraftKey,relayDraftKey,questionDraftPrefix,contextStoreKey,imageDraftIds,putImage,getImage,deleteImage});
