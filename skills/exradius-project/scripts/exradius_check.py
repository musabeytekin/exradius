#!/usr/bin/env python3
"""Local check of an Exradius project config: what the admin panel's Verify does, with the
Docker CLI alone and no Exradius installed.

    exradius_check.py [options] <config dir or exradius.yaml>

Steps, in Verify's order: read and validate exradius.yaml; build the workstation and the
service images; check the workstation image (glibc, sh, bash, git); put the code
repositories under /work; start the services on a temporary network and wait until they
are ready; run every setup command in the workstation under the run's conditions (with
``resources.memory`` as its memory limit and ``workstation.cache`` mounted from a local cache
directory that persists between checks); stop and remove everything but the cache. The first failing step is printed with its output.

Standard library only, Python 3.8 or later. The YAML reader below covers the YAML an
exradius.yaml uses (block and flow collections, quoted, plain and block scalars,
comments); anchors, aliases and tags are refused with a message.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import posixpath
import re
import secrets as _random
import shutil
import subprocess
import sys
import tempfile
import time

CONFIG_VERSION = 1
"""The exradius.yaml version this check (and the skill) understands."""

# ---------------------------------------------------------------------------------------
# YAML (the subset exradius.yaml needs; YAML 1.1 scalar rules, as PyYAML's safe loader)
# ---------------------------------------------------------------------------------------


class YamlError(Exception):
    def __init__(self, line, message):
        super().__init__("line %d: %s" % (line, message))


class YamlDate(object):
    """A plain scalar YAML reads as a date or timestamp (never a string)."""

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return "YamlDate(%r)" % self.text


_NULLS = {"", "~", "null", "Null", "NULL"}
_TRUE = {"yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON", "y", "Y"}
_FALSE = {"no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF", "n", "N"}
# PyYAML's resolver does not treat y/Y/n/N as booleans; keep them strings.
_TRUE -= {"y", "Y"}
_FALSE -= {"n", "N"}
_INT = re.compile(r"^(?:[-+]?0b[01_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+"
                  r"|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$")
_FLOAT = re.compile(r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9_]+(?:[eE][-+][0-9]+)?"
                    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$")
_TIMESTAMP = re.compile(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"
                        r"(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?"
                        r"(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?)$")


def _int_value(text):
    t = text.replace("_", "")
    sign = -1 if t.startswith("-") else 1
    t = t.lstrip("+-")
    if t.startswith("0b"):
        return sign * int(t[2:], 2)
    if t.startswith("0x"):
        return sign * int(t[2:], 16)
    if ":" in t:
        value = 0
        for part in t.split(":"):
            value = value * 60 + int(part)
        return sign * value
    if t != "0" and t.startswith("0"):
        return sign * int(t, 8)
    return sign * int(t)


def resolve_plain(text):
    if text in _NULLS:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if _INT.match(text):
        try:
            return _int_value(text)
        except ValueError:
            return text
    if _FLOAT.match(text):
        t = text.replace("_", "").lower()
        if t.endswith("inf"):
            return float("-inf") if t.startswith("-") else float("inf")
        if t.endswith("nan"):
            return float("nan")
        if ":" in t:
            return float(_int_value(t.split(".")[0])) + float("0." + (t.split(".")[1] or "0"))
        try:
            return float(t)
        except ValueError:
            return text
    if _TIMESTAMP.match(text):
        return YamlDate(text)
    return text


_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
            "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85",
            "_": "\xa0", "L": "\u2028", "P": "\u2029"}


class _Yaml(object):
    def __init__(self, text):
        if text.startswith("\ufeff"):
            text = text[1:]
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.i = 0

    # -- lines --------------------------------------------------------------------------

    @staticmethod
    def _indent(line):
        return len(line) - len(line.lstrip(" "))

    def _significant(self):
        """Index of the next line that is not blank or a comment, or None."""
        j = self.i
        while j < len(self.lines):
            stripped = self.lines[j].strip()
            if stripped and not stripped.startswith("#"):
                if "\t" in self.lines[j][: self._indent(self.lines[j]) + 1] and self.lines[j].lstrip(" ").startswith("\t"):
                    raise YamlError(j + 1, "tabs cannot be used for indentation")
                return j
            j += 1
        return None

    @staticmethod
    def strip_comment(text):
        """``text`` without a trailing ``# comment`` (outside quotes), right-stripped."""
        quote = None
        k = 0
        while k < len(text):
            c = text[k]
            if quote == '"':
                if c == "\\":
                    k += 2
                    continue
                if c == '"':
                    quote = None
            elif quote == "'":
                if c == "'":
                    if k + 1 < len(text) and text[k + 1] == "'":
                        k += 2
                        continue
                    quote = None
            elif c in "\"'" and (k == 0 or text[k - 1] in " \t[{,:-?"):
                quote = c
            elif c == "#" and (k == 0 or text[k - 1] in " \t"):
                return text[:k].rstrip()
            k += 1
        return text.rstrip()

    @staticmethod
    def key_split(text):
        """``(key text, rest)`` when ``text`` is a ``key: value`` line, else None."""
        quote = None
        depth = 0
        k = 0
        if text[:1] in "[{":
            return None
        while k < len(text):
            c = text[k]
            if quote == '"':
                if c == "\\":
                    k += 2
                    continue
                if c == '"':
                    quote = None
            elif quote == "'":
                if c == "'":
                    if k + 1 < len(text) and text[k + 1] == "'":
                        k += 2
                        continue
                    quote = None
            elif c in "\"'" and k == 0:
                quote = c
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            elif c == ":" and depth <= 0 and (k + 1 == len(text) or text[k + 1] in " \t"):
                return text[:k].rstrip(), text[k + 1:].strip()
            k += 1
        return None

    @staticmethod
    def _is_item(content):
        return content == "-" or content.startswith("- ") or content.startswith("-\t")

    # -- nodes --------------------------------------------------------------------------

    def parse(self):
        j = self._significant()
        if j is not None and self.lines[j].strip() in ("---",) :
            self.i = j + 1
        value = self.node(0)
        j = self._significant()
        if j is not None:
            if self.lines[j].strip() == "...":
                return value
            if self.lines[j].strip() == "---":
                raise YamlError(j + 1, "only one YAML document is allowed")
            raise YamlError(j + 1, "unexpected content (check the indentation)")
        return value

    def node(self, min_indent):
        j = self._significant()
        if j is None:
            return None
        indent = self._indent(self.lines[j])
        if indent < min_indent:
            return None
        content = self.strip_comment(self.lines[j][indent:])
        if self._is_item(content):
            return self.sequence(indent)
        if self.key_split(content) is not None:
            return self.mapping(indent)
        self.i = j + 1
        return self.inline_value(content, j, indent)

    def mapping(self, indent):
        result = {}
        while True:
            j = self._significant()
            if j is None:
                return result
            line_indent = self._indent(self.lines[j])
            if line_indent < indent:
                return result
            if line_indent > indent:
                raise YamlError(j + 1, "unexpected indentation")
            content = self.strip_comment(self.lines[j][indent:])
            if self._is_item(content):
                return result
            split = self.key_split(content)
            if split is None:
                raise YamlError(j + 1, "expected 'key: value'")
            key_text, rest = split
            key = self.scalar_key(key_text, j)
            if key in result:
                raise YamlError(j + 1, "duplicate key %r" % (key,))
            self.i = j + 1
            if rest == "":
                nxt = self._significant()
                if nxt is not None:
                    n_indent = self._indent(self.lines[nxt])
                    n_content = self.strip_comment(self.lines[nxt][n_indent:])
                    if n_indent > indent:
                        result[key] = self.node(indent + 1)
                        continue
                    if n_indent == indent and self._is_item(n_content):
                        result[key] = self.sequence(indent)
                        continue
                result[key] = None
            else:
                result[key] = self.inline_value(rest, j, indent)

    def sequence(self, indent):
        result = []
        while True:
            j = self._significant()
            if j is None:
                return result
            line = self.lines[j]
            line_indent = self._indent(line)
            if line_indent < indent:
                return result
            if line_indent > indent:
                raise YamlError(j + 1, "unexpected indentation in a list")
            content = self.strip_comment(line[indent:])
            if not self._is_item(content):
                return result
            after = content[1:]
            rest = after.lstrip(" \t")
            if rest == "":
                self.i = j + 1
                nxt = self._significant()
                if nxt is not None and self._indent(self.lines[nxt]) > indent:
                    result.append(self.node(indent + 1))
                else:
                    result.append(None)
                continue
            item_indent = indent + 1 + (len(after) - len(rest))
            if self._is_item(rest) or self.key_split(rest) is not None:
                # Re-read the item as a block that starts at its own column.
                self.lines[j] = " " * item_indent + line[item_indent:]
                self.i = j
                result.append(self.node(item_indent))
            else:
                self.i = j + 1
                result.append(self.inline_value(rest, j, indent))

    def scalar_key(self, text, j):
        if text.startswith('"') or text.startswith("'"):
            value, end = self.quoted(text, 0, j)
            if text[end:].strip():
                raise YamlError(j + 1, "unexpected text after a quoted key")
            return value
        if text[:1] in "&*!":
            raise YamlError(j + 1, "anchors, aliases and tags are not supported here")
        if text.startswith("? "):
            raise YamlError(j + 1, "complex keys are not supported")
        value = resolve_plain(text)
        return value if not isinstance(value, YamlDate) else value.text

    def inline_value(self, text, j, indent):
        """A value given on the line of its key or list dash (``self.i`` is past that line)."""
        if text[:1] in "&*!":
            raise YamlError(j + 1, "anchors, aliases and tags are not supported by this check "
                                   "(write the value out)")
        if text[:1] in "|>":
            return self.block_scalar(text, j, indent)
        if text[:1] in "[{":
            full = text
            while True:
                try:
                    value, end = self.flow(full, 0, j)
                except _Incomplete:
                    if self.i >= len(self.lines):
                        raise YamlError(j + 1, "unclosed %s" % ("[" if text[0] == "[" else "{"))
                    full = full + " " + self.strip_comment(self.lines[self.i].strip())
                    self.i += 1
                    continue
                if full[end:].strip():
                    raise YamlError(j + 1, "unexpected text after %r" % full[:end])
                return value
        if text[:1] in "\"'":
            full = text
            while True:
                try:
                    value, end = self.quoted(full, 0, j)
                    break
                except _Incomplete:
                    if self.i >= len(self.lines):
                        raise YamlError(j + 1, "unclosed quote")
                    nxt = self.lines[self.i].strip()
                    full = full + ("\n" if not nxt else " " + nxt)
                    self.i += 1
            if self.strip_comment(full[end:]).strip():
                raise YamlError(j + 1, "unexpected text after the closing quote")
            return value
        # A plain scalar may continue on more-indented lines.
        parts = [text]
        while True:
            nxt = self._significant()
            if nxt is None or self._indent(self.lines[nxt]) <= indent:
                break
            more = self.strip_comment(self.lines[nxt].strip())
            if self.key_split(more) is not None or self._is_item(more):
                raise YamlError(nxt + 1, "unexpected indentation (a value cannot continue as a mapping)")
            parts.append(more)
            self.i = nxt + 1
        return resolve_plain(" ".join(parts))

    def block_scalar(self, header, j, indent):
        m = re.match(r"^([|>])([+-]?)([1-9]?)([+-]?)$", header)
        if not m:
            raise YamlError(j + 1, "bad block scalar header %r" % header)
        style, chomp, digit = m.group(1), m.group(2) or m.group(4), m.group(3)
        content_indent = indent + int(digit) if digit else None
        raw = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() == "":
                raw.append("")
                self.i += 1
                continue
            li = self._indent(line)
            if content_indent is None:
                if li <= indent:
                    break
                content_indent = li
            if li < content_indent:
                break
            raw.append(line[content_indent:])
            self.i += 1
        # trailing blank lines belong to chomping
        body = list(raw)
        trailing = 0
        while body and body[-1] == "":
            body.pop()
            trailing += 1
        if style == "|":
            text = "\n".join(body)
        else:
            # Folding: a break between two normal lines is a space; n empty lines between
            # them are n newlines; around more-indented lines every break is kept.
            text = ""
            last = None  # kind of the last non-empty line
            empties = 0
            for line in body:
                if line == "":
                    empties += 1
                    continue
                kind = "more" if line[:1] in " \t" else "normal"
                if last is None:
                    text = "\n" * empties + line
                elif last == "normal" and kind == "normal":
                    text += (" " if empties == 0 else "\n" * empties) + line
                else:
                    text += "\n" * (empties + 1) + line
                last, empties = kind, 0
        if not body:
            return "" if chomp != "+" else "\n" * trailing
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" + "\n" * trailing
        return text + "\n"

    def quoted(self, text, pos, j):
        q = text[pos]
        k = pos + 1
        out = []
        while True:
            if k >= len(text):
                raise _Incomplete()
            c = text[k]
            if q == "'":
                if c == "'":
                    if k + 1 < len(text) and text[k + 1] == "'":
                        out.append("'")
                        k += 2
                        continue
                    return "".join(out), k + 1
                out.append(c)
                k += 1
                continue
            if c == '"':
                return "".join(out), k + 1
            if c == "\\":
                if k + 1 >= len(text):
                    raise _Incomplete()
                e = text[k + 1]
                if e in _ESCAPES:
                    out.append(_ESCAPES[e])
                    k += 2
                    continue
                size = {"x": 2, "u": 4, "U": 8}.get(e)
                if size:
                    try:
                        out.append(chr(int(text[k + 2:k + 2 + size], 16)))
                    except ValueError:
                        raise YamlError(j + 1, "bad escape in a double-quoted string")
                    k += 2 + size
                    continue
                raise YamlError(j + 1, "unknown escape \\%s in a double-quoted string" % e)
            if c == "\n":
                out.append("\n")
                k += 1
                continue
            out.append(c)
            k += 1

    def flow(self, text, pos, j):
        """A flow collection or scalar at ``pos``; ``(value, end)``."""
        k = self._skip(text, pos)
        if k >= len(text):
            raise _Incomplete()
        c = text[k]
        if c == "[":
            items = []
            k += 1
            while True:
                k = self._skip(text, k)
                if k >= len(text):
                    raise _Incomplete()
                if text[k] == "]":
                    return items, k + 1
                value, k = self.flow_entry(text, k, j, in_map=False)
                items.append(value)
                k = self._skip(text, k)
                if k >= len(text):
                    raise _Incomplete()
                if text[k] == ",":
                    k += 1
                elif text[k] != "]":
                    raise YamlError(j + 1, "expected ',' or ']' in a flow list")
        if c == "{":
            result = {}
            k += 1
            while True:
                k = self._skip(text, k)
                if k >= len(text):
                    raise _Incomplete()
                if text[k] == "}":
                    return result, k + 1
                key, k = self.flow_scalar(text, k, j, key=True)
                k = self._skip(text, k)
                value = None
                if k < len(text) and text[k] == ":":
                    k = self._skip(text, k + 1)
                    if k >= len(text):
                        raise _Incomplete()
                    if text[k] not in ",}":
                        value, k = self.flow(text, k, j)
                if isinstance(key, YamlDate):
                    key = key.text
                if key in result:
                    raise YamlError(j + 1, "duplicate key %r" % (key,))
                result[key] = value
                k = self._skip(text, k)
                if k >= len(text):
                    raise _Incomplete()
                if text[k] == ",":
                    k += 1
                elif text[k] != "}":
                    raise YamlError(j + 1, "expected ',' or '}' in a flow mapping")
        return self.flow_scalar(text, k, j)

    def flow_entry(self, text, k, j, in_map):
        value, end = self.flow(text, k, j)
        e = self._skip(text, end)
        if not in_map and e < len(text) and text[e] == ":" and not isinstance(value, (dict, list)):
            # single-pair mapping inside a list: [a: b]
            inner, end = self.flow(text, e + 1, j)
            return {value: inner}, end
        return value, end

    def flow_scalar(self, text, k, j, key=False):
        if k < len(text) and text[k] in "\"'":
            return self.quoted(text, k, j)
        if k < len(text) and text[k] in "&*!":
            raise YamlError(j + 1, "anchors, aliases and tags are not supported by this check")
        start = k
        while k < len(text):
            c = text[k]
            if c in ",]}[{":
                break
            if c == ":" and (k + 1 == len(text) or text[k + 1] in " \t,]}"):
                break
            k += 1
        return resolve_plain(text[start:k].strip()), k

    @staticmethod
    def _skip(text, k):
        while k < len(text) and text[k] in " \t\n":
            k += 1
        return k


