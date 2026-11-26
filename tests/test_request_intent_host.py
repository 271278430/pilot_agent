"""Adversarial scripted model over the installed native tool admission pipeline."""
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from pilot_host.cli import composition
from repopilot.harness import dsh_executable
from repopilot.mock_model import ScriptedModel
from tests.intent_fixture import with_intent_fixture
from tests.test_pilot_infra import plan, setup


def test_native_input_cannot_be_overridden_by_model_generated_plan(tmp_path):
    project = Path(__file__).resolve().parents[1]
    service = setup(tmp_path)
    home = tmp_path / '.local/harness-web'
    home.mkdir(parents=True)
    inbox = tmp_path / 'runs/harness-inbox'
    inbox.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    calls, count, revision = [], 0, None
    draft_plan = plan()
    draft_plan['workload']['input_lengths'] = [128, 512]

    def respond(body):
        nonlocal count
        count += 1
        calls.append({t['function']['name'] for t in body.get('tools', [])})
        if revision is not None and count == 1:
            return 'pilot_infra_revise', revision
        return ('pilot_infra_plan', {'request_id': 'native-intent-' + uuid.uuid4().hex, 'plan': draft_plan}) if count == 1 else None

    with ScriptedModel([], responder=with_intent_fixture(respond)) as model:
        patch = composition(project, {'REPOPILOT_MODEL': 'scripted-intent', 'REPOPILOT_BASE_URL': model.url,
                                     'PILOT_CLIENT_URL': f'http://127.0.0.1:{port}'}, settings_root=tmp_path)
        for row in patch[-1]['insert']:
            if 'root' in row.get('config', {}):
                row['config']['root'] = str(tmp_path)
        for row in patch:
            if row.get('id') == 'working-directory':
                row['config']['defaultDirectory'] = str(inbox)
        (home / 'patch.json').write_text(json.dumps(patch), encoding='utf-8')
        cmd = [dsh_executable(), '--profile', 'web', '--patch', str(home / 'patch.json'), '--no-open', '--port', str(port)]
        if os.name == 'nt':
            cmd = ['cmd.exe', '/c', *cmd]
        with (home / 'host.log').open('wb') as log:
            process = subprocess.Popen(cmd, cwd=inbox, stdout=log, stderr=log,
                                       env={**os.environ, 'DSH_HOME': str(home), 'REPOPILOT_API_KEY': 'fixture-key'})
        client = httpx.Client(follow_redirects=True, timeout=45)

        def rpc(endpoint, args, channel='api'):
            response = client.post(f'http://127.0.0.1:{port}/{channel}/{endpoint}', json={
                'type': 'client-request', 'rpcId': uuid.uuid4().hex, 'method': endpoint,
                'payload': {'args': args} if channel == 'api' else args})
            response.raise_for_status()
            result = response.json()['result']
            assert result['ok'], result
            return result['value']

        try:
            for _ in range(150):
                assert process.poll() is None, (home / 'host.log').read_text(errors='replace')
                if (home / 'entry.json').is_file():
                    try:
                        if client.get(json.loads((home / 'entry.json').read_text())['url']).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                time.sleep(.2)
            else:
                raise AssertionError('Native Host not ready')
            for prompt, expected in [('w', False), ('你能部署Qwen吗', False), ('只检查服务器，不要部署', False),
                                     ('给我写一首关于月亮的诗', False), ('Pilot有哪些角色，默认调用谁', False),
                                     ('请升级宿主GPU驱动并重启服务器', False), ('暂停当前任务', False),
                                     ('帮我部署Qwen3，先保存方案不要启动', True)]:
                count = 0
                start = len(calls)
                gated = prompt in {'w', '给我写一首关于月亮的诗'}
                sid = rpc('new-session', {}, 'pilot-infra-api')['sessionId']
                rpc('session/prompt', {'request': {'sessionId': sid, 'requestId': uuid.uuid4().hex, 'mode': 'queue',
                                                  'content': [{'type': 'text', 'text': prompt}]}})
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    idle = rpc('session/wait', {'request': {'sessionId': sid, 'timeoutMs': 5000}})
                    if idle.get('reason') == 'idle' and (gated or len(calls) > start):
                        break
                else:
                    raise AssertionError('Native input did not settle')
                route = rpc('view', {'session_id': sid}, 'pilot-infra-api').get('request_routing')
                if gated:
                    assert len(calls) == start, 'Business boundary reached the main provider'
                else:
                    assert ('pilot_infra_plan' in calls[start]) == expected, (prompt, calls[start], route)
                    assert ('ask_user_question' in calls[start]), 'Native root clarification tool missing'
                bound = service.store.binding(sid)
                assert bool(bound) == expected, prompt
                if bound:
                    assert bound['state']['status'] == 'draft' and not bound['state']['active_job']
                events = rpc('session/events', {'request': {'address': {'kind': 'session', 'sessionId': sid}, 'maxMessages': 200}})['records']
                errors = [r for r in events if r['event']['type'] == 'tool/result' and r['event']['data']['message']['isError']]
                assert bool(errors) == (not expected and not gated), 'Forced denied plans must fail; fixed notices must not execute tools'
            assert len(service.store.list()) == 1
            updated_plan = {**bound['plan'], 'serving': {**bound['plan']['serving'], 'context_length': 1024}}
            revision = {'request_id': 'semantic-revise-' + uuid.uuid4().hex,
                        'expected_revision': bound['revision'], 'plan': updated_plan}
            count, start = 0, len(calls)
            rpc('session/prompt', {'request': {'sessionId': sid, 'requestId': uuid.uuid4().hex, 'mode': 'queue',
                                             'content': [{'type': 'text', 'text': '把这个待开始方案的上下文长度改成1024'}]}})
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                idle = rpc('session/wait', {'request': {'sessionId': sid, 'timeoutMs': 5000}})
                if idle.get('reason') == 'idle' and len(calls) > start:
                    break
            else:
                raise AssertionError('Native revision did not settle')
            current = service.store.binding(sid)
            assert current['id'] == bound['id'] and current['plan']['serving']['context_length'] == 1024
            assert current['state']['status'] == 'draft' and not current['state']['active_job']
            assert 'pilot_infra_revise' in calls[start] and 'pilot_infra_plan' not in calls[start]
            classifiers = [r for r in model.requests if any('PILOT_BUSINESS_INTENT_CLASSIFIER' in json.dumps(m)
                           for m in r.get('messages', []) if m.get('role') == 'system')]
            assert len(classifiers) == 8, 'One classifier call per complex user input; multiple steps share its result'
        finally:
            client.close()
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, check=False)
            else:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
