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
    assert paths.home == paths.root / "home" and paths.home.is_dir()
    assert paths.tmp == paths.root / "tmp" and stat.S_IMODE(paths.tmp.stat().st_mode) == 0o1777


def test_ensure_user_dirs_recreates_missing_home_and_tmp(tmp_path):
    """A run directory from before home and /tmp were on disk has neither; a resume must
    still mount them."""
    paths = prepare.create_run_directory(tmp_path, "run-1")
    paths.home.rmdir()
    paths.tmp.rmdir()
    prepare.ensure_user_dirs(paths, 1000, 1000)
    assert paths.home.is_dir() and stat.S_IMODE(paths.tmp.stat().st_mode) == 0o1777
    (paths.home / "cache").write_text("kept")
    (paths.tmp / "staged").write_text("kept")
    prepare.ensure_user_dirs(paths, 1000, 1000)
    assert (paths.home / "cache").read_text() == (paths.tmp / "staged").read_text() == "kept"


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


def test_cleanup_copes_with_what_package_managers_leave_in_home(tmp_path, monkeypatch):
    """Read-only files and directories (Go's module cache), an unreadable directory,
    sockets and FIFOs, and symlinks out of the tree, which are removed and not followed."""
    import socket

    paths = prepare.create_run_directory(tmp_path / "data", "run-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    outside.chmod(0o555)

    module = paths.home / "go" / "pkg" / "mod" / "example.com" / "m@v1.0.0"
    module.mkdir(parents=True)
    (module / "go.mod").write_text("module m")
    for path in [module / "go.mod", module, *module.parents]:
        if path == paths.home:
            break
        path.chmod(0o444 if path.is_file() else 0o555)
    locked = paths.home / ".cache" / "locked"
    locked.mkdir(parents=True)
    (locked / "f").write_text("x")
    locked.chmod(0)
    os.mkfifo(paths.home / "fifo")
    monkeypatch.chdir(paths.home)  # AF_UNIX paths are short; tmp_path is not
    server = socket.socket(socket.AF_UNIX)
    server.bind("agent.sock")
    server.close()
    (paths.home / "link-file").symlink_to(outside / "keep.txt")
    (paths.home / "link-dir").symlink_to(outside)
    monkeypatch.chdir(tmp_path)

    prepare.remove_run_directory(paths)
    assert not paths.root.exists()
    assert (outside / "keep.txt").read_text() == "keep"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o555
    outside.chmod(0o755)


def test_cleanup_removes_deep_trees(tmp_path):
    """Deeper than the recursion limit and longer than PATH_MAX (nested node_modules),
    with a read-only directory at the bottom."""
    paths = prepare.create_run_directory(tmp_path, "run-1")
    fd = os.open(paths.home, os.O_RDONLY)
    try:
        for _ in range(1500):
            os.mkdir("node_modules", dir_fd=fd)
            inner = os.open("node_modules", os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = inner
        os.close(os.open("index.js", os.O_WRONLY | os.O_CREAT, 0o444, dir_fd=fd))
        os.mkdir("dist", dir_fd=fd)  # read-only at the bottom: needs the second pass
        os.close(os.open("dist/out.js", os.O_WRONLY | os.O_CREAT, 0o444, dir_fd=fd))
        os.chmod("dist", 0o500, dir_fd=fd)
    finally:
        os.close(fd)
    prepare.remove_run_directory(paths)
    assert not paths.root.exists()
