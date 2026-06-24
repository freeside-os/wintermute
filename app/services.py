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

from pathlib import Path
from google.adk.cli.service_registry import get_service_registry
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.adk.artifacts.file_artifact_service import FileArtifactService

from app.memory_service import PersistentGeminiMemoryService


def gemini_memory_factory(uri: str, **kwargs):
    """Factory to construct the PersistentGeminiMemoryService."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(app_dir)
    chroma_path = os.path.join(root_dir, ".adk", "chroma_memory")
    return PersistentGeminiMemoryService(path=chroma_path)


def winter_session_factory(uri: str, **kwargs):
    """Factory to construct the SqliteSessionService for wintermute."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(app_dir)
    db_path = os.path.join(root_dir, ".adk", "session.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return SqliteSessionService(db_path=db_path)


def winter_artifact_factory(uri: str, **kwargs):
    """Factory to construct the FileArtifactService for wintermute."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(app_dir)
    artifacts_dir = os.path.join(root_dir, ".adk", "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    return FileArtifactService(root_dir=Path(artifacts_dir))


# Register custom memory service under scheme "geminimemory"
get_service_registry().register_memory_service("geminimemory", gemini_memory_factory)

# Register custom session and artifact services
get_service_registry().register_session_service("wintersession", winter_session_factory)
get_service_registry().register_artifact_service("winterartifact", winter_artifact_factory)
