#!/usr/bin/env python3
"""Local check of an Exradius project config: what the admin panel's Verify does, with the
Docker CLI alone and no Exradius installed.

    exradius_check.py [options] <config dir or exradius.yaml>

Steps, in Verify's order: read and validate exradius.yaml (and print the hosts the agent
phase may reach); build the workstation and the service images; check the workstation image
(glibc, sh, bash, git); check that the stdio connections' commands are in it; put the code
repositories under /work; start the egress proxy; start the services and wait until they
are ready; run every setup command in the workstation under the run's conditions (with
``resources.memory`` as its memory limit and ``workstation.cache`` mounted from a local
cache directory that persists between checks); switch the proxy to the agent phase and run
each ``--run`` command and ``--run-file`` script the same way (all of them, once setup has
passed); summarise each step's peak memory, the disk the run used and what the proxy saw;
stop and remove everything but the cache. Failing steps are printed with their output.
/work, the home, /tmp and the state directory are Docker volumes, a Linux filesystem as the
run's PVC is (on macOS a host folder cannot hold Unix sockets).

Like an Exradius run on Kubernetes, the check is one "pod": a holder container owns the
network namespace, with every service name mapped to 127.0.0.1 in its /etc/hosts (the pod's
hostAliases), and the services and the setup commands join it with
``--network container:<holder>``. Services and the workstation share localhost, a ``tcp``
check connects to the pod's own address as the kubelet does, and a service has 120 s from
its start to become ready. The flags mirror the operator's pod builder
(operator/internal/podspec/podspec.go in the Exradius repository).

The network is emulated as in a run (docs/kubernetes.md §11): the pod sits on an internal
Docker network with no route out, and every container that joins it gets the proxy
variables the operator writes. The only way out is a forward proxy (this same script in its
``--egress-proxy`` mode, in a container on both networks): open in the setup phase, the
agent phase's host list afterwards, private addresses refused in both unless
``--allow-private`` names them. ``--open-network`` turns the emulation off.

Standard library only, Python 3.8 or later. The YAML reader below covers the YAML an
exradius.yaml uses (block and flow collections, quoted, plain and block scalars,
comments); anchors, aliases and tags are refused with a message.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import posixpath
import re
import secrets as _random
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.parse import urlsplit

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
RESERVED_SERVICES = ("api", "workstation", "localhost")
MAX_SERVICE_NAME = 55
"""A service's container in a run is ``service-<name>``, a DNS label of at most 63 characters."""
MEMORY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([bkmgBKMG])$")
"""A size with a unit: ``512m``, ``6g`` (binary units; ``MEMORY_PATTERN`` in Exradius)."""
MEMORY_UNITS = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
MEMORY_MIN = 6 * 1024 ** 2
"""The smallest memory a container can be given."""
CACHE_FORBIDDEN = ("/", "/home", "/home/runner", "/tmp", "/etc", "/usr", "/bin", "/lib", "/var", "/opt")
CACHE_FORBIDDEN_UNDER = ("/work", "/exradius", "/opt/exradius", "/proc", "/sys", "/dev", "/etc/passwd", "/etc/group")
"""``workstation.cache`` entries may not be the first set of paths, nor be or lie under the
second: the project directory, Exradius's own mounts, the kernel's file systems and the
generated identity files (docs/run-environment.md §5; ``cache_problem`` in Exradius)."""
NPM = ("registry.npmjs.org",)
PYPI = ("pypi.org", "files.pythonhosted.org")


def _key(key, label, target, name="", required=True, fmt="{}", default=None, host=False):
    """A catalog config key (``catalog.ConfigKey`` in Exradius)."""
    return {"key": key, "label": label, "target": target, "name": name, "required": required, "format": fmt,
            "default": default, "host": host}


CATALOG = {
    # A copy of backend/exradius/catalog.py: transport, command or url, config keys, the
    # launcher the default workstation lacks, and the hosts the agent phase may reach for it.
    "github": {"transport": "http", "command": None, "url": "https://api.githubcopilot.com/mcp/",
               "config": (_key("token", "GitHub token", "header", "Authorization", fmt="Bearer {}"),),
               "requires": None, "hosts": ()},
    "gitlab": {"transport": "stdio", "command": "npx", "url": None,
               "config": (_key("token", "GitLab personal access token", "env", "GITLAB_PERSONAL_ACCESS_TOKEN"),
                          _key("api_url", "GitLab API URL", "env", "GITLAB_API_URL", required=False,
                               default="https://gitlab.com/api/v4", host=True)),
               "requires": "npx", "hosts": NPM},
    "slack": {"transport": "stdio", "command": "npx", "url": None,
              "config": (_key("bot_token", "Slack bot token", "env", "SLACK_BOT_TOKEN"),
                         _key("team_id", "Slack team id", "env", "SLACK_TEAM_ID")),
              "requires": "npx", "hosts": NPM + ("slack.com",)},
    "sentry": {"transport": "stdio", "command": "npx", "url": None,
               "config": (_key("token", "Sentry access token", "env", "SENTRY_ACCESS_TOKEN"),),
               "requires": "npx", "hosts": NPM + ("sentry.io", "*.sentry.io")},
    "linear": {"transport": "http", "command": None, "url": "https://mcp.linear.app/mcp",
               "config": (_key("api_key", "Linear API key", "header", "Authorization", fmt="Bearer {}"),),
               "requires": None, "hosts": ()},
    "jira": {"transport": "stdio", "command": "uvx", "url": None,
             "config": (_key("url", "Jira URL", "env", "JIRA_URL", host=True),
                        _key("username", "Jira username", "env", "JIRA_USERNAME"),
                        _key("api_token", "Jira API token", "env", "JIRA_API_TOKEN")),
             "requires": "uvx", "hosts": PYPI},
    "fetch": {"transport": "stdio", "command": "uvx", "url": None, "config": (), "requires": "uvx", "hosts": PYPI},
    "playwright": {"transport": "stdio", "command": "npx", "url": None, "config": (), "requires": "npx", "hosts": NPM},
    "filesystem": {"transport": "stdio", "command": "npx", "url": None,
                   "config": (_key("path", "Directory", "arg", required=False, default="/work"),),
                   "requires": "npx", "hosts": NPM},
}
LAUNCHERS = ("npx", "uvx")
NPX_VALUE_FLAGS = ("-p", "--package", "-c", "--call", "-w", "--workspace")
UVX_VALUE_FLAGS = (
    "--from", "-w", "--with", "--with-editable", "--with-requirements", "-p", "--python",
    "-c", "--constraints", "--overrides", "-b", "--build-constraints", "--index",
    "--default-index", "-i", "--index-url", "--extra-index-url", "-f", "--find-links",
    "--index-strategy", "--keyring-provider", "--resolution", "--prerelease", "--exclude-newer",
    "--exclude-newer-package", "-P", "--upgrade-package", "--reinstall-package",
    "--refresh-package", "--no-build-package", "--no-binary-package", "-C", "--config-setting",
    "--link-mode", "--python-platform", "--torch-backend", "--env-file", "--cache-dir",
    "--directory", "--project", "--config-file", "--color", "--allow-insecure-host",
)
NPM_EXACT = re.compile(r"^[=v]?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
PYPI_EXACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?\s*===?\s*[A-Za-z0-9._+!-]+$")
SECRET_PLACES = (
    ("repositories", int, "credential"),
    ("services", str, "env", str),
    ("env", str),
    ("connections", str, "config", str),
    ("connections", str, "env", str),
    ("connections", str, "headers", str),
)


def _launcher_options(args, value_flags):
    """``(flag, value)`` pairs before the first positional argument, and the rest."""
    options = []
    index = 0
    while index < len(args) and args[index].startswith("-"):
        flag, eq, value = args[index].partition("=")
        if not eq and flag in value_flags and index + 1 < len(args):
            index += 1
            value = args[index]
        options.append((flag, value))
        index += 1
    return options, args[index:]


def _npm_unpinned(spec):
    if spec.startswith((".", "/", "~")) or ":" in spec or ("/" in spec and not spec.startswith("@")):
        return False  # a path, URL, git or alias spec: not a registry version
    name, _, version = spec[1:].partition("@") if spec.startswith("@") else spec.partition("@")
    return bool(name) and not NPM_EXACT.match(version)


def _pypi_unpinned(requirement):
    if requirement.startswith((".", "/", "~")) or "://" in requirement or " @ " in requirement:
        return False  # a path or a direct URL: not a registry version
    return not PYPI_EXACT.match(requirement.strip())


def unpinned_packages(command, args):
    """Registry packages an ``npx`` or ``uvx`` command runs without an exact version
    (``unpinned_packages`` in Exradius's catalog module)."""
    if command == "npx":
        options, rest = _launcher_options(args, NPX_VALUE_FLAGS)
        specs = [value for flag, value in options if flag in ("-p", "--package")]
        if not specs and rest and not any(flag in ("-c", "--call") for flag, _ in options):
            specs = [rest[0]]
        return [spec for spec in specs if _npm_unpinned(spec)]
    if command == "uvx":
        options, rest = _launcher_options(args, UVX_VALUE_FLAGS)
        unpinned = []
        source = next((value for flag, value in options if flag == "--from"), None)
        if source is not None:
            if _pypi_unpinned(source):
                unpinned.append(source)
        elif rest:
            name, at, version = rest[0].partition("@")
            if not at and _pypi_unpinned(name) or at and version in ("", "latest"):
                unpinned.append(rest[0])
        for flag, value in options:
            if flag in ("-w", "--with"):
                unpinned += [r for r in re.split(r",\s*(?=[A-Za-z0-9])", value) if _pypi_unpinned(r)]
        return unpinned
    return []


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
    path = posixpath.normpath(path.rstrip("/") or "/")
    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    for base in CACHE_FORBIDDEN_UNDER:
        if path == base or path.startswith(base + "/"):
            return None, ("%s is inside /work, which is the project directory; caches live outside it"
                          % path) if base == "/work" else "%s is reserved (%s)" % (path, base)
    if path in CACHE_FORBIDDEN:
        return None, "must name a directory below %s, not %s itself" % (path, path)
    return path, None


def cache_folder(index, entry):
    """The folder of cache entry ``index`` as the worker names it, ``caches/<name>`` on the
    project's volume (``worker/caches.py`` ``cache_specs``):
    ``~/.local/share/pnpm/store`` -> ``0-local_share_pnpm_store``."""
    text = entry[1:] if entry.startswith("~") else entry
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return "%d-%s" % (index, name[:80] or "cache")


DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
IPV4 = re.compile(r"^(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}$")


def _cut_suffix(text, suffix):
    return text[:-len(suffix)] if suffix and text.endswith(suffix) else text


def _cut_prefix(text, prefix):
    return text[len(prefix):] if text.startswith(prefix) else text


def host_key(entry):
    """A ``network.allow`` entry as it is compared: lower case, no trailing dot."""
    return _cut_suffix(entry.strip().lower(), ".")


def host_problem(entry):
    """Why a ``network.allow`` entry is not a host name (``pypi.org``, ``*.pythonhosted.org``,
    an IPv4 address), or None (``host_problem`` in Exradius, word for word)."""
    host = host_key(entry)
    if "://" in host or "/" in host:
        return "write a host name, not a URL (pypi.org, not https://pypi.org/simple)"
    if ":" in host:
        return "write the host without a port: every port of an allowed host is allowed"
    if host in ("*", "*.") or host.startswith("*.") and "*" in host[2:]:
        return "* stands for the subdomains of a name: *.example.com, never every host"
    if "*" in host and not host.startswith("*."):
        return "* is allowed only as a leading *. (*.example.com)"
    name = _cut_prefix(host, "*.")
    if name == "localhost" or name.startswith("127."):
        return "the run's own services are on localhost and need no entry"
    if IPV4.match(name):
        return "*. does not apply to an address" if host.startswith("*.") else None
    labels = name.split(".")
    if len(name) > 253 or not all(DNS_LABEL.match(label) for label in labels):
        return "%r is not a host name" % (entry,)
    return None


def url_host(url):
    """The host of a URL without a secret in it, or None (``network.url_host``)."""
    if not url or SECRET_REF.search(url):
        return None
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def connection_definition(conn):
    """A connection as transport, command, args, url, env and headers, a catalog one
    expanded (``ProjectConfig.connection_definition`` and ``catalog.expand``)."""
    if conn.get("catalog"):
        item = CATALOG[conn["catalog"]]
        given = conn.get("config") or {}
        env, headers, args = {}, {}, []
        for key in item["config"]:
            value = given.get(key["key"], key["default"])
            if value is None:
                continue
            written = key["format"].replace("{}", value)
            if key["target"] == "env":
                env[key["name"]] = written
            elif key["target"] == "header":
                headers[key["name"]] = written
            else:
                args.append(written)
        return {"transport": item["transport"], "command": item["command"], "args": args, "url": item["url"],
                "env": env, "headers": headers}
    return {"transport": "http" if conn.get("url") else "stdio", "command": conn.get("command"),
            "args": list(conn.get("args") or []), "url": conn.get("url"), "env": dict(conn.get("env") or {}),
            "headers": dict(conn.get("headers") or {})}


def proxied(definition):
    """An HTTP connection whose headers hold a secret: a run reaches it through the proxy's
    route, so its host is not on the agent phase's list (``network.proxied``)."""
    return definition["transport"] == "http" and any(
        SECRET_REF.search(str(value)) for value in (definition.get("headers") or {}).values())


def connection_hosts(conn):
    """The hosts one connection reaches besides a proxy route (``network.connection_hosts``)."""
    hosts = []
    definition = connection_definition(conn)
    if conn.get("catalog"):
        item = CATALOG[conn["catalog"]]
        hosts += list(item["hosts"])
        for key in item["config"]:
            if key["host"]:
                host = url_host((conn.get("config") or {}).get(key["key"], key["default"]))
                if host:
                    hosts.append(host)
    if definition["transport"] == "http" and not proxied(definition):
        host = url_host(definition.get("url"))
        if host:
            hosts.append(host)
    return hosts


def used_connections(cfg):
    """Every connection some agent lists, in the file's order (``network.agent_connections``
    without an agent: what a verify Run lists the tools of)."""
    return [name for name in cfg.connections if any(name in names for names in cfg.agent_connections.values())]


def allowed_hosts(cfg, install=()):
    """The agent phase's host list (``network.allowed_hosts(config, None, install)``): the
    install's ``egress.allow``, the config's ``network.allow``, the connections' hosts."""
    hosts = list(install) + list(cfg.network_allow)
    for name in used_connections(cfg):
        hosts += connection_hosts(cfg.connections[name])
    seen = set()
    out = []
    for host in hosts:
        key = host_key(host)
        if key and key not in seen:
            seen.add(key)
            out.append(host.strip())
    return out


def repository_hosts(cfg):
    """The hosts of the config's http(s) repository URLs. In a run git reaches them through
    the proxy's credential route; the local proxy lets the agent phase reach them instead."""
    hosts = []
    for repo in cfg.repositories:
        url = repo.get("url") or ""
        if re.match(r"^https?://", url, re.I):
            host = url_host(url)
            if host and host.lower() not in hosts:
                hosts.append(host.lower())
    return hosts


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
        self.agent_connections = {}  # agent slug -> the connection names it lists
        self.connections = {}  # name -> dict: catalog, config, command, args, env, url, headers
        self.network_allow = []  # network.allow, as written


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
           "env", "notes", "network", "connections", "agents")
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
            v.err(path, "must be a size with a unit, such as 512m or 6g")
            return None
        if size < MEMORY_MIN:
            v.err(path, "must be at least 6m")
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
        service_ports = {}
        for name, service in services.items():
            p = ("services", name)
            if not isinstance(name, str) or not NAME.match(name):
                v.err(p, "service names must match %s" % NAME.pattern)
            elif len(name) > MAX_SERVICE_NAME:
                v.err(p, "service names are at most %d characters" % MAX_SERVICE_NAME)
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
                    for port in dict.fromkeys(ports):
                        owner = service_ports.get(port)
                        if owner is not None and owner != name:
                            v.err(p + ("ports",), "%d is also a port of service %s; services share the run's "
                                  "localhost, so each port is one service's" % (port, owner))
                        service_ports.setdefault(port, name)
            item["ports"] = ports
            item["memory"] = memory(p + ("memory",), service["memory"]) if service.get("memory") is not None else None
            item["command"] = None
            if service.get("command") is not None:
                # Strict list[str] (docs/run-environment.md §6): numbers are refused, empty
                # arguments are real ones (--save "").
                item["command"] = v.str_list(p + ("command",), service["command"], min_len=1)
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

    network = raw.get("network")
    if "network" in raw and v.mapping(("network",), network, ("allow",)) is not None:
        if "allow" in network:
            hosts = set()
            entries = network["allow"]
            if not isinstance(entries, list):
                v.err(("network", "allow"), "must be a list")
                entries = []
            for index, entry in enumerate(entries):
                p = ("network", "allow", index)
                text = v.text(p, entry, non_empty=True)
                if text is None:
                    continue
                problem = host_problem(text)
                if problem:
                    v.err(p, problem)
                    continue
                key = host_key(text)
                if key in hosts:
                    v.err(p, "%r is listed twice" % (text,))
                hosts.add(key)
                cfg.network_allow.append(text)

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
                known = [k["key"] for k in entry["config"]]
                for key in given:
                    if key not in known:
                        v.err(p + ("config", key), "unknown config key for catalog %s (known: %s)"
                              % (conn["catalog"], ", ".join(known) or "none"))
                for key in entry["config"]:
                    if key["required"] and key["key"] not in given:
                        v.err(p + ("config", key["key"]), "required by catalog %s (%s)" % (conn["catalog"], key["label"]))
                launcher = entry["requires"]
            elif conn.get("command") in LAUNCHERS:
                launcher = conn["command"]
                args = conn.get("args") if isinstance(conn.get("args"), list) else []
                unpinned = unpinned_packages(conn["command"], [str(a) for a in args])
                if unpinned:
                    warnings.append("%s: runs %s without an exact version, so each session gets its latest release"
                                    % (fmt_path(p), ", ".join(unpinned)))
            if launcher and default_station:
                warnings.append("%s: needs %s, which the default workstation image does not have" % (fmt_path(p), launcher))
            item = {}
            for key in ("catalog", "command", "url"):
                item[key] = conn.get(key)
            for key in ("config", "env", "headers"):
                item[key] = {str(k): (str(x) if not isinstance(x, str) else x) for k, x in (conn.get(key) or {}).items()} \
                    if isinstance(conn.get(key), dict) else None
            item["args"] = [str(x) for x in conn["args"]] if isinstance(conn.get("args"), list) else None
            cfg.connections[name] = item

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
            cfg.agent_connections[slug] = names

    for name in cfg.connections:
        if not any(name in names for names in cfg.agent_connections.values()):
            warnings.append("connections.%s: no agent lists it, so no run gets it and Verify does not list its tools"
                            % name)

    errors = v.errors + secret_errors
    if required and not raw.get("secret_store"):
        errors.append("secret_store: required, because the file references secrets (%s)" % ", ".join(required))
    return (None if errors else cfg), errors, warnings, required


