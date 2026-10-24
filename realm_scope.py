"""Server-owned realm namespaces. Accounts authenticate globally; gameplay is isolated."""
import re

REALMS = {'white_rabbit': '小白兔', 'red_crab': '红螃蟹'}
DEFAULT = 'white_rabbit'
BOSSES = ('mushroom', 'zombie', 'dorr', 'faust', 'eliza', 'snowman', 'global_mushroom')
BOSS_TABLES = ('channels', 'release_cooldowns', 'logs', 'notifications',
               'robbery_reports', 'exploration_credits', 'exploration_relays')
SHARED_TABLES = ('relay_guard', 'payment_marks', 'page_presence', 'login_records')
TABLES = tuple(table if boss == 'mushroom' else boss + '_' + table
               for boss in BOSSES for table in BOSS_TABLES) + tuple(
    boss + '_' + table for boss in ('dorr', 'faust', 'eliza')
    for table in ('teams', 'team_members', 'invites', 'notice_text')) + SHARED_TABLES
COMBINED = ('logs', 'exploration_credits', 'robbery_reports')
VIEWS = tuple('combined_' + table for table in COMBINED)
PATTERN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\b(" +
                     '|'.join(TABLES + VIEWS + ('users',)) + r')\b')


def rewrite(sql, realm):
    if realm is None:
        return sql  # Only bootstrap, authentication and explicit maintenance tools.
    if realm not in REALMS:
        raise ValueError('Unknown realm')
    reading = sql.lstrip().upper().startswith('SELECT')
    def replace(match):
        table = match.group(1)
        if not table:
            return match.group(0)
        if table == 'users':
            # Filtering joins and target lookups also blocks cross-realm invites,
            # assignments, payment marks and member management by guessed user ID.
            # Account writes use IDs already authorized by these scoped reads.
            return 'realm_' + realm + '_users' if reading else table
        return ('red_crab_' if realm == 'red_crab' else '') + table
    return PATTERN.sub(replace, sql)


def initialize(c, mysql, *, refresh_user_views=True):
    """Additive migration: leave all existing gameplay rows in the default realm."""
    if mysql:
        exists = c.execute("""SELECT 1 FROM information_schema.columns
            WHERE table_schema=DATABASE() AND table_name='users' AND column_name='realm_id'""").fetchone()
    else:
        exists = any(row['name'] == 'realm_id' for row in c.execute('PRAGMA table_info(users)'))
    if not exists:
        c.execute("ALTER TABLE users ADD COLUMN realm_id VARCHAR(20) NOT NULL DEFAULT 'white_rabbit'")
    # Views contain no copied accounts. The global superadmin may manage either
    # realm; ordinary administrators and members belong to exactly one realm.
    for realm in REALMS:
        view = 'realm_' + realm + '_users'
        # Online Boss-only migrations do not change the account-view definition.
        # Keep it in place: live SSE reads can hold its metadata lock for 25s.
        if not refresh_user_views:
            present = (c.execute('SELECT 1 FROM information_schema.views WHERE table_schema=DATABASE() AND table_name=?', (view,)).fetchone()
                       if mysql else c.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (view,)).fetchone())
            if present:
                continue
        if not mysql:
            c.execute('DROP VIEW IF EXISTS ' + view)
        prefix = 'CREATE OR REPLACE VIEW' if mysql else 'CREATE VIEW'
        c.execute(f"{prefix} {view} AS SELECT * FROM users WHERE realm_id='{realm}' OR role='superadmin'")
    migrated = c.execute("SELECT 1 FROM app_migrations WHERE name='realms_v1'").fetchone()
    eliza_migrated = c.execute("SELECT 1 FROM app_migrations WHERE name='realms_eliza_v1'").fetchone()
    snowman_migrated = c.execute("SELECT 1 FROM app_migrations WHERE name='realms_snowman_v1'").fetchone()
    global_migrated = c.execute("SELECT 1 FROM app_migrations WHERE name='realms_global_mushroom_v1'").fetchone()
    if migrated and eliza_migrated and snowman_migrated and global_migrated:
        return
    # Existing realms must receive new Boss tables even after realms_v1 ran.
    # Rebuild combined views after adding them; never copy gameplay rows.
    tables = TABLES if not migrated else tuple(t for t in TABLES if (not eliza_migrated and t.startswith('eliza_')) or (not snowman_migrated and t.startswith('snowman_')) or (not global_migrated and t.startswith('global_mushroom_')))
    for table in tables:
        target = 'red_crab_' + table
        if mysql:
            c.execute(f'CREATE TABLE IF NOT EXISTS {target} LIKE {table}')
        elif not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (target,)).fetchone():
            definition = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()['sql']
            c.execute(rewrite(definition, 'red_crab'))
            for index in c.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)).fetchall():
                sql = rewrite(index['sql'], 'red_crab')
                sql = re.sub(r'\b' + re.escape(index['name']) + r'\b', 'red_crab_' + index['name'], sql)
                c.execute(sql)
    for table in COMBINED:
        view = 'red_crab_combined_' + table
        if not mysql:
            c.execute('DROP VIEW IF EXISTS ' + view)
        prefix = 'CREATE OR REPLACE VIEW' if mysql else 'CREATE VIEW'
        sources = ['red_crab_' + ('' if boss == 'mushroom' else boss + '_') + table for boss in BOSSES]
        c.execute(f'{prefix} {view} AS ' + ' UNION ALL '.join('SELECT * FROM ' + source for source in sources))
    insert = 'INSERT IGNORE' if mysql else 'INSERT OR IGNORE'
    c.execute(f'{insert} INTO red_crab_relay_guard(id) VALUES(1)')
    for boss in BOSSES:
        table = 'red_crab_' + ('' if boss == 'mushroom' else boss + '_') + 'channels'
        c.executemany(f'{insert} INTO {table}(id) VALUES(?)', [(i,) for i in range(1, 61)])
    for marker, applied in (('realms_v1', migrated), ('realms_eliza_v1', eliza_migrated), ('realms_snowman_v1', snowman_migrated), ('realms_global_mushroom_v1', global_migrated)):
        if not applied:
            c.execute('INSERT INTO app_migrations(name,applied_at) VALUES(?,?)', (marker, int(__import__('time').time())))
