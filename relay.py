"""Two-phase exploration handover. All mutations share the channel transaction."""
import secrets


def init(c):
    c.execute('CREATE TABLE IF NOT EXISTS relay_guard (id INTEGER PRIMARY KEY)')
    if not c.execute('SELECT id FROM relay_guard WHERE id=1').fetchone():
        c.execute('INSERT INTO relay_guard(id) VALUES(1)')
    c.execute('''CREATE TABLE IF NOT EXISTS exploration_relays (
        channel_id INTEGER PRIMARY KEY, request_token VARCHAR(64) NOT NULL,
        owner_id BIGINT NOT NULL, owner_claimed_at BIGINT NOT NULL,
        started_at BIGINT NOT NULL, state VARCHAR(16) NOT NULL,
        receiver_id BIGINT NULL, slot VARCHAR(32) NULL, arrival_until BIGINT NULL,
        invited_at BIGINT NOT NULL, relay_count INTEGER NOT NULL DEFAULT 0,
        previous_owner_id BIGINT NULL)''')
    if hasattr(c, 'raw'):
        exists = c.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='exploration_relays' AND column_name='cycle_killed_at'").fetchone()
    else:
        exists = any(r['name'] == 'cycle_killed_at' for r in c.execute('PRAGMA table_info(exploration_relays)'))
    if not exists:
        c.execute('ALTER TABLE exploration_relays ADD COLUMN cycle_killed_at BIGINT NULL')


def lock(c, s):
    # One small guard orders sweep, transfers, reservations and ordinary claim writes.
    c.execute('SELECT id FROM relay_guard WHERE id=1' + (' FOR UPDATE' if s.MYSQL else '')).fetchone()


def owner(ch, s):
    if not ch:
        return None, None
    if ch['parallel_id']:
        return ch['parallel_id'], ch['parallel_claimed_at']
    return (ch['claimant_id'], ch['claimed_at']) if ch['claimant_id'] else (s.exploration_owner(ch), s.exploration_time(ch))


def reservation(c, uid):
    return c.execute("SELECT * FROM exploration_relays WHERE state='travel' AND receiver_id=?", (uid,)).fetchone()


def reserved_slots(c, uid):
    row = reservation(c, uid)
    return {row['slot']} if row else set()


def free_slot(c, uid, s, timed=False):
    if reservation(c, uid):
        return None
    if not c.execute('SELECT id FROM channels WHERE claimant_id=?', (uid,)).fetchone():
        return 'claimant_id'
    if timed:
        return None if c.execute('SELECT id FROM channels WHERE parallel_id=?', (uid,)).fetchone() else 'parallel_id'
    extras = s.user_explorations(c, uid)
    used = {col for row in extras for col, _ in s.EXPLORATION_SLOTS if row[col] == uid}
    return next((col for col, _ in s.EXPLORATION_SLOTS if col not in used), None)


def notice(c, s, uid, channel, kind):
    if uid:
        c.execute('INSERT INTO notifications(user_id,channel_id,kind,created_at) VALUES(?,?,?,?)',
                  (uid, channel, kind, s.now()))


def reset_request(c, row, state):
    c.execute('''UPDATE exploration_relays SET state=?,request_token=?,receiver_id=NULL,
        slot=NULL,arrival_until=NULL WHERE channel_id=?''', (state, secrets.token_urlsafe(32), row['channel_id']))


