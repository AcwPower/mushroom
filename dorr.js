// Shared fixed-clock UI reuses the existing session, realtime transport and account controls.
function isFixedClockBoss(boss){return ['dorr','faust','eliza'].includes(boss)}
function dorrBoss(c){return c?.boss||(isFixedClockBoss(CURRENT_BOSS)?CURRENT_BOSS:'dorr')}
function dorrName(c){return BOSS_NAMES[dorrBoss(c)]}
function dorrNow(){return Date.now()/1000+clockOffset}
function dorrTime(t){return t?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(t*1000)):'尚无记录'}
function dorrSource(id,boss=dorrBoss()){return (CURRENT_BOSS===boss?state.channels:state.other_boss_claims||[]).find(c=>c.id===id&&(!c.boss||c.boss===boss))}
function dorrBtn(op,c,label,cls=''){return `<button type="button" class="${cls}" data-clock-boss="${dorrBoss(c)}" data-dorr="${op}" data-id="${c.id}">${label}</button>`}
function dorrQuickClaim(c){return !c.claimant_id&&c.blocked_until<=dorrNow()?dorrBtn('claim',c,c.anchor_at==null?'开荒':'认领','primary'):''}
function dorrQuickDiscard(c){return c.blocked_until<=dorrNow()&&(c.mine||(!c.claimant_id&&c.anchor_at!=null))?dorrBtn('discard',c,'丢弃至开荒','discard-action'):''}
function dorrTile(c){return `<div class="dorr-channel-choice"><button class="dorr-tile ${c.claimant_id?'occupied':''} ${c.blocked_until>dorrNow()?'blocked':''}" data-clock-boss="${dorrBoss(c)}" data-dorr="preview" data-id="${c.id}" data-dorr-tile-time="${c.refresh_at||0}"><b>${String(c.id).padStart(2,'0')}</b><span>${dorrStatus(c)}</span>${c.refresh_at?`<strong class="countdown dorr-tile-countdown" data-time="${c.refresh_at}" aria-label="距离刷新">${remaining(c.refresh_at)}</strong>`:''}${c.estimated?'<span>时间模糊 · 范围波动</span>':''}<small>${esc(c.team_name||c.owner_nickname||(c.estimated?'时间模糊 · 范围波动':c.refresh_at?dorrTime(c.refresh_at):'可开荒'))}</small></button><div class="dorr-tile-actions">${dorrQuickClaim(c)||'<span></span>'}${c.can_edit_kill?dorrBtn('record',c,'录入时间','dorr-tile-record'):'<span></span>'}</div><div class="actions">${dorrQuickDiscard(c)}</div></div>`}
function dorrStatus(c){return c.blocked_until>dorrNow()?'临时拉黑':!c.anchor_at?(c.claimant_id?'开荒中':'开荒'):dorrNow()>=c.refresh_at?(dorrNow()<c.refresh_at+8?'已刷新':'正在同步下一轮'):c.claimant_id?'已认领':'等待刷新'}
function dorrCard(c,foreign=false){
  const opening=c.anchor_at==null,mine=Boolean(c.mine),admin=state.me.role==='superadmin';
  const ready=!opening&&dorrNow()>=c.refresh_at&&dorrNow()<c.refresh_at+8;
  let actions='';
  if(mine){
    actions+=opening?dorrBtn('record',c,'录入开荒击杀','primary'):dorrBtn('kill',c,'确认击杀','primary');
    if(!opening)actions+=dorrBtn('record',c,'补录 / 校准');
    actions+=dorrBtn('release',c,'释放频道')+dorrBtn('discard',c,'丢弃 · 转开荒','discard-action');
  }else if(!c.claimant_id&&c.blocked_until<=dorrNow()){
    actions+=dorrBtn('claim',c,opening?'认领开荒':state.team?'队伍认领':'认领频道','primary');
    if(c.can_edit_kill||admin)actions+=dorrBtn('record',c,opening?'录入击杀':'补录 / 校准');
  }
  return `<article class="timer-card dorr-card ${c.estimated?'is-estimated':''} ${ready?'dorr-ready':''}" data-boss="${dorrBoss(c)}" data-channel="${c.id}" data-dorr-refresh="${c.refresh_at||0}" data-dorr-mine="${mine?1:0}">
    <div class="dorr-card-head"><span class="card-boss-label" data-boss-label="${dorrBoss(c)}">${dorrName(c)}${foreign?' · 我的持有':''}</span>${dorrBtn('note',c,c.note?'备注 · '+esc(c.note):'＋ 备注','dorr-note')}</div>
    <div class="dorr-card-meta"><span>${mine?c.team_id?'队伍持有':'我的持有':c.label||'系统推荐'}</span><span data-dorr-status>${dorrStatus(c)}</span></div>
    <div class="timer-number"><span>频道</span><button type="button" data-clock-boss="${dorrBoss(c)}" data-dorr="preview" data-id="${c.id}">${String(c.id).padStart(2,'0')}</button></div>
    <p class="timer-caption">${opening?'本轮蹲守时间':'距离刷新'}</p>
    ${opening?`<strong class="dorr-clock" ${c.claimed_at?`data-elapsed="${c.claimed_at}"`:''}>${c.claimed_at?elapsedRelay(c.claimed_at):'待开荒'}</strong>`:`<strong class="dorr-clock" data-dorr-clock>${remaining(c.refresh_at)}</strong>`}
    <div class="dorr-confidence">${c.estimated?`<span>时间模糊 · 范围波动${c.auto_rounds?` · 推算 ${c.auto_rounds} 轮`:""}</span>`:opening?(dorrBoss(c)==='dorr'?'两张地图协作蹲守':'队伍协作蹲守'):'● 实际击杀基准'} </div>
    <div class="dorr-time-row"><span>下次刷新</span><strong>${dorrTime(c.refresh_at)}</strong></div>
    <p class="hint dorr-last">真实击杀 ${c.killed_at?fmt(c.killed_at):'尚未记录'}</p>
    ${c.team_id?`<p class="dorr-owner">${esc(c.team_name||'队伍持有')} · ${c.claimant_id===state.me.id?'我认领':`队友：${esc(c.owner_nickname||'原队员')}`}</p>`:''}
    <div class="timer-actions">${actions}</div>${mine?`<p class="dorr-action-hint">${opening?'填写实际击杀时间，开始 45 分钟计时。':'确认击杀记录当前时间；漏记请补录。'}</p>`:''}${foreign?`<a class="cross-boss-link" href="?boss=${dorrBoss(c)}">前往${dorrName(c)} · 队伍与全部频道 ↗</a>`:''}</article>`;
}
function dorrTeamPanel(s){
  if(s.me.role==='superadmin')return `<section class="panel dorr-team-panel"><p class="eyebrow">${dorrBoss().toUpperCase()} SQUAD</p><h2>队伍协作管理</h2><p>成员可创建最多五人的小队，共享频道操作。队伍管理位于页面下方。</p></section>`;
  const t=s.team,cap=t&&t.captain_id===s.me.id;
  const inviteRows=s.invitations.map(i=>`<div class="dorr-invite"><span><b>${esc(i.name)}</b> · ${esc(i.captain_name)} 邀请你<small>有效至 ${fmt(i.expires_at)}</small></span><div class="actions"><button class="primary" data-team="accept" data-token="${esc(i.token)}">接受邀请</button><button data-team="decline" data-token="${esc(i.token)}">拒绝</button></div></div>`).join('');
  const teamControls=t?`<div class="dorr-team-members">${t.members.map(m=>`<span class="dorr-member">${esc(m.nickname)}<small style="color:${m.online?'#34765e':'#727b73'}">${m.online?'● 在线':'○ 离线'}</small>${m.id===t.captain_id?'<small>队长</small>':''}${cap&&m.id!==s.me.id?`<button data-team="kick" data-user="${m.id}" aria-label="移出 ${esc(m.nickname)}">移出</button>`:''}</span>`).join('')}</div><div class="actions">${cap?'<button data-team="invite" class="primary">邀请队友</button><button data-team="rename">修改队名</button><button data-team="disband">解散队伍</button>':'<button data-team="leave">退出队伍</button>'}</div>${s.sent_invitations.length?`<p class="hint">等待回应：${s.sent_invitations.map(i=>esc(i.nickname)).join('、')}</p>`:''}`:'<p class="hint">分工蹲守。队友共享频道操作权，击杀贡献记给实际提交者。</p><button class="primary" data-team="create">创建五人小队</button>';
  return `<section class="panel dorr-team-panel"><div class="section-heading"><div><p class="eyebrow">${dorrBoss().toUpperCase()} SQUAD</p><h2>${t?esc(t.name):`我的${dorrName()}小队`}</h2></div><span class="count-badge">${t?t.members.length:0} / 5 人</span></div>${teamControls}${inviteRows}<p class="hint">${t?'队伍共享 6 条计时 + 3 条开荒名额。退队后卡片归队伍保留。':'独行可持有 6 条计时 + 3 条开荒频道。'} 游戏内收益请与队友约定平分。关闭网页约 1 分钟后显示离线。</p></section>`;
}
function dorrOnlinePanel(s){
  const people=(s.members||[]).filter(m=>['member','admin'].includes(m.role)&&(Number(m.boss_access)&BOSS_FLAGS[CURRENT_BOSS])&&m.online&&m.on_boss_page);
  return `<section class="panel dorr-online"><div class="section-heading"><div><p class="eyebrow">${dorrBoss().toUpperCase()} MEMBERS</p><h2>${dorrName()}在线成员</h2></div><span class="count-badge">${people.length} 人</span></div><p class="hint">当前停留在${dorrName()}页面的在线成员 · 离开或关闭后最多约 1 分钟移除 · 今日击杀仅统计${dorrName()}</p><p class="hint presence-activity-hint">标签按 Boss 区分持有状态，包含${dorrName()}队伍共享频道；只显示你有权限查看的 Boss，没有持有时显示等待中。</p><div class="presence-list">${people.map(presenceMemberHtml).join('')||`<p class="empty">暂无停留在${dorrName()}页面的在线成员</p>`}</div></section>`;
}
function dorrRecommendations(s,slots){
  // Only display the server's spaced selection; last_kill is metadata, not an
  // extra candidate that can bypass the team's 12-second allocation preference.
  return s.recommendations.map(c=>({...c,label:s.last_kill?.id===c.id?'上次击杀':'系统推荐'})).sort((a,b)=>a.refresh_at-b.refresh_at||a.id-b.id).slice(0,Math.max(0,slots));
}
function renderDorr(){
  const s=state,admin=s.me.role==='superadmin';setWatermark(s.me.nickname);
  document.querySelector('header strong').textContent=`${dorrName()}`;
  identity.innerHTML=`<span class="identity-name">${esc(s.me.nickname)}</span><span class="identity-role">${roleLabel(s.me.role)}${s.me.is_plus&& !admin?' · Plus':''}</span><button id="change-password">账号安全</button><button id="sound-toggle">声音 ${soundEnabled?'开':'关'}</button><button id="logout">退出登录</button>`;
  identity.querySelector('#change-password').onclick=showChangePassword;
  identity.querySelector('#sound-toggle').onclick=e=>{soundEnabled=!soundEnabled;localStorage.setItem(storageKey('timerSound'),soundEnabled?'on':'off');if(soundEnabled)armAudio();e.currentTarget.textContent='声音 '+(soundEnabled?'开':'关')};
  identity.querySelector('#logout').onclick=async()=>{stopEvents();await api('logout',{});state=null;identity.innerHTML='';showAuth()};
  const foreign=s.other_boss_claims||[],held=s.dorr_held;
  const offered=dorrRecommendations(s,6-held.length-foreign.length);
  const notices=s.notices.filter(n=>!dismissedNotices.has(`${CURRENT_BOSS}:${s.me.id}:${n.id}`)).map(n=>['dorr','fixed_clock'].includes(n.kind)?`<div class="release-notice" data-notice-id="${n.id}"><p>${esc(n.message||`${dorrName()}状态已更新`)}</p><button data-action="notice-read" data-id="${n.id}">知道了</button></div>`:noticeHtml(n)).join('');
  const pool=s.spawn_pool.map(c=>`<div class="dorr-pool-item spawn-pool-card ${c.estimated?'is-estimated':''}"><strong>${String(c.id).padStart(2,'0')} <small>线</small></strong><div class="pool-card-count">距离刷新 <b class="countdown" data-time="${c.refresh_at}">${remaining(c.refresh_at)}</b></div><div class="pool-card-window">刷新 ${dorrTime(c.refresh_at)}</div><small>${c.estimated?'时间模糊 · 范围波动':'实际击杀基准'}</small><div class="actions">${dorrQuickClaim(c)}${dorrBtn('preview',c,'详情')}${dorrQuickDiscard(c)}</div></div>`).join('');
  app.innerHTML=`<section class="hero dorr-hero"><div><p class="eyebrow">${dorrBoss().toUpperCase()} · COOPERATIVE HUNT</p><h1>${dorrName()} · ${CURRENT_BOSS==='dorr'?'双图协作':'队伍协作'}</h1><p>45 分钟固定刷新 · 仅我的频道与队伍频道发声提醒</p><nav class="boss-switch" aria-label="切换 Boss">${Object.entries(BOSS_NAMES).map(([id,name])=>admin||(s.me.boss_access&BOSS_FLAGS[id])?`<a href="?boss=${id}" data-switch-boss="${id}" ${id===CURRENT_BOSS?'aria-current="page"':''}>${name}<small>${id===CURRENT_BOSS?'✓ 当前页面':'切换 →'}</small></a>`:`<span class="boss-switch-locked">${name}<small>未开通</small></span>`).join('')}</nav></div><div class="hero-live-clock"><span>北京时间 · 云端同步</span><strong id="beijing-clock">--:--:--</strong></div></section>
    <div class="dorr-top-grid">${dorrTeamPanel(s)}${dorrOnlinePanel(s)}</div>
    ${notices}${supportSosPanel(s)}${relayPanel(s)}<section class="timers-section"><div class="section-heading"><div><p class="eyebrow">MY CHANNELS</p><div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap"><h2>我的频道${s.team?' · 队伍共享':''}</h2><button type="button" class="primary" data-dorr="claim-nearest" title="按刷新时间最多认领6条，避开与自己或本队频道12秒内的冲突；临近线路可在资源池手动认领">一键智能认领 · 最多6条</button></div></div><span class="section-note">持有优先 · 自动认领间隔＞12秒 · 临近线路可手动认领</span></div><div class="dorr-card-grid">${held.map(c=>dorrCard(c)).join('')}${foreign.map(otherBossCard).join('')}${offered.map(c=>dorrCard(c)).join('')}${!held.length&&!foreign.length&&!offered.length?'<div class="timer-empty"><strong>从开荒开始第一轮</strong><p>在下方选一条空闲频道，认领并录入实际击杀时间。</p></div>':''}</div></section>
    <section class="panel dorr-pool"><div class="section-heading"><h2>${dorrName()}刷新资源池</h2><span class="count-badge">${s.spawn_pool.length}</span></div><p class="hint">所有${dorrName()}成员可见 · 尚未认领的频道，按下一次刷新时间排列。</p><div class="dorr-timeline" aria-label="我的线路刷新时间轴"></div><div class="dorr-pool-grid">${pool||'<p class="empty">暂无有效计时频道</p>'}</div></section>
    <section class="panel dorr-all"><div class="section-heading"><div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap"><h2>全部频道 · 60/60</h2><span style="font-size:16px;color:#34765e">北京时间 <strong data-all-clock role="timer" aria-live="off" style="font-size:23px;font-variant-numeric:tabular-nums">${dorrTime(dorrNow())}</strong></span></div><span class="section-note">认领按钮直接提交 · 点频道号查看详情</span></div><div class="dorr-all-grid">${s.channels.map(dorrTile).join('')}</div></section>
    <div class="dorr-lower">${weeklyRankingsHtml(s)}</div>
    ${admin?`<section class="admin-zone"><h2>${dorrName()}管理控制台</h2><section class="panel"><h3>队伍管理</h3>${s.admin_teams.map(t=>`<div class="dorr-admin-team"><b>${esc(t.name)}</b><span>${t.members.map(m=>`${esc(m.nickname)}${m.id===t.captain_id?'（队长）':` <button data-team="kick" data-team-id="${t.id}" data-user="${m.id}">移出</button>`}`).join(' · ')}</span><button data-team="disband" data-team-id="${t.id}">强制解散</button></div>`).join('')||'<p class="empty">暂无队伍</p>'}</section><div class="grid">${adminBlacklistHtml(s)}${adminHtml(s)}</div></section>`:''}${s.me.role==='admin'?`<section class="admin-zone"><div class="grid">${adminHtml(s)}</div></section>`:''}<footer class="footer-note">${dorrName()}独立频道 · 推算时间不计真实击杀 · ${APP_BASE==='/pre/'?'PRE 预发验证环境':'云端实时同步'}</footer>`;
  wireDorr();wireSupportSos();wireRealmHeader(s);
  app.querySelectorAll('[data-relay]').forEach(b=>b.onclick=handleRelay);
  app.querySelector('#relay-expand')?.addEventListener('click',()=>{relayExpanded=!relayExpanded;renderDorr()});
  app.querySelectorAll('[data-action="notice-read"]').forEach(b=>b.onclick=()=>dismissNotice(Number(b.dataset.id)));
  app.querySelectorAll('[data-cross-action]').forEach(b=>b.onclick=handleOtherBossAction);
  app.querySelectorAll('[data-ranking]').forEach(b=>b.onclick=()=>{selectedRanking=b.dataset.ranking;renderDorr()});
  app.querySelectorAll('.boss-switch a,.cross-boss-link').forEach(a=>a.onclick=e=>{if(bossWritesPending){e.preventDefault();toast('操作提交中，请稍后切换')}});
  if(['superadmin','admin'].includes(s.me.role))wireAdmin();scheduleNoticeDismissal();tick();
}
async function dorrSubmit(path,data,button,boss=dorrBoss()){
  const key=boss+':'+path+':'+(data.channel_id||data.token||data.user_id||data.team_id||'');
  if(pendingActions.has(key))return false;
  pendingActions.add(key);if(button)button.disabled=true;toast('正在提交…');
  try{const result=await api(path,data,boss);toast(path===boss+'/claim-nearest'?`本次认领 ${result.claimed_channels.length} 条，当前共 ${result.held_count}/6 条${result.deferred_count?'，已避开 '+result.deferred_count+' 条12秒内临近线路，可在资源池手动认领':result.held_count<6?'，暂无更多空闲计时线路':''}`:'操作成功');await refresh({afterWrite:true});return true}
  catch(e){toast(e.message);await refresh({afterWrite:true});return false}
  finally{pendingActions.delete(key);if(button?.isConnected)button.disabled=false}
}
function dorrModal(title,body,onSubmit){
  const d=document.createElement('dialog');d.className='dorr-dialog';
  d.innerHTML=`<form><h2>${esc(title)}</h2>${body}<div class="actions"><button type="button" data-close>关闭</button>${onSubmit?'<button type="submit" class="primary">确认提交</button>':''}</div></form>`;
  d.querySelector('[data-close]').onclick=()=>d.close();d.addEventListener('close',()=>{d.remove();pendingRender=true;flushDashboard()},{once:true});
  // Embedded channel cards contain action buttons, never implicit form submits.
  d.querySelectorAll('button:not([type])').forEach(b=>b.type='button');
  d.querySelector('form').onsubmit=e=>e.preventDefault();
  if(onSubmit)d.querySelector('form').onsubmit=async e=>{e.preventDefault();const b=e.currentTarget.querySelector('[type=submit]');b.disabled=true;try{if(await onSubmit(new FormData(e.currentTarget),b))d.close()}finally{if(b.isConnected)b.disabled=false}};
  document.body.append(d);d.showModal();return d;
}
function openDorrRecord(c){
  const at=Math.floor(dorrNow());
  const value=new Date((at+8*3600)*1000).toISOString().slice(0,19);
  const d=dorrModal(`${dorrName(c)} ${String(c.id).padStart(2,'0')} 线 · 录入时间`,`<p class="hint">按北京时间填写击杀时间。确定时间选精准；仅知道大概时间选模糊，不计击杀贡献。</p><label>击杀时间（北京时间）<input name="time" type="datetime-local" step="1" value="${value}" required></label><p class="hint" data-clock-mode>当前时间实时更新，提交时锁定。</p><button type="button" data-live-time>使用当前时间</button><p class="hint">预计刷新 = 此时间 + 45 分钟。模糊时间仅供参考，实际刷新存在波动。</p><div class="actions"><button type="button" class="primary" data-time-mode="precise">精准时间提交</button><button type="button" data-time-mode="fuzzy">模糊时间提交</button></div>`);
  let live=true;
  const input=d.querySelector('[name="time"]'),mode=d.querySelector('[data-clock-mode]');
  const update=()=>{if(live&&d.open)input.value=new Date((Math.floor(dorrNow())+8*3600)*1000).toISOString().slice(0,19)};
  const pause=()=>{live=false;mode.textContent='手动补录时间，已停止自动更新。'};
  input.addEventListener('focus',pause);input.addEventListener('input',pause);
  d.querySelector('[data-live-time]').onclick=()=>{live=true;mode.textContent='当前时间实时更新，提交时锁定。';update()};
  const clock=setInterval(update,250);d.addEventListener('close',()=>clearInterval(clock),{once:true});
  const closeRow=d.querySelector('[data-close]').parentElement;
  const submitRow=d.querySelector('[data-time-mode]').parentElement;
  submitRow.append(d.querySelector('[data-close]'));closeRow.remove();
  d.querySelectorAll('[data-time-mode]').forEach(b=>b.onclick=async()=>{
    if(!d.querySelector('form').reportValidity())return;
    const at=live?Math.floor(dorrNow()):Math.floor(Date.parse(input.value+'+08:00')/1000);
    if(!Number.isFinite(at)||at>dorrNow()){toast('击杀时间不能晚于现在');return}
    const buttons=[...d.querySelectorAll('[data-time-mode]')];buttons.forEach(x=>x.disabled=true);
    try{if(await dorrSubmit('edit-kill',{channel_id:c.id,version:c.version,killed_at:at,time_mode:b.dataset.timeMode},b,dorrBoss(c)))d.close()}
    finally{buttons.forEach(x=>x.disabled=false)}
  });
  d.querySelector('form').onsubmit=e=>e.preventDefault();
}
function openDorrPreview(c){
  const admin=state.me.role==='superadmin';
  const d=dorrModal(`${dorrName(c)} · ${String(c.id).padStart(2,'0')} 线`,`<div class="overview-details"><div><span>状态</span><strong>${dorrStatus(c)}</strong></div><div><span>下次刷新${c.estimated?'（推算）':''}</span><strong>${c.refresh_at?fmt(c.refresh_at):'等待开荒'}</strong></div><div><span>真实击杀</span><strong>${c.killed_at?fmt(c.killed_at):'尚未记录'}</strong></div><div><span>当前归属</span><strong>${esc(c.team_name||c.owner_nickname||'无人认领')}</strong></div></div><p>${esc(c.note||'暂无频道备注')}</p><div class="actions">${!c.claimant_id&&c.blocked_until<=dorrNow()?dorrBtn('claim',c,'认领','primary'):''}${c.can_edit_kill?dorrBtn('record',c,'补录 / 校准'):''}${c.mine||admin?dorrBtn('release',c,'释放')+dorrBtn('discard',c,'丢弃转开荒'):''}${dorrBtn('note',c,'编辑备注')}${['superadmin','admin'].includes(state.me.role)?dorrBtn('assign',c,'分配 / 改派'):''}</div>`);
  d.querySelectorAll('[data-dorr]').forEach(b=>b.onclick=()=>{d.close();handleDorr(b)});
}
function handleDorr(b){
  if(b.dataset.dorr==='claim-nearest')return dorrSubmit(dorrBoss()+'/claim-nearest',{},b);
  const c=dorrSource(Number(b.dataset.id),b.dataset.clockBoss||dorrBoss());if(!c){toast('频道已变化，请同步后重试');return}
  const op=b.dataset.dorr;
  if(op==='preview')return openDorrPreview(c);
  if(op==='record')return openDorrRecord(c);
  if(op==='note')return dorrModal(`${dorrName(c)} ${c.id} 线备注`,`<label>常来的对手或分工提醒<input name="note" maxlength="200" value="${esc(c.note||'')}"></label>`,(f,button)=>dorrSubmit('channel-note',{channel_id:c.id,version:c.version,note:String(f.get('note'))},button,dorrBoss(c)));
  if(op==='assign')return dorrModal(`分配${dorrName(c)} ${c.id} 线`,`<p class="hint">成员若已组队，频道归该队伍共享；按队伍名额校验。</p><label>成员<select name="user" required>${state.assignable_users.map(u=>`<option value="${u.id}">${esc(u.nickname)}</option>`).join('')}</select></label>`,(f,button)=>dorrSubmit('admin/assign',{channel_id:c.id,version:c.version,user_id:Number(f.get('user'))},button,dorrBoss(c)));
  return dorrSubmit(op,{channel_id:c.id,version:c.version},b,dorrBoss(c));
}
function handleDorrTeam(b){
  const op=b.dataset.team,extra={};if(b.dataset.teamId)extra.team_id=Number(b.dataset.teamId);
  if(['create','rename'].includes(op))return dorrModal(op==='create'?`创建${dorrName()}队伍`:'修改队伍名称',`<label>队伍名<input name="name" required maxlength="30" value="${esc(op==='rename'?state.team.name:'')}"></label><p class="hint">创建后个人${dorrName()}频道转入队伍，全队共享操作权。</p>`,(f,button)=>dorrSubmit(dorrBoss()+'/team/'+op,{name:String(f.get('name'))},button));
  if(op==='invite')return dorrModal('邀请队友',`<p class="hint">最多 5 人，邀请 24 小时有效。对方接受后才加入。</p><label>选择成员<select name="user" required><option value="">选择昵称</option>${state.invite_candidates.map(u=>`<option value="${u.id}">${esc(u.nickname)} · #${u.id}</option>`).join('')}</select></label>`,(f,button)=>dorrSubmit(dorrBoss()+'/team/invite',{user_id:Number(f.get('user'))},button));
  if(['disband','kick','leave'].includes(op)&&!confirm(op==='disband'?'解散队伍并释放全部队伍频道？计时保留。':'离队或移出后，队伍频道保留，但离队者失去共享操作权。'))return;
  return dorrSubmit(dorrBoss()+'/team/'+op,{...extra,...(b.dataset.token?{token:b.dataset.token}:{}),...(b.dataset.user?{user_id:Number(b.dataset.user)}:{})},b);
}
function wireDorr(){
  app.querySelectorAll('[data-dorr]').forEach(b=>b.onclick=()=>handleDorr(b));
  app.querySelectorAll('[data-team]').forEach(b=>b.onclick=()=>handleDorrTeam(b));
}
function tickDorr(){
  const allClock=app.querySelector('[data-all-clock]');if(allClock)allClock.textContent=dorrTime(dorrNow());
  tickDorrTimeline();
  if(!state)return;
  const t=dorrNow();let play=false;
  app.querySelectorAll('[data-dorr-refresh]').forEach(card=>{
    const at=Number(card.dataset.dorrRefresh);if(!at)return;
    const ready=t>=at&&t<at+8,mine=card.dataset.dorrMine==='1';
    const lead=card.classList.contains('is-estimated')?30:15;
    const reminder=t>=at-lead&&t<at+8;
    card.classList.toggle('dorr-ready',ready);card.classList.toggle('dorr-flash',reminder&&mine);
    const clock=card.querySelector('[data-dorr-clock]');if(clock)clock.textContent=ready?`已刷新 · ${Math.max(0,Math.ceil(at+8-t))} 秒`:t>=at+8?'同步下一轮…':remaining(at);
    const status=card.querySelector('[data-dorr-status]');if(status)status.textContent=ready?'已刷新':t>=at+8?'等待云端同步':mine?'已认领':'等待刷新';
    const kill=card.querySelector('[data-dorr="kill"]');if(kill){kill.disabled=pendingActions.has(card.dataset.boss+':kill:'+card.dataset.channel);kill.title='已在游戏内击杀时点击，按当前服务器时间重新计时';}
    const key=`${card.dataset.boss}:${state.me.id}:${card.dataset.channel}:${at}`;
    if(reminder&&mine&&!alertKey.has(key)){recordAlert(key);play=true}
  });
  if(play)beep();
  app.querySelectorAll('[data-dorr-tile-time]').forEach(tile=>{const at=Number(tile.dataset.dorrTileTime);tile.classList.toggle('dorr-ready',at>0&&t>=at&&t<at+8)});
}

function dorrTimelineRows(held,now){
  const rows=held.filter(c=>Number.isFinite(c.refresh_at)&&c.refresh_at>=now&&c.refresh_at<=now+600).sort((a,b)=>a.refresh_at-b.refresh_at||a.id-b.id);
  return rows.map(c=>{const gap=Math.min(...rows.filter(r=>r.id!==c.id).map(r=>Math.abs(r.refresh_at-c.refresh_at)));return {...c,level:gap<=15?'danger':gap<=30?'warning':'normal',gap}});
}
function tickDorrTimeline(){
  const host=app.querySelector('.dorr-timeline');if(!host||!state)return;
  const now=dorrNow(),rows=dorrTimelineRows(state.dorr_held||[],now);
  const signature=JSON.stringify(rows.map(c=>[c.id,c.refresh_at,c.estimated,c.level]));
  if(host.dataset.signature!==signature){
    host.dataset.signature=signature;
    host.innerHTML=`<div class="dorr-timeline-heading"><strong>我的线路 · 刷新时间轴</strong><small>未来 10 分钟 · 橙色 ≤30秒 / 红色 ≤15秒 · 点击定位</small></div><div class="dorr-axis"><span>现在</span><span>+5 分钟</span><span>+10 分钟</span></div>${rows.length?rows.map(c=>`<div class="dorr-timeline-row"><div class="dorr-timeline-track"><button type="button" class="dorr-timeline-point ${c.level} ${c.estimated?'estimated':''}" data-locate="${c.id}" title="${c.id}线 · ${c.estimated?'预计':'刷新'} ${dorrTime(c.refresh_at)}${c.gap<=30?' · 与其他线路相差 '+c.gap+' 秒':''}">${String(c.id).padStart(2,'0')}线${c.estimated?' · 预计':''}</button></div><span>${dorrTime(c.refresh_at)}</span></div>`).join(''):'<p class="hint">未来 10 分钟暂无自己或队伍持有的计时线路</p>'}<small class="dorr-timeline-note">仅提示刷新拥挤，不影响认领。虚线为模糊时间，实际刷新可能波动。</small>`;
    host.onclick=e=>{const b=e.target.closest('[data-locate]');if(!b)return;const card=app.querySelector(`.dorr-card[data-boss="${CURRENT_BOSS}"][data-channel="${Number(b.dataset.locate)}"]`);if(card){card.scrollIntoView({behavior:'smooth',block:'center'});card.querySelector('.timer-number button')?.focus({preventScroll:true});card.animate([{outline:'3px solid #dba652'},{outline:'3px solid transparent'}],{duration:1600})}};
  }
  for(const c of rows){const b=host.querySelector(`[data-locate="${c.id}"]`);if(b)b.style.left=`${Math.max(0,Math.min(100,(c.refresh_at-now)/6))}%`}
}
