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

    def test_standalone_fbx_uses_descriptor_transport_and_shows_its_reduced_model(self):
        from fixtures.builders import scene_bytes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('input.bin', 'input.zip'):
                (root / name).write_bytes(scene_bytes(axes=True))
            result = validate_input(root / 'input.bin', 'fbx', ChildSettings(scratch=root))
            self.assertEqual(result.preview_input_path, root / 'preview-input.json')
            self.assertIsNone(result.failure_code)
            view = json.loads(result.preview_input_path.read_bytes())
            self.assertEqual((view['provenance'], view['vertexCount'], view['triangleCount']), ('reduced_model', 3, 1))
            states = {finding['rule_id']: finding['status'] for finding in result.report['findings']}
            self.assertEqual(states['fbx.scene'], 'pass')
            self.assertEqual(states['fbx.integrity'], 'pass')
            self.assertNotIn('package.fbx_count', states)
            self.assertEqual(result.model_path, root / 'model.fbx')
            self.assertEqual(result.report['reduced_model']['status'], 'built')
            self.assertEqual(result.report['reduced_model']['output_triangles'], 1)
            with self.assertRaisesRegex(ValueError, 'Unsafe child input'):
                validate_input(root / 'input.zip', 'fbx', ChildSettings(scratch=root))

    def plan_input(self, root, drawing):
        # The converter is a separate program: a stand-in writes the prepared DXF.
        (root / 'input.bin').write_bytes(b'AC1032' + b'\x00' * 64)
        script = root / 'convert.py'
        script.write_text('import sys\nfrom pathlib import Path\n'
                          f'data = bytes.fromhex({drawing.hex()!r})\n'
                          'sys.exit(3) if not data else Path(sys.argv[3]).write_bytes(data)\n')
        return ChildSettings(scratch=root, dwg_converter=[sys.executable, str(script)])

    def test_site_plan_dwg_gives_its_lines_and_a_view(self):
        from test_site_plan import PLAN
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = validate_input(root / 'input.bin', 'dwg', self.plan_input(root, PLAN))
            self.assertEqual(result.report['input_sha256'], hashlib.sha256((root / 'input.bin').read_bytes()).hexdigest())
            self.assertEqual([(f['rule_id'], f['status']) for f in result.report['findings']], [('plan.heights', 'pass')])
            plan = result.report['files'][0]
            self.assertEqual((plan['read_status'], plan['lines'], plan['z_range']), ('read', 2, [131.5, 139.0]))
            self.assertIsNone(result.failure_code)
            self.assertIsNone(result.model_path)
            view = json.loads(result.preview_input_path.read_text())
            self.assertEqual((view['provenance'], view['triangleCount']), ('site_plan', 10))
            # Ribbons have width, so the centre sits within it of the centre of the lines.
            for got, want in zip(view['origin'], (5.0, 5.0, 135.25)): self.assertAlmostEqual(got, want, delta=0.12)
            self.assertNotIn('reduced_model', result.report)
            self.assertEqual([p.name for p in root.iterdir() if p.is_dir()], [])
            with self.assertRaisesRegex(ValueError, 'Unsafe child input'):
                validate_input(root / 'input.zip', 'dwg', ChildSettings(scratch=root))

    def test_site_plan_converted_into_a_broken_file_is_converted_again(self):
        from model_generator.web import validation_child
        from test_site_plan import PLAN
        for broken, rules in ((2, [('plan.heights', 'pass')]), (validation_child.DWG_ATTEMPTS, [('dxf.integrity', 'fail')])):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'input.bin').write_bytes(b'AC1032' + b'\x00' * 64)
                script = root / 'convert.py'
                # The first runs break a text value across lines, as the real converter does.
                script.write_text('import sys\nfrom pathlib import Path\n'
                                  'runs = Path(__file__).with_name("runs")\n'
                                  'n = int(runs.read_text()) if runs.exists() else 0\nruns.write_text(str(n + 1))\n'
                                  f'data = bytes.fromhex({PLAN.hex()!r})\n'
                                  f'Path(sys.argv[3]).write_bytes(b"  1\\nstray\\nbytes\\n" + data if n < {broken} else data)\n')
                result = validate_input(root / 'input.bin', 'dwg', ChildSettings(scratch=root, dwg_converter=[sys.executable, str(script)]))
                self.assertEqual([(f['rule_id'], f['status']) for f in result.report['findings']], rules)
                self.assertEqual(int((root / 'runs').read_text()), min(broken + 1, validation_child.DWG_ATTEMPTS))

    def test_site_plan_that_cannot_be_read_is_reported_not_guessed(self):
        from test_dxf_heights import dxf
        from test_site_plan import polyline
        flat = dxf([polyline(0, [(0, 0), (1, 1)])])
        for drawing, signature, rule in ((b'', b'AC1032', 'dwg.convert'), (flat, b'AC1032', 'plan.heights'),
                                         (flat, b'PK\x03\x04\x00\x00', 'dwg.format')):
            with self.subTest(rule=rule), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = self.plan_input(root, drawing)
                (root / 'input.bin').write_bytes(signature + b'\x00' * 64)
                result = validate_input(root / 'input.bin', 'dwg', settings)
                self.assertEqual([(f['rule_id'], f['status']) for f in result.report['findings']], [(rule, 'fail')])
                self.assertEqual(result.report['files'], [{'file': 'input.dwg', 'read_status': 'failed'}])
                self.assertIsNone(result.preview_input_path)
                self.assertEqual(result.failure_code, 'preview_unsupported')
        from model_generator.web.process_guard import VALIDATION_FILE_BYTES
        with tempfile.TemporaryDirectory() as directory:
            # A drawing larger than a file this process may write is a budget finding, not a failed write.
            root = Path(directory)
            settings = self.plan_input(root, flat)
            (root / 'input.bin').write_bytes(b'AC1032' + b'\x00' * VALIDATION_FILE_BYTES)
            result = validate_input(root / 'input.bin', 'dwg', settings)
            self.assertEqual([f['rule_id'] for f in result.report['findings']], ['dwg.budget'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.plan_input(root, flat)
            result = validate_input(root / 'input.bin', 'dwg', ChildSettings(scratch=root, dwg_converter=str(root / 'absent')))
            self.assertEqual([f['rule_id'] for f in result.report['findings']], ['dwg.converter'])

    def test_model_that_outlasts_its_time_keeps_the_report(self):
        from unittest import mock
        from fixtures.builders import scene_bytes
        from model_generator.web import validation_child
        build = validation_child.reduce_input
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                validation_child, 'reduce_input', lambda *args: build(*args, cpu_seconds=-1.0)):
            root = Path(directory)
            (root / 'input.bin').write_bytes(scene_bytes(axes=True))
            result = validate_input(root / 'input.bin', 'fbx', ChildSettings(scratch=root))
            states = {finding['rule_id']: finding['status'] for finding in result.report['findings']}
            self.assertEqual(states['fbx.scene'], 'pass')
            self.assertIsNone(result.model_path)
            self.assertFalse((root / 'model.fbx').exists())
            self.assertEqual(result.report['reduced_model'], {'status': 'not_built', 'reason': 'model_budget'})
            self.assertIsNone(result.preview_input_path)
            self.assertEqual(result.failure_code, 'preview_budget')

    def test_view_gives_way_when_processor_time_is_spent(self):
        from unittest import mock
        from fixtures.builders import scene_bytes
        from model_generator.web import validation_child
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(validation_child, '_view_time_left', return_value=False):
            root = Path(directory)
            (root / 'input.bin').write_bytes(scene_bytes(axes=True))
            result = validate_input(root / 'input.bin', 'fbx', ChildSettings(scratch=root))
            self.assertEqual(result.model_path, root / 'model.fbx')
            self.assertIsNone(result.preview_input_path)
            self.assertFalse((root / 'preview-input.json').exists())
            self.assertEqual(result.failure_code, 'preview_budget')

    def test_view_that_would_outlast_the_process_is_given_up_with_the_report_kept(self):
        from unittest import mock
        from fixtures.builders import scene_bytes
        from model_generator.web import validation_child
        def endless(*_):
            while True:
                pass
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(validation_child, 'build_mesh_preview', endless), \
                mock.patch.object(validation_child, 'cpu_left', lambda: validation_child.REPORT_CPU_MARGIN + 0.3):
            root = Path(directory)
            (root / 'input.bin').write_bytes(scene_bytes(axes=True))
            result = validate_input(root / 'input.bin', 'fbx', ChildSettings(scratch=root))
            self.assertEqual(result.model_path, root / 'model.fbx')
            self.assertIsNone(result.preview_input_path)
            self.assertFalse((root / 'preview-input.json').exists())
            self.assertEqual((result.failure_code, result.report['reduced_model']['status']), ('preview_budget', 'built'))

    def test_view_over_its_budget_keeps_the_model_and_the_report(self):
        from fixtures.builders import scene_bytes
        from model_generator.web.preview import PreviewLimits
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'input.bin').write_bytes(scene_bytes(axes=True))
            result = validate_input(root / 'input.bin', 'fbx',
                                    ChildSettings(scratch=root, preview_limits=PreviewLimits(vertices=2)))
            self.assertEqual(result.model_path, root / 'model.fbx')
            self.assertIsNone(result.preview_input_path)
            self.assertEqual(result.failure_code, 'preview_budget')
            self.assertEqual(result.report['reduced_model']['status'], 'built')

    def test_corrupt_standalone_fbx_is_not_success(self):
        from fixtures.builders import scene_bytes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'input.bin').write_bytes(scene_bytes(axes=True)[:60])
            result = validate_input(root / 'input.bin', 'fbx', ChildSettings(scratch=root))
            self.assertTrue(any(finding['status'] == 'fail' for finding in result.report['findings']))
            self.assertEqual(result.report['files'][0]['read_status'], 'failed')
            self.assertIsNone(result.model_path)
            self.assertFalse((root / 'model.fbx').exists())
            self.assertEqual(result.report['reduced_model'], {'status': 'not_built', 'reason': 'model_unsupported'})
