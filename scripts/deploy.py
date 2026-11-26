"""Manifest-based Pilot release with external preimages and guarded cleanup.

Plan is read-only. Apply backs up every changed/deleted file, stages the source,
checks native Sessions, replaces files atomically and refreshes only our Host.
Neither operation uploads credentials or changes model/application data.
"""
import argparse
import hashlib
import io
import json
import os
import tarfile
from datetime import datetime
from pathlib import Path

from remote_project import REMOTE, ROOT, connect

SOURCE_DIRS = ('src', 'harness/plugin', 'harness/runtime/plugins', 'prompts',
               'skills', 'scripts', 'evals', 'docs', 'tests', 'tasks')
SOURCE_FILES = ('pyproject.toml', 'requirements.lock', 'README.md', 'AGENTS.md',
                'CHANGELOG.md', '.gitignore', 'harness/runtime/package.json',
                'harness/runtime/package-lock.json', 'harness/runtime/workspaces.mjs')
SOURCE_FILES += ('.github/workflows/ci.yml',)
SOURCE_FILES += ('setup.cfg',)
EXCLUDED = {'node_modules', '__pycache__', '.local', 'runs', '.git'}
DELETE_DIRS = {'src', 'scripts', 'docs', 'tests', 'web', 'collab_web'}


def valid_delete(name):
    path = Path(name)
    return (not path.is_absolute() and '..' not in path.parts
            and bool(path.parts) and path.parts[0] in DELETE_DIRS
            and not (set(path.parts) & EXCLUDED) and not path.name.endswith('.env')
            and path.suffix not in {'.key', '.pem', '.p12'})


def sources():
    result = {}
    paths = [ROOT / name for name in SOURCE_FILES]
    for folder in SOURCE_DIRS:
        for directory, children, names in os.walk(ROOT / folder):
            children[:] = [n for n in children if n not in EXCLUDED and not n.endswith('.egg-info')]
            paths.extend(Path(directory) / name for name in names)
    for path in paths:
        if (not path.is_file() or path.name == '.env' or path.name.endswith('.env')
                or path.suffix in {'.key', '.pem', '.p12', '.pyc'}):
            continue
        assert not path.is_symlink(), str(path)
        name = path.relative_to(ROOT).as_posix()
        assert not (set(Path(name).parts) & EXCLUDED) and path.suffix != '.pyc'
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