class _Incomplete(Exception):
    pass


def load_yaml(text):
    return _Yaml(text).parse()


# ---------------------------------------------------------------------------------------
# The exradius.yaml schema (version 1) and its rules
# ---------------------------------------------------------------------------------------

SECRET_REF = re.compile(r"\$\{secret:([A-Za-z0-9_.\-]+)\}")
SECRET_MARK = "${secret:"
NAME = re.compile(r"^[a-z][a-z0-9-]*$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
IDENTITY_NAME = re.compile(r"^[^<>\x00-\x1f]+$")
IDENTITY_EMAIL = re.compile(r"^[^@<>\s]+@[^@<>\s]+$")
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]*$")
RESERVED_SERVICES = ("api", "workstation")
MEMORY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([bkmgBKMG])$")
"""A Docker size string: ``512m``, ``6g`` (docs/run-environment.md §4)."""
MEMORY_UNITS = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
MEMORY_MIN = 6 * 1024 ** 2
"""Docker refuses a memory limit below 6 MiB."""
CACHE_FORBIDDEN = ("/", "/home", "/home/runner", "/tmp", "/etc", "/usr", "/bin", "/lib", "/var", "/opt")
CACHE_FORBIDDEN_UNDER = ("/work", "/exradius", "/opt/exradius", "/proc", "/sys", "/dev", "/etc/passwd", "/etc/group")
"""``workstation.cache`` entries may not be the first set of paths, nor be or lie under the
second: the project directory, Exradius's own mounts, the kernel's file systems and the
generated identity files (docs/run-environment.md §5; ``cache_problem`` in Exradius)."""
CATALOG = {
    # key: (required config keys, optional config keys, launcher it needs)
    "github": (("token",), (), None),
    "gitlab": (("token",), ("api_url",), "npx"),
    "postgres": (("url",), (), "uvx"),
    "slack": (("bot_token", "team_id"), (), "npx"),
    "sentry": (("token",), (), "npx"),
    "linear": (("api_key",), (), None),
    "jira": (("url", "username", "api_token"), (), "uvx"),
    "fetch": ((), (), "uvx"),
    "playwright": ((), (), "npx"),
    "filesystem": ((), ("path",), "npx"),
}
LAUNCHERS = ("npx", "uvx")
SECRET_PLACES = (
    ("repositories", int, "credential"),
    ("services", str, "env", str),
    ("env", str),
    ("connections", str, "config", str),
    ("connections", str, "env", str),
    ("connections", str, "headers", str),
)