# ---------------------------------------------------------------------------------------
# The egress proxy (a stand-in for exradius-egress's forward proxy, docs/kubernetes.md §11)
# ---------------------------------------------------------------------------------------
#
# The check runs this script in a container as ``--egress-proxy /state``. It reads the phase
# (``/state/phase``: setup or agent) and the policy (``/state/policy.json``: allow,
# repositories, private) per request, so the check switches phases by rewriting a file, and
# appends one JSON line per request to ``/state/records.jsonl``. Decisions mirror
# egress/internal/proxy (proxy.go ``decide``, dial.go ``addresses``): the agent phase's list
# first, then private addresses refused in every phase unless allowed, dialling the address
# that was checked. A refusal is ``403`` with the reason as the status text and the body.

PROXY_PORT = 3128
PROXY_IMAGE = "python:3.13-alpine"
PROXY_STATE = "/state"
INTERNAL_RANGES = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8")]
"""``internalRanges`` in egress/internal/proxy/dial.go."""


def _unmap(ip):
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def internal_address(ip):
    """A private, loopback, link-local or otherwise non-public address."""
    ip = _unmap(ipaddress.ip_address(ip) if isinstance(ip, str) else ip)
    return any(ip.version == net.version and ip in net for net in INTERNAL_RANGES)


def match_host(patterns, host):
    """``MatchHost``: an exact name (any case) or ``*.domain`` for every name below domain,
    not the domain itself."""
    host = _cut_suffix(host.lower(), ".")
    for pattern in patterns:
        pattern = host_key(pattern)
        if not pattern:
            continue
        if pattern.startswith("*."):
            if host.endswith("." + pattern[2:]):
                return True
        elif host == pattern:
            return True
    return False


def private_allows(entry, host, ip, port):
    """One ``--allow-private`` entry (``egress.allowPrivate``): a CIDR, an address, or
    ``host[:port]`` (no port: any)."""
    entry = entry.strip()
    try:
        net = ipaddress.ip_network(entry, strict=False)
    except ValueError:
        net = None
    if net is not None:
        return ip is not None and _unmap(ip).version == net.version and _unmap(ip) in net
    name, want = entry, 0
    match = re.match(r"^(?:\[([^\]]+)\]|([^:]+)):([0-9]+)$", entry)
    if match:
        name, want = match.group(1) or match.group(2), int(match.group(3))
    return name.lower() == host.lower() and (want == 0 or want == port)


def egress_decide(phase, host, port, addresses, allow=(), repositories=(), private=()):
    """The forward proxy's decision, as a pure function. ``addresses`` are what ``host``
    resolves to (a literal address resolves to itself; None: it did not resolve).

    Returns a dict: ``allowed``, ``reason`` (the refusal text, or why it cannot be
    reached), ``via`` ("repository" when only the repository stand-in allowed it,
    "allow-private" when a private address was allowed), ``addresses`` (to dial)."""
    out = {"allowed": True, "reason": "", "via": "", "addresses": []}
    if phase != "setup" and not match_host(allow, host):
        if host_key(host) in [host_key(h) for h in repositories]:
            out["via"] = "repository"
        else:
            out.update(allowed=False, reason="%s is not on this task's network allowlist (network.allow)" % host)
            return out
    literal = None
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    if literal is not None:
        if internal_address(literal):
            if not any(private_allows(e, host, literal, port) for e in private):
                out.update(allowed=False, reason="%s is a private or cluster-internal address" % host)
                return out
            out["via"] = out["via"] or "allow-private"
        out["addresses"] = [str(_unmap(literal))]
        return out
    if addresses is None:
        out["reason"] = "cannot resolve %s" % host
        return out
    usable = []
    for text in addresses:
        ip = _unmap(ipaddress.ip_address(text))
        if not internal_address(ip):
            usable.append(str(ip))
        elif any(private_allows(e, host, ip, port) for e in private):
            usable.append(str(ip))
            out["via"] = out["via"] or "allow-private"
    if not usable:
        out.update(allowed=False, reason="%s resolves to a private or cluster-internal address" % host, via="")
        return out
    out["addresses"] = usable
    return out


def _read_state(state):
    try:
        with open(os.path.join(state, "phase")) as handle:
            phase = handle.read().strip() or "setup"
    except OSError:
        phase = "setup"
    try:
        with open(os.path.join(state, "policy.json")) as handle:
            policy = json.load(handle)
    except (OSError, ValueError):
        policy = {}
    return phase, policy


def _resolve(host, port):
    try:
        found = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    out = []
    for item in found:
        address = item[4][0].split("%")[0]
        if address not in out:
            out.append(address)
    return out


def _one_line(text):
    text = "".join(c if 0x20 <= ord(c) <= 0x7e else " " for c in text)
    return text[:200]


def _split_host_port(text, default):
    match = re.match(r"^\[([^\]]+)\](?::([0-9]+))?$", text) or re.match(r"^([^:]+)(?::([0-9]+))?$", text)
    if not match:
        return text, default
    return match.group(1), int(match.group(2)) if match.group(2) else default