REMOTE_PROGRAM = r'''
import hashlib,importlib.util,json,os,shutil,subprocess,tarfile,uuid
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
import httpx
from pilot_apps.store import AppStore,source_hash
from pilot_projects.store import ProjectStore

root=Path('/data/pilot-owner/pilot')
assert root.resolve()==root
def target(name):
 p=root/name
 assert not Path(name).is_absolute() and '..' not in Path(name).parts
 assert p.resolve().is_relative_to(root),name
 for part in [p,*list(p.parents)]:
  if part==root: break
  assert not part.is_symlink(),name
 return p
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def snapshot():
 store=AppStore(root)
 with store.db.connect() as db:
  ids=[row['id'] for row in db.execute('SELECT id FROM applications ORDER BY id').fetchall()]
 apps=[store.get(id) for id in ids]
 assert not any(a.status=='running' for a in apps),'Delivery is running'
 entry=urlsplit(json.loads((root/'.local/harness-web/entry.json').read_text())['url'])
 login=urlunsplit((entry.scheme,'127.0.0.1:18888',entry.path,entry.query,entry.fragment))
 with httpx.Client(follow_redirects=True,timeout=30) as c:
  c.get(login).raise_for_status()
  r=c.post('http://127.0.0.1:18888/api/session/list',json={'type':'client-request',
   'rpcId':uuid.uuid4().hex,'method':'session/list','payload':{'args':{'_request':{}}}}).json()['result']
  assert r['ok']
  items=r['value']['items']
  assert not any(row.get('running') for row in items),'Native Session is running'
 app_rows=[]
 for a in apps:
  candidate=a.state.get('candidate') or {}
  cid=candidate.get('id')
  folder=root/'runs/apps'/a.id/'candidates'/str(cid)
  app_rows.append({'id':a.id,'status':a.status,'candidate':cid,
   'declared_sha256':candidate.get('source_sha256'),
   'actual_sha256':source_hash(folder) if cid and folder.is_dir() else None,
   'model_identity':(a.state.get('model') or {}).get('identity')})
 with store.db.connect() as db:
  legacy=db.execute('SELECT id FROM conversations ORDER BY id').fetchall()
 return {'applications':sorted(app_rows,key=lambda a:a['id']),
  'sessions':sorted(row['sessionId'] for row in items),
  'legacy': [row['id'] for row in legacy],
  'projects':sorted(row.id for row in ProjectStore(root).list())}

manifest=json.loads(Path(MANIFEST).read_text())
for name in manifest['delete']:
 parts=Path(name).parts
 assert parts and parts[0] in {'src','scripts','docs','tests','web','collab_web'},name
 assert not set(parts)&{'node_modules','__pycache__','.local','runs','.git'},name
 assert not Path(name).name.endswith('.env') and Path(name).suffix not in {'.key','.pem','.p12'},name
changed=[name for name,sha in manifest['sources'].items()
 if not target(name).is_file() or digest(target(name))!=sha]
deleted=[name for name in manifest['delete'] if target(name).is_file()]
for name in deleted: assert name not in manifest['sources'],name
for proc in Path('/proc').iterdir():
 if not proc.name.isdecimal(): continue
 try:
  cmd=(proc/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
  cwd=(proc/'cwd').resolve()
 except (FileNotFoundError,PermissionError,ProcessLookupError): continue
 assert not any(str(root/name) in cmd or (cwd==root and name in cmd)
  for name in deleted),'Retired file still used by a process'
 if str(root) in cmd or cwd.is_relative_to(root):
  for module in manifest.get('retired_modules',[]):
   assert ('-m '+module+' ') not in cmd,'Retired module still used by a process'
before=snapshot()
result={'changed':changed,'delete':deleted,'before':before}
if APPLY:
 if importlib.util.find_spec('pip') is None:
  installer=subprocess.run([str(root/'.venv/bin/python'),'-m','ensurepip','--upgrade'],
   capture_output=True,text=True)
  assert installer.returncode==0,'Offline installer unavailable; no source files changed'
 if importlib.util.find_spec('setuptools') is None:
  build=subprocess.run([str(root/'.venv/bin/python'),'-m','pip','install','setuptools==80.9.0',
   '--no-deps','--disable-pip-version-check','--index-url','https://pypi.tuna.tsinghua.edu.cn/simple',
   '--retries','1','--timeout','10'],capture_output=True,text=True)
  assert build.returncode==0,'Build tooling unavailable; no source files changed'
 backup=Path(BACKUP)
 assert backup.parent==root.parent/'pilot-cleanup-backups' and not backup.exists()
 backup.mkdir(parents=True)
 preimages={}
 with tarfile.open(backup/'preimages.tar.gz','w:gz') as tar:
  for name in sorted(set(changed+deleted)):
   p=target(name)
   if p.exists():
    assert p.is_file(),name
    preimages[name]=digest(p)
    tar.add(p,arcname=name,recursive=False)
 (backup/'manifest.json').write_text(json.dumps({'preimages':preimages,**result},indent=2))
 stage=backup/'stage'
 stage.mkdir()
 with tarfile.open(BUNDLE) as tar:
  for item in tar.getmembers():
   assert item.isfile() and item.name in manifest['sources'],item.name
   p=stage/item.name;p.parent.mkdir(parents=True,exist_ok=True)
   p.write_bytes(tar.extractfile(item).read())
   assert digest(p)==manifest['sources'][item.name],item.name
 assert snapshot()==before,'State changed while staging release'
 # Recheck all preimages before the first mutation; atomic per-file rename.
 for name,sha in preimages.items(): assert digest(target(name))==sha,name
 for name in changed:
  p=target(name);p.parent.mkdir(parents=True,exist_ok=True)
  temporary=p.with_name(p.name+'.pilot-release-tmp')
  assert not temporary.exists(),name
  shutil.copyfile(stage/name,temporary)
  os.replace(temporary,p)
 for name in deleted:
  target(name).unlink()
  p=Path(name)
  if p.suffix=='.py':
   cache=target(str(p.parent/'__pycache__'))
   if cache.is_dir():
    for stale in cache.glob(p.stem+'.*.pyc'): stale.unlink()
 # Only the two retired frontend trees contain reinstallable npm caches.
 result['removed_caches']=[]
 for name in ('web/node_modules','collab_web/node_modules'):
  p=target(name)
  if p.exists():
   assert p.is_dir()
   p.replace(backup/('retired-'+Path(name).parent.name+'-node_modules'))
   result['removed_caches'].append(name)
 for name in ('web','collab_web','src/pilot_workbench','docs/diagrams','docs/schemas','docs/references'):
  p=target(name)
  if p.exists():
   for directory in sorted([d for d in p.rglob('*') if d.is_dir()],key=lambda d:len(d.parts),reverse=True):
    if not directory.is_symlink() and not any(directory.iterdir()):directory.rmdir()
   if not any(p.iterdir()): p.rmdir()
 result['backup']=str(backup)
 result['after_files']=snapshot()
 assert result['after_files']==before,'Application/history identity drift'
 result['sha_verified']=all(digest(target(name))==sha for name,sha in manifest['sources'].items())
 assert result['sha_verified']
 (backup/'result.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
'''


