"""Restore only into an explicitly selected ephemeral database host."""
import argparse
import importlib.util
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
spec=importlib.util.spec_from_file_location('backup_common',Path(__file__).with_name('web-backup-common.py'))
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)
def validate_manifest(value,key,now):
    if (type(value)!=dict or value.get('schema')!=1 or value.get('key')!=key or value.get('format')!='postgres-custom'
            or value.get('database')!='model_generator' or type(value.get('bytes'))!=int or value['bytes']<=0
            or type(value.get('createdAt'))!=int or type(value.get('expiresAt'))!=int
            or not value['createdAt']<=now<value['expiresAt'] or value['expiresAt']-value['createdAt']!=7*86400
            or len(value.get('sha256',''))!=64 or any(c not in '0123456789abcdef' for c in value['sha256'])):
        raise ValueError('Backup manifest invalid or expired')
def restore(key,host):
    env=common.pg_environment(restore=True)
    if not host.startswith('mg-restore-') or env['PGHOST']!=host:raise ValueError('Ephemeral restore host required')
    if not key.startswith('database/') or not key.endswith('/database.dump') or '..' in key:raise ValueError('Backup key invalid')
    client,bucket=common.storage()
    body=client.get_object(Bucket=bucket,Key=key+'.json')['Body']
    wire=body.read(4097);body.close()
    if len(wire)>4096:raise ValueError('Manifest budget exceeded')
    manifest=json.loads(wire);validate_manifest(manifest,key,int(time.time()))
    with tempfile.TemporaryDirectory(prefix='mg-restore-') as directory:
        dump=Path(directory)/'database.dump';client.download_file(bucket,key,str(dump))
        if dump.stat().st_size!=manifest['bytes'] or common.digest(dump)!=manifest['sha256']:raise ValueError('Backup checksum mismatch')
        subprocess.run(['pg_restore','--exit-on-error','--single-transaction','--no-owner','--clean','--if-exists','--dbname','model_generator',str(dump)],env=env,check=True,timeout=600,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        import psycopg
        from model_generator.web.migrate import migrations
        dsn=Path(os.environ['MG_RESTORE_DATABASE_URL_FILE']).read_text().strip()
        with psycopg.connect(dsn,connect_timeout=3) as connection:
            connection.execute("SET statement_timeout='3s'")
            versions=dict(connection.execute('SELECT version,checksum FROM mg.schema_meta'))
            if versions!={version:checksum for version,checksum,_ in migrations()}:raise ValueError('Restored schema checksum mismatch')
            grants=connection.execute("SELECT has_schema_privilege('mg_api','mg','USAGE'),has_schema_privilege('mg_worker','mg','USAGE'),has_table_privilege('mg_api','mg.users','SELECT'),has_table_privilege('mg_worker','mg.jobs','SELECT')").fetchone()
            if grants!=(True,True,True,True):raise ValueError('Restored runtime roles lack required grants')
    return {'status':'restored','sha256':manifest['sha256'],'ephemeralHost':host}
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--key',required=True);parser.add_argument('--ephemeral-host',required=True);args=parser.parse_args()
    try:print(json.dumps(restore(args.key,args.ephemeral_host)))
    except Exception:raise SystemExit('Ephemeral restore gate failed.') from None
