# Model providers

Status: agreed design, 2026-11-05. Companion to `architecture.md` and `onboarding.md`.

## 1. Why

Exradius will run on a public server where nobody edits `deploy/.env`. Anything an operator
changes after installation must live in the database and be managed from the admin panel.
The Anthropic credential is the first such thing: it changes, there may be several, and it
belongs to whoever logs in. Today it comes from the environment file or from a specially
named global secret, with a fallback chain nobody can see. That goes.

The environment (on Kubernetes: the chart's values and the Secrets they name) keeps only
what must exist before the database does: database URL, broker URL, the encryption key,
public URLs, the initial admin password. The panel never tells anyone to edit it.

## 2. The model

A **provider** is a named credential profile that a runtime knows how to inject into a run
container. It is not tied to a project.

```
providers
  id, slug, name
  kind            "anthropic" in v1; later "anthropic_bedrock", "anthropic_vertex", "anthropic_foundry", ...
  auth_method     for anthropic: "api_key" | "oauth_token"
  credential_encrypted   write-only, like secrets
  settings        json, per kind/auth method (region, project, resource, base_url ...)
  is_default      exactly one row true once any exists
  last_test       {ok, tested_at, detail, error} | null; cleared when credential or settings change
  created_at, updated_at

agents.provider_id   nullable; null = use the default provider
```

Deleting a provider that agents reference is refused until they are moved. Deleting the
default is refused until another is made default. With zero providers, task intake rejects
the task with `no provider configured`.

### 2.1 Runtimes declare what they accept

The `Runtime` protocol gains a class-level capability description, exposed through
`GET /api/system/runtimes`:

```json
{
  "name": "claude_code", "label": "Claude Code",
  "providers": [
    {"kind": "anthropic", "label": "Anthropic",
     "auth_methods": [
       {"name": "api_key",     "label": "API key",
        "credential_label": "API key", "settings": []},
       {"name": "oauth_token", "label": "Claude subscription token (claude setup-token)",
        "credential_label": "OAuth token", "settings": [],
        "notice": "Anthropic restricts subscription tokens to personal use of Claude Code; products built on the Agent SDK should use an API key."}
     ]}
  ]
}
```

The panel builds the provider form from this: kind pick list, auth method pick list, then
the credential field and any settings fields. Nothing about providers is hard-coded in
the frontend. Later kinds add entries here and an injection mapping in the runtime; the
panel needs no change.

### 2.2 Injection, Claude Code runtime

Since phase 4 of the move to Kubernetes (`kubernetes.md` §10, §11) no pod holds the
credential. The workstation gets the egress proxy's model route as `ANTHROPIC_BASE_URL`
(`<egress>/anthropic`) and the provider's variable with the value `placeholder`; the proxy
removes the placeholder and sends the credential the way Claude Code would itself:

| kind / auth_method | Container environment | The proxy sets |
|---|---|---|
| anthropic / api_key | `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY=placeholder` | `x-api-key: <key>` |
| anthropic / oauth_token | `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN=placeholder` | `Authorization: Bearer <token>` |
| anthropic_bedrock (later) | `CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| anthropic_vertex (later) | `CLAUDE_CODE_USE_VERTEX=1`, `CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID`, credentials file |
| anthropic_foundry (later) | `CLAUDE_CODE_USE_FOUNDRY=1`, `ANTHROPIC_FOUNDRY_RESOURCE`, `ANTHROPIC_FOUNDRY_API_KEY` |

Later kinds would need routes of their own (Bedrock and Vertex sign requests; the proxy
would have to as well).

The worker resolves the agent's provider (or the default) when it creates the run's Run and
records it on the run (`runs.provider_id`); the proxy reads that provider's credential from
the api for the run's pod (a probe's from its test). A project variable named like any of
these (or `ANTHROPIC_BASE_URL`) is dropped. Sessions also get
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`: Claude Code's telemetry and update checks go to
Anthropic's hosts directly, which the agent phase refuses. MCP servers started by Claude Code
inherit the process environment, which now holds only the placeholder. The run token is still
scrubbed as before.

### 2.3 Test

`POST /api/providers/{id}/test`:

- `anthropic / api_key`: `GET https://api.anthropic.com/v1/models` with the key from the API
  container. Success stores the first few model ids in `detail`.
- `anthropic / oauth_token`: no cheap API check exists. The API publishes a control message;
  the worker creates a probe Run (`kubernetes.md` §13): the default project image with the
  runtime bundle and the provider's variables, running a one-line SDK query ("Reply with
  OK") that reports through the internal API. Result recorded like a verification. The endpoint
  returns 202 with a test id; `GET /api/providers/{id}/tests/{tid}` reports it.

Both store `last_test` on the provider.

## 3. What is removed

- The `ANTHROPIC_API_KEY` exception for global secrets. Global secrets are only referenced
  from connections, no exceptions.
- `EXRADIUS_ANTHROPIC_API_KEY` / `ANTHROPIC_API_KEY` in the worker environment as a source.
  **One-time migration:** at API start, if no provider exists and either the global secret
  `ANTHROPIC_API_KEY` or `EXRADIUS_ANTHROPIC_API_KEY` is present, create a default provider
  named "Anthropic (migrated)" from it and log it. After that the variables are ignored.
- `anthropic_key` and `object_store` from `GET /api/system/status`. The overview shows
  providers instead; the object store is a deployment detail.
- The `ANTHROPIC_API_KEY` line from `deploy/.env.example` and the README's instructions to
  set it there.

## 4. Admin password

`EXRADIUS_ADMIN_PASSWORD` seeds the admin user only when none exists. It no longer resets
the password on change. New endpoint `POST /api/auth/password` with current and new
password; the panel offers it under Settings. Passwords stay scrypt-hashed.

## 5. API summary

```
GET    /api/providers                 [{id, slug, name, kind, auth_method, settings, is_default, last_test, agent_count}]
POST   /api/providers                 {slug, name, kind, auth_method, credential, settings}  -> 201; first one becomes default
GET    /api/providers/{id}
PATCH  /api/providers/{id}            any field; credential optional (write-only, replaces)
DELETE /api/providers/{id}            409 if referenced by agents or is the default with others present
POST   /api/providers/{id}/default    -> 204
POST   /api/providers/{id}/test       -> 200 {ok, detail, error} for api_key; 202 {test_id} for oauth_token
GET    /api/providers/{id}/tests/{tid}

GET    /api/system/runtimes           now includes the providers capability block (§2.1)
GET    /api/system/status             provider: {configured, default: {id, name, kind, auth_method, last_test} | null, count}
                                      (anthropic_key and object_store removed)
POST   /api/auth/password             {current_password, new_password} -> 204

agents: AgentIn/AgentUpdate/AgentOut gain provider_id (nullable)
```

## 6. Admin panel

- **Providers page** (top-level navigation, between Connections and Agents): list with
  kind, auth method, default badge, last test, agents using it. Create form driven by the
  runtimes capability block: kind, then auth method, then credential and settings; the
  notice text from the capability block is shown under the auth method when present.
  Test on save, stored result shown. Set default. Delete confirms and explains when refused.
- **Agent form**: a provider pick list with "Default (<name>)" as the first option. Hidden
  while only one provider exists.
- **Overview status strip**: database, broker, workers, the operator, runtimes, provider
  (name, auth method, last test). No object store.
- **Setup checklist** step 1 becomes "A provider is configured and tested".
- **Settings**: change password form; the environment list shrinks to the values an
  operator may need to know (public API URL, public broker address, the operator, worker
  concurrency) with no edit instructions.
- **Overview, further**: recent tasks with live state chips; the checklist collapses once
  complete and can be reopened; quick actions for "new project", "new agent", "send a task";
  the status strip compact on one row at 1280 px.
