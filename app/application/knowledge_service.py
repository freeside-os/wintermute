"""Hybrid knowledge base priming, retrieval, and target consolidation service."""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.application.ports import StoragePort
from app.bootstrap.config import WintermuteConfig
from app.domain.models import KnowledgeEntry, new_id

EmbeddingFn = Callable[[str, int], Awaitable[list[float]]]

_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")


async def compute_gemini_or_deterministic_embedding(
    text: str,
    dimensions: int = 768,
    *,
    model: str = "gemini-embedding-001",
) -> list[float]:
    """Use Gemini embeddings when authenticated online, with deterministic offline fallback."""
    has_key = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    if has_key and not os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            from google import genai  # type: ignore[import-not-found]

            client = genai.Client()
            resp = await client.aio.models.embed_content(
                model=model,
                contents=text,
                config={"output_dimensionality": dimensions},
            )
            embeddings = getattr(resp, "embeddings", None) or []
            if embeddings and getattr(embeddings[0], "values", None):
                return list(embeddings[0].values)
        except Exception:
            pass
    return compute_deterministic_embedding(text, dimensions=dimensions)


def compute_deterministic_embedding(text: str, dimensions: int = 768) -> list[float]:
    """Generate a normalized deterministic embedding vector for offline/local use."""
    if dimensions <= 0:
        raise ValueError("dimensions must be positive")
    vec = [0.0] * dimensions
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return vec

    for tok in tokens:
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % dimensions
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        weight = 1.0 + min(len(tok), 24) * 0.05
        vec[idx] += sign * weight

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


@dataclass(slots=True)
class KnowledgeService:
    config: WintermuteConfig
    storage: StoragePort
    embedder: EmbeddingFn | None = None

    async def _embed(self, text: str) -> list[float]:
        dims = self.config.memory.embedding_dimensions
        if self.embedder is not None:
            return await self.embedder(text, dims)
        return await compute_gemini_or_deterministic_embedding(
            text,
            dimensions=dims,
            model=self.config.memory.embedding_model,
        )

    async def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        target: str | None = None,
        limit: int | None = None,
    ) -> list[KnowledgeEntry]:
        if not self.config.memory.enabled:
            return []
        effective_limit = limit or self.config.memory.auto_prime_limit
        query_emb = await self._embed(query)
        return await self.storage.search_knowledge(
            query,
            query_embedding=query_emb,
            domain=domain,
            target=target,
            limit=effective_limit,
        )

    async def prime_context(
        self,
        *,
        domain: str,
        target: str | None = None,
        query: str,
        limit: int | None = None,
    ) -> str:
        eff_limit = (
            limit if limit is not None else self.config.memory.auto_prime_limit
        )
        if eff_limit <= 0:
            return ""
        entries = await self.search(
            query,
            domain=domain,
            target=target,
            limit=eff_limit,
        )
        if not entries:
            return ""

        def _clip(val: str, max_len: int = 300) -> str:
            cleaned = " ".join(val.split())
            return cleaned if len(cleaned) <= max_len else f"{cleaned[: max_len - 1].rstrip()}…"

        lines = ["### Relevant Prior Lessons (Knowledge Base)"]
        for entry in entries:
            lines.append(
                f"- [{entry.domain}/{entry.target}] ({entry.outcome}) "
                f"Issue: {_clip(entry.issue)} | Resolution: {_clip(entry.resolution)}"
            )
        return "\n".join(lines)

    async def record_note(
        self,
        *,
        domain: str,
        target: str,
        issue: str,
        resolution: str,
        outcome: str = "success",
        capture_type: str = "explicit",
    ) -> KnowledgeEntry:
        embedding = await self._embed(f"{domain} {target} {issue}\n{resolution}")
        entry = KnowledgeEntry(
            id=new_id("know"),
            domain=domain,
            target=target,
            issue=issue.strip(),
            resolution=resolution.strip(),
            outcome=outcome,
            capture_type=capture_type,
            embedding=embedding,
        )
        await self.storage.upsert_knowledge(entry)

        existing = await self.storage.list_knowledge(
            domain=domain,
            target=target,
            limit=self.config.memory.consolidate_threshold + 10,
        )
        if len(existing) >= self.config.memory.consolidate_threshold:
            consolidated = await self.consolidate_target(domain, target)
            if consolidated is not None:
                return consolidated
        return entry

    async def consolidate_target(self, domain: str, target: str) -> KnowledgeEntry | None:
        entries = await self.storage.list_knowledge(domain=domain, target=target, limit=100)
        if len(entries) <= 1:
            return entries[0] if entries else None

        seen_issues: list[str] = []
        seen_resolutions: list[str] = []
        for entry in entries:
            for part in entry.issue.split(" | "):
                cleaned = part.strip()
                if cleaned and cleaned not in seen_issues:
                    seen_issues.append(cleaned)
            for part in entry.resolution.split(" | "):
                cleaned = part.strip()
                if cleaned and cleaned not in seen_resolutions:
                    seen_resolutions.append(cleaned)

        merged_issue = " | ".join(seen_issues)
        merged_resolution = " | ".join(seen_resolutions)
        embedding = await self._embed(f"{domain} {target} {merged_issue}\n{merged_resolution}")

        consolidated = KnowledgeEntry(
            id=new_id("know"),
            domain=domain,
            target=target,
            issue=merged_issue,
            resolution=merged_resolution,
            outcome="success",
            capture_type="consolidated",
            embedding=embedding,
        )
        await self.storage.replace_target_knowledge(domain, target, consolidated)
        return consolidated

    def build_memory_tools(
        self,
        *,
        default_domain: str = "packages",
        default_target: str = "general",
        read_only: bool = False,
    ) -> list[Callable[..., Any]]:
        service = self

        async def search_memory(
            query: str,
            domain: str = "",
            target: str = "",
            limit: int = 5,
        ) -> str:
            """Search the hybrid SQLite FTS5 + vector knowledge base for prior fixes and patterns."""
            hits = await service.search(
                query,
                domain=domain or None,
                target=target or None,
                limit=limit,
            )
            if not hits:
                return "No matching knowledge entries found."
            return "\n".join(
                f"- [{h.domain}/{h.target}] ({h.outcome}) {h.issue} -> {h.resolution}"
                for h in hits
            )

        async def save_memory_note(
            issue: str,
            resolution: str,
            domain: str = default_domain,
            target: str = default_target,
            outcome: str = "success",
        ) -> str:
            """Save a reusable troubleshooting or implementation note to the knowledge base."""
            saved = await service.record_note(
                domain=domain or default_domain,
                target=target or default_target,
                issue=issue,
                resolution=resolution,
                outcome=outcome,
                capture_type="explicit",
            )
            return f"Saved knowledge entry {saved.id} for {saved.domain}/{saved.target}."

        if read_only:
            return [search_memory]
        return [search_memory, save_memory_note]

