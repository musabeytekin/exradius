"""Report-only delivery warning (docs/environment-and-services.md §4).

At finalize, before the run directory is removed, the worker runs a check over the clones
(``ContainerManager.inspect_clones``) and turns its output into warnings for the result.
It changes nothing in the clones and pushes nothing.
"""

from __future__ import annotations


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def delivery_warnings(output: str) -> list[str]:
    """Warnings from the check's ``dirty`` and ``unpushed`` lines, in the order printed."""
    warnings: list[str] = []
    for line in output.splitlines():
        fields = line.rstrip("\r").split("\t")
        try:
            if fields[0] == "dirty" and len(fields) == 3:
                repo, count = fields[1], int(fields[2] or 0)
                if count:
                    warnings.append(
                        f"{repo}: uncommitted changes in {count} {_plural(count, 'file', 'files')} "
                        "were discarded with the task directory"
                    )
            elif fields[0] == "unpushed" and len(fields) == 4:
                repo, branch, count = fields[1], fields[2], int(fields[3] or 0)
                if count:
                    where = f"on {branch}" if branch else "on a detached HEAD"
                    verb = _plural(count, "was", "were")
                    warnings.append(
                        f"{repo}: {count} {_plural(count, 'commit', 'commits')} {where} {verb} not pushed "
                        f"and {verb} discarded with the task directory"
                    )
        except ValueError:
            continue
    return warnings
