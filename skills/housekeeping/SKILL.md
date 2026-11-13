---
name: housekeeping
description: Repository-wide sweep for code consistency, dead code and unused dependency removal, library freshness, and living documentation alignment.
---

# Repository Housekeeping & Hygiene Sweep

Use this skill for periodic or targeted cleanups across Freeside OS repositories (`straylight`, `fstools`, `installer`, `wintermute`, `packages`, `docs`) to eliminate bitrot and drift.

## Sweep Checklist

1. **Dead Code, Unused Imports & Orphaned Files**
   - Identify unreferenced functions, stale feature flags, commented-out blocks, and unused imports in Rust (`straylight`) and Python (`fstools`, `installer`, `wintermute`).
   - Check for orphaned test fixtures, temporary scripts, or superseded configs and remove them cleanly without altering external behavior.

2. **Consistency & Pattern Alignment**
   - Ensure package recipes use dynamic metadata variables (`$PKG_NAME`, `$PKG_VERSION`, `$DESTDIR`) and `[build.environment]` cleanly rather than hardcoded paths or versions.
   - Verify hexagonal boundaries in `wintermute` (`ports.py` protocols vs adapters) and consistent error handling / typing across CLI modules (`fstools`, `straylight`).
   - Confirm root `justfile` / `just/*.just` recipes stay aligned with underlying CLI subcommands.

3. **Dependency & Library Freshness**
   - Audit `Cargo.toml`, `pyproject.toml`, `.mise.toml`, and `packages/` manifests for pinned outdated crates/packages, duplicate dependencies, or known advisories (use `just_pkg_audit` for OS packages).
   - Upgrade dependencies conservatively in scoped commits and re-verify lockfiles and unit tests after each update.

4. **Living Documentation Alignment**
   - Cross-check `docs/`, `wintermute/skills/`, and `wintermute/agents/` against actual CLI flags, TOML schemas, and file paths.
   - Fix broken or outdated references and enforce relative markdown links only (no `file://` URLs or absolute local paths).

5. **Verification Before Commit**
   - Run the domain test suites (`just_straylight_test`, `just_fstools_test`, `just_installer_test`, `just_wintermute_test`, `just_pkg_verify`) for every repository touched by the sweep.

