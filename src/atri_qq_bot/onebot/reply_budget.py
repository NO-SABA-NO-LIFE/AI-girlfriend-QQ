"""Monotonic budgets for ordinary OneBot reply processing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyBudget:
    started_at: float
    hard_deadline: float
    analysis_deadline: float

    @property
    def delivery_deadline(self) -> float:
        # At most 3 s in the material bucket, 120 s of processing and 2 s
        # for intentional delays. OneBot acknowledgment latency is separate.
        return self.hard_deadline + 2.0

    @classmethod
    def create(
        cls, started_at: float, turn_seconds: float, analysis_seconds: float, chat_reserve: float
    ) -> ReplyBudget:
        hard_deadline = started_at + turn_seconds
        return cls(
            started_at,
            hard_deadline,
            min(started_at + analysis_seconds, hard_deadline - chat_reserve),
        )


@asynccontextmanager
async def reply_phase(deadline: float | None) -> AsyncIterator[None]:
    # timeout_at() otherwise schedules expiration for the next loop iteration.
    if deadline is not None and asyncio.get_running_loop().time() >= deadline:
        raise TimeoutError("reply phase deadline expired")
    async with asyncio.timeout_at(deadline):
        yield
