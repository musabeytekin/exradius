"""The whole flow (design §6, §7) against the fakes."""

from __future__ import annotations

import base64

import pytest

from exradius_bridge_kit.testing import (
    BOT,
    REPORTER,
    FakeBroker,
    FakeExradius,
    FakeTracker,
    event,
    make_kit,
    project_entry,
)


@pytest.fixture
def tracker():
    return FakeTracker()


@pytest.fixture
def exradius():
    return FakeExradius(projects=[project_entry("plane", bridge_config={"scopes": ["PLANE"]})])


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
async def kit(tracker, exradius, broker):
    kit = make_kit(tracker, exradius, broker)
    await kit.start()
    await broker.consume(kit.on_event)
    yield kit
    await kit.close()


async def hand_over(kit, ref):
    await kit.handed_over(ref)
    await kit.idle()


def queued(ref, task_id, agent="scout", routing=None):
    return event("status", ref, task_id, {"state": "queued", "previous": None, "reason": None,
                                          "routing": routing or {"method": "named"}}, agent=agent)


# Starting (§6.1) ------------------------------------------------------------------------------------


async def test_a_hand_over_publishes_a_task(kit, tracker, broker):
    ref = tracker.add("PLANE-101", details={"type": "Bug", "labels": ["api"], "priority": None})
    tracker.comment(ref, "It started on Monday.", to_bot=False)
    tracker.attach_file(ref, "trace.txt", b"Traceback")
    tracker.attach_file(ref, "trace.txt", b"again")
    await hand_over(kit, ref)

    [task] = broker.tasks
    assert task["task_id"] == "PLANE-101" and task["project"] == "plane"
    assert "agent" not in task  # Exradius routes
    assert task["instructions"] == "Fix the estimate route\n\nThe estimate endpoint returns 404."
    assert task["correlation"] == {"bridge": "fake", "instance": "https://tracker.test",
                                   "item": {"id": ref.id, "key": "PLANE-101"}}
    assert task["context"]["item"] == {"key": "PLANE-101", "url": "https://tracker.test/browse/PLANE-101",
                                       "reporter": "Ayşe Yılmaz", "type": "Bug", "labels": ["api"]}
    assert task["context"]["comments"] == [
        {"author": "Ayşe Yılmaz", "created": "2026-11-08T10:00:00Z", "body": "It started on Monday."}]
    assert [(a["name"], base64.b64decode(a["content_base64"])) for a in task["attachments"]] == [
        ("trace.txt", b"Traceback"), ("trace (2).txt", b"again")]
    state = tracker.items[ref.id].state
    assert (state["state"], state["runs"], state["task_id"], state["project"]) == ("starting", 1, "PLANE-101", "plane")
    assert tracker.texts(ref) == []  # the start comment waits for the queued event


async def test_the_start_comment_names_the_agent(kit, tracker, broker):
    ref = tracker.add("PLANE-1")
    await hand_over(kit, ref)
    await broker.deliver(queued(ref, "PLANE-1", routing={"method": "decided", "confidence": 0.91, "guess": False,
                                                         "probabilities": {"scout": 0.92, "developer": 0.08}}))
    assert tracker.texts(ref) == ["**Scout** is working on this."]
    state = tracker.items[ref.id].state
    assert (state["state"], state["agent"], state["label"]) == ("active", "scout", "Scout")


async def test_a_guess_says_so(kit, tracker, broker):
    ref = tracker.add("PLANE-2")
    await hand_over(kit, ref)
    await broker.deliver(queued(ref, "PLANE-2", agent="developer", routing={
        "method": "decided", "confidence": 0.41, "guess": True,
        "probabilities": {"scout": 0.41, "developer": 0.5, "reviewer": 0.06, "writer": 0.03}}))
    assert tracker.texts(ref) == [
        "**Developer** is working on this. This was a guess (Developer 50%, Scout 41%, reviewer 6%)."]


