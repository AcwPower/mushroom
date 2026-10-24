"""Fixed, server-owned table namespaces. Shared accounts; isolated boss state."""
import re
import realm_scope

BOSSES = {'mushroom': '蘑菇王', 'zombie': '僵尸蘑菇王', 'dorr': '多尔', 'faust': '浮士德', 'eliza': '艾利杰', 'snowman': '驮狼雪人', 'global_mushroom': '国际服蘑菇王'}
FLAGS = {'mushroom': 1, 'zombie': 2, 'dorr': 4, 'faust': 8, 'eliza': 16, 'snowman': 32, 'global_mushroom': 64}
TIMING = {'mushroom': (2280, 3600), 'zombie': (2280, 3600), 'dorr': (2700, 2700), 'faust': (2700, 2700), 'eliza': (2700, 2700), 'snowman': (3480, 6000), 'global_mushroom': (3480, 5400)}
WINDOW_BOSSES = ('mushroom', 'zombie', 'snowman', 'global_mushroom')
FIXED_CLOCK_BOSSES = ('dorr', 'faust', 'eliza')
# Logical names shared by fixed-clock bosses; existing Dorr table names stay intact.
CLOCK_TABLES = ('clock_teams', 'clock_team_members', 'clock_invites', 'clock_notice_text')
TABLES = ('channels', 'release_cooldowns', 'logs', 'notifications',
          'robbery_reports', 'exploration_credits', 'exploration_relays')
PATTERN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\b(" + '|'.join(TABLES + CLOCK_TABLES) + r')\b')


class Connection:
    def __init__(self, connection, boss='mushroom', totals=False, realm=None):
        if boss not in BOSSES:
            raise ValueError('Unknown boss')
        self.base = connection.base if isinstance(connection, Connection) else connection
        self.realm = realm if realm is not None else getattr(connection, 'realm', None)
        if self.realm is not None and self.realm not in realm_scope.REALMS:
            raise ValueError('Unknown realm')
        self.boss = boss
        self.totals = totals

    def sql(self, sql):
        def replace(match):
            name = match.group(1)
            if not name:
                return match.group(0)
            if name in CLOCK_TABLES:
                if self.boss not in FIXED_CLOCK_BOSSES:
                    raise ValueError('Fixed-clock table used outside a fixed-clock Boss')
                return self.boss + name.removeprefix('clock')
            if self.totals and name in ('logs', 'exploration_credits', 'robbery_reports'):
                return 'combined_' + name
            return self.boss + '_' + name if self.boss != 'mushroom' else name
        return realm_scope.rewrite(PATTERN.sub(replace, sql), self.realm)

    def execute(self, sql, params=()):
        return self.base.execute(self.sql(sql), params)

    def executemany(self, sql, params):
        return self.base.executemany(self.sql(sql), params)

    def __getattr__(self, name):
        return getattr(self.base, name)

    def __enter__(self):
        self.base.__enter__()
        return self

    def __exit__(self, *args):
        return self.base.__exit__(*args)


def totals(c):
    return Connection(c, totals=True)


def initialize(c, mysql):
    # Existing mushroom tables are authoritative and are never copied or reset.
    for boss in WINDOW_BOSSES:
        if boss == 'mushroom':
            continue
        for table in TABLES:
            target = boss + '_' + table
            if mysql:
                c.execute(f'CREATE TABLE IF NOT EXISTS {target} LIKE {table}')
            elif not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (target,)).fetchone():
                source = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()['sql']
                c.execute(Connection(c, boss).sql(source))
                for index in c.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)).fetchall():
                    sql = Connection(c, boss).sql(index['sql'])
                    sql = re.sub(r'\b' + re.escape(index['name']) + r'\b', boss + '_' + index['name'], sql)
                    c.execute(sql)
        c.executemany(('INSERT IGNORE' if mysql else 'INSERT OR IGNORE') +
                      f' INTO {boss}_channels(id) VALUES(?)', [(i,) for i in range(1, 61)])
    # Fixed-clock channels are initialized by dorr.init.
    for boss in FIXED_CLOCK_BOSSES:
        for table in TABLES:
            if table == 'channels':
                continue
            target = boss + '_' + table
            if mysql:
                c.execute(f'CREATE TABLE IF NOT EXISTS {target} LIKE {table}')
            elif not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (target,)).fetchone():
                source = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()['sql']
                c.execute(Connection(c, boss).sql(source))
                for index in c.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)).fetchall():
                    sql = Connection(c, boss).sql(index['sql'])
                    sql = re.sub(r'\b' + re.escape(index['name']) + r'\b', boss + '_' + index['name'], sql)
                    c.execute(sql)
    for table in ('logs', 'exploration_credits', 'robbery_reports'):
        if not mysql:
            c.execute(f'DROP VIEW IF EXISTS combined_{table}')
        prefix = 'CREATE OR REPLACE VIEW' if mysql else 'CREATE VIEW'
        sources = [table if boss == 'mushroom' else boss + '_' + table for boss in BOSSES]
        c.execute(f'{prefix} combined_{table} AS ' + ' UNION ALL '.join('SELECT * FROM ' + source for source in sources))


def grant_snowman_access(c):
    if not c.execute("SELECT 1 FROM app_migrations WHERE name='snowman_access_v1'").fetchone():
        c.execute('UPDATE users SET boss_access=boss_access | ? WHERE deleted_at IS NULL AND (boss_access & ?)<>0',
                  (FLAGS['snowman'], FLAGS['mushroom']))
        c.execute('INSERT INTO app_migrations(name,applied_at) VALUES(?,?)', ('snowman_access_v1', int(__import__('time').time())))
