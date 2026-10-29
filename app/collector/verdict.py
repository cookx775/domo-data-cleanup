"""`python -m collector.verdict` — the append-only verdict CLI.

It exists because hand-typing thirty timestamped JSON objects is the step that never
happens twice. It **only ever appends valid JSONL**, so hand-editing the ledger stays a
legitimate fallback — this is a convenience over the format, not a gatekeeper to it.

Rejected: annotating the markdown report and parsing it back. That is a parser against
human-edited prose, and the markdown report is not even confirmed for the MVP.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import subprocess
import sys
from typing import Dict, List, Optional

from . import config as config_mod
from . import ledger as ledger_mod
from . import appdb as appdb_mod
import uuid
from . import reconcile as reconcile_mod
from . import triage as triage_mod
from .annotate import head_selector

KEYS = {
    "k": ("verdict", "keep-permanent"),
    "n": ("verdict", "keep-for-now"),
    "r": ("verdict", "revoked"),
    "a": ("lifecycle", "approved"),
    "q": ("lifecycle", "quarantined"),
    "d": ("lifecycle", "deleted"),
    "u": ("lifecycle", "restored"),
}

PROMPT = (
    "  [k]eep-permanent  keep-for-[n]ow  [r]evoke  |  [a]pprove  [q]uarantine  "
    "[d]eleted  restored([u])  |  [s]kip  e[x]it > "
)


def resolve_by(explicit: Optional[str], repo_root: str) -> str:
    """`--by`, else `git config user.email`, else a hard error.

    An unattributed verdict is worthless in a review six months later, so there is no
    default of "unknown".
    """
    if explicit:
        return explicit
    try:
        completed = subprocess.run(
            ["git", "config", "user.email"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        candidate = completed.stdout.decode("utf-8", "replace").strip()
        if candidate:
            return candidate
    except (OSError, subprocess.SubprocessError):
        pass
    raise SystemExit(
        "cannot determine who is recording this verdict. Pass --by <email>, or set "
        "`git config user.email`. An unattributed verdict is worthless in a review six "
        "months later."
    )


def load_findings(runs_dir: str, run_id: Optional[str]) -> dict:
    """Load a named artifact, or the newest canonical findings artifact by `started_at`."""
    paths = sorted(glob.glob(os.path.join(runs_dir, "*.json")))
    if not paths:
        raise SystemExit(f"no findings files in {runs_dir}. Run the collector first.")
    best = None
    best_started = ""
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (ValueError, OSError):
            continue
        if not reconcile_mod.is_findings_artifact(path, document):
            continue
        run = document.get("run") or {}
        if run_id and run.get("run_id") != run_id:
            continue
        started = run.get("started_at") or ""
        if started > best_started:
            best_started = started
            best = document
    if best is None:
        if run_id:
            raise SystemExit(f"no findings file matching run_id={run_id!r} in {runs_dir}")
        raise SystemExit(f"no findings files in {runs_dir}. Run the collector first.")
    return best


def owner_name(candidate: dict) -> str:
    """The owner label shared by the report and its backlog selector."""
    value = (candidate.get("owner") or {}).get("name") or "Unassigned"
    return " ".join(str(value).replace("`", "'").split())


def select_backlog_group(
    document: dict,
    cfg: config_mod.Config,
    owner: str,
    band: str,
    revisit: bool,
    existing: ledger_mod.Ledger,
) -> List[dict]:
    """Select one report group at candidate grain.

    A group is only a selector. Returning the original candidates lets the existing repeated
    ``--set`` path create one ordinary asset+condition ledger line for every decision.
    """
    candidates = list(document.get("candidates") or [])
    missing_bands = [
        candidate.get("candidate_id") or "<missing candidate id>"
        for candidate in candidates
        if not candidate.get("band")
    ]
    if missing_bands:
        run_id = (document.get("run") or {}).get("run_id") or "unknown"
        raise SystemExit(
            f"findings run {run_id} predates the recorded `band` field; refusing to select "
            f"{len(missing_bands)} candidate(s) using a later config. Produce a new findings run."
        )

    bands = cfg.require("triage", "bands")
    configured_bands = {item.get("key") for item in bands if item.get("key")}
    if band not in configured_bands:
        raise SystemExit(
            f"unknown backlog band {band!r}; choose one of {', '.join(sorted(configured_bands))}"
        )

    # Validate the retained historical selector name, but select current findings from the
    # attention recorded by the shared policy.
    head_selector(cfg.require("triage", "head", "select"))
    backlog = [
        candidate
        for candidate in candidates
        if triage_mod.guidance_of(candidate, revisit=revisit).attention
        == triage_mod.ATTENTION_GROUPED
        and owner_name(candidate) == owner
    ]
    by_asset: Dict[tuple, List[dict]] = {}
    for candidate in backlog:
        key = (candidate.get("asset_type") or "", candidate.get("asset_id") or "")
        by_asset.setdefault(key, []).append(candidate)
    selected = [
        candidate
        for records in by_asset.values()
        if triage_mod.asset_band(records, bands) == band
        for candidate in records
    ]
    if not revisit:
        selected = [
            candidate
            for candidate in selected
            if not existing.has_active_state(candidate.get("candidate_id"))
        ]
    selected.sort(
        key=lambda candidate: (
            owner_name(candidate).casefold(),
            str(candidate.get("asset_name") or "").casefold(),
            candidate.get("asset_type") or "",
            candidate.get("asset_id") or "",
            candidate.get("candidate_id") or "",
        )
    )
    return selected


def _twin_parts(evidence: dict) -> List[str]:
    """The twin, by name and by link, from the twin block ticket 44 put on every duplicated
    candidate. Falls back to the bare ids when a findings file predates the block — a recorded
    artifact is never rewritten, and an older file must still review."""
    details = evidence.get("duplicate_twin_detail") or []
    parts: List[str] = []
    if details:
        for detail in details:
            name = detail.get("asset_name") or detail.get("asset_id")
            link = detail.get("deep_link") or "no deep link recorded"
            parts.append(f"twin: {name} ({detail.get('asset_id')}) · {link}")
    else:
        twins = evidence.get("duplicate_twins") or []
        parts.append(f"twins: {', '.join(twins) if twins else 'none recorded'}")
    if evidence.get("duplicate_group_size") is not None:
        parts.append(f"group of {evidence['duplicate_group_size']}")
    if evidence.get("duplicate_group_shared_inputs") is not None:
        parts.append(f"{evidence['duplicate_group_shared_inputs']} shared input(s)")
    if evidence.get("duplicate_cost_comparison"):
        parts.append(evidence["duplicate_cost_comparison"])
    realisable = evidence.get("duplicate_group_realisable") or {}
    if realisable.get("recurring") is not None:
        unit = (realisable.get("recurring_unit") or "").replace("_per_month", "")
        parts.append(
            f"set realisable {realisable['recurring']} {unit}/mo by the sum-minus-max "
            f"convention (an aggregate convention, not a recommendation)"
        )
    return parts


def tag_evidence_text(candidate: dict) -> str:
    """Raw tag evidence for a report/checklist cell, without implying a verdict.

    A dataflow owns no tags on the readable product surface, so every displayed tag stays
    labelled with its output dataset id. Empty means explicitly untagged; unavailable is
    named rather than silently rendered as empty.
    """
    evidence = candidate.get("evidence") or {}
    if candidate.get("asset_type") == "dataset":
        tags = evidence.get("tags")
        if tags is None:
            return "dataset tags unavailable"
        return ", ".join(tags) if tags else ""

    outputs = evidence.get("output_tags")
    if outputs is None:
        return "output dataset tags unavailable"
    tagged = [
        f"{entry.get('dataset_id')}: {', '.join(entry.get('tags') or [])}"
        for entry in outputs
        if entry.get("tags")
    ]
    return "; ".join(tagged)


def _evidence_line(candidate: dict) -> str:
    """The evidence that triggered the finding — enough for a decision without opening
    another file."""
    evidence = candidate.get("evidence") or {}
    consumers = candidate.get("consumers") or {}
    cost = candidate.get("cost") or {}
    parts: List[str] = []
    condition = candidate.get("condition")
    if condition == "stale" and evidence.get("days_stale") is not None:
        # The ATTEMPT. Named as one, because `Last_Run_Date` is when the connector last
        # ran, not when data last arrived (ticket 43).
        parts.append(f"{evidence['days_stale']}d since last run attempt")
    # The LANDING, a second fact that is never folded into the first. Datasets only — a
    # dataflow carries no `landing_history`, its outputs do. `never` with a real attempt is
    # the sentence the old line hid: "last run 2022-09-07" about a dataset that has never
    # held a row.
    landing = evidence.get("landing_history")
    if landing == "never":
        parts.append("data never landed")
    elif landing == "observed" and evidence.get("last_data_at"):
        parts.append(f"data last landed {str(evidence['last_data_at'])[:10]}")
    elif landing == "unknown":
        parts.append("landing unknown (not described by the dataset enumeration)")
    if condition == "unconsumed":
        cards = consumers.get("cards")
        downstream = consumers.get("downstream_dataflows")
        parts.append(
            f"cards={cards if cards is not None else '?'} "
            f"downstream_dataflows={downstream if downstream is not None else '?'}"
        )
        reachable = consumers.get("pages")
        if reachable is not None:
            parts.append(f"pages={reachable}")
        # Ticket 65. A publication with a live subscriber removes the candidate; one with
        # none is a fact the person quarantining should see, because it is a second thing to
        # retire and a reason someone once expected a reader.
        unsubscribed = evidence.get("published_without_subscribers")
        if unsubscribed:
            parts.append(
                f"published to no subscriber in {len(unsubscribed)} publication(s): "
                + ", ".join(unsubscribed)
            )
    if condition == "failing":
        parts.append(f"last attempt: {evidence.get('last_execution_status') or 'error'}")
    if condition == "duplicated":
        # The `duplicated` branch (ticket 44). The action is `decide-which-survives`, which
        # asks a human to COMPARE two objects — so the line names the twin, hands over its
        # link, and reads out the cost sentence, which states equal figures as equal and a
        # difference as a difference. It never names a survivor: that is a business judgment
        # (ticket 25 decision 9), and this line is the most quoted text in the artifact.
        parts.extend(_twin_parts(evidence))
    for relationship in evidence.get("tag_relationships") or []:
        parts.append(
            f"tag {relationship.get('relationship')}: {relationship.get('detail')}"
        )
    parts.append(f"run_history={evidence.get('run_history')}")
    if evidence.get("run_state"):
        parts.append(f"run_state={evidence['run_state']}")
    if evidence.get("outputs_observed") is not None:
        parts.append(f"outputs={evidence['outputs_observed']}")
    # The unit is read off the record, never assumed: this same line renders credits on
    # granite and executions on a subsidiary, and a verdict evidence string is the most
    # quoted text in the whole artifact.
    recurring = cost.get("recurring")
    if recurring is not None:
        unit = (cost.get("recurring_unit") or "").replace("_per_month", "")
        parts.append(f"{recurring} {unit}/mo" if unit else f"{recurring}/mo")
    elif cost.get("basis") == "none":
        parts.append("cost not measured")
    return " · ".join(parts)


def review(
    document: dict,
    cfg: config_mod.Config,
    by: str,
    condition_filter: Optional[str],
    limit: Optional[int],
    revisit: bool,
    existing: ledger_mod.Ledger,
) -> List[ledger_mod.Entry]:
    run_id = (document.get("run") or {}).get("run_id")
    collector_version = (document.get("run") or {}).get("collector_version")
    cycles = int(cfg.get("ledger", "keep_for_now_cycles", default=3))
    cycle_days = int(cfg.get("ledger", "cycle_days", default=30))
    quarantine_days = int(cfg.get("ledger", "quarantine_period_days", default=90))

    # The individual verdict surface is the ranked HEAD, not every actionable finding. The
    # backlog is an owner conversation rendered by `collector.report`; ticket 45 exists
    # because walking all 2,700 candidates here made the configured split a print statement.
    # Resolve membership independently of suppression so `--revisit` can intentionally put a
    # previously judged head candidate back in front of the reviewer.
    head_selector(cfg.require("triage", "head", "select"))
    candidates = [
        candidate
        for candidate in document.get("candidates") or []
        if triage_mod.guidance_of(candidate, revisit=revisit).attention
        == triage_mod.ATTENTION_INDIVIDUAL
    ]
    candidates.sort(
        key=lambda c: (
            c.get("rank") is None,
            c.get("rank") or 0,
            c.get("candidate_id") or "",
        )
    )
    if condition_filter:
        candidates = [c for c in candidates if c.get("condition") == condition_filter]
    if not revisit:
        candidates = [
            c for c in candidates if not existing.has_active_state(c.get("candidate_id"))
        ]
    if limit:
        candidates = candidates[:limit]

    if not candidates:
        print("nothing to review (use --revisit to revisit already-judged candidates)")
        return []

    entries: List[ledger_mod.Entry] = []
    total = len(candidates)
    for index, candidate in enumerate(candidates, start=1):
        print()
        print(f"[{index}/{total}] rank {candidate.get('rank')}  {candidate.get('condition')}")
        print(f"  {candidate.get('asset_type')}  {candidate.get('asset_name')}")
        print(f"  {candidate.get('candidate_id')}")
        owner = candidate.get("owner") or {}
        if owner.get("name"):
            print(f"  owner: {owner.get('name')}")
        print(f"  {_evidence_line(candidate)}")
        link = candidate.get("deep_link")
        # Datasets carry the report's `Link`; dataflows carry a CONSTRUCTED route (ticket 35,
        # built by ticket 44) when both identifier guards hold. An absence is stated rather
        # than shown as a blank line the reviewer might read as a broken link, and it is
        # never "this asset type has none": `coverage.deep_links` says which rows were
        # withheld and why.
        print(f"  {link}" if link else "  (no deep link recorded for this asset — see coverage.deep_links)")

        while True:
            try:
                choice = input(PROMPT).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nstopping; nothing further appended")
                return entries
            if choice in ("x", "exit"):
                return entries
            if choice in ("s", ""):
                break
            if choice not in KEYS:
                print("  unrecognised")
                continue
            axis, value = KEYS[choice]
            reason = None
            if axis == "verdict" and value in ("keep-permanent", "keep-for-now", "revoked"):
                reason = input("  reason (required): ").strip()
                if not reason:
                    print("  a reason is required for a verdict — it is the only part of "
                          "the ledger a human cannot reconstruct")
                    continue
            entries.append(
                ledger_mod.make_entry(
                    candidate_id=candidate["candidate_id"],
                    axis=axis,
                    value=value,
                    by=by,
                    reason=reason,
                    run_id=run_id,
                    asset_name=candidate.get("asset_name"),
                    collector_version=collector_version,
                    keep_for_now_cycles=cycles,
                    cycle_days=cycle_days,
                    quarantine_days=quarantine_days,
                )
            )
            print(f"  recorded {axis}={value}")
            break
    return entries


def parse_set(
    assignments: List[str],
    reason: Optional[str],
    by: str,
    cfg: config_mod.Config,
    names: Dict[str, str],
    run_id: Optional[str],
) -> List[ledger_mod.Entry]:
    """`--set <candidate_id>=<value>` for non-interactive use.

    This is the path ticket 11's deliberate-break test drives, so it accepts a
    `candidate_id` that matches no current candidate: writing a verdict for an asset the
    run never saw is precisely the case reconciliation must classify as
    `unmatched-verdict` rather than crash on.
    """
    cycles = int(cfg.get("ledger", "keep_for_now_cycles", default=3))
    cycle_days = int(cfg.get("ledger", "cycle_days", default=30))
    quarantine_days = int(cfg.get("ledger", "quarantine_period_days", default=90))
    entries: List[ledger_mod.Entry] = []
    for assignment in assignments:
        candidate_id, _, value = assignment.partition("=")
        candidate_id = candidate_id.strip()
        value = value.strip()
        if not candidate_id or not value:
            raise SystemExit(f"--set expects <candidate_id>=<value>, got {assignment!r}")
        if value in ledger_mod.VERDICT_VALUES:
            axis = "verdict"
        elif value in ledger_mod.LIFECYCLE_VALUES:
            axis = "lifecycle"
        elif value in ledger_mod.RECORD_VALUES:
            axis = "record"
        else:
            raise SystemExit(
                f"{value!r} is neither a verdict {ledger_mod.VERDICT_VALUES} nor a "
                f"lifecycle {ledger_mod.LIFECYCLE_VALUES} nor a record correction "
                f"{ledger_mod.RECORD_VALUES}"
            )
        entries.append(
            ledger_mod.make_entry(
                candidate_id=candidate_id,
                axis=axis,
                value=value,
                by=by,
                reason=reason,
                run_id=run_id,
                asset_name=names.get(candidate_id),
                collector_version=None,
                keep_for_now_cycles=cycles,
                cycle_days=cycle_days,
                quarantine_days=quarantine_days,
            )
        )
    return entries


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.verdict",
        description="Append verdicts to the instance ledger. Appends only; never rewrites.",
    )
    parser.add_argument("--instance", required=True)
    parser.add_argument("--run", default=None, help="run_id to review (default: newest)")
    parser.add_argument("--by", default=None, help="defaults to git config user.email")
    parser.add_argument("--condition", default=None, help="only this condition")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--revisit",
        action="store_true",
        help="include candidates that already carry a verdict",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="CANDIDATE_ID=VALUE",
        help="non-interactive: record one verdict. Repeatable.",
    )
    parser.add_argument("--reason", default=None, help="reason for --set verdicts")
    parser.add_argument("--dry-run", action="store_true", help="print lines, append nothing")
    args = parser.parse_args(argv)

    cfg = config_mod.load(args.instance)
    shared = appdb_mod.open_store(cfg) if appdb_mod.configured(cfg) else None
    base = shared.head() if shared else None
    if shared and not base['latest']:
        raise appdb_mod.StorageError('Publish a shared run before making decisions.')
    by = shared.actor()['email'] if shared else resolve_by(args.by, cfg.repo_root)
    existing = shared.ledger(base) if shared else ledger_mod.Ledger(
        cfg.ledger_path,
        cycle_days=int(cfg.get("ledger", "cycle_days", default=30)),
        quarantine_days=int(cfg.get("ledger", "quarantine_days", default=90)),
    ).load()

    if args.set:
        names: Dict[str, str] = {}
        run_id = args.run
        try:
            document = shared.run(args.run, head=base) if shared else load_findings(cfg.runs_dir, args.run)
            run_id = (document.get("run") or {}).get("run_id")
            names = {
                c["candidate_id"]: c.get("asset_name", "")
                for c in document.get("candidates") or []
                if c.get("candidate_id")
            }
        except SystemExit:
            # A --set against no findings file at all is still legitimate: the ledger is
            # the durable artifact and can be seeded before or after a run.
            pass
        entries = parse_set(args.set, args.reason, by, cfg, names, run_id)
        for value in (e.value for e in entries):
            if value in ("keep-permanent", "keep-for-now") and not args.reason:
                raise SystemExit(f"--set {value} requires --reason")
    else:
        document = shared.run(args.run, head=base) if shared else load_findings(cfg.runs_dir, args.run)
        entries = review(
            document, cfg, by, args.condition, args.limit, args.revisit, existing
        )

    if not entries:
        print("nothing appended")
        return 0

    if args.dry_run:
        for entry in entries:
            print(entry.as_line())
        print(f"\n--dry-run: {len(entries)} line(s) NOT written to {cfg.ledger_path}")
        return 0

    if shared:
        document = shared.run(args.run, head=base)
        if not document:
            raise appdb_mod.StorageError("Publish a shared run before making decisions.")
        shared.decide(entries, document['run']['run_id'], base, str(uuid.uuid4()))
        print(f"Recorded {len(entries)} shared decisions.")
        return 0
    written = ledger_mod.append(cfg.ledger_path, entries)
    print(f"\nappended {written} line(s) to {cfg.ledger_path}")
    print("Committing to git stays a human act.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
