# What a change in the code means for the config

`changes.sh` sorts what changed in a repository into groups. For each group: what to
read, what it can change in the config, and what proves the edit. `references/` is the
setup skill's folder (`../../exradius-project/references/`).

A group's line is where to look. Read the diff before deciding
(`git -C <checkout> diff <since> <until> -- <path>`), and read the file as it is now
when the diff alone does not say what the code needs.

## Named by the config

A path that `setup`, `notes`, a role or `check/start-app.sh` names is gone or changed.
The strongest findings: the config says something about this file.

- **Gone**: find what replaced it (`git log --diff-filter=D --stat -- <path>` shows the
  commit, and the commit usually shows the successor). A setup command that calls it
  fails at the next run; a `notes` command that names it sends the agent nowhere. Replace
  the command with what CI runs now, or remove the line when the thing itself is gone.
- **Changed**: a script setup runs may need a tool, a service or a variable it did not
  before; a sample file setup copies may have new keys. Read the diff; the check of
  step 3 shows whether setup still passes.

Proof: the setup step passes; the replaced `notes` command as a `--run`.

## Tool versions

A version file, a manifest's version field, a lockfile's format or a wrapper moved.

- The workstation image follows: the `FROM` tag or the tool's install line in
  `docker/workstation.Dockerfile`, or the `image:` tag. CI's version wins over a version
  file when they disagree; where no official image has the new version yet, the newest
  that has one, and the README says so (references/stacks.md).
- A lockfile format that moved (`lockfileVersion`, `BUNDLED WITH`, a `uv.lock` header)
  means the package manager itself must be at least the version that wrote it: the
  frozen install fails otherwise.
- `packageManager` changing from one tool to another (yarn to pnpm) is a package manager
  change: the next section.
- Version-dependent paths follow: a `cache` path with the language version in it
  (`~/.local/share/bundle/ruby/3.4.0/cache`), a Python path in `env`.
- `notes` and the project `description` when they name the version.

Proof: the image builds, "Check workstation image" passes, the frozen install passes.

## Package managers and workspaces

A lockfile or a manifest was added or deleted, or a workspace file changed.

- **A new package manager** (a second language, a new lockfile): its tool in the
  Dockerfile at the version the lockfile needs, its frozen install in `setup` (in CI's
  order), its download store in `workstation.cache` with the `env` the cache table names
  (references/pitfalls.md), its registry in `network.allow` (references/network.md).
- **One that is gone**: all four leave.
- **A new workspace or module**: does setup install and build it (a filter that names
  packages may miss it)? Does it have checks of its own? Then `notes` gain its commands,
  in the narrow form first.
- **One that is gone**: `setup` filters and `notes` lines that name it.

Proof: setup passes; one command per new package manager that downloads with an empty
cache, as a `--run` (`npm view <package> version`, `uv lock --upgrade-package <package>
--dry-run`, `cargo add --dry-run <crate>`); with a new `cache` entry, a second check
whose install downloads nothing.

## Registries

A registry configuration changed (`.npmrc`, `.yarnrc.yml`, `pip.conf`, `uv.toml`,
`settings.xml`, `NuGet.config`, `.cargo/config.toml`). The script does not show the
content: such a file can hold a token. Read the diff yourself and repeat host names and
variable names only.

- A new or changed host: `network.allow`, and the README's network line.
- A registry that needs a token: a secret name (`${secret:NPM_TOKEN}` in `env`), a row
  in the README with the narrowest permission (read packages).
- A registry on a private address: the README's "Ask the admin" (the install must allow
  it; no entry in the file lifts that).

Proof: the install in setup, and the empty-cache command of the section above in the
agent phase. The check's Network step lists the hosts that were reached and refused.

## Services and images

A compose file, a devcontainer or a Dockerfile of the code changed. The script prints
the services added and removed and the images that moved, next to what the config has
under the same name.

- **A service added**: it belongs in `services` when the tests or the running
  application connect to it, not because compose starts it. Look for what uses it (the
  env name that points at it, a client library that came with it). Then: the image at
  compose's version, `command`, the `env` it needs, `ports`, a `ready` check that proves
  the real port answers, and the application's own `env` pointed at the service by name
  (references/running-the-app.md, references/stacks.md for the common services).
- **An image version moved**: the same tag in `services`. A major version of a database
  can change the `ready` command, the data directory or a required variable: read the
  image's notes for that version before trusting the old block.
- **A service removed**: remove it when nothing in the code connects to it any more,
  with its `env` entries and its `notes` lines.
- **A Dockerfile of the code changed**: its `FROM` and its package lines say what the
  application now needs to build and start. System packages added there usually belong
  in the workstation Dockerfile too (an image library, `ffmpeg`, a database client).
- **A devcontainer changed**: the same reading; its features name tools and versions.

Proof: "Start service" passes with its `ready` check; a test that needs the service as a
`--run`; `check/start-app.sh` shows the application answering.