def memory_bytes(text):
    """Bytes of a Docker size string, or None."""
    match = MEMORY.match(text.strip())
    if not match:
        return None
    return int(float(match.group(1)) * MEMORY_UNITS[match.group(2).lower()])


def cache_target(text):
    """The container path of a ``workstation.cache`` entry, or ``(None, reason)``."""
    if "\x00" in text or "\\" in text or "\n" in text:
        return None, "must be a plain path"
    if text.startswith("~/"):
        path = "/home/runner/" + text[2:]
    elif text == "~":
        return None, "the home itself cannot be a cache; name a folder in it (~/.cache/...)"
    elif text.startswith("/"):
        path = text
    else:
        return None, "must start with ~/ or /"
    if ".." in path.split("/"):
        return None, "must not contain '..'"
    path = posixpath.normpath(path)
    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    for base in CACHE_FORBIDDEN_UNDER:
        if path == base or path.startswith(base + "/"):
            return None, ("%s is inside /work, which is the project directory; caches live outside it"
                          % path) if base == "/work" else "%s is reserved (%s)" % (path, base)
    if path in CACHE_FORBIDDEN:
        return None, "must name a directory below %s, not %s itself" % (path, path)
    return path, None


def cache_folder(index, target):
    """The host folder name of cache entry ``index``: ``<n>-<sanitised path>``."""
    rel = target[len("/home/runner/"):] if target.startswith("/home/runner/") else target
    return "%d-%s" % (index, re.sub(r"[^A-Za-z0-9._]+", "-", rel).strip("-") or "root")


def fmt_path(path):
    text = ""
    for part in path:
        if isinstance(part, int) and not isinstance(part, bool):
            text += "[%d]" % part
        else:
            text += ("." if text else "") + str(part)
    return text or "(file)"


class Validator(object):
    """Checks a parsed file the way Exradius does: strict types, unknown keys are errors."""

    def __init__(self):
        self.errors = []

    def err(self, path, message):
        self.errors.append("%s: %s" % (fmt_path(path), message))

    # -- types --------------------------------------------------------------------------

    def text(self, path, value, pattern=None, non_empty=False, numbers=False, max_len=None):
        """A string. ``numbers``: integers and floats become strings (as in compose files);
        booleans never do."""
        if isinstance(value, bool):
            if numbers:
                self.err(path, "write true, false, yes or no in quotes: the value must be a string")
            else:
                self.err(path, "must be a string")
            return None
        if numbers and isinstance(value, (int, float)):
            value = str(value)
        if isinstance(value, YamlDate):
            self.err(path, "must be a string (YAML reads %s as a date; quote it)" % value.text)
            return None
        if not isinstance(value, str):
            self.err(path, "must be a string")
            return None
        if non_empty and not value:
            self.err(path, "must not be empty")
            return None
        if max_len is not None and len(value) > max_len:
            self.err(path, "at most %d characters" % max_len)
            return None
        if pattern is not None and not pattern.match(value):
            self.err(path, "must match %s" % pattern.pattern)
            return None
        return value

    def boolean(self, path, value):
        if not isinstance(value, bool):
            self.err(path, "must be true or false")
            return None
        return value

    def port(self, path, value):
        if isinstance(value, bool) or not isinstance(value, int):
            self.err(path, "must be an integer")
            return None
        if not 1 <= value <= 65535:
            self.err(path, "must be between 1 and 65535")
            return None
        return value

    def mapping(self, path, value, known, required=()):
        if not isinstance(value, dict):
            self.err(path, "must be a mapping")
            return None
        for key in value:
            if key not in known:
                self.err(path + (key,), "unknown key")
        for key in required:
            if key not in value:
                self.err(path + (key,), "required")
        return value

    def str_map(self, path, value, key_pattern=None, non_empty_keys=False):
        """A mapping of strings (numbers become strings) with string keys."""
        if value is None:
            self.err(path, "must be a mapping")
            return {}
        if not isinstance(value, dict):
            self.err(path, "must be a mapping")
            return {}
        out = {}
        for key, item in value.items():
            if not isinstance(key, str) or isinstance(key, bool):
                self.err(path + (key,), "keys must be strings")
                continue
            if key_pattern is not None and not key_pattern.match(key):
                self.errors.append("%s (key): must match %s" % (fmt_path(path + (key,)), key_pattern.pattern))
                continue
            if non_empty_keys and not key:
                self.err(path + (key,), "must not be empty")
                continue
            text = self.text(path + (key,), item, numbers=True)
            if text is not None:
                out[key] = text
        return out

    def str_list(self, path, value, non_empty=False, numbers=False, min_len=0):
        if not isinstance(value, list):
            self.err(path, "must be a list")
            return []
        if len(value) < min_len:
            self.err(path, "must have at least %d item" % min_len)
        out = []
        for index, item in enumerate(value):
            text = self.text(path + (index,), item, non_empty=non_empty, numbers=numbers)
            if text is not None:
                out.append(text)
        return out


def scan_secrets(raw, errors):
    names = set()

    def walk(value, path):
        if isinstance(value, str):
            if SECRET_MARK not in value:
                return
            where = fmt_path(path)
            allowed = any(
                len(place) == len(path) and all(
                    (p is str and isinstance(x, str)) or (p is int and isinstance(x, int)) or p == x
                    for p, x in zip(place, path))
                for place in SECRET_PLACES)
            if not allowed:
                errors.append("%s: secrets are not allowed here" % where)
            elif value.count(SECRET_MARK) != len(SECRET_REF.findall(value)):
                errors.append("%s: malformed secret reference; write ${secret:NAME} with a NAME of "
                              "letters, digits, '_', '.' and '-'" % where)
            else:
                names.update(SECRET_REF.findall(value))
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, path + (key,))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, path + (index,))

    walk(raw, ())
    return sorted(names)


def repo_path(config_dir, value):
    """``value`` (relative to the config file's directory) from the repository root, or
    None when it is absolute or leaves the directory with ``..``."""
    if value.startswith("/") or "\\" in value or "\x00" in value:
        return None
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if ".." in parts:
        return None
    return posixpath.join(config_dir, *parts) if parts else config_dir


class Config(object):
    """The validated file, as plain values with defaults filled in."""

    def __init__(self):
        self.slug = None
        self.name = None
        self.store = None
        self.repositories = []  # dicts: name, url, branch, push, credential
        self.memory = None  # resources.memory, a Docker size string
        self.workstation = {}  # image, dockerfile, context (repository paths), setup, cache
        self.services = {}  # name -> dict: image, dockerfile, context, env, ports, ready, memory, command
        self.env = {}
        self.required_keys = []
        self.agents = []
        self.connections = []


