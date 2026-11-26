"""Delete reviewed, unused project files only after verifying their saved preimage."""
import argparse
import ast
import hashlib
import json
import zipfile
from pathlib import Path

from audit_cleanup import ROOT, imports, inventory, modules

REPORT = ROOT / 'reports/pilot-cleanup-20261118'
KEEP_SCRIPTS = {
    'audit_cleanup.py', 'cleanup_project.py', 'deploy.py', 'remote_project.py', 'web_tunnel.py',
    'harness_host_login.py', 'harness_host_live.py', 'harness-host-rollout.sh', 'harness-ui-rollout.sh',
    'bootstrap_model_preview.py', 'bootstrap_delivery_engine.py', 'bootstrap_remote.py',
    'prepare_ml_environment.py', 'prepare_docker_remote.py', 'prepare_wasi.py',
    'analyze_delivery.py', 'benchmark_infra.py',
}
OBSOLETE_DOCS = {
    'ARCHITECTURE_EXPLAINED.md', 'COLLABORATION_DESIGN.md', 'COLLABORATION_USAGE.md',
    'CURRENT_SYSTEM_AUDIT.md', 'MODEL_APPLICATION_DESIGN.md', 'MODEL_DELIVERY_BUSINESS.md',
    'MODEL_DELIVERY_GUIDE.md', 'MODULE_ARCHITECTURE.md', 'NATURAL_ROUTING.md',
    'SYSTEM_BLUEPRINT.md', 'WALKTHROUGH_A100.md', 'WEB_WORKBENCH.md', 'WORKBENCH_PLAN.md',
    'MODEL_DELIVERY_DESIGN.md', 'MODEL_DELIVERY_USAGE.md',
}


def plan():
    value = inventory()
    retired = set(value['unreachable'])
    # Empty __init__ files are preserved wherever their package remains active.
    selected = {value['paths'][name] for name in retired}
    selected.update('scripts/' + p.name for p in (ROOT/'scripts').iterdir()
                    if p.is_file() and p.name not in KEEP_SCRIPTS)
    for group in ('web','collab_web','docs/diagrams','docs/schemas','docs/references'):
        selected.update(p.relative_to(ROOT).as_posix() for p in (ROOT/group).rglob('*')
                        if p.is_file() and 'node_modules' not in p.parts and '__pycache__' not in p.parts)
    selected.update('docs/' + name for name in OBSOLETE_DOCS if (ROOT/'docs'/name).is_file())
    available = modules()
    retired_tests = set()
    for p in (ROOT/'tests').glob('test_*.py'):
        dependencies = imports(p,'tests.'+p.stem,available)
        if dependencies & retired:
            retired_tests.add(p.stem)
    # Remove tests that depended only on a retired test fixture, too.
    changed = True
    while changed:
        changed = False
        for p in (ROOT/'tests').glob('test_*.py'):
            if p.stem in retired_tests:
                continue
            for n in ast.walk(ast.parse(p.read_text(encoding='utf-8-sig'))):
                if isinstance(n,ast.ImportFrom) and n.module in {'tests.'+name for name in retired_tests}:
                    retired_tests.add(p.stem)
                    changed = True
                    break
    selected.update('tests/'+name+'.py' for name in retired_tests)
    result={'delete':sorted(selected),'retired_modules':sorted(retired),
            'retired_tests':sorted(retired_tests),'active_modules':len(value['reachable'])}
    (REPORT/'cleanup-plan.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


def apply():
    reviewed = json.loads((REPORT/'cleanup-plan.json').read_text())
    original = json.loads((REPORT/'preimage.json').read_text())
    archive = Path(original['archive']).resolve()
    assert archive.is_file() and not archive.is_relative_to(ROOT), 'Backup must be outside active project'
    paths=[]
    with zipfile.ZipFile(archive) as z:
        for name in reviewed['delete']:
            p=ROOT/name
            if not p.exists():
                continue
            assert not p.is_symlink() and p.resolve().is_relative_to(ROOT) and p.is_file(),name
            expected=original['files'][name]
            assert hashlib.sha256(p.read_bytes()).hexdigest()==expected,name
            assert hashlib.sha256(z.read(name)).hexdigest()==expected,name
            paths.append((name,p))
    for _name,p in paths:
        p.unlink()
    (REPORT/'local-deleted.json').write_text(json.dumps({'archive':str(archive),'files':[n for n,p in paths]},indent=2),encoding='utf-8')
    print(json.dumps({'deleted_files':len(paths),'recoverable_archive':str(archive)},ensure_ascii=False))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if args.apply:
        apply()
    else:
        value=plan()
        print(json.dumps({'files':len(value['delete']),'retired_modules':value['retired_modules'],
                          'retired_tests':value['retired_tests']},indent=2))
