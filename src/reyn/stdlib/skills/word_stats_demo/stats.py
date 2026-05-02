"""Deterministic text statistics for the word_stats_demo skill.

Pure-mode python preprocessor function — runs sandboxed via
reyn._python_harness. Sees a deep-copied artifact dict and returns
JSON-serializable data placed at the configured `into` path.
"""


def compute_text_stats(artifact: dict) -> dict:
    text = artifact.get("data", {}).get("text", "") or ""
    lines = text.splitlines()
    line_count = len(lines) if lines else (1 if text else 0)
    word_count = len(text.split())
    char_count = len(text)
    longest_line = max((len(line) for line in lines), default=0)
    return {
        "char_count": char_count,
        "word_count": word_count,
        "line_count": line_count,
        "longest_line_chars": longest_line,
        # Rough estimate; useful when warning the LLM about long inputs.
        "estimated_tokens": max(1, char_count // 4),
    }