async def test_unclaimed_and_twice_claimed_scopes_hand_back(tracker, broker):
    exradius = FakeExradius(projects=[
        project_entry("bank", bridge_config={"scopes": ["SHARED"]}),
        project_entry("plane", bridge_config={"scopes": ["PLANE", "SHARED"]}),
    ])
    kit = make_kit(tracker, exradius, broker)
    await kit.start()
    lost = tracker.add("OTHER-1")
    shared = tracker.add("SHARED-1")
    await hand_over(kit, lost)
    await hand_over(kit, shared)
    assert broker.tasks == []
    assert tracker.texts(lost) == ["No Exradius project takes work from OTHER."]
    assert tracker.texts(shared) == ["SHARED is claimed by bank and plane; their bridges/fake.yaml files need fixing."]
    assert tracker.items[lost.id].handed_back == tracker.items[shared.id].handed_back == 1
    assert {p["message"] for p in exradius.reports[-1]["problems"]} == {
        "bridges/fake.yaml: SHARED is also claimed by plane", "bridges/fake.yaml: SHARED is also claimed by bank"}
    await kit.close()


async def test_an_active_run_says_still_working_once(kit, tracker, broker):
    ref = tracker.add("PLANE-3")
    await hand_over(kit, ref)
    await hand_over(kit, ref)  # before queued: no agent yet
    await broker.deliver(queued(ref, "PLANE-3"))
    await hand_over(kit, ref)
    assert len(broker.tasks) == 1
    assert tracker.texts(ref) == ["This is still being worked on.", "**Scout** is working on this."]


async def test_an_item_not_with_the_bot_is_ignored(kit, tracker, broker):
    ref = tracker.add("PLANE-4", assigned=False)
    await hand_over(kit, ref)
    assert broker.tasks == [] and tracker.texts(ref) == []


async def test_a_failed_publish_restores_the_state(kit, tracker, broker, exradius):
    ref = tracker.add("PLANE-5")
    broker.down = True
    await hand_over(kit, ref)
    assert tracker.items[ref.id].state is None or tracker.items[ref.id].state.get("runs", 0) == 0
    assert any("PLANE-5: the hand-over failed: broker down" == p["message"] for p in exradius.reports[-1]["problems"])
    broker.down = False
    await hand_over(kit, ref)
    assert [t["task_id"] for t in broker.tasks] == ["PLANE-5"]


async def test_attachments_past_the_cap_are_listed(tracker, broker, exradius):
    from dataclasses import replace

    from exradius_bridge_kit.testing import SETTINGS

    kit = make_kit(tracker, exradius, broker, settings=replace(SETTINGS, max_attachment_mb=0.001))  # ~1 KiB
    await kit.start()
    ref = tracker.add("PLANE-6")
    tracker.attach_file(ref, "small.txt", b"x" * 600)
    tracker.attach_file(ref, "big.bin", b"y" * 900)
    tracker.attach_file(ref, "tiny/..txt", b"z" * 10)
    await hand_over(kit, ref)
    [task] = broker.tasks
    assert [a["name"] for a in task["attachments"]] == ["small.txt", "tiny_..txt"]
    assert task["context"]["skipped_attachments"] == [{"name": "big.bin", "size": 900}]
    await kit.close()


# Duplicates and rejections (§6.3, §7) ---------------------------------------------------------------


async def test_a_duplicate_id_takes_the_next(kit, tracker, broker):
    ref = tracker.add("PLANE-7")
    await hand_over(kit, ref)
    rejected = {"state": "rejected", "previous": None, "reason": "duplicate task_id 'PLANE-7'"}
    await broker.deliver(event("status", ref, "PLANE-7", rejected))
    assert [t["task_id"] for t in broker.tasks] == ["PLANE-7", "PLANE-7-2"]
    assert tracker.items[ref.id].state["retries"] == 1
    assert tracker.texts(ref) == []


async def test_duplicates_give_up_after_five(kit, tracker, broker):
    ref = tracker.add("PLANE-8")
    await hand_over(kit, ref)
    for _ in range(6):
        task_id = broker.tasks[-1]["task_id"]
        await broker.deliver(event("status", ref, task_id, {"state": "rejected", "reason": f"duplicate task_id '{task_id}'"}))
    assert len(broker.tasks) == 6
    assert tracker.texts(ref)[-1].startswith("This could not start: duplicate task_id")
    assert tracker.items[ref.id].state["state"] == "ended"


