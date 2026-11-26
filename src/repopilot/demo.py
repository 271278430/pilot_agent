from __future__ import annotations

from pathlib import Path

from .harness import execute, prepare
from .mock_model import ScriptedModel
from .models import Budget, Task
from .repository import git
from .storage import atomic_json

DEMO_FILES = {
    "money.py": "def apply_discount(amount, percentage):\n    return amount * (1 - percentage)\n",
    "checkout.py": "from money import apply_discount\n\ndef total(items, discount_percent=0):\n    subtotal = sum(price for price, quantity in items)\n    return round(apply_discount(subtotal, discount_percent), 2)\n",
    "tests/test_checkout.py": "from checkout import total\n\ndef test_quantity_and_discount():\n    assert total([(10, 2), (5, 1)], 10) == 22.5\n",
    "tests/test_regression.py": "from checkout import total\nfrom money import apply_discount\n\ndef test_empty_order():\n    assert total([], 0) == 0\n\ndef test_no_discount():\n    assert total([(7, 3)], 0) == 21\n\ndef test_discount_units():\n    assert apply_discount(20, 25) == 15\n",
    ".gitignore": "__pycache__/\n.pytest_cache/\n",
    ".gitattributes": "* text eol=lf\n",
}

DEMO_ACTIONS = [
    ("test", {"suite": "target"}),
    ("search", {"query": "total apply_discount subtotal quantity"}),
    ("read", {"path": "checkout.py", "end": 30}),
    ("read", {"path": "money.py", "end": 30}),
    ("edit", {"path": "checkout.py", "old": "sum(price for price, quantity in items)", "new": "sum(price * quantity for price, quantity in items)"}),
    ("test", {"suite": "target"}),
    ("evidence", {"excluded_hypothesis": "Fixing quantities alone does not fix percentage units."}),
    ("edit", {"path": "money.py", "old": "(1 - percentage)", "new": "(1 - percentage / 100)"}),
    ("test", {"suite": "target"}),
    ("test", {"suite": "regression"}),
]


def create_repo(root: Path) -> Task:
    if root.exists():
        raise ValueError(f"Refusing to overwrite demo repository: {root}")
    root.mkdir(parents=True)
    for name, text in DEMO_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(root, "init")
    git(root, "config", "user.name", "RepoPilot fixture")
    git(root, "config", "user.email", "fixture@localhost")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: fixed cross-file issue baseline")
    return Task(id="checkout-cross-file", repo=str(root.resolve()), commit=git(root, "rev-parse", "HEAD").stdout.strip(), issue="Checkout total ignores item quantities and treats a percentage as a ratio. total([(10, 2), (5, 1)], 10) must equal 22.5. Correct the source implementation; preserve the tests.", target=["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_checkout.py"], regression=["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"])


def run_demo(output: Path, variant: str = "compressed", mode: str = "normal", budget: Budget | None = None) -> dict:
    import uuid

    output.mkdir(parents=True, exist_ok=True)
    task = create_repo(output / ("demo-repo-" + uuid.uuid4().hex[:8]))
    with ScriptedModel(DEMO_ACTIONS, mode=mode) as model:
        run = prepare(task, output, "scripted-fixture", model.url, environment="local", variant=variant, policy="scripted", budget=budget or Budget())
        atomic_json(run / "task.json", task.model_dump())
        report = execute(run, "local-scripted-fixture-key")
        atomic_json(run / "mock-requests.json", model.requests)
    return report
