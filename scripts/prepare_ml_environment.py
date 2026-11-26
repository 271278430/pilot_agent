"""Provision the registered ML recipe inside the user-authorized root."""
import hashlib
import json
import shutil
from pathlib import Path

from infra_pilot.local_transport import LocalRemote
from infra_pilot.spec import InfraSpec
from infra_pilot.worker import MODEL_FILES, PINNED_WEIGHT_SHA, provision

spec = InfraSpec(host='local', username='local', root='/data/pilot-owner', project='infrapilot-ml-env-20261117',
                 gpu_ids=[6,7], train_steps=12, package_source='tuna', service_port=18769)
remote = LocalRemote(spec)
remote.initialize()
root = Path(spec.remote)
# Reuse only verified pinned model artifacts; never alter the old experiment.
old = Path('/data/pilot-owner/infrapilot-smollm-001')
if old.is_dir():
    weights = old / 'models/smollm/model.safetensors'
    if weights.is_file() and hashlib.sha256(weights.read_bytes()).hexdigest() == PINNED_WEIGHT_SHA:
        target = root / 'models/smollm'
        target.mkdir(exist_ok=True)
        for name in MODEL_FILES:
            source = old / 'models/smollm' / name
            if source.is_file() and not source.is_symlink():
                shutil.copyfile(source, target / name)
        receipt = old / 'reports/download.json'
        if receipt.is_file() and not receipt.is_symlink():
            shutil.copyfile(receipt, root / 'reports/download.json')
        print('Reused SHA-verified model files; source unchanged', flush=True)
result = provision(root, spec.model_dump())
(root / 'reports/bootstrap.json').write_text(json.dumps(result))
print(json.dumps(result), flush=True)
