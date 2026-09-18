"""Create a task through a managed account and read it through a running native app-server.
Uses disposable homes and loopback model replies; no real account, GUI or app restarts.
"""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from test_shared_editing_headless import Client, Fixture, ROOT


def run(runtime, original_runtime):
    runtime, original_runtime = runtime.resolve(strict=True), original_runtime.resolve(strict=True)
    output = ROOT / 'artifacts/results' / ('shared-new-task-' + uuid4().hex[:8])
    output.mkdir(parents=True)
    source = output / 'original'
    home = output / 'managed'
    catalog = output / 'catalog.json'
    fixture = Fixture()
    clients = []
    report = dict(status='FAIL', real_model_calls=0, gui_used=False, original_restarted=False,
        validation_scope='record_storage_and_explicit_query_only',
        desktop_auto_refresh_validated=False)
    try:
        original = Client(original_runtime, source, None, 'A', fixture.server.server_port, str(uuid4()))
        clients.append(original)
        pid = original.proc.pid
        original.rpc('thread/list', dict(limit=30))
        original_config = (source / 'config.toml').read_bytes()
        catalog.write_text(json.dumps(dict(version=3, hostId='local', sources=[], legacySources=[dict(
            hostId='local', sourceStoreId='legacy:'+hashlib.sha256(str(source).encode()).hexdigest(),
            codexHome=str(source))])), encoding='utf-8')
        managed = Client(runtime, home, catalog, 'B', fixture.server.server_port, str(uuid4()), new_thread_home=source)
        clients.append(managed)
        task = managed.rpc('thread/start', dict(historyMode='paginated', cwd=str(output)))['thread']['id']
        managed.turn(task, 'Create a new common task with the fixture response.')
        managed.rpc('thread/name/set',dict(threadId=task,name='new common task'))
        listed = original.rpc('thread/list', dict(limit=30))['data']
        record = original.rpc('thread/read', dict(threadId=task,includeTurns=True))['thread']
        # A successful explicit query is not evidence that the open desktop
        # receives notifications. Keep that distinction visible in the report.
        report['native_notifications_observed'] = sorted({
            event['method'] for event in original.events if 'method' in event})
        report['native_new_task_notification_observed'] = any(
            event.get('method') == 'thread/started'
            and event.get('params', {}).get('thread', {}).get('id') == task
            for event in original.events)
        files = list((source / 'sessions').rglob('*'+task+'.jsonl'))
        checks = dict(native_running_list_sees_new_task=any(t['id']==task for t in listed),
            native_read_sees_same_task=record['id']==task,
            original_was_not_restarted=original.proc.pid==pid and original.proc.poll() is None,
            shared_record_contains_reply=len(files)==1 and 'PROFILE_B_DONE' in files[0].read_text(encoding='utf-8'),
            private_home_has_no_duplicate=not list((home / 'sessions').rglob('*.jsonl')),
            selected_account_performs_inference=bool(fixture.calls) and all(call['account']=='B' for call in fixture.calls),
            original_config_untouched=(source/'config.toml').read_bytes()==original_config)
        report.update(status='PASS' if all(checks.values()) else 'FAIL',checks=checks)
    except Exception as error:
        report['error']=str(error)
    finally:
        for client in clients: client.close()
        fixture.close()
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(dict(report=str(output/'report.json'),**report)))
    return report['status']=='PASS'


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--original-runtime',type=Path,required=True)
    args=parser.parse_args()
    raise SystemExit(0 if run(args.runtime,args.original_runtime) else 1)
