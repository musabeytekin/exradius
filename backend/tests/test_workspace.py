"""Git helpers (exradius/git.py) and the runner's pod helper modes (exradius/runner/workspace.py):
``--prepare``, ``--clone`` and the ``files`` sidecar (docs/kubernetes.md §7)."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from exradius import git as gitcmd
from exradius.runner import workspace
from exradius.runner.client import RunEnded
from exradius.schemas.internal import AnswersResponse, AttachmentItem, AttachmentPath, DeliveredAnswer
from tests.conftest import git


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# -- exradius.git -----------------------------------------------------------------------------


def test_ls_remote_lists_branches_and_the_default(remote_repo):
    refs = gitcmd.ls_remote(str(remote_repo))
    assert refs.branches == ["feature", "main"] and refs.default_branch == "main"


def test_clone_default_branch_and_origin(tmp_path, remote_repo):
    dest = tmp_path / "app"
    gitcmd.clone(str(remote_repo), dest, None, None)
    assert (dest / "README.md").read_text() == "hello\n" and not (dest / "feature.txt").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest).strip() == "main"
    assert git("remote", "get-url", "origin", cwd=dest).strip() == str(remote_repo)


def test_clone_a_branch(tmp_path, remote_repo):
    dest = tmp_path / "app"
    gitcmd.clone(str(remote_repo), dest, "feature", None)
    assert (dest / "feature.txt").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest).strip() == "feature"


def test_clone_with_a_credential_keeps_it_out_of_the_clone(tmp_path, remote_repo):
    dest = tmp_path / "app"
    gitcmd.clone(str(remote_repo), dest, None, "s3cret-token")  # a local remote ignores the header
    assert "s3cret-token" not in (dest / ".git" / "config").read_text()


def test_unknown_branch_fails(tmp_path, remote_repo):
    with pytest.raises(gitcmd.GitError, match="nope"):
        gitcmd.clone(str(remote_repo), tmp_path / "app", "nope", None)


def test_unreachable_remote_fails(tmp_path):
    with pytest.raises(gitcmd.GitError):
        gitcmd.clone(str(tmp_path / "missing.git"), tmp_path / "app", None, None)


def test_auth_is_not_leaked_in_errors(tmp_path):
    with pytest.raises(gitcmd.GitError) as info:
        gitcmd.clone(f"https://user:pass@{tmp_path.name}.invalid/x.git", tmp_path / "app", None, "s3cret-token")
    assert "s3cret-token" not in str(info.value) and "pass@" not in str(info.value)
    assert gitcmd.redact("Authorization: Basic eC1hY2Nlc3MtdG9rZW46czNjcmV0LXRva2Vu s3cret-token",
                         "s3cret-token") == "Authorization: Basic *** ***"


def test_mirror_is_created_then_fetched(tmp_path, remote_repo):
    mirror = tmp_path / "mirrors" / "app.git"
    gitcmd.sync_mirror(mirror, str(remote_repo), None)
    assert (mirror / "HEAD").exists() and not (mirror / ".git").exists()  # bare mirror
    work = tmp_path / "pusher"
    git("clone", "-q", str(remote_repo), str(work))
    (work / "new.txt").write_text("new\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "new", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    gitcmd.sync_mirror(mirror, str(remote_repo), None)
    assert "new" in git("log", "--format=%s", "main", cwd=mirror)


# -- the workspace --------------------------------------------------------------------------------


def test_layout_modes(tmp_path):
    workspace.layout(tmp_path)
    assert mode(tmp_path / "work") == 0o755 and mode(tmp_path / "home") == 0o755
    assert mode(tmp_path / "tmp") == 0o1777 and mode(tmp_path / "state") == 0o755
    assert mode(tmp_path / "work" / ".exradius" / "attachments") == 0o775  # the files sidecar's uid is in the group


def test_write_file_is_read_only_and_checks_names(tmp_path):
    written = workspace.write_file(tmp_path, "stack.txt", b"trace")
    assert written.read_bytes() == b"trace" and mode(written) == 0o444
    workspace.write_file(tmp_path, "stack.txt", b"again")  # replaced atomically
    assert written.read_bytes() == b"again"
    assert not list(tmp_path.glob(".*.partial"))
    for bad in ("../escape", "a/b", ".", "..", "nul\x00"):
        with pytest.raises(ValueError):
            workspace.write_file(tmp_path, bad, b"x")


class FakeRunApi:
    """What ``workspace`` reads from the internal API."""

    def __init__(self, attachments=(), blobs=None, answers=()):
        self.attachments = list(attachments)
        self.blobs = dict(blobs or {})
        self.answers = list(answers)  # AnswersResponse per poll; RunEnded or the last one repeats
        self.closed = False
        self.polls = 0

    async def list_attachments(self):
        return self.attachments

    async def get_attachment(self, attachment_id):
        return self.blobs[attachment_id]

    async def wait_answers(self, wait):
        self.polls += 1
        await asyncio.sleep(0)
        item = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        self.closed = True


async def test_prepare_writes_the_task_attachments_read_only(tmp_path, monkeypatch):
    api = FakeRunApi([AttachmentItem(id="a1", name="stack.txt", content_type="text/plain")], {"a1": b"trace"})
    seen = {}

    def make(url, run_id, token):
        seen.update(url=url, run_id=run_id, token=token)
        return api

    monkeypatch.setattr(workspace, "HttpRunApi", make)
    assert await workspace.prepare("task", "r-1", "http://api:8001", "tok", tmp_path) == 0
    assert seen == {"url": "http://api:8001", "run_id": "r-1", "token": "tok"}
    attachment = tmp_path / "work" / ".exradius" / "attachments" / "stack.txt"
    assert attachment.read_bytes() == b"trace" and mode(attachment) == 0o444
    assert workspace.marker(tmp_path / "state", "prepare").exists() and api.closed

    # A recreated pod finds the marker and changes nothing.
    monkeypatch.setattr(workspace, "HttpRunApi", lambda *a: pytest.fail("prepared twice"))
    (tmp_path / "home" / "kept").write_text("x")
    assert await workspace.prepare("task", "r-1", "http://api:8001", "tok", tmp_path) == 0
    assert (tmp_path / "home" / "kept").exists()


async def test_prepare_of_a_verify_run_fetches_no_attachments(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "HttpRunApi", lambda *a: pytest.fail("a verify Run has no attachments"))
    assert await workspace.prepare("verify", "v-1", "http://api:8001", "tok", tmp_path) == 0
    assert (tmp_path / "work" / ".exradius" / "attachments").is_dir()


def clone_env(remote, folder="app", branch="", index="0"):
    return {"EXRADIUS_CLONE_FOLDER": folder, "EXRADIUS_CLONE_URL": str(remote), "EXRADIUS_CLONE_BRANCH": branch,
            "EXRADIUS_CLONE_INDEX": index}


def test_clone_step(tmp_path, remote_repo, capsys):
    work, state = tmp_path / "work", tmp_path / "state"
    work.mkdir()
    assert workspace.clone(work, state, clone_env(remote_repo, branch="feature")) == 0
    assert (work / "app" / "feature.txt").exists()
    assert workspace.marker(state, "clone-0").exists()
    assert "checked out feature at" in capsys.readouterr().out

    # A recreated pod keeps the clone (and the agent's work in it).
    (work / "app" / "wip.txt").write_text("agent work")
    assert workspace.clone(work, state, clone_env(remote_repo, branch="feature")) == 0
    assert (work / "app" / "wip.txt").exists()


def test_clone_step_without_a_branch_and_a_half_done_attempt(tmp_path, remote_repo):
    work, state = tmp_path / "work", tmp_path / "state"
    (work / "api").mkdir(parents=True)
    (work / "api" / "partial").write_text("left by a killed attempt")
    assert workspace.clone(work, state, clone_env(remote_repo, folder="api", index="1")) == 0
    assert not (work / "api" / "partial").exists() and (work / "api" / "README.md").exists()
    assert workspace.marker(state, "clone-1").exists()


def test_clone_step_failure_is_redacted(tmp_path, capsys):
    work, state = tmp_path / "work", tmp_path / "state"
    work.mkdir()
    code = workspace.clone(work, state, clone_env("https://user:s3cret-token@127.0.0.1:9/missing.git"))
    assert code == 1
    out = capsys.readouterr().out
    assert "failed" in out and "s3cret-token" not in out
    assert not workspace.marker(state, "clone-0").exists()


def test_clone_and_mirror_go_through_the_proxy_route(tmp_path, remote_repo):
    """No credential in the clone container: the mirror's fetch is rewritten to the egress
    proxy's git route (a stand-in that adds the credential), and both the mirror and the
    clone keep the plain URL as origin (docs/kubernetes.md §7.5)."""
    import shutil
    import subprocess

    from tests.test_git_identity import GitHttpServer, RouteProxy

    root = tmp_path / "served"
    root.mkdir()
    shutil.copytree(remote_repo, root / "app.git")
    server = GitHttpServer(root, "x-access-token", "repo-token")
    proxy = RouteProxy("x-access-token:repo-token")
    try:
        url = f"{server.url}/app.git"
        work, state, mirrors = tmp_path / "work", tmp_path / "state", tmp_path / "mirrors"
        work.mkdir()
        env = {**clone_env(url, branch="feature"), "EXRADIUS_MIRROR_DIR": str(mirrors)}
        assert workspace.clone(work, state, env, proxy.url) == 0
        assert (work / "app" / "feature.txt").exists()
        assert proxy.requests and all(" /git/127.0.0.1:" in r for r in proxy.requests)
        for repo in (mirrors / "app.git", work / "app"):
            origin = subprocess.run(["git", "config", "remote.origin.url"], cwd=repo, capture_output=True, text=True)
            assert origin.stdout.strip() == url
        # Without the route there is no credential: the server refuses.
        work2, state2 = tmp_path / "work2", tmp_path / "state2"
        work2.mkdir()
        assert workspace.clone(work2, state2, clone_env(url)) == 1
    finally:
        proxy.close()
        server.close()


async def test_files_sidecar_writes_answer_attachments(tmp_path, monkeypatch):
    answer = DeliveredAnswer(request_id="req-1", question="Logs?", answer="here", attachments=[
        AttachmentPath(path=".exradius/attachments/req-1/log.txt", content_type="text/plain", id="req-1.0"),
    ])
    api = FakeRunApi(blobs={"req-1.0": b"the log"}, answers=[
        AnswersResponse(state="waiting_for_input"),
        AnswersResponse(state="resuming", answers=[answer]),
        AnswersResponse(state="resuming", answers=[answer]),  # written once
        AnswersResponse(state="completed"),
    ])
    monkeypatch.setattr(workspace, "HttpRunApi", lambda *a: api)
    assert await asyncio.wait_for(workspace.files("r-1", "http://api", "tok", tmp_path), 5) == 0
    written = tmp_path / "req-1" / "log.txt"
    assert written.read_bytes() == b"the log" and mode(written) == 0o444
    assert mode(tmp_path / "req-1") == 0o555
    assert api.closed


async def test_files_sidecar_stops_when_the_run_is_finalised(tmp_path, monkeypatch):
    api = FakeRunApi(answers=[RunEnded("401")])
    monkeypatch.setattr(workspace, "HttpRunApi", lambda *a: api)
    assert await asyncio.wait_for(workspace.files("r-1", "http://api", "tok", tmp_path), 5) == 0


def test_read_only_attachments_cannot_be_changed(tmp_path):
    directory = tmp_path / "req"
    directory.mkdir()
    workspace.write_file(directory, "a.txt", b"x")
    directory.chmod(0o555)
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            (directory / "new.txt").write_text("x")
    directory.chmod(0o755)