def sweep(c, s):
    changed = False
    for row in c.execute("SELECT * FROM exploration_relays WHERE state<>'ended'").fetchall():
        ch = c.execute('SELECT * FROM channels WHERE id=?', (row['channel_id'],)).fetchone()
        sender = c.execute('SELECT * FROM users WHERE id=?', (row['owner_id'],)).fetchone()
        valid = (owner(ch, s) == (row['owner_id'], row['owner_claimed_at'])
                 and ch['killed_at'] == row['cycle_killed_at']
                 and s.active(sender) and ch['blocked_until'] <= s.now())
        if not valid:
            reset_request(c, row, 'ended')
            if row['receiver_id']:
                notice(c, s, row['receiver_id'], row['channel_id'], 'relay_cancelled')
            if row['state'] in ('open', 'travel'):
                s.log(c, 'relay_cancel', row['channel_id'], None, '频道或发起人状态变化，接力取消')
            changed = True
        elif row['state'] == 'travel':
            receiver = c.execute('SELECT * FROM users WHERE id=?', (row['receiver_id'],)).fetchone()
            col = row['slot']
            occupied = c.execute(f'SELECT id FROM channels WHERE {col}=?', (row['receiver_id'],)).fetchone()
            if row['arrival_until'] <= s.now() or not s.active(receiver) or s.claim_cooldown(c, receiver) or occupied:
                reset_request(c, row, 'open')
                notice(c, s, row['receiver_id'], row['channel_id'], 'relay_reopened')
                notice(c, s, row['owner_id'], row['channel_id'], 'relay_reopened')
                s.log(c, 'relay_reopen', row['channel_id'], None, '到达超时或接手人状态变化，邀请重新开放')
                changed = True
    return changed


def handle(c, s, path, user, ch, data):
    uid, t = user['id'], s.now()
    channel = int(data.get('channel_id', 0))
    row = c.execute('SELECT * FROM exploration_relays WHERE channel_id=?', (channel,)).fetchone()
    if path == '/api/relay/invite':
        holder, claimed = owner(ch, s)
        s.require(holder == uid and ch['blocked_until'] <= t and
                  (ch['killed_at'] is None or ch['parallel_id'] == uid), '只能邀请接力自己的开荒或并发频道')
        s.require(not row or row['state'] not in ('open', 'travel'), '接力邀请已发出', 409)
        if row and row['state'] == 'idle' and (row['owner_id'], row['owner_claimed_at']) == (uid, claimed):
            c.execute("UPDATE exploration_relays SET state='open',request_token=?,invited_at=? WHERE channel_id=?",
                      (secrets.token_urlsafe(32), t, channel))
        else:
            c.execute('DELETE FROM exploration_relays WHERE channel_id=?', (channel,))
            c.execute('''INSERT INTO exploration_relays(channel_id,request_token,owner_id,owner_claimed_at,
                started_at,state,invited_at,cycle_killed_at) VALUES(?,?,?,?,?,'open',?,?)''',
                      (channel, secrets.token_urlsafe(32), uid, claimed, claimed, t, ch['killed_at']))
        s.log(c, 'relay_invite', channel, uid, '发起并发接力，原认领保留' if ch['killed_at'] else '发起开荒接力，原认领保留')
        return
    s.require(row and row['request_token'] == data.get('request_id'), '接力邀请已变化，请刷新', 409)
    if path == '/api/relay/cancel':
        s.require(row['owner_id'] == uid or user['role'] == 'superadmin', '无权撤销邀请')
        s.require(row['state'] in ('open', 'travel'), '邀请已结束', 409)
        notice(c, s, row['receiver_id'], channel, 'relay_cancelled')
        reset_request(c, row, 'idle')
        s.log(c, 'relay_cancel', channel, uid, '撤销接力邀请，原认领保留')
    elif path == '/api/relay/accept':
        s.require(row['state'] == 'open', '已有人接受邀请，请选择其他频道', 409)
        s.require(row['owner_id'] != uid, '不能接受自己的邀请')
        s.require(not s.claim_cooldown(c, user), '认领冻结期间不能接受接力')
        slot = free_slot(c, uid, s, row['cycle_killed_at'] is not None)
        s.require(slot, '对应认领名额已满，或已有一条正在前往的接力', 409)
        if slot == 'parallel_id':
            cooperation = s.cooperation_status(c, user)
            s.require(not cooperation or not cooperation['parallel_blocked'], s.COOPERATION_BLOCK_REASON, 409)
        c.execute("UPDATE exploration_relays SET state='travel',receiver_id=?,slot=?,arrival_until=? WHERE channel_id=?",
                  (uid, slot, t+180, channel))
        s.log(c, 'relay_accept', channel, uid, f'接受用户 {row["owner_id"]} 的邀请，预留名额，等待到达')
    elif path == '/api/relay/leave':
        s.require(row['state'] == 'travel' and row['receiver_id'] == uid, '只能取消自己的前往状态', 409)
        reset_request(c, row, 'open')
        notice(c, s, row['owner_id'], channel, 'relay_reopened')
        s.log(c, 'relay_leave', channel, uid, '取消前往，邀请重新开放')
    elif path == '/api/relay/arrive':
        s.require(row['state'] == 'travel' and row['receiver_id'] == uid and row['arrival_until'] > t,
                  '接力已超时或已取消，请刷新', 409)
        s.require(not s.claim_cooldown(c, user), '认领冻结期间不能接手')
        s.require(owner(ch, s) == (row['owner_id'], row['owner_claimed_at']) and ch['blocked_until'] <= t,
                  '频道状态已变化，无法交接', 409)
        s.require(ch['killed_at'] == row['cycle_killed_at'], '频道计时已变化，无法交接', 409)
        slot = row['slot']
        time_col = dict([('claimant_id', 'claimed_at'), ('parallel_id', 'parallel_claimed_at'), *s.EXPLORATION_SLOTS])[slot]
        s.require(not c.execute(f'SELECT id FROM channels WHERE {slot}=?', (uid,)).fetchone(), '预留名额已不可用', 409)
        c.execute('''UPDATE channels SET claimant_id=NULL,claimed_at=NULL,explorer_id=NULL,
            exploration_claimed_at=NULL,explorer2_id=NULL,exploration2_claimed_at=NULL,
            explorer3_id=NULL,exploration3_claimed_at=NULL,parallel_id=NULL,parallel_claimed_at=NULL,
            sos_at=NULL,sos_token=NULL WHERE id=?''', (channel,))
        c.execute(f'UPDATE channels SET {slot}=?,{time_col}=? WHERE id=?', (uid, t, channel))
        c.execute('''UPDATE exploration_relays SET state='idle',previous_owner_id=owner_id,owner_id=?,
            owner_claimed_at=?,receiver_id=NULL,slot=NULL,arrival_until=NULL,relay_count=relay_count+1,
            request_token=? WHERE channel_id=?''', (uid, t, secrets.token_urlsafe(32), channel))
        s.log(c, 'relay_arrive', channel, uid,
              f'接手用户 {row["owner_id"]}，本轮开始:{row["started_at"]} 原成员本段:{t-row["owner_claimed_at"]}秒；无冻结')
        notice(c, s, row['owner_id'], channel, 'relay_completed')
        notice(c, s, uid, channel, 'relay_received')
    else:
        raise s.ApiError(404, '接力接口不存在')


