import json
import unittest
from unittest.mock import patch
import test_api
import server
import boss_scope
import realm_scope


class GlobalMushroomTests(unittest.TestCase):
    setUpClass = classmethod(test_api.ApiTests.setUpClass.__func__)
    tearDownClass = classmethod(test_api.ApiTests.tearDownClass.__func__)
    login = test_api.ApiTests.login

    def request(self,path,data=None,cookie=None,boss='global_mushroom',realm='white_rabbit'):
        return test_api.ApiTests.request(self,path+('&' if '?' in path else '?')+'boss='+boss+'&realm='+realm,data,cookie)

    def setUp(self):
        server.RATE_BUCKETS.clear()
        with server.db() as c:
            c.execute('UPDATE users SET boss_access=?,plus_until=NULL,frozen=0,banned_until=0,access_until=?,session_version=0', (sum(boss_scope.FLAGS.values()),server.now()+86400))
            c.execute('DELETE FROM sessions')
            self.ids={r['username']:r['id'] for r in c.execute('SELECT id,username FROM users')}
        for realm in realm_scope.REALMS:
            for boss in boss_scope.WINDOW_BOSSES:
                with server.db(boss,realm=realm) as c:
                    for table in ('exploration_relays','exploration_credits','notifications','logs','robbery_reports','release_cooldowns'):
                        c.execute('DELETE FROM '+table)
                    c.execute("""UPDATE channels SET killed_at=NULL,last_killer_id=NULL,claimant_id=NULL,claimed_at=NULL,
                        parallel_id=NULL,parallel_claimed_at=NULL,explorer_id=NULL,exploration_claimed_at=NULL,
                        explorer2_id=NULL,exploration2_claimed_at=NULL,explorer3_id=NULL,exploration3_claimed_at=NULL,
                        sos_at=NULL,sos_token=NULL,blocked_until=0,credit_blocked_until=0,note='' """)
        self.a,self.b=self.login('alice'),self.login('bob')

    def state(self,boss='global_mushroom',cookie=None):
        code,data,_=self.request('state',cookie=cookie or self.a,boss=boss)
        self.assertEqual(code,200,data)
        return data

    def op(self,path,status=200,cookie=None,**data):
        code,result,_=self.request(path,data,cookie or self.a)
        self.assertEqual(code,status,result)
        return result

    def seed(self,cid,at,boss='global_mushroom',realm='white_rabbit'):
        with server.db(boss,realm=realm) as c:
            c.execute('UPDATE channels SET killed_at=? WHERE id=?',(at,cid))

    def test_window_recommendations_and_pool_stop_at_90_minutes(self):
        t=server.now()
        for cid,age in ((1,100),(2,3500),(3,4500),(4,5399),(5,5400)):
            self.seed(cid,t-age)
        with patch.object(server,'now',return_value=t):
            v=self.state()
            self.assertEqual(v['boss_name'],'国际服蘑菇王')
            self.assertEqual([r['channel']['id'] for r in v['recommendations']],[4,3,2,1])
            self.assertEqual([r['id'] for r in v['spawn_pool']],[4,3,2,1])
            for ch in v['channels'][:5]:
                self.assertEqual(ch['earliest_at'],ch['killed_at']+3480)
                self.assertEqual(ch['latest_at'],ch['killed_at']+5400)
            self.op('claim',channel_id=3)
            self.assertEqual(self.state()['own'][0]['latest_at'],t-4500+5400)

    def test_parallel_and_discard_use_58_minute_boundary(self):
        t=server.now()
        self.seed(1,t-100);self.seed(2,t-3479)
        self.op('claim',channel_id=1)
        with patch.object(server,'now',return_value=t):
            self.op('claim-parallel',status=409,channel_id=2,expected_killed_at=t-3479)
            self.op('discard-ready',status=409,channel_id=2,expected_killed_at=t-3479)
        self.seed(2,t-3480)
        with patch.object(server,'now',return_value=t):
            self.op('claim-parallel',channel_id=2,expected_killed_at=t-3480)
            self.assertEqual(self.state()['concurrent'][0]['id'],2)

    def test_window_is_kept_until_90_minutes_plus_grace(self):
        t=server.now();self.seed(1,t-4500)
        with server.db('global_mushroom') as c:
            c.execute('UPDATE channels SET claimant_id=?,claimed_at=? WHERE id=1',(self.ids['alice'],t-4500))
            with patch.object(server,'now',return_value=t):server.sweep(c)
            self.assertEqual(c.execute('SELECT claimant_id FROM channels WHERE id=1').fetchone()['claimant_id'],self.ids['alice'])
            with patch.object(server,'now',return_value=t+900+server.UNKNOWN_AFTER_GUARANTEE):server.sweep(c)
            ch=c.execute('SELECT * FROM channels WHERE id=1').fetchone()
            self.assertIsNone(ch['killed_at']);self.assertIsNone(ch['claimant_id'])
            self.assertEqual(c.execute("SELECT count(*) AS n FROM logs WHERE action='reset_unknown'").fetchone()['n'],1)

    def test_kill_and_edit_keep_other_boss_and_credit_independent(self):
        self.op('claim',channel_id=8)
        self.op('kill',channel_id=8)
        v=self.state();ch=v['channels'][7];at=ch['killed_at']
        self.assertEqual(ch['latest_at'],at+5400)
        self.op('edit-kill',channel_id=8,killed_at=at-60)
        with server.db('global_mushroom') as c:
            self.assertEqual(c.execute("SELECT count(*) AS n FROM logs WHERE action='kill'").fetchone()['n'],1)
            self.assertEqual(c.execute('SELECT credit_blocked_until FROM channels WHERE id=8').fetchone()['credit_blocked_until'],at+5400)
        self.assertIsNone(self.state('mushroom')['channels'][7]['killed_at'])
        self.assertIsNone(self.state('zombie')['channels'][7]['killed_at'])

    def test_sos_cross_page_times_and_transfer(self):
        t=server.now();self.seed(7,t-4000)
        self.op('claim',channel_id=7);self.op('sos',channel_id=7)
        support=self.state('mushroom',self.b)['cross_support']['sos']
        row=next(r for r in support if r['boss']=='global_mushroom')
        self.assertEqual(row['earliest_at'],t-4000+3480)
        self.assertEqual(row['latest_at'],t-4000+5400)
        self.op('accept-sos',cookie=self.b,request_id=row['request_id'])
        foreign=next(c for c in self.state('mushroom',self.b)['other_boss_claims'] if c['boss']=='global_mushroom')
        self.assertEqual(foreign['latest_at'],t-4000+5400)
        self.assertEqual(foreign['sos_from_name'],'alice')

    def test_parallel_relay_uses_long_guarantee(self):
        t=server.now();self.seed(1,t-100);self.seed(2,t-4000)
        self.op('claim',channel_id=1)
        self.op('claim-parallel',channel_id=2,expected_killed_at=t-4000)
        self.op('relay/invite',channel_id=2)
        row=next(r for r in self.state(cookie=self.b)['relays'] if r['channel_id']==2)
        self.assertEqual(row['earliest_at'],t-4000+3480)
        self.assertEqual(row['latest_at'],t-4000+5400)

    def test_permissions_realms_and_statistics(self):
        self.op('claim',channel_id=9);self.op('kill',channel_id=9)
        with server.db('global_mushroom',realm='red_crab') as c:
            self.assertEqual(c.execute('SELECT count(*) AS n FROM channels').fetchone()['n'],60)
            self.assertIsNone(c.execute('SELECT killed_at FROM channels WHERE id=9').fetchone()['killed_at'])
        self.assertEqual(self.request('state',cookie=self.a,realm='red_crab')[0],403)
        with server.db(realm='white_rabbit') as c:
            stats=server.daily_kill_details(c,server.beijing_day_start(),'test')
            row=next(r for r in stats['rows'] if r['user_id']==self.ids['alice'])
            self.assertEqual(row['counts']['global_mushroom'],1);self.assertEqual(row['counts']['mushroom'],0)
            c.execute('UPDATE users SET boss_access=1 WHERE id=?',(self.ids['alice'],))
        self.assertEqual(self.request('state',cookie=self.a)[0],403)

    def root(self):
        import os
        return self.request('login',{'username':'root','password':os.environ['ADMIN_PASSWORD']})[2].split(';')[0]

    def test_manual_access_only_and_no_leak_before_grant(self):
        with server.db() as c:
            c.execute('UPDATE users SET boss_access=63')
            before={r['id']:r['boss_access'] for r in c.execute('SELECT id,boss_access FROM users')}
        server.init_db()
        with server.db() as c:
            self.assertEqual({r['id']:r['boss_access'] for r in c.execute('SELECT id,boss_access FROM users')},before)
        self.seed(12,server.now()-4000)
        self.assertEqual(self.request('state',cookie=self.a)[0],403)
        self.assertEqual(self.request('claim',{'channel_id':12},self.a)[0],403)
        self.assertFalse(any(c['boss']=='global_mushroom' for c in self.state('mushroom')['other_boss_claims']))
        self.assertFalse(any(c['boss']=='global_mushroom' for c in self.state('mushroom')['cross_support']['sos']))
        uid=self.ids['alice'];root=self.root()
        grant={'user_id':uid,'action':'boss_access','boss_access':127}
        self.assertEqual(self.request('admin/user',grant,self.b,boss='mushroom')[0],403)
        self.assertEqual(self.request('admin/user',grant,root,boss='mushroom')[0],200)
        self.assertEqual(self.state()['boss'],'global_mushroom')
        self.op('claim',channel_id=12);self.op('sos',channel_id=12)
        self.assertFalse(any(c['boss']=='global_mushroom' for c in self.state('mushroom',self.b)['cross_support']['sos']))
        grant['boss_access']=63
        self.assertEqual(self.request('admin/user',grant,root,boss='mushroom')[0],409)
        self.op('release',channel_id=12)
        self.assertEqual(self.request('admin/user',grant,root,boss='mushroom')[0],200)
        self.assertEqual(self.request('state',cookie=self.a)[0],403)

    def test_registration_and_creation_do_not_auto_grant(self):
        payload={'username':'newglobal','nickname':'新成员','password':'TestOnlyPassword123!','realm_id':'white_rabbit','boss_access':127}
        self.assertEqual(self.request('register',payload,boss='mushroom')[0],200)
        root=self.root()
        payload.update(username='createdglobal',boss_access=3)
        self.assertEqual(self.request('admin/create-user',payload,root,boss='mushroom')[0],200)
        with server.db() as c:
            for r in c.execute("SELECT boss_access FROM users WHERE username IN ('newglobal','createdglobal')"):
                self.assertFalse(r['boss_access'] & 64)

    def test_existing_realm_upgrade_creates_only_missing_boss_tables(self):
        t=server.now()
        self.seed(20,t,'mushroom',realm='red_crab')
        with server.db() as c:
            c.execute("DELETE FROM app_migrations WHERE name='realms_global_mushroom_v1'")
            # This database is created by setUpClass solely for tests.
            for table in reversed(boss_scope.TABLES):
                c.execute('DROP TABLE red_crab_global_mushroom_'+table)
            realm_scope.initialize(c.base,server.MYSQL,refresh_user_views=False)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM red_crab_global_mushroom_channels').fetchone()['n'],60)
            self.assertEqual(c.execute('SELECT killed_at FROM red_crab_channels WHERE id=20').fetchone()['killed_at'],t)
            self.assertEqual(c.execute('SELECT count(*) AS n FROM red_crab_global_mushroom_channels WHERE killed_at IS NOT NULL').fetchone()['n'],0)

    def test_30_day_blacklist_is_scoped_and_expires(self):
        root=self.root(); t=server.now()
        payload={'channel_ids':list(range(21,61)),'action':'block','minutes':43200}
        self.assertEqual(self.request('admin/channels/blacklist',payload,self.a)[0],403)
        with patch.object(server,'now',return_value=t):
            self.assertEqual(self.request('admin/channels/blacklist',payload,root)[0],200)
            self.op('claim',status=409,channel_id=21)
            self.op('claim',channel_id=20)
        with server.db('global_mushroom') as c:
            rows=list(c.execute('SELECT id,blocked_until FROM channels ORDER BY id'))
            self.assertTrue(all(r['blocked_until']==(t+30*86400 if r['id']>=21 else 0) for r in rows))
            self.assertEqual(c.execute("SELECT count(*) AS n FROM logs WHERE action='channel_blacklist'").fetchone()['n'],40)
            c.execute('UPDATE channels SET blocked_until=? WHERE id=21',(server.now()-1,))
        self.op('claim',cookie=self.b,channel_id=21)
        for realm,boss in (('red_crab','global_mushroom'),('white_rabbit','mushroom')):
            with server.db(boss,realm=realm) as c:
                self.assertEqual(c.execute('SELECT count(*) AS n FROM channels WHERE blocked_until>0').fetchone()['n'],0)
        payload['minutes']=43201
        self.assertEqual(self.request('admin/channels/blacklist',payload,root)[0],400)

if __name__=='__main__':unittest.main()
