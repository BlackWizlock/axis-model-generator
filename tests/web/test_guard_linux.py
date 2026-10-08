"""Mandatory actual Linux guard capability gate; unsupported is a failed test."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


class GuardCapabilityTests(unittest.TestCase):
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
