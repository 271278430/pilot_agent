from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pilot_core.capabilities import operation_names

Role = Literal["supervisor", "repo", "infra", "verifier", "operator"]
OPERATIONS = {
    role: operation_names(role) for role in ("supervisor", "infra")
}


class NewRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="修复订单计算并验证服务", min_length=1, max_length=100)
    goal: str = Field(
        default="修复数量与百分比计算错误，处理服务端口冲突，并独立验收。", max_length=3000
    )
    controller: Literal["scripted", "model"] = "scripted"
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    recipe_id: str = "order-service-v1"
    orchestration: Literal["multi", "single"] = "multi"

    @model_validator(mode="after")
    def registered_recipe(self):
        from .recipes import RECIPES

        if self.recipe_id not in RECIPES:
            raise ValueError("Task recipe is not registered")
        if self.controller == 'scripted' and self.recipe_id != 'order-service-v1':
            raise ValueError("New tasks require a real-model controller")
        return self


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    message_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    sender: Literal["supervisor", "infra"]
    operation: str
    payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def allowed(self):
        if self.operation not in OPERATIONS[self.sender] or self.payload:
            raise ValueError("Unregistered operation or unexpected arguments")
        return self


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "pause", "resume", "cancel"]
