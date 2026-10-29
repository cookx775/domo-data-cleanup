"""Shape-based report discovery. Ticket 15.

Discovery **proposes and never selects**. It writes
`instances/<instance>/cleanup/discovery.proposed.yaml` and **nothing reads it** — an admin
moves entries into the live config, which stays authoritative. Both tiebreaks anyone
reaches for first are disproven on granite:

* **"Most rows" is wrong four times out of five, and anti-correlated with truth.** It picks
  a 429,747-row `Approved Migration Assets - CLEAN` over the real 1,812-row
  `DOMO | Dataflow Details`, and a 2.9M-row usage derivative over the true dataset
  inventory — which reports `rowCount: 0` while being fully populated.
* **"Most recently refreshed" does not discriminate at all** — ten `Dataflow Details`
  candidates tie on the same day.

So an automatic tiebreak binds the collector to a confident, well-formed, wrong table.

**Discovery never fails and always exits zero.** Preflight is the gate and already fails the
run on a missing floor report. A discovery that aborted on the first unfound report would
write a partial proposal, so an admin on an instance nobody here has seen would fix one
report, re-run, and discover the next — the worst possible loop. One run reports every
problem at once.

Entry points are `python -m collector.discover` and `shared.preflight`, both calling
`discover()` here. Two implementations of shape matching would drift, and the one that
drifts is the one that runs on a stranger's instance.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys
import time
from typing import Dict, List, Optional

import yaml

from . import config as config_mod
from . import manifest as manifest_mod
from shared import sql
from shared.domo import DomoClient, DomoError, load_token
from .manifest import Manifest, Report

log = logging.getLogger("collector.discover")

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"
CONFIDENCE_AMBIGUOUS = "ambiguous"
CONFIDENCE_NONE = "none"


def _columns_of(item: dict) -> List[str]:
    """Column names from the schema the search response carries INLINE.

    43 of granite's 2,205 datasets have no schema at all; they are recorded as unexamined
    rather than silently skipped.
    """
    schema = item.get("schema") or {}
    names: List[str] = []
    for table in schema.get("tables") or []:
        for column in table.get("columns") or []:
            name = column.get("name")
            if name:
                names.append(name)
    return names


class Candidate:
    """One dataset considered for one report."""

    __slots__ = (
        "dataset_id",
        "name",
        "rows",
        "transport",
        "provider",
        "columns",
        "confidence",
        "missing_read",
        "probe",
        "why_rejected",
    )

    def __init__(self, item: dict, columns: List[str]) -> None:
        self.dataset_id = item.get("id", "")
        self.name = item.get("name", "")
        self.rows = item.get("rowCount")
        self.transport = item.get("transportType")
        self.provider = item.get("dataProviderType")
        self.columns = columns
        self.confidence = CONFIDENCE_LOW
        self.missing_read: List[str] = []
        self.probe = None
        self.why_rejected = ""

    def tier(self, report: Report) -> str:
        """The confidence tier: how far this candidate's PROVENANCE matches the report it
        claims to be.

        A report published by the connector itself outranks a replica of another
        instance's copy, which outranks a hand-built derivative. It is a TIER, never a
        filter — an instance may legitimately receive governance data by Domo Everywhere
        publish rather than running the connector, so a low-confidence candidate is
        proposed and labelled, never discarded.

        The provider is PREFIX-matched, never compared literally: granite's is
        `domo-governance-d14c2fef-49a8-4898-8ddd-f64998005600`, and the UUID suffix
        identifies the CONNECTOR rather than the instance. That last part is inference from
        one instance — replicas report `domo-federateddata` and hide the source's provider —
        but because this is only a tier, a wrong guess downgrades a report rather than
        losing it.
        """
        provider = str(self.provider or "")
        if self.transport == "CONNECTOR" and provider.startswith(report.provider_prefix):
            return CONFIDENCE_HIGH
        return CONFIDENCE_LOW

    def as_dict(self, include_columns: bool = False) -> dict:
        out: dict = {
            "id": self.dataset_id,
            "name": self.name,
            "rows": self.rows,
            "transport": self.transport,
            "provider": self.provider,
        }
        if self.probe is not None:
            out["probe"] = self.probe.as_dict()
        if self.missing_read:
            out["read_set"] = {"missing": list(self.missing_read)}
        if include_columns:
            out["observed_columns"] = list(self.columns)
        return out

    def as_rejection(self) -> dict:
        out = self.as_dict()
        out["why"] = self.why_rejected
        return out


class ReportProposal:
    __slots__ = ("report", "selected", "confidence", "candidates", "rejected")

    def __init__(self, report: Report) -> None:
        self.report = report
        self.selected: Optional[Candidate] = None
        self.confidence = CONFIDENCE_NONE
        self.candidates: List[Candidate] = []
        self.rejected: List[Candidate] = []

    def as_dict(self) -> dict:
        out: dict = {
            # `id` is filled in ONLY for a lone high-confidence match, so a subsidiary
            # admin with no replicas transcribes nothing — while "config is authoritative"
            # stays literally true, because the file the collector reads is still one a
            # human put there.
            "id": self.selected.dataset_id if self.selected else None,
            "name": self.selected.name if self.selected else None,
            "confidence": self.confidence,
            "floor": self.report.floor,
        }
        if self.selected:
            out["transport"] = self.selected.transport
            out["provider"] = self.selected.provider
            out["probe"] = self.selected.probe.as_dict() if self.selected.probe else None
            out["read_set"] = (
                {"missing": self.selected.missing_read}
                if self.selected.missing_read
                else "complete"
            )
            out["observed_columns"] = list(self.selected.columns)
        if self.confidence == CONFIDENCE_AMBIGUOUS:
            out["choose_between"] = [c.as_dict() for c in self.candidates]
        # The rejected list is load-bearing, not decoration: on granite it runs to a dozen
        # entries per report, and it is the only thing that lets a reviewer see discovery
        # passed over a 429,747-row `CLEAN` table on purpose.
        if self.rejected:
            out["rejected"] = [c.as_rejection() for c in self.rejected]
        return out


class Discovery:
    __slots__ = ("instance", "proposals", "enumeration", "manifest", "unexamined")

    def __init__(self, instance: str, manifest: Manifest) -> None:
        self.instance = instance
        self.manifest = manifest
        self.proposals: Dict[str, ReportProposal] = {}
        self.enumeration: dict = {}
        self.unexamined: List[dict] = []

    def resolved(self) -> Dict[str, str]:
        """report key -> dataset id, for the lone high-confidence matches only."""
        return {
            key: proposal.selected.dataset_id
            for key, proposal in self.proposals.items()
            if proposal.confidence == CONFIDENCE_HIGH and proposal.selected
        }

    def as_dict(self) -> dict:
        return {
            # Ticket 26. UTC, matching `run.as_of_date` and the ledger, so a proposal and
            # the run that follows it cannot be stamped with two different days.
            "discovered_at": _dt.datetime.now(_dt.timezone.utc).date().isoformat(),
            "instance": self.instance,
            "manifest_version": self.manifest.version,
            "note": (
                "PROPOSED, not active. The collector never reads this file. Move the ids you "
                "accept into config.yaml, which stays authoritative. A null id means "
                "discovery refused to choose - see `choose_between`."
            ),
            "enumeration": self.enumeration,
            "reports": {key: p.as_dict() for key, p in self.proposals.items()},
            "unexamined": self.unexamined,
        }

    def summary_lines(self) -> List[str]:
        lines = []
        for key, proposal in self.proposals.items():
            floor = " [floor]" if proposal.report.floor else ""
            if proposal.confidence == CONFIDENCE_HIGH and proposal.selected:
                missing = (
                    f" MISSING {proposal.selected.missing_read}"
                    if proposal.selected.missing_read
                    else ""
                )
                lines.append(
                    f"  {key}{floor}: {proposal.selected.name} "
                    f"({proposal.selected.dataset_id}){missing}"
                )
            elif proposal.confidence == CONFIDENCE_AMBIGUOUS:
                lines.append(
                    f"  {key}{floor}: AMBIGUOUS — {len(proposal.candidates)} candidates, "
                    f"choose one:"
                )
                for candidate in proposal.candidates:
                    probe = candidate.probe
                    state = f"{probe.status}/{probe.rows}rows" if probe else "unprobed"
                    lines.append(
                        f"      {candidate.dataset_id}  {candidate.name}  "
                        f"[{candidate.confidence}, {state}]"
                    )
            else:
                lines.append(
                    f"  {key}{floor}: NO CANDIDATE "
                    f"({len(proposal.rejected)} rejected on provenance or shape)"
                )
        return lines


def discover(
    client: DomoClient,
    manifest: Optional[Manifest] = None,
    probe: bool = True,
) -> Discovery:
    """Enumerate, fingerprint, tier and probe. Never raises on a report it cannot find."""
    manifest = manifest or manifest_mod.load()
    result = Discovery(instance=client.host, manifest=manifest)

    started = time.time()
    calls_before = client.calls
    # `assert_complete=False`: discovery NEVER fails and always exits zero, so a lossy
    # enumeration is reported in the proposal rather than raised. The collector's scoring
    # path asserts completeness instead — see `DomoClient.search_datasets`.
    items = client.search_datasets(assert_complete=False)
    elapsed = time.time() - started

    schemas: Dict[str, List[str]] = {}
    for item in items:
        columns = _columns_of(item)
        if columns:
            schemas[item.get("id", "")] = columns
        else:
            result.unexamined.append({"id": item.get("id"), "name": item.get("name")})

    enumeration = client.last_enumeration or {}
    result.enumeration = {
        "datasets_seen": len(items),
        "distinct_ids": enumeration.get("distinct_ids"),
        "total_count": enumeration.get("total_count"),
        # `datasources/search` is non-deterministically incomplete in two forms — a short
        # page, and a full-length page carrying a duplicate that displaced a real dataset —
        # and both are HTTP 200 with no flag. Recorded rather than raised here, because a
        # proposal an admin reads is worth more than an abort; a report absent from an
        # incomplete read shows up as NO CANDIDATE, which is the honest outcome.
        "complete": enumeration.get("complete"),
        "without_schema": len(result.unexamined),
        "calls": client.calls - calls_before,
        "elapsed_s": round(elapsed, 1),
    }

    for report in manifest:
        proposal = ReportProposal(report)
        result.proposals[report.key] = proposal
        identify = set(report.identify)

        for item in items:
            columns = schemas.get(item.get("id", ""))
            if not columns:
                continue
            observed = manifest.strip_ignored(columns)
            if not identify.issubset(set(observed)):
                continue
            candidate = Candidate(item, observed)
            candidate.confidence = candidate.tier(report)
            # The read set is validated against the match and reported MISSING BY NAME.
            # One combined set would force a bad trade: all read columns in the matching
            # set means a single dropped column yields ZERO candidates, and the admin sees
            # "nothing found" when the truth is "found it, it is missing Execution
            # Success Count".
            candidate.missing_read = [c for c in report.read if c not in set(observed)]
            if candidate.confidence == CONFIDENCE_HIGH:
                proposal.candidates.append(candidate)
            else:
                candidate.why_rejected = (
                    f"transport={candidate.transport} provider={candidate.provider}"
                )
                proposal.rejected.append(candidate)

        # Probe EVERY surviving candidate, not just the chosen one. An ambiguous report
        # writes id: null and leaves the admin choosing on the evidence in the file — and
        # the only other metadata there is the rowCount that lies in both directions.
        # Without a probe the proposal lists a real table and an empty replica side by
        # side and does not say which is which.
        if probe:
            for candidate in proposal.candidates:
                probe_query = sql.probe_query(report.key, report.identify[0])
                try:
                    candidate.probe = client.probe(candidate.dataset_id, probe_query)
                except DomoError as exc:  # pragma: no cover - probe swallows its own
                    log.warning("probe failed for %s: %s", candidate.dataset_id, exc)

        if len(proposal.candidates) == 1:
            proposal.selected = proposal.candidates[0]
            proposal.confidence = CONFIDENCE_HIGH
        elif len(proposal.candidates) > 1:
            proposal.confidence = CONFIDENCE_AMBIGUOUS
        else:
            proposal.confidence = CONFIDENCE_NONE

    return result


def write_proposal(discovery: Discovery, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(
            discovery.as_dict(), handle, sort_keys=False, default_flow_style=False, width=100
        )
    return path


def proposal_path(repo_root: str, instance_key: str) -> str:
    return os.path.join(
        repo_root, "instances", instance_key, "cleanup", "discovery.proposed.yaml"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.discover",
        description=(
            "Propose governance report ids by column shape and provenance. Writes a "
            "proposal the collector never reads; a human moves ids into config.yaml."
        ),
    )
    parser.add_argument("--instance", required=True, help="e.g. acme or acme.domo.com")
    parser.add_argument(
        "--instance-key",
        default=None,
        help="Directory name under instances/ (defaults to the instance's first label)",
    )
    parser.add_argument("--out", default=None, help="Override the output path")
    parser.add_argument("--env", default=None, help="Path to a .env holding DOMO_API_TOKEN")
    parser.add_argument("--no-probe", action="store_true", help="Skip the LIMIT 1 probes")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    instance_key = args.instance_key or args.instance.split(".")[0]
    # `config.repo_root()` finds the directory holding `instances/`, which is the thing
    # actually being addressed. A hardcoded `../../..` is correct in this repo and lands
    # OUTSIDE the handoff package, where `collector/` sits one level down instead of
    # three — an admin unzipping to ~/Downloads/domo-cleanup got their proposal in ~/.
    repo_root = config_mod.repo_root()
    out = args.out or proposal_path(repo_root, instance_key)

    client = DomoClient(args.instance, load_token(args.env))
    result = discover(client, probe=not args.no_probe)
    write_proposal(result, out)

    print(
        f"\nEnumerated {result.enumeration['datasets_seen']} datasets in "
        f"{result.enumeration['calls']} calls, {result.enumeration['elapsed_s']}s "
        f"({result.enumeration['without_schema']} without a schema, recorded unexamined)"
    )
    if result.enumeration.get("complete") is False:
        print(
            f"  WARNING: the enumeration is INCOMPLETE — "
            f"{result.enumeration['distinct_ids']} distinct ids against the server's own "
            f"totalCount of {result.enumeration['total_count']}. A report missing below may "
            f"exist and simply not have been returned. Re-run before acting on a NO "
            f"CANDIDATE line."
        )
    print()
    for line in result.summary_lines():
        print(line)
    print(f"\nProposal written to {out}")
    print("Nothing reads that file. Move the ids you accept into config.yaml.")
    # Always zero. Preflight is the gate.
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