def validate(raw, config_dir="", lookup=None):
    """``(Config or None, errors, warnings)``. ``config_dir`` is the file's directory from
    the repository root; ``lookup(path)`` says what a repository path is: "file", "dir",
    "link" (a symlink on the way) or None."""
    v = Validator()
    warnings = []
    if not isinstance(raw, dict):
        return None, ["(file): must be a mapping with version, project and agents"], [], []
    secret_errors = []
    required = scan_secrets(raw, secret_errors)
    top = ("version", "project", "secret_store", "resources", "repositories", "workstation", "services",
           "env", "notes", "connections", "agents")
    v.mapping((), raw, top, required=("version", "project", "agents"))
    cfg = Config()
    cfg.required_keys = required

    version = raw.get("version")
    if "version" in raw and (isinstance(version, bool) or not isinstance(version, int) or version != CONFIG_VERSION):
        v.err(("version",), "must be %d" % CONFIG_VERSION)

    project = raw.get("project")
    if "project" in raw:
        if v.mapping(("project",), project, ("slug", "name", "description"), required=("slug",)) is not None:
            if "slug" in project:
                cfg.slug = v.text(("project", "slug"), project["slug"], NAME, max_len=63)
            if project.get("name") is not None:
                cfg.name = v.text(("project", "name"), project["name"])
            if "description" in project:
                v.text(("project", "description"), project["description"])

    if raw.get("secret_store") is not None:
        cfg.store = v.text(("secret_store",), raw["secret_store"], NAME, max_len=63)

    def memory(path, value):
        # Numbers are read as text first, so ``512`` is refused for its missing unit.
        text = v.text(path, value, numbers=True)
        if text is None:
            return None
        text = text.strip()
        size = memory_bytes(text)
        if size is None:
            v.err(path, "must be a Docker size such as 512m or 6g (a number and one of b, k, m, g)")
            return None
        if size < MEMORY_MIN:
            v.err(path, "at least 6m (Docker's smallest memory limit)")
            return None
        return text

    resources = raw.get("resources")
    if "resources" in raw and v.mapping(("resources",), resources, ("memory",)) is not None:
        if resources.get("memory") is not None:
            cfg.memory = memory(("resources", "memory"), resources["memory"])

    folders = set()
    if "repositories" in raw:
        repos = raw["repositories"]
        if not isinstance(repos, list):
            v.err(("repositories",), "must be a list")
            repos = []
        for index, repo in enumerate(repos):
            p = ("repositories", index)
            if v.mapping(p, repo, ("name", "url", "branch", "credential", "push"), required=("name", "url")) is None:
                continue
            item = {"name": None, "url": None, "branch": None, "push": False, "credential": None}
            if "name" in repo:
                item["name"] = v.text(p + ("name",), repo["name"], FOLDER, max_len=255)
            if "url" in repo:
                item["url"] = v.text(p + ("url",), repo["url"], non_empty=True)
            if repo.get("branch") is not None:
                item["branch"] = v.text(p + ("branch",), repo["branch"], numbers=True)
            if repo.get("credential") is not None:
                item["credential"] = v.text(p + ("credential",), repo["credential"])
            if "push" in repo:
                item["push"] = v.boolean(p + ("push",), repo["push"])
            if item["name"] in folders:
                v.err(p + ("name",), "duplicate repository name %r" % item["name"])
            if item["name"]:
                folders.add(item["name"])
            if item["push"] and not item["credential"]:
                v.err(p + ("push",), "needs a credential")
            if item["credential"] and not SECRET_REF.search(item["credential"]):
                warnings.append("%s.credential: written in the file; use ${secret:NAME}" % fmt_path(p))
            cfg.repositories.append(item)

    def image(path, value, keys):
        item = {"image": None, "dockerfile": None, "context": None}
        if v.mapping(path, value, keys) is None:
            return None
        for key in ("image", "dockerfile"):
            if value.get(key) is not None:
                item[key] = v.text(path + (key,), value[key], non_empty=True)
        if value.get("context") is not None:
            item["context"] = v.text(path + ("context",), value["context"])
        if item["image"] and item["dockerfile"]:
            v.err(path, "give image or dockerfile, not both")
            return item
        if item["context"] is not None and not item["dockerfile"]:
            v.err(path + ("context",), "only with dockerfile")
        if item["dockerfile"]:
            dockerfile = repo_path(config_dir, item["dockerfile"])
            if dockerfile is None:
                v.err(path + ("dockerfile",), "must be a relative path inside the repository (no '..')")
            if item["context"] is not None:
                context = repo_path(config_dir, item["context"])
            else:
                context = posixpath.dirname(dockerfile or "")
            if context is None:
                v.err(path + ("context",), "must be a relative path inside the repository (no '..')")
            if lookup is not None:
                for key, rel, kind in (("dockerfile", dockerfile, "file"), ("context", context, "dir")):
                    if rel is None:
                        continue
                    found = lookup(rel)
                    if found == "link":
                        v.err(path + (key,), "%s is or passes through a symlink" % (rel or "."))
                    elif found is None:
                        v.err(path + (key,), "%s does not exist in the repository" % (rel or "."))
                    elif found != kind:
                        v.err(path + (key,), "%s is not a %s" % (rel or ".", "file" if kind == "file" else "directory"))
            item["dockerfile"], item["context"] = dockerfile, context
        return item

    station = raw.get("workstation")
    cfg.workstation = {"image": None, "dockerfile": None, "context": None, "setup": [], "cache": []}
    if "workstation" in raw:
        item = image(("workstation",), station, ("image", "dockerfile", "context", "setup", "cache"))
        if item is not None:
            cfg.workstation.update(item)
            if "setup" in station:
                cfg.workstation["setup"] = v.str_list(("workstation", "setup"), station["setup"], non_empty=True)
            if "cache" in station:
                seen = {}
                entries = station["cache"]
                if not isinstance(entries, list):
                    v.err(("workstation", "cache"), "must be a list")
                    entries = []
                for index, entry in enumerate(entries):
                    p = ("workstation", "cache", index)
                    text = v.text(p, entry, non_empty=True)
                    if text is None:
                        continue
                    target, reason = cache_target(text)
                    if target is None:
                        v.err(p, reason)
                    elif target in seen:
                        v.err(p, "%s is already listed (as %r)" % (target, seen[target]))
                    else:
                        seen[target] = text
                        cfg.workstation["cache"].append({"path": text, "target": target})
    default_station = not (cfg.workstation["image"] or cfg.workstation["dockerfile"])

    if "services" in raw:
        services = raw["services"]
        if not isinstance(services, dict):
            v.err(("services",), "must be a mapping")
            services = {}
        for name, service in services.items():
            p = ("services", name)
            if not isinstance(name, str) or not NAME.match(name) or len(name) > 63:
                v.err(p, "service names must match %s" % NAME.pattern)
            elif name in RESERVED_SERVICES or name.startswith("exradius"):
                v.err(p, "the name %r is reserved" % name)
            elif name in folders:
                v.err(p, "the name %r is also a repository folder" % name)
            item = image(p, service, ("image", "dockerfile", "context", "env", "ports", "ready", "memory", "command"))
            if item is None:
                continue
            if not (item["image"] or item["dockerfile"]) and not (service.get("image") or service.get("dockerfile")):
                v.err(p, "needs image or dockerfile")
            item["env"] = v.str_map(p + ("env",), service["env"], ENV_KEY) if "env" in service else {}
            ports = []
            if "ports" in service:
                if not isinstance(service["ports"], list):
                    v.err(p + ("ports",), "must be a list")
                else:
                    ports = [x for x in (v.port(p + ("ports", i), x) for i, x in enumerate(service["ports"])) if x]
                    if len(set(ports)) != len(ports):
                        v.err(p + ("ports",), "ports must be unique")
            item["ports"] = ports
            item["memory"] = memory(p + ("memory",), service["memory"]) if service.get("memory") is not None else None
            item["command"] = None
            if service.get("command") is not None:
                # list[str] (docs/run-environment.md §6): numbers become strings as in env;
                # empty arguments are real ones (--save "").
                item["command"] = v.str_list(p + ("command",), service["command"], numbers=True, min_len=1)
            item["ready"] = None
            ready = service.get("ready")
            if ready is not None:
                if v.mapping(p + ("ready",), ready, ("command", "tcp")) is not None:
                    command = ready.get("command")
                    tcp = ready.get("tcp")
                    if (command is None) == (tcp is None):
                        v.err(p + ("ready",), "give exactly one of command or tcp")
                    elif command is not None:
                        item["ready"] = {"command": v.str_list(p + ("ready", "command"), command,
                                                               non_empty=True, min_len=1)}
                    else:
                        port = v.port(p + ("ready", "tcp"), tcp)
                        item["ready"] = {"tcp": port} if port else None
            cfg.services[name] = item

    if "env" in raw:
        cfg.env = v.str_map(("env",), raw["env"], ENV_KEY)
    if "notes" in raw:
        v.text(("notes",), raw["notes"])

    connections = {}
    if "connections" in raw:
        connections = raw["connections"]
        if not isinstance(connections, dict):
            v.err(("connections",), "must be a mapping")
            connections = {}
        for name, conn in connections.items():
            p = ("connections", name)
            if not isinstance(name, str) or not NAME.match(name) or len(name) > 63:
                v.err(p, "connection names must match %s" % NAME.pattern)
                continue
            if name == "exradius":
                v.err(p, "the name 'exradius' is reserved for the built-in connection")
                continue
            keys = ("catalog", "config", "command", "args", "env", "url", "headers", "type")
            if v.mapping(p, conn, keys) is None:
                continue
            for key in ("catalog", "command", "url", "type"):
                if conn.get(key) is not None:
                    v.text(p + (key,), conn[key], non_empty=key in ("command", "url"))
            for key, pattern in (("config", None), ("env", ENV_KEY), ("headers", None)):
                if conn.get(key) is not None:
                    v.str_map(p + (key,), conn[key], pattern, non_empty_keys=key == "headers")
            if conn.get("args") is not None:
                v.str_list(p + ("args",), conn["args"], numbers=True)
            kinds = [k for k in ("catalog", "command", "url") if conn.get(k) is not None]
            if len(kinds) != 1:
                v.err(p, "give exactly one of catalog, command or url")
                continue
            allowed = {"catalog": ("config",), "command": ("args", "env"), "url": ("headers",)}[kinds[0]]
            for key in ("config", "args", "env", "headers"):
                if conn.get(key) is not None and key not in allowed:
                    v.err(p + (key,), "not used with %s" % kinds[0])
            launcher = None
            if conn.get("catalog") is not None:
                entry = CATALOG.get(conn["catalog"])
                if entry is None:
                    v.err(p + ("catalog",), "unknown catalog entry %r (known: %s)" % (conn["catalog"], ", ".join(CATALOG)))
                    continue
                given = conn.get("config") or {}
                for key in given:
                    if key not in entry[0] + entry[1]:
                        v.err(p + ("config", key), "unknown config key for catalog %s (known: %s)"
                              % (conn["catalog"], ", ".join(entry[0] + entry[1]) or "none"))
                for key in entry[0]:
                    if key not in given:
                        v.err(p + ("config", key), "required by catalog %s" % conn["catalog"])
                launcher = entry[2]
            elif conn.get("command") in LAUNCHERS:
                launcher = conn["command"]
            if launcher and default_station:
                warnings.append("%s: needs %s, which the default workstation image does not have" % (fmt_path(p), launcher))
            cfg.connections.append(name)

    if "agents" in raw:
        agents = raw["agents"]
        if not isinstance(agents, dict):
            v.err(("agents",), "must be a mapping")
            agents = {}
        elif not agents:
            v.err(("agents",), "at least one agent is required")
        for slug, agent in agents.items():
            p = ("agents", slug)
            if not isinstance(slug, str) or not NAME.match(slug) or len(slug) > 63:
                v.err(p, "agent names must match %s" % NAME.pattern)
            keys = ("name", "role", "model", "provider", "runtime", "identity", "connections")
            if v.mapping(p, agent, keys) is None:
                continue
            for key in ("name", "role", "provider"):
                if agent.get(key) is not None:
                    v.text(p + (key,), agent[key])
            if agent.get("model") is not None:
                v.text(p + ("model",), agent["model"], MODEL_ID, max_len=255)
            if "runtime" in agent and agent["runtime"] != "claude_code":
                v.err(p + ("runtime",), "must be claude_code")
            identity = agent.get("identity")
            if identity is not None and v.mapping(p + ("identity",), identity, ("name", "email")) is not None:
                if identity.get("name") is not None:
                    v.text(p + ("identity", "name"), identity["name"], IDENTITY_NAME, max_len=255)
                if identity.get("email") is not None:
                    v.text(p + ("identity", "email"), identity["email"], IDENTITY_EMAIL, max_len=255)
            seen = set()
            names = v.str_list(p + ("connections",), agent["connections"]) if agent.get("connections") is not None else []
            for index, name in enumerate(names):
                if name in seen:
                    v.err(p + ("connections", index), "%r is listed twice" % name)
                elif name not in connections:
                    v.err(p + ("connections", index), "no connection named %r" % name)
                seen.add(name)
            cfg.agents.append(slug)

    errors = v.errors + secret_errors
    if required and not raw.get("secret_store"):
        errors.append("secret_store: required, because the file references secrets (%s)" % ", ".join(required))
    return (None if errors else cfg), errors, warnings, required


