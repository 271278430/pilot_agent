from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    repo: str
    commit: str = "HEAD"
    issue: str = Field(min_length=1)
    target: list[str] = Field(min_length=1)
    regression: list[str] = Field(min_length=1)
    image: str = "repopilot-python:0.1.0"
    # Optional additional source roots; test/config paths always stay protected.
    source_roots: list[str] = Field(default_factory=lambda: ["."])


class Budget(BaseModel):
    max_steps: int = Field(default=40, ge=1)
    max_tokens: int = Field(default=120000, ge=1)
    context_tokens: int = Field(default=12000, ge=2048)
    output_tokens: int = Field(default=2048, ge=128)
    max_cost: float = Field(default=5, gt=0)
    input_price: float = Field(default=0, ge=0)
    output_price: float = Field(default=0, ge=0)
    command_timeout: int = Field(default=60, ge=1, le=600)
    wall_timeout: int = Field(default=900, ge=1)
    no_progress_limit: int = Field(default=4, ge=2)


class RunConfig(BaseModel):
    task: Task
    workspace: str
    baseline: str
    environment: Literal["local", "docker"] = "docker"
    variant: Literal["search", "repomap", "compressed"] = "compressed"
    provider: str = "repopilot-gateway"
    model: str
    base_url: str
    policy: Literal["model", "scripted"] = "model"
    budget: Budget = Field(default_factory=Budget)
    source_hashes: dict[str, str]


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    action_id: str
    tool: str
    exit_code: int
    duration_ms: int
    output: str
    error: str | None = None
    recovered: bool = False


class Acceptance(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    passed: bool = False
    reason: str
    baseline: str
    environment: str
    image_id: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    baseline_target: dict | None = None
    target: dict | None = None
    regression: dict | None = None
