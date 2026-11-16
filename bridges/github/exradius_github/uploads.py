"""Files people put in an issue or a comment (design §4.2).

GitHub's editor uploads a dropped file and writes a link to it into the Markdown:
``![Image](https://github.com/user-attachments/assets/<uuid>)`` (or an ``<img>`` tag once the
image is resized), ``[log.zip](https://github.com/user-attachments/files/<id>/log.zip)`` for
other files, and older forms on ``user-images.githubusercontent.com`` or
``github.com/<owner>/<repo>/assets/...``. Those links need a browser session in a private
repository. The issue's or comment's ``body_html`` (asked for with
``application/vnd.github.full+json``) carries, for each image, a short-lived signed address
on ``private-user-images.githubusercontent.com`` that the bridge can download from.

``find`` returns the Markdown with each upload replaced by ``[attachment: <name>]`` (the
name the agent finds it under) and the uploads, each with the address to download from.
"""

from __future__ import annotations

import html
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime
from posixpath import splitext
from urllib.parse import unquote, urlsplit

from exradius_bridge_kit import AttachmentInfo

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MD_LINK = re.compile(r"(!?)\[([^\]\n]*)\]\(\s*<?(https?://[^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""\b(src|alt)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_HTML_URL = re.compile(r"""\b(?:src|href)\s*=\s*"([^"]+)\"""", re.IGNORECASE)
_PLAIN_ALT = {"", "image", "img", "screenshot", "screen shot"}


@dataclass(frozen=True)
class Upload:
    info: AttachmentInfo
    """``info.id`` is the address to download from."""


def is_upload(url: str, web_host: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in ("user-images.githubusercontent.com", "private-user-images.githubusercontent.com"):
        return True
    if host != web_host:
        return False
    segments = parts.path.strip("/").split("/")
    return segments[:1] == ["user-attachments"] or (len(segments) >= 4 and segments[2] == "assets")


def _key(url: str) -> str:
    found = UUID.search(urlsplit(url).path.lower())
    return found[0] if found else url


def _signed(body_html: str | None) -> dict[str, str]:
    """The signed image addresses in the rendered body, by the upload's uuid."""
    found: dict[str, str] = {}
    for url in _HTML_URL.findall(body_html or ""):
        url = html.unescape(url)
        if urlsplit(url).hostname == "private-user-images.githubusercontent.com":
            found.setdefault(_key(url), url)
    return found


def _name(alt: str, original: str, download: str, image: bool, used: set[str], counter: list[int]) -> str:
    path = unquote(urlsplit(original).path)
    ext = splitext(path)[1].lower() or splitext(urlsplit(download).path)[1].lower()
    if not ext and image:
        ext = ".png"  # the editor's images; the name tells the agent what the file is
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if "/files/" in path and last:
        base = last
    elif alt.strip().lower() not in _PLAIN_ALT:
        base = alt.strip()
        if ext and not base.lower().endswith(ext):
            base += ext
    else:
        counter[0] += 1
        while f"image-{counter[0]}{ext or '.png'}" in used:
            counter[0] += 1
        base = f"image-{counter[0]}{ext or '.png'}"
    base = "".join("_" if c in '/\\\x00:*?"<>|' else c for c in base).strip() or "attachment"
    name, n = base, 2
    stem, suffix = splitext(base)
    while name in used:
        name = f"{stem} ({n}){suffix}"
        n += 1
    used.add(name)
    return name


def find(markdown: str, body_html: str | None, created: datetime | None, web_host: str,
         used: set[str], counter: list[int]) -> tuple[str, list[Upload]]:
    """The Markdown with its uploads named, and the uploads. ``used`` and ``counter`` are
    shared across one item, so names stay unique in it."""
    signed = _signed(body_html)
    uploads: list[Upload] = []
    named: dict[str, str] = {}

    def take(url: str, alt: str, image: bool) -> str:
        url = html.unescape(url)
        if url in named:
            return named[url]
        download = signed.get(_key(url), url)
        name = _name(alt, url, download, image, used, counter)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        uploads.append(Upload(AttachmentInfo(id=download, name=name, content_type=content_type, size=0,
                                             created=created)))
        named[url] = f"[attachment: {name}]"
        return named[url]

    def link(match: re.Match[str]) -> str:
        url = match[3]
        return take(url, match[2], match[1] == "!") if is_upload(url, web_host) else match[0]

    def tag(match: re.Match[str]) -> str:
        attrs = {m[0].lower(): m[1] or m[2] for m in _ATTR.findall(match[0])}
        url = attrs.get("src", "")
        return take(url, attrs.get("alt", ""), True) if url and is_upload(html.unescape(url), web_host) else match[0]

    text = _MD_LINK.sub(link, markdown or "")
    text = _IMG_TAG.sub(tag, text)
    return text, uploads
