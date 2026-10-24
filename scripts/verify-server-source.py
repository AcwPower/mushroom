"""Verify the imported server snapshot without network or database access."""
import hashlib
import json
import sys
from pathlib import Path
root = Path(__file__).resolve().parent.parent
manifest = json.loads((root / 'SERVER_SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
changed = []
for name, entry in manifest['files'].items():
    path = root / name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        changed.append(name)
if changed:
    print('Changed since server snapshot:')
    print('\n'.join(changed))
    sys.exit(1)
print('PASS:', len(manifest['files']), 'server-source files match snapshot at', manifest['captured_at'])
