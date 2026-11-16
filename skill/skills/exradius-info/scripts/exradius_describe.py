#!/usr/bin/env python3
"""An Exradius project config as Exradius reads it: every agent, connection, repository,
service, variable, host and secret key, with the defaults filled in and with a plain
"not set in the file" wherever the install decides.

    exradius_describe.py [--agent SLUG] <config dir or exradius.yaml>

It reads the file and git only, changes nothing, and never prints a value: a variable is
its name and whether it is a secret, a credential is the key it references.

Standard library only, Python 3.8 or later. The YAML reader, the validator and the
catalog are the local check's (../../exradius-project/scripts/exradius_check.py).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK_SCRIPT = os.path.join(HERE, "..", "..", "exradius-project", "scripts", "exradius_check.py")
RECORD = ("check", "reviewed.json")

RUNTIME_DEFAULT = "not set in the file: the runtime's default model on that provider"
PROVIDER_DEFAULT = "not set in the file: the install's default provider"


def load_check():
    spec = importlib.util.spec_from_file_location("exradius_check", CHECK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["exradius_check"] = module
    spec.loader.exec_module(module)
    return module


def git_line(args, cwd):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    proc = subprocess.run(["git"] + list(args), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""


def mapping(value):
    return value if isinstance(value, dict) else {}


def text(value):
    return value if isinstance(value, str) else None


def first_sentence(role, limit=220):
    flat = " ".join((role or "").split())
    if not flat:
        return None
    for index, char in enumerate(flat):
        if char in ".!?" and (index + 1 == len(flat) or flat[index + 1] == " ") and index > 20:
            flat = flat[:index + 1]
            break
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def secret_places(check, raw):
    """``{key: [where it is referenced, ...]}`` in the file's order."""
    found = {}

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, path + [str(key)])
        elif isinstance(value, list):
            for index, item in enumerate(value):
                name = text(mapping(item).get("name")) if path == ["repositories"] else None
                walk(item, path + [name or str(index)])
        elif isinstance(value, str):
            for name in check.SECRET_REF.findall(value):
                where = ".".join(path)
                found.setdefault(name, [])
                if where not in found[name]:
                    found[name].append(where)

    walk(raw, [])
    return found


def secrets_in(check, value):
    return sorted(set(check.SECRET_REF.findall(json.dumps(value)))) if value is not None else []


def connection_line(check, name, conn, cfg):
    """One connection: what it is, how it runs, the keys and the hosts it needs."""
    conn = mapping(conn)
    parts = []
    hosts = []
    if text(conn.get("catalog")):
        key = conn["catalog"]
        entry = check.CATALOG.get(key)
        parts.append("catalog %s" % key)
        if entry is None:
            parts.append("not in this release's catalog")
        elif entry["transport"] == "http":
            parts.append("http, %s" % entry["url"])
        else:
            parts.append("stdio, started with %s in the workstation image" % entry["command"])
    elif text(conn.get("url")):
        parts.append("custom http, %s" % conn["url"])
    elif text(conn.get("command")):
        args = [str(a) for a in conn.get("args") or [] if not check.SECRET_REF.search(str(a))]
        parts.append("custom stdio, %s" % " ".join([conn["command"]] + args)[:120])
    else:
        parts.append("neither catalog, command nor url")
    keys = secrets_in(check, conn)
    if keys:
        parts.append("keys %s" % ", ".join(keys))
    if cfg is not None and name in cfg.connections:
        try:
            definition = check.connection_definition(cfg.connections[name])
            hosts = list(check.connection_hosts(cfg.connections[name]))
            if check.proxied(definition):
                parts.append("reached through Exradius's proxy, which adds the secret header")
        except Exception:  # noqa: BLE001 - a connection the validator refused
            hosts = []
    if hosts:
        parts.append("adds hosts %s" % ", ".join(hosts))
    return "; ".join(parts)


