"""Generate disposable synthetic credentials; never print their values."""
from pathlib import Path
import os
import secrets
import sys
root = Path(sys.argv[1])
root.mkdir(mode=0o700, parents=True, exist_ok=True)
# Compose binds these disposable files directly into different container UIDs.
# Keep the host directory private; fixtures are read-only across those UIDs.
values = {name: secrets.token_hex(32) for name in ('admin', 'api', 'worker', 'migrator', 'auth_key', 's3_access', 's3_secret')}
for name, value in values.items():
    path = root / name
    path.write_text(value)
    path.chmod(0o444)
for role in ('api', 'worker', 'migrator'):
    path = root / (role + '_dsn')
    path.write_text('postgresql://mg_' + role + ':' + values[role] + '@postgres/model_generator?options=-csearch_path%3Dmg%2Cpg_catalog')
    path.chmod(0o444)
path = root / 'admin_dsn'
path.write_text('postgresql://postgres:' + values['admin'] + '@postgres/model_generator')
path.chmod(0o444)
print('Synthetic test secrets generated.')
