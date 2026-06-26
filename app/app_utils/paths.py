"""Workspace path resolution utilities.

All tools that need to locate files on disk should import from here rather
than constructing paths ad-hoc.  The root is driven by the
``WINTERMUTE_WORKSPACE_ROOT`` environment variable, which **must** be set
before the agent or any tool is imported.  Load it via ``.env`` (the project
ships with ``python-dotenv`` and calls ``load_dotenv()`` in ``agent.py``).
"""

import os


def workspace_root() -> str:
    """Returns the absolute path to the Freeside workspace root.

    Raises:
        RuntimeError: If WINTERMUTE_WORKSPACE_ROOT is not set in the environment.
    """
    val = os.environ.get("WINTERMUTE_WORKSPACE_ROOT")
    if not val:
        raise RuntimeError(
            "WINTERMUTE_WORKSPACE_ROOT is not set. "
            "Copy .env.example to .env and fill in the correct value."
        )
    return val


def packages_root() -> str:
    """Returns the absolute path to the packages directory inside the workspace root."""
    return os.path.join(workspace_root(), "packages")
