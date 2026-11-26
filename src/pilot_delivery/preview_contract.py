"""Versioned interactive generation, separate from the frozen delivery API v1."""

from typing import Literal

from pydantic import Field, model_validator

from pilot_core.contracts import Contract


class ModelMessage(Contract):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class PreviewRequest(Contract):
    messages: list[ModelMessage] = Field(min_length=1, max_length=11)
    max_new_tokens: int = Field(default=512, ge=1, le=1024)

    @model_validator(mode="after")
    def bounded_history(self):
        if self.messages[-1].role != "user" or any(
            m.role != ("user" if i % 2 == 0 else "assistant") for i, m in enumerate(self.messages)
        ):
            raise ValueError("History must alternate user/assistant, ending with user")
        if sum(len(m.content) for m in self.messages) > 24000:
            raise ValueError("Conversation input is too long; start a new model conversation")
        return self


def completion_stats(rows, eos_ids):
    counts, reasons = [], []
    for row in rows:
        end = next((i for i, value in enumerate(row) if value in eos_ids), None)
        counts.append(end + 1 if end is not None else len(row))
        reasons.append("stop" if end is not None else "length")
    return counts, reasons
