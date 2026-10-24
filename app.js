const APP_BASE=location.pathname.startsWith('/pre/')?'/pre/':'/';
const BOSS_NAMES={mushroom:'蘑菇王',zombie:'僵尸蘑菇王',dorr:'多尔',faust:'浮士德',eliza:'艾利杰',snowman:'驮狼雪人',global_mushroom:'国际服蘑菇王'};
const BOSS_FLAGS={mushroom:1,zombie:2,dorr:4,faust:8,eliza:16,snowman:32,global_mushroom:64};
const BOSS_WINDOWS={mushroom:[38,60],zombie:[38,60],snowman:[58,100],global_mushroom:[58,90]};
const requestedBoss=new URLSearchParams(location.search).get('boss');
const CURRENT_BOSS=Object.hasOwn(BOSS_NAMES,requestedBoss)?requestedBoss:'mushroom';
const REALM_NAMES={white_rabbit:'小白兔',red_crab:'红螃蟹'};
const requestedRealm=new URLSearchParams(location.search).get('realm');
let activeRealm=Object.hasOwn(REALM_NAMES,requestedRealm)?requestedRealm:null;
document.documentElement.dataset.boss=CURRENT_BOSS;
document.title=BOSS_NAMES[CURRENT_BOSS]+' · 频道协作';
document.querySelector('header strong').textContent=BOSS_NAMES[CURRENT_BOSS];
const storageKey=key=>APP_BASE+key;
const alertStorageKey=()=>storageKey('timerAlerts:'+(activeRealm||'white_rabbit'));
let bossWritesPending=0;
const app=document.querySelector('#app'), identity=document.querySelector('#identity');
const accountWatermark=document.createElement('div');
accountWatermark.className='account-watermark';
accountWatermark.setAttribute('aria-hidden','true');
accountWatermark.hidden=true;
document.body.append(accountWatermark);
let watermarkName='';
function setWatermark(displayName){
  const name=String(displayName||'');
  if(name===watermarkName)return;
  watermarkName=name;
  if(!name){accountWatermark.style.backgroundImage='';accountWatermark.hidden=true;return}
  const width=Math.max(260,Math.min(600,Array.from(name).length*18+120)),height=160;
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('xmlns','http://www.w3.org/2000/svg');
  svg.setAttribute('width',String(width));svg.setAttribute('height',String(height));
  const label=document.createElementNS('http://www.w3.org/2000/svg','text');
  label.setAttribute('x',String(width/2));label.setAttribute('y',String(height/2));
  label.setAttribute('text-anchor','middle');label.setAttribute('dominant-baseline','middle');
  label.setAttribute('transform',`rotate(-24 ${width/2} ${height/2})`);
  label.setAttribute('fill',CURRENT_BOSS==='global_mushroom'?'#745989':CURRENT_BOSS==='snowman'?'#436487':CURRENT_BOSS==='zombie'?'#386354':'#f0d8ff');label.setAttribute('font-size','20');
  label.setAttribute('font-weight','700');label.setAttribute('font-family','Microsoft YaHei, sans-serif');
  label.textContent=name;svg.append(label);
  accountWatermark.style.backgroundImage=`url("data:image/svg+xml,${encodeURIComponent(new XMLSerializer().serializeToString(svg))}")`;
  accountWatermark.style.backgroundSize=`${width}px ${height}px`;
  accountWatermark.hidden=false;
}
let selectedRanking='exploration';
let memberSearch='';
let state=null, events=null, loginPending=false, refreshVersion=0, alertKey=new Set(JSON.parse(localStorage.getItem(alertStorageKey())||'[]')), clockOffset=0, soundEnabled=localStorage.getItem(storageKey('timerSound'))!=='off', audioContext=null, blacklistSelection=new Set();
const flashUntil=new Map();
const noticeTimers=new Map(), noticeDeadlines=new Map(), dismissedNotices=new Set();
function dismissNotice(id){
  if(!state)return;
  const key=`${CURRENT_BOSS}:${state.me.id}:${id}`;
  if(dismissedNotices.has(key))return;
  clearTimeout(noticeTimers.get(key));noticeTimers.delete(key);dismissedNotices.add(key);
  noticeDeadlines.delete(key);
  app.querySelector(`[data-notice-id="${id}"]`)?.remove();
  return api('notice/read',{notice_id:id}).then(refresh).catch(e=>{if(e.status!==404)toast('通知已隐藏，已读状态暂未同步')});
}
function scheduleNoticeDismissal(){
  const uid=state.me.id;
  for(const notice of state.notices||[]){
    const key=`${CURRENT_BOSS}:${uid}:${notice.id}`;
    if(dismissedNotices.has(key)||noticeTimers.has(key))continue;
    if(!app.querySelector(`[data-notice-id="${notice.id}"]`))continue;
    noticeDeadlines.set(key,Date.now()+5000);
    noticeTimers.set(key,setTimeout(()=>{noticeTimers.delete(key);if(state?.me.id===uid)dismissNotice(notice.id)},5000));
  }
  updateNoticeCountdowns();
}
function updateNoticeCountdowns(){
  if(!state)return;
  app.querySelectorAll('[data-action="notice-read"]').forEach(button=>{
    const deadline=noticeDeadlines.get(`${CURRENT_BOSS}:${state.me.id}:${button.dataset.id}`);
    if(deadline)button.textContent=`知道了（${Math.max(0,Math.ceil((deadline-Date.now())/1000))} 秒后自动确认）`;
  });
}
setInterval(updateNoticeCountdowns,250);
let renderedStateKey='', pendingRender=false, pointerActive=false, interactionUntil=0;
// Synchronize data immediately; replace the dashboard only after the interaction finishes.
function stateKey(value){const {server_time,...content}=value;const t=Date.now()/1000+clockOffset;return JSON.stringify({...content,spawnPhases:(value.channels||[]).map(c=>!!c.earliest_at&&c.earliest_at<=t)})}
function flushDashboard(){
  if(!state||!pendingRender||document.hidden)return;
  const focused=document.activeElement;
  if(pointerActive||Date.now()<interactionUntil||document.querySelector('dialog[open]')||
    (app.contains(focused)&&focused.matches('input,select,textarea,[contenteditable="true"]')))return;
  const key=stateKey(state);
  if(key===renderedStateKey){pendingRender=false;return}
  // Keep filter values and panel scroll positions when unrelated channel data changes.
  const values=[...app.querySelectorAll('input[id],select[id],textarea[id]')].map(el=>[el.id,el.value]);
  const scrolls=[...app.querySelectorAll('[id]')].filter(el=>el.scrollTop||el.scrollLeft).map(el=>[el.id,el.scrollTop,el.scrollLeft]);
  const disclosures=[...app.querySelectorAll('details[id]')].map(el=>[el.id,el.open]);
  const logPanel=app.querySelector('#logs-panel');
  render();
  // Log browsing is a manual snapshot: live channel updates must not clear its results.
  if(logPanel)app.querySelector('#logs-panel')?.replaceWith(logPanel);
  else if(app.querySelector('#logs-panel'))queueMicrotask(loadLogs);
  for(const [id,value] of values){const el=document.getElementById(id);if(el&&app.contains(el))el.value=value}
  for(const [id,open] of disclosures){const el=document.getElementById(id);if(el&&app.contains(el))el.open=open}
  for(const [id,top,left] of scrolls){const el=document.getElementById(id);if(el){el.scrollTop=top;el.scrollLeft=left}}
  filterMemberRows();
  renderedStateKey=key;pendingRender=false;
}
document.addEventListener('pointerdown',()=>{pointerActive=true;interactionUntil=Date.now()+300},{capture:true});
for(const event of ['pointerup','pointercancel'])document.addEventListener(event,()=>{pointerActive=false;interactionUntil=Date.now()+300},{capture:true});
document.addEventListener('keydown',()=>{interactionUntil=Date.now()+300},{capture:true});
window.addEventListener('blur',()=>{pointerActive=false});
setInterval(flushDashboard,300);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const roleLabel=role=>({superadmin:'超级管理员',admin:'普通管理员',member:'普通成员'}[role]||'未知角色');
const fmt=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'开荒';
const fmtBeijingMinute=t=>t?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(t*1000)):'开荒';
const remaining=t=>{if(!t)return '等待击杀记录';let s=t-Math.floor(Date.now()/1000+clockOffset);if(s<=0)return '00:00:00';return `${Math.floor(s/3600).toString().padStart(2,'0')}:${Math.floor(s%3600/60).toString().padStart(2,'0')}:${(s%60).toString().padStart(2,'0')}`};
function toast(s){let el=document.querySelector('#toast');el.textContent=s;el.style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.style.display='none',3500)}
async function api(path,data,boss=CURRENT_BOSS,options={}){
  const tracksWrite=data!==undefined&&path!=='activity';
  if(tracksWrite)bossWritesPending++;
  const controller=options.controller||new AbortController();
  const timer=setTimeout(()=>controller.abort(),data===undefined?10000:15000);
  try{
    const r=await fetch(APP_BASE+'api/'+path,{method:data===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Boss-Id':boss,...(activeRealm?{'X-Realm-Id':activeRealm}:{}),'X-Page-Visible':!document.hidden&&boss===CURRENT_BOSS?'1':'0'},signal:controller.signal,body:data===undefined?undefined:JSON.stringify(data)});
    let j;try{j=await r.json()}catch(e){if(controller.signal.aborted)throw e;const error=Error('服务器暂时未返回有效结果，请等待状态同步后再操作');error.status=r.status;throw error}
    if(!r.ok){const error=Error(j.error||'请求失败');error.status=r.status;error.code=j.code;error.allowedBosses=j.allowed_bosses;error.realmId=j.realm_id;throw error}return j;
  }catch(e){if(controller.signal.aborted)throw Error(data===undefined?'状态同步超时，正在等待重连':'提交超时，结果尚未确认，请等待状态同步，勿连续重复点击');throw e}
  finally{clearTimeout(timer);if(tracksWrite)bossWritesPending--}
}
let refreshInFlight=null, refreshController=null, refreshOwnerVersion=0;
async function refresh(options={}){
  if(loginPending)return;
  if(refreshInFlight){
    if(!options.afterWrite&&refreshOwnerVersion===refreshVersion)return refreshInFlight;
    // A completed write takes priority over an older read, which can be stalled.
    // Invalidate before aborting so its response/error can never replace fresh state.
    refreshVersion++;refreshController?.abort();
  }
  const version=++refreshVersion;refreshOwnerVersion=version;
  const controller=new AbortController();refreshController=controller;
  const task=(async()=>{
    try{
      const next=await api('state',undefined,CURRENT_BOSS,{controller});if(version!==refreshVersion)return;
      if(next.realm_id)setActiveRealm(next.realm_id);
      const earned=state&&!state.me.is_plus&&next.me.is_plus&&next.me.plus_earned_at;
      state=next;if(earned)toast('连续 5 天达标，已自动升级长期 Plus');
      clockOffset=state.server_time-Date.now()/1000;pendingRender=true;flushDashboard();if(!events)connectEvents();
    }catch(e){
      if(version!==refreshVersion)return;
      if(e.code==='realm_forbidden'&&Object.hasOwn(REALM_NAMES,e.realmId)){
        setActiveRealm(e.realmId);stopEvents();setTimeout(()=>refresh(),0);
      }else if(e.code==='boss_forbidden'){
        state=null;stopEvents();showBossAccessDenied(e.allowedBosses||[]);
      }else if(e.status===401||e.status===403){const wasLoggedIn=!!state;state=null;identity.innerHTML='';showAuth();if(events){events.close();events=null}if(wasLoggedIn)toast(e.message||'登录已失效，请重新登录')}
      else if(!state||options.afterWrite)toast('操作结果正在同步，请勿重复提交；网络恢复后会自动更新');
    }
  })();
  refreshInFlight=task;
  try{await task}finally{if(refreshInFlight===task){refreshInFlight=null;refreshController=null}}
}
function stopEvents(){const stream=events;events=null;stream?.close()}
function setActiveRealm(realm){
  if(!Object.hasOwn(REALM_NAMES,realm)||realm===activeRealm)return;
  activeRealm=realm;
  alertKey=new Set(JSON.parse(localStorage.getItem(alertStorageKey())||'[]'));
  flashUntil.clear();blacklistSelection.clear();
  const url=new URL(location.href);url.searchParams.set('realm',realm);
  history.replaceState(null,'',url.pathname+url.search);
}
function wireRealmHeader(s){
  const realm=s.realm_id||s.me.realm_id||'white_rabbit',name=REALM_NAMES[realm];
  document.title=name+'区 · '+BOSS_NAMES[CURRENT_BOSS]+' · 频道协作';
  identity.insertAdjacentHTML('afterbegin',s.me.role==='superadmin'
    ?`<label class="realm-switch">当前区服<select id="realm-switch" aria-label="管理区服">${Object.entries(REALM_NAMES).map(([id,label])=>`<option value="${id}" ${id===realm?'selected':''}>${label}区</option>`).join('')}</select></label>`
    :`<span class="realm-badge">${name}区</span>`);
  const selector=identity.querySelector('#realm-switch');
  if(selector)selector.onchange=()=>{
    if(bossWritesPending||pendingActions.size){selector.value=realm;toast('操作正在提交，请完成后再切换区服');return}
    const url=new URL(location.href);url.searchParams.set('realm',selector.value);location.assign(url.href);
  };
  for(const link of app.querySelectorAll('.boss-switch a,.cross-boss-link')){
    const url=new URL(link.getAttribute('href'),location.href);url.searchParams.set('realm',realm);link.href=url.href;
  }
}
function connectEvents(){
  if(events||loginPending||!state)return;
  const stream=new EventSource(APP_BASE+'api/events?boss='+CURRENT_BOSS+(activeRealm?'&realm='+encodeURIComponent(activeRealm):''));events=stream;
  stream.addEventListener('update',()=>{if(events===stream)refresh()});
  stream.addEventListener('session-revoked',()=>{
    if(events!==stream)return;
    stopEvents();
    // The stream may carry a cookie from before another tab renewed this login.
    // Only a fresh authenticated request can decide whether to sign out.
    refresh();
  });
  stream.onerror=()=>{
    if(events!==stream)return;
    stopEvents();
    const version=refreshVersion;
    setTimeout(()=>{if(state&&!events&&!loginPending&&version===refreshVersion)refresh()},2500);
  };
}
// Only real user interactions renew idle time; polling and SSE never do.
let lastActivitySent=0, activityInFlight=false;
async function reportActivity(event){
  if(!event.isTrusted||!state||document.hidden||!['member','admin'].includes(state.me.role)||activityInFlight)return;
  if(Date.now()-lastActivitySent<10000)return;
  activityInFlight=true;lastActivitySent=Date.now();
  try{await api('activity',{})}catch(e){if(e.status===401)await refresh();else lastActivitySent=0}
  finally{activityInFlight=false}
}
for(const type of ['click','keydown','input','wheel','touchmove']){
  document.addEventListener(type,reportActivity,{capture:true,passive:true});
}
function showAuth(mode='login'){
  explorationReminderUser=null;explorationReminder.hidden=true;
  renderedStateKey='';pendingRender=false;
  setWatermark('');
  const forms={
    login:`<form id="login-form"><label>登录账号<input name="username" autocomplete="username" required></label><label>密码<input name="password" type="password" autocomplete="current-password" required></label><button class="primary">进入工作台</button></form><button type="button" data-auth="reset">已有重置密钥？设置新密码</button><p class="muted tiny">新注册账号需超级管理员审批通过后才能登录。</p>`,
    register:`<form id="register-form"><fieldset class="realm-picker"><legend>选择区服</legend><div class="realm-options"><label><input type="radio" name="realm_id" value="white_rabbit" checked required><span>小白兔区</span></label><label><input type="radio" name="realm_id" value="red_crab" required><span>红螃蟹区</span></label></div><p class="hint">请选择游戏角色所在区服，登录后自动进入该区。</p></fieldset><label>登录账号<input name="username" autocomplete="username" pattern="[A-Za-z0-9_-]{3,30}" minlength="3" maxlength="30" required></label><label>显示昵称<input name="nickname" maxlength="30" required></label><label>密码（9～128 位）<input name="password" type="password" autocomplete="new-password" minlength="9" maxlength="128" required></label><label>确认密码<input name="confirm_password" type="password" autocomplete="new-password" minlength="9" maxlength="128" required></label><button type="submit" class="primary">提交注册申请</button></form><p class="muted tiny">超级管理员审批时选择可访问的 Boss；通过后会员有效期至 2099 年。无需邮箱、手机号或验证码；每个公网 IP 仅限注册一个普通账号，共用网络请联系管理员。</p>`,
    reset:`<form id="reset-form"><label>登录账号<input name="username" autocomplete="username" required></label><label>恢复码或管理员提供的重置密钥<input name="recovery_code" autocomplete="off" required></label><label>设置新密码（9～128 位）<input name="new_password" type="password" autocomplete="new-password" minlength="9" maxlength="128" required></label><label>确认新密码<input name="confirm_password" type="password" autocomplete="new-password" minlength="9" maxlength="128" required></label><button type="submit" class="primary">设置新密码</button></form><p class="muted tiny">忘记密码时可使用自己保存的恢复码，或联系超级管理员获取一次性重置密钥。设置成功后请用新密码登录并保存网站发给你的新恢复码。</p>`
  };
  app.innerHTML=`<section class="auth panel"><p class="eyebrow">TEAM TIMER / 60 CHANNELS</p><h1>${mode==='register'?'加入频道协作':mode==='reset'?'找回账号':'一起守住每次刷新。'}</h1><p class="muted">${mode==='login'?'记录击杀、认领频道、准时支援。频道信息按角色严格隔离。':'请妥善保存账号和恢复码。'}</p><nav class="auth-tabs" aria-label="账号操作"><button type="button" data-auth="login" class="${mode==='login'?'selected':''}">登录</button><button type="button" data-auth="register" class="${mode==='register'?'selected':''}">注册</button><button type="button" data-auth="reset" class="${mode==='reset'?'selected':''}">忘记密码</button></nav>${forms[mode]}</section>`;
  wireAuth();
}
function showBossAccessDenied(allowed){
  renderedStateKey='';pendingRender=false;setWatermark('');identity.innerHTML='';
  const links=allowed.filter(boss=>Object.hasOwn(BOSS_NAMES,boss)).map(boss=>`<a class="primary boss-access-link" href="?boss=${boss}">前往${BOSS_NAMES[boss]}</a>`).join('');
  app.innerHTML=`<section class="auth panel boss-access-denied"><p class="eyebrow">BOSS ACCESS</p><h1>尚未开通${BOSS_NAMES[CURRENT_BOSS]}权限</h1><p class="muted">请联系超级管理员开通。当前可访问：</p><div class="actions">${links||'<span>暂未开通 Boss 权限</span>'}</div><button type="button" id="boss-denied-logout">退出当前账号</button></section>`;
  app.querySelector('#boss-denied-logout').onclick=async()=>{try{await api('logout',{})}catch{}showAuth()};
}
function showOneTimeSecret(title,description,secret,copyLabel){
  const dialog=document.createElement('dialog');dialog.className='recovery-dialog';
  dialog.innerHTML='<h2></h2><p></p><input readonly><div class="actions"><button type="button" class="primary" data-copy></button><button type="button" data-close>我已保存</button></div>';
  dialog.querySelector('h2').textContent=title;
  dialog.querySelector('p').textContent=description;
  dialog.querySelector('input').value=secret;
  dialog.querySelector('input').setAttribute('aria-label',title);
  dialog.querySelector('[data-copy]').textContent=copyLabel;
  dialog.querySelector('[data-copy]').onclick=async()=>{try{await navigator.clipboard.writeText(secret);toast('已复制')}catch{dialog.querySelector('input').select();toast('请手动复制')}};
  dialog.querySelector('[data-close]').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();
}
function showRecoveryCode(code,notice=''){showOneTimeSecret('请保存新恢复码',`${notice}恢复码仅显示这一次。忘记密码时需要登录账号和恢复码；每次使用或重新生成后，旧码立即失效。`,code,'复制恢复码')}
function showChangePassword(){
  const dialog=document.createElement('dialog');dialog.className='recovery-dialog';
  dialog.innerHTML='<form><h2>账号安全</h2><label>当前密码<input name="current_password" type="password" autocomplete="current-password" required></label><p class="muted tiny">旧账号没有恢复码或已经遗失？输入当前密码，重新生成一个。</p><button type="button" data-issue-recovery>生成新恢复码</button><hr><label>新密码（9～128 位）<input name="new_password" type="password" autocomplete="new-password" minlength="9" maxlength="128" required></label><label>确认新密码<input name="confirm_password" type="password" autocomplete="new-password" minlength="9" maxlength="128" required></label><p class="muted tiny">修改密码后所有设备会退出登录，并生成新的恢复码。</p><div class="actions"><button class="primary">保存新密码</button><button type="button" data-close>取消</button></div></form>';
  dialog.querySelector('[data-close]').onclick=()=>dialog.close();
  dialog.querySelector('[data-issue-recovery]').onclick=async()=>{const input=dialog.querySelector('[name=current_password]');if(!input.reportValidity())return;const button=dialog.querySelector('[data-issue-recovery]');button.disabled=true;try{const result=await api('password/recovery',{current_password:input.value});dialog.close();showRecoveryCode(result.recovery_code)}catch(x){toast(x.message);button.disabled=false}};
  dialog.querySelector('form').onsubmit=async e=>{e.preventDefault();const values=Object.fromEntries(new FormData(e.currentTarget));if(values.new_password!==values.confirm_password){toast('两次输入的新密码不一致');return}delete values.confirm_password;try{const result=await api('password/change',values);dialog.close();refreshVersion++;state=null;identity.innerHTML='';events?.close();events=null;showAuth();showRecoveryCode(result.recovery_code);toast('密码已修改，请重新登录')}catch(x){toast(x.message)}};
  dialog.addEventListener('close',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();
}
function wireAuth(){
  app.querySelectorAll('[data-auth]').forEach(button=>button.onclick=()=>showAuth(button.dataset.auth));
  const login=app.querySelector('#login-form');if(login)login.onsubmit=async e=>{
    e.preventDefault();if(loginPending)return;
    loginPending=true;refreshVersion++;stopEvents();armAudio();
    const button=login.querySelector('button');if(button)button.disabled=true;
    try{
      const result=await api('login',Object.fromEntries(new FormData(login)));
      if(result.realm_id)setActiveRealm(result.realm_id);
      if(!result.allowed_bosses?.includes(CURRENT_BOSS)){
        loginPending=false;
        showBossAccessDenied(result.allowed_bosses||[]);
        if(result.recovery_code)showRecoveryCode(result.recovery_code);
        return;
      }
      loginPending=false;await refresh();
      if(result.recovery_code)showRecoveryCode(result.recovery_code);
    }catch(x){toast(x.message)}
    finally{loginPending=false;if(button)button.disabled=false}
  };
  const register=app.querySelector('#register-form');if(register)register.onsubmit=async e=>{
    e.preventDefault();
    const button=register.querySelector('button[type="submit"]');
    if(button.disabled)return;
    const values=Object.fromEntries(new FormData(register));
    if(values.password!==values.confirm_password){toast('两次输入的密码不一致');return}
    delete values.confirm_password;
    button.disabled=true;button.textContent='正在提交…';
    try{
      const result=await api('register',values);
      showAuth();showRecoveryCode(result.recovery_code,`${REALM_NAMES[result.realm_id]}区注册申请已提交；超级管理员审批通过后即可登录。`);
    }catch(x){toast(x.message)}
    finally{button.disabled=false;button.textContent='提交注册申请'}
  };
  const reset=app.querySelector('#reset-form');if(reset)reset.onsubmit=async e=>{
    e.preventDefault();
    const button=reset.querySelector('button[type="submit"]');
    if(button.disabled)return;
    const values=Object.fromEntries(new FormData(reset));
    if(values.new_password!==values.confirm_password){toast('两次输入的新密码不一致');return}
    delete values.confirm_password;
    button.disabled=true;button.textContent='正在设置…';
    try{
      const result=await api('password/reset',values);
      showAuth();showRecoveryCode(result.recovery_code);toast('新密码已设置，请重新登录');
    }catch(x){toast(x.message)}
    finally{button.disabled=false;button.textContent='设置新密码'}
  };
}
function channelNoteButton(c){
  const note=state?.channels.find(channel=>channel.id===c.id)?.note||'';
  return `<button type="button" class="channel-note-button ${note?'has-note':''}" data-action="note" data-id="${c.id}" title="${esc(note||'记录这条线常来的对手')}">${note?'备注 · '+esc(note):'＋ 备注'}</button>`;
}
const noteDialog=document.createElement('dialog');
noteDialog.className='channel-note-dialog';document.body.append(noteDialog);
function openChannelNote(id,view=state,boss=CURRENT_BOSS){
  const channel=view?.channels.find(c=>c.id===id);if(!channel)return;
  const original=channel.note||'';
  noteDialog.innerHTML=`<form><h2>${String(id).padStart(2,'0')} 频道备注</h2><p class="muted tiny">记录常来的对手，所有成员可见。</p><label>对手提醒<input name="note" maxlength="200" value="${esc(original)}" placeholder="例如：某玩家常来此线"></label><div class="actions"><button type="button" id="cancel-note">取消</button><button type="submit" class="primary">保存备注</button></div></form>`;
  noteDialog.querySelector('#cancel-note').onclick=()=>noteDialog.close();
  noteDialog.querySelector('form').onsubmit=async e=>{e.preventDefault();const button=e.currentTarget.querySelector('[type="submit"]');button.disabled=true;try{await api('channel-note',{channel_id:id,note:String(new FormData(e.currentTarget).get('note')),expected_note:original},boss);noteDialog.close();await refresh();toast('频道备注已保存')}catch(error){button.disabled=false;toast(error.message)}};
  noteDialog.showModal();
}
function timerCard(c,kind,view=state){
  if(!c)return `<div class="timer-empty"><span class="empty-icon">◇</span><strong>暂无推荐频道</strong><span>可从下方开荒频道选择并认领</span></div>`;
  const parallel=Boolean(view?.concurrent?.some(held=>held.id===c.id));
  const current=parallel||Boolean(view?.own?.some(held=>held.id===c.id)), recent=kind==='上次击杀';
  if(current&&!c.killed_at)return explorationCard(c,view);
  const time=Date.now()/1000+clockOffset;
  const status=c.latest_at&&time>=c.latest_at?'late':c.earliest_at&&time>=c.earliest_at?'ready':'waiting';
  const statusText={late:'已到保底时间',ready:'已到进刷时间',waiting:c.killed_at?'等待进刷':'等待击杀'}[status];
  const countdownTarget=current?c.latest_at:c.earliest_at;
  const countdownLabel=current?'距离保底时间':'距离进刷时间';
  const lastKillHere=view?.last_kill?.id===c.id;
  const canClaim=!view?.own.length||Boolean(c.killed_at&&view.own[0]?.killed_at==null&&(view.explorations||[]).length<3);
  const sosState=current&&c.sos_at?`<div class="sos-pending" role="status" title="SOS 发起时间：${esc(fmt(c.sos_at))}"><strong>SOS 已发出 · 等待接管</strong><span>${new Date(c.sos_at*1000).toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})}</span></div>`:'';
  const claimable=canClaim&&(!recent||view?.last_kill_claimable);
  const mainAction=`<button class="primary" data-action="${claimable?'claim':'preview'}" data-id="${c.id}">${claimable?'普通认领':'查看频道概要'}</button>`;
  let actions=current?`<button class="action-kill" data-action="kill" data-id="${c.id}">确认击杀</button><button class="action-robbed" data-action="robbed" data-id="${c.id}">被抢 · 登记玩家</button><button class="danger" data-action="${c.sos_at?'cancel-sos':'sos'}" data-id="${c.id}">${c.sos_at?'取消 SOS':'SOS 求救'}</button><button data-action="release" data-id="${c.id}">释放频道</button><button class="discard-action" data-action="discard" data-id="${c.id}">丢弃频道 · 转开荒</button>`:recent?`${mainAction}${c.can_edit_kill?`<button data-action="edit" data-id="${c.id}">修改击杀时间</button>`:''}`:`${mainAction}${lastKillHere&&c.can_edit_kill?`<button data-action="edit" data-id="${c.id}">修改上次击杀</button>`:''}`;
  if(parallel){
    const r=(view.relays||[]).find(r=>r.channel_id===c.id&&r.owner_id===view.me.id);
    const pending=r&&['open','travel'].includes(r.state);
    actions=`<button class="action-kill" data-action="kill" data-id="${c.id}">确认击杀</button><button class="action-robbed" data-action="robbed" data-id="${c.id}">被抢 · 登记玩家</button>${relayButton(pending?'cancel':'invite',c.id,r,pending?'撤销接力':'邀请接力认领')}<button data-action="release" data-id="${c.id}">释放频道</button><button class="discard-action" data-action="discard" data-id="${c.id}">丢弃频道 · 转开荒</button>`;
    if(pending)actions+=`<p class="parallel-handoff-hint">${r.state==='travel'?esc(r.receiver_name)+'前往中，等待到达':'邀请已发出，等待接手'}</p>`;
    else actions+=`<p class="parallel-handoff-hint" data-primary-spawn="${view.own[0]?.earliest_at||0}"></p>`;
  }else if(!current){
    const live=view.channels.find(x=>x.id===c.id);
    const ready=live&&!live.claimant_id&&!live.explorer_id&&live.blocked_until<=time&&live.killed_at&&live.earliest_at<=time;
    if(ready){
      const blocked=view.cooperation?.parallel_blocked||(view.concurrent||[]).length||(view.reserved_slots||[]).includes('parallel_id')||view.claim_blocked_until>time;
      if(view.own.length)actions+=`<button class="parallel-claim" data-action="claim-parallel" data-id="${c.id}" data-cycle="${c.killed_at}" ${blocked?'disabled':''}>${view.cooperation?.parallel_blocked?'协作提醒 · 并发暂缓':(view.concurrent||[]).length?'并发名额已满':'并发认领'}</button>`;
      actions+=`<button class="discard-action" data-action="discard-ready" data-id="${c.id}" data-cycle="${c.killed_at}">丢弃 · 转开荒</button>`;
    }
  }
  return `<article class="timer-card ${current||(actions.match(/<button\b/g)||[]).length>2?'compact-timer-card':''} ${status} ${parallel?'parallel-card':''}" data-channel="${c.id}">${channelNoteButton(c)}<div class="timer-top"><span class="timer-kind">${kind}</span><span class="timer-status"><i></i><span class="timer-status-text">${statusText}</span></span></div><div class="timer-number"><span>频道</span><button class="timer-channel-link" type="button" data-action="preview" data-id="${c.id}" aria-label="查看 ${c.id} 频道概要">${c.id.toString().padStart(2,'0')}</button></div><div class="timer-caption">${countdownLabel}</div><div class="timer-clock countdown ${current?'guarantee-clock':'spawn-clock'}" data-time="${countdownTarget||''}">${esc(remaining(countdownTarget))}</div><div class="timer-window"><span>进刷 — 保底</span><strong>${c.killed_at?`${new Date(c.earliest_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false})} — ${new Date(c.latest_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false})}`:'等待击杀记录'}</strong></div><div class="timer-kill">上次击杀 · ${fmt(c.killed_at)}</div>${c.sos_from_name?`<p class="timer-caption" style="overflow-wrap:anywhere">救援接手自：${esc(c.sos_from_name)}</p>`:''}${sosState}<div class="timer-actions">${actions}</div></article>`;
}
function explorationCard(c,view=state){
  const extra=(view.explorations||[]).some(x=>x.id===c.id);
  const r=(view.relays||[]).find(r=>r.channel_id===c.id&&r.owner_id===view.me.id);
  const claimed=c.exploration_claimed_at||c.claimed_at,started=r?.started_at||claimed;
  const pending=r&&['open','travel'].includes(r.state);
  return `<article class="timer-card exploration-card" data-channel="${c.id}">${channelNoteButton(c)}<div class="timer-top"><span class="timer-kind">${extra?'额外开荒':'常规认领 · 开荒'}</span><span class="relay-status">${r?.state==='travel'?'队友前往中':r?.state==='open'?'等待接力':r?.relay_count?'接力中':'蹲守中'}</span></div><div class="timer-number"><span>开荒频道</span><button data-action="preview" data-id="${c.id}">${String(c.id).padStart(2,'0')}</button></div><p class="timer-caption">本轮已登记蹲守</p><strong class="relay-clock" data-elapsed="${started}">${elapsedRelay(started)}</strong><p class="hint">${r?.previous_owner_name?`接手自 ${esc(r.previous_owner_name)} · `:''}我的本次蹲守 <span data-elapsed="${claimed}">${elapsedRelay(claimed)}</span></p>${r?.state==='travel'?`<p class="relay-travel">${esc(r.receiver_name)}正在赶来<br><small>等待对方确认到达，你仍持有频道</small></p>`:r?.state==='open'?'<p class="relay-travel">邀请已开放，接手成功后自动交接</p>':'<p class="hint">蹲守时长不代表进刷或保底倒计时</p>'}<div class="timer-actions"><button class="primary" data-action="${extra?'finish-exploration':'edit'}" data-id="${c.id}">已击杀，记录时间</button>${relayButton(pending?'cancel':'invite',c.id,r,pending?'撤销邀请':'邀请接力')}<button data-action="end-explore" data-id="${c.id}">结束本次开荒</button></div></article>`;
}
let relayExpanded=false;
function elapsedRelay(start){const seconds=Math.max(0,Math.floor(Date.now()/1000+clockOffset-Number(start)));return [Math.floor(seconds/3600),Math.floor(seconds%3600/60),seconds%60].map(n=>String(n).padStart(2,'0')).join(':')}
function relayButton(action,id,row,label,disabled=false){return `<button type="button" data-relay="${action}" data-relay-boss="${row?.boss||CURRENT_BOSS}" data-id="${id}" data-request="${esc(row?.request_token||'')}" ${action==='arrive'?`data-arrival="${row.arrival_until}"`:''} ${disabled?'disabled':''}>${label}</button>`}
function supportSosPanel(s){
  const rows=s.cross_support?.sos||[];
  return `<section class="panel sos-section ${rows.length?'':'is-empty'}"><div class="section-heading"><h2>SOS 求救 · 跨 Boss</h2><span class="count-badge">${rows.length}</span></div><div class="sos-card-grid">${rows.map(r=>`<article class="sos-card"><strong>${esc(BOSS_NAMES[r.boss])}</strong><p class="sos-requester" style="overflow-wrap:anywhere">求救者：${esc(r.requester_name||'未知成员')}</p><div class="sos-card-times"><div><span>进刷</span><strong>${fmtBeijingMinute(r.earliest_at)}</strong></div><div><span>最终保底</span><strong>${fmtBeijingMinute(r.latest_at)}</strong></div></div><button type="button" data-support-sos="${esc(r.request_id)}" data-support-boss="${r.boss}" ${r.accept_reason?'disabled':''}>救援 ${esc(BOSS_NAMES[r.boss])} · ${String(r.channel_id).padStart(2,'0')} 线</button>${r.accept_reason?`<p class="hint">${esc(r.accept_reason)}</p>`:''}</article>`).join('')||'<p class="empty">目前没有求救申请</p>'}</div>${rows.length?'<p class="hint">接取指定频道；仅释放求救所属 Boss 的原常规认领，不影响其他 Boss。</p>':''}</section>`;
}
function wireSupportSos(){
  app.querySelectorAll('[data-support-sos]').forEach(b=>b.onclick=async()=>{
    if(b.disabled)return;
    const boss=b.dataset.supportBoss;
    const r=state.cross_support?.sos.find(x=>x.boss===boss&&x.request_id===b.dataset.supportSos);
    if(!r){toast('求救状态已变化，请刷新');return}
    if(r.held_channel&&!confirm(`接受救援将自动释放你在${BOSS_NAMES[boss]}的 ${String(r.held_channel).padStart(2,'0')} 线，不产生冷却。确认接管？`))return;
    b.disabled=true;
    try{await action('accept-sos',{request_id:r.request_id},`${BOSS_NAMES[boss]}救援已接管`,boss)}finally{if(b.isConnected)b.disabled=false}
  });
}
function relayPanel(s){
  const mine=r=>r.owner_id===s.me.id||r.receiver_id===s.me.id;
  const rows=[...(s.relays||[]).map(r=>({...r,boss:CURRENT_BOSS})),...(s.cross_support?.relays||[])].filter(r=>r.state!=='idle').sort((a,b)=>Number(mine(b))-Number(mine(a))||a.started_at-b.started_at);
  if(!rows.length)return '';
  return `<section class="panel relay-panel"><div class="section-heading"><div><p class="eyebrow">EXPLORATION RELAY</p><h2>频道接力 · ${rows.length}</h2></div>${rows.length>2?`<button id="relay-expand">${relayExpanded?'收起':'展开全部'}</button>`:''}</div><p class="hint">接受后预留名额；请在 3 分钟内到达游戏频道，再点“我已到达”完成交接。</p><div class="relay-grid">${(relayExpanded?rows:rows.slice(0,2)).map(r=>{
    const receiver=r.receiver_id===s.me.id,owner=r.owner_id===s.me.id;
    let buttons='',detail='';
    if(receiver){detail=`<p class="relay-travel">前往中 · 尚未接手<br>到达剩余 <b class="countdown" data-time="${r.arrival_until}">${remaining(r.arrival_until)}</b></p><p class="hint">已预留 1 个${r.slot==='claimant_id'?'常规':r.slot==='parallel_id'?'并发':'额外开荒'}名额，请先在游戏内到达此频道。</p>`;buttons=relayButton('arrive',r.channel_id,r,'我已到达',r.arrival_until<=Date.now()/1000+clockOffset)+relayButton('leave',r.channel_id,r,'取消前往')}
    else if(owner){detail=r.state==='travel'?`<p class="relay-travel">${esc(r.receiver_name)}正在赶来 · <span class="countdown" data-time="${r.arrival_until}">${remaining(r.arrival_until)}</span></p>`:'<p class="hint">等待队友接受，你仍持有此频道</p>';buttons=relayButton('cancel',r.channel_id,r,'撤销邀请')}
    else if(r.state==='travel'){detail=`<p class="hint">${esc(r.receiver_name)}已接受，正在前往</p>`}
    else{detail=`<p class="hint">${esc(r.accept_reason||`接手将使用 1 个${r.accept_slot==='claimant_id'?'常规':r.accept_slot==='parallel_id'?'并发':'额外开荒'}名额`)}</p>`;buttons=relayButton('accept',r.channel_id,r,'接受邀请',!!r.accept_reason)}
    if(s.me.role==='superadmin'&&!owner)buttons+=relayButton('cancel',r.channel_id,r,'管理员撤销');
    return `<article class="relay-item ${receiver?'is-travel':''}"><div class="section-heading"><strong>${esc(BOSS_NAMES[r.boss])} · ${String(r.channel_id).padStart(2,'0')} <small>${r.kind==='parallel'?'并发接力':'开荒接力'}</small></strong><span>${esc(r.owner_name)} 发起</span></div>${r.kind==='parallel'?`<p>距保底 <b class="countdown guarantee-clock" data-time="${r.latest_at}">${remaining(r.latest_at)}</b> · 已接力 ${r.relay_count} 次</p>`:`<p>累计蹲守 <b data-elapsed="${r.started_at}">${elapsedRelay(r.started_at)}</b> · 已接力 ${r.relay_count} 次</p>`}${r.note?`<p class="hint">备注：${esc(r.note)}</p>`:''}${detail}<div class="actions">${buttons}</div></article>`;
  }).join('')}</div></section>`;
}
async function handleRelay(e){
  const button=e.currentTarget;if(button.disabled)return;button.disabled=true;
  const operation=button.dataset.relay;
  try{await api('relay/'+operation,{channel_id:Number(button.dataset.id),request_id:button.dataset.request},button.dataset.relayBoss||CURRENT_BOSS);await refresh({afterWrite:true});toast({invite:'接力邀请已发出',accept:'已预留名额，请前往游戏频道并确认到达',arrive:'到达已确认，接力交接成功',leave:'已取消前往，邀请重新开放',cancel:'接力邀请已撤销'}[operation])}
  catch(error){toast(error.message);await refresh()}
  finally{if(button.isConnected)button.disabled=false}
}
function spawnPoolCard(c){
  const time=Date.now()/1000+clockOffset;
  const ready=c.earliest_at&&time>=c.earliest_at;
  const clock=t=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(t*1000));
  return `<button type="button" class="spawn-pool-card ${ready?'ready':''}" data-action="preview" data-id="${c.id}" aria-label="查看并认领 ${c.id} 频道"><span class="pool-card-top"><strong>${c.id.toString().padStart(2,'0')} <small>频道</small></strong><em>${ready?'已进刷':'即将进刷'}</em></span><span class="pool-card-count">距离进刷 <b class="countdown" data-time="${c.earliest_at}">${esc(remaining(c.earliest_at))}</b></span><span class="pool-card-window">进刷 ${clock(c.earliest_at)} · 保底 ${clock(c.latest_at)}</span></button>`;
}
function openingRecordMode(c,s){
  if(c.killed_at)return null;
  if(c.explorer_id===s.me.id)return 'finish-exploration';
  if(c.explorer_id)return null;
  const ownClaim=c.claimant_id===s.me.id;
  if(c.claimant_id&&!ownClaim)return null;
  const now=Date.now()/1000+clockOffset;
  if(s.me.role!=='superadmin'&&!ownClaim&&(c.blocked_until>now||s.me.banned_until>now))return null;
  return 'edit';
}
function adminChannelIndex(s){
  const channels=[...s.channels].sort((a,b)=>a.id-b.id);
  return `<section class="panel span12 channel-index"><div class="section-heading"><div><p class="eyebrow">ALL CHANNELS</p><h2>全部频道 · ${channels.length}/60</h2></div><span class="section-note">空闲频道可认领；已有常规认领时，可再认领最多 3 条开荒频道</span></div><div class="admin-channel-grid">${channels.map(c=>{const blocked=c.blocked_until>Date.now()/1000+clockOffset;const status=blocked?'临时拉黑':c.explorer_id?'开荒中':c.claimant_id?'已认领':c.killed_at?'待认领':'开荒';const cls=blocked?'blocked':c.explorer_id||c.claimant_id?'occupied':c.killed_at?'known':'unknown';const recordMode=openingRecordMode(c,s);const owner=c.owner_nickname;return `<div class="admin-channel-tile"><button type="button" class="admin-channel ${cls}" data-action="preview" data-id="${c.id}" aria-label="查看 ${c.id} 频道，${status}${owner?'，认领人 '+esc(owner):''}"><strong>${c.id.toString().padStart(2,'0')}</strong><small>${status}</small>${owner?`<span class="admin-channel-owner" title="认领人：${esc(owner)}">认领 · ${esc(owner)}</span>`:''}<span class="admin-channel-manage">查看 / 操作 ↗</span></button>${c.killed_at&&c.can_edit_kill?`<button type="button" class="channel-index-edit" data-action="edit" data-id="${c.id}" aria-label="修改 ${c.id} 频道击杀时间">修改时间</button>`:recordMode?`<button type="button" class="channel-index-edit" data-action="${recordMode}" data-id="${c.id}" aria-label="录入 ${c.id} 频道击杀时间">录入时间</button>`:'<span class="channel-index-noedit">暂不可录入</span>'}</div>`}).join('')}</div></section>`;
}
function adminBlacklistHtml(s){
  const t=Date.now()/1000+clockOffset;
  const blocked=s.channels.filter(c=>c.blocked_until>t);
  return `<section class="panel span12 blacklist-panel"><div class="section-heading"><div><p class="eyebrow">CHANNEL ACCESS</p><h2>批量临时拉黑</h2></div><span class="count-badge">当前 ${blocked.length} 条</span></div><p class="hint">选择频道和时长后批量拉黑；到期自动恢复。已有认领可继续完成，但拉黑期间不可新认领、改派或接援。</p><div class="blacklist-toolbar"><button type="button" id="blacklist-select-active">选择已拉黑</button><button type="button" id="blacklist-clear-selection">清空选择</button><label>拉黑时长 <select id="blacklist-duration"><option value="10">10 分钟</option><option value="30">30 分钟</option><option value="60" selected>1 小时</option><option value="180">3 小时</option><option value="1440">1 天</option><option value="10080">7 天</option><option value="43200">30 天</option></select></label><button type="button" class="primary" id="blacklist-apply">批量拉黑</button><button type="button" id="blacklist-unblock">解除拉黑</button><strong id="blacklist-count">已选 ${blacklistSelection.size} 条</strong></div><div class="blacklist-grid">${s.channels.map(c=>{const active=c.blocked_until>t, selected=blacklistSelection.has(c.id);return `<button type="button" class="blacklist-channel ${active?'is-blocked':''} ${selected?'is-selected':''}" data-blacklist-id="${c.id}" aria-pressed="${selected}" title="${active?'拉黑至 '+fmt(c.blocked_until):'可认领'}"><strong>${String(c.id).padStart(2,'0')}</strong><small>${active?'至 '+fmtBeijingMinute(c.blocked_until):'可用'}</small></button>`}).join('')}</div></section>`;
}
function memberActivityHtml(member){
  if(!member.online||(!member.activity_by_boss&&!member.channel_activity))return '';
  const labels={claimed:'认领中',exploring:'开荒中'};
  const holdings=member.activity_by_boss||{[CURRENT_BOSS]:member.channel_activity};
  const badges=Object.keys(BOSS_NAMES).flatMap(boss=>Object.entries(labels).flatMap(([kind,label])=>{
    const ids=(holdings[boss]?.[kind]||[]).map(Number).filter(id=>Number.isInteger(id)&&id>0);
    return ids.length?[`<span class="member-activity ${kind}" data-boss="${boss}" title="${BOSS_NAMES[boss]} · ${label}：${ids.map(id=>String(id).padStart(2,'0')).join('、')} 线">${BOSS_NAMES[boss]} · ${label} ${ids.length}</span>`]:[];
  }));
  return badges.join('')||'<span class="member-activity waiting" title="你可查看的 Boss 下没有持有频道">等待中</span>';
}
function presenceMemberHtml(member){
  return `<div class="presence-member"><span class="presence-dot"></span><strong title="${esc(member.nickname)}">${esc(member.nickname)}</strong>${memberActivityHtml(member)}<span class="presence-kills">今日击杀 <b>${Number(member.kills)||0}</b></span></div>`;
}
function presenceHtml(s){
  const bossFlag=BOSS_FLAGS[CURRENT_BOSS];
  const members=(s.members||[]).filter(member=>['member','admin'].includes(member.role)&&(Number(member.boss_access)&bossFlag));
  const group=(online)=>{
    const people=members.filter(m=>online?Boolean(m.online)&&m.recent_boss_kill:!m.online);
    return `<div class="presence-group ${online?'online':'offline'}"><div class="presence-label"><strong>${online?'在线成员':'离线成员'}</strong><span>${people.length} 人</span></div><div class="presence-list">${people.map(presenceMemberHtml).join('')||`<p class="empty">${online?'近 60 分钟暂无击杀成员':'当前没有离线成员'}</p>`}</div></div>`;
  };
  return `<section class="panel presence-panel" aria-label="成员在线状态与击杀数量"><div class="section-heading"><div><p class="eyebrow">TEAM STATUS</p><h2>成员在线状态</h2></div><span class="section-note">在线：近 60 分钟击杀且近 30 分钟有操作 · 离线：当前不在线的本 Boss 成员 · 今日击杀仅统计当前 Boss</span></div><p class="hint presence-activity-hint">标签按各 Boss 主题色区分持有状态。只显示你有权限查看的 Boss；没有持有时显示等待中。</p><div class="presence-groups">${group(true)}${group(false)}</div></section>`;
}
function rosterHtml(s){
  const claimed=new Map(s.channels.filter(c=>c.claimant_id&&!c.is_parallel).map(c=>[c.claimant_id,c.id]));
  const exploring=new Map();
  for(const channel of s.channels.filter(c=>c.explorer_id))exploring.set(channel.explorer_id,[...(exploring.get(channel.explorer_id)||[]),channel.id]);
  const groups=[
    ['待审批','pending',s.users.filter(u=>u.pending_approval)],
    ['管理团队','manager',s.users.filter(u=>u.role==='superadmin')],
    ['当前认领','claimed',s.users.filter(u=>['member','admin'].includes(u.role)&&!u.pending_approval&&claimed.has(u.id))],
    ['其他成员','member',s.users.filter(u=>['member','admin'].includes(u.role)&&!u.pending_approval&&!claimed.has(u.id))]
  ];
  return `<section class="roster panel"><div class="section-heading"><div><p class="eyebrow">TEAM OVERVIEW</p><h2>队伍状态</h2></div><span class="section-note">仅超级管理员可见 · ${s.users.length} 位成员</span></div>${groups.map(([title,cls,users])=>`<div class="roster-group ${cls}"><div class="roster-label"><span>${title}</span><small>${users.length}</small></div><div class="roster-list">${users.length?users.map(u=>`<span class="member-pill ${u.frozen||u.banned_until>Date.now()/1000||u.access_until<Date.now()/1000?'inactive':''}"><span class="pill-dot"></span><span>${esc(u.nickname)}</span>${claimed.has(u.id)?`<b>${claimed.get(u.id)} 频道</b>`:''}${exploring.has(u.id)?`<b>开荒 ${exploring.get(u.id).join('、')} 频道</b>`:''}</span>`).join(''):'<span class="roster-empty">暂无成员</span>'}</div></div>`).join('')}</section>`;
}
function existingWeeklyRankingsHtml(s){
  const week=s.weekly_rankings||{exploration:[],rescue:[],robbery:[]};
  const day=t=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit'}).format(new Date(t*1000));
  const period=week.starts_at?`${day(week.starts_at)} · 00:00 起`:'今日';
  const list=(rows,nameKey,countKey,empty)=>rows.length?`<ol class="weekly-rank-list">${rows.map((row,i)=>`<li class="${row.user_id===s.me.id?'is-me':''}"><span class="weekly-rank-position">${i+1}</span><strong title="${esc(row[nameKey])}">${esc(row[nameKey])}</strong><b>${row[countKey]} 次</b></li>`).join('')}</ol>`:`<p class="weekly-rank-empty">${empty}</p>`;
  return `<aside class="weekly-rankings" aria-label="今日排行榜"><div class="weekly-rank-period"><strong>今日排名</strong><span>北京时间 ${period}</span></div><section class="panel weekly-rank-card"><p class="eyebrow">EXPLORATION</p><h3>开荒数量</h3><p class="hint">开荒频道完成击杀后计入 · 仅统计今日完成的击杀</p>${list(week.exploration,'nickname','total','今日暂无开荒击杀')}</section><section class="panel weekly-rank-card"><p class="eyebrow">SOS RESCUES</p><h3>SOS 救援</h3><p class="hint">成功接管求救计入</p>${list(week.rescue,'nickname','total','今日暂无成功救援')}</section></aside>`;
}
function noticeHtml(n){
  const channel=String(n.channel_id).padStart(2,'0');
  if(n.kind.startsWith('relay_')){
    if(dismissedNotices.has(`${CURRENT_BOSS}:${state.me.id}:${n.id}`))return '';
    const messages={relay_completed:'队友已到达，频道已交接；本次释放不冻结。',relay_received:'已确认到达，频道已加入我的认领，原计时记录已保留。',relay_cancelled:'接力已取消，预留名额已释放。',relay_reopened:'前往超时、取消或状态变化，邀请已重新开放，原成员仍持有频道。'};
    return `<div class="release-notice" data-notice-id="${n.id}" role="status"><div><strong>${channel} 频道 · 接力通知</strong><p>${messages[n.kind]||'接力状态已更新'} ${fmt(n.created_at)}</p></div><button data-action="notice-read" data-id="${n.id}">知道了</button></div>`;
  }
  const title=n.kind==='admin_assign'?`管理员已分配给你 ${channel} 频道`:n.kind==='admin_reassign'?`你的 ${channel} 频道已被管理员改派`:n.kind==='sos_accepted'?`${channel} 频道救援已被接管`:n.kind==='guarantee_expired'?`${channel} 频道已到期，需重新开荒`:`管理员已释放你的 ${channel} 频道`;
  const detail=n.kind==='admin_assign'?'该频道已加入“我的认领”，可以开始计时和记录击杀。':n.kind==='admin_reassign'?'原认领已结束，频道已分配给其他成员；本次不增加认领冷却。':n.kind==='sos_accepted'?'队友已经接手，你的认领已结束；本次交接没有主动释放冷却。':n.kind==='guarantee_expired'?'超过最终保底宽限时间，系统已释放并转为开荒频道；本次不增加主动释放冷却。':'该频道已从“我的认领”移除；本次释放不增加认领冷却。';
  if(dismissedNotices.has(`${CURRENT_BOSS}:${state.me.id}:${n.id}`))return '';
  return `<div class="release-notice" data-notice-id="${n.id}" role="status"><div><strong>${title}</strong><p>${detail} ${fmt(n.created_at)}</p></div><button type="button" data-action="notice-read" data-id="${n.id}">知道了</button></div>`;
}
function releaseCooldownHint(boss=CURRENT_BOSS){
  if(state.me.role==='superadmin')return '管理员释放不冻结。';
  return boss==='zombie'?'僵尸蘑菇王主动释放无冷却，可立即重新认领。':`${BOSS_NAMES[boss]}主动释放后冻结认领 10 分钟。`;
}
function labelBossCards(html,boss){
  return html.replace(/<article class="timer-card([^>]*)>/g,`<article class="timer-card$1 data-boss="${boss}"><div class="card-boss-label" data-boss-label="${boss}">${BOSS_NAMES[boss]}</div>`);
}
function otherBossCard(c){
  if(isFixedClockBoss(c.boss))return dorrCard(c,true);
  const clock=t=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(t*1000));
  const opening=!c.killed_at,extra=c.claim_kind==='exploration',parallel=c.claim_kind==='parallel';
  const r=c.relay,pending=r&&['open','travel'].includes(r.state);
  const claimed=extra?c.exploration_claimed_at:c.claimed_at,started=r?.started_at||claimed;
  const at=Date.now()/1000+clockOffset;
  const phase=c.latest_at&&at>=c.latest_at?'late':c.earliest_at&&at>=c.earliest_at?'ready':'waiting';
  const status=opening?(pending?'接力中':'蹲守中'):phase==='late'?'已到保底时间':phase==='ready'?'已到进刷时间':'等待进刷';
  const button=(action,label,cls='')=>`<button type="button" class="${cls}" data-cross-action="${action}" data-boss="${c.boss}" data-id="${c.id}">${label}</button>`;
  let actions=opening?button('record','已击杀，记录时间','primary'):button('kill','确认击杀','action-kill');
  if(opening||parallel)actions+=button(pending?'relay/cancel':'relay/invite',pending?'撤销接力':'邀请接力认领','cross-relay');
  else actions+=button(c.sos_at?'cancel-sos':'sos',c.sos_at?'取消 SOS':'SOS 求救','danger');
  if(!opening)actions+=button('release','释放频道');
  actions+=button('discard',opening?'结束本次开荒':'丢弃频道 · 转开荒','discard-action');
  const detail=opening?`<p class="timer-caption">本轮已登记蹲守</p><strong class="relay-clock" data-elapsed="${started}">${elapsedRelay(started)}</strong><p class="hint">我的本次蹲守 <span data-elapsed="${claimed}">${elapsedRelay(claimed)}</span><br>蹲守时长不代表进刷或保底倒计时</p>`:`<p class="timer-caption">距离保底时间</p><strong class="timer-clock countdown guarantee-clock" data-time="${c.latest_at}">${remaining(c.latest_at)}</strong><div class="timer-secondary"><span>距离进刷</span><strong class="countdown" data-time="${c.earliest_at}">${remaining(c.earliest_at)}</strong></div><div class="timer-window"><span>进刷 — 保底</span><strong>${clock(c.earliest_at)} — ${clock(c.latest_at)}</strong></div><p class="timer-kill">上次击杀 · ${fmt(c.killed_at)}</p>`;
  return `<article class="timer-card other-boss-card ${opening?'exploration-card':phase}" data-channel="${c.id}" data-boss="${c.boss}"><div class="card-boss-label" data-boss-label="${c.boss}">${BOSS_NAMES[c.boss]} · 我的持有</div><div class="timer-top"><span class="timer-kind">${extra?'额外开荒':parallel?'并发认领':opening?'常规认领 · 开荒':'当前认领'}</span><span class="timer-status"><i></i><span class="${opening?'':'timer-status-text'}">${status}</span></span></div><div class="timer-number"><span>频道</span><strong>${String(c.id).padStart(2,'0')}</strong></div>${detail}${c.sos_from_name?`<p class="timer-caption" style="overflow-wrap:anywhere">救援接手自：${esc(c.sos_from_name)}</p>`:''}${c.note?`<p class="hint cross-note">备注：${esc(c.note)}</p>`:''}${c.sos_at?'<div class="sos-pending">SOS 已发出 · 等待队友接管</div>':''}${pending?`<p class="relay-travel">${r.state==='travel'?esc(r.receiver_name)+'正在赶来，等待确认到达':'等待队友接力，你仍持有此频道'}</p>`:''}<div class="timer-actions">${actions}</div><a class="cross-boss-link" href="?boss=${c.boss}">前往${BOSS_NAMES[c.boss]} · 更多操作 ↗</a></article>`;
}
async function handleOtherBossAction(e){
  const button=e.currentTarget;if(button.disabled)return;
  const boss=button.dataset.boss,id=Number(button.dataset.id),operation=button.dataset.crossAction;
  const card=(state?.other_boss_claims||[]).find(c=>c.boss===boss&&c.id===id);
  if(!card){toast('该频道已交接或释放，正在同步');await refresh();return}
  const label=`${BOSS_NAMES[boss]} · ${String(id).padStart(2,'0')} 频道`;
  if(operation==='record')return openKillTimeDialog(id,card.claim_kind==='exploration',boss);
  const messages={kill:'击杀已记录',sos:'SOS 已发出','cancel-sos':'SOS 已取消',release:'频道已释放',discard:'频道已转为开荒','relay/invite':'接力邀请已发出','relay/cancel':'接力已撤销'};
  if(!Object.hasOwn(messages,operation))return;
  button.disabled=true;
  try{await action(operation,{channel_id:id,...(operation.startsWith('relay/')?{request_id:card.relay?.request_token||''}:{})},`${label} · ${messages[operation]}`,boss)}
  finally{if(button.isConnected)button.disabled=false}
}
function compareRecommendationTime(a,b){return (a.channel.earliest_at??Infinity)-(b.channel.earliest_at??Infinity)||a.channel.id-b.channel.id}
function render(){
  if(!state)return;
  if(isFixedClockBoss(CURRENT_BOSS))return renderDorr();
  const s=state,admin=s.me.role==='superadmin';
  setWatermark(s.me.nickname);
  document.querySelector('header strong').textContent=BOSS_NAMES[CURRENT_BOSS];
  identity.innerHTML=`<span class="identity-name">${esc(s.me.nickname)}</span><span class="identity-role">${roleLabel(s.me.role)}${['member','admin'].includes(s.me.role)&&s.me.is_plus?' · Plus':''}</span><button id="change-password">账号安全</button><button id="sound-toggle" aria-pressed="${soundEnabled}">声音 ${soundEnabled?'开':'关'}</button><button id="logout">退出登录</button>`;
  identity.querySelector('#change-password').onclick=()=>showChangePassword();
  identity.querySelector('#sound-toggle').onclick=e=>{soundEnabled=!soundEnabled;localStorage.setItem(storageKey('timerSound'),soundEnabled?'on':'off');if(soundEnabled)armAudio();e.currentTarget.textContent=`声音 ${soundEnabled?'开':'关'}`;e.currentTarget.setAttribute('aria-pressed',String(soundEnabled))};
  identity.querySelector('#logout').onclick=async()=>{refreshVersion++;stopEvents();await api('logout',{});refreshVersion++;state=null;showAuth();identity.innerHTML=''};
  const known=admin?s.channels.filter(c=>c.killed_at).length:null;
  const spawnPool=s.spawn_pool||[];
  const ownIds=new Set([...s.own,...(s.concurrent||[])].map(c=>c.id));
  const foreignCards=[...(s.other_boss_claims||[])].sort((a,b)=>a.boss.localeCompare(b.boss)||compareRecommendationTime({channel:a},{channel:b})).map(c=>c.boss==='zombie'&&s.zombie_hub?zombieHubCard(c):otherBossCard(c)).join('');
  const ownedEntries=[...s.own.map(channel=>({channel,kind:'当前认领'})),...(s.concurrent||[]).map(channel=>({channel,kind:'并发认领'})),...(s.explorations||[]).map(channel=>({channel,kind:'开荒认领'}))];
  const isAvailable=channel=>{
    const current=s.channels.find(c=>c.id===channel.id),now=Date.now()/1000+clockOffset;
    return !!current&&!current.claimant_id&&!current.explorer_id&&current.blocked_until<=now&&!!current.killed_at&&current.latest_at>now;
  };
  const recommendationKind=(channel,isOwn)=>`${channel.earliest_at<=Date.now()/1000+clockOffset?'进刷中':'未进刷'} · ${isOwn?'我的击杀':'系统推荐'}`;
  const recommendations=(s.recommendations?s.recommendations.map(x=>({channel:x.channel,kind:recommendationKind(x.channel,x.is_own)})):[
    s.recommendation&&{channel:s.recommendation,kind:recommendationKind(s.recommendation,s.recommendation_is_own)},
    s.extra_recommendation&&{channel:s.extra_recommendation,kind:recommendationKind(s.extra_recommendation,s.extra_recommendation_is_own)}
  ]).filter(x=>x&&!ownIds.has(x.channel.id)&&isAvailable(x.channel));
  const recentKill=s.last_kill&&isAvailable(s.last_kill)&&!ownIds.has(s.last_kill.id)&&!recommendations.some(x=>x.channel.id===s.last_kill.id)?s.last_kill:null;
  const availableChannels=[...recommendations,...(recentKill?[{channel:recentKill,kind:'上次击杀'}]:[])]
    .sort(compareRecommendationTime);
  const dual=CURRENT_BOSS==='mushroom'&&s.zombie_hub;
  const zombieHeld=dual?(s.other_boss_claims||[]).filter(c=>c.boss==='zombie'):[];
  const remainingCardSlots=Math.max(0,4-s.own.length-(s.concurrent||[]).length-(s.explorations||[]).length-(dual?0:(s.other_boss_claims||[]).length));
  const visibleZombieCandidates=(s.zombie_hub?.recommendations||[]).filter(x=>!zombieHeld.some(c=>c.id===x.channel.id)).sort(compareRecommendationTime).slice(0,Math.max(0,4-zombieHeld.length));
  const visibleAvailableChannels=availableChannels.slice(0,remainingCardSlots);
  const nearestRecommendedSpawn=Math.min(...visibleAvailableChannels.map(x=>x.channel.earliest_at??Infinity));
  const availableCards=visibleAvailableChannels.length?visibleAvailableChannels.map(x=>timerCard(x.channel,x.kind)).join(''):remainingCardSlots&&!visibleZombieCandidates.length&&!s.own.length?timerCard(null,'系统推荐'):'';
  const currentEntries=[...ownedEntries.sort(compareRecommendationTime),...visibleAvailableChannels];
  const currentCards=currentEntries.length?currentEntries.map(x=>x.channel.killed_at?timerCard(x.channel,x.kind):explorationCard(x.channel)).join(''):availableCards;
  const zombieCards=[...[...zombieHeld].sort((a,b)=>compareRecommendationTime({channel:a},{channel:b})),...visibleZombieCandidates.map(x=>x.channel)].map(c=>zombieHubCard(c)).join('');
  const sosItems=[...s.sos].sort((a,b)=>(a.earliest_at??Infinity)-(b.earliest_at??Infinity)||(a.latest_at??Infinity)-(b.latest_at??Infinity));
  const acceptBlockedReason=s.claim_blocked_until>Date.now()/1000+clockOffset?'主动释放冻结中，暂不能接受救援':'';
  app.innerHTML=`<div class="boss-context"><span class="boss-label"><i></i>${BOSS_NAMES[CURRENT_BOSS]}<small>专属频道</small></span><span class="boss-scope">频道独立 · ${CURRENT_BOSS==='zombie'?'主动释放无冷却':'主动释放冷却 10 分钟'} · 排行榜按 Boss 统计</span></div><section class="hero"><div class="hero-copy"><p class="eyebrow">${CURRENT_BOSS==='global_mushroom'?'GLOBAL MUSHROOM KING':CURRENT_BOSS==='snowman'?'YETI & PEPE':CURRENT_BOSS==='zombie'?'ZOMBIE MUSHROOM KING':'MUSHROOM KING'} · LIVE OPERATIONS</p><div class="hero-heading"><div class="hero-boss-selector"><h1>频道协作台</h1><p class="boss-switch-label"><span aria-hidden="true">⇄</span> 切换 Boss</p><nav class="boss-switch" aria-label="切换 Boss">${Object.entries(BOSS_NAMES).map(([id,name])=>`${admin||(s.me.boss_access&BOSS_FLAGS[id])?`<a href="?boss=${id}" data-switch-boss="${id}" aria-label="${name}${id===CURRENT_BOSS?'，当前页面':'，点击切换'}" ${id===CURRENT_BOSS?'aria-current="page"':''}><span class="boss-switch-name">${name}</span><small class="boss-switch-state">${id===CURRENT_BOSS?'✓ 当前页面':'点击切换 →'}</small></a>`:`<span class="boss-switch-locked" aria-disabled="true"><span class="boss-switch-name">${name}</span><small class="boss-switch-state">未开通权限</small></span>`}`).join('')}</nav></div><div class="hero-live-clock" role="timer" aria-live="off" aria-label="北京时间"><span>北京时间 · 云端同步</span><strong id="beijing-clock">--:--:--</strong></div></div><p>${['snowman','global_mushroom'].includes(CURRENT_BOSS)?`击杀后 ${BOSS_WINDOWS[CURRENT_BOSS][0]} 分钟进入待刷，${BOSS_WINDOWS[CURRENT_BOSS][1]} 分钟保底。推荐频道按进刷时间排序。`:'记录击杀、追踪刷新、及时支援。每一次认领都清晰可见。'}</p></div></section>
    ${presenceHtml(s)}${s.plus_progress?`<section class="panel" role="status"><strong>连续活跃 · 升级长期 Plus</strong><p>连续达标 ${s.plus_progress.days}/5 天 · 今日有效击杀 ${s.plus_progress.today_kills}/6 次 · ${s.plus_progress.logged_in_today?'今日已登录':'今天请重新登录以完成签到'}</p><p class="hint">按北京时间，连续 5 天每天登录并有效击杀至少 6 次，自动升级长期 Plus。有效开荒也计入；修改时间、被抢和重复刷开荒不计入。中断一天重新计算。</p></section>`:''}${s.claim_blocked_until>Date.now()/1000+clockOffset?`<section class="panel" role="status"><strong>主动释放 · 认领冻结中</strong><p>剩余 <span class="cooldown-countdown" data-time="${s.claim_blocked_until}">${remaining(s.claim_blocked_until)}</span> · 每次释放固定冻结 10 分钟，到期自动恢复；已有频道仍可完成击杀。</p></section>`:''}
    <div class="metrics"><div><span>当前 Boss · 我的认领</span><strong>${s.own.length}<small> / 1 · 并发 ${(s.concurrent||[]).length}/1 · 开荒 ${(s.explorations||[]).length}/3</small></strong></div><div><span>开荒频道</span><strong>${s.unknown.length}<small> / 60</small></strong></div><div><span>今日救援</span><strong>${s.rescues_today}<small> · 建议 ${s.me.rescue_target} 次</small></strong></div>${admin?`<div><span>已记录击杀</span><strong>${known}<small> / 60</small></strong></div>`:`<div><span>账号有效期</span><strong class="metric-date">${fmt(s.me.access_until)}</strong></div>`}</div>
    ${supportSosPanel(s)}
    ${(s.notices||[]).filter(n=>['admin_release','admin_reassign','admin_assign','sos_accepted','guarantee_expired','relay_completed','relay_received','relay_cancelled','relay_reopened'].includes(n.kind)).map(noticeHtml).join('')}
    ${s.me.role==='superadmin'?rosterHtml(s):''}
    ${relayPanel(s)}<section class="timers-section"><div class="section-heading"><div><p class="eyebrow">CHANNEL TIMERS</p><div class="my-channel-title"><h2>我的频道</h2><button type="button" class="primary help-explore-button" data-action="help-explore">帮忙开荒</button>${s.zombie_hub?'<button type="button" class="zombie-help" data-zombie-explore>僵尸蘑菇王 · 帮忙开荒</button>':''}<span class="exploration-gap-hint" id="exploration-gap-hint" data-nearest-spawn="${Number.isFinite(nearestRecommendedSpawn)?nearestRecommendedSpawn:0}" role="status" hidden><span class="exploration-gap-arrow" aria-hidden="true">←</span><span>当前还有剩余时间，建议去开荒<br><small>点击左侧「帮忙开荒」</small></span></span></div></div><div class="timer-heading-actions"><span class="section-note">同一 Boss 已认领优先 · 持有与推荐分别按进刷时间排序</span>${s.last_kill?.can_edit_kill?`<button type="button" data-action="edit" data-id="${s.last_kill.id}">修改我上次击杀的时间 · ${s.last_kill.id.toString().padStart(2,'0')} 频道</button>`:''}</div></div><div class="timers-layout">${dual?`<div class="boss-card-rows"><section><h3>蘑菇王</h3><div class="timer-grid">${labelBossCards(currentCards,CURRENT_BOSS)}</div></section><section><h3>僵尸蘑菇王</h3><div class="timer-grid">${zombieCards}${!zombieHeld.length&&!visibleZombieCandidates.length?'<p class="hint">暂无可推荐频道</p>':''}</div></section>${(s.other_boss_claims||[]).some(c=>c.boss!=='zombie')?`<section><h3>其他 Boss · 我的持有</h3><div class="timer-grid">${s.other_boss_claims.filter(c=>c.boss!=='zombie').map(otherBossCard).join('')}</div></section>`:''}</div>`:`<div class="timer-grid">${labelBossCards(currentCards,CURRENT_BOSS)}${foreignCards}</div>`}${weeklyRankingsHtml(s)}</div></section>
    ${s.me.is_plus?`<section class="panel spawn-pool-section"><div class="section-heading"><div><p class="eyebrow">SHARED SPAWN POOL</p><h2>进刷资源池</h2></div><span class="count-badge">${spawnPool.length+(s.zombie_hub?.spawn_pool.length||0)}</span></div><p class="hint">所有成员都可查看即将进刷且尚未认领的频道。点击卡片查看概要；已有常规认领时，需先处理当前频道才能认领资源池频道。</p><div class="spawn-pool-grid">${[...spawnPool.map(c=>({...c,boss:CURRENT_BOSS})),...(s.zombie_hub?.spawn_pool||[]).map(c=>({...c,boss:'zombie'}))].sort((a,b)=>a.earliest_at-b.earliest_at||a.latest_at-b.latest_at||a.boss.localeCompare(b.boss)||a.id-b.id).map(c=>{let card=spawnPoolCard(c).replace('<span class="pool-card-top">',`<span>${BOSS_NAMES[c.boss]}</span><span class="pool-card-top">`);return c.boss!==CURRENT_BOSS?card.replace('spawn-pool-card','spawn-pool-card zombie-hub-card').replace('data-action=','data-z-action='):card}).join('')}${!spawnPool.length&&!s.zombie_hub?.spawn_pool.length?'<p class="empty">暂无空闲计时频道</p>':''}</div></section>`:''}
    <section class="all-channels-section"><div class="grid">${adminChannelIndex(s)}</div></section>
    ${['superadmin','admin'].includes(s.me.role)?`<section class="admin-zone"><div class="section-heading"><div><p class="eyebrow">CONTROL CENTER</p><h2>管理控制台</h2></div><span class="section-note">${admin?'超级管理员':'普通管理员 · 日志与频道分配'}</span></div><div class="grid">${admin?adminBlacklistHtml(s):''}${adminHtml(s)}</div></section>`:''}<footer class="footer-note">频道状态由云端实时同步 · 支持自主注册</footer>`;
  app.querySelectorAll('[data-ranking]').forEach(button=>button.onclick=()=>{
    selectedRanking=button.dataset.ranking;
    app.querySelectorAll('[data-ranking]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.ranking===selectedRanking)));
    app.querySelectorAll('[data-ranking-panel]').forEach(panel=>panel.hidden=panel.dataset.rankingPanel!==selectedRanking);
  });
  app.querySelectorAll('.boss-switch a,.cross-boss-link').forEach(link=>link.onclick=e=>{if(bossWritesPending||pendingActions.size){e.preventDefault();toast('操作正在提交，请完成后再切换 Boss')}});
  app.querySelectorAll('[data-action]').forEach(b=>b.onclick=handleAction);
  wireZombieHub();
  app.querySelectorAll('[data-cross-action]').forEach(b=>b.onclick=handleOtherBossAction);
  app.querySelectorAll('[data-action="claim"]').forEach(b=>b.onclick=()=>{
    if(b.disabled)return;
    action('claim',{channel_id:Number(b.dataset.id)},'频道已认领',CURRENT_BOSS,b);
  });
  app.querySelectorAll('[data-action="claim-exploration"]').forEach(b=>b.onclick=()=>{
    if(b.disabled)return;b.disabled=true;
    action('claim-exploration',{channel_id:Number(b.dataset.id)},'开荒频道已认领').finally(()=>{if(b.isConnected)b.disabled=false});
  });
  app.querySelectorAll('[data-action="finish-exploration"]').forEach(b=>b.onclick=()=>openKillTimeDialog(Number(b.dataset.id),true));
  if(['superadmin','admin'].includes(s.me.role))wireAdmin();
  wireSupportSos();
  app.querySelectorAll('[data-relay]').forEach(button=>button.onclick=handleRelay);
  app.querySelector('#relay-expand')?.addEventListener('click',()=>{relayExpanded=!relayExpanded;render()});
  wireDorr();
  wireRealmHeader(s);
  scheduleNoticeDismissal();
  tick();
}
const logActionNames={auto_advance:'多尔推算下一轮',team_create:'创建多尔队伍',team_invite:'邀请队员',team_accept:'接受组队邀请',team_decline:'拒绝组队邀请',team_leave:'退出队伍',team_kick:'移出队员',team_disband:'解散队伍',team_rename:'修改队伍名',claim_parallel:'并发认领频道',relay_invite:'发起频道接力',relay_accept:'接受接力准备前往',relay_arrive:'到达并完成接力',relay_cancel:'撤销频道接力',relay_leave:'取消前往接力',relay_reopen:'接力邀请重新开放',user_role:'调整管理员权限',user_plus_auto:'活跃达标自动升级 Plus',user_plus:'调整 Plus 会员',user_boss_access:'调整 Boss 权限',user_cooldown_clear:'解除认领冷却',user_unfreeze:'解除账号冻结或封禁',cancel_sos:'取消求救',channel_note:'修改频道备注',claim:'认领频道',kill:'确认击杀',edit_kill:'修改击杀时间',sos:'发起求救',accept_sos:'接受救援',report_robbed:'登记被抢',overridden:'管理员改派',release:'释放频道',discard:'丢弃频道转开荒',timeout_release:'超时释放',reset_unknown:'重置为开荒',assign:'分配频道',channel_blacklist:'临时拉黑频道',channel_unblacklist:'解除频道拉黑',user_create:'创建角色',user_register:'提交注册申请',user_approve:'审批通过注册',user_password_reset:'恢复码重置密码',user_password_change:'本人修改密码',user_recovery_issue:'重新生成恢复码',user_password_admin_reset:'管理员重置密码',user_ban:'封禁角色',user_freeze:'冻结角色',user_target:'修改救援建议',user_access:'延长有效期',user_delete:'删除角色'};
const logActionName=action=>logActionNames[action]||'其他操作';
function memberStatusHtml(u,t){
  const items=[];
  if(u.pending_approval)items.push('<span class="status-pending">待超级管理员审批</span>');
  else{
    if(u.frozen)items.push('<span class="status-frozen">账号已冻结</span>');
    if(u.banned_until>t)items.push(`<span class="status-banned">封禁或超时处罚至 ${fmt(u.banned_until)}</span>`);
    for(const [boss,until] of Object.entries(u.claim_cooldowns||{}))if(until>t)items.push(`<span class="status-banned">${BOSS_NAMES[boss]}认领冷却至 ${fmt(until)}</span>`);
    if(u.access_until<=t)items.push('<span class="status-expired">会员已到期</span>');
  }
  return `<div class="member-statuses">${items.join('')||'<span class="status-normal">正常</span>'}</div>`;
}
function adminHtml(s){
  const logOptions=Object.entries(logActionNames).map(([action,label])=>`<option value="${action}">${label}</option>`).join('');
  const logs=`<section id="logs-panel" class="panel span12"><h2>${BOSS_NAMES[CURRENT_BOSS]} · 操作日志</h2><div class="toolbar"><input id="log-channel" type="number" min="1" max="60" placeholder="频道号"><select id="log-action"><option value="">全部操作</option>${logOptions}</select><button id="filter-logs">筛选</button></div><div id="logs" class="table-wrap"></div></section>`;
  if(s.me.role!=='superadmin')return `<section class="panel span12"><h2>频道分配</h2><p>点击全部频道中的卡片，在详情中选择“分配成员”。</p></section>${logs}`;
  const t=Date.now()/1000+clockOffset;
  const restricted=u=>u.frozen||u.banned_until>t||u.access_until<=t;
  const pendingCount=s.users.filter(u=>u.pending_approval).length;
  const users=[...s.users].sort((a,b)=>Number(b.pending_approval)-Number(a.pending_approval)||Number(restricted(b))-Number(restricted(a))||a.id-b.id).map(u=>{
    const manageable=u.role!=='superadmin';
    const cooldownButtons=Object.entries(u.claim_cooldowns||{}).filter(([,until])=>until>t).map(([boss])=>` <button data-user-action="clear_cooldown" data-cooldown-boss="${boss}" data-user="${u.id}">解除${BOSS_NAMES[boss]}认领冷却</button>`).join('');
    const controls=manageable?(u.pending_approval?`<button class="primary" data-user-action="approve" data-user="${u.id}">选择权限并审批</button> <button class="user-delete" data-user-action="delete" data-user="${u.id}">拒绝并删除</button>`:`<button data-user-action="role" data-user="${u.id}">${u.role==='admin'?'取消普通管理员':'设为普通管理员'}</button> <button data-user-action="boss_access" data-user="${u.id}">Boss 权限</button> <button data-user-action="plus" data-user="${u.id}">Plus 设置</button> <button data-user-action="target" data-user="${u.id}">救援建议</button> <button data-user-action="ban" data-user="${u.id}">封禁</button> <button data-user-action="${u.frozen||u.banned_until>t?'unfreeze':'freeze'}" data-user="${u.id}">${u.frozen||u.banned_until>t?'解除账号冻结/封禁':'冻结账号'}</button>${cooldownButtons} <button data-user-action="reset_password" data-user="${u.id}">重置密码</button> <button class="user-delete" data-user-action="delete" data-user="${u.id}">删除角色</button>`):'—';
    const bossAccess=u.role==='superadmin'?'全部 Boss 权限':u.pending_approval?'待审批勾选':Object.entries(BOSS_NAMES).filter(([id])=>u.boss_access&BOSS_FLAGS[id]).map(([,name])=>name).join('、')||'未开通';
    return `<tr data-member-id="${u.id}" class="${u.pending_approval?'pending-user':restricted(u)?'restricted-user':''}"><td>${esc(u.nickname)} (${esc(u.username)})</td><td>${roleLabel(u.role)}${['member','admin'].includes(u.role)&&u.is_plus?' · Plus':''}${u.is_plus&&['member','admin'].includes(u.role)?`<small>（${u.plus_until===null?'长期':fmt(u.plus_until)}）</small>`:''}</td><td>${u.pending_approval?'审批后起算':fmt(u.access_until)}</td><td>${esc(bossAccess)}</td><td>${u.rescue_target}</td><td>${memberStatusHtml(u,t)}</td><td>${controls}</td></tr>`;
  }).join('');
  return `<section class="panel span12"><h2>击杀统计</h2><button id="open-kill-stats">按日期查看各 Boss 击杀明细</button></section>${adminRiskHtml(s)}<section class="panel span12"><h2>成员管理${pendingCount?` · 待审批 ${pendingCount}`:''}</h2><div class="toolbar member-search-toolbar"><label for="member-search">搜索成员</label><input id="member-search" type="search" placeholder="输入昵称或账号" value="${esc(memberSearch)}" autocomplete="off"><button type="button" id="clear-member-search">清空</button><span id="member-search-count" class="hint" role="status"></span></div><p id="member-search-empty" class="empty" hidden>没有找到匹配的成员，请换个昵称或账号。</p><div class="table-wrap"><table><thead><tr><th>用户</th><th>角色</th><th>有效期</th><th>Boss 权限</th><th>救援建议</th><th>状态</th><th>操作</th></tr></thead><tbody>${users}</tbody></table></div></section>${logs}`;
}
function adminRiskHtml(s){
  const records=s.login_records||[],risks=records.filter(record=>record.risk);
  const row=record=>`<tr><td>${esc(record.nickname)}</td><td>${fmt(record.at)}</td><td>${esc(record.ip)}</td><td class="login-device">${esc(record.device)}</td><td>${record.takeover?'顶掉旧会话':'普通登录'}</td><td>${esc(record.risk||'未发现异常')}</td></tr>`;
  return `<section class="panel span12"><h2>今日击杀排行</h2><p class="hint">北京时间当天统计 · 包含普通成员及超管</p><div class="table-wrap"><table><thead><tr><th>排名</th><th>昵称</th><th>今日击杀</th></tr></thead><tbody>${(s.today_kill_ranking||[]).map((member,i)=>`<tr><td>${i+1}</td><td>${esc(member.nickname)}</td><td>${Number(member.kills)||0} 次</td></tr>`).join('')}</tbody></table></div></section><section class="panel span12"><h2>疑似共享账号提醒 · ${risks.length}</h2><p class="hint">10 分钟内切换 IP / 设备，或 1 小时至少 3 次顶号时提醒；仅供核对，不自动封禁。IP 变化不能认定异地或共享账号。</p>${risks.length?`<div class="table-wrap"><table><thead><tr><th>昵称</th><th>时间</th><th>IP</th><th>设备 / 浏览器</th><th>会话</th><th>风险提示</th></tr></thead><tbody>${risks.map(row).join('')}</tbody></table></div>`:'<p class="empty">暂无风险提醒</p>'}</section><section class="panel span12"><h2>登录记录</h2><p class="hint">从功能上线后记录成功登录 · 保留 30 天，显示最近 100 条 · 仅超管可见</p><div class="table-wrap"><table><thead><tr><th>昵称</th><th>时间</th><th>IP</th><th>设备 / 浏览器</th><th>会话</th><th>风险提示</th></tr></thead><tbody>${records.map(row).join('')||'<tr><td colspan="6">暂无登录记录</td></tr>'}</tbody></table></div></section>`;
}
function chooseBossAccess(target,approve=false){
  const dialog=document.createElement('dialog');dialog.className='boss-access-dialog';
  const prior=approve?0:target.boss_access;
  dialog.innerHTML=`<form><h2>${approve?'审批成员':'设置 Boss 权限'} · ${esc(target.nickname)}</h2><p class="hint">请选择可访问的 Boss，至少勾选一个。未授权的 Boss 无法查看刷新时间或执行频道操作。</p><div class="boss-access-options">${Object.entries(BOSS_NAMES).map(([id,name])=>`<label><input type="checkbox" name="${id}" ${prior&BOSS_FLAGS[id]?'checked':''}> ${name}</label>`).join('')}</div>${approve?'<p class="hint">审批通过后，会员有效期至 2099 年。</p>':'<p class="hint">成员仍持有频道或正在前往接力时，请先处理后再关闭对应权限。</p>'}<div class="actions"><button type="button" data-cancel>取消</button><button type="submit" class="primary">${approve?'通过审批':'保存权限'}</button></div></form>`;
  dialog.querySelector('[data-cancel]').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>dialog.remove(),{once:true});
  dialog.querySelector('form').onsubmit=async e=>{
    e.preventDefault();const form=e.currentTarget;
    const mask=Object.keys(BOSS_FLAGS).reduce((mask,id)=>mask|(form.elements[id].checked?BOSS_FLAGS[id]:0),0);
    if(!mask){toast('请至少勾选一个 Boss 权限');return}
    const save=form.querySelector('button[type="submit"]');save.disabled=true;
    try{await api('admin/user',{user_id:target.id,action:approve?'approve':'boss_access',boss_access:mask});dialog.close();toast(approve?'成员已审批，权限已开通':'Boss 权限已更新');await refresh({afterWrite:true})}
    catch(error){toast(error.message);save.disabled=false}
  };
  document.body.append(dialog);dialog.showModal();
}
const pendingActions=new Set();
async function action(path,data,successMessage='操作成功',boss=CURRENT_BOSS,button=null){
  const key=`${boss}:${path}:${data.channel_id??data.request_id??''}`;
  if(pendingActions.has(key)){toast('正在处理，请勿重复点击');return}
  const label=button?.textContent;let accepted=false;
  if(button){button.disabled=true;button.textContent='提交中…'}
  pendingActions.add(key);toast('正在提交，请稍候…');
  try{await api(path,data,boss);accepted=true;if(button)button.textContent=path.startsWith('claim')?'已认领 · 同步中…':'已提交 · 同步中…';toast(successMessage);await refresh({afterWrite:true});return true}
  catch(e){toast(e.message);await refresh({afterWrite:true});return false}
  finally{pendingActions.delete(key);if(button?.isConnected&&!accepted){button.disabled=false;button.textContent=label}}
}
const channelDialog=document.createElement('dialog');
channelDialog.className='channel-dialog';
document.body.append(channelDialog);
const assignDialog=document.createElement('dialog');
assignDialog.className='assign-dialog';
document.body.append(assignDialog);
const robbedDialog=document.createElement('dialog');
robbedDialog.className='robbed-dialog';
document.body.append(robbedDialog);
function openRobbedDialog(id,view=state,boss=CURRENT_BOSS){
  if(![...(view?.own||[]),...(view?.concurrent||[])].some(c=>c.id===id)){toast('只能登记当前认领的频道');return}
  robbedDialog.innerHTML=`<form id="robbed-form"><p class="eyebrow">BOSS REPORT</p><h2>${String(id).padStart(2,'0')} 频道 · 被抢登记</h2><p class="muted tiny">填写抢 Boss 玩家的完整角色名称。提交后按当前时间记录击杀、完成频道，并计入右侧排行；不会触发主动释放冷却。</p><label>抢 Boss 玩家完整角色名称<input name="player_id" required maxlength="100" autocomplete="off" placeholder="请填写完整角色名称"></label><div class="actions"><button type="button" id="cancel-robbed">取消</button><button type="submit" class="primary" id="save-robbed">确认被抢并完成频道</button></div></form>`;
  const form=robbedDialog.querySelector('form'),save=robbedDialog.querySelector('#save-robbed');
  robbedDialog.querySelector('#cancel-robbed').onclick=()=>robbedDialog.close();
  form.onsubmit=async e=>{e.preventDefault();const player_id=String(new FormData(form).get('player_id')||'').trim();if(!player_id){toast('请输入完整角色名称');return}save.disabled=true;try{await api('report-robbed',{channel_id:id,player_id},boss);robbedDialog.close();await refresh();toast('已记录被抢击杀时间并完成频道')}catch(x){save.disabled=false;toast(x.message)}};
  if(!robbedDialog.open)robbedDialog.showModal();
  form.querySelector('input').focus();
}
function openAssignDialog(id){
  if(!state?.channels)return;
  const channel=state.channels.find(c=>c.id===id);
  if(!channel){toast('频道不存在，请刷新');return}
  const claimed=new Map(state.channels.filter(c=>c.claimant_id&&!c.is_parallel).map(c=>[c.claimant_id,c.id]));
  const now=Date.now()/1000+clockOffset;
  const members=state.assignable_users||[];
  assignDialog.innerHTML=`<form id="assign-form"><p class="eyebrow">ASSIGN CHANNEL</p><h2>分配 ${id.toString().padStart(2,'0')} 频道</h2><p class="muted tiny">请选择成员。提交成功后，成员页面会自动同步为“已认领”。</p><label>分配给<select name="user_id" required><option value="">请选择成员</option>${members.map(u=>{const held=claimed.get(u.id),disabled=u.frozen||u.banned_until>now||u.access_until<=now||held&&held!==id;return `<option value="${u.id}" ${disabled?'disabled':''} ${channel.claimant_id===u.id?'selected':''}>${esc(u.nickname)} (#${u.id})${held&&held!==id?` · 已认领 ${held} 频道`:''}${u.frozen||u.banned_until>now||u.access_until<=now?' · 暂不可用':''}</option>`}).join('')}</select></label><div class="actions"><button type="button" id="cancel-assign">取消</button><button type="submit" class="primary" id="save-assign">确认分配</button></div></form>`;
  const form=assignDialog.querySelector('form');
  assignDialog.querySelector('#cancel-assign').onclick=()=>assignDialog.close();
  form.onsubmit=async e=>{e.preventDefault();const userId=Number(new FormData(form).get('user_id')),member=members.find(u=>u.id===userId);if(!member)return;const save=assignDialog.querySelector('#save-assign');save.disabled=true;try{await api('admin/assign',{channel_id:id,user_id:userId});assignDialog.close();await refresh();toast(`${id} 频道已分配给 ${member.nickname}`)}catch(x){save.disabled=false;toast(x.message)}};
  if(!assignDialog.open)assignDialog.showModal();
}
function openChannelPreview(id){
  if(!state||!Number.isInteger(id)||id<1||id>60)return;
  const admin=state.me.role==='superadmin';
  const own=[...state.own,...(state.concurrent||[])].find(c=>c.id===id);
  const ownExplore=(state.explorations||[]).find(c=>c.id===id);
  const recommended=state.recommendation?.id===id;
  const extraRecommended=state.extra_recommendation?.id===id;
  const recent=state.last_kill?.id===id;
  const unknown=state.unknown.includes(id);
  const pooled=state.spawn_pool?.find(c=>c.id===id);
  const channel=state.channels.find(c=>c.id===id);
  if(!channel){toast('该频道信息不可见，请刷新');return}
  const held=!!state.own.length;
  const occupied=!!(channel.claimant_id||channel.explorer_id);
  const blocked=channel.blocked_until>Date.now()/1000+clockOffset;
  const canClaim=(!held||Boolean(channel.killed_at&&state.own[0]?.killed_at==null&&(state.explorations||[]).length<3))&&!occupied&&!blocked&&(!channel.killed_at||channel.latest_at>Date.now()/1000+clockOffset);
  const canExplore=held&&(state.explorations||[]).length<3&&!occupied&&!blocked&&unknown;
  const canDiscard=admin||!!own||!!ownExplore;
  const recordMode=openingRecordMode(channel,state);
  const status=blocked?'临时拉黑':own?'我的认领':ownExplore?'我的开荒认领':occupied?channel.explorer_id?'开荒中':'已被认领':recommended?'系统推荐':extraRecommended?'额外推荐':pooled?'进刷资源':recent?'最近击杀':unknown?'开荒频道':'待认领';
  const claimant=occupied?channel.owner_nickname:null;
  const explanation=blocked?'该频道临时拉黑至 '+fmt(channel.blocked_until)+'，到期后自动恢复。':occupied?'该频道已被认领。':held&&unknown?'开荒名额已满（3/3）。':held?'你已有常规认领；只有开荒频道能作为额外认领。':channel.killed_at?'本轮保底时间已过，请等待转为开荒频道。':'';
  const adminControls=['superadmin','admin'].includes(state.me.role)?`<div class="overview-admin-actions"><strong>${BOSS_NAMES[CURRENT_BOSS]} · 频道管理</strong><div><button type="button" id="overview-assign" ${channel.explorer_id||channel.is_parallel||blocked?'disabled':''}>分配成员</button>${admin?`<button type="button" id="overview-edit" ${channel.explorer_id?'disabled':''}>修改击杀时间</button><button type="button" id="overview-release" ${occupied?'':'disabled'}>释放认领</button>`:''}</div></div>`:'';
  channelDialog.innerHTML=`<div class="channel-overview"><p class="eyebrow">CHANNEL OVERVIEW</p><div class="overview-heading"><h2>${id.toString().padStart(2,'0')} <small>频道</small></h2><span class="overview-status">${status}</span></div><div class="overview-details"><div><span>上次击杀</span><strong>${fmt(channel.killed_at)}</strong></div><div><span>进刷</span><strong>${fmt(channel.earliest_at)}</strong></div><div><span>最终保底</span><strong>${fmt(channel.latest_at)}</strong></div>${claimant?`<div><span>当前认领</span><strong>${esc(claimant)}</strong></div>`:''}</div>${adminControls}<p class="hint">${canClaim?'空闲频道可以直接认领。':canExplore?`可额外认领开荒频道，当前 ${(state.explorations||[]).length}/3 条；常规认领保持不变。`:explanation||'此频道当前仅可查看概要。'}</p><div class="actions"><button type="button" id="overview-close">关闭</button>${canClaim?'<button type="button" class="primary" id="overview-claim">确认认领此频道</button>':''}${canExplore?'<button type="button" class="primary" id="overview-explore">开荒认领</button>':''}${own||ownExplore?'<button type="button" id="overview-release-own">释放认领</button>':''}${channel.killed_at&&channel.can_edit_kill&&!admin?'<button type="button" id="overview-edit-own">修改击杀时间</button>':''}${recordMode&&!admin?'<button type="button" id="overview-record">录入击杀时间</button>':''}${canDiscard?'<button type="button" class="discard-action" id="overview-discard">丢弃频道 · 转开荒</button>':''}</div></div>`;
  channelDialog.querySelector('#overview-close').onclick=()=>channelDialog.close();
  channelDialog.querySelector('#overview-claim')?.addEventListener('click',()=>{channelDialog.close();action('claim',{channel_id:id})});
  channelDialog.querySelector('#overview-explore')?.addEventListener('click',()=>{channelDialog.close();action('claim-exploration',{channel_id:id},'开荒频道已认领')});
  channelDialog.querySelector('#overview-release-own')?.addEventListener('click',()=>{channelDialog.close();action('release',{channel_id:id})});
  channelDialog.querySelector('#overview-edit-own')?.addEventListener('click',()=>{channelDialog.close();openKillTimeDialog(id)});
  channelDialog.querySelector('#overview-record')?.addEventListener('click',()=>{channelDialog.close();openKillTimeDialog(id,recordMode==='finish-exploration')});
  channelDialog.querySelector('#overview-discard')?.addEventListener('click',()=>{channelDialog.close();action('discard',{channel_id:id},'频道已丢弃并转为开荒')});
  channelDialog.querySelector('#overview-assign')?.addEventListener('click',()=>{channelDialog.close();openAssignDialog(id)});
  channelDialog.querySelector('#overview-edit')?.addEventListener('click',()=>{channelDialog.close();openKillTimeDialog(id)});
  channelDialog.querySelector('#overview-release')?.addEventListener('click',()=>{channelDialog.close();action('release',{channel_id:id})});
  if(!channelDialog.open)channelDialog.showModal();
}
const killTimeDialog=document.createElement('dialog');
killTimeDialog.className='kill-time-dialog';
document.body.append(killTimeDialog);
function beijingParts(seconds){
  return Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(new Date(seconds*1000)).filter(p=>p.type!=='literal').map(p=>[p.type,p.value]));
}
function openKillTimeDialog(id,exploration=false,boss=CURRENT_BOSS){
  if(!state)return;
  const foreign=boss!==CURRENT_BOSS;
  const channel=foreign?((state.other_boss_claims||[]).find(c=>c.boss===boss&&c.id===id)||state.zombie_hub?.channels.find(c=>c.id===id)):exploration?(state.explorations||[]).find(c=>c.id===id):state.channels?.find(c=>c.id===id)||state.own.find(c=>c.id===id)||state.last_kill?.id===id&&state.last_kill;
  if(!channel){toast('该频道不可修改，请刷新');return}
  if(!exploration&&channel.killed_at&&!channel.can_edit_kill){toast('你无权修改此频道的击杀时间');return}
  const recording=!exploration&&!channel.killed_at;
  if(recording&&!foreign&&state.me.role!=='superadmin'&&openingRecordMode(channel,state)!=='edit'){
    toast('该频道当前不可录入击杀时间，请刷新');return;
  }
  const now=Math.floor(Date.now()/1000+clockOffset);
  const initial=!exploration&&channel.killed_at&&channel.killed_at>=now-7*86400&&channel.killed_at<=now+60?channel.killed_at:now;
  const selected=beijingParts(initial);
  const dateKey=p=>`${p.year}-${p.month}-${p.day}`;
  const dates=Array.from({length:8},(_,i)=>{const p=beijingParts(now-i*86400);return {value:dateKey(p),label:i===0?'今天':i===1?'昨天':`${p.month} 月 ${p.day} 日`}});
  const options=(length,value)=>Array.from({length},(_,i)=>{const n=String(i).padStart(2,'0');return `<option value="${n}" ${n===value?'selected':''}>${n}</option>`}).join('');
  killTimeDialog.innerHTML=`<form id="kill-time-form"><p class="eyebrow">${exploration?'EXPLORATION KILL':recording?'RECORD KILL TIME':'EDIT KILL TIME'}</p><h2>${BOSS_NAMES[boss]} · ${id.toString().padStart(2,'0')} 频道 · ${exploration?'完成开荒并记录击杀':recording?'录入开荒击杀时间':'修改击杀时间'}</h2><p class="muted tiny">${exploration?'请选择小号实际击杀的北京时间。':recording?'请选择实际击杀的北京时间；录入后频道进入正常计时。':'当前记录：'+fmtBeijingMinute(channel.killed_at)+'。请选择实际击杀的北京时间，'}秒数按 00 记录。</p><div class="kill-time-grid"><label class="date-field">日期<select name="day">${dates.map(d=>`<option value="${d.value}" ${d.value===dateKey(selected)?'selected':''}>${d.label} · ${d.value}</option>`).join('')}</select></label><label>时<select name="hour">${options(24,selected.hour)}</select></label><label>分<select name="minute">${options(60,selected.minute)}</select></label></div><div class="kill-time-preview" id="kill-time-preview" aria-live="polite"></div><div class="actions"><button type="button" id="cancel-kill-time">取消</button><button class="primary" type="submit" id="save-kill-time">${exploration?'确认完成开荒':recording?'确认录入击杀':'保存时间'}</button></div></form>`;
  const form=killTimeDialog.querySelector('form'),preview=killTimeDialog.querySelector('#kill-time-preview'),save=killTimeDialog.querySelector('#save-kill-time');
  const chosen=()=>{const value=Object.fromEntries(new FormData(form));const [year,month,day]=value.day.split('-').map(Number);return Date.UTC(year,month-1,day,Number(value.hour)-8,Number(value.minute),0)/1000};
  const [earliestMinutes,latestMinutes]=BOSS_WINDOWS[boss];
  const update=()=>{const at=chosen(),current=Math.floor(Date.now()/1000+clockOffset),valid=at>=current-7*86400&&at<=current+60;save.disabled=!valid;preview.innerHTML=valid?`<span>实际击杀 <strong>${fmtBeijingMinute(at)}</strong></span><span>进刷 · 击杀后 ${earliestMinutes} 分钟 <strong>${fmtBeijingMinute(at+earliestMinutes*60)}</strong></span><span>最终保底 · 击杀后 ${latestMinutes} 分钟 <strong>${fmtBeijingMinute(at+latestMinutes*60)}</strong></span><p class="formula-note">击杀时间 = 下一次保底时间 − ${latestMinutes} 分钟</p>`:'<span class="invalid">只能选择最近 7 天至当前时间后 1 分钟内的时间。</span>'};
  form.onchange=update;
  form.onsubmit=e=>{e.preventDefault();update();if(save.disabled)return;const at=chosen();save.disabled=true;killTimeDialog.close();action(exploration?'kill-exploration':'edit-kill',{channel_id:id,killed_at:at,expected_killed_at:channel.killed_at},exploration?'开荒击杀已记录，常规认领仍保留':recording?'开荒击杀时间已录入':'击杀时间已更新',boss)};
  killTimeDialog.querySelector('#cancel-kill-time').onclick=()=>killTimeDialog.close();
  update();
  if(!killTimeDialog.open)killTimeDialog.showModal();
}
const parallelTimeNotice=document.createElement('dialog');
parallelTimeNotice.className='recovery-dialog';
document.body.append(parallelTimeNotice);
let parallelTimeNoticeTimer=null;
parallelTimeNotice.addEventListener('close',()=>{clearInterval(parallelTimeNoticeTimer);parallelTimeNoticeTimer=null});
function showParallelTimeNotice(primary,channelId){
  const now=Date.now()/1000+clockOffset;
  if(!primary?.earliest_at||primary.earliest_at>now)return;
  clearInterval(parallelTimeNoticeTimer);
  parallelTimeNotice.innerHTML=`<h2>注意主频道时间</h2><p>你已并发认领 <strong>${String(channelId).padStart(2,'0')} 线</strong>，主认领 <strong>${String(primary.id).padStart(2,'0')} 线也已进刷</strong>。</p><p>主频道距保底：<strong class="countdown guarantee-clock" data-time="${primary.latest_at}">${remaining(primary.latest_at)}</strong><br>保底时间：${fmt(primary.latest_at)}</p><p>请合理安排两条线路的时间，优先照看主频道，别错过刷新。来不及时，可邀请其他成员接力并发频道。</p><div class="actions"><button type="button" class="primary" id="parallel-notice-dismiss">知道了（5 秒后关闭）</button></div>`;
  const button=parallelTimeNotice.querySelector('#parallel-notice-dismiss');
  button.onclick=()=>parallelTimeNotice.close();
  const deadline=Date.now()+5000;
  if(!parallelTimeNotice.open)parallelTimeNotice.showModal();
  parallelTimeNoticeTimer=setInterval(()=>{const left=Math.max(0,Math.ceil((deadline-Date.now())/1000));button.textContent=`知道了（${left} 秒后关闭）`;if(!left)parallelTimeNotice.close()},250);
}
async function claimParallelWithNotice(button){
  toast('正在提交，请稍候…');
  try{
    await api('claim-parallel',{channel_id:Number(button.dataset.id),expected_killed_at:Number(button.dataset.cycle)});
    toast('并发频道已认领');await refresh({afterWrite:true});
    if(state)showParallelTimeNotice(state.own[0],Number(button.dataset.id));
  }catch(error){toast(error.message);await refresh()}
  finally{if(button.isConnected)button.disabled=false}
}
function handleAction(e){
  if(['claim-parallel','discard-ready'].includes(e.currentTarget.dataset.action)){
    const b=e.currentTarget;if(b.disabled)return;
    b.disabled=true;
    if(b.dataset.action==='claim-parallel'){if(!state.own.length)return action('claim',{channel_id:Number(b.dataset.id)},'频道已普通认领').finally(()=>{if(b.isConnected)b.disabled=false});return claimParallelWithNotice(b)}
    return action(b.dataset.action,{channel_id:Number(b.dataset.id),expected_killed_at:Number(b.dataset.cycle)},b.dataset.action==='claim-parallel'?'并发频道已认领':'频道已转为开荒').finally(()=>{if(b.isConnected)b.disabled=false});
  }
if(e.currentTarget.dataset.action==='help-explore')return suggestExploration();if(e.currentTarget.dataset.action==='end-explore')return endExploration(Number(e.currentTarget.dataset.id),e.currentTarget);let b=e.currentTarget,id=Number(b.dataset.id),a=b.dataset.action;if(a==='notice-read')return dismissNotice(id);if(a==='note')return openChannelNote(id);if(a==='preview')return openChannelPreview(id);if(a==='robbed')return openRobbedDialog(id);if(a==='kill'){if(b.disabled)return;b.disabled=true;return action('kill',{channel_id:id},'击杀已记录').finally(()=>{if(b.isConnected)b.disabled=false})}if(a==='cancel-sos')return action('cancel-sos',{channel_id:id},'求救已取消，频道认领保留');if(a==='sos')return action('sos',{channel_id:id},'求救已发出，等待队友接管');if(a==='release')return action('release',{channel_id:id});if(a==='discard')return action('discard',{channel_id:id},'频道已丢弃并转为开荒');if(a==='accept'){const held=state.own[0];if(held&&!confirm(`接受救援会自动释放你当前的 ${String(held.id).padStart(2,'0')} 频道。确定接管？`))return;return action('accept-sos',{request_id:b.dataset.request},held?'救援已接管，原频道已自动释放':'救援已接管')}if(a==='edit')return openKillTimeDialog(id);if(a==='assign')return openAssignDialog(id)}
function memberMatchesSearch(user,query){
  const needle=String(query||'').normalize('NFKC').trim().toLocaleLowerCase();
  return !needle||[user.nickname,user.username].some(value=>String(value||'').normalize('NFKC').toLocaleLowerCase().includes(needle));
}
function filterMemberRows(){
  const input=app.querySelector('#member-search');if(!input)return;
  memberSearch=input.value;let visible=0;
  const users=new Map(state.users.map(u=>[String(u.id),u]));
  app.querySelectorAll('[data-member-id]').forEach(row=>{const user=users.get(row.dataset.memberId);row.hidden=!user||!memberMatchesSearch(user,memberSearch);if(!row.hidden)visible++});
  app.querySelector('#member-search-count').textContent=`显示 ${visible} / ${state.users.length} 人`;
  app.querySelector('#member-search-empty').hidden=visible>0;
}
function wireAdmin(){
  if(state.me.role==='admin'){app.querySelector('#filter-logs').onclick=loadLogs;return;}
  const search=app.querySelector('#member-search');
  if(search){search.oninput=filterMemberRows;app.querySelector('#clear-member-search').onclick=()=>{search.value='';filterMemberRows();search.focus()};filterMemberRows();}
  app.querySelector('#open-kill-stats')?.addEventListener('click',openKillStats);
  const updateBlacklistSelection=()=>{
    app.querySelectorAll('[data-blacklist-id]').forEach(b=>{const selected=blacklistSelection.has(Number(b.dataset.blacklistId));b.classList.toggle('is-selected',selected);b.setAttribute('aria-pressed',String(selected))});
    app.querySelector('#blacklist-count').textContent=`已选 ${blacklistSelection.size} 条`;
  };
  app.querySelectorAll('[data-blacklist-id]').forEach(b=>b.onclick=()=>{const id=Number(b.dataset.blacklistId);blacklistSelection.has(id)?blacklistSelection.delete(id):blacklistSelection.add(id);updateBlacklistSelection()});
  app.querySelector('#blacklist-select-active').onclick=()=>{blacklistSelection=new Set(state.channels.filter(c=>c.blocked_until>Date.now()/1000+clockOffset).map(c=>c.id));updateBlacklistSelection()};
  app.querySelector('#blacklist-clear-selection').onclick=()=>{blacklistSelection.clear();updateBlacklistSelection()};
  for(const [buttonId,operation] of [['blacklist-apply','block'],['blacklist-unblock','unblock']])app.querySelector('#'+buttonId).onclick=async()=>{
    const ids=[...blacklistSelection].sort((a,b)=>a-b);
    if(!ids.length){toast('请先选择频道');return}
    const minutes=Number(app.querySelector('#blacklist-duration').value);
    const verb=operation==='block'?'临时拉黑':'解除拉黑';
    if(!confirm(`确定对 ${ids.length} 条频道${verb}？${operation==='block'?`时长 ${minutes} 分钟，到期自动恢复。`:''}`))return;
    const button=app.querySelector('#'+buttonId);button.disabled=true;
    try{await api('admin/channels/blacklist',{channel_ids:ids,action:operation,minutes});blacklistSelection.clear();await refresh();toast(`${ids.length} 条频道已${verb}`)}catch(e){toast(e.message);button.disabled=false}
  };
  app.querySelectorAll('[data-user-action]').forEach(b=>b.onclick=async()=>{
    const a=b.dataset.userAction,id=Number(b.dataset.user),data={user_id:id,action:a};
    const target=state.users.find(u=>u.id===id);
    if(a==='approve'||a==='boss_access'){if(target)chooseBossAccess(target,a==='approve');return}
    if(a==='ban'){const x=prompt('封禁分钟数','60');if(!x)return;data.minutes=Number(x)}
    if(a==='role'){if(!target||!confirm(`确定${target.role==='admin'?'取消':'授予'}「${target.nickname}」普通管理员权限？`))return;data.role=target.role==='admin'?'member':'admin';}
    if(a==='target'){const x=prompt('每日建议救援次数（不影响账号使用）','3');if(x===null)return;data.value=Number(x)}
    if(a==='plus'){const x=prompt('Plus：-1 长期、0 关闭、1/7/30 天','-1');if(x===null)return;data.days=Number(x)}
    if(a==='clear_cooldown'){
      data.cooldown_boss=b.dataset.cooldownBoss;
      if(!target||!confirm(`解除「${target.nickname}」在${BOSS_NAMES[data.cooldown_boss]}的认领冷却？`))return;
    }
    if(a==='delete'&&(!target||!confirm(`确定删除角色「${target.nickname}」（${target.username}）？该角色会立即退出，当前认领会释放，历史操作记录仍保留。`)))return;
    if(['ban','freeze'].includes(a)&&!confirm('这会立即使该用户会话失效并释放其频道，继续？'))return;
    if(a==='reset_password'){
      if(!target||!confirm(`请先核实「${target.nickname}」的身份。生成密钥后，原密码、原恢复码和所有登录会话立即失效。确定继续？`))return;
      b.disabled=true;
      try{const result=await api('admin/user',data);showOneTimeSecret('一次性重置密钥',`账号「${target.username}」的密钥仅显示一次。请通过可信渠道交给本人；本人在登录页点击“已有重置密钥？设置新密码”，输入账号、密钥和新密码。使用后此密钥立即失效。`,result.reset_key,'复制重置密钥');toast('已生成重置密钥')}
      catch(x){toast(x.message)}finally{b.disabled=false}
      return;
    }
    action('admin/user',data,a==='approve'?'审批通过，1 天日卡已开通':a==='delete'?'角色已删除':'操作成功');
  });
  app.querySelector('#filter-logs').onclick=loadLogs;
}
let logRequestVersion=0;
async function loadLogs(){const version=++logRequestVersion,target=app.querySelector('#logs');if(!target)return;let q=new URLSearchParams();let channel=app.querySelector('#log-channel')?.value,action=app.querySelector('#log-action')?.value;if(channel)q.set('channel',channel);if(action)q.set('action',action);try{let x=await api('logs?'+q);if(version!==logRequestVersion||!target.isConnected)return;const top=target.scrollTop,left=target.scrollLeft;target.innerHTML=`<table><thead><tr><th>时间（北京时间）</th><th>Boss</th><th>频道</th><th>操作者</th><th>类型</th><th>详情</th></tr></thead><tbody>${x.logs.map(r=>`<tr><td>${fmt(r.at)}</td><td>${esc(r.boss||BOSS_NAMES[CURRENT_BOSS])}</td><td>${r.channel_id??'—'}</td><td>${esc(r.actor||'系统')}</td><td>${esc(logActionName(r.action))}</td><td>${esc(r.detail)}</td></tr>`).join('')}</tbody></table>`;target.scrollTop=top;target.scrollLeft=left}catch(e){if(version===logRequestVersion&&target.isConnected)toast(e.message)}}
function armAudio(){
  if(!soundEnabled)return null;
  try{
    const C=window.AudioContext||window.webkitAudioContext;
    if(!C)return null;
    if(!audioContext)audioContext=new C();
    if(audioContext.state==='suspended')audioContext.resume().catch(()=>{});
    return audioContext;
  }catch{return null}
}
document.addEventListener('pointerdown',armAudio,{capture:true});
document.addEventListener('keydown',armAudio,{capture:true});
function beep(){
  const ctx=armAudio();
  if(!ctx)return;
  const play=()=>{try{const o=ctx.createOscillator(),g=ctx.createGain();o.type='sine';o.frequency.value=720;g.gain.setValueAtTime(.0001,ctx.currentTime);g.gain.exponentialRampToValueAtTime(.09,ctx.currentTime+.02);g.gain.exponentialRampToValueAtTime(.0001,ctx.currentTime+.23);o.connect(g);g.connect(ctx.destination);o.start();o.stop(ctx.currentTime+.24)}catch{}};
  if(ctx.state==='suspended')ctx.resume().then(play).catch(()=>{});else play();
}
function recordAlert(key){alertKey.add(key);localStorage.setItem(alertStorageKey(),JSON.stringify([...alertKey].slice(-200)))}
function shouldShowExplorationGap(s,nearest,time){
  return s&&['member','admin'].includes(s.me.role)&&!s.own.length&&!(s.explorations||[]).length&&Number.isFinite(nearest)&&nearest>0&&nearest-time>13*60;
}
function updateExplorationGap(){
  const hint=document.querySelector('#exploration-gap-hint');
  const show=!!hint&&shouldShowExplorationGap(state,Number(hint.dataset.nearestSpawn),Date.now()/1000+clockOffset);
  if(hint)hint.hidden=!show;
  if(show&&updateExplorationGap.visibleUser!==state.me.id){
    if(typeof matchMedia!=='function'||!matchMedia('(prefers-reduced-motion: reduce)').matches){
      hint.animate?.([{opacity:1,transform:'translateX(0)'},{opacity:0.4,transform:'translateX(-5px)'},{opacity:1,transform:'translateX(0)'}],{duration:800,iterations:2,easing:'ease-in-out'});
    }
  }
  updateExplorationGap.visibleUser=show?state.me.id:null;
}
function tick(){
  if(state&&stateKey(state)!==renderedStateKey)pendingRender=true;
  app.querySelectorAll('[data-primary-spawn]').forEach(el=>{
    const at=Number(el.dataset.primarySpawn),left=at-Date.now()/1000-clockOffset;
    el.textContent=at&&left<=300?'主频道已临近或进入进刷，可邀请队友接力此频道。':'需要返回主频道时，可邀请队友接力。';
  });

  tickDorr();
  updateExplorationGap();
  if(!state)return;
  app.querySelectorAll('[data-elapsed]').forEach(el=>el.textContent=elapsedRelay(el.dataset.elapsed));
  app.querySelectorAll('[data-arrival]').forEach(button=>{if(Number(button.dataset.arrival)<=Date.now()/1000+clockOffset)button.disabled=true});
  const t=Date.now()/1000+clockOffset;
  const clock=document.querySelector('#beijing-clock');
  if(clock)clock.textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(Date.now()+clockOffset*1000));
  document.querySelectorAll('.countdown,.cooldown-countdown').forEach(el=>el.textContent=remaining(Number(el.dataset.time)));
  const cards=[...(state.other_boss_claims||[]),...state.own,...(state.concurrent||[]),...(state.recommendations||[]).map(x=>x.channel),state.recommendation,state.last_kill].filter(Boolean);
  for(const c of cards){
    if(isFixedClockBoss(c.boss||CURRENT_BOSS))continue;
    const card=app.querySelector(`.timer-card[data-boss="${c.boss||CURRENT_BOSS}"][data-channel="${c.id}"]`);
    if(!card)continue;
    const late=!!c.latest_at&&t>=c.latest_at,ready=!late&&!!c.earliest_at&&t>=c.earliest_at;
    card.classList.toggle('late',late);card.classList.toggle('ready',ready);card.classList.toggle('waiting',!late&&!ready);
    const label=card.querySelector('.timer-status-text');
    if(label)label.textContent=late?'已到保底时间':ready?'已到进刷时间':c.killed_at?'等待进刷':'等待击杀';
  }
  const monitored=[...(state.other_boss_claims||[]),...state.own,...(state.concurrent||[]),...(state.last_kill&&!state.own.some(c=>c.id===state.last_kill.id)?[state.last_kill]:[])];
  for(const c of monitored){
    if(isFixedClockBoss(c.boss||CURRENT_BOSS))continue;
    const card=app.querySelector(`.timer-card[data-boss="${c.boss||CURRENT_BOSS}"][data-channel="${c.id}"]`);
    const startKey=`${c.boss||CURRENT_BOSS}:${state.me.id}:${c.id}:earliest:${c.earliest_at}`;
    if(c.earliest_at&&t>=c.earliest_at&&t<c.latest_at&&!alertKey.has(startKey)){
      recordAlert(startKey);flashUntil.set(startKey,Date.now()+8000);
      toast(`${BOSS_NAMES[c.boss||CURRENT_BOSS]} · 频道 ${c.id} 已到进刷时间`);beep();
    }
    if(c.latest_at&&t>=c.latest_at){
      if(!alertKey.has(startKey))recordAlert(startKey);
      const endKey=`${c.boss||CURRENT_BOSS}:${state.me.id}:${c.id}:latest:${c.latest_at}`;
      if(!alertKey.has(endKey)){recordAlert(endKey);toast(`${BOSS_NAMES[c.boss||CURRENT_BOSS]} · 频道 ${c.id} 已到最终保底时间`);beep()}
    }
    if(card)card.classList.toggle('flash',!!flashUntil.get(startKey)&&flashUntil.get(startKey)>Date.now());
  }
}
setInterval(tick,1000);
setInterval(()=>{if(state)refresh()},8000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state)refresh()});
// A voluntary reminder, never a prerequisite for claiming a boss channel.
function canSuggestExploration(s,time){
  if(isFixedClockBoss(CURRENT_BOSS))return false;
  if(s?.me.role!=='member'||(s.claim_blocked_until||0)>time)return false;
  if((s.explorations||[]).length||s.own.some(c=>!c.killed_at))return false;
  if(s.own.some(c=>c.earliest_at&&c.earliest_at<=time+300))return false;
  return s.channels.some(c=>!c.killed_at&&!c.claimant_id&&!c.explorer_id&&(c.blocked_until||0)<=time);
}
const explorationReminder=document.createElement('aside');
explorationReminder.className='exploration-reminder';explorationReminder.hidden=true;
explorationReminder.setAttribute('aria-label','开荒邀请');
explorationReminder.innerHTML='<p role="status">有空的话，帮忙开荒一个频道吧，让大家多一些打 Boss 的机会。</p><div class="actions"><button class="primary" data-reminder-help>帮忙开荒</button><button data-reminder-dismiss>稍后再说</button></div>';
document.body.append(explorationReminder);
explorationReminder.querySelector('[data-reminder-help]').onclick=()=>{explorationReminder.hidden=true;suggestExploration()};
explorationReminder.querySelector('[data-reminder-dismiss]').onclick=()=>{explorationReminder.hidden=true};
let explorationReminderUser=null,explorationReminderSince=0;
function checkExplorationReminder(){
  const uid=state?.me.id??null;
  if(uid!==explorationReminderUser){explorationReminderUser=uid;explorationReminderSince=Date.now();explorationReminder.hidden=true}
  const time=Date.now()/1000+clockOffset;
  if(!canSuggestExploration(state,time)){explorationReminder.hidden=true;return}
  if(document.hidden||loginPending||Date.now()-explorationReminderSince<300000||document.querySelector('dialog[open]'))return;
  const focused=document.activeElement;
  if(pointerActive||Date.now()<interactionUntil||focused?.matches('input,textarea,select,[contenteditable="true"]'))return;
  const day=new Date((time+8*3600)*1000).toISOString().slice(0,10);
  const key=storageKey('explorationReminder:'+uid);
  if(!explorationReminder.hidden)return;
  try{
    const raw=localStorage.getItem(key);
    // Preserve today's already-shown reminder when upgrading the old date-only value.
    let record;
    if(raw===day){record={day,count:1,last:time};localStorage.setItem(key,JSON.stringify(record))}
    else{try{record=raw?JSON.parse(raw):null}catch{record=null}}
    if(!record||record.day!==day)record={day,count:0,last:0};
    const limit=3;
    if(record.count>=limit||(record.count&&time-record.last<7200))return;
    record={day,count:record.count+1,last:time};
    localStorage.setItem(key,JSON.stringify(record));
  }catch{return}
  explorationReminder.hidden=false;
}
setInterval(checkExplorationReminder,15000);

showAuth();refresh();

// Administrators can also create member accounts for users on shared networks.
const createUserDialog=document.createElement('dialog');
createUserDialog.innerHTML=`<form method="dialog" id="create-user-form"><h2>创建成员</h2><p data-create-realm class="hint"></p><label>用户名<input name="username" required minlength="3" maxlength="30"></label><label>昵称<input name="nickname" required maxlength="30"></label><label>初始密码（至少 9 位）<input name="password" type="password" required minlength="9" autocomplete="new-password"></label><p class="hint">会员有效期至 2099 年；请勾选可访问的 Boss。</p><div class="boss-access-options">${Object.entries(BOSS_NAMES).map(([id,name])=>`<label><input type="checkbox" name="${id}"> ${name}</label>`).join('')}</div><div class="actions"><button class="primary" value="create">创建</button><button type="button" id="cancel-create">取消</button></div></form>`;
document.body.append(createUserDialog);
createUserDialog.querySelector('#cancel-create').onclick=()=>createUserDialog.close();
createUserDialog.querySelector('form').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget,values=Object.fromEntries(new FormData(form));values.boss_access=Object.keys(BOSS_FLAGS).reduce((mask,id)=>mask|(form.elements[id].checked?BOSS_FLAGS[id]:0),0);for(const id of Object.keys(BOSS_FLAGS))delete values[id];if(!values.boss_access){toast('请至少勾选一个 Boss 权限');return}try{await api('admin/create-user',values);createUserDialog.close();form.reset();toast('成员已创建');refresh()}catch(x){toast(x.message)}};
new MutationObserver(()=>{const heading=[...app.querySelectorAll('h2')].find(x=>x.textContent.startsWith('成员管理'));if(heading&&!heading.querySelector('#create-user-button')){const button=document.createElement('button');button.id='create-user-button';button.textContent='创建成员';button.style.marginLeft='14px';button.onclick=()=>{createUserDialog.querySelector('[data-create-realm]').textContent='所属区服：'+REALM_NAMES[activeRealm||'white_rabbit']+'区';createUserDialog.showModal()};heading.append(button)}}).observe(app,{childList:true,subtree:true});

function weeklyRankingsHtml(s){
  const boards=[['exploration','本周开荒榜',s.exploration_board||{rows:[],mine:0},'仅统计有效开荒','完成'],['sos','本周 SOS 救援榜',s.sos_board||{rows:[],mine:0},'成功接管求救计入','救援'],['retreat','今日认怂榜',s.retreat_board||{rows:[],mine:0},'主动释放 + 发起 SOS · 全 Boss 合计','合计']];
  return `<aside class="weekly-rankings compact-rankings"><section class="panel weekly-rank-card"><div class="ranking-switch" role="group" aria-label="选择排行榜"><button type="button" data-ranking="exploration" aria-pressed="${selectedRanking==='exploration'}">开荒榜</button><button type="button" data-ranking="sos" aria-pressed="${selectedRanking==='sos'}">SOS 榜</button><button type="button" data-ranking="retreat" aria-pressed="${selectedRanking==='retreat'}">认怂榜</button></div>${boards.map(([key,title,board,rule,verb])=>`<div class="ranking-content" data-ranking-panel="${key}" ${selectedRanking===key?'':'hidden'}><h3>${title}</h3><p class="hint">${key==='retreat'?'北京时间今日':'北京时间周一至周日'} · ${rule}</p><div class="ranking-scroll" tabindex="0" aria-label="${title}名单">${board.rows.map((r,i)=>`<div class="explore-rank-row"><b>${i+1}</b><span>${esc(r.nickname)}${key==='retreat'?`<small style="display:block;font-size:10px;opacity:.8;margin-top:3px">释放 ${r.releases} · 求救 ${r.requests}</small>`:''}</span><strong>${r.total} 次</strong></div>`).join('')||`<p class="empty">${key==='retreat'?'今日':'本周'}暂无记录</p>`}</div><p class="explore-mine">我${key==='retreat'?'今日':'本周'}${verb} <strong>${board.mine} 次</strong></p>${key==='exploration'?'<p class="hint">重复录入、修改时间不重复计数。丢弃后本轮重新录入不计贡献。</p>':''}</div>`).join('')}</section></aside>`;
}
const exploreDialog=document.createElement('dialog');exploreDialog.className='recovery-dialog';document.body.append(exploreDialog);
let explorationSuggestions=[];
async function suggestExploration(change=false,boss=CURRENT_BOSS){
  if(!state)return;
  if(!change)explorationSuggestions=[];
  const source=boss===CURRENT_BOSS?state:await api('state',undefined,boss);
  const own=source.own.length,full=own&&(source.explorations||[]).length>=3;
  if(full){toast('开荒名额已满，请先处理已有开荒频道');return}
  if(source.claim_blocked_until>Date.now()/1000+clockOffset){toast('认领冷却中，请等待倒计时结束');return}
  try{
    const result=await api('exploration/suggestion?exclude='+explorationSuggestions.join(','),undefined,boss);
    const c=result.channel;
    if(!c){exploreDialog.close();toast('暂无可认领的未知频道');return}
    explorationSuggestions.push(c.id);
    exploreDialog.innerHTML=`<p class="eyebrow">EXPLORE TOGETHER</p><h2>${BOSS_NAMES[boss]} · 推荐开荒频道</h2><div class="explore-number">${String(c.id).padStart(2,'0')}</div><p>暂无有效击杀时间</p>${c.note?`<p class="explore-note">频道备注：${esc(c.note)}</p>`:''}<p>前往游戏检查或蹲守，击杀后回来记录时间。</p><p class="hint">${own?'确认后占用 1 个额外开荒名额':'确认后占用常规认领位置'}；需要自行前往游戏频道。</p><div class="actions"><button class="primary" id="explore-confirm">认领并前往</button><button id="explore-change">换一个</button><button id="explore-cancel">取消</button></div>`;
    exploreDialog.querySelector('#explore-cancel').onclick=()=>exploreDialog.close();
    exploreDialog.querySelector('#explore-change').onclick=()=>suggestExploration(true,boss);
    exploreDialog.querySelector('#explore-confirm').onclick=async e=>{
      e.currentTarget.disabled=true;
      try{await api(own?'claim-exploration':'claim',{channel_id:c.id},boss);exploreDialog.close();await refresh();toast('开荒已认领，请前往游戏对应频道')}
      catch(err){await refresh();toast(err.message);if(err.status===409)await suggestExploration(true,boss);else exploreDialog.close()}
    };
    if(!exploreDialog.open)exploreDialog.showModal();
  }catch(e){toast(e.message)}
}
async function endExploration(id,button){
  if(button.disabled)return;
  button.disabled=true;
  try{await action('discard',{channel_id:id},'已结束开荒，频道已回到无人认领的开荒列表')}
  finally{if(button.isConnected)button.disabled=false}
}
if(APP_BASE==='/pre/'){
  const banner=document.createElement('div');banner.className='pre-banner';banner.textContent='预发环境 · 独立测试数据 · 操作不会写入正式数据库';document.body.prepend(banner);
}

/* Small, dependency-free XLSX writer for the already-authorized statistics result. */
const KillStatsExport = (() => {
  'use strict';
  const MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  const encoder = new TextEncoder();
  const xml = value => String(value ?? '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffe\uffff]/g, '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;');
  const crcTable = Array.from({length: 256}, (_, n) => {
    for (let bit = 0; bit < 8; bit++) n = (n >>> 1) ^ ((n & 1) ? 0xedb88320 : 0);
    return n >>> 0;
  });
  const crc32 = bytes => {
    let crc = 0xffffffff;
    for (const byte of bytes) crc = (crc >>> 8) ^ crcTable[(crc ^ byte) & 255];
    return (crc ^ 0xffffffff) >>> 0;
  };
  // ZIP stored entries keep the workbook self-contained and require no CDN or server work.
  function zip(files) {
    const chunks = [], directory = [];
    let offset = 0, directorySize = 0;
    for (const [path, content] of Object.entries(files)) {
      const name = encoder.encode(path), data = encoder.encode(content), crc = crc32(data);
      const local = new Uint8Array(30 + name.length), l = new DataView(local.buffer);
      l.setUint32(0, 0x04034b50, true); l.setUint16(4, 20, true); l.setUint16(6, 0x800, true);
      l.setUint16(12, 33, true); // 1980-01-01, a valid deterministic ZIP date.
      l.setUint32(14, crc, true); l.setUint32(18, data.length, true); l.setUint32(22, data.length, true);
      l.setUint16(26, name.length, true); local.set(name, 30);
      const central = new Uint8Array(46 + name.length), c = new DataView(central.buffer);
      c.setUint32(0, 0x02014b50, true); c.setUint16(4, 20, true); c.setUint16(6, 20, true);
      c.setUint16(8, 0x800, true); c.setUint16(14, 33, true); c.setUint32(16, crc, true);
      c.setUint32(20, data.length, true); c.setUint32(24, data.length, true);
      c.setUint16(28, name.length, true); c.setUint32(42, offset, true); central.set(name, 46);
      chunks.push(local, data); directory.push(central);
      offset += local.length + data.length; directorySize += central.length;
    }
    const end = new Uint8Array(22), e = new DataView(end.buffer);
    e.setUint32(0, 0x06054b50, true); e.setUint16(8, directory.length, true);
    e.setUint16(10, directory.length, true); e.setUint32(12, directorySize, true); e.setUint32(16, offset, true);
    return new Blob([...chunks, ...directory, end], {type: MIME});
  }
  const textCell = (ref, value, style = 0) => `<c r="${ref}" s="${style}" t="inlineStr"><is><t xml:space="preserve">${xml(value)}</t></is></c>`;
  const numberCell = (ref, value, style = 3) => {
    if (!Number.isSafeInteger(value) || value < 0) throw new Error('统计数据无效，请重新查询');
    return `<c r="${ref}" s="${style}" t="n"><v>${value}</v></c>`;
  };
  function build(stats) {
    if (!stats || !/^\d{4}-\d{2}-\d{2}$/.test(stats.date) || !Array.isArray(stats.rows)) throw new Error('请先查询统计数据');
    const keys = Object.keys(BOSS_NAMES);
    const totalCol=String.fromCharCode(67+keys.length), paidCol=String.fromCharCode(68+keys.length);
    const headers = ['排名', '用户', ...Object.values(BOSS_NAMES), '总计', '当日缴费标记'];
    const rows = stats.rows; // Preserve the server's total-descending order, including ties and zero counts.
    const totals = keys.map(key => rows.reduce((n, u) => n + (u.counts[key] || 0), 0));
    if (totals.reduce((a, b) => a + b, 0) !== stats.total) throw new Error('统计合计不一致，请重新查询');
    const table = rows.map((u, i) => {
      const row = i + 5;
      if (keys.reduce((n, key) => n + (u.counts[key] || 0), 0) !== u.total) throw new Error('用户合计不一致，请重新查询');
      return `<row r="${row}">${numberCell('A'+row, i+1)}${textCell('B'+row, u.nickname + (u.deleted ? '（已删除）' : ''))}${keys.map((key, j) => numberCell(String.fromCharCode(67+j)+row, u.counts[key] || 0)).join('')}${numberCell(totalCol+row, u.total, 4)}${textCell(paidCol+row, u.payment?.paid ? '已缴费' : '未标记')}</row>`;
    }).join('');
    const end = rows.length + 5;
    const worksheet = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<dimension ref="A1:${paidCol}${end}"/><sheetViews><sheetView workbookViewId="0"><pane xSplit="2" ySplit="4" topLeftCell="C5" activePane="bottomRight" state="frozen"/></sheetView></sheetViews>
<sheetFormatPr defaultRowHeight="22"/><cols><col min="1" max="1" width="8" customWidth="1"/><col min="2" max="2" width="32" customWidth="1"/><col min="3" max="${keys.length+3}" width="17" customWidth="1"/><col min="${keys.length+4}" max="${keys.length+4}" width="20" customWidth="1"/></cols>
<sheetData><row r="1" ht="30" customHeight="1">${textCell('A1',(stats.realm_name?stats.realm_name+'区 · ':'')+'各用户 Boss 击杀明细',1)}</row><row r="2">${textCell('A2',`统计日期：${stats.date}（北京时间） · 总击杀 ${stats.total} 次 · 按总数量降序`)}</row>
<row r="4" ht="26" customHeight="1">${headers.map((h,i)=>textCell(String.fromCharCode(65+i)+'4',h,2)).join('')}</row>${table}
<row r="${end}">${textCell('A'+end,'合计',4)}${textCell('B'+end,rows.length+' 位用户',4)}${totals.map((n,i)=>numberCell(String.fromCharCode(67+i)+end,n,4)).join('')}${numberCell(totalCol+end,stats.total,4)}${textCell(paidCol+end,rows.filter(u=>u.payment?.paid).length+' 人已缴费',4)}</row></sheetData>
<autoFilter ref="A4:${paidCol}${Math.max(4,end-1)}"/><mergeCells count="2"><mergeCell ref="A1:${paidCol}1"/><mergeCell ref="A2:${paidCol}2"/></mergeCells>
<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/><pageSetup orientation="landscape" paperSize="9"/></worksheet>`;
    return zip({
      '[Content_Types].xml': `<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>`,
      '_rels/.rels': `<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`,
      'xl/workbook.xml': `<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Boss击杀明细" sheetId="1" r:id="rId1"/></sheets></workbook>`,
      'xl/_rels/workbook.xml.rels': `<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>`,
      'xl/styles.xml': `<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="3"><font><sz val="11"/><name val="Microsoft YaHei"/><color rgb="FF263D35"/></font><font><b/><sz val="16"/><name val="Microsoft YaHei"/><color rgb="FF245D47"/></font><font><b/><sz val="11"/><name val="Microsoft YaHei"/><color rgb="FFFFFFFF"/></font></fonts><fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF246C52"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE7EFE4"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="2" fillId="2" borderId="0" xfId="0" applyFill="1"/><xf numFmtId="1" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="1" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>`,
      'xl/worksheets/sheet1.xml': worksheet
    });
  }
  function download(stats) {
    const url = URL.createObjectURL(build(stats)), link = document.createElement('a');
    link.href = url; link.download = `${stats.realm_name?stats.realm_name+'区_':''}Boss击杀明细_${stats.date}.xlsx`; link.hidden = true;
    document.body.append(link);
    try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000); }
  }
  return Object.freeze({build, download});
})();

