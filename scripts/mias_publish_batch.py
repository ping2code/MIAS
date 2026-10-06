"""Publish a prevalidated batch only through the OpenShift publisher."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, '/home/pentu/MyRepos/MIAS')
from artifact_store.kinds import KINDS

OWNER = Path('/home/pentu/MyRepos/MIAS/operations/publisher-owner.json')

def oc(*args, input=None):
    result = subprocess.run(['oc', '-n', 'mias', *args], input=input, text=True, capture_output=True, timeout=200)
    if result.returncode:
        raise RuntimeError('OpenShift operation failed: ' + args[0])
    return result.stdout

def cleanup_owned_publisher():
    if not OWNER.exists():
        return
    oc('scale','deployment/mias-publisher','--replicas=0')
    deployment = json.loads(oc('get','deployment/mias-publisher','-o','json'))
    if deployment['spec']['replicas'] != 0:
        raise RuntimeError('publisher did not return to zero')
    OWNER.unlink()
    print(json.dumps({'publisher_replicas':0}),flush=True)

def publish(directory):
    directory = Path(directory)
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
    deployment = json.loads(oc('get','deployment/mias-publisher','-o','json'))
    if deployment['spec']['replicas'] != 0:
        raise RuntimeError('publisher already in use')
    with OWNER.open('x') as owner:
        json.dump({'run':str(directory)},owner)
    try:
        oc('scale','deployment/mias-publisher','--replicas=1')
        deadline = time.monotonic() + 60
        while True:
            items = json.loads(oc('get','pod','-l','app.kubernetes.io/name=mias-publisher','-o','json'))['items']
            if any(not p['metadata'].get('deletionTimestamp') for p in items):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('publisher pod was not created')
            time.sleep(2)
        oc('wait','--for=condition=Ready','pod','-l','app.kubernetes.io/name=mias-publisher','--timeout=180s')
        pods = json.loads(oc('get','pod','-l','app.kubernetes.io/name=mias-publisher','-o','json'))['items']
        pod = next(p['metadata']['name'] for p in pods if not p['metadata'].get('deletionTimestamp'))
        print(oc('exec',pod,'--','python','-m','artifact_store.runner','verify'), flush=True)
        for entry in manifest:
            remote = '/tmp/mias-operational-' + Path(entry['file']).name
            oc('cp',entry['file'],'mias/'+pod+':'+remote)
            print(oc('exec',pod,'--','python','-m','artifact_store.runner','publish','--kind',entry['kind'],'--file',remote),flush=True)
        print(oc('exec',pod,'--','python','-m','artifact_store.runner','verify'), flush=True)
    finally:
        cleanup_owned_publisher()
    probe = """
import os,json,sys,urllib.request
manifest=json.loads(sys.stdin.readline())
base='http://127.0.0.1:'+os.environ.get('MIAS_API_PORT','8080')
headers={'Authorization':'Bearer '+os.environ['MIAS_API_READ_TOKEN']}
paths={'trade-setup':'trade-setups','invalidation-check':'invalidation-checks'}
with urllib.request.urlopen(base+'/health/ready',timeout=15) as r:
    assert r.status==200
for e in manifest:
    path='/api/v1/'+paths.get(e['kind'],e['kind'])+'/'+e['id']+'/canonical'
    with urllib.request.urlopen(urllib.request.Request(base+path,headers=headers),timeout=15) as r:
        data=json.load(r)
        assert r.status==200
    print(json.dumps({'kind':e['kind'],'id':e['id'],'visible':True}))
"""
    # The API refresh interval is 30 seconds; allow one complete interval.
    time.sleep(35)
    import base64
    encoded = base64.b64encode(probe.encode()).decode()
    command = "import base64;exec(base64.b64decode('" + encoded + "'))"
    output = oc('exec','-i','deployment/mias-api','--','python','-c',command,input=json.dumps(manifest)+'\n')
    print(output,flush=True)
    (directory/'published-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')

if __name__ == '__main__':
    try:
        if sys.argv[1] == '--cleanup-owned-publisher':
            try:
                cleanup_owned_publisher()
            finally:
                if os.environ.get('SERVICE_RESULT') not in (None, 'success'):
                    (OWNER.parent/'HALTED.json').write_text(json.dumps({'status':'HALTED','reason':'service_interrupted'})+'\n')
        else:
            publish(sys.argv[1])
    except Exception as error:
        print(json.dumps({'status':'STOPPED','error_type':type(error).__name__,'detail':str(error) if isinstance(error,(ValueError,RuntimeError)) else 'operation failed'}))
        sys.exit(1)
