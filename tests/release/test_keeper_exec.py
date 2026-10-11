"""The keeper read retries a runtime start failure and nothing else."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('cold',Path(__file__).resolve().parents[2]/'scripts/cold-recovery-web.py')
cold=importlib.util.module_from_spec(spec);spec.loader.exec_module(cold)

OCI='OCI runtime exec failed: exec failed: unable to start container process: procReady not received\n'


class Runner:
    def __init__(self,*results):self.results=list(results);self.calls=0
    def __call__(self,args,**kwargs):
        self.calls+=1;code,out,err=self.results.pop(0)
        if code:raise subprocess.CalledProcessError(code,args,out,err)
        return out


class KeeperExecTests(unittest.TestCase):
    def setUp(self):
        sleeper=patch.object(cold.time,'sleep');self.sleep=sleeper.start();self.addCleanup(sleeper.stop)
    def read(self,run):
        with patch.object(cold.subprocess,'check_output',side_effect=run):return cold.keeper_exec('keeper','code')
    def test_runtime_start_failure_is_retried(self):
        run=Runner((128,OCI,''),(126,'',OCI),(0,'[1, 2, "a"]\n',''))
        self.assertEqual(self.read(run),'[1, 2, "a"]\n')
        self.assertEqual((run.calls,self.sleep.call_count),(3,2))
    def test_retries_are_bounded(self):
        run=Runner((128,OCI,''),(128,OCI,''),(128,OCI,''))
        with self.assertRaises(subprocess.CalledProcessError) as raised:self.read(run)
        self.assertEqual((run.calls,raised.exception.returncode),(3,128))
    def test_failure_of_the_code_itself_is_not_retried(self):
        # A malformed lock generation exits 1; a second read must not hide it.
        run=Runner((1,'','AssertionError: Malformed current lock generation\n'))
        with self.assertRaises(subprocess.CalledProcessError):self.read(run)
        self.assertEqual((run.calls,self.sleep.call_count),(1,0))
    def test_runtime_exit_code_without_its_message_is_not_retried(self):
        run=Runner((128,'','container is not running\n'))
        with self.assertRaises(subprocess.CalledProcessError):self.read(run)
        self.assertEqual(run.calls,1)


if __name__=='__main__':
    unittest.main()
