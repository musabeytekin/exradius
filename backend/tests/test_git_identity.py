"""Agent identity (docs/agent-identity.md): the git variables a Run's workstation gets
(``worker/git_identity.py``), the git config and hooks the runner and the bundle write
(``runner/gitconfig.py``), real commits with the identity and trailers, and pushes through
the credential helper."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from exradius.git import RepoSpec
from exradius.models import SecretStore, SecretStoreEntry
from exradius.runner import gitconfig
from exradius.worker import git_identity
from exradius.worker.git_identity import Identity, workstation_git
from exradius.worker.intake import accept_answer
from tests.conftest import BASE_CONFIG, commit_config, git
from tests.test_run_manager import cluster, manager, published, states, submit_task  # noqa: F401
from tests.fakes import runner

IDENTITY = Identity(name='Bug "Fixer" Bot', email="bug-fixer@agents.example", agent="bug-fixer", task_id="T-42")


def run_git(*args, cwd: Path, env: dict[str, str], check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=check, timeout=60)


def container_env(home: Path, extra: dict[str, str]) -> dict[str, str]:
    """What the workstation's shell would have: an empty home, no system git config of the
    host, no terminal to prompt on, plus what the Run spec adds."""
    home.mkdir(exist_ok=True)
    return {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            **extra}


def resolve(entries: list[dict[str, str]], secret: dict[str, str]) -> dict[str, str]:
    """Env entries as the kubelet resolves them (``secretKey`` from the per-run Secret)."""
    return {e["name"]: e["value"] if "value" in e else secret[e["secretKey"]] for e in entries}


# Contents ---------------------------------------------------------------------------------------


def test_workstation_git_environment():
    repos = [
        RepoSpec("api", "https://git.example/acme/api.git", "main", "ghp_token", push_allowed=True),
        RepoSpec("docs", "https://git.example/acme/docs.git", "main", "deploy:s3cret", push_allowed=True),
        RepoSpec("readonly", "https://git.example/acme/ro.git", "main", "ghp_other", push_allowed=False),
        RepoSpec("ssh", "git@git.example:acme/ssh.git", "main", "key", push_allowed=True),
    ]
    entries, secret = workstation_git(IDENTITY, repos)
    # Only push-allowed http(s) remotes with a secret; the token part, under push.<N>.
    assert secret == {"push.0": "ghp_token", "push.1": "s3cret"}
    assert [e for e in entries if "secretKey" in e] == [
        {"name": "EXRADIUS_GIT_CREDENTIAL_0", "secretKey": "push.0"},
        {"name": "EXRADIUS_GIT_CREDENTIAL_1", "secretKey": "push.1"},
    ]
    assert "ghp_token" not in json.dumps(entries) and "s3cret" not in json.dumps(entries)  # the spec is secret-free
    env = resolve(entries, secret)
    assert env == {
        "GIT_AUTHOR_NAME": 'Bug "Fixer" Bot', "GIT_AUTHOR_EMAIL": "bug-fixer@agents.example",
        "GIT_COMMITTER_NAME": 'Bug "Fixer" Bot', "GIT_COMMITTER_EMAIL": "bug-fixer@agents.example",
        "GIT_CONFIG_GLOBAL": "/exradius/state/git/config",
        "EXRADIUS_AGENT": "bug-fixer", "EXRADIUS_TASK_ID": "T-42",
        "EXRADIUS_GIT_CREDENTIAL_0": "ghp_token", "EXRADIUS_GIT_CREDENTIAL_1": "s3cret",
        # The hooks path and the helpers at the command-line level, where no file overrides them.
        "GIT_CONFIG_COUNT": "7",
        "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*",
        "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": "/opt/exradius/runtime/git-hooks",
        "GIT_CONFIG_KEY_2": "credential.https://git.example.useHttpPath", "GIT_CONFIG_VALUE_2": "true",
        "GIT_CONFIG_KEY_3": "credential.https://git.example/acme/api.git.helper", "GIT_CONFIG_VALUE_3": "",
        "GIT_CONFIG_KEY_4": "credential.https://git.example/acme/api.git.helper",
        "GIT_CONFIG_VALUE_4": '!f() { test "$1" = get || exit 0; echo "username=x-access-token"; '
                              'echo "password=$EXRADIUS_GIT_CREDENTIAL_0"; }; f',
        "GIT_CONFIG_KEY_5": "credential.https://git.example/acme/docs.git.helper", "GIT_CONFIG_VALUE_5": "",
        "GIT_CONFIG_KEY_6": "credential.https://git.example/acme/docs.git.helper",
        "GIT_CONFIG_VALUE_6": '!f() { test "$1" = get || exit 0; echo "username=deploy"; '
                              'echo "password=$EXRADIUS_GIT_CREDENTIAL_1"; }; f',
    }


def test_setup_containers_get_safe_directory_only():
    assert git_identity.setup_git() == [
        {"name": "GIT_CONFIG_COUNT", "value": "1"},
        {"name": "GIT_CONFIG_KEY_0", "value": "safe.directory"},
        {"name": "GIT_CONFIG_VALUE_0", "value": "*"},
    ]


def test_config_files_and_hooks(tmp_path):
    gitconfig.write_git_config(tmp_path, IDENTITY.name, IDENTITY.email)
    managed = (tmp_path / "git" / "exradius.gitconfig").read_text()
    assert '\tname = "Bug \\"Fixer\\" Bot"\n' in managed
    assert '\temail = "bug-fixer@agents.example"\n' in managed
    assert "[push]\n\tautoSetupRemote = true\n" in managed and "[init]\n\tdefaultBranch = main\n" in managed
    # Nothing a repository-local value could override lives in a file.
    assert "[core]" not in managed and "[credential" not in managed
    assert (tmp_path / "git" / "config").read_text().endswith("[include]\n\tpath = exradius.gitconfig\n")

    hooks_dir = tmp_path / "git-hooks"
    gitconfig.write_hooks(hooks_dir)
    hooks = sorted(p.name for p in hooks_dir.iterdir())
    assert hooks == sorted(gitconfig.HOOK_NAMES) and "push-to-checkout" not in hooks
    for name in hooks:
        hook = hooks_dir / name
        assert hook.read_text() == gitconfig.DISPATCHER and os.access(hook, os.X_OK)
    assert '--trailer "Exradius-Task: $EXRADIUS_TASK_ID"' in gitconfig.DISPATCHER


def test_global_config_is_kept_and_managed_config_rewritten(tmp_path):
    gitconfig.write_git_config(tmp_path, IDENTITY.name, IDENTITY.email)
    global_file = tmp_path / "git" / "config"
    global_file.write_text(global_file.read_text() + "[alias]\n\tst = status\n")  # the agent's own setting
    gitconfig.write_git_config(tmp_path, "Renamed", "new@agents.example")
    assert "st = status" in global_file.read_text()
    assert '"Renamed"' in (tmp_path / "git" / "exradius.gitconfig").read_text()


def test_the_runner_writes_the_git_config_at_start(tmp_path):
    from exradius.runner import main as runner_main

    runner_main._git_config(tmp_path, {"GIT_AUTHOR_NAME": "Bot", "GIT_AUTHOR_EMAIL": "bot@agents.example"})
    assert '"Bot"' in (tmp_path / "git" / "exradius.gitconfig").read_text()
    runner_main._git_config(tmp_path / "none", {})  # a verify pod has no identity: nothing written
    assert not (tmp_path / "none").exists()


# Real git ---------------------------------------------------------------------------------------


class Pod:
    """A clone and the state and bundle directories, as the pod mounts them, with the
    workstation's git variables pointed at them."""

    def __init__(self, root: Path, remote: Path, repos: list[RepoSpec] = ()):
        self.work = root / "work"
        self.state = root / "state"
        self.hooks = root / "runtime" / "git-hooks"
        self.app = self.work / "app"
        self.work.mkdir(parents=True)
        git("clone", "-q", str(remote), str(self.app))
        gitconfig.write_hooks(self.hooks)
        gitconfig.write_git_config(self.state, IDENTITY.name, IDENTITY.email)
        entries, secret = workstation_git(IDENTITY, list(repos))
        env = resolve(entries, secret)
        env = {k: v.replace(git_identity.HOOKS_PATH, str(self.hooks)).replace(
            git_identity.GLOBAL_CONFIG, str(self.state / "git" / "config")) for k, v in env.items()}
        self.env = container_env(root / "home", env)


