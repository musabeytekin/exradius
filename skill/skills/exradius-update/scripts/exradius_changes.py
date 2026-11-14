#!/usr/bin/env python3
"""What changed in a project's code since its Exradius config was last checked against it.

    exradius_changes.py [options] <config dir or exradius.yaml>

For each repository of the config with a local checkout, it compares two commits and
lists only the changes that can make the config stale, grouped by the part of the config
they touch: tool versions, package managers and workspaces, registries, services, env
names, CI, scripts, the team's guide, and the files the config itself names in setup,
notes, roles and check/start-app.sh.

The first commit is the one the config was last compared with: ``check/reviewed.json``
next to exradius.yaml holds it (``--record`` writes the file). Without the file it is
estimated: the last commit of the code before the config's own last commit. The second is
the head of the branch the config names, as the checkout knows it (``origin/<branch>``;
``--fetch`` asks the remote first).

It reads git only. It prints names and versions, never the content of an env file or a
registry configuration, and it changes nothing but the record file, and that only with
``--record``.

Standard library only, Python 3.8 or later. The YAML reader and the validator are the
local check's (../../exradius-project/scripts/exradius_check.py).
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import os
import posixpath
import re
import subprocess
import sys
import time
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK_SCRIPT = os.path.join(HERE, "..", "..", "exradius-project", "scripts", "exradius_check.py")
PLUGIN_JSON = os.path.join(HERE, "..", "..", "..", ".claude-plugin", "plugin.json")
RECORD = "check/reviewed.json"
"""Next to exradius.yaml: the commit of each repository the config was last compared with."""


def load_check():
    spec = importlib.util.spec_from_file_location("exradius_check", CHECK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["exradius_check"] = module
    spec.loader.exec_module(module)
    return module


def skill_version():
    try:
        with open(PLUGIN_JSON, encoding="utf-8") as handle:
            return str(json.load(handle)["version"])
    except Exception:  # noqa: BLE001 - a script copied out of the plugin
        return "unknown"


def git(args, cwd=None):
    """``(exit code, stdout, stderr)``; stdout is not stripped."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    proc = subprocess.run(["git"] + list(args), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace").strip()


def git_line(args, cwd=None):
    code, out, _err = git(args, cwd)
    return out.strip() if code == 0 else ""


def cut(text, limit=100):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def some(names, limit=15):
    names = list(names)
    return ", ".join(names[:limit]) + (" and %d more" % (len(names) - limit) if len(names) > limit else "")


# ---------------------------------------------------------------------------------------
# What a changed path means
# ---------------------------------------------------------------------------------------

GROUPS = OrderedDict([
    ("named", ("Named by the config", "setup, notes, roles and check/start-app.sh name these")),
    ("versions", ("Tool versions", "the workstation Dockerfile or image, setup")),
    ("managers", ("Package managers and workspaces", "setup, workstation.cache, network.allow, notes")),
    ("registries", ("Registries", "network.allow, secret names, the README; content not shown")),
    ("services", ("Services and images", "services, the Dockerfile's system packages, env")),
    ("env", ("Environment", "env, ${secret:...} names, the README's keys; names only")),
    ("ci", ("CI", "setup, the commands in notes, services, tool versions")),
    ("commands", ("Scripts and task runners", "setup, notes")),
    ("guide", ("The team's guide and delivery", "notes, roles; a question when it restricts agents")),
    ("clone", ("Clone", "submodules and LFS: the clone fetches neither")),
    ("folders", ("New top-level folders", "a new application or workspace: setup, notes")),
    ("dependencies", ("Dependencies", "a new native dependency may need a system package; the check shows it")),
])

SKIP_DIRS = {"node_modules", "vendor", "third_party", "testdata", "__fixtures__"}

VERSION_FILES = {".nvmrc", ".node-version", ".python-version", ".ruby-version", ".java-version", ".go-version",
                 ".bun-version", ".terraform-version", ".tool-versions", "mise.toml", ".mise.toml", ".sdkmanrc",
                 "rust-toolchain"}
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb",
             "uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock", "Gemfile.lock", "Cargo.lock", "go.sum",
             "composer.lock", "mix.lock", "packages.lock.json", "gradle.lockfile"}
MANIFESTS = ["package.json", "pyproject.toml", "requirements*.txt", "setup.py", "Pipfile", "go.mod", "Cargo.toml",
             "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "Gemfile",
             "composer.json", "mix.exs", "*.csproj", "*.fsproj", "*.sln"]
WORKSPACES = {"pnpm-workspace.yaml", "turbo.json", "nx.json", "go.work", "lerna.json", "rush.json"}
REGISTRIES = {".npmrc", ".yarnrc", ".yarnrc.yml", "pip.conf", "uv.toml", ".pypirc", "settings.xml",
              "nuget.config", "bunfig.toml"}
REGISTRY_PATHS = (".cargo/config.toml", ".cargo/config", ".bundle/config")
TASK_RUNNERS = ["Makefile", "makefile", "GNUmakefile", "justfile", "Justfile", "Taskfile.yml", "Taskfile.yaml",
                "Procfile", "Procfile.*", "tox.ini", "noxfile.py"]