def remote(client, program):
    stdin, stdout, stderr = client.exec_command('cd ' + REMOTE + ' && .venv/bin/python -', timeout=300)
    stdin.write(program)
    stdin.channel.shutdown_write()
    output, error = stdout.read().decode(), stderr.read().decode()
    status = stdout.channel.recv_exit_status()
    if status:
        raise RuntimeError('Release check failed: ' + error[-2000:])
    return json.loads(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--cleanup-plan', type=Path)
    parser.add_argument('--report-dir', type=Path, help='Task-scoped release evidence under reports/')
    args = parser.parse_args()
    report = (ROOT / (args.report_dir or Path('reports/pilot-cleanup-20261118'))).resolve()
    assert report.is_relative_to((ROOT / 'reports').resolve()), 'Release reports must stay under reports/'
    cleanup = json.loads(args.cleanup_plan.read_text()) if args.cleanup_plan else {'delete': []}
    assert all(valid_delete(name) for name in cleanup['delete']), 'Cleanup contains protected paths'
    report.mkdir(parents=True, exist_ok=True)
    manifest = {'sources': sources(), 'delete': cleanup['delete'],
                'retired_modules': cleanup.get('retired_modules', [])}
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    incoming = '/data/pilot-owner/incoming/pilot-release-' + stamp
    backup = '/data/pilot-owner/pilot-cleanup-backups/' + stamp
    client, _ = connect()
    try:
        with client.open_sftp() as sftp:
            sftp.mkdir(incoming)
            with sftp.file(incoming + '/manifest.json', 'w') as f:
                f.write(json.dumps(manifest))
            if args.apply:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode='w:gz') as tar:
                    for name in manifest['sources']:
                        tar.add(ROOT / name, arcname=name, recursive=False)
                data.seek(0)
                sftp.putfo(data, incoming + '/source.tar.gz')
        settings = {'MANIFEST': incoming + '/manifest.json', 'BUNDLE': incoming + '/source.tar.gz',
                    'BACKUP': backup, 'APPLY': args.apply}
        result = remote(client, '\n'.join(k + '=' + repr(v) for k, v in settings.items()) + '\n' + REMOTE_PROGRAM)
        filename = 'remote-applied.json' if args.apply else 'remote-plan.json'
        (report / filename).write_text(json.dumps(result, indent=2), encoding='utf-8')
        if args.apply:
            local_backups = ROOT.parent / 'Pilot清理备份'
            local_backups.mkdir(parents=True, exist_ok=True)
            with client.open_sftp() as sftp:
                sftp.get(backup + '/preimages.tar.gz', str(local_backups / ('a100-preimages-' + stamp + '.tar.gz')))
            stdin, stdout, stderr = client.exec_command('cd ' + REMOTE + ' && .venv/bin/python scripts/pilot.py run --require-data-disk -- .venv/bin/python -m compileall -q src scripts && .venv/bin/python scripts/pilot.py run --require-data-disk -- .venv/bin/python -m pip install --no-index --no-deps --no-build-isolation -e . && bash scripts/harness-ui-rollout.sh', timeout=300)
            output, error = stdout.read().decode(), stderr.read().decode()
            status = stdout.channel.recv_exit_status()
            (report / 'rollout.log').write_text(output + error, encoding='utf-8')
            assert status == 0, 'Host refresh failed; inspect rollout.log and saved preimages'
            print(output.strip())
        print(json.dumps({'changed': len(result['changed']), 'deleted': len(result['delete']),
                          'applied': args.apply, 'backup': result.get('backup'), 'report': filename}))
    finally:
        client.close()


if __name__ == '__main__':
    main()
