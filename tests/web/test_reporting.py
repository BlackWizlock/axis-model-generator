import json
import unittest
from model_generator.web.reporting import sanitize_report


class ReportingTests(unittest.TestCase):
    def test_punctuation_delimited_sources_in_nested_values_and_keys(self):
        sources = ('/private/source.rvt', '//private/source.rvt', '///private/source.rvt', r'C:\private\source.rvt',
                   r'\\server\private\source.rvt', 'https://private.invalid/source.rvt')
        for delimiter in ('[', ']', ',', ';', '{', '}', '|', '!', '?', '+', '@', '#', '(', ':', '='):
            for source in sources:
                with self.subTest(delimiter=delimiter, source=source):
                    value = 'model' + delimiter + source + ']: diagnostic'
                    clean = sanitize_report({'findings': [{'actual': {value: value}, 'expected': value, 'message': value}]})
                    self.assertNotIn(source, json.dumps(clean))
                    self.assertEqual(clean['removed_source_values'], 4)
        relative = ['scene/mesh.fbx', './scene/mesh.fbx', '../scene/mesh.fbx',
                    'model[scene/mesh.fbx]: diagnostic', 'sources,scene/mesh.fbx']
        clean = sanitize_report({'findings': [{'message': value} for value in relative]})
        self.assertEqual([item['message'] for item in clean['findings']], relative)
        self.assertEqual(clean['removed_source_values'], 0)

    def test_real_fbx_transform_warning_redacts_bracketed_source(self):
        from dataclasses import asdict
        from fixtures.builders import scene_bytes
        from model_generator.fbx_binary import parse_fbx
        from model_generator.fbx_inspection import inspect_fbx
        for source in ('/private/source.rvt','//private/source.rvt','///private/source.rvt'):
            with self.subTest(source=source):
                tree = parse_fbx(scene_bytes())
                for node in tree[1].children:
                    if node.name == 'Model': node.props[1] = 'model['+source+']'
                _, findings = inspect_fbx(tree, 'scene/mesh.fbx')
                warning = next(item for item in findings if item.rule_id == 'fbx.transform')
                self.assertIn(source, warning.message)
                clean = sanitize_report({'findings': [asdict(warning)]})
                self.assertNotIn(source, json.dumps(clean))
                self.assertEqual(clean['removed_source_values'], 1)

    def test_multiple_leading_slashes_in_raw_nested_fields_and_keys(self):
        for source in ('//private/source.rvt','///private/source.rvt','//server/share/source.rvt'):
            with self.subTest(source=source):
                clean=sanitize_report({'findings':[{'actual':{'Video':{'Filename':source},source:source},'file':'scene/mesh.fbx'}]})
                self.assertNotIn(source,json.dumps(clean))
                self.assertEqual(clean['removed_source_values'],3)
                self.assertEqual(clean['findings'][0]['file'],'scene/mesh.fbx')

    def test_nested_source_paths_removed_and_relative_logical_paths_preserved(self):
        report = {'profile': {'status': 'research'}, 'coverage': {'technical': 'partial', 'profile': 'research', 'procedure': 'unknown', 'external': 'not_checked'},
                  'findings': [{'file': 'scene/mesh.fbx', 'actual': {'Texture': {'Filename': '/private/source.fbx'}, 'Video': {'RelativeFilename': r'C:\\private\\source.png'}},
                                'expected': r'\\server\private\model', 'message': 'Bad input at https://private.invalid/model', 'html': '<img src=x onerror=alert(1)>'}]}
        clean = sanitize_report(report)
        text = json.dumps(clean)
        for source in ('private/source', 'private\\\\', 'private.invalid', 'server'):
            self.assertNotIn(source, text)
        self.assertEqual(clean['removed_source_values'], 4)
        self.assertEqual(clean['findings'][0]['file'], 'scene/mesh.fbx')
        self.assertEqual(clean['coverage'], report['coverage'])
        self.assertEqual(clean['profile']['status'], 'research')
        self.assertEqual(clean['findings'][0]['html'], '<img src=x onerror=alert(1)>')

    def test_findings_depth_utf8_and_finite_budgets(self):
        report = {'coverage': {'technical': 'partial'}, 'findings': [{'actual': float('inf'), 'expected': '\ud800', 'message': 'я' * 10000}] * 3}
        clean = sanitize_report(report, max_findings=2)
        self.assertEqual(len(clean['findings']), 2)
        self.assertTrue(clean['report_truncated'])
        self.assertEqual(clean['original_findings_count'], 3)
        self.assertEqual(len(clean['findings'][0]['message'].encode('utf-8')), 4096)
        json.dumps(clean, ensure_ascii=False, allow_nan=False).encode('utf-8', errors='strict')

    def test_total_wire_cap_and_cycle(self):
        value = {'findings': [{'actual': 'a' * 4096} for _ in range(100)]}
        clean = sanitize_report(value, max_bytes=4096)
        self.assertLessEqual(len(json.dumps(clean, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')), 4096)
        self.assertTrue(clean['report_truncated'])
        cycle = []; cycle.append(cycle)
        self.assertTrue(sanitize_report({'findings': [], 'nested': cycle})['report_truncated'])

    def test_ten_thousand_large_findings_truncate_in_bounded_time(self):
        import time
        value={'findings':[{'actual':'a'*4096,'ordinal':i} for i in range(10000)]}
        start=time.monotonic(); clean=sanitize_report(value)
        self.assertLess(time.monotonic()-start,5)
        self.assertLess(len(clean['findings']),10000)
        self.assertEqual(clean['original_findings_count'],10000)
        self.assertEqual([f['ordinal'] for f in clean['findings']],list(range(len(clean['findings']))))
