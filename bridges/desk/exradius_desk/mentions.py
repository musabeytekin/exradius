"""Mentions in comments (design §8): stored as references, rendered with today's names."""

from __future__ import annotations

import re
from collections.abc import Mapping

EXRADIUS = "Exradius"
"""The built-in assignee's name, in the assignee list, the mention list and comments."""

TOKEN = re.compile(r"@\[(?:person:(\d+)|(exradius))\]")


def parse(body: str) -> tuple[set[int], bool]:
    """The people mentioned, and whether Exradius is."""
    people: set[int] = set()
    exradius = False
    for match in TOKEN.finditer(body):
        if match.group(2):
            exradius = True
        else:
            people.add(int(match.group(1)))
    return people, exradius


def plain(body: str, names: Mapping[int, str]) -> str:
    """Mentions as ``@Name`` in plain text: for the agent and for mail."""

    def name(match: re.Match) -> str:
        if match.group(2):
            return f"@{EXRADIUS}"
        return f"@{names.get(int(match.group(1)), 'someone')}"

    return TOKEN.sub(name, body)
