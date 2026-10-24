"""Shared fixed clock and team ownership for Dorr, Faust and Eliza."""
import json
import secrets
import boss_scope
import view_cache

PERIOD = boss_scope.TIMING['dorr'][0]
GRACE = 8
ALLOCATION_GAP = 12
TEAM_LIMIT = 5


def spaced_candidates(rows, held, limit):
    """Pick earliest available slots without crowding this owner's schedule.

    Compare actual next-refresh anchors, not remaining countdown displays.
    Close alternatives stay free for another team; explicit manual claims are
    still allowed. The write path calls this under the realm's shared guard.
    """
    anchors = [r['anchor_at'] for r in held if r['anchor_at'] is not None]
    selected, deferred = [], 0
    for row in sorted(rows, key=lambda r: (r['anchor_at'], r['id'])):
        if len(selected) >= max(0, limit):
            break
        if any(abs(row['anchor_at'] - anchor) <= ALLOCATION_GAP for anchor in anchors):
            deferred += 1
            continue
        selected.append(row)
        anchors.append(row['anchor_at'])
    return selected, deferred


def init(c, s, boss='dorr'):
    c = boss_scope.Connection(c, boss)
    pk = 'BIGINT PRIMARY KEY AUTO_INCREMENT' if s.MYSQL else 'INTEGER PRIMARY KEY'
    c.execute('''CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY, killed_at BIGINT NULL, anchor_at BIGINT NULL,
        last_killer_id BIGINT NULL, estimated INTEGER NOT NULL DEFAULT 0,
        auto_rounds INTEGER NOT NULL DEFAULT 0, version BIGINT NOT NULL DEFAULT 0,
        claimant_id BIGINT NULL, claimed_at BIGINT NULL, team_id BIGINT NULL,
        claim_kind VARCHAR(16) NULL, last_owner_id BIGINT NULL, last_team_id BIGINT NULL,
        blocked_until BIGINT NOT NULL DEFAULT 0, note VARCHAR(200) NOT NULL DEFAULT '',
        credit_blocked_until BIGINT NOT NULL DEFAULT 0, exploded_at BIGINT NULL,
        parallel_id BIGINT NULL, parallel_claimed_at BIGINT NULL,
        explorer_id BIGINT NULL, exploration_claimed_at BIGINT NULL,
        explorer2_id BIGINT NULL, exploration2_claimed_at BIGINT NULL,
        explorer3_id BIGINT NULL, exploration3_claimed_at BIGINT NULL,
        sos_at BIGINT NULL, sos_token VARCHAR(64) NULL)''')
    c.executemany(('INSERT IGNORE' if s.MYSQL else 'INSERT OR IGNORE') +
                  ' INTO channels(id) VALUES(?)', [(i,) for i in range(1, 61)])
    c.execute(f'''CREATE TABLE IF NOT EXISTS clock_teams (
        id {pk}, name VARCHAR(30) NOT NULL, captain_id BIGINT NOT NULL,
        created_at BIGINT NOT NULL, active INTEGER NOT NULL DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS clock_team_members (
        user_id BIGINT PRIMARY KEY, team_id BIGINT NOT NULL, joined_at BIGINT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS clock_invites (
        token VARCHAR(64) PRIMARY KEY, team_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
        created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL, status VARCHAR(16) NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS clock_notice_text (
        notification_id BIGINT PRIMARY KEY, message VARCHAR(500) NOT NULL)''')
    c.execute('CREATE TABLE IF NOT EXISTS app_migrations (name VARCHAR(100) PRIMARY KEY, applied_at BIGINT NOT NULL)')
    migration = boss + '_access_v1'
    if not c.execute('SELECT 1 FROM app_migrations WHERE name=?', (migration,)).fetchone():
        # New pages inherit their predecessor's access once; revocations persist.
        source_flag = {'dorr': 1, 'faust': 4, 'eliza': 8}[boss]
        c.execute('UPDATE users SET boss_access=boss_access | ? WHERE deleted_at IS NULL AND (boss_access & ?)<>0',
                  (boss_scope.FLAGS[boss], source_flag))
        c.execute('INSERT INTO app_migrations VALUES(?,?)', (migration, s.now()))


