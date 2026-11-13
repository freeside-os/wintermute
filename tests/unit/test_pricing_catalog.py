"""Unit tests for automatic pricing discovery, SQLite caching, and local zero-cost models."""

from pathlib import Path

import pytest

from app.adapters.outbound.pricing_catalog import PricingCatalogAdapter
from app.adapters.outbound.sqlite_storage import SQLiteStorageAdapter
from app.bootstrap.config import load_config


@pytest.mark.asyncio
async def test_local_models_resolve_zero_cost(tmp_path: Path):
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    storage = SQLiteStorageAdapter(db_path=tmp_path / "wm.db")
    await storage.initialize()

    adapter = PricingCatalogAdapter(config=cfg, storage=storage)
    local_pricing = await adapter.resolve_pricing(cfg.models["local_coder"])
    assert local_pricing.input_cost_per_1m == 0.0
    assert local_pricing.output_cost_per_1m == 0.0
    assert local_pricing.source == "local_zero_cost"


@pytest.mark.asyncio
async def test_models_dev_discovery_and_sqlite_cache(tmp_path: Path):
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    storage = SQLiteStorageAdapter(db_path=tmp_path / "wm.db")
    await storage.initialize()

    calls: list[str] = []

    async def fake_fetcher(url: str) -> dict:
        calls.append(url)
        return {
            "google": {
                "models": {
                    "gemini-4-argon": {
                        "id": "gemini-4-argon",
                        "cost": {
                            "input": 2.5,
                            "output": 15.0,
                            "cache_read": 0.5,
                            "reasoning": 15.0,
                        },
                    },
                    "gemini-3.8-flash": {
                        "id": "gemini-3.8-flash",
                        "cost": {
                            "input": 0.2,
                            "output": 0.8,
                            "cache_read": 0.05,
                        },
                    },
                }
            }
        }

    adapter = PricingCatalogAdapter(
        config=cfg, storage=storage, feed_fetcher=fake_fetcher
    )
    catalog = await adapter.sync_catalog()
    assert catalog["gemini-4-argon"].input_cost_per_1m == 2.5
    assert catalog["gemini-4-argon"].output_cost_per_1m == 15.0
    assert catalog["gemini-3.8-flash"].cached_input_cost_per_1m == 0.05
    assert len(calls) == 1

    # Fresh adapter reading from SQLite cache should not hit network
    adapter2 = PricingCatalogAdapter(
        config=cfg, storage=storage, feed_fetcher=fake_fetcher
    )
    resolved = await adapter2.resolve_pricing(cfg.models["argon"])
    assert resolved.input_cost_per_1m == 2.5
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_litellm_fallback_and_offline_tier_fallback(tmp_path: Path):
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    storage = SQLiteStorageAdapter(db_path=tmp_path / "wm.db")
    await storage.initialize()

    async def litellm_fallback_fetcher(url: str) -> dict:
        if "models.dev" in url:
            raise ConnectionError("models.dev down")
        return {
            "gemini/gemini-4-hydrogen": {
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000004,
                "cache_read_input_token_cost": 0.00000025,
            }
        }

    adapter = PricingCatalogAdapter(
        config=cfg, storage=storage, feed_fetcher=litellm_fallback_fetcher
    )
    catalog = await adapter.sync_catalog()
    assert catalog["gemini-4-hydrogen"].input_cost_per_1m == 1.0
    assert catalog["gemini-4-hydrogen"].output_cost_per_1m == 4.0

    # Offline completely -> tier fallback
    async def offline_fetcher(url: str) -> dict:
        raise OSError("offline")

    offline_adapter = PricingCatalogAdapter(
        config=cfg,
        storage=SQLiteStorageAdapter(db_path=tmp_path / "offline.db"),
        feed_fetcher=offline_fetcher,
    )
    await offline_adapter.storage.initialize()
    argon_fallback = await offline_adapter.resolve_pricing(cfg.models["argon"])
    assert argon_fallback.source == "tier_fallback:pro"
    assert argon_fallback.input_cost_per_1m > 0