@pytest.fixture
def pod(tmp_path, remote_repo):
    return Pod(tmp_path / "pod", remote_repo)


def test_commit_gets_identity_and_trailers(pod, tmp_path):
    env, app = pod.env, pod.app
    (app / "fix.txt").write_text("fixed\n")
    run_git("add", "fix.txt", cwd=app, env=env)
    run_git("commit", "-m", "Fix the invoices", cwd=app, env=env)

    fields = run_git("log", "-1", "--format=%an|%ae|%cn|%ce", cwd=app, env=env).stdout.strip()
    assert fields == 'Bug "Fixer" Bot|bug-fixer@agents.example|Bug "Fixer" Bot|bug-fixer@agents.example'
    message = run_git("log", "-1", "--format=%B", cwd=app, env=env).stdout
    assert message.strip() == "Fix the invoices\n\nExradius-Task: T-42\nExradius-Agent: bug-fixer"

    # Amending keeps one set of trailers.
    run_git("commit", "--amend", "--no-edit", cwd=app, env=env)
    message = run_git("log", "-1", "--format=%B", cwd=app, env=env).stdout
    assert message.count("Exradius-Task:") == 1 and message.count("Exradius-Agent:") == 1

    # Without the identity variables git falls back to the config file alone.
    bare = {k: v for k, v in env.items() if not k.startswith("GIT_AUTHOR") and not k.startswith("GIT_COMMITTER")}
    assert run_git("config", "user.email", cwd=app, env=bare).stdout.strip() == "bug-fixer@agents.example"
    # `git config --global` writes the kept file in the state directory, not the home.
    run_git("config", "--global", "alias.st", "status", cwd=app, env=env)
    assert "st = status" in (pod.state / "git" / "config").read_text()
    assert not (tmp_path / "pod" / "home" / ".gitconfig").exists()


