"""Structured diagnostic parser for Rust compiler (cargo check / clippy) JSON output."""

from dataclasses import dataclass
import json
import subprocess
from pathlib import Path


@dataclass
class RustDiagnostic:
    """Parsed Rust compiler diagnostic message."""
    level: str
    message: str
    code: str | None
    file_path: str | None
    line_start: int | None
    column_start: int | None
    rendered: str | None


class RustDiagnosticParser:
    """Executes cargo check or cargo clippy with JSON message format and extracts structured diagnostics."""

    @classmethod
    def run_check(cls, target_dir: Path, clippy: bool = False) -> list[RustDiagnostic]:
        cmd = ["cargo", "clippy" if clippy else "check", "--message-format=json"]
        try:
            res = subprocess.run(
                cmd,
                cwd=target_dir,
                capture_output=True,
                text=True,
                check=False
            )
            return cls.parse_json_stream(res.stdout)
        except Exception as e:
            return [RustDiagnostic(
                level="error",
                message=f"Failed to execute cargo command: {e}",
                code=None,
                file_path=None,
                line_start=None,
                column_start=None,
                rendered=str(e)
            )]

    @classmethod
    def parse_json_stream(cls, stream: str) -> list[RustDiagnostic]:
        diagnostics = []
        for line in stream.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
                if data.get("reason") == "compiler-message":
                    msg = data.get("message", {})
                    level = msg.get("level", "error")
                    message_text = msg.get("message", "")
                    code_info = msg.get("code")
                    code = code_info.get("code") if code_info else None
                    rendered = msg.get("rendered")

                    spans = msg.get("spans", [])
                    file_path = None
                    line_start = None
                    column_start = None

                    primary_spans = [s for s in spans if s.get("is_primary")]
                    if primary_spans:
                        p = primary_spans[0]
                        file_path = p.get("file_name")
                        line_start = p.get("line_start")
                        column_start = p.get("column_start")

                    diagnostics.append(RustDiagnostic(
                        level=level,
                        message=message_text,
                        code=code,
                        file_path=file_path,
                        line_start=line_start,
                        column_start=column_start,
                        rendered=rendered
                    ))
            except json.JSONDecodeError:
                continue
        return diagnostics
