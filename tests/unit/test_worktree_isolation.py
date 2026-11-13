"""Unit tests for leak-proof multi-repo worktree isolation and git --no-gpg-sign commits."""

import subprocess
from pathlib import Path

import pytest

from app.adapters.outbound.worktree_manager import WorktreeManagerAdapter
from app.bootstrap.config import load_config


def _init_git_repo(repo_dir: Path, filename: str, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@freeside.local"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Freeside Test"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--no-gpg-sign", "-m", "init"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_lazy_worktree_materialization_diff_and_commit(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".gitignore").write_text(".wintermute/\n", encoding="utf-8")
    _init_git_repo(ws / "packages", "README.md", "packages\n")
    _init_git_repo(ws / "straylight", "Cargo.toml", "[package]\nname='straylight'\n")

    default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
    cfg = load_config(workspace_root=ws, default_toml_path=default_toml)
    wt = WorktreeManagerAdapter(config=cfg, btrfs_detector=lambda _: False)

    info = await wt.ensure_worktree("task_iso")
    assert "packages" in info.symlinked_sub_repos
    assert "straylight" in info.symlinked_sub_repos
    assert Path(info.build_root) == ws / "build" / "workspace" / "task_iso"
    assert (Path(info.root_path) / ".gitignore").is_file()
    assert not (Path(info.root_path) / ".gitignore").is_symlink()

    # Materialize packages before editing/building
    info = await wt.ensure_materialized("task_iso", ["packages"])
    assert "packages" in info.materialized_sub_repos
    assert "packages" not in info.symlinked_sub_repos
    assert not (Path(info.root_path) / "packages").is_symlink()
    assert (Path(info.root_path) / "straylight").is_symlink()

    # Edit file inside materialized worktree; main workspace stays untouched
    target_file = Path(info.root_path) / "packages" / "README.md"
    target_file.write_text("packages updated in worktree\n", encoding="utf-8")
    assert (ws / "packages" / "README.md").read_text(encoding="utf-8") == "packages\n"

    diff = await wt.get_git_diff("task_iso", ["packages"])
    assert "packages updated in worktree" in diff

    sha = await wt.commit_changes("task_iso", "wintermute(packages): test commit", ["packages"])
    assert sha is not None and len(sha) >= 7

    await wt.cleanup_worktree("task_iso")
    assert not Path(info.root_path).exists()


@pytest.mark.asyncio
async def test_build_root_falls_back_to_wintermute_builds_when_unwritable(tmp_path: Path):
    ws = tmp_path / "ws"
    _init_git_repo(ws / "wintermute", "pyproject.toml", "[project]\nname='wintermute'\n")
    unwritable_parent = ws / "build" / "workspace"
    unwritable_parent.mkdir(parents=True, exist_ok=True)
    unwritable_parent.chmod(0o555)

    try:
        default_toml = Path(__file__).resolve().parents[2] / "wintermute.toml"
        cfg = load_config(workspace_root=ws, default_toml_path=default_toml)
        wt = WorktreeManagerAdapter(config=cfg, btrfs_detector=lambda _: False)

        info = await wt.ensure_worktree("task_perm")
        assert Path(info.build_root) == cfg.workspace.worktrees_dir.parent / "builds" / "task_perm"
        assert (Path(info.build_root) / "packages").is_dir()

        await wt.cleanup_worktree("task_perm")
        assert not Path(info.build_root).exists()
    finally:
        unwritable_parent.chmod(0o755)

