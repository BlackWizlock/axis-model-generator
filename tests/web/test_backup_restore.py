"""Backup manifests reject expiry, hash/schema ambiguity before restore."""
import importlib.util
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('restore',ROOT/'scripts/restore-web-db.py')
restore=importlib.util.module_from_spec(spec);spec.loader.exec_module(restore)
class BackupManifest(unittest.TestCase):
    def sample(self):
        return {'schema':1,'key':'database/100/database.dump','format':'postgres-custom','database':'model_generator','createdAt':100,'expiresAt':100+7*86400,'bytes':42,'sha256':'a'*64}
    def test_valid(self):restore.validate_manifest(self.sample(),'database/100/database.dump',101)
    def test_expired_and_corrupt(self):
        for change in ({'expiresAt':101},{'sha256':'G'*64},{'bytes':0},{'schema':2},{'database':'axis'},{'key':'other'},{'createdAt':True}):
            with self.subTest(change=change):
                value=self.sample();value.update(change)
                with self.assertRaises(ValueError):restore.validate_manifest(value,'database/100/database.dump',101)
