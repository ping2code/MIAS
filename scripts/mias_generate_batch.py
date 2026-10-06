"""Generate a validated operational batch using existing MIAS interfaces; no publishing."""
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

REPO = Path('/home/pentu/MyRepos/MIAS')
PYTHON = str(REPO / '.venv/bin/python')
sys.path.insert(0, str(REPO))

def run(module, *args):
    result = subprocess.run([PYTHON, '-m', module, *map(str, args)], cwd=REPO, text=True, capture_output=True)
    if result.returncode:
        print(json.dumps({'stage': module, 'status': 'FAILED', 'exit_code': result.returncode}))
        # Supported runners emit safe diagnostics; keep them local for inspection.
        (OUT / (module.replace('.', '-') + '.failure.log')).write_text(result.stdout + result.stderr)
        raise RuntimeError(module)
    print(json.dumps({'stage': module, 'status': 'PASSED'}), flush=True)
    return result

OUT = REPO / 'operations' / 'runs' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
OUT.mkdir(parents=True, exist_ok=False)
print(json.dumps({'output_directory': str(OUT)}), flush=True)
try:
    os.environ['MARKET_DATA_PROVIDER'] = 'massive_stocks'
    os.environ['TECHNICAL_EVIDENCE_LEDGER_ENABLED'] = 'false'
    os.environ['OPTIONS_DATA_PROVIDER'] = 'massive'
    run('market_context.runner', '--symbols', 'META,NVDA', '--out', OUT / 'market-context.json')
    manifest = []
    for symbol in ('META', 'NVDA'):
        path = lambda suffix: OUT / (symbol + '-' + suffix + '.json')
        run('options_data.runner', '--symbol', symbol, '--output', path('snapshot'),
            '--page-limit', '250', '--max-pages', '60', '--max-requests', '80')
        snapshot = json.loads(path('snapshot').read_text())
        if snapshot['provenance']['truncated']:
            raise ValueError('complete chain required')
        run('live_validation.phase10', 'snapshot-summary', '--snapshot', path('snapshot'))
        packet = run('evidence_packet.runner', '--symbol', symbol, '--as-of', snapshot['as_of'],
                     '--market-context', OUT / 'market-context.json', '--output', path('packet'))
        (OUT / (symbol + '-packet.log')).write_text(packet.stderr)
        run('evidence_synthesis.runner', '--packet', path('packet'), '--output', path('synthesis'))
        run('live_validation.phase10', 'market-intelligence', '--synthesis', path('synthesis'), '--output', path('market-intelligence'))
        run('live_validation.phase10', 'options-intelligence', '--snapshot', path('snapshot'), '--output', path('options-intelligence'))
        assessment = run('trade_setup.runner', '--market-intelligence', path('market-intelligence'),
            '--options-intelligence', path('options-intelligence'),
            '--policy', REPO / 'live_validation/policies/phase10e_live_validation.policy.json',
            '--assessment-output', path('trade-setup'))
        print(assessment.stdout, flush=True)
        from artifact_store.kinds import KINDS
        for kind in ('market-intelligence', 'options-intelligence', 'trade-setup'):
            data = json.loads(path(kind).read_text())
            KINDS[kind].validate(data)
            manifest.append({'kind': kind, 'file': str(path(kind)), 'id': data[KINDS[kind].id_field]})
    from trade_setup.invalidation import check_invalidation, verify_invalidation
    from trade_setup.runner import write_all, canonical_text
    from artifact_store.kinds import KINDS
    prior = []
    for receipt in sorted((REPO / 'operations/runs').glob('*/published-manifest.json')):
        for entry in json.loads(receipt.read_text()):
            if entry['kind'] in ('trade-setup', 'invalidation-check'):
                data = json.loads(Path(entry['file']).read_text())
                KINDS[entry['kind']].validate(data)
                if data[KINDS[entry['kind']].id_field] != entry['id']:
                    raise ValueError('prior manifest identity mismatch')
                prior.append((entry['kind'], data))
    terminal = {data['setup_ref']['assessment_id'] for kind, data in prior
                if kind == 'invalidation-check' and data['result'] == 'invalidated'}
    for kind, setup in prior:
        if kind != 'trade-setup' or setup['outcome']['status'] != 'setup_candidates' or setup['assessment_id'] in terminal:
            continue
        symbol = setup['inputs']['symbol']
        if symbol not in ('META', 'NVDA'):
            continue
        mi = json.loads((OUT / (symbol + '-market-intelligence.json')).read_text())
        check = check_invalidation(setup, mi).to_dict()
        verify_invalidation(check, setup, mi)
        KINDS['invalidation-check'].validate(check)
        target = OUT / (check['invalidation_id'].replace(':', '-') + '-invalidation.json')
        write_all({str(target): canonical_text(check)})
        manifest.append({'kind': 'invalidation-check', 'file': str(target), 'id': check['invalidation_id']})
    print(json.dumps({'invalidation_checks': sum(e['kind'] == 'invalidation-check' for e in manifest)}))
    (OUT / 'validated-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'status': 'VALIDATED', 'artifacts': len(manifest), 'output_directory': str(OUT)}))
except Exception as error:
    print(json.dumps({'status': 'STOPPED', 'error_type': type(error).__name__, 'output_directory': str(OUT)}))
    sys.exit(1)
