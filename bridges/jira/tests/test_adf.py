"""ADF to Markdown and back."""

from __future__ import annotations

from exradius_jira import adf


def t(value, *marks):
    return adf.text(value, *({"type": m} if isinstance(m, str) else m for m in marks))


def test_a_jira_description_reads_as_markdown():
    doc = adf.doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [t("Steps")]},
        {"type": "orderedList", "content": [
            {"type": "listItem", "content": [adf.paragraph(t("Open "), t("Estimates", "strong"))]},
            {"type": "listItem", "content": [
                adf.paragraph(t("Click ")),
                {"type": "bulletList", "content": [{"type": "listItem", "content": [adf.paragraph(t("Save", "code"))]}]},
            ]},
        ]},
        adf.paragraph(t("see "), t("the docs", {"type": "link", "attrs": {"href": "https://x.test"}}), {"type": "hardBreak"}, t("thanks")),
        {"type": "codeBlock", "attrs": {"language": "python"}, "content": [t("raise ValueError")]},
        {"type": "panel", "attrs": {"panelType": "info"}, "content": [adf.paragraph(t("Only on staging"))]},
        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"type": "file", "id": "abc", "alt": "screen.png"}}]},
        {"type": "table", "content": [
            {"type": "tableRow", "content": [{"type": "tableHeader", "content": [adf.paragraph(t("a"))]}, {"type": "tableHeader", "content": [adf.paragraph(t("b|c"))]}]},
            {"type": "tableRow", "content": [{"type": "tableCell", "content": [adf.paragraph(t("1"))]}, {"type": "tableCell", "content": [adf.paragraph(t("2"))]}]},
        ]},
    )
    assert adf.to_markdown(doc) == (
        "## Steps\n\n"
        "1. Open **Estimates**\n"
        "2. Click \n"
        "   - `Save`\n\n"
        "see [the docs](https://x.test)  \nthanks\n\n"
        "```python\nraise ValueError\n```\n\n"
        "> Only on staging\n\n"
        "[attachment: screen.png]\n\n"
        "| a | b\\|c |\n| --- | --- |\n| 1 | 2 |"
    )


def test_marks_keep_spaces_outside_and_mentions_can_be_dropped():
    doc = adf.doc(adf.paragraph(adf.mention("bot", "AI Agent"), t(" it is "), t("bold ", "strong"), t("now")))
    assert adf.to_markdown(doc) == "@AI Agent it is **bold** now"
    assert adf.to_markdown(doc, skip_mention="bot") == "it is **bold** now"
    assert adf.mentions(doc, "bot") and not adf.mentions(doc, "someone")


def test_unknown_and_empty_input():
    assert adf.to_markdown(None) == ""
    assert adf.to_markdown(adf.doc({"type": "mystery", "content": [t("still here")]})) == "still here"
    assert adf.from_markdown("") == [] and adf.from_markdown("   \n") == []


def test_markdown_becomes_valid_adf():
    blocks = adf.from_markdown(
        "# Done\n\n"
        "Fixed `route` in [api](https://x.test) and **really *it***.\n\n"
        "- - nested first\n"
        "- second\n\n"
        "> ## quoted heading\n> > deeper\n\n"
        "---\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    kinds = [b["type"] for b in blocks]
    assert kinds == ["heading", "paragraph", "bulletList", "blockquote", "rule", "table"]

    inline = blocks[1]["content"]
    assert {"type": "text", "text": "route", "marks": [{"type": "code"}]} in inline
    assert {"type": "text", "text": "api", "marks": [{"type": "link", "attrs": {"href": "https://x.test"}}]} in inline
    assert {"type": "text", "text": "it", "marks": [{"type": "strong"}, {"type": "em"}]} in inline

    first_item = blocks[2]["content"][0]["content"]
    assert first_item[0] == {"type": "paragraph"}  # a list item may not start with a list
    assert first_item[1]["type"] == "bulletList"

    quote = blocks[3]["content"]
    assert [b["type"] for b in quote] == ["paragraph", "paragraph"]
    assert quote[0]["content"][0]["marks"] == [{"type": "strong"}]

    table = blocks[5]
    assert table["content"][0]["content"][0]["type"] == "tableHeader"
    assert table["content"][1]["content"][1]["content"][0]["content"][0]["text"] == "2"


def test_code_inside_a_link_keeps_only_the_link_mark():
    [paragraph] = adf.from_markdown("**[`x`](https://x.test)**")
    assert paragraph["content"][0]["marks"] == [{"type": "link", "attrs": {"href": "https://x.test"}}, {"type": "code"}]


def test_round_trip():
    markdown = "## Result\n\nChanged **two** files:\n\n- `api.py`\n- `tests/test_api.py`\n\n```\nuv run pytest\n```"
    assert adf.to_markdown(adf.doc(*adf.from_markdown(markdown))) == markdown