def describe(check, raw, cfg, errors, config_path, config_root, only_agent=None):
    out = []
    say = out.append
    project = mapping(raw.get("project"))
    slug = text(project.get("slug")) or "?"
    agents = mapping(raw.get("agents"))
    connections = mapping(raw.get("connections"))
    users = {}
    for agent_slug, agent in agents.items():
        for name in mapping(agent).get("connections") or []:
            users.setdefault(str(name), []).append(str(agent_slug))

    def agent_block(agent_slug, agent):
        agent = mapping(agent)
        name = text(agent.get("name")) or str(agent_slug).capitalize()
        say("  %s  (name: %s%s)" % (agent_slug, name, "" if text(agent.get("name")) else ", the default"))
        say("    model      %s" % (text(agent.get("model")) or RUNTIME_DEFAULT))
        say("    provider   %s" % (text(agent.get("provider")) or PROVIDER_DEFAULT))
        say("    runtime    claude_code (Claude Code through the Agent SDK; the only runtime)")
        identity = mapping(agent.get("identity"))
        email = text(identity.get("email"))
        say("    git author %s <%s>%s" % (text(identity.get("name")) or name,
                                         email or "%s@%s.exradius.local" % (agent_slug, slug),
                                         "" if email else " (the default address)"))
        listed = [str(n) for n in agent.get("connections") or []]
        say("    tools      Claude Code's own and the built-in exradius tools (ask a question, submit the result, "
            "attachments)%s" % ("" if listed else "; no connections"))
        for name in listed:
            if name in connections:
                say("      + %s: %s" % (name, connection_line(check, name, connections[name], cfg)))
            else:
                say("      + %s: not defined under connections" % name)
        role = text(agent.get("role"))
        if role and role.strip():
            count = len(role.strip().splitlines())
            say("    role       %d line%s; opens: %s" % (count, "" if count == 1 else "s", first_sentence(role)))
        else:
            say("    role       none: the agent has Exradius's rules, the environment and the notes only")

    if only_agent is not None:
        if only_agent not in agents:
            raise SystemExit("the config has no agent %s (it has: %s)" % (only_agent, ", ".join(agents) or "none"))
        say("Agent of project %s:" % slug)
        agent_block(only_agent, agents[only_agent])
        role = text(mapping(agents[only_agent]).get("role"))
        if role and role.strip():
            say("")
            say("    The role in full:")
            for line in role.rstrip().splitlines():
                say("      " + line)
        return out

    say("Project %s%s" % (slug, " (%s)" % project["name"] if text(project.get("name")) else ""))
    if text(project.get("description")):
        say("  %s" % " ".join(project["description"].split()))
    say("  file     %s" % config_path)
    commit = git_line(["log", "-1", "--format=%h %cs %s", "--", os.path.dirname(config_path)], config_root)
    branch = git_line(["rev-parse", "--abbrev-ref", "HEAD"], config_root)
    origin = git_line(["remote", "get-url", "origin"], config_root)
    if commit:
        say("  commit   %s (the config's last; branch %s%s)" % (commit[:110], branch,
                                                          ", origin %s" % origin if origin else ", no origin"))
        if git_line(["status", "--porcelain", "--", os.path.dirname(config_path)], config_root):
            say("           with uncommitted changes: Exradius reads the committed file at the branch head")
    else:
        say("  commit   none: the folder is not a git repository or has no commit")
    if errors:
        say("  ERRORS   %d; Exradius rejects the project's tasks while they stand:" % len(errors))
        for error in errors[:20]:
            say("           " + error)

    say("")
    say("Agents (%d)" % len(agents))
    for agent_slug, agent in agents.items():
        agent_block(agent_slug, agent)
    say("  a task that names no agent: %s" % (
        "goes to the only agent" if len(agents) == 1 else
        "Exradius picks one from each agent's name, role and tools; that needs agent routing set up in the "
        "install, and without it such a task is rejected"))

    say("")
    say("Connections (%d)" % len(connections))
    for name, conn in connections.items():
        used = users.get(str(name))
        say("  %s: %s; %s" % (name, connection_line(check, str(name), conn, cfg),
                              "used by %s" % ", ".join(used) if used else "no agent lists it, so no run starts it"))
    if not connections:
        say("  none")

    say("")
    repos = raw.get("repositories") if isinstance(raw.get("repositories"), list) else []
    say("Repositories (%d), each cloned into /work/<name>" % len(repos))
    for repo in repos:
        repo = mapping(repo)
        keys = secrets_in(check, repo.get("credential"))
        say("  %s: %s, branch %s, %s, %s" % (
            repo.get("name"), repo.get("url"), text(repo.get("branch")) or "the remote's default",
            "agents may push" if repo.get("push") is True else "read-only (agents attach a patch)",
            "credential %s" % ", ".join(keys) if keys else "no credential (public)"))
    if not repos:
        say("  none")

    say("")
    station = mapping(raw.get("workstation"))
    say("Workstation")
    if text(station.get("dockerfile")):
        say("  image    built from %s" % station["dockerfile"])
    elif text(station.get("image")):
        say("  image    %s" % station["image"])
    else:
        say("  image    not set in the file: Exradius's default image (Ubuntu 24.04 with git, curl, build-essential)")
    memory = text(mapping(raw.get("resources")).get("memory"))
    say("  memory   %s" % (memory + " (the pod's request and limit)" if memory else "not set: no request, no limit"))
    setup = station.get("setup") if isinstance(station.get("setup"), list) else []
    say("  setup    %d command%s, run in /work before every task" % (len(setup), "" if len(setup) == 1 else "s"))
    for command in setup:
        say("    - %s" % " ".join(str(command).split())[:160])
    cache = station.get("cache") if isinstance(station.get("cache"), list) else []
    if cache:
        say("  cache    kept across runs: %s" % ", ".join(
            str(mapping(c).get("path", c)) if isinstance(c, dict) else str(c) for c in cache))

    services = mapping(raw.get("services"))
    say("")
    say("Services (%d), on the run's localhost by name" % len(services))
    for name, service in services.items():
        service = mapping(service)
        ready = mapping(service.get("ready"))
        how = "command" if "command" in ready else "tcp %s" % ready["tcp"] if "tcp" in ready else \
            "the image's HEALTHCHECK, else running"
        ports = service.get("ports") if isinstance(service.get("ports"), list) else []
        say("  %s: %s, ports %s, ready by %s%s" % (
            name, text(service.get("image")) or "built from %s" % service.get("dockerfile"),
            ", ".join(str(p) for p in ports) or "none listed", how,
            ", memory %s" % service["memory"] if text(service.get("memory")) else ""))
    if not services:
        say("  none")

    env = mapping(raw.get("env"))
    say("")
    say("Environment of setup and the agent (%d; names only)" % len(env))
    plain = [str(k) for k, v in env.items() if not check.SECRET_REF.search(str(v))]
    secret = ["%s (%s)" % (k, ", ".join(secrets_in(check, v))) for k, v in env.items()
              if check.SECRET_REF.search(str(v))]
    if plain:
        say("  plain    %s" % ", ".join(plain))
    if secret:
        say("  secret   %s" % ", ".join(secret))

    say("")
    say("Network of the agent")
    say("  always   its model, the project's repositories (git), HTTP connections with a secret header")
    if cfg is not None:
        hosts = check.allowed_hosts(cfg)
        say("  allowed  %s" % (", ".join(hosts) if hosts else "nothing else from the file"))
    else:
        allow = mapping(raw.get("network")).get("allow")
        say("  allowed  %s" % (", ".join(str(h) for h in allow) if isinstance(allow, list) else "nothing else"))
    say("           plus what the install allows every project (not in the file); setup reaches any public host")

    places = secret_places(check, raw)
    say("")
    store = text(raw.get("secret_store"))
    say("Secrets")
    say("  store    %s" % (store + " (filled by the admin, who also allows the project to use it)" if store else
                           "none named" + ("" if not places else ": an error, the file references keys")))
    for key, where in places.items():
        say("  %s: %s" % (key, ", ".join(where)))
    if not places:
        say("  the file references no key")

    notes = text(raw.get("notes"))
    say("")
    say("Notes   %s" % ("%d lines, quoted to every agent on every task" % len(notes.strip().splitlines())
                         if notes and notes.strip() else "none"))
    bridges = os.path.join(os.path.dirname(config_path), "bridges")
    if os.path.isdir(bridges):
        files = sorted(f for f in os.listdir(bridges) if f.endswith((".yaml", ".yml")))
        if files:
            say("Bridges %s" % ", ".join("bridges/" + f for f in files))
    record = os.path.join(os.path.dirname(config_path), *RECORD)
    if os.path.isfile(record):
        try:
            with open(record, encoding="utf-8") as handle:
                data = json.load(handle)
            entries = ["%s at %s" % (n, str(mapping(e).get("commit", "?"))[:8])
                       for n, e in mapping(data.get("repositories")).items()]
            say("Checked against the code: %s (skill %s); changes.sh of exradius-update compares from there"
                % (", ".join(entries) or "no repository", data.get("skill", "unknown")))
        except (OSError, ValueError):
            say("Checked against the code: check/reviewed.json cannot be read")
    else:
        say("Checked against the code: no check/reviewed.json (written by skill 0.5.0 or earlier, or by hand)")
    return out


def main(argv=None):
    if sys.version_info < (3, 8):
        sys.stderr.write("exradius describe needs Python 3.8 or later\n")
        return 2
    parser = argparse.ArgumentParser(
        prog="describe.sh",
        description="Print an Exradius project config as Exradius reads it, defaults filled in. Reads only.")
    parser.add_argument("config", nargs="?", default=".", help="the config folder or exradius.yaml (default: .)")
    parser.add_argument("--agent", metavar="SLUG", help="only this agent, with its role in full")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    check = load_check()
    config_path, config_root, config_dir = check.find_config(args.config)
    try:
        with open(config_path, encoding="utf-8") as handle:
            raw = check.load_yaml(handle.read())
    except Exception as exc:  # noqa: BLE001 - YamlError or an unreadable file
        sys.stderr.write("%s cannot be read: %s\n" % (config_path, exc))
        return 2
    if not isinstance(raw, dict):
        sys.stderr.write("%s is not a mapping\n" % config_path)
        return 2
    cfg, errors, _warnings, _required = check.validate(raw, config_dir, check.make_lookup(config_root))
    if errors:
        cfg = None
    for line in describe(check, raw, cfg, errors, config_path, config_root, args.agent):
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
