"""Quote replies: a comment that quotes the bot's question answers it (design §3.2)."""

from __future__ import annotations

import re

from exradius_github import state

MIN_QUOTE = 8
_QUOTE = re.compile(r"^\s{0,3}>")
_EMPHASIS = re.compile(r"[*_`~]")
_SPACE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _SPACE.sub(" ", _EMPHASIS.sub("", text)).strip().casefold()


def quoted_lines(body: str) -> list[str]:
    """The text of every quoted line, nested quotes unwrapped."""
    lines = []
    for line in (body or "").splitlines():
        if not _QUOTE.match(line):
            continue
        while _QUOTE.match(line):
            line = _QUOTE.sub("", line, count=1)
        lines.append(line)
    return lines


def quotes(reply: str, question: str) -> bool:
    """Whether the reply quotes the question: one quoted line of eight or more characters
    that appears in the question, ignoring emphasis, spacing and case."""
    text = _norm(state.strip(question))
    for line in quoted_lines(reply):
        quoted = _norm(line)
        if len(quoted) >= MIN_QUOTE and quoted in text:
            return True
    return False


def without_quotes(body: str) -> str:
    kept = [line for line in (body or "").splitlines() if not _QUOTE.match(line)]
    return "\n".join(kept).strip()
