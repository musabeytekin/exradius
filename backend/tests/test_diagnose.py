"""The pattern table of worker/diagnose.py (docs/setup-and-verify.md §1.3), on tails as the
worker records them."""

from __future__ import annotations

import pytest

from exradius.worker.diagnose import diagnose, diagnose_steps

# The three failures of the first week (outline, saleor, bank), as the steps logged them.

OUTLINE_COREPACK = "exited with 127\n/bin/sh: 1: corepack: not found"

SALEOR_UV = """exited with 1
warning: Failed to parse `pyproject.toml` during settings discovery:
  TOML parse error at line 146, column 17
      |
  146 | exclude-newer = "3 weeks"
      |                 ^^^^^^^^^
  failed to parse year in date "3 weeks": failed to parse "3 we" as year (a four digit integer): invalid digit, expected 0-9 but got  

Using CPython 3.12.16 interpreter at: /usr/local/bin/python3
Creating virtual environment at: .venv
Ignoring existing lockfile due to removal of timestamp cutoff: `global: 0001-01-01T00:00:00Z, django: 2026-11-03T15:25:00Z`
Resolved 229 packages in 5.93s
The lockfile at `uv.lock` needs to be updated, but `--locked` was provided. To update the lockfile, run `uv lock`."""

BANK_PYTHON3 = "exited with 127\nbash: line 1: python3: command not found"


def test_outline_corepack_missing():
    sentence = diagnose("Setup: cd outline && corepack enable && yarn install --immutable", OUTLINE_COREPACK)
    assert sentence.startswith("The workstation image has no `corepack`: node images from 25 on")
    assert "npm install -g corepack" in sentence


def test_saleor_uv_too_old_for_exclude_newer():
    sentence = diagnose("Setup: cd saleor && uv sync --locked", SALEOR_UV)
    assert sentence == ("The uv in the workstation image is older than the project's uv settings "
                        "(it cannot parse `exclude-newer`): install a newer uv in the workstation Dockerfile.")


def test_bank_python3_not_on_path():
    sentence = diagnose("Setup: make -C bank test", BANK_PYTHON3)
    assert sentence.startswith("The workstation image has no `python3`: install python3")


@pytest.mark.parametrize(
    ("log", "tool"),
    [
        ("exited with 127\n/bin/sh: 1: mvn: not found", "mvn"),
        ("exited with 127\nsh: pnpm: command not found", "pnpm"),
        ("exited with 127\n/usr/bin/env: 'node': No such file or directory", "node"),
        ("exited with 127\nenv: ‘python3’: No such file or directory", "python3"),
        ('OCI runtime create failed: exec: "bash": executable file not found in $PATH', "bash"),
    ],
)
def test_missing_executables_are_named(log, tool):
    assert diagnose("Setup: make deps", log).startswith(f"The workstation image has no `{tool}`")


def test_a_missing_script_is_a_path_not_a_tool():
    sentence = diagnose("Setup: ./gradlew build", "exited with 127\n/bin/sh: 1: ./gradlew: not found")
    assert sentence.startswith("`./gradlew` does not exist in the workstation when `./gradlew build` runs")


def test_killed_setup_is_out_of_memory():
    log = "exited with 137\n[2/4] Fetching packages...\n"
    assert diagnose("Setup: yarn install", log).startswith("`yarn install` was killed (exit 137): it ran out of memory")


def test_lockfile_tool_mismatches():
    yarn = ("exited with 1\nerror This project's package.json defines \"packageManager\": \"yarn@4.5.0\". "
            "However the current global version of Yarn is 1.22.22.")
    assert "does not match the project's `packageManager`" in diagnose("Setup: yarn install", yarn)
    pnpm = ("exited with 1\n ERR_PNPM_BAD_PM_VERSION  This project is configured to use v9 of pnpm. "
            "Your current pnpm is v8.15.9")
    assert diagnose("Setup: pnpm install --frozen-lockfile", pnpm).startswith("The pnpm in the workstation image")
    npm = ("exited with 1\nnpm WARN read-shrinkwrap This version of npm is compatible with lockfileVersion@1, "
           "but package-lock.json was generated for lockfileVersion@3. I'll try to do my best with it!")
    assert diagnose("Setup: npm ci", npm).startswith("The npm in the workstation image")
    uv = "exited with 2\nerror: Failed to parse `uv.lock`\n  Caused by: unsupported lock version 2"
    assert diagnose("Setup: uv sync --locked", uv).startswith("The uv in the workstation image is older than the project's `uv.lock`")


DOCKER_BUILD = """building workstation from docker/workstation.Dockerfile failed:
Step 1/6 : FROM node:26-bookworm
 ---> 6c2b1f0e2c1a
Step 2/6 : RUN apt-get update && apt-get install -y --no-install-recommends make
 ---> Using cache
 ---> 91d1c8b2a0f3
Step 3/6 : RUN corepack enable
 ---> Running in 4b5e3c1d2a9f
/bin/sh: 1: corepack: not found
The command '/bin/sh -c corepack enable' returned a non-zero code: 127"""


