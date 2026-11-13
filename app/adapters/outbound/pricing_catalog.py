"""Automatic model pricing catalog adapter with SQLite caching and local zero-cost rules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.application.ports import PricingCatalogPort, StoragePort
from app.bootstrap.config import ModelProfile, WintermuteConfig
from app.domain.cost import ModelPricing
from app.domain.models import utcnow_iso

FeedFetcher = Callable[[str], Awaitable[dict[str, Any]]]


async def _default_feed_fetcher(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def _parse_models_dev_payload(payload: dict[str, Any]) -> dict[str, ModelPricing]:
    """Parse models.dev /api.json nested provider -> models structure."""
    parsed: dict[str, ModelPricing] = {}
    for provider_key, provider_val in payload.items():
        if not isinstance(provider_val, dict):
            continue
        models_map = provider_val.get("models")
        if not isinstance(models_map, dict):
            continue
        for model_key, model_info in models_map.items():
            if not isinstance(model_info, dict):
                continue
            cost = model_info.get("cost")
            if not isinstance(cost, dict):
                continue
            input_cost = float(cost.get("input", 0.0) or 0.0)
            output_cost = float(cost.get("output", 0.0) or 0.0)
            cache_read = float(cost.get("cache_read", input_cost * 0.25) or 0.0)
            reasoning_cost = float(cost.get("reasoning", output_cost) or output_cost)
            model_id = str(model_info.get("id") or model_key)
            pricing = ModelPricing(
                model_id=model_id,
                input_cost_per_1m=input_cost,
                cached_input_cost_per_1m=cache_read,
                output_cost_per_1m=output_cost,
                thinking_cost_per_1m=reasoning_cost,
                source=f"models.dev:{provider_key}",
                fetched_at=utcnow_iso(),
            )
            parsed[model_id] = pricing
            if "/" in model_id:
                parsed.setdefault(model_id.split("/", 1)[-1], pricing)
    return parsed


def _parse_litellm_payload(payload: dict[str, Any]) -> dict[str, ModelPricing]:
    """Parse LiteLLM model_prices_and_context_window.json flat model mapping."""
    parsed: dict[str, ModelPricing] = {}
    for model_id, info in payload.items():
        if not isinstance(info, dict):
            continue
        if "input_cost_per_token" not in info and "output_cost_per_token" not in info:
            continue
        in_per_tok = float(info.get("input_cost_per_token", 0.0) or 0.0)
        out_per_tok = float(info.get("output_cost_per_token", 0.0) or 0.0)
        cache_per_tok = float(
            info.get("cache_read_input_token_cost", in_per_tok * 0.25) or 0.0
        )
        thinking_per_tok = float(
            info.get("output_cost_per_reasoning_token", out_per_tok) or out_per_tok
        )
        pricing = ModelPricing(
            model_id=model_id,
            input_cost_per_1m=round(in_per_tok * 1_000_000.0, 6),
            cached_input_cost_per_1m=round(cache_per_tok * 1_000_000.0, 6),
            output_cost_per_1m=round(out_per_tok * 1_000_000.0, 6),
            thinking_cost_per_1m=round(thinking_per_tok * 1_000_000.0, 6),
            source="litellm",
            fetched_at=utcnow_iso(),
        )
        parsed[model_id] = pricing
        if "/" in model_id:
            parsed.setdefault(model_id.split("/", 1)[-1], pricing)
    return parsed


def parse_pricing_feed(url: str, payload: dict[str, Any]) -> dict[str, ModelPricing]:
    """Auto-detect feed format (models.dev or LiteLLM) and normalize into ModelPricing."""
    if any(
        isinstance(v, dict) and isinstance(v.get("models"), dict)
        for v in payload.values()
    ):
        return _parse_models_dev_payload(payload)
    if "models.dev" in url:
        parsed = _parse_models_dev_payload(payload)
        if parsed:
            return parsed
    return _parse_litellm_payload(payload)


def _fuzzy_tier_fallback(model_id: str) -> ModelPricing:
    lowered = model_id.lower()
    if any(tok in lowered for tok in ("argon", "pro", "opus", "ultra")):
        return ModelPricing(
            model_id=model_id,
            input_cost_per_1m=1.25,
            cached_input_cost_per_1m=0.3125,
            output_cost_per_1m=10.0,
            thinking_cost_per_1m=10.0,
            source="tier_fallback:pro",
        )
    if any(tok in lowered for tok in ("hydrogen", "sonnet", "medium")):
        return ModelPricing(
            model_id=model_id,
            input_cost_per_1m=0.60,
            cached_input_cost_per_1m=0.15,
            output_cost_per_1m=4.0,
            thinking_cost_per_1m=4.0,
            source="tier_fallback:mid",
        )
    if any(tok in lowered for tok in ("flash", "haiku", "mini", "lite", "nano")):
        return ModelPricing(
            model_id=model_id,
            input_cost_per_1m=0.15,
            cached_input_cost_per_1m=0.0375,
            output_cost_per_1m=0.60,
            thinking_cost_per_1m=0.60,
            source="tier_fallback:flash",
        )
    return ModelPricing(
        model_id=model_id,
        input_cost_per_1m=0.50,
        cached_input_cost_per_1m=0.125,
        output_cost_per_1m=2.0,
        thinking_cost_per_1m=2.0,
        source="tier_fallback:default",
    )


@dataclass(slots=True)
class PricingCatalogAdapter(PricingCatalogPort):
    config: WintermuteConfig
    storage: StoragePort
    feed_fetcher: FeedFetcher | None = None
    _memory_cache: dict[str, ModelPricing] = field(default_factory=dict)

    async def sync_catalog(self, force: bool = False) -> dict[str, ModelPricing]:
        ttl = self.config.cost.pricing_cache_ttl_hours
        discovered: dict[str, ModelPricing] = {}

        if not force:
            all_cached = True
            for profile in self.config.models.values():
                if profile.is_local:
                    continue
                cached = await self.storage.get_cached_pricing(
                    profile.model, max_age_hours=ttl
                )
                if cached is not None:
                    self._memory_cache[profile.model] = cached
                    discovered[profile.model] = cached
                else:
                    all_cached = False
            if all_cached:
                return discovered

        fetcher = self.feed_fetcher or _default_feed_fetcher
        catalog: dict[str, ModelPricing] = {}
        for feed_url in self.config.cost.pricing_feeds:
            try:
                payload = await fetcher(feed_url)
                parsed = parse_pricing_feed(feed_url, payload)
                if parsed:
                    catalog.update(parsed)
                    break
            except Exception:
                continue

        for model_id, pricing in catalog.items():
            self._memory_cache[model_id] = pricing
            await self.storage.upsert_cached_pricing(pricing)

        for profile in self.config.models.values():
            resolved = await self.resolve_pricing(profile)
            discovered[profile.model] = resolved
        return discovered

    async def resolve_pricing(self, profile: ModelProfile) -> ModelPricing:
        if profile.is_local:
            return ModelPricing(
                model_id=profile.model,
                input_cost_per_1m=0.0,
                cached_input_cost_per_1m=0.0,
                output_cost_per_1m=0.0,
                thinking_cost_per_1m=0.0,
                source="local_zero_cost",
            )

        if profile.model in self._memory_cache:
            return self._memory_cache[profile.model]

        ttl = self.config.cost.pricing_cache_ttl_hours
        cached = await self.storage.get_cached_pricing(profile.model, max_age_hours=ttl)
        if cached is not None:
            self._memory_cache[profile.model] = cached
            return cached

        # Fuzzy match against any loaded catalog entries before tier fallback
        lowered = profile.model.lower()
        for candidate_id, candidate_pricing in self._memory_cache.items():
            if lowered in candidate_id.lower() or candidate_id.lower() in lowered:
                matched = ModelPricing(
                    model_id=profile.model,
                    input_cost_per_1m=candidate_pricing.input_cost_per_1m,
                    cached_input_cost_per_1m=candidate_pricing.cached_input_cost_per_1m,
                    output_cost_per_1m=candidate_pricing.output_cost_per_1m,
                    thinking_cost_per_1m=candidate_pricing.thinking_cost_per_1m,
                    source=f"fuzzy:{candidate_pricing.source}",
                )
                self._memory_cache[profile.model] = matched
                return matched

        fallback = _fuzzy_tier_fallback(profile.model)
        self._memory_cache[profile.model] = fallback
        return fallback

