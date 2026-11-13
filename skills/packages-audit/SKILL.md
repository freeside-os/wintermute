---
name: packages-audit
description: Read-only verification, dependency graph inspection, and OSV CVE auditing for Freeside packages.
---

# Freeside Package Auditing

Use this skill for read-only compliance checks, dependency inspection, and CVE auditing.

## Available Tools
- `just_pkg_verify` and `just_pkg_verify_all`: Validate `package.manifest` and `package.justfile` compliance.
- `just_pkg_resolve`: Compute topological build order for a package or group.
- `just_pkg_info` and `just_pkg_list`: Inspect package metadata and groups.
- `just_pkg_audit`: Query OSV feeds for known CVEs and detect missing SHA256 checksums.

## Reporting Format
- Categorize CVE findings by package, advisory ID, severity/fixed version, and missing SHA256 checksums.
- Recommend the follow-up remediation path for each finding: `packages-upgrade` (compatible upstream fix version available) vs `cve-remediation` (backport patch required).

