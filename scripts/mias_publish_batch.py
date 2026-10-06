"""Publish a prevalidated batch only through the OpenShift publisher.

Fail closed. Every OpenShift command failure raises ``OpenShiftError`` with the stage, the command (never its input or
an exec payload), the exit code, a category and a redacted stderr tail. Authentication and authorization failures are
never retried. Waits are bounded polls, not fixed sleeps:
- publisher pod creation, up to POD_CREATE_SECONDS: a kube-controller-manager leader failover can delay ReplicaSet pod
  creation by minutes (2026-10-05 12:00 cycle);
- API visibility of the published artifacts, up to API_VISIBILITY_SECONDS: the API index refresh takes longer as the
  store grows (2026-10-05 09:30 cycle). Only readiness failures and 404 (not yet indexed) are retried; 401, 403 or any
  other status fails at once.

``--cleanup-owned-publisher`` (systemd ExecStopPost) returns an owned publisher to zero and records a halt only if none
exists: an existing, more specific HALTED.json is never overwritten.
"""
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
sys.path.insert(0, '/home/pentu/MyRepos/MIAS')
from artifact_store.kinds import KINDS

OPERATIONS = Path('/home/pentu/MyRepos/MIAS/operations')
OWNER = OPERATIONS / 'publisher-owner.json'
HALTED = OPERATIONS / 'HALTED.json'
EXPECTED_KUBECONFIG = '/home/pentu/.kube/mias-config'
EXPECTED_SERVER = 'https://api.ngc.sirii.org:6443'
PUBLISHER = 'deployment/mias-publisher'
PUBLISHER_PODS = 'app.kubernetes.io/name=mias-publisher'
OC_TIMEOUT_SECONDS = 200
POD_CREATE_SECONDS = 240
POD_POLL_SECONDS = 5
API_VISIBILITY_SECONDS = 300
API_POLL_SECONDS = 10
_now, _sleep = time.monotonic, time.sleep

_REDACTIONS = (
    (re.compile(r'(?i)(bearer\s+)\S+'), r'\1<redacted>'),
    (re.compile(r'(?i)((?:token|password|passwd|secret|api[_-]?key)\w*["\']?\s*[=:]\s*["\']?)[^\s"\',]+'), r'\1<redacted>'),
    (re.compile(r'(://[^:/@\s]+:)[^@\s]+@'), r'\1<redacted>@'),
)
_AUTH_FAILURE = re.compile(r'(?i)unauthorized|must be logged in|forbidden|token (?:has )?expired|\b40[13]\b')


def redact(text):
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class PublishError(RuntimeError):
    """A publish-path failure with the stage it happened in and safe, structured details."""

    def __init__(self, stage, message, **details):
        super().__init__(message)
        self.stage = stage
        self.details = details

    def record(self):
        return {'stage': self.stage, 'detail': str(self), **self.details}


class OpenShiftError(PublishError):
    pass


_stage = 'idle'


def _enter(stage):
    global _stage
    _stage = stage


def describe(args):
    """The command for logs: everything up to ``--``, then only the program name (never an exec payload)."""
    if '--' in args:
        cut = args.index('--')
        args = args[:cut + 2] if len(args) > cut + 1 else args[:cut]
    return ' '.join(args)


