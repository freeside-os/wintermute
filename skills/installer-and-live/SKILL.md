---
name: installer-and-live
description: Develop and verify the Freeside Textual TUI installer and live ISO/QEMU orchestration.
---

# Freeside Installer & Live Image Development

Use this skill when working on `installer/` or live ISO orchestration.

## Workflow
- Write or update `pytest` unit tests in `installer/` before modifying installer logic.
- Run `just_installer_test` and `just_installer_build` to verify tests and PEX packaging.
- For live ISO assembly or QEMU verification, use `just_live_build` and `just_live_run` (protected by global build locks).

