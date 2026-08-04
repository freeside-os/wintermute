"""Type definitions for Wintermute agentic platform."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, TypeAlias


class KnownDomain(StrEnum):
    """Built-in domains for IDE autocompletion and type safety."""
    PACKAGES = "packages"
    STRAYLIGHT = "straylight"
    INSTALLER = "installer"
    SIMSTIM = "simstim"
    FREESIDECTL = "freesidectl"
    FSTOOLS = "fstools"


DomainKind: TypeAlias = KnownDomain | str


class KnownCoreWorkflow(StrEnum):
    """Universal core workflows supported across components."""
    CHECK = "check"
    FIX = "fix"
    CREATE = "create"
    IMPORT = "import"
    UPGRADE = "upgrade"
    TEST = "test"
    AUDIT = "audit"
    RELEASE = "release"


class PackagesWorkflow(StrEnum):
    CREATE_PACKAGE = "packages:create"
    IMPORT_PKGBUILD = "packages:import"
    FIX_BUILD = "packages:fix"
    UPGRADE_VERSION = "packages:upgrade"
    REVIEW_COMPLIANCE = "packages:review"
    SECURITY_AUDIT = "packages:security_audit"


class StraylightWorkflow(StrEnum):
    RUST_CHECK = "straylight:check"
    CLIPPY_FIX = "straylight:clippy_fix"
    AUDIT_RPC = "straylight:audit_rpc"
    BENCHMARK_CASYNC = "straylight:benchmark"


class InstallerWorkflow(StrEnum):
    RUST_CHECK = "installer:check"
    QEMU_BOOT_TEST = "installer:qemu_boot_test"
    DPS_UUID_AUDIT = "installer:dps_uuid_audit"


class SimstimWorkflow(StrEnum):
    PROVISION = "simstim:provision"
    SNAPSHOT_VERIFY = "simstim:snapshot_verify"


class FreesidectlWorkflow(StrEnum):
    SCAFFOLD_SUBCOMMAND = "freesidectl:scaffold_subcommand"
    COMPLETION_SYNC = "freesidectl:completion_sync"


class FstoolsWorkflow(StrEnum):
    SCHEMA_VALIDATE = "fstools:schema_validate"
    CAIDX_AUDIT = "fstools:caidx_audit"


WorkflowKind: TypeAlias = (
    KnownCoreWorkflow
    | PackagesWorkflow
    | StraylightWorkflow
    | InstallerWorkflow
    | SimstimWorkflow
    | FreesidectlWorkflow
    | FstoolsWorkflow
    | str
)


@dataclass(frozen=True)
class ExecutionSpec:
    """Execution specification for a single workflow task."""
    domain: DomainKind
    workflow: WorkflowKind
    target: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    execution_env: Literal["host", "simstim"] = "host"
    tools_filter: list[str] | None = None
    skills_filter: list[str] | None = None
    session_id: str | None = None
    auth_context: dict[str, Any] = field(default_factory=dict)
