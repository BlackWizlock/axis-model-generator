"""Host-only physical fencing for the offline scratch identity exception."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import urllib.request
import urllib.error


@contextmanager
def operator_lock(root):
    root=Path(root)
    descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    lock=None
    try:
        info=os.fstat(descriptor)
        if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise ValueError('Own operator root must be owned and mode 0700')
        lock=os.open('.release-operator.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=descriptor)
        info=os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1:
            raise ValueError('Unsafe operator lock')
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Another release or recovery operator is active') from None
        yield
    finally:
        if lock is not None:os.close(lock)
        os.close(descriptor)


def maintenance_gate(origin,confirmed):
    if not confirmed or origin!='https://model.axisconsult.ru':
        raise ValueError('Explicit operator-maintained public 503 required')
    for path in ('/','/health/live'):
        try:
            with urllib.request.urlopen(origin+path,timeout=5) as response:
                status=response.status
        except urllib.error.HTTPError as error:
            status=error.code
            error.close()
        if status!=503:raise ValueError('Public maintenance 503 is not active')


def inspect(container):
    return json.loads(subprocess.check_output(['docker','inspect',container]))[0]


def holders(project,images,keeper_gate,allow_missing_keeper=False):
    if not re.fullmatch(r'model-generator-mvp|mg-cold-proof-[0-9a-f]{8,16}',project):
        raise ValueError('Explicit own production or isolated cold proof project required')
    ids=set()
    for role in ('api','worker'):
        ids.update(subprocess.check_output(['docker','ps','-a','-q','--filter','volume='+project+'_'+role+'_scratch'],text=True).split())
    result={container:inspect(container) for container in ids}
    roles=set()
    for value in result.values():
        labels=value['Config'].get('Labels') or {}
        role=labels.get('com.docker.compose.service')
        if labels.get('com.docker.compose.project')!=project or role not in ('api','worker','scratch-keeper'):
            raise ValueError('Foreign or unclassified scratch holder')
        expected=images['keeper' if role=='scratch-keeper' else role]['id']
        if value['Image']!=expected:raise ValueError('Scratch holder image mismatch')
        roles.add(role)
        for mount in value['Mounts']:
            if mount.get('Type')=='bind' and (mount.get('Source','').endswith('/docker.sock') or mount.get('Destination','').endswith('/docker.sock')):
                raise ValueError('Docker daemon mount forbidden')
            if mount.get('Type')=='volume' and mount.get('Name') not in {project+'_'+name for name in ('api_scratch','worker_scratch','api_journal','worker_journal')}:
                raise ValueError('Foreign volume on scratch holder')
        if role=='scratch-keeper':keeper_gate(value,expected,project)
        else:
            expected_networks={project+'_mg_db',project+'_mg_files'}
            if role=='api':expected_networks.add(project+'_mg_edge')
            if set(value['NetworkSettings']['Networks'])!=expected_networks:
                raise ValueError('Writer network ownership mismatch')
            own=[m for m in value['Mounts'] if m.get('Type')=='volume' and m.get('Name')==project+'_'+role+'_scratch']
            if len(own)!=1 or own[0]['Destination']!='/scratch' or not own[0]['RW']:
                raise ValueError('Writer scratch ownership mismatch')
    if roles!={'api','worker','scratch-keeper'} and not (allow_missing_keeper and roles=={'api','worker'}):
        raise ValueError('Complete old writer and keeper inventory required')
    return result


def stopped(value):
    state=value['State']
    return not state['Running'] and not state.get('Restarting') and not state.get('Paused') and state.get('Pid',0)==0 and value['HostConfig']['RestartPolicy']['Name']=='no'


def prepare(compose,project,images,keeper_gate):
    # Inventory ALL holders before mutating even one container.
    inventory=holders(project,images,keeper_gate)
    for container in inventory:
        subprocess.run(['docker','update','--restart=no',container],check=True,capture_output=True)
    for container in inventory:
        subprocess.run(['docker','stop','--time','30',container],check=True,capture_output=True)
    inventory=holders(project,images,keeper_gate)
    if not all(stopped(value) for value in inventory.values()):
        raise ValueError('Physical old holder exit was not confirmed')
    # Every former scratch mount is now inactive. Only the stable keeper may
    # mount the replacement tmpfs before the identity exception is considered.
    subprocess.run(compose+['up','-d','--wait','--no-recreate','scratch-keeper'],check=True,capture_output=True)
    inventory=holders(project,images,keeper_gate)
    for value in inventory.values():
        role=value['Config']['Labels']['com.docker.compose.service']
        if role=='scratch-keeper':
            if not value['State']['Running'] or value['State'].get('Pid',0)<=0 or value['State'].get('Paused') or value['State'].get('Restarting'):
                raise ValueError('Fresh keeper must hold replacement scratch')
        elif not stopped(value):raise ValueError('Old writer restarted during preparation')


RESET_CODE="""import json,sys,psycopg
from pathlib import Path
expected=tuple(json.load(sys.stdin))
with psycopg.connect(Path('/run/secrets/database').read_text()) as con:
    if con.execute('SELECT current_database(),current_user').fetchone()!=('model_generator','mg_migrator'):
        raise RuntimeError('Own migrator identity required')
    con.execute("SET LOCAL lock_timeout='500ms'")
    con.execute("SET LOCAL statement_timeout='1000ms'")
    if con.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename IN ('mg_api','mg_worker')").fetchone()[0]:
        raise RuntimeError('Runtime PostgreSQL connections remain')
    if con.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted").fetchone()[0]:
        raise RuntimeError('Advisory lock remains')
    row=con.execute('SELECT device,inode,generation FROM mg.api_lock_identity WHERE singleton FOR UPDATE').fetchone()
    if row is not None:
        if row!=expected:raise RuntimeError('Expected old lock identity mismatch')
        deleted=con.execute('DELETE FROM mg.api_lock_identity WHERE singleton AND device=%s AND inode=%s AND generation IS NOT DISTINCT FROM %s RETURNING device,inode,generation',expected).fetchone()
        if deleted!=expected:raise RuntimeError('Expected singleton delete failed')
