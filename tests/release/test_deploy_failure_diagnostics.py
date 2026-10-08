"""Failure evidence survives own fixture teardown without synthetic credentials."""
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class DeployFailureDiagnosticsTests(unittest.TestCase):
    def test_failed_start_retains_all_owned_service_logs_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'scripts').mkdir(); (root/'_scratch').mkdir()
            script=root/'scripts'/'ci-web-deploy.py'
            shutil.copyfile(Path(__file__).resolve().parents[2]/'scripts'/'ci-web-deploy.py',script)
            overlay=root/'_scratch'/'network.yaml'; overlay.write_text('networks: {}\n')
            calls=[]
            synthetic='synthetic-password-only-for-regression'
            dsn='postgresql://mg_migrator:'+synthetic+'@postgres/model_generator'
            def command(args,**kwargs):
                calls.append(args)
                if 'scripts/web-test-env.py' in args:
                    runtime=Path(args[-1])
                    (runtime/'migrator_dsn').write_text(dsn)
                    (runtime/'admin').write_text(synthetic)
                    return subprocess.CompletedProcess(args,0,'','')
                if 'logs' in args:
                    self.assertEqual(args[-3:],['api','worker','postgres'])
                    return subprocess.CompletedProcess(args,0,'api '+dsn+'\nworker '+synthetic+'\npostgres stopped','')
                return subprocess.CompletedProcess(args,1 if 'up' in args else 0,'','')
            argv=[str(script),'--network-overlay',str(overlay)]
            for name in ('api','worker','proxy','backup','emulator','keeper'):
                argv.extend(['--'+name+'-image','synthetic:'+name])
            with patch.object(sys,'argv',argv),patch('subprocess.run',side_effect=command):
                with self.assertRaisesRegex(RuntimeError,'redacted diagnostics'):
                    runpy.run_path(str(script),run_name='__main__')
            evidence=root/'_scratch'/'production-failure-redacted.log'
            wire=evidence.read_text()
            self.assertNotIn(synthetic,wire); self.assertNotIn(dsn,wire)
            for value in ('api [REDACTED]','worker [REDACTED]','postgres stopped','exit: 1'):
                self.assertIn(value,wire)
            self.assertEqual(evidence.stat().st_mode & 0o777,0o600)
            self.assertTrue(any('down' in args and '--volumes' in args for args in calls))
            self.assertEqual(sorted(path.name for path in (root/'_scratch').iterdir()),['network.yaml','production-failure-redacted.log'])
