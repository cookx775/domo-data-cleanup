"""Render the findings artifact into deterministic Markdown reports.

The JSON findings file remains the artifact of record. This module is a pure projection of it:
one renderer serves the master report and each per-owner supplement, and no Domo read or ledger
write happens here. Reports are emitted only when an administrator asks for the backlog route.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import re
import shlex
import sys
import unicodedata
from collections import defaultdict
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from . import config as config_mod
from . import glossary as glossary_mod
from . import workload as workload_mod
from . import triage as triage_mod
from . import verdict as verdict_mod
from .annotate import head_selector


CHANGE_ORDER = (
    "new",
    "returned",
    "still_open",
    "resolved",
    "removed",
    "retired",
    "disappeared",
    "unverifiable",
    "suppressed",
)
CONDITION_ORDER = ("unconsumed", "stale", "failing", "duplicated")

#: One label per change count, carrying its qualification after an em dash where it has one.
#: The Markdown digest prints the whole string; the HTML digest sets the part before the dash
#: as the row's name and the part after it as the definition beneath.  Keeping it as ONE string
#: is deliberate: two fields would let the two renderers describe the same count differently.
CHANGE_LABELS = {
    "new": "New",
    "returned": "Returned",
    "still_open": "Still open",
    "resolved": "Resolved — positively observed and no longer meeting the condition",
    "removed": "Removed — absent from the complete current presence source",
    "retired": "Retired — removal agrees with an approved, quarantined, or deleted record",
    "disappeared": "Disappeared — absent from a healthy source; reported, not celebrated",
    "unverifiable": "Unverifiable — absence under degraded coverage proves nothing",
    "suppressed": "Suppressed by a prior verdict or lifecycle record",
}

#: The three pillar and condition states, in administrator language rather than the enum.
COVERAGE_STATUS = {"ok": "ran clean", "partial": "degraded", "blocked": "did not run"}


def change_label_parts(name: str) -> Tuple[str, Optional[str]]:
    """Split one :data:`CHANGE_LABELS` entry into (name, qualification).

    A label with no em dash has no qualification to split off, so it is returned whole and the
    HTML digest renders a single line for it.
    """
    head, _, tail = CHANGE_LABELS[name].partition(" — ")
    return head, tail or None


def change_count_rows(document: dict) -> List[Tuple[str, int]]:
    """(change name, count) in :data:`CHANGE_ORDER`, read off the stored reconciliation."""
    counts = ((document.get("reconciliation") or {}).get("counts")) or {}
    return [(name, int(counts.get(name, 0))) for name in CHANGE_ORDER]


def coverage_pillar_rows(document: dict) -> List[Tuple[str, str, str, Optional[str]]]:
    """(pillar, status in administrator language, assets examined, coverage-exception detail)."""
    rows = []
    for pillar in (document.get("coverage") or {}).get("pillars") or []:
        status = COVERAGE_STATUS.get(pillar.get("status"), _text(pillar.get("status")))
        detail = (pillar.get("exception") or {}).get("detail")
        rows.append((
            _text(pillar.get("pillar")),
            status,
            _text(pillar.get("assets_examined")),
            _text(detail) if detail else None,
        ))
    return rows


def coverage_condition_rows(document: dict) -> List[Tuple[str, str, Optional[str], int]]:
    """(condition, status, what degraded it, current candidate count) in :data:`CONDITION_ORDER`.

    `_condition_status` carries a degradation's cause in the same string as its status, which
    the Markdown digest prints whole.  Splitting it here lets the HTML digest present the bare
    status as a chip: a compound string matches no known status and would be styled as though
    the condition had not run at all, which understates a degraded one.
    """
    rows = []
    for condition in CONDITION_ORDER:
        status, current = _condition_status(document, condition)
        bare, _, cause = status.partition(" — ")
        rows.append((condition, bare, cause or None, current))
    return rows


def watch_rows(document: dict) -> List[dict]:
    """One row per quarantined asset waiting out its period, with the elapsed arithmetic done.

    `elapsed` and `period` are days against the run's own `as_of_date`, so a stored findings
    file always reports the progress it measured rather than progress since.  Elapsed time is
    the only signal here: it is never evidence that nobody complained.
    """
    watch = document.get("watch_list") or []
    if not watch:
        return []
    as_of = _dt.date.fromisoformat((document.get("run") or {}).get("as_of_date"))
    rows = []
    for entry in watch:
        quarantined = _dt.date.fromisoformat(str(entry.get("quarantined_at"))[:10])
        eligible = _dt.date.fromisoformat(entry.get("delete_eligible_after"))
        rows.append(
            {
                "asset_name": _text(entry.get("asset_name")),
                "candidate_id": _text(entry.get("candidate_id")),
                "quarantined": quarantined.isoformat(),
                "elapsed": max(0, (as_of - quarantined).days),
                "period": max(0, (eligible - quarantined).days),
            }
        )
    return rows


def _text(value) -> str:
    """One Markdown-safe line. Source names remain recognizable without becoming markup."""
    if value is None or value == "":
        return "unknown"
    return " ".join(str(value).replace("`", "'").split())


def _code(value) -> str:
    return f"`{_text(value)}`"


def _unit(value: Optional[str]) -> str:
    return {
        "credits_per_month": "credits/month",
        "executions_per_month": "executions/month",
        "rows_under_management": "rows under management",
    }.get(value, _text(value) if value else "not measured")


def _number(value) -> str:
    if value is None:
        return "not measured"
    decimal = Decimal(str(value))
    if decimal == decimal.to_integral():
        return str(decimal.quantize(Decimal("1")))
    return format(decimal.normalize(), "f")


def _asset_key(candidate: dict) -> Tuple[str, str]:
    return candidate.get("asset_type") or "", candidate.get("asset_id") or ""


def _owner(candidate: dict) -> str:
    return verdict_mod.owner_name(candidate)


def _rank(candidate: dict) -> Optional[int]:
    value = candidate.get("rank")
    return value if isinstance(value, int) else None


def _require_recorded_bands(document: dict) -> None:
    missing = [
        candidate.get("candidate_id") or "<missing candidate id>"
        for candidate in document.get("candidates") or []
        if not candidate.get("band")
    ]
    if missing:
        run_id = (document.get("run") or {}).get("run_id") or "unknown"
        raise SystemExit(
            f"findings run {run_id} predates the recorded `band` field; refusing to render "
            f"{len(missing)} candidate(s) from a later config. Produce a new findings run so "
            "the report is a projection of its artifact of record."
        )


def _group_assets(candidates: Iterable[dict], bands: List[dict]) -> List[dict]:
    """Group one review selection to asset grain; candidates never repeat within it."""
    grouped: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for candidate in candidates:
        grouped[_asset_key(candidate)].append(candidate)

    rows: List[dict] = []
    for key, records in grouped.items():
        records.sort(
            key=lambda candidate: (
                _rank(candidate) is None,
                _rank(candidate) or 0,
                candidate.get("candidate_id") or "",
            )
        )
        ranks = [rank for rank in (_rank(candidate) for candidate in records) if rank is not None]
        link = next((candidate.get("deep_link") for candidate in records if candidate.get("deep_link")), None)
        rows.append(
            {
                "key": key,
                "asset_type": key[0],
                "asset_id": key[1],
                "asset_name": records[0].get("asset_name") or key[1],
                "deep_link": link,
                "owner": _owner(records[0]),
                "band": triage_mod.asset_band(records, bands),
                "best_rank": min(ranks) if ranks else None,
                "candidates": records,
            }
        )
    rows.sort(
        key=lambda row: (
            row["best_rank"] is None,
            row["best_rank"] or 0,
            row["asset_type"],
            row["asset_id"],
        )
    )
    return rows


def _asset_rows(
    document: dict, cfg: config_mod.Config, owner: Optional[str]
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Return individual, recorded-decision and grouped rows at asset grain.

    The split is candidate-grained: one asset may have a recurring ``unconsumed`` candidate in
    individual decisions and a zero-cost ``stale`` candidate in owner-grouped review. Grouping
    is inside each selection so candidate identity and its own guidance remain intact.
    """
    _require_recorded_bands(document)
    candidates = list(document.get("candidates") or [])
    if owner is not None:
        candidates = [candidate for candidate in candidates if _owner(candidate) == owner]
    # Retain config validation for historical artifacts; current artifacts record the shared
    # policy's attention output and every report consumes that rather than restating the rule.
    head_selector(cfg.require("triage", "head", "select"))
    individual = []
    recorded = []
    grouped = []
    for candidate in candidates:
        attention = triage_mod.guidance_of(candidate).attention
        if attention == triage_mod.ATTENTION_INDIVIDUAL:
            individual.append(candidate)
        elif attention == triage_mod.ATTENTION_NONE:
            recorded.append(candidate)
        else:
            grouped.append(candidate)
    bands = cfg.require("triage", "bands")
    return (
        _group_assets(individual, bands),
        _group_assets(recorded, bands),
        _group_assets(grouped, bands),
    )


