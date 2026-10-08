"""Fixed transport names and exact metadata contracts."""
import unittest
from model_generator.input_descriptor import input_filename, make_descriptor

class DescriptorTests(unittest.TestCase):
    def test_fixed_names(self):
        self.assertEqual(input_filename(0), 'input.zip')
        self.assertEqual(input_filename(1), 'input.bin')
        for version in (True, -1, 2, '../model.rvt'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                input_filename(version)

    def test_descriptor(self):
        self.assertEqual(make_descriptor('zip-fbx', 'модель.zip', 1, 'a' * 64),
            dict(version=1, kind='zip-fbx', displayName='модель.zip', bytes=1, sha256='a' * 64))
        with self.assertRaisesRegex(ValueError, 'engine_unavailable'):
            make_descriptor('rvt', 'модель.rvt', 1, 'a' * 64)

    def test_metadata_validation_and_normalization(self):
        self.assertEqual(make_descriptor('portable-package', 'e\u0301.zip', 1, 'a'*64)['displayName'], 'é.zip')
        for name,size,sha in [('',1,'a'*64), ('x\n',1,'a'*64), ('я'*81,1,'a'*64),
                              ('x',True,'a'*64), ('x',0,'a'*64), ('x',256*1024**2+1,'a'*64),
                              ('x',1,'A'*64), ('x',1,'a'*63), (None,1,'a'*64)]:
            with self.subTest(name=name,size=size), self.assertRaises(ValueError):
                make_descriptor('zip-fbx', name, size, sha)