ENTRY_SCRIPTS = ["bin/setup", "bin/dev", "bin/ci", "script/setup", "script/bootstrap", "script/test",
                 "script/server", "scripts/setup*", "scripts/bootstrap*", "scripts/dev*", "scripts/test*"]
CI_PREFIXES = (".github/workflows/", ".github/actions/", ".circleci/", ".forgejo/workflows/", ".gitea/workflows/",
               ".buildkite/", ".gitlab/ci/", ".woodpecker/")
CI_FILES = [".gitlab-ci.yml", "Jenkinsfile*", "azure-pipelines*.yml", ".woodpecker*.yml", ".woodpecker*.yaml",
            ".travis.yml", "bitbucket-pipelines.yml", ".drone.yml"]
GUIDES = {"agents.md", "claude.md", "contributing.md", "codeowners", "pull_request_template.md"}
HOOKS = ["lefthook.yml", ".lefthook.yml", ".pre-commit-config.yaml", "commitlint.config.*", ".commitlintrc*"]
DOCKERFILES = ["Dockerfile", "Dockerfile.*", "*.Dockerfile", "Containerfile"]

M = re.MULTILINE
FIELDS = [
    (["go.mod"], [("go", re.compile(r"^go\s+(\S+)", M)), ("toolchain", re.compile(r"^toolchain\s+(\S+)", M))]),
    (["pyproject.toml"], [("requires-python", re.compile(r"^requires-python\s*=\s*[\"']([^\"']+)", M)),
                          ("uv required-version", re.compile(r"^required-version\s*=\s*[\"']([^\"']+)", M))]),
    (["uv.lock"], [("requires-python", re.compile(r"^requires-python\s*=\s*[\"']([^\"']+)", M))]),
    (["Gemfile"], [("ruby", re.compile(r"^ruby\s+(.+)$", M))]),
    (["Gemfile.lock"], [("RUBY VERSION", re.compile(r"^RUBY VERSION\n\s+(.+)$", M)),
                        ("BUNDLED WITH", re.compile(r"^BUNDLED WITH\n\s+(\S+)", M))]),
    (["composer.json"], [("php", re.compile(r"\"php\"\s*:\s*\"([^\"]+)\""))]),
    (["mix.exs"], [("elixir", re.compile(r"elixir:\s*\"([^\"]+)\""))]),
    (["pom.xml"], [("java", re.compile(r"<(?:java\.version|maven\.compiler\.(?:release|source|target))>([^<]+)<"))]),
    (["build.gradle", "build.gradle.kts"],
     [("java", re.compile(r"(?:JavaLanguageVersion\.of|jvmToolchain)\((\d+)\)"
                          r"|(?:source|target)Compatibility\s*=?\s*(?:JavaVersion\.VERSION_)?['\"]?([\w.]+)"))]),
    (["*.csproj", "*.fsproj", "Directory.Build.props"],
     [("TargetFramework", re.compile(r"<TargetFrameworks?>([^<]+)<"))]),
    (["Cargo.toml"], [("rust-version", re.compile(r"^rust-version\s*=\s*\"([^\"]+)\"", M))]),
    (["rust-toolchain.toml"], [("channel", re.compile(r"^channel\s*=\s*\"([^\"]+)\"", M))]),
    (["global.json"], [("sdk", re.compile(r"\"version\"\s*:\s*\"([^\"]+)\""))]),
    (["pnpm-lock.yaml"], [("lockfileVersion", re.compile(r"^lockfileVersion:\s*['\"]?([\w.]+)", M))]),
    (["package-lock.json", "npm-shrinkwrap.json"], [("lockfileVersion", re.compile(r"\"lockfileVersion\"\s*:\s*(\d+)"))]),
    (["gradle-wrapper.properties", "maven-wrapper.properties"],
     [("distributionUrl", re.compile(r"^distributionUrl=(.+)$", M))]),
    (DOCKERFILES, [("FROM", re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", M | re.IGNORECASE))]),
    ([".gitattributes"], [("LFS patterns", re.compile(r"^(\S+)\s.*\bfilter=lfs\b", M))]),
]
CI_VALUE = re.compile(r"^\s*-?\s*([\w.-]*version(?:-file)?|image)\s*:\s*(.+?)\s*$", M)
ENV_NAME = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", M)
ENV_COMMENTED = re.compile(r"^\s*#\s?([A-Z][A-Z0-9_]{2,})=", M)
TOKEN_SPLIT = re.compile(r"[\s\"'`=,;()<>\[\]{}|&]+")
TOKEN = re.compile(r"^[\w@.+-][\w@.+/-]*$")


def matches(name, patterns):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def extract(path, text):
    """``{label: value}`` of the version fields a file of this name carries."""
    base = posixpath.basename(path)
    found = OrderedDict()
    for patterns, extractors in FIELDS:
        if not matches(base, patterns):
            continue
        for label, regex in extractors:
            values = []
            for hit in regex.findall(text or ""):
                value = hit if isinstance(hit, str) else next((part for part in hit if part), "")
                value = value.strip().strip("\"'")
                if value and value not in values:
                    values.append(value)
            if values:
                found[label] = ", ".join(values)
    return found


def is_env_file(base):
    low = base.lower()
    return (low.startswith(".env") or low.endswith(".env") or ".env." in low
            or low in ("env.example", "env.sample", "env.template"))


def env_names(text):
    names = []
    for regex in (ENV_NAME, ENV_COMMENTED):
        for name in regex.findall(text or ""):
            if name not in names:
                names.append(name)
    return names


def is_compose(base):
    low = base.lower()
    return low.endswith((".yml", ".yaml")) and (low.startswith("docker-compose") or low.startswith("compose"))


def compose_services(check, text):
    """``{service: image or "build <context>"}``, or None when the file cannot be read."""
    try:
        data = check.load_yaml(text)
    except Exception:  # noqa: BLE001 - anchors, merge keys: the reader is exradius.yaml's
        return None
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return None
    out = OrderedDict()
    for name, body in services.items():
        body = body if isinstance(body, dict) else {}
        if isinstance(body.get("image"), str):
            out[str(name)] = body["image"]
        elif "build" in body:
            build = body["build"]
            context = build.get("context", ".") if isinstance(build, dict) else build
            out[str(name)] = "build %s" % context
        else:
            out[str(name)] = "no image"
    return out


def ci_values(text):
    values = set()
    for key, value in CI_VALUE.findall(text or ""):
        value = value.split(" #")[0].strip().strip("\"'")
        if value and not value.startswith(("{", "[", "|", ">")):
            values.add("%s: %s" % (key, value))
    return values


def package_json(old, new):
    """``(version lines, script lines, dependency lines)`` between two package.json texts."""
    try:
        a = json.loads(old) if old else {}
        b = json.loads(new) if new else {}
    except ValueError:
        return [], [], []
    if not isinstance(a, dict) or not isinstance(b, dict):
        return [], [], []
    versions, scripts, deps = [], [], []
    for key in ("packageManager", "engines", "volta", "workspaces"):
        if a.get(key) != b.get(key):
            versions.append("%s: %s -> %s" % (key, cut(json.dumps(a.get(key)), 60), cut(json.dumps(b.get(key)), 60)))
    sa = a.get("scripts") if isinstance(a.get("scripts"), dict) else {}
    sb = b.get("scripts") if isinstance(b.get("scripts"), dict) else {}
    added = [name for name in sb if name not in sa]
    removed = [name for name in sa if name not in sb]
    if added:
        scripts.append("scripts added: %s" % some(added))
    if removed:
        scripts.append("scripts removed: %s" % some(removed))
    for name in sb:
        if name in sa and sa[name] != sb[name]:
            scripts.append("script %s: %s -> %s" % (name, cut(sa[name], 70), cut(sb[name], 70)))

    def names(doc):
        out = set()
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            if isinstance(doc.get(key), dict):
                out.update(doc[key])
        return out

    gained, lost = sorted(names(b) - names(a)), sorted(names(a) - names(b))
    if gained:
        deps.append("added: %s" % some(gained))
    if lost:
        deps.append("removed: %s" % some(lost))
    return versions, scripts, deps


# ---------------------------------------------------------------------------------------
# The config and the repositories
# ---------------------------------------------------------------------------------------

class Repo(object):
    def __init__(self, name, url, branch):
        self.name, self.url, self.branch = name, url, branch
        self.path = None  # the local checkout
        self.until = self.until_ref = self.base = self.base_how = None
        self.head = None
        self.notes = []  # about the checkout and the comparison
        self.findings = OrderedDict()  # group -> lines
        self.commits = self.files = 0
        self.quiet_locks = 0
        self.same_as_config = False

    def add(self, group, line):
        self.findings.setdefault(group, [])
        if line not in self.findings[group]:
            self.findings[group].append(line)

    def show(self, rev, path):
        code, out, _err = git(["show", "%s:%s" % (rev, path)], self.path)
        return out if code == 0 else None

    def tree(self, rev):
        """``(files, folders)`` at a commit."""
        code, out, _err = git(["ls-tree", "-r", "--name-only", "-z", rev], self.path)
        files = set(p for p in out.split("\0") if p) if code == 0 else set()
        folders = set()
        for path in files:
            while "/" in path:
                path = path.rsplit("/", 1)[0]
                if path in folders:
                    break
                folders.add(path)
        return files, folders


def config_repositories(raw):
    out = []
    items = raw.get("repositories") if isinstance(raw, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("url"), str):
            branch = item.get("branch")
            out.append(Repo(item["name"], item["url"], branch if isinstance(branch, str) else None))
    return out


def locate(check, repo, args, config_root):
    """The local checkout of a repository: --repo, --repos-dir, the config's own repository
    or a folder next to it, the last two only when their origin is the config's URL."""
    if repo.name in args.repo_map:
        return args.repo_map[repo.name]
    for base in args.repos_dir:
        candidate = os.path.join(base, repo.name)
        if os.path.isdir(candidate):
            return candidate
    wanted = check.normalise_url(repo.url)
    for candidate in (config_root, os.path.join(os.path.dirname(config_root), repo.name), os.getcwd()):
        if os.path.isdir(candidate):
            origin = git_line(["remote", "get-url", "origin"], candidate)
            if origin and check.normalise_url(origin) == wanted:
                return git_line(["rev-parse", "--show-toplevel"], candidate) or candidate
    return None


def resolve_until(repo):
    """The commit a run would clone, as far as the checkout knows it."""
    refs = []
    if repo.branch:
        refs += ["refs/remotes/origin/%s" % repo.branch, "refs/heads/%s" % repo.branch]
    else:
        refs += ["refs/remotes/origin/HEAD"]
    refs.append("HEAD")
    for ref in refs:
        sha = git_line(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], repo.path)
        if sha:
            return ref.replace("refs/remotes/", "").replace("refs/heads/", ""), sha
    return None, None


def describe(repo, sha):
    return git_line(["log", "-1", "--format=%h %cs", sha], repo.path) or sha[:12]


def config_last_change(config_root, config_dir, config_path):
    """``(commit or None, ISO date, its history in a few words)`` of the config's own last change."""
    lines = git_line(["log", "--format=%H %cI", "--", config_dir or "."], config_root).splitlines()
    if lines:
        sha, date = lines[0].split(" ", 1)
        history = "its only commit" if len(lines) == 1 else \
            "the last of %d commits since %s" % (len(lines), lines[-1].split(" ", 1)[1][:10])
        return sha, date, history
    return None, time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(os.path.getmtime(config_path))), ""


def read_record(directory):
    """``(record or None, warning or None)``."""
    path = os.path.join(directory, *RECORD.split("/"))
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("repositories"), dict):
            raise ValueError("no repositories")
        return data, None
    except (OSError, ValueError) as exc:
        return None, "%s cannot be read (%s): the baseline is estimated instead" % (RECORD, exc)


