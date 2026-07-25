// extracted verbatim from app.js — shared state lives on globalThis (see AGENTS.md)
Object.assign(globalThis,{setHtml,positionUsagePanel,closeUsage,toggleUsage,usageReset,ugauge,usageBar,spark,mdInline,md});
globalThis.lastMove=0;globalThis.lastTap=0;
document.addEventListener('touchstart',()=>{lastTap=Date.now()},{passive:true});
document.addEventListener('pointerdown',()=>{lastTap=Date.now()},{passive:true});
document.addEventListener('touchmove',()=>{lastMove=Date.now()},{passive:true});
document.addEventListener('wheel',()=>{lastMove=Date.now()},{passive:true});
const touching=()=>Date.now()-lastMove<1500||Date.now()-lastTap<800;
// Write HTML only when it actually changed. An identical innerHTML assignment
// still tears down and re-parses the subtree, dropping focus and forcing
// relayout — twice a second, for nothing.
function setHtml(element,html){
  if(!element||element.__setHtml===html)return false;
  element.__setHtml=html;element.innerHTML=html;return true;
}
// scrollbar auto-hide: thumbs are transparent until the element actually scrolls
// (class fades 700ms after the last scroll event). Deliberately NOT tied into
// lastMove — programmatic scrolls (sticky-bottom restores) fire scroll events
// too, and feeding those into the render guard would starve re-renders.
globalThis.sbFade=undefined;
globalThis.fstripScroll=0;   // shared so the file strip keeps its position across the md
                      // viewer ↔ full chat view switch (a fresh DOM element each side)
document.addEventListener('scroll',e=>{
  const el=e.target===document?document.documentElement:e.target;
  if(el.classList){
    el.classList.add('scrolling');
    if(el.classList.contains('fstrip'))fstripScroll=el.scrollLeft;   // remember it live
  }
  clearTimeout(sbFade);
  sbFade=setTimeout(()=>document.querySelectorAll('.scrolling').forEach(x=>x.classList.remove('scrolling')),700);
},{passive:true,capture:true});
const fmt$=v=>v==null?'unavailable':'$'+(v>=100?v.toFixed(0):v>=10?v.toFixed(1):v.toFixed(2));
const fmtTok=v=>v==null?'—':v>=1e9?(v/1e9).toFixed(2)+'B':v>=1e6?(v/1e6).toFixed(2)+'M':v>=1e3?(v/1e3).toFixed(0)+'k':v;
const fmtAge=s=>s>=86400?Math.round(s/86400)+'d':s>=3600?Math.round(s/3600)+'h':s>=60?Math.round(s/60)+'m':Math.round(s)+'s';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const stateLabel={running:'Working',needs_you:'Response needed',turn_done:'Available',idle:'Available',
  stalled:'Slow',stalled_or_prompt:'Check session',dormant:'Inactive',reopenable:'Reopenable',
  stale:'Unavailable',blocked:'Limit reached',error:'Fix needed'};

