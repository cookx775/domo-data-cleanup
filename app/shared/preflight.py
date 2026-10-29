"""The prerequisite contract, executed.

**Preflight verifies by QUERYING, never by existence.** A dataset can exist, report a
current `lastUpdated`, and still be useless. Both failure modes are live:

1. **Empty but present.** Altimate's `Domostats | People` replicas exist and return zero
   rows. An existence check passes; the pillar then produces silently wrong output.
2. **Archived but fresh-looking.** `RXA | Dataset Cleanup` reports a plausible 1,649-row
   metadata count and returns `410 Gone` when queried.

Three things preflight deliberately does NOT do:

* **It asserts no admin role.** Governance datasets can be shared to a non-admin, and
  demanding Admin would block a legitimate subsidiary setup (ticket 04 decision 6). Every
  zero-row result becomes a coverage exception regardless of cause.
* **It cannot check token expiry.** `/api/data/v1/accesstokens` lists tokens with an
  `expires` epoch but nothing maps the token *in hand* back to its row, so "does it work
  right now" is the only honest check.
* **It never picks a dataset id.** On a missing or failed entry it calls the SAME shape
  matcher discovery uses and PRINTS candidates without writing a proposal. Config stays
  authoritative.

A missing FLOOR report is a hard prerequisite failure with no fallback. Every other report
is handled per condition, over a required floor — one blocked leg never stops the others.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import subprocess
import sys
from typing import Dict, List, Optional

from collector import config as config_mod
from collector import manifest as manifest_mod
from shared import sql
from collector.discover import Discovery, discover
from shared.domo import (
    ArchivedError,
    AuthError,
    DomoClient,
    DomoError,
    NotFoundError,
    ProbeResult,
    load_token,
)
from collector.manifest import Manifest, Report

log = logging.getLogger("preflight")

#: A report refreshed longer ago than this is reported as stale. Not a failure: a run
#: against a five-month-stale replica would silently report April as if it were today, and
#: the honest response is to say so loudly and let the operator decide.
FRESHNESS_WARN_DAYS = 3


class PrerequisiteFailure(Exception):
    """A required floor report is missing, empty, archived or the wrong shape. The data
    pillar cannot run and its absence aborts the run (docs/prerequisites.md)."""


class ReportCheck:
    __slots__ = (
        "key",
        "floor",
        "dataset_id",
        "probe",
        "observed_columns",
        "missing_read",
        "missing_identify",
        "last_refreshed",
        "stale_days",
        "error",
    )

    def __init__(self, report: Report, dataset_id: Optional[str]) -> None:
        self.key = report.key
        self.floor = report.floor
        self.dataset_id = dataset_id
        self.probe: Optional[ProbeResult] = None
        self.observed_columns: List[str] = []
        self.missing_read: List[str] = []
        self.missing_identify: List[str] = []
        self.last_refreshed: Optional[str] = None
        self.stale_days: Optional[int] = None
        self.error: str = ""

    @property
    def ok(self) -> bool:
        """Usable for its consuming conditions: queried, non-empty, and carrying every
        column that identifies it. A missing READ column degrades rather than blocks —
        the condition that needs it reports a coverage exception by name."""
        return bool(
            self.dataset_id
            and self.probe
            and self.probe.ok
            and not self.missing_identify
            and not self.error
        )

    @property
    def degraded(self) -> bool:
        return self.ok and bool(self.missing_read)

    @property
    def reason(self) -> Optional[str]:
        if self.ok:
            return None
        if not self.dataset_id:
            return "dataset-missing"
        if self.missing_identify:
            return "shape-mismatch"
        if self.error:
            return "dataset-error"
        return self.probe.reason if self.probe else "dataset-error"

    @property
    def detail(self) -> str:
        if self.ok and not self.missing_read:
            return ""
        if not self.dataset_id:
            return (
                f"no id configured for `{self.key}`. Run "
                f"`python -m collector.discover` and move an id into config.yaml."
            )
        if self.missing_identify:
            return (
                f"{self.dataset_id} is missing identifying columns "
                f"{self.missing_identify} — this is not a `{self.key}` report"
            )
        if self.error:
            return f"{self.dataset_id}: {self.error}"
        if self.probe and self.probe.status == 200 and not self.probe.rows:
            return (
                f"{self.dataset_id} exists and returns ZERO ROWS. The connector is "
                f"configured but has produced no data."
            )
        if self.probe and self.probe.status == 410:
            return f"{self.dataset_id} is archived (410) while its metadata looks healthy"
        if self.probe and self.probe.status == 404:
            return f"{self.dataset_id} does not exist on this instance"
        if self.missing_read:
            return f"{self.dataset_id} is missing read columns {self.missing_read}"
        return f"{self.dataset_id}: {self.probe.detail if self.probe else 'unknown'}"

    def as_dict(self) -> dict:
        return {
            "report": self.key,
            "floor": self.floor,
            "dataset_id": self.dataset_id,
            "ok": self.ok,
            "probe": self.probe.as_dict() if self.probe else None,
            "missing_identify": self.missing_identify,
            "missing_read": self.missing_read,
            "last_refreshed": self.last_refreshed,
            "stale_days": self.stale_days,
            "reason": self.reason,
            "detail": self.detail,
        }


class PreflightResult:
    def __init__(self, instance: str) -> None:
        self.instance = instance
        self.checks: Dict[str, ReportCheck] = {}
        self.custody: List[str] = []
        self.discovery_hint: List[str] = []
        # The front door needs the same probed candidates that preflight printed, so it can
        # ask the administrator to affirm one. Keeping the object here avoids a second
        # enumeration with a possibly different answer from Domo's unstable listing.
        self.discovery: Optional[Discovery] = None

    @property
    def floor_failures(self) -> List[ReportCheck]:
        return [c for c in self.checks.values() if c.floor and not c.ok]

    @property
    def ok(self) -> bool:
        return not self.floor_failures

    def available(self, key: str) -> bool:
        check = self.checks.get(key)
        return bool(check and check.ok)

    def missing_read(self, key: str) -> List[str]:
        check = self.checks.get(key)
        return list(check.missing_read) if check else []

    def exception_for(self, key: str) -> Optional[dict]:
        """Shaped for `coverage.pillars[].exception` in the findings schema."""
        check = self.checks.get(key)
        if not check or check.ok:
            return None
        return {
            "reason": check.reason,
            "detail": check.detail,
            "dataset": check.dataset_id,
        }

    def as_dict(self) -> dict:
        return {
            "instance": self.instance,
            "ok": self.ok,
            "reports": [c.as_dict() for c in self.checks.values()],
            "credential_custody": self.custody,
        }

    def report_lines(self) -> List[str]:
        lines = []
        for check in self.checks.values():
            floor = "floor" if check.floor else "cond "
            if check.ok:
                mark = "DEGRADED" if check.degraded else "ok"
                rows = check.probe.rows if check.probe else "?"
                fresh = check.last_refreshed or "freshness unknown"
                extra = f"  MISSING READ {check.missing_read}" if check.missing_read else ""
                lines.append(
                    f"  [{mark:9s}] {floor} {check.key:20s} {rows} row(s) probed, "
                    f"refreshed {fresh}{extra}"
                )
                if check.stale_days is not None and check.stale_days > FRESHNESS_WARN_DAYS:
                    lines.append(
                        f"              STALE: last refreshed {check.stale_days} days ago. "
                        f"Findings will describe that date, not today."
                    )
            else:
                lines.append(f"  [{'FAILED':9s}] {floor} {check.key:20s} {check.detail}")
        return lines


def _inside_git_work_tree(path: str) -> bool:
    """True if any ancestor directory of `path` holds a `.git` entry (directory or file)."""
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(directory, ".git")):
            return True
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent


def check_credential_custody(cfg: config_mod.Config) -> List[str]:
    """Refuse to proceed if the instance `.env` is tracked by a git work tree.

    Ticket 04 decision 3. The token is a personal credential, and this repo is committed
    and shared; a tracked `.env` leaks it to everyone who clones. Returns a list of
    problems — empty means clean.
    """
    problems: List[str] = []
    env_path = cfg.env_path
    if not os.path.exists(env_path):
        return problems
    try:
        completed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", env_path],
            cwd=cfg.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # No git available. Outside any work tree the file cannot be tracked, which is the
        # normal case for an administrator's package install; inside one, say so rather than
        # implying the file was checked.
        if not _inside_git_work_tree(env_path):
            return problems
        return [f"could not verify whether {env_path} is git-tracked (git unavailable)"]
    if completed.returncode == 0:
        problems.append(
            f"{env_path} is TRACKED BY GIT. That file holds a developer access token and "
            f"this repo is shared. `git rm --cached` it and confirm it is gitignored before "
            f"running again."
        )
    return problems


def _freshness(client: DomoClient, report_key: str, dataset_id: str) -> tuple:
    """(last_refreshed, stale_days) from the connector-injected `_BATCH_LAST_RUN_`.

    That column is in the manifest's `ignore_columns` — it carries no identifying
    information and is excluded from every fingerprint — but it is exactly the right
    freshness signal, because it records when the CONNECTOR last wrote, not when someone
    touched the dataset's metadata.

    Absent on a report that is not connector-published, in which case freshness is reported
    as unknown rather than assumed current.
    """
    query = sql.build(
        report_key,
        "SELECT MAX({_BATCH_LAST_RUN_}) AS last_run FROM table",
        limit=5,
        name="freshness",
    )
    try:
        result = client.execute(dataset_id, query)
    except DomoError:
        return None, None
    if not len(result):
        return None, None
    value = result.one().get("last_run")
    if value.is_ambiguous or value.is_absent:
        return None, None
    try:
        stamp = value.as_datetime()
    except ValueError:
        return None, None
    days = (_dt.datetime.now(_dt.timezone.utc) - stamp).days
    return stamp.replace(tzinfo=None).isoformat(timespec="seconds"), days


def check_report_id(client: DomoClient, manifest: Manifest, report: Report, dataset_id: str) -> ReportCheck:
    """Apply the configured-report shape and query checks to one proposed ID."""
    check = ReportCheck(report, dataset_id)
    try:
        observed = manifest.strip_ignored(client.columns(dataset_id))
        check.observed_columns = observed
        present = set(observed)
        check.missing_identify = [c for c in report.identify if c not in present]
        check.missing_read = [c for c in report.read if c not in present]
    except ArchivedError:
        check.probe = ProbeResult(dataset_id, 410, None, "archived")
        return check
    except NotFoundError:
        check.probe = ProbeResult(dataset_id, 404, None, "no dataset with this id")
        return check
    except AuthError as exc:
        check.probe = ProbeResult(dataset_id, 403, None, str(exc))
        return check
    except DomoError as exc:
        check.error = str(exc)
        return check

    identify_column = report.identify[0]
    if identify_column not in set(check.observed_columns):
        identify_column = check.observed_columns[0] if check.observed_columns else identify_column
    check.probe = client.probe(dataset_id, sql.probe_query(report.key, identify_column))
    if check.probe and check.probe.ok:
        check.last_refreshed, check.stale_days = _freshness(client, report.key, dataset_id)
    return check


def preflight(
    client: DomoClient,
    cfg: config_mod.Config,
    manifest: Optional[Manifest] = None,
    offer_discovery: bool = True,
) -> PreflightResult:
    manifest = manifest or manifest_mod.load()
    result = PreflightResult(instance=cfg.instance)
    result.custody = check_credential_custody(cfg)

    for report in manifest:
        dataset_id = cfg.report_id(report.key)
        if not dataset_id:
            result.checks[report.key] = ReportCheck(report, dataset_id)
            continue
        result.checks[report.key] = check_report_id(client, manifest, report, dataset_id)

    failures = [c for c in result.checks.values() if not c.ok]
    if failures and offer_discovery:
        result.discovery, result.discovery_hint = _discovery_candidates(
            client, manifest, [c.key for c in failures]
        )

    return result


def _discovery_candidates(
    client: DomoClient, manifest: Manifest, keys: List[str]
) -> tuple:
    """Print candidates for the failed reports, WITHOUT writing a proposal.

    Calls the same `discover()` discovery does — two implementations of shape matching
    would drift, and the one that drifts is the one that runs on a stranger's instance.
    """
    lines: List[str] = []
    try:
        found = discover(client, manifest=manifest, probe=True)
    except AuthError:
        # An invalid or cross-instance credential is not a report-selection problem. The
        # caller must stop before displaying candidates or changing configuration.
        raise
    except DomoError as exc:
        return None, [f"  (could not enumerate datasets to suggest candidates: {exc})"]
    for key in keys:
        proposal = found.proposals.get(key)
        if not proposal:
            continue
        if proposal.confidence == "high" and proposal.selected:
            lines.append(
                f"  {key}: one high-confidence candidate — "
                f"{proposal.selected.name} ({proposal.selected.dataset_id})"
            )
        elif proposal.candidates:
            lines.append(f"  {key}: {len(proposal.candidates)} candidates, choose one —")
            for candidate in proposal.candidates:
                lines.append(f"      {candidate.dataset_id}  {candidate.name}")
        else:
            lines.append(f"  {key}: no candidate matched the shape on this instance")
    if lines:
        lines.append(
            "  (nothing was written — put an id into config.yaml yourself, or run "
            "`python -m collector.discover` for the full proposal with rejections)"
        )
    return found, lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shared.preflight",
        description="Verify the prerequisite contract against an instance, by querying.",
    )
    parser.add_argument("--instance", required=True, help="Instance key under instances/")
    parser.add_argument("--env", default=None, help="Path to a .env holding DOMO_API_TOKEN")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        cfg = config_mod.load(args.instance)
        client = DomoClient(cfg.domain, load_token(args.env or cfg.env_path))
        result = preflight(client, cfg)
    except AuthError as exc:
        print("\nAUTHENTICATION STOP — no report candidates were selected or written.")
        print(f"  {exc}")
        print("  Replace or export DOMO_API_TOKEN for this exact instance, then run preflight again.")
        return 2

    print(f"\nPreflight — {cfg.instance} ({cfg.domain})\n")
    for line in result.report_lines():
        print(line)

    if result.custody:
        print("\nCREDENTIAL CUSTODY:")
        for problem in result.custody:
            print(f"  {problem}")

    if result.discovery_hint:
        print("\nCandidates for the failed reports:")
        for line in result.discovery_hint:
            print(line)

    if result.ok:
        print("\nFloor reports OK. The data pillar can run.")
        return 0
    print("\nFLOOR REPORT FAILURE — the data pillar cannot run:")
    for check in result.floor_failures:
        print(f"  {check.key}: {check.detail}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
