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


@pytest.mark.asyncio
async def test_add_session_to_memory_with_gemini(tmp_path) -> None:
    from unittest.mock import MagicMock, patch

    test_path = str(tmp_path / "chroma_test_gemini")
    service = PersistentGeminiMemoryService(path=test_path)

    session = Session(
        id="test-session-gemini",
        app_name="test_app",
        user_id="test_user",
        state={},
        events=[
            Event(
                author="user",
                content=types.Content(
                    role="user",
                    parts=[types.Part(text="Fails to build zlib")],
                ),
            ),
        ],
        last_update_time=123.0,
    )

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Summary: zlib error fixed by flag"
    mock_client.models.generate_content.return_value = mock_response

    # We must patch GOOGLE_API_KEY in env to avoid Client raising error on initialization if empty
    with patch.dict(os.environ, {"GOOGLE_API_KEY": "fake-key"}), \
         patch("google.genai.Client", return_value=mock_client) as mock_client_cls:
        await service.add_session_to_memory(session)

        # Verify mock was called
        assert mock_client_cls.call_count >= 1
        mock_client.models.generate_content.assert_called_once()
        args, kwargs = mock_client.models.generate_content.call_args
        assert kwargs["model"] == "gemini-2.5-flash"
        assert "Fails to build zlib" in kwargs["contents"]

    # Verify the summary was upserted
    res = service.collection.get(ids=["test-session-gemini"])
    assert res["documents"] == ["Summary: zlib error fixed by flag"]


@pytest.mark.asyncio
async def test_add_session_to_memory_fallback(tmp_path) -> None:
    from unittest.mock import patch

    test_path = str(tmp_path / "chroma_test_fallback")
    service = PersistentGeminiMemoryService(path=test_path)

    session = Session(
        id="test-session-fallback",
        app_name="test_app",
        user_id="test_user",
        state={},
        events=[
            Event(
                author="user",
                content=types.Content(
                    role="user",
                    parts=[types.Part(text="Fails to build zlib")],
                ),
            ),
        ],
        last_update_time=123.0,
    )

    # Force Client to throw exception
    with patch.dict(os.environ, {"GOOGLE_API_KEY": "fake-key"}), \
         patch("google.genai.Client", side_effect=ValueError("API key error")):
        await service.add_session_to_memory(session)

    # Verify the fallback (truncated raw event) was upserted
    res = service.collection.get(ids=["test-session-fallback"])
    assert len(res["documents"]) == 1
    assert "[user]: Fails to build zlib" in res["documents"][0]


@pytest.mark.asyncio
async def test_add_memory_deterministic_hash(tmp_path) -> None:
    import hashlib
    test_path = str(tmp_path / "chroma_test_hash")
    service = PersistentGeminiMemoryService(path=test_path)

    direct_entry = MemoryEntry(
        content=types.Content(
            role="model",
            parts=[types.Part(text="Some specific compile issue resolution here.")],
        ),
        author="memory_summarizer_agent",
        timestamp="2026-06-23T00:00:00",
    )

    await service.add_memory(
        app_name="test_app",
        user_id="test_user",
        memories=[direct_entry]
    )

    doc_text = "Some specific compile issue resolution here."
    expected_hash = hashlib.sha256(doc_text.encode('utf-8')).hexdigest()[:16]
    expected_id = f"mem_{expected_hash}"

    # Verify it has the expected ID
    res = service.collection.get()
    assert expected_id in res["ids"]

    # Verify if we add again, it doesn't duplicate (it upserts the same ID)
    await service.add_memory(
        app_name="test_app",
        user_id="test_user",
        memories=[direct_entry]
    )
    res2 = service.collection.get()
    assert len(res2["ids"]) == 1


@pytest.mark.asyncio
async def test_scan_build_log_semantic_kb_match(tmp_path) -> None:
    from unittest.mock import patch
    from app.tools.compilation import scan_build_log
    from google.adk.memory.memory_entry import MemoryEntry
    from google.genai import types

    # Mock the ChromaDB search to return a memory entry
    mock_entry = MemoryEntry(
        id="test-match-123",
        content=types.Content(
            role="model",
            parts=[types.Part(text="Use special environment variable FIX_SEMANTIC=1 to bypass")],
        ),
        author="system",
    )

    async def mock_search(*args, **kwargs):
        from app.memory_service import SearchMemoryResponseList
        return SearchMemoryResponseList([mock_entry])

    with patch("app.tools.compilation.read_build_logs") as mock_read, \
         patch("app.memory_service.PersistentGeminiMemoryService.search_memory", new=mock_search), \
         patch("app.tools.compilation.get_workspace_root", return_value=str(tmp_path)):

        mock_read.return_value = {
            "status": "success",
            "content": "Unknown compiler error: fail compiling main.c because of missing context flags"
        }

        res = scan_build_log("test-pkg")

        assert res["status"] == "success"
        assert res["signature"] == "semantic_kb_match"
        assert res["proposal"]["type"] == "semantic_note"
        assert "Use special environment variable FIX_SEMANTIC=1" in res["proposal"]["message"]