def head_command(document: dict, cfg: config_mod.Config, row: dict) -> str:
    """The exact command that records a decision on one head asset — one `--set` per candidate,
    a placeholder verdict, a mandatory reason, and `--dry-run`, mirroring the backlog group
    command. Reading happens in this document; the terminal is only where a decision is
    written down. Granite rehearsal, 2026-09-02: a 130-asset head walked one prompt at a time
    with no report in hand is a loop nobody finishes."""
    run_id = (document.get("run") or {}).get("run_id")
    parts = ["python", "cleanup.py", "review", "--instance", cfg.instance_key, "--run", run_id]
    for candidate in row["candidates"]:
        parts.extend(["--set", f"{candidate['candidate_id']}=<verdict>"])
    parts.extend(["--reason", "<why>", "--by", "<you@example.com>", "--dry-run"])
    return " ".join(shlex.quote(str(part)) for part in parts)


def _entry_lines(row: dict, level: int) -> List[str]:
    title = _text(row["asset_name"])
    if row.get("deep_link"):
        title = f"[{title}]({row['deep_link']})"
    rank = f"rank {row['best_rank']} · " if row.get("best_rank") is not None else ""
    lines = [f"{'#' * level} {title}", ""]
    lines.append(
        f"- {rank}{_code(row['asset_type'])} {_code(row['asset_id'])} · owner "
        f"{_text(row['owner'])} · band {_code(row['band'])}"
    )
    candidate_ids = ", ".join(_code(c["candidate_id"]) for c in row["candidates"])
    lines.append(f"- Candidate IDs: {candidate_ids}")
    tag_text = verdict_mod.tag_evidence_text(row["candidates"][0])
    if tag_text:
        label = "Output dataset tags" if row["asset_type"] == "dataflow" else "Tags"
        lines.append(f"- {label}: {_text(tag_text)}")
    lines.append("- Conditions and evidence:")
    for candidate in row["candidates"]:
        suppressed = " · suppressed by a prior record" if (candidate.get("ledger") or {}).get("suppressed") else ""
        lines.append(
            f"  - **{_text(candidate.get('condition'))}** ({_code(candidate['candidate_id'])}) — "
            f"{_text(verdict_mod._evidence_line(candidate))}{suppressed}"
        )
        lines.append(f"    - **Guidance:** {_text(recommended_action_text(candidate))}")
        if (candidate.get("ledger") or {}).get("suppressed"):
            lines.append(
                f"    - **Recorded decision:** {_text(recorded_decision_text(candidate))}"
            )
            lines.append(f"    - **Recurring cost:** {_text(recurring_cost_text(candidate))}")
    return lines


