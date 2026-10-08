"""Mandatory actual Linux guard capability gate; unsupported is a failed test."""
import json
import tempfile
from pathlib import Path
import subprocess
import sys
import unittest


class GuardCapabilityTests(unittest.TestCase):
    def test_neutral_symlink_outside_and_arbitrary_name_cannot_reach_reader(self):
        from unittest.mock import patch
        from model_generator.web.validation_child import validate_input, ChildSettings
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); scratch=root/'scratch'; scratch.mkdir()
            outside=root/'input.bin'; outside.write_bytes(b'not-a-zip')
            link=scratch/'input.bin'; link.symlink_to(outside)
            other=scratch/'user-file.zip'; other.write_bytes(b'not-a-zip')
            for path in (link,outside,other):
                with self.subTest(path=path),patch('model_generator.web.validation_child.validate_path',side_effect=AssertionError('Unsafe input reached reader')):
                    with self.assertRaisesRegex(ValueError,'Unsafe child input'):
                        validate_input(path,'zip-fbx',ChildSettings(scratch=scratch))

    def test_neutral_guard_denies_real_network_and_host_reads(self):
        from model_generator.web.worker import run_child
        from web.helpers import settings_for
        with tempfile.TemporaryDirectory() as tmp:
            scratch=Path(tmp); (scratch/'input.bin').write_bytes(b'not-a-zip')
            result,code=run_child(settings_for(scratch),scratch,'zip-fbx',lambda:False,
                                  descriptor_version=1,probe='guard')
            self.assertIsNone(code,result)
            proof=json.loads((scratch/'guard.json').read_text())
            self.assertTrue(proof['ownInput'])
            self.assertTrue({'socket','connect','mount','parent','parentfd','parentroot','parentmem'}.issubset(proof['denied']))

    def test_required_controls_install_and_deny_real_access(self):
        probe = Path(__file__).parent / 'fixtures' / 'guard_probe.py'
        result = subprocess.run([sys.executable, str(probe), '--capabilities'],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=35, close_fds=True)
        evidence = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, evidence)
        self.assertTrue(evidence['ok'], evidence)
        self.assertEqual(evidence['platform'], 'Linux')
        self.assertEqual({item['control'] for item in evidence['controls']},
                         {'pdeathsig', 'seccomp', 'landlock'})


if __name__ == '__main__':
    unittest.main()