def resolve_base(repo, args, record, config_commit, config_date):
    def commit(rev):
        return git_line(["rev-parse", "--verify", "--quiet", rev + "^{commit}"], repo.path)

    if repo.name in args.since_map:
        given = args.since_map[repo.name]
        sha = commit(given)
        if not sha and re.match(r"^\d{4}-\d{2}-\d{2}", given):
            sha = git_line(["rev-list", "-1", "--before=%s" % given, repo.until], repo.path)
        if not sha:
            raise SystemExit("--since %s: %s is neither a commit of %s nor a date with a commit before it"
                             % (repo.name, given, repo.path))
        return sha, "given with --since"
    entry = (record or {}).get("repositories", {}).get(repo.name)
    if isinstance(entry, dict) and isinstance(entry.get("commit"), str):
        sha = commit(entry["commit"])
        if sha:
            return sha, "recorded in %s" % RECORD
        repo.notes.append("the recorded commit %s is not in this checkout (history rewritten, or a shallow clone): "
                          "the baseline is estimated" % entry["commit"][:12])
    if repo.same_as_config and config_commit:
        if git(["merge-base", "--is-ancestor", config_commit, repo.until], repo.path)[0] == 0:
            return config_commit, "estimated: the config's own last commit"
    sha = git_line(["rev-list", "-1", "--before=%s" % config_date, repo.until], repo.path)
    if sha:
        return sha, "estimated: the last commit before the config's last change (%s)" % config_date[:10]
    roots = git_line(["rev-list", "--max-parents=0", repo.until], repo.path).split()
    if roots:
        return roots[-1], "estimated: the first commit (the code is younger than the config)"
    return None, None


