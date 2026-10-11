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

    def test_finished_fbx_job_without_a_reason_never_reports_a_pending_preview(self):
        # Such a job was processed before its kind had a view: waiting would be a false promise.
        for kind in ('fbx', 'zip-fbx'):
            self.assertEqual(self.reason(kind), {'availability': 'unavailable', 'reason': 'preview_unsupported'})
            self.assertEqual(self.reason(kind, state='completed')['reason'], 'preview_unsupported')

    def test_running_fbx_job_waits_for_its_preview(self):
        for kind in ('fbx', 'zip-fbx'):
            self.assertEqual(self.reason(kind, state='running')['reason'], 'preview_pending')

    def test_preview_is_available_for_every_kind_with_both_files(self):
        files = [{'id': str(n) * 32, 'kind': kind, 'bytes': 9, 'sha256': 'c' * 64}
                 for n, kind in ((2, 'preview'), (3, 'thumbnail'))]
        for kind in ('fbx', 'zip-fbx', 'portable-package'):
            self.assertEqual(job_dto(row(kind, state='completed'), files, 10)['capabilities']['preview'],
                             {'availability': 'available', 'reason': None})
            self.assertEqual(job_dto(row(kind, state='completed'), files[:1], 10)['capabilities']['preview']['availability'],
                             'unavailable')

    def test_recorded_reason_wins_over_the_default(self):
        self.assertEqual(self.reason('fbx', preview_failure_code='preview_budget')['reason'], 'preview_budget')

    def test_other_kinds_keep_their_reasons(self):
        self.assertEqual(self.reason('zip-fbx', preview_failure_code='preview_roundtrip_error')['reason'],
                         'preview_roundtrip_error')
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
