import os
import subprocess
import pytest
from app.tools.compilation import verify_package, build_package, read_build_logs, parse_compiler_errors

def test_verify_package(monkeypatch):
    class MockProcess:
        returncode = 0
        stdout = "Verified"
        stderr = ""

    def mock_run(*args, **kwargs):
        return MockProcess()

    monkeypatch.setattr(subprocess, "run", mock_run)
    res = verify_package("test-pkg", workspace_root="tests/test_data")
    assert res["status"] == "success"
    assert res["stdout"] == "Verified"

def test_build_package(monkeypatch):
    class MockProcess:
        returncode = 0
        stdout = "Built"
        stderr = ""

    def mock_run(*args, **kwargs):
        return MockProcess()

    monkeypatch.setattr(subprocess, "run", mock_run)
    res = build_package("test-pkg", keep_sandbox=True, workspace_root="tests/test_data")
    assert res["status"] == "success"
    assert res["stdout"] == "Built"

def test_read_build_logs(tmpdir, monkeypatch):
    log_dir = str(tmpdir)
    monkeypatch.setattr("app.tools.compilation.glob.glob", lambda pattern: [os.path.join(log_dir, "test-pkg-123.log")])
    monkeypatch.setattr("os.path.getmtime", lambda f: 1)

    with open(os.path.join(log_dir, "test-pkg-123.log"), "w") as f:
        f.write("Some log\nERROR: bad\nMore log")

    res = read_build_logs("test-pkg", workspace_root="tests/test_data")
    assert res["status"] == "success"
    assert "bad" in res["content"]

def test_read_build_logs_not_found(monkeypatch):
    monkeypatch.setattr("app.tools.compilation.glob.glob", lambda pattern: [])
    res = read_build_logs("test-pkg", workspace_root="tests/test_data")
    assert res["status"] == "error"

def test_parse_compiler_errors():
    logs = "\n".join([f"Line {i}" for i in range(300)])
    logs += "\nERROR: something failed\n"
    logs += "\n".join([f"Line {i+300}" for i in range(10)])

    parsed = parse_compiler_errors(logs)
    assert "something failed" in parsed

    no_errors = "\n".join([f"Line {i}" for i in range(300)])
    parsed_no_errors = parse_compiler_errors(no_errors)
    assert "No specific error patterns matched" in parsed_no_errors
