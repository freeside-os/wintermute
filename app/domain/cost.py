"""Cost accounting, token usage, and budget enforcement domain models."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.models import utcnow_iso


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    cached_tokens: int = 0
    candidate_tokens: int = 0
    thoughts_tokens: int = 0
    total_tokens: int = 0

    @property
    def uncached_prompt_tokens(self) -> int:
        return max(0, self.prompt_tokens - self.cached_tokens)

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            candidate_tokens=self.candidate_tokens + other.candidate_tokens,
            thoughts_tokens=self.thoughts_tokens + other.thoughts_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True, slots=True)
class ModelPricing:
    model_id: str
    input_cost_per_1m: float = 0.0
    cached_input_cost_per_1m: float = 0.0
    output_cost_per_1m: float = 0.0
    thinking_cost_per_1m: float = 0.0
    source: str = "default"
    fetched_at: str = field(default_factory=utcnow_iso)

    def calculate_usd(self, usage: TokenUsage) -> float:
        """Compute total USD cost from uncached input, cached input, output, and thinking tokens."""
        uncached_cost = (usage.uncached_prompt_tokens / 1_000_000.0) * self.input_cost_per_1m
        cached_cost = (usage.cached_tokens / 1_000_000.0) * self.cached_input_cost_per_1m
        output_cost = (usage.candidate_tokens / 1_000_000.0) * self.output_cost_per_1m
        thinking_cost = (usage.thoughts_tokens / 1_000_000.0) * self.thinking_cost_per_1m
        return round(uncached_cost + cached_cost + output_cost + thinking_cost, 8)


@dataclass(slots=True)
class CostLedgerEntry:
    id: str
    task_id: str
    subagent_run_id: str | None
    role: str
    model_id: str
    usage: TokenUsage
    usd_cost: float
    timestamp: str = field(default_factory=utcnow_iso)


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    max_task_usd: float = 5.0
    max_daily_usd: float = 25.0
    pause_on_budget_exceeded: bool = True


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    allowed: bool
    reason: str = ""
    task_usd_spent: float = 0.0
    task_usd_limit: float = 0.0
    daily_usd_spent: float = 0.0
    daily_usd_limit: float = 0.0

