"""Committed standalone export, local Docker suites and isolated runtime acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
ROOT=Path(__file__).resolve().parents[1]
def run(args,cwd,env,log):
    with log.open('ab') as stream:
        result=subprocess.run(args,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT)
    if result.returncode:raise RuntimeError('Required local gate failed: '+log.name)
def owned_overlay(value):
    path=Path(value)
    resolved=path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_relative_to(ROOT.resolve()/'_scratch') or resolved.suffix not in {'.yaml','.yml'}:raise ValueError('Own scratch network overlay required')
    return resolved

def main():
    platform=os.environ.get('MG_TEST_PLATFORM','linux/amd64')
    if platform not in {'linux/amd64','linux/arm64'}:raise ValueError('Unsupported local platform')
    if not shutil.which('gitleaks'):raise RuntimeError('Required gitleaks unavailable')
    if subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,text=True).strip():raise RuntimeError('Commit tracked changes before standalone all')
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    scratch=ROOT/'_scratch';scratch.mkdir(exist_ok=True)
    evidence=scratch/('web-all-'+uuid.uuid4().hex[:12]);evidence.mkdir(mode=0o700)
    spec=importlib.util.spec_from_file_location('export_public',ROOT/'scripts/export-public.py')
    exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
    policy=os.environ.get('MG_PUBLIC_PRIVATE_POLICY')
    if not policy:
        policy=evidence/'private-policy.json';policy.write_text(json.dumps({'schema_version':1,'denyTokens':['synthetic-private-policy-sentinel-'+uuid.uuid4().hex]}))
    source=evidence/'source';manifest=exporter.build_export(ROOT,source,revision,Path(policy))
    env={key:value for key,value in os.environ.items() if not key.startswith('MG_')}
    env['MG_TEST_PLATFORM']=platform
    run(['gitleaks','dir',str(source),'--redact','--no-banner'],ROOT,env,evidence/'gitleaks.log')
    # Fresh Git metadata is synthetic. Private history is never mounted or copied.
    for args in (['git','init','-q'],['git','config','user.name','Synthetic Source'],['git','config','user.email','synthetic@users.noreply.github.com']):run(args,source,env,evidence/'source-git.log')
    paths=[entry['path'] for entry in manifest['files']]
    run(['git','add','--',*paths],source,env,evidence/'source-git.log')
    run(['git','commit','-qm','sanitized standalone source'],source,env,evidence/'source-git.log')
    (source/'_scratch').mkdir(exist_ok=True)
    if os.environ.get('MG_TEST_COMPOSE_OVERRIDE'):
        original=owned_overlay(os.environ['MG_TEST_COMPOSE_OVERRIDE']);copied=source/'_scratch/test-networks.yaml';shutil.copyfile(original,copied);env['MG_TEST_COMPOSE_OVERRIDE']=str(copied)
    deploy_overlay=source/'_scratch/deploy-networks.yaml'
    if os.environ.get('MG_DEPLOY_NETWORK_OVERLAY'):
        shutil.copyfile(owned_overlay(os.environ['MG_DEPLOY_NETWORK_OVERLAY']),deploy_overlay)
    else:
        deploy_overlay.write_text('networks:\n  mg_egress:\n    internal: true\n')
    # core check is deliberately distinct from PG/S3/runtime tests.
    context=evidence/'core-context';(context/'source').mkdir(parents=True)
    shutil.copytree(source,context/'source',dirs_exist_ok=True,ignore=shutil.ignore_patterns('.git','_scratch'))
    shutil.copyfile(source/'deploy/dev/Dockerfile.check',context/'Dockerfile')
    prefix='axis-model-generator/all-'+evidence.name
    images=[]
    try:
        core=prefix+':core';images.append(core)
        run(['docker','build','--platform',platform,'-t',core,'-f',str(context/'Dockerfile'),str(context)],ROOT,env,evidence/'core-build.log')
        run(['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,noexec,nosuid,size=1g','--memory','2g','--cpus','1',core],ROOT,env,evidence/'core-tests.log')
        run(['bash','scripts/ci-web.sh','--task','diagnostics'],source,env,evidence/'web-tests.log')
        run(['bash','scripts/ci-web.sh','--task','ui'],source,env,evidence/'ui-tests.log')
        tags={}
        for component in ('api','worker','proxy','backup','keeper'):
            tag=prefix+':'+component;images.append(tag);tags[component]=tag
            run(['docker','build','--platform',platform,'-t',tag,'-f','deploy/web/Dockerfile.'+('scratch-keeper' if component=='keeper' else component),'.'],source,env,evidence/(component+'-build.log'))
        emulator=prefix+':emulator';images.append(emulator)
        run(['docker','build','--platform',platform,'-t',emulator,'-f','deploy/web/Dockerfile.s3-test-emulator','.'],source,env,evidence/'emulator-build.log')
        args=['python3','scripts/ci-web-deploy.py','--network-overlay',str(deploy_overlay),'--emulator-image',emulator]
        for component,tag in tags.items():args.extend(['--'+component+'-image',tag])
        run(args,source,env,evidence/'deploy-tests.log')
        result={'status':'passed','source_revision':revision,'platform':platform,'exported_files':len(paths),'private_policy':'provided' if os.environ.get('MG_PUBLIC_PRIVATE_POLICY') else 'synthetic_fixture','coverage':['core','realPG-private-S3','Linux-guards-recovery','frontend','production-runtime','backup-restore','public-export-gitleaks'],'production':'not_deployed','external_gates':['native-amd64','YC-IAM-region','Caddy-TLS-client-sentinel','backup-schedule']}
        (evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    finally:
        for tag in images:
            subprocess.run(['docker','image','rm',tag],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        print('Local evidence: '+str(evidence))
if __name__=='__main__':
    try:main()
    except Exception as error:raise SystemExit(str(error)) from None