async def test_a_rejection_is_posted_and_hands_back(kit, tracker, broker):
    ref = tracker.add("PLANE-9")
    await hand_over(kit, ref)
    reason = "agent routing failed: OpenAI answered 503 three times"
    await broker.deliver(event("status", ref, "PLANE-9", {"state": "rejected", "reason": reason}))
    assert tracker.texts(ref) == [f"This could not start: {reason}"]
    assert tracker.items[ref.id].handed_back == 1 and tracker.items[ref.id].state["state"] == "ended"
    await hand_over(kit, ref)  # handed back to the reporter, so nothing
    tracker.items[ref.id].assignee = BOT
    await hand_over(kit, ref)
    assert broker.tasks[-1]["task_id"] == "PLANE-9-2"


# Questions and answers (§6.2) -------------------------------------------------------------------------


async def test_a_question_and_its_answer(kit, tracker, broker):
    ref = tracker.add("PLANE-10")
    await hand_over(kit, ref)
    await broker.deliver(queued(ref, "PLANE-10"))
    tracker.comment(ref, "chatter before the question")
    await broker.deliver(event("info_request", ref, "PLANE-10", {
        "request_id": "q-1", "question": "Which **branch**?", "context": "I see two."}, agent="scout"))
    assert tracker.texts(ref)[-1] == ("**Scout** asks @Ayşe Yılmaz:\n\nWhich **branch**?\n\nI see two.\n\n"
                                      "*Mention me in your reply to answer.*")
    state = tracker.items[ref.id].state
    assert state["state"] == "waiting" and state["request_id"] == "q-1"

    tracker.comment(ref, "just talking", to_bot=False)
    await kit.commented(ref)
    await kit.idle()
    assert broker.answers == []
    tracker.attach_file(ref, "log.txt", b"logs")
    tracker.comment(ref, "Use `preview`.")
    tracker.comment(ref, "Or main.")
    await kit.commented(ref)
    await kit.idle()
    [answer] = broker.answers
    assert answer["task_id"] == "PLANE-10" and answer["request_id"] == "q-1" and answer["answer"] == "Use `preview`."
    assert [a["name"] for a in answer["attachments"]] == ["log.txt"]
    assert tracker.items[ref.id].state["state"] == "active"
    await kit.commented(ref)
    await kit.idle()
    assert len(broker.answers) == 1  # nothing waits any more


async def test_a_reply_by_the_bot_does_not_answer(kit, tracker, broker):
    ref = tracker.add("PLANE-11")
    await hand_over(kit, ref)
    await broker.deliver(queued(ref, "PLANE-11"))
    await broker.deliver(event("info_request", ref, "PLANE-11", {"request_id": "q", "question": "?"}, agent="scout"))
    tracker.comment(ref, "talking to myself", author=BOT)
    await kit.commented(ref)
    await kit.idle()
    assert broker.answers == []


async def test_a_question_without_a_reporter(kit, tracker, broker):
    ref = tracker.add("PLANE-12", reporter=None)
    await hand_over(kit, ref)
    await broker.deliver(queued(ref, "PLANE-12"))
    await broker.deliver(event("info_request", ref, "PLANE-12", {"request_id": "q", "question": "Which one?"}))
    assert tracker.texts(ref)[-1].startswith("**Scout** asks:\n\nWhich one?")


# Results (§6.3) -----------------------------------------------------------------------------------------