# ---------------------------------------------------------------------------------------
# Docker: the run's conditions
# ---------------------------------------------------------------------------------------
#
# These flags mirror backend/exradius/worker/containers.py in the Exradius repository
# (ContainerManager._create, _start_service, _wait_ready). Change both together.

RUN_UID, RUN_GID = 1000, 1000
WORK_DIR = "/work"
HOME_DIR = "/home/runner"
STATE_DIR = "/exradius/state"
GIT_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
SERVICE_CAPS = ("CHOWN", "SETUID", "SETGID", "FOWNER", "DAC_OVERRIDE", "NET_BIND_SERVICE")
SERVICE_READY_TIMEOUT = 120
SERVICE_POLL_INTERVAL = 1.0
SERVICE_RUNNING_GRACE = 1.0
PASSWD = "root:x:0:0:root:/root:/bin/sh\nrunner:x:%d:%d::%s:/bin/bash\n" % (RUN_UID, RUN_GID, HOME_DIR)
GROUP = "root:x:0:\nrunner:x:%d:\n" % RUN_GID
DEFAULT_IMAGE_DOCKERFILE = """\
# Stand-in for exradius-project-default, the image a project without a workstation gets.
FROM ubuntu:24.04
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git curl ca-certificates build-essential \\
 && rm -rf /var/lib/apt/lists/*
"""
DEFAULT_IMAGE_TAG = "exradius-check/default:ubuntu-24.04"

IMAGE_PROBE = r"""
if [ -f /etc/alpine-release ]; then echo "alpine: $(cat /etc/alpine-release)"; fi
v=$(getconf GNU_LIBC_VERSION 2>/dev/null)
if [ -z "$v" ]; then
  for f in /lib/*/libc.so.6 /lib64/libc.so.6 /lib/libc.so.6 /usr/lib/*/libc.so.6 /usr/lib64/libc.so.6; do
    [ -e "$f" ] && v="glibc (libc.so.6 at $f)" && break
  done
fi
if [ -n "$v" ]; then echo "glibc: $v"; else echo "glibc: missing"; fi
for t in bash git; do
  p=$(command -v "$t" 2>/dev/null) && echo "$t: $p" || echo "$t: missing"
done
command -v git >/dev/null 2>&1 && echo "git-version: $(git --version 2>&1)"
echo "arch: $(uname -m)"
echo "id: $(id 2>&1)"
echo "home: $HOME"
echo "PATH: $PATH"
for t in node npm npx corepack yarn pnpm bun python3 python pip uv uvx poetry java mvn gradle go cargo rustc ruby bundle php composer dotnet make gcc psql; do
  p=$(command -v "$t" 2>/dev/null) && echo "tool: $t $p"
done
true
"""


def docker_env(extra=None):
    env = dict(os.environ)
    env["DOCKER_CLI_HINTS"] = "false"
    if extra:
        env.update(extra)
    return env


def run(cmd, env=None, timeout=None, input_text=None):
    """``(exit code, combined output)``."""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env or docker_env(),
                              timeout=timeout, input=input_text.encode() if input_text is not None else None)
    except subprocess.TimeoutExpired as exc:
        out = exc.output.decode(errors="replace") if exc.output else ""
        return -1, out + "\n(timed out after %ss)" % timeout
    except OSError as exc:
        return -1, str(exc)
    return proc.returncode, proc.stdout.decode(errors="replace")