class EgressProxy(object):
    """The forward proxy: ``CONNECT host:port`` and absolute-form plain HTTP."""

    def __init__(self, state, host="0.0.0.0", port=PROXY_PORT):
        import http.server
        import socketserver

        self.state = state
        self.lock = threading.Lock()
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                proxy.connect(self)

            def _plain(self):
                proxy.plain(self)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _plain

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server((host, port), Handler)
        self.port = self.server.server_address[1]

    def serve_forever(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()

    # -- deciding and recording ---------------------------------------------------------

    def decide(self, host, port):
        phase, policy = _read_state(self.state)
        literal = True
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = False
        addresses = None if literal else _resolve(host, port)
        decision = egress_decide(phase, host, port, addresses, policy.get("allow") or (),
                                 policy.get("repositories") or (), policy.get("private") or ())
        decision["phase"] = phase
        return decision

    def record(self, decision, host, port, route):
        line = json.dumps({"phase": decision["phase"], "host": host, "port": port, "route": route,
                           "allowed": decision["allowed"], "reason": decision["reason"], "via": decision["via"],
                           "at": time.time()})
        with self.lock:
            with open(os.path.join(self.state, "records.jsonl"), "a") as handle:
                handle.write(line + "\n")

    @staticmethod
    def refuse(handler, code, reason):
        reason = _one_line(reason)
        body = (reason + "\n").encode()
        handler.wfile.write(("HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: %d\r\n"
                             "Connection: close\r\n\r\n" % (code, reason, len(body))).encode() + body)
        handler.wfile.flush()
        handler.close_connection = True

    @staticmethod
    def dial(decision, port):
        last = None
        for address in decision["addresses"]:
            try:
                return socket.create_connection((address, port), timeout=30)
            except OSError as exc:
                last = exc
        raise last or OSError("no address")

    # -- CONNECT ------------------------------------------------------------------------

    def connect(self, handler):
        host, port = _split_host_port(handler.path, 443)
        decision = self.decide(host, port)
        if not decision["allowed"]:
            self.record(decision, host, port, "connect")
            self.refuse(handler, 403, decision["reason"])
            return
        try:
            if not decision["addresses"]:
                raise OSError(decision["reason"] or "no address")
            upstream = self.dial(decision, port)
        except OSError as exc:
            decision["reason"] = _one_line(str(exc))
            self.record(decision, host, port, "connect")
            self.refuse(handler, 502, "cannot reach %s: %s" % (host, decision["reason"]))
            return
        self.record(decision, host, port, "connect")
        handler.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        handler.wfile.flush()
        handler.close_connection = True
        upstream.settimeout(None)
        client = handler.connection
        client.settimeout(None)
        self.splice(client, upstream)

    @staticmethod
    def splice(client, upstream):
        def pump(source, target):
            try:
                while True:
                    data = source.recv(65536)
                    if not data:
                        break
                    target.sendall(data)
            except OSError:
                pass
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        other = threading.Thread(target=pump, args=(upstream, client))
        other.daemon = True
        other.start()
        pump(client, upstream)
        other.join()
        upstream.close()

    # -- plain HTTP in absolute form ----------------------------------------------------

    def plain(self, handler):
        try:
            url = urlsplit(handler.path)
        except ValueError:
            url = None
        if url is None or not url.scheme or not url.hostname:
            self.refuse(handler, 400, "this is a forward proxy: send CONNECT or an absolute URL")
            return
        host = url.hostname
        port = url.port or (443 if url.scheme == "https" else 80)
        decision = self.decide(host, port)
        if not decision["allowed"]:
            self.record(decision, host, port, "http")
            self.refuse(handler, 403, decision["reason"])
            return
        try:
            if not decision["addresses"]:
                raise OSError(decision["reason"] or "no address")
            upstream = self.dial(decision, port)
        except OSError as exc:
            decision["reason"] = _one_line(str(exc))
            self.record(decision, host, port, "http")
            self.refuse(handler, 502, "cannot reach %s: %s" % (host, decision["reason"]))
            return
        self.record(decision, host, port, "http")
        target = url.path or "/"
        if url.query:
            target += "?" + url.query
        lines = ["%s %s HTTP/1.1" % (handler.command, target)]
        for key, value in handler.headers.items():
            if key.lower() in ("proxy-connection", "proxy-authorization", "connection", "keep-alive"):
                continue
            lines.append("%s: %s" % (key, value))
        lines.append("Connection: close")
        try:
            upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            length = handler.headers.get("Content-Length")
            if length and length.isdigit():
                remaining = int(length)
                while remaining > 0:
                    chunk = handler.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    upstream.sendall(chunk)
                    remaining -= len(chunk)
            elif "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
                while True:
                    size_line = handler.rfile.readline()
                    upstream.sendall(size_line)
                    size = int(size_line.split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        while True:
                            trailer = handler.rfile.readline()
                            upstream.sendall(trailer)
                            if trailer in (b"\r\n", b"\n", b""):
                                break
                        break
                    upstream.sendall(handler.rfile.read(size + 2))
            while True:
                data = upstream.recv(65536)
                if not data:
                    break
                handler.wfile.write(data)
            handler.wfile.flush()
        except (OSError, ValueError):
            pass
        finally:
            upstream.close()
            handler.close_connection = True


def egress_proxy_main(state):
    """``--egress-proxy STATE``: serve until stopped; ``STATE/ready`` says it listens."""
    proxy = EgressProxy(state)
    with open(os.path.join(state, "ready"), "w") as handle:
        handle.write("%d\n" % proxy.port)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------------------------------
# Docker: the run's conditions
# ---------------------------------------------------------------------------------------
#
# These flags mirror the run pod the Exradius operator builds (operator/internal/podspec/
# podspec.go in the Exradius repository: the workstation's and setup's security context and
# mounts, the services' sidecars, the shared network namespace) and its readiness rules
# (operator/internal/status/status.go). Change both together.

RUN_UID, RUN_GID = 1000, 1000
WORK_DIR = "/work"
HOME_DIR = "/home/runner"
STATE_DIR = "/exradius/state"
GIT_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
SERVICE_CAPS = ("CHOWN", "SETUID", "SETGID", "FOWNER", "DAC_OVERRIDE", "NET_BIND_SERVICE")
SERVICE_READY_TIMEOUT = 120
KILLED = 137
"""The exit code of a container killed by its memory limit (or any SIGKILL)."""
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
for t in node npm npx corepack yarn pnpm bun python3 python pip uv uvx poetry java mvn gradle go cargo rustc ruby bundle php composer dotnet mix make gcc psql curl wget tar ffmpeg; do
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


class Log(object):
    """The full log. It counts its lines, so a step's output can be cited by line number,
    and it is written from the step's thread and the progress line's."""

    def __init__(self, path):
        self.path = path
        self.handle = open(path, "w", encoding="utf-8", errors="replace")
        self.lines = 0
        self.lock = threading.Lock()
        self.closed = False

    def write(self, text):
        """Write ``text``; returns the line number its first line has."""
        with self.lock:
            number = self.lines + 1
            if not self.closed:
                self.handle.write(text)
                self.handle.flush()
            self.lines += text.count("\n")
        return number

    def flush(self):
        pass

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.handle.close()


def stream(cmd, log, verbose, env=None, input_text=None, marker=None, hidden=None, numbers=None):
    """Run ``cmd``, writing its output to ``log`` (and the terminal with ``verbose``);
    ``(exit code, output)``. Text from ``marker`` on (the step wrapper's report) goes to
    ``hidden`` instead of the output; ``numbers`` gets each output line's number in the log."""
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
        if marker:
            at = line.find(marker)
            if at >= 0:
                if hidden is not None:
                    hidden.append(line[at:].strip())
                line = line[:at] + "\n" if at else ""
                if not line:
                    continue
        chunks.append(line)
        number = log.write(line)
        if numbers is not None:
            numbers.append(number)
        if verbose:
            sys.stdout.write("      | " + line)
            sys.stdout.flush()
    proc.wait()
    return proc.returncode, "".join(chunks)


ANSI = re.compile(r"\x1b\[[0-9;?<=>]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text):
    """``text`` as a terminal would leave it: no escape sequences, and of a line rewritten
    with carriage returns (a progress bar) only its last state."""
    rows = []
    for row in ANSI.sub("", text).split("\n"):
        parts = [p for p in row.split("\r") if p]
        rows.append(parts[-1] if parts else "")
    return "\n".join(rows)


def tail(text, lines=60):
    rows = strip_ansi(text).rstrip().split("\n")
    if len(rows) <= lines:
        return "\n".join(rows)
    return "... (%d lines above)\n" % (len(rows) - lines) + "\n".join(rows[-lines:])


def last_lines(text, count=10):
    """The last ``count`` non-empty lines of ``text``, escape sequences stripped."""
    return [row.rstrip()[:300] for row in strip_ansi(text).split("\n") if row.strip()][-count:]


FAILURE_MARKERS = ("--- FAIL", "FAILED", "Failed!", "Caused by:", "[ERROR]", "Error:", "error:", "panic:",
                   "Traceback", "npm ERR!", "ELIFECYCLE", "Killed", "signal 9")
"""Words that start the report of a failure in common tools' output (go test, pytest, JUnit
and Maven, .NET, Rust, Node, the kernel's OOM killer)."""


def failure_lines(text, numbers=None, tail_lines=60, limit=15):
    """The lines above a failed step's last ``tail_lines`` that carry a failure marker:
    ``[(line number, text)]``, numbered as in the full log when ``numbers`` (one per line of
    ``text``) is given, else from 1. Repeated texts are listed once; past ``limit``, the
    first five and the last ones."""
    rows = strip_ansi(text).rstrip().split("\n")
    above = rows[:max(0, len(rows) - tail_lines)]
    found = []
    seen = set()
    for index, row in enumerate(above):
        if not any(marker in row for marker in FAILURE_MARKERS):
            continue
        key = row.strip()
        if key in seen:
            continue
        seen.add(key)
        number = numbers[index] if numbers and index < len(numbers) else index + 1
        found.append((number, row.rstrip()[:300]))
    if len(found) > limit:
        found = found[:5] + [(None, "... (%d more)" % (len(found) - limit))] + found[-(limit - 5):]
    return found


def short_title(command, limit=100):
    """A command as a step title: its first line, cut to about ``limit`` characters."""
    rows = [row.strip() for row in command.strip().split("\n") if row.strip()]
    first = rows[0] if rows else command.strip()
    if len(first) > limit:
        return first[:limit - 3].rstrip() + "..."
    if len(rows) > 1:
        return first + " ..."
    return first


class StepFailed(Exception):
    """``numbers``: the output's line numbers in the full log (it was streamed there); None
    when the output is not in the log yet. ``notes``: what the check adds (the proxy's view)."""

    def __init__(self, summary, output="", notes="", numbers=None):
        super().__init__(summary)
        self.summary = summary
        self.output = output
        self.notes = notes
        self.numbers = numbers


class Failure(object):
    def __init__(self, name, summary, output="", notes="", numbers=None, log_line=None):
        self.name = name
        self.summary = summary
        self.output = output
        self.notes = notes
        self.numbers = numbers
        self.log_line = log_line


def host_fault(code, output):
    """Why a step failed because of this machine or its Docker rather than the project, or ""."""
    text = output or ""
    if "error getting credentials" in text:
        return ("Docker could not read its registry credentials (error getting credentials): a fault of this "
                "machine's Docker setup, not of the project")
    if code not in (-1, 125):
        return ""
    if re.search(r"Resource temporarily unavailable|EAGAIN|\[Errno (?:11|35)\]", text):
        return ("this machine could not start another process (EAGAIN: too many processes, or too little "
                "memory): the host's fault, not the project's; run the check again when it is less loaded")
    if code == -1:
        return "the check could not start docker (%s): the host's fault, not the project's" % _one_line(text)
    return "Docker could not start the container (exit 125): the host's fault, not the project's"


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


def check_home():
    """Where the check keeps its caches and logs: ``~/.cache/exradius-check``."""
    return os.path.join(os.path.expanduser("~"), ".cache", "exradius-check")


def guess_slug(path):
    """The project's slug from exradius.yaml before it is validated (for the log's name)."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = load_yaml(handle.read())
        slug = raw["project"]["slug"]
        if isinstance(slug, str) and NAME.match(slug):
            return slug
    except Exception:  # noqa: BLE001 - any unreadable file: the folder's name instead
        pass
    name = re.sub(r"[^a-z0-9-]+", "-", os.path.basename(os.path.dirname(os.path.abspath(path))).lower()).strip("-")
    return name or "project"


LOG_KEEP = 20
"""Full logs kept per project slug in the default log folder."""


def default_log_path(slug, when, base):
    """``<base>/logs/<slug>-<YYYYmmdd-HHMMSS>.log``."""
    return os.path.join(base, "logs", "%s-%s.log" % (slug, time.strftime("%Y%m%d-%H%M%S", time.localtime(when))))


def logs_to_remove(names, slug, keep=LOG_KEEP):
    """Of the file names in the log folder, this slug's logs beyond the newest ``keep``."""
    pattern = re.compile(r"^%s-\d{8}-\d{6}(?:-[0-9a-f]+)?\.log$" % re.escape(slug))
    mine = sorted(n for n in names if pattern.match(n))
    return mine[:max(0, len(mine) - keep)]


def default_cache_dir(config_path, slug, base):
    """``(folder, kept)``: ``.exradius-cache`` next to exradius.yaml when it exists (where
    earlier versions of the check put it), else ``<base>/<slug>``."""
    legacy = os.path.join(os.path.dirname(config_path), CACHE_DIR_NAME)
    if os.path.isdir(legacy):
        return legacy, True
    return os.path.join(base, slug), False


HEARTBEAT = 300
"""Seconds between the "still running" lines of a step (without -v)."""

VOLUMES = (("work", WORK_DIR), ("home", HOME_DIR), ("tmp", "/tmp"), ("state", STATE_DIR))
"""The run's directories that are subPaths of its workspace PVC (a Linux filesystem): Docker
volumes here, so on Docker Desktop they live in its Linux VM and not on a macOS mount."""

STEP_WRAPPER = r"""
tok=$2
sampler=
if [ -r /sys/fs/cgroup/memory.current ] && [ -r /sys/fs/cgroup/memory.stat ]; then
  (
    trap 'echo "$tok ws=$max"; exit 0' TERM
    max=0
    while :; do
      cur=0
      inactive=0
      read cur < /sys/fs/cgroup/memory.current
      while read key value; do
        if [ "$key" = inactive_file ]; then inactive=$value; fi
      done < /sys/fs/cgroup/memory.stat
      ws=$((cur - inactive))
      if [ "$ws" -gt "$max" ]; then max=$ws; fi
      sleep 0.2 2>/dev/null || sleep 1
    done
  ) </dev/null &
  sampler=$!
fi
/bin/sh -c "$1" </dev/null
code=$?
if [ -n "$sampler" ]; then kill -TERM "$sampler" 2>/dev/null; wait "$sampler" 2>/dev/null; fi
peak=
kills=
if [ -r /sys/fs/cgroup/memory.peak ]; then read peak < /sys/fs/cgroup/memory.peak; fi
if [ -r /sys/fs/cgroup/memory.events ]; then
  while read key value; do
    if [ "$key" = oom_kill ]; then kills=$value; fi
  done < /sys/fs/cgroup/memory.events
fi
echo "$tok peak=$peak oom_kill=$kills"
exit $code
"""
"""Runs a setup or --run command as ``/bin/sh -c CMD`` (stdin from /dev/null, the same cwd
and environment) and reports its container's memory afterwards from cgroup v2: the largest
working set sampled every 0.2 s (memory.current less inactive_file, the kubelet's measure:
memory.peak also counts page cache, which reaches the limit in any step that writes many
files) and memory.events' oom_kill. Nothing is reported on cgroup v1."""


def wrapper_report(lines):
    """``{"ws": bytes, "peak": bytes, "oom_kill": n}`` from the wrapper's lines (keys absent
    when unknown)."""
    out = {}
    for line in lines:
        for key, value in re.findall(r"\b(ws|peak|oom_kill)=(\d+)", line):
            out[key] = int(value)
    return out


GIB = 1024 ** 3


def memory_text(size):
    """A memory size as Docker's units count it: ``1.3 GiB``, ``312 MiB``."""
    if size >= GIB:
        return "%.1f GiB" % (size / GIB)
    if size >= 1024 ** 2:
        return "%d MiB" % round(size / 1024 ** 2)
    return "%d KiB" % max(1, round(size / 1024))


def memory_verdict(code, declared, host_total, oom_kills):
    """What a step's exit code and its container's oom_kill count say about memory:
    ``(summary of a failed step or "", note for a step whose top process survived or "")``."""
    limit = memory_bytes(declared) if declared else None
    host_small = bool(limit and host_total and limit > host_total)
    if code == KILLED:
        if host_small:
            return ("exited with 137: killed: Docker has %s, less than the declared %s; give Docker more memory or "
                    "check on a larger machine (a run's limit is the declared %s)"
                    % (memory_text(host_total), declared, declared), "")
        if declared:
            # Verify's wording (worker/verify.py): exit 137 under a declared limit.
            return ("exited with 137: exceeded its declared memory (%s); raise resources.memory or lower the step's "
                    "parallelism" % declared, "")
        return ("exited with 137 (killed: usually out of memory; Docker has %s and resources.memory is not declared)"
                % (memory_text(host_total) if host_total else "an unknown amount of memory"), "")
    if oom_kills:
        if host_small:
            where = "Docker's %s, less than the declared %s" % (memory_text(host_total), declared)
        elif declared:
            where = "resources.memory %s" % declared
        else:
            where = "Docker's %s; resources.memory is not declared" % (
                memory_text(host_total) if host_total else "memory")
        return "", "a process in this step was killed for memory (limit: %s)" % where
    return "", ""


def memory_advice(peak, step, declared):
    """The final report's memory lines: the highest peak against ``resources.memory``, with
    the skill's rule (the peak plus about 1 GB for the agent, which runs within the limit,
    and never below 4g)."""
    # At least 4g: the commands measured here are narrower than a real ticket's.
    suggest = max(4, int(-(-(peak + GIB) // GIB)))
    lines = ["highest peak %s in \"%s\"; resources.memory %s" % (memory_text(peak), step, declared or "not declared")]
    limit = memory_bytes(declared) if declared else None
    if limit is None:
        lines.append("declare resources.memory: the peak plus about 1 GB for the agent, at least 4g (%dg)" % suggest)
    elif limit - peak < GIB:
        lines.append("warning: that leaves less than 1 GiB of resources.memory (%s) for the agent, which runs within "
                     "the same limit: declare the peak plus about 1 GB, at least 4g (%dg)" % (declared, suggest))
    return lines


RESERVED_SUFFIXES = (".invalid", ".test", ".example", ".localhost")
RESERVED_NAMES = ("invalid", "test", "example", "localhost")


def host_remark(host, pod_ip=None):
    """``(kind, text)`` about a host the proxy refused or could not reach: ``own`` (the
    run's own address), ``loopback``, ``private`` (another literal private address),
    ``fake`` (not a name a real host has) or ``""``."""
    bare = (host or "").strip("[]")
    if pod_ip and bare == pod_ip:
        return "own", ("%s is the run's own address: a request to it goes to the proxy, in a run too; use 127.0.0.1 "
                       "or localhost" % bare)
    try:
        literal = _unmap(ipaddress.ip_address(bare))
    except ValueError:
        literal = None
    if literal is not None:
        if literal.is_loopback:
            return "loopback", ("%s is the loopback address: a tool sent it to the proxy, ignoring NO_PROXY (in a run "
                                "too)" % bare)
        if internal_address(literal):
            return "private", ("%s is a private address that only the install's egress.allowPrivate opens, not "
                               "network.allow" % bare)
        return "", ""
    name = bare.rstrip(".").lower()
    if "." not in name or name.endswith(RESERVED_SUFFIXES) or name in RESERVED_NAMES:
        return "fake", "not a real host: a test fixture or a script error"
    return "", ""


def refusal_text(host, reason, pod_ip=None):
    """One refused request for the step notes: ``host: reason``, with the remark that says
    what the reason does not."""
    kind, remark = host_remark(host, pod_ip)
    if kind in ("own", "loopback", "private"):
        return "%s: %s" % (host, remark)
    if kind == "fake":
        return "%s: %s; %s" % (host, reason, remark)
    return "%s: %s" % (host, reason)


def size_text(size, empty=False):
    """A cache folder's size: ``empty`` only when it holds nothing."""
    if empty:
        return "empty"
    if size < 1e6:
        return "%d KB" % max(1, round(size / 1e3))
    if size < 1e9:
        return "%.0f MB" % (size / 1e6)
    return "%.1f GB" % (size / 1e9)


def disk_text(sizes, caches=None):
    """The report's Disk line: ``2.4 GB (/work 1.6 GB, home 0.7 GB, /tmp 0.1 GB); caches 1.2 GB``."""
    def size(n):
        return "0 KB" if not n else size_text(n)

    total = sum(sizes.values())
    text = "%s (/work %s, home %s, /tmp %s)" % (size(total), size(sizes.get("work", 0)), size(sizes.get("home", 0)),
                                               size(sizes.get("tmp", 0)))
    if caches is not None:
        text += "; caches %s" % size(caches)
    return text


def run_file_command(text, target):
    """The command that runs a ``--run-file`` script copied to ``target``: its ``#!`` line
    (the interpreter and one optional argument, as Linux reads it), or /bin/sh."""
    first = text.split("\n", 1)[0].rstrip("\r")
    argv = ["/bin/sh"]
    if first.startswith("#!") and first[2:].strip():
        parts = first[2:].strip().split(None, 1)
        argv = parts[:1] + ([parts[1].strip()] if len(parts) > 1 else [])
    return " ".join(shlex.quote(a) for a in argv + [target])


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


class Check(object):
    def __init__(self, args):
        self.args = args
        self.id = _random.token_hex(4)
        self.label = "exradius-check=%s" % self.id
        self.steps = []  # (name, state, seconds, summary)
        self.failures = []  # Failure, in order; the first is the result's
        self.stopped = False  # a failure that ends the check (all but a --run's)
        self.root = None
        self.volumes = []
        self.pod = None  # the container that holds the shared network namespace
        self.pod_ip = None
        self.secrets = {}
        self.log = None
        self.log_path = None
        self.verbose = args.verbose
        self.cleanup_image = None
        self.cache_dir = None
        self.cache_warm = False
        self.emulate = not args.open_network
        self.network = None  # the internal Docker network the pod sits on
        self.proxy = None  # the egress proxy's container
        self.proxy_url = None
        self.egress_dir = None  # the proxy's state directory (phase, policy, records)
        self.agent_hosts = []
        self.repo_hosts = []
        self.live_mounts = []
        self.host_memory = None
        self.peaks = []  # (step, bytes)
        self.step_peak = None
        self.images = {}
        self.disk = None

    # -- reporting ----------------------------------------------------------------------

    def say(self, text=""):
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        if self.log is not None:
            self.log.write(text + "\n")

    def heartbeat(self, name, started, done):
        while not done.wait(HEARTBEAT):
            self.say("....  still running: %s, %d min" % (name, round((time.time() - started) / 60)))

    def step(self, name, action, always=False, keep_going=False, command=None):
        """``always``: a step that runs after a failure too and never fails by itself (INFO).
        ``keep_going``: its failure does not stop the steps after it (a --run's)."""
        if self.stopped and not always:
            self.steps.append((name, "skipped", 0.0, ""))
            self.say("SKIP  %s" % name)
            return None
        log_line = self.log.write("\n===== %s\n" % name) + 1
        if command is not None and command.strip() != name.split(": ", 1)[-1]:
            self.log.write("command:\n%s\n" % command.rstrip("\n"))
        if self.verbose:
            self.say("....  %s" % name)
        self.step_peak = None
        started = time.time()
        done = threading.Event()
        if not self.verbose and not always:
            ticker = threading.Thread(target=self.heartbeat, args=(name, started, done))
            ticker.daemon = True
            ticker.start()
        try:
            summary = action()
            state = "passed"
        except StepFailed as exc:
            state = "failed"
            summary = exc.summary
            self.failures.append(Failure(name, exc.summary, exc.output, exc.notes, exc.numbers, log_line))
            if not keep_going:
                self.stopped = True
            if exc.numbers is None and exc.output and exc.output.strip():
                self.log.write(exc.output.rstrip("\n") + "\n")
            if exc.notes:
                self.log.write(exc.notes + "\n")
        except KeyboardInterrupt:
            self.failures.append(Failure(name, "interrupted", log_line=log_line))
            self.stopped = True
            self.steps.append((name, "failed", time.time() - started, "interrupted"))
            raise
        finally:
            done.set()
        seconds = time.time() - started
        self.steps.append((name, state, seconds, summary or ""))
        mark = "INFO" if always else "PASS" if state == "passed" else "FAIL"
        facts = []
        if seconds >= 0.5:
            facts.append("%.1fs" % seconds)
        if self.step_peak:
            facts.append("peak %s" % memory_text(self.step_peak))
        self.say("%s  %s%s" % (mark, name, " (%s)" % ", ".join(facts) if facts else ""))
        if summary:
            for line in str(summary).splitlines():
                self.say("      %s" % line)
        return state == "passed"

    # -- main ---------------------------------------------------------------------------

    def main(self):
        args = self.args
        path, repo_root, config_dir = find_config(args.config)
        self.config_path, self.repo_root, self.config_dir = path, repo_root, config_dir
        self.slug = guess_slug(path)
        if args.log:
            self.log_path = os.path.abspath(os.path.expanduser(args.log))
        else:
            self.log_path = default_log_path(self.slug, time.time(), check_home())
            folder = os.path.dirname(self.log_path)
            os.makedirs(folder, exist_ok=True)
            if os.path.exists(self.log_path):
                self.log_path = self.log_path[:-len(".log")] + "-%s.log" % self.id
            for name in logs_to_remove(os.listdir(folder), self.slug, LOG_KEEP - 1):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
        if os.path.dirname(self.log_path):
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        self.log = Log(self.log_path)
        self.say("exradius check: %s" % path)
        self.say("full log: %s" % self.log_path)
        previous = {}
        for name in ("SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                number = getattr(signal, name)
                previous[number] = signal.signal(number, _raise_interrupt)
        try:
            self.run_steps()
            self.disk = self.measure_disk()
        except KeyboardInterrupt:
            self.say("\ninterrupted; cleaning up")
        finally:
            # A second signal must not stop the clean-up half way.
            for number in previous:
                signal.signal(number, signal.SIG_IGN)
            if not args.validate:
                self.cleanup()
            for number, handler in previous.items():
                signal.signal(number, handler)
        return self.report()

    def run_steps(self):
        self.step("Read config", self.read_config)
        if self.args.validate or self.failures:
            return
        code, out = run(["docker", "version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"])
        if code != 0:
            self.failures.append(Failure("Docker", "the Docker daemon is not reachable", out))
            self.stopped = True
            return
        self.say("      docker %s" % out.strip())
        code, out = run(["docker", "info", "--format", "{{.MemTotal}}"])
        try:
            self.host_memory = int(out.strip())
        except ValueError:
            self.host_memory = None
        self.check_budget()
        self.prepare_images()
        self.step("Check workstation image", self.check_image)
        if used_connections(self.config):
            self.step("Check connections", self.check_connections)
        self.step("Prepare /work", self.prepare_work)
        if self.emulate:
            self.step("Start egress proxy", self.start_proxy)
        for name in self.config.services:
            self.step("Start service %s" % name, lambda n=name: self.start_service(n))
        for command in self.config.workstation["setup"]:
            title = "Setup: %s" % short_title(command)
            self.step(title, lambda c=command, t=title: self.setup(c, title=t), command=command)
        if self.args.runs and self.emulate and not self.stopped:
            self.set_phase("agent")
        for index, (kind, value) in enumerate(self.args.runs):
            if kind == "file":
                title = "Agent phase: %s" % os.path.basename(value)
                self.step(title, lambda p=value, t=title, i=index: self.setup(None, agent=True, title=t, run_file=(i, p)),
                          keep_going=True, command="--run-file %s" % value)
            else:
                title = "Agent phase: %s" % short_title(value)
                self.step(title, lambda c=value, t=title: self.setup(c, agent=True, title=t), keep_going=True,
                          command=value)
        if self.emulate and self.egress_dir is not None:
            self.step("Network", self.network_summary, always=True)

    def check_budget(self):
        """Exradius places a run's pod on a node whose free memory fits the declared sizes
        (requests); a pod larger than every node waits as "waiting for cluster capacity"."""
        cfg = self.config
        declared = [cfg.memory] + [s["memory"] for s in cfg.services.values()]
        reservation = sum(memory_bytes(m) for m in declared if m)
        if not reservation or not self.host_memory:
            return
        total = self.host_memory
        if reservation > total:
            self.say("      warning: the declared memory (%.1f GiB in all) is more than this Docker host has "
                     "(%.1f GiB): a cluster node needs at least that much free, or the run waits as \"waiting "
                     "for cluster capacity\"" % (reservation / 1024 ** 3, total / 1024 ** 3))

    def report(self):
        self.say("")
        if self.peaks:
            step, peak = max(self.peaks, key=lambda item: item[1])
            lines = memory_advice(peak, step, self.config.memory)
            self.say("Memory: %s" % lines[0])
            # After a setup failure the steps that would have used the most did not run.
            for line in lines[1:] if not self.stopped else []:
                self.say("  %s" % line)
        if self.disk:
            self.say("Disk: %s" % self.disk)
        if not self.failures and self.args.validate:
            self.say("RESULT: the file is valid (only read and validated; no Docker steps ran).")
            code = 0
        elif not self.failures:
            what = "The setup commands and the agent-phase commands" if self.args.runs else "The setup commands"
            if self.emulate:
                self.say("RESULT: passed. %s ran under the run's conditions." % what)
            else:
                self.say("RESULT: passed. %s ran under the run's conditions except its network: with --open-network "
                         "every container reached the network directly, so nothing here shows what the run's proxy "
                         "would refuse." % what)
            parts = ["the agent runtime and model", "providers", "pushes and git's credential route",
                     "MCP connections' tool lists"]
            if not self.args.platform:
                parts.append("the cluster's CPU architecture (--platform)")
            if self.args.allow or self.args.allow_private:
                parts.append("the install's egress.allow and egress.allowPrivate beyond --allow and --allow-private")
            else:
                parts.append("the install's egress.allow and egress.allowPrivate (--allow, --allow-private)")
            parts.append("image builds' network (Docker's own network here, the proxy in the cluster)")
            self.say("Not covered locally: %s." % ", ".join(parts))
            code = 0
        else:
            first = self.failures[0]
            self.say("RESULT: failed at step \"%s\": %s" % (first.name, first.summary))
            if len(self.failures) > 1:
                self.say("Also failed: %s" % ", ".join("\"%s\"" % f.name for f in self.failures[1:]))
            for failure in self.failures:
                self.show_failure(failure, len(self.failures) > 1)
            code = 1
        if self.log is not None:
            self.say("")
            self.say("Full log: %s" % self.log_path)
            self.log.close()
        return code

    def show_failure(self, failure, named):
        title = "the failing step" if not named else "step \"%s\"" % failure.name
        if failure.summary == "interrupted":
            return
        if named:
            self.say("")
            self.say("== %s: %s" % (failure.name, failure.summary))
        output = failure.output or ""
        if output.strip():
            marked = failure_lines(output, failure.numbers, self.args.tail)
            if marked:
                self.say("")
                where = "line numbers in the full log" if failure.numbers else "line numbers in the output"
                self.say("Earlier lines of %s that look like a failure (%s):" % (title, where))
                for number, text in marked:
                    self.say("%7s  %s" % (number if number is not None else "", text))
            self.say("")
            self.say("Output of %s (last lines):" % title)
            self.say(tail(output, self.args.tail))
        else:
            self.say("")
            where = " (its part of the full log starts at line %d)" % failure.log_line if failure.log_line else ""
            self.say("%s printed nothing%s: %s" % (title[0].upper() + title[1:], where, self.log_path))
        if failure.notes:
            self.say("")
            self.say(failure.notes)

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
        rel = os.path.relpath(self.config_path, self.repo_root)
        code_top, _ = git(["rev-parse", "--show-toplevel"], self.repo_root)
        code, commit = git(["log", "-1", "--format=%h %s"], self.repo_root)
        own_folder = os.path.dirname(os.path.realpath(self.config_path))
        foreign = False
        if code_top == 0 and self.config_dir and self.config_dir != ".exradius":
            tracked, _ = git(["ls-files", "--error-unmatch", "--", rel], self.repo_root)
            if tracked != 0:
                foreign = True
                warnings.append("%s is inside the git repository %s but not committed there: a config repository of "
                                "its own needs `git init -b main` and a commit in %s" % (own_folder, self.repo_root,
                                                                                         own_folder))
        if code == 0 and commit:
            lines.append("config repository %s, commit %s" % (self.repo_root, commit))
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
            if code == 0 and status and not foreign:
                warnings.append("not committed yet (Exradius reads the pushed branch head):\n  "
                                + status.replace("\n", "\n  "))
        elif code_top == 0:
            lines.append("%s is a git repository with no commit yet: commit exradius.yaml (Exradius reads the pushed "
                         "branch head)" % self.repo_root)
        else:
            lines.append("%s is not in a git repository yet" % self.repo_root)
        lines.append("file: %s" % rel)
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
        self.agent_hosts = allowed_hosts(config, self.args.allow)
        self.repo_hosts = repository_hosts(config)
        if self.agent_hosts:
            lines.append("network: the agent phase may reach %s" % ", ".join(self.agent_hosts))
        else:
            lines.append("network: the agent phase reaches no host besides the model, the repositories and the "
                         "connections' routes")
        self.secrets = {name: "chk" + _random.token_hex(10) for name in required}
        if config.workstation["cache"]:
            kept = False
            if self.args.cache_dir:
                folder = self.args.cache_dir
            else:
                folder, kept = default_cache_dir(self.config_path, config.slug, check_home())
            self.cache_dir = os.path.realpath(folder)
            sizes, self.cache_warm = cache_summary(self.cache_dir, config.workstation["cache"])
            lines.append("cache: %s: %s" % (self.cache_dir, sizes))
            if kept:
                lines.append("  (the .exradius-cache next to exradius.yaml exists, so the check keeps using it; the "
                             "default is %s)" % os.path.join(check_home(), config.slug))
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
        # Exradius builds with rootless BuildKit in the cluster.
        cmd = ["docker", "build", "-f", dockerfile, "-t", tag, "--label", "exradius.build=%s" % name, "--progress=plain"]
        cmd += self.platform_args()
        if self.args.no_cache:
            cmd.append("--no-cache")
        cmd.append(context)
        env = docker_env({"DOCKER_BUILDKIT": "1"})
        numbers = []
        code, out = stream(cmd, self.log, self.verbose, env=env, numbers=numbers)
        if code != 0:
            fault = host_fault(code, out)
            raise StepFailed("building %s from %s failed (exit %d)%s%s" % (
                name, item["dockerfile"], code, "; " + fault if fault else "", failed_instruction(out)), out,
                numbers=numbers)
        self.images[name] = tag
        return "built %s from %s (context %s)" % (tag, item["dockerfile"], item["context"] or ".")

    def build_default(self):
        cmd = ["docker", "build", "-t", DEFAULT_IMAGE_TAG] + self.platform_args() + ["-"]
        numbers = []
        code, out = stream(cmd, self.log, self.verbose, env=docker_env({"DOCKER_BUILDKIT": "1"}),
                           input_text=DEFAULT_IMAGE_DOCKERFILE, numbers=numbers)
        if code != 0:
            fault = host_fault(code, out)
            raise StepFailed("building the default workstation stand-in failed (exit %d)%s"
                             % (code, "; " + fault if fault else ""), out, numbers=numbers)
        self.images["workstation"] = DEFAULT_IMAGE_TAG
        return "no workstation image or dockerfile: Ubuntu 24.04 with git, curl, ca-certificates, build-essential"

    def pull(self, name, image):
        code, _ = run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        if code == 0 and not self.args.pull:
            self.images[name] = image
            return "%s is present locally" % image
        numbers = []
        code, out = stream(["docker", "pull"] + self.platform_args() + [image], self.log, self.verbose, numbers=numbers)
        if code != 0:
            fault = host_fault(code, out)
            raise StepFailed("could not pull %s%s" % (image, "; " + fault if fault else ""), out, numbers=numbers)
        self.images[name] = image
        return "pulled %s" % image

    def volume(self, key):
        return "exradius-check-%s-%s" % (self.id, key)

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
        for key, target in VOLUMES:
            # nocopy: a PVC's subPath does not show what the image has at that path.
            flags += ["--mount", "type=volume,source=%s,target=%s,volume-nocopy" % (self.volume(key), target)]
        mounts = [
            (self.attachments, WORK_DIR + "/.exradius/attachments", True),
            (self.passwd, "/etc/passwd", True),
            (self.group, "/etc/group", True),
        ] + self.cache_mounts() + list(extra_mounts)
        for source, target, read_only in mounts:
            flags += ["--mount", "type=bind,source=%s,target=%s%s" % (source, target, ",readonly" if read_only else "")]
        flags += memory_flags(self.config.memory)
        return flags

    def cache_mounts(self):
        """``workstation.cache``: one folder per entry under the local cache directory,
        shared by every check of this config (in Exradius: the project volume's caches/).
        Their mount points inside the home are made by ``make_volumes``."""
        mounts = []
        for index, entry in enumerate(self.config.workstation["cache"]):
            source = os.path.join(self.cache_dir, cache_folder(index, entry["path"]))
            if not os.path.isdir(source):
                os.makedirs(source)
                os.chmod(source, 0o777)
            mounts.append((source, entry["target"], False))
        return mounts

    def make_root(self):
        """The host folder for what stays on the host: /etc/passwd and /etc/group, the
        empty attachments folder, the proxy's state, clones on their way into /work."""
        if self.root is not None:
            return
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="exradius-check-%s-" % self.id))
        etc = os.path.join(self.root, "etc")
        self.attachments = os.path.join(self.root, "attachments")
        for d in (etc, self.attachments):
            os.makedirs(d)
        os.chmod(self.attachments, 0o755)
        self.passwd = os.path.join(etc, "passwd")
        self.group = os.path.join(etc, "group")
        for target, text in ((self.passwd, PASSWD), (self.group, GROUP)):
            with open(target, "w") as handle:
                handle.write(text)
            os.chmod(target, 0o644)

    def make_volumes(self):
        """/work, the home, /tmp and the state directory, which a run has on one PVC: Docker
        volumes, prepared as root in a helper container of the workstation image (owned by
        the run user, /tmp 1777, the cache mount points inside the home made for the run
        user first, as the worker's caches.ensure does: Docker would make ~/.cache root's
        for ~/.cache/uv)."""
        if self.volumes:
            return
        self.make_root()
        for key, _ in VOLUMES:
            name = self.volume(key)
            code, out = run(["docker", "volume", "create", "--label", self.label, name])
            if code != 0:
                fault = host_fault(code, out)
                raise StepFailed("could not create the Docker volume %s%s" % (name, "; " + fault if fault else ""), out)
            self.volumes.append(name)
        script = ["set -e", "mkdir -p /v/work/.exradius/attachments"]
        for entry in self.config.workstation["cache"]:
            target = entry["target"]
            if target.startswith(HOME_DIR + "/"):
                script.append("mkdir -p %s" % shlex.quote("/v/home/" + target[len(HOME_DIR) + 1:]))
        script += ["chown -R %d:%d /v/work /v/home /v/state" % (RUN_UID, RUN_GID),
                   "chmod 0777 /v/work /v/home /v/state", "chmod 0755 /v/work/.exradius", "chmod 1777 /v/tmp"]
        cmd = ["docker", "run", "--rm", "--user", "0:0", "--network", "none", "--label", self.label]
        for key, _ in VOLUMES:
            cmd += ["--mount", "type=volume,source=%s,target=/v/%s,volume-nocopy" % (self.volume(key), key)]
        cmd += self.platform_args() + ["--entrypoint", "/bin/sh", self.images["workstation"], "-c", "\n".join(script)]
        code, out = run(cmd, timeout=300)
        if code != 0:
            fault = host_fault(code, out)
            raise StepFailed("could not run /bin/sh as root in %s to prepare the run's volumes (exit %d)%s"
                             % (self.images["workstation"], code, "; " + fault if fault else ""), out)

    def check_image(self):
        self.make_volumes()
        image = self.images["workstation"]
        cmd = ["docker", "run", "--rm"] + self.run_flags() + self.platform_args() + \
            ["--entrypoint", "/bin/sh", image, "-c", IMAGE_PROBE]
        code, out = run(cmd, timeout=120)
        self.log.write(out)
        if code != 0:
            raise StepFailed("could not run /bin/sh in %s as uid %d with a read-only root filesystem (exit %d)"
                             % (image, RUN_UID, code), out, numbers=[])
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
            raise StepFailed("; ".join(problems), out, numbers=[])
        return detail

    def check_connections(self):
        """Each stdio connection an agent lists needs its command in the workstation image:
        Verify's "List tools <name>" starts it there."""
        commands = []
        lines = []
        for name in used_connections(self.config):
            definition = connection_definition(self.config.connections[name])
            if definition["transport"] != "stdio":
                lines.append("%s: HTTP (%s), nothing to start in the workstation" % (
                    name, "through the proxy's route" if proxied(definition) else url_host(definition["url"]) or "?"))
                continue
            commands.append((name, definition["command"]))
        if not commands:
            return "\n".join(lines)
        probe = "".join('p=$(command -v "%s" 2>/dev/null) && echo "%s: $p" || echo "%s: missing"\n'
                        % (cmd.replace('"', '\\"'), name, name) for name, cmd in commands)
        env = self.workstation_env()
        env.update({"HOME": HOME_DIR})
        cmd = ["docker", "run", "--rm", "--network", "none"] + self.run_flags() + self.platform_args()
        for key, value in env.items():
            cmd += ["-e", "%s=%s" % (key, value)]
        cmd += ["--entrypoint", "/bin/sh", self.images["workstation"], "-c", probe]
        code, out = run(cmd, timeout=120)
        self.log.write(out)
        if code != 0:
            raise StepFailed("could not run /bin/sh in the workstation image (exit %d)" % code, out, numbers=[])
        found = {}
        for row in out.splitlines():
            key, _, value = row.partition(": ")
            found[key] = value
        missing = []
        for name, command in commands:
            path = found.get(name, "missing")
            if path == "missing":
                missing.append("connection %s needs %s, which is not on the workstation image's PATH (in Exradius, "
                               "Verify's \"List tools %s\" would fail)" % (name, command, name))
            else:
                lines.append("%s: %s at %s" % (name, command, path))
        if missing:
            raise StepFailed("; ".join(missing), "\n".join(lines + [out]))
        return "\n".join(lines)

    def start_proxy(self):
        """The internal network with no route out, and the forward proxy on it and on the
        default bridge: the only way a container of the pod reaches the network."""
        self.make_root()
        network = "exradius-check-%s" % self.id
        code, out = run(["docker", "network", "create", "--internal", "--label", self.label, network])
        if code != 0:
            raise StepFailed("could not create the internal network %s" % network, out)
        self.network = network
        image = self.args.proxy_image
        code, _ = run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        if code != 0:
            numbers = []
            code, out = stream(["docker", "pull", image], self.log, self.verbose, numbers=numbers)
            if code != 0:
                raise StepFailed("could not pull the proxy's image %s" % image, out, numbers=numbers)
        self.egress_dir = os.path.join(self.root, "egress")
        script_dir = os.path.join(self.root, "egress-script")
        for d in (self.egress_dir, script_dir):
            os.makedirs(d)
        os.chmod(self.egress_dir, 0o777)
        shutil.copyfile(os.path.realpath(__file__), os.path.join(script_dir, "exradius_check.py"))
        os.chmod(script_dir, 0o755)
        os.chmod(os.path.join(script_dir, "exradius_check.py"), 0o644)
        self.write_state("phase", "setup\n")
        self.write_state("policy.json", json.dumps({"allow": self.agent_hosts, "repositories": self.repo_hosts,
                                                    "private": list(self.args.allow_private)}))
        self.write_state("records.jsonl", "")
        container = "exradius-check-%s-egress" % self.id
        cmd = ["docker", "run", "-d", "--name", container, "--label", self.label, "--user", "%d:%d" % (RUN_UID, RUN_GID),
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--mount", "type=bind,source=%s,target=/check,readonly" % script_dir,
               "--mount", "type=bind,source=%s,target=%s" % (self.egress_dir, PROXY_STATE),
               "--entrypoint", "python3", image, "-I", "-B", "/check/exradius_check.py", "--egress-proxy", PROXY_STATE]
        code, out = run(cmd)
        if code != 0:
            raise StepFailed("could not start the egress proxy (%s)" % image, out)
        self.proxy = container
        code, out = run(["docker", "network", "connect", network, container])
        if code != 0:
            raise StepFailed("could not connect the egress proxy to %s" % network, out)
        code, out = run(["docker", "inspect", "--format",
                         '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % network, container])
        address = out.strip()
        if code != 0 or not address:
            raise StepFailed("could not read the egress proxy's address on %s" % network, out)
        started = time.time()
        while not os.path.exists(os.path.join(self.egress_dir, "ready")):
            code, running = run(["docker", "inspect", "--format", "{{.State.Running}}", container])
            if running.strip() != "true" or time.time() - started > 30:
                raise StepFailed("the egress proxy did not start", self.service_logs(container))
            time.sleep(0.2)
        self.proxy_url = "http://%s:%d" % (address, PROXY_PORT)
        lines = ["%s on %s (internal, no route out), image %s" % (self.proxy_url, network, image),
                 "setup: any public host; agent phase: %s" % (", ".join(self.agent_hosts) or "no host")]
        if self.repo_hosts:
            lines.append("agent phase, for the repositories (git's route in a run): %s" % ", ".join(self.repo_hosts))
        if self.args.allow_private:
            lines.append("private addresses allowed: %s" % ", ".join(self.args.allow_private))
        return "\n".join(lines)

    def write_state(self, name, text):
        path = os.path.join(self.egress_dir, name)
        temporary = path + ".new"
        with open(temporary, "w") as handle:
            handle.write(text)
        os.chmod(temporary, 0o666)
        os.rename(temporary, path)

    def set_phase(self, phase):
        if self.egress_dir is not None:
            self.write_state("phase", phase + "\n")

    def records(self, start=0):
        """``(records, end offset)`` of the proxy's log from byte ``start``."""
        if self.egress_dir is None:
            return [], 0
        path = os.path.join(self.egress_dir, "records.jsonl")
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                data = handle.read()
        except OSError:
            return [], start
        out = []
        complete = data[:data.rfind(b"\n") + 1] if b"\n" in data else b""
        for line in complete.decode("utf-8", "replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out, start + len(complete)

    def records_offset(self):
        return self.records(0)[1]

    def proxy_env(self, java=False):
        """``proxyEnv`` (and ``javaEnv`` for setup and the workstation) of podspec.go."""
        if not self.proxy_url:
            return {}
        none = ["localhost", "127.0.0.1", "::1"] + [n for n in self.config.services]
        env = {}
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[key] = self.proxy_url
        env["NO_PROXY"] = env["no_proxy"] = ",".join(none)
        env["YARN_HTTP_PROXY"] = env["YARN_HTTPS_PROXY"] = self.proxy_url
        if java:
            host, port = self.proxy_url[len("http://"):].rsplit(":", 1)
            options = ("-Dhttp.proxyHost=%s -Dhttp.proxyPort=%s -Dhttps.proxyHost=%s -Dhttps.proxyPort=%s "
                       "-Dhttp.nonProxyHosts=%s -Daether.connector.http.useSystemProperties=true"
                       % (host, port, host, port, "|".join(h for h in none if ":" not in h)))
            own = self.config.env.get("JAVA_TOOL_OPTIONS")
            if own and not SECRET_REF.search(own):
                options = own + " " + options
            env["JAVA_TOOL_OPTIONS"] = options
        return env

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
        self.make_volumes()
        clones = os.path.join(self.root, "clones")
        os.makedirs(clones, exist_ok=True)
        lines = []
        warnings = []
        for repo in self.config.repositories:
            name = repo["name"]
            dest = os.path.join(clones, name)
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
                    self.live_mounts.append((source, WORK_DIR + "/" + name, False))
                    lines.append("%s: %s mounted as it is (%s, %s); a live checkout is a host mount (on macOS: no "
                                 "Unix sockets in it, and slower)" % (name, source, head, short))
                    continue
                code, out = git(["clone", "--quiet", source, dest], None)
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
            self.copy_into_work(name, dest)
            shutil.rmtree(dest, ignore_errors=True)
        if self.args.live and self.must_open_up():
            warnings.append("--live on Linux with uid %d: the run user (uid %d) may not be able to write the "
                            "mounted checkouts" % (os.getuid(), RUN_UID))
        if not self.config.repositories:
            lines.append("no repositories")
        return "\n".join(lines + ["warning: %s" % w for w in warnings])

    def copy_into_work(self, name, folder):
        """A clone into /work/<name> on the work volume, written by the run user from a tar
        stream, so its files are uid 1000's on every host, as the run's clone containers'."""
        cmd = ["docker", "run", "--rm", "-i", "--user", "%d:%d" % (RUN_UID, RUN_GID), "--network", "none",
               "--label", self.label,
               "--mount", "type=volume,source=%s,target=%s,volume-nocopy" % (self.volume("work"), WORK_DIR)]
        cmd += self.platform_args() + ["--entrypoint", "tar", self.images["workstation"], "-xf", "-", "-C", WORK_DIR]
        problem = ""
        with tempfile.TemporaryFile() as output:
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                        env=docker_env())
            except OSError as exc:
                raise StepFailed("could not copy %s into /work: %s" % (name, host_fault(-1, str(exc))), str(exc))
            try:
                with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                    archive.add(folder, arcname=name)
                proc.stdin.close()
            except (OSError, tarfile.TarError) as exc:
                problem = str(exc)
            code = proc.wait()
            output.seek(0)
            text = output.read().decode(errors="replace")
        if code != 0 or problem:
            hint = ""
            if re.search(r"executable file not found|\"tar\"|no such file", text, re.I):
                hint = "; the check copies clones with tar, which the workstation image lacks"
            elif host_fault(code, text):
                hint = "; " + host_fault(code, text)
            raise StepFailed("could not copy %s into /work as uid %d (exit %d)%s" % (name, RUN_UID, code, hint),
                             text + ("\n" + problem if problem else ""))

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

    def start_pod(self):
        """The holder of the run's network namespace, as a pod's: a container of the
        workstation image that only sleeps, with every service name as 127.0.0.1 in its
        /etc/hosts (the pod's hostAliases). Services and setup containers join it."""
        if self.pod is not None:
            return
        container = "exradius-check-%s-pod" % self.id
        cmd = ["docker", "run", "-d", "--name", container, "--label", self.label, "--user", "%d:%d" % (RUN_UID, RUN_GID),
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"] + self.platform_args()
        if self.network:
            cmd += ["--network", self.network]
        for name in self.config.services:
            cmd += ["--add-host", "%s:127.0.0.1" % name]
        cmd += ["--entrypoint", "/bin/sh", self.images["workstation"], "-c",
                'trap "exit 0" TERM INT; while :; do sleep 3600 & wait $!; done']
        code, out = run(cmd)
        if code != 0:
            raise StepFailed("could not start the container that holds the run's network", out)
        self.pod = container
        if self.network:
            fmt = '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % self.network
        else:
            fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"
        code, out = run(["docker", "inspect", "--format", fmt, container])
        addresses = out.split()
        self.pod_ip = addresses[0] if code == 0 and addresses else "127.0.0.1"

    def start_service(self, name):
        service = self.config.services[name]
        image = self.images[name]
        self.start_pod()
        env = self.service_env(service)
        container = "exradius-check-%s-%s" % (self.id, name)
        # A run's service is a sidecar of its pod (operator: podspec.service): its image's
        # user and entrypoint, a writable root, these capabilities and nothing else.
        cmd = ["docker", "run", "-d", "--name", container, "--network", "container:%s" % self.pod, "--cap-drop", "ALL"]
        for cap in SERVICE_CAPS:
            cmd += ["--cap-add", cap]
        cmd += ["--security-opt", "no-new-privileges", "--label", self.label] + self.platform_args()
        env.update(self.proxy_env())
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
            check = "port %d" % ready["tcp"]
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
                if state.get("ExitCode") == KILLED and service["memory"]:
                    # As the worker (run_objects.service_failure): exit 137 under a declared limit.
                    raise StepFailed("service %s (%s) exceeded its declared memory (%s): exited with code 137 "
                                     "before it was ready" % (name, image, service["memory"]),
                                     self.service_logs(container))
                raise StepFailed("service %s (%s) exited with code %s before it was ready"
                                 % (name, image, state.get("ExitCode")), self.service_logs(container))
            if "tcp" in ready:
                # As the kubelet's tcpSocket probe: a connection to the pod's own address, so a
                # service that listens only on 127.0.0.1 never passes (use a ready command).
                code, out = run(["docker", "exec", self.pod, "bash", "-c",
                                 "exec 3<>/dev/tcp/%s/%d" % (self.pod_ip, ready["tcp"])], timeout=5)
                if code == 0:
                    break
                last = " (last check: no connection to %s:%d)" % (self.pod_ip, ready["tcp"])
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
                hint = ""
                if "tcp" in ready:
                    hint = ("\nA tcp check connects to the run's own address, not 127.0.0.1: a service that listens "
                            "only on 127.0.0.1 needs a ready command instead.")
                raise StepFailed("service %s (%s) did not become ready within %ds waiting for %s%s%s"
                                 % (name, image, timeout, check, last, hint), self.service_logs(container))
            time.sleep(SERVICE_POLL_INTERVAL)
        extra = "".join([", memory %s" % service["memory"] if service["memory"] else "",
                         ", command %s" % " ".join(service["command"])[:80] if service["command"] else ""])
        return "%s ready in %.1fs (%s), reachable as %s on localhost%s" % (image, time.time() - started, check, name,
                                                                            extra)

    def service_logs(self, container):
        code, out = run(["docker", "logs", "--tail", "40", container])
        return out

    def setup(self, command, agent=False, title=None, run_file=None):
        """A setup command, or (``agent``) a ``--run`` command or ``--run-file`` script in the
        agent phase: the same container, image, mounts, environment and limits."""
        image = self.images["workstation"]
        if self.emulate:
            self.start_pod()
        env = self.workstation_env()
        env.update({"HOME": HOME_DIR, "EXRADIUS_STATE_DIR": STATE_DIR, "EXRADIUS_WORK_DIR": WORK_DIR})
        env.update(GIT_ENV)
        env.update(self.proxy_env(java=True))
        mounts = list(self.live_mounts)
        if run_file is not None:
            index, path = run_file
            with open(path, "rb") as handle:
                data = handle.read()
            folder = os.path.join(self.root, "run-files", str(index))
            os.makedirs(folder, exist_ok=True)
            copy = os.path.join(folder, os.path.basename(path))
            with open(copy, "wb") as handle:
                handle.write(data)
            os.chmod(copy, 0o755)
            target = "/exradius/run-files/%d/%s" % (index, os.path.basename(path))
            mounts.append((copy, target, True))
            command = run_file_command(data.decode("utf-8", "replace"), target)
            self.log.write("runs: %s\n" % command)
        container = "exradius-check-%s-%s-%s" % (self.id, "run" if agent else "setup", _random.token_hex(3))
        cmd = ["docker", "run", "--name", container] + self.run_flags(mounts) + self.platform_args()
        if self.pod:
            cmd += ["--network", "container:%s" % self.pod]
        for key, value in env.items():
            # Explicit values (throwaway secrets, the project env): passing them through the
            # docker CLI's own environment would also change its HOME and PATH.
            cmd += ["-e", "%s=%s" % (key, value)]
        marker = "@@exradius-check-%s@@" % _random.token_hex(6)
        cmd += ["--entrypoint", "/bin/sh", image, "-c", STEP_WRAPPER, "exradius-step", command, marker]
        offset = self.records_offset()
        started = time.time()
        hidden, numbers = [], []
        code, out = stream(cmd, self.log, self.verbose, marker=marker, hidden=hidden, numbers=numbers)
        seconds = time.time() - started
        usage = wrapper_report(hidden)
        if "oom_kill" not in usage:
            _, killed = run(["docker", "inspect", "--format", "{{.State.OOMKilled}}", container])
            if killed.strip() == "true":
                usage["oom_kill"] = 1
        run(["docker", "rm", "-f", container])
        if usage.get("ws"):
            self.step_peak = usage["ws"]
            self.peaks.append((title or command, usage["ws"]))
        if usage:
            self.log.write("memory: working set peak %s, memory.peak %s (page cache included), oom_kill %s\n" % (
                memory_text(usage["ws"]) if usage.get("ws") else "?",
                memory_text(usage["peak"]) if usage.get("peak") else "?", usage.get("oom_kill", "?")))
        verdict, note = memory_verdict(code, self.config.memory, self.host_memory, usage.get("oom_kill"))
        if code != 0:
            if verdict:
                summary = verdict
            else:
                summary = "exited with %d after %.0fs" % (code, seconds)
                fault = host_fault(code, out)
                if fault:
                    summary += " (%s)" % fault
                elif code == 127:
                    summary += " (command not found: the workstation image lacks a tool this command runs)"
            if note:
                summary += "; " + note
            raise StepFailed(summary, out, self.network_notes(out, offset), numbers)
        lines = ["warning: %s" % note] if note else []
        if agent:
            shown = last_lines(out, 10)
            if shown:
                lines += ["| %s" % row for row in shown]
            else:
                lines.append("no output")
        else:
            shown = last_lines(out, 1)
            if shown:
                lines.append("last line: %s" % shown[0][:200])
        return "\n".join(lines)

    def network_notes(self, out, offset):
        """What the proxy refused or could not reach during a failed step, and the hint for a
        tool that ignored the proxy (``diagnose._network`` in Exradius)."""
        if not self.emulate:
            return ""
        notes = []
        records, _ = self.records(offset)
        refused = []
        errors = {}
        for record in records:
            host = record.get("host") or ""
            if not record.get("allowed"):
                line = refusal_text(host, record.get("reason"), self.pod_ip)
                if line not in refused:
                    refused.append(line)
            elif record.get("reason"):
                count, reason = errors.get(host.lower(), (0, record.get("reason")))
                errors[host.lower()] = (count + 1, reason)
        if refused:
            notes.append("refused during this step (may be unrelated to the failure):")
            notes += ["  %s" % line for line in refused]
        for host, (count, reason) in errors.items():
            notes.append(proxy_error_line(host, count, reason, self.pod_ip))
        # A refused connection to the run's own localhost is the application not listening
        # (yet), not a tool that ignored the proxy.
        scan = "\n".join(row for row in out.splitlines() if not LOCAL_TARGET.search(row))
        if not ALLOWLIST_REFUSAL.search(out) and (NO_PROXY_USED.search(scan) or NO_NAME.search(scan)):
            notes.append("hint: the run reaches the network only through the proxy that HTTPS_PROXY, HTTP_PROXY and "
                         "JAVA_TOOL_OPTIONS name; this command (or a tool it runs) ignored them and tried a direct "
                         "connection, which a run's pod does not have. Configure the tool to use the proxy (Maven: a "
                         "<proxy> in settings.xml); the project's own code that connects directly cannot be "
                         "configured: fetch what it needs in a setup command, or leave that test to CI.")
            if not records:
                notes.append("nothing in this step went through the proxy: a tool or the project's own code "
                             "connected, or resolved a name, directly. In a run public names do resolve, and the "
                             "connection is then dropped.")
        return "\n".join(notes)

    def network_summary(self):
        """What the proxy saw in the whole check (never fails)."""
        records, _ = self.records(0)
        lines = []

        def hosts_of(items):
            counts = {}
            for record in items:
                host = (record.get("host") or "").lower()
                counts[host] = counts.get(host, 0) + 1
            return counts

        def counted(counts):
            return ", ".join("%s (%d request%s)" % (h, n, "" if n == 1 else "s") for h, n in counts.items())

        def remarks(counts):
            out = []
            for host in counts:
                kind, text = host_remark(host, self.pod_ip)
                if kind:
                    out.append("  %s" % text if kind != "fake" else "  %s: %s" % (host, text))
            return out

        setup = [r for r in records if r.get("phase") == "setup"]
        agent = [r for r in records if r.get("phase") == "agent"]
        refused_agent = hosts_of(r for r in agent if not r.get("allowed"))
        refused_setup = hosts_of(r for r in setup if not r.get("allowed"))
        if refused_agent:
            lines.append("refused in the agent phase: %s" % counted(refused_agent))
            lines += remarks(refused_agent)
        if refused_setup:
            lines.append("refused in setup: %s" % counted(refused_setup))
            lines += remarks(refused_setup)
        errors = {}
        for record in records:
            if record.get("allowed") and record.get("reason"):
                host = (record.get("host") or "").lower()
                count, reason = errors.get(host, (0, record.get("reason")))
                errors[host] = (count + 1, reason)
        for host, (count, reason) in errors.items():
            lines.append(proxy_error_line(host, count, reason, self.pod_ip))
        reached_setup = list(hosts_of(r for r in setup if r.get("allowed") and not r.get("reason")))
        if reached_setup:
            lines.append("reached in setup: %s" % ", ".join(reached_setup))
        reached_agent = list(hosts_of(r for r in agent if r.get("allowed") and not r.get("reason")))
        if reached_agent:
            lines.append("reached in the agent phase: %s" % ", ".join(reached_agent))
        setup_only = [h for h in reached_setup if not match_host(self.agent_hosts, h)]
        if setup_only:
            lines.append("reached in setup only: %s" % ", ".join(setup_only))
            lines.append("  the agent phase cannot reach them: add to network.allow those the agent needs "
                         "(to add or update a dependency, to download a browser)")
        repos = list(hosts_of(r for r in agent if r.get("allowed") and r.get("via") == "repository"
                              and not r.get("reason")))
        if repos:
            lines.append("repository hosts reached in the agent phase: %s" % ", ".join(repos))
            lines.append("  in a run only git reaches them, through Exradius's route; other requests to them are "
                         "refused unless network.allow lists them")
        private = list(hosts_of(r for r in records if r.get("allowed") and r.get("via") == "allow-private"))
        if private:
            lines.append("private addresses allowed by --allow-private: %s" % ", ".join(private))
            lines.append("  the install's egress.allowPrivate must list them")
        if not lines:
            lines.append("no host reached" + ("" if records else " (no request went through the proxy)"))
        if self.cache_warm:
            lines.append("the caches were warm: hosts whose downloads came from a cache are not listed")
        return "\n".join(lines)

    def measure_disk(self):
        """What the run left on its volume (/work, the home, /tmp; the caches are the
        project volume's, counted apart): the admin's "disk per run". None when the volumes
        were never prepared."""
        if not self.volumes or not self.images.get("workstation"):
            return None
        keys = ("work", "home", "tmp")
        cmd = ["docker", "run", "--rm", "--user", "0:0", "--network", "none", "--label", self.label]
        for key in keys:
            cmd += ["--mount", "type=volume,source=%s,target=/v/%s,readonly,volume-nocopy" % (self.volume(key), key)]
        cmd += self.platform_args() + ["--entrypoint", "du", self.images["workstation"], "-sk"] + ["/v/" + k for k in keys]
        code, out = run(cmd, timeout=600)
        sizes = {}
        for row in out.splitlines():
            match = re.match(r"^(\d+)\s+/v/(\w+)$", row.strip())
            if match:
                sizes[match.group(2)] = int(match.group(1)) * 1024
        if code != 0 or len(sizes) != len(keys):
            self.log.write("disk: could not measure the volumes (exit %d):\n%s\n" % (code, out))
            return None
        caches = None
        if self.cache_dir and os.path.isdir(self.cache_dir):
            code, out = run(["du", "-sk", self.cache_dir], timeout=600)
            first = out.split("\t")[0].strip()
            if code == 0 and first.isdigit():
                caches = int(first) * 1024
        return disk_text(sizes, caches)

    def leftovers(self):
        """What the check's label still names: ``{kind: [names]}``, None for a kind Docker did
        not answer for."""
        out = {}
        for kind, cmd in (("containers", ["docker", "ps", "-aq", "--filter", "label=" + self.label]),
                          ("networks", ["docker", "network", "ls", "-q", "--filter", "label=" + self.label]),
                          ("volumes", ["docker", "volume", "ls", "-q", "--filter", "label=" + self.label])):
            code, text = run(cmd, timeout=60)
            out[kind] = text.split() if code == 0 else None
        return out

    def cleanup(self):
        before = self.leftovers()
        if before["containers"]:
            run(["docker", "rm", "-f", "-v"] + before["containers"], timeout=120)
        if before["networks"]:
            run(["docker", "network", "rm"] + before["networks"], timeout=60)
        if before["volumes"] and not self.args.keep:
            run(["docker", "volume", "rm"] + before["volumes"], timeout=120)
        removed_root = False
        if self.root and os.path.isdir(self.root):
            try:
                shutil.rmtree(self.root)
            except OSError:
                # Files the run user wrote may not be removable by this user (Linux): remove
                # them as root in a throwaway container.
                image = self.images.get("workstation")
                if image:
                    run(["docker", "run", "--rm", "--user", "0:0", "--mount",
                         "type=bind,source=%s,target=/x" % self.root, "--entrypoint", "/bin/sh", image,
                         "-c", "rm -rf /x/* /x/.[!.]*"])
                shutil.rmtree(self.root, ignore_errors=True)
            removed_root = not os.path.isdir(self.root)
        after = self.leftovers()
        removed = []
        still = []
        for kind in ("containers", "networks", "volumes"):
            was, now = before[kind] or [], after[kind]
            if now is None:
                still.append("%s: Docker did not answer, check with docker %s ls --filter label=%s"
                             % (kind, kind[:-1] if kind != "containers" else "container", self.label))
                continue
            gone = [n for n in was if n not in now]
            if gone:
                removed.append("%d %s" % (len(gone), kind if len(gone) != 1 else kind[:-1]))
            if now and not (kind == "volumes" and self.args.keep):
                still.append("%s %s" % (kind, " ".join(now)))
        if removed_root:
            removed.append("the temporary folder")
        if removed:
            self.say("DONE  Clean up: removed %s" % ", ".join(removed))
        if still:
            self.say("WARN  Clean up: still there: %s" % "; ".join(still))
            self.say("      remove them with docker rm -f, docker network rm and docker volume rm")
        kept = after["volumes"] if self.args.keep else None
        if kept:
            mounts = dict(VOLUMES)
            ordered = [self.volume(k) for k, _ in VOLUMES if self.volume(k) in kept]
            self.say("DONE  Clean up: kept the volumes as asked: %s" % ", ".join(
                "%s (%s)" % (self.volume(k), mounts[k]) for k, _ in VOLUMES if self.volume(k) in kept))
            look = ["docker", "run", "--rm", "-it", "--user", "%d:%d" % (RUN_UID, RUN_GID), "-e", "HOME=" + HOME_DIR,
                    "-w", WORK_DIR]
            for key, target in VOLUMES:
                if self.volume(key) in kept:
                    look += ["-v", "%s:%s" % (self.volume(key), target)]
            look += ["--entrypoint", "bash", self.images.get("workstation") or "<workstation image>"]
            self.say("      look inside: %s" % " ".join(shlex.quote(a) for a in look))
            self.say("      remove them: docker volume rm %s" % " ".join(ordered))


ALLOWLIST_REFUSAL = re.compile(r"(\S+) is not on this task's network allowlist")
NO_PROXY_USED = re.compile(
    r"Connect(?:ion)? timed out|connect: connection timed out|ETIMEDOUT|Network is unreachable|ENETUNREACH"
    r"|connect to \S+ port \d+ (?:after \d+ ms: )?(?:Connection timed out|Couldn't connect)", re.I)
"""A tool that ignored the proxy (``diagnose.NO_PROXY_USED`` in Exradius)."""
NO_NAME = re.compile(r"Could not resolve host|couldn't resolve|getaddrinfo|ENOTFOUND|EAI_AGAIN|fetch failed|NoIPsError"
                     r"|Temporary failure in name resolution|Name or service not known|No such host is known"
                     r"|Failed to connect to (?!localhost |127\.|::1 )\S+ port \d+|No route to host|EHOSTUNREACH")
"""A direct connection on the check's internal network: names do not resolve and addresses
have no route (in a run's pod public names resolve and the connection times out instead,
NO_PROXY_USED). Ruby's resolver, Node's fetch and .NET word it their own ways."""


LOCAL_TARGET = re.compile(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\b|\[::1\]", re.I)
"""A line about a connection to the run's own loopback, which never goes to the proxy."""


def proxy_error_line(host, count, reason, pod_ip=None):
    """A request the proxy allowed but could not complete (no address, no connection)."""
    kind, remark = host_remark(host, pod_ip)
    return "could not reach (proxy error): %s (%d): %s%s" % (host, count, reason, "; " + remark if kind == "fake" else "")


CACHE_DIR_NAME = ".exradius-cache"
"""Where earlier versions kept the local stand-in for the project volume's caches/ (next to
exradius.yaml); still used when it exists. The default is ~/.cache/exradius-check/<slug>."""


def memory_flags(memory):
    """A declared memory is the container's cgroup limit, swap included (run-environment §4)."""
    return ["--memory", memory, "--memory-swap", memory] if memory else []


def cache_summary(cache_dir, entries):
    """``(text, warm)``: the size of each ``workstation.cache`` folder (``~/.cache/uv 236 MB,
    ...``), and whether any holds something."""
    parts = []
    warm = False
    for index, entry in enumerate(entries):
        folder = os.path.join(cache_dir, cache_folder(index, entry["path"]))
        size = 0
        empty = True
        if os.path.isdir(folder):
            try:
                empty = not os.listdir(folder)
            except OSError:
                empty = True
            if not empty:
                code, out = run(["du", "-sk", folder])
                first = out.split("\t")[0].strip()
                size = int(first) * 1024 if code == 0 and first.isdigit() else 0
        warm = warm or not empty
        parts.append("%s %s" % (entry["path"], size_text(size, empty)))
    return ", ".join(parts), warm


BUILDKIT_STEP = re.compile(r"^#\d+ \[[^\]]*\] ((?:RUN|COPY|ADD)\s.*)$")
BUILDKIT_PREFIX = re.compile(r"^#\d+ (?:\d+\.\d+ )?")


def failed_instruction(output):
    """The Dockerfile step that failed and its first error line, from a BuildKit (plain
    progress) or a classic build log."""
    step = None
    for line in output.splitlines():
        if line.startswith("Step ") and "/" in line.split(":")[0]:
            step = line.strip()
        elif BUILDKIT_STEP.match(line.strip()):
            step = line.strip()
    if step and step.startswith("#"):
        lines = output.splitlines()
        after = [BUILDKIT_PREFIX.sub("", l.strip()) for l in lines[[i for i, l in enumerate(lines) if l.strip() == step][-1] + 1:]]
        error = next((l for l in after if any(w in l.lower() for w in ("error", "not found", "failed", "denied",
                                                                     "unable", "no such", "e: ", "cannot"))), None)
        return ":\n%s%s" % (BUILDKIT_STEP.match(step).group(1), "\n" + error if error else "")
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


EPILOG = """\
Each --run and --run-file runs in its own container once the setup commands have passed,
even after an earlier one failed. Processes a command leaves in the background die with its
container: nothing needs stopping or cleaning up after it for the check.

/work, the home, /tmp and the state directory are Docker volumes (a Linux filesystem, as the
run's volume is); --keep keeps them. Each setup and --run step shows its peak memory (the
largest working set sampled in its container, page cache not counted); the summary compares
the highest with resources.memory.
"""


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="check.sh",
        description="Check an Exradius project config locally with Docker, the way Verify does.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.set_defaults(runs=[])
    parser.add_argument("config", nargs="?", default=".",
                        help="the config repository folder (with exradius.yaml or .exradius/exradius.yaml), "
                             "or the file itself; default: the current folder")
    parser.add_argument("--repo", action="append", default=[], metavar="NAME=PATH",
                        help="local checkout of the repository named NAME in the config (repeatable)")
    parser.add_argument("--repos-dir", action="append", default=[], metavar="DIR",
                        help="folder holding checkouts named like the config's repositories (repeatable)")
    parser.add_argument("--live", action="store_true",
                        help="mount the local checkouts as they are (uncommitted changes, existing "
                             "node_modules or .venv included) instead of a fresh clone of them; a host mount, "
                             "so on macOS no Unix sockets in it, and slower")
    parser.add_argument("--validate", action="store_true", help="only read and validate the file; no Docker")
    parser.add_argument("--run", action="append", dest="runs", type=lambda v: ("cmd", v), metavar="CMD",
                        help="after the setup commands, run CMD in the workstation in the agent phase, under the "
                             "agent's network (repeatable): the test and lint commands the notes name. CMD runs as "
                             "/bin/sh -c CMD: bash syntax needs bash -c '...' or --run-file")
    parser.add_argument("--run-file", action="append", dest="runs", type=lambda v: ("file", v), metavar="PATH",
                        help="a script on this machine, copied into the run and executed from /work in the agent "
                             "phase by its #! line (default /bin/sh); repeatable, in order with --run: for starting "
                             "the application in the background, waiting for it and probing it")
    parser.add_argument("--allow", action="append", default=[], metavar="HOST",
                        help="a host of the install's egress.allow, which the agent phase may reach on top of "
                             "network.allow (repeatable)")
    parser.add_argument("--allow-private", action="append", default=[], metavar="HOST_OR_CIDR",
                        help="a private address the proxy may reach, as the install's egress.allowPrivate: a CIDR, "
                             "an address or host[:port] (repeatable)")
    parser.add_argument("--open-network", action="store_true",
                        help="do not emulate the run's network: no egress proxy, every container reaches the "
                             "network directly")
    parser.add_argument("--proxy-image", default=PROXY_IMAGE, metavar="IMAGE",
                        help="the image the egress proxy runs in (Python 3.8 or later; default %s)" % PROXY_IMAGE)
    parser.add_argument("--platform", help="build and run for this platform, e.g. linux/amd64 (the cluster's)")
    parser.add_argument("--pull", action="store_true", help="pull images named with image: even when present locally")
    parser.add_argument("--no-cache", action="store_true", help="build without the Docker layer cache")
    parser.add_argument("--buildkit", action="store_true", help=argparse.SUPPRESS)  # builds always use BuildKit
    parser.add_argument("--keep", action="store_true",
                        help="keep the Docker volumes of /work, the home, /tmp and the state directory, and print "
                             "how to look inside and how to remove them")
    parser.add_argument("--cache-dir", metavar="DIR",
                        help="where workstation.cache folders live between checks (default: "
                             "~/.cache/exradius-check/<project slug>, or .exradius-cache next to exradius.yaml "
                             "when that exists; delete it to start cold)")
    parser.add_argument("--log", metavar="PATH",
                        help="write the full log to PATH (default: ~/.cache/exradius-check/logs/"
                             "<slug>-<date>-<time>.log, the newest %d per project kept)" % LOG_KEEP)
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
    runs = []
    for kind, value in args.runs:
        if kind == "file":
            value = os.path.abspath(os.path.expanduser(value))
            if not os.path.isfile(value):
                parser.error("--run-file: %s is not a file" % value)
        runs.append((kind, value))
    args.runs = runs
    args.run = [value for kind, value in runs if kind == "cmd"]
    args.repos_dir = [os.path.expanduser(d) for d in args.repos_dir]
    args.allow = [h.strip() for item in args.allow for h in item.split(",") if h.strip()]
    args.allow_private = [h.strip() for item in args.allow_private for h in item.split(",") if h.strip()]
    if args.open_network and (args.allow or args.allow_private):
        parser.error("--allow and --allow-private need the emulated network (no --open-network)")
    if args.cache_dir:
        args.cache_dir = os.path.abspath(os.path.expanduser(args.cache_dir))
    return args


def main(argv=None):
    if sys.version_info < (3, 8):
        sys.stderr.write("exradius check needs Python 3.8 or later\n")
        return 2
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--egress-proxy"]:
        # The proxy's own process, in its container (start_proxy).
        if len(argv) != 2:
            sys.stderr.write("usage: exradius_check.py --egress-proxy STATE_DIR\n")
            return 2
        return egress_proxy_main(argv[1])
    args = parse_args(argv)
    return Check(args).main()


if __name__ == "__main__":
    sys.exit(main())
