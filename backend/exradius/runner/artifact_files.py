"""Read only regular files beneath the working directory, without following symlinks."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from exradius.artifact_limits import MAX_FILE_BYTES


class ArtifactError(ValueError):
    pass


def read_artifact(root: Path, path: str) -> tuple[str, bytes]:
    if "\x00" in path:
        raise ArtifactError("a file path cannot contain NUL")
    candidate = Path(os.path.abspath(root / path))
    if not candidate.is_relative_to(root):
        raise ArtifactError(f"artifacts must be inside {root}")
    parts = candidate.relative_to(root).parts
    if not parts:
        raise ArtifactError("choose a regular file, not the working directory")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise ArtifactError("only regular files can be uploaded")
        with os.fdopen(descriptor, "rb") as stream:
            if metadata.st_size > MAX_FILE_BYTES:
                raise ArtifactError(f"a file is at most {MAX_FILE_BYTES} bytes")
            content = stream.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise ArtifactError(f"a file is at most {MAX_FILE_BYTES} bytes")
            return candidate.name, content
    except FileNotFoundError as exc:
        raise ArtifactError(f"no such file: {candidate}; paths are relative to {root}, not the repository") from exc
    except OSError as exc:
        raise ArtifactError(f"cannot read {candidate}; choose a readable regular file without symlinks") from exc
    finally:
        os.close(directory)
