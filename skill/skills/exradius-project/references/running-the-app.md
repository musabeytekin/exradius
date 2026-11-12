# Running the application in a run

Some tickets cannot be done from the tests: "reproduce this in the running app", a page
that renders wrong, an endpoint that fails only with real data. The agent then starts the
application in its workstation, as a developer does on a laptop, and talks to it on
`localhost`. Verify never starts it, and neither does a check that runs only tests. In
this skill's trials on twelve open source projects every setup passed its checks; two
applications could not start at all (one lacked ffmpeg, one a Redis service and a data
folder), and not one project's `notes` said how to start it: every agent spent its first
turns finding the command, the port, a login and the switch from the test settings.

So a setup is done when the application was started once under the run's conditions and
`notes` say how. A library or a one-shot command line tool has nothing to keep running:
run its main function once the same way (build the binary and call it), and say that.

## What to find while reading

- **How developers start it**: the dev compose file's `command:`, a devcontainer's
  `postStartCommand`, `Procfile` and `Procfile.dev`, `bin/dev`, Makefile targets (`run`,
  `dev`, `serve`), package scripts (`dev`, `start`), the README's and CONTRIBUTING's
  development section. Not the production Dockerfile's `CMD`: it starts a built artifact
  a run does not have.
- **Which processes**: the server; a worker or queue consumer; a frontend dev server or a
  one-time asset build. Start what the application is unusable without.
- **The port, and a request that proves it works**: a page or an API call that reads the
  database and needs the built assets, not only a health endpoint or an open port.
- **What it needs to start that no test needs.** A queue, cache or search service the
  tests mock; a program it validates at start (ffmpeg, ImageMagick, a PDF renderer); data
  the development image carries (a GeoIP database, geodata, fonts); folders whose default
  is on the read-only root (uploads, media, a model cache).
- **The switch from testing to development**: the mode variable or settings module
  (`RAILS_ENV`, `MIX_ENV`, `NODE_ENV`, `DJANGO_SETTINGS_MODULE`, a Spring profile), the
  development database and who creates and migrates it, the seed.
- **A login**: the user the development seed creates, the command that creates one, or
  the first-run wizard's API, called once in the start script and written into `notes`.
- **A user interface in another repository**: either that repository belongs to the
  project (step 2 asks), or the server is started without it (a `--nowebclient` kind of
  switch) and `notes` say the API is what there is.

## Where each need goes