def recommended_action_text(candidate: dict) -> str:
    """Render the shared policy's recorded action and the evidence basis beside it."""
    guidance = triage_mod.guidance_of(candidate)
    label = triage_mod.ACTION_METADATA[guidance.recommended_action]["label"]
    basis = guidance.recommended_action_basis
    cost = candidate.get("cost") or {}
    if basis == triage_mod.BASIS_RECURRING_POSITIVE:
        detail = (
            f"stops {_number(cost.get('recurring'))} "
            f"{_unit(cost.get('recurring_unit'))} of recurring usage"
        )
    elif basis == triage_mod.BASIS_RECURRING_ZERO:
        detail = "no recurring saving is measured"
    elif basis == triage_mod.BASIS_RECURRING_UNKNOWN:
        detail = "recurring cost was not measured"
    elif basis == triage_mod.BASIS_DUPLICATED:
        detail = "the evidence identifies twins but cannot choose a survivor"
    elif basis == triage_mod.BASIS_SUPPRESSED:
        detail = "a recorded decision currently covers this candidate"
    else:
        detail = f"the {_text(candidate.get('condition'))} finding needs a human remediation decision"
    return f"{label} — {detail}."


def recorded_decision_text(candidate: dict) -> str:
    """Name every folded decision axis and its reason without guessing which one won."""
    ledger = candidate.get("ledger") or {}
    decisions = []
    for axis in ("verdict", "lifecycle"):
        value = ledger.get(axis)
        if value:
            reason = ledger.get(f"{axis}_reason") or "reason not recorded"
            decisions.append(f"{axis} {_text(value)} — {_text(reason)}")
    return "; ".join(decisions) or "recorded decision details unavailable"