function openKillStats(){
 const d=document.createElement('dialog');d.className='kill-stats-dialog';d.style.maxWidth='1200px';d.style.width='calc(100% - 32px)';
 const today=new Date((Date.now()/1000+clockOffset+28800)*1000).toISOString().slice(0,10);
 d.innerHTML=`<h2>各用户 Boss 击杀明细</h2><p class="hint">按北京时间日期统计有效击杀，按总数降序排列。</p><form class="toolbar"><input type="date" name="date" aria-label="统计日期" style="color-scheme:${CURRENT_BOSS==='mushroom'?'dark':'light'}" value="${today}" required><button type="button" data-date-picker>选择日期</button><button type="submit">查询</button><button type="button" data-export disabled>下载 Excel</button><button type="button" data-close>关闭</button></form><div data-results class="table-wrap"></div>`;
 const dateInput=d.querySelector('[name="date"]');
 const showDatePicker=()=>{try{if(dateInput.showPicker)dateInput.showPicker();else dateInput.focus()}catch(e){dateInput.focus()}};
 d.querySelector('[data-date-picker]').onclick=showDatePicker;
 dateInput.addEventListener('click',showDatePicker);
 d.querySelector('[data-close]').onclick=()=>d.close();d.addEventListener('close',()=>d.remove(),{once:true});
 let serial=0, loadedStats=null, paymentSaving=false;
 const exportButton=d.querySelector('[data-export]');
 const updateExport=()=>{exportButton.disabled=paymentSaving||!loadedStats||loadedStats.date!==dateInput.value;exportButton.title=exportButton.disabled?'请先查询所选日期，等待统计及缴费标记同步完成':'下载当前查询结果（Excel）'};
 dateInput.addEventListener('input',()=>{serial++;loadedStats=null;updateExport()});
 exportButton.onclick=()=>{
   if(exportButton.disabled)return;
   exportButton.disabled=true;
   try{KillStatsExport.download(loadedStats);toast('已生成 '+loadedStats.date+' 的 Excel 文件')}
   catch(e){toast('导出失败：'+e.message)}
   finally{updateExport()}
 };
 const query=async()=>{const id=++serial,out=d.querySelector('[data-results]');loadedStats=null;updateExport();out.textContent='正在查询…';try{const r=await api('admin/kill-stats?date='+encodeURIComponent(d.querySelector('[name="date"]').value));if(id!==serial||!d.open)return;loadedStats=r;out.innerHTML=`<p>${r.realm_name?esc(r.realm_name)+'区 · ':''}${esc(r.date)} · 总击杀 ${r.total} 次</p><table><thead><tr><th>排名</th><th>用户</th>${Object.values(BOSS_NAMES).map(n=>`<th>${esc(n)}</th>`).join('')}<th>总计</th><th>当日缴费标记</th></tr></thead><tbody>${r.rows.map((u,i)=>`<tr><td>${i+1}</td><td>${esc(u.nickname)}${u.deleted?'（已删除）':''}</td>${Object.keys(BOSS_NAMES).map(b=>`<td>${u.counts[b]||0}</td>`).join('')}<td><strong>${u.total}</strong></td><td><strong>${u.payment.paid?'已缴费':'未标记'}</strong> <button type="button" data-pay-user="${u.user_id}" data-pay-date="${esc(r.date)}" data-pay-version="${u.payment.version}" data-pay-next="${u.payment.paid?'0':'1'}">${u.payment.paid?'撤销标记':'标记已缴费'}</button>${u.payment.history.length?`<details><summary>操作记录（${u.payment.history.length}）</summary>${u.payment.history.map(h=>`<div>${esc(fmt(h.at))} · ${esc(h.actor)} · ${h.paid?'标记已缴费':'撤销标记'}</div>`).join('')}</details>`:''}</td></tr>`).join('')}</tbody></table>`;updateExport()}catch(e){if(id===serial){loadedStats=null;updateExport();out.textContent=e.message}}};
 d.querySelector('[data-results]').onclick=async e=>{const b=e.target.closest('[data-pay-user]');if(!b||b.disabled||paymentSaving)return;b.disabled=true;paymentSaving=true;updateExport();try{await api('admin/payment-mark',{user_id:Number(b.dataset.payUser),date:b.dataset.payDate,version:Number(b.dataset.payVersion),paid:b.dataset.payNext==='1'});await query()}catch(err){toast(err.message);b.disabled=false;loadedStats=null}finally{paymentSaving=false;updateExport()}};
 d.querySelector('form').onsubmit=e=>{e.preventDefault();query()};document.body.append(d);d.showModal();query();
}

