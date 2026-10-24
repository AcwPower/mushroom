import hashlib
import hmac
import ipaddress
import re
import json
import os
import secrets
import sqlite3
import threading
import time
import traceback
import sys
import relay
import boss_scope
import realm_scope
import dorr
import view_cache
import service_runtime
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).parent
STATIC_DIR = Path(os.environ.get('STATIC_DIR', str(ROOT)))
DATABASE_URL = os.environ.get('DATABASE_URL', '')
MYSQL = DATABASE_URL.startswith('mysql://') or os.environ.get('DB_ENGINE') == 'mysql'
if DATABASE_URL and not (MYSQL or DATABASE_URL.startswith('sqlite:///')):
    raise RuntimeError('DATABASE_URL must start with mysql:// or sqlite:///')
DB_PATH = DATABASE_URL.removeprefix('sqlite://') if DATABASE_URL.startswith('sqlite:///') else os.environ.get('DB_PATH', str(ROOT / 'timer.db'))
PORT = int(os.environ.get('PORT', '8000'))
HOST = os.environ.get('HOST', '127.0.0.1')
PRODUCTION = os.environ.get('APP_ENV', 'development') == 'production'
APP_BASE_URL = os.environ.get('APP_BASE_URL', '')
COOKIE_NAME = os.environ.get('COOKIE_NAME', 'session')
COOKIE_PATH = os.environ.get('COOKIE_PATH', '/')
CLAIM_TIMEOUT = int(os.environ.get('CLAIM_TIMEOUT_MINUTES', '90')) * 60
CLAIM_PENALTY = int(os.environ.get('CLAIM_PENALTY_MINUTES', '5')) * 60
UNKNOWN_AFTER_GUARANTEE = max(0, int(os.environ.get('UNKNOWN_AFTER_GUARANTEE_MINUTES', '3'))) * 60
IDLE_TIMEOUT = 30 * 60
IDLE_MESSAGE = '连续 30 分钟未操作，已自动退出，请重新登录'
DAY = 86400
LONG_TERM_UNTIL = int(datetime(2099, 12, 31, 23, 59, 59,
                               tzinfo=timezone(timedelta(hours=8))).timestamp())
BOSS_ACCESS = boss_scope.FLAGS
# Bulk Plus closure suspends automatic re-awards. Re-enable deliberately via environment.
PLUS_AUTO_AWARD_ENABLED = os.environ.get('PLUS_AUTO_AWARD_ENABLED', '0') == '1'
LOCK = threading.Lock()
REVISION = 0
REALM_REVISIONS = {realm: 0 for realm in realm_scope.REALMS}
RATE_LOCK = threading.Lock()
RATE_BUCKETS = {}

