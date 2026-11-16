---
name: exradius-agent
description: Add, change or remove an agent of a project that is already on Exradius. Decides with the developer whether the work needs an agent of its own, writes its slug, name and role so that Exradius's routing can tell it from the project's other agents, gives it the connections (MCP tools) it needs and nothing else, sets model, provider or git identity when asked, validates the file, and publishes with the developer's go-ahead, in the right order when a new secret key is involved. Use when someone wants a new agent for a project (a reviewer, a triager, a test writer), wants an agent to use another model or tool, or wants to rename or remove one.
argument-hint: "[what the agent is for; config folder when it is not the current one]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/describe.sh *), Bash(bash ${CLAUDE_SKILL_DIR}/scripts/describe.sh *), Bash(${CLAUDE_SKILL_DIR}/scripts/check.sh *), Bash(bash ${CLAUDE_SKILL_DIR}/scripts/check.sh *)
---

# Add or change an agent of an Exradius project

An agent is a few lines under `agents` in the project's `exradius.yaml`: a slug that
tasks name, a name, a `role`, the `connections` it may use, and optionally a model, a
provider and a git identity. Every agent of a project works in the same workstation, on
the same repositories, with the same services, environment and `notes`. What makes one
agent different from another is its role, its tools and its model, and nothing else.

Two things decide whether a new agent works. Its role must tell it what a good result
is, as `notes` tell it how the project works. And the roles of all agents together must
divide the tickets: when a task names no agent, Exradius picks one by reading each
agent's name, role and tools, and two agents that could both claim a ticket make that a
coin toss. No local check proves either; the first tickets do. So the work here is
mostly careful wording, shown to the developer before anything is published.

This skill is for a project that has a config. Without an `exradius.yaml`, use
`exradius-project`. The file must say `version: 1`.

## What to read, and when

`references/` is the setup skill's folder,
`${CLAUDE_SKILL_DIR}/../exradius-project/references/`.

| File | Read it |
|---|---|
| `references/schema.md`, "Agents" and "Connections" under Rules | before writing: the keys, the name rules, the catalog table |
| `${CLAUDE_SKILL_DIR}/../exradius-project/SKILL.md`, the section `### Roles` | before writing a role: the order of its parts and the wording for proof from the running application |
| `references/examples.md` | for the roles of projects like this one |
| `references/pitfalls.md`, "The Playwright connection has no browser" | when the agent gets a browser |
| `references/network.md` | when a custom connection needs hosts |
| `references/admin-readme.md` | when keys, hosts or the list of agents in the README change |

## Hard rules

- **Never ask for, read, print or write a secret value.** A connection's credential is
  `${secret:NAME}` in the file, a row in the README and a line for the admin.
- **Never call the Exradius API** or the admin panel. A provider's slug, whether routing
  is set up and what the store holds are the admin's to say.
- **A small diff.** The new agent's block, and the smallest edit to another role where
  the two would overlap. Keep comments, key order and everything that still holds.
- **Show before you publish.** A push or a pull request happens only after the developer
  has seen the diff and said yes. Never force-push.
- **Do not promise what a role cannot hold.** A role is an instruction, not a
  permission (step 3, "What limits an agent").

## 1. Read what is there