print('Offline expected identity exception passed; other durable state untouched')
"""


def reset(compose,root,project,images,expected,keeper_gate,migrator_file=None):
    if (len(expected)!=3 or any(type(value)!=int or value<0 for value in expected[:2])
            or (expected[2] is not None and (not isinstance(expected[2],str) or not re.fullmatch(r'[0-9a-f]{32}',expected[2])))):
        raise ValueError('Explicit expected old device, inode and generation required')
    inventory=holders(project,images,keeper_gate)
    for value in inventory.values():
        role=value['Config']['Labels']['com.docker.compose.service']
        if role=='scratch-keeper':
            if not value['State']['Running'] or value['State'].get('Pid',0)<=0 or value['State'].get('Paused') or value['State'].get('Restarting'):
                raise ValueError('Fresh keeper must hold replacement scratch')
        elif not stopped(value):raise ValueError('Live or restartable old writer forbids reset')
    keeper=next(container for container,value in inventory.items() if value['Config']['Labels']['com.docker.compose.service']=='scratch-keeper')
    identity_code="""import os,json,re,stat
p='/scratch/api/api.lock'
try:fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
except FileNotFoundError:print('null')
else:
    try:
        s=os.fstat(fd);b=os.pread(fd,33,0)
        assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_size==32 and re.fullmatch(b'[0-9a-f]{32}',b),'Malformed current lock generation'
        print(json.dumps([s.st_dev,s.st_ino,b.decode('ascii')]))
    finally:os.close(fd)
"""
    identity=json.loads(subprocess.check_output(['docker','exec',keeper,'python','-I','-c',identity_code],text=True))
    if identity is not None and tuple(identity)==tuple(expected):
        raise ValueError('Scratch identity survived; offline exception is unnecessary')
    dsn=Path(migrator_file) if migrator_file is not None else Path(root)/'secrets/runtime/migrator_dsn'
    if dsn.is_symlink() or not dsn.resolve(strict=True).is_relative_to(Path(root).resolve(strict=True)):
        raise ValueError('Own file-mounted migrator credential required')
    subprocess.run(['docker','run','--rm','-i','--network',project+'_mg_db','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','10001:10001','--memory','256m','--cpus','.5','--pids-limit','32','-v',str(dsn)+':/run/secrets/database:ro','--entrypoint','python',images['api']['id'],'-c',RESET_CODE],input=json.dumps(expected),text=True,check=True,capture_output=True)
