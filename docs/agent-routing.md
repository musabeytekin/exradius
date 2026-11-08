# Agent routing

Status: agreed 2026-11-08, after the first live Jira trial. Companion to `architecture.md`
§4 and `integration-api.md`. Internal design note.

## 1. Why

In the first Jira trial people picked the agent in a cascading field, and Jira's issue view
could not even set its second level. The deeper problem is that the choice should not be
theirs: they know the work, not the agents. The person only hands the work over (in Jira,
by assigning the issue to the bot), and Exradius picks the agent itself.

So a task names its project and may leave out the agent. Exradius then picks one at intake,
from the project's **agent profiles**, with OpenAI's Decisions API
(`POST /v1/decisions`, model `gpt-6-luna`, one `choice` question). The answer gives the
choice, a probability per agent and a confidence.

Routing lives in Exradius, not in the bridges: every integration gets it, the profiles are
built from what only Exradius knows (roles and connections), and the OpenAI key is kept in
one place.

## 2. The task message

- `agent` becomes optional (architecture §4.1). When a task names an agent, that agent runs
  as before and no call is made. When it is omitted or `null`, Exradius routes.
- `project` stays required. An integration finds it from its own configuration
  (`integration-api.md` §6).
- `exradius publish-task --agent` becomes optional, and the panel's New task form offers
  "Pick automatically" as the first agent option.

## 3. Agent profiles

A profile is what the decision knows about one agent:

```json
{
  "slug": "scout",
  "name": "Scout",
  "role": "You investigate and report. You never change code.",
  "connections": [
    {
      "name": "github",
      "kind": "catalog github",
      "tools": [
        {"name": "get_issue", "description": "Get details of a specific issue...", "annotations": {"readOnlyHint": true}},
        {"name": "create_pull_request", "description": "Create a new pull request...", "annotations": {"readOnlyHint": false}}
      ],
      "tools_listed_at": "2026-11-08T17:02:10Z"
    }
  ]
}
```

- **`name` and `role`** are read from the config at intake, the same read that checks the
  project (config-repo.md §3). An agent without a role says `"role": null`.
- **`connections`** are the agent's connections in config order. `kind` is `catalog <key>`,
  `custom stdio` or `custom http`.
- **`tools`** come from the connection's MCP `tools/list`, as the last Verify stored them
  (§3.1): each tool's name, description and annotations (`readOnlyHint`,
  `destructiveHint` and the rest, when the server gives them). Annotations are the most
  solid evidence of what an agent can do, so they are kept as the server sent them. Input
  schemas are left out: they are long and say how to call a tool, not what it is for.
- **Not in a profile:** the built-in `exradius` tools and the runtime's own tools (every
  agent has them); model, provider and identity (they do not say what the agent is for);
  repositories and `push` (they belong to the project, so every agent of it has the same).

### 3.1 Tool lists

