// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{terminalButton,singleQBlock,fileStrip,viewerSurfaceBar,latestFileButton,sessionSurfaceBar,keepStripScroll,keepSessionActionScroll,renderViewerBar,sessTitleBlock,viewerFormat,sandboxedHtmlDocument,showViewerFrame,showJsonDocument,viewFile,seedTargetedWorkspaceHistory,closeViewer,syncOverlayHistory,dismissOverlay,modalOpener,resolveModalOpener,modalVisible,modalStack,modalFocusable,syncModalStack,syncVisualViewport,statusGraphPoint,statusLineHtml,statusCostToggle,toggleStatusDetails,refreshStatusStrip});
const terminalActions=new Map();
function terminalButton(s,card=false){
  if(!s)return'';
  const cls=`expandbtn termbtn${card?' deskonly':''}`;
  if(s.provider==='codex'){
    if(!s.capabilities?.focus_terminal||s.capabilities?.focus_terminal_mode!=='focus')return'';
    const action=terminalActions.get(s.session_id)||{};
    return`<button class="${cls}" title="bring this session's terminal tab to the front"
      ${action.busy?'disabled':''} onclick="event.stopPropagation();focusSession('${s.session_id}',this)">${action.busy?'Opening…':action.ok?'Opened ✓':'Open'}</button>`;
  }
  if(s.capabilities?.focus_terminal){
    const action=terminalActions.get(s.session_id)||{};
    return`<button class="${cls}" title="bring this session's terminal tab to the front"
      ${action.busy?'disabled':''} onclick="event.stopPropagation();focusSession('${s.session_id}',this)">${action.busy?'Opening…':action.ok?'Opened ✓':'Terminal'}</button>`;
  }
  return'';
}
globalThis.viewerQOpen=true;globalThis.viewerPath=null;
// one question's full interaction block (options + descriptions + Other + ✕ dismiss);
// shared by the card's amber box (pre='msg') and the viewer's docked bar (pre='vmsg')
function singleQBlock(s,p,pre){
  const sid=s.session_id;
  const q=p.questions[0],ms=q.multiSelect,n=(q.options||[]).length;
  const otherKey=questionDraftPrefix(sid,p.nonce)+'other:0';
  if(otherDraft[sid]==null)otherDraft[sid]=q.secret?'':draftValue(otherKey);
  const sel=multiSel[sid]=multiSel[sid]||new Set();
  const locked=nativePromptLocked(s,p);
  return`<div class="ptool"><span class="ptlabel">${esc(q.header||'question')} — ${esc(nativePromptLabel(s))}</span>
      <button class="xbtn" ${locked?'disabled':''} title="${p.dismiss_action==='cancel_turn'?'dismiss by stopping this Codex turn':'dismiss — chat about this instead'}" onclick="sendDismiss('${sid}','${p.nonce}','${pre}')">✕</button></div>
    ${p.files&&p.files.length?`<div class="pfiles"><span class="plabel">read first</span>${p.files.map(f=>fchip(sid,f,f.caption)).join('')}</div>`:''}
    <div class="qtext">${esc(q.question)}</div>
    <div class="optgrid">${(q.options||[]).map((o,i)=>`<button class="optbtn ${sel.has(i+1)?'sel':''}${/\(recommended\)/i.test(String(o.label||''))?' rec':''}" ${locked?'disabled':''}
        onclick="${ms?`toggleOpt('${sid}',${i+1})`:`sendOption('${sid}','${p.nonce}',[${i+1}],'${pre}')`}">
        ${esc(o.label)}${o.description?`<small>${esc(o.description)}</small>`:''}</button>`).join('')}</div>
    ${q.allowOther!==false?`<div class="freetext"><input id="oth-${pre}-${sid}" ${q.secret?'':`data-draft-key="${esc(otherKey)}"`} ${locked?'disabled':''} placeholder="Other — type your own answer" ${q.secret?'type="password"':''}
      value="${esc(otherDraft[sid]||'')}" oninput="otherDraft['${sid}']=this.value"
      ${ms?'':`onkeydown="if(event.key==='Enter')sendOther('${sid}','${p.nonce}',${n},'${pre}')"`}>
      ${ms?'':`<button class="pbtn send" ${locked?'disabled':''} onclick="sendOther('${sid}','${p.nonce}',${n},'${pre}')">answer</button>`}</div>`:''}
    ${ms?`<div class="pbtns"><button class="pbtn send" ${locked?'disabled':''} onclick="sendMulti('${sid}','${p.nonce}',${n},'${pre}')">submit selection</button></div>`:''}`;
}
// The delivered-file strip: identical markup and position (docked bar, directly
// above the send box) in BOTH full-screen surfaces, so they read as one screen.
// `cur` marks the file the viewer currently shows.
function fileStrip(sid,files){
  if(!files||!files.length)return'';
  const stripName=name=>{const chars=Array.from(String(name||''));return esc(chars.length>40?`${chars.slice(0,40).join('')}…`:chars.join(''));};
  return`<div class="stripbox"><div class="fstrip">${files.map(f=>`<button class="fchip ${f.file_id===sessionView?.fileId?'cur':''}"
    ${f.missing||!f.file_id?'disabled':''} title="${esc(f.caption||f.name)}" aria-label="${esc(f.name)}${f.missing?' (gone)':''}"
    onclick="viewFile('${enc(sid)}','${enc(f.file_id)}')">${f.kind==='image'?'🖼':'📄'} ${stripName(f.name)}${f.missing?' (gone)':''}</button>`).join('')}</div></div>`;
}
function viewerSurfaceBar(sid,files){
  return`<div class="surfacebar viewer-surfacebar"><div class="surfacebar-main">${fileStrip(sid,files)||'<span class="surfacebar-empty">Current file</span>'}</div>
    <button class="pbtn surfacebar-action chatjump" onclick="openSession('${sid}')">chat</button></div>`;
}
function latestFileButton(sid,files){
  const f=(files||[])[0];if(!f)return'';
  return`<button class="pbtn surfacebar-action latestfile" ${f.missing?'disabled':''} title="${esc(f.caption||f.path)}"
    onclick="viewFile('${enc(sid)}','${enc(f.file_id)}')">${f.kind==='image'?'🖼':'📄'} <span>${esc(f.name)}</span></button>`;
}
function sessionSurfaceBar(s,files){
  const status=statusLineHtml(s.status_line,'session:'+s.session_id),latest=latestFileButton(s.session_id,files);
  if(!status&&!latest)return'';
  return`<div class="surfacebar session-surfacebar"><div class="surfacebar-main">${status}</div>${latest}</div>`;
}
// horizontal scroll position survives the 2s re-render AND the viewer↔chat switch
// (fstripScroll is a shared global, updated live on scroll)
function keepStripScroll(root,fn){
  const old=root.querySelector('.fstrip');
  if(old)fstripScroll=old.scrollLeft;
  fn();
  const ns=root.querySelector('.fstrip');
  if(ns)ns.scrollLeft=fstripScroll;
}
function keepSessionActionScroll(root,fn){
  const contextTop=root.querySelector('.session-context')?.scrollTop||0;
  const question=root.querySelector('.question-scroll');
  if(question?.dataset.scrollKey)setQuestionScrollPosition(
    question.dataset.scrollKey,question.scrollTop);
  keepStripScroll(root,fn);
  const context=root.querySelector('.session-context');if(context)context.scrollTop=contextTop;
  const nextQuestion=root.querySelector('.question-scroll');
  if(nextQuestion?.dataset.scrollKey)nextQuestion.scrollTop=
    questionScrollPositions.get(nextQuestion.dataset.scrollKey)||0;
}
function renderViewerBar(force){
  if(sessionView?.section==='files')renderWorkspaceFiles(force);
}
// Full chat headers identify the conversation. Operational metadata lives in
// the status strip above the composer, where it can update independently.
// Console: title + one quiet mono identity line (project · branch · provider · access).
function sessTitleBlock(s){
  if(!s)return '<span class="sesstitle"><b>session</b></span>';
  const identity=[s.project,s.branch&&s.branch!=='HEAD'?s.branch:null,s.provider,
    s.access==='view_only'||s.read_only?'view only':null].filter(Boolean).join(' · ');
  return `<span class="sesstitle"><b>${esc(s.title||s.project||'session')}</b>${identity?`<small>${esc(identity)}</small>`:''}</span>`;
}
function viewerFormat(name,kind){
  if(kind==='image')return'image';
  const ext=(String(name||'').match(/\.([^.]+)$/)||[])[1]?.toLowerCase()||'';
  if(['md','markdown'].includes(ext))return'markdown';
  if(['html','htm'].includes(ext))return'html';
  if(ext==='pdf')return'pdf';
  if(ext==='json')return'json';
  return'text';
}
function sandboxedHtmlDocument(source){
  const parsed=new DOMParser().parseFromString(String(source||''),'text/html');
  parsed.querySelectorAll('script[src],iframe,frame,object,embed,meta,base,link').forEach(node=>node.remove());
  parsed.querySelectorAll('*').forEach(element=>{
    for(const attr of [...element.attributes]){
      const key=attr.name.toLowerCase(),value=attr.value.trim().toLowerCase();
      if(['srcdoc','action','formaction','target','ping'].includes(key))
        element.removeAttribute(attr.name);
      else if(['href','xlink:href','srcset'].includes(key))element.removeAttribute(attr.name);
      else if(['src','poster','data'].includes(key)&&!value.startsWith('data:'))
        element.removeAttribute(attr.name);
    }
  });
  const styles=[...parsed.querySelectorAll('style')].map(node=>node.outerHTML).join('');
  parsed.querySelectorAll('style').forEach(node=>node.remove());
  const headScripts=[...parsed.head.querySelectorAll('script:not([src])')].map(node=>node.outerHTML).join('');
  parsed.head.querySelectorAll('script:not([src])').forEach(node=>node.remove());
  return`<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; media-src data: blob:; font-src data: blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'">
    ${styles}${headScripts}</head><body>${parsed.body.innerHTML}</body></html>`;
}
function showViewerFrame(body,className,title,{src='',srcdoc='',sandbox=true}={}){
  body.classList.add('frameview');
  const frame=document.createElement('iframe');frame.className=`fileframe ${className}`;
  frame.title=title;if(sandbox!==false)frame.setAttribute('sandbox',sandbox===true?'':sandbox);frame.referrerPolicy='no-referrer';
  if(srcdoc)frame.srcdoc=srcdoc;else frame.src=src;
  body.replaceChildren(frame);
}
function showJsonDocument(body,text){
  body.replaceChildren();
  const pre=document.createElement('pre');pre.className='raw jsondoc';
  try{pre.textContent=JSON.stringify(JSON.parse(text),null,2);}
  catch(error){
    const message=document.createElement('div');message.className='fileerror';
    message.textContent=`Invalid JSON — ${error.message}`;body.append(message);pre.textContent=text;
  }
  body.append(pre);
}
function viewFile(encodedSid,encodedFileId){
  const sid=decodeURIComponent(encodedSid),fileId=decodeURIComponent(encodedFileId||'');
  if(!/^[0-9a-f]{24}$/.test(fileId))return;
  seedTargetedWorkspaceHistory(sid,'files');
  openSessionWorkspace(sid,'files',fileId,true);
}
function seedTargetedWorkspaceHistory(sid,section){
  if(!sessionView||sessionView.sid!==sid)openSessionWorkspace(sid,'chat',null,true);
  if(sessionView?.section!==section)openSessionWorkspace(sid,section,null,true);
}
function closeViewer(){viewerSid=null;viewerPath=null;}
// ---- back-gesture / Esc closes the open full-screen overlay ----------------
// The fullscreen surfaces are mutually exclusive, so we model
// "an overlay is open" as ONE logical state: push a single history entry when we
// go from none-open to open, and the phone's back-swipe (popstate) closes it
// instead of navigating away from the dashboard. Closing via ✕/Esc calls
// history.back() so the pushed entry is consumed and history stays balanced.
globalThis.histPushed=false;globalThis.schedulePushed=false;globalThis.settingsSectionDepth=0;
const fullscreenOverlaySelectors=['#sview','#searchview','#handoffview','#outboxview','#scheduleview'];
const anyOverlay=()=>fullscreenOverlaySelectors.some(id=>$(id).style.display==='flex');
function syncOverlayHistory(){
  if(anyOverlay()&&!histPushed){histPushed=true;history.pushState({fdOverlay:1},'');}
}
window.addEventListener('popstate',()=>{
  const destination=hashDestination();
  if(settingsOpen&&settingsSectionDepth>0&&destination.route==='settings'){
    settingsSection=SETTINGS_SECTIONS.includes(destination.detail)?destination.detail:'notifications';
    settingsSectionDepth=Math.max(0,settingsSectionDepth-1);renderSettings(true);return;
  }
  if(schedulePushed){schedulePushed=false;closeSchedule();return;}
  if(handoffPushed){
    handoffPushed=false;
    const destination=handoffOpenAfterBack;handoffOpenAfterBack=null;
    closeHandoff();
    if(destination)primarySessionAction(destination);
    return;
  }
  const workspace=parseSessionHash();
  if(workspace){applyWorkspaceRoute(workspace);return;}
  if(histPushed){
    histPushed=false;
    closeConfirm();closeHandoff();closeViewer();closeAgent();closeSession();closeSearchView();closeOutbox();closeSchedule();
    return;
  }
  if(sessionView)closeSession();
  notificationDetailId=destination.route==='notifications'?destination.detail:'';
  if(!notificationDetailId){notificationDetail=null;notificationDetailError='';}
  navigateTo(destination.route,false,Boolean(notificationDetailId));
});
function dismissOverlay(){
  if(usageOpen)return closeUsage();
  if(overflowOpen)return closeOverflow();
  if(document.querySelector('.composerplus.open'))return closeComposerMenus();
  if($('#confirm').style.display==='flex')return closeConfirm();   // ask first
  if(handoffPushed)return history.back();
  if(schedulePushed)return history.back();
  if(sessionView)return history.back();
  if(histPushed)history.back();          // → popstate does the actual close
  else{closeHandoff();closeViewer();closeAgent();closeSession();closeSearchView();closeOutbox();closeSchedule();}
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')dismissOverlay();});
document.addEventListener('click',e=>{
  if(usageOpen&&!e.target.closest('#usagepanel')&&!e.target.closest('#usagechip')&&
    !e.target.closest('#railusage'))closeUsage();
  if(overflowOpen&&!e.target.closest('.ovwrap'))closeOverflow();
  if(!e.target.closest('.composertools'))closeComposerMenus();
  if($('#mobilemore').classList.contains('open')&&!e.target.closest('#mobilemore')&&!e.target.closest('[data-route="more"]'))closeMobileMore();
});

// Full-screen surfaces are real, stack-aware dialogs. Their markup predates the
// modal controller, so semantics and focus ownership are applied centrally.
const modalDefinitions=[
  ['sview','stitle2','Session workspace'],
  ['searchview','searchviewtitle','Search result'],['handoffview','handofftitle','Continue in another session'],
  ['outboxview',null,'Message Outbox'],['scheduleview','scheduletitle','Schedule message'],
  ['confirm',null,'Confirmation']];
const modalOpeners=new WeakMap(),modalOpenState=new WeakSet();globalThis.modalLastClosedOpener=null;
function modalOpener(element){
  if(!element||element===document.body)return null;
  const card=element.closest?.('[data-sid]');
  return{element,id:element.id||'',sid:card?.dataset.sid||'',aria:element.getAttribute?.('aria-label')||''};
}
function resolveModalOpener(record){
  if(!record)return null;
  if(record.element?.isConnected)return record.element;
  if(record.id){const found=document.getElementById(record.id);if(found)return found;}
  if(record.sid&&record.aria){
    return[...document.querySelectorAll('[data-sid]')].find(card=>card.dataset.sid===record.sid)
      ?.querySelector(`[aria-label="${CSS.escape(record.aria)}"]`)||null;
  }
  return null;
}
function modalVisible(root){return root&&root.style.display==='flex';}
// The desktop-docked session pane (#sview.docked) is NOT a modal: the queue
// stays interactive beside it, so it never joins the modal stack, never inerts
// lower layers, and never traps Tab. It still goes inert under a real modal.
function modalStack(){return modalDefinitions.map(([id])=>document.getElementById(id))
  .filter(root=>modalVisible(root)&&!root.classList.contains('docked'))
  .sort((a,b)=>(Number(getComputedStyle(a).zIndex)||0)-(Number(getComputedStyle(b).zIndex)||0));}
function modalFocusable(root){return[...root.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')]
  .filter(element=>!element.hidden&&element.getClientRects().length);}
function syncModalStack(){
  const stack=modalStack(),top=stack.at(-1)||null;
  let opened=false;
  for(const [id] of modalDefinitions){
    const root=document.getElementById(id),displayed=modalVisible(root),inStack=stack.includes(root);
    if(inStack&&!modalOpenState.has(root)){
      opened=true;modalOpenState.add(root);modalOpeners.set(root,modalOpener(document.activeElement));
    }else if(!inStack&&modalOpenState.has(root)){
      modalOpenState.delete(root);modalLastClosedOpener=modalOpeners.get(root)||modalLastClosedOpener;
    }
    // a displayed docked pane (visible, not in the stack) is a lower layer:
    // interactive when nothing modal is above it, inert under the top modal
    root.inert=(inStack&&root!==top)||(!inStack&&displayed&&Boolean(top));
    root.setAttribute('aria-hidden',displayed&&(root===top||(!inStack&&!top))?'false':'true');
  }
  for(const id of ['appshell','bottomnav','mobilemore','usagepanel']){
    const root=document.getElementById(id);if(root)root.inert=Boolean(top);
  }
  if(opened)requestAnimationFrame(syncVisualViewport);
  if(top)requestAnimationFrame(()=>{
    if(modalStack().at(-1)!==top||top.contains(document.activeElement))return;
    const restore=resolveModalOpener(modalLastClosedOpener);
    ((restore&&top.contains(restore)&&!restore.closest('[inert]'))?restore:
      (modalFocusable(top)[0]||top)).focus({preventScroll:true});modalLastClosedOpener=null;
  });
  else requestAnimationFrame(()=>{
    const restore=resolveModalOpener(modalLastClosedOpener);modalLastClosedOpener=null;
    if(restore&&!restore.closest('[inert]')&&!restore.closest(fullscreenOverlaySelectors.join(',')))
      restore.focus({preventScroll:true});
  });
}
for(const [id,labelledBy,label] of modalDefinitions){
  const root=document.getElementById(id);root.setAttribute('role','dialog');root.setAttribute('aria-modal','true');
  root.tabIndex=-1;if(labelledBy)root.setAttribute('aria-labelledby',labelledBy);else root.setAttribute('aria-label',label);
  root.setAttribute('aria-hidden','true');
  new MutationObserver(syncModalStack).observe(root,{attributes:true,attributeFilter:['style']});
}
document.addEventListener('keydown',event=>{
  if(event.key!=='Tab')return;const top=modalStack().at(-1);if(!top)return;
  const focusable=modalFocusable(top);if(!focusable.length){event.preventDefault();top.focus();return;}
  const first=focusable[0],lastItem=focusable.at(-1),active=document.activeElement;
  if(!top.contains(active)||(event.shiftKey&&active===first)||(!event.shiftKey&&active===lastItem)){
    event.preventDefault();(event.shiftKey?lastItem:first).focus();
  }
},true);

// iOS resizes the visual viewport independently from the fixed layout viewport.
// Keep full-screen reading surfaces fitted to the pixels above the keyboard so
// the dashboard beneath them can never peek through.
globalThis.visualViewportBaseline=Math.max(1,Math.round(
  window.visualViewport?.height||window.innerHeight||document.documentElement.clientHeight||1));
function syncVisualViewport(){
  const readingAnchor=captureReadingAnchor();
  const viewport=globalThis.__fleetVisualViewportOverride||window.visualViewport;
  const height=Math.max(1,Math.round(viewport?.height||window.innerHeight));
  const top=Math.max(0,Math.round(viewport?.offsetTop||0));
  const layoutHeight=Math.max(height,window.innerHeight||0,document.documentElement.clientHeight||0);
  const focused=document.activeElement;
  const editingOverlay=focused&&['INPUT','TEXTAREA','SELECT'].includes(focused.tagName)&&
    Boolean(focused.closest(fullscreenOverlaySelectors.join(',')));
  if(!editingOverlay)visualViewportBaseline=Math.max(height,layoutHeight);
  const keyboardOpen=Math.max(layoutHeight-height-top,visualViewportBaseline-height-top)>80;
  document.documentElement.style.setProperty('--fleet-visual-height',height+'px');
  document.documentElement.style.setProperty('--fleet-visual-top',top+'px');
  document.documentElement.classList.toggle('keyboard-open',keyboardOpen);
  const compact=matchMedia('(pointer:coarse)').matches||innerWidth<=820;
  const activeOverlay=focused?.closest?.(fullscreenOverlaySelectors.join(','))||modalStack().at(-1)||null;
  for(const selector of fullscreenOverlaySelectors){const root=$(selector);if(compact&&root===activeOverlay){
    root.style.setProperty('inset','auto 0px');root.style.setProperty('top',top+'px');
    root.style.setProperty('bottom','auto');root.style.setProperty('height',height+'px');
  }else for(const property of ['inset','top','bottom','height'])root.style.removeProperty(property);}
  globalThis.syncQuestionDrawerGeometry?.();
  restoreReadingAnchor(readingAnchor);
}
window.visualViewport?.addEventListener('resize',syncVisualViewport);
window.visualViewport?.addEventListener('scroll',syncVisualViewport);
window.addEventListener('orientationchange',()=>requestAnimationFrame(syncVisualViewport));
document.addEventListener('focusin',event=>{
  if(event.target.closest?.(fullscreenOverlaySelectors.join(',')))requestAnimationFrame(syncVisualViewport);
},true);
document.addEventListener('focusout',event=>{
  if(!event.target.closest?.('.session-composer .composer'))return;
  setTimeout(()=>{
    const anchor=captureReadingAnchor();
    if(!document.activeElement?.closest?.('.session-composer .composer'))
      event.target.closest?.('.session-composer')?.classList.remove('composer-active');
    syncVisualViewport();restoreReadingAnchor(anchor);
  },0);
},true);
globalThis.chatTouch=null;
globalThis.sessionScrollIntentAt=0;
const noteSessionScrollIntent=event=>{
  if(event.target?.closest?.('#sbody')){
    sessionScrollIntentAt=Date.now();
    sessionReadingIntentRevision++;
  }
};
document.addEventListener('wheel',noteSessionScrollIntent,{passive:true,capture:true});
document.addEventListener('touchmove',noteSessionScrollIntent,{passive:true,capture:true});
document.addEventListener('pointerdown',noteSessionScrollIntent,{passive:true,capture:true});
document.addEventListener('keydown',event=>{
  if(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(event.key)&&
      event.target?.closest?.('#sview')){
    sessionScrollIntentAt=Date.now();
    sessionReadingIntentRevision++;
  }
},true);
document.addEventListener('touchstart',event=>{
  const input=document.activeElement;
  const surface=event.target.closest?.('#sbody,#vbody,#abody,#sfilelist,#sagentlistpane,#spanel-details,.question-scroll');
  if(!surface||!input?.closest?.('.session-composer'))return chatTouch=null;
  const touch=event.touches?.[0];
  chatTouch=touch?{x:touch.clientX,y:touch.clientY,input,surface}:null;
},{passive:true,capture:true});
document.addEventListener('touchmove',event=>{
  if(!chatTouch)return;
  const touch=event.touches?.[0];if(!touch)return;
  const dx=Math.abs(touch.clientX-chatTouch.x),dy=Math.abs(touch.clientY-chatTouch.y);
  if(dy<10||dy<=dx)return;
  chatTouch.input.blur();closeComposerMenus();chatTouch=null;
  requestAnimationFrame(syncVisualViewport);
},{passive:true,capture:true});
document.addEventListener('touchend',()=>{chatTouch=null;},{passive:true,capture:true});
$('#sbody')?.addEventListener('scroll',()=>{
  // Resizing images/messages also emits scroll events on mobile. Only an
  // actual reader gesture may disengage follow-tail; layout growth is handled
  // by the ResizeObserver and must remain pinned.
  if(Date.now()-sessionScrollIntentAt<1200)sessionFollowTail=sessionNearTail($('#sbody'));
},{passive:true});
syncVisualViewport();

// ---- full-screen session view ----------------------------------------------
// Same overlay shape as the subagent view, but this one is a real terminal
// channel: send box, question block, interrupt/mute. Ids use the `sft-`/`smsg-`
// prefixes — the card's `ft-`/`msg-` elements coexist in the DOM.
const statusExpanded=new Set(),statusCostsOpen=new Set();
function statusGraphPoint(value){
  const n=Number(value)||0;
  if(n>20000)return['█','hot'];if(n>15000)return['▇','warm'];if(n>10000)return['▆','warm'];
  if(n>7500)return['▅','warn'];if(n>5000)return['▄','warn'];if(n>2500)return['▃','cool'];
  if(n>1000)return['▂','cool'];return['▁','cool'];
}
function statusLineHtml(status,key){
  if(!status||typeof status!=='object')return'';
  const id=String(key||'status'),expanded=statusExpanded.has(id),costOpen=statusCostsOpen.has(id);
  const branch=status.branch&&status.branch!=='HEAD'?String(status.branch):'';
  const git=[];
  if(branch){
    let label='⎇ '+branch;
    if(Number.isFinite(status.ahead)&&status.ahead>0)label+=` ↑${status.ahead}`;
    if(Number.isFinite(status.behind)&&status.behind>0)label+=` ↓${status.behind}`;
    git.push(`<span>${esc(label)}</span>`);
  }
  if(status.worktree_label)git.push(`<span title="${esc(status.worktree||'')}">${esc(status.worktree_label)}</span>`);
  const model=[];
  if(status.model)model.push(`<span>${esc(status.model)}${status.effort?` · ${esc(status.effort)}`:''}</span>`);
  const context=[];
  if(Number.isFinite(status.context_pct))context.push(`Ctx: ${status.context_pct}%`);
  if(Number.isFinite(status.compact_remaining))context.push(`→${fmtTok(status.compact_remaining)}`);
  if(context.length)model.push(`<span>${esc(context.join('  '))}</span>`);
  const cache=[];
  if(Number.isFinite(status.cache_read_pct)){
    const tier=status.cache_read_pct>=90?'good':status.cache_read_pct>=75?'warn':status.cache_read_pct>=50?'warm':'hot';
    cache.push(`<span class="${tier}">♻ ${status.cache_read_pct}%</span>`);
  }
  if(Number.isFinite(status.cache_write)){
    let cw=`✎ ${fmtTok(status.cache_write)}`;
    if(Number(status.cache_write_spikes)>0)cw+=` · spikes ${status.cache_write_spikes}`;
    if(Number(status.cache_write_peak)>0)cw+=` · peak ${fmtTok(status.cache_write_peak)}`;
    cache.push(`<span>${esc(cw)}</span>`);
  }
  const breakdown=Array.isArray(status.cost_breakdown)?status.cost_breakdown:[];
  let cost='';
  if(Number.isFinite(status.tree_cost)){
    const prefix=status.cost_scope==='estimated'?'~':'';
    const label=status.cost_label==='agent'?'agent':'tree';
    const delta=Number.isFinite(status.turn_cost)?` · +${fmt$(status.turn_cost)}`:'';
    const summary=`${label} ${prefix}${fmt$(status.tree_cost)}${delta}`;
    cost=breakdown.length>1?`<details class="status-cost" ${costOpen?'open':''}
      ontoggle="statusCostToggle('${enc(id)}',this.open)"><summary>${esc(summary)}</summary>
      <div class="status-cost-breakdown">${breakdown.map(item=>`<span>${esc(item.label||item.kind||'Usage')}<b>${esc(prefix+fmt$(item.cost))}</b></span>`).join('')}
      ${status.cost_breakdown_omitted?`<small>+${status.cost_breakdown_omitted} more</small>`:''}</div></details>`:
      `<span class="status-tree-cost">${esc(summary)}</span>`;
    cache.push(cost);
  }
  const history=(status.cache_write_history||[]).filter(Number.isFinite).slice(-50);
  const graph=history.map(value=>{const [glyph,tier]=statusGraphPoint(value);return`<i class="${tier}">${glyph}</i>`;}).join('');
  const primary=git.length?`<div class="status-primary">${git.join('<em>│</em>')}</div>`:'';
  const secondary=model.length?`<div class="status-secondary">${model.join('<em>│</em>')}</div>`:'';
  const details=(cache.length||graph)?`<div class="status-details">
    ${cache.length?`<div class="status-cache">${cache.join('<em>│</em>')}</div>`:''}
    ${graph?`<div class="status-graph" aria-label="Cache write history: ${esc(history.join(', '))}"><b>CW</b>${graph}</div>`:''}
  </div>`:'';
  if(!primary&&!secondary&&!details)return'';
  return`<section class="statusstrip${expanded?' expanded':''}${status.frozen?' frozen':''}" data-status-key="${esc(id)}">
    ${primary}${secondary}
    ${details?`<button class="status-expand" aria-label="${expanded?'collapse':'expand'} status details" aria-expanded="${expanded}"
      onclick="toggleStatusDetails('${enc(id)}')">${expanded?'hide usage details':'usage details'}</button>${details}`:''}
  </section>`;
}
function statusCostToggle(encodedKey,isOpen){
  const key=decodeURIComponent(encodedKey);if(isOpen)statusCostsOpen.add(key);else statusCostsOpen.delete(key);
}
function toggleStatusDetails(encodedKey){
  const key=decodeURIComponent(encodedKey);if(statusExpanded.has(key))statusExpanded.delete(key);else statusExpanded.add(key);
  if(key.startsWith('agent:'))renderAgent(true);else if(sessionView?.closed)renderClosed(true);else renderSession(true);
}
function refreshStatusStrip(hostSelector,status,key){
  const current=document.querySelector(hostSelector+' .statusstrip');
  if(current){const anchor=captureReadingAnchor();current.outerHTML=statusLineHtml(status,key);
    restoreReadingAnchor(anchor);}
}

Object.assign(globalThis,{terminalActions,fullscreenOverlaySelectors,anyOverlay,modalDefinitions,modalOpeners,modalOpenState,noteSessionScrollIntent,statusExpanded,statusCostsOpen});
