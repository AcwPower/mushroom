"""Read-only production smoke check, run as root on the CVM."""
import json
import urllib.request

env = {}
for line in open('/etc/mushroom-king-timer.env', encoding='utf-8'):
    if line.strip() and not line.startswith('#'):
        key, value = line.strip().split('=', 1)
        env[key] = value

base = 'http://127.0.0.1:8000/api/'
data = json.dumps({'username': 'root', 'password': env['ADMIN_PASSWORD']}).encode()
req = urllib.request.Request(base + 'login', data=data,
    headers={'Content-Type': 'application/json', 'Origin': env['APP_BASE_URL']})
with urllib.request.urlopen(req, timeout=5) as response:
    assert json.load(response)['ok']
    cookie = response.headers['Set-Cookie'].split(';', 1)[0]
with urllib.request.urlopen(urllib.request.Request(base + 'state', headers={'Cookie': cookie}), timeout=5) as response:
    state = json.load(response)
assert state['me']['role'] == 'superadmin'
assert len(state['channels']) == 60
print('Smoke check passed: admin login and 60 MySQL channels')