- **Fetched at Verify.** After the setup steps, Verify adds one step per connection that
  some agent uses: "List tools github". It runs in the verify pod (`kubernetes.md` §13): the
  workstation container of a Run with `purpose: verify`, with the run's environment, after
  its setup, the runtime bundle mounted read-only, as the agent would (services, on
  `localhost`, come in phase 3). The bundle's `exradius-runner --verify <id>` reads the
  connection specs with secrets substituted from `GET /internal/verifications/{id}/spec`,
  starts each server, sends `initialize` and `tools/list` (following `nextCursor`), and
  posts the tools (or the error and the tail of a stdio server's stderr) to
  `/internal/verifications/{id}/tools`. It uses the `mcp` client that the Agent SDK already
  brings into the bundle. Each connection times out after 50 seconds.
  `exradius-runner --list-tools` (one connection from `EXRADIUS_LIST_TOOLS`, written to
  `/tmp/exradius-list-tools.json`) stays for the skill's local check.
- **A failure fails Verify** (decided): the agent would not get those tools in a run either.
  The step's log shows the error, as other steps do.
- **Stored per connection,** in a new table:

  ```
  connection_tools
    project_id, connection (name), definition_hash, tools (json), commit,
    verification_id, listed_at          unique (project_id, connection)
  ```

  `definition_hash` is the sha256 of the connection's definition as the config writes it
  (secret references, not their values). A Verify that lists a connection replaces its row.
- **Fresh enough?** At intake, a connection's stored tools are used only when their
  `definition_hash` matches the connection in the config just read. Otherwise the profile
  says `"tools": null` with `"tools_note": "not listed since this connection changed"`, or
  `"not listed yet"`, and the decision goes on with name, role and connection names.
  Verify runs by itself after a config commit (setup-and-verify.md), so this gap is
  usually short.
- A server can change its tools with no commit on our side (a remote server, an unpinned
  `npx -y` package). That is parked in `ideas.md`.

### 3.2 In the panel

The project page gets an **Agents** tab: each agent's name and role, and each of its
connections with its tools (name, description, annotations), with "listed by Verify
<time>", "not listed yet" or "not listed since this connection changed". It shows what
routing will see; it does not explain routing.

## 4. The decision

**When.** At intake (`worker/intake.py`), after the duplicate check, the config read, the
health check and the provider check, and before the task is stored. The call runs outside
any database transaction.

**One agent, no call.** A project with a single agent needs no decision (and Decisions
accepts no fewer than two choices). The method is `only_agent`, and it works without an
OpenAI key.

**The request:**

```json
{
  "model": "gpt-6-luna",
  "prompt_cache_key": "exradius-routing-plane",
  "prompt_cache_options": {"mode": "explicit"},
  "input": [
    {"role": "developer", "content": "You route work to the agents of one software project. ..."},
    {"role": "user", "content": [
      {"type": "input_text", "text": "The agents of project Plane:"},
      {"type": "input_json", "data": {"agents": ["... profiles, by slug ..."]}},
      {"type": "input_text", "text": "End of the agent profiles.", "prompt_cache_breakpoint": {"mode": "explicit"}}
    ]},
    {"role": "user", "content": [
      {"type": "input_text", "text": "# Task PLANE-12\n<instructions>"},
      {"type": "input_json", "data": {"context": {"...": "..."}, "attachments": ["trace.txt (text/plain)"]}}
    ]}
  ],
  "questions": [{
    "type": "choice",
    "name": "agent",
    "instructions": "Which agent should do this task?",
    "choices": [
      {"value": "developer", "description": "Developer"},
      {"value": "scout", "description": "Scout"}
    ]
  }]
}
```

- **Profiles first, the ticket last.** The developer message and the profiles are the same
  for every task of the project, so they end at an explicit cache breakpoint
  (`input_json` parts cannot carry one, hence the closing text part). Profiles are sorted
  by slug and sent without `tools_listed_at`, so the prefix only changes when a role, a
  connection or a tool does. `prompt_cache_key` keeps each project's prefix apart. When
  the profiles pass `input_json`'s limits they go as text, like a large context.
- **The developer message** is fixed text in `exradius/routing.py`: what the profiles are,
  that the role is the agent's job description and the tools are what it can act with,
  and that the choice is the agent best able to do the whole task.
- **The ticket** is what the agent would get: `# Task <task id>` and the instructions,
  then the context and the attachments' names and types. Attachment content is not sent
  in v1. When the context would pass `input_json`'s limit (256 KiB), it is sent as
  pretty-printed text cut to that size, with a note that it was cut.
- **Choices** are the agent slugs, each described by its name. There is no "other" choice,
  because a task always runs with the top pick (§5).

**The answer.** The answer to `agent` must be a `choice` whose value is one of the slugs. A
`refusal` answer, or a value that is not a slug, is a routing failure (§6).

## 5. Low confidence

When `confidence` is below **0.6** (a constant, `GUESS_BELOW` in `routing.py`), the routing
is marked `guess: true`. The task still runs with the top pick, and the integration says it
was a guess (the Jira bridge, in its start comment). The value is a starting point, to be
calibrated with the routing evaluations planned for later.

## 6. When routing cannot answer

The task is rejected with the reason, like any other rejection. A rejected task id is not
stored, so an integration can send the same id again later.

| Problem | Retries | Reason |
|---|---|---|
| No OpenAI key, more than one agent | none | `agent routing is not configured (no OpenAI API key), and project plane has 2 agents` |
| Network error, timeout, `429`, `5xx` | 3 attempts in all, waiting 2 s then 6 s; each attempt times out after 30 s | `agent routing failed: OpenAI answered 503 three times` |
| `400`, `401`, `403`, `404` | none | `agent routing failed: OpenAI answered 401: <OpenAI's message>` |
| Refusal, or a value that is not a slug | none | `agent routing failed: the decision refused to choose` |

The reason never contains the key. While a call is retried, the worker's consumer slot for
that message stays busy, which a short delay at intake can afford.

## 7. The OpenAI key

- **Panel:** Settings gets an **Agent routing** section: the key (write-only: set, replace,
  delete), the time it was set, and a **Test** action. The test runs in the API process:
  a two-choice decision on a fixed sentence, showing the answer, the time it took or the
  error. It is not a provider: providers run agents, this key only routes.
- **Storage:** a single-row table, encrypted with `EXRADIUS_SECRET_KEY` like provider
  credentials (`crypto.py`):

  ```
  routing_settings
    id (= 1), openai_key_encrypted, key_set_at, last_test (json: ok, at, detail, ms)
  ```

- **API:** `GET /api/routing` (key set or not, when, last test), `PUT /api/routing/key`,
  `DELETE /api/routing/key`, `POST /api/routing/test`.
- **Environment:** `EXRADIUS_OPENAI_BASE_URL`, default `https://api.openai.com/v1`, for
  tests and for a proxy the installer runs. The model is fixed (`gpt-6-luna`, the only one
  Decisions supports).
- **What leaves Exradius:** the ticket's instructions and context, attachment names, and
  the agents' names, roles and tool lists. Setting the key is the admin's consent to that.

## 8. Recorded and published

**On the run,** a new `runs.routing` JSON:

```json
{
  "method": "decided",
  "agent": "scout",
  "confidence": 0.48,
  "guess": true,
  "probabilities": {"developer": 0.41, "scout": 0.59},
  "model": "gpt-6-luna",
  "ms": 840,
  "input_tokens": 9120,
  "cached_tokens": 8960,
  "decided_at": "2026-11-08T17:10:03Z"
}
```

`method` is `named` (the task named the agent), `only_agent` or `decided`; the decision's
fields are present only for `decided`.

**In events,** the `queued` status event (the first event of an accepted task) carries the
routing next to the state, and the envelope's `agent` is the chosen slug on that event and
every later one:

```json
{
  "type": "status",
  "agent": "scout",
  "payload": {
    "state": "queued",
    "previous": null,
    "reason": null,
    "routing": {"method": "decided", "confidence": 0.48, "guess": true, "probabilities": {"developer": 0.41, "scout": 0.59}}
  }
}
```

Other events carry no `routing`. Integrations that ignore unknown fields are unaffected
(architecture §4).

**In the panel,** the task page shows the agent with how it was picked: "named by the
task", "the project's only agent", or "picked automatically" with the confidence, a
"guess" mark below 0.6, and the probabilities.

## 9. Changes by area

- **Messages:** `TaskMessage.agent` optional; `StatusPayload.routing` optional.
- **Routing module** (new `exradius/routing.py`): build profiles, the request, the call with
  retries over httpx, the answer's checks, `GUESS_BELOW`.
- **Intake:** route when `agent` is missing; store `tasks.agent` (the chosen slug) and
  `runs.routing`; put `routing` on the `queued` event.
- **Verify:** the "List tools" steps; `exradius-runner --list-tools` in the runner package.
- **Models and migration** (`0005`): `connection_tools`, `routing_settings`, `runs.routing`.
- **API:** the routing settings routes; the project's agent profiles
  (`GET /api/projects/{id}/agents`); `routing` on the task and run views.
- **Frontend:** Settings, Agent routing; the project's Agents tab; routing on the task page;
  "Pick automatically" in the New task form.
- **CLI:** `--agent` optional.

## 10. Tests

- Intake: a named agent makes no call; one agent makes no call and needs no key; no key
  with two agents rejects at once; a decided agent is stored with `runs.routing` and the
  `queued` event's `routing`; low confidence gives `guess: true` and still runs.
- The call, against an httpx mock: retries on `503` and timeouts then rejects; no retry on
  `401`; a refusal and an unknown value reject; the key never appears in a reason or a log.
- The request: profiles before the ticket, the breakpoint after the profiles, profiles
  sorted, the context cut at the limit.
- Profiles: stored tools used when the hash matches, `null` with a note when the
  connection changed or was never listed.
- Verify: a connection that lists its tools stores a row; one that fails fails the step and
  the verification. `list-tools` itself against a small stdio MCP server in the tests.

## 11. Not in v1

- An "other" choice that would reject work no agent fits.
- Images and attachment content as evidence.
- Routing again when an answer arrives, or a person overriding the pick.
- Routing evaluations and calibrating `GUESS_BELOW`; deliberately postponed until the
  system is built.
- Rejection reason codes (integration-api.md §10).
