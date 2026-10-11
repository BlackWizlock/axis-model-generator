import unittest
from dataclasses import replace

from model_generator.dxf_heights import read_heights
from model_generator.limits import Limits, ReadError


def dxf(entities, units=6, version='AC1015', codepage='ANSI_1251', header=True):
    """ASCII DXF from (type, [(code, value), ...]) records."""
    lines = []
    if header:
        lines += ['0', 'SECTION', '2', 'HEADER', '9', '$ACADVER', '1', version,
                  '9', '$DWGCODEPAGE', '3', codepage]
        if units is not None:
            lines += ['9', '$INSUNITS', '70', str(units)]
        lines += ['0', 'ENDSEC']
    lines += ['0', 'SECTION', '2', 'ENTITIES']
    for index, (kind, groups) in enumerate(entities):
        lines += ['0', kind, '5', format(index + 16, 'X'), '8', 'relief']
        for code, value in groups:
            lines += [str(code), str(value)]
    lines += ['0', 'ENDSEC', '0', 'EOF']
    return ('\r\n'.join(lines) + '\r\n').encode('cp1251')


def point(x, y, z=0):
    return ('POINT', [(10, x), (20, y), (30, z)])


def text(value, x, y, height=1):
    return ('TEXT', [(10, x), (20, y), (30, 0), (40, height), (1, value)])


class GeometryTests(unittest.TestCase):
    def test_points_with_z_are_samples_in_metres_with_their_origin(self):
        survey = read_heights(dxf([point(1000, 2000, 127500), point(5, 5)], units=4))
        self.assertEqual(survey.mode, 'geometry')
        self.assertEqual(survey.unit_meters, 0.001)
        sample = survey.samples[0]
        self.assertEqual((sample.x, sample.y, sample.z), (1.0, 2.0, 127.5))
        self.assertEqual((sample.source, sample.entity, sample.handle, sample.layer),
                         ('geometry', 'POINT', '10', 'relief'))
        self.assertEqual(survey.counts['flat'], 1)
        self.assertEqual(survey.z_range, (127.5, 127.5))

    def test_lines_faces_and_3d_polylines_keep_their_vertices(self):
        survey = read_heights(dxf([
            ('LINE', [(10, 0), (20, 0), (30, 120), (11, 10), (21, 0), (31, 121)]),
            ('3DFACE', [(10, 0), (20, 0), (30, 120), (11, 4), (21, 0), (31, 120), (12, 4), (22, 4), (32, 121),
                        (13, 4), (23, 4), (33, 121)]),
            ('POLYLINE', [(66, 1), (70, 9), (10, 0), (20, 0), (30, 0)]),
            ('VERTEX', [(10, 0), (20, 0), (30, 125)]), ('VERTEX', [(10, 3), (20, 0), (30, 126)]),
            ('VERTEX', [(10, 3), (20, 3), (30, 127)]), ('SEQEND', [])]))
        self.assertEqual([(l.entity, len(l.points), l.closed, l.source) for l in survey.lines],
                         [('LINE', 2, False, 'geometry'), ('3DFACE', 3, True, 'geometry'),
                          ('POLYLINE', 3, True, 'geometry')])
        self.assertEqual(survey.z_range, (120.0, 127.0))

    def test_partly_flat_geometry_is_not_a_height(self):
        survey = read_heights(dxf([('LINE', [(10, 0), (20, 0), (30, 120), (11, 10), (21, 0), (31, 0)])]))
        self.assertEqual((survey.mode, survey.lines, survey.counts['flat']), ('none', (), 1))


