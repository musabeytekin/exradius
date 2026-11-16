"""The tracker against the fake GitHub (design §4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from exradius_bridge_kit import ItemGone, Markdown, Mention, Message, Paragraph, Person, Span, TrackerProblem

from exradius_github.github import GitHub
from exradius_github.tracker import STARTING, GitHubTracker, decode_state, parse_ref, ref_for
from tests.conftest import REPO
from tests.fakes import API


async def test_start_learns_the_bot_and_refuses_a_bad_token(fake, tracker):
    assert tracker.bot == Person("exradius-bot", "exradius-bot")
    client = GitHub(API, "wrong", transport=fake.transport())
    with pytest.raises(TrackerProblem, match="GitHub refused the bot account's token .*401 Bad credentials"):
        await GitHubTracker(client, "https://github.com").start()
    await client.aclose()


def test_refs():
    ref = ref_for("Acme/Plane", 12)
    assert (ref.id, ref.key) == ("acme/plane#12", "plane-12")
    assert parse_ref(ref) == ("acme/plane", 12)


async def test_item(fake, tracker):
    key = fake.add_issue(REPO, 7, "Estimates 404", author=fake.ayse, assignees=(fake.bot,), body="Since **Monday**.")
    for n in range(3):  # more than one page
        fake.add_comment(key, fake.mehmet, f"note {n}")
    fake.add_comment(key, fake.bot, "Earlier result.\n\n<!-- exradius:e30= -->")
    item = await tracker.item(ref_for(REPO, 7))
    assert (item.ref.key, item.scope, item.title, item.body) == ("plane-7", "acme/plane", "Estimates 404", "Since **Monday**.")
    assert item.url == "https://github.com/acme/plane/issues/7"
    assert item.reporter == Person("ayse", "ayse") and item.assigned_to_bot
    assert [c.body for c in item.comments] == ["note 0", "note 1", "note 2", "Earlier result."]
    assert item.details["labels"] == ["bug"] and item.details["assignees"] == ["exradius-bot"]
    with pytest.raises(ItemGone):
        await tracker.item(ref_for(REPO, 99))


async def test_state_rides_in_the_bots_latest_comment(fake, tracker):
    key = fake.add_issue(REPO, 1, "x", author=fake.ayse, assignees=(fake.bot,))
    ref = ref_for(REPO, 1)
    assert await tracker.load_state(ref) is None
    await tracker.save_state(ref, {"state": "starting", "runs": 1})
    [first] = fake.bodies(key)
    assert first.startswith(STARTING) and "<!--" in first
    assert await tracker.load_state(ref) == {"state": "starting", "runs": 1}

    await tracker.post(ref, Message((Paragraph((Span("Scout", bold=True), Span(" is working on this."))),)))
    await tracker.save_state(ref, {"state": "active", "runs": 1})
    assert fake.bodies(key)[1].startswith("**Scout** is working on this.\n\n<!-- exradius:")
    assert decode_state(fake.bodies(key)[1]) == {"state": "active", "runs": 1}
    assert await tracker.load_state(ref) == {"state": "active", "runs": 1}
    # A person's comment after it changes nothing: only the bot's comments carry state.
    fake.add_comment(key, fake.ayse, "<!-- exradius:eyJzdGF0ZSI6ImVuZGVkIn0= -->")
    assert await tracker.load_state(ref) == {"state": "active", "runs": 1}


async def test_post_renders_mentions_and_inlines_text_files(fake, tracker):
    key = fake.add_issue(REPO, 2, "x", author=fake.ayse)
    ref = ref_for(REPO, 2)
    await tracker.attach(ref, "notes.md", "text/markdown", b"# Notes\n```sh\nmake\n```\n")
    posted = await tracker.post(ref, Message((
        Paragraph((Span("Scout", bold=True), Span(" asks "), Mention(Person("ayse", "ayse")), Span(":"))),
        Markdown("Which *branch*?"),
    )))
    assert isinstance(posted, datetime) and posted.tzinfo == UTC
    [body] = fake.bodies(key)
    assert body.startswith("**Scout** asks @ayse:\n\nWhich *branch*?\n\n<details><summary>notes.md</summary>")
    assert "````\n# Notes\n```sh\nmake\n```\n````" in body
    await tracker.post(ref, Message((Paragraph((Span("again"),)),)))
    assert fake.bodies(key)[1] == "again"


async def test_hand_back_to_the_author(fake, tracker):
    key = fake.add_issue(REPO, 3, "x", author=fake.ayse, assignees=(fake.bot,))
    await tracker.hand_back(ref_for(REPO, 3))
    assert fake.assignees(key) == ["ayse"]
    # Someone else took it meanwhile: left alone.
    fake.assign(key, fake.mehmet)
    await tracker.hand_back(ref_for(REPO, 3))
    assert fake.assignees(key) == ["mehmet"]
    # An author who cannot be assigned: the issue ends up unassigned.
    other = fake.add_issue(REPO, 4, "x", author=fake.outsider, assignees=(fake.bot,))
    await tracker.hand_back(ref_for(REPO, 4))
    assert fake.assignees(other) == []


async def test_assigned_to_bot_leaves_out_pull_requests_and_others(fake, tracker):
    for n in range(1, 4):
        fake.add_issue(REPO, n, "x", author=fake.ayse, assignees=(fake.bot,))
    fake.add_issue(REPO, 4, "x", author=fake.ayse, assignees=(fake.mehmet,))
    fake.add_issue(REPO, 5, "pr", author=fake.ayse, assignees=(fake.bot,), pull_request=True)
    assert [r.key for r in await tracker.assigned_to_bot()] == ["plane-1", "plane-2", "plane-3"]


async def test_replies_need_a_mention_and_drop_it(fake, tracker):
    key = fake.add_issue(REPO, 6, "x", author=fake.ayse, assignees=(fake.bot,))
    fake.add_comment(key, fake.ayse, "@exradius-bot too early")
    asked = await tracker.post(ref_for(REPO, 6), Message((Paragraph((Span("Which branch?"),)),)))
    fake.add_comment(key, fake.mehmet, "no mention here")
    fake.add_comment(key, fake.mehmet, "mail me at x@exradius-bot.dev, or @exradius-bots")
    fake.add_comment(key, fake.ayse, "> Which branch?\n\n@Exradius-Bot  the preview one")
    fake.add_comment(key, fake.bot, "@exradius-bot talking to myself")
    replies = await tracker.replies(ref_for(REPO, 6), since=asked)
    assert [(r.author, r.body) for r in replies] == [(Person("ayse", "ayse"), "the preview one")]


async def test_other_files_go_to_the_artifacts_branch(fake, tracker):
    key = fake.add_issue(REPO, 8, "x", author=fake.ayse)
    ref = ref_for(REPO, 8)
    await tracker.attach(ref, "admin orders.png", "image/png", b"\x89PNG\x00one")
    await tracker.attach(ref, "report.pdf", "application/pdf", b"%PDF\x00")
    await tracker.post(ref, Message((Paragraph((Span("done"),)),)))
    files = fake.files(REPO, "exradius-artifacts")
    assert files == {"README.md": files["README.md"], "plane-8/admin orders.png": b"\x89PNG\x00one",
                     "plane-8/report.pdf": b"%PDF\x00"}
    assert b"can be deleted" in files["README.md"]
    first = next(c for c in fake.commits.values() if not c["parents"])  # an orphan: no history of the code
    assert first["message"] == "plane-8: admin orders.png"
    head = fake.refs[(REPO, "exradius-artifacts")]
    [body] = fake.bodies(key)
    assert f"![admin orders.png](https://github.com/acme/plane/blob/" in body
    assert "/plane-8/admin%20orders.png?raw=true)" in body
    assert f"[Open](https://github.com/acme/plane/blob/{head}/plane-8/report.pdf?raw=true)" in body

    # A later run's file of the same name: a new commit, and the old link still points at the old one.
    fake.race = 1  # someone else moves the branch first: the bridge goes again on top of it
    await tracker.attach(ref, "admin orders.png", "image/png", b"\x89PNG\x00two")
    await tracker.post(ref, Message((Paragraph((Span("again"),)),)))
    assert fake.files(REPO, "exradius-artifacts")["plane-8/admin orders.png"] == b"\x89PNG\x00two"
    assert fake.commits[fake.refs[(REPO, "exradius-artifacts")]]["message"] == "plane-8: admin orders.png"
    assert fake.bodies(key)[1].count("![admin orders.png]") == 1


async def test_a_long_text_file_goes_to_the_branch_and_a_refusal_is_raised(fake, tracker):
    fake.add_issue(REPO, 9, "x", author=fake.ayse)
    ref = ref_for(REPO, 9)
    await tracker.attach(ref, "big.log", "text/plain", b"x" * 60_000)
    assert "plane-9/big.log" in fake.files(REPO, "exradius-artifacts")
    fake.collaborators[REPO].discard(fake.bot)  # the bot lost write access
    with pytest.raises(Exception, match="404"):
        await tracker.attach(ref, "shot.png", "image/png", b"\x89PNG\x00")


def test_uploads_are_found_named_and_signed():
    from exradius_github import uploads

    u1 = "https://github.com/user-attachments/assets/0000000a-1111-4222-8333-00000000000b"
    u2 = "https://github.com/user-attachments/assets/0000000c-1111-4222-8333-00000000000d"
    body = (f"Broken:\n\n![Image]({u1})\n\n<img width=\"300\" alt=\"orders page\" src=\"{u2}\" />\n\n"
            "[server.log](https://github.com/user-attachments/files/123/server.log) and "
            f"again ![Image]({u1}), [docs](https://example.com/x.png), "
            "![old](https://user-images.githubusercontent.com/1/abc.jpg)")
    html = ("<p><img src=\"https://private-user-images.githubusercontent.com/1/9-0000000a-1111-4222-8333-00000000000b.png"
            "?jwt=abc&amp;s=1\"></p>")
    used, counter = {"image-1.png"}, [0]
    text, found = uploads.find(body, html, None, "github.com", used, counter)
    assert text == ("Broken:\n\n[attachment: image-2.png]\n\n[attachment: orders page.png]\n\n"
                    "[attachment: server.log] and again [attachment: image-2.png], [docs](https://example.com/x.png), "
                    "[attachment: old.jpg]")
    assert [(f.info.name, f.info.content_type, f.info.id) for f in found] == [
        ("image-2.png", "image/png",
         "https://private-user-images.githubusercontent.com/1/9-0000000a-1111-4222-8333-00000000000b.png?jwt=abc&s=1"),
        ("server.log", "text/plain", "https://github.com/user-attachments/files/123/server.log"),
        ("old.jpg", "image/jpeg", "https://user-images.githubusercontent.com/1/abc.jpg"),
        # Resized images are <img> tags, found after the Markdown links; with no signed
        # address in the rendered body, the link itself is tried.
        ("orders page.png", "image/png", u2),
    ]


async def test_item_passes_uploads_and_downloads_signed_ones(fake, tracker):
    image = fake.upload_image(b"\x89PNG body")
    key = fake.add_issue(REPO, 11, "Broken page", author=fake.ayse,
                         body=f"See ![Image]({image}) and [crash.zip](https://github.com/user-attachments/files/7/crash.zip)")
    shot = fake.upload_image(b"\x89PNG comment")
    fake.add_comment(key, fake.mehmet, f"Same here: ![Image]({shot})")
    item = await tracker.item(ref_for(REPO, 11))
    assert item.body == "See [attachment: image-1.png] and [attachment: crash.zip]"
    assert item.comments[0].body == "Same here: [attachment: image-2.png]"
    names = [a.name for a in item.attachments]
    assert names == ["image-1.png", "crash.zip", "image-2.png"]
    assert await tracker.download(item.attachments[0]) == b"\x89PNG body"
    assert await tracker.download(item.attachments[2]) == b"\x89PNG comment"
    with pytest.raises(Exception, match="a page, not the file"):
        await tracker.download(item.attachments[1])


async def test_a_reply_brings_its_uploads(fake, tracker):
    key = fake.add_issue(REPO, 12, "x", author=fake.ayse, assignees=(fake.bot,))
    asked = await tracker.post(ref_for(REPO, 12), Message((Paragraph((Span("Which page?"),)),)))
    shot = fake.upload_image(b"\x89PNG answer")
    fake.add_comment(key, fake.ayse, f"@exradius-bot this one: ![Image]({shot})")
    [reply] = await tracker.replies(ref_for(REPO, 12), since=asked)
    assert reply.body == "this one: [attachment: image-1.png]"
    [info] = reply.attachments
    assert await tracker.download(info) == b"\x89PNG answer"
