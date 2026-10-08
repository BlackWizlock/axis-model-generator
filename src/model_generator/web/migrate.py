"""Explicit numbered migrations, only using the own migration credential."""
import hashlib
from pathlib import Path
import time
import psycopg
from .config import read_secret_env, validate_database_url

EXPECTED_SCHEMA_VERSION = 10
MIGRATION_LOCK = 0x4d474d494752


def migrations():
    result=[]
    for path in sorted(Path(__file__).with_name('migrations').glob('[0-9][0-9][0-9]-*.sql')):
        data=path.read_bytes()
        result.append((int(path.name.split('-')[0]),hashlib.sha256(data).hexdigest(),data.decode('utf-8')))
    if [item[0] for item in result] != list(range(1,EXPECTED_SCHEMA_VERSION+1)):
        raise RuntimeError('Migration inventory is invalid')
    return result


def migration_identity(con):
    row=con.execute("SELECT current_database(),current_user,(SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='mg'),current_setting('search_path')").fetchone()
    if row[:3] != ('model_generator','mg_migrator','mg_migrator') or row[3].replace(' ','') != 'mg,pg_catalog':
        raise RuntimeError('Migration database identity is invalid')


def migrate(database_url: str) -> None:
    validate_database_url(database_url,'mg_migrator',migration=True)
    try:
        with psycopg.connect(database_url,autocommit=True,connect_timeout=1) as con:
            migration_identity(con)
            con.execute("SET statement_timeout='5s'")
            con.execute('SELECT pg_advisory_lock(%s)',(MIGRATION_LOCK,))
            try:
                exists=con.execute("SELECT to_regclass('mg.schema_meta')").fetchone()[0]
                applied=dict(con.execute('SELECT version,checksum FROM mg.schema_meta').fetchall()) if exists else {}
                known={version:checksum for version,checksum,_ in migrations()}
                if any(known.get(v)!=c for v,c in applied.items()) or sorted(applied)!=list(range(1,len(applied)+1)):
                    raise RuntimeError('Migration checksum or version mismatch')
                for version,checksum,sql in migrations():
                    if version in applied: continue
                    with con.transaction():
                        con.execute(sql)
                        con.execute('INSERT INTO schema_meta VALUES(%s,%s,%s)',(version,checksum,int(time.time())))
            finally: con.execute('SELECT pg_advisory_unlock(%s)',(MIGRATION_LOCK,))
    except psycopg.Error:
        raise RuntimeError('Own PostgreSQL migration unavailable') from None


def main():
    try:
        migrate(read_secret_env('MG_MIGRATION_DATABASE_URL'))
    except (RuntimeError,ValueError,OSError):
        raise SystemExit('Own PostgreSQL migration failed.') from None
    print(f'Own PostgreSQL migration version {EXPECTED_SCHEMA_VERSION} verified.')


if __name__ == '__main__': main()
