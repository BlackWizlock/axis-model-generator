"""Public input capabilities must match accepted runtime adapters."""
import unittest

from model_generator.input_formats import public_formats, require_diagnostics


class InputFormatsTests(unittest.TestCase):
    def test_matrix_and_runtime_gate(self):
        rows = {row['id']: row for row in public_formats()}
        self.assertEqual(set(rows), {'zip-fbx', 'portable-package', 'fbx',
            'ifc', 'glb', 'gltf', 'obj', 'rvt', 'dwg', 'skp', '3dm'})
        for kind, row in rows.items():
            active = kind in {'zip-fbx', 'portable-package'}
            self.assertEqual(row['extensions'], ['.zip' if active else '.' + kind])
            self.assertEqual(row['upload'], active)
            self.assertEqual(row['diagnostics'], active)
            self.assertEqual(row['preview'], kind == 'portable-package')
            self.assertFalse(row['generation'])
            self.assertEqual(row['reason'], None if active else 'engine_unavailable')
            if active:
                require_diagnostics(kind)
            else:
                with self.assertRaisesRegex(ValueError, '^engine_unavailable$'):
                    require_diagnostics(kind)
        with self.assertRaisesRegex(ValueError, '^unsupported_format$'):
            require_diagnostics('blend')

    def test_callers_cannot_mutate_registry(self):
        rows = public_formats()
        rows[0]['diagnostics'] = False
        rows[0]['extensions'].append('.blend')
        rows.pop()
        fresh = public_formats()
        self.assertEqual(len(fresh), 11)
        self.assertTrue(fresh[0]['diagnostics'])
        self.assertEqual(fresh[0]['extensions'], ['.zip'])
        require_diagnostics('zip-fbx')