# ---------------------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------------------

def mentions(raw, config_file_dir):
    """``[(where, [folders the command changes to], text)]`` from setup, notes, roles and
    the start script."""
    out = []
    workstation = raw.get("workstation") if isinstance(raw.get("workstation"), dict) else {}
    setup = workstation.get("setup") if isinstance(workstation.get("setup"), list) else []
    for index, command in enumerate(setup):
        if isinstance(command, str):
            out.append(("setup[%d]" % index, True, command))
    if isinstance(raw.get("notes"), str):
        out.append(("notes", False, raw["notes"]))
    agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
    for slug, agent in agents.items():
        if isinstance(agent, dict) and isinstance(agent.get("role"), str):
            out.append(("the role of %s" % slug, False, agent["role"]))
    start = os.path.join(config_file_dir, "check", "start-app.sh")
    if os.path.isfile(start):
        try:
            with open(start, encoding="utf-8", errors="replace") as handle:
                out.append(("check/start-app.sh", True, handle.read()))
        except OSError:
            pass
    return out


def named_paths(texts, repo_names):
    """``{(repository, path): [where, ...]}``: every path of a repository that the texts could
    mean. Whether it is a path at all is decided against the tree."""
    out = OrderedDict()

    def strip_work(token):
        token = token[len("/work/"):] if token.startswith("/work/") else token
        return token[2:] if token.startswith("./") else token

    for where, is_command, text in texts:
        folders = [""]
        if is_command:
            for folder in re.findall(r"\bcd\s+([^\s;&|]+)", text):
                folder = strip_work(folder.strip("\"'")).rstrip("/")
                if folder and folder not in folders:
                    folders.append(folder)
        for token in TOKEN_SPLIT.split(text):
            token = strip_work(token.strip()).rstrip(".:,/")
            if len(token) < 3 or not TOKEN.match(token) or ("/" not in token and "." not in token):
                continue
            candidates = [posixpath.normpath(posixpath.join(folder, token)) for folder in folders]
            if not is_command:
                # Notes speak from inside a repository ("from /work/<name>").
                candidates += [posixpath.join(name, token) for name in repo_names]
            for candidate in candidates:
                first, _sep, rest = candidate.partition("/")
                if first in repo_names and rest and not rest.startswith(".."):
                    out.setdefault((first, rest), [])
                    if where not in out[(first, rest)]:
                        out[(first, rest)].append(where)
    return out


