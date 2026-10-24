import concurrent.futures
import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import server


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_password = os.environ.get('ADMIN_PASSWORD')
        os.environ['ADMIN_PASSWORD'] = 'TestOnlyAdminPassword123!'
        cls.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = os.path.join(cls.tmp.name, 'test.db')
        server.init_db()
        with server.db() as c:
            for name in ('alice', 'bob'):
                c.execute('INSERT INTO users(username,nickname,password_hash,access_until,created_at) VALUES(?,?,?,?,?)',
                          (name, name, server.hash_password('Password123'), server.now()+86400, server.now()))
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.thread.join(timeout=3)
        if cls.previous_password is None: os.environ.pop('ADMIN_PASSWORD', None)
        else: os.environ['ADMIN_PASSWORD'] = cls.previous_password
        try: cls.tmp.cleanup()
        except PermissionError: pass  # Windows may retain a short-lived HTTP worker handle.

    def request(self, path, data=None, cookie=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if cookie: headers['Cookie'] = cookie
        c.request('GET' if data is None else 'POST', '/api/'+path, json.dumps(data).encode() if data is not None else None, headers)
        r = c.getresponse(); body = json.loads(r.read()); status = r.status; set_cookie = r.getheader('Set-Cookie'); c.close()
        return status, body, set_cookie

    def login(self, name):
        status, _, cookie = self.request('login', {'username': name, 'password': 'Password123'})
        self.assertEqual(status, 200)
        return cookie.split(';')[0]

    def setUp(self):
        with server.db() as c:
            c.execute('''UPDATE channels SET killed_at=NULL,last_killer_id=NULL,claimant_id=NULL,
                claimed_at=NULL,sos_at=NULL,sos_token=NULL,explorer_id=NULL,exploration_claimed_at=NULL,
                explorer2_id=NULL,exploration2_claimed_at=NULL,explorer3_id=NULL,
                exploration3_claimed_at=NULL,blocked_until=0,exploded_at=NULL,note='' ''')
            c.execute('UPDATE channels SET killed_at=? WHERE id IN (1,2)', (server.now()-100,))
            c.execute('UPDATE users SET banned_until=0,frozen=0,session_version=0')
            c.execute('DELETE FROM release_cooldowns')
            c.execute('DELETE FROM notifications')
            c.execute('DELETE FROM robbery_reports')
            c.execute('DELETE FROM logs')
            c.execute('DELETE FROM login_records')

    def test_admin_bulk_temporary_blacklist_blocks_new_claims_and_expires(self):
        alice, bob = self.login('alice'), self.login('bob')
        root = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})[2].split(';')[0]
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        payload = {'channel_ids': [1, 3, 4], 'action': 'block', 'minutes': 30}
        self.assertEqual(self.request('admin/channels/blacklist', payload, bob)[0], 403)
        self.assertEqual(self.request('admin/channels/blacklist', payload, root)[0], 200)
        view = self.request('state', cookie=alice)[1]
        self.assertEqual([c['id'] for c in view['own']], [3])
        self.assertNotIn(4, view['unknown'])
        self.assertNotIn(1, [c['id'] for c in self.request('state', cookie=root)[1]['spawn_pool']])
        self.assertEqual(self.request('claim-exploration', {'channel_id': 4}, alice)[0], 409)
        self.assertEqual(self.request('claim', {'channel_id': 4}, bob)[0], 409)
        with server.db() as c:
            bob_id = c.execute("SELECT id FROM users WHERE username='bob'").fetchone()['id']
        self.assertEqual(self.request('admin/assign', {'channel_id': 4, 'user_id': bob_id}, root)[0], 400)
        self.assertEqual(self.request('kill', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('admin/channels/blacklist', {'channel_ids': [1, 4], 'action': 'unblock'}, root)[0], 200)
        self.assertIn(4, self.request('state', cookie=bob)[1]['unknown'])
        self.assertIn(1, [c['id'] for c in self.request('state', cookie=root)[1]['spawn_pool']])
        with server.db() as c:
            c.execute('UPDATE channels SET blocked_until=? WHERE id=5', (server.now()-1,))
        self.assertIn(5, self.request('state', cookie=bob)[1]['unknown'])
        self.assertEqual(self.request('claim', {'channel_id': 5}, bob)[0], 200)

    def test_member_presence_counts_kills_and_recent_session_activity(self):
        alice, bob = self.login('alice'), self.login('bob')
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('kill', {'channel_id': 3}, alice)[0], 200)
        with server.db() as c:
            uid = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute("INSERT INTO logs(at,actor_id,action,detail) VALUES(?,?,?,?)",
                      (server.beijing_day_start()-1, uid, 'kill', '开荒'))
        members = self.request('state', cookie=bob)[1]['members']
        by_name = {m['nickname']: m for m in members}
        self.assertTrue(by_name['alice']['online'])
        self.assertTrue(by_name['bob']['online'])
        self.assertEqual(by_name['alice']['kills'], 1)
        self.assertEqual(by_name['bob']['kills'], 0)
        self.assertNotIn('username', by_name['alice'])

    def test_member_presence_uses_last_web_action_not_background_refresh(self):
        self.login('alice')
        bob = self.login('bob')
        with server.db() as c:
            uid = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute('UPDATE sessions SET last_seen_at=?,last_action_at=? WHERE user_id=?',
                      (server.now()-120, server.now()-1790, uid))
        people = {m['nickname']: m for m in self.request('state', cookie=bob)[1]['members']}
        self.assertEqual(people['alice']['online'], 1)
        with server.db() as c:
            c.execute('UPDATE sessions SET last_seen_at=?,last_action_at=? WHERE user_id=?',
                      (server.now(), server.now()-1801, uid))
        people = {m['nickname']: m for m in self.request('state', cookie=bob)[1]['members']}
        self.assertEqual(people['alice']['online'], 0)

    def test_bulk_blacklist_validation_is_atomic(self):
        root = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})[2].split(';')[0]
        for ids, minutes in (([1, 61], 10), ([1, 1], 10), ([1], 0), ([1], 43201)):
            self.assertEqual(self.request('admin/channels/blacklist',
                             {'channel_ids': ids, 'action': 'block', 'minutes': minutes}, root)[0], 400)
        with server.db() as c:
            self.assertEqual(c.execute('SELECT blocked_until FROM channels WHERE id=1').fetchone()['blocked_until'], 0)

    def test_new_login_kicks_previous_session_and_event_stream(self):
        first = self.login('alice')
        stream = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            stream.request('GET', '/api/events', headers={'Cookie': first})
            response = stream.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'event: update', response.fp.readline())
            second = self.login('alice')
            self.assertEqual(self.request('state', cookie=first)[0], 401)
            self.assertEqual(self.request('state', cookie=second)[0], 200)
            lines = [response.fp.readline() for _ in range(5)]
            self.assertTrue(any(b'event: session-revoked' in line for line in lines))
            with server.db() as c:
                user_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
                self.assertEqual(c.execute('SELECT count(*) AS n FROM sessions WHERE user_id=?', (user_id,)).fetchone()['n'], 1)
        finally:
            stream.close()

    def test_parallel_logins_leave_only_latest_session_active(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.login('alice'), range(2)))
        statuses = [self.request('state', cookie=cookie)[0] for cookie in results]
        self.assertEqual(sorted(statuses), [200, 401])

    def test_atomic_claim_and_private_state(self):
        a, b = self.login('alice'), self.login('bob')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.request, 'claim', {'channel_id': 3}, cookie) for cookie in (a, b)]
            results = [f.result() for f in futures]
        self.assertEqual(sorted(r[0] for r in results), [200, 409])
        loser = b if results[0][0] == 200 else a
        _, view, _ = self.request('state', cookie=loser)
        self.assertEqual(len(view['channels']), 60)
        self.assertNotEqual(view['recommendation']['id'], 3)
        self.assertNotIn(3, view['unknown'])

    def test_overdue_channels_return_to_unknown_after_guarantee_grace(self):
        alice, bob = self.login('alice'), self.login('bob')
        fixed = server.now()
        threshold = fixed - 3600 - server.UNKNOWN_AFTER_GUARANTEE
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=55',
                      (threshold + 1, alice_id))
            c.execute('''UPDATE channels SET killed_at=?,last_killer_id=?,claimant_id=?,claimed_at=?
                WHERE id=56''', (threshold - 60, alice_id, alice_id, fixed))
        with patch.object(server, 'now', return_value=fixed):
            self.assertEqual(self.request('state', cookie=alice)[1]['last_kill']['id'], 55)
            with server.db() as c:
                c.execute('UPDATE channels SET killed_at=? WHERE id=55', (threshold,))
            view = self.request('state', cookie=bob)[1]
            self.assertIn(55, view['unknown'])
            with server.db() as c:
                stale = c.execute('SELECT * FROM channels WHERE id=55').fetchone()
                claimed = c.execute('SELECT * FROM channels WHERE id=56').fetchone()
                reset = c.execute("SELECT count(*) AS n FROM logs WHERE action='reset_unknown' AND channel_id=55").fetchone()
            self.assertIsNone(stale['killed_at'])
            self.assertIsNone(stale['last_killer_id'])
            self.assertEqual(stale['exploded_at'], fixed)
            self.assertIsNone(claimed['claimant_id'])
            self.assertIsNone(claimed['killed_at'])
            self.assertEqual(claimed['exploded_at'], fixed)
            self.assertEqual(reset['n'], 1)
            owner_view = self.request('state', cookie=alice)[1]
            self.assertIn(56, owner_view['unknown'])
            self.assertEqual(owner_view['claim_blocked_until'], 0)
            self.assertEqual(owner_view['release_count'], 0)
            self.assertEqual(owner_view['notices'][0]['kind'], 'guarantee_expired')
            self.assertEqual(owner_view['notices'][0]['channel_id'], 56)
            self.assertEqual(self.request('claim', {'channel_id': 55}, bob)[0], 200)
            self.assertEqual(self.request('state', cookie=bob)[1]['channels'][54]['exploded_at'], fixed)
            self.assertEqual(self.request('kill', {'channel_id': 55}, bob)[0], 200)
            self.assertIsNone(self.request('state', cookie=bob)[1]['channels'][54]['exploded_at'])

    def test_robbery_report_requires_claim_and_ranks_full_player_id(self):
        a, b = self.login('alice'), self.login('bob')
        report = {'channel_id': 3, 'player_id': '玩家完整ID-001'}
        self.assertEqual(self.request('report-robbed', report, a)[0], 403)
        self.assertEqual(self.request('claim', {'channel_id': 3}, a)[0], 200)
        self.assertEqual(self.request('report-robbed', {'channel_id': 3, 'player_id': '  '}, a)[0], 400)
        self.assertEqual(self.request('report-robbed', report, b)[0], 403)
        self.assertEqual(self.request('sos', {'channel_id': 3}, a)[0], 200)
        before = server.now()
        self.assertEqual(self.request('report-robbed', report, a)[0], 200)
        after = server.now()
        self.assertEqual(self.request('report-robbed', report, a)[0], 403)
        with server.db() as c:
            channel = c.execute('SELECT * FROM channels WHERE id=3').fetchone()
        self.assertGreaterEqual(channel['killed_at'], before)
        self.assertLessEqual(channel['killed_at'], after)
        self.assertIsNone(channel['last_killer_id'])
        self.assertIsNone(channel['claimant_id'])
        self.assertIsNone(channel['claimed_at'])
        self.assertIsNone(channel['sos_at'])
        _, view, _ = self.request('state', cookie=a)
        self.assertEqual(view['own'], [])
        self.assertEqual(view['claim_blocked_until'], 0)
        self.assertEqual(self.request('claim', {'channel_id': 5}, a)[0], 200)
        self.assertEqual(self.request('claim', {'channel_id': 4}, b)[0], 200)
        self.assertEqual(self.request('report-robbed', {'channel_id': 4, 'player_id': '玩家完整ID-001'}, b)[0], 200)
        _, view, _ = self.request('state', cookie=a)
        self.assertEqual(view['robbery_ranking'][0]['player_id'], '玩家完整ID-001')
        self.assertEqual(view['robbery_ranking'][0]['reports'], 2)
        self.assertEqual(view['own'][0]['id'], 5)

    def test_sos_transfer_and_ban(self):
        a, b = self.login('alice'), self.login('bob')
        self.assertEqual(self.request('claim', {'channel_id': 3}, a)[0], 200)
        self.assertEqual(self.request('claim', {'channel_id': 1}, b)[0], 200)
        self.assertEqual(self.request('sos', {'channel_id': 3}, a)[0], 200)
        self.assertEqual(self.request('sos', {'channel_id': 3}, a)[0], 409)
        self.assertIsNotNone(self.request('state', cookie=a)[1]['own'][0]['sos_at'])
        _, view, _ = self.request('state', cookie=b)
        self.assertEqual(len(view['sos']), 1)
        self.assertNotIn('id', view['sos'][0])
        self.assertNotIn('channel', view['sos'][0])
        item = view['sos'][0]
        self.assertNotIn('nickname', item)
        self.assertEqual(self.request('accept-sos', {'request_id': 'x'*43}, b)[0], 409)
        self.assertEqual(self.request('state', cookie=b)[1]['own'][0]['id'], 1)
        self.assertEqual(self.request('accept-sos', {'request_id': item['request_id']}, b)[0], 200)
        _, view, _ = self.request('state', cookie=b)
        self.assertEqual(view['own'][0]['id'], 3)
        self.assertEqual(view['claim_blocked_until'], 0)
        self.assertEqual(view['release_count'], 0)
        released = self.request('state', cookie=a)[1]
        self.assertEqual(released['own'], [])
        self.assertEqual(released['notices'][0]['kind'], 'sos_accepted')
        self.assertEqual(released['notices'][0]['channel_id'], 3)
        status, _, root_cookie = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})
        self.assertEqual(status, 200)
        root = root_cookie.split(';')[0]
        self.assertIn(1, [channel['id'] for channel in self.request('state', cookie=root)[1]['spawn_pool']])
        with server.db() as c:
            uid = c.execute("SELECT id FROM users WHERE username='bob'").fetchone()[0]
        self.assertEqual(self.request('admin/user', {'user_id': uid, 'action': 'ban', 'minutes': 60}, root)[0], 200)
        self.assertEqual(self.request('state', cookie=b)[0], 401)
        with server.db() as c:
            self.assertIsNone(c.execute('SELECT claimant_id FROM channels WHERE id=3').fetchone()[0])

    def test_admin_can_create_member_without_sms(self):
        status, _, root_cookie = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})
        self.assertEqual(status, 200)
        cookie = root_cookie.split(';')[0]
        self.assertEqual(self.request('admin/create-user', {'username': 'shortreject', 'nickname': '短密码',
                         'password': '12345678', 'days': 1}, cookie)[0], 400)
        self.assertEqual(self.request('admin/create-user', {'username': 'newmember', 'nickname': '新成员',
                         'password': '123456789', 'days': 1}, cookie)[0], 200)
        status, _, _ = self.request('login', {'username': 'newmember', 'password': '123456789'})
        self.assertEqual(status, 200)

    def test_former_admin_is_demoted_and_cannot_assign(self):
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            bob_id = c.execute("SELECT id FROM users WHERE username='bob'").fetchone()['id']
            c.execute("UPDATE users SET role='admin' WHERE id=?", (bob_id,))
        try:
            old_session = self.login('bob')
            server.init_db()
            self.assertEqual(self.request('state', cookie=old_session)[0], 401)
            with server.db() as c:
                self.assertEqual(c.execute('SELECT role FROM users WHERE id=?', (bob_id,)).fetchone()['role'], 'member')
            ordinary = self.login('bob')
            view = self.request('state', cookie=ordinary)[1]
            self.assertEqual(view['me']['role'], 'member')
            self.assertEqual(len(view['channels']), 60)
            self.assertNotIn('users', view)
            self.assertNotIn('assignable_users', view)
            self.assertEqual(self.request('admin/assign', {'channel_id': 3, 'user_id': alice_id}, ordinary)[0], 403)
            self.assertEqual(self.request('admin/user', {'user_id': alice_id, 'action': 'freeze'}, ordinary)[0], 403)
        finally:
            with server.db() as c:
                c.execute("UPDATE users SET role='member' WHERE id=?", (bob_id,))

    def test_admin_delete_member_revokes_access_releases_claim_and_keeps_audit(self):
        alice = self.login('alice')
        root = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})[2].split(';')[0]
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            root_id = c.execute("SELECT id FROM users WHERE username='root'").fetchone()['id']
            c.execute('UPDATE users SET registration_ip=? WHERE id=?', ('192.0.2.123', alice_id))
        self.assertEqual(self.request('admin/user', {'user_id': root_id, 'action': 'delete'}, root)[0], 403)
        self.assertEqual(self.request('admin/user', {'user_id': alice_id, 'action': 'delete'}, root)[0], 200)
        self.assertEqual(self.request('state', cookie=alice)[0], 401)
        self.assertEqual(self.request('login', {'username': 'alice', 'password': 'Password123'})[0], 401)
        admin_view = self.request('state', cookie=root)[1]
        self.assertNotIn(alice_id, [u['id'] for u in admin_view['users']])
        with server.db() as c:
            deleted = c.execute('SELECT * FROM users WHERE id=?', (alice_id,)).fetchone()
            channel = c.execute('SELECT claimant_id FROM channels WHERE id=3').fetchone()
            audit = c.execute("SELECT count(*) AS n FROM logs WHERE action='user_delete' AND detail=?",
                              (f'用户 {alice_id}',)).fetchone()['n']
        self.assertIsNotNone(deleted['deleted_at'])
        self.assertEqual(deleted['nickname'], 'alice')
        self.assertIsNone(deleted['registration_ip'])
        self.assertIsNone(channel['claimant_id'])
        self.assertEqual(audit, 1)
        self.assertEqual(self.request('admin/create-user', {'username': 'alice', 'nickname': 'alice',
            'password': 'Password123', 'days': 1}, root)[0], 200)

    def test_rankings_use_beijing_day_and_count_only_completed_openings(self):
        start = server.beijing_day_start()
        end = start + server.DAY
        self.assertEqual(server.beijing_day_start(start - 1), start - server.DAY)
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            bob_id = c.execute("SELECT id FROM users WHERE username='bob'").fetchone()['id']
            events = [
                (start - 1, 3, alice_id, 'kill', '开荒'),
                (start, 3, alice_id, 'kill', '开荒'),
                (start + 1, 4, bob_id, 'kill', ''),
                (start + 2, 5, bob_id, 'kill', '开荒'),
                (end - 1, 5, alice_id, 'accept_sos', ''),
                (end, 5, bob_id, 'accept_sos', ''),
            ]
            c.executemany('INSERT INTO logs(at,channel_id,actor_id,action,detail) VALUES(?,?,?,?,?)', events)
            c.executemany('''INSERT INTO robbery_reports(channel_id,reporter_id,claim_started_at,player_id,reported_at)
                VALUES(?,?,?,?,?)''', [(3, alice_id, start + 3, '敌人甲', start + 3),
                                   (4, bob_id, start + 4, '敌人甲', start + 4),
                                   (5, bob_id, start + 5, '敌人乙', end)])
            with patch.object(server, 'now', return_value=start + 3600):
                ranks = server.weekly_rankings(c)
        self.assertEqual(ranks['starts_at'], start)
        self.assertEqual([(r['nickname'], r['total']) for r in ranks['exploration']], [('alice', 1), ('bob', 1)])
        self.assertEqual([(r['nickname'], r['total']) for r in ranks['rescue']], [('alice', 1)])
        self.assertEqual([(r['player_id'], r['reports']) for r in ranks['robbery']], [('敌人甲', 2)])

    def test_only_unknown_channel_kill_counts_as_opening(self):
        alice = self.login('alice')
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('kill', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('kill', {'channel_id': 3}, alice)[0], 200)
        with server.db() as c:
            details = [row['detail'] for row in c.execute("SELECT detail FROM logs WHERE action='kill' AND channel_id=3 ORDER BY id")]
            ranking = server.weekly_rankings(c)['exploration']
        self.assertEqual(details, ['开荒', ''])
        self.assertEqual(ranking[0]['total'], 1)
    def test_self_registration_ip_limit_and_recovery_reset(self):
        self.assertEqual(self.request('sms/request', {'phone': '13800138000'})[0], 404)
        status, result, cookie = self.request('register', {'username': 'selfsignup',
            'nickname': '新成员', 'password': 'RegisteredPassword123!'})
        self.assertEqual(status, 200)
        self.assertTrue(result['pending_approval'])
        self.assertIsNone(cookie)
        self.assertGreaterEqual(len(result['recovery_code']), 40)
        self.assertEqual(self.request('state')[0], 401)
        self.assertEqual(self.request('login', {'username': 'selfsignup',
            'password': 'RegisteredPassword123!'})[0], 403)
        self.assertEqual(self.request('register', {'username': 'secondselfsignup',
            'nickname': '第二成员', 'password': 'RegisteredPassword123!'})[0], 409)
        with server.db() as c:
            row = c.execute("SELECT * FROM users WHERE username='selfsignup'").fetchone()
            self.assertEqual(row['role'], 'member')
            self.assertEqual(row['access_until'], 0)
            self.assertEqual(row['pending_approval'], 1)
            self.assertIsNone(row['phone'])
            self.assertEqual(row['registration_ip'], '127.0.0.1')
            self.assertNotEqual(row['recovery_hash'], result['recovery_code'])
            self.assertIsNone(c.execute("SELECT id FROM users WHERE username='secondselfsignup'").fetchone())
        self.assertEqual(self.request('password/reset', {'username': 'selfsignup',
            'recovery_code': 'wrong', 'new_password': 'NewPassword123!'})[0], 401)
        status, reset, _ = self.request('password/reset', {'username': 'selfsignup',
            'recovery_code': result['recovery_code'], 'new_password': 'NewPassword123!'})
        self.assertEqual(status, 200)
        self.assertNotEqual(reset['recovery_code'], result['recovery_code'])
        self.assertEqual(self.request('login', {'username': 'selfsignup', 'password': 'RegisteredPassword123!'})[0], 401)
        self.assertEqual(self.request('login', {'username': 'selfsignup', 'password': 'NewPassword123!'})[0], 403)
        alice = self.login('alice')
        with server.db() as c:
            user_id = c.execute("SELECT id FROM users WHERE username='selfsignup'").fetchone()['id']
        self.assertEqual(self.request('admin/user', {'user_id': user_id, 'action': 'approve'}, alice)[0], 403)
        root = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})[2].split(';')[0]
        self.assertTrue(next(u for u in self.request('state', cookie=root)[1]['users']
                             if u['id'] == user_id)['pending_approval'])
        self.assertEqual(self.request('admin/user', {'user_id': user_id, 'action': 'approve'}, root)[0], 200)
        self.assertEqual(self.request('admin/user', {'user_id': user_id, 'action': 'approve'}, root)[0], 409)
        with server.db() as c:
            row = c.execute('SELECT access_until,pending_approval FROM users WHERE id=?', (user_id,)).fetchone()
            self.assertEqual(row['pending_approval'], 0)
            self.assertGreaterEqual(row['access_until'] - server.now(), server.DAY - 2)
            self.assertLessEqual(row['access_until'] - server.now(), server.DAY)
        self.assertEqual(self.request('login', {'username': 'selfsignup', 'password': 'NewPassword123!'})[0], 200)
        self.assertEqual(self.request('password/reset', {'username': 'selfsignup',
            'recovery_code': result['recovery_code'], 'new_password': 'AnotherPassword123!'})[0], 401)

    def test_logged_in_password_change_rotates_recovery_and_session(self):
        first = self.login('alice')
        status, result, _ = self.request('password/change', {'current_password': 'Password123',
            'new_password': 'ChangedPassword123!'}, first)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(result['recovery_code']), 40)
        self.assertEqual(self.request('state', cookie=first)[0], 401)
        self.assertEqual(self.request('login', {'username': 'alice', 'password': 'ChangedPassword123!'})[0], 200)
        with server.db() as c:
            c.execute('UPDATE users SET password_hash=?,recovery_hash=NULL WHERE username=?',
                      (server.hash_password('Password123'), 'alice'))

    def test_existing_account_receives_one_time_recovery_code_on_login(self):
        with server.db() as c:
            c.execute('INSERT INTO users(username,nickname,password_hash,access_until,created_at) VALUES(?,?,?,?,?)',
                      ('legacyrecovery', '旧成员', server.hash_password('Password123'), server.now() + server.DAY, server.now()))
        status, first, _ = self.request('login', {'username': 'legacyrecovery', 'password': 'Password123'})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(first['recovery_code']), 40)
        status, second, _ = self.request('login', {'username': 'legacyrecovery', 'password': 'Password123'})
        self.assertEqual(status, 200)
        self.assertNotIn('recovery_code', second)

    def test_logged_in_old_account_can_generate_new_recovery_code(self):
        bob = self.login('bob')
        self.assertEqual(self.request('password/recovery', {'current_password': 'wrong'}, bob)[0], 401)
        status, result, _ = self.request('password/recovery', {'current_password': 'Password123'}, bob)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(result['recovery_code']), 40)
        self.assertEqual(self.request('state', cookie=bob)[0], 200)
        with server.db() as c:
            row = c.execute("SELECT recovery_hash FROM users WHERE username='bob'").fetchone()
            self.assertTrue(server.check_password(result['recovery_code'], row['recovery_hash']))

    def test_admin_issues_one_time_key_for_member_chosen_password(self):
        bob = self.login('bob')
        root = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})[2].split(';')[0]
        with server.db() as c:
            bob_id = c.execute("SELECT id FROM users WHERE username='bob'").fetchone()['id']
            root_id = c.execute("SELECT id FROM users WHERE username='root'").fetchone()['id']
            c.execute('UPDATE users SET recovery_hash=? WHERE id=?',
                      (server.hash_password('OldRecoveryCode123'), bob_id))
        self.assertEqual(self.request('admin/user', {'user_id': root_id, 'action': 'reset_password'}, root)[0], 403)
        self.assertEqual(self.request('admin/user', {'user_id': bob_id, 'action': 'reset_password'}, bob)[0], 403)
        status, result, _ = self.request('admin/user', {'user_id': bob_id, 'action': 'reset_password'}, root)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(result['reset_key']), 40)
        self.assertNotIn('temporary_password', result)
        self.assertNotIn('recovery_code', result)
        self.assertEqual(self.request('state', cookie=bob)[0], 401)
        self.assertEqual(self.request('login', {'username': 'bob', 'password': 'Password123'})[0], 401)
        self.assertEqual(self.request('login', {'username': 'bob', 'password': result['reset_key']})[0], 401)
        self.assertEqual(self.request('password/reset', {'username': 'bob',
            'recovery_code': 'OldRecoveryCode123', 'new_password': 'MemberChosen123!'})[0], 401)
        self.assertEqual(self.request('password/reset', {'username': 'bob',
            'recovery_code': 'invalid', 'new_password': 'MemberChosen123!'})[0], 401)
        first_key = result['reset_key']
        status, result, _ = self.request('admin/user', {'user_id': bob_id, 'action': 'reset_password'}, root)
        self.assertEqual(status, 200)
        self.assertNotEqual(result['reset_key'], first_key)
        self.assertEqual(self.request('password/reset', {'username': 'bob',
            'recovery_code': first_key, 'new_password': 'MemberChosen123!'})[0], 401)
        status, reset, _ = self.request('password/reset', {'username': 'bob',
            'recovery_code': result['reset_key'], 'new_password': 'MemberChosen123!'})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(reset['recovery_code']), 40)
        self.assertEqual(self.request('password/reset', {'username': 'bob',
            'recovery_code': result['reset_key'], 'new_password': 'AnotherPassword123!'})[0], 401)
        self.assertEqual(self.request('login', {'username': 'bob', 'password': 'MemberChosen123!'})[0], 200)
        with server.db() as c:
            detail = c.execute("SELECT detail FROM logs WHERE action='user_password_admin_reset' ORDER BY id DESC LIMIT 1").fetchone()['detail']
            self.assertNotIn(result['reset_key'], detail)
            c.execute('UPDATE users SET password_hash=?,recovery_hash=NULL WHERE id=?',
                      (server.hash_password('Password123'), bob_id))

    def test_member_release_has_fixed_ten_minute_cooldown(self):
        alice = self.login('alice')
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('release', {'channel_id': 3}, alice)[0], 200)
        view = self.request('state', cookie=alice)[1]
        self.assertEqual(view['own'], [])
        self.assertAlmostEqual(view['claim_blocked_until'], server.now() + 600, delta=2)
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 403)
        with server.db() as c:
            c.execute('UPDATE release_cooldowns SET ends_at=?', (server.now()-1,))
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('release', {'channel_id': 3}, alice)[0], 200)
        self.assertAlmostEqual(self.request('state', cookie=alice)[1]['claim_blocked_until'], server.now()+600, delta=2)

    def test_admin_release_does_not_penalize_member(self):
        alice = self.login('alice')
        status, _, root_cookie = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})
        self.assertEqual(status, 200)
        root = root_cookie.split(';')[0]
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('release', {'channel_id': 3}, root)[0], 200)
        released = self.request('state', cookie=alice)[1]
        self.assertEqual(released['claim_blocked_until'], 0)
        self.assertEqual(released['own'], [])
        self.assertEqual(released['notices'][0]['channel_id'], 3)
        self.assertEqual(released['notices'][0]['kind'], 'admin_release')
        bob = self.login('bob')
        notice_id = released['notices'][0]['id']
        self.assertEqual(self.request('notice/read', {'notice_id': notice_id}, bob)[0], 404)
        self.assertEqual(self.request('notice/read', {'notice_id': notice_id}, alice)[0], 200)
        self.assertEqual(self.request('state', cookie=alice)[1]['notices'], [])
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('release', {'channel_id': 3}, root)[0], 200)
        self.assertEqual(self.request('claim', {'channel_id': 3}, root)[0], 200)
        self.assertEqual(self.request('release', {'channel_id': 3}, root)[0], 200)
        self.assertEqual(self.request('claim', {'channel_id': 3}, root)[0], 200)

    def test_admin_automatic_timeout_does_not_ban_admin(self):
        root = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})[2].split(';')[0]
        self.assertEqual(self.request('claim', {'channel_id': 3}, root)[0], 200)
        with server.db() as c:
            c.execute('UPDATE channels SET claimed_at=? WHERE id=3',
                      (server.now()-server.CLAIM_TIMEOUT-1,))
        view = self.request('state', cookie=root)[1]
        self.assertEqual(view['own'], [])
        self.assertEqual(view['me']['banned_until'], 0)
        self.assertEqual(view['claim_blocked_until'], 0)
        self.assertEqual(self.request('claim', {'channel_id': 3}, root)[0], 200)

    def test_admin_assignment_is_immediately_visible_to_member(self):
        alice = self.login('alice')
        status, _, root_cookie = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})
        self.assertEqual(status, 200)
        root = root_cookie.split(';')[0]
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
        before = server.REVISION
        self.assertEqual(self.request('admin/assign', {'channel_id': 55, 'user_id': alice_id}, root)[0], 200)
        self.assertGreater(server.REVISION, before)
        state = self.request('state', cookie=alice)[1]
        self.assertEqual([c['id'] for c in state['own']], [55])
        self.assertNotIn(55, state['unknown'])
        self.assertEqual(state['notices'][0]['kind'], 'admin_assign')
        self.assertEqual(state['notices'][0]['channel_id'], 55)

    def test_admin_reassignment_notifies_displaced_member_and_rejects_admin_target(self):
        alice, bob = self.login('alice'), self.login('bob')
        status, _, root_cookie = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})
        self.assertEqual(status, 200)
        root = root_cookie.split(';')[0]
        with server.db() as c:
            bob_id = c.execute("SELECT id FROM users WHERE username='bob'").fetchone()['id']
            root_id = c.execute("SELECT id FROM users WHERE username='root'").fetchone()['id']
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('admin/assign', {'channel_id': 3, 'user_id': root_id}, root)[0], 400)
        self.assertEqual(self.request('admin/assign', {'channel_id': 3, 'user_id': bob_id}, root)[0], 200)
        old = self.request('state', cookie=alice)[1]
        self.assertEqual(old['own'], [])
        self.assertEqual(old['notices'][0]['kind'], 'admin_reassign')
        self.assertEqual(old['notices'][0]['channel_id'], 3)
        self.assertEqual(self.request('state', cookie=bob)[1]['own'][0]['id'], 3)

    def test_sos_request_id_selects_exact_channel_with_matching_timers(self):
        alice, bob = self.login('alice'), self.login('bob')
        status, _, root_cookie = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})
        self.assertEqual(status, 200)
        root = root_cookie.split(';')[0]
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('claim', {'channel_id': 4}, bob)[0], 200)
        self.assertEqual(self.request('sos', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('sos', {'channel_id': 4}, bob)[0], 200)
        items = self.request('state', cookie=root)[1]['sos']
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0]['request_id'], items[1]['request_id'])
        with server.db() as c:
            second = c.execute('SELECT sos_token FROM channels WHERE id=4').fetchone()['sos_token']
        self.assertEqual(self.request('accept-sos', {'request_id': second}, root)[0], 200)
        self.assertEqual(self.request('state', cookie=root)[1]['own'][0]['id'], 4)
        self.assertEqual(self.request('state', cookie=alice)[1]['own'][0]['id'], 3)

    def test_member_can_edit_recorded_channel_from_all_channels_with_stale_write_protection(self):
        alice, bob = self.login('alice'), self.login('bob')
        first, second = server.now()-180, server.now()-120
        with server.db() as c:
            uid = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=3', (first, uid))
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=4', (second, uid))
        self.assertEqual(self.request('edit-kill', {'channel_id': 3, 'killed_at': server.now()-60}, alice)[0], 403)
        changed = server.now()-60
        self.assertEqual(self.request('edit-kill', {'channel_id': 3, 'killed_at': changed,
            'expected_killed_at': first}, bob)[0], 200)
        self.assertEqual(self.request('edit-kill', {'channel_id': 3, 'killed_at': server.now()-30,
            'expected_killed_at': first}, alice)[0], 409)
        with server.db() as c:
            row = c.execute('SELECT killed_at,last_killer_id FROM channels WHERE id=3').fetchone()
            edit = c.execute("SELECT actor_id FROM logs WHERE channel_id=3 AND action='edit_kill' ORDER BY id DESC LIMIT 1").fetchone()
            bob_id = c.execute("SELECT id FROM users WHERE username='bob'").fetchone()['id']
        self.assertEqual((row['killed_at'], row['last_killer_id']), (changed, uid))
        self.assertEqual(edit['actor_id'], bob_id)
        self.assertEqual(self.request('edit-kill', {'channel_id': 4, 'killed_at': server.now()-60,
            'expected_killed_at': second}, alice)[0], 200)
        self.assertEqual(self.request('edit-kill', {'channel_id': 55, 'killed_at': server.now()-60,
            'expected_killed_at': 0}, bob)[0], 409)

    def test_member_records_unknown_channel_time_without_overwriting_another_claim(self):
        alice, bob = self.login('alice'), self.login('bob')
        killed = server.now()-60
        with server.db() as c:
            c.execute('UPDATE channels SET exploded_at=? WHERE id=55', (server.now()-120,))
        self.assertEqual(self.request('edit-kill', {'channel_id': 55, 'killed_at': killed}, alice)[0], 409)
        self.assertEqual(self.request('edit-kill', {'channel_id': 55, 'killed_at': killed,
            'expected_killed_at': None}, alice)[0], 200)
        with server.db() as c:
            row = c.execute('SELECT killed_at,last_killer_id,exploded_at FROM channels WHERE id=55').fetchone()
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            opening = c.execute("SELECT action,detail FROM logs WHERE channel_id=55 ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((row['killed_at'], row['last_killer_id'], row['exploded_at']), (killed, alice_id, None))
        self.assertEqual((opening['action'], opening['detail']), ('kill', '开荒'))
        self.assertEqual(self.request('edit-kill', {'channel_id': 55, 'killed_at': killed,
            'expected_killed_at': None}, bob)[0], 409)
        self.assertEqual(self.request('claim', {'channel_id': 56}, bob)[0], 200)
        self.assertEqual(self.request('edit-kill', {'channel_id': 56, 'killed_at': killed,
            'expected_killed_at': None}, alice)[0], 409)
        self.assertEqual(self.request('edit-kill', {'channel_id': 56, 'killed_at': killed,
            'expected_killed_at': None}, bob)[0], 200)
        self.assertNotIn(56, [channel['id'] for channel in self.request('state', cookie=bob)[1]['own']])

    def test_beijing_day_start_differs_from_utc_midnight(self):
        from datetime import datetime, timezone
        instant = int(datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc).timestamp())
        expected = int(datetime(2026, 9, 13, 16, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(server.beijing_day_start(instant), expected)

    def test_member_keeps_last_kill_card_while_holding_another_channel(self):
        alice = self.login('alice')
        status, _, root_cookie = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})
        self.assertEqual(status, 200)
        root = root_cookie.split(';')[0]
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute('UPDATE channels SET killed_at=NULL WHERE id=1')
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=2', (server.now()-100, alice_id))
        self.assertEqual(self.request('admin/assign', {'channel_id': 55, 'user_id': alice_id}, root)[0], 200)
        view = self.request('state', cookie=alice)[1]
        self.assertEqual(view['own'][0]['id'], 55)
        self.assertIsNone(view['recommendation'])
        self.assertEqual(view['last_kill']['id'], 2)
        self.assertEqual(self.request('claim', {'channel_id': 2}, alice)[0], 200)
        self.assertEqual(self.request('edit-kill', {'channel_id': 55, 'killed_at': server.now()-60}, alice)[0], 403)

    def test_member_keeps_last_kill_edit_access_after_admin_correction(self):
        alice, bob = self.login('alice'), self.login('bob')
        status, _, root_cookie = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})
        self.assertEqual(status, 200)
        root = root_cookie.split(';')[0]
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=3', (server.now()-120, alice_id))
        self.assertEqual(self.request('state', cookie=alice)[1]['last_kill']['id'], 3)
        self.assertEqual(self.request('edit-kill', {'channel_id': 3, 'killed_at': server.now()-90}, root)[0], 200)
        self.assertEqual(self.request('state', cookie=alice)[1]['last_kill']['id'], 3)
        self.assertEqual(self.request('edit-kill', {'channel_id': 3, 'killed_at': server.now()-60}, alice)[0], 200)
        self.assertEqual(self.request('edit-kill', {'channel_id': 3, 'killed_at': server.now()-30}, bob)[0], 403)
        current = self.request('state', cookie=bob)[1]['channels'][2]['killed_at']
        self.assertEqual(self.request('edit-kill', {'channel_id': 3, 'killed_at': server.now()-30,
            'expected_killed_at': current}, bob)[0], 200)

    def test_edited_kill_time_sets_wait_and_guarantee_windows(self):
        alice = self.login('alice')
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=3', (server.now()-120, alice_id))
        killed = (server.now()-180)//60*60
        self.assertEqual(self.request('edit-kill', {'channel_id': 3, 'killed_at': killed}, alice)[0], 200)
        last = self.request('state', cookie=alice)[1]['last_kill']
        self.assertEqual(last['killed_at'], killed)
        self.assertEqual(last['earliest_at'], killed + 38*60)
        self.assertEqual(last['latest_at'], killed + 60*60)

    def test_recommendation_prioritizes_current_window_then_resets_expired_kills(self):
        alice = self.login('alice')
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            bob_id = c.execute("SELECT id FROM users WHERE username='bob'").fetchone()['id']
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=1', (server.now()-300, alice_id))
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=2', (server.now()-100, alice_id))
        status, view, _ = self.request('state', cookie=alice)
        self.assertEqual(status, 200)
        self.assertEqual(view['recommendation']['id'], 1)
        self.assertEqual(view['last_kill']['id'], 2)
        with server.db() as c:
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=3', (server.now()-500, bob_id))
        view = self.request('state', cookie=alice)[1]
        self.assertEqual(view['recommendation']['id'], 3)
        self.assertFalse(view['recommendation_is_own'])
        self.assertEqual(view['extra_recommendation']['id'], 1)
        self.assertTrue(view['extra_recommendation_is_own'])
        self.assertEqual(view['spawn_pool'], [])
        with server.db() as c:
            c.execute('UPDATE channels SET claimant_id=?,claimed_at=? WHERE id=3', (bob_id, server.now()))
        status, view, _ = self.request('state', cookie=alice)
        self.assertEqual(status, 200)
        self.assertEqual(view['recommendation']['id'], 1)
        self.assertEqual(view['unknown'], list(range(4, 61)))
        with server.db() as c:
            c.execute('UPDATE channels SET killed_at=? WHERE id=1', (server.now()-7200,))
            c.execute('UPDATE channels SET killed_at=? WHERE id=2', (server.now()-7000,))
        expired_view = self.request('state', cookie=alice)[1]
        self.assertIsNone(expired_view['recommendation'])
        self.assertIn(1, expired_view['unknown'])
        self.assertIn(2, expired_view['unknown'])
        self.assertEqual(self.request('claim', {'channel_id': 2}, alice)[0], 200)

    def test_recommendation_slots_prefer_ready_then_upcoming_and_keep_last_kill_separate(self):
        alice = self.login('alice')
        t = server.now()
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=3', (t-3000, alice_id))
            c.execute('UPDATE channels SET killed_at=? WHERE id=4', (t-500,))
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=5', (t-50, alice_id))
        view = self.request('state', cookie=alice)[1]
        self.assertEqual(view['recommendation']['id'], 3)
        self.assertLessEqual(view['recommendation']['earliest_at'], t)
        self.assertLess(t, view['recommendation']['latest_at'])
        self.assertEqual(view['extra_recommendation']['id'], 4)
        self.assertGreater(view['extra_recommendation']['earliest_at'], t)
        self.assertEqual(view['last_kill']['id'], 5)
        self.assertNotIn(5, (view['recommendation']['id'], view['extra_recommendation']['id']))
        self.assertEqual(self.request('claim', {'channel_id': 4}, alice)[0], 200)

    def test_recommendation_uses_two_upcoming_channels_when_none_is_ready(self):
        alice = self.login('alice')
        view = self.request('state', cookie=alice)[1]
        self.assertEqual([view['recommendation']['id'], view['extra_recommendation']['id']], [1, 2])

    def test_claimed_member_gets_upcoming_channels_before_ready_ones(self):
        alice = self.login('alice')
        t = server.now()
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute('UPDATE channels SET claimant_id=?,claimed_at=? WHERE id=6', (alice_id, t))
            c.execute('UPDATE channels SET killed_at=? WHERE id IN (1,2)', (t-3000,))
            c.execute('UPDATE channels SET killed_at=? WHERE id=3', (t-2000,))
            c.execute('UPDATE channels SET killed_at=? WHERE id=4', (t-1000,))
        view = self.request('state', cookie=alice)[1]
        self.assertEqual([view['recommendation']['id'], view['extra_recommendation']['id']], [3, 4])
        self.assertTrue(all(card['earliest_at'] > t for card in
                            (view['recommendation'], view['extra_recommendation'])))
        with server.db() as c:
            c.execute('UPDATE channels SET killed_at=NULL WHERE id=4')
        view = self.request('state', cookie=alice)[1]
        self.assertEqual([view['recommendation']['id'], view['extra_recommendation']['id']], [3, 1])

    def test_recommendation_uses_two_ready_channels_and_excludes_guarantee_boundary(self):
        alice = self.login('alice')
        t = server.now()
        with server.db() as c:
            c.execute('UPDATE channels SET killed_at=? WHERE id=1', (t-3000,))
            c.execute('UPDATE channels SET killed_at=? WHERE id=2', (t-2900,))
            c.execute('UPDATE channels SET killed_at=? WHERE id=3', (t-3600,))
        with patch.object(server, 'now', return_value=t):
            view = self.request('state', cookie=alice)[1]
        self.assertEqual([view['recommendation']['id'], view['extra_recommendation']['id']], [1, 2])
        self.assertLess(view['recommendation']['earliest_at'], t)
        self.assertLess(view['extra_recommendation']['earliest_at'], t)

    def test_imminent_own_channel_beats_fresh_and_stale_channels(self):
        alice = self.login('alice')
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=1', (server.now()-10000, alice_id))
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=2', (server.now()-2000, alice_id))
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=55', (server.now()-300, alice_id))
        view = self.request('state', cookie=alice)[1]
        self.assertEqual(view['recommendation']['id'], 2)
        self.assertLess(view['recommendation']['earliest_at'], server.now()+5*60)

    def test_member_sees_only_recommendations_and_can_claim_selected_other_kill(self):
        alice = self.login('alice')
        with server.db() as c:
            alice_id = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            bob_id = c.execute("SELECT id FROM users WHERE username='bob'").fetchone()['id']
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=? WHERE id=55', (server.now()-300, bob_id))
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=?,claimant_id=?,claimed_at=? WHERE id=56',
                      (server.now()-100, alice_id, alice_id, server.now()))
        view = self.request('state', cookie=alice)[1]
        self.assertEqual(view['own'][0]['id'], 56)
        self.assertEqual(view['last_kill']['id'], 56)
        self.assertEqual(view['recommendation']['id'], 55)
        self.assertFalse(view['recommendation_is_own'])
        self.assertEqual(view['extra_recommendation']['id'], 1)
        self.assertEqual(view['spawn_pool'], [])
        self.assertNotIn(55, view['unknown'])
        with server.db() as c:
            c.execute('UPDATE channels SET claimant_id=NULL,claimed_at=NULL WHERE id=56')
            c.execute('UPDATE channels SET killed_at=? WHERE id=56', (server.now()-7200,))
        self.assertEqual(self.request('state', cookie=alice)[1]['recommendation']['id'], 55)
        self.assertEqual(self.request('claim', {'channel_id': 2}, alice)[0], 200)

    def test_plus_access_controls_pool_and_recommendation_count(self):
        alice = self.login('alice')
        root = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})[2].split(';')[0]
        with server.db() as c:
            uid = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            c.execute('UPDATE channels SET killed_at=?,last_killer_id=NULL WHERE id IN (1,2,3,4,5)', (server.now()-60,))
        view = self.request('state', cookie=alice)[1]
        self.assertFalse(view['me']['is_plus'])
        self.assertEqual(len(view['recommendations']), 2)
        self.assertEqual(view['spawn_pool'], [])
        self.assertEqual(self.request('admin/user', {'user_id': uid, 'action': 'plus', 'days': -1}, alice)[0], 403)
        self.assertEqual(self.request('admin/user', {'user_id': uid, 'action': 'plus', 'days': -1}, root)[0], 200)
        view = self.request('state', cookie=alice)[1]
        self.assertTrue(view['me']['is_plus'])
        self.assertEqual(len(view['recommendations']), 4)
        self.assertEqual(len(view['spawn_pool']), 5)
        self.assertEqual(set(view['spawn_pool'][0]), {'id', 'earliest_at', 'latest_at'})
        with server.db() as c:
            c.execute('UPDATE users SET plus_until=? WHERE id=?', (server.now()-1, uid))
        view = self.request('state', cookie=alice)[1]
        self.assertFalse(view['me']['is_plus'])
        self.assertEqual(len(view['recommendations']), 2)
        self.assertEqual(view['spawn_pool'], [])

    def test_members_see_all_channels_without_pool(self):
        root = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})[2].split(';')[0]
        bob = self.login('bob')
        self.assertIn(1, [row['id'] for row in self.request('state', cookie=root)[1]['spawn_pool']])
        view = self.request('state', cookie=bob)[1]
        self.assertEqual(len(view['channels']), 60)
        self.assertEqual(view['spawn_pool'], [])
        self.assertNotIn('last_killer_id', view['channels'][0])
        self.assertIsNone(view['channels'][0]['owner_nickname'])
        self.assertEqual(self.request('claim', {'channel_id': 3}, root)[0], 200)
        updated = self.request('state', cookie=bob)[1]['channels'][2]
        self.assertEqual(updated['claimant_id'], -1)
        self.assertIsNone(updated['claimed_at'])
        self.assertEqual(updated['owner_nickname'], '超级管理员')
        self.assertEqual(self.request('state', cookie=root)[1]['channels'][2]['owner_nickname'], '超级管理员')

    def test_discard_requires_own_claim_and_resets_to_exploration(self):
        alice, bob = self.login('alice'), self.login('bob')
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('discard', {'channel_id': 3}, bob)[0], 403)
        self.assertEqual(self.request('kill', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('discard', {'channel_id': 3}, alice)[0], 403)
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('discard', {'channel_id': 3}, alice)[0], 200)
        with server.db() as c:
            row = c.execute('SELECT killed_at,last_killer_id,claimant_id FROM channels WHERE id=3').fetchone()
            self.assertIsNone(row['killed_at'])
            self.assertIsNone(row['last_killer_id'])
            self.assertIsNone(row['claimant_id'])
        self.assertIn(3, self.request('state', cookie=bob)[1]['unknown'])

    def test_claimed_personal_last_kill_is_not_available_but_remains_editable(self):
        alice, bob = self.login('alice'), self.login('bob')
        self.assertEqual(self.request('claim', {'channel_id': 44}, alice)[0], 200)
        self.assertEqual(self.request('kill', {'channel_id': 44}, alice)[0], 200)
        self.assertEqual(self.request('claim', {'channel_id': 44}, bob)[0], 200)
        view = self.request('state', cookie=alice)[1]
        self.assertEqual(view['last_kill']['id'], 44)
        self.assertFalse(view['last_kill_claimable'])
        self.assertEqual(view['channels'][43]['owner_nickname'], 'bob')
        self.assertNotIn(44, [c['id'] for c in view['spawn_pool']])

    def test_login_risks_and_today_ranking_are_superadmin_only(self):
        alice = self.login('alice')
        self.login('alice')
        alice = self.login('alice')
        view = self.request('state', cookie=alice)[1]
        self.assertNotIn('login_records', view)
        self.assertNotIn('today_kill_ranking', view)
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('kill', {'channel_id': 3}, alice)[0], 200)
        root = self.request('login', {'username': 'root', 'password': os.environ['ADMIN_PASSWORD']})[2].split(';')[0]
        admin = self.request('state', cookie=root)[1]
        self.assertEqual(admin['today_kill_ranking'][0]['nickname'], 'alice')
        events = [r for r in admin['login_records'] if r['nickname']=='alice']
        self.assertEqual(len(events), 3)
        # Two takeovers so far; the fourth login is the third takeover.
        alice = self.login('alice')
        events = self.request('state', cookie=root)[1]['login_records']
        self.assertIn('1小时内至少3次顶号', next(r for r in events if r['nickname']=='alice')['risk'])
        self.assertEqual(self.request('state', cookie=alice)[0], 200)

    def test_cancel_sos_preserves_claim_and_invalidates_old_request(self):
        alice, bob = self.login('alice'), self.login('bob')
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('sos', {'channel_id': 3}, alice)[0], 200)
        request_id = self.request('state', cookie=bob)[1]['sos'][0]['request_id']
        self.assertEqual(self.request('cancel-sos', {'channel_id': 3}, bob)[0], 409)
        self.assertEqual(self.request('cancel-sos', {'channel_id': 3}, alice)[0], 200)
        own = self.request('state', cookie=alice)[1]['own'][0]
        self.assertEqual(own['id'], 3)
        self.assertIsNone(own['sos_at'])
        self.assertEqual(self.request('state', cookie=bob)[1]['sos'], [])
        self.assertEqual(self.request('accept-sos', {'request_id': request_id}, bob)[0], 409)
        self.assertEqual(self.request('sos', {'channel_id': 3}, alice)[0], 200)

    def test_channel_notes_are_shared_persistent_and_reject_stale_edits(self):
        alice, bob = self.login('alice'), self.login('bob')
        self.assertEqual(self.request('channel-note', {'channel_id': 3, 'note': '常来的对手',
            'expected_note': ''}, alice)[0], 200)
        self.assertEqual(self.request('state', cookie=bob)[1]['channels'][2]['note'], '常来的对手')
        self.assertEqual(self.request('channel-note', {'channel_id': 3, 'note': '另一对手',
            'expected_note': ''}, bob)[0], 409)
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('discard', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('state', cookie=bob)[1]['channels'][2]['note'], '常来的对手')

    def test_unmet_rescue_advice_does_not_restrict_login_or_claim(self):
        with server.db() as c:
            c.execute("UPDATE users SET rescue_target=100 WHERE username='bob'")
        try:
            bob = self.login('bob')
            view = self.request('state', cookie=bob)[1]
            self.assertEqual(view['rescues_today'], 0)
            self.assertEqual(view['me']['rescue_target'], 100)
            self.assertEqual(self.request('claim', {'channel_id': 1}, bob)[0], 200)
        finally:
            with server.db() as c:
                c.execute("UPDATE users SET rescue_target=3 WHERE username='bob'")

    def test_opening_first_can_then_claim_recommended_channel(self):
        alice = self.login('alice')
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        self.assertEqual(self.request('claim', {'channel_id': 1}, alice)[0], 200)
        view = self.request('state', cookie=alice)[1]
        self.assertEqual([r['id'] for r in view['own']], [1])
        self.assertEqual([r['id'] for r in view['explorations']], [3])
        for channel_id in (4, 5):
            self.assertEqual(self.request('claim-exploration', {'channel_id': channel_id}, alice)[0], 200)
        self.assertEqual(self.request('claim-exploration', {'channel_id': 6}, alice)[0], 403)
        self.assertEqual(self.request('claim', {'channel_id': 2}, alice)[0], 403)

    def test_three_concurrent_explorations_are_independent(self):
        alice = self.login('alice')
        bob = self.login('bob')
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        for channel_id in (4, 5, 6):
            self.assertEqual(self.request('claim-exploration', {'channel_id': channel_id}, alice)[0], 200)
        view = self.request('state', cookie=alice)[1]
        self.assertEqual([row['id'] for row in view['explorations']], [4, 5, 6])
        self.assertEqual([row['id'] for row in view['own']], [3])
        other_view = self.request('state', cookie=bob)[1]['channels'][4]
        self.assertEqual(other_view['explorer_id'], -1)
        self.assertEqual(other_view['owner_nickname'], 'alice')
        self.assertEqual(self.request('claim-exploration', {'channel_id': 7}, alice)[0], 403)
        self.assertEqual(self.request('claim', {'channel_id': 5}, bob)[0], 409)
        self.assertEqual(self.request('release', {'channel_id': 5}, bob)[0], 403)
        killed = server.now() - 60
        self.assertEqual(self.request('kill-exploration', {'channel_id': 6, 'killed_at': killed}, alice)[0], 200)
        self.assertEqual([row['id'] for row in self.request('state', cookie=alice)[1]['explorations']], [4, 5])
        self.assertEqual(self.request('claim-exploration', {'channel_id': 7}, alice)[0], 200)
        self.assertEqual(self.request('release', {'channel_id': 5}, alice)[0], 200)
        self.assertEqual(self.request('claim-exploration', {'channel_id': 8}, alice)[0], 403)
        with server.db() as c:
            c.execute('UPDATE release_cooldowns SET ends_at=?', (server.now()-1,))
        self.assertEqual(self.request('claim-exploration', {'channel_id': 8}, alice)[0], 200)
        view = self.request('state', cookie=alice)[1]
        self.assertEqual([row['id'] for row in view['explorations']], [4, 7, 8])
        self.assertEqual([row['id'] for row in view['own']], [3])
        self.assertEqual(self.request('discard', {'channel_id': 8}, alice)[0], 200)
        self.assertEqual([row['id'] for row in self.request('state', cookie=alice)[1]['explorations']], [4, 7])
        self.assertIn(8, self.request('state', cookie=bob)[1]['unknown'])

    def test_third_exploration_timeout_releases_only_that_channel(self):
        alice = self.login('alice')
        self.assertEqual(self.request('claim', {'channel_id': 3}, alice)[0], 200)
        for channel_id in (4, 5, 6):
            self.assertEqual(self.request('claim-exploration', {'channel_id': channel_id}, alice)[0], 200)
        with server.db() as c:
            c.execute('UPDATE channels SET exploration3_claimed_at=? WHERE id=6',
                      (server.now()-server.CLAIM_TIMEOUT-1,))
            c.execute('BEGIN IMMEDIATE')
            self.assertTrue(server.sweep(c))
            uid = c.execute("SELECT id FROM users WHERE username='alice'").fetchone()['id']
            self.assertIsNone(c.execute('SELECT explorer3_id FROM channels WHERE id=6').fetchone()['explorer3_id'])
            self.assertEqual(c.execute('SELECT explorer_id FROM channels WHERE id=4').fetchone()['explorer_id'], uid)
            self.assertEqual(c.execute('SELECT explorer2_id FROM channels WHERE id=5').fetchone()['explorer2_id'], uid)
            self.assertEqual(c.execute('SELECT claimant_id FROM channels WHERE id=3').fetchone()['claimant_id'], uid)


if __name__ == '__main__': unittest.main()