async def test_a_completed_result_with_artifacts(kit, tracker, broker, exradius):
    ref = tracker.add("PLANE-13")
    await hand_over(kit, ref)
    await broker.deliver(queued(ref, "PLANE-13"))
    exradius.artifacts["report.docx"] = b"PK docx"
    payload = {"state": "completed", "summary": "Found it: `estimates` is not registered.",
               "warnings": ["branch PLANE-13 was not pushed"],
               "artifacts": [{"name": "report.docx", "content_type": "application/vnd.openxmlformats", "url": exradius.artifact_url("report.docx")},
                             {"name": "gone.txt", "content_type": "text/plain", "url": exradius.artifact_url("gone.txt")}]}
    await broker.deliver(event("result", ref, "PLANE-13", payload, agent="scout"))
    assert tracker.texts(ref)[-1] == ("**Scout** finished.\n\nFound it: `estimates` is not registered.\n\n**Warnings**\n\n"
                                      "- branch PLANE-13 was not pushed\n\nAttached: report.docx\n\nCould not attach: gone.txt")
    assert tracker.items[ref.id].attached == [("report.docx", "application/vnd.openxmlformats", b"PK docx")]
    assert tracker.items[ref.id].handed_back == 1 and tracker.items[ref.id].assignee == REPORTER
    assert tracker.items[ref.id].state["state"] == "ended"


@pytest.mark.parametrize(("payload", "text"), [
    ({"state": "failed", "error": "the runner crashed", "summary": "Half done."},
     "**Scout** could not finish: the runner crashed\n\nHalf done."),
    ({"state": "cancelled"}, "**Scout** was stopped."),
])
async def test_other_results(kit, tracker, broker, payload, text):
    ref = tracker.add("PLANE-14")
    await hand_over(kit, ref)
    await broker.deliver(queued(ref, "PLANE-14"))
    await broker.deliver(event("result", ref, "PLANE-14", payload, agent="scout"))
    assert tracker.texts(ref)[-1] == text


async def test_events_for_other_runs_and_bridges_are_ignored(kit, tracker, broker):
    ref = tracker.add("PLANE-15")
    await hand_over(kit, ref)
    await broker.deliver(queued(ref, "PLANE-15-old"))
    await broker.deliver(event("result", ref, "PLANE-15", {"state": "completed"}, bridge="jira"))
    await broker.deliver(event("result", ref, "PLANE-15", {"state": "completed"}, instance="https://other.test"))
    await broker.deliver({"type": "result", "task_id": "PLANE-15", "correlation": {"ticket": 1}, "payload": {}})
    assert tracker.texts(ref) == [] and tracker.items[ref.id].state["state"] == "starting"


async def test_a_missed_queued_event_still_names_the_agent(kit, tracker, broker):
    ref = tracker.add("PLANE-16")
    await hand_over(kit, ref)
    await broker.deliver(event("status", ref, "PLANE-16", {"state": "running", "previous": "preparing"}, agent="developer"))
    await broker.deliver(event("result", ref, "PLANE-16", {"state": "completed"}, agent="developer"))
    assert tracker.texts(ref) == ["**Developer** finished."]


# The startup reconcile (§10) ---------------------------------------------------------------------------


async def test_reconcile_starts_missed_hand_overs_and_answers(tracker, exradius, broker):
    fresh = tracker.add("PLANE-20")
    waiting = tracker.add("PLANE-21")
    tracker.items[waiting.id].state = {"runs": 1, "task_id": "PLANE-21", "project": "plane", "agent": "scout",
                                       "label": "Scout", "state": "waiting", "request_id": "q-9",
                                       "asked_at": "2026-11-08T09:00:00Z"}
    tracker.comment(waiting, "Here you go.")
    active = tracker.add("PLANE-22")
    tracker.items[active.id].state = {"runs": 1, "task_id": "PLANE-22", "project": "plane", "state": "active"}
    kit = make_kit(tracker, exradius, broker)
    await kit.start()
    assert [t["task_id"] for t in broker.tasks] == ["PLANE-20"]
    assert [(a["task_id"], a["answer"]) for a in broker.answers] == [("PLANE-21", "Here you go.")]
    assert tracker.texts(active) == []
    await kit.close()


async def test_a_v0_state_continues_the_run_count(kit, tracker, broker):
    ref = tracker.add("PLANE-1")
    tracker.items[ref.id].state = {"runs": 2, "task_id": "PLANE-1-2", "project": "plane", "agent": "scout",
                                   "label": "Plane · Scout", "state": "ended", "prompted_for_agent": False}
    await hand_over(kit, ref)
    assert broker.tasks[-1]["task_id"] == "PLANE-1-3"
    assert tracker.items[ref.id].state["prompted_for_agent"] is False  # unknown fields are kept
