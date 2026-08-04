"""Workspace configuration and path resolution for Freeside sub-repositories."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorkspaceConfig:
    """Resolves Freeside OS sub-repository paths from environment or default workspace location."""
    workspace_root: Path

    def __init__(self, root: str | Path | None = None):
        if root:
            self.workspace_root = Path(root).resolve()
        elif os.getenv("FREESIDE_WORKSPACE_ROOT"):
            self.workspace_root = Path(os.environ["FREESIDE_WORKSPACE_ROOT"]).resolve()
        else:
            # Default to standard freeside workspace location
            self.workspace_root = Path("/home/dq/Code/freeside").resolve()

    @property
    def packages_path(self) -> Path:
        return self.workspace_root / "packages"

    @property
    def straylight_path(self) -> Path:
        return self.workspace_root / "straylight"

    @property
    def installer_path(self) -> Path:
        return self.workspace_root / "installer"

    @property
    def simstim_path(self) -> Path:
        return self.workspace_root / "simstim"

    @property
    def freesidectl_path(self) -> Path:
        return self.workspace_root / "freesidectl"

    @property
    def fstools_path(self) -> Path:
        return self.workspace_root / "fstools"

    @property
    def docs_path(self) -> Path:
        return self.workspace_root / "docs"

    @property
    def wintermute_path(self) -> Path:
        return self.workspace_root / "wintermute"