def stat_text(status, stat):
    word = {"A": "added", "D": "deleted"}.get(status)
    if word:
        return word
    return "binary" if stat[0] is None else "+%d -%d" % stat


def examine(check, repo, status, path, stat, config_env, config_services):
    """Sort one changed path into the groups it belongs to."""
    parts = path.split("/")
    base = parts[-1]
    low = base.lower()
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return
    old = new = None

    def texts():
        return (repo.show(repo.base, path) if status != "A" else None,
                repo.show(repo.until, path) if status != "D" else None)

    def field_changes():
        before, after = extract(path, old or ""), extract(path, new or "")
        out = []
        for label in list(before) + [k for k in after if k not in before]:
            if before.get(label) != after.get(label):
                out.append("%s %s -> %s" % (label, cut(before.get(label, "none"), 60), cut(after.get(label, "none"), 60)))
        return out

    what = stat_text(status, stat)
    carries_fields = any(matches(base, patterns) for patterns, _e in FIELDS)
    if carries_fields or base in VERSION_FILES or low == "package.json" or is_env_file(base) or is_compose(base):
        old, new = texts()

    if path.startswith(CI_PREFIXES) or matches(base, CI_FILES):
        old, new = texts()
        before, after = ci_values(old), ci_values(new)
        detail = []
        if before - after:
            detail.append("no longer %s" % some(sorted(before - after), 6))
        if after - before:
            detail.append("now %s" % some(sorted(after - before), 6))
        repo.add("ci", "%s (%s)%s" % (path, what, ": " + "; ".join(detail) if detail else ""))
        return
    if base in VERSION_FILES:
        repo.add("versions", "%s: %s -> %s" % (path, cut(old.strip() if old else "none", 60),
                                               cut(new.strip() if new else "none", 60)))
        return
    if is_env_file(base):
        before, after = env_names(old), env_names(new)
        gained = [n for n in after if n not in before]
        lost = [n for n in before if n not in after]

        def mark(names, note):
            return some(["%s (%s)" % (n, note) if n in config_env else n for n in names], 40)

        if gained:
            repo.add("env", "%s: added %s" % (path, mark(gained, "already in the config's env")))
        if lost:
            repo.add("env", "%s: removed %s" % (path, mark(lost, "still in the config's env")))
        return
    if is_compose(base):
        before = compose_services(check, old) if old is not None else {}
        after = compose_services(check, new) if new is not None else {}
        if before is None or after is None:
            repo.add("services", "%s (%s): not read here (anchors or merge keys); read the diff" % (path, what))
            return

        def ours(name, absent=""):
            if name not in config_services:
                return absent
            return "; the config's %s is %s" % (name, config_services[name])

        lines = ["service added: %s (%s%s)" % (n, after[n], ours(n, "; the config has no service of that name"))
                 for n in after if n not in before]
        lines += ["service removed: %s (%s%s)" % (n, before[n], ours(n)) for n in before if n not in after]
        lines += ["%s: %s -> %s%s" % (n, before[n], after[n], ours(n))
                  for n in after if n in before and before[n] != after[n]]
        repo.add("services", "%s (%s)%s" % (path, what, ": " + "; ".join(lines) if lines
                                            else ": the same services and images; other settings changed"))
        return
    if ".devcontainer" in parts[:-1] or low in ("devcontainer.json", ".devcontainer.json"):
        repo.add("services", "%s (%s)" % (path, what))
        return
    if matches(base, DOCKERFILES):
        changes = field_changes()
        repo.add("services", "%s (%s)%s" % (path, what, ": " + "; ".join(changes) if changes else ""))
        return
    if base in REGISTRIES or low in REGISTRIES or path.endswith(REGISTRY_PATHS):
        repo.add("registries", "%s (%s)" % (path, what))
        return
    if base in WORKSPACES:
        repo.add("managers", "%s (%s)" % (path, what))
        return
    if base in LOCKFILES:
        if status in ("A", "D"):
            repo.add("managers", "%s %s: a package manager %s" % (path, what, "is new" if status == "A" else "is gone"))
        changes = field_changes() if status == "M" else []
        for change in changes:
            repo.add("versions", "%s: %s" % (path, change))
        if status == "M" and not changes:
            repo.quiet_locks += 1
        return
    if low == "package.json":
        versions, scripts, deps = package_json(old, new)
        if status in ("A", "D"):
            repo.add("managers", "%s %s: a package or workspace %s" % (path, what, "is new" if status == "A" else "is gone"))
        for line in versions:
            repo.add("versions", "%s: %s" % (path, line))
        for line in scripts if status == "M" else []:
            repo.add("commands", "%s: %s" % (path, line))
        for line in deps if status == "M" else []:
            repo.add("dependencies", "%s: %s" % (path, line))
        return
    if matches(base, MANIFESTS):
        if status in ("A", "D"):
            repo.add("managers", "%s %s: a module or workspace %s" % (path, what, "is new" if status == "A" else "is gone"))
            for label, value in extract(path, new or "").items():
                repo.add("versions", "%s: %s %s" % (path, label, value))
            return
        changes = field_changes() if carries_fields else []
        for change in changes:
            repo.add("versions", "%s: %s" % (path, change))
        repo.add("dependencies", "%s (%s)" % (path, what))
        return
    if carries_fields:
        for change in field_changes():
            repo.add("clone" if base == ".gitattributes" else "versions", "%s: %s" % (path, change))
        return
    if base == ".gitmodules":
        repo.add("clone", "%s (%s)" % (path, what))
        return
    if matches(base, TASK_RUNNERS) or matches(path, ENTRY_SCRIPTS):
        repo.add("commands", "%s (%s)" % (path, what))
        return
    if low in GUIDES or parts[0] == ".claude" or parts[0] == ".husky" or matches(base, HOOKS):
        repo.add("guide", "%s (%s)" % (path, what))


