"""Leak-proof multi-repo git worktree and Btrfs CoW workspace manager."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.application.ports import WorktreePort
from app.bootstrap.config import WintermuteConfig
from app.domain.models import WorktreeInfo

_IGNORED_MTIME_DIRS = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".venv", "target", "build", ".wintermute"}
)


async def _run_cmd(
    args: Sequence[str], *, cwd: Path | None = None
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


@dataclass(slots=True)
class WorktreeManagerAdapter(WorktreePort):
    config: WintermuteConfig
    btrfs_detector: Callable[[Path], bool] | None = None
    _worktrees: dict[str, WorktreeInfo] = field(default_factory=dict)
    _direct_tasks: set[str] = field(default_factory=set)

    def _is_btrfs(self, path: Path) -> bool:
        if not self.config.workspace.prefer_btrfs_cow:
            return False
        if self.btrfs_detector is not None:
            return self.btrfs_detector(path)
        return False

    async def _detect_btrfs_async(self, path: Path) -> bool:
        if not self.config.workspace.prefer_btrfs_cow:
            return False
        if self.btrfs_detector is not None:
            return self.btrfs_detector(path)
        rc, out, _ = await _run_cmd(["stat", "-f", "-c", "%T", str(path)])
        return rc == 0 and out.strip().lower() == "btrfs"

    def _task_root(self, task_id: str) -> Path:
        return self.config.workspace.worktrees_dir / task_id

    def _task_build_root(self, task_id: str) -> Path:
        return self.config.workspace.workspace_root / "build" / "workspace" / task_id

    def _fallback_build_root(self, task_id: str) -> Path:
        return self.config.workspace.worktrees_dir.parent / "builds" / task_id

    def _ensure_build_dir(self, preferred: Path, fallback: Path) -> Path:
        try:
            preferred.mkdir(parents=True, exist_ok=True)
            (preferred / "packages").mkdir(parents=True, exist_ok=True)
            return preferred
        except PermissionError:
            fallback.mkdir(parents=True, exist_ok=True)
            (fallback / "packages").mkdir(parents=True, exist_ok=True)
            return fallback

    def register_direct_workspace(self, task_id: str) -> WorktreeInfo:
        """Register direct workspace execution (--no-worktree) without isolation clones."""
        self._direct_tasks.add(task_id)
        ws_root = self.config.workspace.workspace_root.resolve()
        build_root = self._ensure_build_dir(
            (ws_root / "build").resolve(),
            self._fallback_build_root(task_id),
        )
        info = WorktreeInfo(
            task_id=task_id,
            root_path=str(ws_root),
            build_root=str(build_root),
            branch_name="HEAD",
            is_btrfs_cow=False,
            materialized_sub_repos=set(self.config.workspace.sub_repos),
            symlinked_sub_repos=set(),
        )
        self._worktrees[task_id] = info
        return info

    async def ensure_worktree(
        self, task_id: str, *, use_worktree: bool = True
    ) -> WorktreeInfo:
        if not use_worktree or task_id in self._direct_tasks:
            return self.register_direct_workspace(task_id)
        if task_id in self._worktrees:
            info = self._worktrees[task_id]
            resolved_build = self._ensure_build_dir(
                Path(info.build_root),
                self._fallback_build_root(task_id),
            )
            info.build_root = str(resolved_build)
            return info

        ws_root = self.config.workspace.workspace_root
        wt_root = self._task_root(task_id)
        branch_name = f"wintermute/{task_id}"

        self.config.workspace.worktrees_dir.mkdir(parents=True, exist_ok=True)
        build_root = self._ensure_build_dir(
            self._task_build_root(task_id),
            self._fallback_build_root(task_id),
        )

        is_btrfs = await self._detect_btrfs_async(ws_root)
        materialized: set[str] = set()
        symlinked: set[str] = set()

        if is_btrfs and not wt_root.exists():
            rc, _, _ = await _run_cmd(
                ["btrfs", "subvolume", "snapshot", str(ws_root), str(wt_root)]
            )
            if rc == 0:
                materialized = set(self.config.workspace.sub_repos)
            else:
                is_btrfs = False

        if not is_btrfs:
            wt_root.mkdir(parents=True, exist_ok=True)
            if ws_root.exists():
                for entry in ws_root.iterdir():
                    if entry.name in {".wintermute", "build", ".git"}:
                        continue
                    dest = wt_root / entry.name
                    if dest.exists() or dest.is_symlink():
                        continue
                    if entry.name == ".gitignore" and entry.is_file():
                        # Git opens .gitignore with O_NOFOLLOW; copy rather than symlink.
                        shutil.copy2(entry, dest)
                    elif entry.name in self.config.workspace.sub_repos:
                        os.symlink(entry, dest)
                        symlinked.add(entry.name)
                    else:
                        os.symlink(entry, dest)

        info = WorktreeInfo(
            task_id=task_id,
            root_path=str(wt_root),
            build_root=str(build_root),
            branch_name=branch_name,
            is_btrfs_cow=is_btrfs,
            materialized_sub_repos=materialized,
            symlinked_sub_repos=symlinked,
        )
        self._worktrees[task_id] = info
        return info

    async def ensure_materialized(
        self, task_id: str, sub_repos: Sequence[str]
    ) -> WorktreeInfo:
        info = await self.ensure_worktree(task_id)
        if info.is_btrfs_cow or task_id in self._direct_tasks:
            return info

        ws_root = self.config.workspace.workspace_root
        wt_root = Path(info.root_path)

        for repo in sub_repos:
            if not repo or repo in info.materialized_sub_repos:
                continue
            src_repo = ws_root / repo
            dst_repo = wt_root / repo
            if not src_repo.exists():
                continue

            if dst_repo.is_symlink():
                dst_repo.unlink()

            if not dst_repo.exists():
                used_git_worktree = False
                if (src_repo / ".git").exists():
                    rc_head, _, _ = await _run_cmd(
                        ["git", "-C", str(src_repo), "rev-parse", "--verify", "HEAD"]
                    )
                    if rc_head == 0:
                        rc_wt, _, _ = await _run_cmd(
                            [
                                "git",
                                "-C",
                                str(src_repo),
                                "worktree",
                                "add",
                                "-B",
                                info.branch_name,
                                str(dst_repo),
                            ]
                        )
                        used_git_worktree = rc_wt == 0
                if not used_git_worktree:
                    shutil.copytree(src_repo, dst_repo, symlinks=True)

            info.symlinked_sub_repos.discard(repo)
            info.materialized_sub_repos.add(repo)

        return info

    async def get_latest_mtime(
        self, task_id: str, domain: str | None = None
    ) -> float:
        info = await self.ensure_worktree(task_id)
        wt_root = Path(info.root_path)
        scan_roots: list[Path] = []
        if domain and (wt_root / domain).exists() and not (wt_root / domain).is_symlink():
            scan_roots.append(wt_root / domain)
        else:
            for repo in info.materialized_sub_repos:
                candidate = wt_root / repo
                if candidate.exists() and not candidate.is_symlink():
                    scan_roots.append(candidate)
            if not scan_roots:
                for entry in wt_root.iterdir():
                    if not entry.is_symlink() and entry.name not in _IGNORED_MTIME_DIRS:
                        scan_roots.append(entry)

        latest = 0.0
        for root in scan_roots:
            if root.is_file():
                latest = max(latest, root.stat().st_mtime)
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _IGNORED_MTIME_DIRS]
                for fname in filenames:
                    fpath = Path(dirpath) / fname
                    try:
                        latest = max(latest, fpath.stat().st_mtime)
                    except OSError:
                        continue
        return latest

    def _target_repo_dirs(
        self, info: WorktreeInfo, sub_repos: Sequence[str] | None = None
    ) -> list[tuple[str, Path]]:
        wt_root = Path(info.root_path)
        targets = list(sub_repos) if sub_repos else sorted(info.materialized_sub_repos)
        dirs: list[tuple[str, Path]] = []
        if (wt_root / ".git").exists() and not sub_repos:
            dirs.append(("root", wt_root))
        for repo in targets:
            repo_dir = wt_root / repo
            if repo_dir.exists() and not repo_dir.is_symlink() and (repo_dir / ".git").exists():
                dirs.append((repo, repo_dir))
        return dirs

    async def get_git_diff(
        self, task_id: str, sub_repos: Sequence[str] | None = None
    ) -> str:
        info = await self.ensure_worktree(task_id)
        chunks: list[str] = []
        for repo, repo_dir in self._target_repo_dirs(info, sub_repos):
            _, status_out, _ = await _run_cmd(
                ["git", "-C", str(repo_dir), "status", "--short"]
            )
            _, diff_out, _ = await _run_cmd(["git", "-C", str(repo_dir), "diff"])
            if status_out.strip() or diff_out.strip():
                chunks.append(f"=== {repo} ===\n{status_out}{diff_out}".strip())
        return "\n\n".join(chunks)

    async def commit_changes(
        self,
        task_id: str,
        message: str,
        sub_repos: Sequence[str] | None = None,
    ) -> str | None:
        info = await self.ensure_worktree(task_id)
        last_sha: str | None = None
        for _, repo_dir in self._target_repo_dirs(info, sub_repos):
            _, status_out, _ = await _run_cmd(
                ["git", "-C", str(repo_dir), "status", "--porcelain"]
            )
            if not status_out.strip():
                continue
            await _run_cmd(["git", "-C", str(repo_dir), "add", "-A"])
            rc, _, err = await _run_cmd(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    message,
                ]
            )
            if rc != 0:
                raise RuntimeError(f"git commit failed in {repo_dir}: {err}")
            rc_sha, sha_out, _ = await _run_cmd(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"]
            )
            if rc_sha == 0:
                last_sha = sha_out.strip()
        return last_sha

    async def merge_locally(self, task_id: str) -> list[str]:
        info = await self.ensure_worktree(task_id)
        ws_root = self.config.workspace.workspace_root
        merged: list[str] = []
        for repo in sorted(info.materialized_sub_repos):
            main_repo = ws_root / repo
            if not (main_repo / ".git").exists():
                continue
            rc, _, _ = await _run_cmd(
                [
                    "git",
                    "-C",
                    str(main_repo),
                    "merge",
                    "--no-ff",
                    "--no-gpg-sign",
                    info.branch_name,
                ]
            )
            if rc == 0:
                merged.append(repo)
        return merged

    async def cleanup_worktree(self, task_id: str) -> None:
        is_direct = task_id in self._direct_tasks
        self._direct_tasks.discard(task_id)
        info = self._worktrees.pop(task_id, None)
        ws_root = self.config.workspace.workspace_root.resolve()
        if is_direct or (info and Path(info.root_path).resolve() == ws_root):
            return
        wt_root = Path(info.root_path) if info else self._task_root(task_id)
        build_root = Path(info.build_root) if info else self._task_build_root(task_id)

        if info and not info.is_btrfs_cow:
            for repo in list(info.materialized_sub_repos):
                src_repo = ws_root / repo
                dst_repo = wt_root / repo
                if (src_repo / ".git").exists() and dst_repo.exists():
                    await _run_cmd(
                        [
                            "git",
                            "-C",
                            str(src_repo),
                            "worktree",
                            "remove",
                            "--force",
                            str(dst_repo),
                        ]
                    )

        if info and info.is_btrfs_cow and wt_root.exists():
            await _run_cmd(["btrfs", "subvolume", "delete", str(wt_root)])

        if wt_root.exists():
            shutil.rmtree(wt_root, ignore_errors=True)
        for candidate_build in (build_root, self._fallback_build_root(task_id)):
            if candidate_build.exists():
                shutil.rmtree(candidate_build, ignore_errors=True)

