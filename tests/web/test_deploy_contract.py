"""Required production topology is checked through Docker Compose itself."""
import json
import subprocess
import unittest
import shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
class DeployContract(unittest.TestCase):
    def test_postgres_initialization_keeps_passwords_out_of_arguments(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('admin','backup','api','worker','migrator'):
                (root/name).write_text('synthetic-secret-'+name)
            script=(ROOT/'deploy/web/init-db.sh').read_text().replace('/run/secrets/',str(root)+'/')
            # A shell function observes the actual psql invocation, including stdin.
            prefix='''psql() {
  python3 -c 'import json,os,sys;print(json.dumps({"argv":sys.argv[1:],"sql":sys.stdin.read(),"passwords":[os.environ[k] for k in ("PGPASSWORD","MG_INIT_BACKUP_PASSWORD","MG_INIT_API_PASSWORD","MG_INIT_WORKER_PASSWORD","MG_INIT_MIGRATOR_PASSWORD")]}))' "$@"
}
'''
            result=subprocess.run(['bash','-c',prefix+script],text=True,capture_output=True,env=os.environ)
            self.assertEqual(result.returncode,0,result.stderr)
            invocation=json.loads(result.stdout)
            self.assertEqual(len(invocation['passwords']),5)
            for secret in invocation['passwords']:
                self.assertNotIn(secret,' '.join(invocation['argv']))
                self.assertNotIn(secret,invocation['sql'])
            self.assertEqual(invocation['argv'],['-v','ON_ERROR_STOP=1','--username','postgres','--dbname','postgres'])

    def test_compose_isolated_and_bounded(self):
        if not shutil.which('docker'):
            text=(ROOT/'deploy/web/compose.yaml').read_text()
            self.assertNotIn('ports:',text)
            self.assertEqual(text.count('internal: true'),3)
            self.assertIn('size=4294967296',text)
            return
        result=subprocess.run(['docker','compose','--env-file','deploy/web/.env.example','-f','deploy/web/compose.yaml','config','--format','json'],cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        doc=json.loads(result.stdout)
        for name in ('mg_edge','mg_db','mg_files'): self.assertTrue(doc['networks'][name]['internal'])
        self.assertNotIn('caddy',doc['services'])
        expected={'api':{'mg_edge','mg_db','mg_files'},'worker':{'mg_db','mg_files'},'postgres':{'mg_db'},'s3-proxy':{'mg_files','mg_egress'}}
        for name,nets in expected.items():
            service=doc['services'][name]
            self.assertEqual(set(service['networks']),nets)
            self.assertFalse(service.get('ports'))
            self.assertTrue(service['read_only'])
            self.assertIn('ALL',service['cap_drop'])
            self.assertEqual(service['pids_limit'],64)
            self.assertTrue(service['user'])
            for mount in service.get('tmpfs',[]):self.assertTrue(mount.startswith('/'),mount)
            self.assertEqual(float(service['cpus']), {'api':1,'worker':1,'postgres':0.5,'s3-proxy':0.25}[name])
        self.assertNotIn('axis_private',result.stdout)
        self.assertNotIn('docker.sock',result.stdout)
    def test_manifest_locks_match(self):
        import hashlib
        manifest=json.loads((ROOT/'deploy/web/runtime-manifest.json').read_text())
        self.assertEqual(manifest['previewBackend'],'python-cpu')
        for path,digest in manifest['locks'].items(): self.assertEqual(hashlib.sha256((ROOT/path).read_bytes()).hexdigest(),digest)

    def test_proxy_rejects_metadata_private_and_ports(self):
        import importlib.util,threading,http.client
        path=ROOT/'deploy/web/s3-proxy.py'
        spec=importlib.util.spec_from_file_location('proxy',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        server=module.Server(('127.0.0.1',0),module.Proxy)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            for target in ('169.254.169.254:80','127.0.0.1:443','postgres:5432','storage.yandexcloud.net:80','evil.example:443'):
                client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                client.request('CONNECT',target)
                self.assertEqual(client.getresponse().status,403)
                client.close()
        finally:
            server.shutdown();server.server_close();thread.join(3)
