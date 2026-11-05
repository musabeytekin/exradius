"""Prompt assembly (architecture §5.2). Never mentions workspaces or Exradius internals."""

from __future__ import annotations

import json

from exradius.schemas.internal import DeliveredAnswer, RunSpecResponse


def build_prompt(spec: RunSpecResponse) -> str:
    parts = [spec.agent_prompt.strip(), f"# Task\n{spec.instructions.strip()}"]
    if spec.context is not None:
        parts.append(f"# Context\n{json.dumps(spec.context, indent=2, ensure_ascii=False)}")
    if spec.attachments:
        lines = "\n".join(f"- {a.path} ({a.content_type})" for a in spec.attachments)
        parts.append(f"# Attachments\n{lines}")
    return "\n\n".join(p for p in parts if p)


def build_resume_message(answers: list[DeliveredAnswer]) -> str:
    sections = []
    for answer in answers:
        text = f"Answer to your question: {answer.question.strip()}\n\n{answer.answer.strip()}"
        if answer.attachments:
            lines = "\n".join(f"- {a.path} ({a.content_type})" for a in answer.attachments)
            text += f"\n\nAttachments:\n{lines}"
        sections.append(text)
    return "\n\n---\n\n".join(sections)
