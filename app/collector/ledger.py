"""The verdict ledger: an append-only event log of human judgement.

Everything the collector needs about suppression is **folded out of this file at read
time**. Nothing about suppression is stored twice, and no run ever rewrites the file.

**This module reads and folds. It never writes.** Only `collector.verdict` appends.

Two decisions here are load-bearing and easy to get wrong:

**File order wins, never the `at` timestamp.** An append-only file is chronological by
construction, and a hand-typed or clock-skewed timestamp must not be able to reorder
history. `at` is recorded as data.

**A cycle is an absolute DATE, not a decrementing counter.** With `cadence.mode:
on_demand`, three runs in one afternoon would have burned a whole three-cycle grant. The
CLI stamps `suppress_until = at + cycles x cycle_days` at verdict time and stores it, so
re-running is idempotent — which is the determinism guarantee ADR-0001 requires — and
changing `keep_for_now_cycles` later never retroactively moves a grant someone already gave.

**Two independent axes over the same key, and `approved` deliberately does NOT suppress.**
Collapsing them breaks the loop in opposite directions: if `approved` suppressed, the thing
you agreed to act on would drop off the list and never get done; if `quarantined` did not,
every future run would be clogged with work already completed and ADR-0002's quarantine
clock would have nowhere to live.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from typing import Dict, List, Optional

LEDGER_VERSION = 1

AXIS_VERDICT = "verdict"
AXIS_LIFECYCLE = "lifecycle"
AXIS_RECORD = "record"

VERDICT_VALUES = ("keep-permanent", "keep-for-now", "revoked")
LIFECYCLE_VALUES = ("approved", "quarantined", "restored", "deleted")
RECORD_VALUES = ("withdrawn",)

#: Suppressing values, per axis. `approved` and `restored` are absent on purpose.
_SUPPRESSING_VERDICTS = {"keep-permanent"}
_SUPPRESSING_LIFECYCLE = {"quarantined", "deleted"}

#: Must stay in lockstep with the `condition` enum in `schema/findings.schema.json` — a
#: condition missing here is a condition whose verdicts are silently REJECTED as malformed
#: lines, so it can never be suppressed. `test_ledger` asserts the two agree.
CANDIDATE_ID = re.compile(
    r"^(dataset|dataflow|card|page|app|user):[^:]+:"
    r"(orphaned|stale|failing|unconsumed|duplicated|low-reach|inactive)$"
)


class LedgerLineError(Exception):
    """One line could not be read. Never fatal — a malformed line is collected and
    reported, because crashing on it would make the whole loop hostage to one typo, and
    hand-editing the file is a legitimate fallback."""


def parse_candidate_id(candidate_id: str) -> Optional[tuple]:
    """(asset_type, asset_id, condition) or None when the id is malformed."""
    if not CANDIDATE_ID.match(candidate_id or ""):
        return None
    asset_type, _, rest = candidate_id.partition(":")
    asset_id, _, condition = rest.rpartition(":")
    return asset_type, asset_id, condition


class Entry:
    """One ledger line."""

    __slots__ = (
        "version",
        "candidate_id",
        "axis",
        "value",
        "at",
        "by",
        "reason",
        "run_id",
        "suppress_until",
        "delete_eligible_after",
        "asset_name_at_verdict",
        "collector_version",
        "position",
    )

    def __init__(self, raw: dict, position: int) -> None:
        self.version = raw.get("v", LEDGER_VERSION)
        self.candidate_id = raw.get("candidate_id", "")
        self.axis = raw.get("axis", "")
        self.value = raw.get("value", "")
        self.at = raw.get("at")
        self.by = raw.get("by")
        self.reason = raw.get("reason")
        self.run_id = raw.get("run_id")
        self.suppress_until = raw.get("suppress_until")
        self.delete_eligible_after = raw.get("delete_eligible_after")
        self.asset_name_at_verdict = raw.get("asset_name_at_verdict")
        self.collector_version = raw.get("collector_version")
        self.position = position

    def validate(self) -> None:
        if not self.candidate_id:
            raise LedgerLineError("no candidate_id")
        if self.axis not in (AXIS_VERDICT, AXIS_LIFECYCLE, AXIS_RECORD):
            raise LedgerLineError(f"unknown axis {self.axis!r}")
        allowed = (
            VERDICT_VALUES
            if self.axis == AXIS_VERDICT
            else LIFECYCLE_VALUES
            if self.axis == AXIS_LIFECYCLE
            else RECORD_VALUES
        )
        if self.value not in allowed:
            raise LedgerLineError(f"{self.axis} value {self.value!r} not in {allowed}")
        if self.axis == AXIS_RECORD and not self.reason:
            raise LedgerLineError(
                "withdrawn carries no reason. Withdrawal corrects an invalid candidate "
                "identity; without a reason the audit trail cannot say why it was invalid."
            )
        if self.axis == AXIS_VERDICT and self.value in ("keep-permanent", "keep-for-now"):
            if not self.reason:
                raise LedgerLineError(
                    f"{self.value} carries no reason. The reason string is the only part of "
                    f"this file a human cannot reconstruct."
                )
        if self.axis == AXIS_VERDICT and self.value == "keep-for-now" and not self.suppress_until:
            raise LedgerLineError(
                "keep-for-now carries no suppress_until. A cycle is an absolute date, "
                "written at verdict time — without it the grant has no end."
            )

    def as_line(self) -> str:
        out = {
            "v": self.version,
            "candidate_id": self.candidate_id,
            "axis": self.axis,
            "value": self.value,
            "at": self.at,
            "by": self.by,
        }
        for key in (
            "reason",
            "run_id",
            "suppress_until",
            "delete_eligible_after",
            "asset_name_at_verdict",
            "collector_version",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return json.dumps(out, ensure_ascii=False)


class Folded:
    """The current state of one candidate_id, after folding both axes."""

    __slots__ = (
        "candidate_id",
        "verdict",
        "verdict_at",
        "verdict_by",
        "verdict_reason",
        "verdict_run_id",
        "suppress_until",
        "lifecycle",
        "lifecycle_at",
        "lifecycle_by",
        "lifecycle_reason",
        "delete_eligible_after",
        "asset_name_at_verdict",
        "withdrawn",
        "withdrawn_at",
        "withdrawn_by",
        "withdrawn_reason",
    )

    def __init__(self, candidate_id: str) -> None:
        self.candidate_id = candidate_id
        self.verdict: Optional[str] = None
        self.verdict_at: Optional[str] = None
        self.verdict_by: Optional[str] = None
        self.verdict_reason: Optional[str] = None
        self.verdict_run_id: Optional[str] = None
        self.suppress_until: Optional[str] = None
        self.lifecycle: Optional[str] = None
        self.lifecycle_at: Optional[str] = None
        self.lifecycle_by: Optional[str] = None
        self.lifecycle_reason: Optional[str] = None
        self.delete_eligible_after: Optional[str] = None
        self.asset_name_at_verdict: Optional[str] = None
        self.withdrawn = False
        self.withdrawn_at: Optional[str] = None
        self.withdrawn_by: Optional[str] = None
        self.withdrawn_reason: Optional[str] = None

    # -- suppression ---------------------------------------------------------------------

    def keep_for_now_expired(self, today: _dt.date) -> bool:
        if self.verdict != "keep-for-now" or not self.suppress_until:
            return False
        return today > _dt.date.fromisoformat(self.suppress_until)

    def suppressed(self, today: _dt.date) -> bool:
        if self.withdrawn:
            return False
        if self.verdict in _SUPPRESSING_VERDICTS:
            return True
        if self.verdict == "keep-for-now" and not self.keep_for_now_expired(today):
            return True
        if self.lifecycle in _SUPPRESSING_LIFECYCLE:
            return True
        return False

    def returned(self, today: _dt.date) -> bool:
        """A candidate whose `keep-for-now` has run out.

        A LEVEL, not an edge — true in every run after the grant lapsed, not just the first.
        `reconcile` therefore uses it only for a candidate the prior run never listed at
        all; where a prior record exists, the prior record's own `ledger.suppressed` is what
        decides `returned`. Reading this as the edge would report a two-year-old expiry as
        fresh news in every run since."""
        return self.keep_for_now_expired(today)

    def cycles_remaining(self, today: _dt.date, cycle_days: int) -> Optional[int]:
        """DERIVED FOR DISPLAY. Never stored — storing it would need every run to decrement
        it, which is exactly the counter this design replaced with a date."""
        if self.verdict != "keep-for-now" or not self.suppress_until:
            return None
        remaining = (_dt.date.fromisoformat(self.suppress_until) - today).days
        if remaining <= 0:
            return 0
        return max(1, -(-remaining // max(cycle_days, 1)))

    def as_dict(self, today: _dt.date, cycle_days: int) -> dict:
        return {
            "suppressed": self.suppressed(today),
            "verdict": self.verdict,
            "verdict_at": self.verdict_at,
            "verdict_by": self.verdict_by,
            "verdict_reason": self.verdict_reason,
            "suppress_until": self.suppress_until,
            "cycles_remaining": self.cycles_remaining(today, cycle_days),
            "lifecycle": self.lifecycle,
            "lifecycle_at": self.lifecycle_at,
            "lifecycle_by": self.lifecycle_by,
            "lifecycle_reason": self.lifecycle_reason,
            "delete_eligible_after": self.delete_eligible_after,
            "withdrawn": self.withdrawn,
            "withdrawn_at": self.withdrawn_at,
            "withdrawn_by": self.withdrawn_by,
            "withdrawn_reason": self.withdrawn_reason,
        }


class Ledger:
    def __init__(self, path: str, cycle_days: int = 30, quarantine_days: int = 90) -> None:
        self.path = path
        self.cycle_days = cycle_days
        self.quarantine_days = quarantine_days
        self.entries: List[Entry] = []
        self.malformed: List[dict] = []
        self.state: Dict[str, Folded] = {}

    # -- reading -------------------------------------------------------------------------

    def load_records(self, records: List[dict]) -> "Ledger":
        """Validate and fold an explicitly ordered shared history without a local file."""
        self.entries = []
        self.malformed = []
        for position, raw in enumerate(records, 1):
            entry = Entry(raw, position)
            entry.validate()
            self.entries.append(entry)
        self._fold()
        return self

    def load(self) -> "Ledger":
        """Read and fold. A missing file is an empty ledger, not an error — run one has no
        ledger, and the handoff package ships without one (the CLI creates it on the first
        verdict)."""
        if not os.path.exists(self.path):
            return self
        with open(self.path, "r", encoding="utf-8") as handle:
            for position, line in enumerate(handle, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    raw = json.loads(line)
                except ValueError as exc:
                    self.malformed.append(
                        {"line": position, "error": f"not JSON: {exc}", "text": line[:200]}
                    )
                    continue
                entry = Entry(raw, position)
                try:
                    entry.validate()
                except LedgerLineError as exc:
                    self.malformed.append(
                        {
                            "line": position,
                            "error": str(exc),
                            "candidate_id": entry.candidate_id,
                        }
                    )
                    continue
                self.entries.append(entry)
        self._fold()
        return self

    def _fold(self) -> None:
        """Last-write-wins per candidate_id, ORDERED BY POSITION IN THE FILE.

        The two axes fold independently against the same key, so a `quarantined` lifecycle
        does not erase the `keep-for-now` verdict that preceded it, and vice versa.
        """
        self.state = {}
        for entry in sorted(self.entries, key=lambda e: e.position):
            folded = self.state.get(entry.candidate_id)
            if folded is None:
                folded = Folded(entry.candidate_id)
                self.state[entry.candidate_id] = folded
            if entry.asset_name_at_verdict:
                folded.asset_name_at_verdict = entry.asset_name_at_verdict
            if entry.axis == AXIS_RECORD:
                # A withdrawal is an append-only tombstone for an invalid candidate
                # identity. The history remains in ``entries`` while neither axis remains
                # active in the fold. A later ordinary line starts a fresh active record.
                folded.verdict = None
                folded.verdict_at = None
                folded.verdict_by = None
                folded.verdict_reason = None
                folded.verdict_run_id = None
                folded.suppress_until = None
                folded.lifecycle = None
                folded.lifecycle_at = None
                folded.lifecycle_by = None
                folded.lifecycle_reason = None
                folded.delete_eligible_after = None
                folded.withdrawn = True
                folded.withdrawn_at = entry.at
                folded.withdrawn_by = entry.by
                folded.withdrawn_reason = entry.reason
                continue
            if folded.withdrawn:
                folded.withdrawn = False
                folded.withdrawn_at = None
                folded.withdrawn_by = None
                folded.withdrawn_reason = None
            if entry.axis == AXIS_VERDICT:
                folded.verdict = entry.value
                folded.verdict_at = entry.at
                folded.verdict_by = entry.by
                folded.verdict_reason = entry.reason
                folded.verdict_run_id = entry.run_id
                # `revoked` returns the candidate to normal scoring AS IF NEVER JUDGED, so
                # the grant date goes with it. Leaving it would keep suppressing.
                folded.suppress_until = (
                    entry.suppress_until if entry.value == "keep-for-now" else None
                )
            else:
                folded.lifecycle = entry.value
                folded.lifecycle_at = entry.at
                folded.lifecycle_by = entry.by
                folded.lifecycle_reason = entry.reason
                # `restored` puts the asset back in the instance and must put it back in
                # our model of it too — otherwise the asset is live and suppressed forever,
                # which is ADR-0002's primary safety net failing silently.
                folded.delete_eligible_after = (
                    entry.delete_eligible_after if entry.value == "quarantined" else None
                )

    # -- access --------------------------------------------------------------------------

    def get(self, candidate_id: str) -> Optional[Folded]:
        return self.state.get(candidate_id)

    def has_active_state(self, candidate_id: str) -> bool:
        folded = self.get(candidate_id)
        return bool(folded and not folded.withdrawn)

    def retained_unconsumed_ids(self, today: _dt.date) -> set:
        """Active human keeps, limited to the condition whose closure they terminate.

        Quarantine and deletion deliberately do not retain a chain: they are remediation,
        not a statement that the asset remains intentional. An expired keep-for-now likewise
        vanishes from this set without a ledger rewrite.
        """
        retained = set()
        for candidate_id, folded in self.state.items():
            if folded.withdrawn:
                continue
            parsed = parse_candidate_id(candidate_id)
            if not parsed or parsed[2] != "unconsumed":
                continue
            if folded.lifecycle in _SUPPRESSING_LIFECYCLE:
                continue
            if folded.verdict == "keep-permanent" or (
                folded.verdict == "keep-for-now" and not folded.keep_for_now_expired(today)
            ):
                retained.add(candidate_id)
        return retained

    def suppressed_ids(self, today: _dt.date) -> set:
        return {cid for cid, f in self.state.items() if not f.withdrawn and f.suppressed(today)}

    def watch_list(
        self,
        today: _dt.date,
        observed_keys: Optional[set] = None,
        presence_keys: Optional[set] = None,
        presence_complete_types: Optional[set] = None,
    ) -> List[dict]:
        """Quarantined assets awaiting the separate, later deletion decision.

        A quarantined asset is suppressed from candidates but cannot vanish — someone has
        to come back at `delete_eligible_after` and make the call. Kept in the findings
        file rather than a second artifact, because a second file is a second thing to
        forget.

        `delete_eligible` is ELAPSED TIME ONLY. The collector has no complaint signal and
        never asserts that a quarantine period passed without complaint; that assertion is
        the human's, recorded by appending `deleted` or `restored`.
        """
        out: List[dict] = []
        for candidate_id, folded in sorted(self.state.items()):
            if folded.withdrawn:
                continue
            if folded.lifecycle != "quarantined":
                continue
            parsed = parse_candidate_id(candidate_id)
            if not parsed:
                continue
            asset_type, asset_id, _ = parsed
            asset_key = f"{asset_type}:{asset_id}"
            if (
                presence_keys is not None
                and presence_complete_types is not None
                and asset_type in presence_complete_types
                and asset_key not in presence_keys
            ):
                # Reconciliation owns the resulting terminal record. Leaving it here too
                # would say both "awaiting deletion" and "retired" about one asset.
                continue
            eligible_after = folded.delete_eligible_after
            if not eligible_after and folded.lifecycle_at:
                eligible_after = (
                    _dt.date.fromisoformat(folded.lifecycle_at[:10])
                    + _dt.timedelta(days=self.quarantine_days)
                ).isoformat()
            still_present = None
            if presence_keys is not None and asset_type in (presence_complete_types or set()):
                still_present = asset_key in presence_keys
            elif observed_keys is not None:
                still_present = asset_key in observed_keys
            out.append(
                {
                    "candidate_id": candidate_id,
                    "asset_type": asset_type,
                    "asset_id": asset_id,
                    "asset_name": folded.asset_name_at_verdict or asset_id,
                    "quarantined_at": folded.lifecycle_at,
                    "quarantined_by": folded.lifecycle_by,
                    "delete_eligible_after": eligible_after,
                    "delete_eligible": bool(
                        eligible_after and today >= _dt.date.fromisoformat(eligible_after)
                    ),
                    "still_present": still_present,
                }
            )
        return out

    def stats(self) -> dict:
        return {
            "path": self.path,
            "exists": os.path.exists(self.path),
            "lines_read": len(self.entries),
            "malformed_lines": len(self.malformed),
            "candidates_with_state": len(self.state),
        }


def append(path: str, entries: List[Entry]) -> int:
    """Append validated lines. The ONLY writer in this codebase, called by
    `collector.verdict`.

    Because it only ever appends valid JSONL, hand-editing the file stays a legitimate
    fallback — the CLI is a convenience over the format, not a gatekeeper to it.
    """
    for entry in entries:
        entry.validate()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(entry.as_line() + "\n")
    return len(entries)


def make_entry(
    candidate_id: str,
    axis: str,
    value: str,
    by: str,
    reason: Optional[str] = None,
    run_id: Optional[str] = None,
    asset_name: Optional[str] = None,
    collector_version: Optional[str] = None,
    keep_for_now_cycles: int = 3,
    cycle_days: int = 30,
    quarantine_days: int = 90,
    now: Optional[_dt.datetime] = None,
) -> Entry:
    """Build one line, stamping the absolute dates at verdict time."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    raw = {
        "v": LEDGER_VERSION,
        "candidate_id": candidate_id,
        "axis": axis,
        "value": value,
        "at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "by": by,
        "reason": reason,
        "run_id": run_id,
        "asset_name_at_verdict": asset_name,
        "collector_version": collector_version,
    }
    if axis == AXIS_VERDICT and value == "keep-for-now":
        raw["suppress_until"] = (
            now.date() + _dt.timedelta(days=keep_for_now_cycles * cycle_days)
        ).isoformat()
    if axis == AXIS_LIFECYCLE and value == "quarantined":
        raw["delete_eligible_after"] = (
            now.date() + _dt.timedelta(days=quarantine_days)
        ).isoformat()
    return Entry(raw, position=0)
