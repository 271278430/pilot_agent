from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import Field

from repopilot.storage import atomic_json

from .capabilities import for_role, resolve
from .contracts import Contract

PROJECT = Path(__file__).resolve().parents[2]


class SkillDefinition(Contract):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    role: str
    description: str
    capabilities: list[str]
    path: str


SKILLS = [
    SkillDefinition(name='deliver-trained-model',role='delivery_single',description='单会话完成模型工具服务交付与代码/环境闭环。',capabilities=['delivery_single.preflight','delivery_single.candidate','delivery_single.deploy','delivery_single.verify','delivery_single.finish'],path='skills/delivery_single/deliver-trained-model/SKILL.md'),
    SkillDefinition(name='coordinate-model-delivery',role='delivery_supervisor',description='依据候选与实测故障委派源码和环境角色，独立验收。',capabilities=['delivery_supervisor.delegate_repo','delivery_supervisor.delegate_infra','delivery_supervisor.verify'],path='skills/delivery_supervisor/coordinate-model-delivery/SKILL.md'),
    SkillDefinition(name='repair-model-adapter',role='delivery_repo',description='修复模型服务接口代码，交付通过复验的新候选。',capabilities=['delivery_repo.read','delivery_repo.edit','delivery_repo.candidate'],path='skills/delivery_repo/repair-model-adapter/SKILL.md'),
    SkillDefinition(name='deploy-model-candidate',role='delivery_infra',description='部署已验收代码和真实模型，修复有证据的自身运行配置。',capabilities=['delivery_infra.deploy','delivery_infra.inspect','delivery_infra.repair_env'],path='skills/delivery_infra/deploy-model-candidate/SKILL.md'),
    SkillDefinition(name="repair-python-issue", role="repo", description="定位并修复已登记Python问题，保留失败与回归证据。",
                    capabilities=["repo.search", "repo.read", "repo.edit", "repo.test", "repo.evidence"], path="skills/repo/repair-python-issue/SKILL.md"),
    SkillDefinition(name="deliver-local-service", role="supervisor", description="按补丁验收、部署、服务验收的依赖交付固定任务。",
                    capabilities=["supervisor.delegate_repo", "supervisor.delegate_infra", "supervisor.verify", "supervisor.finish"], path="skills/supervisor/deliver-local-service/SKILL.md"),
    SkillDefinition(name="recover-owned-service", role="infra", description="部署登记本机服务，只处理自身端口故障。",
                    capabilities=["infra.probe", "infra.stage", "infra.start", "infra.diagnose", "infra.repair"], path="skills/infra/recover-owned-service/SKILL.md"),
    SkillDefinition(name='build-model-environment', role='ml', description='按登记依赖链验证CUDA/NCCL、模型推理、训练恢复与服务，不改宿主驱动。',
                    capabilities=['ml.status','ml.advance','ml.inspect','ml.diagnose','ml.repair'], path='skills/ml/build-model-environment/SKILL.md'),
]


def skill_catalog(role: str | None = None):
    selected = [item for item in SKILLS if role is None or item.role == role]
    for skill in selected:
        file = PROJECT / skill.path
        if file.is_symlink() or not file.resolve().is_relative_to((PROJECT / "skills" / skill.role).resolve()):
            raise ValueError("Skill path escaped role scope")
        if not file.is_file():
            raise ValueError("Registered skill is missing")
        for capability in skill.capabilities:
            owner, name = capability.split(".", 1)
            if owner != skill.role:
                raise ValueError("Skill cannot grant cross-role capabilities")
            resolve(owner, name)
    return selected


def skill_plugins(role: str):
    skill_catalog(role)
    return [
        {"id": "pilot-skills", "name": "@deepseek-ai/dsh-skill"},
        {"id": "pilot-skill-filesystem", "name": "@deepseek-ai/dsh-skill-filesystem", "inject": ["skills"],
         "config": {"includeDefaultRoots": False, "customSkillDirs": [str(PROJECT / "skills" / role)], "watch": False}},
        {"id": "pilot-tool-skill", "name": "@deepseek-ai/dsh-tool-skill", "inject": ["agents", "tools", "skills", "workingDirectory"],
         "config": {"catalogDescriptionMaxLength": 180}},
    ]


def allowed_tools(role: str):
    return [f"mcp__{item.server}__{item.name}" for item in for_role(role)] + ["skill"]


def system_prompt(role: str) -> str:
    return (PROJECT / "prompts" / f"{role}.txt").read_text(encoding="utf-8")


def prompt_manifest(directory: Path, role: str):
    text = system_prompt(role)
    value = {"version": 1, "role": role, "template": f"prompts/{role}.txt",
             "template_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "context_role": "user", "dynamic_sources_are_system": False,
             "tools": allowed_tools(role), "skills": [s.name for s in skill_catalog(role)]}
    atomic_json(directory / "prompt-manifest.json", value)
    return value
