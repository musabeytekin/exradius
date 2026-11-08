"""What went wrong in a failed verification, in one or two sentences (docs/setup-and-verify.md §1.3).

A table of plain substring and regex checks over the failed step's name, its command (for
setup steps) and its log tail; the first match wins. No match gives ``None``, and the panel
shows only the step and the tail. No model is asked.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

SETUP = "Setup: "
BUILD = "Build "


@dataclass(frozen=True)
class Failure:
    step: str
    log: str

    @property
    def command(self) -> str | None:
        return self.step[len(SETUP):] if self.step.startswith(SETUP) else None

    @property
    def subject(self) -> str:
        """``Build workstation`` → ``workstation``, ``Build service db`` → ``service db``."""
        return self.step[len(BUILD):] if self.step.startswith(BUILD) else self.step


Rule = Callable[[Failure], str | None]


def _code(text: str) -> str:
    return f"`{text}`"


# Read config ---------------------------------------------------------------------------------


def _config(f: Failure) -> str | None:
    if f.step != "Read config":
        return None
    lines = [line.strip() for line in f.log.splitlines() if line.strip()]
    if not lines:
        return None
    errors = [line.removeprefix("error: ") for line in lines if line.startswith("error: ")]
    if errors:
        more = f" (and {len(errors) - 2} more)" if len(errors) > 2 else ""
        return f"The config has errors: {'; '.join(errors[:2])}{more}."
    return lines[0][0].upper() + lines[0][1:] + "."


# Repositories --------------------------------------------------------------------------------

AUTH = re.compile(
    r"Authentication failed|could not read Username|could not read Password|terminal prompts disabled"
    r"|Permission denied \(publickey|HTTP Basic: Access denied|The requested URL returned error: 40[13]"
    r"|Invalid username or (password|token)",
    re.I,
)
NO_BRANCH = re.compile(r"Remote branch (\S+) not found|couldn't find remote ref (\S+)", re.I)
NO_REPO = re.compile(
    r"Repository not found|does not appear to be a git repository|The requested URL returned error: 404"
    r"|repository '[^']*' not found|does not exist",
    re.I,
)
REPO_STEP = re.compile(r"^(?:Fetch mirror|Clone) (\S+)")


def _clone(f: Failure) -> str | None:
    step = REPO_STEP.match(f.step)
    if not step:
        return None
    repo = step.group(1)
    if AUTH.search(f.log):
        return (f"Git could not authenticate for repository {repo}: check the credential in its `auth` "
                "and the key's value in the store.")
    if branch := NO_BRANCH.search(f.log):
        name = (branch.group(1) or branch.group(2)).strip("'\"")
        return f"Repository {repo} has no branch {_code(name)}: check its `branch`."
    if NO_REPO.search(f.log):
        return (f"Repository {repo} was not found: check its `url` (a private repository that the "
                "credential cannot read is also reported as not found).")
    return None


# Killed --------------------------------------------------------------------------------------

KILLED = re.compile(r"exited with (?:code )?137\b|non-zero code: 137\b|exit code: 137\b")


def _killed(f: Failure) -> str | None:
    if not KILLED.search(f.log):
        return None
    what = _code(f.command) if f.command else f"the {f.subject} step"
    return (f"{what[0].upper()}{what[1:]} was killed (exit 137): it ran out of memory; lower its parallelism "
            "or declare `resources.memory` so the run's pod is placed on a node with room for it.")


DECLARED = re.compile(r"exceeded its declared memory \(([^)]+)\)")
SERVICE_STEP = re.compile(r"^Start service (\S+)")


def _declared_memory(f: Failure) -> str | None:
    """A container killed by its own declared limit (docs/run-environment.md §4)."""
    match = DECLARED.search(f.log)
    if not match:
        return None
    limit = match.group(1)
    if service := SERVICE_STEP.match(f.step):
        return (f"Service {service.group(1)} exceeded its declared memory ({limit}) and was killed: "
                "raise its `memory` in the config.")
    what = _code(f.command) if f.command else f"the {f.subject} step"
    return (f"{what[0].upper()}{what[1:]} exceeded the workstation's declared memory ({limit}) and was killed: "
            "raise `resources.memory` in the config, or lower the command's parallelism.")


# Kubernetes: image pulls and placement (docs/kubernetes.md §13) ----------------------------

PULL = re.compile(r"ErrImagePull|ImagePullBackOff|ImagePullFailed|InvalidImageName|failed to pull|pull access denied"
                  r"|manifest unknown|not found: manifest|failed to resolve reference", re.I)
IMAGE = re.compile(r'\bimage\s+"?([\w.:/@-]*\w)"?', re.I)
UNSCHEDULABLE = re.compile(r"Unschedulable|didn't match|Insufficient (memory|cpu)|nodes are available", re.I)


def _image_pull(f: Failure) -> str | None:
    if not PULL.search(f.log):
        return None
    image = IMAGE.search(f.log)
    what = f"The image {_code(image.group(1))}" if image else "An image of the run's pod"
    return (f"{what} could not be pulled: check `workstation.image` (name, tag, registry) and, for a private "
            "registry, the install's `runs.imagePullSecrets`.")


def _unschedulable(f: Failure) -> str | None:
    if not UNSCHEDULABLE.search(f.log) or "waiting for cluster capacity" not in f.log:
        return None
    if memory := re.search(r"Insufficient (memory|cpu)", f.log, re.I):
        return (f"No node has enough {memory.group(1).lower()} for the run's pod: lower `resources.memory`, or add "
                "a node with more room to the cluster.")
    return "No node can take the run's pod: check the install's `runs.nodeSelector` and `runs.tolerations`."


# Services ------------------------------------------------------------------------------------

SERVICE = re.compile(r"^service (\S+) \(([^)]*)\) (.*)$", re.M)


def _service(f: Failure) -> str | None:
    if not f.step.startswith("Start service "):
        return None
    match = SERVICE.search(f.log)
    if not match:
        return None
    name, image, rest = match.groups()
    if rest.startswith("did not become ready"):
        within = re.search(r"within (\d+s)", rest)
        return (f"Service {name} never became ready{' within ' + within.group(1) if within else ''}: "
                "check its `env` and its `ready` check.")
    if exited := re.match(r"exited with code (\S+) before it was ready", rest):
        return (f"Service {name} ({image}) exited with code {exited.group(1)} before it was ready: "
                "its log tail shows why; check its `env`.")
    if rest.startswith("could not start"):
        return f"Service {name} could not start: {rest.removeprefix('could not start: ')}"
    return None


# Tools in the image older than the project needs -------------------------------------------------

TOOL_MISMATCHES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"exclude-newer[\s\S]{0,400}?(failed to parse|parse error)|"
                   r"(TOML parse error|failed to parse)[\s\S]{0,400}?exclude-newer", re.I),
        "The uv in the workstation image is older than the project's uv settings "
        "(it cannot parse `exclude-newer`): install a newer uv in the workstation Dockerfile.",
    ),
    (
        re.compile(r"unsupported lock(file)? version|lock(file)? version \S+ is not supported|"
                   r"uv\.lock[\s\S]{0,200}?(unknown|unsupported) (lock )?(schema|version)", re.I),
        "The uv in the workstation image is older than the project's `uv.lock` needs: "
        "install a newer uv in the workstation Dockerfile.",
    ),
    (
        re.compile(r"\"?packageManager\"?[\s\S]{0,300}?(However|current global version|corepack)|"
                   r"This project is configured to use yarn", re.I),
        "The yarn in the workstation image does not match the project's `packageManager`: "
        "enable corepack in the workstation Dockerfile (`npm install -g corepack && corepack enable`).",
    ),
    (
        re.compile(r"ERR_PNPM_BAD_PM_VERSION|ERR_PNPM_LOCKFILE_BREAKING_CHANGE|"
                   r"Ignoring not compatible lockfile|ERR_PNPM_UNSUPPORTED_ENGINE"),
        "The pnpm in the workstation image is older than the project's lockfile needs: "
        "install the pnpm version of `packageManager` in the workstation Dockerfile.",
    ),
    (
        re.compile(r"compatible with lockfileVersion@\d+, but [\s\S]{0,80}?lockfileVersion@\d+|"
                   r"old lockfile|unsupported lockfileVersion", re.I),
        "The npm in the workstation image is older than the project's `lockfileVersion` needs: "
        "use a newer node image or install a newer npm in the workstation Dockerfile.",
    ),
]


def _tool_mismatch(log: str) -> str | None:
    return next((sentence for pattern, sentence in TOOL_MISMATCHES if pattern.search(log)), None)


# A missing executable in a setup command ----------------------------------------------------------

MISSING = [
    re.compile(r"(?:^|\n)\S*sh: (?:\d+: |line \d+: )?(\S+): (?:command )?not found"),
    re.compile(r"(?:^|\n|\s)(\S+): command not found"),
    re.compile(r"(?:^|\n)\S*env: ['‘\"]?([^'’\"\s:]+)['’\"]?: No such file or directory"),
    re.compile(r"(?:^|\n)\S*sh: (?:\d+: |line \d+: )?(\S+): No such file or directory"),
    re.compile(r"exec: \"?([^\"\s]+)\"?: executable file not found in \$PATH"),
]

TOOL_HINTS = {
    "corepack": "node images from 25 on no longer ship it; add `RUN npm install -g corepack` to the "
                "workstation Dockerfile",
    "python3": "install python3 in the workstation Dockerfile, or link the image's interpreter to "
               "`/usr/local/bin/python3`",
    "python": "install python in the workstation Dockerfile, or link the image's `python3` to "
              "`/usr/local/bin/python`",
}


def _missing_tool(f: Failure) -> str | None:
    if f.command is None:
        return None
    for pattern in MISSING:
        if match := pattern.search(f.log):
            tool = match.group(1).strip("'\"")
            hint = TOOL_HINTS.get(tool.rsplit("/", 1)[-1])
            if tool.startswith(("./", "/")) or "/" in tool:
                return (f"{_code(tool)} does not exist in the workstation when {_code(f.command)} runs: "
                        "check the path, or that the file is committed and executable.")
            return (f"The workstation image has no {_code(tool)}: "
                    + (hint if hint else "install it in the workstation Dockerfile or use an image that has it")
                    + ".")
    return None


def _setup_mismatch(f: Failure) -> str | None:
    return _tool_mismatch(f.log) if f.command is not None else None


# Dockerfile builds ---------------------------------------------------------------------------------

INSTRUCTION = re.compile(
    r"^(?:Step \d+/\d+ : |#\d+ \[[^\]]*\] )?((?:RUN|COPY|ADD)\s(?!failed:).*)$", re.M | re.I
)
ERROR_LINE = re.compile(
    r"error|not found|No such file|failed|cannot|could not|denied|returned a non-zero code", re.I
)
NOISE = re.compile(r"^\s*(--->|Removing intermediate|#\d+ (sha256|DONE|CACHED)|\s*$)")


def _build(f: Failure) -> str | None:
    if not f.step.startswith(BUILD):
        return None
    lines = f.log.splitlines()
    last = None
    for index, line in enumerate(lines):
        if INSTRUCTION.match(line.strip()):
            last = index
    if last is None:
        error = next((line.strip() for line in reversed(lines) if ERROR_LINE.search(line)), None)
        return f"The {f.subject} image failed to build: {error}" if error else None
    instruction = INSTRUCTION.match(lines[last].strip()).group(1).strip()
    after = [line.strip() for line in lines[last + 1:] if not NOISE.match(line)]
    error = next((line for line in after if ERROR_LINE.search(line)), after[-1] if after else None)
    sentence = f"The {f.subject} image failed to build at {_code(instruction)}" + (f": {error}" if error else ".")
    mismatch = _tool_mismatch("\n".join(lines[last:]))
    return f"{sentence} {mismatch}" if mismatch else sentence


RULES: list[Rule] = [_config, _image_pull, _unschedulable, _clone, _declared_memory, _killed, _service, _build,
                     _setup_mismatch, _missing_tool]


def diagnose(step: str, log: str) -> str | None:
    """The diagnosis of a failed step, or ``None`` when no pattern matches."""
    failure = Failure(step, log or "")
    for rule in RULES:
        if (sentence := rule(failure)) is not None:
            return sentence
    return None


def diagnose_steps(steps: list[dict]) -> str | None:
    """The diagnosis of the first failed step of a verification."""
    failed = next((s for s in steps if s.get("state") == "failed"), None)
    return diagnose(failed["name"], failed.get("log", "")) if failed else None
