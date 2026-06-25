import os
from pathlib import Path

from google.adk.artifacts.file_artifact_service import FileArtifactService
from google.adk.cli.service_registry import get_service_registry
from google.adk.sessions.sqlite_session_service import SqliteSessionService

from app.memory_service import PersistentGeminiMemoryService


def memory_factory(uri: str, **kwargs):
    """Factory to construct the PersistentGeminiMemoryService."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(app_dir)
    chroma_path = os.path.join(root_dir, ".adk", "chroma_memory")
    return PersistentGeminiMemoryService(path=chroma_path)


def sessions_factory(uri: str, **kwargs):
    """Factory to construct the SqliteSessionService for wintermute."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(app_dir)
    db_path = os.path.join(root_dir, ".adk", "session.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return SqliteSessionService(db_path=db_path)


def artifacts_factory(uri: str, **kwargs):
    """Factory to construct the FileArtifactService for wintermute."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(app_dir)
    artifacts_dir = os.path.join(root_dir, ".adk", "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    return FileArtifactService(root_dir=Path(artifacts_dir))


# Register custom memory service under scheme "memory"
get_service_registry().register_memory_service("memory", memory_factory)

# Register custom session and artifact services
get_service_registry().register_session_service("session", sessions_factory)
get_service_registry().register_artifact_service("artifact", artifacts_factory)


# Monkeypatch ADK service factories to use our registered custom services by default when no command-line URIs are provided.
try:
    import sys
    import google.adk.cli.utils.service_factory as sf

    # 1. Define wrapped versions of the factory functions
    def patched_create_session_service_from_options(*args, **kwargs):
        if kwargs.get("session_service_uri") is None:
            kwargs["session_service_uri"] = "session://"
        return sf._original_create_session_service_from_options(*args, **kwargs)

    def patched_create_artifact_service_from_options(*args, **kwargs):
        if kwargs.get("artifact_service_uri") is None:
            kwargs["artifact_service_uri"] = "artifact://"
        return sf._original_create_artifact_service_from_options(*args, **kwargs)

    def patched_create_memory_service_from_options(*args, **kwargs):
        if kwargs.get("memory_service_uri") is None:
            kwargs["memory_service_uri"] = "memory://"
        return sf._original_create_memory_service_from_options(*args, **kwargs)

    # 2. Save original functions on the service_factory module if not already done
    if not hasattr(sf, "_original_create_session_service_from_options"):
        sf._original_create_session_service_from_options = sf.create_session_service_from_options
        sf.create_session_service_from_options = patched_create_session_service_from_options

    if not hasattr(sf, "_original_create_artifact_service_from_options"):
        sf._original_create_artifact_service_from_options = sf.create_artifact_service_from_options
        sf.create_artifact_service_from_options = patched_create_artifact_service_from_options

    if not hasattr(sf, "_original_create_memory_service_from_options"):
        sf._original_create_memory_service_from_options = sf.create_memory_service_from_options
        sf.create_memory_service_from_options = patched_create_memory_service_from_options

    # 3. Patch modules that have already imported the functions
    for mod_name in ["google.adk.cli.cli", "google.adk.cli.fast_api"]:
        mod = sys.modules.get(mod_name)
        if mod is not None:
            if hasattr(mod, "create_session_service_from_options"):
                mod.create_session_service_from_options = patched_create_session_service_from_options
            if hasattr(mod, "create_artifact_service_from_options"):
                mod.create_artifact_service_from_options = patched_create_artifact_service_from_options
            if hasattr(mod, "create_memory_service_from_options"):
                mod.create_memory_service_from_options = patched_create_memory_service_from_options
except Exception as e:
    import logging
    logging.getLogger(__name__).warning("Failed to monkeypatch ADK default service options: %s", e)

