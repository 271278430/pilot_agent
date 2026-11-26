"""Project-owned runtime paths and process environment policy."""

import os
from dataclasses import dataclass
from pathlib import Path

from repopilot.storage import atomic_json

ENV_PATHS = {
    "HOME": ".local/home",
    "XDG_CONFIG_HOME": ".local/config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
    "XDG_CACHE_HOME": ".cache/xdg",
    "TMPDIR": ".local/tmp",
    "TMP": ".local/tmp",
    "TEMP": ".local/tmp",
    "PIP_CACHE_DIR": ".cache/pip",
    "UV_CACHE_DIR": ".cache/uv",
    "npm_config_cache": ".cache/npm",
    "HF_HOME": ".cache/huggingface",
    "TORCH_HOME": ".cache/torch",
    "TRITON_CACHE_DIR": ".cache/triton",
    "CUDA_CACHE_PATH": ".cache/cuda",
    "VLLM_CACHE_ROOT": ".cache/vllm",
    "MODELSCOPE_CACHE": ".cache/modelscope",
    "TILELANG_CACHE_DIR": ".cache/tilelang",
    "NUMBA_CACHE_DIR": ".cache/numba",
    "TIKTOKEN_CACHE_DIR": ".cache/tiktoken",
    "VIRTUALENV_OVERRIDE_APP_DATA": ".cache/virtualenv",
    "PYTHONPYCACHEPREFIX": ".cache/pycache",
    "RUFF_CACHE_DIR": ".cache/ruff",
}



@dataclass(frozen=True)
class StorageLayout:
    root: Path

    def __post_init__(self):
        original = Path(self.root).absolute()
        if any(p.is_symlink() for p in [original, *original.parents]):
            raise ValueError("Project storage cannot follow symbolic links")
        object.__setattr__(self, "root", original.resolve())

    def path(self, relative):
        target = self.root / relative
        if any(p.is_symlink() for p in [target, *target.parents] if p != self.root.parent):
            raise ValueError("Managed storage cannot follow symbolic links")
        if not target.resolve().is_relative_to(self.root):
            raise ValueError("Storage path escaped project root")
        return target

    def require_data_disk(self):
        if os.name == "nt":
            return
        data = Path("/data")
        if not self.root.is_relative_to(data) or not data.is_dir():
            raise ValueError("Production storage must be below /data")
        if data.stat().st_dev == Path("/").stat().st_dev:
            raise ValueError("/data must be a separate data filesystem")

    def prepare(self, strict=False):
        if strict:
            self.require_data_disk()
        for relative in {*ENV_PATHS.values(), ".cache/build", ".local/logs", ".local/storage", "runs", "resources"}:
            path = self.path(relative)
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt" and relative.startswith((".local/", ".cache/")):
                path.chmod(0o700)
        value = {
            "version": 1,
            "project": str(self.root),
            "environment_paths": {k: str(self.path(v)) for k, v in ENV_PATHS.items()},
            "roles": {
                "source": "src, harness, scripts, tests, docs, skills, prompts",
                "private_state": ".local",
                "rebuildable_cache": ".cache",
                "task_evidence": "runs",
                "registered_assets": "resources (or the separately owned configured root)",
                "reports": "reports",
                "archives": str(self.root.parent / "pilot-archives"),
                "release_backups": str(self.root.parent / "pilot-cleanup-backups"),
                "docker": "Shared daemon storage; inspect DockerRootDir independently",
            },
        }
        atomic_json(self.path(".local/storage/layout.json"), value)
        return value

    def environment(self, base=None, strict=False):
        self.prepare(strict)
        result = dict(os.environ if base is None else base)
        # npm treats these spellings as the same option; remove inherited aliases.
        result = {k: v for k, v in result.items() if k.lower() != "npm_config_cache"}
        result.update({k: str(self.path(v)) for k, v in ENV_PATHS.items()})
        result.update(PILOT_HOST_ROOT=str(self.root), PYTHONNOUSERSITE="1")
        if strict:
            result["PILOT_REQUIRE_DATA_DISK"] = "1"
        return result
