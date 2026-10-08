"""Explicit separately attested legacy transition; host-only, under operator lock."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


def previous_gate(directory,source,export_hash):
    if not re.fullmatch(r'[0-9a-f]{40}',source):raise ValueError('Explicit previous source required')
    manifest=json.loads((directory/'images.json').read_text())
    exported_path=directory/'source/public-manifest.json'
    if hashlib.sha256(exported_path.read_bytes()).hexdigest()!=export_hash:raise ValueError('Previous export hash mismatch')
    exported=json.loads(exported_path.read_text())
    if manifest['source_revision']!=source or exported['source_revision']!=source:raise ValueError('Previous source mismatch')
    # Old deliveries predate OCI labels; exact manifest, source/export hash and
    # platform remain mandatory. If an old label exists it must agree.
    for role in ('api','worker'):
        pin=manifest['images'][role]['id']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',pin):raise ValueError('Previous exact image ID required')
        actual=json.loads(subprocess.check_output(['docker','image','inspect',pin]))[0]
        revision=(actual['Config'].get('Labels') or {}).get('org.opencontainers.image.revision')
        if actual['Id']!=pin or actual['Os']!='linux' or actual['Architecture']!='amd64' or revision not in (None,source):
            raise ValueError('Previous image/source/platform mismatch')
    manifest['_export_manifest_sha256']=export_hash
    return manifest


def writer_gate(inventory):
    for value in inventory.values():
        role=value['Config']['Labels']['com.docker.compose.service']
        if role=='scratch-keeper':continue
        host=value['HostConfig']
        if (value['Config']['User']!='10001:10001' or not host['ReadonlyRootfs']
                or 'ALL' not in host.get('CapDrop',[]) or 'no-new-privileges:true' not in host.get('SecurityOpt',[])
                or host['PidsLimit']!=64 or host['NanoCpus']!=1000000000
                or host['Memory']!=(512 if role=='api' else 2048)*1024**2
                or host.get('Privileged') or host.get('PortBindings')):
            raise ValueError('Legacy writer hardening mismatch')


OFFLINE="""import psycopg
from pathlib import Path
with psycopg.connect(Path('/run/secrets/database').read_text()) as c:
    assert c.execute('SELECT current_database(),current_user').fetchone()==('model_generator','mg_migrator'),'Own migrator required'
    c.execute("SET LOCAL lock_timeout='500ms'");c.execute("SET LOCAL statement_timeout='1000ms'")
    assert not c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename IN ('mg_api','mg_worker')").fetchone()[0],'Runtime connections remain'
    assert not c.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted").fetchone()[0],'Advisory lock remains'
"""
SNAPSHOT="""import json,psycopg
from pathlib import Path
from psycopg import sql
with psycopg.connect(Path('/run/secrets/database').read_text()) as c:
    result={}
    for (name,) in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='mg' ORDER BY tablename"):
        result[name]=c.execute(sql.SQL("SELECT COALESCE(jsonb_agg(value ORDER BY value::text),'[]'::jsonb) FROM (SELECT to_jsonb(t) value FROM mg.{} t) q").format(sql.Identifier(name))).fetchone()[0]
    print(json.dumps(result,sort_keys=True))
"""
# Head and hash every existing object using bounded transport calls; never
# write objects or expose object names/credentials in operator output.
S3_SNAPSHOT="""import boto3,hashlib,json
from botocore.config import Config
from model_generator.web.config import Settings
s=Settings.from_env().storage
client=boto3.client('s3',endpoint_url=s.endpoint,region_name=s.region,aws_access_key_id=s.access_key_file.read_text().strip(),aws_secret_access_key=s.secret_key_file.read_text().strip(),config=Config(connect_timeout=1,read_timeout=2,retries={'total_max_attempts':1},proxies={'https':s.proxy_url,'http':s.proxy_url}))
result={}
for page in client.get_paginator('list_objects_v2').paginate(Bucket=s.bucket):
    for row in page.get('Contents',[]):
        body=client.get_object(Bucket=s.bucket,Key=row['Key'])['Body'];digest=hashlib.sha256()
        try:
            while data:=body.read(65536):digest.update(data)
        finally:body.close()
        result[row['Key']]=digest.hexdigest()
print(json.dumps(result,sort_keys=True))
"""
STARTUP_DENY="""import asyncio
from model_generator.web.app import create_default_app
async def main():
    app=create_default_app()
    try:
        async with app.router.lifespan_context(app):raise AssertionError('Legacy startup accepted')
    except RuntimeError as error:
        if str(error)!='API lock identity changed':raise
