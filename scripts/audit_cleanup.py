"""Read-only Python dependency inventory for the supported Pilot entrypoints."""
import ast
import importlib.util
import json
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOTS = ["pilot_host.cli", "pilot_host.bridge", "pilot_projects.api_engine",
         "pilot_delivery.preview_engine", "pilot_delivery.engine",
         "repopilot.cli", "infra_pilot.cli", "infra_pilot.worker", "repopilot.mock_model"]


def modules():
    result = {}
    for path in (ROOT / "src").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(ROOT / "src").with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        result[".".join(parts)] = path
    return result


def imports(path, name, available):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = ("." * node.level) + (node.module or "")
            if node.level:
                module = importlib.util.resolve_name(module, package)
            found.add(module)
            found.update(module + "." + alias.name for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in available:
            found.add(node.value)
    for item in tuple(found):
        parts = item.split(".")
        found.update(".".join(parts[:i]) for i in range(1, len(parts)))
    return found & available.keys()


def inventory():
    available = modules()
    graph = {name: sorted(imports(path, name, available)) for name, path in available.items()}
    initial = [*ROOTS, *{name.split('.')[0] for name in ROOTS}]
    pending, reached = deque(initial), set()
    while pending:
        name = pending.popleft()
        if name in reached:
            continue
        assert name in available, name
        reached.add(name)
        pending.extend(graph[name])
    return {"roots": ROOTS, "reachable": sorted(reached), "unreachable": sorted(set(available) - reached),
            "paths": {name: path.relative_to(ROOT).as_posix() for name, path in available.items()}, "graph": graph}


if __name__ == "__main__":
    value = inventory()
    report = ROOT / "reports/pilot-cleanup-20261118"
    report.mkdir(exist_ok=True)
    (report / "dependency-audit.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
    print(json.dumps({"reachable_modules": len(value["reachable"]), "unreachable": value["unreachable"]}, indent=2))