def view(c, s, user):
    uid = user['id']
    names = {r['id']: r['nickname'] for r in c.execute('SELECT id,nickname FROM users')}
    result = []
    cooperation = s.cooperation_status(c, user) if c.boss == 'mushroom' else None
    for row in c.execute("SELECT * FROM exploration_relays WHERE state IN ('open','travel','idle') ORDER BY started_at,invited_at"):
        if row['state'] == 'idle' and row['owner_id'] != uid:
            continue
        ch = c.execute('SELECT * FROM channels WHERE id=?', (row['channel_id'],)).fetchone()
        if owner(ch, s) != (row['owner_id'], row['owner_claimed_at']) or ch['killed_at'] != row['cycle_killed_at']:
            continue
        timed = row['cycle_killed_at'] is not None
        available_slot = free_slot(c, uid, s, timed)
        reason = ('认领冻结中' if s.claim_cooldown(c, user) else
                  '已有前往中的接力或对应认领名额已满' if not available_slot else '')
        if not reason and available_slot == 'parallel_id' and cooperation and cooperation['parallel_blocked']:
            reason = s.COOPERATION_BLOCK_REASON
        item = dict(row)
        item.update(kind='parallel' if timed else 'exploration',
                    earliest_at=ch['killed_at']+s.boss_scope.TIMING[c.boss][0] if timed else None,
                    latest_at=ch['killed_at']+s.boss_scope.TIMING[c.boss][1] if timed else None)
        item.update(owner_name=names.get(row['owner_id'], '成员'), receiver_name=names.get(row['receiver_id'], ''),
                    previous_owner_name=names.get(row['previous_owner_id'], ''), note=ch['note'],
                    accept_reason=reason, accept_slot=available_slot)
        result.append(item)
    return result
