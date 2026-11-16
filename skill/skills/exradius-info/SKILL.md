---
name: exradius-info
description: Answer questions about an Exradius project from its config, without changing anything. Which model, provider and git identity an agent runs with, which tools and connections it has, whether it can push or open pull requests, what the agents may reach on the network, which secret keys the project needs and where each is used, the services, the setup commands, how a task finds its agent. Use when someone asks what a project on Exradius, or one of its agents, is, has or can do ("which model does the scout use?", "can the developer agent open pull requests?", "which secrets does this project need?"). Read-only; changes go to exradius-agent and exradius-update.
argument-hint: "[the question; config folder when it is not the current one]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/describe.sh *), Bash(bash ${CLAUDE_SKILL_DIR}/scripts/describe.sh *)
---

# Answer questions about an Exradius project

A project on Exradius is one file, `exradius.yaml`, in a git repository. Exradius reads it
at the head of a branch for every task, so the file answers most questions about a
project: its agents, what each runs with and can use, what the workspace holds. Some
answers are not in the file because the install decides them, and a few are only in the
runs. Your job is to give the answer the file supports, to say plainly where the file
stops, and to name the place that knows the rest.

You change nothing here: no edit, no commit, no push. When the question turns into a
request ("then make the scout use the bigger model"), answer it, say what the change
would be, and make it with the skill it belongs to once the person asks:
`exradius-agent` for agents, `exradius-update` for a config that fell behind the code,
`exradius-project` for a project without a config or a failed Verify or task.

## Hard rules

- **Never ask for, read or print a secret value**, and never open an untracked `.env` or
  a credentials file to answer. A secret is the name of its key.
- **Never call the Exradius API** or the admin panel, and never ask for an Exradius URL
  or login. What only the install knows, the admin or the panel answers; say which page.
- **Never fill a gap with a guess.** "The file does not set a model" is an answer. A
  model name you believe to be the current default is not.

## 1. Find the config and describe it

The config is `exradius.yaml` or `.exradius/exradius.yaml` in the current folder, in a
sibling `../exradius-*` folder, or where the person points. Without one, say so: the
project is not set up from here (`exradius-project` does that).

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/describe.sh [--agent <slug>] <config folder>
```

It prints the file as Exradius reads it, with the defaults filled in: per agent the
model, the provider, the git author, the tools and how the role opens (`--agent` prints
one agent with its role in full); the connections with how each runs, the keys it needs
and the agents that list it; the repositories with branch, push and credential key; the
workstation, its setup commands and caches; the services; the variable names; the hosts
the agent may reach; the secret keys with every place that references them. It needs git
and Python, takes a moment, and never prints a value.

Mind which file you are describing. Exradius reads the committed file at the head of the
branch on the remote. The first lines show the config's last commit and say when the
folder has uncommitted changes; `git fetch` and `git status -sb` in the config folder
show whether the local branch is behind. When either is the case, say that the answer is
for the local file and what differs.

## 2. Answer

Answer the question that was asked, in a sentence or two, in the person's language, with
the key of the file it rests on. Do not paste the description. Every answer is one of
three kinds, and saying which is part of the answer:

- **In the file**: "`agents.scout.model` is `claude-haiku-5-5`."
- **A default Exradius applies**: the file leaves it out and the rule is fixed. The
  agent's name is its slug capitalised, its git address is
  `<agent>@<project slug>.exradius.local`, a repository without `branch` uses the
  remote's default branch, a repository without `push: true` is read-only.
- **Decided by the install**: the file leaves it out and the admin's install fills it.
  Name where it shows.

| The question | Where the answer is |
|---|---|
| Which model does an agent use? | `agents.<slug>.model`. Not set: the runtime's default model on the agent's provider, which the file does not name. The panel's Providers page shows, in a provider's test result, the model that answered |
| Which provider, whose key? | `agents.<slug>.provider`, a provider slug of the install. Not set: the install's default provider (the Providers page marks it). The credential is never in the file |
| What can an agent do? | Its tools: Claude Code's own (read, edit, run commands in the workstation), the built-in `exradius` tools, and the tools of the connections it lists. A connection's exact tool list comes from the server: the project's Verify tab shows it |
| Can it push or open pull requests? | Pushing is per repository and the same for every agent: `repositories[].push`. A pull request also needs a connection that can open one (`github`, `gitlab`) on that agent's list, and a token allowed to; the token's permissions are not in the file |
| What may it not do? | A role's "never" is an instruction, not a lock. What holds an agent back is what it lacks: a connection, `push`, a host on the network list |
| What can it reach on the network? | Its model, the repositories (git only), its connections, and the "allowed" hosts of the description. The install can allow more for every project; the file does not show that. Setup reaches any public host |
| Which secrets does the project need? | The "Secrets" section: every key and where it is referenced, and the store that must hold them. Whether the store has them is the admin's: the project's Secrets tab |
| What is in the agent's environment? | The names under "Environment". A secret listed there is readable by every agent of the project |
| Which agent gets a ticket? | The one the task names. A task that names none: the only agent, or with several, Exradius picks from each agent's name, role and tools, which needs agent routing set up in the install (Settings). Why a past task went where it did: that task's page |
| What does the agent know about the project? | `notes` (quoted to every agent), the role (`--agent` prints it), and what Exradius generates from the file: the repositories, the services, the network line |
| Is the config still right for the code? | Not answered here: `exradius-update` compares the code with the commits the last line of the description names |
| What did a run do, how long, what did it cost? | Not in the file: the task's page in the panel |

For how Exradius treats a key, read the setup skill's references rather than answer from
memory: `${CLAUDE_SKILL_DIR}/../exradius-project/references/schema.md` (every key, its
default and its rules), `network.md` (what a run reaches in each phase),
`run-conditions.md` (what the workstation is: uid, read-only root, no Docker daemon).

When the description opens with ERRORS, lead with that: Exradius rejects the project's
tasks while the file has errors, which is likely why the person is asking.