def stream(cmd, log, verbose, env=None, input_text=None):
    """Run ``cmd``, writing its output to ``log`` (and the terminal with ``verbose``);
    ``(exit code, output)``."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env or docker_env(),
                                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL)
    except OSError as exc:
        return -1, str(exc)
    if input_text is not None:
        proc.stdin.write(input_text.encode())
        proc.stdin.close()
    chunks = []
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode(errors="replace")
        chunks.append(line)
        log.write(line)
        log.flush()
        if verbose:
            sys.stdout.write("      | " + line)
            sys.stdout.flush()
    proc.wait()
    return proc.returncode, "".join(chunks)


def tail(text, lines=60):
    rows = text.rstrip().splitlines()
    if len(rows) <= lines:
        return "\n".join(rows)
    return "... (%d lines above)\n" % (len(rows) - lines) + "\n".join(rows[-lines:])


def listening_ports(proc_net):
    ports = set()
    for line in proc_net.splitlines():
        fields = line.split()
        if len(fields) > 3 and fields[3] == "0A" and ":" in fields[1]:
            try:
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
            except ValueError:
                continue
    return ports


class StepFailed(Exception):
    def __init__(self, summary, output=""):
        super().__init__(summary)
        self.summary = summary
        self.output = output


# ---------------------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------------------


def git(args, cwd):
    cmd = ["git"] + (["-C", cwd] if cwd else []) + list(args)
    code, out = run(cmd, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    return code, out.strip()


def find_config(target):
    """``(file path, repository root, config dir from the root)``."""
    target = os.path.abspath(target)
    if os.path.isfile(target):
        path = target
    else:
        for rel in ("exradius.yaml", os.path.join(".exradius", "exradius.yaml")):
            candidate = os.path.join(target, rel)
            if os.path.isfile(candidate):
                path = candidate
                break
        else:
            raise SystemExit("no exradius.yaml or .exradius/exradius.yaml in %s" % target)
    directory = os.path.dirname(path)
    code, top = git(["rev-parse", "--show-toplevel"], directory)
    root = os.path.realpath(top) if code == 0 and top else os.path.realpath(directory)
    rel = os.path.relpath(os.path.realpath(directory), root)
    return path, root, "" if rel == "." else rel.replace(os.sep, "/")


def make_lookup(root):
    def lookup(rel):
        current = root
        for part in [p for p in rel.split("/") if p]:
            current = os.path.join(current, part)
            if os.path.islink(current):
                return "link"
        if os.path.isfile(current):
            return "file"
        if os.path.isdir(current):
            return "dir"
        return None

    return lookup


def normalise_url(url):
    url = url.strip().rstrip("/")
    url = re.sub(r"\.git$", "", url)
    url = re.sub(r"^[a-z+]+://([^@/]+@)?", "", url)
    url = re.sub(r"^[^@/]+@([^:/]+):", r"\1/", url)
    return url.lower()


class Check(object):
    def __init__(self, args):
        self.args = args
        self.id = _random.token_hex(4)
        self.label = "exradius-check=%s" % self.id
        self.steps = []  # (name, state, seconds, summary)
        self.failure = None  # (name, summary, output)
        self.root = None
        self.network = None
        self.secrets = {}
        self.log = None
        self.log_path = None
        self.verbose = args.verbose
        self.cleanup_image = None
        self.cache_dir = None

    # -- reporting ----------------------------------------------------------------------

    def say(self, text=""):
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def step(self, name, action):
        if self.failure is not None:
            self.steps.append((name, "skipped", 0.0, ""))
            self.say("SKIP  %s" % name)
            return None
        self.log.write("\n===== %s\n" % name)
        self.log.flush()
        if self.verbose:
            self.say("....  %s" % name)
        started = time.time()
        try:
            summary = action()
            state = "passed"
        except StepFailed as exc:
            state = "failed"
            summary = exc.summary
            self.failure = (name, exc.summary, exc.output)
            self.log.write(exc.output + "\n" + exc.summary + "\n")
        except KeyboardInterrupt:
            self.failure = (name, "interrupted", "")
            self.steps.append((name, "failed", time.time() - started, "interrupted"))
            raise
        seconds = time.time() - started
        self.steps.append((name, state, seconds, summary or ""))
        mark = "PASS" if state == "passed" else "FAIL"
        timing = " (%.1fs)" % seconds if seconds >= 0.5 else ""
        self.say("%s  %s%s" % (mark, name, timing))
        if summary:
            for line in str(summary).splitlines():
                self.say("      %s" % line)
        return state == "passed"

    # -- main ---------------------------------------------------------------------------

    def main(self):
        args = self.args
        path, repo_root, config_dir = find_config(args.config)
        self.config_path, self.repo_root, self.config_dir = path, repo_root, config_dir
        log_dir = tempfile.gettempdir()
        self.log_path = os.path.join(log_dir, "exradius-check-%s.log" % self.id)
        self.log = open(self.log_path, "w")
        self.say("exradius check: %s" % path)
        try:
            self.run_steps()
        except KeyboardInterrupt:
            self.say("\ninterrupted; cleaning up")
        finally:
            if not args.validate:
                self.cleanup()
        return self.report()

    def run_steps(self):
        self.step("Read config", self.read_config)
        if self.args.validate or self.failure:
            return
        code, out = run(["docker", "version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"])
        if code != 0:
            self.failure = ("Docker", "the Docker daemon is not reachable", out)
            return
        self.say("      docker %s" % out.strip())
        self.images = {}
        self.prepare_images()
        self.step("Check workstation image", self.check_image)
        self.step("Prepare /work", self.prepare_work)
        for name in self.config.services:
            self.step("Start service %s" % name, lambda n=name: self.start_service(n))
        for command in self.config.workstation["setup"]:
            self.step("Setup: %s" % command, lambda c=command: self.setup(c))

    def report(self):
        self.say("")
        if self.failure is None and self.args.validate:
            self.say("RESULT: the file is valid (only read and validated; no Docker steps ran).")
            code = 0
        elif self.failure is None:
            self.say("RESULT: passed. The setup commands ran under the run's conditions.")
            if not self.args.validate:
                self.say("Not covered locally: the Exradius runtime, providers, pushes, the server's CPU "
                         "architecture and its network.")
            code = 0
        else:
            name, summary, output = self.failure
            self.say("RESULT: failed at step \"%s\": %s" % (name, summary))
            if output and output.strip():
                self.say("")
                self.say("Output of the failing step (last lines):")
                self.say(tail(output, self.args.tail))
            code = 1
        if self.log is not None:
            self.log.close()
            self.say("")
            self.say("Full log: %s" % self.log_path)
        return code

    # -- steps --------------------------------------------------------------------------

    def read_config(self):
        with open(self.config_path, encoding="utf-8") as handle:
            text = handle.read()
        try:
            raw = load_yaml(text)
        except YamlError as exc:
            raise StepFailed("not valid YAML: %s" % exc)
        config, errors, warnings, required = validate(raw, self.config_dir, make_lookup(self.repo_root))
        lines = []
        code, commit = git(["log", "-1", "--format=%h %s"], self.repo_root)
        if code == 0 and commit:
            lines.append("config repository %s, commit %s" % (self.repo_root, commit))
            rel = os.path.relpath(self.config_path, self.repo_root)
            paths = [rel]
            if config is not None:
                for item in [config.workstation] + list(config.services.values()):
                    if item.get("dockerfile"):
                        paths += [item["dockerfile"], item["context"] or "."]
            code, status = git(["status", "--porcelain", "--"] + sorted(set(paths)), self.repo_root)
            contexts = sorted(set(paths[2::2]))
            if contexts:
                code_i, ignored = git(["ls-files", "--others", "--ignored", "--exclude-standard", "--"] + contexts,
                                      self.repo_root)
                if code_i == 0 and ignored:
                    shown = [l for l in ignored.splitlines() if not l.startswith(CACHE_DIR_NAME + "/")
                             and ("/" + CACHE_DIR_NAME + "/") not in l]
                    if shown:
                        warnings.append("ignored files in a build context: a local build sees them, Exradius "
                                        "builds from the repository and does not:\n  " + "\n  ".join(shown[:10])
                                        + ("\n  ..." if len(shown) > 10 else ""))
            if code == 0 and status:
                warnings.append("not committed yet (Exradius reads the pushed branch head):\n  "
                                + status.replace("\n", "\n  "))
        else:
            lines.append("%s is not in a git repository yet" % self.repo_root)
        lines.append("file: %s" % (os.path.relpath(self.config_path, self.repo_root)))
        if required:
            lines.append("secret store %s, required keys: %s" % (raw.get("secret_store") if isinstance(raw, dict) else None,
                                                                  ", ".join(required)))
        else:
            lines.append("required keys: none")
        lines += ["warning: %s" % w for w in warnings]
        if errors:
            raise StepFailed("%d error%s in the file" % (len(errors), "" if len(errors) == 1 else "s"),
                             "\n".join(lines + ["error: %s" % e for e in errors]))
        self.config = config
        self.secrets = {name: "chk" + _random.token_hex(10) for name in required}
        if config.workstation["cache"]:
            self.cache_dir = os.path.realpath(self.args.cache_dir or os.path.join(os.path.dirname(self.config_path),
                                                                                  CACHE_DIR_NAME))
            lines.append("cache: %s: %s" % (self.cache_dir, cache_summary(self.cache_dir, config.workstation["cache"])))
            for item in [config.workstation] + list(config.services.values()):
                context = item.get("context")
                if item.get("dockerfile") and context is not None:
                    root = os.path.realpath(os.path.join(self.repo_root, context))
                    if self.cache_dir == root or self.cache_dir.startswith(root + os.sep):
                        lines.append("warning: the cache directory is inside the build context %s; docker build "
                                     "would send it to the daemon: add %s to a .dockerignore there, or pass "
                                     "--cache-dir" % (context or ".", os.path.relpath(self.cache_dir, root)))
        if config.memory or any(s["memory"] for s in config.services.values()):
            parts = ["workstation %s" % (config.memory or "unlimited")]
            parts += ["%s %s" % (n, s["memory"]) for n, s in config.services.items() if s["memory"]]
            lines.append("memory: %s" % ", ".join(parts))
        if self.args.validate:
            lines.append("agents: %s" % ", ".join(config.agents))
        return "\n".join(lines)

    def platform_args(self):
        return ["--platform", self.args.platform] if self.args.platform else []

    def prepare_images(self):
        cfg = self.config
        station = cfg.workstation
        if station["dockerfile"]:
            self.step("Build workstation", lambda: self.build("workstation", station))
        elif station["image"]:
            self.step("Pull workstation image %s" % station["image"], lambda: self.pull("workstation", station["image"]))
        else:
            self.step("Build default workstation (stand-in for exradius-project-default)", self.build_default)
        for name, service in cfg.services.items():
            if service["dockerfile"]:
                self.step("Build service %s" % name, lambda n=name, s=service: self.build(n, s))
            else:
                self.step("Pull service image %s (%s)" % (name, service["image"]),
                          lambda n=name, s=service: self.pull(n, s["image"]))

    def build(self, name, item):
        tag = "exradius-check/%s/%s:latest" % (self.config.slug, name)
        dockerfile = os.path.join(self.repo_root, item["dockerfile"])
        context = os.path.join(self.repo_root, item["context"] or "")
        # The Exradius worker builds through the Engine API's classic builder (no BuildKit),
        # so BuildKit-only syntax (RUN --mount, heredocs) fails there; build the same way.
        cmd = ["docker", "build", "-f", dockerfile, "-t", tag, "--label", "exradius.build=%s" % name]
        cmd += self.platform_args()
        if self.args.no_cache:
            cmd.append("--no-cache")
        cmd.append(context)
        env = docker_env({"DOCKER_BUILDKIT": "0"} if not self.args.buildkit else None)
        code, out = stream(cmd, self.log, self.verbose, env=env)
        if code != 0:
            raise StepFailed("building %s from %s failed (exit %d)%s" % (
                name, item["dockerfile"], code, failed_instruction(out)), out)
        self.images[name] = tag
        return "built %s from %s (context %s)" % (tag, item["dockerfile"], item["context"] or ".")

    def build_default(self):
        cmd = ["docker", "build", "-t", DEFAULT_IMAGE_TAG] + self.platform_args() + ["-"]
        code, out = stream(cmd, self.log, self.verbose, env=docker_env({"DOCKER_BUILDKIT": "0"}),
                           input_text=DEFAULT_IMAGE_DOCKERFILE)
        if code != 0:
            raise StepFailed("building the default workstation stand-in failed (exit %d)" % code, out)
        self.images["workstation"] = DEFAULT_IMAGE_TAG
        return "no workstation image or dockerfile: Ubuntu 24.04 with git, curl, ca-certificates, build-essential"

    def pull(self, name, image):
        code, _ = run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        if code == 0 and not self.args.pull:
            self.images[name] = image
            return "%s is present locally" % image
        code, out = stream(["docker", "pull"] + self.platform_args() + [image], self.log, self.verbose)
        if code != 0:
            raise StepFailed("could not pull %s" % image, out)
        self.images[name] = image
        return "pulled %s" % image

    def run_flags(self, extra_mounts=()):
        """The setup container's conditions (ContainerManager._create)."""
        flags = [
            "--user", "%d:%d" % (RUN_UID, RUN_GID),
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--label", self.label,
            "--workdir", WORK_DIR,
        ]
        mounts = [
            (self.work, WORK_DIR, False),
            (os.path.join(self.work, ".exradius", "attachments"), WORK_DIR + "/.exradius/attachments", True),
            (self.state, STATE_DIR, False),
            (self.home, HOME_DIR, False),
            (self.tmp, "/tmp", False),
            (self.passwd, "/etc/passwd", True),
            (self.group, "/etc/group", True),
        ] + self.cache_mounts() + list(extra_mounts)
        for source, target, read_only in mounts:
            flags += ["--mount", "type=bind,source=%s,target=%s%s" % (source, target, ",readonly" if read_only else "")]
        flags += memory_flags(self.config.memory)
        return flags

    def cache_mounts(self):
        """``workstation.cache``: one folder per entry under the local cache directory,
        shared by every check of this config (on the server: <data_dir>/caches/<slug>/)."""
        mounts = []
        for index, entry in enumerate(self.config.workstation["cache"]):
            source = os.path.join(self.cache_dir, cache_folder(index, entry["target"]))
            if not os.path.isdir(source):
                os.makedirs(source)
                os.chmod(source, 0o777)
            mounts.append((source, entry["target"], False))
        return mounts

    def check_image(self):
        self.make_root()
        image = self.images["workstation"]
        cmd = ["docker", "run", "--rm"] + self.run_flags() + self.platform_args() + \
            ["--entrypoint", "/bin/sh", image, "-c", IMAGE_PROBE]
        code, out = run(cmd, timeout=120)
        self.log.write(out)
        if code != 0:
            raise StepFailed("could not run /bin/sh in %s as uid %d with a read-only root filesystem (exit %d)"
                             % (image, RUN_UID, code), out)
        facts = {}
        tools = []
        for line in out.splitlines():
            key, _, value = line.partition(": ")
            if key == "tool":
                tools.append(value.split(" ")[0])
            elif key:
                facts[key] = value
        problems = []
        if "alpine" in facts:
            problems.append("Alpine Linux %s (musl): use a Debian or Ubuntu based image" % facts["alpine"])
        if facts.get("glibc", "missing") == "missing":
            problems.append("no glibc found (musl and other libcs are not supported)")
        for tool in ("bash", "git"):
            if facts.get(tool, "missing") == "missing":
                problems.append("%s is not on PATH" % tool)
        summary = "%s; bash %s; %s; %s" % (facts.get("glibc", "?"), facts.get("bash", "?"),
                                           facts.get("git-version", "git ?"), facts.get("arch", "?"))
        detail = summary + "\nPATH=%s\ntools on PATH: %s" % (facts.get("PATH", "?"), " ".join(tools) or "none")
        if problems:
            raise StepFailed("; ".join(problems), out)
        return detail

    def make_root(self):
        if self.root is not None:
            return
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="exradius-check-%s-" % self.id))
        self.work = os.path.join(self.root, "work")
        self.home = os.path.join(self.root, "home")
        self.tmp = os.path.join(self.root, "tmp")
        self.state = os.path.join(self.root, "state")
        etc = os.path.join(self.root, "etc")
        for d in (self.work, os.path.join(self.work, ".exradius", "attachments"), self.home, self.tmp,
                  self.state, etc):
            os.makedirs(d)
        for d in (self.work, self.home, self.state):
            os.chmod(d, 0o777)
        os.chmod(self.tmp, 0o1777)
        os.chmod(os.path.join(self.work, ".exradius"), 0o755)
        self.passwd = os.path.join(etc, "passwd")
        self.group = os.path.join(etc, "group")
        for target, text in ((self.passwd, PASSWD), (self.group, GROUP)):
            with open(target, "w") as handle:
                handle.write(text)
            os.chmod(target, 0o644)
        self.live_mounts = []

    def source_for(self, repo):
        name = repo["name"]
        if name in self.args.repo_map:
            return self.args.repo_map[name]
        for base in self.args.repos_dir:
            candidate = os.path.join(base, name)
            if os.path.isdir(candidate):
                return candidate
        # A config kept inside the code repository: that checkout is the repository.
        code, origin = git(["remote", "get-url", "origin"], self.repo_root)
        if code == 0 and origin and normalise_url(origin) == normalise_url(repo["url"]):
            return self.repo_root
        return None

    def prepare_work(self):
        self.make_root()
        lines = []
        warnings = []
        for repo in self.config.repositories:
            name = repo["name"]
            dest = os.path.join(self.work, name)
            source = self.source_for(repo)
            if source is not None:
                source = os.path.abspath(source)
                code, head = git(["rev-parse", "--abbrev-ref", "HEAD"], source)
                if code != 0:
                    raise StepFailed("%s is not a git checkout" % source, head)
                code, short = git(["log", "-1", "--format=%h %s"], source)
                if repo["branch"] and head != repo["branch"]:
                    warnings.append("%s: the checkout is on %s, the config names %s" % (name, head, repo["branch"]))
                code, dirty = git(["status", "--porcelain", "--untracked-files=no"], source)
                if self.args.live:
                    os.makedirs(dest)
                    self.live_mounts.append((source, WORK_DIR + "/" + name, False))
                    lines.append("%s: %s mounted as it is (%s, %s)" % (name, source, head, short))
                    continue
                # Hard links share the object files with the checkout; not where the copy's
                # modes are changed below (Linux with a uid other than the run's).
                links = [] if not self.must_open_up() else ["--no-hardlinks"]
                code, out = git(["clone", "--quiet"] + links + [source, dest], None)
                if code != 0:
                    raise StepFailed("could not copy %s" % source, out)
                git(["remote", "set-url", "origin", repo["url"]], dest)
                if dirty:
                    warnings.append("%s: uncommitted changes in %s are not included (--live mounts the checkout "
                                    "as it is)" % (name, source))
                lines.append("%s: fresh clone of %s (%s, %s)" % (name, source, head, short))
            else:
                cmd = ["clone", "--quiet"] + (["--branch", repo["branch"]] if repo["branch"] else []) + [repo["url"], dest]
                self.log.write("git %s\n" % " ".join(cmd))
                code, out = git(cmd, None)
                if code != 0:
                    raise StepFailed("could not clone %s (no local checkout given with --repo %s=PATH)"
                                     % (repo["url"], name), out)
                code, short = git(["log", "-1", "--format=%h %s"], dest)
                lines.append("%s: cloned %s (%s, %s)" % (name, repo["url"], repo["branch"] or "default branch", short))
            if self.must_open_up():
                # The run user (uid 1000) must be able to write the clone, as on the server.
                run(["chmod", "-R", "a+rwX", dest])
        if self.args.live and self.must_open_up():
            warnings.append("--live on Linux with uid %d: the run user (uid %d) may not be able to write the "
                            "mounted checkouts" % (os.getuid(), RUN_UID))
        if not self.config.repositories:
            lines.append("no repositories")
        return "\n".join(lines + ["warning: %s" % w for w in warnings])

    @staticmethod
    def must_open_up():
        """Linux bind mounts keep the host's owners; Docker Desktop maps them to the
        container's user."""
        return platform.system() == "Linux" and os.getuid() != RUN_UID

    def service_env(self, service):
        return {k: SECRET_REF.sub(lambda m: self.secrets[m.group(1)], v) for k, v in service["env"].items()}

    def workstation_env(self):
        env = {}
        for service in self.config.services.values():
            for value in service["env"].values():
                for name in SECRET_REF.findall(value):
                    if ENV_KEY.match(name):
                        env[name] = self.secrets[name]
        env.update({k: SECRET_REF.sub(lambda m: self.secrets[m.group(1)], v) for k, v in self.config.env.items()})
        return env

    def start_service(self, name):
        service = self.config.services[name]
        image = self.images[name]
        if self.network is None:
            self.network = "exradius-check-%s" % self.id
            code, out = run(["docker", "network", "create", "--driver", "bridge", "--label", self.label, self.network])
            if code != 0:
                self.network = None
                raise StepFailed("could not create the services network", out)
        env = self.service_env(service)
        container = "exradius-check-%s-%s" % (self.id, name)
        cmd = ["docker", "run", "-d", "--name", container, "--hostname", name, "--network", self.network,
               "--network-alias", name, "--cap-drop", "ALL"]
        for cap in SERVICE_CAPS:
            cmd += ["--cap-add", cap]
        cmd += ["--security-opt", "no-new-privileges", "--label", self.label] + self.platform_args()
        for key, value in env.items():
            cmd += ["-e", "%s=%s" % (key, value)]
        cmd += memory_flags(service["memory"])
        # ``command`` replaces the image's CMD; its ENTRYPOINT is kept.
        cmd.append(image)
        cmd += list(service["command"] or [])
        code, out = run(cmd)
        if code != 0:
            raise StepFailed("service %s (%s) could not start" % (name, image), out)
        started = time.time()
        ready = service["ready"] or {}
        if "tcp" in ready:
            check = "port %d listening" % ready["tcp"]
        elif "command" in ready:
            check = "command " + " ".join(ready["command"])
        else:
            code, health = run(["docker", "inspect", "--format", "{{json .Config.Healthcheck}}", container])
            test = []
            try:
                test = (json.loads(health) or {}).get("Test") or []
            except ValueError:
                pass
            check = "image healthcheck" if test and test[0] != "NONE" else "running"
        last = ""
        timeout = SERVICE_READY_TIMEOUT
        while True:
            code, state_json = run(["docker", "inspect", "--format", "{{json .State}}", container])
            try:
                state = json.loads(state_json)
            except ValueError:
                state = {}
            if state.get("Status") in ("exited", "dead") or (state and not state.get("Running", True)):
                if state.get("OOMKilled") and service["memory"]:
                    raise StepFailed("service %s exceeded its declared memory (%s)" % (name, service["memory"]),
                                     self.service_logs(container))
                raise StepFailed("service %s (%s) exited with code %s before it was ready"
                                 % (name, image, state.get("ExitCode")), self.service_logs(container))
            if "tcp" in ready:
                code, out = run(["docker", "exec", container, "cat", "/proc/net/tcp", "/proc/net/tcp6"])
                if ready["tcp"] in listening_ports(out):
                    break
                last = "" if code == 0 else " (last check: exit %d: %s)" % (code, out.strip()[:200])
            elif "command" in ready:
                code, out = run(["docker", "exec", container] + list(ready["command"]))
                if code == 0:
                    break
                last = " (last check: exit %d: %s)" % (code, out.strip()[-300:])
            elif check == "image healthcheck":
                health = (state.get("Health") or {}).get("Status")
                if health == "healthy":
                    break
                last = " (health: %s)" % health
            elif time.time() - started >= SERVICE_RUNNING_GRACE:
                break
            if time.time() - started >= timeout:
                raise StepFailed("service %s (%s) did not become ready within %ds waiting for %s%s"
                                 % (name, image, timeout, check, last), self.service_logs(container))
            time.sleep(SERVICE_POLL_INTERVAL)
        extra = "".join([", memory %s" % service["memory"] if service["memory"] else "",
                         ", command %s" % " ".join(service["command"])[:80] if service["command"] else ""])
        return "%s ready in %.1fs (%s), reachable as %s%s" % (image, time.time() - started, check, name, extra)

    def service_logs(self, container):
        code, out = run(["docker", "logs", "--tail", "40", container])
        return out

    def setup(self, command):
        image = self.images["workstation"]
        env = self.workstation_env()
        env.update({"HOME": HOME_DIR, "EXRADIUS_STATE_DIR": STATE_DIR, "EXRADIUS_WORK_DIR": WORK_DIR})
        env.update(GIT_ENV)
        container = "exradius-check-%s-setup-%s" % (self.id, _random.token_hex(3))
        cmd = ["docker", "run", "--name", container] + self.run_flags(self.live_mounts) + self.platform_args()
        if self.network:
            cmd += ["--network", self.network]
        for key, value in env.items():
            # Explicit values (throwaway secrets, the project env): passing them through the
            # docker CLI's own environment would also change its HOME and PATH.
            cmd += ["-e", "%s=%s" % (key, value)]
        cmd += ["--entrypoint", "/bin/sh", image, "-c", command]
        started = time.time()
        code, out = stream(cmd, self.log, self.verbose)
        seconds = time.time() - started
        _, oom = run(["docker", "inspect", "--format", "{{.State.OOMKilled}}", container])
        run(["docker", "rm", "-f", container])
        if code != 0:
            hint = ""
            if oom.strip() == "true" and self.config.memory:
                hint = " (exceeded its declared memory (%s): raise resources.memory)" % self.config.memory
            elif code == 137:
                hint = " (killed: usually out of memory; Docker has %s)" % docker_memory()
            elif code == 127:
                hint = " (command not found: the workstation image lacks a tool this command runs)"
            elif code == 125:
                hint = " (docker could not start the container)"
            raise StepFailed("exited with %d after %.0fs%s" % (code, seconds, hint), out)
        last = [l for l in out.strip().splitlines() if l.strip()][-1:] if out.strip() else []
        return ("last line: %s" % last[0][:200]) if last else ""

    def cleanup(self):
        ids = run(["docker", "ps", "-aq", "--filter", "label=" + self.label])[1].split()
        if ids:
            run(["docker", "rm", "-f", "-v"] + ids)
        if self.network:
            run(["docker", "network", "rm", self.network])
        if self.root and os.path.isdir(self.root):
            if self.args.keep:
                self.say("DONE  Clean up: kept %s (work, home, tmp) as asked" % self.root)
                self.root = None
        if self.root and os.path.isdir(self.root):
            try:
                shutil.rmtree(self.root)
            except OSError:
                # Files the run user wrote may not be removable by this user (Linux): remove
                # them as root in a throwaway container.
                image = self.images.get("workstation") if hasattr(self, "images") else None
                if image:
                    run(["docker", "run", "--rm", "--user", "0:0", "--mount",
                         "type=bind,source=%s,target=/x" % self.root, "--entrypoint", "/bin/sh", image,
                         "-c", "rm -rf /x/* /x/.[!.]*"])
                shutil.rmtree(self.root, ignore_errors=True)
        removed = []
        if ids:
            removed.append("%d container%s" % (len(ids), "" if len(ids) == 1 else "s"))
        if self.network:
            removed.append("network %s" % self.network)
        if self.root:
            removed.append("the temporary folder")
        if removed:
            self.say("DONE  Clean up: removed %s" % ", ".join(removed))