def test_dockerfile_failure_names_the_instruction_and_the_error():
    assert diagnose("Build workstation", DOCKER_BUILD) == (
        "The workstation image failed to build at `RUN corepack enable`: /bin/sh: 1: corepack: not found"
    )


def test_dockerfile_copy_failure():
    log = """building service api from api.Dockerfile failed:
Step 4/5 : COPY requirements.txt /srv/
COPY failed: file not found in build context or excluded by .dockerignore: stat requirements.txt: file does not exist"""
    assert diagnose("Build service api", log) == (
        "The service api image failed to build at `COPY requirements.txt /srv/`: COPY failed: file not found "
        "in build context or excluded by .dockerignore: stat requirements.txt: file does not exist"
    )


def test_dockerfile_failure_adds_a_tool_mismatch():
    log = DOCKER_BUILD.replace("RUN corepack enable", "RUN uv sync --locked").replace(
        "/bin/sh: 1: corepack: not found", SALEOR_UV.split("\n", 1)[1])
    sentence = diagnose("Build workstation", log)
    assert sentence.startswith("The workstation image failed to build at `RUN uv sync --locked`: warning: Failed to parse")
    assert sentence.endswith("install a newer uv in the workstation Dockerfile.")


def test_dockerfile_killed():
    log = DOCKER_BUILD.replace("returned a non-zero code: 127", "returned a non-zero code: 137")
    assert "was killed (exit 137)" in diagnose("Build workstation", log)


def test_service_readiness():
    log = ("service db (postgres:16) did not become ready within 60s waiting for pg_isready -U app "
           "(last: no response)\n2026-11-07 10:00:01 FATAL:  password authentication failed")
    assert diagnose("Start service db", log) == (
        "Service db never became ready within 60s: check its `env` and its `ready` check."
    )
    exited = "service redis (redis:8) exited with code 1 before it was ready\n*** FATAL CONFIG FILE ERROR ***"
    assert diagnose("Start service redis", exited).startswith("Service redis (redis:8) exited with code 1")
    missing = "service db (acme/pg:nope) could not start: manifest for acme/pg:nope not found"
    assert diagnose("Start service db", missing) == "Service db could not start: manifest for acme/pg:nope not found"


@pytest.mark.parametrize(
    ("log", "start"),
    [
        ("git clone --mirror https://github.com/acme/app.git failed: remote: Invalid username or token.\n"
         "fatal: Authentication failed for 'https://github.com/acme/app.git/'", "Git could not authenticate"),
        ("git clone --mirror https://github.com/acme/app.git failed: fatal: could not read Username for "
         "'https://github.com': terminal prompts disabled", "Git could not authenticate"),
        ("git clone --mirror https://github.com/acme/ap.git failed: remote: Repository not found.\n"
         "fatal: repository 'https://github.com/acme/ap.git/' not found", "Repository app was not found"),
    ],
)
def test_mirror_failures(log, start):
    assert diagnose("Fetch mirror app", log).startswith(start)


def test_missing_branch():
    log = ("git clone --local --branch feature /data/mirrors/x/app /data/runs/x/project/app failed: "
           "Cloning into '/data/runs/x/project/app'...\nwarning: Could not find remote branch feature to clone.\n"
           "fatal: Remote branch feature not found in upstream origin")
    assert diagnose("Clone app (feature)", log) == "Repository app has no branch `feature`: check its `branch`."


def test_config_errors_and_keys():
    log = ("config error: notez: unknown key\ncommit 1a2b3c4d5e6f (exradius.yaml on main)\n"
           "error: notez: unknown key\nerror: env.A: must be a string\nerror: x: y\nmissing keys: none")
    assert diagnose("Read config", log) == (
        "The config has errors: notez: unknown key; env.A: must be a string (and 1 more)."
    )
    keys = ("missing secrets A, B in store billing\ncommit 1a2b3c4d5e6f (exradius.yaml on main)\n"
            "missing keys: A, B (store billing)")
    assert diagnose("Read config", keys) == "Missing secrets A, B in store billing."


def test_no_match_is_none():
    assert diagnose("Setup: make check", "exited with 2\nsetup output\nboom") is None
    assert diagnose("Prepare attachments folder", "PermissionError: [Errno 13] Permission denied") is None
    assert diagnose("Build workstation", "") is None


def test_the_first_failed_step_is_diagnosed():
    steps = [
        {"name": "Read config", "state": "passed", "log": "commit abc"},
        {"name": "Setup: corepack enable", "state": "failed", "log": OUTLINE_COREPACK},
        {"name": "Remove directory", "state": "failed", "log": "OSError: busy"},
    ]
    assert diagnose_steps(steps).startswith("The workstation image has no `corepack`")
    assert diagnose_steps([{"name": "Read config", "state": "passed", "log": ""}]) is None