def oc(*args, input=None):
    command = describe(args)
    try:
        result = subprocess.run(['oc', '-n', 'mias', *args], input=input, text=True, capture_output=True,
                                timeout=OC_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise OpenShiftError(_stage, f'oc {command} timed out after {OC_TIMEOUT_SECONDS}s',
                             command=command, category='timeout') from None
    if result.returncode:
        stderr = redact((result.stderr or '').strip())[-600:]
        category = 'authentication' if _AUTH_FAILURE.search(result.stderr or '') else 'command'
        raise OpenShiftError(_stage, f'oc {command} failed (exit {result.returncode}, {category}): {stderr}',
                             command=command, exit_code=result.returncode, category=category, stderr=stderr)
    return result.stdout


def verify_cluster_identity(environ=None):
    """The MIAS cluster through the dedicated kubeconfig, with a working login. Never retried."""
    environ = os.environ if environ is None else environ
    if environ.get('KUBECONFIG') != EXPECTED_KUBECONFIG:
        raise PublishError(_stage, 'KUBECONFIG must be ' + EXPECTED_KUBECONFIG, category='configuration')
    server = oc('whoami', '--show-server').strip()
    if server != EXPECTED_SERVER:
        raise PublishError(_stage, 'unexpected OpenShift server: ' + server, category='configuration')
    oc('whoami')                                  # authenticates against the server


def write_halt(record):
    """Create HALTED.json only if it does not exist. Returns True if this call wrote it."""
    data = (json.dumps(record) + '\n').encode()
    try:
        fd = os.open(HALTED, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def cleanup_owned_publisher():
    if not OWNER.exists():
        return
    oc('scale', PUBLISHER, '--replicas=0')
    deployment = json.loads(oc('get', PUBLISHER, '-o', 'json'))
    if deployment['spec']['replicas'] != 0:
        raise RuntimeError('publisher did not return to zero')
    OWNER.unlink()
    print(json.dumps({'publisher_replicas': 0}), flush=True)


def _publisher_events():
    """Recent publisher ReplicaSet/pod events for a failure record (best effort, never raises)."""
    try:
        events = json.loads(oc('get', 'events', '-o', 'json'))['items']
    except Exception:
        return []
    picked = [e for e in events if e.get('involvedObject', {}).get('name', '').startswith('mias-publisher')]
    picked.sort(key=lambda e: e.get('lastTimestamp') or e.get('eventTime') or '')
    return [{'object': e['involvedObject'].get('kind'), 'reason': e.get('reason'),
             'message': redact(e.get('message') or '')[:300], 'at': e.get('lastTimestamp') or e.get('eventTime')}
            for e in picked[-5:]]


def wait_for_publisher_pod():
    deadline = _now() + POD_CREATE_SECONDS
    while True:
        items = json.loads(oc('get', 'pod', '-l', PUBLISHER_PODS, '-o', 'json'))['items']
        if any(not p['metadata'].get('deletionTimestamp') for p in items):
            return
        if _now() >= deadline:
            status = json.loads(oc('get', PUBLISHER, '-o', 'json')).get('status', {})
            raise PublishError('pod_create', f'publisher pod was not created within {POD_CREATE_SECONDS}s',
                               category='timeout',
                               deployment_status={k: status.get(k) for k in ('replicas', 'readyReplicas', 'conditions')},
                               events=_publisher_events())
        _sleep(POD_POLL_SECONDS)


PROBE = """
import os,json,sys,urllib.request,urllib.error
manifest=json.loads(sys.stdin.readline())
base='http://127.0.0.1:'+os.environ.get('MIAS_API_PORT','8080')
headers={'Authorization':'Bearer '+os.environ['MIAS_API_READ_TOKEN']}
paths={'trade-setup':'trade-setups','invalidation-check':'invalidation-checks'}
def get(path,auth,body=False):
    try:
        with urllib.request.urlopen(urllib.request.Request(base+path,headers=headers if auth else {}),timeout=15) as r:
            data=r.read()
            if body:
                json.loads(data)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except ValueError:
        return 'invalid_body'
    except Exception:
        return 0
print(json.dumps({'ready':get('/health/ready',False),'artifacts':[{'kind':e['kind'],'id':e['id'],
    'status':get('/api/v1/'+paths.get(e['kind'],e['kind'])+'/'+e['id']+'/canonical',True,True)} for e in manifest]}))
"""
_PROBE_COMMAND = "import base64;exec(base64.b64decode('" + base64.b64encode(PROBE.encode()).decode() + "'))"


def wait_for_api_visibility(manifest):
    """Poll the API (inside the API pod) until it is ready and every artifact's canonical body is served."""
    pending = list(manifest)
    deadline = _now() + API_VISIBILITY_SECONDS
    while True:
        result = json.loads(oc('exec', '-i', 'deployment/mias-api', '--', 'python', '-c', _PROBE_COMMAND,
                               input=json.dumps(pending) + '\n'))
        fatal = [a for a in result['artifacts'] if a['status'] not in (200, 404)]
        if fatal:
            raise PublishError('api_visibility', 'the API returned a non-retryable status for a published artifact',
                               category='api', statuses=fatal, ready=result['ready'])
        for item in result['artifacts']:
            if item['status'] == 200:
                print(json.dumps({'kind': item['kind'], 'id': item['id'], 'visible': True}), flush=True)
        pending = [e for e, a in zip(pending, result['artifacts']) if a['status'] != 200]
        if result['ready'] == 200 and not pending:
            return
        if _now() >= deadline:
            raise PublishError('api_visibility',
                               f'published artifacts were not all visible through the API within {API_VISIBILITY_SECONDS}s',
                               category='timeout', ready=result['ready'], not_visible=[e['id'] for e in pending])
        _sleep(API_POLL_SECONDS)


def publish(directory):
    directory = Path(directory)
    _enter('validate_manifest')
    manifest = json.loads((directory / 'validated-manifest.json').read_text())
    if not manifest:
        raise ValueError('empty manifest')
    for entry in manifest:
        path = Path(entry['file'])
        if path.parent.resolve() != directory.resolve() or path.is_symlink():
            raise ValueError('unexpected artifact path')
        data = json.loads(path.read_text())
        kind = KINDS[entry['kind']]
        kind.validate(data)
        if data[kind.id_field] != entry['id']:
            raise ValueError('manifest identity mismatch')
    _enter('preflight')
    verify_cluster_identity()
    _enter('claim_publisher')
    deployment = json.loads(oc('get', PUBLISHER, '-o', 'json'))
    if deployment['spec']['replicas'] != 0:
        raise PublishError('claim_publisher', 'publisher already in use', category='conflict')
    with OWNER.open('x') as owner:
        json.dump({'run': str(directory)}, owner)
    failure = None
    try:
        _enter('scale_up')
        oc('scale', PUBLISHER, '--replicas=1')
        _enter('pod_create')
        wait_for_publisher_pod()
        _enter('pod_ready')
        oc('wait', '--for=condition=Ready', 'pod', '-l', PUBLISHER_PODS, '--timeout=180s')
        pods = json.loads(oc('get', 'pod', '-l', PUBLISHER_PODS, '-o', 'json'))['items']
        pod = next(p['metadata']['name'] for p in pods if not p['metadata'].get('deletionTimestamp'))
        _enter('store_verify_before')
        print(oc('exec', pod, '--', 'python', '-m', 'artifact_store.runner', 'verify'), flush=True)
        for entry in manifest:
            remote = '/tmp/mias-operational-' + Path(entry['file']).name
            _enter('copy')
            oc('cp', entry['file'], 'mias/' + pod + ':' + remote)
            _enter('publish')
            print(oc('exec', pod, '--', 'python', '-m', 'artifact_store.runner', 'publish', '--kind', entry['kind'],
                     '--file', remote), flush=True)
        _enter('store_verify_after')
        print(oc('exec', pod, '--', 'python', '-m', 'artifact_store.runner', 'verify'), flush=True)
    except BaseException as error:
        failure = error
        if not isinstance(error, PublishError):
            error.stage = _stage
        raise
    finally:
        _enter('cleanup')
        try:
            cleanup_owned_publisher()
        except Exception as cleanup_error:
            if failure is None:
                raise
            if isinstance(failure, PublishError):
                failure.details['cleanup_error'] = redact(str(cleanup_error))[:600]
            print(json.dumps({'status': 'CLEANUP_FAILED', 'error_type': type(cleanup_error).__name__}), flush=True)
    _enter('api_visibility')
    wait_for_api_visibility(manifest)
    _enter('record')
    (directory / 'published-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    _enter('idle')


def cleanup_after_service():
    """systemd ExecStopPost: return an owned publisher to zero; halt (without overwriting) on a failed service run."""
    service_result = os.environ.get('SERVICE_RESULT')
    cleanup_error = None
    try:
        cleanup_owned_publisher()
    except Exception as error:
        cleanup_error = error
    if service_result not in (None, 'success') or cleanup_error is not None:
        failed_run = service_result not in (None, 'success')
        record = {'status': 'HALTED', 'reason': 'service_interrupted' if failed_run else 'cleanup_failed',
                  'service_result': service_result, 'exit_code': os.environ.get('EXIT_CODE'),
                  'exit_status': os.environ.get('EXIT_STATUS'), 'at': utc_now()}
        if cleanup_error is not None:
            record['cleanup_error'] = redact(str(cleanup_error))[:600]
        written = write_halt(record)
        print(json.dumps({'halt_recorded': written, 'existing_halt_preserved': not written}), flush=True)
    if cleanup_error is not None:
        raise cleanup_error


if __name__ == '__main__':
    try:
        if sys.argv[1] == '--cleanup-owned-publisher':
            cleanup_after_service()
        else:
            publish(sys.argv[1])
    except Exception as error:
        record = error.record() if isinstance(error, PublishError) else {
            'detail': redact(str(error))[:600] if isinstance(error, (ValueError, RuntimeError)) else 'operation failed'}
        print(json.dumps({'status': 'STOPPED', 'error_type': type(error).__name__, **record}))
        sys.exit(1)