@view_cache.cached()
def team(c, uid):
    return c.execute('''SELECT t.* FROM clock_teams t JOIN clock_team_members m ON m.team_id=t.id
        WHERE m.user_id=? AND t.active=1''', (uid,)).fetchone()


def members(c, tid):
    now = int(__import__('time').time())
    return c.execute('''SELECT u.id,u.nickname,
        CASE WHEN u.frozen=0 AND u.deleted_at IS NULL AND u.banned_until<=? AND u.access_until>?
        AND EXISTS(SELECT 1 FROM sessions s WHERE s.user_id=u.id AND s.version=u.session_version
        AND s.expires_at>? AND s.last_seen_at>? AND s.last_action_at>?) THEN 1 ELSE 0 END AS online
        FROM clock_team_members m JOIN users u ON u.id=m.user_id
        WHERE m.team_id=? ORDER BY m.joined_at,u.id''', (now, now, now, now-60, now-1800, tid)).fetchall()


def owns(c, row, uid):
    if row['claimant_id'] is None:
        return False
    if row['team_id']:
        t = team(c, uid)
        return bool(t and t['id'] == row['team_id'])
    return row['claimant_id'] == uid


def notice(c, s, uid, cid, message):
    result = c.execute("INSERT INTO notifications(user_id,channel_id,kind,created_at) VALUES(?,?,'fixed_clock',?)",
                       (uid, cid or 0, s.now()))
    c.execute('INSERT INTO clock_notice_text VALUES(?,?)', (result.lastrowid, message))


def recipients(c, row):
    return [m['id'] for m in members(c, row['team_id'])] if row['team_id'] else ([row['claimant_id']] if row['claimant_id'] else [])


def release(c, cid):
    c.execute('''UPDATE channels SET claimant_id=NULL,claimed_at=NULL,team_id=NULL,claim_kind=NULL,
        version=version+1 WHERE id=?''', (cid,))


def disband(c, s, t, actor):
    people = members(c, t['id'])
    for ch in c.execute('SELECT id FROM channels WHERE team_id=?', (t['id'],)).fetchall():
        release(c, ch['id'])
    c.execute('DELETE FROM clock_team_members WHERE team_id=?', (t['id'],))
    c.execute('UPDATE clock_teams SET active=0 WHERE id=?', (t['id'],))
    c.execute("UPDATE clock_invites SET status='cancelled' WHERE team_id=? AND status='pending'", (t['id'],))
    for m in people:
        notice(c, s, m['id'], 0, f'队伍「{t["name"]}」已解散，持有频道已释放，计时保留。')
    s.log(c, 'team_disband', None, actor, f'队伍 {t["id"]}：{t["name"]}')


def cleanup_user(c, s, uid, actor):
    """Called under the shared guard for account disable/delete, from any Boss page."""
    scoped = c
    t = team(scoped, uid)
    if t:
        if t['captain_id'] == uid:
            disband(scoped, s, t, actor)
        else:
            scoped.execute('DELETE FROM clock_team_members WHERE user_id=?', (uid,))
            notice(scoped, s, t['captain_id'], 0, '一名队员账号停用，已移出队伍，队伍频道保留。')
    for ch in scoped.execute('SELECT id FROM channels WHERE claimant_id=? AND team_id IS NULL', (uid,)).fetchall():
        release(scoped, ch['id'])
    scoped.execute("UPDATE clock_invites SET status='cancelled' WHERE user_id=? AND status='pending'", (uid,))


