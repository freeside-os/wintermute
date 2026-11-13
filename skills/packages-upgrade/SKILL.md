---
name: packages-upgrade
description: Upgrade Freeside OS packages to newer upstream versions, refresh SHA256 checksums, rebase or prune patches, and verify reverse dependencies.
---

# Freeside Package Upgrades

Use this skill when bumping a package under `packages/<pkg_name>/` to a newer upstream release.

## Workflow
1. **Inspect Current Package State**
   - Review `packages/<pkg_name>/package.manifest`, `package.justfile`, and any files under `patches/` using `view_file` and `just_pkg_info`.
2. **Check Upstream Release Notes & Sources**
   - Use `search_web` / `read_url_content` to verify the new version URL, release notes, build-system changes, added/removed dependencies, and potential SONAME/ABI breaks.
3. **Update Manifest & Dynamic Recipes**
   - Update `version` (and `[dependencies]` or `[build.environment]` flags if needed) in `packages/<pkg_name>/package.manifest`.
   - Ensure `package.justfile` keeps using `$PKG_NAME` and `$PKG_VERSION` dynamically (never hardcode version strings).
4. **Refresh SHA256 Checksums**
   - Run `just_pkg_checksum(target=...)` to recompute and pin the updated tarball checksums cleanly.
5. **Rebase or Prune Patches**
   - Drop patches that were merged upstream and cleanly rebase remaining musl-libc or cross-compilation patches.
6. **Verify & Record Lessons**
   - Run `just_pkg_verify(pkg_name=...)`, `just_pkg_resolve(pkg_name=...)`, and `just_pkg_build(pkg_name=...)`.
   - Record non-obvious ABI, musl, or build-flag adjustments via `save_memory_note`.

