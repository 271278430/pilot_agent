"""Build the project sandbox image without changing the host Docker configuration."""
import json
from pathlib import Path

from pilot_core.docker import docker

ROOT = Path(__file__).resolve().parents[1]
if ROOT != Path('/data/pilot-owner/pilot'):
    raise RuntimeError('Unexpected deployment root')
for image in ('public.ecr.aws/docker/library/python:3.11-slim', 'docker.m.daocloud.io/library/python:3.11-slim'):
    print('Pulling', image, flush=True)
    result = docker(['pull', image], timeout=240, check=False)
    print(result.stdout[-2000:] + result.stderr[-1000:], flush=True)
    if result.returncode == 0:
        break
else:
    raise RuntimeError('No base image available')
folder = ROOT / '.local/docker'
folder.mkdir(exist_ok=True)
(folder / 'Dockerfile').write_text(f'FROM {image}\nRUN pip install --no-cache-dir pytest==8.3.5 -i https://pypi.tuna.tsinghua.edu.cn/simple\nENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\nWORKDIR /workspace\nUSER 65534:65534\n')
result = docker(['build', '-t', 'pilot-python:0.5.0', str(folder)], timeout=300)
print(result.stdout[-2000:], flush=True)
image_id = docker(['image', 'inspect', 'pilot-python:0.5.0', '--format', '{{.Id}}']).stdout.strip()
(ROOT / '.local/bootstrap-ready.json').write_text(json.dumps({'ready': True, 'root': str(ROOT), 'image': 'pilot-python:0.5.0', 'image_id': image_id}))
