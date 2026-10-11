"""Public job envelope: the preview reason stays truthful for every input kind."""
from datetime import date
import unittest

from model_generator.web.jobs import job_dto


def row(kind, **extra):
    base = {'id': '1' * 32, 'display_name': 'model', 'input_kind': kind, 'region': 'moscow',
            'procedure': 'diagnostic', 'state': 'failed', 'stage': 'finished', 'created_at': 1,
            'expires_at': 100, 'failure_code': None, 'cancel_requested': False,
            'input_sha256': 'a' * 64, 'worker_epoch': 'b' * 32, 'submission_date': date(2026, 10, 11),
            'coverage': {}, 'preview_failure_code': None}
    return {**base, **extra}


class PreviewReasonTests(unittest.TestCase):
    def reason(self, kind, **extra):
        return job_dto(row(kind, **extra), [], 10)['capabilities']['preview']

    def test_standalone_fbx_never_reports_a_pending_preview(self):
        # No preview is ever built for this kind, so waiting would be a false promise.
        self.assertEqual(self.reason('fbx'), {'availability': 'unavailable', 'reason': 'preview_unsupported'})

    def test_recorded_reason_wins_over_the_default(self):
        self.assertEqual(self.reason('fbx', preview_failure_code='preview_budget')['reason'], 'preview_budget')

    def test_other_kinds_keep_their_reasons(self):
        self.assertEqual(self.reason('zip-fbx')['reason'], 'zip_fbx_preview_not_verified')
        self.assertEqual(self.reason('portable-package')['reason'], 'preview_pending')

    def test_reduced_model_is_available_only_with_its_file(self):
        model = {'id': '2' * 32, 'kind': 'model', 'bytes': 9, 'sha256': 'c' * 64}
        for kind, expected in (('zip-fbx', 'model_not_built'), ('fbx', 'model_not_built'), ('portable-package', 'model_unsupported')):
            self.assertEqual(job_dto(row(kind), [], 10)['capabilities']['model'],
                             {'availability': 'unavailable', 'reason': expected})
        self.assertEqual(job_dto(row('fbx', state='completed'), [model], 10)['capabilities']['model'],
                         {'availability': 'available', 'reason': None})
        self.assertEqual(job_dto(row('portable-package', state='completed'), [model], 10)['capabilities']['model']['availability'],
                         'unavailable')


if __name__ == '__main__':
    unittest.main()