def compare(check, repo, raw, named, config_dir):
    """Fill ``repo.findings`` from the diff between its two commits."""
    repo.commits = int(git_line(["rev-list", "--count", "%s..%s" % (repo.base, repo.until)], repo.path) or 0)
    if git(["merge-base", "--is-ancestor", repo.base, repo.until], repo.path)[0] != 0:
        repo.notes.append("the baseline is not an ancestor of %s (a rewritten or other branch): the two trees are "
                          "compared as they are" % repo.until_ref)
    code, out, err = git(["diff", "--no-renames", "--name-status", "-z", repo.base, repo.until], repo.path)
    if code != 0:
        repo.notes.append("git diff failed: %s" % err)
        return
    fields = out.split("\0")
    changed = OrderedDict((fields[i + 1], fields[i][:1]) for i in range(0, len(fields) - 1, 2))
    code, out, _err = git(["diff", "--no-renames", "--numstat", "-z", repo.base, repo.until], repo.path)
    stats = {}
    for entry in out.split("\0"):
        cells = entry.split("\t", 2)
        if len(cells) == 3:
            stats[cells[2]] = (None, None) if cells[0] == "-" else (int(cells[0]), int(cells[1]))
    prefix = config_dir + "/" if repo.same_as_config and config_dir else None
    if prefix:
        changed = OrderedDict((p, s) for p, s in changed.items() if not p.startswith(prefix))
    repo.files = len(changed)
    if not changed:
        return

    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    services = OrderedDict()
    for name, body in (raw.get("services") if isinstance(raw.get("services"), dict) else {}).items():
        body = body if isinstance(body, dict) else {}
        services[str(name)] = body.get("image") if isinstance(body.get("image"), str) else "built from a Dockerfile"
    for path, status in changed.items():
        examine(check, repo, status, path, stats.get(path, (0, 0)), env, services)

    old_files, old_folders = repo.tree(repo.base)
    new_files, new_folders = repo.tree(repo.until)
    for (name, path), where in named.items():
        if name != repo.name:
            continue
        if path in old_files or path in old_folders:
            if path not in new_files and path not in new_folders:
                repo.add("named", "%s is gone (%s)" % (path, ", ".join(where)))
            elif changed.get(path) == "M":
                repo.add("named", "%s changed, %s (%s)" % (path, stat_text("M", stats.get(path, (0, 0))),
                                                          ", ".join(where)))
    top_old = set(p.split("/")[0] for p in old_folders)
    top_new = set(p.split("/")[0] for p in new_folders)
    for folder in sorted(top_new - top_old):
        if not folder.startswith("."):
            repo.add("folders", "%s/" % folder)


