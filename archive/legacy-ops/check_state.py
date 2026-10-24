"""Read-only MySQL check for the authenticated state builders on the CVM."""
import os
import sys
from pathlib import Path

for line in Path('/etc/mushroom-king-timer.env').read_text(encoding='utf-8').splitlines():
    if line.strip() and not line.startswith('#'):
        key, value = line.split('=', 1)
        os.environ[key] = value

sys.path.insert(0, '/opt/mushroom-king-timer')
import server

with server.db() as connection:
    for role in ('superadmin', 'member'):
        user = connection.execute('SELECT * FROM users WHERE role=? LIMIT 1', (role,)).fetchone()
        if user is None:
            continue
        state = server.admin_view(connection, user) if role == 'superadmin' else server.member_view(connection, user)
        assert state['me']['role'] == role
        if role == 'superadmin':
            assert len(state['channels']) == 60

print('Authenticated state check passed')
