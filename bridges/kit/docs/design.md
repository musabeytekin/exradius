# exradius-bridge-kit design

Status: agreed 2026-11-08. Internal design note. The Exradius side it relies on is
`docs/architecture.md` §4, `docs/integration-api.md` and `docs/agent-routing.md` in the
Exradius repository.

## 1. What it is

A Python library (`exradius_bridge_kit`) for writing **bridges**: services that connect a
work tracker (Jira, GitHub, ...) to an Exradius installation. It does everything bridges
have in common, so a bridge writes only what is specific to its tracker.

The kit owns the **whole flow**: work handed to the bot becomes a task, the start is
announced with the agent Exradius picked, the agent's questions become comments, a reply
becomes an answer, the result becomes a comment with the files, and the work goes back to
a person. A bridge implements a small tracker interface (§4) and feeds the kit what its
tracker's webhooks say (§5).

It is **only a library**: no service, no image, no command line. Each bridge is its own
repository and image and depends on the kit. The first is the Jira bridge
(`exradius-jira`); the GitHub bridge moves onto the kit later.

Like every bridge, the kit speaks only Exradius's public contract and imports nothing from
Exradius:
- the broker: `exradius.tasks`, `exradius.answers` and `exradius.cancels` in, `exradius.events`
  out;
- the integration API: the directory with the bridge's files, its `directory.changed`
  notice, and the bridge status report.

## 2. What people do

They hand work to the bot, nothing more. In Jira that is assigning the issue to the bot
account. They never choose an agent: the task names only the project, and Exradius picks
the agent (agent-routing.md). The kit tells them which agent took the work, and whether
that was a guess.

## 3. Which project

A bridge's configuration for a project lives in the project's config repository, in
`bridges/<bridge name>.yaml` (config-repo.md §2.1). Exradius passes the file through
uninterpreted; the bridge gives the kit a pydantic model for it and a function that says
which **scopes** the file claims. A scope is how the tracker groups work: a Jira space key
(`PLANE`), a GitHub repository (`acme/plane`).

```yaml
# bridges/jira.yaml in the plane config repository
projects: [PLANE]
```

On every directory fetch the kit:

1. validates each project's `bridge_config` with the bridge's model (strict, unknown keys
   are errors);
2. collects the scopes each valid file claims;
3. builds the map from scope to Exradius project.

Problems become the bridge's status (§9), each naming its project:
- `bridge_config_error` from Exradius (the file is not YAML, not a mapping, too large);
- validation errors, with the path (`bridges/jira.yaml: projects.0: must be a Jira project key`);
- a scope claimed by more than one project. The scope then maps to none of them, so work
  never goes to the wrong project.

## 4. The tracker interface

The bridge implements this protocol. All methods are async except the first two.

```python
class Tracker(Protocol):
    bridge: str                         # the bridge name: "jira"; the token's bridge
    instance: str                       # which tracker: "https://acme.atlassian.net"
    config_model: type[BaseModel]       # the schema of bridges/<bridge>.yaml

    def claims(self, config: BaseModel) -> Iterable[str]: ...   # the scopes a file claims

    async def start(self) -> None: ...                     # check credentials; raise TrackerProblem
    async def item(self, ref: ItemRef) -> Item: ...        # read one work item
    async def download(self, attachment: AttachmentInfo) -> bytes: ...
    async def post(self, ref: ItemRef, message: Message) -> datetime | None: ...  # the comment's time
    async def attach(self, ref: ItemRef, name: str, content_type: str, data: bytes) -> None: ...
    async def hand_back(self, ref: ItemRef) -> None: ...   # to the reporter, if still with the bot
    async def load_state(self, ref: ItemRef) -> dict | None: ...
    async def save_state(self, ref: ItemRef, state: dict) -> None: ...
    async def assigned_to_bot(self) -> list[ItemRef]: ...  # for the startup reconcile
    async def replies(self, ref: ItemRef, since: datetime) -> list[Reply]: ...
```

The data types, all in `exradius_bridge_kit.tracker`:

