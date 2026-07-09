from unittest.mock import patch

from click.testing import CliRunner

from app.cli import cli
from app.tools.compilation import scan_build_log
from app.tools.status import update_task_status

# ------------------------------------------------------------------------------
# 1. Test Status Tool
# ------------------------------------------------------------------------------

def test_update_task_status(capsys) -> None:
    update_task_status("Compiling package...")
    captured = capsys.readouterr()
    assert captured.out == "[status] Compiling package...\n"


# ------------------------------------------------------------------------------
# 2. Test Signature Log Scanner
# ------------------------------------------------------------------------------

def test_scan_build_log_redefined_inline() -> None:
    mock_log = {
        "status": "success",
        "file": "test-pkg-1.0.log",
        "content": "Line 42: error: redefinition of 'foo_inline' as inline function"
    }
    with patch("app.tools.compilation.read_build_logs", return_value=mock_log):
        res = scan_build_log("test-pkg")
        assert res["status"] == "success"
        assert res["signature"] == "redefined_inline"
        assert res["proposal"]["type"] == "env_injection"
        assert res["proposal"]["env"]["CFLAGS"] == "-fcommon"


def test_scan_build_log_missing_argp() -> None:
    mock_log = {
        "status": "success",
        "file": "test-pkg-1.0.log",
        "content": "Line 99: usr/bin/ld: cannot find -largp"
    }
    with patch("app.tools.compilation.read_build_logs", return_value=mock_log):
        res = scan_build_log("test-pkg")
        assert res["status"] == "success"
        assert res["signature"] == "missing_argp"
        assert res["proposal"]["type"] == "env_injection"
        assert res["proposal"]["env"]["LDFLAGS"] == "-largp"


def test_scan_build_log_missing_makeinfo() -> None:
    mock_log = {
        "status": "success",
        "file": "test-pkg-1.0.log",
        "content": "Line 12: makeinfo: command not found"
    }
    with patch("app.tools.compilation.read_build_logs", return_value=mock_log):
        res = scan_build_log("test-pkg")
        assert res["status"] == "success"
        assert res["signature"] == "missing_makeinfo"
        assert res["proposal"]["type"] == "env_injection"
        assert res["proposal"]["env"]["MAKEINFO"] == "true"


def test_scan_build_log_no_match() -> None:
    mock_log = {
        "status": "success",
        "file": "test-pkg-1.0.log",
        "content": "A perfectly successful build log"
    }
    with patch("app.tools.compilation.read_build_logs", return_value=mock_log):
        res = scan_build_log("test-pkg")
        assert res["status"] == "success"
        assert res["signature"] is None
        assert res["proposal"] is None


# ------------------------------------------------------------------------------
# 3. Test CLI Subcommands
# ------------------------------------------------------------------------------

def test_cli_check_single_package() -> None:
    runner = CliRunner()

    mock_verify = {"status": "success", "stdout": "All verified"}
    mock_feeds = {
        "status": "success",
        "cves": [
            {"package": "test-pkg", "cve_id": "CVE-2026-1111", "severity": "HIGH", "fixed_version": "1.0.1"}
        ]
    }

    with patch("app.use_cases.verify_package", return_value=mock_verify), \
         patch("app.use_cases.query_security_feeds", return_value=mock_feeds), \
         patch("app.use_cases.get_latest_upstream_version", return_value="1.0.1"), \
         patch("app.use_cases.packages_root", return_value="/tmp/packages"), \
         patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open_manifest()):

        result = runner.invoke(cli, ["check", "test-pkg"])
        assert result.exit_code == 0
        assert "Package: test-pkg" in result.output
        assert "Local Version: 1.0.0" in result.output
        assert "Upstream version: 1.0.1" in result.output
        assert "Lint Verification: SUCCESS" in result.output
        assert "Security CVEs: CVE-2026-1111 (Severity: HIGH)" in result.output


def test_cli_import_package_arch() -> None:
    runner = CliRunner()

    with patch("app.use_cases.pkgbuild_exists_on_arch", return_value=True), \
         patch("app.use_cases.import_pkgbuild", return_value={"status": "success"}), \
         patch("app.use_cases.run_workflow_sync", return_value={}) as mock_run:

        result = runner.invoke(cli, ["import", "test-pkg"])
        assert result.exit_code == 0
        assert "Importing PKGBUILD for package 'test-pkg'..." in result.output
        assert "Routing 'test-pkg' to refinement and sandbox build..." in result.output
        mock_run.assert_called_once()