def sweep(c, s):
    s.relay.lock(c, s)
    t = s.now()
    touched = False
    invalid = c.execute('''SELECT m.user_id FROM clock_team_members m JOIN users u ON m.user_id=u.id
        WHERE u.deleted_at IS NOT NULL OR u.frozen=1 OR u.pending_approval=1 OR u.banned_until>?
        OR u.access_until<=? OR (u.boss_access & ?)=0''', (t, t, boss_scope.FLAGS[c.boss])).fetchall()
    for m in invalid:
        cleanup_user(c, s, m['user_id'], None)
        touched = True
    for row in c.execute('SELECT * FROM channels WHERE anchor_at IS NOT NULL AND anchor_at+?+?<=?', (PERIOD, GRACE, t)).fetchall():
        # No confirmation by the end of the grace window: forecast a kill at
        # that deadline, then wait another full PERIOD. Catch up using the same
        # PERIOD + GRACE cycle, without adding request/sweep processing delays.
        rounds = (t - row['anchor_at']) // (PERIOD + GRACE)
        new_anchor = row['anchor_at'] + rounds * (PERIOD + GRACE)
        for uid in recipients(c, row):
            notice(c, s, uid, row['id'], f'{row["id"]:02d} 线未收到击杀确认，已释放并推算下一轮。可补录真实击杀时间。')
        c.execute('''UPDATE channels SET anchor_at=?,estimated=1,auto_rounds=auto_rounds+?,
            last_owner_id=CASE WHEN claimant_id IS NOT NULL THEN claimant_id ELSE last_owner_id END,
            last_team_id=CASE WHEN claimant_id IS NOT NULL THEN team_id ELSE last_team_id END,
            claimant_id=NULL,claimed_at=NULL,team_id=NULL,claim_kind=NULL,version=version+1 WHERE id=?''',
                  (new_anchor, rounds, row['id']))
        s.log(c, 'auto_advance', row['id'], None, f'推算推进 {rounds} 轮；原基准 {row["anchor_at"]}，新基准 {new_anchor}；每轮等待 {GRACE} 秒后视为模糊击杀，非真实击杀')
        touched = True
    if c.execute("UPDATE clock_invites SET status='expired' WHERE status='pending' AND expires_at<=?", (t,)).rowcount:
        touched = True
    return touched


def detail(row, boss='dorr'):
    value = dict(row)
    value.update(boss=boss, boss_name=boss_scope.BOSSES[boss], refresh_at=row['anchor_at'] + PERIOD if row['anchor_at'] is not None else None)
    value['earliest_at'] = value['latest_at'] = value['refresh_at']
    value['is_parallel'] = False
    return value


def can_edit(c, row, user):
    if row['claimant_id'] is not None:
        return owns(c, row, user['id'])
    if user['role'] == 'superadmin' or owns(c, row, user['id']):
        return True
    if row['claimant_id'] or row['blocked_until'] > __import__('time').time():
        return False
    if row['anchor_at'] is None:
        return True
    t = team(c, user['id'])
    return (row['last_killer_id'] == user['id'] or
            bool(t and row['last_team_id'] == t['id']) or
            bool(row['estimated'] and row['last_owner_id'] == user['id']))


def capacity(c, uid, kind, extra=1):
    t = team(c, uid)
    sql, params = ('team_id=?', (t['id'],)) if t else ('team_id IS NULL AND claimant_id=?', (uid,))
    count = c.execute(f'SELECT count(*) AS n FROM channels WHERE {sql} AND claim_kind=?', (*params, kind)).fetchone()['n']
    limit = 3 if kind == 'opening' else 6
    return count + extra <= limit


def merge_personal(c, s, uid, tid):
    kept, freed = [], []
    for kind in ('timed', 'opening'):
        count = c.execute('SELECT count(*) AS n FROM channels WHERE claim_kind=? AND team_id=?', (kind, tid)).fetchone()['n']
        slots = max(0, (3 if kind == 'opening' else 6)-count)
        order = 'claimed_at,id' if kind == 'opening' else 'anchor_at,id'
        rows = c.execute(f'SELECT id FROM channels WHERE team_id IS NULL AND claimant_id=? AND claim_kind=? ORDER BY {order}', (uid, kind)).fetchall()
        for index, row in enumerate(rows):
            cid = row['id']
            if index < slots:
                c.execute('UPDATE channels SET team_id=?,version=version+1 WHERE id=?', (tid, cid))
                kept.append(cid)
            else:
                release(c, cid)
                freed.append(cid)
                s.log(c, 'release', cid, uid, f'{boss_scope.BOSSES[c.boss]}入队超额自动释放，保留计时，无冷却')
    if kept or freed:
        message = f'入队频道整理：转入 {len(kept)} 条，释放 {len(freed)} 条。'
        if freed:
            message += '释放频道：' + '、'.join(f'{cid:02d}' for cid in freed) + '；原计时保留。'
        notice(c, s, uid, 0, message)


