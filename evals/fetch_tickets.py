"""Fetch each task's ticket, the upstream issue as it was reported, into evals/tickets/.

The texts are not committed. They are read from GitHub with `gh` and checked against the hash
in the task file, so an issue that was edited after the task was selected is noticed.

    cd backend && uv run python ../evals/fetch_tickets.py
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent


def ticket_text(issue: dict) -> str:
    body = (issue["body"] or "").replace("\r\n", "\n").strip()
    return f"# {issue['title'].strip()}\n\n{body}\n"


def main() -> int:
    tickets = ROOT / "tickets"
    tickets.mkdir(exist_ok=True)
    changed = 0
    for path in sorted((ROOT / "tasks").glob("*.yaml")):
        task = yaml.safe_load(path.read_text())
        issue = task["issue"]
        fetched = subprocess.run(
            ["gh", "api", f"repos/{issue['upstream']}/issues/{issue['number']}"],
            capture_output=True, text=True, check=True,
        )
        text = ticket_text(json.loads(fetched.stdout))
        if hashlib.sha256(text.encode()).hexdigest() != task["issue"]["sha256"]:
            changed += 1
            print(f"{task['id']}: the issue text changed since the task was selected", file=sys.stderr)
            continue
        (tickets / f"{task['id']}.md").write_text(text)
        print(f"{task['id']}: {len(text)} characters")
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
