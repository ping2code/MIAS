"""Publish-path failure handling: halt-reason preservation, specific failure records, bounded waits, no retry of
authentication failures, and the publisher always returned to zero. No cluster access: ``oc`` is a fake."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import mias_operational_cycle as cycle
import mias_publish_batch as publisher

POD = 'mias-publisher-abc'
KIND = SimpleNamespace(validate=lambda data: data, id_field='intelligence_id')


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class FakeCluster:
    """Just enough of `oc -n mias …` for the publish path."""

    def __init__(self, clock, pod_after=0.0, api=None, fail=None, fail_exact=None, server=publisher.EXPECTED_SERVER):
        self.clock, self.pod_after, self.server, self.fail = clock, pod_after, server, fail or {}
        self.fail_exact = fail_exact or {}
        self.replicas, self.scaled_at, self.calls = 0, None, []
        self.api = list(api or [])

    def __call__(self, *args, input=None):
        self.calls.append(args)
        if args in self.fail_exact:
            raise self.fail_exact[args]
        for prefix, error in self.fail.items():
            if args[:len(prefix)] == prefix:
                raise error
        if args == ('whoami', '--show-server'):
            return self.server + '\n'
        if args == ('whoami',):
            return 'mias-admin\n'
        if args[:2] == ('get', publisher.PUBLISHER):
            return json.dumps({'spec': {'replicas': self.replicas}, 'status': {'replicas': self.replicas}})
        if args[0] == 'scale':
            self.replicas = int(args[2].split('=')[1])
            self.scaled_at = self.clock.now() if self.replicas else None
            return ''
        if args[:2] == ('get', 'pod'):
            created = self.replicas and self.clock.now() - self.scaled_at >= self.pod_after
            return json.dumps({'items': [{'metadata': {'name': POD}}] if created else []})
        if args[:2] == ('get', 'events'):
            return json.dumps({'items': [{'involvedObject': {'kind': 'ReplicaSet', 'name': 'mias-publisher-7d5f'},
                                          'reason': 'FailedCreate', 'message': 'leader election', 'lastTimestamp': 't'}]})
        if args[0] in ('wait', 'cp'):
            return ''
        if args[:2] == ('exec', POD):
            return '{"result": "VERIFIED"}' if 'verify' in args else '{"result": "PUBLISHED"}'
        if args[:3] == ('exec', '-i', 'deployment/mias-api'):
            pending = json.loads(input)
            ready, status = self.api.pop(0) if len(self.api) > 1 else self.api[0]
            return json.dumps({'ready': ready, 'artifacts': [{'kind': e['kind'], 'id': e['id'], 'status': status}
                                                             for e in pending]})
        raise AssertionError(f'unexpected oc call {args}')

    def api_probes(self):
        return [c for c in self.calls if c[:3] == ('exec', '-i', 'deployment/mias-api')]


class PublishPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ops = root / 'operations'
        self.ops.mkdir()
        self.run_dir = root / 'run'
        self.run_dir.mkdir()
        manifest = []
        for name, ident in (('META', 'sha256:' + 'a' * 64), ('NVDA', 'sha256:' + 'b' * 64)):
            path = self.run_dir / f'{name}-market-intelligence.json'
            path.write_text(json.dumps({'intelligence_id': ident}))
            manifest.append({'kind': 'market-intelligence', 'id': ident, 'file': str(path)})
        (self.run_dir / 'validated-manifest.json').write_text(json.dumps(manifest))
        self.manifest = manifest
        self.clock = Clock()
        for target, value in ((publisher, {'OWNER': self.ops / 'publisher-owner.json', 'HALTED': self.ops / 'HALTED.json',
                                           'KINDS': {'market-intelligence': KIND}, '_now': self.clock.now,
                                           '_sleep': self.clock.sleep}),
                              (cycle, {'HALTED': self.ops / 'HALTED.json'})):
            for name, replacement in value.items():
                patcher = patch.object(target, name, replacement)
                patcher.start()
                self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {'KUBECONFIG': publisher.EXPECTED_KUBECONFIG})
        env.start()
        self.addCleanup(env.stop)

    def publish_with(self, cluster):
        out = io.StringIO()
        with patch.object(publisher, 'oc', cluster), redirect_stdout(out):
            try:
                publisher.publish(self.run_dir)
                return None, out.getvalue()
            except Exception as error:          # noqa: BLE001 - the test inspects it
                return error, out.getvalue()

    def assert_publisher_released(self, cluster):
        self.assertEqual(cluster.replicas, 0)
        self.assertFalse(publisher.OWNER.exists())

    # --- success and bounded retries -------------------------------------------------------------------------------

    def test_publish_succeeds_after_bounded_api_visibility_polling(self):
        cluster = FakeCluster(self.clock, api=[(200, 404), (503, 404), (200, 200)])
        error, out = self.publish_with(cluster)
        self.assertIsNone(error)
        self.assertTrue((self.run_dir / 'published-manifest.json').exists())
        self.assertEqual(json.loads((self.run_dir / 'published-manifest.json').read_text()), self.manifest)
        self.assertEqual(len(cluster.api_probes()), 3)
        self.assertEqual(self.clock.sleeps.count(publisher.API_POLL_SECONDS), 2)    # polls, not one fixed 35 s sleep
        self.assertEqual(out.count('"visible": true'), 2)
        self.assert_publisher_released(cluster)

    def test_pod_created_late_but_within_the_bound(self):
        cluster = FakeCluster(self.clock, pod_after=150, api=[(200, 200)])           # a controller-manager failover
        error, _ = self.publish_with(cluster)
        self.assertIsNone(error)
        self.assert_publisher_released(cluster)

    # --- fail closed ---------------------------------------------------------------------------------------------

    def test_pod_never_created_fails_with_a_specific_record_and_releases_the_publisher(self):
        cluster = FakeCluster(self.clock, pod_after=10_000, api=[(200, 200)])
        error, _ = self.publish_with(cluster)
        self.assertIsInstance(error, publisher.PublishError)
        record = error.record()
        self.assertEqual((record['stage'], record['category']), ('pod_create', 'timeout'))
        self.assertIn(f'within {publisher.POD_CREATE_SECONDS}s', record['detail'])
        self.assertEqual(record['events'][0]['reason'], 'FailedCreate')
        self.assertGreaterEqual(self.clock.t, publisher.POD_CREATE_SECONDS)
        self.assertLess(self.clock.t, publisher.POD_CREATE_SECONDS + 2 * publisher.POD_POLL_SECONDS)   # bounded
        self.assertFalse((self.run_dir / 'published-manifest.json').exists())
        self.assertEqual(cluster.api_probes(), [])
        self.assert_publisher_released(cluster)

    def test_artifacts_not_visible_within_the_bound_fail_closed(self):
        cluster = FakeCluster(self.clock, api=[(200, 404)])
        error, _ = self.publish_with(cluster)
        self.assertEqual((error.stage, error.details['category']), ('api_visibility', 'timeout'))
        self.assertEqual(error.details['not_visible'], [e['id'] for e in self.manifest])
        self.assertLessEqual(self.clock.t, publisher.API_VISIBILITY_SECONDS + publisher.API_POLL_SECONDS)
        self.assertFalse((self.run_dir / 'published-manifest.json').exists())
        self.assert_publisher_released(cluster)

    def test_api_authorization_failure_is_not_retried(self):
        cluster = FakeCluster(self.clock, api=[(200, 401)])
        error, _ = self.publish_with(cluster)
        self.assertEqual((error.stage, error.details['category']), ('api_visibility', 'api'))
        self.assertEqual(len(cluster.api_probes()), 1)
        self.assertFalse((self.run_dir / 'published-manifest.json').exists())

    def test_openshift_authentication_failure_is_not_retried_and_never_scales(self):
        unauthorized = publisher.OpenShiftError('preflight', 'oc whoami failed (exit 1, authentication)',
                                                category='authentication')
        cluster = FakeCluster(self.clock, api=[(200, 200)], fail_exact={('whoami',): unauthorized})
        error, _ = self.publish_with(cluster)
        self.assertIs(error, unauthorized)
        self.assertEqual([c for c in cluster.calls if c[0] == 'scale'], [])
        self.assertEqual(cluster.calls, [('whoami', '--show-server'), ('whoami',)])      # one attempt, then stop
        self.assertFalse(publisher.OWNER.exists())

    def test_wrong_kubeconfig_is_refused_before_any_oc_call(self):
        cluster = FakeCluster(self.clock, api=[(200, 200)])
        with patch.dict(os.environ, {'KUBECONFIG': '/home/pentu/.kube/config'}):
            error, _ = self.publish_with(cluster)
        self.assertEqual(error.details['category'], 'configuration')
        self.assertEqual(cluster.calls, [])

    def test_wrong_cluster_is_refused(self):
        cluster = FakeCluster(self.clock, api=[(200, 200)], server='https://api.other.example:6443')
        error, _ = self.publish_with(cluster)
        self.assertEqual(error.details['category'], 'configuration')
        self.assertEqual([c for c in cluster.calls if c[0] == 'scale'], [])

    def test_publish_command_failure_records_the_stage_and_releases_the_publisher(self):
        failure = publisher.OpenShiftError('publish', 'oc exec mias-publisher-abc -- python failed (exit 137, command): ',
                                           category='command', exit_code=137)
        cluster = FakeCluster(self.clock, api=[(200, 200)], fail={('exec', POD, '--', 'python', '-m', 'artifact_store.runner', 'publish'): failure})
        error, _ = self.publish_with(cluster)
        self.assertIs(error, failure)
        self.assertFalse((self.run_dir / 'published-manifest.json').exists())
        self.assert_publisher_released(cluster)

    def test_publisher_in_use_is_refused_without_taking_ownership(self):
        cluster = FakeCluster(self.clock, api=[(200, 200)])
        cluster.replicas = 1
        error, _ = self.publish_with(cluster)
        self.assertEqual(error.details['category'], 'conflict')
        self.assertFalse(publisher.OWNER.exists())
        self.assertEqual(cluster.replicas, 1)                                   # someone else's publisher untouched


class OcCommandTests(unittest.TestCase):
    def run_oc(self, completed=None, raises=None, *args):
        with patch.object(publisher.subprocess, 'run', side_effect=raises, return_value=completed):
            with self.assertRaises(publisher.OpenShiftError) as caught:
                publisher.oc(*args)
        return caught.exception

    def test_failure_keeps_command_exit_code_category_and_redacted_stderr(self):
        stderr = 'error: You must be logged in to the server (Unauthorized)\nAuthorization: Bearer sha256~SECRET123'
        error = self.run_oc(SimpleNamespace(returncode=1, stderr=stderr, stdout=''), None,
                            'exec', '-i', 'deployment/mias-api', '--', 'python', '-c', 'PAYLOAD')
        record = error.record()
        self.assertEqual(record['category'], 'authentication')
        self.assertEqual(record['exit_code'], 1)
        self.assertEqual(record['command'], 'exec -i deployment/mias-api -- python')   # never the exec payload
        self.assertIn('Unauthorized', record['detail'])
        self.assertNotIn('SECRET123', json.dumps(record))
        self.assertNotIn('PAYLOAD', json.dumps(record))

    def test_timeout_is_reported(self):
        error = self.run_oc(None, subprocess.TimeoutExpired('oc', 200), 'get', 'pod')
        self.assertEqual(error.details['category'], 'timeout')

    def test_redaction(self):
        text = 'password=hunter2 token: abc postgresql://mias:pw@db:5432/x Bearer xyz'
        self.assertEqual(publisher.redact(text),
                         'password=<redacted> token: <redacted> postgresql://mias:<redacted>@db:5432/x Bearer <redacted>')


class HaltRecordTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ops = Path(tmp.name)
        self.halted = self.ops / 'HALTED.json'
        for target, name, value in ((publisher, 'HALTED', self.halted), (publisher, 'OWNER', self.ops / 'owner.json'),
                                    (cycle, 'HALTED', self.halted)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def cleanup_after(self, service_result, oc=None):
        out = io.StringIO()
        env = {k: v for k, v in {'SERVICE_RESULT': service_result, 'EXIT_CODE': 'exited', 'EXIT_STATUS': '1'}.items() if v}
        with patch.dict(os.environ, env), patch.object(publisher, 'oc', oc or FakeCluster(None)), redirect_stdout(out):
            publisher.cleanup_after_service()
        return out.getvalue()

    def test_service_stop_never_overwrites_a_specific_halt(self):
        specific = {'status': 'HALTED', 'stage': 'pod_create', 'detail': 'publisher pod was not created within 240s'}
        self.halted.write_text(json.dumps(specific) + '\n')
        before = self.halted.read_bytes()
        out = self.cleanup_after('exit-code')
        self.assertEqual(self.halted.read_bytes(), before)
        self.assertIn('"existing_halt_preserved": true', out)

    def test_interrupted_service_without_a_halt_records_the_interruption(self):
        self.cleanup_after('timeout')
        record = json.loads(self.halted.read_text())
        self.assertEqual((record['reason'], record['service_result']), ('service_interrupted', 'timeout'))
        self.assertEqual(oct(self.halted.stat().st_mode & 0o777), '0o600')

    def test_successful_service_records_nothing(self):
        self.cleanup_after('success')
        self.assertFalse(self.halted.exists())

    def test_service_stop_returns_an_owned_publisher_to_zero(self):
        (self.ops / 'owner.json').write_text('{}')
        cluster = FakeCluster(Clock())
        cluster.replicas = 1
        self.cleanup_after('timeout', oc=cluster)
        self.assertEqual(cluster.replicas, 0)
        self.assertFalse((self.ops / 'owner.json').exists())

    def test_failed_cleanup_halts_and_keeps_ownership(self):
        (self.ops / 'owner.json').write_text('{}')
        offline = publisher.OpenShiftError('cleanup', 'oc scale failed', category='command')
        cluster = FakeCluster(Clock(), fail={('scale',): offline})
        with self.assertRaises(publisher.OpenShiftError):
            self.cleanup_after('success', oc=cluster)
        self.assertEqual(json.loads(self.halted.read_text())['reason'], 'cleanup_failed')
        self.assertTrue((self.ops / 'owner.json').exists())

    def test_cycle_records_the_specific_failure(self):
        error = publisher.PublishError('pod_create', 'publisher pod was not created within 240s', category='timeout')
        record = cycle.halt_record(error, 'publish', '/runs/x')
        written = json.loads(self.halted.read_text())
        self.assertTrue(record['halt_recorded'])
        self.assertEqual((written['stage'], written['cycle_stage'], written['run'], written['category']),
                         ('pod_create', 'publish', '/runs/x', 'timeout'))
        self.assertEqual(written['detail'], 'publisher pod was not created within 240s')

    def test_cycle_never_overwrites_an_existing_halt(self):
        self.halted.write_text('{"status": "HALTED", "stage": "first"}\n')
        record = cycle.halt_record(RuntimeError('second'), 'freshness', None)
        self.assertFalse(record['halt_recorded'])
        self.assertEqual(json.loads(self.halted.read_text())['stage'], 'first')

    def test_cycle_redacts_unexpected_error_text(self):
        cycle.halt_record(RuntimeError('connect postgresql://mias:pw@db/x failed'), 'freshness', None)
        self.assertNotIn(':pw@', self.halted.read_text())


if __name__ == '__main__':
    unittest.main()
