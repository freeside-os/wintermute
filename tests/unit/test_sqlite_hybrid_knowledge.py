"""Unit tests for SQLite FTS5 + vector hybrid knowledge search and consolidation."""

from pathlib import Path

import pytest

from app.adapters.outbound.sqlite_storage import SQLiteStorageAdapter, sanitize_fts5_query
from app.application.knowledge_service import KnowledgeService
from app.bootstrap.config import load_config


def test_sanitize_fts5_query_handles_compiler_punctuation():
    raw = 'src/main.c:42:10: fatal error: "openssl/ssl.h": cannot find -lssl'
    sanitized = sanitize_fts5_query(raw)
    assert '"src/main.c"' in sanitized
    assert '"openssl/ssl.h"' in sanitized
    assert '"-lssl"' in sanitized


@pytest.mark.asyncio
async def test_hybrid_knowledge_search_rrf_and_consolidation(tmp_path: Path):
    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    cfg = load_config(workspace_root=tmp_path, default_toml_path=default_toml)
    storage = SQLiteStorageAdapter(
        db_path=tmp_path / "wm.db",
        embedding_dimensions=cfg.memory.embedding_dimensions,
    )
    await storage.initialize()
    service = KnowledgeService(config=cfg, storage=storage)

    await service.record_note(
        domain="packages",
        target="curl",
        issue="configure: error: openssl libraries not found (-lssl)",
        resolution="Add openssl to build.dependencies in package.manifest",
    )
    await service.record_note(
        domain="packages",
        target="zlib",
        issue="missing -fPIC in static archive build on musl",
        resolution="Set CFLAGS='-O2 -fPIC' under [build.environment]",
    )

    hits = await service.search(
        'error: cannot find -lssl in configure', domain="packages"
    )
    assert len(hits) >= 1
    assert hits[0].target == "curl"

    # Trigger automatic consolidation at consolidate_threshold (5)
    for i in range(5):
        await service.record_note(
            domain="packages",
            target="busybox",
            issue=f"musl header issue #{i}",
            resolution=f"patch step #{i}",
        )

    entries = await storage.list_knowledge(domain="packages", target="busybox")
    assert len(entries) == 1
    assert entries[0].capture_type == "consolidated"
    assert "musl header issue #0" in entries[0].issue
    assert "patch step #4" in entries[0].resolution

