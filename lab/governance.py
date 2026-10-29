"""GitHub governance for the autonomous laboratory (Issue #107, §"GitHub governance").

The laboratory may create **only new ``lab`` issues**, and only through this
module, which sits behind the policy gate.  Concretely:

* a finding is validated first (confirmed defect/regression/security/non-conformance
  with evidence, reproduction, risk, expected result, boundaries and a link to an
  already approved requirement) — ``lab`` is not a channel for product ideas;
* duplicates are refused before anything is published;
* ``i``/``ir``/``var`` writes, edits of foreign issues, closing, pushing, branches
  and PRs are refused by the gate;
* every attempt — allowed or denied — is audited, and a retry never duplicates an
  issue because the decision key is persisted next to the created issue.

The GitHub call itself is injected (:class:`IssueTransport`), so tests prove the
policy without a token and the production transport stays a thin ``gh`` wrapper.
"""

import json
import hashlib
from functools import wraps
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .policy import (Action, PolicyDenied, PolicyGate, reserve_identifier,
                     validate_lab_finding)
from .state_store import append_jsonl, read_json, read_jsonl, write_json
from .state_store import exclusive


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with exclusive(self.state_dir / ".governance.lock"):
            return method(self, *args, **kwargs)
    return call

_ISSUER = {"i": Action.CREATE_I_ISSUE, "ir": Action.CREATE_IR_ISSUE,
           "var": Action.CREATE_VAR_ISSUE}
_IDENTIFIER = {kind: re.compile(rf"^{kind}\.\d+\.\d+\.\d+\.\d+$")
               for kind in _ISSUER}