def checkout_state(check, repo):
    origin = git_line(["remote", "get-url", "origin"], repo.path)
    if origin and check.normalise_url(origin) != check.normalise_url(repo.url):
        repo.notes.append("the checkout's origin is %s, the config names %s" % (origin, repo.url))
    default = git_line(["symbolic-ref", "--short", "--quiet", "refs/remotes/origin/HEAD"], repo.path)
    default = default[len("origin/"):] if default.startswith("origin/") else default
    if repo.branch and default and default != repo.branch:
        repo.notes.append("origin's default branch is %s, the config names %s" % (default, repo.branch))
    if repo.branch and origin and not repo.until_ref.startswith("origin/"):
        repo.notes.append("this checkout has no origin/%s: compared up to %s" % (repo.branch, repo.until_ref))
    repo.head = git_line(["rev-parse", "HEAD"], repo.path)
    if repo.head and repo.head != repo.until:
        on = git_line(["rev-parse", "--abbrev-ref", "HEAD"], repo.path)
        repo.notes.append("the checkout is at %s (%s), not at %s: the check clones the checkout as it is"
                          % (describe(repo, repo.head), on, repo.until_ref))
    if git_line(["status", "--porcelain", "--untracked-files=no"], repo.path):
        repo.notes.append("the checkout has uncommitted changes: neither this comparison nor the check sees them")


# ---------------------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------------------

def write_record(directory, repos, previous):
    """Write check/reviewed.json: for each repository the newest commit that is both in the
    checkout's HEAD and in the branch a run clones."""
    entries = OrderedDict()
    lines = []
    old = (previous or {}).get("repositories", {})
    for repo in sorted(repos, key=lambda r: r.name):
        if repo.path is None or not repo.until:
            if isinstance(old.get(repo.name), dict):
                entries[repo.name] = old[repo.name]
                lines.append("%s: no local checkout; the earlier entry is kept" % repo.name)
            else:
                lines.append("%s: no local checkout; not recorded" % repo.name)
            continue
        head = git_line(["rev-parse", "HEAD"], repo.path)
        sha = git_line(["merge-base", head, repo.until], repo.path) if head else ""
        sha = sha or repo.until
        entry = OrderedDict()
        if repo.branch:
            entry["branch"] = repo.branch
        entry["commit"] = sha
        entries[repo.name] = entry
        note = "" if sha == repo.until else " (the checkout is behind or beside %s: later commits are compared " \
                                            "again next time)" % repo.until_ref
        lines.append("%s: %s%s" % (repo.name, describe(repo, sha), note))
    path = os.path.join(directory, *RECORD.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(OrderedDict([("repositories", entries), ("skill", skill_version())]), handle, indent=2)
        handle.write("\n")
    return path, lines


# ---------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="changes.sh",
        description="List what changed in a project's code since its Exradius config was last compared with it.")
    parser.add_argument("config", nargs="?", default=".", help="the config folder or exradius.yaml (default: .)")
    parser.add_argument("--repo", action="append", default=[], metavar="NAME=PATH",
                        help="the local checkout of a repository of the config (repeatable)")
    parser.add_argument("--repos-dir", action="append", default=[], metavar="DIR",
                        help="a folder holding checkouts named as the config's repositories")
    parser.add_argument("--since", action="append", default=[], metavar="NAME=REV|DATE",
                        help="compare this repository from REV (a commit, or a date: the last commit before it) "
                             "instead of the recorded or estimated commit")
    parser.add_argument("--fetch", action="store_true", help="git fetch origin in every checkout first")
    parser.add_argument("--record", action="store_true",
                        help="write %s for the commits the checkouts are at, and stop; commit the file" % RECORD)
    parser.add_argument("--exit-code", action="store_true", help="exit 1 when something was found")
    args = parser.parse_args(argv)
    args.repo_map, args.since_map = {}, {}
    for option, target in (("--repo", args.repo_map), ("--since", args.since_map)):
        for item in getattr(args, option[2:]):
            name, sep, value = item.partition("=")
            if not sep or not name or not value:
                parser.error("%s takes NAME=%s, got %r" % (option, "PATH" if option == "--repo" else "REV", item))
            target[name] = os.path.expanduser(value) if option == "--repo" else value
    for name, path in args.repo_map.items():
        if not os.path.isdir(path):
            parser.error("--repo %s: %s is not a folder" % (name, path))
    args.repos_dir = [os.path.expanduser(d) for d in args.repos_dir]
    return args


