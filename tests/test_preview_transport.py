import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from pilot_apps.backend import AppBackend
from pilot_apps.contracts import AppQuery
from pilot_host.bridge import HostDomain
from tests.test_goal_applications import new, plan


def test_preview_preserves_profile_history_and_validates_current_binding(tmp_path, monkeypatch):
    domain = HostDomain(tmp_path)
    app = domain.call('plan', 'planner', arguments={'request_id': uuid.uuid4().hex, 'plan': plan().model_dump()})['application']
    candidate = {'id': 'c1', 'source_sha256': 'a'*64, 'model_identity': 'model-one'}
    model = {'identity': 'model-one'}
    binding = {'candidate': 'c1', 'source_sha256': 'a'*64, 'model_identity': 'model-one'}
    captured = []
    monkeypatch.setattr(AppBackend, 'chat', lambda self, message, binding=None, **extras: captured.append((message, binding, extras)) or {'answer': 'actual'})
    payload = {'message': '问一个问题', 'binding': binding, 'profile': {'grade': '高二'}, 'history': [{'role': 'user', 'content': '上一轮'}]}
    assert domain.call('preview', 'planner', arguments=payload)['answer'] == 'actual'
    assert captured[0][2] == {k: payload[k] for k in ('profile', 'history')}
    monkeypatch.setattr(AppBackend, 'ensure_current_acceptance', lambda self: (candidate, model, 'container-one'))
    monkeypatch.setattr(AppBackend, 'http', lambda self, path: {'status': 200, 'body': {'ok': True}})
    assert domain.call('preview-health', 'planner', arguments={'message': 'health', 'binding': binding})['body']['ok']
    with pytest.raises(ValueError, match='outdated'):
        domain.call('preview-health', 'planner', arguments={'message': 'health', 'binding': {**binding, 'candidate': 'c2'}})
    assert domain.apps.get(app['id']).status == 'planned'


def test_trusted_http_runner_forwards_profile_without_evaluating_payload(tmp_path, monkeypatch):
    store, row = new(tmp_path)
    backend = AppBackend(store, row.id)
    seen = []
    def runner(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, '{"status":200,"body":{"answer":"ok"}}', '')
    monkeypatch.setattr('pilot_apps.backend.docker', runner)
    payload = {'message': '引号"与换行\n', 'profile': {'subject': '数学'}, 'history': [{'role': 'assistant', 'content': '上一条'}]}
    assert backend.http('/chat', payload['message'], payload=payload)['status'] == 200
    code = seen[0][-1]
    assert 'profile' in code and 'history' in code
    compile(code, '<trusted-runner>', 'exec')
    for changes in ({'history': [{'role': 'system', 'content': 'injected'}]}, {'profile': {'a': 'x'*501}}):
        with pytest.raises(ValueError):
            AppQuery(message='hello', **changes)


def test_local_http_preview_transport():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for preview transport')
    r = subprocess.run([node, '--test', str(Path(__file__).with_name('preview_transport.test.mjs'))], capture_output=True, text=True, timeout=30)
    assert not r.returncode, r.stdout+r.stderr