CACHE_DIR_NAME = ".exradius-cache"
"""The local stand-in for <data_dir>/caches/<slug>/, next to exradius.yaml (git-ignored)."""


def memory_flags(memory):
    """A declared memory is the container's cgroup limit, swap included (run-environment §4)."""
    return ["--memory", memory, "--memory-swap", memory] if memory else []


def cache_summary(cache_dir, entries):
    """The size of each ``workstation.cache`` folder: ``~/.cache/uv 236 MB, ...``."""
    parts = []
    for index, entry in enumerate(entries):
        folder = os.path.join(cache_dir, cache_folder(index, entry["target"]))
        size = 0
        if os.path.isdir(folder):
            code, out = run(["du", "-sk", folder])
            first = out.split("\t")[0].strip()
            size = int(first) * 1024 if code == 0 and first.isdigit() else 0
        parts.append("%s %s" % (entry["path"], "%.0f MB" % (size / 1e6) if size > 1e6 else "empty"))
    return ", ".join(parts)


def failed_instruction(output):
    """The Dockerfile step that failed and its first error line, from a classic build log."""
    step = None
    for line in output.splitlines():
        if line.startswith("Step ") and "/" in line.split(":")[0]:
            step = line.strip()
    error = None
    lines = output.splitlines()
    if step:
        after = lines[[i for i, l in enumerate(lines) if l.strip() == step][-1] + 1:]
        for line in after:
            low = line.lower()
            if any(word in low for word in ("error", "not found", "failed", "denied", "requires",
                                            "unable", "no such", "e: ", "cannot")):
                error = line.strip()
                break
        if error is None:
            rest = [l.strip() for l in after if l.strip() and not l.startswith(" --->")]
            error = rest[-1] if rest else None
    if not step:
        return ""
    return ":\n%s%s" % (step, "\n" + error if error else "")


