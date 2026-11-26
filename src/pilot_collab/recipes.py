"""Immutable self-built tasks; no reference patch or action sequence enters the workspace."""
import hashlib
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from repopilot.models import Task
from repopilot.repository import git

from .fixture import create_fixture

IMAGE = 'pilot-python:0.5.0'


@dataclass(frozen=True)
class Recipe:
    id: str
    title: str
    issue: str
    keywords: tuple[str, ...]
    files: dict[str, str]
    cases: tuple[tuple[dict[str, JsonValue], JsonValue], ...]
    source_roots: tuple[str, ...]


SERVICE = '''import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from logic import compute

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, required=True)
parser.add_argument('--run-id', required=True)
args = parser.parse_args()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'run_id': args.run_id, 'healthy': True}).encode())
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        value = compute(payload)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'value': value}).encode())
HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
'''

RECIPES = {
    'boltons-chunking-v1': Recipe('boltons-chunking-v1', '修复 Boltons 分块补齐并验证接口',
        'This is an injected fault in the pinned open-source Boltons source. chunked_iter must pad the last chunk when fill is explicitly provided, including fill=None. '
        'Without fill, the final short chunk must remain unpadded. Fix only boltons/iterutils.py; preserve all tests, docs and service.py.',
        ('Boltons', '分块补齐'), {}, (({'items': [1,2,3,4], 'size': 3, 'fill': None}, [[1,2,3],[4,None,None]]),
            ({'items': [1,2,3,4], 'size': 3}, [[1,2,3],[4]]),
            ({'items': [], 'size': 3, 'fill': 0}, [])), ('boltons/iterutils.py',)),
    'order-service-v1': Recipe('order-service-v1', '修复订单计价并验证服务',
        '修复商品数量和折扣百分比计算，启动服务并验证金额。', ('订单', '计价'), {},
        (({'items': [[10, 2], [5, 1]], 'discount': 10}, 22.5),
         ({'items': [[7, 3]], 'discount': 0}, 21.0),
         ({'items': [], 'discount': 0}, 0.0)), ('checkout.py', 'money.py')),
    'pagination-service-v1': Recipe('pagination-service-v1', '修复分页边界并验证接口',
        'Pagination uses one-based pages. Fix the slice offset and total_pages rounding. '
        'Empty input has zero total pages. Preserve service.py, tests and API.md. Only change logic.py.',
        ('分页', 'pagination'), {
            'logic.py': '''def compute(payload):
    items, page, size = payload['items'], payload['page'], payload['size']
    start = page * size
    return {'items': items[start:start + size], 'total_pages': len(items) // size}
''',
            'tests/test_logic.py': '''import pytest
from logic import compute
@pytest.mark.parametrize('n,size,page', [(5,2,1),(5,2,2),(5,2,3),(0,3,1),(6,3,2),(1,5,1),(13,4,4)])
def test_pagination(n,size,page):
    items = list(range(n))
    assert compute({'items':items,'page':page,'size':size}) == {'items':items[(page-1)*size:page*size], 'total_pages':(n+size-1)//size}
''',
            'API.md': 'Page numbers start at one. A partial last page counts as a page. Empty input has zero pages.',
        }, (({'items': [1,2,3,4,5], 'page': 1, 'size': 2}, {'items': [1,2], 'total_pages': 3}),
            ({'items': [1,2,3,4,5], 'page': 3, 'size': 2}, {'items': [5], 'total_pages': 3}),
            ({'items': [], 'page': 1, 'size': 2}, {'items': [], 'total_pages': 0})), ('logic.py',)),
    'weighted-metric-v1': Recipe('weighted-metric-v1', '修复加权指标并验证接口',
        'Compute the weighted mean with denominator sum(weights), not row count. '
        'Zero total weight returns 0.0. Preserve service.py and tests. Only change logic.py and weights.py.',
        ('加权', '指标', 'weighted'), {
            'logic.py': '''from weights import denominator
def compute(payload):
    values, weights = payload['values'], payload['weights']
    return sum(v*w for v,w in zip(values,weights)) / denominator(weights)
''',
            'weights.py': 'def denominator(weights):\n    return len(weights)\n',
            'tests/test_logic.py': '''import pytest
from logic import compute
@pytest.mark.parametrize('values,weights,expected', [([10,20],[1,3],17.5),([5],[4],5),([],[],0),([1,2],[0,0],0),([0,10,20],[1,2,1],10),([7,7],[2,8],7)])
def test_weighted(values,weights,expected):
    assert compute({'values':values,'weights':weights}) == expected
''',
            'API.md': 'The weighted mean divides the weighted sum by the sum of weights. Zero total weight returns zero.',
        }, (({'values': [10,20], 'weights': [1,3]}, 17.5),
            ({'values': [], 'weights': []}, 0.0),
            ({'values': [5], 'weights': [4]}, 5.0)), ('logic.py','weights.py')),
    'normalization-service-v1': Recipe('normalization-service-v1', '修复配置归一化并验证接口',
        'Normalize config keys by strip and lowercase; values must retain their original type. '
        'String boolean values TRUE/FALSE are converted case-insensitively after stripping. '
        'Other string values are stripped. Preserve service.py and tests. Only change logic.py.',
        ('归一化', '配置解析', 'normalization'), {
            'logic.py': '''def compute(payload):
    return {key.lower(): str(value).strip() for key,value in payload['config'].items()}
''',
            'tests/test_logic.py': '''import pytest
from logic import compute
@pytest.mark.parametrize('config,expected', [({' ENABLED ':' TRUE '},{'enabled':True}),({'N':3},{'n':3}),({'FLAG':False},{'flag':False}),({'A':' x '},{'a':'x'}),({' B ':'false'},{'b':False}),({},{}),({'x':None},{'x':None}),({'v':[1,2]},{'v':[1,2]})])
def test_config(config,expected):
    assert compute({'config':config}) == expected
''',
            'API.md': 'Normalize keys; preserve typed JSON values. Convert only strings TRUE and FALSE to booleans.',
        }, (({'config': {' ENABLED ': ' TRUE ', 'N': 3}}, {'enabled': True, 'n': 3}),
            ({'config': {'FLAG': False, 'name': ' x '}}, {'flag': False, 'name': 'x'}),
            ({'config': {}}, {})), ('logic.py',)),
}


