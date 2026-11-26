from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from repopilot.models import ToolResult
from repopilot.tools import TOOL_SPECS

from .contracts import (
    DiagnosisResult,
    EmptyInput,
    FinishResult,
    Outcome,
    ProbeResult,
    RepairResult,
    RepoDelivery,
    SessionReport,
    StageResult,
    StartResult,
    StatusResult,
    VerificationResult,
)


@dataclass(frozen=True)
class Capability:
    id: str
    server: str
    name: str
    role: str
    description: str
    effect: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    requires: tuple[str, ...] = ()

    def input(self, value: dict) -> dict:
        return self.input_model.model_validate(value).model_dump(mode="json")

    def output(self, value: dict) -> dict:
        return self.output_model.model_validate(value).model_dump(mode="json", exclude_none=True)

    def descriptor(self):
        return {
            "id": self.id, "version": "1.0", "server": self.server,
            "name": self.name, "role": self.role, "description": self.description,
            "effect": self.effect, "requires": list(self.requires),
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
            "implementation": "self-authored business adapter",
        }


COLLAB = {
    "supervisor": {
        "status": (StatusResult, "read", (), "读取角色状态与可用产物"),
        "delegate_repo": (RepoDelivery, "source_write", (), "委派登记源码任务并独立验收"),
        "delegate_infra": (SessionReport, "process_start", ("repo.accepted",), "委派本机服务部署"),
        "verify": (VerificationResult, "verify", ("repo.accepted", "infra.healthy"), "独立源码与HTTP验收"),
        "finish": (FinishResult, "state_write", ("verification.settled",), "依据验收结束任务"),
    },
    "infra": {
        "probe": (ProbeResult, "read", (), "探测本机固定服务环境"),
        "stage": (StageResult, "source_write", ("probe", "repo.accepted"), "校验补丁后准备部署副本"),
        "start": (StartResult, "process_start", ("stage",), "启动任务持有的服务"),
        "diagnose": (DiagnosisResult, "read", ("startup.failure",), "依据实际失败分类"),
        "repair": (RepairResult, "state_write", ("owned_port_conflict",), "有界修复任务自身端口"),
    },
}


def build_registry() -> dict[str, Capability]:
    rows = []
    for name, (model, description) in TOOL_SPECS.items():
        effect = "source_write" if name == "edit" else "command" if name in {"shell", "test"} else "read"
        rows.append(Capability(f"repo.{name}", "repopilot", name, "repo", description, effect, model, ToolResult))
    for role, specs in COLLAB.items():
        for name, (output, effect, requires, description) in specs.items():
            rows.append(Capability(f"{role}.{name}", "collab", name, role, description, effect, EmptyInput, Outcome[output], requires))
    from infra_pilot.service import SPECS

    from .contracts import RemoteToolResponse

    for name, (model, description) in SPECS.items():
        effect = 'read' if name in {'status', 'inspect', 'diagnose'} else 'process_start' if name == 'advance' else 'state_write'
        rows.append(Capability(f'ml.{name}', 'infra', name, 'ml', description, effect, model, RemoteToolResponse))
    from pilot_delivery.capabilities import build
    rows.extend(build())
    return {row.id: row for row in rows}


REGISTRY = build_registry()


def for_role(role: str) -> list[Capability]:
    return [row for row in REGISTRY.values() if row.role == role]


def operation_names(role: str) -> set[str]:
    return {row.name for row in for_role(role)}


def resolve(role: str, name: str) -> Capability:
    try:
        return REGISTRY[f"{role}.{name}"]
    except KeyError as exc:
        raise ValueError(f"Unregistered capability for role {role}") from exc