def issue_scope(kind, title, identifier="", metadata=None):
    """Owner-review digest of an implementation/proposal creation request."""
    return hashlib.sha256(json.dumps({"kind": kind, "title": str(title).strip(),
        "identifier": str(identifier).strip(), "metadata": metadata or {}},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
#: Reviewer identities that cannot count as an independent review.
_SELF_REVIEWERS = frozenset({"lab", "self", "author", "автор", "me", "same"})


class GovernanceError(RuntimeError):
    """A governance rule refused the operation."""


class RemoteRejected(GovernanceError):
    """The remote server explicitly refused a request without creating an effect."""


@dataclass
class IssueTransport:
    """Minimal GitHub surface the laboratory is allowed to use."""

    dry_run: bool = True

    def create_issue(self, title: str, body: str, labels=()) -> dict:
        if self.dry_run:
            return {"number": None, "url": "", "dry_run": True, "title": title}
        raise NotImplementedError

    def find_duplicates(self, title: str) -> list:
        if self.dry_run:
            return []
        raise NotImplementedError

    def get_issue(self, number):
        raise GovernanceError("Remote read-back is required before acceptance/transfer")

    def identifiers(self):
        return []

    def close_issue(self, issue, comment: str = "") -> dict:
        """Close an issue — reachable only through a delegated, gated decision."""
        if self.dry_run:
            return {"number": issue, "state": "closed", "dry_run": True}
        raise NotImplementedError


class GhIssueTransport(IssueTransport):
    """Production transport: ``gh`` with a minimal-permission token."""

    def __init__(self, repository: str, *, token: str | None = None, dry_run: bool = True):
        super().__init__(dry_run=dry_run)
        self.repository = repository
        self.token = token if token is not None else os.getenv("LAB_GITHUB_TOKEN", "")

    def _gh(self, *arguments: str) -> str:
        environment = dict(os.environ)
        if self.token:
            environment["GH_TOKEN"] = self.token
        completed = subprocess.run(["gh", *arguments], capture_output=True, text=True,
                                   env=environment, timeout=60)
        if completed.returncode != 0:
            raise GovernanceError(completed.stderr.strip()[:500] or "gh failed")
        return completed.stdout

    def create_issue(self, title: str, body: str, labels=()) -> dict:
        if self.dry_run:
            return {"number": None, "url": "", "dry_run": True, "title": title}
        command = ["issue", "create", "--repo", self.repository, "--title", title,
                   "--body", body]
        for label in labels:
            command.extend(["--label", str(label)])
        url = self._gh(*command).strip()
        return {"number": None, "url": url, "dry_run": False, "title": title}

    def find_duplicates(self, title: str) -> list:
        if self.dry_run:
            return []
        output = self._gh("issue", "list", "--repo", self.repository, "--state", "all",
                          "--search", title, "--json", "number,title,body,state", "--limit", "100")
        try:
            return json.loads(output or "[]")
        except ValueError:
            return []

    def get_issue(self, number):
        return json.loads(self._gh("issue", "view", str(number), "--repo", self.repository, "--json", "number,title,body,state"))

    def _request(self, method, path, payload=None):
        args = ["api", f"repos/{self.repository}{path}", "--method", method]
        if payload is None:
            return json.loads(self._gh(*args))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8", delete=False) as stream:
            json.dump(payload, stream, ensure_ascii=False)
            name = stream.name
        try:
            return json.loads(self._gh(*args, "--input", name))
        finally:
            Path(name).unlink(missing_ok=True)

    def identifiers(self):
        if self.dry_run:
            return []
        output = self._gh("api", "--paginate", f"repos/{self.repository}/issues?state=all&per_page=100", "--jq", '.[] | (.title + " " + (.body // ""))')
        return re.findall(r"(?:i|ir|ri|rt|lab|var)\.\d+\.\d+\.\d+\.\d+", output)

    def close_issue(self, issue, comment: str = "") -> dict:
        if self.dry_run:
            return {"number": issue, "state": "closed", "dry_run": True}
        return GitHubApiTransport.close_issue(self, issue, comment)


class GitHubApiTransport(IssueTransport):
    """Transport over the GitHub REST API with a minimal-permission token.

    The laboratory talks to a GitHub HTTP endpoint, so the base URL is
    configurable (``LAB_GITHUB_API_URL``): the production API by default and a
    controlled endpoint in the acceptance environment (Issue i.0.0.2.50, L6).
    Every call is a real request — the answer is parsed, a refusal becomes an
    error and the token travels only in the ``Authorization`` header.
    """

    API_VERSION = "2022-11-28"
    USER_AGENT = "vertep-lab"

    def __init__(self, repository: str, *, token: str = "", base_url: str = "",
                 dry_run: bool = False, timeout: float = 30.0, opener=None):
        super().__init__(dry_run=dry_run)
        self.repository = str(repository or "").strip()
        self.token = str(token or "")
        self.base_url = (str(base_url or "").strip() or "https://api.github.com").rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    # -- plumbing --------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None):
        url = f"{self.base_url}/repos/{self.repository}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": self.API_VERSION,
                   "User-Agent": self.USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read()[:200].decode("utf-8", "replace").strip()
            except Exception:  # pragma: no cover - the body is optional
                detail = ""
            error_class = RemoteRejected if 400 <= error.code < 500 and error.code != 408 else GovernanceError
            raise error_class(
                f"github refused {method} {path}: HTTP {error.code} {detail}".strip()) from error
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise GovernanceError(f"github transport unreachable: {error}") from error
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise GovernanceError("github returned a non-JSON answer") from error

    # -- issues ----------------------------------------------------------
    def create_issue(self, title: str, body: str, labels=()) -> dict:
        if self.dry_run:
            return {"number": None, "url": "", "dry_run": True, "title": title}
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = [str(label) for label in labels]
        data = self._request("POST", "/issues", payload)
        if not isinstance(data, dict) or data.get("number") is None:
            raise GovernanceError("github accepted the request but returned no issue number")
        return {"number": int(data["number"]),
                "url": str(data.get("html_url") or ""),
                "dry_run": False, "title": title}

    def find_duplicates(self, title: str) -> list:
        if self.dry_run:
            return []
        needle = str(title or "").strip().lower()
        if not needle:
            return []
        data = self._request("GET", "/issues?state=all&per_page=100")
        items = data if isinstance(data, list) else (data.get("data") or [])
        found: list[dict] = []
        for item in items:
            if not isinstance(item, dict) or item.get("pull_request"):
                continue
            candidate = str(item.get("title", "")).strip().lower()
            if candidate and (candidate == needle or needle in candidate
                              or candidate in needle):
                found.append({"number": item.get("number"),
                              "body": item.get("body", ""), "state": item.get("state", ""),
                              "title": item.get("title")})
        return found

    def get_issue(self, number):
        return self._request("GET", f"/issues/{int(number)}")

    def identifiers(self):
        if self.dry_run:
            return []
        result = []
        page = 1
        while True:
            data = self._request("GET", f"/issues?state=all&per_page=100&page={page}")
            if not isinstance(data, list):
                raise GovernanceError("Invalid remote issue inventory")
            for item in data:
                if not item.get("pull_request"):
                    result.extend(re.findall(r"(?:i|ir|ri|rt|lab|var)\.\d+\.\d+\.\d+\.\d+", str(item.get("title", "")) + " " + str(item.get("body", ""))))
            if len(data) < 100:
                return result
            page += 1

    def close_issue(self, issue, comment: str = "") -> dict:
        if self.dry_run:
            return {"number": issue, "state": "closed", "dry_run": True}
        try:
            number = int(issue)
        except (TypeError, ValueError) as error:
            raise GovernanceError(
                f"closing an issue needs its GitHub number, got {issue!r}") from error
        remote = self.get_issue(number)
        if not isinstance(remote, dict) or remote.get("number") != number:
            raise GovernanceError("Remote close target is unconfirmed")
        if str(remote.get("state", "")).lower() == "closed":
            return {"number": number, "state": "closed", "dry_run": False, "already": True}
        if comment:
            marker = f"<!-- vertep-lab-close:{hashlib.sha256((str(number) + comment).encode()).hexdigest()} -->"
            comments = self._request("GET", f"/issues/{number}/comments?per_page=100")
            if not isinstance(comments, list):
                raise GovernanceError("Remote close comment history is unconfirmed")
            if not any(marker in str(item.get("body", "")) for item in comments):
                self._request("POST", f"/issues/{number}/comments", {"body": comment + "\n\n" + marker})
        data = self._request("PATCH", f"/issues/{number}", {"state": "closed"})
        state = str(data.get("state", "closed")).lower() if isinstance(data, dict) else "closed"
        return {"number": number, "state": state, "dry_run": False}


def default_transport() -> IssueTransport:
    """The transport the laboratory is allowed to use.

    A real transport needs an explicit token *and* repository; without them the
    laboratory stays in dry-run, and every answer says so (``dry_run``), so a
    run can never look published while nothing was published.
    """
    token = os.getenv("LAB_GITHUB_TOKEN", "").strip()
    repository = os.getenv("LAB_GITHUB_REPOSITORY", "").strip()
    if token and repository:
        return GitHubApiTransport(repository, token=token,
                                  base_url=os.getenv("LAB_GITHUB_API_URL", "").strip())
    return IssueTransport(dry_run=True)


class Governance:
    """The only channel through which the laboratory touches GitHub issues."""

    def __init__(self, gate: PolicyGate, transport: IssueTransport, *, state_dir,
                 actor: str = "lab", repository: str = ""):
        self.gate = gate
        self.transport = transport
        self.actor = actor
        self.repository = repository
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _created_path(self) -> Path:
        return self.state_dir / "created-issues.json"

    def created(self) -> list:
        payload = read_json(self._created_path, default=[]) or []
        return payload if isinstance(payload, list) else []

    def _already_created(self, key: str) -> dict | None:
        for record in self.created():
            if record.get("key") == key:
                return record
        return None

    def known_identifiers(self) -> set:
        """Identifiers already held by an issue, so a number is never reused."""
        taken: set = set()
        for value in self.transport.identifiers():
            taken.add(re.sub(r"^(?:ri|rt)\.", "ir.", value))
        for intent in (read_json(self.state_dir / "issue-intents.json", {}) or {}).values():
            if intent.get("identifier"):
                taken.add(intent["identifier"])
        for record in self.created():
            for source in (record, record.get("finding") or {},
                           record.get("issue") or {}, record.get("metadata") or {}):
                value = source.get("identifier") if isinstance(source, dict) else ""
                if value:
                    taken.add(str(value))
        return taken

    def _create_effect(self, key, title, body, labels, identifier):
        path = self.state_dir / "issue-intents.json"
        intents = read_json(path, {}) or {}
        full_title = f"{identifier} — {title}"
        content_hash = hashlib.sha256(json.dumps({"title": full_title, "body": body, "labels": labels}, sort_keys=True).encode()).hexdigest()
        marker = f"<!-- vertep-lab-operation:{hashlib.sha256((key + content_hash).encode()).hexdigest()} -->"
        complete_body = body + "\n\n" + marker
        expected_hash = hashlib.sha256(complete_body.encode()).hexdigest()
        prior = intents.get(key)
        if prior and prior["content_hash"] != content_hash:
            raise GovernanceError("Approval/idempotency key reused for a different scope")
        duplicates = self.transport.find_duplicates(title)
        if prior:
            for issue in duplicates:
                if issue.get("title") == full_title and marker in issue.get("body", ""):
                    if hashlib.sha256(issue["body"].encode()).hexdigest() != expected_hash:
                        raise GovernanceError("Remote issue body changed during recovery")
                    return {**issue, "expected_body_sha256": expected_hash}
            if prior.get("state") in {"sending", "uncertain"}:
                raise GovernanceError("Remote create outcome is unconfirmed; source remains open")
        if duplicates:
            return None
        intents[key] = {"identifier": identifier, "content_hash": content_hash, "state": "sending"}
        write_json(path, intents)
        try:
            created = self.transport.create_issue(full_title, complete_body, labels=labels)
            return {**created, "expected_body_sha256": expected_hash}
        except BaseException as error:
            intents[key]["state"] = "reserved" if isinstance(error, RemoteRejected) else "uncertain"
            write_json(path, intents)
            raise

    def _record_by_identifier(self, identifier: str) -> dict | None:
        for record in self.created():
            if str(record.get("identifier", "")) == identifier:
                return record
        return None

    @property
    def _evidence_path(self) -> Path:
        return self.state_dir / "ir-evidence.jsonl"

    def ir_evidence(self) -> list:
        """Every submitted ``ir`` evidence record, oldest first (Issue #251)."""
        return [item for item in read_jsonl(self._evidence_path)
                if isinstance(item, dict)]

    @property
    def _closed_path(self) -> Path:
        return self.state_dir / "closed-issues.json"

    def closed(self) -> dict:
        payload = read_json(self._closed_path, default={}) or {}
        return payload if isinstance(payload, dict) else {}

    # -- findings --------------------------------------------------------
    @serialized
    def propose_lab_issue(self, finding: dict, *, key: str = "") -> dict:
        """Validate and publish a ``lab`` finding; every other write is refused."""
        from .policy import reserve_identifier
        validation = validate_lab_finding(finding)
        decision_key = key or str(finding.get("identifier") or "")
        decision_key = decision_key or str(finding.get("title", ""))[:80]

        existing = self._already_created(decision_key)
        if existing is not None:
            if existing.get("finding") != finding:
                raise GovernanceError("Idempotency key reused for a different issue scope")
            # A retry or restart must never duplicate an issue.
            self.gate.trail.append(actor=self.actor, action="create_lab_issue",
                                   subject=decision_key, decision="DUPLICATE",
                                   rule="idempotent_retry",
                                   reason=str(existing.get("issue", "")))
            return {"created": False, "reason": "already created",
                    "issue": existing.get("issue", existing), "record": existing}

        if not validation["valid"]:
            self.gate.trail.append(actor=self.actor, action="create_lab_issue",
                                   subject=decision_key, decision="REJECTED",
                                   rule="finding_validation",
                                   reason="; ".join(validation["problems"]))
            raise GovernanceError("finding is not a confirmed defect: "
                                  + "; ".join(validation["problems"]))

        try:
            self.gate.require(Action.CREATE_LAB_ISSUE, actor=self.actor,
                              subject=decision_key)
        except PolicyDenied as error:
            return {"created": False, "reason": str(error),
                    "decision": error.decision.as_dict()}

        existing_numbers = list(self.known_identifiers())
        for rec in self.created():
            for candidate in (rec.get("lab_id"), rec.get("identifier"),
                              rec.get("key"),
                              (rec.get("finding") or {}).get("identifier")):
                if candidate:
                    existing_numbers.append(str(candidate))
        # Reserve the next lab identifier from the shared continuous series
        # (§4.2).  The decision key stays stable so a retry resolves to the
        # already created record instead of minting a second issue.
        try:
            lab_id = reserve_identifier("lab", existing_numbers)
        except Exception:
            lab_id = decision_key

        pending = (read_json(self.state_dir / "issue-intents.json", {}) or {}).get(decision_key)
        if pending:
            lab_id = pending["identifier"]
        issue = self._create_effect(decision_key, str(finding.get("title", "")),
                                   render_lab_body(finding) + f"\n\n{lab_id}", ("lab",), lab_id)
        if issue is None:
            return {"created": False, "reason": "duplicate finding"}
        record = {"key": decision_key,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "actor": self.actor, "finding": finding, "issue": issue,
                  "lab_id": lab_id}
        write_json(self._created_path, [*self.created(), record])
        self.gate.trail.append(actor=self.actor, action="create_lab_issue",
                               subject=decision_key, decision="CREATED",
                               rule="owner_grant",
                               reason=str(issue.get("url") or "dry-run"))
        append_jsonl(self.state_dir / "governance.jsonl",
                     {"at": record["created_at"], "action": "create_lab_issue",
                      "subject": decision_key, "issue": issue})
        return {"created": True, "issue": issue, "record": record}

    # -- refused surfaces ------------------------------------------------
    def _refuse_or_report(self, action: Action, subject: str) -> dict:
        decision = self.gate.decide(action, actor=self.actor, subject=subject)
        return {"allowed": decision.allowed, "reason": decision.reason,
                "rule": decision.rule}

    def edit_issue(self, issue: str, body: str = "") -> dict:
        """Editing any issue — including a ``lab`` one — is never autonomous."""
        return self._refuse_or_report(Action.EDIT_ISSUE, issue)

    def submit_ir_evidence(self, issue: str, evidence: dict) -> dict:
        """Record and validate evidence for a candidate ``ir`` close.

        Evidence must be a *real, complete* proof (Issue i.0.0.2.50, L6):
        version/commit, environment, expected vs actual, an **independent**
        reviewer and machine-readable test results that are fully green —
        ``passed > 0``, ``failed == 0``, ``skipped == 0``.  A partial, skipped
        or self-reviewed result is rejected here, before any close is even
        considered.
        """
        required = ("version", "commit", "environment", "expected", "actual",
                    "reviewer")
        if not isinstance(evidence, dict):
            raise GovernanceError("evidence must be an object")
        problems: list[str] = []
        from .evidence import verify
        problems.extend(verify(evidence, self.actor, issue, self.state_dir))
        missing = [key for key in required if not str(evidence.get(key, "")).strip()]
        if missing:
            problems.append("missing " + ", ".join(missing))
        reviewer = str(evidence.get("reviewer", "")).strip()
        if reviewer:
            if reviewer.lower() in _SELF_REVIEWERS or reviewer == str(self.actor):
                problems.append("reviewer must be an independent party, not the author")
        test_results = evidence.get("test_results")
        if not isinstance(test_results, dict):
            problems.append("test_results must report passed, failed and skipped")
        else:
            try:
                if any(type(test_results.get(key)) is not int for key in ("passed", "failed", "skipped")):
                    raise ValueError("integer counters required")
                passed = test_results["passed"]
                failed = test_results["failed"]
                skipped = test_results["skipped"]
            except (TypeError, ValueError):
                passed = failed = skipped = -1
                problems.append("test_results must use integer counters")
            if passed < 1:
                problems.append(f"evidence is incomplete: {passed} test(s) passed")
            if failed != 0:
                problems.append(f"evidence is incomplete: {failed} test(s) failed")
            if skipped != 0:
                problems.append(f"evidence is incomplete: {skipped} test(s) skipped")
        if problems:
            self.gate.trail.append(actor=self.actor, action="submit_ir_evidence",
                                  subject=issue, decision="REJECTED",
                                  rule="incomplete_evidence",
                                  reason="; ".join(problems))
            return {"accepted": False, "missing": missing, "problems": problems}
        record = {"issue": issue, "evidence": evidence,
                  "submitted_at": datetime.now(timezone.utc).isoformat(),
                  "actor": self.actor}
        append_jsonl(self._evidence_path, record)
        self.gate.trail.append(actor=self.actor, action="submit_ir_evidence",
                              subject=issue, decision="RECORDED",
                              rule="evidence_submitted", reason="evidence recorded")
        return {"accepted": True, "record": record}

    @serialized
    def delegate_close_ir(self, issue: str, evidence: dict | None = None) -> dict:
        """Owner-delegated close of an ``ir`` after complete, independent proof.

        The gate stays the authority and the close really happens: without a
        matching ``delegate_close_ir`` grant, or with incomplete/self-reviewed
        evidence, the transport is never touched.  A repeated decision is
        idempotent, so a retry after a crash never closes an issue twice.
        """
        if evidence is None:
            self.gate.trail.append(actor=self.actor, action="delegate_close_ir",
                                   subject=issue, decision="REJECTED",
                                   rule="incomplete_evidence",
                                   reason="real evidence is required for an ir close")
            return {"allowed": False, "reason": "real evidence is required for an "
                                                "ir close",
                    "rule": "incomplete_evidence"}
        verdict = self.submit_ir_evidence(issue, evidence)
        if not verdict.get("accepted"):
            return {"allowed": False, "reason": "incomplete evidence",
                    "rule": "incomplete_evidence",
                    "missing": verdict.get("missing", []),
                    "problems": verdict.get("problems", [])}
        prior = self.closed().get(str(issue))
        if isinstance(prior, dict):
            return {"allowed": True, "closed": True, "already": True,
                    "issue": issue, "record": prior}
        try:
            self.gate.require(Action.DELEGATE_CLOSE_IR, actor=self.actor, subject=issue)
        except PolicyDenied as error:
            return {"allowed": False, "reason": str(error),
                    "rule": error.decision.rule}
        if self.transport.dry_run:
            return {"allowed": True, "closed": False, "rule": "dry_run_not_real_acceptance"}
        recorded_target = (self._record_by_identifier(issue) or {}).get("issue", {}).get("number")
        target = evidence.get("issue_number") or recorded_target
        if recorded_target and str(target) != str(recorded_target):
            return {"allowed": False, "closed": False, "rule": "mismatched_remote_target"}
        target = target or (int(issue) if str(issue).isdigit() else None)
        if not target:
            return {"allowed": False, "closed": False, "rule": "unconfirmed_remote_target"}
        comment = ("Закрито за делегованим рішенням власника: повний доказ "
                   f"({evidence.get('version')} / {evidence.get('commit')}), "
                   "тести пройдено, незалежний review "
                   f"({evidence.get('reviewer')}).")
        try:
            close = self.transport.close_issue(target, comment=comment)
            if close.get("state") != "closed" or close.get("dry_run"):
                raise GovernanceError("Remote close was not confirmed")
        except Exception as error:  # fail closed: the issue simply stays open
            self.gate.trail.append(actor=self.actor, action="delegate_close_ir",
                                   subject=issue, decision="FAILED",
                                   rule="transport_error", reason=str(error))
            return {"allowed": True, "closed": False, "issue": issue,
                    "reason": f"close failed: {error}; source stays open"}
        record = {"issue": issue, "closed_at": datetime.now(timezone.utc).isoformat(),
                  "actor": self.actor, "reviewer": evidence.get("reviewer"),
                  "close": close, "comment": comment}
        payload = self.closed()
        payload[str(issue)] = record
        write_json(self._closed_path, payload)
        self.gate.trail.append(actor=self.actor, action="delegate_close_ir:effect",
                               subject=issue, decision="CLOSED", rule="owner_grant",
                               reason=str(close))
        append_jsonl(self.state_dir / "governance.jsonl",
                     {"at": record["closed_at"], "action": "delegate_close_ir",
                      "subject": issue, "close": close})
        closed_state = str(close.get("state", "closed")).lower() == "closed"
        return {"allowed": True, "closed": closed_state, "issue": issue,
                "close": close, "record": record}

    @serialized
    def create_issue(self, kind: str, title: str, *, identifier: str = "",
                     key: str = "", metadata: dict | None = None) -> dict:
        """Create exactly one approved ``i``/``ir``/``var`` issue, once.

        The sequence is fixed (Issue i.0.0.2.50, L5): idempotency check →
        duplicate check → numeric reservation → gate authorisation → transport
        effect → durable record.  A denial returns *before* the transport is
        touched, so a refused request never leaves an issue behind, and an
        approved request creates **exactly one** issue for that approval.
        """
        if kind not in _ISSUER:
            raise GovernanceError(f"laboratory may not create a {kind!r} issue")
        title = str(title).strip()
        if not title:
            raise GovernanceError("an issue title is required")
        identifier = str(identifier or "").strip()
        subject = identifier or title
        decision_key = str(key or "").strip() or subject
        scope = issue_scope(kind, title, identifier, metadata) if kind in {"i", "var"} else None

        existing = self._already_created(decision_key)
        if existing is not None:
            if (existing.get("title") != title or existing.get("kind") != kind or
                    (identifier and existing.get("identifier") != identifier) or
                    (existing.get("metadata") or {}) != (metadata or {})):
                raise GovernanceError("Idempotency key reused for a different issue scope")
            self.gate.trail.append(actor=self.actor, action=f"create_{kind}_issue",
                                   subject=subject, decision="DUPLICATE",
                                   rule="idempotent_retry",
                                   reason=str(existing.get("identifier", "")))
            return {"allowed": True, "created": False, "reason": "already created",
                    "identifier": existing.get("identifier", ""),
                    "issue": existing.get("issue", existing), "record": existing}

        try:
            self.gate.require(_ISSUER[kind], actor=self.actor, subject=subject, scope=scope)
        except PolicyDenied as error:
            return {"allowed": False, "reason": str(error), "rule": error.decision.rule,
                    "approval_scope": scope}

        taken = self.known_identifiers()
        pending = (read_json(self.state_dir / "issue-intents.json", {}) or {}).get(decision_key)
        if pending and not identifier:
            identifier = pending["identifier"]
        if identifier:
            if not _IDENTIFIER[kind].match(identifier):
                raise GovernanceError(f"identifier must look like {kind}.A.B.C.D")
            if identifier in taken and not (pending and pending["identifier"] == identifier):
                self.gate.trail.append(actor=self.actor,
                                       action=f"create_{kind}_issue", subject=subject,
                                       decision="REJECTED", rule="identifier_used",
                                       reason=identifier)
                return {"allowed": True, "created": False,
                        "reason": f"identifier {identifier} is already used"}
        else:
            identifier = reserve_identifier(kind, taken)

        try:
            self.gate.require(_ISSUER[kind], actor=self.actor, subject=subject, scope=scope)
        except PolicyDenied as error:
            return {"allowed": False, "reason": str(error),
                    "rule": error.decision.rule}

        issue = self._create_effect(decision_key, title, render_issue_body(kind, title, identifier, metadata), (kind,), identifier)
        if issue is None:
            return {"allowed": True, "created": False, "reason": "duplicate issue"}
        record = {"key": decision_key, "identifier": identifier, "kind": kind,
                  "title": title,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "actor": self.actor, "issue": issue}
        if metadata:
            record["metadata"] = dict(metadata)
        write_json(self._created_path, [*self.created(), record])
        self.gate.trail.append(actor=self.actor, action=f"create_{kind}_issue:effect",
                               subject=subject, decision="CREATED",
                               rule="owner_grant",
                               reason=str(issue.get("url") or "dry-run"))
        append_jsonl(self.state_dir / "governance.jsonl",
                     {"at": record["created_at"], "action": f"create_{kind}_issue",
                      "subject": subject, "identifier": identifier, "issue": issue})
        return {"allowed": True, "created": True, "identifier": identifier,
                "issue": issue, "record": record}

    def create_i_issue(self, title: str, **kwargs) -> dict:
        return self.create_issue("i", title, **kwargs)

    def create_ir_issue(self, title: str, **kwargs) -> dict:
        return self.create_issue("ir", title, **kwargs)

    def create_var_issue(self, title: str, **kwargs) -> dict:
        return self.create_issue("var", title, **kwargs)

    # -- consolidation (AGENTS.md §4.7 order, no loss) --------------------
    @property
    def _transfers_path(self) -> Path:
        return self.state_dir / "transfers.json"

    def transfers(self) -> dict:
        payload = read_json(self._transfers_path, default={}) or {}
        return payload if isinstance(payload, dict) else {}

    @serialized
    def consolidate_ir(self, source: dict, successors: list, *,
                       reason: str = "") -> dict:
        """Transfer the remainder of a failed ``ir`` and close it — fail closed.

        The ``AGENTS.md`` §4.7 order is enforced end to end: plan successors →
        authorise → create them (idempotently, with reserved numbers) → verify
        links and full coverage from the persisted records → only then close the
        source.  Any refusal, transport error or coverage gap leaves the source
        open; a retry resumes without duplicating an already created successor.
        """
        if not isinstance(source, dict):
            raise GovernanceError("source must be an object")
        source_id = str(source.get("identifier", "")).strip()
        if not source_id:
            raise GovernanceError("source identifier is required")
        remaining = [str(item).strip() for item in
                     (source.get("remaining_criteria") or []) if str(item).strip()]
        if not remaining:
            raise GovernanceError("source has no remaining criteria to transfer")
        if not isinstance(successors, (list, tuple)) or not successors:
            raise GovernanceError("at least one successor is required")

        prior = self.transfers().get(source_id)
        if isinstance(prior, dict) and prior.get("closed"):
            return {"allowed": True, "already": True, "closed": True,
                    "created": list(prior.get("successors", [])),
                    "issue": source_id, "reason": "already consolidated"}

        transferred: list[str] = []
        titles: list[str] = []
        planned: list[dict] = []
        for item in successors:
            if not isinstance(item, dict):
                raise GovernanceError("each successor must be an object")
            title = str(item.get("title", "")).strip()
            if not title:
                raise GovernanceError("each successor needs a title")
            criteria = [str(c).strip() for c in (item.get("transferred_criteria") or [])
                        if str(c).strip()]
            if not criteria:
                raise GovernanceError(f"successor {title!r} transfers no criteria")
            unknown = sorted(set(criteria) - set(remaining))
            if unknown:
                return {"allowed": False, "created": [], "closed": False,
                        "rule": "scope_check",
                        "reason": f"successor {title!r} adds criteria outside the "
                                  f"source remainder: {unknown}"}
            transferred.extend(criteria)
            titles.append(title)
            planned.append({"title": title, "transferred_criteria": criteria,
                            "source": item})
        if len(set(titles)) != len(titles):
            return {"allowed": False, "created": [], "closed": False,
                    "rule": "scope_check",
                    "reason": "successor titles must be unique"}
        if set(transferred) != set(remaining) or len(transferred) != len(set(transferred)):
            missing = sorted(set(remaining) - set(transferred))
            return {"allowed": False, "created": [], "closed": False,
                    "rule": "coverage",
                    "reason": "remaining criteria must be transferred exactly once"
                              + (f"; missing {missing}" if missing else "")}

        # Reserve the entire plan before the first remote effect, including
        # automatically assigned numbers. Retries cannot change its scope.
        plans_path = self.state_dir / "transfer-intents.json"
        plans = read_json(plans_path, {}) or {}
        scope = hashlib.sha256(json.dumps({"source": source, "successors": successors,
                                          "reason": reason}, sort_keys=True).encode()).hexdigest()
        previous = plans.get(source_id)
        if previous:
            if previous["scope"] != scope:
                return {"allowed": False, "created": [], "closed": False,
                        "rule": "scope", "reason": "transfer retry changed its approved scope"}
            for entry, identifier in zip(planned, previous["identifiers"]):
                entry["source"] = {**entry["source"], "identifier": identifier}

        # Resolve identifiers through the shared continuous series (§4.2).
        taken = self.known_identifiers() | {source_id}
        for entry in planned:
            identifier = str(entry["source"].get("identifier", "")).strip()
            if identifier:
                if not _IDENTIFIER["ir"].match(identifier):
                    raise GovernanceError("successor identifier must look like ir.A.B.C.D")
                if identifier in taken:
                    # An identifier minted by a previous attempt of this very
                    # transfer is not a collision: its issue already exists and
                    # the idempotency check below reuses it instead of minting
                    # a second one.
                    record = self._record_by_identifier(identifier) or {}
                    link = record.get("metadata") or {}
                    intent = (read_json(self.state_dir / "issue-intents.json", {}) or {}).get(identifier)
                    if link.get("transferred_from") != source_id and not (intent and intent.get("identifier") == identifier):
                        return {"allowed": False, "created": [], "closed": False,
                                "rule": "identifier_used",
                                "reason": f"identifier {identifier} is already used"}
            else:
                identifier = reserve_identifier("ir", taken)
            taken.add(identifier)
            entry["identifier"] = identifier

        plans[source_id] = {"scope": scope, "identifiers": [entry["identifier"] for entry in planned]}
        write_json(plans_path, plans)

        # Authorisation before any effect: the owner approves the transfer and
        # the close of exactly this source issue.
        for action in (Action.CONSOLIDATE_IR, Action.DELEGATE_CLOSE_IR):
            decision = self.gate.decide(action, actor=self.actor, subject=source_id)
            if not decision.allowed:
                self.gate.trail.append(actor=self.actor, action="consolidate_ir",
                                       subject=source_id, decision="REJECTED",
                                       rule=decision.rule, reason=decision.reason)
                return {"allowed": False, "created": [], "closed": False,
                        "rule": decision.rule, "reason": decision.reason}

        created: list[str] = []
        for entry in planned:
            try:
                outcome = self.create_issue(
                    "ir", entry["title"], identifier=entry["identifier"],
                    key=entry["identifier"],
                    metadata={"transferred_from": source_id,
                              "transferred_criteria": entry["transferred_criteria"],
                              "source_title": str(source.get("title", ""))})
            except Exception as error:
                return {"allowed": True, "created": created, "closed": False,
                        "rule": "transport_error",
                        "reason": f"successor creation failed: {error}; source stays open"}
            if not outcome.get("allowed"):
                return {"allowed": False, "created": created, "closed": False,
                        "rule": outcome.get("rule", ""),
                        "reason": outcome.get("reason", "")}
            created.append(entry["identifier"])

        # Verify links and full coverage from the *persisted* records.
        records = [self._record_by_identifier(identifier) for identifier in created]
        if any(record is None for record in records):
            return {"allowed": True, "created": created, "closed": False,
                    "rule": "verification",
                    "reason": "successor record missing; source stays open"}
        covered: list[str] = []
        for record in records:
            metadata = record.get("metadata") or {}
            if metadata.get("transferred_from") != source_id:
                return {"allowed": True, "created": created, "closed": False,
                        "rule": "verification",
                        "reason": f"link {source_id} → {record.get('identifier')} "
                                  "missing; source stays open"}
            covered.extend(metadata.get("transferred_criteria") or [])
            try:
                remote = self.transport.get_issue(record["issue"]["number"])
                remote_hash = hashlib.sha256(str(remote.get("body", "")).encode()).hexdigest()
                if remote_hash != record["issue"]["expected_body_sha256"]:
                    raise GovernanceError("Remote successor body differs from approved coverage")
            except Exception as error:
                return {"allowed": True, "created": created, "closed": False,
                        "rule": "verification", "reason": f"Remote coverage unconfirmed: {error}"}
        if set(covered) != set(remaining) or len(covered) != len(set(covered)):
            return {"allowed": True, "created": created, "closed": False,
                    "rule": "verification",
                    "reason": "transfer verification failed; source stays open"}

        comment = (reason or
                   f"Перенесено залишок {sorted(remaining)} до "
                   f"{', '.join(created)}; перевірено зв'язки й повноту перенесення.")
        try:
            source_record = self._record_by_identifier(source_id) or {}
            source_number = source.get("number") or source_record.get("issue", {}).get("number")
            if self.transport.dry_run or not source_number:
                raise GovernanceError("Verified numeric source issue is required before transfer close")
            close = self.transport.close_issue(
                source_number, comment=comment)
            if close.get("state") != "closed" or close.get("dry_run"):
                raise GovernanceError("Remote source close was not confirmed")
        except Exception as error:
            return {"allowed": True, "created": created, "closed": False,
                    "rule": "transport_error",
                    "reason": f"close failed: {error}; source stays open"}
        transfer_record = {"source": source_id, "successors": created,
                           "remaining_criteria": list(remaining),
                           "closed": True,
                           "closed_at": datetime.now(timezone.utc).isoformat(),
                           "close": close, "reason": reason}
        payload = self.transfers()
        payload[source_id] = transfer_record
        write_json(self._transfers_path, payload)
        self.gate.trail.append(actor=self.actor, action="consolidate_ir:effect",
                               subject=source_id, decision="CONSOLIDATED",
                               rule="owner_grant",
                               reason=f"closed source, created {', '.join(created)}")
        append_jsonl(self.state_dir / "governance.jsonl",
                     {"at": transfer_record["closed_at"], "action": "consolidate_ir",
                      "subject": source_id, "successors": created})
        return {"allowed": True, "created": created, "closed": True,
                "issue": source_id, "close": close, "record": transfer_record}

    def push(self) -> dict:
        return self._refuse_or_report(Action.PUSH, "")

    # -- refused surfaces ------------------------------------------------
    def close_issue(self, issue: str) -> dict:
        """Closing any issue — including an ``ir`` — is never autonomous."""
        return self._refuse_or_report(Action.CLOSE_ISSUE, issue)

    def probe_surface(self, kind: str, subject: str = "") -> dict:
        """Side-effect-free probe: rule on a surface without touching it.

        The panel may ask *why* ``i``/``ir``/``var`` creation, editing, closing
        or pushing is refused; the answer comes from the gate and never reaches
        the transport (Issue i.0.0.2.50, L4/L6).
        """
        actions = {"i": Action.CREATE_I_ISSUE, "ir": Action.CREATE_IR_ISSUE,
                   "var": Action.CREATE_VAR_ISSUE, "lab": Action.CREATE_LAB_ISSUE,
                   "edit": Action.EDIT_ISSUE, "close": Action.CLOSE_ISSUE,
                   "push": Action.PUSH}
        action = actions.get(str(kind))
        if action is None:
            raise GovernanceError(f"unknown issue surface {kind}")
        return self._refuse_or_report(action, subject)


def render_lab_body(finding: dict) -> str:
    """Render a ``lab`` issue body from a validated finding."""
    lines = [
        "## Тип",
        "",
        f"`lab` — знахідка автономної лабораторії ({finding.get('kind', 'defect')}).",
        "",
        "## Доказ",
        "",
        str(finding.get("evidence", "")).strip(),
        "",
        "## Відтворення",
        "",
        str(finding.get("reproduction", "")).strip(),
        "",
        "## Ризик",
        "",
        str(finding.get("risk", "")).strip(),
        "",
        "## Очікуваний результат",
        "",
        str(finding.get("expected_result", "")).strip(),
        "",
        "## Межі",
        "",
        str(finding.get("boundaries", "")).strip(),
        "",
        "## Джерело та погоджений scope",
        "",
        f"- Джерело: {str(finding.get('source', '')).strip()}",
        f"- Погоджена вимога: {str(finding.get('approved_scope', '')).strip()}",
        "",
        "Це не продуктова ідея та не нова функція: лабораторія фіксує лише",
              "підтверджений дефект, регресію, порушення безпеки або невідповідність уже",
              "затвердженим вимогам (`AGENTS.md` §4.6).",
    ]
    return "\n".join(lines)


def render_issue_body(kind: str, title: str, identifier: str = "",
                      metadata: dict | None = None) -> str:
    """Render the body of an approved ``i``/``ir``/``var`` issue."""
    metadata = metadata or {}
    lines = [
        "## Тип",
        "",
        f"`{kind}` — створено автономною лабораторією за погодженням власника "
        "(`AGENTS.md` §4.6).",
        "",
        "## Назва",
        "",
        title,
    ]
    if identifier:
        lines += ["", "## Ідентифікатор", "", f"`{identifier}`"]
    criteria = [str(item) for item in (metadata.get("transferred_criteria") or [])]
    if criteria:
        lines += ["", "## Перенесений залишок",
                  "", f"- Джерело: `{metadata.get('transferred_from', '')}`",
                  f"- Критерії: {', '.join(f'`{item}`' for item in criteria)}"]
    lines += ["", "## Межі",
              "",
              "Лише погоджений scope цього Issue; нові вимоги не додаються."]
    return "\n".join(lines)