class ElevationTests(unittest.TestCase):
    def test_contour_elevation_is_told_apart_from_geometry(self):
        survey = read_heights(dxf([
            ('LWPOLYLINE', [(90, 3), (70, 1), (38, 131.5), (10, 0), (20, 0), (10, 5), (20, 0), (10, 5), (20, 5)]),
            ('LWPOLYLINE', [(90, 2), (70, 0), (10, 0), (20, 0), (10, 5), (20, 0)]),
            ('POLYLINE', [(66, 1), (70, 0), (10, 0), (20, 0), (30, 132)]),
            ('VERTEX', [(10, 0), (20, 0), (30, 0)]), ('VERTEX', [(10, 9), (20, 0), (30, 0)]), ('SEQEND', [])]))
        self.assertEqual(survey.mode, 'elevation')
        self.assertEqual([(l.source, l.closed, l.points[0][2]) for l in survey.lines],
                         [('elevation', True, 131.5), ('elevation', False, 132.0)])
        self.assertEqual(survey.counts['flat'], 1)

    def test_tilted_planes_meshes_and_paper_space_are_counted_not_read(self):
        survey = read_heights(dxf([
            ('LWPOLYLINE', [(90, 2), (38, 131), (10, 0), (20, 0), (10, 5), (20, 0), (210, 0), (220, 1), (230, 0)]),
            ('POLYLINE', [(66, 1), (70, 64)]), ('VERTEX', [(10, 0), (20, 0), (30, 5)]), ('SEQEND', []),
            ('POINT', [(10, 1), (20, 1), (30, 140), (67, 1)]), ('CIRCLE', [(10, 0), (20, 0), (30, 9), (40, 1)])]))
        self.assertEqual(survey.mode, 'none')
        self.assertEqual((survey.counts['tilted'], survey.counts['unread'], survey.counts['paper_space']), (1, 2, 1))


class LabelTests(unittest.TestCase):
    def test_label_is_a_height_only_for_its_single_survey_point(self):
        survey = read_heights(dxf([point(100, 100), text('127,35', 101, 101),
                                   point(200, 200), text('128.10', 201, 200), text('129.00', 200, 201),
                                   text('130.00', 500, 500), text('ПК 12', 100, 102), text('К-12', 100, 103)]))
        self.assertEqual(survey.mode, 'label')
        self.assertEqual([(s.x, s.y, s.z, s.source, s.entity) for s in survey.samples],
                         [(100.0, 100.0, 127.35, 'label', 'TEXT')])
        self.assertEqual((survey.counts['labels'], survey.counts['labels_unanchored']), (4, 3))

    def test_label_between_two_points_is_ambiguous(self):
        survey = read_heights(dxf([point(100, 100), point(101, 100), text('127.35', 100.5, 100)]))
        self.assertEqual((survey.mode, survey.counts['labels_unanchored']), ('none', 1))

    def test_label_metres_are_not_scaled_by_the_drawing_unit(self):
        survey = read_heights(dxf([point(100000, 100000), text('127.35', 100500, 100000, 500)], units=4))
        self.assertEqual([(s.x, s.y, s.z) for s in survey.samples], [(100.0, 100.0, 127.35)])

    def test_block_attribute_states_the_height_of_its_block(self):
        survey = read_heights(dxf([
            ('INSERT', [(66, 1), (2, 'mark'), (10, 7), (20, 8), (30, 0)]),
            ('ATTRIB', [(10, 7), (20, 8), (30, 0), (40, 1), (1, '141.20'), (2, 'H')]), ('SEQEND', []),
            ('INSERT', [(66, 1), (2, 'mark'), (10, 9), (20, 8), (30, 0)]),
            ('ATTRIB', [(10, 9), (20, 8), (30, 0), (40, 1), (1, '141.20'), (2, 'H')]),
            ('ATTRIB', [(10, 9), (20, 8), (30, 0), (40, 1), (1, '17'), (2, 'N')]), ('SEQEND', [])]))
        self.assertEqual([(s.x, s.y, s.z, s.entity) for s in survey.samples], [(7.0, 8.0, 141.2, 'INSERT')])
        self.assertEqual((survey.counts['blocks'], survey.counts['labels_unanchored']), (2, 2))

    def test_geometry_wins_over_a_label_on_the_same_point(self):
        survey = read_heights(dxf([point(1, 1, 150), text('127.35', 1, 2)]))
        self.assertEqual((survey.mode, [s.z for s in survey.samples]), ('geometry', [150.0]))

    def test_mtext_formatting_and_cyrillic_text_are_decoded(self):
        survey = read_heights(dxf([point(1, 1), ('MTEXT', [(10, 1), (20, 2), (30, 0), (40, 1),
                                                           (1, '{\\fArial|b0;127.35}')]),
                                   text('отм. 5', 9, 9)]))
        self.assertEqual([s.z for s in survey.samples], [127.35])


