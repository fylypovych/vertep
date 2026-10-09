"""Isolation guarantees for the laboratory working copy.

Issue #107 fixes the trust boundary: the laboratory works in its own Git
working copy with its own Docker volumes and never writes to the production
Docker socket, containers, database, volumes, secrets or configuration.  This
module turns that boundary into something mechanical instead of prose, so the
acceptance criteria ("a test agent cannot write to production …") can be
proved by tests.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: Paths the laboratory must never touch, whatever the configuration says.
FORBIDDEN_PATHS = (
    "/var/run/docker.sock",
    "/opt/vertep",
    "/data/config",
    "/data/storage",
    "/data/backups",
    "/etc/vertep",
)

#: Host variables that would point the laboratory at production infrastructure.
FORBIDDEN_ENVIRONMENT = (
    "DATABASE_URL", "UPDATE_DATABASE_URL", "REDIS_URL", "CORE_ADDRESS",
    "POSTGRES_PASSWORD", "JWT_SECRET", "WORKER_SECRET", "ADMIN_PASSWORD",
    "DOCKER_HOST", "RUNTIME_SIGNING_PRIVATE_KEY", "VERTEP_ROOT",
)

_MOUNT_LINE = re.compile(r"^\s*-\s*(?P<source>[^:]+):(?P<target>/[^:]+)")
_DOCKER_COMMAND = re.compile(
    r"\bdocker\b(?:\s+-H\s+\S+)?\s+(?:compose\s+)?(?:-\S+\s+)*"
    r"(?:ps|exec|logs|stop|restart|rm|up|down|kill|cp)\b")


class IsolationViolation(RuntimeError):
    """Raised when a configuration or command would break the trust boundary."""


@dataclass(frozen=True)
class IsolationReport:
    isolated: bool
    problems: list[str]

    def as_dict(self) -> dict:
        return {"isolated": self.isolated, "problems": list(self.problems)}


def _resolved(path) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def _overlaps(candidate: str, forbidden: str) -> bool:
    target = _resolved(forbidden)
    resolved = _resolved(candidate)
    return resolved == target or resolved.startswith(target + os.sep)


def check_working_copy(root) -> IsolationReport:
    """Verify that ``root`` is a laboratory copy and not the production tree."""
    if not Path(root).is_dir():
        return IsolationReport(False, [f"working copy does not exist: {root}"])
    resolved = _resolved(root)
    problems = [f"working copy overlaps production path {forbidden}"
                for forbidden in FORBIDDEN_PATHS if _overlaps(resolved, forbidden)]
    production_root = os.getenv("VERTEP_PRODUCTION_ROOT", "")
    if production_root and _resolved(production_root) == resolved:
        problems.append("working copy points at the production repository")
    return IsolationReport(not problems, problems)


def check_environment(environment: dict | None = None) -> IsolationReport:
    """Verify that no production credential or service address is present."""
    environment = os.environ if environment is None else environment
    present = sorted(name for name in FORBIDDEN_ENVIRONMENT
                     if str(environment.get(name, "")).strip())
    return IsolationReport(not present,
                           [f"production variable is set: {name}" for name in present])


def check_command(command) -> IsolationReport:
    """Refuse a command that would drive the production Docker daemon directly."""
    text = " ".join(str(part) for part in command)
    problems = [f"command references production path {forbidden}"
                for forbidden in FORBIDDEN_PATHS if forbidden in text]
    if _DOCKER_COMMAND.search(text):
        problems.append("command would drive the production Docker daemon")
    return IsolationReport(not problems, problems)


def check_compose_mounts(compose_text: str) -> IsolationReport:
    """Verify that a laboratory Compose file mounts no production path."""
    problems: list[str] = []
    for line in compose_text.splitlines():
        match = _MOUNT_LINE.match(line)
        if not match:
            continue
        source = match.group("source").strip().strip("'\"")
        if source.startswith("${") or not source.startswith(("/", ".")):
            continue
        for forbidden in FORBIDDEN_PATHS:
            if _overlaps(source, forbidden):
                problems.append(f"mount {source} reaches production path {forbidden}")
    return IsolationReport(not problems, problems)


class Workspace:
    """The laboratory's own working copy: paths, isolation checks and journals."""

    def __init__(self, root, *, repository_url: str = "", branch: str = "main",
                 environ=None):
        self.root = Path(root)
        self.repository_url = repository_url
        self.branch = branch
        #: Environment the isolation check inspects; ``None`` means the real
        #: process environment (the fail-closed default for the lab VM).
        self.environ = environ

    @property
    def state_dir(self) -> Path:
        return self.root / ".lab"

    @property
    def reports_dir(self) -> Path:
        return self.state_dir / "reports"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "artifacts"

    def prepare(self) -> None:
        for path in (self.state_dir, self.reports_dir, self.artifacts_dir):
            path.mkdir(parents=True, exist_ok=True)

    def verify(self) -> IsolationReport:
        """Fail-closed isolation check used before every autonomous run."""
        working_copy = check_working_copy(self.root)
        environment = check_environment(self.environ)
        problems = working_copy.problems + environment.problems
        return IsolationReport(not problems, problems)

    def require_isolated(self) -> None:
        report = self.verify()
        if not report.isolated:
            raise IsolationViolation("; ".join(report.problems))

    def as_dict(self) -> dict:
        report = self.verify()
        return {"root": str(self.root), "repository_url": self.repository_url,
                "branch": self.branch, "state_dir": str(self.state_dir),
                "isolated": report.isolated, "problems": report.problems}