Find the config (`exradius.yaml` or `.exradius/exradius.yaml` in the current folder, a
sibling `../exradius-*` folder, or where the developer points), fetch its repository so
you edit the head of the branch Exradius reads, and stop to ask when the folder has
uncommitted changes that are not yours.

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/describe.sh <config folder>
bash ${CLAUDE_SKILL_DIR}/scripts/describe.sh --agent <slug> <config folder>
```

The first prints every agent with its model, provider, tools and how its role opens, and
the connections with the agents that list them. The second prints one agent's role in
full: read every existing role and `notes` before writing a word. The new role is
written in their voice, names the checks as `notes` name them, and repeats neither.

## 2. Is it an agent?

Listen to what the developer wants and decide where it belongs. Tell them when it
belongs elsewhere; the decision is theirs.

- **A new agent** when there is a kind of ticket of its own and at least one of: its own
  definition of done (a review ends in comments, not in a branch), its own tools (a
  triager with the tracker's connection), its own model, or something the other agents
  have that it must not have.
- **A line in `notes`** when it is a rule for everyone ("always add a changelog entry").
- **A sentence in an existing role** when it refines what that agent already does.
- **`model` on an existing agent** when the same work should run on another model.
- **The ticket itself** when it is about one piece of work.

What a further agent costs: every ticket that names no agent now has one more candidate,
each connection it lists is started in each of its runs and listed by Verify, and one
more role has to be kept true as the project changes.

## 3. Ask, then write

Ask only what the request and the file do not answer, in one round, each question with
the default you would use (the AskUserQuestion tool when it is available; offer the
values that exist rather than a blank):

1. **The tickets**: which ones it takes, with two or three real examples, and what it
   must never do. No default: this is the agent.
2. **Does it change code?** Then its checks, its proof and its delivery are the
   developer agent's. Default: from the examples.
3. **Tools**: which of the project's connections it uses, and whether it needs one the
   project does not have yet: a catalog entry (`github`, `gitlab`, `slack`, `sentry`,
   `linear`, `jira`, `fetch`, `playwright`, `filesystem`) or a custom MCP server.
   Default: what its delivery needs and nothing else.
4. **Model**: only when the developer brought it up. Default: left out, so the install's
   default applies, as for the other agents.

Do not ask for the slug, the name, the provider or the git identity: propose the first
two, leave the others out. With nobody to ask, take the defaults, list them as
assumptions, commit locally and do not publish.

Then write the block.

- **`slug`**: `[a-z][a-z0-9-]*`, a noun for the job (`reviewer`, `triager`), not a
  persona and not a model's name. Tasks, trackers and scripts name it, so it is the one
  thing that is costly to change later.
- **`name`**: the slug capitalised unless the team calls the job something else.
- **`role`**, in the order of the setup skill's "Roles": which tickets the agent takes
  and what it never does, in its first sentence; when its work is done; for an agent
  that changes code, what it shows as proof and how it delivers. For those last parts
  reuse the project's own sentences from the developer role instead of new wording. An
  agent that does not change code says what its result contains instead (findings with
  file paths, review comments with the line each is about). No persona, no adjectives
  about the agent, nothing Exradius's rules or `notes` already say.
- **`connections`**: only what this agent uses.
  - One the file already has: its name.
  - A new catalog entry: its config keys from the schema's table, each secret as
    `${secret:NAME}`. Reuse the key the file already has for the same credential. An
    agent that must only read gets its own connection entry with its own, narrower key
    (`github-read` with `catalog: github` and `${secret:GITHUB_READ_TOKEN}`), because a
    token's permissions are the only thing that limits a connection.
  - A stdio connection is started with `npx` or `uvx` inside the workstation: the
    launcher must be in the image. A custom one pins its package to an exact version and
    needs its hosts in `network.allow`; a catalog entry brings its own hosts.
  - `playwright` brings no browser: the image and a setup command do (pitfalls.md).
- **`model`**: the id exactly as the provider knows it. Exradius checks only its shape;
  a wrong id fails at the agent's first task. **`provider`**: a provider slug of the
  install, which only the admin can tell you; an unknown slug rejects the agent's tasks.
- **`identity`**: leave it out (`<slug>@<project slug>.exradius.local`) unless the git
  host needs a real address for the agent's commits.

**The roles must divide the tickets.** Take the developer's examples and a few of the
project's recent tickets when you can see them (`gh issue list`, the tracker's own tool)
and write down, reading only each agent's name and the first sentence of its role, which
agent each ticket goes to. Where two could take the same ticket, narrow a first sentence:
the new one first, and an existing role only by the smallest edit that separates them.
Keep that table; step 5 shows it.

**What limits an agent.** The role tells an agent what to do; it does not stop it. What
an agent can do is fixed by what it has: every agent of the project has the workstation,
the repositories with their `push` setting, the environment with its secrets and the
network list. Only the connections, and what their tokens allow, differ per agent. So a
reviewer in a project with `push: true` is told not to push and is able to. When the
developer needs that to be impossible, the answer is a narrower token on that agent's
connection, or a project of its own with `push: false`. Say this instead of writing
"cannot" into a role.

Update `README.md` when the change adds or removes a key or a host, and its line that
lists the agents.

## 4. Check

Always:

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/check.sh --validate <config folder>
```

It reads the file as Exradius does (unknown keys, names, a connection an agent lists and
the file lacks) and prints the required keys and the hosts the agent may reach. No
Docker, a moment.

The full check (Docker, minutes; the setup skill's step 4 describes it) is needed only
when the change touches what it proves: a new stdio connection (its "Check connections"
step fails when the launcher is not in the image), a change to the Dockerfile or to
setup, or a check command the new role names that `notes` did not have:

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/check.sh --repo <name>=<path to checkout> \
  --run '<the new check command>' <config folder>
```

A change to a role, a model, a provider or an identity needs no more than `--validate`,
and nothing local proves it: say so. What a connection's tools are, and whether its
token works, only Verify shows.

## 5. Show, then publish

Show the developer:

- the diff, with the reason for every line outside the new agent's block;
- the role in full, and the table of tickets and which agent takes each;
- what the agent can do that its role tells it not to (step 3), when that applies;
- the assumptions, and what could not be checked here.

Then publish with the go-ahead, in this order:

- **A new secret key**: Exradius reads the branch head for every task and rejects the
  project's tasks while a referenced key is missing from the store. Push a branch and
  open a pull request, give the developer the line for the admin (the key, its format,
  where to get it, the narrowest permission that works), and merge after the admin
  added it.
- **A second agent in a project that had one**: until now a task that named no agent
  went to the only one. With two, Exradius picks, which needs agent routing set up in
  the install (the admin's Settings); without it such tasks are rejected. Ask the
  developer to confirm with the admin before the change reaches the branch.
- **Otherwise**: push to the config repository's branch, or open a pull request when the
  branch is protected or the config lives in `.exradius/`.

Tell the developer what follows: the agent exists from the next task; Verify reruns by
itself on the new commit and lists the tools of a new connection, which routing reads
from then on; a changed Dockerfile is built before the next run. Suggest the trial that
does prove the agent: two or three real past tickets sent to it by name, and one sent
without an agent to see where routing puts it. Whoever can create tasks does that, not
this skill.

## Changing an agent

- **Model or provider**: one line, `--validate`, publish. Say that the id is not checked
  until a task runs.
- **Role**: the smallest edit. When the first sentence changes, redo the division of
  tickets in step 3.
- **Connections**: adding one is step 3's connections and step 4's check. Taking one
  away: when no agent lists it any more, remove its entry as well, and say which keys
  nothing references now (the admin may delete them after the merge).

## Removing or renaming an agent

A task that names an agent the file no longer has is rejected. Before removing one, find
who names it: a bridge's settings, a script that publishes tasks, an automation in the
tracker. The file cannot tell you; ask. Then remove the block, the connections only it
listed, and the keys and hosts only they needed. Other roles may have been written
around it ("the reviewer takes those"): reread them. With one agent left, tasks that
name none go to it and routing is no longer involved.

A new slug is a removal and an addition: everything that named the old one breaks, and
the default git address changes with it. Prefer changing `name` and keeping the slug.