class UnitAndIntegrityTests(unittest.TestCase):
    def test_unit_is_declared_or_stated_and_never_guessed(self):
        data = dxf([point(1, 1, 5)], units=0)
        with self.assertRaises(ReadError) as raised:
            read_heights(data)
        self.assertEqual(raised.exception.rule, 'dxf.units')
        self.assertEqual(read_heights(data, unit_meters=1.0).samples[0].z, 5.0)
        self.assertEqual(read_heights(dxf([point(1, 1, 5)], header=False), unit_meters=0.001).samples[0].z, 0.005)
        with self.assertRaises(ReadError):
            read_heights(dxf([point(1, 1, 5)], units=4), unit_meters=1.0)
        self.assertEqual(read_heights(dxf([point(1, 1, 5)], units=4), unit_meters=0.001).unit_meters, 0.001)
        for bad in (0, -1, float('nan')):
            with self.subTest(bad=bad), self.assertRaises(ReadError):
                read_heights(data, unit_meters=bad)
        with self.assertRaises(ReadError):
            read_heights(dxf([point(1, 1, 5)], units=3))

    def test_malformed_files_are_refused(self):
        good = dxf([point(1, 1, 5)])
        cases = {'binary': b'AutoCAD Binary DXF\r\n\x1a\x00' + good,
                 'odd': good + b'0\r\n',
                 'no_eof': good.replace(b'0\r\nEOF\r\n', b''),
                 'trailing': good + b'0\r\nPOINT\r\n',
                 'code': good.replace(b'\r\n30\r\n', b'\r\nxx\r\n'),
                 'number': good.replace(b'30\r\n5\r\n', b'30\r\nfive\r\n'),
                 'nan': good.replace(b'30\r\n5\r\n', b'30\r\nnan\r\n'),
                 'no_entities': good.replace(b'ENTITIES', b'OBJECTS'),
                 'repeat': good.replace(b'30\r\n5\r\n', b'30\r\n5\r\n30\r\n6\r\n'),
                 'sequence': dxf([('POLYLINE', [(66, 1), (70, 8)]), ('VERTEX', [(10, 0), (20, 0), (30, 5)])]),
                 'codepage': dxf([point(1, 1, 5)], codepage='ANSI_932')}
        for name, data in cases.items():
            with self.subTest(name=name), self.assertRaises(ReadError):
                read_heights(data)

    def test_budgets_are_enforced(self):
        data = dxf([point(1, 1, 5), point(2, 2, 6)])
        for field, value in (('dxf_bytes', 10), ('dxf_pairs', 5), ('dxf_entities', 1)):
            with self.subTest(field=field), self.assertRaises(ReadError) as raised:
                read_heights(data, replace(Limits(), **{field: value}))
            self.assertEqual(raised.exception.rule, 'dxf.budget')

    def test_utf8_text_of_newer_versions_is_decoded(self):
        data = dxf([point(1, 1), text('127.35', 1, 2)], version='AC1027').decode('cp1251').replace(
            'relief', 'рельеф').encode('utf-8')
        self.assertEqual(read_heights(data).samples[0].layer, 'рельеф')
        # An older version is read in its declared code page, not as UTF-8.
        self.assertNotEqual(read_heights(data.replace(b'AC1027', b'AC1015')).samples[0].layer, 'рельеф')


if __name__ == '__main__':
    unittest.main()