def recurring_cost_text(candidate: dict) -> str:
    """Render cost from the finding itself; unknown never collapses into measured zero."""
    cost = candidate.get("cost") or {}
    recurring = cost.get("recurring")
    if recurring is None:
        return "not measured"
    return f"{_number(recurring)} {_unit(cost.get('recurring_unit'))}"


def _change_lines(document: dict) -> List[str]:
    reconciliation = document.get("reconciliation") or {}
    lines = ["## Change since the prior run", ""]
    prior = (document.get("run") or {}).get("prior_run_id")
    lines.append(f"Prior run: {_code(prior) if prior else 'none — this is the baseline run'}.\n")
    for name, count in change_count_rows(document):
        lines.append(f"- **{CHANGE_LABELS[name]}:** {count}")

    returned = reconciliation.get("returned") or []
    if returned:
        lines.extend(["", "Returned candidates (with the verdict that stopped applying):", ""])
        for entry in returned:
            lines.append(
                f"- {_text(entry.get('asset_name'))} ({_code(entry.get('candidate_id'))}) — "
                f"{_text(entry.get('returned_from') or 'prior suppression stopped applying')}"
            )

    removed = reconciliation.get("removed") or []
    if removed:
        lines.extend(["", "Removed assets:", ""])
        for entry in removed:
            reason = (entry.get("removal_reason") or "removed").replace("-", " ")
            lines.append(
                f"- {_text(entry.get('asset_name'))} ({_code(entry.get('candidate_id'))}) — "
                f"{_text(reason)}"
            )

    retired = reconciliation.get("retired") or []
    if retired:
        lines.extend(["", "Retired this run:", ""])
        for entry in retired:
            lines.append(
                f"- {_text(entry.get('asset_name'))} ({_code(entry.get('candidate_id'))}) — "
                f"recorded {_code(entry.get('recorded_lifecycle'))}, now absent from the "
                "complete current presence source"
            )

    register = reconciliation.get("retirement_register") or []
    lines.extend(["", f"Retirement register: **{len(register)}** terminal candidate(s)."])
    for entry in register:
        lines.append(
            f"- {_text(entry.get('asset_name'))} ({_code(entry.get('candidate_id'))}) — "
            f"recorded {_code(entry.get('recorded_lifecycle'))}; terminal since "
            f"{_code(entry.get('state_first_seen_run_id'))}"
        )

    exceptions = reconciliation.get("lifecycle_exceptions") or []
    lines.extend(["", f"Lifecycle exceptions: **{len(exceptions)}**."])
    for entry in exceptions:
        lines.append(
            f"- {_text(entry.get('asset_name'))} ({_code(entry.get('candidate_id'))}) — "
            f"{_text(entry.get('detail'))}"
        )

    unmatched = reconciliation.get("unmatched_verdicts") or []
    lines.extend(["", f"Unmatched verdicts: **{len(unmatched)}**."])
    for entry in unmatched:
        lines.append(
            f"- {_code(entry.get('candidate_id'))}: {_text(entry.get('axis'))}="
            f"{_text(entry.get('value'))} for {_text(entry.get('asset_name_at_verdict'))}"
        )
    if unmatched:
        lines.append(
            "If the candidate identity was mistyped, append a reasoned `withdrawn` correction; "
            "if it was real, investigate an identifier change rather than withdrawing it."
        )

    malformed = ((document.get("coverage") or {}).get("ledger") or {}).get("malformed") or []
    lines.extend(["", f"Rejected ledger lines: **{len(malformed)}**."])
    for entry in malformed:
        subject = entry.get("candidate_id") or entry.get("text") or "unreadable line"
        lines.append(
            f"- Line **{entry.get('line')}** ({_code(subject)}): {_text(entry.get('error'))}"
        )
    return lines


