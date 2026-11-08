"""Labels start tasks; questions, Quote-reply answers and results, against a fake GitHub."""

from __future__ import annotations

import pytest

from exradius_github.state import decode
from tests.conftest import World
from tests.fakes import BOT


async def started(world: World, number: int = 7, **issue):
    ref = world.issue(number, **issue)
    await world.label(ref)
    return ref


def question_event(world: World, ref, question: str = "Which **version** broke it?", request_id: str = "rq-1") -> dict:
    return world.event(ref, "info_request", {"request_id": request_id, "question": question, "context": None})


# -- starting ----------------------------------------------------------------------------


async def test_an_agent_label_starts_a_task(world: World):
    ref = world.issue(body="Steps:\n\n1. open estimates")
    world.github.repos["acme/plane"]["issues"][7]["labels"].append({"name": "bug", "description": "Something isn't working"})
    world.github.add_comment("acme/plane", 7, "can", "Seen on staging too.")
    await world.label(ref)

    [task] = world.publisher.tasks
    assert task["task_id"] == "plane-7"
    assert (task["project"], task["agent"]) == ("plane", "developer")
    assert task["instructions"] == "Estimates return 500\n\nSteps:\n\n1. open estimates"
    assert task["context"]["issue"]["labels"] == ["bug"]  # not the agent label
    assert task["context"]["issue"]["url"] == "https://github.com/acme/plane/issues/7"
    assert task["context"]["comments"] == [{"author": "can", "created": "2026-11-08T09:00:02Z", "body": "Seen on staging too."}]
    assert task["correlation"] == {"github": {"host": "github.com", "repository": "acme/plane", "issue": 7, "installation": 77}}
    assert world.github.bodies("acme/plane", 7)[-1] == "**Plane · Developer** is working on this."
    state = world.state(ref)
    assert (state.state, state.requester, state.agent_label) == ("active", "octocat", "agent: Developer")


async def test_the_state_marker_is_hidden_from_the_agent(world: World):
    ref = await started(world)
    await world.bridge.on_event(world.event(ref, "result", {"state": "completed", "summary": "Done."}))
    await world.label(ref)
    comments = world.publisher.tasks[1]["context"]["comments"]
    assert all("exradius-github:" not in c["body"] for c in comments)
    assert world.publisher.tasks[1]["task_id"] == "plane-7-2"


async def test_another_agent_label_during_a_run_is_taken_off(world: World):
    ref = await started(world)
    await world.label(ref, "agent: Scout")
    assert len(world.publisher.tasks) == 1
    assert world.github.bodies("acme/plane", 7)[-1] == "**Plane · Developer** is still working on this."
    assert world.github.issue_labels("acme/plane", 7) == ["agent: Developer"]


async def test_labels_that_are_not_the_bridges_do_nothing(world: World):
    ref = world.issue()
    await world.bridge.labeled(ref, world.github.label_issue("acme/plane", 7, "bug"), "octocat")
    assert world.publisher.tasks == [] and world.github.comments("acme/plane", 7) == []


async def test_a_stale_agent_label_is_removed_with_a_note(world: World):
    ref = world.issue()
    world.github.add_label("acme/plane", "agent: Gone", "Exradius plane/gone")
    await world.label(ref, "agent: Gone")
    assert world.publisher.tasks == []
    assert world.github.bodies("acme/plane", 7) == ["The label `agent: Gone` no longer stands for an available agent, so it was removed."]
    assert world.github.issue_labels("acme/plane", 7) == []


async def test_a_failed_publish_says_so_and_takes_the_label_off(world: World):
    ref = world.issue()
    world.publisher.fail = True
    with pytest.raises(ConnectionError):
        await world.label(ref)
    assert world.github.bodies("acme/plane", 7) == [
        "**Plane · Developer** could not start: Exradius could not be reached. Add the label again to retry."
    ]
    assert world.state(ref).state == "ended"
    assert world.github.issue_labels("acme/plane", 7) == []


# -- questions and answers ---------------------------------------------------------------


async def test_a_question_mentions_whoever_added_the_label(world: World):
    ref = await started(world)
    await world.bridge.on_event(question_event(world, ref))
    assert world.github.bodies("acme/plane", 7)[-1] == (
        "**Plane · Developer** asks @octocat:\n\nWhich **version** broke it?\n\n_Use Quote reply on this comment to answer._"
    )
    assert (world.state(ref).state, world.state(ref).request_id) == ("waiting", "rq-1")


async def test_a_quote_reply_answers_and_plain_comments_do_not(world: World):
    ref = await started(world)
    await world.bridge.on_event(question_event(world, ref))

    plain = world.github.add_comment("acme/plane", 7, "can", "I think 2.3?")
    await world.bridge.comment_created(ref, plain)
    other_quote = world.github.add_comment("acme/plane", 7, "can", "> Estimates return 500\n\nsame here")
    await world.bridge.comment_created(ref, other_quote)
    assert world.publisher.answers == []

    reply = world.github.add_comment("acme/plane", 7, "octocat", "> Which version broke it?\n\nVersion 2.4, after the migration.")
    await world.bridge.comment_created(ref, reply)
    [answer] = world.publisher.answers
    assert answer == {"version": 1, "task_id": "plane-7", "request_id": "rq-1", "answer": "Version 2.4, after the migration.", "attachments": []}
    assert world.state(ref).state == "active"
    # The question comment's text is unchanged; only its hidden state moved on.
    assert world.github.bodies("acme/plane", 7)[1].startswith("**Plane · Developer** asks @octocat:")

    again = world.github.add_comment("acme/plane", 7, "octocat", "> Which version broke it?\n\nOr 2.5.")
    await world.bridge.comment_created(ref, again)
    assert len(world.publisher.answers) == 1


