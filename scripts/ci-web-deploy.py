from pathlib import Path
import os,subprocess,json,sys,uuid
import argparse,ipaddress
parser=argparse.ArgumentParser()
for name in ('api','worker','proxy','backup','emulator','keeper'):parser.add_argument('--'+name+'-image',required=True)
parser.add_argument('--network-overlay',required=True)
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
network_overlay=Path(args.network_overlay).resolve(strict=True)
if not network_overlay.is_relative_to(root/'_scratch') or Path(args.network_overlay).is_symlink():raise SystemExit('Own scratch overlay required')
project='mg-task7-'+uuid.uuid4().hex[:10];runtime=root/'_scratch'/project;runtime.mkdir(mode=0o700)
last_command='initialization'
last_returncode=None
def run(args,*,check=True,input=None):
 global last_command,last_returncode
 # Record the operation and own service, never inline code, stdin or cookies.
 if 'compose' in args:
  operation=args[len(compose):] if 'compose' in globals() else []
  services=[part for part in operation if part in ('api','worker','postgres','backup','restore','scratch-keeper','s3-proxy','minio','mg-restore-proof')]
  last_command='compose '+(operation[0] if operation else 'command')+' '+','.join(services)
 else: last_command=args[0]+' '+('inspect' if 'inspect' in args else 'command')
 value=subprocess.run(args,text=True,capture_output=True,env=env,input=input)
 last_returncode=value.returncode
 if check and value.returncode:
  raise RuntimeError('Task7 command failed with exit '+str(value.returncode))
 return value
subprocess.run([sys.executable,'scripts/web-test-env.py',str(runtime)],check=True,capture_output=True)
import secrets
password=secrets.token_hex(32)
(runtime/'backup').write_text(password)
(runtime/'backup_dsn').write_text('postgresql://mg_backup:'+password+'@postgres/model_generator')
for path in runtime.iterdir():path.chmod(0o444)
(runtime/'restore_dsn').write_text((runtime/'migrator_dsn').read_text().replace('@postgres/','@mg-restore-proof/'));(runtime/'restore_dsn').chmod(0o444)
env=os.environ.copy();env.update(MG_PUBLIC_ORIGIN='https://testserver',MG_API_IMAGE=args.api_image,MG_WORKER_IMAGE=args.worker_image,MG_PROXY_IMAGE=args.proxy_image,MG_KEEPER_IMAGE=args.keeper_image,MG_SECRET_ROOT=str(runtime),MG_S3_REGION='us-east-1',MG_S3_BUCKET='model-generator-test',MG_TRUSTED_PROXY_IPS='127.0.0.1')
overlay=runtime/'overlay.yaml'
backup_env='''      MG_TEST_MODE: "1"
      MG_BACKUP_PROFILE: ephemeral-emulator
      MG_BACKUP_BUCKET: model-generator-backup-test
      MG_BACKUP_REGION: us-east-1
      MG_BACKUP_PROXY_URL: http://s3-proxy:8080
      MG_BACKUP_ACCESS_KEY_FILE: /run/secrets/s3_access
      MG_BACKUP_SECRET_KEY_FILE: /run/secrets/s3_secret
'''
overlay.write_text('''services:
  api:
    environment:
      MG_TEST_MODE: "1"
      MG_S3_PROFILE: ephemeral-emulator
      MG_S3_ENDPOINT: http://minio:9000
      MG_MIGRATION_DATABASE_URL_FILE: /run/secrets/migrator_dsn
    secrets: [migrator_dsn]
  worker:
    environment:
      MG_TEST_MODE: "1"
      MG_S3_PROFILE: ephemeral-emulator
      MG_S3_ENDPOINT: http://minio:9000
  s3-proxy:
    entrypoint: [python, /proxy.py]
    volumes: ["'''+str(root/'deploy/web/s3-test-proxy.py')+''':/proxy.py:ro"]
  minio:
    image: '''+args.emulator_image+'''
    environment:
      MINIO_ROOT_USER_FILE: /run/secrets/s3_access
      MINIO_ROOT_PASSWORD_FILE: /run/secrets/s3_secret
    secrets: [s3_access, s3_secret]
    networks: [mg_egress]
  mg-restore-proof:
    image: postgres:16.15-bookworm@sha256:0ea6700a3b4f0ae6ce746519073558aed4d88a79d8d07622a9a644946c7319c4
    environment:
      POSTGRES_PASSWORD_FILE: /run/secrets/admin
    secrets: [admin, api, worker, migrator, backup]
    volumes: ["'''+str(root/'deploy/web/init-db.sh')+''':/docker-entrypoint-initdb.d/init.sh:ro"]
    networks: [mg_db]
  backup:
    image: '''+args.backup_image+'''
    environment:
'''+backup_env+'''      MG_BACKUP_DATABASE_URL_FILE: /run/secrets/backup_dsn
    secrets: [backup_dsn, s3_access, s3_secret]
    networks: [mg_db, mg_files]
    tmpfs: ["/tmp:rw,noexec,nosuid,size=256m,uid=10001,gid=10001"]
  restore:
    image: '''+args.backup_image+'''
    environment:
'''+backup_env+'''      MG_RESTORE_DATABASE_URL_FILE: /run/secrets/restore_dsn
    secrets: [restore_dsn, s3_access, s3_secret]
    networks: [mg_db, mg_files]
    tmpfs: ["/tmp:rw,noexec,nosuid,size=256m,uid=10001,gid=10001"]
'''+network_overlay.read_text()+'''
secrets:
  backup_dsn:
    file: '''+str(runtime/'backup_dsn')+'''
  migrator_dsn:
    file: '''+str(runtime/'migrator_dsn')+'''
  restore_dsn:
    file: '''+str(runtime/'restore_dsn')+'\n')
