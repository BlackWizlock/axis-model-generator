import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from model_generator.dwg_convert import dwg_to_dxf
from model_generator.limits import Limits, ReadError

DWG = b'AC1032' + b'\x00' * 64


def program(folder, body):
    # Run through the shell: the test stand mounts its temporary folder noexec.
    path = Path(folder) / 'converter'
    path.write_text(body + '\n')
    return ('/bin/sh', str(path))


class ConvertTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.folder = holder.name

    def rule(self, data=DWG, **options):
        with self.assertRaises(ReadError) as raised:
            dwg_to_dxf(data, **options)
        return raised.exception.rule

    def test_converter_output_is_returned(self):
        tool = program(self.folder, 'cp "$4" "$3"')
        self.assertEqual(dwg_to_dxf(DWG, program=tool), DWG)

    def test_only_known_dwg_versions_reach_the_converter(self):
        tool = program(self.folder, 'cp "$4" "$3"')
        for data in (b'', b'AC1009' + b'\x00' * 8, b'  0\nSECTION\n', b'PK\x03\x04'):
            with self.subTest(data=data[:6]):
                self.assertEqual(self.rule(data, program=tool), 'dwg.format')

    def test_failures_and_budgets_are_refused(self):
        self.assertEqual(self.rule(program=program(self.folder, 'exit 3')), 'dwg.convert')
        self.assertEqual(self.rule(program=program(self.folder, 'exit 0')), 'dwg.convert')
        self.assertEqual(self.rule(program=str(Path(self.folder) / 'absent')), 'dwg.converter')
        self.assertEqual(self.rule(program=program(self.folder, 'sleep 5'), timeout=0.2), 'dwg.budget')
        self.assertEqual(self.rule(limits=replace(Limits(), dxf_bytes=10)), 'dwg.budget')
        tool = program(self.folder, 'head -c 4096 /dev/zero > "$3"')
        self.assertEqual(self.rule(program=tool, limits=replace(Limits(), dxf_bytes=1024)), 'dwg.budget')
        tool = program(self.folder, 'ln -s /etc/hostname "$3"')
        self.assertEqual(self.rule(program=tool), 'dwg.convert')


if __name__ == '__main__':
    unittest.main()
