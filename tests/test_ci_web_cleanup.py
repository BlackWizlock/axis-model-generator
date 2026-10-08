"""Exercise the actual CI shell lifecycle without building or touching Docker."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
log = pathlib.Path(os.environ['DOCKER_LOG'])
with log.open('a') as stream:
    stream.write(json.dumps(args) + '\n')
mode = os.environ['FAKE_MODE']
if args[:2] == ['buildx', 'build'] and mode == 'build-failure':
    sys.exit(42)
if args[:2] == ['image', 'inspect'] and '--format' in args:
    if args[2] == os.environ['MG_TEST_IMAGE']:
        print('linux amd64 axis-model-generator cleanup-test auth-test test-revision')
    elif 'Local emulator image ID' in args[-1]:
        print('sha256:fake')
    else:
        print('linux amd64 axis-model-generator cleanup-test storage-emulator-test test-revision 45521908307306e925c98d629e1c17d78c8b72b6ee242b1bfb1409f7d8ee5841')
elif args[:3] == ['buildx', 'history', 'ls']:
    print(os.environ['MG_TEST_BUILD_NAME'])
    print(os.environ['MG_TEST_EMULATOR_BUILD_NAME'])
elif args[:2] == ['network', 'inspect']:
    print('true')
elif args[0] == 'exec' and args[-1] == '/proof/state':
    state = log.with_suffix('.state')
    print(state.read_text() if state.exists() else 'seeded')
elif args[0] == 'exec' and args[-2] == '-c':
    stage = {'offline': 'offline-passed', 'recovered': 'restart-passed', 'finish': 'finished'}
    log.with_suffix('.state').write_text(stage[args[-1].split()[2]])
elif args[0] == 'wait':
    print('0')
elif args[0] == 'compose' and 'down' in args and mode == 'down-failure':
    sys.exit(1)
elif args[:2] == ['image', 'rm'] and mode == 'image-in-use':
    sys.exit(1)
elif args[:2] == ['buildx', 'build'] and mode == 'term':
    import signal
    os.kill(os.getppid(), signal.SIGTERM)
'''


class CiWebCleanupTests(unittest.TestCase):
    def run_pipeline(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            script = Path(__file__).resolve().parents[1] / 'scripts/ci-web.sh'
            (root / 'scripts/ci-web.sh').write_text(script.read_text())
            docker = root / 'docker'
            docker.write_text(FAKE_DOCKER)
            docker.chmod(0o755)
            log = root / 'docker.jsonl'
            env = dict({key:value for key,value in os.environ.items() if not key.startswith('MG_')}, PATH=os.environ['PATH'],
                       **{'BASH_FUNC_docker%%': '() { python3 "$FAKE_DOCKER_FILE" "$@"; }'},
                       FAKE_DOCKER_FILE=str(docker), MG_TEST_PLATFORM='linux/amd64',
                       DOCKER_LOG=str(log), FAKE_MODE=mode,
                       MG_TEST_WORKTREE_NAME='cleanup-test',
                       MG_TEST_REVISION='test-revision', MG_TEST_BRANCH='test')
            result = subprocess.run(['bash', str(root / 'scripts/ci-web.sh'),
                                     '--task', 'auth'], env=env, capture_output=True,
                                    text=True, timeout=10)
            self.assertTrue(log.exists(),f'Fake Docker was never called; exit={result.returncode}; stderr={result.stderr}; stdout={result.stdout}')
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(list((root / '.web-test-runtime').glob('*')))
            return result, calls

    def assert_owned_cleanup(self, calls):
        removals = [args for args in calls if args[:2] == ['image', 'rm']]
        self.assertEqual(len(removals), 2)
        self.assertTrue(removals[0][2].startswith('axis-model-generator/cleanup-test:auth-test-'))
        self.assertTrue(removals[1][2].startswith('axis-model-generator/cleanup-test:storage-emulator-test-'))
        self.assertTrue(all(len(args) == 3 for args in removals))
        self.assertFalse(any('prune' in args for args in calls))
        down = next(i for i, args in enumerate(calls) if args[0] == 'compose' and 'down' in args)
        first_remove = next(i for i, args in enumerate(calls) if args[:2] == ['image', 'rm'])
        self.assertLess(down, first_remove)

    def test_success_removes_only_owned_tags_after_containers(self):
        result, calls = self.run_pipeline('success')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_owned_cleanup(calls)

    def test_build_failure_preserves_failure_status_and_cleans(self):
        result, calls = self.run_pipeline('build-failure')
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assert_owned_cleanup(calls)

    def test_failed_teardown_retains_images(self):
        result, calls = self.run_pipeline('down-failure')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(args[:2] == ['image', 'rm'] for args in calls))
        self.assertIn('retaining image tags', result.stderr)

    def test_image_in_use_never_forced(self):
        result, calls = self.run_pipeline('image-in-use')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_owned_cleanup(calls)
        self.assertIn('retained image in use', result.stderr)

    def test_term_cleans_and_preserves_signal_exit(self):
        result, calls = self.run_pipeline('term')
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assert_owned_cleanup(calls)


if __name__ == '__main__':
    unittest.main()


class WorkerProbeOwnershipTests(unittest.TestCase):
    def test_generated_and_legacy_owned_projects_reach_stub_only(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/ci-web-worker-probes.sh'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('worker_dsn', 's3_access', 's3_secret'):
                (root / name).write_text('synthetic')
            env = {key: value for key, value in os.environ.items() if not key.startswith('MG_')}
            env['MG_TEST_SECRET_ROOT'] = str(root)
            env['BASH_FUNC_docker%%'] = '() { return 77; }'
            for project in ('axis-model-generator-source-diagnostics-123', 'axis-model-generator-source-diagnostics-123-456-789'):
                result = subprocess.run(['bash', str(script), project], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 77, result.stderr)
            for project in ('other-source-diagnostics-123-456-789', 'axis-model-generator-source-auth-123-456-789', 'axis-model-generator-source-diagnostics-123-456', 'axis-model-generator-source-diagnostics-123-456-789-extra'):
                result = subprocess.run(['bash', str(script), project], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('Own jobs/preview', result.stdout)


class SyntheticFixturePermissionsTests(unittest.TestCase):
    def test_private_directory_contains_only_readonly_cross_uid_fixtures(self):
        import stat
        import sys
        script = Path(__file__).resolve().parents[1] / 'scripts/web-test-env.py'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'fixtures'
            result = subprocess.run([sys.executable, str(script), str(root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'Synthetic test secrets generated.\n')
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            files = list(root.iterdir())
            self.assertEqual(len(files), 11)
            for path in files:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444, path.name)
                self.assertTrue(path.stat().st_size)


class WorkerProbeOverlayTests(unittest.TestCase):
    def test_same_owned_overlay_reaches_compose_and_unsafe_paths_are_rejected(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/ci-web-worker-probes.sh'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            copied = root / 'scripts' / script.name
            copied.write_text(script.read_text())
            scratch = root / '_scratch'
            scratch.mkdir()
            overlay = scratch / 'network.yaml'
            overlay.write_text('networks: {}\n')
            outside = root / 'outside.yaml'
            outside.write_text('networks: {}\n')
            link = scratch / 'link.yaml'
            link.symlink_to(overlay)
            env = {key: value for key, value in os.environ.items() if not key.startswith('MG_')}
            env['BASH_FUNC_docker%%'] = '() { echo "$@"; return 77; }'
            env['MG_TEST_COMPOSE_OVERRIDE'] = str(overlay)
            result = subprocess.run(['bash', str(copied), 'axis-model-generator-source-diagnostics-1-2-3'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertIn('-f deploy/web/compose.test.yaml -f ' + str(overlay.resolve()), result.stdout)
            for unsafe in (outside, link):
                env['MG_TEST_COMPOSE_OVERRIDE'] = str(unsafe)
                result = subprocess.run(['bash', str(copied), 'axis-model-generator-source-diagnostics-1-2-3'], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('compose -p', result.stdout)