compose=['docker','compose','-p',project,'-f','deploy/web/compose.yaml','-f',str(overlay)]
try:
 run(compose+['up','-d','--wait','postgres','mg-restore-proof','minio','s3-proxy'])
 run(compose+['run','--rm','--entrypoint','python','api','-m','model_generator.web.migrate'])
 run(compose+['run','--rm','--entrypoint','python','api','-c',"import boto3,os;from pathlib import Path;from botocore.config import Config;c=boto3.client('s3',endpoint_url='http://minio:9000',aws_access_key_id=Path('/run/secrets/s3_access').read_text(),aws_secret_access_key=Path('/run/secrets/s3_secret').read_text(),config=Config(proxies={'http':'http://s3-proxy:8080'}));c.create_bucket(Bucket='model-generator-test');c.create_bucket(Bucket='model-generator-backup-test')"])
 print('Own production PostgreSQL initialization/migration and emulator S3 proxy passed')
 run(compose+['up','-d','--wait','scratch-keeper'])
 keeper=run(compose+['ps','-q','scratch-keeper']).stdout.strip()
 keeper_info=json.loads(run(['docker','inspect',keeper]).stdout)[0]
 assert keeper_info['HostConfig']['NetworkMode']=='none' and keeper_info['HostConfig']['ReadonlyRootfs']
 assert all(not mount['RW'] for mount in keeper_info['Mounts'])
 print('Networkless read-only scratch keeper preserves tmpfs mount lifetime')
 run(compose+['up','-d','--wait','api','worker'])
 run(compose+['exec','-T','api','python','scripts/web-healthcheck.py','--url','http://localhost:8000/health/ready'])
 print('Actual production API and worker readiness passed with controlled S3 emulator')
 for network in ('mg_edge','mg_db','mg_files'):
  assert run(['docker','network','inspect',project+'_'+network,'--format','{{.Internal}}']).stdout.strip()=='true'
 for service,networks,memory,cpu in (('api',{'mg_edge','mg_db','mg_files'},512*1024**2,1000000000),('worker',{'mg_db','mg_files'},2048*1024**2,1000000000)):
  container=run(compose+['ps','-q',service]).stdout.strip()
  config=json.loads(run(['docker','inspect',container,'--format','{{json .HostConfig}}']).stdout)
  assert config['ReadonlyRootfs'] and config['Memory']==memory and config['NanoCpus']==cpu and config['PidsLimit']==64 and 'ALL' in config['CapDrop'] and not config['PortBindings']
  actual=json.loads(run(['docker','inspect',container,'--format','{{json .NetworkSettings.Networks}}']).stdout)
  assert set(actual)=={project+'_'+name for name in networks}
  code="import shutil;from pathlib import Path;assert shutil.disk_usage('/scratch').free>=2*1024**3;assert Path('/scratch').stat().st_mode&0o777==0o700;assert Path('/journal').stat().st_uid==10001"
  run(compose+['exec','-T',service,'python','-c',code])
 assert run(compose+['exec','-T','api','python','scripts/web-healthcheck.py','--url','http://localhost:8000/health/live','--host','api'],check=False).returncode!=0
 print('Actual own networks, resource quotas, private scratch/journal and public Host policy passed')

 for service in ('api','worker'):
  code="import socket; targets=[('1.1.1.1',443),('169.254.169.254',80)];\nfor host,port in targets:\n try: c=socket.create_connection((host,port),timeout=1)\n except OSError: continue\n c.close();raise SystemExit('Direct backend TCP unexpectedly allowed')"
  run(compose+['exec','-T',service,'python','-c',code])
 print('Actual backend public/metadata direct TCP denied')
 fixture_code=(root/'tests/fixtures/package_builders.py').read_text()+"\nimport base64;print(base64.b64encode(make_package()).decode())"
 fixture=run(compose+['exec','-T','api','python','-c',fixture_code]).stdout.strip()
 http_proof_code=(root/'tests/web/production_http_proof.py').read_text()
 result=run(compose+['exec','-T','api','python','-c',http_proof_code],input=json.dumps({'mode':'create','fixture':fixture}))
 http_proof=json.loads(result.stdout)
 print('Actual production images upload/job/report/preview/thumbnail and private access passed')


 run(compose+['stop','worker'])
 active_code=(root/'tests/web/production_active_restore_proof.py').read_text()
 active=json.loads(run(compose+['exec','-T','api','python','-c',active_code],input=json.dumps({'mode':'seed','fixture':fixture,'proof':http_proof})).stdout)
 run(compose+['stop','api'])
 # Prior processes physically stopped. Test a finalization snapshot at the durable boundary.
 finalize_code="import psycopg,sys;from pathlib import Path;c=psycopg.connect(Path('/run/secrets/migrator_dsn').read_text());c.execute(\"UPDATE mg.uploads SET state='finalizing' WHERE id=%s AND protocol='chunks-v1' AND received_bytes=declared_bytes AND request_epoch IS NULL\",(sys.stdin.read(),));c.commit()"
 run(compose+['run','--rm','-T','--entrypoint','python','api','-c',finalize_code],input=active['finalizing'])
 print('Stopped own processes; backup includes queued job, receiving and finalizing chunks')
 code="import psycopg;from pathlib import Path;c=psycopg.connect(Path('/run/secrets/backup_dsn').read_text());\ntry:c.execute('CREATE TABLE mg.forbidden_write(id int)')\nexcept psycopg.errors.InsufficientPrivilege:pass\nelse:raise SystemExit('Backup DDL unexpectedly allowed')"
 run(compose+['run','--rm','--entrypoint','python','backup','-c',code])
 print('Actual readonly backup DB role denies DDL')
 snapshot_code="import psycopg,json;from pathlib import Path;c=psycopg.connect(Path('/run/secrets/backup_dsn').read_text());print(json.dumps(c.execute('SELECT user_id,created_at,expires_at,last_seen FROM mg.sessions ORDER BY user_id').fetchall()))"
 session_snapshot=json.loads(run(compose+['run','--rm','--entrypoint','python','backup','-c',snapshot_code]).stdout)
 result=run(compose+['run','--rm','backup']);proof=json.loads(result.stdout.strip().splitlines()[-1]);key=f"database/{proof['createdAt']}/database.dump"
 print('Actual backup image pg_dump and S3 upload passed')
 result=run(compose+['run','--rm','--entrypoint','python','restore','scripts/restore-web-db.py','--key',key,'--ephemeral-host','mg-restore-proof'])
 print('Actual ephemeral pg_restore/hash/schema passed')
 code="import psycopg,json,sys;from pathlib import Path;c=psycopg.connect(Path('/run/secrets/restore_dsn').read_text());assert c.execute('SELECT count(*) FROM mg.users').fetchone()[0]==2;assert c.execute('SELECT count(DISTINCT user_id),bool_and(expires_at-created_at>=172800) FROM mg.sessions').fetchone()==(2,True);assert json.loads(json.dumps(c.execute('SELECT user_id,created_at,expires_at,last_seen FROM mg.sessions ORDER BY user_id').fetchall()))==json.load(sys.stdin);assert c.execute('SELECT count(DISTINCT owner_id) FROM mg.quota_scopes WHERE owner_id IS NOT NULL').fetchone()[0]==2;print('Restored separate owners, private sessions, TTL and quotas passed')"
 run(compose+['run','--rm','-T','--entrypoint','python','restore','-c',code],input=json.dumps(session_snapshot))
 print('Actual restored two-owner session/TTL/quota proof passed')
 run(compose+['stop','api','worker'])
 for service in ('api','worker'):
  assert not run(compose+['ps','--status','running','-q',service]).stdout.strip()
 # Preserve physical scratch lock identity. No fencing reset in this recovery.
 dsn=runtime/'api_dsn';dsn.chmod(0o600);dsn.write_text(dsn.read_text().replace('@postgres/','@mg-restore-proof/'));dsn.chmod(0o444)
 wdsn=runtime/'worker_dsn';wdsn.chmod(0o600);wdsn.write_text(wdsn.read_text().replace('@postgres/','@mg-restore-proof/'));wdsn.chmod(0o444)
 run(compose+['up','-d','--wait','--force-recreate','api','worker'])
 run(compose+['exec','-T','api','python','-c',active_code],input=json.dumps({'mode':'verify','proof':http_proof,'active':active}))
 print('Active-operation restore passed with preserved physical fencing')
 run(compose+['exec','-T','api','python','-c',http_proof_code],input=json.dumps({'mode':'verify','proof':http_proof}))
 print('Restored private HTTP and artifact SHA256 verification passed')

