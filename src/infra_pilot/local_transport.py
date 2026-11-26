"""Same-host recipe transport: no SSH credential and no model-supplied commands."""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .spec import InfraSpec


class LocalRemote:
    def __init__(self, spec: InfraSpec):
        self.spec = spec
        self.root = Path(spec.remote)
        if Path(spec.root) != Path('/data/pilot-owner') or not self.root.is_relative_to(Path(spec.root)):
            raise ValueError('Local recipes are limited to /data/pilot-owner')

    def path(self, relative):
        path = self.root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('Recipe path escaped workspace')
        return path

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise ValueError('Invalid local recipe root')
        for name in ('jobs', 'logs', 'reports', 'models', 'checkpoints'):
            self.path(name).mkdir(exist_ok=True)
        shutil.copyfile(Path(__file__).with_name('worker.py'), self.path('worker.py'))
        self.write('spec.json', self.spec.model_dump())

    def worker(self, *args):
        result = subprocess.run([sys.executable, str(self.path('worker.py')), str(self.root), *args],
                                cwd=self.root, text=True, capture_output=True, timeout=45)
        if result.returncode:
            raise RuntimeError('local_recipe_error: ' + result.stderr[-1500:])
        return json.loads(result.stdout)

    def wait(self, job, seconds=20):
        end = time.monotonic() + seconds
        while True:
            state = self.worker('inspect', job)
            if state['status'] != 'running' or time.monotonic() >= end:
                return state
            time.sleep(1)

    def write(self, relative, value):
        from repopilot.storage import atomic_json

        atomic_json(self.path(relative), value)

    def fetch(self, relative, local):
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.path(relative), local)

    def read(self, relative):
        return json.loads(self.path(relative).read_text())

    def close(self):
        pass