// ---- on-demand provider plan usage ----------------------------------------
globalThis.usageOpen=false;
const USAGE_GAP=8;      // the small gap between the button and the panel
const USAGE_EDGE=8;     // keep this much clear of every viewport edge
// The panel's top-left pins to the Usage button's bottom-left on desktop AND
// mobile. It is position:fixed, so the coordinates are remeasured whenever the
// button can move under it (scroll, resize, keyboard-driven viewport changes).
function positionUsagePanel(){
  const panel=$('#usagepanel'),chip=$('#usagechip');
  if(!panel||!chip||!usageOpen)return;
  if(panel.classList.contains('fromrail'))return; // CSS pins it beside the rail footer
  const button=chip.getBoundingClientRect();
  const vw=window.innerWidth,vh=window.innerHeight;
  // shrink to whatever fits to the button's right, so the left edges can line
  // up; only a panel narrower than the floor gets nudged left of the button
  const floor=Math.min(300,vw-2*USAGE_EDGE);
  const width=Math.max(floor,Math.min(520,vw-button.left-USAGE_EDGE));
  panel.style.setProperty('--usage-width',`${Math.round(width)}px`);
  const left=Math.max(USAGE_EDGE,Math.min(button.left,vw-width-USAGE_EDGE));
  const top=Math.max(USAGE_EDGE,button.bottom+USAGE_GAP);
  panel.style.setProperty('--usage-left',`${Math.round(left)}px`);
  panel.style.setProperty('--usage-top',`${Math.round(top)}px`);
  panel.style.setProperty('--usage-maxh',`${Math.max(120,Math.round(vh-top-USAGE_EDGE))}px`);
}
function closeUsage(){
  usageOpen=false;
  $('#usagepanel')?.classList.remove('open');
  $('#usagechip')?.setAttribute('aria-expanded','false');
}
function toggleUsage(source){
  usageOpen=!usageOpen;
  const panel=$('#usagepanel');
  // Desktop opens from the rail footer (pinned beside the rail); mobile still
  // anchors under the Now-header Usage chip.
  panel?.classList.toggle('fromrail',source==='rail');
  panel?.classList.toggle('open',usageOpen);
  $('#usagechip')?.setAttribute('aria-expanded',String(usageOpen));
  $('#railusage')?.setAttribute('aria-expanded',String(usageOpen));
  // measure only once it is displayed, or offsetWidth is 0
  if(usageOpen)positionUsagePanel();
}
addEventListener('scroll',()=>positionUsagePanel(),true);
addEventListener('resize',()=>positionUsagePanel());
window.visualViewport?.addEventListener('resize',()=>positionUsagePanel());
window.visualViewport?.addEventListener('scroll',()=>positionUsagePanel());
function usageReset(iso){
  if(!iso)return'';
  const t=Date.parse(iso);if(isNaN(t))return'';
  const secs=Math.round((t-Date.now())/1000);
  const near=secs<86400;
  const when=new Date(t).toLocaleString([],near?{hour:'numeric',minute:'2-digit'}
    :{weekday:'short',hour:'numeric',minute:'2-digit'});
  return secs>0?`resets ${when} · ${fmtAge(secs)} left`:`resets ${when}`;
}
function ugauge(label,pct,reset){
  if(pct==null)return'';
  const col=pct>=90?'var(--red)':pct>=70?'var(--amber)':'var(--green)';
  return`<div class="ugauge"><span class="ulabel">${esc(String(label||''))}</span>
    <span class="ubar"><i style="width:${Math.min(pct,100)}%;background:${col}"></i></span>
    <span class="upct">${pct}%</span>
    ${reset?`<span class="ureset">${reset}</span>`:''}</div>`;
}
function usageBar(legacy,providers){
  const el=$('#usagebody'),chip=$('#usagechip');
  if(!el||!chip)return;
  const claude=(providers&&providers.claude)||legacy;
  const codex=providers&&providers.codex;
  // Spark has a separate preview-model allowance. Keep it in the provider/API
  // data, but omit the unused model-specific bucket from the account summary.
  const codexBuckets=(codex?.buckets||[]).filter(b=>
    !/^gpt-5\.3-codex-spark\b/i.test(String(b.label||'')));
  const claudeProfiles=claude?.profiles?.length?claude.profiles:[claude];
  const activeClaude=claudeProfiles.find(profile=>profile?.active) || claudeProfiles.find(Boolean);
  const claudeWindows=activeClaude?[activeClaude.five_hour_pct,
    claude?.show_week===false?null:activeClaude.weekly_pct]
    .filter(value=>Number.isFinite(Number(value))).map(value=>Math.round(Number(value))):[];
  const activeCodex=codexBuckets.map(bucket=>Number(bucket.used_pct))
    .filter(Number.isFinite).sort((a,b)=>b-a)[0];
  const summaries=[];
  if(claudeWindows.length)summaries.push(`Claude ${claudeWindows.join('/')}`);
  if(Number.isFinite(activeCodex))summaries.push(`Codex ${Math.round(activeCodex)}`);
  chip.textContent=`Usage${summaries.length?` · ${summaries.join(' · ')}`:''}`;
  chip.title='Claude active account: 5-hour/weekly · Codex: highest active non-Spark window';
  // Desktop rail footer: per-window usage bars (single %-used numbers,
  // amber ≥70 / red ≥90) + signed-in account. Same data as the chip summary.
  const rail=$('#railusage'),railUser=$('#railuser');
  if(rail){
    const rows=[];
    if(activeClaude){
      if(Number.isFinite(Number(activeClaude.five_hour_pct)))
        rows.push(['CLAUDE 5H',Math.round(Number(activeClaude.five_hour_pct))]);
      if(claude?.show_week!==false&&Number.isFinite(Number(activeClaude.weekly_pct)))
        rows.push(['CLAUDE WK',Math.round(Number(activeClaude.weekly_pct))]);
    }
    if(Number.isFinite(activeCodex))rows.push(['CODEX',Math.round(activeCodex)]);
    rail.innerHTML=rows.map(([label,pct])=>{
      const tone=pct>=90?'crit':pct>=70?'warn':'';
      return`<span class="urow"><span>${esc(label)}</span><span class="${tone}">${pct}%</span></span>`+
        `<span class="ubarline"><i class="${tone}" style="width:${Math.min(pct,100)}%"></i></span>`;
    }).join('');
  }
  if(railUser){
    const email=String(activeClaude?.email||codex?.email||'');
    const name=email.split('@')[0];
    railUser.innerHTML=name?`<span class="railavatar">${esc(name.slice(0,1))}</span>
      <span><b>${esc(name)}</b><small>signed in</small></span>`:'';
  }
  const claudeHtml=claude&&claudeProfiles.some(p=>p&&(p.five_hour_pct!=null||p.weekly_pct!=null||p.email))||claude?.lifetime_tokens!=null
    ?`<div class="uprovider">${claudeProfiles.filter(Boolean).map((profile,index)=>`<div class="uaccount">
      <div class="uhead"><span class="uname">Claude Code</span>
        ${profile.email?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="uemail">${esc(profile.email)}</span></span>`:''}
        ${claude.show_active!==false&&profile.active?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="uactive">active</span></span>`:''}
        ${index===0&&claude.lifetime_tokens!=null?`<span class="useg" title="All local Claude transcripts on this Mac across profiles, including saved subagents; excludes deleted history, claude.ai, and other computers"><i class="usep" aria-hidden="true">·</i><span class="umeta">${fmtTok(claude.lifetime_tokens)} local lifetime tokens</span></span>`:''}</div>
      ${ugauge('5-hour',profile.five_hour_pct,usageReset(profile.five_hour_reset))}
      ${claude.show_week===false?'':ugauge('weekly',profile.weekly_pct,usageReset(profile.weekly_reset))}
      ${claude.show_week===false?'':ugauge('Fable weekly',profile.fable_weekly_pct,usageReset(profile.fable_weekly_reset))}</div>`).join('')}</div>`:'';
  const codexHtml=codex&&(codexBuckets.length||codex.email||codex.plan_type||codex.lifetime_tokens!=null||codex.reset_credits||codex.error)?`<div class="uprovider">
    <div class="uhead"><span class="uname">Codex CLI</span>
      ${codex.email?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="uemail">${esc(codex.email)}</span></span>`:''}
      ${codex.plan_type?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="umeta">${esc(codex.plan_type.replaceAll('_',' '))}</span></span>`:''}
      ${codex.lifetime_tokens!=null?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="umeta">${fmtTok(codex.lifetime_tokens)} lifetime tokens</span></span>`:''}
      ${codex.reset_credits?`<span class="useg"><i class="usep" aria-hidden="true">·</i><span class="umeta">${codex.reset_credits} reset credit</span></span>`:''}</div>
    ${codex.error?`<span class="umeta">${codex.stale?'stale — ':''}${esc(codex.error)}</span>`:''}
    ${codexBuckets.map(b=>ugauge(b.label,b.used_pct,usageReset(b.reset))).join('')}</div>`:'';
  if(!claudeHtml&&!codexHtml){el.className='empty';el.innerHTML='<div class="usageempty">Usage data is unavailable.</div>';return;}
  el.className='';el.innerHTML=claudeHtml+codexHtml;
}

function spark(pts,w=64,h=16){
  if(!pts||pts.length<2)return'';
  const min=Math.min(...pts),max=Math.max(...pts),r=max-min||1;
  const p=pts.map((v,i)=>`${(i/(pts.length-1)*w).toFixed(1)},${(h-2-(v-min)/r*(h-4)).toFixed(1)}`).join(' ');
  return`<svg class="spark" width="${w}" height="${h}"><polyline points="${p}" fill="none" stroke="var(--green)" stroke-width="1.5"/></svg>`;
}

// ---- tiny markdown renderer (self-contained: no CDN on the tailnet path) ----
function mdInline(s){
  return esc(s).split(/(`[^`]*`)/).map(p=>{
    if(p.length>1&&p.startsWith('`')&&p.endsWith('`'))return'<code>'+p.slice(1,-1)+'</code>';
    return p
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
      .replace(/\*([^*\n]+)\*/g,'<i>$1</i>');
  }).join('');
}
function md(src){
  const lines=(src||'').replace(/\r/g,'').split('\n');
  let html='',i=0,m;
  const isList=l=>/^\s*([-*+]|\d+\.)\s+/.test(l);
  const cells=r=>r.replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|').map(c=>c.trim());
  const isTableSep=l=>/^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(l)&&l.includes('-');
  while(i<lines.length){
    const l=lines[i];
    if(/^\s*```/.test(l)){
      const buf=[];i++;
      while(i<lines.length&&!/^\s*```/.test(lines[i]))buf.push(lines[i++]);
      i++;html+='<pre><code>'+esc(buf.join('\n'))+'</code></pre>';continue;
    }
    if(!l.trim()){i++;continue;}
    if(m=l.match(/^(#{1,6})\s+(.*)/)){const n=m[1].length;html+=`<h${n}>${mdInline(m[2])}</h${n}>`;i++;continue;}
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)){html+='<hr>';i++;continue;}
    if(/^\s*>/.test(l)){
      const buf=[];while(i<lines.length&&/^\s*>/.test(lines[i]))buf.push(lines[i++].replace(/^\s*>\s?/,''));
      html+='<blockquote>'+md(buf.join('\n'))+'</blockquote>';continue;
    }
    if(isList(l)){
      const ord=/^\s*\d+\./.test(l),items=[];
      while(i<lines.length&&isList(lines[i])){
        let it=lines[i].replace(/^\s*([-*+]|\d+\.)\s+/,'');i++;
        while(i<lines.length&&/^\s{2,}\S/.test(lines[i])&&!isList(lines[i]))it+=' '+lines[i++].trim();
        items.push(it);
      }
      html+=(ord?'<ol>':'<ul>')+items.map(x=>'<li>'+mdInline(x)+'</li>').join('')+(ord?'</ol>':'</ul>');continue;
    }
    if(l.includes('|')&&i+1<lines.length&&isTableSep(lines[i+1])){
      const head=cells(l);i+=2;let rows='';
      while(i<lines.length&&lines[i].includes('|')&&lines[i].trim())
        rows+='<tr>'+cells(lines[i++]).map(c=>'<td>'+mdInline(c)+'</td>').join('')+'</tr>';
      html+='<table><tr>'+head.map(c=>'<th>'+mdInline(c)+'</th>').join('')+'</tr>'+rows+'</table>';continue;
    }
    const buf=[l];i++;
    while(i<lines.length&&lines[i].trim()&&!/^\s*(#{1,6}\s|```|>)/.test(lines[i])
          &&!isList(lines[i])&&!(lines[i].includes('|')&&i+1<lines.length&&isTableSep(lines[i+1])))
      buf.push(lines[i++]);
    html+='<p>'+buf.map(mdInline).join('<br>')+'</p>';
  }
  return html;
}

// ---- durable message Outbox -----------------------------------------------

Object.assign(globalThis,{touching,fmt$,fmtTok,fmtAge,esc,stateLabel,USAGE_GAP,USAGE_EDGE});