def team_view(c, s, user):
    uid = user['id']
    t = team(c, uid)
    value = {**dict(t), 'members': [dict(m) for m in members(c, t['id'])]} if t else None
    invites = [dict(r) for r in c.execute('''SELECT i.token,i.expires_at,t.name,u.nickname AS captain_name
        FROM clock_invites i JOIN clock_teams t ON t.id=i.team_id JOIN users u ON u.id=t.captain_id
        WHERE i.user_id=? AND i.status='pending' AND i.expires_at>? AND t.active=1''', (uid, s.now()))]
    candidates = []
    outgoing = []
    if t and t['captain_id'] == uid:
        candidates = [dict(r) for r in c.execute('''SELECT u.id,u.nickname FROM users u
            WHERE u.role IN ('member','admin') AND u.deleted_at IS NULL AND u.pending_approval=0 AND u.frozen=0 AND u.banned_until<=?
            AND u.access_until>? AND (u.boss_access & ?)<>0 AND NOT EXISTS
            (SELECT 1 FROM clock_team_members m WHERE m.user_id=u.id) ORDER BY u.nickname,u.id''', (s.now(), s.now(), boss_scope.FLAGS[c.boss]))]
        outgoing = [dict(r) for r in c.execute('''SELECT i.token,i.expires_at,u.nickname FROM clock_invites i
            JOIN users u ON u.id=i.user_id WHERE i.team_id=? AND i.status='pending' AND i.expires_at>?''', (t['id'], s.now()))]
    all_teams = [{**dict(r), 'members': [dict(m) for m in members(c, r['id'])]}
                 for r in c.execute('SELECT * FROM clock_teams WHERE active=1 ORDER BY id')] if user['role'] == 'superadmin' else []
    return {'team': value, 'invitations': invites, 'invite_candidates': candidates,
            'sent_invitations': outgoing, 'admin_teams': all_teams}


def view(c, s, user):
    uid, t = user['id'], s.now()
    people = s.member_presence(c, user)
    names = {p['id']: p['nickname'] for p in people}
    teams = {r['id']: r['name'] for r in c.execute('SELECT id,name FROM clock_teams WHERE active=1')}
    rows = c.execute('SELECT * FROM channels ORDER BY id').fetchall()
    channels = []
    for row in rows:
        value = detail(row, c.boss)
        value.update(mine=owns(c, row, uid), can_edit_kill=can_edit(c, row, user),
                     owner_nickname=names.get(row['claimant_id'], '原队员') if row['claimant_id'] else None,
                     team_name=teams.get(row['team_id']))
        channels.append(value)
    held = [r for r in channels if r['mine']]
    held.sort(key=lambda r: (r['claim_kind'] == 'opening', r['refresh_at'] if r['refresh_at'] is not None else (r['claimed_at'] or 0), r['id']))
    pool = sorted([r for r in channels if r['anchor_at'] is not None and not r['claimant_id'] and r['blocked_until'] <= t],
                  key=lambda r: (r['refresh_at'], r['id']))
    last = next(iter(sorted([r for r in channels if r['last_killer_id'] == uid and r in pool],
                            key=lambda r: -(r['killed_at'] or 0))), None)
    timed = [r for r in held if r['claim_kind'] == 'timed']
    recommended, deferred = spaced_candidates(pool, timed, max(0, 6-len(timed)))
    notifications = [dict(r) for r in c.execute('''SELECT n.id,n.channel_id,n.kind,n.created_at,x.message
        FROM notifications n LEFT JOIN clock_notice_text x ON x.notification_id=n.id
        WHERE n.user_id=? AND n.read_at IS NULL ORDER BY n.id DESC LIMIT 20''', (uid,))]
    return {'boss': c.boss, 'boss_name': boss_scope.BOSSES[c.boss], 'me': s.public_user(user), 'server_time': t,
            'members': people, 'channels': channels, 'dorr_held': held, **team_view(c, s, user),
            'own': [r for r in held if r['claim_kind'] == 'timed'], 'concurrent': [],
            'explorations': [r for r in held if r['claim_kind'] == 'opening'],
            'unknown': [r['id'] for r in channels if r['anchor_at'] is None and not r['claimant_id'] and r['blocked_until'] <= t],
            'other_boss_claims': s.other_boss_claims(c, user), 'recommendations': recommended, 'last_kill': last,
            'recommendation_deferred': deferred, 'allocation_gap_seconds': ALLOCATION_GAP,
            'spawn_pool': pool, 'notices': notifications, 'cross_support': s.cross_support(c, user),
            'claim_blocked_until': 0, 'next_release_cooldown_minutes': 0,
            'exploration_board': s.exploration_board(c, uid), 'sos_board': s.weekly_sos_board(c, uid),
            'retreat_board': s.retreat_board(c, uid), 'relays': [], 'sos': [], 'reserved_slots': []}