def _condition_status(document: dict, condition: str) -> Tuple[str, int]:
    coverage = document.get("coverage") or {}
    population = ((document.get("reconciliation") or {}).get("population") or {}).get(condition) or {}
    current = int(population.get("current", (document.get("summary") or {}).get("by_condition", {}).get(condition, 0)))
    if condition == "unconsumed":
        consumption = coverage.get("consumption") or {}
        if consumption.get("signal") != "present":
            return "did not run", current
        unknown = int((consumption.get("adjudication") or {}).get("unknown") or 0)
        return (f"degraded — {unknown} unknown" if unknown else "ran clean"), current
    if condition == "duplicated":
        duplication = coverage.get("duplication") or {}
        if not duplication.get("available"):
            return "did not run", current
        unknown = len(duplication.get("unknown_outputs") or []) + len(duplication.get("unknown_dataflows") or [])
        return (f"degraded — {unknown} unknown across both grains" if unknown else "ran clean"), current
    return ("did not run" if population.get("condition_unavailable") else "ran clean"), current


def _placement_floor(document: dict) -> Tuple[int, int]:
    by_dataset: Dict[str, int] = {}
    for candidate in document.get("candidates") or []:
        if candidate.get("asset_type") != "dataset":
            continue
        value = (candidate.get("consumers") or {}).get("cards_with_no_page_recorded")
        if value is not None:
            by_dataset[candidate.get("asset_id") or ""] = int(value)
    positive = {dataset_id: value for dataset_id, value in by_dataset.items() if value > 0}
    return sum(positive.values()), len(positive)


def _coverage_lines(document: dict) -> List[str]:
    lines = ["## Coverage digest", "", "Pillars:", ""]
    for pillar, status, examined, detail in coverage_pillar_rows(document):
        suffix = f" — {detail}" if detail else ""
        lines.append(f"- **{pillar}:** {status}; assets examined: {examined}{suffix}")

    lines.extend(["", "Conditions:", ""])
    for condition, status, cause, current in coverage_condition_rows(document):
        qualified = f"{status} — {cause}" if cause else status
        lines.append(f"- **{condition}:** {qualified}; {current} candidate(s)")

    for label, prose in coverage_qualification_rows(document):
        lines.append(f"- **{label}:** {prose}")
    lines.extend(
        [
            "",
            "The full per-asset coverage arrays remain in the stored findings; this digest never reproduces them.",
        ]
    )
    return lines