def catalog():
    return [{'id': r.id, 'title': r.title, 'goal': r.issue, 'origin': 'open-source repository with injected fault' if r.id == 'boltons-chunking-v1' else 'self-built fault-injection task',
             'source_roots': list(r.source_roots)} for r in RECIPES.values()]


def create_task(recipe_id: str, root: Path) -> Task:
    recipe = RECIPES[recipe_id]
    if recipe_id == 'order-service-v1':
        return create_fixture(root).model_copy(update={'image': IMAGE})
    root.mkdir()
    if recipe_id == 'boltons-chunking-v1':
        folder = Path(__file__).resolve().parents[2] / 'evals/external'
        metadata = json.loads((folder / 'boltons-source.json').read_text())
        archive = folder / 'boltons-source.tar.gz'
        if hashlib.sha256(archive.read_bytes()).hexdigest() != metadata['sha256']:
            raise ValueError('External source digest mismatch')
        with tarfile.open(archive) as source:
            total = 0
            for member in source.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError('External source links or special files are forbidden')
                parts = Path(member.name).parts[1:]
                destination = root.joinpath(*parts)
                if not parts or '..' in parts or not destination.resolve().is_relative_to(root.resolve()):
                    raise ValueError('Unsafe external archive path')
                total += member.size
                if total > 10_000_000 or member.size > 2_000_000:
                    raise ValueError('External source exceeds the task size bound')
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.extractfile(member).read())
        code = root / 'boltons/iterutils.py'
        text = code.read_text()
        needle = "        fill_val = kw.pop('fill')"
        if text.count(needle) != 1:
            raise ValueError('Pinned fault injection site changed')
        code.write_text(text.replace(needle, needle + '\n        do_fill = False', 1), newline='\n')
        files = {
            'logic.py': "from boltons.iterutils import chunked_iter\ndef compute(payload):\n    kw = {'fill':payload['fill']} if 'fill' in payload else {}\n    return list(chunked_iter(payload['items'],payload['size'],**kw))\n",
            'tests/test_logic.py': "import pytest\nfrom logic import compute\n@pytest.mark.parametrize('items,size,fill', [([1,2,3,4],3,None),([1],4,0),([1,2],3,'x'),([],3,None),([1,2,3],3,None)])\ndef test_padding(items,size,fill):\n    chunks=[items[i:i+size] for i in range(0,len(items),size)]\n    if chunks and len(chunks[-1])<size:chunks[-1] += [fill]*(size-len(chunks[-1]))\n    assert compute({'items':items,'size':size,'fill':fill}) == chunks\n",
            'tests/test_agent_regression.py': "from boltons.iterutils import chunked,windowed,pairwise,unique,flatten\ndef test_existing_behaviors():\n    assert chunked([1,2,3,4],3)==[[1,2,3],[4]]\n    assert windowed([1,2,3],2)==[(1,2),(2,3)]\n    assert pairwise([1,2,3])==[(1,2),(2,3)]\n    assert unique([1,2,1])==[1,2]\n    assert flatten([[1],[2,3]])==[1,2,3]\n",
            'SOURCE.json': json.dumps({**metadata, 'fault': 'self-injected do_fill override; not an upstream bug claim'}),
        }
    else:
        files = recipe.files
    for name, content in {**files, 'service.py': SERVICE}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='\n')
    git(root, 'init')
    git(root, 'config', 'user.name', 'Pilot fixture')
    git(root, 'config', 'user.email', 'fixture@localhost')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'test: immutable registered task')
    return Task(id=recipe_id, repo=str(root), commit=git(root, 'rev-parse', 'HEAD').stdout.strip(),
                issue=recipe.issue, target=['python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests/test_logic.py'],
                regression=['python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests/test_agent_regression.py' if recipe_id == 'boltons-chunking-v1' else 'tests'],
                source_roots=list(recipe.source_roots), image=IMAGE)
