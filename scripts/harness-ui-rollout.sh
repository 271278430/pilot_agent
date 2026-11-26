set -e
cd /data/pilot-owner/pilot
test "$(pwd -P)" = /data/pilot-owner/pilot
.venv/bin/python - <<'PY'
import json,uuid
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
import httpx
entry=urlsplit(json.loads(Path('.local/harness-web/entry.json').read_text())['url'])
login=urlunsplit((entry.scheme,'127.0.0.1:18888',entry.path,entry.query,entry.fragment))
with httpx.Client(follow_redirects=True,timeout=30) as client:
 assert client.get(login).status_code==200
 result=client.post('http://127.0.0.1:18888/api/session/list',json={
  'type':'client-request','rpcId':uuid.uuid4().hex,'method':'session/list',
  'payload':{'args':{'_request':{}}}}).json()['result']
 assert result['ok']
 assert not any(item.get('running') for item in result['value']['items']), 'Native Session still running'
 print('Native Sessions idle before owned Host refresh')
PY
exec bash scripts/harness-host-rollout.sh
