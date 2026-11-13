---
name: packages-troubleshooting
description: Diagnose and fix failing Freeside package sandbox builds using logs, musl patches, and hybrid memory.
---

# Freeside Package Build Troubleshooting

Use this skill when `just_pkg_build` fails inside the `systemd-nspawn` musl sandbox.

## Workflow
1. Inspect compiler and linker diagnostics via `just_pkg_logs(pkg_name="<pkg>")`.
2. Query prior musl/sandbox solutions via `search_memory(query="<error signature>", domain="packages")`.
3. Apply minimal, clean fixes (missing `build.dependencies`, musl header/libc compatibility patches, or `[build.environment]` flags).
4. Re-run `just_pkg_verify` and `just_pkg_build`.
5. Persist reusable fixes via `save_memory_note(issue=..., resolution=..., domain="packages", target="<pkg>")`.

