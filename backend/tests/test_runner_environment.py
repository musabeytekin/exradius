"""The runner is a guest in the project's container (docs/runtime-bundles.md §5): it cleans
its own process, and the agent gets the container's environment plus only the provider
credential (which the container already carries) and ``CLAUDE_CONFIG_DIR``."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from exradius.runner.environment import INTERPRETER_VARIABLES, isolate
from exradius.runtimes import RunSpec, get_runtime

PROJECT_ENV = {
    "PYTHONPATH": "/work/vendor",
    "PYTHONHOME": "/usr/local",
    "PYTHONSTARTUP": "/work/.pythonrc",
    "VIRTUAL_ENV": "/work/.venv",
    "NODE_OPTIONS": "--max-old-space-size=4096",
    "NODE_PATH": "/work/node_modules",
    "LOG_LEVEL": "debug",
    "ANTHROPIC_API_KEY": "sk-provider",  # the provider credential, injected by the worker
}

SDK_OWN = {"CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_SDK_READS_SESSION_STATE", "CLAUDECODE", "PWD"}
"""Markers the Agent SDK itself adds when it starts the CLI (not ours to control)."""


def test_isolate_cleans_only_the_runners_process():
    environ = {**PROJECT_ENV, "PATH": "/usr/local/bin:/usr/bin", "EXRADIUS_RUN_TOKEN": "tok",
               "EXRADIUS_RUNTIME_DIR": "/opt/exradius/runtime"}
    env = isolate(environ)
    # The runner's own process: no interpreter steering, no token, no bundle location.
    assert not set(INTERPRETER_VARIABLES) & set(environ)
    assert "EXRADIUS_RUN_TOKEN" not in environ and "EXRADIUS_RUNTIME_DIR" not in environ
    assert environ["LOG_LEVEL"] == "debug" and environ["PATH"] == "/usr/local/bin:/usr/bin"
    # What the agent gets: the container's environment as received, minus the run token.
    assert env.container == {**PROJECT_ENV, "PATH": "/usr/local/bin:/usr/bin"}
    assert env.run_token == "tok" and env.runtime_dir == Path("/opt/exradius/runtime")


def test_the_kubelets_api_server_variables_do_not_reach_the_agent():
    """The kubelet adds them to every container, enableServiceLinks or not (docs/kubernetes.md §23)."""
    kubelet = {
        "KUBERNETES_SERVICE_HOST": "10.96.0.1", "KUBERNETES_SERVICE_PORT": "443",
        "KUBERNETES_SERVICE_PORT_HTTPS": "443", "KUBERNETES_PORT": "tcp://10.96.0.1:443",
        "KUBERNETES_PORT_443_TCP": "tcp://10.96.0.1:443", "KUBERNETES_PORT_443_TCP_ADDR": "10.96.0.1",
        "KUBERNETES_PORT_443_TCP_PORT": "443", "KUBERNETES_PORT_443_TCP_PROTO": "tcp",
    }
    env = isolate({**PROJECT_ENV, **kubelet, "KUBERNETES_NAMESPACE": "the project's own"})
    assert env.container == {**PROJECT_ENV, "KUBERNETES_NAMESPACE": "the project's own"}


@pytest.fixture
def fake_bundle(tmp_path):
    """A bundle directory whose ``bin/claude`` records the environment it was started with."""
    record = tmp_path / "cli-env.json"
    cli = tmp_path / "bundle" / "bin" / "claude"
    cli.parent.mkdir(parents=True)
    cli.write_text(
        f"#!{sys.executable} -I\n"
        "import json, os, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        "sys.exit(1)\n"
    )
    cli.chmod(0o755)
    return tmp_path / "bundle", record


async def test_agent_environment_is_the_container_environment(fake_bundle, monkeypatch, tmp_path):
    from claude_agent_sdk import ClaudeSDKClient

    bundle, record = fake_bundle
    for key, value in PROJECT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("EXRADIUS_RUN_TOKEN", "tok")
    monkeypatch.setenv("EXRADIUS_RUNTIME_DIR", str(bundle))
    monkeypatch.setenv("EXRADIUS_STATE_DIR", str(tmp_path))

    environment = isolate()  # as the runner does first thing, on the real os.environ
    container = environment.container
    assert "PYTHONPATH" not in os.environ  # the runner's own process is clean

    runtime = get_runtime("claude_code", environment.runtime_dir)
    spec = RunSpec(run_id="r1", working_dir=tmp_path, state_dir=tmp_path, prompt="hi", connections=[], env=container)
    options = runtime.options(spec)
    assert options.cli_path == str(bundle / "bin" / "claude")  # started by path, not via PATH

    client = ClaudeSDKClient(options)
    with pytest.raises(Exception):
        async with asyncio.timeout(20):
            await client.connect()  # the fake CLI exits; it only records its environment
    agent_env = json.loads(record.read_text())

    def without_sdk(env):
        return {key: value for key, value in env.items() if key not in SDK_OWN}

    assert without_sdk(agent_env) == without_sdk({**container, "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")})
    # The project's interpreter settings and the provider credential reach the agent ...
    for key in ("PYTHONPATH", "NODE_OPTIONS", "VIRTUAL_ENV", "ANTHROPIC_API_KEY", "LOG_LEVEL"):
        assert agent_env[key] == PROJECT_ENV[key]
    # ... the runner's token and the bundle do not.
    assert "EXRADIUS_RUN_TOKEN" not in agent_env and "EXRADIUS_RUNTIME_DIR" not in agent_env
    assert str(bundle) not in agent_env.get("PATH", "")
