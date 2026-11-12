# The network of a run

A run reaches the network only through Exradius's egress proxy. Nothing in the pod has a
direct route out: a request that does not go to the proxy is dropped, whatever the tool.
What the proxy lets through depends on the phase.

| Phase | Containers | Reaches |
|---|---|---|
| setup | cloning, the services while setup runs, every `workstation.setup` command; image builds too | any public host |
| agent | the agent's session, and the services from then on | its model, the project's repositories (git only), its connections, the install's own list, and `network.allow` |

So a config whose setup downloads everything passes Verify, and the agent still gets a
`403` the first time it needs a host nobody listed. `network.allow` is where the project
says what the agent may reach.

## What needs no entry

- **The services**: they are on the run's `localhost`.
- **The model**: Exradius routes it; no pod holds the key.
- **The repositories in `repositories`**: git fetches and pushes go through a route that
  adds the credential (a push only with `push: true`). Only git: a release download or an
  API call to the same host (`github.com`, `api.github.com`) is an ordinary request and
  needs an entry.
- **Connections the agent lists**: a catalog entry brings its own hosts (the registry its
  launcher downloads it from and the service's API; `gitlab` its `api_url`, `jira` its
  `url`), and a custom `url` connection its URL's host. A custom `command` connection
  brings none: list what it reaches.
- **Hosts the admin allowed for every project** (the install's `egress.allow`). The
  developer usually does not know them; assume none and say so in the README.

## Writing `network.allow`

```yaml
network:
  allow:
    - registry.npmjs.org
    - pypi.org
    - files.pythonhosted.org
    - '*.example-cdn.com'         # quoted: a leading * is YAML alias syntax
```

Host names or IPv4 addresses; `*.name` for every name below it (not the name itself). No
URLs, no ports (every port of an allowed host is allowed), no bare `*`, no `localhost`,
each once.

List what the agent's work needs after setup has run, and nothing "just in case": the
list is the project's statement of where code that an agent runs may send data.

1. **The package registries of every package manager in setup.** Without them the agent
   cannot add or upgrade a dependency, and a ticket that says "bump X" ends in a `403`.
   This is the entry almost every project needs.
2. **Hosts the tests and builds reach at run time**: a schema or fixture download, a
   browser download for end-to-end tests, a public API a test calls, a Maven or Gradle
   plugin that is resolved on first use rather than by the install.
3. **Hosts of custom stdio connections** (the package registry of an `npx`/`uvx` server
   and the API it talks to).

Take the hosts from evidence, in this order:

- **The check's own list.** `check.sh` runs setup through a proxy, and its last step,
  Network, prints `reached in setup: ...`, then `reached in setup only: ...` for the hosts
  among them the agent cannot reach, and `refused in the agent phase: ...` for what the
  `--run` commands asked for and did not get. Those are the real hosts, mirrors and CDNs
  included. Start from them.
- **The project's registry configuration**: `.npmrc` and `.yarnrc.yml` (`registry`,
  `npmRegistryServer`, scoped registries), `pip.conf`, `[[tool.uv.index]]` and
  `[tool.poetry.source]` in `pyproject.toml`, `settings.xml` mirrors and `<repositories>`
  in `pom.xml`, `repositories {}` in Gradle files, `GOPROXY`/`GOPRIVATE`,
  `.cargo/config.toml`, `source` lines in the `Gemfile`, `NuGet.config`.
- **The defaults**, when nothing overrides them:

| Tool | Hosts |
|---|---|
| npm, pnpm | `registry.npmjs.org` |
| Yarn 2+ | `registry.yarnpkg.com` (its default `npmRegistryServer`), or the registry `.yarnrc.yml` names |
| uv, pip, Poetry | `pypi.org`, `files.pythonhosted.org` |
| Maven | `repo.maven.apache.org` (plus any mirror the build tries first: Bank of Anthos needed `maven-central.storage-download.googleapis.com`) |
| Gradle | `repo.maven.apache.org`, `plugins.gradle.org`, and `services.gradle.org` for the wrapper's distribution |
| Go | `proxy.golang.org`, `sum.golang.org` |
| Cargo | `index.crates.io`, `static.crates.io` |
| Bundler | `rubygems.org`, `index.rubygems.org` |
| Composer | `repo.packagist.org` (packages are then downloaded from their source host, usually `api.github.com` and `codeload.github.com`) |
| NuGet | `api.nuget.org` |
| Playwright browsers | `cdn.playwright.dev`, `playwright.download.prss.microsoft.com` |

The table is a starting point from the tools' defaults; the check's list is what the
project really used. When they differ, the check is right.

