import json
import os
from typing import Any, Dict, List

PATTERN_FILE = "compiler_patterns.json"

DEFAULT_PATTERNS = [
    {
        "regex": r"(?i)\berror\b",
        "description": "General error keyword",
        "false_positive_contexts": [],
        "disabled": False
    },
    {
        "regex": r"(?i)fatal error",
        "description": "Fatal error keyword",
        "false_positive_contexts": [],
        "disabled": False
    },
    {
        "regex": r"(?i)\bfailed\b",
        "description": "General failed keyword",
        "false_positive_contexts": [],
        "disabled": False
    },
    {
        "regex": r"(?i)undefined reference",
        "description": "Linker undefined reference",
        "false_positive_contexts": [],
        "disabled": False
    },
    {
        "regex": r"(?i)ld returned",
        "description": "Linker ld returned error",
        "false_positive_contexts": [],
        "disabled": False
    },
    {
        "regex": r"(?i)cannot find -l",
        "description": "Linker cannot find library",
        "false_positive_contexts": [],
        "disabled": False
    },
    {
        "regex": r"(?i)collect2:",
        "description": "GCC collect2 error",
        "false_positive_contexts": [],
        "disabled": False
    }
]

def load_patterns() -> List[Dict[str, Any]]:
    if not os.path.exists(PATTERN_FILE):
        with open(PATTERN_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_PATTERNS, f, indent=2)
        return DEFAULT_PATTERNS

    with open(PATTERN_FILE, "r", encoding="utf-8") as f:
        try:
            patterns = json.load(f)
            return patterns
        except json.JSONDecodeError:
            return DEFAULT_PATTERNS

def save_patterns(patterns: List[Dict[str, Any]]):
    with open(PATTERN_FILE, "w", encoding="utf-8") as f:
        json.dump(patterns, f, indent=2)

def get_active_patterns() -> List[str]:
    patterns = load_patterns()
    return [p["regex"] for p in patterns if not p.get("disabled", False)]

def add_error_pattern(regex: str, description: str) -> str:
    """Adds a new compiler error regex pattern to the active list.

    Args:
        regex: The regex pattern to match errors.
        description: A short description of what this pattern catches.

    Returns:
        A success message.
    """
    patterns = load_patterns()
    for p in patterns:
        if p["regex"] == regex:
            return f"Pattern {regex} already exists."

    patterns.append({
        "regex": regex,
        "description": description,
        "false_positive_contexts": [],
        "disabled": False
    })
    save_patterns(patterns)
    return f"Pattern {regex} added successfully."

def report_false_positive_pattern(regex: str, context: str) -> str:
    """Reports a pattern for causing false positives (noise). If reported from 3 different contexts, it gets disabled.

    Args:
        regex: The regex pattern to report.
        context: The context (e.g. package name) where it failed.

    Returns:
        A message about the updated pattern status.
    """
    patterns = load_patterns()
    for p in patterns:
        if p["regex"] == regex:
            if "false_positive_contexts" not in p:
                p["false_positive_contexts"] = []

            if context not in p["false_positive_contexts"]:
                p["false_positive_contexts"].append(context)

            if len(p["false_positive_contexts"]) >= 3:
                p["disabled"] = True
                save_patterns(patterns)
                return f"Pattern '{regex}' disabled after 3 false positive reports."

            save_patterns(patterns)
            return f"Pattern '{regex}' reported. {3 - len(p['false_positive_contexts'])} more reports until disabled."

    return f"Pattern '{regex}' not found."
