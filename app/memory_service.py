# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

import chromadb
import chromadb.utils.embedding_functions as embedding_functions
from google.adk.memory.base_memory_service import (
    BaseMemoryService,
    SearchMemoryResponse,
)
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

if TYPE_CHECKING:
    from google.adk.sessions.session import Session


class SearchMemoryResponseList(list):
    """A list of MemoryEntry that also acts as a SearchMemoryResponse by exposing a `.memories` property."""

    @property
    def memories(self) -> list[MemoryEntry]:
        return self


class PersistentGeminiMemoryService(BaseMemoryService):
    """A persistent memory service using ChromaDB and Google Gemini embeddings."""

    def __init__(self, path: str = "./chroma_memory"):
        self.client = chromadb.PersistentClient(path=path)

        # Configure embedding function based on whether we are using Vertex AI or AI Studio
        use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") == "True"
        if use_vertex:
            self.embedding_function = embedding_functions.GoogleGeminiEmbeddingFunction(
                model_name="gemini-embedding-001",
                task_type="RETRIEVAL_DOCUMENT",
                vertexai=True,
                project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                location=os.environ.get("GOOGLE_CLOUD_LOCATION") or "global",
                api_key_env_var=None
            )
        else:
            # Ensure GEMINI_API_KEY is present in environment if GOOGLE_API_KEY is
            if os.environ.get("GOOGLE_API_KEY") and not os.environ.get("GEMINI_API_KEY"):
                os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]
            self.embedding_function = embedding_functions.GoogleGeminiEmbeddingFunction(
                model_name="gemini-embedding-001",
                task_type="RETRIEVAL_DOCUMENT",
                api_key_env_var="GEMINI_API_KEY"
            )

        self.collection = self.client.get_or_create_collection(
            name="wintermute_packaging_knowledge",
            embedding_function=self.embedding_function
        )

    async def add_session_to_memory(self, session: Session) -> None:
        """Extract text from session events, combine it, and upsert it into ChromaDB."""
        texts = []
        for event in session.events:
            if event.content and event.content.parts:
                parts_text = [p.text for p in event.content.parts if p.text]
                if parts_text:
                    texts.append(f"[{event.author}]: {' '.join(parts_text)}")

        combined_text = "\n".join(texts).strip()
        if not combined_text:
            return

        metadata = {
            "app_name": session.app_name,
            "user_id": session.user_id,
            "timestamp": str(session.last_update_time)
        }

        self.collection.upsert(
            ids=[session.id],
            documents=[combined_text],
            metadatas=[metadata]
        )

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Adds explicit memory items directly to the ChromaDB collection."""
        for m in memories:
            text_parts = [p.text for p in m.content.parts if p.text] if m.content and m.content.parts else []
            doc_text = " ".join(text_parts).strip()
            if not doc_text:
                continue

            metadata = {
                "app_name": app_name,
                "user_id": user_id,
                "timestamp": m.timestamp or "",
                "author": m.author or "summarizer",
            }
            if m.custom_metadata:
                metadata.update(m.custom_metadata)
            if custom_metadata:
                metadata.update(custom_metadata)

            mem_id = m.id or f"mem_{os.urandom(8).hex()}"

            self.collection.upsert(
                ids=[mem_id],
                documents=[doc_text],
                metadatas=[metadata]
            )

    async def search_memory(
        self,
        query: str | None = None,
        app_name: str | None = None,
        user_id: str | None = None,
        limit: int = 5,
        **kwargs
    ) -> list[MemoryEntry] | SearchMemoryResponse:
        """Query ChromaDB using semantic text search and return a list/SearchMemoryResponse of MemoryEntry objects."""
        actual_query = query or kwargs.get("query")
        actual_app_name = app_name or kwargs.get("app_name")
        actual_user_id = user_id or kwargs.get("user_id")

        if not actual_query:
            return SearchMemoryResponseList([])

        # Build metadata filter
        where_filter = {}
        if actual_app_name:
            where_filter["app_name"] = actual_app_name
        if actual_user_id:
            where_filter["user_id"] = actual_user_id

        if len(where_filter) > 1:
            where_clause = {"$and": [{k: v} for k, v in where_filter.items()]}
        elif len(where_filter) == 1:
            where_clause = where_filter
        else:
            where_clause = None

        results = self.collection.query(
            query_texts=[actual_query],
            n_results=limit,
            where=where_clause
        )

        memory_entries = []
        if results and "documents" in results and results["documents"]:
            docs = results["documents"][0]
            ids = results["ids"][0]
            metadatas = results["metadatas"][0] if results.get("metadatas") else [None] * len(docs)

            for doc, doc_id, meta in zip(docs, ids, metadatas, strict=True):
                metadata = meta or {}
                content = types.Content(
                    role="model",
                    parts=[types.Part(text=doc)]
                )
                memory_entries.append(
                    MemoryEntry(
                        id=doc_id,
                        content=content,
                        custom_metadata=metadata,
                        author=metadata.get("author", "system"),
                        timestamp=metadata.get("timestamp")
                    )
                )

        return SearchMemoryResponseList(memory_entries)