## Environment

Names were added to or removed from a tracked env file. The script prints names only and
marks the ones the config's `env` already has.

- **Added**: does the application fail to start, or a test fail, without it? Then it
  goes into `env`: pointed at a service by name, a test-only value in plain text, or
  `${secret:NAME}` when it is a real password, token or key. A name that merely tunes
  something (a log level, an optional integration) stays out.
- **Removed and still in the config**: remove it from `env` once nothing in the code
  reads it (`git grep NAME`). When it was a secret no other entry references, the key
  leaves the README's table and the admin may delete it from the store.
- A secret here is in the agent's environment: prefer a token that can only read, and
  say in the README what it must be allowed to do.

Proof: the application's start and the tests, which now read the variable. A new secret
gets a throwaway value in the check: a test that calls the real service with it fails
there and is listed as not proven.

## CI

A workflow or pipeline file changed. CI is the first source: it starts from a clean
clone, as a run does. The script prints the `*-version` and `image` values that came and
went; everything else is in the diff.

- **Install and build steps** that changed: `setup` follows, in CI's order.
- **Test, lint, type-check and format commands** that changed: `notes` follow, with the
  narrow form of each, and the role's sentence about which checks to run when it names
  them.
- **Service containers** that came, went or moved to another version: `services`, as in
  "Services and images".
- **Tool versions**: as in "Tool versions".
- **A new job** (an e2e suite, a new linter): can it run in a run? If so `notes` gain
  its command; if not (it needs a browser farm, a cloud account, a Docker daemon)
  `notes` say it stays for CI.
- **A job that is gone**: its command leaves `notes`.

Proof: every command you add or change in `notes`, as a `--run`.

## Scripts and task runners

`Makefile`, `justfile`, a `Taskfile`, `Procfile`, the `scripts` of a `package.json`, or
an entry script (`bin/setup`, `bin/dev`) changed.

- A script name that `setup` or `notes` use was renamed, removed or now runs something
  else: follow it. `lint` going from one linter to another may need the new tool in the
  image and its config's hosts in `network.allow`.
- A new script that checks a change (a type check, a new test target) is a candidate for
  `notes` when CI runs it.
- `Procfile` and `bin/dev` describe how the application starts: a new process there (a
  worker, an asset watcher) belongs in `check/start-app.sh` and in the `notes` on
  running the application.

Proof: the commands as `--run`; the start as `--run-file`.

## The team's guide and delivery

`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, the pull request template, `CODEOWNERS`,
`.claude/`, or git hooks changed. `notes` point at the guide and do not copy it, so most
edits here need nothing. Read the diff for what would stop or mislead an agent:

- a rule against AI-written contributions, a notice that the project moved, a delivery
  procedure that contradicts the config: a question for the developer (step 4), never
  something to work around;
- commands the guide now tells agents to run that differ from `notes`: the guide is the
  second source after CI; bring `notes` in line and prove the command;
- a new branch to target, a new commit convention: the `notes` line on delivery;
- hooks that came (husky, lefthook, pre-commit): setup must install what they call, and
  `notes` say what a commit runs. Prove it with a commit as a `--run`.

## Clone

`.gitmodules` or the LFS patterns of `.gitattributes` changed. The clone fetches neither
submodules nor LFS objects (references/pitfalls.md): a new submodule is either a
repository of its own in the file or fetched in `setup`; new LFS patterns need
`git lfs pull` in setup when the checks read those files, and `git-lfs` in the image.

## New top-level folders

A folder appeared at the root of a repository. Often a new application, service or
workspace: look for its manifest and its CI job. When it has its own checks, `setup`
and `notes` gain it as in "Package managers and workspaces".

## Dependencies

Manifests changed without a version field moving. Nearly always nothing to do: setup
installs from the lockfile. Look for two things:

- a dependency that compiles or loads a system library (an image, PDF, crypto, database
  or browser binding): the library and its headers in the workstation Dockerfile. The
  check's install or the first test that loads it shows this;
- a tool that changes how the project is checked (a new test runner, a browser
  automation package): `notes`, and for a browser the Dockerfile's libraries and the
  browser download in setup (references/pitfalls.md).

## What no diff shows

- **A repository that is not in the file.** A second checkout in a CI workflow, a path
  dependency that points outside the repository (`../shared`), a compose build context
  outside it. A question for the developer.
- **The repository itself.** The report's notes: `origin` moved (a rename or a transfer;
  the `url` follows), the default branch changed (the `branch` follows when the team
  moved, after asking), the branch is gone.
- **Statements with a date.** "The suite is red on main since November" is checked
  again: run it, compare with CI, and update or remove the sentence.
- **What the install changed.** A catalog connection that Exradius no longer has shows
  as a `--validate` error; a host the admin's install now allows or refuses does not
  show at all. The developer or the admin says so.
