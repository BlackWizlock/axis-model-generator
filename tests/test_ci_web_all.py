"""Core selection and aggregate fail-closed boundaries without Docker mutation."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
class AllContracts(unittest.TestCase):
    def test_core_command_does_not_discover_required_web_suites(self):
        pipeline=load('pipeline','scripts/dev-pipeline.py')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tests/web').mkdir(parents=True)
            (root/'tests/test_core.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_core(self):self.assertTrue(True)\n')
            (root/'tests/web/__init__.py').write_text('')
            (root/'tests/web/test_web.py').write_text("raise RuntimeError('Core runner must not import nested web')")
            result=subprocess.run([sys.executable,*pipeline.TEST_COMMAND[1:]],cwd=root,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('Ran 1 test',result.stderr)
    def test_network_override_requires_owned_scratch_and_rejects_symlink(self):
        module=load('all_runner','scripts/ci-web-all.py')
        with tempfile.TemporaryDirectory() as directory:
            module.ROOT=Path(directory);scratch=module.ROOT/'_scratch';scratch.mkdir()
            file=scratch/'own.yaml';file.write_text('networks: {}')
            self.assertEqual(module.owned_overlay(str(file)),file.resolve())
            outside=module.ROOT/'other.yaml';outside.write_text('networks: {}')
            with self.assertRaises(ValueError):module.owned_overlay(str(outside))
            link=scratch/'link.yaml';link.symlink_to(file)
            with self.assertRaises(ValueError):module.owned_overlay(str(link))
