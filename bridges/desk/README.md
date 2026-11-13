# exradius-desk

Exradius Desk is a small task system and Exradius's own bridge, on
[the bridge kit](../kit). People sign in with OpenID Connect and keep tasks in Exradius
projects. Each task has a title, description, files, status, comments and an assignee. Assign
a task to a person, or to **Exradius**: that starts a run. Exradius picks the agent, posts its
questions as comments (answer by mentioning Exradius, or with Answer), posts the result with
its files, and assigns the task back.

Who may use which project is declared in that project's config repository, in
`bridges/desk.yaml`:

```yaml
groups: [payments-team]
```

The design is in [docs/design.md](docs/design.md).

## What it needs

- An OpenID Connect provider: a client with the redirect URI `<DESK_PUBLIC_URL>/auth/callback`,
  the authorization code flow with PKCE, and a groups claim in the ID token or userinfo.
- In Exradius: an integration token for the bridge `desk` (Settings, Integrations).
- A Postgres database of its own.
- Optionally an SMTP server for mail.

## Environment

| Variable | |
|---|---|
| `DESK_DATABASE_URL` | `postgresql://...` |
| `DESK_PUBLIC_URL` | `https://desk.example.com` |
| `DESK_OIDC_ISSUER`, `DESK_OIDC_CLIENT_ID` | The provider and the client. |
| `DESK_OIDC_CLIENT_SECRET` | A confidential client's secret. |
| `DESK_OIDC_DISCOVERY_URL` | When Desk reaches the provider at another address than browsers. |
| `DESK_OIDC_SCOPES`, `DESK_OIDC_GROUPS_CLAIM` | Default `openid profile email` and `groups`. |
| `DESK_SESSION_HOURS` | Default `12`. |
| `DESK_SMTP_HOST`, `DESK_SMTP_PORT`, `DESK_SMTP_TLS`, `DESK_SMTP_USERNAME`, `DESK_SMTP_PASSWORD`, `DESK_SMTP_FROM` | Optional mail. |
| `EXRADIUS_BROKER_URL` | The Exradius broker; `EXRADIUS_BROKER_USERNAME` and `EXRADIUS_BROKER_PASSWORD` are put into it. |
| `EXRADIUS_API_URL` | Exradius's API: the directory, the status report, result files. |
| `EXRADIUS_INTEGRATION_TOKEN` | The `desk` token. |
| `MAX_ATTACHMENT_MB` | Default `10`. |
| `PORT` | Default `8080`. |

In a cluster the `desk` component of `charts/exradius` sets all of this from values.

## Build and test

```bash
uv sync && uv run pytest
cd web && npm install && npm run lint && npm test && npm run build
docker build --build-context kit=../kit -t exradius-desk .
exradius-desk migrate     # the database; the chart's init container runs it
```

Locally against the kind cluster, from the repository root:

```bash
make -C deploy/kind dev-desk     # Keycloak with ada/ada, ben/ben (team-a), cem/cem (team-b)
tilt up                          # Desk at http://localhost:3200
make -C deploy/kind desk-token   # once the api answers on localhost:8100
```
