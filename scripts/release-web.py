"""Promote imported, accepted images into the isolated diagnostic MVP only."""
import argparse
import hashlib
import json
import importlib.util
import re
import subprocess
from pathlib import Path


def manifest_image_ids(manifest):
    images=manifest.get('images')
    if not isinstance(images,dict) or not images:raise ValueError('Exact manifest image map required')
    result={}
    for role,value in images.items():
        pin=value.get('id') if isinstance(value,dict) else None
        if not isinstance(role,str) or not isinstance(pin,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',pin):
            raise ValueError('Exact manifest image ID required for every role')
        result[role]=pin
    return result


def previous_binding_gate(directory,previous):
    attestation=json.loads((directory/'cold-native-attestation.json').read_text())
    if (attestation.get('legacy_source')!=previous['source_revision']
            or attestation.get('previous_images')!=manifest_image_ids(previous)
            or attestation.get('previous_export_manifest_sha256')!=previous['_export_manifest_sha256']):
        raise ValueError('Exact previous images and export native legacy evidence required')


def evidence_gate(directory, accepted_source):
    manifest = json.loads((directory / 'images.json').read_text())
    if not re.fullmatch(r'[0-9a-f]{40}', accepted_source) or manifest['source_revision'] != accepted_source:
        raise ValueError('Accepted source does not match delivered images')
    if (directory / 'final-native.exit').read_text().strip() != '0':
        raise ValueError('Final native acceptance failed or unfinished')
    final = (directory / 'final-native.log').read_text()
    if 'Final exact native UI, ingress and production active restore acceptance passed' not in final:
        raise ValueError('Final native acceptance evidence missing')
    diagnostic = (directory / 'diagnostics.log').read_text()
    counts = [int(value) for value in re.findall(r'^Ran ([0-9]+) tests in .+$', diagnostic, re.MULTILINE)]
    if len(counts) != 2 or counts[0] < 246 or counts[1] < 187:
        raise ValueError('Full native diagnostic suite evidence missing')
    if 'diagnostics Docker acceptance passed.' not in diagnostic:
        raise ValueError('Full native diagnostic evidence missing')
    if re.search(r'(^FAILED|FAILED \(|skipped=|^ERROR:)', diagnostic, re.MULTILINE):
        raise ValueError('Diagnostic acceptance contains failure or skip')
    if (directory/'cold-native.exit').read_text().strip()!='0':
        raise ValueError('Final native cold acceptance failed or unfinished')
    if 'Final native isolated cold recovery acceptance passed' not in (directory/'cold-native.log').read_text():
        raise ValueError('Final native cold acceptance evidence missing')
    attestation=json.loads((directory/'cold-native-attestation.json').read_text())
    if attestation.get('runtime_images')!=manifest_image_ids(manifest):
        raise ValueError('Cold proof exact runtime images mismatch')
    exported=json.loads((directory/'source/public-manifest.json').read_text())
    if attestation['runtime_source']!=accepted_source or exported['source_revision']!=accepted_source:
        raise ValueError('Cold proof source mismatch')
    hashes={row['path']:row['sha256'] for row in exported['files']}
    for name in ('cold-recovery-web.py','ci-web-cold-recovery.py','legacy-recovery-web.py'):
        delivered=hashlib.sha256((directory/'source/scripts'/name).read_bytes()).hexdigest()
        if delivered!=hashes['scripts/'+name] or delivered!=attestation['helpers'][name] or delivered!=hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest():
            raise ValueError('Cold proof reviewed helper mismatch')
    return manifest

def topology_gate(config):
    expected = {'api': {'mg_edge', 'mg_db', 'mg_files'}, 'worker': {'mg_db', 'mg_files'}, 'postgres': {'mg_db'}, 's3-proxy': {'mg_files', 'mg_egress'}, 'ingress': {'mg_edge'}, 'backup-proxy': {'mg_backup_files', 'mg_backup_egress'}, 'backup': {'mg_db', 'mg_backup_files'}, 'scratch-keeper': set()}
    if set(config['services']) != set(expected):
        raise ValueError('Unexpected production service')
    for name, nets in expected.items():
        service = config['services'][name]
        if set(service.get('networks', {})) != nets or service.get('ports') or service.get('privileged') or service.get('network_mode') != ('none' if name == 'scratch-keeper' else None):
            raise ValueError('Unexpected service network or ports')
        if not service.get('read_only') or 'ALL' not in service.get('cap_drop', []):
            raise ValueError('Service hardening missing')
    internal = {'mg_edge', 'mg_db', 'mg_files', 'mg_backup_files'}
    if set(config['networks']) != set.union(*expected.values()):
        raise ValueError('Unexpected network')
    for name, network in config['networks'].items():
        if network.get('external') or bool(network.get('internal')) != (name in internal):
            raise ValueError('External network or wrong internal flag')
    if config['services']['ingress']['networks']['mg_edge'].get('ipv4_address') != '10.251.61.3' or config['services']['api']['networks']['mg_edge'].get('ipv4_address') != '10.251.61.2':
        raise ValueError('Trusted ingress address mismatch')


def keeper_gate(actual, expected_image, prefix):
    host = actual['HostConfig']
    if (actual['Image'] != expected_image or actual['Config']['User'] != '10001:10001'
            or host['NetworkMode'] != 'none' or not host['ReadonlyRootfs']
            or host['Memory'] != 64 * 1024**2 or host['NanoCpus'] != 50000000
            or host['PidsLimit'] != 8 or 'ALL' not in host.get('CapDrop', [])
            or 'no-new-privileges:true' not in host.get('SecurityOpt', []) or host.get('PortBindings')):
        raise ValueError('Existing keeper image or hardening mismatch')
    mounts = {row['Destination']: row for row in actual['Mounts']}
    if set(mounts) != {'/scratch/api', '/scratch/worker'}:
        raise ValueError('Existing keeper mount scope mismatch')
    for role in ('api', 'worker'):
        mount = mounts['/scratch/' + role]
        if mount['Type'] != 'volume' or mount['Name'] != prefix + '_' + role + '_scratch' or mount['RW']:
            raise ValueError('Existing keeper must hold only own read-only scratch')
    if actual['Config']['Entrypoint'] != ['python', '-I', '-c', 'import time; time.sleep(10**9)'] or actual['Config'].get('Cmd'):
        raise ValueError('Existing keeper command changed')
    if any(value.startswith('MG_') for value in actual['Config'].get('Env', [])):
        raise ValueError('Keeper must not receive application environment or secrets')


def configuration_gate(root, directory, accepted_source, export_hash):
    manifest_path = directory / 'source/public-manifest.json'
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != export_hash:
        raise ValueError('Accepted export manifest hash mismatch')
    exported = json.loads(manifest_path.read_text())
    if exported['source_revision'] != accepted_source:
        raise ValueError('Configuration source mismatch')
    hashes = {row['path']: row['sha256'] for row in exported['files']}
    for name in ('compose.yaml', 'compose.ingress-unix.yaml', 'compose.backup.yaml', 'ingress-unix.json', 'init-db.sh'):
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != hashes['deploy/web/' + name]:
            raise ValueError('Delivered runtime configuration changed')


_cold_spec=importlib.util.spec_from_file_location('cold_operator',Path(__file__).with_name('cold-recovery-web.py'))
cold_operator=importlib.util.module_from_spec(_cold_spec);_cold_spec.loader.exec_module(cold_operator)

_legacy_spec=importlib.util.spec_from_file_location('legacy_operator',Path(__file__).with_name('legacy-recovery-web.py'))
legacy_operator=importlib.util.module_from_spec(_legacy_spec);_legacy_spec.loader.exec_module(legacy_operator)


FINAL_ROLES = {'api':'api','worker':'worker','s3-proxy':'proxy','ingress':'ingress','backup-proxy':'proxy','scratch-keeper':'keeper','postgres':None}

READONLY_PROMOTED_PROBE = """import os,stat,signal,psycopg
from pathlib import Path
from model_generator.web.config import Settings
from model_generator.web.migrate import migrations
from model_generator.web.lock_identity import generation as read_generation
signal.signal(signal.SIGALRM,lambda *_: (_ for _ in ()).throw(TimeoutError('Finalization probe deadline')))
signal.alarm(5)
s=Settings.from_env();p=s.data_root/'api.lock'
f=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
try:
    st=os.fstat(f);current=p.stat(follow_symlinks=False)
    assert stat.S_ISREG(st.st_mode) and st.st_nlink==1 and st.st_uid==os.getuid()
    assert stat.S_ISREG(current.st_mode) and current.st_nlink==1 and (st.st_dev,st.st_ino)==(current.st_dev,current.st_ino)
    generation=read_generation(f)
    with psycopg.connect(s.database_url,autocommit=True,connect_timeout=1,options='-c default_transaction_read_only=on -c statement_timeout=1000 -c lock_timeout=500 -c idle_in_transaction_session_timeout=200') as c:
        assert dict(c.execute('SELECT version,checksum FROM mg.schema_meta ORDER BY version').fetchall())=={v:h for v,h,_ in migrations()}
        assert c.execute('SELECT device,inode,generation FROM mg.api_lock_identity WHERE singleton').fetchall()==[(st.st_dev,st.st_ino,generation)]
    current=p.stat(follow_symlinks=False)
    assert stat.S_ISREG(current.st_mode) and current.st_nlink==1 and (current.st_dev,current.st_ino)==(st.st_dev,st.st_ino) and read_generation(f)==generation
finally:os.close(f)
print('Read-only promoted schema and physical generation verified')
"""


def secret_destination(secret):
    target=secret.get('target',secret['source'])
    if not isinstance(target,str):raise ValueError('Exact own secret target required')
    if re.fullmatch(r'[A-Za-z0-9_-]+',target):return '/run/secrets/'+target
    if re.fullmatch(r'/run/secrets/[A-Za-z0-9_-]+',target):return target
    raise ValueError('Exact own secret target required')


def memory_bytes(value):
    if type(value) is int and value>0:return value
    if isinstance(value,str) and re.fullmatch(r'[1-9][0-9]*',value):return int(value)
    raise ValueError('Exact positive decimal memory bytes required')


def promoted_container_gate(actual, service, configured, expected, config, accepted_source):
    state=actual['State'];host=actual['HostConfig'];labels=actual['Config'].get('Labels') or {}
    if (actual['Image']!=expected or labels.get('com.docker.compose.project')!='model-generator-mvp'
            or labels.get('com.docker.compose.service')!=service or not state['Running']
            or state.get('Pid',0)<=0 or state.get('Paused') or state.get('Restarting')):
        raise ValueError('Final running runtime image/project/state mismatch')
    if configured.get('healthcheck') and state.get('Health',{}).get('Status')!='healthy':
        raise ValueError('Final runtime health mismatch')
    if (not host['ReadonlyRootfs'] or 'ALL' not in host.get('CapDrop',[])
            or 'no-new-privileges:true' not in host.get('SecurityOpt',[]) or host.get('Privileged')
            or host.get('PortBindings') or host.get('PublishAllPorts') or host.get('CapAdd')
            or host.get('Devices') or host.get('DeviceRequests') or host.get('PidMode')
            or host['PidsLimit']!=configured['pids_limit'] or host['Memory']!=memory_bytes(configured['mem_limit'])
            or host['NanoCpus']!=round(float(configured['cpus'])*10**9)
            or actual['Config']['User']!=configured['user']):
        raise ValueError('Final runtime hardening mismatch')
    expected_networks={'none'} if service=='scratch-keeper' else {config['networks'][name]['name'] for name in configured.get('networks',{})}
    if set(actual['NetworkSettings']['Networks'])!=expected_networks:
        raise ValueError('Final runtime network mismatch')
    if service=='scratch-keeper':
        if host['RestartPolicy']['Name'] not in ('no',configured['restart']):raise ValueError('Keeper restart policy mismatch')
        keeper_gate(actual,expected,'model-generator-mvp')
    elif host['RestartPolicy']['Name']!=configured['restart']:
        raise ValueError('Final runtime restart policy mismatch')
    if service in ('api','worker') and labels.get('org.opencontainers.image.revision')!=accepted_source:
        raise ValueError('Final runtime source mismatch')
    # Every configured volume and secret must be the accepted own mount; no extras.
    mounts={}
    for volume in configured.get('volumes',[]):
        source=volume['source'];kind=volume['type']
        if kind=='volume':source=config['volumes'][source]['name']
        mounts[volume['target']]=(kind,source,not volume.get('read_only',False))
    for secret in configured.get('secrets',[]):
        mounts[secret_destination(secret)]=('bind',config['secrets'][secret['source']]['file'],False)
    observed={m['Destination']:(m['Type'],m['Name'] if m['Type']=='volume' else m['Source'],m['RW']) for m in actual['Mounts'] if m['Type']!='tmpfs'}
    if observed!=mounts:raise ValueError('Final runtime mount mismatch')


def command_argv(value):
    # Docker may serialize a resolved empty argv as null or []; inheritance is
    # resolved first, so configured null never erases an image command.
    if value is None or value==[] or value=='':return None
    if not isinstance(value,list) or any(not isinstance(v,str) for v in value):
        raise ValueError('Normalized exact command argv required')
    return value


def effective_command(configured,image):
    override=configured.get('entrypoint') is not None
    entrypoint=configured['entrypoint'] if override else image.get('Entrypoint')
    command=configured.get('command')
    if command is None:command=None if override else image.get('Cmd')
    return command_argv(entrypoint),command_argv(command)


def final_runtime_gate(compose,manifest,config,accepted_source,strict=False):
    ids=subprocess.check_output(['docker','ps','-a','-q','--filter','label=com.docker.compose.project=model-generator-mvp'],text=True).split()
    actuals=[cold_operator.inspect(id) for id in ids]
    names=[(a['Config'].get('Labels') or {}).get('com.docker.compose.service') for a in actuals]
    if strict and (len(names)!=len(FINAL_ROLES) or set(names)!=set(FINAL_ROLES)):
        raise ValueError('Complete exact promoted runtime inventory required')
    result={}
    for service,role in FINAL_ROLES.items():
        matches=[a for a,n in zip(actuals,names) if n==service]
        if len(matches)!=1:raise ValueError('Expected one own runtime container')
        actual=matches[0];image=manifest['images'][role]['id'] if role else config['services'][service]['image']
        info=json.loads(subprocess.check_output(['docker','image','inspect',image]))[0]
        if info['Architecture']!='amd64' or info['Os']!='linux':raise ValueError('Final runtime platform mismatch')
        expected=info['Id']
        if strict:
            configured=config['services'][service]
            entrypoint,command=effective_command(configured,info['Config'])
            if command_argv(actual['Config'].get('Entrypoint'))!=entrypoint or command_argv(actual['Config'].get('Cmd'))!=command:
                raise ValueError('Final runtime command mismatch')
        if strict:promoted_container_gate(actual,service,config['services'][service],expected,config,accepted_source)
        elif actual['Image']!=expected or not actual['State']['Running'] or actual['State'].get('Pid',0)<=0:
            raise ValueError('Final running runtime image mismatch')
        result[service]=actual
    return result


def finalize_promoted(compose,manifest,config,accepted_source):
    inventory=final_runtime_gate(compose,manifest,config,accepted_source,strict=True)
    holders=cold_operator.holders('model-generator-mvp',manifest['images'],keeper_gate)
    if {a['Id'] for a in holders.values()}!={inventory[n]['Id'] for n in ('api','worker','scratch-keeper')}:
        raise ValueError('Physical scratch holders mismatch')
    subprocess.run(compose+['exec','-T','api','python','-c',READONLY_PROMOTED_PROBE],check=True,capture_output=True,timeout=8)
    subprocess.run(compose+['exec','-T','api','python','scripts/web-healthcheck.py','--url','http://localhost:8000/health/ready'],check=True,capture_output=True,timeout=8)
    # Recheck exact physical inventory immediately before the single allowed mutation.
    current=final_runtime_gate(compose,manifest,config,accepted_source,strict=True)
    if {n:a['Id'] for n,a in current.items()}!={n:a['Id'] for n,a in inventory.items()}:
        raise ValueError('Promoted inventory changed during verification')
    subprocess.run(['docker','update','--restart='+config['services']['scratch-keeper']['restart'],current['scratch-keeper']['Id']],check=True,capture_output=True)
    print('Accepted already-promoted runtime finalized; public route unchanged')


def promote(root, directory, accepted_source, export_hash, check_only=False, prepare_cold=False, cold_identity=None, maintenance_confirmed=False, previous=None, finalize=False):
    with cold_operator.operator_lock(root):
        return _promote_locked(root,directory,accepted_source,export_hash,check_only,prepare_cold,cold_identity,maintenance_confirmed,previous,finalize)


def _promote_locked(root, directory, accepted_source, export_hash, check_only=False, prepare_cold=False, cold_identity=None, maintenance_confirmed=False, previous=None, finalize=False):
    manifest = evidence_gate(directory, accepted_source)
    configuration_gate(root, directory, accepted_source, export_hash)
    if previous is not None:previous_binding_gate(directory,previous)
    env = dict(line.split('=', 1) for line in (root / 'production.env').read_text().splitlines() if line and not line.startswith('#'))
    for name in ('api', 'worker', 'proxy', 'backup', 'ingress', 'keeper'):
        expected = manifest['images'][name]['id']
        actual = json.loads(subprocess.check_output(['docker', 'image', 'inspect', expected]))[0]
        if actual['Id'] != expected or actual['Architecture'] != 'amd64' or actual['Os'] != 'linux' or env['MG_' + name.upper() + '_IMAGE'] != expected:
            raise ValueError('Production image identity/platform mismatch')
        if name in ('api','worker') and (actual['Config'].get('Labels') or {}).get('org.opencontainers.image.revision')!=accepted_source:
            raise ValueError('New runtime OCI source label mismatch')
    if env['MG_PUBLIC_ORIGIN'] != 'https://model.axisconsult.ru' or env['MG_TRUSTED_PROXY_IPS'] != '10.251.61.3':
        raise ValueError('Unexpected production origin or trusted ingress')
    caddy = json.loads(subprocess.check_output(['docker', 'inspect', 'caddy']))[0]
    if any(name.startswith('model-generator') for name in caddy['NetworkSettings']['Networks']):
        raise ValueError('Shared Caddy must not join model networks')
    compose = ['docker', 'compose', '--env-file', str(root / 'production.env'), '-p', 'model-generator-mvp', '--profile', 'backup']
    for name in ('compose.yaml', 'compose.ingress-unix.yaml', 'compose.backup.yaml', 'networks.yaml'):
        compose += ['-f', str(root / name)]
    config = json.loads(subprocess.check_output(compose + ['config', '--format', 'json']))
    baseline = json.loads(subprocess.check_output(compose[:-2] + ['config', '--format', 'json']))
    def strip_networks(value):
        value = json.loads(json.dumps(value))
        value.pop('networks', None)
        for service in value['services'].values():
            service.pop('networks', None)
        return value
    if strip_networks(config) != strip_networks(baseline):
        raise ValueError('Network overlay changed non-network production configuration')
    topology_gate(config)
    socket_root = Path(env['MG_SOCKET_ROOT']).resolve(strict=True)
    allowed_parent = next(Path(m['Source']) for m in caddy['Mounts'] if m['Destination'] == '/etc/caddy/Caddyfile.d')
    if socket_root != allowed_parent / 'model-generator-ingress' or socket_root.stat().st_uid != 10001 or socket_root.stat().st_mode & 0o777 != 0o700:
        raise ValueError('Ingress socket directory ownership or scope invalid')
    for key in ('MG_SECRET_ROOT', 'MG_BACKUP_SECRET_ROOT'):
        if not Path(env[key]).resolve(strict=True).is_relative_to(root):
            raise ValueError('Secrets must belong to own deployment')
    if set(caddy['NetworkSettings']['Networks']) & {v['name'] for v in config['networks'].values()}:
        raise ValueError('Shared Caddy network overlap')
    existing = subprocess.check_output(compose + ['ps', '-a', '-q', 'scratch-keeper'], text=True).strip()
    if existing:
        actual = json.loads(subprocess.check_output(['docker', 'inspect', existing]))[0]
        keeper_gate(actual, manifest['images']['keeper']['id'], 'model-generator-mvp')
    if finalize:
        if check_only or prepare_cold or cold_identity is not None or previous is not None:raise ValueError('Finalization is a separate promoted-only phase')
        cold_operator.maintenance_gate(env['MG_PUBLIC_ORIGIN'],maintenance_confirmed)
        return finalize_promoted(compose,manifest,config,accepted_source)
    if check_only:
        print('Read-only release configuration, evidence and isolation gates passed')
        return
    if previous is not None and (cold_identity is None or cold_identity[2] is not None or prepare_cold):
        raise ValueError('Previous attestation requires explicit legacy expected identity phase')
    if prepare_cold or cold_identity is not None:
        cold_operator.maintenance_gate(env['MG_PUBLIC_ORIGIN'],maintenance_confirmed)
    if prepare_cold:
        cold_operator.prepare(compose,'model-generator-mvp',manifest['images'],keeper_gate)
        print('Old own scratch holders physically stopped; fresh stable keeper holds replacement tmpfs; public 503 preserved')
        return
    if cold_identity is not None:
        if cold_identity[2] is None and previous is None:
            raise ValueError('Legacy transition requires separately attested offline rollout')
        inventory=cold_operator.holders('model-generator-mvp',manifest['images'],keeper_gate) if previous is None else {}
        if any(not cold_operator.stopped(v) for v in inventory.values() if v['Config']['Labels']['com.docker.compose.service']!='scratch-keeper'):
            raise ValueError('Physical writer fence required before migration')
    if previous is None:
        subprocess.run(compose + ['up', '-d', '--wait', '--no-recreate', 'scratch-keeper'], check=True)
        subprocess.run(compose + ['up', '-d', '--wait', 'postgres', 's3-proxy'], check=True)
    def migrate():
        subprocess.run(['docker', 'run', '--rm', '--network', 'model-generator-mvp_mg_db', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '10001:10001', '--memory', '256m', '--cpus', '.5', '--pids-limit', '32', '-v', str(root / 'secrets/runtime/migrator_dsn') + ':/run/secrets/database:ro', '-e', 'MG_MIGRATION_DATABASE_URL_FILE=/run/secrets/database', '--entrypoint', 'python', manifest['images']['api']['id'], '-m', 'model_generator.web.migrate'], check=True)
    if previous is not None:
        legacy_operator.transition(compose,root,'model-generator-mvp',manifest['images'],previous,cold_identity,cold_operator,keeper_gate,migrate,lambda:subprocess.run(compose+['up','-d','--wait','postgres','s3-proxy'],check=True,capture_output=True))
        subprocess.run(compose+['up','-d','--wait','postgres','s3-proxy'],check=True,capture_output=True)
    else:
        preflight="""import psycopg
from pathlib import Path
with psycopg.connect(Path('/run/secrets/database').read_text()) as c:
    if c.execute("SELECT to_regclass('mg.api_lock_identity')").fetchone()[0]:
        has_generation=c.execute("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='mg' AND table_name='api_lock_identity' AND column_name='generation')").fetchone()[0]
        query='SELECT EXISTS(SELECT 1 FROM mg.api_lock_identity'+(' WHERE generation IS NULL' if has_generation else '')+')'
        if c.execute(query).fetchone()[0]:raise RuntimeError('Separately attested legacy offline transition required before migration')
"""
        subprocess.run(['docker','run','--rm','--network','model-generator-mvp_mg_db','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','10001:10001','--memory','256m','--cpus','.5','--pids-limit','32','-v',str(root/'secrets/runtime/migrator_dsn')+':/run/secrets/database:ro','--entrypoint','python',manifest['images']['api']['id'],'-c',preflight],check=True,capture_output=True)
        migrate()
        # Ordinary release cannot bypass a legacy identity's explicit offline gate.
        legacy_guard="import psycopg;from pathlib import Path;\nwith psycopg.connect(Path('/run/secrets/database').read_text()) as c:assert not c.execute('SELECT EXISTS(SELECT 1 FROM mg.api_lock_identity WHERE generation IS NULL)').fetchone()[0],'Legacy offline transition required'"
        subprocess.run(['docker','run','--rm','--network','model-generator-mvp_mg_db','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','10001:10001','--memory','256m','--cpus','.5','--pids-limit','32','-v',str(root/'secrets/runtime/migrator_dsn')+':/run/secrets/database:ro','--entrypoint','python',manifest['images']['api']['id'],'-c',legacy_guard],check=True,capture_output=True)
        if cold_identity is not None:
            cold_operator.reset(compose,root,'model-generator-mvp',manifest['images'],cold_identity,keeper_gate)
    replacement=['--force-recreate'] if cold_identity is not None else []
    subprocess.run(compose + ['up', '-d', '--wait'] + replacement + ['api', 'worker', 'ingress', 'backup-proxy'], check=True)
    final_runtime_gate(compose,manifest,config,accepted_source)
    if cold_identity is not None:
        keeper=subprocess.check_output(compose+['ps','-q','scratch-keeper'],text=True).strip()
        keeper_gate(cold_operator.inspect(keeper),manifest['images']['keeper']['id'],'model-generator-mvp')
        subprocess.run(['docker','update','--restart='+config['services']['scratch-keeper']['restart'],keeper],check=True,capture_output=True)
    subprocess.run(compose + ['exec', '-T', 'api', 'python', 'scripts/web-healthcheck.py', '--url', 'http://localhost:8000/health/ready'], check=True)
    print('Accepted exact images promoted into private isolated MVP; public route unchanged')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--accepted-source', required=True)
    parser.add_argument('--deployment-root', type=Path, required=True)
    parser.add_argument('--export-manifest-sha256', required=True)
    parser.add_argument('--promote-authorized', action='store_true')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--finalize-promoted',action='store_true')
    parser.add_argument('--prepare-cold-recovery',action='store_true')
    parser.add_argument('--cold-recovery-identity',help='Explicit expected offline device:inode:generation (legacy NULL denied until attested rollout)')
    parser.add_argument('--public-maintenance-confirmed',action='store_true')
    parser.add_argument('--previous-evidence',type=Path)
    parser.add_argument('--previous-source')
    parser.add_argument('--previous-export-manifest-sha256')
    args = parser.parse_args()
    if args.finalize_promoted and (args.check_only or args.prepare_cold_recovery or args.cold_recovery_identity or args.previous_evidence or args.previous_source or args.previous_export_manifest_sha256 or not args.promote_authorized or not args.public_maintenance_confirmed):
        parser.error('Finalization requires separate authorized already-promoted state under public 503')
    cold_identity=None
    if args.cold_recovery_identity:
        if not re.fullmatch(r'[0-9]+:[0-9]+:([0-9a-f]{32}|legacy)',args.cold_recovery_identity):parser.error('Explicit offline device:inode:generation required')
        device,inode,nonce=args.cold_recovery_identity.split(':')
        cold_identity=(int(device),int(inode),None if nonce=='legacy' else nonce)
    if (args.prepare_cold_recovery or cold_identity is not None) and (args.check_only or not args.promote_authorized or not args.public_maintenance_confirmed):
        parser.error('Cold phase requires promotion authorization and confirmed public 503')
    if args.prepare_cold_recovery and cold_identity is not None:parser.error('Preparation and identity reset are separate explicit phases')
    if not args.promote_authorized and not args.check_only:
        parser.error('Direct user production authorization required')
    previous=None
    if any((args.previous_evidence,args.previous_source,args.previous_export_manifest_sha256)):
        if not all((args.previous_evidence,args.previous_source,args.previous_export_manifest_sha256)) or cold_identity is None or cold_identity[2] is not None or args.prepare_cold_recovery:
            parser.error('Complete separate previous attestation and expected legacy identity required')
        previous=legacy_operator.previous_gate(args.previous_evidence.resolve(strict=True),args.previous_source,args.previous_export_manifest_sha256)
    try:
        promote(args.deployment_root.absolute(), args.evidence.resolve(strict=True), args.accepted_source, args.export_manifest_sha256, args.check_only,args.prepare_cold_recovery,cold_identity,args.public_maintenance_confirmed,previous,args.finalize_promoted)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
        raise SystemExit('Production promotion gate failed; inspect own release log') from None