def _hook(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def test_project_hooks_run_after_the_exradius_part(pod, tmp_path):
    """A repository-local core.hooksPath (husky's prepare script writes one) neither drops
    the trailers nor stops the project's own hooks."""
    env, app = pod.env, pod.app
    run_git("config", "core.hooksPath", ".husky/_", cwd=app, env=env)  # as `husky` does
    ran = tmp_path / "ran.log"
    _hook(app / ".husky" / "_" / "pre-commit", f'echo "pre-commit $(git rev-parse --show-toplevel)" >> {ran}\n')
    _hook(app / ".husky" / "_" / "commit-msg", f'echo "commit-msg $(basename "$1")" >> {ran}\n')
    _hook(app / ".husky" / "_" / "prepare-commit-msg", 'echo "Project-Ticket: PLANE-1" >> "$1"\n')
    (app / "fix.txt").write_text("fixed\n")
    run_git("add", "fix.txt", cwd=app, env=env)
    run_git("commit", "-m", "Fix the invoices", cwd=app, env=env)

    message = run_git("log", "-1", "--format=%B", cwd=app, env=env).stdout
    assert "Exradius-Task: T-42" in message and "Exradius-Agent: bug-fixer" in message
    assert "Project-Ticket: PLANE-1" in message  # the project's own prepare-commit-msg ran too
    assert ran.read_text().splitlines() == [f"pre-commit {app.resolve()}", "commit-msg COMMIT_EDITMSG"]


def test_a_failing_project_hook_fails_the_commit(pod):
    env, app = pod.env, pod.app
    run_git("config", "core.hooksPath", ".husky/_", cwd=app, env=env)
    _hook(app / ".husky" / "_" / "pre-commit", 'echo "lint-staged: 1 error" >&2\nexit 3\n')
    (app / "bad.txt").write_text("bad\n")
    run_git("add", "bad.txt", cwd=app, env=env)
    commit = run_git("commit", "-m", "Bad change", cwd=app, env=env, check=False)
    assert commit.returncode != 0 and "lint-staged: 1 error" in commit.stderr
    assert run_git("rev-list", "--count", "HEAD", cwd=app, env=env).stdout.strip() == "1"  # nothing committed


def test_default_hooks_folder_and_stdin(pod, tmp_path):
    """Without a configured hooks path the project's hooks are in .git/hooks; hooks that
    read stdin (pre-push, reference-transaction) get it unchanged."""
    env, app = pod.env, pod.app
    seen = tmp_path / "stdin.log"
    _hook(app / ".git" / "hooks" / "post-commit", f'echo post-commit >> {seen}\n')
    _hook(app / ".git" / "hooks" / "reference-transaction", f'[ "$1" = committed ] && cat >> {seen}\nexit 0\n')
    (app / "x.txt").write_text("x\n")
    run_git("add", "x.txt", cwd=app, env=env)
    run_git("commit", "-m", "X", cwd=app, env=env)
    lines = seen.read_text().splitlines()
    assert "post-commit" in lines
    assert any(line.endswith(" refs/heads/main") for line in lines)
    # A project env that sets its own command-line config cannot reach the container.
    assert git_identity.without_command_line_config({"GIT_CONFIG_COUNT": "0", "GIT_CONFIG_KEY_0": "x", "A": "1"}) == {"A": "1"}


class GitHttpServer:
    """``git http-backend`` behind HTTP Basic auth: a stand-in for a hosted remote."""

    def __init__(self, root: Path, username: str, password: str):
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        backend = Path(subprocess.run(["git", "--exec-path"], capture_output=True, text=True).stdout.strip()) / "git-http-backend"
        self.auth_attempts: list[str | None] = []
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                auth = self.headers.get("Authorization")
                server_self.auth_attempts.append(auth)
                if auth != expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                path, _, query = self.path.partition("?")
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                result = subprocess.run(
                    [str(backend)], input=body, capture_output=True, timeout=60,
                    env={"GIT_PROJECT_ROOT": str(root), "GIT_HTTP_EXPORT_ALL": "1", "PATH": os.environ["PATH"],
                         "REQUEST_METHOD": self.command, "PATH_INFO": path, "QUERY_STRING": query,
                         "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
                         "REMOTE_USER": username, "REMOTE_ADDR": "127.0.0.1", "GIT_HTTP_MAX_REQUEST_BUFFER": "100M"},
                )
                head, _, payload = result.stdout.partition(b"\r\n\r\n")
                status = 200
                headers = []
                for line in head.decode().split("\r\n"):
                    name, _, value = line.partition(":")
                    if name.lower() == "status":
                        status = int(value.split()[0])
                    elif name:
                        headers.append((name, value.strip()))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def http_remote(remote_repo, tmp_path):
    root = tmp_path / "served"
    root.mkdir()
    shutil.copytree(remote_repo, root / "app.git")
    subprocess.run(["git", "config", "http.receivepack", "true"], cwd=root / "app.git", check=True)
    server = GitHttpServer(root, "deploy", "push-token")
    yield server, root / "app.git"
    server.close()


def _commit(app: Path, env: dict[str, str]) -> None:
    (app / "pushed.txt").write_text("from the agent\n")
    run_git("add", "pushed.txt", cwd=app, env=env)
    run_git("commit", "-m", "Agent change", cwd=app, env=env)


@pytest.mark.parametrize("push_allowed", [True, False])
def test_push_through_the_credential_helper(http_remote, remote_repo, tmp_path, push_allowed):
    server, bare = http_remote
    url = f"{server.url}/app.git"
    repo = RepoSpec("app", url, "main", "deploy:push-token", push_allowed=push_allowed)
    pod = Pod(tmp_path / "pod", remote_repo, [repo])
    env, app = pod.env, pod.app
    run_git("remote", "set-url", "origin", url, cwd=app, env={"PATH": os.environ["PATH"]})  # as the clone step does
    run_git("checkout", "-b", "agent/fix", cwd=app, env=env)
    _commit(app, env)

    push = run_git("push", cwd=app, env=env, check=False)  # push.autoSetupRemote: no -u needed
    if push_allowed:
        assert push.returncode == 0, push.stderr
        log = run_git("log", "-1", "--format=%an %s", "agent/fix", cwd=bare, env=env).stdout.strip()
        assert log == 'Bug "Fixer" Bot Agent change'
        assert server.auth_attempts[-1] == "Basic " + base64.b64encode(b"deploy:push-token").decode()
    else:
        assert push.returncode != 0
        assert "Authentication failed" in push.stderr or "could not read Username" in push.stderr
        assert run_git("branch", "--list", "agent/fix", cwd=bare, env=env).stdout.strip() == ""
    # Nothing about the credential is written into the clone.
    clone_config = (app / ".git" / "config").read_text()
    assert "push-token" not in clone_config and "credential" not in clone_config and "extraHeader" not in clone_config


# Through the run manager --------------------------------------------------------------------------


async def test_every_session_of_a_run_has_the_identity(manager, sessions, store, cluster, published, configured, box,
                                                       accept):
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + (
        "secret_store: billing\nrepositories:\n"
        "  - {name: app, url: 'https://git.example/acme/app.git', branch: main, credential: '${secret:GIT_TOKEN}', push: true}\n"
    )})
    async with sessions() as session:
        secret_store = SecretStore(name="billing")
        session.add(secret_store)
        await session.flush()
        session.add(SecretStoreEntry(store_id=secret_store.id, key="GIT_TOKEN", value_encrypted=box.encrypt("ghp_push")))
        await session.commit()
    seen = []

    def check(run, env):
        seen.append({k: env.get(k) for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_EMAIL", "EXRADIUS_AGENT",
                                             "EXRADIUS_TASK_ID", "EXRADIUS_GIT_CREDENTIAL_0", "GIT_CONFIG_GLOBAL")})

    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status="waiting_for_input", question="Which region?", check=check))
    await manager.execute(run_id)
    [info] = [e for e in published if e.type == "info_request"]
    answer = json.dumps({"version": 1, "task_id": "T-1", "request_id": info.payload["request_id"], "answer": "eu"})
    await accept_answer(sessions, store, answer.encode())
    cluster.behaviours.append(runner(sessions, check=check))
    await manager.resume(run_id)
    await cluster.idle()

    assert states(published)[-1] == "completed"
    expected = {"GIT_AUTHOR_NAME": "Bug Fixer Bot", "GIT_COMMITTER_EMAIL": "bug-fixer@agents.example",
                "EXRADIUS_AGENT": "bug-fixer", "EXRADIUS_TASK_ID": "T-1", "EXRADIUS_GIT_CREDENTIAL_0": "ghp_push",
                "GIT_CONFIG_GLOBAL": "/exradius/state/git/config"}
    assert seen == [expected, expected]  # the first session and the resumed one, in the same pod