def docker_memory():
    code, out = run(["docker", "info", "--format", "{{.MemTotal}}"])
    try:
        return "%.1f GB of memory" % (int(out.strip()) / 1e9)
    except ValueError:
        return "an unknown amount of memory"


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="check.sh",
        description="Check an Exradius project config locally with Docker, the way Verify does.")
    parser.add_argument("config", nargs="?", default=".",
                        help="the config repository folder (with exradius.yaml or .exradius/exradius.yaml), "
                             "or the file itself; default: the current folder")
    parser.add_argument("--repo", action="append", default=[], metavar="NAME=PATH",
                        help="local checkout of the repository named NAME in the config (repeatable)")
    parser.add_argument("--repos-dir", action="append", default=[], metavar="DIR",
                        help="folder holding checkouts named like the config's repositories (repeatable)")
    parser.add_argument("--live", action="store_true",
                        help="mount the local checkouts as they are (uncommitted changes, existing "
                             "node_modules or .venv included) instead of a fresh clone of them")
    parser.add_argument("--validate", action="store_true", help="only read and validate the file; no Docker")
    parser.add_argument("--platform", help="build and run for this platform, e.g. linux/amd64 (the server's)")
    parser.add_argument("--pull", action="store_true", help="pull images named with image: even when present locally")
    parser.add_argument("--no-cache", action="store_true", help="build without the Docker layer cache")
    parser.add_argument("--buildkit", action="store_true",
                        help="build with BuildKit (Exradius builds without it; use only to compare)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary work, home and tmp folders")
    parser.add_argument("--cache-dir", metavar="DIR",
                        help="where workstation.cache folders live between checks (default: .exradius-cache "
                             "next to exradius.yaml; delete it to start cold)")
    parser.add_argument("-v", "--verbose", action="store_true", help="stream build and setup output")
    parser.add_argument("--tail", type=int, default=60, help="lines of output shown for the failing step")
    args = parser.parse_args(argv)
    args.repo_map = {}
    for item in args.repo:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            parser.error("--repo takes NAME=PATH, got %r" % item)
        path = os.path.expanduser(path)
        if not os.path.isdir(path):
            parser.error("--repo %s: %s is not a folder" % (name, path))
        args.repo_map[name] = path
    args.repos_dir = [os.path.expanduser(d) for d in args.repos_dir]
    if args.cache_dir:
        args.cache_dir = os.path.abspath(os.path.expanduser(args.cache_dir))
    return args


def main(argv=None):
    if sys.version_info < (3, 8):
        sys.stderr.write("exradius check needs Python 3.8 or later\n")
        return 2
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return Check(args).main()


if __name__ == "__main__":
    sys.exit(main())