async def test_a_pasted_marker_in_someone_elses_comment_is_not_trusted(world: World):
    ref = await started(world)
    marker = world.github.comments("acme/plane", 7)[-1]["body"].split("\n\n")[-1]
    world.github.add_comment("acme/plane", 7, "mallory", "**x**\n\n" + marker.replace("active", "waiting"))
    assert world.state(ref).state == "active"


# -- results -----------------------------------------------------------------------------


async def test_a_result_inlines_text_artifacts_and_takes_the_label_off(world: World):
    ref = await started(world)
    world.artifacts["https://exradius.acme.test/a/1?sig=x"] = b"# Report\n\n```py\nx = 1\n```"
    world.artifacts["https://exradius.acme.test/a/2?sig=y"] = b"\x89PNG"
    await world.bridge.on_event(world.event(ref, "result", {
        "state": "completed",
        "summary": "Fixed the route and added a test.",
        "warnings": ["branch plane-7-fix was not pushed"],
        "artifacts": [
            {"name": "report.md", "content_type": "text/markdown", "url": "https://exradius.acme.test/a/1?sig=x"},
            {"name": "screen.png", "content_type": "image/png", "url": "https://exradius.acme.test/a/2?sig=y"},
        ],
        "transcript_url": "https://exradius.acme.test/t?sig=z",
    }))

    body = world.github.bodies("acme/plane", 7)[-1]
    assert body.startswith("**Plane · Developer** finished. @octocat\n\nFixed the route and added a test.")
    assert "- branch plane-7-fix was not pushed" in body
    assert "<details><summary>report.md</summary>\n\n````\n# Report\n\n```py\nx = 1\n```\n````\n\n</details>" in body
    assert "Not attached (GitHub cannot hold files on issues): screen.png" in body
    assert "sig=z" not in body
    assert world.github.issue_labels("acme/plane", 7) == []
    assert world.state(ref).state == "ended"


async def test_a_failure_and_a_rejection_are_reported(world: World):
    ref = await started(world)
    await world.bridge.on_event(world.event(ref, "result", {"state": "failed", "error": "the image failed to build"}))
    assert world.github.bodies("acme/plane", 7)[-1] == "**Plane · Developer** could not finish: the image failed to build. @octocat"

    second = await started(world, 8)
    await world.bridge.on_event(world.event(second, "status", {"state": "rejected", "reason": "missing secret DB_PASSWORD in store plane"}))
    assert world.github.bodies("acme/plane", 8)[-1] == "**Plane · Developer** could not start: missing secret DB_PASSWORD in store plane. @octocat"
    assert world.github.issue_labels("acme/plane", 8) == []


async def test_a_duplicate_task_id_is_retried_without_a_new_comment(world: World):
    ref = await started(world)
    await world.bridge.on_event(world.event(ref, "status", {"state": "rejected", "reason": "duplicate task_id 'plane-7'"}))
    assert [t["task_id"] for t in world.publisher.tasks] == ["plane-7", "plane-7-2"]
    assert len(world.github.comments("acme/plane", 7)) == 1
    assert decode(world.github.comments("acme/plane", 7)[0]["body"]).task_id == "plane-7-2"


async def test_events_for_another_host_or_an_old_run_are_ignored(world: World):
    ref = await started(world)
    other = world.event(ref, "result", {"state": "completed"})
    other["correlation"]["github"]["host"] = "ghe.acme.test"
    await world.bridge.on_event(other)
    await world.bridge.on_event(world.event(ref, "result", {"state": "completed"}, task_id="plane-6"))
    await world.bridge.on_event(world.event(ref, "result", {"state": "completed"}) | {"correlation": {"jira": {}}})
    assert len(world.github.comments("acme/plane", 7)) == 1


# -- directory and reconcile -------------------------------------------------------------


async def test_a_directory_notice_resyncs_the_labels(world: World):
    world.directory.directory["projects"][0]["agents"].append({"slug": "reviewer", "name": "Reviewer"})
    await world.bridge.on_event({"version": 1, "type": "directory_changed", "payload": {"projects": ["plane"]}})
    assert world.github.label_names("acme/plane")["agent: Reviewer"] == "Exradius plane/reviewer"


async def test_reconcile_starts_labels_added_while_down_and_catches_up_answers(world: World):
    missed = world.issue(9)
    world.github.label_issue("acme/plane", 9, "agent: Scout", actor="maintainer")

    waiting = await started(world, 10)
    await world.bridge.on_event(question_event(world, waiting))
    world.github.add_comment("acme/plane", 10, "octocat", "> Which version broke it?\n\n2.4")

    await world.bridge.reconcile()
    assert [t["task_id"] for t in world.publisher.tasks] == ["plane-10", "plane-9"]
    assert world.state(missed).requester == "maintainer"
    assert [a["answer"] for a in world.publisher.answers] == ["2.4"]


async def test_a_bot_comment_is_never_an_answer(world: World):
    ref = await started(world)
    await world.bridge.on_event(question_event(world, ref))
    own = world.github.add_comment("acme/plane", 7, BOT, "> Which version broke it?\n\nself")
    await world.bridge.comment_created(ref, own)
    assert world.publisher.answers == []
