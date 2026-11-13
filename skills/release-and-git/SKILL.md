---
name: release-and-git
description: Multi-repo status checks, diff review, release tagging, and documentation link compliance.
---

# Freeside Release & Git Workflows

Use this skill for cross-repository git inspections, documentation updates, and release preparation.

## Rules
- Inspect multi-repo status and diffs using `just_git_status` and `just_git_diff`.
- Always commit inside Freeside sub-repositories with `git commit --no-gpg-sign -m "..."`.
- Use relative markdown links only in documentation and reports; never emit `file://` URLs or absolute local paths.