| The application needs | Goes into |
|---|---|
| a service (the dev compose's Redis, queue, search) | `services`, with a `ready` check, even though no test connects to it |
| a system program or library | the workstation Dockerfile |
| data files a development image carries | the Dockerfile (`COPY --from=<that image>@<digest> <path> <path>`), or a setup download when it is small |
| a writable folder instead of a default on the root filesystem | `env`, pointing below `/home/runner` or `/tmp` |
| settings true for tests and for running (service URLs, a throwaway key) | `env` |
| the mode switch (`RAILS_ENV=test`, `MIX_ENV=test`, a test settings module) | the command, not `env`: test runners set their own mode, and a mode in `env` makes a plain start use the test settings (Phoenix does not even listen under `MIX_ENV=test`) |
| the development database, its migrations and seed; an asset build; a first compile | a setup command when it takes under about a minute; otherwise `notes`, with how long it takes |

Every run pays for setup and most tickets never start the application: when a step is
slow, leave it to `notes` and say its duration, so the agent decides.

## Prove it with the check

Write the start as a script, keep it in the config repository as `check/start-app.sh`,
and pass it with `--run-file` (it runs from `/work` in the agent phase, by its `#!` line):

```bash
#!/bin/bash
# What notes tell the agent, run once: start the app, prove it answers.
set -u
cd /work/<repo>
<the commands notes give before the start: database, assets>
<start command> > /tmp/app.log 2>&1 &

for i in $(seq 1 90); do
  curl -fs -o /dev/null http://localhost:<port>/<health path> && break
  sleep 2
done
# A request that needs the database and the assets. Its output is the proof.
if ! curl -fsS http://localhost:<port>/<page or API call> | head -c 400; then
  echo; echo "--- the app's log"; tail -n 60 /tmp/app.log
  exit 1
fi
```

- The same commands, word for word, that `notes` will give. When the script needs a
  trick `notes` do not mention, the agent will need it too. So write `notes` from the
  finished script, line by line, not from memory: every command and request in it that
  changes something is in `notes`, in the same order. (One script made a `GET` before
  the `POST` that sets the first user's password; the `GET` created that user, `notes`
  left it out, and an agent following them locked itself out.)
- Wait for the application itself. Some answer on their port before they are ready (a
  placeholder server during migrations, a maintenance page): then wait for the log line
  that says it is up (`grep -q 'Startup complete' /tmp/app.log`) or for a route only the
  real application serves, and tell `notes` which.
- Listen on and request `localhost` or `127.0.0.1`. A request to the run's own address
  (the one a server prints as "available on http://10.x.x.x:8080") goes to the proxy and
  is refused. Some applications accept only the host name they are configured for (a
  cookie domain, CSRF origin): use that one, usually `localhost`.
- The check prints the last ten lines of a passing step: let them be the evidence (a
  status code, a piece of the page, the logged-in user's name), and make the script exit
  non-zero when the evidence is missing. A script that proves only an open port has let
  a server pass that could not reach its queue.
- When it is cheap, one real action: log in as the seeded user, create a record.
- Without `curl` in the image, use the language's own client (`python3 -c`, `node -e`)
  or add `curl` to the Dockerfile: the agent needs it for the same purpose.
- What the script leaves running ends with the step. In a run nothing ends it for the
  agent: when the application holds a port or the database the tests use, `notes` say how
  to stop it (`pkill -f '<pattern>'`; a Makefile's stop target may kill only a wrapper).
- The step's network is the agent's. Update checks and telemetry an application sends at
  start are refused and show as `refused in the agent phase`; that is harmless when the
  application runs, and they stay off `network.allow`. Name them in `notes` only when
  they fill the log, so the agent does not chase them.
- The application and the agent's own commands share `resources.memory`, and an agent
  leaves the application running while it rebuilds or tests. The check measures each
  step alone: add this step's peak to the heaviest other one.

## What `notes` say

Short, after the check commands, in the form that just ran. Two that were proven this
way (each project's own values: do not copy the commands, copy the shape):

```yaml
notes: |
  ...
  Running the app (to reproduce a bug; the tests do not need it): setup created and
  seeded the development databases. `RAILS_ENV=development bin/rails server -p 3000` and,
  for background jobs, `RAILS_ENV=development bin/jobs`, both in the background; it
  answers on http://localhost:3000/ within seconds. Log in as `test` / `test` (an admin).
  For realistic data run `RAILS_ENV=development bin/rails fake_data` first (20 s). Stop
  it with `pkill -f puma; pkill -f solid-queue`. There is no browser: check pages
  through their HTML.
```

```yaml
notes: |
  ...
  Running the app: the API with `cd backend && uv run fastapi dev --host 127.0.0.1 --port 8000`
  (http://localhost:8000/docs; it also serves the built frontend), the frontend dev
  server with `cd frontend && bun run dev --host 127.0.0.1 --port 5173 --strictPort`.
  The first superuser is `admin@example.com`, password as in the repository's `.env`.
  There is no browser here: check a page through its API calls and its HTML.
```

Say what the seed holds when an empty page would mislead ("users only, no stories"), and
the duration and memory of any step that is not quick. Say what is needed and nothing
else: the commands in order, the port and URL, the login,
what takes long, how to stop it when that matters, and what cannot work.

## What cannot work, and what to say instead

- **No browser**, unless the project gives its agents the `playwright` connection
  (pitfalls.md). Without it a bug that exists only in the rendered page is checked
  through the HTML and the API; `notes` say so.
- **Features that call hosts outside `network.allow`**: fetching a feed, a plugin
  catalogue, a metadata provider, a model download. Either the host goes on the list
  (ask the developer: it is where an agent's code may send data), or `notes` name the
  feature as out of reach. A model or data set can also be downloaded by a setup command
  into a `workstation.cache` folder.
- **Hardware and scale**: a GPU, a real media library, production data.

## The usual ways a start fails

| Seen | Cause | Fix |
|---|---|---|
| the server exits at start naming a program (`ffmpeg`, `convert`) or a missing file | it validates what only the development image has | install it in the Dockerfile; copy the data from that image |
| `ECONNREFUSED`, `EAI_AGAIN <name>`, "connection refused" for a host no service has | a service the tests mock | add the service |
| the server starts and serves nothing, or uses the test database | the test mode came from `env` | take the mode out of `env`; give it on the test commands when they need it |
| pages load without styles, or static files are 404 | assets are not built, or the settings in use do not serve them | the asset build in the script and `notes`; the development settings (Django: `runserver --insecure` under settings with `DEBUG` off) |
| `403` on a form post or a login that works in a browser | the cookie or CSRF host is `localhost` and the request used `127.0.0.1` | request the configured host name |
| "read-only file system" for uploads, media or a cache | a default path on the image | `env` pointing into the home or `/tmp` |
| `refused: <an address like 172.x or 10.x>` under the step | the script or a test called the run's own address | `localhost`; see network.md, "The run's own address" |
| the first request takes minutes | a second full compile for the development mode | say so in `notes`, or compile in setup when most tickets need it |
