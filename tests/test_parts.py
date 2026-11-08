"""Repository URLs, the state marker and Quote reply detection."""

from __future__ import annotations

import pytest

from exradius_github.replies import quotes, without_quotes
from exradius_github.repos import repo_key
from exradius_github.state import IssueState, decode, encode, latest, strip, task_id_for, with_state


@pytest.mark.parametrize(
    "url, key",
    [
        ("https://github.com/acme/Plane.git", "acme/plane"),
        ("https://github.com/acme/plane", "acme/plane"),
        ("https://github.com/acme/plane/", "acme/plane"),
        ("git@github.com:acme/plane.git", "acme/plane"),
        ("ssh://git@github.com/acme/plane.git", "acme/plane"),
        ("ssh://git@github.com:22/acme/plane", "acme/plane"),
        ("https://gitlab.com/acme/plane.git", None),
        ("https://github.com/acme", None),
        ("https://github.com/acme/plane/tree/main", None),
    ],
)
def test_repo_key(url, key):
    assert repo_key(url, "github.com") == key


def test_task_ids():
    assert task_id_for("acme/plane", 7, 1) == "plane-7"
    assert task_id_for("acme/plane", 7, 3) == "plane-7-3"


def test_the_marker_round_trips_and_is_replaced_not_stacked():
    state = IssueState(runs=1, task_id="plane-7", state="active", label="Plane · Developer")
    body = with_state("**Plane · Developer** is working on this.", state)
    assert decode(body) == state
    assert "-->" in body and "plane-7" not in body  # base64, nothing readable
    state.state = "waiting"
    again = with_state(body, state)
    assert again.count("exradius-github:") == 1 and decode(again).state == "waiting"
    assert strip(again).strip() == "**Plane · Developer** is working on this."
    assert decode("no marker") is None and decode("<!-- exradius-github:!!! -->") is None


def test_latest_reads_only_the_bots_comments():
    marker = encode(IssueState(task_id="x", state="active"))
    comments = [
        {"user": {"login": "bot[bot]"}, "body": f"start\n\n{marker}"},
        {"user": {"login": "mallory"}, "body": encode(IssueState(task_id="y", state="waiting"))},
        {"user": {"login": "bot[bot]"}, "body": "a comment without state"},
    ]
    state, comment = latest(comments, "bot[bot]")
    assert state.task_id == "x" and comment is comments[0]
    assert latest([], "bot[bot]")[0] == IssueState()


QUESTION = "**Plane · Developer** asks @octocat:\n\nWhich *version* broke the `estimates` route?\n\n_Use Quote reply on this comment to answer._"


@pytest.mark.parametrize(
    "reply, answers",
    [
        ("> Which version broke the estimates route?\n\n2.4", True),
        ("> **Plane · Developer** asks @octocat:\n> \n> Which version broke the estimates route?\n\n2.4", True),
        (">   which VERSION   broke\n\n2.4", True),  # part of a line, spacing and case
        (">> Which version broke the estimates route?\n\nnested", True),
        ("> Which\n\ntoo short", False),
        ("> Something else entirely\n\nno", False),
        ("Which version broke the estimates route? 2.4", False),  # not a quote
    ],
)
def test_quote_replies(reply, answers):
    assert quotes(reply, QUESTION) is answers


def test_the_answer_is_the_reply_without_its_quotes():
    assert without_quotes("> Which version?\n>\n\nVersion 2.4.\n\n> again\nThanks") == "Version 2.4.\n\nThanks"
