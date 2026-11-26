"""Domain requests, independent of the official Session/turn lifecycle."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServingConfig(Contract):
    context_length: int = Field(default=4096, ge=512, le=32768)
    mem_fraction_static: float = Field(default=0.18, ge=0.05, le=0.6)
    disable_cuda_graph: bool = True
    attention_backend: str = Field(default='triton',pattern=r'^[a-z0-9_]{1,40}$')
    max_new_tokens: int = Field(default=96, ge=8, le=1024)
    stop: list[str] = Field(default_factory=list, max_length=4)


class Workload(Contract):
    cache_mode: Literal["cold", "warm_prefix"] = "cold"
    seed: int = Field(default=17, ge=0, le=4294967295)
    input_lengths: list[int] = Field(
        default_factory=lambda: [128, 1024], min_length=1, max_length=4
    )
    concurrency: list[int] = Field(default_factory=lambda: [1, 2], min_length=1, max_length=4)
    requests_per_case: int = Field(default=4, ge=2, le=32)
    max_new_tokens: int = Field(default=48, ge=8, le=512)
    timeout_seconds: int = Field(default=90, ge=5, le=300)

    @model_validator(mode="after")
    def limits(self):
        if any(n < 16 or n > 16384 for n in self.input_lengths):
            raise ValueError("Input lengths must be 16–16384 tokens")
        if any(n < 1 or n > 16 for n in self.concurrency):
            raise ValueError("Concurrency must be 1–16")
        if len(set(self.input_lengths)) != len(self.input_lengths) or len(
            set(self.concurrency)
        ) != len(self.concurrency):
            raise ValueError("Duplicate matrix entries")
        if len(self.input_lengths) * len(self.concurrency) * self.requests_per_case > 256:
            raise ValueError("Test matrix exceeds 256 requests")
        return self


class InfraPlan(Contract):
    title: str = Field(min_length=2, max_length=80)
    goal: str = Field(min_length=5, max_length=2400)
    kind: Literal["deploy_verify", "diagnose_repair", "compare", "test"] = "deploy_verify"
    machine_id: str = Field(default="a100-server", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    gpu_ids: list[int] = Field(default_factory=lambda: [7], min_length=1, max_length=8)
    model_asset: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    image_asset: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    strategy: Literal["auto", "single", "multi"] = "auto"
    serving: ServingConfig = Field(default_factory=ServingConfig)
    workload: Workload = Field(default_factory=Workload)
    target_task_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    candidate_ids: list[str] = Field(default_factory=list, max_length=2)
    performance_required: bool = True
    ttft_slo_ms: float | None = Field(default=None, gt=0, le=600000)
    deadline_seconds: int = Field(default=2400, ge=120, le=14400)
    max_tokens: int | None = Field(default=300000, ge=1000, le=100000000)
    authorization_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    engine_source_profile: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,80}$")

    @model_validator(mode="after")
    def identity(self):
        if self.kind == "deploy_verify" and self.target_task_id:
            raise ValueError("A deployment creates its own service; do not bind a target task")
        if len(set(self.gpu_ids)) != len(self.gpu_ids) or any(n < 0 for n in self.gpu_ids):
            raise ValueError("GPU IDs must be unique and non-negative")
        if self.kind in {"compare", "test", "diagnose_repair"} and not self.target_task_id:
            raise ValueError("This task must bind an existing registered deployment task")
        if self.kind == "compare" and (
            len(self.candidate_ids) != 2 or len(set(self.candidate_ids)) != 2
        ):
            raise ValueError("Comparison requires exactly two distinct candidate IDs")
        if (
            max(self.workload.input_lengths) + self.workload.max_new_tokens
            > self.serving.context_length
        ):
            raise ValueError("Workload plus output exceeds planned context length")
        if any(not re.fullmatch(r"c[1-9][0-9]*", value) for value in self.candidate_ids):
            raise ValueError("Invalid candidate identity")
        return self


class CreateTask(Contract):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")
    plan: InfraPlan


class ReviseTask(CreateTask):
    expected_revision: int = Field(ge=1)


class BudgetUpdate(Contract):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")
    expected_revision: int = Field(ge=1)
    max_tokens: int | None = Field(default=None, ge=1000, le=100000000)


class Control(Contract):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")
    expected_revision: int = Field(ge=1)


class CandidateRequest(Contract):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")
    parent_id: str | None = Field(default=None, pattern=r"^c[1-9][0-9]*$")
    serving: ServingConfig
    reason: str = Field(min_length=5, max_length=1000)


class Hypothesis(Contract):
    symptom: str = Field(min_length=5, max_length=1000)
    cause: str = Field(min_length=5, max_length=1000)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    experiment: str = Field(min_length=5, max_length=1000)
    needs_source: bool = False
    needs_infra: bool = False


class AdapterEdit(Contract):
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str = Field(min_length=10, max_length=12000)


class JobRequest(Contract):
    candidate_id: str = Field(pattern=r"^c[1-9][0-9]*$")


class EngineSourceRead(Contract):
    path: str = Field(min_length=1, max_length=240)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=100, ge=1, le=200)


class EngineSourceSearch(Contract):
    text: str = Field(min_length=2, max_length=160)
    limit: int = Field(default=30, ge=1, le=60)


class EngineSourcePatch(Contract):
    path: str = Field(min_length=1, max_length=240)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    old_text: str = Field(min_length=1, max_length=16000)
    new_text: str = Field(max_length=16000)


class EngineReview(Contract):
    candidate_id: str = Field(pattern=r"^c[1-9][0-9]*$")
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$", description="Use the current candidate's review_digest from engine_source_status; never its candidate digest or source_digest.")