def foreign_cards(c, s, user):
    scoped = c
    return [{**detail(row, c.boss), 'claim_kind': 'exploration' if row['claim_kind'] == 'opening' else 'primary',
             'mine': True, 'can_edit_kill': True}
            for row in scoped.execute('''SELECT ch.*,u.nickname AS owner_nickname,t.name AS team_name
                FROM channels ch LEFT JOIN users u ON u.id=ch.claimant_id
                LEFT JOIN clock_teams t ON t.id=ch.team_id
                WHERE ch.claimant_id IS NOT NULL ORDER BY ch.id''')
            if owns(scoped, row, user['id'])]


def handle(c, s, user, path, data):
    uid, admin = user['id'], user['role'] == 'superadmin'
    if path == f'/api/{c.boss}/claim-nearest':
        # Caller holds relay_guard through commit, shared with single claims and teams.
        group = team(c, uid)
        clause, args = ('team_id=?', (group['id'],)) if group else ('team_id IS NULL AND claimant_id=?', (uid,))
        held = c.execute(f"SELECT * FROM channels WHERE {clause} AND claim_kind='timed'", args).fetchall()
        count = len(held)
        needed = max(0, 6-count)
        pool = c.execute('''SELECT * FROM channels WHERE anchor_at IS NOT NULL
            AND claimant_id IS NULL AND blocked_until<=? ORDER BY anchor_at,id''', (s.now(),)).fetchall() if needed else []
        rows, deferred = spaced_candidates(pool, held, needed)
        for row in rows:
            c.execute("UPDATE channels SET claimant_id=?,claimed_at=?,team_id=?,claim_kind='timed',version=version+1 WHERE id=?",
                      (uid, s.now(), group['id'] if group else None, row['id']))
            s.log(c, 'claim', row['id'], uid, f'{boss_scope.BOSSES[c.boss]}智能认领：按刷新时间，避开本队{ALLOCATION_GAP}秒内的临近线路')
        return {'claimed_channels': [r['id'] for r in rows], 'held_count': count+len(rows),
                'deferred_count': deferred, 'allocation_gap_seconds': ALLOCATION_GAP}
    if path.startswith(f'/api/{c.boss}/team/'):
        return team_action(c, s, user, path.rsplit('/', 1)[1], data)
    allowed = ('claim', 'kill', 'edit-kill', 'release', 'discard', 'channel-note', 'admin/assign')
    op = path.removeprefix('/api/')
    s.require(op in allowed, f'{boss_scope.BOSSES[c.boss]}不支持该操作', 400)
    cid = data.get('channel_id')
    s.require(type(cid) is int and 1 <= cid <= 60, '频道无效', 400)
    row = c.execute('SELECT * FROM channels WHERE id=?', (cid,)).fetchone()
    s.require(type(data.get('version')) is int and data['version'] == row['version'], '本轮状态已变化，可能已被队友操作，请同步后重试', 409)
    mine = owns(c, row, uid)
    if op == 'edit-kill':
        s.require(not row['claimant_id'] or mine, '频道已被认领，仅持有人或同队成员可以录入时间', 403)
    if op == 'channel-note':
        note = data.get('note')
        s.require(isinstance(note, str) and len(note) <= 200 and not any(ord(x) < 32 for x in note), '备注限 200 字', 400)
        c.execute('UPDATE channels SET note=?,version=version+1 WHERE id=?', (note.strip(), cid))
        s.log(c, 'channel_note', cid, uid, f'更新{boss_scope.BOSSES[c.boss]}共享备注')
        return
    if op in ('claim', 'admin/assign'):
        target = user
        if op == 'admin/assign':
            s.require(user['role'] in ('superadmin', 'admin'))
            target = c.execute('SELECT * FROM users WHERE id=?', (data.get('user_id'),)).fetchone()
            s.require(s.active(target) and target['role'] in ('member', 'admin') and s.can_access_boss(target, c.boss), f'成员不可用或无{boss_scope.BOSSES[c.boss]}权限', 400)
        else:
            s.require(row['claimant_id'] is None, '频道已被认领', 409)
        s.require(row['blocked_until'] <= s.now(), '频道临时拉黑中', 409)
        kind = 'opening' if row['anchor_at'] is None else 'timed'
        s.require(not owns(c, row, target['id']), '已经持有此频道', 409)
        s.require(capacity(c, target['id'], kind), '认领名额已满：个人或队伍计时 6／开荒 3', 409)
        for prior in recipients(c, row):
            notice(c, s, prior, cid, f'{cid:02d} 线已由管理员改派。')
        group = team(c, target['id'])
        c.execute('''UPDATE channels SET claimant_id=?,claimed_at=?,team_id=?,claim_kind=?,version=version+1 WHERE id=?''',
                  (target['id'], s.now(), group['id'] if group else None, kind, cid))
        s.log(c, 'assign' if op == 'admin/assign' else 'claim', cid, uid,
              f'{boss_scope.BOSSES[c.boss]} {kind}，认领用户 {target["id"]}，队伍 {group["id"] if group else "个人"}')
        if op == 'admin/assign':
            notice(c, s, target['id'], cid, f'管理员已分配 {cid:02d} 线给你或你的队伍。')
        return
    if op in ('release', 'discard'):
        free_discard = op == 'discard' and row['claimant_id'] is None and row['anchor_at'] is not None and row['blocked_until'] <= s.now()
        s.require(admin or mine or free_discard, '只能丢弃空闲计时频道或自己及本队持有的频道', 403)
        s.require(row['claimant_id'] is not None or admin or free_discard, '频道已经释放', 409)
        release(c, cid)
        if op == 'discard':
            c.execute('''UPDATE channels SET killed_at=NULL,anchor_at=NULL,last_killer_id=NULL,estimated=0,
                auto_rounds=0,last_owner_id=NULL,last_team_id=NULL,credit_blocked_until=? WHERE id=?''',
                      (max(row['credit_blocked_until'], (row['anchor_at'] or 0)+PERIOD), cid))
        s.log(c, op, cid, uid, '管理员释放，无惩罚' if admin and op == 'release' else
              '主动释放，无认领冷却' if op == 'release' else f'{boss_scope.BOSSES[c.boss]}丢弃，转开荒')
    else:
        s.require(admin or (mine if op == 'kill' else can_edit(c, row, user)), '无权修改此频道，请联系持有人或管理员', 403)
        opening = row['anchor_at'] is None
        if op == 'kill':
            s.require(row['claimant_id'] is not None, '请先认领频道', 409)
            at = s.now()
        else:
            at = data.get('killed_at')
            s.require(type(at) is int and s.now()-7*s.DAY <= at <= s.now(), '请选择过去 7 天内的实际击杀时间，不能晚于现在', 400)
        if op == 'edit-kill':
            s.require(data.get('time_mode', 'precise') in ('precise', 'fuzzy'), '时间类型无效', 400)
        if op == 'edit-kill' and data.get('time_mode') == 'fuzzy':
            # Only the forecast anchor changes; no fabricated kill or contribution.
            group = team(c, uid)
            c.execute('UPDATE channels SET anchor_at=?,estimated=1,auto_rounds=0,last_owner_id=?,last_team_id=?,version=version+1 WHERE id=?',
                      (at, uid, group['id'] if group else None, cid))
            s.log(c, 'edit_kill', cid, uid, f'{boss_scope.BOSSES[c.boss]}模糊时间录入：{at}，范围波动，不计击杀')
            for prior in recipients(c, row):
                if prior != uid:
                    notice(c, s, prior, cid, f'{user["nickname"]} 已录入 {cid:02d} 线模糊时间，范围波动。')
            return
        # System forecasts never create kills. Timestamp corrections never duplicate an existing kill.
        # A real kill during the just-finished grace window still belongs to
        # the forecast round, even if it is recorded after automatic release.
        forecast_window_start = row['anchor_at'] - GRACE if row['auto_rounds'] else row['anchor_at']
        actual_event = op == 'kill' or opening or bool(row['estimated'] and at >= forecast_window_start)
        # Releasing a completed channel must not erase its team's correction
        # rights. Corrections keep the original scope, even if the editor has
        # since joined another team; a new kill establishes a new scope.
        last_owner, last_team = row['last_owner_id'], row['last_team_id']
        if actual_event:
            group = team(c, uid)
            last_owner = row['claimant_id'] if row['claimant_id'] is not None else uid
            last_team = row['team_id'] if row['claimant_id'] is not None else (group['id'] if group else None)
        c.execute('''UPDATE channels SET killed_at=?,anchor_at=?,estimated=0,auto_rounds=0,last_killer_id=?,
            last_owner_id=?,last_team_id=?,version=version+1 WHERE id=?''',
                  (at, at, uid if actual_event else row['last_killer_id'], last_owner, last_team, cid))
        if actual_event:
            release(c, cid)
            s.log(c, 'kill', cid, uid, '开荒' if opening else f'{boss_scope.BOSSES[c.boss]}实际击杀')
            if (opening and row['claimant_id'] and row['claimed_at'] is not None and
                    at >= row['claimed_at']-120 and at+PERIOD>s.now() and row['credit_blocked_until']<=s.now()):
                c.execute('INSERT INTO exploration_credits VALUES(?,?,?,?,?)', (cid, uid, row['claimed_at'], at, s.now()))
                c.execute('UPDATE channels SET credit_blocked_until=? WHERE id=?', (at+PERIOD, cid))
        else:
            s.log(c, 'edit_kill', cid, uid, f'{boss_scope.BOSSES[c.boss]}时间校准：{row["killed_at"]} → {at}')
    for prior in recipients(c, row):
        if prior != uid:
            verbs = {'kill': '确认击杀', 'edit-kill': '补录／校准击杀时间', 'release': '释放', 'discard': '丢弃转开荒'}
            notice(c, s, prior, cid, f'{user["nickname"]} 已对 {cid:02d} 线执行「{verbs[op]}」。')


