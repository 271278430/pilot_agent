from pydantic import Field

from pilot_core.contracts import Contract, EmptyInput

from .contracts import DeliveryToolResult


class ReadArgs(Contract):
    path: str = Field(max_length=180)


class EditArgs(ReadArgs):
    old: str = Field(min_length=1, max_length=12000)
    new: str = Field(max_length=12000)


class RepairArgs(Contract):
    action: str = Field(pattern=r"^(socket|token_limit|port)$")


DESCRIPTIONS = {
    "status": (
        "read",
        EmptyInput,
        "Read current release, candidate, runtime and latest evidenced failure.",
    ),
    "preflight": (
        "read",
        EmptyInput,
        "Probe genuine model engine and pinned runtime plan before source/deploy work.",
    ),
    "read": ("read", ReadArgs, "Read a registered service source or fixed API/test file."),
    "edit": (
        "source_write",
        EditArgs,
        "Replace one exact source occurrence; only app.py and adapter.py may change.",
    ),
    "source_test": (
        "command",
        EmptyInput,
        "Run protected source tests inside verified networkless Docker.",
    ),
    "candidate": (
        "state_write",
        EmptyInput,
        "Export full patch to fixed baseline; certify source tests, hash and parent candidate.",
    ),
    "deploy": (
        "process_start",
        EmptyInput,
        "Deploy current source-certified candidate and selected genuine model capability.",
    ),
    "inspect": (
        "read",
        EmptyInput,
        "Read actual service health/failure evidence. No invented successful result.",
    ),
    "repair_env": (
        "state_write",
        RepairArgs,
        "Repair only the action permitted by an actual recorded environment failure.",
    ),
    "verify": (
        "verify",
        EmptyInput,
        "Independent genuine model API acceptance; failures include reproducible evidence.",
    ),
    "finish": (
        "state_write",
        EmptyInput,
        "Finish only if the latest candidate and environment have independent acceptance.",
    ),
    "delegate_repo": (
        "source_write",
        EmptyInput,
        "Invoke RepoPilot official Session to certify or repair current source using latest evidence.",
    ),
    "delegate_infra": (
        "process_start",
        EmptyInput,
        "Invoke InfraPilot official Session to deploy current candidate and fix evidenced environment issues.",
    ),
}
ROLES = {
    "delivery_single": [
        "status",
        "preflight",
        "read",
        "edit",
        "source_test",
        "candidate",
        "deploy",
        "inspect",
        "repair_env",
        "verify",
        "finish",
    ],
    "delivery_supervisor": [
        "status",
        "preflight",
        "delegate_repo",
        "delegate_infra",
        "verify",
        "finish",
    ],
    "delivery_repo": ["status", "read", "edit", "source_test", "candidate"],
    "delivery_infra": ["status", "preflight", "deploy", "inspect", "repair_env"],
}


def build():
    from pilot_core.capabilities import Capability

    return [
        Capability(
            f"{role}.{name}", "delivery", name, role, description, effect, model, DeliveryToolResult
        )
        for role, names in ROLES.items()
        for name in names
        for effect, model, description in [DESCRIPTIONS[name]]
    ]
