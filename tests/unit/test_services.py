import os
import pytest
from app.services import memory_factory, sessions_factory, artifacts_factory
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.adk.artifacts.file_artifact_service import FileArtifactService
from app.memory_service import PersistentGeminiMemoryService

@pytest.fixture(autouse=True)
def mock_embedding_function(monkeypatch):
    import chromadb.utils.embedding_functions as embedding_functions
    class MockGoogleGeminiEmbeddingFunction(embedding_functions.GoogleGeminiEmbeddingFunction):
        def __init__(self, *args, **kwargs):
            pass
        def __call__(self, input):
            return [[0.1] * 768 for _ in input]
    monkeypatch.setattr(embedding_functions, "GoogleGeminiEmbeddingFunction", MockGoogleGeminiEmbeddingFunction)

def test_memory_factory():
    service = memory_factory("memory://")
    assert isinstance(service, PersistentGeminiMemoryService)
    assert ".adk/chroma_memory" in service.client.get_settings().persist_directory

def test_sessions_factory():
    service = sessions_factory("session://")
    assert isinstance(service, SqliteSessionService)
    assert service._db_path.endswith(".adk/session.db")

def test_artifacts_factory():
    service = artifacts_factory("artifact://")
    assert isinstance(service, FileArtifactService)
    assert str(service.root_dir).endswith(".adk/artifacts")

def test_patched_factories():
    import google.adk.cli.utils.service_factory as sf
    import inspect

    # Check that they handle None URIs correctly
    assert sf.create_session_service_from_options(session_service_uri=None, base_dir="/tmp")
    assert sf.create_artifact_service_from_options(artifact_service_uri=None, base_dir="/tmp")
    assert sf.create_memory_service_from_options(memory_service_uri=None, base_dir="/tmp")