def team_action(c, s, user, op, data):
    uid, t = user['id'], team(c, user['id'])
    admin = user['role'] == 'superadmin'
    if op == 'create':
        s.require(user['role'] in ('member', 'admin'), '超级管理员请使用队伍管理，不创建成员队伍', 403)
        s.require(not t, '你已经在队伍中', 409)
        name = str(data.get('name', '')).strip()
        s.require(1 <= len(name) <= 30, '队伍名称需 1～30 字', 400)
        result = c.execute('INSERT INTO clock_teams(name,captain_id,created_at) VALUES(?,?,?)', (name, uid, s.now()))
        tid = result.lastrowid
        merge_personal(c, s, uid, tid)
        c.execute('INSERT INTO clock_team_members VALUES(?,?,?)', (uid, tid, s.now()))
    elif op in ('accept', 'decline'):
        invite = c.execute("SELECT * FROM clock_invites WHERE token=? AND user_id=? AND status='pending' AND expires_at>?",
                           (str(data.get('token', '')), uid, s.now())).fetchone()
        s.require(invite, '邀请已失效或已处理', 409)
        target = c.execute('SELECT * FROM clock_teams WHERE id=? AND active=1', (invite['team_id'],)).fetchone()
        s.require(target, '队伍已解散', 409)
        if op == 'accept':
            s.require(not t, '请先退出原队伍', 409)
            s.require(len(members(c, target['id'])) < TEAM_LIMIT, '队伍已满', 409)
            merge_personal(c, s, uid, target['id'])
            c.execute('INSERT INTO clock_team_members VALUES(?,?,?)', (uid, target['id'], s.now()))
            c.execute("UPDATE clock_invites SET status='cancelled' WHERE user_id=? AND status='pending'", (uid,))
        c.execute('UPDATE clock_invites SET status=? WHERE token=?', (op, invite['token']))
        notice(c, s, target['captain_id'], 0, f'{user["nickname"]} 已{ "加入队伍" if op == "accept" else "拒绝邀请" }。')
    else:
        if admin and data.get('team_id'):
            t = c.execute('SELECT * FROM clock_teams WHERE id=? AND active=1', (data['team_id'],)).fetchone()
        s.require(t, '队伍不存在', 409)
        if op == 'leave':
            s.require(t['captain_id'] != uid, '队长请使用解散队伍', 409)
            c.execute('DELETE FROM clock_team_members WHERE user_id=? AND team_id=?', (uid, t['id']))
            notice(c, s, t['captain_id'], 0, f'{user["nickname"]} 已退出，队伍频道保留。')
        else:
            s.require(admin or t['captain_id'] == uid, '仅队长或超级管理员可以管理队伍', 403)
            if op == 'disband':
                disband(c, s, t, uid)
                return
            if op == 'rename':
                name = str(data.get('name', '')).strip()
                s.require(1 <= len(name) <= 30, '队伍名称需 1～30 字', 400)
                c.execute('UPDATE clock_teams SET name=? WHERE id=?', (name, t['id']))
            elif op == 'kick':
                target = data.get('user_id')
                s.require(target != t['captain_id'], '移出队长请解散队伍', 409)
                s.require(c.execute('DELETE FROM clock_team_members WHERE user_id=? AND team_id=?', (target, t['id'])).rowcount == 1,
                          '该成员不在队伍中', 409)
                notice(c, s, target, 0, f'你已离开「{t["name"]}」，队伍频道保留。')
                notice(c, s, t['captain_id'], 0, '一名成员已移出队伍。')
            elif op == 'invite':
                s.require(len(members(c, t['id'])) < TEAM_LIMIT, '队伍已满', 409)
                target = c.execute('SELECT * FROM users WHERE id=?', (data.get('user_id'),)).fetchone()
                s.require(s.active(target) and target['role'] in ('member', 'admin') and s.can_access_boss(target, c.boss) and not team(c, target['id']),
                          f'该成员不可用、无{boss_scope.BOSSES[c.boss]}权限或已有队伍', 409)
                s.require(not c.execute("SELECT 1 FROM clock_invites WHERE team_id=? AND user_id=? AND status='pending' AND expires_at>?",
                                       (t['id'], target['id'], s.now())).fetchone(), '已经邀请，请等待对方处理', 409)
                c.execute('INSERT INTO clock_invites VALUES(?,?,?,?,?,?)',
                          (secrets.token_urlsafe(24), t['id'], target['id'], s.now(), s.now()+86400, 'pending'))
                notice(c, s, target['id'], 0, f'「{t["name"]}」邀请你加入，请在队伍面板接受或拒绝。')
            else:
                s.require(False, '未知队伍操作', 400)
    s.log(c, 'team_'+op, None, uid, json.dumps({k: data[k] for k in ('team_id','user_id','name') if k in data}, ensure_ascii=False))
