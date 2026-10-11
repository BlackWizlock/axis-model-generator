"""Neutral inputs must reach the bounded reader, never bypass validation."""
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from model_generator.web.validation_child import ChildSettings, validate_input


class NeutralInputTests(unittest.TestCase):
    def test_new_input_failures_have_registered_help_with_preserved_categories(self):
        from model_generator.web.diagnostic_catalog import ERROR_CODES,error_help
        from model_generator.web.security import ApiError,error_response
        for code,status,category,retryable in (('engine_unavailable',422,'input',False),
                ('input_descriptor_mismatch',500,'service',True)):
            with self.subTest(code=code):
                self.assertIn(code,ERROR_CODES)
                help=error_help(code,status)
                self.assertEqual(help['category'],category); self.assertEqual(help['retryable'],retryable)
                self.assertTrue(help['nextAction'])
                response=error_response(ApiError(code,'Input is unavailable.',status),'a'*32)
                self.assertEqual(response.status_code,status)
                error=json.loads(response.body)['error']
                self.assertEqual(error['code'],code); self.assertEqual(error['category'],category)
                self.assertEqual(error['retryable'],retryable)

    def test_cli_rejects_unknown_version_and_arbitrary_input_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'input.bin').write_bytes(b'not-a-zip')
            for extra in (['--descriptor-version','2'],['--input',str(root/'input.bin')]):
                with self.subTest(extra=extra):
                    result=subprocess.run([sys.executable,'-m','model_generator.web.validation_child',
                        '--kind','zip-fbx','--scratch',directory,*extra],stdin=subprocess.DEVNULL,
                        capture_output=True,timeout=10)
                    self.assertEqual(result.returncode,2)
                    self.assertFalse((root/'report.json').exists())

    def test_corrupt_neutral_file_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'input.bin'
            path.write_bytes(b'not-a-zip')
            result = validate_input(path, 'zip-fbx', ChildSettings(scratch=root))
            self.assertNotEqual(result.report['coverage']['technical'], 'passed')
            self.assertTrue(any(finding['status'] == 'fail' for finding in result.report['findings']))
            self.assertEqual(result.report['input_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_standalone_fbx_uses_descriptor_transport_and_has_no_preview(self):
        from fixtures.builders import scene_bytes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('input.bin', 'input.zip'):
                (root / name).write_bytes(scene_bytes(axes=True))
            result = validate_input(root / 'input.bin', 'fbx', ChildSettings(scratch=root))
            self.assertIsNone(result.preview_input_path)
            self.assertEqual(result.failure_code, 'preview_unsupported')
            states = {finding['rule_id']: finding['status'] for finding in result.report['findings']}
            self.assertEqual(states['fbx.scene'], 'pass')
            self.assertEqual(states['fbx.integrity'], 'pass')
            self.assertNotIn('package.fbx_count', states)
            with self.assertRaisesRegex(ValueError, 'Unsafe child input'):
                validate_input(root / 'input.zip', 'fbx', ChildSettings(scratch=root))

    def test_corrupt_standalone_fbx_is_not_success(self):
        from fixtures.builders import scene_bytes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'input.bin').write_bytes(scene_bytes(axes=True)[:60])
            result = validate_input(root / 'input.bin', 'fbx', ChildSettings(scratch=root))
            self.assertTrue(any(finding['status'] == 'fail' for finding in result.report['findings']))
            self.assertEqual(result.report['files'][0]['read_status'], 'failed')
