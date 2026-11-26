set -e
cd /data/pilot-owner/pilot
test "$(pwd -P)" = /data/pilot-owner/pilot
export PILOT_ROLLOUT_CHECK="${1:-}"
.venv/bin/python - <<'PY'
import json,os,signal,sqlite3,time,uuid
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
import httpx
root=Path.cwd()
owned=[]
for proc in Path('/proc').iterdir():
 if not proc.name.isdecimal(): continue
 try:
  args=[p.decode() for p in (proc/'cmdline').read_bytes().split(b'\0') if p]
  if proc.stat().st_uid!=os.getuid() or (proc/'cwd').resolve()!=root: continue
  if '-m' in args and args[args.index('-m')+1]=='pilot_host.cli' and '--port' in args and args[args.index('--port')+1]=='18888': owned.append(int(proc.name))
 except (OSError,ValueError): continue
assert len(owned)<=1,'Multiple owned Hosts; inspect before rollout'
for dbfile in (root/'runs').glob('*/*.sqlite*'):
 if dbfile.suffix not in {'.sqlite','.sqlite3'}: continue
 with sqlite3.connect(dbfile.as_uri()+'?mode=ro',uri=True) as db:
  tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  for table in tables&{'applications','tasks','build_tasks'}:
   cursor=db.execute('SELECT * FROM "'+table+'"')
   columns=[c[0] for c in cursor.description]
   for row in cursor:
    values=dict(zip(columns,row)); state=json.loads(values.get('state') or '{}')
    assert values.get('status',state.get('status'))!='running' and not state.get('active_job'),'Business task/Job is active'
if owned:
 entry=urlsplit(json.loads((root/'.local/harness-web/entry.json').read_text())['url'])
 login=urlunsplit((entry.scheme,'127.0.0.1:18888',entry.path,entry.query,entry.fragment))
 with httpx.Client(follow_redirects=True,timeout=20,trust_env=False) as client:
  assert client.get(login).status_code==200,'Owner login unavailable'
  value=client.post('http://127.0.0.1:18888/api/session/list',json={'type':'client-request','rpcId':uuid.uuid4().hex,'method':'session/list','payload':{'args':{'_request':{}}}}).json()['result']
  assert value['ok'] and not any(row.get('running') for row in value['value']['items']),'Native Session active'
print('Owned Host and task idle checks passed; models and shared services are outside rollout')
if os.environ.get('PILOT_ROLLOUT_CHECK')=='--check': raise SystemExit(0)
if owned:
 pid=owned[0]
 identity=(Path('/proc',str(pid))/'stat').read_text().rsplit(')',1)[1].split()[19]
 children=[]
 for proc in Path('/proc').iterdir():
  if not proc.name.isdecimal(): continue
  try:
   parent=int((proc/'stat').read_text().rsplit(')',1)[1].split()[1])
   if parent==pid:
    command=(proc/'cmdline').read_bytes()
    assert b'node' in command and b'18888' in command,'Unexpected owned child; inspect before stopping'
    children.append(int(proc.name))
  except FileNotFoundError: pass
 assert (Path('/proc',str(pid))/'stat').read_text().rsplit(')',1)[1].split()[19]==identity,'Host PID identity changed'
 for child in children: os.kill(child,signal.SIGTERM)
 try: os.kill(pid,signal.SIGTERM)
 except ProcessLookupError: pass
 for _ in range(100):
  if not any(Path('/proc',str(p)).exists() for p in [pid,*children]): break
  time.sleep(.1)
 else: raise RuntimeError('Owned Host did not stop; no replacement started')
PY
if [ "${1:-}" = --check ]; then exit 0; fi
.venv/bin/python scripts/pilot.py prepare --require-data-disk > /dev/null
mkdir -p .local/logs
nohup .venv/bin/python scripts/pilot.py run --require-data-disk -- .venv/bin/python -m pilot_host.cli --root /data/pilot-owner/pilot --port 18888 --client-url http://127.0.0.1:18889 >> .local/logs/host.log 2>&1 < /dev/null &
echo $! > .local/harness-web.pid
echo $! > .local/web.pid
