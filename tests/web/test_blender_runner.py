"""Actual synthetic child-file checks, never Blender rendering or guard evidence."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from model_generator.web.blender_runner import (
    BlenderResult, _prepare_launch, check_blender_outputs, run_blender_preview)
from model_generator.web.config import Settings
from model_generator.web.preview import PreviewError, PreviewLimits, build_synthetic_demo, write_preview


class BlenderOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings(self.root, 'https://example.invalid', b'0' * 32,
                                 'postgresql://mg_worker:fake@db/model_generator', db_role='mg_worker',
                                 blender_path=Path('/opt/blender/blender'))
        self.input = self.root / 'preview-input.json'
        write_preview(build_synthetic_demo(), self.input, PreviewLimits())
        self.output = self.root / 'staging'
        self.output.mkdir(mode=0o700)

    def child(self, case):
        fixture = Path(__file__).parent / 'fixtures' / 'controlled_child.py'
        result = subprocess.run([sys.executable, str(fixture), '--input', str(self.input),
                                 '--output-dir', str(self.output), '--case', case],
                                env={'PYTHONDONTWRITEBYTECODE': '1'}, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                                close_fds=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def check(self):
        return check_blender_outputs(self.input, self.output, self.settings)

    def reject(self, code=None):
        with self.assertRaises(PreviewError) as raised:
            self.check()
        if code is not None: self.assertEqual(raised.exception.code, code)
        self.assertNotIn(str(self.root), str(raised.exception))

    def test_actual_good_synthetic_protocol_files(self):
        self.child('good')
        result = self.check()
        self.assertIsInstance(result, BlenderResult)
        self.assertEqual((result.version, result.vertex_count, result.triangle_count), ('4.5.14', 3, 1))
        self.assertEqual(result.bounds, build_synthetic_demo()['bounds'])
        self.assertEqual(result.thumbnail_path, self.output / 'thumbnail.png')
        self.assertEqual(result.measurements_path, self.output / 'measurements.json')
        with self.assertRaises(AttributeError): result.version = '4.5.13'

    def test_invalid_actual_measurements_and_png(self):
        cases = ('version', 'vertices', 'triangles', 'bool_count', 'float_count', 'schema',
                 'bool_schema', 'fingerprint', 'reversed_bounds', 'nan_bounds', 'string_bounds',
                 'bool_bounds', 'bounds_fields', 'negative_error', 'nan_error', 'bool_error',
                 'descriptor', 'missing_field', 'duplicate', 'json_oversize', 'invalid_utf8',
                 'json_array', 'json_deep', 'png_dimensions', 'png_signature', 'png_crc',
                 'png_truncated', 'png_oversize', 'extra_output', 'missing_png',
                 'missing_measurements', 'symlink_measurements', 'symlink_png',
                 'hardlink_measurements', 'fifo')
        for case in cases:
            with self.subTest(case=case):
                for path in self.output.iterdir(): path.unlink()
                self.child(case)
                self.reject()

    def test_bounds_and_float32_tolerance_cannot_be_weakened(self):
        for case in ('bounds', 'float32_error'):
            with self.subTest(case=case):
                for path in self.output.iterdir(): path.unlink()
                self.child(case)
                self.reject('preview_roundtrip_error')

    def test_exact_measurement_budget_and_tolerance_boundary(self):
        self.child('json_exact_limit')
        self.assertEqual((self.output / 'measurements.json').stat().st_size, 65536)
        self.check()
        for path in self.output.iterdir(): path.unlink()
        self.child('tolerance_boundary')
        self.check()
        for path in self.output.iterdir(): path.unlink()
        self.child('png_exact_limit')
        self.assertEqual((self.output / 'thumbnail.png').stat().st_size, 4 * 1024**2)
        self.check()

    def test_directory_rejection_does_not_leak_descriptors(self):
        self.child('good')
        self.output.chmod(0o755)
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(20): self.reject()
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_checked_raw_wire_fingerprint_includes_whitespace(self):
        self.child('good')
        original = self.input.read_bytes()
        self.input.write_bytes(original + b' ')
        self.reject()
        measurements = json.loads((self.output / 'measurements.json').read_bytes())
        measurements['fingerprint'] = hashlib.sha256(self.input.read_bytes()).hexdigest()
        (self.output / 'measurements.json').write_text(json.dumps(measurements))
        self.check()

    def test_symlink_ancestors_escape_dotdot_and_root_paths(self):
        self.child('good')
        alias = self.root / 'alias'
        alias.symlink_to(self.output, target_is_directory=True)
        for path in (alias, alias / '..' / 'staging', Path('relative'), self.root):
            with self.subTest(path=str(path)), self.assertRaises(PreviewError):
                check_blender_outputs(self.input, path, self.settings)
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaises(PreviewError):
                check_blender_outputs(self.input, Path(outside), self.settings)
        with self.assertRaises(PreviewError):
            check_blender_outputs(self.input, self.output, replace(self.settings, data_root=alias))

    def test_input_revalidated_without_child_launch(self):
        cases = (b'{}', b'{"schemaVersion":1,"schemaVersion":1}', b'NaN', b'\xff',
                 b' ' * (16 * 1024**2 + 1))
        with patch('subprocess.Popen', side_effect=AssertionError('Production child launched')):
            for wire in cases:
                with self.subTest(size=len(wire)):
                    self.input.write_bytes(wire)
                    with self.assertRaises(PreviewError) as raised:
                        run_blender_preview(self.input, self.output, self.settings, lambda: False)
                    self.assertNotEqual(raised.exception.code, 'preview_runtime_unavailable')

    def test_input_symlinks_hardlinks_and_outside_root_are_rejected(self):
        self.child('good')
        actual = self.root / 'actual.json'
        self.input.rename(actual)
        self.input.symlink_to(actual)
        self.reject()
        self.input.unlink()
        self.input.hardlink_to(actual)
        self.reject()
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / 'preview.json'
            external.write_bytes(actual.read_bytes())
            with self.assertRaises(PreviewError):
                check_blender_outputs(external, self.output, self.settings)

    def test_descriptor_change_during_read_is_rejected(self):
        self.child('good')
        real_read = os.read
        replaced = False
        def swap(descriptor, count):
            nonlocal replaced
            wire = real_read(descriptor, count)
            if not replaced and b'float32MaxErrorMetres' in wire:
                replaced = True
                path = self.output / 'measurements.json'
                path.unlink()
                path.write_bytes(wire)
            return wire
        with patch('model_generator.web.blender_runner.os.read', side_effect=swap):
            self.reject()

    def test_invalid_configured_budgets_fail_closed(self):
        for name, values in {'blender_cpu_seconds': (0, 61, True, 1.5),
                             'blender_wall_seconds': (0, 91, float('nan')),
                             'blender_memory_bytes': (0, 1024**3 + 1, True),
                             'preview_max_bytes': (0, 16 * 1024**2 + 1)}.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(PreviewError):
                    run_blender_preview(self.input, self.output, replace(self.settings, **{name: value}), lambda: False)

    def test_cancel_and_nonempty_staging_refuse_launch(self):
        with self.assertRaises(PreviewError) as raised:
            run_blender_preview(self.input, self.output, self.settings, lambda: True)
        self.assertEqual(raised.exception.code, 'preview_cancelled')
        (self.output / 'untrusted.txt').write_text('anything')
        with self.assertRaises(PreviewError) as raised:
            run_blender_preview(self.input, self.output, self.settings, lambda: False)
        self.assertEqual(raised.exception.code, 'preview_unsupported')

    def test_fixed_command_minimal_environment_and_guard_refusal(self):
        with patch.dict(os.environ, {'MG_AUTH_KEY': 'fake-secret', 'DATABASE_URL': 'fake-db',
                                    'AWS_SECRET_ACCESS_KEY': 'fake-s3', 'PYTHONPATH': '/untrusted'}):
            launch = _prepare_launch(self.input, self.output, self.settings)
        expected_script = Path(__file__).resolve().parents[2] / 'workers' / 'blender_preview.py'
        self.assertEqual(launch.argv, ('/opt/blender/blender', '--background', '--factory-startup',
                                      '--disable-autoexec', '--threads', '1', '--python', str(expected_script),
                                      '--', '--input', str(self.input), '--output-dir', str(self.output)))
        self.assertNotIn('fake-secret', str(launch.environment))
        self.assertNotIn('PYTHONPATH', dict(launch.environment))
        self.assertEqual((launch.cpu_seconds, launch.wall_seconds, launch.memory_bytes), (60, 90, 1024**3))
        with patch('subprocess.Popen', side_effect=AssertionError('Production child launched')):
            with self.assertRaises(PreviewError) as raised:
                run_blender_preview(self.input, self.output, self.settings, lambda: False)
        self.assertEqual(raised.exception.code, 'preview_runtime_unavailable')
        self.assertEqual(list(self.output.iterdir()), [])

    def test_runtime_identity_requires_pinned_actual_binary_and_script(self):
        from model_generator.web.blender_runner import runtime_identity
        with self.assertRaises(PreviewError) as raised:
            runtime_identity(self.settings)
        self.assertEqual(raised.exception.code,'preview_runtime_unavailable')
        binary=self.root/'fake-blender'
        binary.write_bytes(b'Not the pinned official executable')
        binary.chmod(0o555)
        with self.assertRaises(PreviewError):
            runtime_identity(replace(self.settings,blender_path=binary))

    def test_actual_preflight_missing_runtime_never_verifies(self):
        from model_generator.web.blender_runner import preflight_blender
        with self.assertRaises(PreviewError) as raised:
            preflight_blender(replace(self.settings,blender_path=None))
        self.assertEqual(raised.exception.code,'preview_runtime_unavailable')
        self.assertEqual(list(self.root.glob('preflight/*')),[])


if __name__ == '__main__': unittest.main()