except Exception as e:
 from urllib.parse import urlsplit
 failed_command=last_command
 failed_returncode=last_returncode
 failure_result=run(compose+['logs','--no-color','--tail','500','api','worker','postgres'],check=False)
 failure=('Failed phase: '+failed_command+'; exit: '+str(failed_returncode)+'; exception: '+type(e).__name__+'\n'+failure_result.stdout+failure_result.stderr)
 redactions=set()
 for secret in runtime.iterdir():
  if secret.name not in ('overlay.yaml',):
   value=secret.read_text().strip()
   if value:
    redactions.add(value)
    if value.startswith('postgresql://'):
     password=urlsplit(value).password
     if password:redactions.add(password)
 for value in sorted(redactions,key=len,reverse=True):
  failure=failure.replace(value,'[REDACTED]')
 failure_path=root/'_scratch'/'production-failure-redacted.log'
 failure_path.write_text(failure[-256*1024:])
 failure_path.chmod(0o600)
 print('Production proof failed; redacted API/worker/PostgreSQL diagnostics retained');
 raise RuntimeError('Production proof failed; see redacted diagnostics') from None
finally:
 run(compose+['down','--volumes','--remove-orphans'],check=False)
 for path in runtime.iterdir():path.unlink()
 runtime.rmdir()