def coverage_qualification_rows(document: dict) -> List[Tuple[str, str]]:
    """(label, prose) for the four qualifications that follow the condition list.

    These are sentences rather than figures, so both renderers take the prose verbatim from
    here; only the label is presented differently.
    """
    coverage = document.get("coverage") or {}
    rows: List[Tuple[str, str]] = []
    consumption = coverage.get("consumption") or {}
    adjudication = consumption.get("adjudication") or {}
    count = int(adjudication.get("adjudicated") or 0)
    movement = consumption.get("workload_change")
    count = len(set(consumption["contested"])) if isinstance(consumption.get("contested"), list) else count
    rows.append(("Lineage adjudication",
                 f"{count} dataset(s), {workload_mod.describe(movement)}."))

    # Ticket 65. The fifth consumer type gets its own line, because it is the one a reader
    # of a publisher's report will otherwise miss: a dataset nothing on THIS instance reads.
    publications = consumption.get("publications")
    if publications is None:
        rows.append((
            "Publication consumers",
            "not checked in this older findings file. A dataset "
            "published to another instance may be listed as unconsumed.",
        ))
    elif publications.get("signal") != "present":
        rows.append((
            "Publication consumers",
            f"the publish API was not read, so `unconsumed` did "
            f"not run — {_text(publications.get('detail') or publications.get('signal'))}.",
        ))
    else:
        pc = publications.get("counts") or {}
        rows.append((
            "Publication consumers",
            f"{_text(pc.get('publications'))} publication(s) read "
            f"in {_text(publications.get('calls'))} request(s); "
            f"{_text(pc.get('published_datasets'))} dataset(s) published, "
            f"{_text(pc.get('published_datasets_with_live_subscriber'))} to at least one "
            f"subscribing instance. {_text(pc.get('candidate_datasets_removed'))} dataset(s) "
            f"with no card on this instance are consumed only through publication and are "
            f"not candidates.",
        ))

    counts = consumption.get("counts") or {}
    landing_unknown = int(counts.get("datasets_without_landing_field") or 0)
    dup = coverage.get("duplication") or {}
    dup_output_unknown = len(dup.get("unknown_outputs") or [])
    dup_flow_unknown = len(dup.get("unknown_dataflows") or [])
    lineage_unknown = int(adjudication.get("unknown") or len(consumption.get("unreachable") or []))
    rows.append((
        "Unknowns",
        f"landing field {landing_unknown}; lineage adjudication {lineage_unknown}; "
        f"duplicate outputs {dup_output_unknown}; duplicate dataflows {dup_flow_unknown}.",
    ))

    cards, datasets = _placement_floor(document)
    rows.append((
        "Placement floor",
        f"at least {cards} card(s) across {datasets} candidate dataset(s) "
        "have no page recorded. This is a floor on unplaced cards, never proof that the "
        "remaining cards are placed.",
    ))
    return rows


def _head_lines(document: dict, cfg: config_mod.Config, rows: List[dict]) -> List[str]:
    candidate_count = sum(len(row["candidates"]) for row in rows)
    lines = [
        "## Individual decisions",
        "",
        f"{len(rows)} asset(s), carrying {candidate_count} finding(s). Worked one asset at a time. "
        "Read the entry, decide, then paste its command with a verdict and a reason filled in; "
        "`--dry-run` prints the ledger line(s) without writing them. Verdicts: `keep-permanent`, "
        "`keep-for-now`, `revoked`. Lifecycle: `approved`, `quarantined`, `restored`, `deleted`.",
    ]
    if not rows:
        return lines + ["", "No assets require individual decisions in this selection."]
    for row in rows:
        lines.extend([""] + _entry_lines(row, 3))
        lines.extend(["", "```bash", head_command(document, cfg, row), "```"])
    return lines


def _recorded_lines(document: dict, cfg: config_mod.Config, rows: List[dict]) -> List[str]:
    lines = [
        "## Decisions already recorded",
        "",
        "Suppressed candidates remain visible with their decision, reason and cost, but belong "
        "to neither active work queue.",
    ]
    if not rows:
        return lines + ["", "No candidates are covered by a recorded decision in this selection."]
    for row in rows:
        lines.extend([""] + _entry_lines(row, 3))
        lines.extend(
            [
                "",
                "To revisit this decision, use the browser controls or preview replacement "
                "ledger lines explicitly:",
                "",
                "```bash",
                head_command(document, cfg, row),
                "```",
            ]
        )
    return lines


