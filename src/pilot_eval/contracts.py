from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .problems import problem


class EvaluationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Literal["calibration", "held_out"] = "calibration"
    case_ids: list[str] = Field(min_length=1, max_length=20)
    strategies: list[Literal["single", "multi", "auto"]] = Field(default_factory=lambda: ["single"], min_length=1, max_length=3)
    repetitions: int = Field(default=1, ge=1, le=3)
    max_tokens: int = Field(default=1500000, ge=1000, le=100000000)
    trial_max_tokens: int = Field(default=200000, ge=1000, le=1000000)
    deadline_seconds: int = Field(default=1800, ge=120, le=14400)
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def partitions(self):
        if len(set(self.case_ids)) != len(self.case_ids) or len(set(self.strategies)) != len(self.strategies):
            raise ValueError("Duplicate evaluation case or strategy")
        rows = [problem(identity) for identity in self.case_ids]
        if any(row["split"] == "held_out" for row in rows) and self.phase != "held_out":
            raise ValueError("Held-out families cannot be used for calibration")
        if (self.input_price_per_million is None) != (self.output_price_per_million is None):
            raise ValueError("Both input and output prices are required for priced costs")
        return self