Not every host the check prints belongs on the list. Plane's check showed `nodejs.org`
under `reached in setup only` (fetched once by the install) and `telemetry.vercel.com`
under `refused in the agent phase` (Turbo's telemetry); its three check commands passed
without either, so neither was listed. A host goes on the list when the agent's work
fails or is cut short without it.

Dependencies of a package that are fetched from git (`"dep": "github:owner/repo"`, a
`git+https` requirement) come from the git host as ordinary requests: they need
`github.com` and `codeload.github.com` listed even when the project's own repository is
on GitHub.

## What the agent is told, and what a refusal looks like

The agent's environment section ends with the list: "Outbound network access is limited
to the repositories above and to: registry.npmjs.org, pypi.org." It knows not to try
other hosts, so `notes` need not repeat the list. Say in `notes` only what follows from
it: "dependencies can be added (the registries are reachable); browsers for the e2e suite
are not downloaded here".

A refused request is a `403` whose text names the host:

- `<host> is not on this task's network allowlist (network.allow)`: add the host, or
  accept that the agent cannot reach it;
- `<host> resolves to a private or cluster-internal address`: see below.

curl prints `CONNECT tunnel failed, response 403`; Python and Go clients print the
reason. In Exradius the task page's Network tab lists every refused host with its reason,
which is where a developer finds the entry a failed task was missing.

## Limits no entry lifts

- **Private addresses.** `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`,
  loopback and link-local addresses are refused in every phase, setup and builds
  included, whatever `network.allow` says. An internal package mirror, a private registry
  or an internal API on such an address needs the admin's `egress.allowPrivate` in the
  install's values. Find these while reading the project (a registry host that is not
  public), name them in the README's "Ask the admin" section, and pass them to the check
  with `--allow-private`.
- **Only HTTP and HTTPS.** Anything else cannot leave the pod: an external Postgres or
  Redis, SMTP, an SSH remote. A database the tests need is a `services` entry, not an
  external host. Repository URLs must be `https://`; rewrite `git@host:owner/repo.git`
  as `https://host/owner/repo.git`. The same holds for the `postgres` catalog connection:
  it reaches a database in `services`, not one outside the run.
- **Tools that ignore the proxy variables.** Every container gets `HTTP_PROXY`,
  `HTTPS_PROXY` and `NO_PROXY` (and lower case), `YARN_HTTP_PROXY` and `YARN_HTTPS_PROXY`
  (Yarn 2+ reads only those), and `JAVA_TOOL_OPTIONS` with Java's proxy properties and
  the flag that makes Maven's resolver use them. npm, pnpm, Yarn, uv, pip, Maven, curl and
  git work as they are. A tool that reads none of these hangs until it times out
  ("connection timed out", "network is unreachable"): give it its own proxy setting in
  the workstation Dockerfile or in `env`, pointing at `$HTTPS_PROXY`. A project that sets
  its own `JAVA_TOOL_OPTIONS` in `env` keeps it; Exradius appends to it.
- **Image builds** go through the proxy as setup does: any public host, no private one.
  Build secrets are not given, so a Dockerfile cannot authenticate to a private registry
  at build time; install private packages in a setup command, where `env` secrets exist.

## Private package registries

When the project installs from a registry that needs a token (a scoped npm registry, a
private PyPI index, Artifactory, GitHub Packages):

- the token is a store key, referenced in `env` (`NPM_TOKEN: "${secret:NPM_TOKEN}"`), and
  the tool reads it from the environment as its config file already says
  (`//npm.pkg.github.com/:_authToken=${NPM_TOKEN}` in `.npmrc`, `UV_INDEX_<NAME>_PASSWORD`,
  `settings.xml` with `${env.ARTIFACTORY_TOKEN}`);
- its host goes into `network.allow` when the agent should be able to add dependencies
  from it;
- on a private address it also needs the admin's `egress.allowPrivate`.

A secret in `env` is in the agent's environment too. That is how the tool can use it; it
also means the agent can read it. Use a read-only token scoped to the registry.

## What the local check does about all this

`check.sh` puts the run's containers on a Docker network with no route out and starts a
small proxy next to them, with the same variables in every container. Setup runs with
the proxy open; the commands given with `--run` run with the agent phase's list
(`network.allow`, the connections' hosts, and `--allow HOST` for hosts the admin's
install allows). So a tool that ignores the proxy fails locally as it would in a run, and
a missing entry shows up as the same `403`.

Differences to know:

- the repositories' hosts are open in the local agent phase, because the check has no
  stand-in for git's credential route; the Network step names them
  (`repository hosts reached in the agent phase`) when something reached them, since in a
  run only git may;
- a tool that ignores the proxy fails at once locally ("Could not resolve host"), where
  in a run it waits for a timeout; the check adds a hint to the failed step either way;
- image builds use Docker's own network, not the proxy;
- the model and the connections' tool lists are not exercised.