def _watch_lines(document: dict, owner: Optional[str]) -> List[str]:
    lines = ["## Watch list", ""]
    if owner is not None:
        lines.append(
            "Owner supplements do not repeat the watch list: watch-list records carry no owner, "
            "so filtering them would invent an attribution. The master report carries it."
        )
        return lines
    rows = watch_rows(document)
    if not rows:
        lines.append("No assets are waiting out a quarantine period.")
        return lines
    for row in rows:
        lines.extend(
            [
                f"### {row['asset_name']}",
                "",
                f"- {_code(row['candidate_id'])} · quarantined {row['quarantined']} · "
                f"**{row['elapsed']} / {row['period']} days elapsed**",
                "- The collector has no complaint signal and reports elapsed time only. "
                "Deletion eligibility is not evidence that nobody complained.",
            ]
        )
    return lines


def _oldest_key(row: dict) -> tuple:
    days = [
        (candidate.get("evidence") or {}).get("days_stale")
        for candidate in row["candidates"]
        if (candidate.get("evidence") or {}).get("days_stale") is not None
    ]
    oldest = max(days) if days else None
    return (oldest is None, -(oldest or 0), row["asset_type"], row["asset_id"])


def _storage_total(rows: Iterable[dict]) -> str:
    totals: Dict[Optional[str], Decimal] = defaultdict(Decimal)
    measured = False
    for row in rows:
        # Every condition on one asset carries the same asset cost. Count the asset once.
        cost = (row["candidates"][0].get("cost") or {})
        value = cost.get("additional_if_deleted")
        if value is None:
            continue
        measured = True
        totals[cost.get("additional_if_deleted_unit")] += Decimal(str(value))
    if not measured:
        return "not measured"
    return ", ".join(
        f"{_number(value)} {_unit(unit)}" for unit, value in sorted(totals.items(), key=lambda item: str(item[0]))
    )


def group_command(document: dict, cfg: config_mod.Config, owner: str, band: str) -> str:
    run_id = (document.get("run") or {}).get("run_id")
    parts = [
        "python", "cleanup.py", "review", "--instance", cfg.instance_key, "--run", run_id,
        "--backlog", "--owner", owner, "--band", band, "--verdict", "<verdict>",
        "--reason", "<shared reason>", "--dry-run",
    ]
    return " ".join(shlex.quote(str(part)) for part in parts)


def _backlog_lines(document: dict, cfg: config_mod.Config, rows: List[dict]) -> List[str]:
    groups: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["owner"], row["band"])].append(row)
    band_order = {
        band["key"]: index for index, band in enumerate(cfg.require("triage", "bands"))
    }
    ordered = sorted(
        groups,
        key=lambda key: (key[0].casefold(), band_order.get(key[1], len(band_order)), key[1]),
    )

    lines = ["## Owner-grouped review", "", f"{len(rows)} asset(s) in {len(ordered)} owner × band group(s)."]
    if not rows:
        return lines + ["", "No backlog assets are in this selection."]

    for owner, band in ordered:
        group = sorted(groups[(owner, band)], key=_oldest_key)
        candidate_count = sum(len(row["candidates"]) for row in group)
        conditions = sorted(
            {candidate.get("condition") for row in group for candidate in row["candidates"] if candidate.get("condition")}
        )
        lines.extend(
            [
                "",
                f"### {_text(owner)} — {_code(band)}",
                "",
                f"- **{len(group)} asset(s)** · **{candidate_count} candidate(s)** · conditions: "
                + ", ".join(_code(condition) for condition in conditions),
                f"- Summed storage, one figure per asset: **{_storage_total(group)}**. "
                "This is a displayed group figure, not the deduplicated instance opportunity.",
            ]
        )

        if len(group) <= 2:
            lines.extend(["", f"<details><summary>And {len(group)} other asset(s) — full list</summary>", ""])
        else:
            lines.extend(["", "Oldest asset sample (up to ten):", ""])
            for row in group[:10]:
                days = [
                    (candidate.get("evidence") or {}).get("days_stale")
                    for candidate in row["candidates"]
                    if (candidate.get("evidence") or {}).get("days_stale") is not None
                ]
                age = f" — {max(days)} days since last run attempt" if days else " — never run"
                lines.append(f"- {_text(row['asset_name'])} ({_code(row['asset_id'])}){age}")
            if len(group) > 10:
                lines.append(f"- … and {len(group) - 10} others; expand the full list below.")
            lines.extend(["", f"<details><summary>Full asset list ({len(group)})</summary>", ""])

        for row in group:
            lines.extend(_entry_lines(row, 4) + [""])
        lines.extend(["</details>", "", "Preview this exact group before writing ledger lines:", "", "```bash"])
        lines.append(group_command(document, cfg, owner, band))
        lines.extend(["```", "", "Remove `--dry-run` only after reviewing the complete expansion."])
    return lines


