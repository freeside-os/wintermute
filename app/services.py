import os
from app.memory_service import PersistentGeminiMemoryService


def memory_factory(uri: str, **kwargs):
    """Factory to construct the PersistentGeminiMemoryService."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(app_dir)
    chroma_path = os.path.join(root_dir, ".adk", "chroma_memory")
    return PersistentGeminiMemoryService(path=chroma_path)


