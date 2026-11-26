"""Trusted entry point outside editable engine sources, mounted read-only in OCI."""

import hashlib
import importlib
import importlib.metadata
import json
import os
import runpy
import sys
from pathlib import Path


def main():
    runtime, receipt, expected, *arguments = sys.argv[1:]
    expected = json.loads(expected)
    wait_for_authorization = runpy.run_path(str(Path(__file__).with_name('engine_admission.py')))['wait_for_authorization']
    wait_for_authorization(expected['launch_authorization'],
                           {k: expected[k] for k in ('launch_nonce', 'candidate_digest',
                            'source_digest', 'wheel_sha256', 'launcher_sha256', 'admission_sha256')})
    sys.path.insert(0, runtime)
    os.environ["PYTHONPATH"] = runtime
    # Resolve and record before importing editable code. Its mounted files
    # are independently hashed by the external executor before acceptance.
    spec = importlib.util.find_spec("sglang")
    module_path = Path(spec.origin).resolve()
    if not module_path.is_relative_to(Path(runtime).resolve()):
        raise ValueError("The service imported a different engine package")
    version = importlib.metadata.version("sglang")
    if version != expected["version"]:
        raise ValueError("Loaded engine distribution version differs")
    value = {
        **expected,
        "module": str(module_path),
        "module_sha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
        "distribution_version": version,
        "namespace_pid": os.getpid(),
        "gpu_ids": os.environ["PILOT_GPU_IDS"],
        'cuda_visible_devices':os.environ['CUDA_VISIBLE_DEVICES'],
    }
    path = Path(receipt)
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)
    sys.argv = ["sglang.launch_server", *arguments]
    runpy.run_module("sglang.launch_server", run_name="__main__")


if __name__ == "__main__":
    main()
