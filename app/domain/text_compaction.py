"""Diagnostic-preserving text compaction helpers for tool outputs, logs, and diffs."""

from __future__ import annotations

import re

_ERROR_LINE_RE = re.compile(
    r"(?:\berror\b[:\[]|\bfatal\b[:\s]|\bfail(?:ed|ure)?\b|\btraceback\b|"
    r"\bassertionerror\b|\bexception\b|undefined reference|\bpanic\b|"
    r"^\s*E\s+|^\s*FAILED\b)",
    re.IGNORECASE,
)


def compact_command_output(
    text: str,
    *,
    max_chars: int = 12_000,
    head_chars: int = 2_000,
    tail_chars: int = 6_000,
) -> str:
    """Compact noisy command/log output while preserving head, error lines, and tail."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    if max_chars < 400:
        suffix = "\n[... truncated ...]"
        keep = max(0, max_chars - len(suffix))
        return f"{text[:keep]}{suffix}"[:max_chars]

    eff_head = min(head_chars, max_chars // 4)
    eff_tail = min(tail_chars, max_chars // 2)
    head = text[:eff_head].rstrip()
    tail_start = max(eff_head, len(text) - eff_tail)
    tail = text[tail_start:].lstrip()
    middle = text[eff_head:tail_start]

    middle_budget = max(0, max_chars - len(head) - len(tail) - 120)
    matched_lines: list[str] = []
    used = 0
    seen: set[str] = set()
    for raw_line in middle.splitlines():
        if not _ERROR_LINE_RE.search(raw_line):
            continue
        clipped_line = raw_line if len(raw_line) <= 240 else f"{raw_line[:237]}..."
        if clipped_line in seen:
            continue
        cost = len(clipped_line) + 1
        if used + cost > middle_budget:
            break
        seen.add(clipped_line)
        matched_lines.append(clipped_line)
        used += cost

    omitted_before = max(0, tail_start - eff_head)
    parts: list[str] = [head]
    if matched_lines:
        parts.append(
            f"[... {omitted_before:,} middle chars scanned; key diagnostic lines ...]\n"
            + "\n".join(matched_lines)
        )
    else:
        parts.append(f"[... omitted {omitted_before:,} chars ...]")
    parts.append(tail)

    compacted = "\n".join(p for p in parts if p)
    if len(compacted) <= max_chars:
        return compacted
    overflow = len(compacted) - max_chars
    if len(tail) > overflow + 32:
        trimmed_tail = tail[overflow + 20 :]
        parts[-1] = f"[...]\n{trimmed_tail}"
        compacted = "\n".join(p for p in parts if p)
    return compacted[:max_chars]


def compact_diff_output(diff_text: str, *, max_chars: int = 24_000) -> str:
    """Compact oversized git diffs while keeping touched-file headers and tail context."""
    if not diff_text or max_chars <= 0 or len(diff_text) <= max_chars:
        return diff_text

    file_headers = [
        ln
        for ln in diff_text.splitlines()
        if ln.startswith(("diff --git ", "+++ ", "--- "))
    ]
    header_summary = ""
    if file_headers:
        header_block = "\n".join(file_headers[:60])
        header_summary = f"[Touched files summary]\n{header_block}\n\n"

    remaining = max(512, max_chars - len(header_summary))
    body = compact_command_output(
        diff_text,
        max_chars=remaining,
        head_chars=remaining // 2,
        tail_chars=remaining // 3,
    )
    combined = f"{header_summary}{body}"
    return combined[:max_chars]

