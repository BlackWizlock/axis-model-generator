"""Own PostgreSQL heartbeat plus installed CPU runtime; no filesystem heartbeat."""
import time
from model_generator.web.config import Settings
from model_generator.web.db import Database
from model_generator.web.preview_runner import installed_preview_fingerprint

def main():
    db=None
    try:
        settings=Settings.from_env()
        if settings.db_role!='mg_worker': raise ValueError
        db=Database(settings)
        with db.connect() as connection:
            row=connection.execute('SELECT * FROM worker_state WHERE singleton').fetchone()
        now=int(time.time())
        if (not row or not now-15<=row['heartbeat']<=now or not row['guard_verified']
                or not row['runtime_verified'] or row['runtime_fingerprint']!=installed_preview_fingerprint()):
            raise ValueError
    except Exception: raise SystemExit('Worker health gate failed.') from None
    finally:
        if db: db.close()
if __name__=='__main__': main()