| Type | Fields |
|---|---|
| `ItemRef` | `id` (stable: Jira's issue id), `key` (what people see, the base of task ids: `PLANE-101`) |
| `Item` | `ref`, `scope`, `title`, `body` (Markdown), `url`, `reporter`, `assigned_to_bot`, `attachments`, `comments`, `details` (tracker-specific facts for the context: type, priority, labels, links, ...) |
| `Person` | `id`, `name` |
| `Comment` | `author`, `created`, `body` (Markdown) |
| `AttachmentInfo` | `id`, `name`, `content_type`, `size`, `created` |
| `Reply` | `author`, `created`, `body` (Markdown, the bot's mention removed), `attachments` (added since the question) |
| `Message` | blocks: `Paragraph` (the kit's own words: `Span` text with bold or italic, and `Mention(person)`) and `Markdown` (the agent's text); the tracker renders them in its own format, and `plain()` gives Markdown |

`replies` returns only comments that count as answers: written by a person (not the bot)
after `since`, and addressed to the bot the tracker's way (in Jira, an ADF mention of the
bot account). How a tracker recognises that is its business.

`item` raises `ItemGone` when the item no longer exists, and the kit then drops what it was
doing for it.

## 5. What the bridge feeds the kit

The bridge runs its own webhook listener, checks its tracker's signatures and drops
repeated deliveries. It then calls one of:

- `await kit.handed_over(ref)`: the item was assigned to the bot (or created assigned);
- `await kit.commented(ref)`: a comment was written on the item.

Both return at once; the work is queued per item (§10). The kit decides what happens: a
comment matters only while a question waits, an assignment only when no run is active.

## 6. The flow

### 6.1 Starting

On `handed_over`:

1. **Active run** (§8 state `starting`, `active` or `waiting`): post "**Scout** is still
   working on this." once per run, nothing else.
2. **Scope not claimed** by any project: post "No Exradius project takes work from
   PLANE." and hand back. A scope claimed twice says so ("PLANE is claimed by plane and
   bank; their bridges/jira.yaml files need fixing.") and hands back.
3. Otherwise build the task (§7), save the state as `starting`, publish it.

On the `queued` status event, the kit posts the **start comment**, naming the agent from
the event's `agent` and the directory's agent name:

| `payload.routing` | Start comment |
|---|---|
| `named`, `only_agent`, or `decided` and not a guess | "**Scout** is working on this." |
| `decided` with `guess: true` | "**Scout** is working on this. This was a guess (Scout 59%, Developer 41%)." |

The probabilities are shown from highest to lowest, rounded to whole percents, and at most
the top three.

### 6.2 Questions and answers

On `task.info_request`:

> **Scout** asks @Reporter:
>
> *question*
>
> *context, if the agent gave one*
>
> Mention me in your reply to answer.

The state becomes `waiting` with the `request_id` and the time of the question, as the
tracker gave it to the comment (so replies are compared by the tracker's clock). On
`commented` while waiting, the kit asks `replies(ref, since=asked_at)` and publishes the
first reply on `exradius.answers`: its Markdown, and its attachments within the size cap
(§7). The state becomes `active`. Comments while nothing waits are ignored in v1.

### 6.3 Results

On `task.result` the kit posts one comment and uploads the artifacts:

| State | Comment |
|---|---|
| `completed` | "**Scout** finished." Then the summary, the warnings, and the names of the attached files. |
| `failed` | "**Scout** could not finish:" the error, then the summary if there is one. |
| `cancelled` | "**Scout** was stopped." |

Artifacts are downloaded from their signed URLs and attached. One that fails to download
or attach is named in the comment ("could not attach report.docx") and logged; the rest
go on. The transcript link is not posted: it works for anyone who has it.

On a `rejected` status: "This could not start:" and the reason. A duplicate task id is
retried instead (§7).

After any result or rejection the item is **handed back**, the state becomes `ended`, and
handing it to the bot again starts a new run.

## 7. The task message

| Field | Value |
|---|---|
| `task_id` | The item key for the first run (`PLANE-101`), then `PLANE-101-2`, `-3`, from the run counter (§8). Branches start with the task id, so trackers that link branches by key find them. |
| `project` | The project the item's scope maps to (§3). |
| `agent` | Left out: Exradius routes. |
| `instructions` | The title, a blank line, then the body. |
| `context` | `{"item": {"key", "url", "reporter", ...details}, "comments": [...], "skipped_attachments": [...]}`. All comments, the bot's included: on a rerun, earlier questions, answers and results are part of what the agent should know. |
| `attachments` | The item's attachments, inlined as base64, oldest first, while the total stays within `MAX_ATTACHMENT_MB` (default 10, which fits RabbitMQ's default message size after base64). The rest are listed in `skipped_attachments`. Names are made plain file names and unique (`log (2).txt`). |
| `correlation` | `{"bridge": "jira", "instance": "https://acme.atlassian.net", "item": {"id": "10042", "key": "PLANE-101"}}` |

**Duplicate ids.** Exradius rejects a task id it has seen, even one whose run failed. If the
rejection's reason starts with `duplicate task_id`, the kit increments the run counter and
publishes again, up to five times, before posting the rejection. Matching the text is the
only option the contract offers today.

## 8. State

The kit keeps no database. Each item's state is a small JSON document the tracker stores
on the item (a Jira issue property, ...):

```json
{
  "runs": 2,
  "task_id": "PLANE-101-2",
  "project": "plane",
  "agent": "scout",
  "label": "Scout",
  "state": "waiting",
  "request_id": "a7c1...",
  "asked_at": "2026-11-08T10:02:41Z",
  "retries": 0,
  "told_still_working": false
}
```

- **`state`** is `starting` (published, no `queued` yet), `active`, `waiting` or `ended`.
- **`agent` and `label`** are set from the `queued` event; `label` (the agent's name) is
  kept so a later directory change does not rename the agent mid-run.
- **Write order.** For a task, the state is saved before publishing, because events are
  matched against it, and restored if publishing fails. For an answer, the answer is
  published first: if saving then fails, a later reply answers again, and Exradius keeps
  only the first answer to a request.
- Unknown fields in a stored state are kept, and missing ones default, so a bridge can
  take over states written by an older version of itself.

## 9. Status

The kit reports to `PUT /api/integration/v1/status` (integration-api.md §7) at startup,
after every directory fetch, whenever its problems change, and every 5 minutes otherwise.
The problems are those of §3, plus the tracker's own: `start()` raising `TrackerProblem`
("Jira answered 401 for the bot account"), and the tracker failing repeatedly afterwards.
A report that fails is logged and retried with the next one; it never stops the bridge.

`StatusReporter` holds this part (the report's shape, the `only_if_changed` check, the
5-minute heartbeat), so a bridge that does not use `Kit`'s flow can still report the way
`Kit` does. Exradius Desk is the first (`bridges/desk/docs/design.md` §11).

## 10. Exradius side

- **Directory.** Fetched at startup with the integration token, and again on every
  `directory.changed` that names the bridge (or names no bridges), with `If-None-Match`.
- **Events.** A durable queue `exradius-bridge.<bridge>.events`, bound to `exradius.events`
  with `task.*` and `directory.changed`. Task events whose correlation is not this bridge
  and instance are acknowledged and ignored. Events are handled one at a time (prefetch 1),
  which keeps an item's events in Exradius's order. A handler that fails is retried once,
  then the event is dropped and logged, so one bad event cannot block every other item.
- **Per item, one at a time.** Webhook calls, events and the reconcile for one item run
  under that item's lock.
- **Startup.** Read settings; `tracker.start()`; fetch the directory and report status;
  connect to the broker; reconcile: for each item `assigned_to_bot()`, no active run means a
  fresh hand-over, `waiting` means look for a reply after `asked_at`; then consume events.
  The bridge serves webhooks once `kit.run()` has started.
- **Settings** (environment): `EXRADIUS_BROKER_URL`, `EXRADIUS_API_URL`,
  `EXRADIUS_INTEGRATION_TOKEN`, `MAX_ATTACHMENT_MB`. A bridge adds its own.

## 11. Package

Python 3.14, uv; aio-pika, httpx, pydantic. No web framework: listening for webhooks is the
bridge's.

```
exradius_bridge_kit/
  __init__.py     the public names
  tracker.py      the Tracker protocol and its data types (§4)
  kit.py          Kit: handed_over, commented, run (§5, §10)
  flow.py         starting, questions, answers, results (§6, §7)
  state.py        the item state (§8)
  texts.py        every sentence the kit posts
  projects.py     bridge files: validation, claims, problems (§3)
  exradius.py     broker, directory, status clients
  status.py       StatusReporter (§9)
  settings.py
  testing.py      FakeTracker, FakeBroker, FakeExradius, for the kit's tests and the bridges'
tests/
```

`testing.py` ships with the library, so a bridge tests its tracker against the same fakes
the kit is tested with. Until the kit has a remote, bridges depend on it by path, and their
images build with it as a named build context.

## 12. Tests

Against `FakeTracker`, `FakeBroker` and `FakeExradius` (httpx `MockTransport`):
- the start: unclaimed and twice-claimed scopes, an active run, the start comment for each
  routing method and for a guess;
- duplicate-id retry;
- questions, a reply, a late reply, a reply while nothing waits;
- results: each state, artifacts, one artifact failing;
- bridge files: invalid content, a claim conflict, an Exradius-side error, all reported;
- status: reported at startup and on change, a failing report does not stop the kit;
- the startup reconcile;
- one broker test against a real RabbitMQ when `EXRADIUS_KIT_TEST_BROKER_URL` is set.

## 13. Later

- The GitHub bridge on the kit (an issue's repository is its scope; state in a hidden
  comment or the issue's metadata).
- A mention of the bot after a run ended starting a follow-up run.
- Cancel in the flow (in Jira, unassigning the bot). Exradius has a public cancel since
  2026-11-14 (integration-api.md §10), and `Broker.publish_cancel` sends it; the flow does
  not use it yet.
- Texts in other languages.