def main(argv=None):
    if sys.version_info < (3, 8):
        sys.stderr.write("exradius changes needs Python 3.8 or later\n")
        return 2
    args = parse_args(sys.argv[1:] if argv is None else argv)
    check = load_check()
    say = lambda text="": sys.stdout.write(text + "\n")  # noqa: E731

    config_path, config_root, config_dir = check.find_config(args.config)
    config_file_dir = os.path.dirname(config_path)
    try:
        with open(config_path, encoding="utf-8") as handle:
            raw = check.load_yaml(handle.read())
    except Exception as exc:  # noqa: BLE001 - YamlError or an unreadable file
        sys.stderr.write("%s cannot be read: %s\n" % (config_path, exc))
        return 2
    if not isinstance(raw, dict):
        sys.stderr.write("%s is not a mapping\n" % config_path)
        return 2
    _config, errors, _warnings, _required = check.validate(raw, config_dir, check.make_lookup(config_root))
    repos = config_repositories(raw)
    for name in list(args.repo_map) + list(args.since_map):
        if name not in [r.name for r in repos]:
            sys.stderr.write("the config has no repository named %s (it has: %s)\n"
                             % (name, ", ".join(r.name for r in repos) or "none"))
            return 2
    record, record_warning = read_record(config_file_dir)
    config_commit, config_date, config_history = config_last_change(config_root, config_dir, config_path)

    for repo in repos:
        repo.path = locate(check, repo, args, config_root)
        if repo.path is None:
            continue
        repo.path = os.path.abspath(repo.path)
        if git(["rev-parse", "--git-dir"], repo.path)[0] != 0:
            repo.notes.append("%s is not a git checkout" % repo.path)
            repo.path = None
            continue
        top = git_line(["rev-parse", "--show-toplevel"], repo.path)
        repo.same_as_config = bool(top) and os.path.realpath(top) == os.path.realpath(config_root)
        if args.fetch:
            code, _out, err = git(["fetch", "--quiet", "origin"], repo.path)
            if code != 0:
                repo.notes.append("git fetch origin failed (%s): compared with what the checkout has" % cut(err, 160))
        repo.until_ref, repo.until = resolve_until(repo)

    if args.record:
        path, lines = write_record(config_file_dir, repos, record)
        say("recorded in %s (skill %s):" % (path, skill_version()))
        for line in lines:
            say("  " + line)
        say("Commit the file with the config.")
        return 0

    say("Config: %s" % config_path)
    say("  last changed %s%s" % (config_date[:10], " (commit %s, %s)" % (config_commit[:8], config_history)
                                 if config_commit else " (not committed)"))
    if record:
        written = record.get("skill", "unknown")
        say("  %s: written by skill %s%s" % (RECORD, written, "" if written == skill_version()
                                             else "; this is skill %s" % skill_version()))
    else:
        say("  no %s: the file was written by skill 0.5.0 or earlier (this is skill %s), and the baselines below "
            "are estimated from the config's last change. If that change was a small fix and not a full pass over "
            "the code, give an earlier commit or date: --since NAME=REV|YYYY-MM-DD" % (RECORD, skill_version()))
    if record_warning:
        say("  " + record_warning)
    if errors:
        say("  the file has %d error%s for this release: check.sh --validate lists them"
            % (len(errors), "" if len(errors) == 1 else "s"))
    if git_line(["status", "--porcelain", "--", config_dir or "."], config_root):
        say("  the config folder has uncommitted changes")
    if not repos:
        say("\nThe config has no repositories: nothing to compare.")
        return 0

    named = named_paths(mentions(raw, config_file_dir), [r.name for r in repos])
    found = False
    for repo in repos:
        say("\n== %s (%s%s)" % (repo.name, repo.url, ", branch %s" % repo.branch if repo.branch else ""))
        if repo.path is None or not repo.until:
            for note in repo.notes:
                say("  note: " + note)
            say("  no local checkout: pass --repo %s=PATH (or --repos-dir); not compared" % repo.name)
            found = True
            continue
        repo.base, repo.base_how = resolve_base(repo, args, record, config_commit, config_date)
        checkout_state(check, repo)
        say("  checkout %s" % repo.path)
        if not repo.base:
            say("  no commit to compare from; not compared")
            continue
        say("  since    %s (%s)" % (describe(repo, repo.base), repo.base_how))
        say("  until    %s (%s)" % (describe(repo, repo.until), repo.until_ref))
        if repo.base != repo.until:
            compare(check, repo, raw, named, config_dir)
        for note in repo.notes:
            say("  note: " + note)
        if repo.base == repo.until:
            say("  no commits since: nothing to compare")
            continue
        say("  %d commit%s, %d file%s changed" % (repo.commits, "" if repo.commits == 1 else "s",
                                                  repo.files, "" if repo.files == 1 else "s"))
        if not repo.findings:
            say("  nothing among them that the config depends on")
        for group, (title, touches) in GROUPS.items():
            lines = repo.findings.get(group)
            if not lines:
                continue
            found = True
            say("\n  %s  [%s]" % (title, touches))
            limit = 12 if group == "dependencies" else 40
            for line in lines[:limit]:
                say("    " + line)
            if len(lines) > limit:
                say("    and %d more" % (len(lines) - limit))
        if repo.quiet_locks:
            say("\n  %d lockfile%s changed without a format change: setup installs from them, nothing to do"
                % (repo.quiet_locks, "" if repo.quiet_locks == 1 else "s"))
        if repo.findings:
            say("\n  Read a change:  git -C %s diff %s %s -- <path>" % (repo.path, repo.base[:12], repo.until[:12]))
    return 1 if found and args.exit_code else 0


if __name__ == "__main__":
    sys.exit(main())