MYSQL_SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id BIGINT PRIMARY KEY AUTO_INCREMENT, username VARCHAR(30) NOT NULL UNIQUE,
 nickname VARCHAR(30) NOT NULL, phone VARCHAR(15) UNIQUE,
 password_hash VARCHAR(200) NOT NULL, role VARCHAR(20) NOT NULL DEFAULT 'member',
 access_until BIGINT NOT NULL DEFAULT 0, banned_until BIGINT NOT NULL DEFAULT 0,
 frozen BOOLEAN NOT NULL DEFAULT FALSE, rescue_target INT NOT NULL DEFAULT 3,
 session_version BIGINT NOT NULL DEFAULT 0, created_at BIGINT NOT NULL,
 deleted_at BIGINT NULL, registration_ip VARCHAR(45) NULL UNIQUE,
 recovery_hash VARCHAR(200) NULL, pending_approval BOOLEAN NOT NULL DEFAULT FALSE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS channels (
 id INT PRIMARY KEY, killed_at BIGINT NULL, last_killer_id BIGINT NULL,
 claimant_id BIGINT NULL UNIQUE, claimed_at BIGINT NULL, sos_at BIGINT NULL,
 sos_token CHAR(43) NULL UNIQUE, explorer_id BIGINT NULL UNIQUE,
 exploration_claimed_at BIGINT NULL,
 explorer2_id BIGINT NULL UNIQUE, exploration2_claimed_at BIGINT NULL,
 explorer3_id BIGINT NULL UNIQUE, exploration3_claimed_at BIGINT NULL,
 blocked_until BIGINT NOT NULL DEFAULT 0, exploded_at BIGINT NULL,
 CONSTRAINT fk_last_killer FOREIGN KEY(last_killer_id) REFERENCES users(id),
 CONSTRAINT fk_claimant FOREIGN KEY(claimant_id) REFERENCES users(id),
 CONSTRAINT fk_explorer FOREIGN KEY(explorer_id) REFERENCES users(id),
 CONSTRAINT fk_explorer2 FOREIGN KEY(explorer2_id) REFERENCES users(id),
 CONSTRAINT fk_explorer3 FOREIGN KEY(explorer3_id) REFERENCES users(id),
 CONSTRAINT chk_channel CHECK(id BETWEEN 1 AND 60)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS sessions (
 token_hash CHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL,
 version BIGINT NOT NULL, expires_at BIGINT NOT NULL,
 last_seen_at BIGINT NOT NULL DEFAULT 0,
 FOREIGN KEY(user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS release_cooldowns (
 user_id BIGINT PRIMARY KEY, ends_at BIGINT NOT NULL,
 FOREIGN KEY(user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS logs (
 id BIGINT PRIMARY KEY AUTO_INCREMENT, at BIGINT NOT NULL,
 channel_id INT NULL, actor_id BIGINT NULL,
 action VARCHAR(40) NOT NULL, detail TEXT NOT NULL,
 INDEX logs_channel(channel_id,id), INDEX logs_action(action,id),
 FOREIGN KEY(actor_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS notifications (
 id BIGINT PRIMARY KEY AUTO_INCREMENT, user_id BIGINT NOT NULL,
 channel_id INT NOT NULL, kind VARCHAR(40) NOT NULL,
 created_at BIGINT NOT NULL, read_at BIGINT NULL,
 INDEX notifications_user(user_id,read_at,id),
 FOREIGN KEY(user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS robbery_reports (
 id BIGINT PRIMARY KEY AUTO_INCREMENT, channel_id INT NOT NULL,
 reporter_id BIGINT NOT NULL, claim_started_at BIGINT NOT NULL,
 player_id VARCHAR(100) COLLATE utf8mb4_bin NOT NULL, reported_at BIGINT NOT NULL,
 UNIQUE KEY robbery_claim(channel_id,reporter_id,claim_started_at),
 INDEX robbery_player(player_id),
 FOREIGN KEY(channel_id) REFERENCES channels(id),
 FOREIGN KEY(reporter_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
'''


def now():
    return int(time.time())


class MySQLConnection:
    def __init__(self):
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError('Install production dependencies: pip install -r requirements.txt') from exc
        u = urlparse(DATABASE_URL) if DATABASE_URL else None
        self.raw = pymysql.connect(host=(u.hostname if u else os.environ.get('DB_HOST', '127.0.0.1')),
                                   port=(u.port if u else int(os.environ.get('DB_PORT', '3306'))),
                                   user=(unquote(u.username) if u else os.environ['DB_USER']),
                                   password=(unquote(u.password) if u else os.environ['DB_PASSWORD']),
                                   database=(u.path.lstrip('/') if u else os.environ['DB_NAME']),
                                   charset='utf8mb4', autocommit=False,
                                   cursorclass=pymysql.cursors.DictCursor, connect_timeout=5)
        self.raw.cursor().execute('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')

    def __enter__(self): return self
    def __exit__(self, typ, value, tb):
        try:
            if typ: self.raw.rollback()
            else: self.raw.commit()
        finally: self.raw.close()
    def execute(self, sql, params=()):
        if sql == 'BEGIN IMMEDIATE': sql = 'START TRANSACTION'
        sql = sql.replace('?', '%s').replace('max(banned_until,%s)', 'GREATEST(banned_until,%s)')
        sql = sql.replace('max(access_until,%s)', 'GREATEST(access_until,%s)')
        cursor = self.raw.cursor(); cursor.execute(sql, params); return cursor
    def executemany(self, sql, params):
        cursor = self.raw.cursor(); cursor.executemany(sql.replace('?', '%s'), params); return cursor
    def commit(self): self.raw.commit()


def db(boss='mushroom', realm=None):
    if MYSQL: return boss_scope.Connection(MySQLConnection(), boss, realm=realm)
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=15000')
    return boss_scope.Connection(c, boss, realm=realm)


def ensure_exploration_indexes(c):
    # Cover weekly totals and the current member's weekly count separately.
    for name, columns in (
            ('exploration_credits_time_user', 'credited_at,user_id'),
            ('exploration_credits_user_time', 'user_id,credited_at')):
        if MYSQL:
            exists = c.execute('''SELECT 1 FROM information_schema.statistics
                WHERE table_schema=DATABASE() AND table_name='exploration_credits'
                AND index_name=? LIMIT 1''', (name,)).fetchone()
            if not exists:
                c.execute(f'CREATE INDEX {name} ON exploration_credits ({columns})')
        else:
            c.execute(f'CREATE INDEX IF NOT EXISTS {name} ON exploration_credits ({columns})')


def ensure_log_indexes(c):
    indexes = ((f'{c.boss}_logs_action_time_user', ('action', 'at', 'actor_id')),
               (f'{c.boss}_logs_user_action_time', ('actor_id', 'action', 'at')))
    if MYSQL:
        table = c.sql('logs')
        c.execute('SET SESSION lock_wait_timeout=2')
        missing = []
        for name, columns in indexes:
            existing = [row['COLUMN_NAME'] for row in c.execute('''SELECT COLUMN_NAME
                FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=?
                AND index_name=? ORDER BY seq_in_index''', (table, name))]
            if existing:
                require(existing == list(columns), '统计索引定义不一致：' + name)
            else:
                missing.append(f'ADD INDEX {name} ({",".join(columns)})')
        if missing:
            c.execute('ALTER TABLE logs ' + ','.join(missing) + ', ALGORITHM=INPLACE, LOCK=NONE')
    else:
        for name, columns in indexes:
            c.execute(f'CREATE INDEX IF NOT EXISTS {name} ON logs ({",".join(columns)})')


def init_db():
    admin_password = os.environ.get('ADMIN_PASSWORD', '')
    if len(admin_password) < 16:
        raise RuntimeError('Set ADMIN_PASSWORD to a unique password of at least 16 characters')
    if PRODUCTION and not MYSQL:
        raise RuntimeError('Production requires MySQL: set DB_ENGINE=mysql and DB_* variables')
    if not MYSQL: Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        relay.init(c)
        if MYSQL:
            c.execute('''CREATE TABLE IF NOT EXISTS login_records (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,user_id BIGINT NOT NULL,at BIGINT NOT NULL,
                ip VARCHAR(45) NOT NULL,device VARCHAR(300) NOT NULL,
                takeover BOOLEAN NOT NULL,risk VARCHAR(200) NOT NULL,
                INDEX login_user_time(user_id,at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
        else:
            c.execute('''CREATE TABLE IF NOT EXISTS login_records (
                id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,at INTEGER NOT NULL,
                ip TEXT NOT NULL,device TEXT NOT NULL,takeover INTEGER NOT NULL,risk TEXT NOT NULL)''')
            c.execute('CREATE INDEX IF NOT EXISTS login_user_time ON login_records(user_id,at)')
        if MYSQL:
            for statement in MYSQL_SCHEMA.strip().split(';'):
                if statement.strip(): c.execute(statement)
        else:
            c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, nickname TEXT NOT NULL,
          phone TEXT UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
          access_until INTEGER NOT NULL DEFAULT 0, banned_until INTEGER NOT NULL DEFAULT 0,
          frozen INTEGER NOT NULL DEFAULT 0, rescue_target INTEGER NOT NULL DEFAULT 3,
          session_version INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
          deleted_at INTEGER, registration_ip TEXT UNIQUE, recovery_hash TEXT,
          pending_approval INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS channels (
          id INTEGER PRIMARY KEY CHECK(id BETWEEN 1 AND 60), killed_at INTEGER,
          last_killer_id INTEGER REFERENCES users(id), claimant_id INTEGER REFERENCES users(id),
          claimed_at INTEGER, sos_at INTEGER, sos_token TEXT UNIQUE,
          explorer_id INTEGER UNIQUE REFERENCES users(id), exploration_claimed_at INTEGER,
          explorer2_id INTEGER UNIQUE REFERENCES users(id), exploration2_claimed_at INTEGER,
          explorer3_id INTEGER UNIQUE REFERENCES users(id), exploration3_claimed_at INTEGER,
          blocked_until INTEGER NOT NULL DEFAULT 0, exploded_at INTEGER,
          FOREIGN KEY(claimant_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          version INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          last_seen_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS release_cooldowns (
          user_id INTEGER PRIMARY KEY REFERENCES users(id), ends_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS logs (
          id INTEGER PRIMARY KEY, at INTEGER NOT NULL, channel_id INTEGER,
          actor_id INTEGER REFERENCES users(id), action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          channel_id INTEGER NOT NULL, kind TEXT NOT NULL,
          created_at INTEGER NOT NULL, read_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS robbery_reports (
          id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id),
          reporter_id INTEGER NOT NULL REFERENCES users(id), claim_started_at INTEGER NOT NULL,
          player_id TEXT NOT NULL, reported_at INTEGER NOT NULL,
          UNIQUE(channel_id,reporter_id,claim_started_at)
        );
        CREATE INDEX IF NOT EXISTS logs_channel ON logs(channel_id, id);
        CREATE INDEX IF NOT EXISTS logs_action ON logs(action, id);
        CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, read_at, id);
        CREATE INDEX IF NOT EXISTS robbery_player ON robbery_reports(player_id);
            ''')
        if MYSQL:
            column = c.execute('''SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='users' AND column_name='deleted_at' ''').fetchone()
            if not column:
                c.execute('ALTER TABLE users ADD COLUMN deleted_at BIGINT NULL')
        elif not any(row['name'] == 'deleted_at' for row in c.execute('PRAGMA table_info(users)')):
            c.execute('ALTER TABLE users ADD COLUMN deleted_at INTEGER')
        for name, mysql_type, sqlite_type in (
                ('registration_ip', 'VARCHAR(45) NULL UNIQUE', 'TEXT'),
                ('recovery_hash', 'VARCHAR(200) NULL', 'TEXT'),
                ('pending_approval', 'BOOLEAN NOT NULL DEFAULT FALSE', 'INTEGER NOT NULL DEFAULT 0'),
                ('plus_until', 'BIGINT NULL DEFAULT 0', 'INTEGER DEFAULT 0'),
                ('plus_earned_at', 'BIGINT NOT NULL DEFAULT 0', 'INTEGER NOT NULL DEFAULT 0')):
            if MYSQL:
                exists = c.execute('''SELECT 1 FROM information_schema.columns
                    WHERE table_schema=DATABASE() AND table_name='users' AND column_name=?''', (name,)).fetchone()
                if not exists:
                    c.execute(f'ALTER TABLE users ADD COLUMN {name} {mysql_type}')
            elif not any(row['name'] == name for row in c.execute('PRAGMA table_info(users)')):
                c.execute(f'ALTER TABLE users ADD COLUMN {name} {sqlite_type}')
        if MYSQL:
            has_boss_access = c.execute('''SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='users' AND column_name='boss_access' ''').fetchone()
        else:
            has_boss_access = any(row['name'] == 'boss_access' for row in c.execute('PRAGMA table_info(users)'))
        if not has_boss_access:
            c.execute('ALTER TABLE users ADD COLUMN boss_access INTEGER NOT NULL DEFAULT 3')
            c.execute('UPDATE users SET boss_access=3 WHERE deleted_at IS NULL')
            c.execute('UPDATE users SET access_until=? WHERE role=? AND deleted_at IS NULL AND pending_approval=0 AND access_until<?',
                      (LONG_TERM_UNTIL, 'member', LONG_TERM_UNTIL))
            c.execute('UPDATE users SET plus_until=? WHERE role=? AND deleted_at IS NULL AND plus_until>? AND plus_until<?',
                      (LONG_TERM_UNTIL, 'member', now(), LONG_TERM_UNTIL))
        if not MYSQL:
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_registration_ip ON users(registration_ip)')
        c.execute('UPDATE users SET registration_ip=NULL WHERE deleted_at IS NOT NULL AND registration_ip IS NOT NULL')
        if MYSQL:
            column = c.execute('''SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='channels' AND column_name='sos_token' ''').fetchone()
            if not column:
                c.execute('ALTER TABLE channels ADD COLUMN sos_token CHAR(43) NULL UNIQUE')
        elif not any(row['name'] == 'sos_token' for row in c.execute('PRAGMA table_info(channels)')):
            c.execute('ALTER TABLE channels ADD COLUMN sos_token TEXT')
        if not MYSQL:
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS channels_sos_token ON channels(sos_token)')
        if MYSQL:
            column = c.execute('''SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='channels' AND column_name='explorer_id' ''').fetchone()
            if not column:
                c.execute('ALTER TABLE channels ADD COLUMN explorer_id BIGINT NULL UNIQUE')
                c.execute('ALTER TABLE channels ADD CONSTRAINT fk_explorer FOREIGN KEY(explorer_id) REFERENCES users(id)')
            column = c.execute('''SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='channels' AND column_name='exploration_claimed_at' ''').fetchone()
            if not column:
                c.execute('ALTER TABLE channels ADD COLUMN exploration_claimed_at BIGINT NULL')
        else:
            columns = {row['name'] for row in c.execute('PRAGMA table_info(channels)')}
            if 'explorer_id' not in columns:
                c.execute('ALTER TABLE channels ADD COLUMN explorer_id INTEGER REFERENCES users(id)')
            if 'exploration_claimed_at' not in columns:
                c.execute('ALTER TABLE channels ADD COLUMN exploration_claimed_at INTEGER')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS channels_explorer_id ON channels(explorer_id)')
        for name, sql_type in (('parallel_id', 'BIGINT NULL'), ('parallel_claimed_at', 'BIGINT NULL')):
            if MYSQL:
                exists = c.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='channels' AND column_name=?", (name,)).fetchone()
            else:
                exists = any(r['name'] == name for r in c.execute('PRAGMA table_info(channels)'))
            if not exists:
                c.execute(f'ALTER TABLE channels ADD COLUMN {name} {sql_type}')
        if MYSQL:
            if not c.execute("SHOW INDEX FROM channels WHERE Key_name='channels_parallel_id'").fetchone():
                c.execute('CREATE UNIQUE INDEX channels_parallel_id ON channels(parallel_id)')
        else:
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS channels_parallel_id ON channels(parallel_id)')
        for slot in (2, 3):
            owner_column, time_column = f'explorer{slot}_id', f'exploration{slot}_claimed_at'
            if MYSQL:
                owner_exists = c.execute('''SELECT 1 FROM information_schema.columns
                    WHERE table_schema=DATABASE() AND table_name='channels' AND column_name=?''', (owner_column,)).fetchone()
                if not owner_exists:
                    c.execute(f'ALTER TABLE channels ADD COLUMN {owner_column} BIGINT NULL UNIQUE')
                    c.execute(f'ALTER TABLE channels ADD CONSTRAINT fk_explorer{slot} FOREIGN KEY({owner_column}) REFERENCES users(id)')
                time_exists = c.execute('''SELECT 1 FROM information_schema.columns
                    WHERE table_schema=DATABASE() AND table_name='channels' AND column_name=?''', (time_column,)).fetchone()
                if not time_exists:
                    c.execute(f'ALTER TABLE channels ADD COLUMN {time_column} BIGINT NULL')
            else:
                columns = {row['name'] for row in c.execute('PRAGMA table_info(channels)')}
                if owner_column not in columns:
                    c.execute(f'ALTER TABLE channels ADD COLUMN {owner_column} INTEGER REFERENCES users(id)')
                if time_column not in columns:
                    c.execute(f'ALTER TABLE channels ADD COLUMN {time_column} INTEGER')
                c.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS channels_{owner_column} ON channels({owner_column})')
        if MYSQL:
            column = c.execute('''SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='channels' AND column_name='blocked_until' ''').fetchone()
            if not column:
                c.execute('ALTER TABLE channels ADD COLUMN blocked_until BIGINT NOT NULL DEFAULT 0')
        elif not any(row['name'] == 'blocked_until' for row in c.execute('PRAGMA table_info(channels)')):
            c.execute('ALTER TABLE channels ADD COLUMN blocked_until INTEGER NOT NULL DEFAULT 0')
        if MYSQL:
            column = c.execute('''SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='channels' AND column_name='exploded_at' ''').fetchone()
            if not column:
                c.execute('ALTER TABLE channels ADD COLUMN exploded_at BIGINT NULL')
        elif not any(row['name'] == 'exploded_at' for row in c.execute('PRAGMA table_info(channels)')):
            c.execute('ALTER TABLE channels ADD COLUMN exploded_at INTEGER')
        if MYSQL:
            column = c.execute('''SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='channels' AND column_name='note' ''').fetchone()
            if not column:
                c.execute("ALTER TABLE channels ADD COLUMN note VARCHAR(200) NOT NULL DEFAULT ''")
        elif not any(row['name'] == 'note' for row in c.execute('PRAGMA table_info(channels)')):
            c.execute("ALTER TABLE channels ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        payment_pk = 'BIGINT PRIMARY KEY AUTO_INCREMENT' if MYSQL else 'INTEGER PRIMARY KEY'
        c.execute(f"""CREATE TABLE IF NOT EXISTS payment_marks (
            id {payment_pk}, user_id BIGINT NOT NULL, payment_date VARCHAR(10) NOT NULL,
            paid INTEGER NOT NULL, actor_id BIGINT NOT NULL, marked_at BIGINT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS page_presence (
            user_id BIGINT PRIMARY KEY, session_version BIGINT NOT NULL,
            boss VARCHAR(20) NOT NULL, seen_at BIGINT NOT NULL)""")
        if MYSQL:
            column = c.execute('''SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='sessions' AND column_name='last_seen_at' ''').fetchone()
            if not column:
                c.execute('ALTER TABLE sessions ADD COLUMN last_seen_at BIGINT NOT NULL DEFAULT 0')
        elif not any(row['name'] == 'last_seen_at' for row in c.execute('PRAGMA table_info(sessions)')):
            c.execute('ALTER TABLE sessions ADD COLUMN last_seen_at INTEGER NOT NULL DEFAULT 0')
        if MYSQL:
            column = c.execute('''SELECT 1 FROM information_schema.columns
                WHERE table_schema=DATABASE() AND table_name='sessions' AND column_name='last_action_at' ''').fetchone()
            if not column:
                c.execute('ALTER TABLE sessions ADD COLUMN last_action_at BIGINT NOT NULL DEFAULT 0')
                c.execute('UPDATE sessions SET last_action_at=?', (now(),))
        elif not any(row['name'] == 'last_action_at' for row in c.execute('PRAGMA table_info(sessions)')):
            c.execute('ALTER TABLE sessions ADD COLUMN last_action_at INTEGER NOT NULL DEFAULT 0')
            c.execute('UPDATE sessions SET last_action_at=?', (now(),))
        c.execute('''CREATE TABLE IF NOT EXISTS exploration_credits (
            channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL, claimed_at BIGINT NOT NULL,
            killed_at BIGINT NOT NULL, credited_at BIGINT NOT NULL,
            PRIMARY KEY(channel_id,user_id,claimed_at))''')
        ensure_exploration_indexes(c)
        if MYSQL:
            column = c.execute('''SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE()
                AND table_name='channels' AND column_name='credit_blocked_until' ''').fetchone()
        else:
            column = any(r['name'] == 'credit_blocked_until' for r in c.execute('PRAGMA table_info(channels)'))
        if not column:
            c.execute('ALTER TABLE channels ADD COLUMN credit_blocked_until BIGINT NOT NULL DEFAULT 0')
        for row in c.execute('SELECT id FROM channels WHERE sos_at IS NOT NULL AND sos_token IS NULL'):
            c.execute('UPDATE channels SET sos_token=? WHERE id=?', (secrets.token_urlsafe(32), row['id']))
        if MYSQL:
            c.executemany('INSERT IGNORE INTO channels(id) VALUES(?)', [(i,) for i in range(1, 61)])
        else:
            c.executemany('INSERT OR IGNORE INTO channels(id) VALUES(?)', [(i,) for i in range(1, 61)])
        # Existing channels reset by the grace-period rule retain their reason.
        for channel in c.execute('SELECT id FROM channels WHERE killed_at IS NULL AND exploded_at IS NULL').fetchall():
            latest = c.execute('''SELECT action,at FROM logs WHERE channel_id=?
                AND action IN ('reset_unknown','discard','kill','edit_kill','report_robbed','screenshot_import')
                ORDER BY id DESC LIMIT 1''', (channel['id'],)).fetchone()
            if latest and latest['action'] == 'reset_unknown':
                c.execute('UPDATE channels SET exploded_at=? WHERE id=?', (latest['at'], channel['id']))
        if not c.execute("SELECT 1 FROM users WHERE role='superadmin'").fetchone():
            c.execute('INSERT INTO users(username,nickname,password_hash,role,access_until,created_at) VALUES(?,?,?,?,?,?)',
                      ('root', '超级管理员', hash_password(admin_password), 'superadmin', now() + 10 * 365 * DAY, now()))
        # Ordinary administrators retain their explicitly granted role.
        boss_scope.initialize(c.base, MYSQL)
        for fixed_boss in boss_scope.FIXED_CLOCK_BOSSES:
            dorr.init(c.base, sys.modules[__name__], fixed_boss)
        for boss in boss_scope.BOSSES:
            ensure_log_indexes(boss_scope.Connection(c, boss))
        realm_scope.initialize(c.base, MYSQL)
        boss_scope.grant_snowman_access(c)


def set_boss_cooldown(c, uid, until):
    row = c.execute('SELECT ends_at FROM release_cooldowns WHERE user_id=?', (uid,)).fetchone()
    until = max(until, row['ends_at'] if row else 0)
    c.execute('DELETE FROM release_cooldowns WHERE user_id=?', (uid,))
    c.execute('INSERT INTO release_cooldowns(user_id,ends_at) VALUES(?,?)', (uid, until))


def claim_cooldown(c, user):
    if user['role'] == 'superadmin':
        return 0
    row = c.execute('SELECT ends_at FROM release_cooldowns WHERE user_id=?', (user['id'],)).fetchone()
    return row['ends_at'] if row and row['ends_at'] > now() else 0


def voluntary_release_minutes(c, user):
    return 10 if c.boss in ('mushroom', 'snowman', 'global_mushroom') and user['role'] in ('member', 'admin') else 0


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ':' + digest.hex()


def check_password(password, encoded):
    try:
        salt, digest = encoded.split(':')
        return hmac.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1), bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def log(c, action, channel=None, actor=None, detail=''):
    c.execute('INSERT INTO logs(at,channel_id,actor_id,action,detail) VALUES(?,?,?,?,?)', (now(), channel, actor, action, detail))


def changed(realm=None):
    global REVISION
    with LOCK:
        REVISION += 1
        for selected in ([realm] if realm is not None else realm_scope.REALMS):
            REALM_REVISIONS[selected] += 1


def active(user):
    t = now()
    return bool(user and user['deleted_at'] is None and not user['pending_approval'] and not user['frozen']
                and user['banned_until'] <= t and user['access_until'] > t)


def has_plus(u):
    return u['role'] == 'superadmin' or u['plus_until'] is None or u['plus_until'] > now()


def allowed_bosses(user):
    return list(BOSS_ACCESS) if user['role'] == 'superadmin' else [
        boss for boss, flag in BOSS_ACCESS.items() if user['boss_access'] & flag]


def can_access_boss(user, boss):
    return user['role'] == 'superadmin' or bool(user['boss_access'] & BOSS_ACCESS[boss])


def public_user(u):
    return {**{k: u[k] for k in ('id', 'username', 'nickname', 'role', 'access_until', 'banned_until',
                               'frozen', 'rescue_target', 'pending_approval', 'plus_until', 'plus_earned_at', 'boss_access', 'realm_id')},
            'is_plus': has_plus(u)}


def channel_detail(row, boss='mushroom'):
    earliest, latest = boss_scope.TIMING[boss]
    killed = row['killed_at']
    return {'id': row['id'], 'killed_at': killed, 'earliest_at': killed + earliest if killed else None,
            'latest_at': killed + latest if killed else None, 'claimed_at': row['claimed_at'] or row['parallel_claimed_at'],
            'sos_at': row['sos_at'], 'claimant_id': row['claimant_id'] or row['parallel_id'], 'last_killer_id': row['last_killer_id'],
            'is_parallel': row['parallel_id'] is not None,
            'explorer_id': exploration_owner(row), 'exploration_claimed_at': exploration_time(row),
            'blocked_until': row['blocked_until'], 'exploded_at': row['exploded_at'], 'note': row['note']}


EXPLORATION_SLOTS = (('explorer_id', 'exploration_claimed_at'),
                     ('explorer2_id', 'exploration2_claimed_at'),
                     ('explorer3_id', 'exploration3_claimed_at'))
NO_EXPLORER_SQL = 'parallel_id IS NULL AND ' + ' AND '.join(f'{owner} IS NULL' for owner, _ in EXPLORATION_SLOTS)


def channel_owner(row):
    return row['claimant_id'] or row['parallel_id'] or exploration_owner(row)


def ready_unclaimed(row, boss='mushroom'):
    return bool(row and not channel_owner(row) and row['blocked_until'] <= now()
                and row['killed_at'] is not None and row['killed_at'] + boss_scope.TIMING[boss][0] <= now())


def promote_parallel(c, uid):
    # Called inside the guarded kill transaction; preserve the original claim clock.
    if not uid or c.execute('SELECT id FROM channels WHERE claimant_id=?', (uid,)).fetchone():
        return
    row = c.execute('SELECT * FROM channels WHERE parallel_id=?', (uid,)).fetchone()
    if row:
        c.execute('''UPDATE channels SET claimant_id=parallel_id,claimed_at=parallel_claimed_at,
            parallel_id=NULL,parallel_claimed_at=NULL WHERE id=? AND parallel_id=?''', (row['id'], uid))
        log(c, 'claim', row['id'], uid, '主频道完成击杀，并发频道自动转为主认领；原计时保留')


def exploration_owner(row):
    return next((row[owner] for owner, _ in EXPLORATION_SLOTS if row[owner] is not None), None)


def exploration_time(row):
    return next((row[claimed] for owner, claimed in EXPLORATION_SLOTS if row[owner] is not None), None)


def can_edit_kill(row, user):
    if row['killed_at'] is None or exploration_owner(row) is not None:
        return False
    if user['role'] == 'superadmin':
        return True
    if row['claimant_id'] is not None or row['parallel_id'] is not None:
        return (row['claimant_id'] or row['parallel_id']) == user['id']
    return row['last_killer_id'] == user['id']


def user_explorations(c, user_id):
    where = ' OR '.join(f'{owner}=?' for owner, _ in EXPLORATION_SLOTS)
    return c.execute(f'SELECT * FROM channels WHERE {where} ORDER BY id', (user_id,) * 3).fetchall()


BEIJING = timezone(timedelta(hours=8))


def week_bounds(timestamp=None):
    current = datetime.fromtimestamp(now() if timestamp is None else timestamp, BEIJING)
    start = (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int((start + timedelta(days=7)).timestamp())


def beijing_day_start(timestamp=None):
    current = datetime.fromtimestamp(now() if timestamp is None else timestamp, BEIJING)
    return int(current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def plus_progress(c, uid):
    c = boss_scope.totals(c)
    today = beijing_day_start()
    start, end = today - 5 * DAY, today + DAY
    login_days = {beijing_day_start(r['at']) for r in c.execute(
        'SELECT at FROM login_records WHERE user_id=? AND at>=? AND at<?', (uid, start, end))}
    counts = {}
    # Opening kills must earn a valid credit; corrections and discard loops do not count.
    events = list(c.execute("SELECT at FROM logs WHERE actor_id=? AND action='kill' AND detail<>? AND at>=? AND at<?",
                            (uid, '开荒', start, end)))
    events += list(c.execute('SELECT credited_at AS at FROM exploration_credits WHERE user_id=? AND credited_at>=? AND credited_at<?',
                             (uid, start, end)))
    for event in events:
        day = beijing_day_start(event['at'])
        counts[day] = counts.get(day, 0) + 1
    qualifies_today = today in login_days and counts.get(today, 0) >= 6
    cursor = today if qualifies_today else today - DAY
    streak = 0
    while streak < 5 and cursor in login_days and counts.get(cursor, 0) >= 6:
        streak += 1
        cursor -= DAY
    return {'days': streak, 'required_days': 5, 'today_kills': counts.get(today, 0),
            'required_kills': 6, 'logged_in_today': today in login_days}


def maybe_award_plus(c, uid):
    if not PLUS_AUTO_AWARD_ENABLED:
        return False
    user = c.execute('SELECT * FROM users WHERE id=?' + (' FOR UPDATE' if MYSQL else ''), (uid,)).fetchone()
    if not active(user) or user['role'] not in ('member', 'admin') or has_plus(user) or user['plus_earned_at']:
        return False
    if plus_progress(c, uid)['days'] < 5:
        return False
    updated = c.execute('UPDATE users SET plus_until=NULL,plus_earned_at=? WHERE id=? AND plus_earned_at=0', (now(), uid))
    if updated.rowcount != 1:
        return False
    log(c, 'user_plus_auto', None, uid, '连续5天每天登录且有效击杀至少6次，自动升级长期 Plus')
    return True


def credit_exploration(c, ch, uid, killed):
    claimed = ch['claimed_at'] if ch['claimant_id'] == uid else (
        exploration_time(ch) if exploration_owner(ch) == uid else None)
    if (ch['killed_at'] is not None or claimed is None or killed < claimed - 120
            or killed + boss_scope.TIMING[c.boss][1] <= now() or killed > now() + 60
            or ch['credit_blocked_until'] > now()):
        return
    if c.execute('SELECT 1 FROM exploration_credits WHERE channel_id=? AND user_id=? AND claimed_at=?',
                 (ch['id'], uid, claimed)).fetchone():
        return
    c.execute('INSERT INTO exploration_credits VALUES(?,?,?,?,?)', (ch['id'],uid,claimed,killed,now()))
    c.execute('UPDATE channels SET credit_blocked_until=? WHERE id=?', (killed + boss_scope.TIMING[c.boss][1], ch['id']))


def exploration_board(c, uid):
    start, end = week_bounds()
    rows = [dict(r) for r in c.execute('''SELECT u.id AS user_id,u.nickname,count(*) AS total
        FROM exploration_credits e JOIN users u ON u.id=e.user_id
        WHERE e.credited_at>=? AND e.credited_at<? AND u.deleted_at IS NULL
        GROUP BY u.id,u.nickname ORDER BY total DESC,u.nickname ASC,u.id ASC LIMIT 20''', (start,end))]
    mine = c.execute('SELECT count(*) AS total FROM exploration_credits WHERE user_id=? AND credited_at>=? AND credited_at<?',
                     (uid,start,end)).fetchone()['total']
    return {'rows':rows,'mine':mine,'starts_at':start,'ends_at':end}


def weekly_sos_board(c, uid):
    start, end = week_bounds()
    rows = [dict(r) for r in c.execute('''SELECT u.id AS user_id,u.nickname,count(*) AS total
        FROM logs l JOIN users u ON u.id=l.actor_id
        WHERE l.action='accept_sos' AND l.at>=? AND l.at<? AND u.deleted_at IS NULL
        GROUP BY u.id,u.nickname ORDER BY total DESC,u.nickname ASC,u.id ASC LIMIT 20''', (start,end))]
    mine = c.execute('''SELECT count(*) AS total FROM logs
        WHERE action='accept_sos' AND actor_id=? AND at>=? AND at<?''', (uid,start,end)).fetchone()['total']
    return {'rows': rows, 'mine': mine, 'starts_at': start, 'ends_at': end}


def retreat_board(c, uid):
    """Daily voluntary releases and successful SOS requests across both bosses."""
    start = beijing_day_start()
    end = start + DAY
    rows = [dict(r) for r in c.execute('''SELECT u.id AS user_id,u.nickname,
        SUM(CASE WHEN l.action='release' THEN 1 ELSE 0 END) AS releases,
        SUM(CASE WHEN l.action='sos' THEN 1 ELSE 0 END) AS requests,COUNT(*) AS total
        FROM logs l JOIN users u ON u.id=l.actor_id
        WHERE (l.action='sos' OR (l.action='release' AND l.detail IN
            ('主动释放，冻结认领 10 分钟','主动释放，无认领冷却')))
        AND l.at>=? AND l.at<? AND u.deleted_at IS NULL AND u.role IN ('member','admin')
        GROUP BY u.id,u.nickname ORDER BY total DESC,u.nickname ASC,u.id ASC''', (start,end))]
    # MySQL returns SUM(integer) as Decimal, which JSON cannot serialize.
    for row in rows:
        for key in ('releases', 'requests', 'total'):
            row[key] = int(row[key] or 0)
    mine = next((r['total'] for r in rows if r['user_id'] == uid), 0)
    return {'rows': rows, 'mine': mine, 'starts_at': start, 'ends_at': end}


def weekly_rankings(c):
    # Keep the response key compatible with already-open pages; all counts are daily.
    start = beijing_day_start()
    end = start + DAY
    def member_rank(action, opening=False):
        detail_filter = "AND l.detail='开荒'" if opening else ''
        return [dict(row) for row in c.execute(f'''SELECT u.id AS user_id,u.nickname,count(*) AS total
            FROM logs l JOIN users u ON u.id=l.actor_id
            WHERE l.action=? {detail_filter} AND l.at>=? AND l.at<? AND u.deleted_at IS NULL
            GROUP BY u.id,u.nickname ORDER BY total DESC,u.nickname ASC,u.id ASC LIMIT 20''',
            (action, start, end))]
    robbed = [dict(row) for row in c.execute('''SELECT player_id,count(*) AS reports,
        max(reported_at) AS last_reported_at FROM robbery_reports
        WHERE reported_at>=? AND reported_at<? GROUP BY player_id
        ORDER BY reports DESC,last_reported_at DESC,player_id ASC LIMIT 20''', (start, end))]
    return {'starts_at': start, 'ends_at': end,
            'exploration': member_rank('kill', opening=True),
            'rescue': member_rank('accept_sos'), 'robbery': robbed}


@view_cache.cached()
def spawn_pool(c):
    # Only unclaimed channels still inside their current spawn cycle belong in the shared pool.
    t = now()
    return c.execute('''SELECT * FROM channels WHERE killed_at IS NOT NULL AND claimant_id IS NULL AND parallel_id IS NULL
        AND blocked_until <= ? AND killed_at + ? > ? ORDER BY killed_at ASC, id ASC''', (t, boss_scope.TIMING[c.boss][1], t)).fetchall()


def recommendations(c, user_id, pool, limit=2):
    # The pool and recommendations share chronological eligibility, including own kills.
    options = sorted((row for row in pool if channel_owner(row) is None),
                     key=lambda row: (row['killed_at'], row['id']))
    selected = options[:limit]
    return (selected + [None] * limit)[:limit]


def member_channel_activity(c):
    """Current Boss holdings, including parallel/opening slots and shared Dorr teams."""
    activity = {}

    def add(uid, cid, opening):
        if uid is not None:
            entry = activity.setdefault(uid, {'claimed': [], 'exploring': []})
            entry['exploring' if opening else 'claimed'].append(cid)

    if c.boss in boss_scope.FIXED_CLOCK_BOSSES:
        rows = c.execute('''SELECT ch.id,ch.claimant_id,ch.team_id,ch.claim_kind,m.user_id
            FROM channels ch LEFT JOIN clock_teams t ON t.id=ch.team_id AND t.active=1
            LEFT JOIN clock_team_members m ON m.team_id=t.id
            WHERE ch.claimant_id IS NOT NULL ORDER BY ch.id''')
        for row in rows:
            add(row['user_id'] if row['team_id'] else row['claimant_id'],
                row['id'], row['claim_kind'] == 'opening')
    else:
        owners = ('claimant_id', 'parallel_id', *(key for key, _ in EXPLORATION_SLOTS))
        rows = c.execute('SELECT id,killed_at,' + ','.join(owners) +
                         ' FROM channels WHERE ' + ' OR '.join(key + ' IS NOT NULL' for key in owners) + ' ORDER BY id')
        for row in rows:
            for uid in {row[key] for key in owners} - {None}:
                add(uid, row['id'], row['killed_at'] is None)
    return activity


def member_presence(c, viewer=None):
    t = now()
    recent_boss_killers = {row['actor_id'] for row in c.execute('''SELECT DISTINCT actor_id FROM logs
        WHERE action='kill' AND actor_id IS NOT NULL AND at>=? AND at<=?''', (t - 3600, t))}
    rows = c.execute('''SELECT u.id,u.nickname,u.role,u.boss_access,
        COALESCE(k.kills,0) AS kills,
        CASE WHEN u.frozen=0 AND u.banned_until<=? AND u.access_until>?
          AND EXISTS(SELECT 1 FROM sessions s WHERE s.user_id=u.id
            AND s.version=u.session_version AND s.expires_at>?
            AND s.last_action_at>?) THEN 1 ELSE 0 END AS online
        FROM users u LEFT JOIN (SELECT actor_id,count(*) AS kills FROM logs
          WHERE action='kill' AND at>=? AND at<? GROUP BY actor_id) k ON k.actor_id=u.id
        WHERE u.deleted_at IS NULL AND u.pending_approval=0
        ORDER BY online DESC,kills DESC,u.nickname ASC,u.id ASC''',
        (t, t, t, t - IDLE_TIMEOUT, beijing_day_start(t), beijing_day_start(t) + DAY))
    page_users = {r['user_id'] for r in c.execute("""SELECT p.user_id FROM page_presence p
        JOIN users u ON u.id=p.user_id WHERE p.boss=? AND p.seen_at>?
        AND p.session_version=u.session_version""", (c.boss,t-60))}
    visible_bosses = [boss for boss in boss_scope.BOSSES if can_access_boss(viewer, boss)] if viewer is not None else [c.boss]
    activities = {boss: member_channel_activity(boss_scope.Connection(c, boss)) for boss in visible_bosses}
    activity = activities.get(c.boss, {})
    return [{**dict(row), 'recent_boss_kill': row['id'] in recent_boss_killers,
             'on_boss_page': row['id'] in page_users,
             'channel_activity': activity.get(row['id'], {'claimed': [], 'exploring': []})
                 if can_access_boss(row, c.boss) else {'claimed': [], 'exploring': []},
             'activity_by_boss': {boss: holdings.get(row['id'], {'claimed': [], 'exploring': []})
                 for boss, holdings in activities.items() if can_access_boss(row, boss)}} for row in rows]


def sos_origin(c, row):
    # Match the current acquisition, not an older SOS on the same channel.
    if not row['claimant_id'] or not row['claimed_at']:
        return None
    event = c.execute("""SELECT action,actor_id,detail,at FROM logs WHERE channel_id=?
        AND action IN ('accept_sos','claim','claim_parallel','assign','relay_arrive')
        ORDER BY id DESC LIMIT 1""", (row['id'],)).fetchone()
    if not event or event['action'] != 'accept_sos' or event['actor_id'] != row['claimant_id'] or event['at'] < row['claimed_at']:
        return None
    match = re.fullmatch(r'来自用户 ([0-9]+)', event['detail'] or '')
    if not match:
        return None
    source = c.execute('SELECT nickname FROM users WHERE id=?', (int(match[1]),)).fetchone()
    return source['nickname'] if source else '已删除成员'


def other_boss_claims(c, user):
    """Only the signed-in user's holdings, using server-owned boss namespaces."""
    uid = user['id']
    cards = []
    owner_columns = ('claimant_id', 'parallel_id', *(owner for owner, _ in EXPLORATION_SLOTS))
    for boss in boss_scope.BOSSES:
        if boss == c.boss or not can_access_boss(user, boss):
            continue
        if boss in boss_scope.FIXED_CLOCK_BOSSES:
            # Fixed-clock holdings belong only on their own Boss page.
            continue
        scoped = boss_scope.Connection(c, boss)
        holdings = scoped.execute('SELECT * FROM channels WHERE ' +
                                 ' OR '.join(f'{column}=?' for column in owner_columns) + ' ORDER BY id',
                                 (uid,) * len(owner_columns)).fetchall()
        if not holdings:
            continue
        relays = {r['channel_id']: r for r in relay.view(scoped, sys.modules[__name__], user)
                  if r['owner_id'] == uid}
        for row in holdings:
            detail = {**channel_detail(row, boss), 'sos_from_name': sos_origin(scoped, row)}
            for key in ('claimant_id', 'explorer_id', 'last_killer_id'):
                detail.pop(key, None)
            kind = 'primary' if row['claimant_id'] == uid else 'parallel' if row['parallel_id'] == uid else 'exploration'
            handoff = relays.get(row['id'])
            detail.update(boss=boss, boss_name=boss_scope.BOSSES[boss], claim_kind=kind,
                          relay={key: handoff.get(key) for key in ('state', 'started_at', 'receiver_name',
                              'request_token', 'arrival_until', 'relay_count')} if handoff else None)
            cards.append(detail)
    order = {'primary': 0, 'parallel': 1, 'exploration': 2}
    return sorted(cards, key=lambda card: (card['boss'], order[card['claim_kind']], card['id']))


def cross_support(c, user):
    result = {'sos': [], 'relays': []}
    for boss in boss_scope.WINDOW_BOSSES:
        if not can_access_boss(user, boss):
            continue
        scoped = boss_scope.Connection(c, boss)
        held = scoped.execute('SELECT id FROM channels WHERE claimant_id=?', (user['id'],)).fetchone()
        blocked = claim_cooldown(scoped, user) > now()
        reserved = 'claimant_id' in relay.reserved_slots(scoped, user['id'])
        reason = '该 Boss 认领冻结中' if blocked else '该 Boss 常规名额已为接力预留' if reserved else ''
        for row in scoped.execute('''SELECT id,sos_token,killed_at,
                (SELECT nickname FROM users WHERE users.id=channels.claimant_id) AS requester_name FROM channels
                WHERE sos_at IS NOT NULL AND claimant_id!=? AND blocked_until<=? ORDER BY sos_at''', (user['id'], now())):
            result['sos'].append({'boss': boss, 'channel_id': row['id'], 'requester_name': row['requester_name'], 'request_id': row['sos_token'],
                'earliest_at': row['killed_at']+boss_scope.TIMING[boss][0] if row['killed_at'] else None,
                'latest_at': row['killed_at']+boss_scope.TIMING[boss][1] if row['killed_at'] else None,
                'accept_reason': reason, 'held_channel': held['id'] if held else None})
        if boss != c.boss:
            result['relays'].extend({**r, 'boss': boss} for r in relay.view(scoped, sys.modules[__name__], user) if r['state'] != 'idle')
    return result


def daily_kill_details(c, start, date):
    users = {r['id']: {'user_id': r['id'], 'nickname': r['nickname'], 'deleted': r['deleted_at'] is not None,
                      'counts': {boss: 0 for boss in boss_scope.BOSSES}, 'total': 0}
             for r in c.execute('SELECT id,nickname,deleted_at FROM users')}
    for boss in boss_scope.BOSSES:
        scoped = boss_scope.Connection(c, boss)
        for row in scoped.execute("SELECT actor_id,count(*) AS n FROM logs WHERE action='kill' AND at>=? AND at<? GROUP BY actor_id", (start, start+DAY)):
            if row['actor_id'] in users:
                users[row['actor_id']]['counts'][boss] = int(row['n'])
                users[row['actor_id']]['total'] += int(row['n'])
    for u in users.values():
        u['payment'] = {'paid': False, 'version': 0, 'history': []}
    for r in c.execute("""SELECT p.*,u.nickname AS actor_name FROM payment_marks p
        LEFT JOIN users u ON u.id=p.actor_id WHERE payment_date=? ORDER BY p.id DESC""",(date,)):
        if r['user_id'] not in users:
            continue
        mark = users[r['user_id']]['payment']
        if not mark['version']:
            mark.update(paid=bool(r['paid']),version=r['id'])
        mark['history'].append({'paid':bool(r['paid']),'at':r['marked_at'],'actor':r['actor_name'] or '管理员'})
    rows = sorted((u for u in users.values() if not u['deleted'] or u['total'] or u['payment']['version']), key=lambda u: (-u['total'], u['nickname'], u['user_id']))
    return {'date': date, 'rows': rows, 'total': sum(u['total'] for u in rows),
            'realm_id': c.realm or realm_scope.DEFAULT,
            'realm_name': realm_scope.REALMS[c.realm or realm_scope.DEFAULT]}


COOPERATION_BLOCK_REASON = '僵尸线路积压：近72小时蘑菇王至少10次、僵尸0次，暂不能新增蘑菇王并发。完成1次有效僵尸击杀或积压缓解后自动恢复；普通认领不受影响。'


@view_cache.cached(key=lambda c, user: user['id'])
def cooperation_status(c, user):
    if user['role'] == 'superadmin' or not all(can_access_boss(user, b) for b in ('mushroom', 'zombie')):
        return None
    t = now()
    counts = {}
    for boss in ('mushroom', 'zombie'):
        scoped = boss_scope.Connection(c, boss)
        # Use the same credited-opening rule as activity progress. Opening logs also
        # contain unclaimed backfills and discard loops, which must not unlock support.
        timed = scoped.execute("SELECT count(*) AS n FROM logs WHERE actor_id=? AND action='kill' AND detail<>'开荒' AND at>=? AND at<=?",
                               (user['id'], t-3*DAY, t)).fetchone()['n']
        opening = scoped.execute('SELECT count(*) AS n FROM exploration_credits WHERE user_id=? AND credited_at>=? AND credited_at<=?',
                                 (user['id'], t-3*DAY, t)).fetchone()['n']
        counts[boss] = timed + opening
    zombie = boss_scope.Connection(c, 'zombie')
    ready = [row for row in spawn_pool(zombie) if channel_owner(row) is None and row['killed_at']+2280 <= t]
    mushroom = boss_scope.Connection(c, 'mushroom')
    primary = mushroom.execute('SELECT killed_at FROM channels WHERE claimant_id=?', (user['id'],)).fetchone()
    near = bool(primary and primary['killed_at'] is not None and primary['killed_at']+2280 <= t+300 and primary['killed_at']+3780 > t)
    timed_held = mushroom.execute('SELECT killed_at FROM channels WHERE claimant_id=? OR parallel_id=?', (user['id'], user['id'])).fetchall()
    mushroom_due = any(row['killed_at'] is not None and row['killed_at']+2280 <= t+300 and row['killed_at']+3780 > t for row in timed_held)
    zombie_held = zombie.execute('SELECT id FROM channels WHERE claimant_id=? OR parallel_id=? OR explorer_id=? OR explorer2_id=? OR explorer3_id=?',
                                  (user['id'],)*5).fetchone()
    pause_reason = ('zombie_held' if zombie_held else 'zombie_travel' if relay.reservation(zombie, user['id']) else
                    'zombie_cooldown' if claim_cooldown(zombie, user) else 'mushroom_due' if mushroom_due else '')
    goal = counts['mushroom']//5
    owed = max(0, goal-counts['zombie'])
    pressure = len(ready) >= 3
    restricted = pressure and counts['mushroom'] >= 10 and counts['zombie'] == 0
    blocked = c.boss == 'mushroom' and restricted
    return {'mushroom_kills': counts['mushroom'], 'zombie_kills': counts['zombie'],
            'target': goal, 'remaining': owed, 'backlog': len(ready), 'primary_soon': near,
            'pause_reason': pause_reason,
            'remind': pressure and owed > 0 and not pause_reason, 'parallel_blocked': blocked,
            'mushroom_parallel_blocked': restricted,
            'parallel_reason': COOPERATION_BLOCK_REASON if restricted else '',
            'opportunities': [{'id': row['id'], 'latest_at': row['killed_at']+3600} for row in ready[:3]]}


def zombie_hub(c, user):
    if c.boss != 'mushroom' or not can_access_boss(user, 'zombie'):
        return None
    return member_view(boss_scope.Connection(c,'zombie'),user,hub_only=True)


@view_cache.scope
def member_view(c, user, hub_only=False):
    if c.boss in boss_scope.FIXED_CLOCK_BOSSES:
        return dorr.view(c, sys.modules[__name__], user)
    uid = user['id']
    def private_detail(row):
        value = {**channel_detail(row, c.boss), 'can_edit_kill': can_edit_kill(row, user), 'sos_from_name': sos_origin(c, row)}
        value.pop('claimant_id'); value.pop('last_killer_id'); value.pop('explorer_id')
        return value
    unknown = [r['id'] for r in c.execute(f'''SELECT id FROM channels WHERE killed_at IS NULL
        AND claimant_id IS NULL AND {NO_EXPLORER_SQL} AND blocked_until <= ? ORDER BY id''', (now(),))]
    own = [private_detail(r) for r in c.execute('SELECT * FROM channels WHERE claimant_id=? ORDER BY id', (uid,))]
    concurrent = [private_detail(r) for r in c.execute('SELECT * FROM channels WHERE parallel_id=? ORDER BY id', (uid,))]
    explorations = user_explorations(c, uid)
    last = c.execute('SELECT * FROM channels WHERE last_killer_id=? ORDER BY killed_at DESC,id ASC LIMIT 1', (uid,)).fetchone()
    pool = spawn_pool(c)
    recommended = recommendations(c, uid, pool, 4 if has_plus(user) else 2)
    rec, extra = recommended[:2]
    members = list(c.execute('SELECT id,nickname FROM users WHERE deleted_at IS NULL AND pending_approval=0')) if hub_only else member_presence(c, user)
    nicknames = {member['id']: member['nickname'] for member in members}
    channels = []
    for row in c.execute('SELECT * FROM channels ORDER BY id'):
        detail = {**channel_detail(row, c.boss), 'can_edit_kill': can_edit_kill(row, user)}
        holder = row['claimant_id'] or row['parallel_id']
        detail['claimant_id'] = uid if holder == uid else -1 if holder else None
        owner = exploration_owner(row)
        detail['explorer_id'] = uid if owner == uid else -1 if owner else None
        detail['owner_nickname'] = nicknames.get(holder or owner)
        detail.pop('last_killer_id')
        if holder != uid:
            detail['claimed_at'] = None
            detail['sos_at'] = None
        if owner != uid:
            detail['exploration_claimed_at'] = None
        channels.append(detail)
    if hub_only:
        return {'me': public_user(user), 'channels': channels, 'own': own, 'concurrent': concurrent,
                'explorations': [private_detail(row) for row in explorations],
                'relays': relay.view(c, sys.modules[__name__], user),
                'reserved_slots': list(relay.reserved_slots(c, uid)),
                'claim_blocked_until': claim_cooldown(c, user),
                'last_kill': private_detail(last) if last else None,
                'last_kill_claimable': bool(last and channel_owner(last) is None
                    and last['blocked_until'] <= now() and last['killed_at'] + boss_scope.TIMING[c.boss][1] > now()),
                'spawn_pool': [{'id': row['id'], 'earliest_at': row['killed_at'] + boss_scope.TIMING[c.boss][0],
                    'latest_at': row['killed_at'] + boss_scope.TIMING[c.boss][1]} for row in pool] if has_plus(user) else [],
                'recommendations': [{'channel': private_detail(row), 'is_own': row['last_killer_id'] == uid}
                    for row in recommended if row]}
    # Show the channel before accepting; request tokens still protect stale requests.
    sos = [{'channel_id': r['id'], 'requester_name': r['requester_name'], 'request_id': r['sos_token'], 'killed_at': r['killed_at'],
            'earliest_at': r['killed_at'] + boss_scope.TIMING[c.boss][0] if r['killed_at'] else None,
            'latest_at': r['killed_at'] + boss_scope.TIMING[c.boss][1] if r['killed_at'] else None}
           for r in c.execute('''SELECT ch.id,ch.sos_token,ch.killed_at,
               (SELECT nickname FROM users WHERE users.id=ch.claimant_id) AS requester_name FROM channels ch
               WHERE ch.sos_at IS NOT NULL AND ch.claimant_id != ?
               AND ch.blocked_until <= ? ORDER BY ch.sos_at''', (uid, now()))]
    today = beijing_day_start()
    rescues = boss_scope.totals(c).execute("SELECT count(*) AS n FROM logs WHERE actor_id=? AND action='accept_sos' AND at>=?", (uid, today)).fetchone()['n']
    notices = [dict(r) for r in c.execute('''SELECT id,channel_id,kind,created_at FROM notifications
        WHERE user_id=? AND read_at IS NULL ORDER BY id DESC LIMIT 10''', (uid,))]
    weekly = weekly_rankings(c)
    return {'boss': c.boss, 'boss_name': boss_scope.BOSSES[c.boss], 'me': public_user(user), 'members': members,
            'cooperation': cooperation_status(c, user),
            'zombie_hub': zombie_hub(c,user), 'other_boss_claims': other_boss_claims(c, user), 'cross_support': cross_support(c, user),
            'concurrent': concurrent, 'reserved_slots': list(relay.reserved_slots(c, uid)),
            'relays': relay.view(c, sys.modules[__name__], user),
            'plus_progress': plus_progress(c, uid) if PLUS_AUTO_AWARD_ENABLED and user['role'] in ('member', 'admin') and not has_plus(user) and not user['plus_earned_at'] else None,
            'channels': channels, 'claim_blocked_until': claim_cooldown(c, user), 'release_count': 0,
            'next_release_cooldown_minutes': voluntary_release_minutes(c, user), 'unknown': unknown, 'own': own,
            'spawn_pool': [{'id': row['id'], 'earliest_at': row['killed_at'] + boss_scope.TIMING[c.boss][0],
                            'latest_at': row['killed_at'] + boss_scope.TIMING[c.boss][1]} for row in pool] if has_plus(user) else [],
            'exploration': private_detail(explorations[0]) if explorations else None,
            'explorations': [private_detail(row) for row in explorations],
            'weekly_rankings': weekly, 'exploration_board': exploration_board(c, uid), 'sos_board': weekly_sos_board(c, uid), 'retreat_board': retreat_board(c, uid),
            'robbery_ranking': weekly['robbery'],
            'last_kill': private_detail(last) if last else None,
            'last_kill_claimable': bool(last and channel_owner(last) is None
                                        and last['blocked_until'] <= now() and last['killed_at'] + boss_scope.TIMING[c.boss][1] > now()),
            'recommendation': private_detail(rec) if rec else None,
            'recommendations': [{'channel': private_detail(row), 'is_own': row['last_killer_id'] == uid}
                                for row in recommended if row],
            'recommendation_is_own': bool(rec and rec['last_killer_id'] == uid),
            'extra_recommendation': private_detail(extra) if extra else None,
            'extra_recommendation_is_own': bool(extra and extra['last_killer_id'] == uid),
            'sos': sos, 'rescues_today': rescues, 'notices': notices, 'server_time': now()}


def readable_log_detail(c, detail):
    """Render legacy log metadata without changing the audit records themselves."""
    def timestamp(match):
        value = int(match[2])
        return match[1] + datetime.fromtimestamp(value, timezone(timedelta(hours=8))).strftime('%Y/%m/%d %H:%M:%S') + '（北京时间）'
    text = re.sub(r'((?:原击杀时间|击杀时间|原时间|新时间|时间|到期|截止|时间录入|时间校准)[:：]\s*|原基准\s+|新基准\s+|→\s*)([0-9]{10})(?![0-9])', timestamp, detail or '')
    def nickname(match):
        row = c.execute('SELECT nickname FROM users WHERE id=?', (int(match[2]),)).fetchone()
        return match[1] + (row['nickname'] if row else '已删除成员 #' + match[2])
    return re.sub(r'((?:原认领人[:：]|认领用户\s+|分配给用户\s+|来自用户\s+|被管理员\s+|分配给\s+|用户\s+))([0-9]+)(?![0-9])', nickname, text)


def admin_view(c, user):
    result = member_view(c, user)
    if c.boss not in boss_scope.FIXED_CLOCK_BOSSES and user['role'] == 'superadmin':
        result['spawn_pool'] = [channel_detail(row, c.boss) for row in spawn_pool(c)]
        owner_names = {channel['id']: channel['owner_nickname'] for channel in result['channels']}
        result['channels'] = [{**channel_detail(row, c.boss), 'can_edit_kill': can_edit_kill(row, user), 'owner_nickname': owner_names[row['id']]}
                              for row in c.execute('SELECT * FROM channels ORDER BY id')]
    result['assignable_users'] = [
        {k: r[k] for k in ('id', 'nickname', 'role', 'access_until', 'banned_until', 'frozen', 'boss_access')}
        for r in c.execute("SELECT * FROM users WHERE deleted_at IS NULL AND pending_approval=0 AND role IN ('member','admin') ORDER BY id")
        if can_access_boss(r, c.boss)]
    if user['role'] == 'superadmin':
        result['login_records'] = [dict(row) for row in c.execute('''SELECT l.at,l.ip,l.device,l.takeover,l.risk,u.nickname
            FROM login_records l JOIN users u ON u.id=l.user_id
            WHERE l.at>=? ORDER BY l.id DESC LIMIT 100''', (now()-30*DAY,))]
        today = beijing_day_start()
        combined_kills = {row['actor_id']: row['kills'] for row in c.execute('''
            SELECT actor_id,count(*) AS kills FROM logs WHERE action='kill' AND actor_id IS NOT NULL
            AND at>=? AND at<? GROUP BY actor_id''', (today, today + DAY))}
        result['today_kill_ranking'] = sorted(
            [{**member, 'kills': combined_kills.get(member['id'], 0)} for member in result['members']],
            key=lambda member: (-member['kills'], member['nickname'], member['id']))
        cooldowns = {}
        for selected_boss in boss_scope.BOSSES:
            scoped = boss_scope.Connection(c, selected_boss)
            for row in scoped.execute('SELECT user_id,ends_at FROM release_cooldowns WHERE ends_at>?', (now(),)):
                cooldowns.setdefault(row['user_id'], {})[selected_boss] = row['ends_at']
        result['users'] = [{**public_user(r), 'claim_cooldowns': cooldowns.get(r['id'], {})}
                           for r in c.execute('SELECT * FROM users WHERE deleted_at IS NULL ORDER BY id')]
    return result


class ApiError(Exception):
    def __init__(self, status, message, **payload):
        self.status, self.message = status, message
        self.payload = payload


def require(condition, message='无权操作', status=403):
    if not condition:
        raise ApiError(status, message)


def rate_limit(kind, address, limit, window):
    current = now()
    key = (kind, address)
    with RATE_LOCK:
        recent = [t for t in RATE_BUCKETS.get(key, []) if t > current - window]
        require(len(recent) < limit, '操作太频繁，请稍后重试', 429)
        recent.append(current)
        RATE_BUCKETS[key] = recent
        if len(RATE_BUCKETS) > 10000:
            for old in [k for k, values in RATE_BUCKETS.items() if values[-1] < current - 3600]:
                del RATE_BUCKETS[old]


def valid_new_password(value):
    require(isinstance(value, str) and 9 <= len(value) <= 128, '密码需为 9～128 位', 400)
    return value


def claim_timeout(boss):
    # Long spawn windows must not lose their holder at the mushroom 90-minute limit.
    return max(CLAIM_TIMEOUT, boss_scope.TIMING[boss][1] + UNKNOWN_AFTER_GUARANTEE)


def sweep(c, admin_releasing_channel=None):
    if c.boss in boss_scope.FIXED_CLOCK_BOSSES:
        return dorr.sweep(c, sys.modules[__name__])
    relay.lock(c, sys.modules[__name__])
    t = now()
    expired = c.execute('''SELECT id,killed_at,claimant_id,parallel_id FROM channels WHERE killed_at IS NOT NULL
        AND killed_at <= ? ORDER BY id''' + (' FOR UPDATE' if MYSQL else ''),
        (t - boss_scope.TIMING[c.boss][1] - UNKNOWN_AFTER_GUARANTEE,)).fetchall()
    for ch in expired:
        updated = c.execute('''UPDATE channels SET killed_at=NULL,last_killer_id=NULL,exploded_at=?,
            claimant_id=NULL,claimed_at=NULL,parallel_id=NULL,parallel_claimed_at=NULL,sos_at=NULL,sos_token=NULL WHERE id=? AND killed_at=?''',
            (t, ch['id'], ch['killed_at']))
        if updated.rowcount:
            log(c, 'reset_unknown', ch['id'], None,
                f'保底超时后自动转开荒频道，原击杀时间:{ch["killed_at"]} 原认领人:{ch["claimant_id"]}')
            if ch['claimant_id'] or ch['parallel_id']:
                c.execute('''INSERT INTO notifications(user_id,channel_id,kind,created_at)
                    VALUES(?,?,?,?)''', (ch['claimant_id'] or ch['parallel_id'], ch['id'], 'guarantee_expired', t))
    query = 'SELECT * FROM channels WHERE claimant_id IS NOT NULL AND claimed_at <= ?'
    stale = [row for row in c.execute(query + (' FOR UPDATE' if MYSQL else ''),
             (t - claim_timeout(c.boss),)).fetchall() if row['id'] != admin_releasing_channel]
    for ch in stale:
        uid = ch['claimant_id']
        owner = c.execute('SELECT role FROM users WHERE id=?', (uid,)).fetchone()
        if owner and owner['role'] in ('member', 'admin'):
            set_boss_cooldown(c, uid, t + CLAIM_PENALTY)
        c.execute('UPDATE channels SET claimant_id=NULL,claimed_at=NULL,sos_at=NULL,sos_token=NULL WHERE id=? AND claimant_id=?', (ch['id'], uid))
        log(c, 'timeout_release', ch['id'], uid, '管理员超时自动释放，无惩罚' if owner and owner['role'] not in ('member', 'admin') else '')
    stale_explorations = []
    for owner_col, time_col in (*EXPLORATION_SLOTS, ('parallel_id', 'parallel_claimed_at')):
        rows = c.execute(f'''SELECT id,{owner_col} AS explorer_id FROM channels
            WHERE {owner_col} IS NOT NULL AND {time_col} <= ?'''
            + (' FOR UPDATE' if MYSQL else ''), (t - claim_timeout(c.boss),)).fetchall()
        stale_explorations.extend((row, owner_col, time_col) for row in rows if row['id'] != admin_releasing_channel)
    for ch, owner_col, time_col in stale_explorations:
        uid = ch['explorer_id']
        owner = c.execute('SELECT role FROM users WHERE id=?', (uid,)).fetchone()
        if owner and owner['role'] in ('member', 'admin'):
            set_boss_cooldown(c, uid, t + CLAIM_PENALTY)
        c.execute(f'UPDATE channels SET {owner_col}=NULL,{time_col}=NULL WHERE id=? AND {owner_col}=?', (ch['id'], uid))
        log(c, 'timeout_release', ch['id'], uid,
            '管理员开荒超时自动释放，无惩罚' if owner and owner['role'] not in ('member', 'admin') else '开荒认领超时')
    relayed = relay.sweep(c, sys.modules[__name__])
    return bool(stale or stale_explorations or expired or relayed)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def client_ip(self):
        peer = ipaddress.ip_address(self.client_address[0])
        # Nginx overwrites X-Real-IP; only the local reverse proxy is trusted.
        raw = self.headers.get('X-Real-IP', '').strip() if PRODUCTION and peer.is_loopback else ''
        try:
            address = ipaddress.ip_address(raw) if raw else peer
        except ValueError:
            raise ApiError(400, '客户端地址无效')
        return str(address.ipv4_mapped or address) if isinstance(address, ipaddress.IPv6Address) else str(address)

    def send(self, status, data, cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        if cookie is not None:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get('Content-Length', '0'))
        require(length <= 16384, '请求过大', 413)
        try:
            return json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            raise ApiError(400, 'JSON 格式错误')

    def user(self, c):
        self.idle_expired = False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return None
        token = cookie.get(COOKIE_NAME)
        if not token:
            return None
        row = c.execute('''SELECT u.*, s.last_action_at AS session_last_action_at FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>? AND s.version=u.session_version''', (token_hash(token.value), now())).fetchone()
        if row and row['role'] in ('member', 'admin') and row['session_last_action_at'] <= now() - IDLE_TIMEOUT:
            self.idle_expired = True
            return None
        return row

    def touch_activity(self, c, user):
        c.execute('UPDATE sessions SET last_action_at=? WHERE user_id=? AND version=? AND last_action_at>?',
                  (now(), user['id'], user['session_version'], now() - IDLE_TIMEOUT))

    def select_realm(self, c, user):
        requested = self.headers.get('X-Realm-Id') or parse_qs(urlparse(self.path).query).get('realm', [None])[0]
        require(requested is None or requested in realm_scope.REALMS, '区服无效', 400)
        own = user['realm_id']
        require(own in realm_scope.REALMS, '账号区服无效，请联系超级管理员', 403)
        if requested and requested != own and user['role'] != 'superadmin':
            raise ApiError(403, '只能访问账号所属区服', code='realm_forbidden', realm_id=own)
        c.realm = requested if requested and user['role'] == 'superadmin' else own

    def execute(self, method):
        try:
            path = urlparse(self.path).path
            if PRODUCTION and method == 'POST':
                origin = self.headers.get('Origin')
                require(APP_BASE_URL.startswith('https://'), '生产环境需要配置 HTTPS 地址', 500)
                require(not origin or origin == APP_BASE_URL.rstrip('/'), '请求来源不匹配', 403)
            if path == '/api/sms/request':
                raise ApiError(404, '接口未开放')
            if not path.startswith('/api/'):
                if method != 'GET':
                    raise ApiError(405, 'Method not allowed')
                name = 'index.html' if path == '/' else path.lstrip('/')
                require(name in ('index.html', 'app.js', 'dorr.js', 'style.css'), 'Not found', 404)
                data = (STATIC_DIR / name).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', {'index.html': 'text/html', 'app.js': 'text/javascript', 'dorr.js': 'text/javascript', 'style.css': 'text/css'}[name] + '; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
                return
            boss = self.headers.get('X-Boss-Id') or parse_qs(urlparse(self.path).query).get('boss', ['mushroom'])[0]
            require(boss in boss_scope.BOSSES, 'Boss 类型无效', 400)
            with db(boss) as c:
                if method == 'GET' and path == '/api/health':
                    c.execute('SELECT 1').fetchone()
                    self.send(200, {'ok': True}); return
                if method == 'GET' and path == '/api/events':
                    user = self.user(c)
                    require(active(user), IDLE_MESSAGE if self.idle_expired else '请登录', 401)
                    self.select_realm(c, user)
                    require(can_access_boss(user, boss), '未开通该 Boss 权限', 403)
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    start, seen = time.time(), -1
                    while time.time() - start < 25 and not service_runtime.STOPPING.is_set():
                        current_user = self.user(c)
                        if not active(current_user) or not can_access_boss(current_user, boss):
                            reason = IDLE_MESSAGE if self.idle_expired else '该账号已在另一台设备登录或会话失效，你已退出'
                            self.wfile.write(('event: session-revoked\ndata: ' + json.dumps({'reason': reason}, ensure_ascii=False) + '\n\n').encode())
                            self.wfile.flush()
                            return
                        if REALM_REVISIONS[c.realm] != seen:
                            seen = REALM_REVISIONS[c.realm]
                            self.wfile.write(f'event: update\ndata: {seen}\n\n'.encode())
                            self.wfile.flush()
                        service_runtime.STOPPING.wait(1)
                    return
                if method == 'POST' and path == '/api/register':
                    address = self.client_ip()
                    data = self.body()
                    realm = data.get('realm_id', realm_scope.DEFAULT)
                    require(isinstance(realm, str) and realm in realm_scope.REALMS, '请选择有效区服', 400)
                    username = str(data.get('username', '')).strip()
                    nickname = str(data.get('nickname', '')).strip()
                    require(3 <= len(username) <= 30 and username.isascii() and
                            all(ch.isalnum() or ch in '_-' for ch in username), '账号需为 3～30 位英文字母、数字、下划线或短横线', 400)
                    require(1 <= len(nickname) <= 30, '昵称需为 1～30 个字符', 400)
                    password = valid_new_password(data.get('password'))
                    c.execute('BEGIN IMMEDIATE')
                    require(not c.execute('SELECT 1 FROM users WHERE registration_ip=?', (address,)).fetchone(),
                            '该 IP 已注册普通账号；共用网络请联系管理员创建账号', 409)
                    require(not c.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone(),
                            '用户名已存在', 409)
                    recovery = secrets.token_urlsafe(32)
                    try:
                        cursor = c.execute('''INSERT INTO users(username,nickname,password_hash,role,
                            access_until,created_at,registration_ip,recovery_hash,pending_approval,boss_access,realm_id)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                            (username, nickname, hash_password(password), 'member', 0,
                             now(), address, hash_password(recovery), 1, 0, realm))
                    except Exception as exc:
                        if isinstance(exc, sqlite3.IntegrityError) or (MYSQL and exc.__class__.__name__ == 'IntegrityError'):
                            raise ApiError(409, '用户名或注册 IP 已被使用')
                        raise
                    user_id = cursor.lastrowid
                    c.realm = realm
                    log(c, 'user_register', None, user_id, '自主注册，等待超级管理员审批')
                    c.commit(); changed(c.realm)
                    self.send(200, {'ok': True, 'pending_approval': True, 'recovery_code': recovery, 'realm_id': realm})
                    return
                if method == 'POST' and path == '/api/password/reset':
                    rate_limit('password_reset', self.client_ip(), 10, 3600)
                    data = self.body()
                    username = str(data.get('username', '')).strip()
                    code = str(data.get('recovery_code', ''))
                    require(1 <= len(username) <= 30 and 1 <= len(code) <= 100, '账号或密钥错误', 401)
                    password = valid_new_password(data.get('new_password'))
                    c.execute('BEGIN IMMEDIATE')
                    u = c.execute('SELECT * FROM users WHERE username=?' + (' FOR UPDATE' if MYSQL else ''),
                                  (username,)).fetchone()
                    require(u and u['deleted_at'] is None and u['recovery_hash'] and
                            check_password(code, u['recovery_hash']), '账号或密钥错误', 401)
                    c.realm = u['realm_id']
                    replacement = secrets.token_urlsafe(32)
                    c.execute('''UPDATE users SET password_hash=?,recovery_hash=?,session_version=session_version+1
                        WHERE id=?''', (hash_password(password), hash_password(replacement), u['id']))
                    c.execute('DELETE FROM sessions WHERE user_id=?', (u['id'],))
                    log(c, 'user_password_reset', None, u['id'], '使用恢复码重置密码')
                    c.commit(); changed(c.realm)
                    secure = '; Secure' if PRODUCTION else ''
                    self.send(200, {'ok': True, 'recovery_code': replacement},
                              f'{COOKIE_NAME}=; HttpOnly; SameSite=Strict{secure}; Path={COOKIE_PATH}; Max-Age=0')
                    return
                if method == 'POST' and path == '/api/login':
                    rate_limit('login', self.client_ip(), 100, 900)
                    data = self.body()
                    if not MYSQL:
                        c.execute('BEGIN IMMEDIATE')
                    u = c.execute('SELECT * FROM users WHERE username=?' + (' FOR UPDATE' if MYSQL else ''),
                                  (str(data.get('username', '')),)).fetchone()
                    require(u and check_password(str(data.get('password', '')), u['password_hash']), '账号或密码错误', 401)
                    require(not u['pending_approval'], '注册申请待超级管理员审批，请稍后再登录', 403)
                    require(active(u), '账号已停用或会员已到期', 403)
                    c.realm = u['realm_id']
                    timestamp = now()
                    address = self.client_ip()
                    device = str(self.headers.get('User-Agent', '未知设备'))[:300]
                    previous = c.execute('SELECT * FROM login_records WHERE user_id=? ORDER BY id DESC LIMIT 1', (u['id'],)).fetchone()
                    takeover = bool(c.execute('SELECT 1 FROM sessions WHERE user_id=? AND expires_at>?', (u['id'], timestamp)).fetchone())
                    risks = []
                    if previous and timestamp-previous['at'] <= 600:
                        if previous['ip'] != address:
                            risks.append('10分钟内切换IP')
                        if previous['device'] != device:
                            risks.append('10分钟内切换设备')
                    recent = c.execute('SELECT count(*) AS n FROM login_records WHERE user_id=? AND at>=? AND takeover=1',
                                       (u['id'], timestamp-3600)).fetchone()['n']
                    if takeover and recent >= 2:
                        risks.append('1小时内至少3次顶号')
                    c.execute('INSERT INTO login_records(user_id,at,ip,device,takeover,risk) VALUES(?,?,?,?,?,?)',
                              (u['id'], timestamp, address, device, int(takeover), '；'.join(risks)))
                    c.execute('DELETE FROM login_records WHERE at<?', (timestamp-30*DAY,))
                    recovery = secrets.token_urlsafe(32) if not u['recovery_hash'] else None
                    if recovery:
                        c.execute('UPDATE users SET recovery_hash=? WHERE id=?', (hash_password(recovery), u['id']))
                    token = secrets.token_urlsafe(32)
                    version = u['session_version'] + 1
                    c.execute('UPDATE users SET session_version=? WHERE id=?', (version, u['id']))
                    c.execute('DELETE FROM sessions WHERE user_id=?', (u['id'],))
                    c.execute('''INSERT INTO sessions(token_hash,user_id,version,expires_at,last_seen_at,last_action_at)
                        VALUES(?,?,?,?,?,?)''', (token_hash(token), u['id'], version, now() + 7 * DAY, now(), now()))
                    maybe_award_plus(c, u['id'])
                    c.commit(); changed(c.realm)
                    secure = '; Secure' if PRODUCTION else ''
                    response = {'ok': True, 'allowed_bosses': allowed_bosses(u), 'realm_id': u['realm_id']}
                    if recovery:
                        response['recovery_code'] = recovery
                    self.send(200, response, f'{COOKIE_NAME}={token}; HttpOnly; SameSite=Strict{secure}; Path={COOKIE_PATH}; Max-Age={7 * DAY}')
                    return
                user = self.user(c)
                require(active(user), IDLE_MESSAGE if self.idle_expired else '会话已失效，请重新登录', 401)
                self.select_realm(c, user)
                if not can_access_boss(user, boss) and path not in ('/api/logout', '/api/password/change', '/api/password/recovery'):
                    self.send(403, {'error': '未开通该 Boss 权限，请联系超级管理员',
                                    'code': 'boss_forbidden', 'allowed_bosses': allowed_bosses(user)})
                    return
                if method == 'POST' and path == '/api/activity':
                    self.body()
                    self.touch_activity(c, user); c.commit()
                    self.send(200, {'ok': True}); return
                if method == 'POST' and path == '/api/logout':
                    cookie = SimpleCookie(); cookie.load(self.headers.get('Cookie', ''))
                    if cookie.get(COOKIE_NAME):
                        c.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(cookie[COOKIE_NAME].value),))
                    secure = '; Secure' if PRODUCTION else ''
                    self.send(200, {'ok': True}, f'{COOKIE_NAME}=; HttpOnly; SameSite=Strict{secure}; Path={COOKIE_PATH}; Max-Age=0'); return
                if method == 'POST' and path == '/api/password/change':
                    data = self.body()
                    password = valid_new_password(data.get('new_password'))
                    current = str(data.get('current_password', ''))
                    c.execute('BEGIN IMMEDIATE')
                    user = c.execute('SELECT * FROM users WHERE id=?' + (' FOR UPDATE' if MYSQL else ''),
                                     (user['id'],)).fetchone()
                    require(len(current) <= 128 and check_password(current, user['password_hash']),
                            '当前密码错误', 401)
                    replacement = secrets.token_urlsafe(32)
                    c.execute('''UPDATE users SET password_hash=?,recovery_hash=?,session_version=session_version+1
                        WHERE id=?''', (hash_password(password), hash_password(replacement), user['id']))
                    c.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
                    log(c, 'user_password_change', None, user['id'], '本人修改密码')
                    c.commit(); changed(c.realm)
                    secure = '; Secure' if PRODUCTION else ''
                    self.send(200, {'ok': True, 'recovery_code': replacement},
                              f'{COOKIE_NAME}=; HttpOnly; SameSite=Strict{secure}; Path={COOKIE_PATH}; Max-Age=0')
                    return
                if method == 'POST' and path == '/api/password/recovery':
                    rate_limit('recovery_issue', self.client_ip(), 10, 3600)
                    data = self.body()
                    current = str(data.get('current_password', ''))
                    c.execute('BEGIN IMMEDIATE')
                    user = c.execute('SELECT * FROM users WHERE id=?' + (' FOR UPDATE' if MYSQL else ''),
                                     (user['id'],)).fetchone()
                    require(len(current) <= 128 and check_password(current, user['password_hash']),
                            '当前密码错误', 401)
                    replacement = secrets.token_urlsafe(32)
                    c.execute('UPDATE users SET recovery_hash=? WHERE id=?',
                              (hash_password(replacement), user['id']))
                    log(c, 'user_recovery_issue', None, user['id'], '本人重新生成恢复码')
                    c.commit(); changed(c.realm)
                    self.send(200, {'ok': True, 'recovery_code': replacement})
                    return
                if method == 'GET' and path == '/api/state':
                    c.execute('BEGIN IMMEDIATE')
                    # Keep the same guard -> session lock order as business writes.
                    relay.lock(c, sys.modules[__name__])
                    if self.headers.get('X-Page-Visible') == '1':
                        c.execute('DELETE FROM page_presence WHERE user_id=?', (user['id'],))
                        c.execute('INSERT INTO page_presence(user_id,session_version,boss,seen_at) VALUES(?,?,?,?)',
                                  (user['id'],user['session_version'],boss,now()))
                    c.execute('UPDATE sessions SET last_seen_at=? WHERE user_id=? AND version=?',
                              (now(), user['id'], user['session_version']))
                    swept = sweep(c)
                    awarded = maybe_award_plus(c, user['id'])
                    if awarded:
                        user = c.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
                    c.commit()
                    if swept or awarded:
                        changed(c.realm)
                    view = admin_view(c, user) if user['role'] in ('superadmin', 'admin') else member_view(c, user)
                    view.update(realm_id=c.realm, realm_name=realm_scope.REALMS[c.realm],
                                realms=realm_scope.REALMS if user['role'] == 'superadmin' else {c.realm: realm_scope.REALMS[c.realm]})
                    self.send(200, view); return
                if method == 'GET' and path == '/api/exploration/suggestion':
                    excluded = parse_qs(urlparse(self.path).query).get('exclude', [''])[0].split(',')
                    rows = c.execute(f'''SELECT ch.*,
                        (SELECT max(l.at) FROM logs l WHERE l.channel_id=ch.id
                         AND l.action IN ('kill','report_robbed')) AS last_record
                        FROM channels ch WHERE killed_at IS NULL AND claimant_id IS NULL
                        AND {NO_EXPLORER_SQL} AND blocked_until<=?''', (now(),)).fetchall()
                    choices = [r for r in rows if str(r['id']) not in excluded] or rows
                    if choices:
                        oldest = min(r['last_record'] or 0 for r in choices)
                        selected = secrets.choice([r for r in choices if (r['last_record'] or 0) == oldest])
                    else:
                        selected = None
                    self.send(200, {'channel': channel_detail(selected, c.boss) if selected else None}); return
                if method == 'POST' and path == '/api/admin/payment-mark':
                    require(user['role'] == 'superadmin')
                    data = self.body()
                    from datetime import datetime
                    date = data.get('date')
                    try:
                        require(isinstance(date,str) and datetime.strptime(date,'%Y-%m-%d').strftime('%Y-%m-%d')==date,'日期无效',400)
                    except ValueError:
                        raise ApiError(400,'日期无效')
                    uid = data.get('user_id')
                    require(type(uid) is int and type(data.get('paid')) is bool and type(data.get('version')) is int,'参数无效',400)
                    c.execute('BEGIN IMMEDIATE')
                    require(c.execute('SELECT id FROM users WHERE id=?',(uid,)).fetchone() is not None,'用户不存在',404)
                    relay.lock(c, sys.modules[__name__])
                    last = c.execute('SELECT id,paid FROM payment_marks WHERE user_id=? AND payment_date=? ORDER BY id DESC LIMIT 1',(uid,date)).fetchone()
                    require((last['id'] if last else 0)==data['version'],'标记已被更新，请重新查询后操作',409)
                    c.execute('INSERT INTO payment_marks(user_id,payment_date,paid,actor_id,marked_at) VALUES(?,?,?,?,?)',(uid,date,int(data['paid']),user['id'],now()))
                    c.commit()
                    self.send(200,{'ok':True});return
                if method == 'GET' and path == '/api/admin/kill-stats':
                    require(user['role'] == 'superadmin')
                    from datetime import datetime, timezone, timedelta
                    date = parse_qs(urlparse(self.path).query).get('date', [''])[0]
                    try:
                        day = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=timezone(timedelta(hours=8)))
                        require(day.strftime('%Y-%m-%d') == date, '日期格式应为 YYYY-MM-DD', 400)
                    except ValueError:
                        raise ApiError(400, '请选择有效日期')
                    start = int(day.timestamp())
                    self.send(200, daily_kill_details(c, start, date)); return
                if method == 'GET' and path == '/api/logs':
                    require(user['role'] in ('superadmin', 'admin'))
                    q = parse_qs(urlparse(self.path).query)
                    channel = q.get('channel', [''])[0]; action = q.get('action', [''])[0]
                    clauses, args = [], []
                    if user['role'] == 'admin':
                        clauses.append("l.action NOT LIKE ? AND (l.channel_id IS NOT NULL OR l.action LIKE ?)")
                        args.extend(('user_%', 'team_%'))
                    if channel:
                        require(channel.isdigit() and 1 <= int(channel) <= 60, '频道无效', 400)
                        clauses.append('l.channel_id=?'); args.append(int(channel))
                    if action:
                        clauses.append('l.action=?'); args.append(action)
                    where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
                    rows = c.execute(f'''SELECT l.id,l.at,l.channel_id,l.action,l.detail,u.nickname AS actor
                        FROM logs l LEFT JOIN users u ON u.id=l.actor_id {where} ORDER BY l.id DESC LIMIT 300''', args)
                    self.send(200, {'logs': [{**dict(r), 'detail': readable_log_detail(c, r['detail']), 'boss': boss_scope.BOSSES[c.boss]} for r in rows]}); return
                require(method == 'POST', 'Method not allowed', 405)
                data = self.body()
                c.execute('BEGIN IMMEDIATE')
                channel_id = int(data.get('channel_id', 0) or 0)
                admin_releasing_channel = channel_id if path == '/api/release' and user['role'] == 'superadmin' else None
                sweep(c, admin_releasing_channel=admin_releasing_channel)
                # Refresh user after sweep, which may impose a timeout penalty.
                user = c.execute('SELECT * FROM users WHERE id=?' + (' FOR UPDATE' if MYSQL else ''),
                                 (user['id'],)).fetchone()
                uid = user['id']; admin = user['role'] == 'superadmin'
                require(active(user) and can_access_boss(user, boss), '账号或 Boss 权限已变化，请重新登录', 403)
                if boss in boss_scope.FIXED_CLOCK_BOSSES and path not in ('/api/admin/user', '/api/admin/create-user', '/api/admin/channels/blacklist', '/api/notice/read'):
                    result = dorr.handle(c, sys.modules[__name__], user, path, data)
                    self.touch_activity(c, user)
                    c.commit(); changed(c.realm); self.send(200, {'ok': True, **(result or {})}); return
                if path in ('/api/claim', '/api/claim-exploration', '/api/claim-parallel', '/api/accept-sos'):
                    require(not claim_cooldown(c, user), '主动释放后冻结 10 分钟，请等待倒计时结束', 403)
                ch = c.execute('SELECT * FROM channels WHERE id=?' + (' FOR UPDATE' if MYSQL else ''), (channel_id,)).fetchone() if channel_id else None
                if path.startswith('/api/relay/'):
                    relay.handle(c, sys.modules[__name__], path, user, ch, data)
                elif path == '/api/channel-note':
                    require(ch, '频道不存在', 404)
                    note = data.get('note')
                    require(isinstance(note, str) and len(note) <= 200 and
                            not any(ord(char) < 32 for char in note), '备注限 200 字，不能包含控制字符', 400)
                    require(data.get('expected_note') == ch['note'], '备注已被修改，请刷新后重试', 409)
                    c.execute('UPDATE channels SET note=? WHERE id=?', (note.strip(), channel_id))
                    log(c, 'channel_note', channel_id, uid, '更新频道对手提醒')
                elif path == '/api/notice/read':
                    notice_id = int(data.get('notice_id', 0) or 0)
                    updated = c.execute('UPDATE notifications SET read_at=? WHERE id=? AND user_id=? AND read_at IS NULL',
                                        (now(), notice_id, uid))
                    require(updated.rowcount == 1, '通知不存在', 404)
                elif path == '/api/claim-parallel':
                    cooperation = cooperation_status(c, user)
                    require(not cooperation or not cooperation['parallel_blocked'], COOPERATION_BLOCK_REASON, 409)
                    require(ready_unclaimed(ch, c.boss), '只有已进刷且无人认领的频道才能并发认领', 409)
                    require(data.get('expected_killed_at') == ch['killed_at'], '频道计时已变化，请刷新', 409)
                    require('parallel_id' not in relay.reserved_slots(c, uid), '并发名额已为接力预留', 409)
                    require(not c.execute('SELECT id FROM channels WHERE parallel_id=?', (uid,)).fetchone(), '最多额外认领 1 条并发频道', 409)
                    c.execute('UPDATE channels SET parallel_id=?,parallel_claimed_at=? WHERE id=?', (uid, now(), channel_id))
                    log(c, 'claim_parallel', channel_id, uid, '额外并发认领已进刷频道')
                elif path == '/api/discard-ready':
                    require(ready_unclaimed(ch, c.boss), '只能丢弃已进刷且仍无人认领的频道', 409)
                    require(data.get('expected_killed_at') == ch['killed_at'], '频道计时已变化，请刷新', 409)
                    c.execute('''UPDATE channels SET killed_at=NULL,last_killer_id=NULL,exploded_at=NULL,
                        credit_blocked_until=?,sos_at=NULL,sos_token=NULL WHERE id=?''',
                        (max(ch['credit_blocked_until'], ch['killed_at'] + boss_scope.TIMING[c.boss][1]), channel_id))
                    log(c, 'discard', channel_id, uid, '丢弃已进刷无人认领频道，转开荒')
                elif path == '/api/claim':
                    require(user['banned_until'] <= now(), '认领暂时受限')
                    reserved = relay.reserved_slots(c, uid)
                    require('claimant_id' not in reserved, '常规名额已为接力预留，请先到达或取消前往', 409)
                    held = c.execute('SELECT * FROM channels WHERE claimant_id=?', (uid,)).fetchone()
                    require(not held or (held['killed_at'] is None and ch and ch['killed_at'] is not None),
                            '请先处理当前认领')
                    require(ch and ch['blocked_until'] <= now() and channel_owner(ch) is None and
                            (ch['killed_at'] is None or ch['killed_at'] + boss_scope.TIMING[c.boss][1] > now()), '频道当前不可认领，请刷新', 409)
                    if held:
                        extras = user_explorations(c, uid)
                        require(len(extras) + len(reserved - {'parallel_id'}) < 3, '开荒名额已满（含接力预留）', 409)
                        used = {owner for row in extras for owner, _ in EXPLORATION_SLOTS if row[owner] == uid}
                        used |= reserved
                        owner_col, time_col = next((owner, claimed) for owner, claimed in EXPLORATION_SLOTS if owner not in used)
                        c.execute(f'''UPDATE channels SET {owner_col}=?,{time_col}=?,claimant_id=NULL,
                            claimed_at=NULL,sos_at=NULL,sos_token=NULL WHERE id=?''', (uid, held['claimed_at'], held['id']))
                        log(c, 'claim_exploration', held['id'], uid, '先开荒后认领计时频道，保留原开荒')
                    try:
                        result = c.execute(f'''UPDATE channels SET claimant_id=?,claimed_at=?,sos_at=NULL,sos_token=NULL
                            WHERE id=? AND blocked_until <= ? AND claimant_id IS NULL AND {NO_EXPLORER_SQL}''',
                            (uid, now(), channel_id, now()))
                    except Exception as exc:
                        if MYSQL and exc.__class__.__name__ == 'IntegrityError':
                            raise ApiError(409, '认领状态已变化，请刷新')
                        raise
                    require(result.rowcount == 1, '已被抢先认领', 409)
                    log(c, 'claim', channel_id, uid, '开荒' if ch['killed_at'] is None else '')
                elif path == '/api/claim-exploration':
                    require(user['banned_until'] <= now(), '认领暂时受限')
                    require(c.execute('SELECT 1 FROM channels WHERE claimant_id=?', (uid,)).fetchone(),
                            '请先认领一个常规频道')
                    held_explorations = user_explorations(c, uid)
                    reserved = relay.reserved_slots(c, uid) - {'claimant_id', 'parallel_id'}
                    require(len(held_explorations) + len(reserved) < 3, '最多同时认领 3 条开荒频道（含接力预留）')
                    require(ch and ch['blocked_until'] <= now() and ch['killed_at'] is None and ch['claimant_id'] is None
                            and exploration_owner(ch) is None and ch['parallel_id'] is None, '该频道暂不可开荒认领', 409)
                    used_slots = {owner for row in held_explorations for owner, _ in EXPLORATION_SLOTS
                                  if row[owner] == uid}
                    used_slots |= reserved
                    owner_col, time_col = next((owner, claimed) for owner, claimed in EXPLORATION_SLOTS
                                               if owner not in used_slots)
                    try:
                        result = c.execute(f'''UPDATE channels SET {owner_col}=?,{time_col}=?
                            WHERE id=? AND blocked_until <= ? AND killed_at IS NULL AND claimant_id IS NULL AND {NO_EXPLORER_SQL}''',
                            (uid, now(), channel_id, now()))
                    except Exception as exc:
                        if MYSQL and exc.__class__.__name__ == 'IntegrityError':
                            raise ApiError(409, '开荒认领已变化，请刷新')
                        raise
                    require(result.rowcount == 1, '该频道已被抢先认领', 409)
                    log(c, 'claim_exploration', channel_id, uid, '小号开荒')
                elif path == '/api/exploration/check':
                    require(ch and ch['killed_at'] is None and
                            (ch['claimant_id'] == uid or exploration_owner(ch) == uid), '只能检查自己的开荒频道')
                    log(c, 'exploration_check', channel_id, uid, '暂未发现，继续等待')
                elif path == '/api/kill-exploration':
                    require(ch and exploration_owner(ch) == uid and ch['killed_at'] is None,
                            '只能完成自己当前的开荒频道')
                    killed = int(data.get('killed_at', now()))
                    require(now() - 7 * DAY <= killed <= now() + 60, '时间超出允许范围', 400)
                    owner_col, time_col = next((owner, claimed) for owner, claimed in EXPLORATION_SLOTS
                                               if ch[owner] == uid)
                    result = c.execute(f'''UPDATE channels SET killed_at=?,last_killer_id=?,exploded_at=NULL,
                        {owner_col}=NULL,{time_col}=NULL
                        WHERE id=? AND {owner_col}=? AND killed_at IS NULL''',
                            (killed, uid, channel_id, uid))
                    require(result.rowcount == 1, '开荒认领已变化，请刷新', 409)
                    credit_exploration(c, ch, uid, killed)
                    log(c, 'kill', channel_id, uid, '开荒')
                elif path == '/api/kill':
                    require(ch and (admin or ch['claimant_id'] == uid or ch['parallel_id'] == uid)
                            and (exploration_owner(ch) is None or exploration_owner(ch) == uid),
                            '只能击杀当前认领频道')
                    t = now()
                    c.execute('''UPDATE channels SET killed_at=?,last_killer_id=?,exploded_at=NULL,claimant_id=NULL,
                        claimed_at=NULL,sos_at=NULL,sos_token=NULL,explorer_id=NULL,
                        exploration_claimed_at=NULL,explorer2_id=NULL,exploration2_claimed_at=NULL,
                        explorer3_id=NULL,exploration3_claimed_at=NULL,parallel_id=NULL,parallel_claimed_at=NULL WHERE id=?''', (t, uid, channel_id))
                    credit_exploration(c, ch, uid, t)
                    log(c, 'kill', channel_id, uid, '开荒' if ch['killed_at'] is None else '')
                    promote_parallel(c, ch['claimant_id'])
                elif path == '/api/edit-kill':
                    require(ch and exploration_owner(ch) is None, '开荒中的频道不能修改击杀时间')
                    expected = data.get('expected_killed_at')
                    recording = ch['killed_at'] is None
                    if not recording:
                        require(can_edit_kill(ch, user), '仅当前认领人可修改；无人认领时仅原击杀记录者或管理员可修改', 403)
                    if not admin:
                        if recording:
                            require('expected_killed_at' in data and expected is None,
                                    '开荒状态已变化，请刷新后重试', 409)
                            require(ch['claimant_id'] in (None, uid), '该频道已由其他成员认领', 409)
                            require(ch['blocked_until'] <= now() or ch['claimant_id'] == uid,
                                    '该频道当前不可录入击杀时间', 409)
                            require(ch['claimant_id'] == uid or user['banned_until'] <= now(),
                                    '暂不可录入开荒频道击杀')
                        elif expected is None:
                            require('expected_killed_at' not in data,
                                    '击杀时间已被更新，请刷新后重试', 409)
                            # Keep already-open older pages usable for the previous personal-last-kill action.
                            last_kill = c.execute('''SELECT id FROM channels WHERE last_killer_id=?
                                ORDER BY killed_at DESC,id ASC LIMIT 1''', (uid,)).fetchone()
                            require(ch['last_killer_id'] == uid and last_kill and last_kill['id'] == channel_id,
                                    '请刷新页面后再修改该频道时间')
                        else:
                            require(isinstance(expected, int) and not isinstance(expected, bool),
                                    '原击杀时间无效', 400)
                            require(expected == ch['killed_at'], '击杀时间已被更新，请刷新后重试', 409)
                    killed = int(data.get('killed_at', 0))
                    require(now() - 7 * DAY <= killed <= now() + 60, '时间超出允许范围', 400)
                    if recording and not admin:
                        c.execute('''UPDATE channels SET killed_at=?,last_killer_id=?,exploded_at=NULL,
                            claimant_id=NULL,claimed_at=NULL,sos_at=NULL,sos_token=NULL WHERE id=?''',
                            (killed, uid, channel_id))
                        credit_exploration(c, ch, uid, killed)
                        log(c, 'kill', channel_id, uid, '开荒')
                        promote_parallel(c, ch['claimant_id'])
                    else:
                        # Correcting a timestamp must not transfer credit away from the original killer.
                        c.execute('UPDATE channels SET killed_at=?,last_killer_id=?,exploded_at=NULL WHERE id=?',
                                  (killed, ch['last_killer_id'], channel_id))
                        log(c, 'edit_kill', channel_id, uid, f'原时间:{ch["killed_at"]} 新时间:{killed}')
                elif path == '/api/sos':
                    require(ch and ch['claimant_id'] == uid, '只能对当前认领求救')
                    require(ch['sos_at'] is None, '求救已发出，等待队友接管', 409)
                    c.execute('UPDATE channels SET sos_at=?,sos_token=? WHERE id=?',
                              (now(), secrets.token_urlsafe(32), channel_id))
                    log(c, 'sos', channel_id, uid)
                elif path == '/api/cancel-sos':
                    require(ch and ch['claimant_id'] == uid and ch['sos_at'] is not None,
                            '求救已取消或被接管，请刷新', 409)
                    c.execute('UPDATE channels SET sos_at=NULL,sos_token=NULL WHERE id=?', (channel_id,))
                    log(c, 'cancel_sos', channel_id, uid, '本人取消求救，保留认领')
                elif path == '/api/report-robbed':
                    require(ch and (ch['claimant_id'] == uid or ch['parallel_id'] == uid), '只能登记当前认领的频道被抢')
                    holder_col, time_col = ('parallel_id', 'parallel_claimed_at') if ch['parallel_id'] == uid else ('claimant_id', 'claimed_at')
                    claim_started = ch[time_col]
                    player_id = str(data.get('player_id', '')).strip()
                    require(1 <= len(player_id) <= 100 and not any(ord(char) < 32 for char in player_id),
                            '请输入完整角色名称（最多 100 字）', 400)
                    require(not c.execute('''SELECT 1 FROM robbery_reports WHERE channel_id=?
                        AND reporter_id=? AND claim_started_at=?''',
                        (channel_id, uid, claim_started)).fetchone(), '本次认领已登记被抢', 409)
                    killed_at = now()
                    c.execute('''INSERT INTO robbery_reports(channel_id,reporter_id,claim_started_at,player_id,reported_at)
                        VALUES(?,?,?,?,?)''', (channel_id, uid, claim_started, player_id, killed_at))
                    result = c.execute(f'''UPDATE channels SET killed_at=?,last_killer_id=NULL,exploded_at=NULL,
                        claimant_id=NULL,claimed_at=NULL,parallel_id=NULL,parallel_claimed_at=NULL,sos_at=NULL,sos_token=NULL
                        WHERE id=? AND {holder_col}=? AND {time_col}=?''',
                        (killed_at, channel_id, uid, claim_started))
                    require(result.rowcount == 1, '频道认领已变化，请刷新', 409)
                    log(c, 'report_robbed', channel_id, uid, f'被抢，击杀时间:{killed_at} 角色名称:{player_id}')
                elif path == '/api/accept-sos':
                    require(user['banned_until'] <= now(), '接管暂时受限')
                    require('claimant_id' not in relay.reserved_slots(c, uid), '常规名额已为接力预留，请先到达或取消前往', 409)
                    # The opaque request ID identifies one SOS without exposing its channel number.
                    request_id = str(data.get('request_id', ''))
                    require(len(request_id) == 43, '求救请求无效', 400)
                    target = c.execute('''SELECT * FROM channels
                        WHERE sos_at IS NOT NULL AND sos_token=? AND claimant_id!=?'''
                        + (' FOR UPDATE' if MYSQL else ''), (request_id, uid)).fetchone()
                    require(target and target['blocked_until'] <= now(), '求救频道暂不可接管，请刷新', 409)
                    prior = target['claimant_id']
                    held = c.execute('SELECT * FROM channels WHERE claimant_id=?' + (' FOR UPDATE' if MYSQL else ''),
                                     (uid,)).fetchone()
                    if held:
                        c.execute('''UPDATE channels SET claimant_id=NULL,claimed_at=NULL,sos_at=NULL,sos_token=NULL
                            WHERE id=? AND claimant_id=?''', (held['id'], uid))
                        log(c, 'release', held['id'], uid, '接受 SOS 自动释放，无认领冷却')
                    accepted_at = now()
                    result = c.execute('UPDATE channels SET claimant_id=?,claimed_at=?,sos_at=NULL,sos_token=NULL WHERE id=? AND claimant_id=? AND sos_token=?',
                              (uid, accepted_at, target['id'], prior, request_id))
                    require(result.rowcount == 1, '求救已被接受，请刷新', 409)
                    c.execute('INSERT INTO logs(at,action,channel_id,actor_id,detail) VALUES(?,?,?,?,?)',
                              (accepted_at, 'accept_sos', target['id'], uid, f'来自用户 {prior}'))
                    log(c, 'release', target['id'], prior, 'SOS 交接')
                    c.execute('''INSERT INTO notifications(user_id,channel_id,kind,created_at)
                        VALUES(?,?,?,?)''', (prior, target['id'], 'sos_accepted', now()))
                elif path == '/api/release':
                    require(ch and (admin or channel_owner(ch) == uid), '无权释放')
                    prior_claimant = channel_owner(ch)
                    c.execute('''UPDATE channels SET claimant_id=NULL,claimed_at=NULL,sos_at=NULL,
                        sos_token=NULL,explorer_id=NULL,exploration_claimed_at=NULL,
                        explorer2_id=NULL,exploration2_claimed_at=NULL,
                        explorer3_id=NULL,exploration3_claimed_at=NULL,parallel_id=NULL,parallel_claimed_at=NULL WHERE id=?''', (channel_id,))
                    if not admin:
                        minutes = voluntary_release_minutes(c, user)
                        if minutes:
                            set_boss_cooldown(c, uid, now() + minutes * 60)
                        log(c, 'release', channel_id, uid,
                            '主动释放，冻结认领 10 分钟' if minutes else '主动释放，无认领冷却')
                    else:
                        log(c, 'release', channel_id, uid, '管理员释放，无惩罚')
                        if prior_claimant and prior_claimant != uid:
                            c.execute('''INSERT INTO notifications(user_id,channel_id,kind,created_at)
                                VALUES(?,?,?,?)''', (prior_claimant, channel_id, 'admin_release', now()))
                elif path == '/api/discard':
                    require(ch and (admin or channel_owner(ch) == uid),
                            '只能丢弃自己认领的频道')
                    prior_claimant = channel_owner(ch)
                    c.execute('''UPDATE channels SET killed_at=NULL,last_killer_id=NULL,exploded_at=NULL,claimant_id=NULL,
                        claimed_at=NULL,sos_at=NULL,sos_token=NULL,explorer_id=NULL,
                        exploration_claimed_at=NULL,explorer2_id=NULL,exploration2_claimed_at=NULL,
                        explorer3_id=NULL,exploration3_claimed_at=NULL,parallel_id=NULL,parallel_claimed_at=NULL WHERE id=?''', (channel_id,))
                    if ch['killed_at'] is not None:
                        c.execute('UPDATE channels SET credit_blocked_until=? WHERE id=?',
                                  (max(ch['credit_blocked_until'], ch['killed_at'] + boss_scope.TIMING[c.boss][1]), channel_id))
                    log(c, 'discard', channel_id, uid, '丢弃频道，重置为开荒')
                    if admin and prior_claimant and prior_claimant != uid:
                        c.execute('''INSERT INTO notifications(user_id,channel_id,kind,created_at)
                            VALUES(?,?,?,?)''', (prior_claimant, channel_id, 'admin_release', now()))
                elif path == '/api/admin/assign':
                    require(user['role'] in ('superadmin', 'admin'))
                    target_id = int(data.get('user_id', 0))
                    require('claimant_id' not in relay.reserved_slots(c, target_id), '该成员常规名额已为接力预留', 409)
                    target = c.execute('SELECT * FROM users WHERE id=?', (target_id,)).fetchone()
                    require(ch and ch['blocked_until'] <= now() and exploration_owner(ch) is None and ch['parallel_id'] is None and target and target['role'] in ('member', 'admin')
                            and active(target) and can_access_boss(target, boss), '频道或成员不可用', 400)
                    require(ch['claimant_id'] != target_id, '频道已经分配给该成员', 409)
                    require(not c.execute('SELECT 1 FROM channels WHERE claimant_id=? AND id!=?', (target_id, channel_id)).fetchone(), '该成员已有认领', 409)
                    if ch['claimant_id'] and ch['claimant_id'] != target_id:
                        log(c, 'overridden', channel_id, ch['claimant_id'], f'被管理员 {uid} 分配给 {target_id}')
                        c.execute('''INSERT INTO notifications(user_id,channel_id,kind,created_at)
                            VALUES(?,?,?,?)''', (ch['claimant_id'], channel_id, 'admin_reassign', now()))
                    c.execute('UPDATE channels SET claimant_id=?,claimed_at=?,sos_at=NULL,sos_token=NULL WHERE id=?', (target_id, now(), channel_id))
                    log(c, 'assign', channel_id, uid, f'分配给用户 {target_id}')
                    if target['role'] in ('member', 'admin') and ch['claimant_id'] != target_id:
                        c.execute('''INSERT INTO notifications(user_id,channel_id,kind,created_at)
                            VALUES(?,?,?,?)''', (target_id, channel_id, 'admin_assign', now()))
                elif path == '/api/admin/channels/blacklist':
                    require(admin)
                    ids = data.get('channel_ids')
                    require(isinstance(ids, list) and 1 <= len(ids) <= 60 and
                            all(type(i) is int and 1 <= i <= 60 for i in ids) and len(set(ids)) == len(ids),
                            '请选择 1 至 60 个不重复的有效频道', 400)
                    operation = data.get('action')
                    require(operation in ('block', 'unblock'), '拉黑操作无效', 400)
                    minutes = data.get('minutes')
                    if operation == 'block':
                        require(type(minutes) is int and 1 <= minutes <= 43200, '拉黑时长须为 1 至 43200 分钟（30 天）', 400)
                    until = now() + minutes * 60 if operation == 'block' else 0
                    for selected_id in sorted(ids):
                        c.execute('UPDATE channels SET blocked_until=? WHERE id=?', (until, selected_id))
                        log(c, 'channel_blacklist' if operation == 'block' else 'channel_unblacklist',
                            selected_id, uid, f'临时拉黑 {minutes} 分钟' if operation == 'block' else '解除临时拉黑')
                elif path == '/api/admin/create-user':
                    require(user['role'] == 'superadmin', '仅超级管理员可以管理成员')
                    username = str(data.get('username', '')).strip()
                    nickname = str(data.get('nickname', '')).strip()
                    password = str(data.get('password', ''))
                    boss_access = data.get('boss_access')
                    require(type(boss_access) is int and 1 <= boss_access <= sum(BOSS_ACCESS.values()), '请勾选至少一个 Boss 权限', 400)
                    require(3 <= len(username) <= 30 and 1 <= len(nickname) <= 30,
                            '用户名或昵称不合要求', 400)
                    require(len(password) >= 9, '初始密码至少 9 位', 400)
                    try:
                        c.execute('INSERT INTO users(username,nickname,password_hash,access_until,created_at,boss_access,realm_id) VALUES(?,?,?,?,?,?,?)',
                                  (username, nickname, hash_password(password), LONG_TERM_UNTIL, now(), boss_access, c.realm))
                    except Exception as exc:
                        if isinstance(exc, sqlite3.IntegrityError) or (MYSQL and exc.__class__.__name__ == 'IntegrityError'):
                            raise ApiError(409, '用户名已存在')
                        raise
                    log(c, 'user_create', None, uid, f'创建成员 {username}')
                elif path == '/api/admin/user':
                    require(user['role'] == 'superadmin', '仅超级管理员可以管理成员')
                    target_id = int(data.get('user_id', 0))
                    target = c.execute('SELECT * FROM users WHERE id=?' + (' FOR UPDATE' if MYSQL else ''),
                                       (target_id,)).fetchone()
                    require(target and target['deleted_at'] is None and target['role'] != 'superadmin',
                            '无权管理该用户')
                    action = str(data.get('action', ''))
                    if action == 'role':
                        require(not target['pending_approval'] and data.get('role') in ('member', 'admin'), '角色无效', 400)
                        c.execute('UPDATE users SET role=? WHERE id=?', (data['role'], target_id))
                        log(c, 'user_role', None, uid, f"用户 {target_id}：{target['role']} → {data['role']}")
                        c.commit(); changed(c.realm); self.send(200, {'ok': True}); return
                    if action == 'approve':
                        require(target['role'] in ('member', 'admin') and target['pending_approval'],
                                '该成员无需审批', 409)
                        selected = data.get('boss_access')
                        require(type(selected) is int and 1 <= selected <= sum(BOSS_ACCESS.values()), '请勾选至少一个 Boss 权限', 400)
                        c.execute('UPDATE users SET pending_approval=0,access_until=?,boss_access=? WHERE id=?',
                                  (LONG_TERM_UNTIL, selected, target_id))
                        log(c, 'user_approve', None, uid, f'审批通过成员 {target_id}，Boss 权限 {selected}')
                        c.commit(); changed(c.realm); self.send(200, {'ok': True}); return
                    if action == 'boss_access':
                        selected = data.get('boss_access')
                        require(type(selected) is int and 1 <= selected <= sum(BOSS_ACCESS.values()), '请勾选至少一个 Boss 权限', 400)
                        removed = target['boss_access'] & ~selected
                        for selected_boss, flag in BOSS_ACCESS.items():
                            if not removed & flag:
                                continue
                            scoped = boss_scope.Connection(c, selected_boss)
                            if selected_boss in boss_scope.FIXED_CLOCK_BOSSES:
                                require(not dorr.team(scoped, target_id), f'该成员仍在{boss_scope.BOSSES[selected_boss]}队伍中，请先移出或解散队伍', 409)
                            held = scoped.execute('''SELECT 1 FROM channels WHERE claimant_id=? OR parallel_id=?
                                OR explorer_id=? OR explorer2_id=? OR explorer3_id=? LIMIT 1''', (target_id,) * 5).fetchone()
                            require(not held, f'该成员仍持有{boss_scope.BOSSES[selected_boss]}频道，请先处理频道后再关闭权限', 409)
                            traveling = scoped.execute('''SELECT 1 FROM exploration_relays WHERE receiver_id=?
                                AND state='travel' LIMIT 1''', (target_id,)).fetchone()
                            require(not traveling, '该成员正在前往接力，请先结束接力', 409)
                        c.execute('UPDATE users SET boss_access=? WHERE id=?', (selected, target_id))
                        log(c, 'user_boss_access', None, uid, f'用户 {target_id} 的 Boss 权限设置为 {selected}')
                    elif action == 'ban':
                        minutes = max(1, min(525600, int(data.get('minutes', 60))))
                        c.execute('UPDATE users SET banned_until=?,session_version=session_version+1 WHERE id=?', (now() + minutes * 60, target_id))
                    elif action == 'freeze':
                        c.execute('UPDATE users SET frozen=1,session_version=session_version+1 WHERE id=?', (target_id,))
                    elif action == 'unfreeze':
                        c.execute('UPDATE users SET frozen=0,banned_until=0 WHERE id=?', (target_id,))
                    elif action == 'clear_cooldown':
                        selected_boss = data.get('cooldown_boss')
                        require(selected_boss in boss_scope.BOSSES, '请选择要解除冷却的 Boss', 400)
                        scoped = boss_scope.Connection(c, selected_boss)
                        scoped.execute('DELETE FROM release_cooldowns WHERE user_id=?', (target_id,))
                        log(scoped, 'user_cooldown_clear', None, uid, f'解除用户 {target_id} 的认领冷却')
                        c.commit(); changed(c.realm); self.send(200, {'ok': True}); return
                    elif action == 'target':
                        value = int(data.get('value', 3)); require(0 <= value <= 100, '指标超出范围', 400)
                        c.execute('UPDATE users SET rescue_target=? WHERE id=?', (value, target_id))
                    elif action == 'access':
                        days = int(data.get('days', 1)); require(days in (1, 7), '仅支持日卡、周卡测试流程', 400)
                        c.execute('UPDATE users SET access_until=max(access_until,?)+? WHERE id=?', (now(), days * DAY, target_id))
                    elif action == 'plus':
                        days = int(data.get('days', 0))
                        require(days in (-1, 0, 1, 7, 30), 'Plus 支持关闭、长期或 1/7/30 天', 400)
                        ends_at = None if days == -1 else 0 if days == 0 else max(target['plus_until'] or now(), now()) + days * DAY
                        c.execute('UPDATE users SET plus_until=? WHERE id=?', (ends_at, target_id))
                    elif action == 'reset_password':
                        reset_key = secrets.token_urlsafe(32)
                        c.execute('''UPDATE users SET password_hash=?,recovery_hash=?,
                            session_version=session_version+1 WHERE id=?''',
                            (hash_password(secrets.token_urlsafe(32)), hash_password(reset_key), target_id))
                        c.execute('DELETE FROM sessions WHERE user_id=?', (target_id,))
                        log(c, 'user_password_admin_reset', None, uid, f'为用户 {target_id} 生成一次性密码重置密钥')
                        c.commit(); changed(c.realm)
                        self.send(200, {'ok': True, 'reset_key': reset_key})
                        return
                    elif action == 'delete':
                        retired_name = f'del_{target_id:x}_{secrets.token_hex(3)}'
                        c.execute('''UPDATE users SET username=?,phone=NULL,registration_ip=NULL,
                            password_hash=?,recovery_hash=NULL,access_until=0,frozen=1,deleted_at=?,
                            session_version=session_version+1 WHERE id=?''',
                            (retired_name, hash_password(secrets.token_urlsafe(32)), now(), target_id))
                        c.execute('DELETE FROM sessions WHERE user_id=?', (target_id,))
                        c.execute('DELETE FROM notifications WHERE user_id=?', (target_id,))
                        c.execute('DELETE FROM release_cooldowns WHERE user_id=?', (target_id,))
                    else:
                        raise ApiError(400, '未知操作')
                    if action in ('ban', 'freeze', 'delete'):
                        for selected_boss in boss_scope.BOSSES:
                            if selected_boss in boss_scope.FIXED_CLOCK_BOSSES:
                                dorr.cleanup_user(boss_scope.Connection(c, selected_boss), sys.modules[__name__], target_id, uid)
                                continue
                            scoped = boss_scope.Connection(c, selected_boss)
                            for held in scoped.execute('SELECT id FROM channels WHERE parallel_id=?', (target_id,)).fetchall():
                                scoped.execute('UPDATE channels SET parallel_id=NULL,parallel_claimed_at=NULL WHERE id=?', (held['id'],))
                                log(scoped, 'release', held['id'], uid, f'用户 {target_id} 的并发认领被停用或删除')
                            for held in scoped.execute('SELECT id FROM channels WHERE claimant_id=?', (target_id,)).fetchall():
                                scoped.execute('UPDATE channels SET claimant_id=NULL,claimed_at=NULL,sos_at=NULL,sos_token=NULL WHERE id=?', (held['id'],))
                                log(scoped, 'release', held['id'], uid, f'用户 {target_id} 被停用或删除')
                            for extra in user_explorations(scoped, target_id):
                                owner_col, time_col = next((owner, claimed) for owner, claimed in EXPLORATION_SLOTS
                                                           if extra[owner] == target_id)
                                scoped.execute(f'UPDATE channels SET {owner_col}=NULL,{time_col}=NULL WHERE id=?', (extra['id'],))
                                log(scoped, 'release', extra['id'], uid, f'用户 {target_id} 的开荒认领被停用或删除')
                            relay.sweep(scoped, sys.modules[__name__])
                    log(c, 'user_' + action, None, uid, f'用户 {target_id}')
                else:
                    raise ApiError(404, '接口不存在')
                if boss not in boss_scope.FIXED_CLOCK_BOSSES:
                    relay.sweep(c, sys.modules[__name__])
                if path != '/api/notice/read':
                    self.touch_activity(c, user)
                if path in ('/api/kill', '/api/kill-exploration', '/api/edit-kill'):
                    maybe_award_plus(c, uid)
                c.commit(); changed(c.realm); self.send(200, {'ok': True}); return
        except ApiError as e:
            self.send(e.status, {'error': e.message, **e.payload})
        except (ValueError, TypeError):
            self.send(400, {'error': '参数格式错误'})
        except BrokenPipeError:
            pass

    def do_GET(self): self.execute('GET')
    def do_POST(self): self.execute('POST')
    def log_message(self, fmt, *args): pass


def serve():
    init_db()
    def maintenance():
        tick = 0
        while not service_runtime.STOPPING.is_set():
            try:
                for realm in realm_scope.REALMS:
                    for selected_boss in boss_scope.BOSSES:
                        if selected_boss not in boss_scope.FIXED_CLOCK_BOSSES and tick % 5:
                            continue
                        with db(selected_boss, realm=realm) as c:
                            c.execute('BEGIN IMMEDIATE'); swept = sweep(c); c.commit()
                            if swept:
                                changed(c.realm)
            except Exception:
                traceback.print_exc()
            tick += 1
            service_runtime.STOPPING.wait(1)
    maintenance_thread = threading.Thread(target=maintenance, daemon=True)
    maintenance_thread.start()
    print(f'Listening on {HOST}:{PORT}', flush=True)
    try:
        service_runtime.run(HOST, PORT, Handler)
    finally:
        service_runtime.STOPPING.set()
        maintenance_thread.join(timeout=10)


if __name__ == '__main__':
    serve()