def test_cli_import_package_not_found() -> None:
    runner = CliRunner()

    with patch("app.use_cases.pkgbuild_exists_on_arch", return_value=False):
        result = runner.invoke(cli, ["import", "test-pkg"])
        assert result.exit_code == 1
        assert "Error: PKGBUILD for 'test-pkg' not found" in result.output


def test_cli_create_package() -> None:
    runner = CliRunner()

    with patch("app.use_cases.run_workflow_sync", return_value={}) as mock_run:
        result = runner.invoke(cli, ["create", "test-pkg", "--version", "2.0.0", "--group", "base"])
        assert result.exit_code == 0
        assert "Scaffolding skeleton for package 'test-pkg'" in result.output
        mock_run.assert_called_once()
        # Verify passed state
        state_arg = mock_run.call_args[0][2]
        assert state_arg["version"] == "2.0.0"
        assert state_arg["group"] == "base"


def test_cli_fix_success_immediately() -> None:
    runner = CliRunner()

    with patch("app.use_cases.build_package", return_value={"status": "success"}):
        result = runner.invoke(cli, ["fix", "test-pkg"])
        assert result.exit_code == 0
        assert "compiled successfully! No fixes needed." in result.output


def test_cli_fix_auto_patch_success() -> None:
    runner = CliRunner()

    mock_build_fail = {"status": "error"}
    mock_build_success = {"status": "success"}
    mock_scan = {
        "status": "success",
        "signature": "redefined_inline",
        "proposal": {
            "type": "env_injection",
            "env": {"CFLAGS": "-fcommon"},
            "message": "Injecting CFLAGS"
        }
    }

    with patch("app.use_cases.build_package", side_effect=[mock_build_fail, mock_build_success]), \
         patch("app.use_cases.scan_build_log", return_value=mock_scan), \
         patch("app.use_cases.inject_env_into_manifest", return_value=True):

        result = runner.invoke(cli, ["fix", "test-pkg"])
        assert result.exit_code == 0
        assert "Matched signature: redefined_inline" in result.output
        assert "Package 'test-pkg' built successfully after signature auto-patch!" in result.output


def test_cli_upgrade_with_version() -> None:
    runner = CliRunner()

    with patch("app.use_cases.run_workflow_sync", return_value={}) as mock_run:
        result = runner.invoke(cli, ["upgrade", "test-pkg", "1.2.3"])
        assert result.exit_code == 0
        assert "Upgrading package 'test-pkg' to version 1.2.3..." in result.output
        mock_run.assert_called_once()


def test_cli_check_all_healthy() -> None:
    runner = CliRunner()

    mock_verify = {"status": "success", "stdout": "All verified"}
    mock_feeds = {"status": "success", "cves": []}
    mock_workspace_pkgs = {"status": "success", "packages": ["pkg-a", "pkg-b"]}

    with patch("app.use_cases.verify_package", return_value=mock_verify), \
         patch("app.use_cases.query_security_feeds", return_value=mock_feeds), \
         patch("app.use_cases.get_latest_upstream_version", return_value="1.0.0"), \
         patch("app.use_cases.list_workspace_packages", return_value=mock_workspace_pkgs), \
         patch("app.use_cases.packages_root", return_value="/tmp/packages"), \
         patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open_manifest()):

        result = runner.invoke(cli, ["check", "--all"])
        assert result.exit_code == 0
        assert "All packages are healthy" in result.output
        assert "Package: pkg-a" not in result.output



# ------------------------------------------------------------------------------
# Mock Helpers
# ------------------------------------------------------------------------------

def mock_open_manifest():
    import io
    manifest_content = """
    [package]
    name = "test-pkg"
    version = "1.0.0"
    """
    def _open(*args, **kwargs):
        mode = kwargs.get("mode", args[1] if len(args) > 1 else "r")
        if "b" in mode:
            return io.BytesIO(manifest_content.encode("utf-8"))
        return io.StringIO(manifest_content)
    return _open

