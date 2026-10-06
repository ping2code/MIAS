"""Operational orchestration only: frozen analytical modules remain unchanged."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

REPO = Path('/home/pentu/MyRepos/MIAS')
sys.path.insert(0, str(REPO))
from market_data.calendar import default_calendar
from market_data.models import EXCHANGE_TZ, Interval
from market_data.config import load_market_data_settings
from persistence.config import DatabaseSettings
from persistence.database import make_engine
from persistence.news_audit import read_only
from persistence.models import technical_snapshots
from persistence.technical_snapshot_repository import content_hash, row_from_db
from sqlalchemy import select

ROOT = REPO / 'operations'
HALTED = ROOT / 'HALTED.json'
PYTHON = str(REPO / '.venv/bin/python')

def expected_start(calendar, cutoff, interval):
    day = cutoff.astimezone(EXCHANGE_TZ).date()
    while True:
        times = calendar.session_times(day)
        if times:
            if not interval.intraday:
                if times.close <= cutoff:
                    return datetime.combine(day, datetime.min.time(), tzinfo=EXCHANGE_TZ)
            else:
                start, last = times.open, None
                while start < times.close:
                    if min(start + interval.delta, times.close) <= cutoff:
                        last = start
                    start += interval.delta
                if last is not None:
                    return last
        day = calendar.previous_trading_day(day)

def check_persistence_log(output):
    lines = [line for line in output.splitlines() if 'event=technical_persistence ' in line]
    if len(lines) != 1:
        raise ValueError('missing persistence accounting')
    fields = dict(re.findall(r'(\w+)=(\S+)', lines[0]))
    for key in ('conflict','failed','dropped_queue_full','dropped_shutdown','dropped_initializing','failed_initializing','invalid_row'):
        if fields.get(key) != '0':
            raise ValueError('persistence failure: ' + key)
    if fields.get('persisted') != '6' or fields.get('queued') != '6' or fields.get('drained') != 'true':
        raise ValueError('incomplete technical persistence')

def verify_freshness(cutoff):
    calendar = default_calendar()
    engine = make_engine(DatabaseSettings.from_env())
    try:
        with read_only(engine) as session:
            for symbol in ('META','NVDA'):
                for label in ('1d','1h','5m'):
                    interval = Interval.parse(label)
                    table = technical_snapshots
                    row = session.execute(select(table).where(table.c.symbol == symbol, table.c.interval == label,
                        table.c.engine_version == 'phase4c-v2').order_by(table.c.snapshot_timestamp.desc()).limit(1)).mappings().one()
                    if row['snapshot_timestamp'] < expected_start(calendar, cutoff, interval):
                        raise ValueError('stale persisted snapshot')
                    latest_cutoff = datetime.now(timezone.utc) - timedelta(seconds=load_market_data_settings(os.environ).delay_seconds)
                    bar = SimpleNamespace(timestamp=row['snapshot_timestamp'], interval=interval)
                    if calendar.bar_end(bar) > latest_cutoff:
                        raise ValueError('incomplete persisted bar')
                    if content_hash(row_from_db(row)) != row['content_hash']:
                        raise ValueError('persisted snapshot hash mismatch')
                    print(json.dumps({'freshness':'PASSED','symbol':symbol,'interval':label,
                                      'bar':row['snapshot_timestamp'].isoformat()}),flush=True)
    finally:
        engine.dispose()

def scheduled_window(now):
    local = now.astimezone(EXCHANGE_TZ)
    return default_calendar().is_trading_day(local.date()) and 510 <= local.hour*60+local.minute <= 990

def execute():
    ROOT.mkdir(exist_ok=True)
    with (ROOT/'cycle.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"status":"SKIPPED_ALREADY_RUNNING"}')
            return 0
        if HALTED.exists():
            print('{"status":"HALTED_REQUIRES_OPERATOR_REVIEW"}')
            return 1
        if '--scheduled' in sys.argv and not scheduled_window(datetime.now(timezone.utc)):
            print('{"status":"OUTSIDE_OPERATIONAL_WINDOW"}')
            return 0
        try:
            os.environ.update(MARKET_DATA_PROVIDER='massive_stocks',
                MARKET_DATA_INCLUDE_EXTENDED_HOURS='false',
                TECHNICAL_SNAPSHOT_PERSISTENCE_SHADOW_ENABLED='true',
                TECHNICAL_SNAPSHOT_ENGINE_VERSION='phase4c-v2',
                TECHNICAL_EVIDENCE_LEDGER_ENABLED='false')
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=load_market_data_settings(os.environ).delay_seconds)
            result = subprocess.run([PYTHON,'-m','technical.runner','--symbols','META,NVDA','--timeframes','1d,1h,5m'],
                                    cwd=REPO,text=True,capture_output=True,timeout=900)
            if result.returncode:
                raise RuntimeError('technical generation failed')
            check_persistence_log(result.stdout+result.stderr)
            verify_freshness(cutoff)
            result = subprocess.run([PYTHON,str(REPO/'scripts/mias_generate_batch.py')],cwd=REPO,
                                    text=True,capture_output=True,timeout=1500)
            print(result.stdout,flush=True)
            if result.returncode:
                raise RuntimeError('batch validation failed')
            records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            final = records[-1]
            if final.get('status') != 'VALIDATED':
                raise ValueError('missing batch validation receipt')
            from mias_publish_batch import publish
            publish(final['output_directory'])
            print('{"status":"CYCLE_COMPLETE"}',flush=True)
            return 0
        except Exception as error:
            record = {'status':'HALTED','error_type':type(error).__name__,
                      'at':datetime.now(timezone.utc).isoformat()}
            HALTED.write_text(json.dumps(record)+'\n')
            print(json.dumps(record),flush=True)
            return 1

if __name__ == '__main__':
    sys.exit(execute())