asyncio.run(main())
"""


def transition(compose,root,project,new_images,previous,expected,cold,keeper_gate,migrate,dependencies=None):
    def run(args,**kwargs):
        try:return subprocess.run(args,timeout=180,**kwargs)
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as error:
            # Keep the first exact failing phase privately, never raw credentials
            # or command text. CLI retains its short public failure envelope.
            text=error.stderr or '';text=text.decode(errors='replace') if isinstance(text,bytes) else text
            secrets=root/'secrets/runtime'
            values=set()
            if secrets.exists():
                for path in secrets.iterdir():
                    if path.is_file() and not path.is_symlink():
                        value=path.read_text().strip();values.add(value)
                        if value.startswith('postgresql://') and urlsplit(value).password:values.add(urlsplit(value).password)
            for value in sorted(values,key=len,reverse=True):
                if value:text=text.replace(value,'[REDACTED]')
            path=root/'legacy-command-failure.json'
            if not path.exists():
                code=args[args.index('-c')+1] if '-c' in args else None
                phase={OFFLINE:'quiescence',SNAPSHOT:'database-snapshot',S3_SNAPSHOT:'s3-snapshot',STARTUP_DENY:'actual-startup-denial'}.get(code,'migration-or-container-control')
                payload={'phase':phase,'operation':'compose' if 'compose' in args else 'database','returncode':getattr(error,'returncode',None),'timeout':isinstance(error,subprocess.TimeoutExpired),'stderr':text[-65536:]}
                path.write_text(json.dumps(payload));path.chmod(0o600)
            raise
    if expected[2] is not None:raise ValueError('Explicit expected legacy NULL required')
    mixed=dict(previous['images']);mixed['keeper']=new_images['keeper']
    inventory=cold.holders(project,mixed,keeper_gate,allow_missing_keeper=True);writer_gate(inventory)
    # Validate old writers before adding the optional stable new keeper. This
    # never starts or recreates an old writer.
    if not any(v['Config']['Labels']['com.docker.compose.service']=='scratch-keeper' for v in inventory.values()):
        run(compose+['up','-d','--wait','--no-deps','--no-recreate','scratch-keeper'],check=True,capture_output=True)
    cold.prepare(compose,project,mixed,keeper_gate)
    dsn=root/'secrets/runtime/migrator_dsn'
    if dsn.is_symlink() or not dsn.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ValueError('Own file-mounted migrator credential required')
    def database(code):
        return run(['docker','run','--rm','--network',project+'_mg_db','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','10001:10001','--memory','256m','--cpus','.5','--pids-limit','32','-v',str(dsn)+':/run/secrets/database:ro','--entrypoint','python',new_images['api']['id'],'-c',code],check=True,capture_output=True,text=True).stdout
    database(OFFLINE)
    old_identity=json.loads(database("import json,psycopg;from pathlib import Path;\nwith psycopg.connect(Path('/run/secrets/database').read_text()) as c:print(json.dumps(c.execute('SELECT device,inode FROM mg.api_lock_identity WHERE singleton').fetchone()))"))
    if old_identity!=list(expected[:2]):raise ValueError('Expected previous identity mismatch before migration')
    run(compose+['up','--no-start','--no-deps','--force-recreate','api','worker'],check=True,capture_output=True)
    # --no-start creates writers without starting them. Disable their normal policy immediately,
    # then demand exact new image inventory with PID0 before any migration.
    inventory=cold.holders(project,new_images,keeper_gate)
    for container,value in inventory.items():
        if value['Config']['Labels']['com.docker.compose.service']!='scratch-keeper':
            run(['docker','update','--restart=no',container],check=True,capture_output=True)
    inventory=cold.holders(project,new_images,keeper_gate)
    if any(not cold.stopped(v) for v in inventory.values() if v['Config']['Labels']['com.docker.compose.service']!='scratch-keeper'):
        raise ValueError('New stopped-only writer replacement failed')
    database(OFFLINE)
    if dependencies is not None:dependencies()
    migrate()
    before=json.loads(database(SNAPSHOT))
    identity=before['api_lock_identity']
    if len(identity)!=1 or (identity[0]['device'],identity[0]['inode'],identity[0]['generation'])!=expected:
        raise ValueError('Expected actual legacy identity mismatch')
    def api(code):
        return run(compose+['run','--rm','--no-deps','-T','--entrypoint','python','api','-c',code],check=True,capture_output=True,text=True).stdout
    s3_before=json.loads(api(S3_SNAPSHOT));api(STARTUP_DENY)
    inventory=cold.holders(project,new_images,keeper_gate)
    if any(not cold.stopped(v) for v in inventory.values() if v['Config']['Labels']['com.docker.compose.service']!='scratch-keeper'):
        raise ValueError('Writer restarted after negative startup')
    if json.loads(database(SNAPSHOT))!=before or json.loads(api(S3_SNAPSHOT))!=s3_before:
        raise ValueError('Negative startup changed durable state')
    cold.reset(compose,root,project,new_images,expected,keeper_gate)
    cold.reset(compose,root,project,new_images,expected,keeper_gate)
    after=json.loads(database(SNAPSHOT))
    if after.pop('api_lock_identity')!=[]:raise ValueError('Legacy identity singleton not cleared')
    before.pop('api_lock_identity')
    if after!=before or json.loads(api(S3_SNAPSHOT))!=s3_before:raise ValueError('Offline reset changed other durable state')
