"""Atlassian Document Format to Markdown and back (design §5).

Jira descriptions and comments arrive as ADF; agents read Markdown. Agents write Markdown;
comments go out as ADF. Both directions cover what tickets and results actually use and
fall back to plain text for the rest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

Node = dict[str, Any]

# -- building ADF ------------------------------------------------------------------------


def doc(*blocks: Node) -> Node:
    return {"type": "doc", "version": 1, "content": [b for b in blocks if b]}


def paragraph(*inline: Node) -> Node:
    content = [n for n in inline if n]
    return {"type": "paragraph", "content": content} if content else {"type": "paragraph"}


def text(value: str, *marks: Node) -> Node | None:
    if not value:
        return None
    node: Node = {"type": "text", "text": value}
    if marks:
        node["marks"] = list(marks)
    return node


def strong(value: str) -> Node | None:
    return text(value, {"type": "strong"})


def em(value: str) -> Node | None:
    return text(value, {"type": "em"})


def mention(account_id: str, name: str) -> Node:
    return {"type": "mention", "attrs": {"id": account_id, "text": f"@{name}"}}


def bullet_list(items: list[str]) -> Node:
    return {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [paragraph(text(item))]} for item in items],
    }


def mentions(node: Any, account_id: str) -> bool:
    """Whether the document mentions the account anywhere."""
    if isinstance(node, dict):
        if node.get("type") == "mention" and (node.get("attrs") or {}).get("id") == account_id:
            return True
        return any(mentions(child, account_id) for child in node.get("content") or [])
    return False


# -- ADF to Markdown ---------------------------------------------------------------------


def to_markdown(node: Node | None, *, skip_mention: str | None = None) -> str:
    """Markdown for an ADF document. ``skip_mention`` drops mentions of that account."""
    if not node:
        return ""
    return _Writer(skip_mention).blocks(node.get("content") or []).strip()


class _Writer:
    def __init__(self, skip_mention: str | None):
        self.skip = skip_mention

    def blocks(self, nodes: list[Node], sep: str = "\n\n") -> str:
        return sep.join(part for part in (self.block(n) for n in nodes) if part.strip())

    def block(self, node: Node) -> str:
        kind = node.get("type")
        content = node.get("content") or []
        attrs = node.get("attrs") or {}
        match kind:
            case "paragraph":
                return self.inline(content)
            case "heading":
                level = min(max(int(attrs.get("level") or 1), 1), 6)
                return "#" * level + " " + self.inline(content)
            case "bulletList":
                return "\n".join(self.item(i, "- ") for i in content)
            case "orderedList":
                start = int(attrs.get("order") or 1)
                return "\n".join(self.item(i, f"{start + n}. ") for n, i in enumerate(content))
            case "taskList":
                lines = []
                for item in content:
                    box = "[x]" if (item.get("attrs") or {}).get("state") == "DONE" else "[ ]"
                    lines.append(f"- {box} " + self.inline(item.get("content") or []))
                return "\n".join(lines)
            case "decisionList":
                return "\n".join("- " + self.inline(i.get("content") or []) for i in content)
            case "codeBlock":
                code = "".join(n.get("text", "") for n in content)
                fence = "~~~" if "```" in code else "```"
                return f"{fence}{attrs.get('language') or ''}\n{code}\n{fence}"
            case "blockquote" | "panel":
                inner = self.blocks(content)
                return "\n".join("> " + line if line else ">" for line in inner.split("\n"))
            case "rule":
                return "---"
            case "mediaSingle" | "mediaGroup":
                return "\n".join(self.media(n) for n in content if n.get("type") == "media")
            case "media":
                return self.media(node)
            case "table":
                return self.table(content)
            case "expand" | "nestedExpand":
                title = attrs.get("title")
                body = self.blocks(content)
                return f"**{title}**\n\n{body}" if title else body
            case "blockCard" | "embedCard":
                return f"<{attrs['url']}>" if attrs.get("url") else ""
            case _:
                if any(_is_block(n) for n in content):
                    return self.blocks(content)
                return self.inline(content)

    def item(self, node: Node, marker: str) -> str:
        body = self.blocks(node.get("content") or [], sep="\n")
        lines = body.split("\n") if body else [""]
        pad = " " * len(marker)
        return "\n".join([marker + lines[0]] + [pad + line if line else line for line in lines[1:]])

    def media(self, node: Node) -> str:
        attrs = node.get("attrs") or {}
        if attrs.get("type") == "external" and attrs.get("url"):
            return f"![{attrs.get('alt') or 'image'}]({attrs['url']})"
        name = attrs.get("alt")
        return f"[attachment: {name}]" if name else "[attachment]"

    def table(self, rows: list[Node]) -> str:
        cells = [
            [self.blocks(c.get("content") or [], sep=" ").replace("\n", " ").replace("|", "\\|") for c in r.get("content") or []]
            for r in rows
        ]
        cells = [r for r in cells if r]
        if not cells:
            return ""
        width = max(len(r) for r in cells)
        cells = [r + [""] * (width - len(r)) for r in cells]
        lines = ["| " + " | ".join(cells[0]) + " |", "|" + " --- |" * width]
        lines += ["| " + " | ".join(r) + " |" for r in cells[1:]]
        return "\n".join(lines)

    def inline(self, nodes: list[Node]) -> str:
        out: list[str] = []
        for node in nodes:
            kind = node.get("type")
            attrs = node.get("attrs") or {}
            match kind:
                case "text":
                    out.append(_marked(node.get("text", ""), node.get("marks") or []))
                case "hardBreak":
                    out.append("  \n")
                case "mention":
                    if self.skip and attrs.get("id") == self.skip:
                        continue
                    name = str(attrs.get("text") or "someone")
                    out.append(name if name.startswith("@") else f"@{name}")
                case "emoji":
                    out.append(str(attrs.get("text") or attrs.get("shortName") or ""))
                case "inlineCard":
                    out.append(f"<{attrs['url']}>" if attrs.get("url") else "")
                case "date":
                    out.append(_date(attrs.get("timestamp")))
                case "status":
                    out.append(str(attrs.get("text") or ""))
                case "mediaInline":
                    out.append(self.media(node))
                case _:
                    out.append(self.inline(node.get("content") or []))
        return "".join(out)


_BLOCKS = {
    "paragraph", "heading", "bulletList", "orderedList", "taskList", "decisionList", "codeBlock",
    "blockquote", "panel", "rule", "mediaSingle", "mediaGroup", "table", "expand", "nestedExpand",
    "blockCard", "embedCard",
}


def _is_block(node: Node) -> bool:
    return node.get("type") in _BLOCKS


def _marked(value: str, marks: list[Node]) -> str:
    if not value:
        return ""
    kinds = {m.get("type"): m for m in marks}
    # Markdown emphasis does not take surrounding spaces: keep them outside the markers.
    lead = value[: len(value) - len(value.lstrip())]
    trail = value[len(value.rstrip()) :]
    core = value.strip()
    if not core:
        return value
    if "code" in kinds:
        ticks = "``" if "`" in core else "`"
        core = f"{ticks}{core}{ticks}"
    else:
        if "strike" in kinds:
            core = f"~~{core}~~"
        if "em" in kinds:
            core = f"*{core}*"
        if "strong" in kinds:
            core = f"**{core}**"
    link = kinds.get("link")
    if link and (link.get("attrs") or {}).get("href"):
        core = f"[{core}]({link['attrs']['href']})"
    return lead + core + trail


def _date(timestamp: Any) -> str:
    try:
        return datetime.fromtimestamp(int(timestamp) / 1000, UTC).date().isoformat()
    except (TypeError, ValueError):
        return ""


# -- Markdown to ADF ---------------------------------------------------------------------

_md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

# What ADF lets these containers hold; anything else is rewritten (_fit).
_IN_LIST_ITEM = {"paragraph", "bulletList", "orderedList", "codeBlock"}
_IN_QUOTE = {"paragraph", "bulletList", "orderedList", "codeBlock"}


def from_markdown(markdown: str | None) -> list[Node]:
    """ADF block nodes for Markdown text (to put into a ``doc``)."""
    if not markdown or not markdown.strip():
        return []
    return _blocks(SyntaxTreeNode(_md.parse(markdown)).children)


def _blocks(nodes: list[SyntaxTreeNode]) -> list[Node]:
    out: list[Node] = []
    for node in nodes:
        out.extend(_block(node))
    return out


def _block(node: SyntaxTreeNode) -> list[Node]:
    match node.type:
        case "paragraph":
            content = _inline_of(node)
            return [{"type": "paragraph", "content": content}] if content else []
        case "heading":
            level = int(node.tag[1]) if node.tag[1:].isdigit() else 1
            return [{"type": "heading", "attrs": {"level": level}, "content": _inline_of(node)}]
        case "bullet_list":
            return [{"type": "bulletList", "content": [_list_item(c) for c in node.children]}]
        case "ordered_list":
            listing: Node = {"type": "orderedList", "content": [_list_item(c) for c in node.children]}
            start = node.attrs.get("start")
            if isinstance(start, int) and start != 1:
                listing["attrs"] = {"order": start}
            return [listing]
        case "fence" | "code_block":
            code = node.content.rstrip("\n")
            block: Node = {"type": "codeBlock"}
            language = (node.info or "").strip().split(" ")[0] if node.type == "fence" else ""
            if language:
                block["attrs"] = {"language": language}
            if code:
                block["content"] = [{"type": "text", "text": code}]
            return [block]
        case "blockquote":
            content = _fit(_blocks(node.children), _IN_QUOTE)
            return [{"type": "blockquote", "content": content}] if content else []
        case "hr":
            return [{"type": "rule"}]
        case "table":
            return [_table(node)]
        case "html_block":
            value = node.content.strip()
            return [paragraph(text(value))] if value else []
        case _:
            return _blocks(node.children)


def _list_item(node: SyntaxTreeNode) -> Node:
    content = _fit(_blocks(node.children), _IN_LIST_ITEM)
    if not content or content[0]["type"] not in {"paragraph", "codeBlock"}:
        content.insert(0, {"type": "paragraph"})
    return {"type": "listItem", "content": content}


def _fit(blocks: list[Node], allowed: set[str]) -> list[Node]:
    """Rewrite blocks a container cannot hold: headings become bold paragraphs, nested
    quotes are flattened, tables become one paragraph per row, rules are dropped."""
    out: list[Node] = []
    for block in blocks:
        kind = block["type"]
        if kind in allowed:
            out.append(block)
        elif kind == "heading":
            inline = [dict(n, marks=[*(n.get("marks") or []), {"type": "strong"}]) if n["type"] == "text" else n
                      for n in block.get("content") or []]
            out.append(paragraph(*inline))
        elif kind == "blockquote":
            out.extend(_fit(block.get("content") or [], allowed))
        elif kind == "table":
            for row in block.get("content") or []:
                cells = [_plain(cell) for cell in row.get("content") or []]
                out.append(paragraph(text(" | ".join(c for c in cells if c))))
        # rules and anything else unknown are dropped
    return out


def _plain(node: Node) -> str:
    if node.get("type") == "text":
        return node.get("text", "")
    return " ".join(p for p in (_plain(c) for c in node.get("content") or []) if p)


def _table(node: SyntaxTreeNode) -> Node:
    rows: list[Node] = []
    for section in node.children:  # thead, tbody
        for row in section.children:
            cells = []
            for cell in row.children:
                kind = "tableHeader" if cell.type == "th" else "tableCell"
                cells.append({"type": kind, "content": [paragraph(*_inline_of(cell))]})
            rows.append({"type": "tableRow", "content": cells})
    return {"type": "table", "content": rows}


def _inline_of(node: SyntaxTreeNode) -> list[Node]:
    out: list[Node] = []
    for child in node.children:
        if child.type == "inline":
            out.extend(_inline(child.children, []))
    return _merge(out)


def _inline(nodes: list[SyntaxTreeNode], marks: list[Node]) -> list[Node]:
    out: list[Node] = []
    for node in nodes:
        match node.type:
            case "text" | "html_inline":
                out.append(_text(node.content, marks))
            case "softbreak":
                out.append(_text(" ", marks))
            case "hardbreak":
                out.append({"type": "hardBreak"})
            case "code_inline":
                # ADF lets the code mark combine with a link only.
                out.append(_text(node.content, [m for m in marks if m["type"] == "link"] + [{"type": "code"}]))
            case "strong":
                out.extend(_inline(node.children, marks + [{"type": "strong"}]))
            case "em":
                out.extend(_inline(node.children, marks + [{"type": "em"}]))
            case "s":
                out.extend(_inline(node.children, marks + [{"type": "strike"}]))
            case "link":
                href = str(node.attrs.get("href") or "")
                link = [{"type": "link", "attrs": {"href": href}}] if href else []
                out.extend(_inline(node.children, marks + link))
            case "image":
                alt = "".join(c.content for c in node.children) or "image"
                src = str(node.attrs.get("src") or "")
                link = [{"type": "link", "attrs": {"href": src}}] if src else []
                out.append(_text(alt, marks + link))
            case _:
                out.extend(_inline(node.children, marks))
    return [n for n in out if n]


def _text(value: str, marks: list[Node]) -> Node | None:
    if not value:
        return None
    node: Node = {"type": "text", "text": value}
    unique: list[Node] = []
    for mark in marks:
        if mark not in unique:
            unique.append(mark)
    if unique:
        node["marks"] = unique
    return node


def _merge(nodes: list[Node]) -> list[Node]:
    """Join neighbouring text nodes that carry the same marks."""
    out: list[Node] = []
    for node in nodes:
        last = out[-1] if out else None
        if last and last["type"] == node["type"] == "text" and last.get("marks") == node.get("marks"):
            out[-1] = dict(last, text=last["text"] + node["text"])
        else:
            out.append(node)
    return out
