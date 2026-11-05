import os
import stat
from pathlib import Path

import pytest

from exradius.worker import prepare
from tests.conftest import git


def repo(remote: Path, branch: str = "main", name: str = "app") -> prepare.RepoSpec:
    return prepare.RepoSpec(name=name, url=str(remote), branch=branch)


def test_run_directory_layout(tmp_path):
    paths = prepare.create_run_directory(tmp_path, "run-1")
    assert paths.root == tmp_path / "runs" / "run-1"
    assert paths.project.is_dir() and paths.state.is_dir()
    assert paths.attachments == paths.project / ".exradius" / "attachments"
    assert paths.attachments.is_dir()


def test_mirror_and_clone(tmp_path, remote_repo):
    data = tmp_path / "data"
    paths = prepare.create_run_directory(data, "run-1")
    prepare.checkout_repositories(data, "billing", paths, [repo(remote_repo)])

    mirror = prepare.mirror_path(data, "billing", "app")
    assert (mirror / "HEAD").exists() and not (mirror / ".git").exists()  # bare mirror

    clone = paths.project / "app"
    assert (clone / "README.md").read_text() == "hello\n"
    assert not (clone / "feature.txt").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone).strip() == "main"
    # origin points at the real remote, not at the host-only mirror path
    assert git("remote", "get-url", "origin", cwd=clone).strip() == str(remote_repo)
    # --local clones do not depend on the mirror through alternates
    assert not (clone / ".git" / "objects" / "info" / "alternates").exists()


def test_configured_branch_is_checked_out(tmp_path, remote_repo):
    data = tmp_path / "data"
    paths = prepare.create_run_directory(data, "run-1")
    prepare.checkout_repositories(data, "billing", paths, [repo(remote_repo, branch="feature")])
    clone = paths.project / "app"
    assert (clone / "feature.txt").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone).strip() == "feature"


def test_mirror_is_refreshed_with_fetch(tmp_path, remote_repo):
    data = tmp_path / "data"
    first = prepare.create_run_directory(data, "run-1")
    prepare.checkout_repositories(data, "billing", first, [repo(remote_repo)])

    # Push a new commit to the remote.
    work = tmp_path / "pusher"
    git("clone", "-q", str(remote_repo), str(work))
    (work / "new.txt").write_text("new\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "new", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)

    second = prepare.create_run_directory(data, "run-2")
    prepare.checkout_repositories(data, "billing", second, [repo(remote_repo)])
    assert (second.project / "app" / "new.txt").exists()
    assert not (first.project / "app" / "new.txt").exists()  # runs never share a directory


def test_concurrent_runs_are_isolated(tmp_path, remote_repo):
    data = tmp_path / "data"
    a = prepare.create_run_directory(data, "run-a")
    b = prepare.create_run_directory(data, "run-b")
    for paths in (a, b):
        prepare.checkout_repositories(data, "billing", paths, [repo(remote_repo)])
    (a.project / "app" / "README.md").write_text("changed by a\n")
    assert (b.project / "app" / "README.md").read_text() == "hello\n"


def test_multiple_repositories(tmp_path, remote_repo):
    data = tmp_path / "data"
    paths = prepare.create_run_directory(data, "run-1")
    prepare.checkout_repositories(
        data, "billing", paths, [repo(remote_repo, name="api"), repo(remote_repo, "feature", name="web")]
    )
    assert sorted(p.name for p in paths.project.iterdir()) == [".exradius", "api", "web"]


def test_unknown_branch_fails(tmp_path, remote_repo):
    data = tmp_path / "data"
    paths = prepare.create_run_directory(data, "run-1")
    with pytest.raises(prepare.PreparationError, match="nope"):
        prepare.checkout_repositories(data, "billing", paths, [repo(remote_repo, branch="nope")])


def test_unreachable_remote_fails(tmp_path):
    data = tmp_path / "data"
    paths = prepare.create_run_directory(data, "run-1")
    with pytest.raises(prepare.PreparationError):
        prepare.checkout_repositories(data, "billing", paths, [repo(tmp_path / "missing.git")])


def test_auth_header_is_not_leaked_in_errors(tmp_path):
    spec = prepare.RepoSpec(name="app", url=str(tmp_path / "missing.git"), branch="main", auth="s3cret-token")
    with pytest.raises(prepare.PreparationError) as info:
        prepare.refresh_mirror(tmp_path, "billing", spec)
    assert "s3cret-token" not in str(info.value)


def test_attachments_are_read_only(tmp_path):
    paths = prepare.create_run_directory(tmp_path, "run-1")
    written = prepare.write_attachments(paths.attachments, [("stack.txt", b"trace"), ("a.png", b"\x89PNG")])
    prepare.seal_read_only(paths.attachments)
    assert [p.read_bytes() for p in written] == [b"trace", b"\x89PNG"]
    for path in written:
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert stat.S_IMODE(paths.attachments.stat().st_mode) == 0o555
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            (paths.attachments / "new.txt").write_text("x")


def test_attachment_name_validation(tmp_path):
    with pytest.raises(prepare.PreparationError):
        prepare.write_attachments(tmp_path, [("../escape", b"x")])


def test_cleanup_removes_run_directory_but_keeps_mirror(tmp_path, remote_repo):
    data = tmp_path / "data"
    paths = prepare.create_run_directory(data, "run-1")
    prepare.checkout_repositories(data, "billing", paths, [repo(remote_repo)])
    prepare.write_attachments(paths.attachments, [("a.txt", b"x")])
    prepare.seal_read_only(paths.attachments)

    prepare.remove_run_directory(paths)
    assert not paths.root.exists()
    assert (prepare.mirror_path(data, "billing", "app") / "HEAD").exists()
    prepare.remove_run_directory(paths)  # idempotent