def render(document: dict, cfg: config_mod.Config, owner: Optional[str] = None) -> str:
    """Render one selection. ``owner=None`` is the master; a name is its supplement."""
    individual_rows, recorded_rows, grouped_rows = _asset_rows(document, cfg, owner)
    run = document.get("run") or {}
    title = f"Cleanup report — {_text(run.get('instance'))}"
    if owner is not None:
        title += f" — {_text(owner)}"
    lines = [
        f"# {title}",
        "",
        f"Run {_code(run.get('run_id'))} · as of {_code(run.get('as_of_date'))} · "
        f"collector {_code(run.get('collector_version'))} · config {_code(run.get('config_version'))}",
        "",
        "This Markdown is a deterministic view of the findings JSON; the JSON remains the artifact of record.",
        "",
    ]
    for section in (
        _change_lines(document),
        _coverage_lines(document),
        _head_lines(document, cfg, individual_rows),
        _watch_lines(document, owner),
        _recorded_lines(document, cfg, recorded_rows),
        _backlog_lines(document, cfg, grouped_rows),
        # Last, on every selection: an owner supplement is the document that gets handed to
        # someone, and that someone has never read CONTEXT.md.
        glossary_mod.lines(cfg, document),
    ):
        lines.extend(section + [""])
    return "\n".join(lines).rstrip() + "\n"


def _slug(owner: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", owner).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-") or "unassigned"
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def reports_dir(cfg: config_mod.Config, run_id: str) -> str:
    return os.path.join(cfg.instance_dir, "cleanup", "reports", run_id)


def write_reports(cfg: config_mod.Config, document: dict, out_dir: Optional[str] = None) -> List[str]:
    """Write the HTML decision surface, master Markdown, and owner supplements."""
    if cfg.require("reporting", "report_document") != "markdown":
        raise SystemExit("reporting.report_document must be `markdown` for this renderer")
    _require_recorded_bands(document)
    run_id = (document.get("run") or {}).get("run_id")
    if not run_id:
        raise SystemExit("findings file carries no run.run_id; refusing to name its report directory")
    out_dir = out_dir or reports_dir(cfg, run_id)
    os.makedirs(out_dir, exist_ok=True)

    # Import here because report_html deliberately consumes this module's authoritative
    # `_asset_rows` selection.  Keeping one direction at call time avoids two renderers
    # independently deciding which candidates belong in the report.
    from . import report_html

    html_path = os.path.join(out_dir, "summary.html")
    with open(html_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(report_html.render(document, cfg))

    selections = [("master.md", None)]
    owners = sorted({_owner(candidate) for candidate in document.get("candidates") or []}, key=lambda value: (value.casefold(), value))
    selections.extend((f"owner-{_slug(owner)}.md", owner) for owner in owners)
    written: List[str] = [html_path]
    for filename, owner in selections:
        path = os.path.join(out_dir, filename)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(render(document, cfg, owner=owner))
        written.append(path)
    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.report",
        description="Render a findings file into a master report and per-owner supplements.",
    )
    parser.add_argument("--instance", required=True)
    parser.add_argument("--run", default=None, help="run_id to render (default: newest)")
    args = parser.parse_args(argv)
    cfg = config_mod.load(args.instance)
    document = verdict_mod.load_findings(cfg.runs_dir, args.run)
    written = write_reports(cfg, document)
    print(f"wrote {len(written)} report document(s) to {os.path.dirname(written[0])}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
