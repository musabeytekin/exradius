"""The kit's tracker over the fake Jira (design §4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from exradius_bridge_kit import ItemGone, ItemRef, Markdown, Mention, Message, Paragraph, Person, Span, TrackerProblem

from exradius_jira import adf
from exradius_jira.jira import Jira
from exradius_jira.tracker import JiraTracker
from tests.fakes import SITE


def ref_of(fake, issue_id: str) -> ItemRef:
    return ItemRef(id=issue_id, key=fake.issues[issue_id]["key"])


async def test_a_bad_token_is_a_tracker_problem(fake):
    client = Jira(SITE, "nobody@acme.test", "token", transport=fake.transport())
    with pytest.raises(TrackerProblem, match="Jira refused the bot account"):
        await JiraTracker(client).start()
    await client.aclose()


async def test_reading_an_issue(fake, tracker):
    issue_id = fake.add_issue(
        "PLANE-101", "Estimates 404", reporter=fake.ayse, assignee=fake.bot,
        description=adf.doc(adf.paragraph(adf.text("The "), adf.strong("estimate"), adf.text(" route is missing."))),
        labels=["api"], parent={"key": "PLANE-80", "fields": {"summary": "Estimates"}},
    )
    fake.add_comment(issue_id, fake.mehmet, adf.doc(adf.paragraph(adf.text("Since Monday."))))
    fake.add_attachment(issue_id, "trace.txt", b"Traceback")
    item = await tracker.item(ref_of(fake, issue_id))
    assert (item.ref.key, item.scope, item.title) == ("PLANE-101", "PLANE", "Estimates 404")
    assert item.body == "The **estimate** route is missing."
    assert item.url == f"{SITE}/browse/PLANE-101" and item.assigned_to_bot is True
    assert item.reporter == Person(fake.ayse.account_id, "Ayşe Yılmaz")
    assert item.details["type"] == "Bug" and item.details["labels"] == ["api"]
    assert item.details["parent"] == {"key": "PLANE-80", "summary": "Estimates"}
    assert [(c.author, c.body) for c in item.comments] == [("Mehmet Kaya", "Since Monday.")]
    [attachment] = item.attachments
    assert (attachment.name, attachment.size, attachment.content_type) == ("trace.txt", 9, "text/plain")
    assert await tracker.download(attachment) == b"Traceback"
    with pytest.raises(ItemGone):
        await tracker.item(ItemRef(id="99999", key="PLANE-999"))


async def test_the_bot_as_reporter_is_no_reporter(fake, tracker):
    issue_id = fake.add_issue("PLANE-1", "x", reporter=fake.bot, assignee=fake.bot)
    assert (await tracker.item(ref_of(fake, issue_id))).reporter is None


async def test_posting_renders_adf_with_a_mention(fake, tracker):
    issue_id = fake.add_issue("PLANE-2", "x", reporter=fake.ayse)
    message = Message((
        Paragraph((Span("Scout", bold=True), Span(" asks "), Mention(Person(fake.ayse.account_id, "Ayşe")), Span(":"))),
        Markdown("Which `branch`?"),
        Paragraph((Span("Mention me in your reply to answer.", italic=True),)),
    ))
    created = await tracker.post(ref_of(fake, issue_id), message)
    assert isinstance(created, datetime) and created.tzinfo is not None
    [comment] = fake.comments[issue_id]
    head, question, footer = comment["body"]["content"]
    assert head["content"][0] == {"type": "text", "text": "Scout", "marks": [{"type": "strong"}]}
    assert head["content"][2] == {"type": "mention", "attrs": {"id": fake.ayse.account_id, "text": "@Ayşe"}}
    assert question["content"][1] == {"type": "text", "text": "branch", "marks": [{"type": "code"}]}
    assert footer["content"][0]["marks"] == [{"type": "em"}]


async def test_hand_back(fake, tracker):
    to_reporter = fake.add_issue("PLANE-3", "x", reporter=fake.ayse, assignee=fake.bot)
    taken = fake.add_issue("PLANE-4", "x", reporter=fake.ayse, assignee=fake.mehmet)
    unassignable = fake.add_issue("PLANE-5", "x", reporter=fake.mehmet, assignee=fake.bot)
    fake.unassignable.add(fake.mehmet.account_id)
    for issue_id in (to_reporter, taken, unassignable):
        await tracker.hand_back(ref_of(fake, issue_id))
    assert fake.assignee(to_reporter) == fake.ayse.account_id
    assert fake.assignee(taken) == fake.mehmet.account_id  # someone took it meanwhile
    assert fake.assignee(unassignable) is None


async def test_replies_need_a_mention_and_come_after_the_question(fake, tracker):
    issue_id = fake.add_issue("PLANE-6", "x", reporter=fake.ayse, assignee=fake.bot)
    ref = ref_of(fake, issue_id)
    mention = adf.mention(fake.bot.account_id, "AI Agent")
    fake.add_comment(issue_id, fake.ayse, adf.doc(adf.paragraph(mention, adf.text(" too early"))))
    asked = await tracker.post(ref, Message((Paragraph((Span("Which branch?"),)),)))
    fake.add_comment(issue_id, fake.ayse, adf.doc(adf.paragraph(adf.text("no mention"))))
    fake.add_comment(issue_id, fake.bot, adf.doc(adf.paragraph(mention, adf.text(" the bot itself"))))
    fake.add_attachment(issue_id, "log.txt", b"logs")
    fake.add_comment(issue_id, fake.mehmet, adf.doc(adf.paragraph(mention, adf.text(" use "), adf.text("preview", {"type": "code"}))))
    [reply] = await tracker.replies(ref, asked)
    assert reply.author == Person(fake.mehmet.account_id, "Mehmet Kaya")
    assert reply.body == "use `preview`"
    assert [a.name for a in reply.attachments] == ["log.txt"]
    assert len(await tracker.replies(ref, None)) == 2


async def test_state_lives_in_the_issue_property_and_v0_states_read(fake, tracker):
    issue_id = fake.add_issue("PLANE-7", "x", reporter=fake.ayse, assignee=fake.bot)
    ref = ref_of(fake, issue_id)
    assert await tracker.load_state(ref) is None
    fake.properties[(issue_id, "exradius")] = {"runs": 2, "task_id": "PLANE-7-2", "state": "ended",
                                               "label": "Plane · Scout", "prompted_for_agent": False}
    assert (await tracker.load_state(ref))["runs"] == 2
    await tracker.save_state(ref, {"runs": 3})
    assert fake.properties[(issue_id, "exradius")] == {"runs": 3}


async def test_assigned_to_bot(fake, tracker):
    mine = fake.add_issue("PLANE-8", "x", reporter=fake.ayse, assignee=fake.bot)
    fake.add_issue("PLANE-9", "x", reporter=fake.ayse, assignee=fake.ayse)
    assert await tracker.assigned_to_bot() == [ItemRef(id=mine, key="PLANE-8")]


def test_parse_time_keeps_the_offset():
    from exradius_jira.jira import parse_time

    assert parse_time("2026-11-08T10:02:41.123+0000") == datetime(2026, 11, 8, 10, 2, 41, 123000, tzinfo=UTC)
