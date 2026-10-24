"""Read-only production check for expired channel records."""
import os
import sys
from pathlib import Path

for line in Path('/etc/mushroom-king-timer.env').read_text(encoding='utf-8').splitlines():
    if line.strip() and not line.startswith('#'):
        key, value = line.split('=', 1)
        os.environ[key] = value

sys.path.insert(0, '/opt/mushroom-king-timer')
import server

assert server.UNKNOWN_AFTER_GUARANTEE == 2 * 60
with server.db() as connection:
    overdue = connection.execute('''SELECT count(*) AS n FROM channels
        WHERE killed_at IS NOT NULL AND killed_at <= ?''',
        (server.now() - 3600 - server.UNKNOWN_AFTER_GUARANTEE,)).fetchone()['n']
    unknown = connection.execute('''SELECT count(*) AS n FROM channels
        WHERE killed_at IS NULL AND claimant_id IS NULL''').fetchone()['n']
    resets = connection.execute("SELECT count(*) AS n FROM logs WHERE action='reset_unknown'").fetchone()['n']
    exploded = connection.execute('''SELECT count(*) AS n FROM channels
        WHERE killed_at IS NULL AND exploded_at IS NOT NULL''').fetchone()['n']
    invalid = connection.execute('''SELECT count(*) AS n FROM channels
        WHERE killed_at IS NOT NULL AND exploded_at IS NOT NULL''').fetchone()['n']

assert overdue == 0, f'{overdue} overdue channels remain'
assert invalid == 0, f'{invalid} killed channels have stale exploded markers'
print(f'Unknown reset check passed: {unknown} unknown channels, {exploded} exploded, {resets} automatic resets')
