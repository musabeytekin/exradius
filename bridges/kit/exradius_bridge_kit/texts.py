"""Every sentence the kit posts (design §6). English only in v1."""

from __future__ import annotations

from typing import Any

from exradius_bridge_kit.tracker import Markdown, Mention, Message, Paragraph, Person, Span


def _agent(label: str | None) -> Span:
    return Span(label or "The agent", bold=True)


def _message(*blocks: Paragraph | Markdown | None) -> Message:
    return Message(tuple(b for b in blocks if b is not None and not (isinstance(b, Markdown) and not b.text.strip())))


def percent(p: float) -> str:
    return f"{round(p * 100)}%"


def start(label: str, routing: dict[str, Any] | None, names: dict[str, str]) -> Message:
    """The start comment: the agent Exradius picked, and whether it was a guess."""
    head = Paragraph((_agent(label), Span(" is working on this.")))
    if not (routing and routing.get("method") == "decided" and routing.get("guess")):
        return _message(head)
    probabilities = sorted((routing.get("probabilities") or {}).items(), key=lambda kv: -kv[1])[:3]
    if probabilities:
        shown = ", ".join(f"{names.get(slug, slug)} {percent(p)}" for slug, p in probabilities)
        note = f" This was a guess ({shown})."
    else:
        note = " This was a guess."
    return _message(Paragraph((_agent(label), Span(" is working on this."), Span(note))))


def still_working(label: str | None) -> Message:
    if label is None:
        return _message(Paragraph((Span("This is still being worked on."),)))
    return _message(Paragraph((_agent(label), Span(" is still working on this."))))


def unclaimed(scope: str) -> Message:
    return _message(Paragraph((Span(f"No Exradius project takes work from {scope}."),)))


def claimed_twice(scope: str, projects: list[str], bridge: str) -> Message:
    names = " and ".join([", ".join(projects[:-1]), projects[-1]]) if len(projects) > 1 else projects[0]
    return _message(Paragraph((
        Span(f"{scope} is claimed by {names}; their bridges/{bridge}.yaml files need fixing."),
    )))


def question(label: str | None, reporter: Person | None, text: str, context: str | None) -> Message:
    asker = (_agent(label), Span(" asks "), Mention(reporter), Span(":")) if reporter else (_agent(label), Span(" asks:"))
    return _message(
        Paragraph(asker),
        Markdown(text or ""),
        Markdown(context or ""),
        Paragraph((Span("Mention me in your reply to answer.", italic=True),)),
    )


def result(label: str | None, payload: dict[str, Any], attached: list[str], failed: list[str]) -> Message:
    state = payload.get("state")
    if state == "completed":
        head = Paragraph((_agent(label), Span(" finished.")))
    elif state == "cancelled":
        head = Paragraph((_agent(label), Span(" was stopped.")))
    else:
        head = Paragraph((_agent(label), Span(" could not finish: " + str(payload.get("error") or "no reason was given."))))
    warnings = [str(w) for w in payload.get("warnings") or []]
    return _message(
        head,
        Markdown(str(payload.get("summary") or "")),
        Paragraph((Span("Warnings", bold=True),)) if warnings else None,
        Markdown("\n".join(f"- {w}" for w in warnings)) if warnings else None,
        Paragraph((Span("Attached: " + ", ".join(attached)),)) if attached else None,
        Paragraph((Span("Could not attach: " + ", ".join(failed)),)) if failed else None,
    )


def rejected(reason: str) -> Message:
    return _message(Paragraph((Span("This could not start: " + (reason or "no reason was given.")),)))


def too_large(names: list[str]) -> str:
    """Appended to an answer whose attachments did not fit."""
    return "\n\n(Too large to pass on: " + ", ".join(names) + ".)"