function zombieHubCard(c){
 const v=state.zombie_hub;if(!v)return '';
 const kind=v.own.some(x=>x.id===c.id)?'当前认领':v.concurrent.some(x=>x.id===c.id)?'并发认领':'系统推荐';
 const opening=v.explorations.some(x=>x.id===c.id);
 return labelBossCards(opening?explorationCard(c,v):timerCard(c,kind,v),'zombie').replace('timer-card','timer-card zombie-hub-card').replaceAll('data-action=','data-z-action=').replaceAll('data-relay-boss="mushroom"','data-relay-boss="zombie"');
}
function wireZombieHub(){
 app.querySelector('[data-zombie-explore]')?.addEventListener('click',()=>suggestExploration(false,'zombie').catch(e=>toast(e.message)));
 app.querySelectorAll('[data-z-action]').forEach(b=>b.onclick=()=>handleZombieAction(b));
}
async function handleZombieAction(b){
 if(b.disabled)return;const id=Number(b.dataset.id),op=b.dataset.zAction,v=state.zombie_hub;
 if(!v)return;const c=v.channels.find(x=>x.id===id);if(!c)return;
 if(op==='note')return openChannelNote(id,v,'zombie');
 if(op==='robbed')return openRobbedDialog(id,v,'zombie');
 if(op==='edit'||op==='finish-exploration')return openKillTimeDialog(id,op==='finish-exploration','zombie');
 if(op==='preview'){
  const d=dorrModal(`僵尸蘑菇王 · ${id} 频道`,zombieHubCard(c));
  d.querySelectorAll('[data-z-action]').forEach(x=>{if(x.dataset.zAction==='preview')x.disabled=true;else x.onclick=e=>{e.preventDefault();d.close();handleZombieAction(x)}});
  return;
 }
 const allowed=['claim','claim-parallel','discard-ready','kill','sos','cancel-sos','release','discard','end-explore'];if(!allowed.includes(op))return;
 const completed=await action(op==='end-explore'?'discard':op,{channel_id:id,...(['discard-ready','claim-parallel'].includes(op)?{expected_killed_at:Number(b.dataset.cycle)}:{})},'僵尸蘑菇王 · 操作成功','zombie',b);if(completed&&op==='claim-parallel')showParallelTimeNotice(v.own[0],id);
}


