---
name: straylight-dev
description: Develop, test, and verify the Straylight Rust CLI, straylightd daemon, and state sync engine.
---

# Straylight Rust Development

Use this skill when modifying `straylight/` (Rust CLI, `straylightd` daemon, or state synchronization).

## Workflow
- Follow the `tdd_coding` pipeline: write or update unit/integration tests first, then implement production code.
- Always invoke `just_straylight_test` and `just_straylight_build` instead of running raw `cargo` commands directly.
- Preserve RPC/CLI contracts and verify clean compilation with no warnings.

