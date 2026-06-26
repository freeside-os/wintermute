import os
import shutil

import pytest
from google.adk.events.event import Event
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session
from google.genai import types

from app.memory_service import PersistentGeminiMemoryService


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"),
    reason="GEMINI_API_KEY not set; skipping memory service test in CI"
)
async def test_persistent_gemini_memory_service() -> None:
    # Use a temporary directory for ChromaDB in testing
    test_path = "./chroma_memory_test"
    if os.path.exists(test_path):
        shutil.rmtree(test_path)

    try:
        service = PersistentGeminiMemoryService(path=test_path)

        # 1. Test add_session_to_memory
        session = Session(
            id="test-session-123",
            app_name="test_app",
            user_id="test_user",
            state={},
            events=[
                Event(
                    author="user",
                    content=types.Content(
                        role="user",
                        parts=[types.Part(text="We are compiling zlib with musl.")],
                    ),
                ),
                Event(
                    author="builder_agent",
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                text="Encountered undefined reference to inline function. Fixed by injecting fgnu89-inline CFLAGS."
                            )
                        ],
                    ),
                ),
            ],
            last_update_time=1234567.0,
        )

        await service.add_session_to_memory(session)

        # 2. Test add_memory directly (simulating the summarizer agent's write)
        direct_entry = MemoryEntry(
            id="test-summary-456",
            content=types.Content(
                role="model",
                parts=[types.Part(text="Summary: zlib compilation fails on musl inline functions. Fix: use CFLAGS fgnu89-inline.")],
            ),
            author="memory_summarizer_agent",
            timestamp="2026-06-23T00:00:00",
        )

        await service.add_memory(
            app_name="test_app",
            user_id="test_user",
            memories=[direct_entry]
        )

        # 3. Test search_memory matching the summary
        results = await service.search_memory(
            query="zlib musl inline functions fix",
            app_name="test_app",
            user_id="test_user",
        )

        # Verify results
        assert len(results) > 0
        memories = results.memories if hasattr(results, "memories") else results
        # We expect both the session and the summary to match
        assert len(memories) >= 1

        # Ensure our direct summary is retrieved
        summary_results = [m for m in memories if m.id == "test-summary-456"]
        assert len(summary_results) == 1
        assert "Summary: zlib compilation fails" in summary_results[0].content.parts[0].text
        assert summary_results[0].custom_metadata["app_name"] == "test_app"
        assert summary_results[0].custom_metadata["user_id"] == "test_user"

    finally:
        # Clean up testing directory
        if os.path.exists(test_path):
            shutil.rmtree(test_path)
