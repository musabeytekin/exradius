"""Prompt assembly (architecture §13).

System prompt: the runtime's preset, then ``OPERATING_RULES``, then the generated environment
section (docs/environment-and-services.md §1), then the agent's prompt (its role). First user message: the ticket only (task, context, attachments). Neither mentions
workspaces or Exradius internals.
"""

from __future__ import annotations

import json

from exradius.schemas.internal import DeliveredAnswer, RunSpecResponse

OPERATING_RULES = """\
You are working unattended on a task that was assigned through an external system such as
an issue tracker. The task text below was written by a person who does not know how you
run and who will see only two things from you: the questions you ask and the result you
deliver.

- You start inside the project's directory. Each folder in it is a git clone of one of
  the project's repositories, checked out on its working branch with origin set to the
  real remote.
- If you cannot proceed without something only that person can tell you, call
  request_information with one clear question written for them, then stop; you will be
  resumed with the answer. Each question is a round trip through their system, so ask
  only when necessary and never more than one question at a time.
- When you are done, call submit_result with a result written for that person: what you
  found, what you changed, and what they should do next. Attach files that help.
- Nobody is watching. Do not ask for confirmation or permission; decide and act. If the
  task cannot be done, say why in the result.
- The project directory exists only for this task and is discarded afterwards, so
  changes left uncommitted are lost. If your work changes a repository, commit it there
  on a new branch whose name starts with the task id shown in the task heading, and push
  that branch to origin; say in the result what you pushed and where. If the push is
  refused, attach a patch of your commits (git format-patch) to the result and say so.
- Refer to files by their path inside the repository, never by the directory you run in."""
"""How every agent runs, whatever its prompt or the ticket says (architecture §13). The
admin panel's guide shows this text verbatim."""


def build_system_prompt(spec: RunSpecResponse) -> str:
    """Appended to the runtime's own system prompt: the operating rules, the environment,
    then the role."""
    parts = (OPERATING_RULES, spec.environment_text.strip(), spec.agent_prompt.strip())
    return "\n\n".join(part for part in parts if part)


def build_task_message(spec: RunSpecResponse) -> str:
    """The first user message: the ticket as it arrived, and nothing else."""
    parts = [f"# Task {spec.task_id}\n{spec.instructions.strip()}"]
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
