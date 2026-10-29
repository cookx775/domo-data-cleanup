"""`python -m collector.annotate` — the agent layer's two seams: what goes in, and what comes back.

The findings file **does not fit**. Granite's run one is 4.7MB over 2,526 candidates; it pastes
into no chat window, and `prompts/CONTRACT.md` forbids solving that with tool calls, because the
layer is portable across Claude, enterprise ChatGPT and Domo AI tiles by design (ADR-0001) and
therefore cannot assume a filesystem. One JSON document in, one JSON document out, and the one
going in has to be small enough to travel by hand.

So this module does two things, and they are one seam rather than two:

* **`slice`** emits the two shapes ticket 12 measured — the **individual decisions** (internally
  `ranked-head`) that paste into any chat window, and a **projected population** that travels as
  a file upload.
* **`check`** implements CONTRACT.md's three rejection rules, which nothing implemented until
  now. ADR-0001 is why that matters: the layer is *additive only*, so an annotation naming an
  asset the run never found is the exact shape of the failure the ADR exists to prevent.

**The projection is lossy on purpose, and which fields survive is a decision, not a trim.** Each
slice carries what its prompts actually read and nothing else — the alternative, shipping
"everything, smaller", makes the size question return the moment a findings file grows. The
per-field reasoning is in `HEAD_*` and `POPULATION_*` below, and a test asserts in both
directions that the emitted field set and the prompt documents still agree.

Deterministic like everything else the collector emits: two calls over one findings file produce
byte-identical output.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Callable, Dict, List, Optional, Tuple

from . import config as config_mod
from . import reconcile as reconcile_mod
from . import triage as triage_mod

SCHEMA_FILENAME = "annotations.schema.json"

#: How much of a rejected value is quoted back. A schema error carries the value that failed,
#: and for a top-level failure that value is the whole document — 68KB of it on granite's first
#: real round trip. A rejection an admin has to scroll is the traceback this module exists to
#: avoid, so every quoted message is cut here.
MAX_MESSAGE = 160

#: How many offending ids a rejection prints before it summarises. A rejection is a remediation
#: list an admin acts on, and a 400-line one is a traceback with extra steps.
MAX_LISTED = 10


# ---------------------------------------------------------------------------------------
# What each slice carries
# ---------------------------------------------------------------------------------------

#: `evidence` keys the head keeps. `purpose-inference.md` names run history, `last_run`,
#: `schedule` and `source_datasets` as the signals it reads, and reads `run_history: absent`
#: as "never ran at all" — a different story from an asset that ran for two years and stopped.
#: `days_stale` and `executions_retained` are read by `purpose-inference.md` too — the
#: history-or-experiment judgment; `narrative.md` never reads either (the earlier comment
#: here said it did, corrected by ticket 33). `executions_retained` is the count Domo still
#: RETAINS (rolling ~6-month window, 50-execution floor), not a lifetime count, and the prompt
#: reads it as such.
#:
#: `landing_history` and `last_data_at` travel too (ticket 43), and the decision is
#: deliberate rather than a reflex to carry a new field: `purpose-inference.md` reasons about
#: whether an asset was ever LOAD-BEARING from `last_run` and `executions_retained`, and a dataset
#: that ran on schedule for four years and never landed a row was never load-bearing — the
#: attempt history alone tells the opposite story. The prompt names both fields, so the
#: two-directional slice/prompt test holds. Dataflow rows simply lack them; `_pick` copies only
#: what is present.
HEAD_EVIDENCE = (
    "run_history",
    "last_run",
    "landing_history",
    "last_data_at",
    "last_execution_status",
    "executions_retained",
    "days_stale",
    "schedule",
    "source_datasets",
)

#: `consumers` keys the head keeps. `purpose-inference.md` quotes this block literally —
#: "`consumers: {cards: 0, pages: 0, downstream_dataflows: 0}` means nothing reads its output
#: NOW" — so the shape it quotes is the shape it gets. `cards_with_no_page_recorded` travels
#: with them as a FLOOR on unplaced cards (ticket 39): evidence about the cards, never a smaller
#: reader count, and the prompt says so where it names the field.
HEAD_CONSUMERS = ("cards", "cards_with_no_page_recorded", "pages", "downstream_dataflows")

#: `cost` keys the head keeps. Every figure travels WITH ITS UNIT: an instance meters in credits
#: or in executions, the two are never converted, and a bare number would let a prompt state a
#: figure in a unit that is not the run's. `basis` says which meter produced it.
#: `additional_if_deleted` is NOT here, and its absence is the rule working. It is a real
#: figure in findings.json and no prompt reads it — `narrative.md` leads on
#: `cost_model.opportunity.recurring` and is forbidden every other figure in that block. A slice
#: that carried it would be shipping "everything, smaller", which is the habit this module
#: exists to avoid.
HEAD_COST = (
    "recurring",
    "recurring_unit",
    "basis",
    "engine",
)

#: Candidate-level keys the head keeps whole.
HEAD_SCALARS = ("candidate_id", "rank", "asset_name", "asset_type", "condition")

#: Document-level blocks the head carries. `cost_model` is not optional decoration:
#: `narrative.md` names `cost_model.opportunity.recurring` and its `recurring_unit` as the one
#: figure it may lead with, and explicitly forbids `attributed_recurring` — on the instance this
#: was calibrated against that figure was 84% of all spend against a true opportunity of 13%.
#: `summary` supplies the denominator for "these five are worth more than the other 2,470".
HEAD_DOCUMENT_BLOCKS = ("run", "cost_model", "summary")

#: What DOES NOT travel, recorded here rather than left for a reader to infer from absence:
#:
#: * `coverage` — 23KB on granite, and no prompt reads it. Its audience is a human auditing what
#:   the run could and could not see.
#: * `reconciliation`, `watch_list` — run-to-run bookkeeping; nothing to annotate.
#: * `ledger` — a HUMAN artifact. The may/may-not table forbids the agent acting on a verdict,
#:   and a model that can see one will weigh it.
#: * `deep_link` — no prompt references it. Datasets carry the report's `Link`; dataflows
#:   carry a constructed route since ticket 44 (built on ticket 35's ruling). A link is for a
#:   HUMAN to open, and the layer that reads this slice has no browser.
#: * the twin block (`evidence.duplicate_twin_detail`, `duplicate_group_realisable`,
#:   `duplicate_cost_comparison`, `duplicate_group_shared_inputs` — ticket 44). Decided
#:   rather than defaulted: `HEAD_EVIDENCE` is a whitelist, so the block stays out unless
#:   named, and it is NOT named. It carries the twin's cost, consumers and link — exactly the
#:   evidence a model would need to pick a survivor, and `redundancy.md` forbids naming one
#:   ("you cannot see which is consumed — that is not in this slice"). Ticket 25 decision 9
#:   left that prohibition standing and ruled the AI half of the survivor decision out of the
#:   MVP; handing the head slice the comparison evidence would lift it by the back door. The
#:   block is for the human reviewer and the report renderer.
#: * `recommended_action` — "review", "quarantine" or "decide-which-survives"; the prompts
#:   read the condition instead.
#: * `utilization` — reach/engagement percentiles no prompt reads.
#: * `pillar`, `asset_id`, `first_seen_run_id` — `candidate_id` already carries type and id, and
#:   the prompts key on it.
HEAD_DROPPED = (
    "coverage", "reconciliation", "watch_list", "ledger", "deep_link",
    "attention", "recommended_action", "recommended_action_basis", "utilization", "pillar",
    "asset_id", "first_seen_run_id",
)

#: Population row keys. `redundancy.md` states its own input: candidate ids, `asset_name`,
#: `asset_type`, `owner`, condition and `source_datasets` — "names and lineage, not evidence or
#: cost". `grouping.md` takes the same slice. Lineage is first among these because it is the one
#: signal that does not lie the way names do.
POPULATION_KEYS = (
    "candidate_ids", "asset_name", "asset_type", "owner", "conditions", "source_datasets",
)


# ---------------------------------------------------------------------------------------
# Head membership — declared in config, never inferred here
# ---------------------------------------------------------------------------------------

def _recurring(candidate: dict) -> float:
    value = (candidate.get("cost") or {}).get("recurring")
    return value or 0.0


#: `triage.head.select` is a NAME in config, and this is the registry it names into. A registry
#: rather than an `if` chain because an unknown name must RAISE: silently falling back to
#: "everything" would ship a 2,526-row head that fits nowhere, and the failure would look like a
#: prompt problem rather than a config one.
HEAD_SELECTORS: Dict[str, Callable[[dict], bool]] = {
    "recurring_cost_above_zero": lambda candidate: _recurring(candidate) > 0,
    # Ticket 25 decision 6, built by ticket 44: this historical selector identifies candidates
    # that need individual attention. A `duplicated` candidate's action is pairwise business
    # judgment that a bulk yes over one owner's band cannot honestly give. Ticket 67 records
    # attention on current findings, so this registry is their compatibility path. Measured on
    # granite run 010: 84 -> 134
    # candidates over 132 assets, and of 25 duplicate groups none was split across the line.
    # `recurring_cost_above_zero` is kept as a named selector so an older config still
    # resolves and so the two populations stay comparable.
    "recurring_cost_above_zero_or_duplicated": lambda candidate: (
        _recurring(candidate) > 0 or candidate.get("condition") == "duplicated"
    ),
}


def head_selector(selector_name: str) -> Callable[[dict], bool]:
    """Resolve the config's named head rule, failing closed on an unknown name."""
    try:
        return HEAD_SELECTORS[selector_name]
    except KeyError:
        raise SystemExit(
            f"unknown head selector {selector_name!r} in triage.head.select. "
            f"Known: {', '.join(sorted(HEAD_SELECTORS))}. A slice built on a selector nobody "
            f"defined would be the wrong size in a way that looks like a prompt failure."
        ) from None


def select_head(candidates: List[dict], selector_name: str) -> List[dict]:
    """The individual-decision slice, ordered by the candidates' OWN `rank`.

    The rank is read, never recomputed. `run.py` ranks over unsuppressed candidates and writes
    the result into the artifact of record; a second ranker here could disagree with the file it
    is slicing, and then two documents describing one run would order it differently with nothing
    to say which was right. `review/sample.py` recomputes rank deliberately, for a different job
    — it is measuring what a *later* config would have ranked.

    Suppressed candidates leave the head. A suppression is a verdict a human already recorded,
    and asking a model to explain one re-opens a closed decision — the same reason the `ledger`
    block does not travel.
    """
    # The named selector is retained for historical findings that predate recorded attention.
    # Current findings carry the output of triage.guidance, so every consumer reads the same
    # policy result instead of independently restating its recurring-or-duplicated rule.
    selector = head_selector(selector_name)
    chosen = [
        candidate for candidate in candidates
        if (
            triage_mod.guidance_of(candidate).attention == triage_mod.ATTENTION_INDIVIDUAL
            if "attention" in candidate
            else selector(candidate) and not (candidate.get("ledger") or {}).get("suppressed")
        )
    ]
    # `rank` is nullable in the schema; a candidate without one sorts last rather than crashing,
    # and ties break on candidate_id so the order is total.
    chosen.sort(key=lambda c: (c.get("rank") is None, c.get("rank") or 0, c["candidate_id"]))
    return chosen


# ---------------------------------------------------------------------------------------
# The two slices
# ---------------------------------------------------------------------------------------

def _owner_name(candidate: dict) -> Optional[str]:
    """Owner flattens to a name. The `user_id` is an identifier for joining, and nothing in the
    agent layer joins — `purpose-inference.md` explicitly says not to infer purpose from the
    owner's name, so the id would be weight with no use."""
    return (candidate.get("owner") or {}).get("name")


def _pick(source: Optional[dict], keys: Tuple[str, ...]) -> dict:
    source = source or {}
    return {key: source[key] for key in keys if key in source}


def head_slice(document: dict, selector_name: str) -> dict:
    """Individual decisions under the compatible ``ranked-head`` slice identifier.

    Each selected candidate carries the evidence a reader needs to say what it is for. The
    slice pastes into any chat window, which is the entire specification.
    """
    chosen = select_head(document.get("candidates") or [], selector_name)
    rows = []
    for candidate in chosen:
        row = {key: candidate[key] for key in HEAD_SCALARS if key in candidate}
        row["owner"] = _owner_name(candidate)
        row["evidence"] = _pick(candidate.get("evidence"), HEAD_EVIDENCE)
        row["consumers"] = _pick(candidate.get("consumers"), HEAD_CONSUMERS)
        row["cost"] = _pick(candidate.get("cost"), HEAD_COST)
        rows.append(row)
    sliced = {key: document[key] for key in HEAD_DOCUMENT_BLOCKS if key in document}
    sliced["slice"] = "ranked-head"
    sliced["candidates"] = rows
    return sliced


def population_slice(document: dict) -> dict:
    """The projected population — one row per ASSET, not per candidate.

    Granite carries 2,526 candidates over 1,737 distinct assets, because one asset can be both
    stale and unconsumed. Per candidate the projection is 568KB / ~142k tokens; per asset it is
    445KB / ~111k, which is the figure ticket 12 recorded and the one that travels.

    The dedup is not only a size win, it is the right grain for the question. `redundancy.md`
    asks whether two *assets* do the same job and `grouping.md` asks which *assets* a human
    would decide about together; the same asset appearing three times under three conditions is
    noise in both. `candidate_ids` keeps the contract's key — an annotation still comes back
    keyed by candidate_id, which is what makes it joinable to findings.
    """
    grouped: Dict[Tuple[str, str], dict] = {}
    for candidate in document.get("candidates") or []:
        key = (candidate.get("asset_type") or "", candidate.get("asset_id") or "")
        row = grouped.get(key)
        if row is None:
            row = grouped[key] = {
                "candidate_ids": [],
                "asset_name": candidate.get("asset_name"),
                "asset_type": candidate.get("asset_type"),
                "owner": _owner_name(candidate),
                "conditions": [],
                "source_datasets": (candidate.get("evidence") or {}).get("source_datasets") or [],
            }
        row["candidate_ids"].append(candidate["candidate_id"])
        condition = candidate.get("condition")
        if condition and condition not in row["conditions"]:
            row["conditions"].append(condition)
    for row in grouped.values():
        row["candidate_ids"].sort()
        row["conditions"].sort()
    run = document.get("run") or {}
    return {
        # Enough of `run` for the model to copy `run_id` verbatim, as `_SHARED.md` requires,
        # and to say which instance and window it is looking at. The rest is head-slice material.
        "run": {
            key: run[key] for key in ("run_id", "instance", "as_of_date", "window")
            if key in run
        },
        "slice": "projected-population",
        "assets": [grouped[key] for key in sorted(grouped)],
    }


def annotations_dir(cfg: config_mod.Config) -> str:
    return os.path.join(cfg.instance_dir, "cleanup", "annotations")


def write_slices(cfg: config_mod.Config, document: dict, out_dir: Optional[str] = None) -> List[str]:
    """Both slices, written beside each other and named for the run they came from.

    The head is indented and the population is not, and that is a decision about readers rather
    than a style inconsistency. A human pastes the head and may well read it; nobody reads 1,737
    projected rows, and indenting them costs 136KB of whitespace inside a budget measured in
    tokens.
    """
    run_id = (document.get("run") or {}).get("run_id")
    if not run_id:
        raise SystemExit("findings file carries no run.run_id; refusing to name a slice after it")
    out_dir = out_dir or annotations_dir(cfg)
    os.makedirs(out_dir, exist_ok=True)
    selector_name = cfg.require("triage", "head", "select")

    written = []
    for name, payload, indent in (
        ("head", head_slice(document, selector_name), 2),
        ("population", population_slice(document), None),
    ):
        path = os.path.join(out_dir, f"{run_id}.{name}.json")
        # Overwritable, unlike a findings file or a ledger: a slice is DERIVED, reproducible
        # byte-for-byte from a file that is itself write-once, so nothing is lost by rewriting it.
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=indent, ensure_ascii=False,
                      separators=None if indent else (",", ":"), sort_keys=False)
            handle.write("\n")
        written.append(path)
    return written


# ---------------------------------------------------------------------------------------
# Validation — CONTRACT.md's three rejection rules
# ---------------------------------------------------------------------------------------

class Rejection:
    """One reason a file was rejected, in an admin's language.

    Rendered as preflight renders a failed report: a remediation list, never a traceback. The
    consequence is bounded and worth stating on screen — CONTRACT.md:93 says a malformed
    annotation layer degrades the report to findings-only, which is still complete and still
    actionable, because findings alone were always the deliverable.
    """

    __slots__ = ("rule", "detail", "items")

    def __init__(self, rule: str, detail: str, items: Optional[List[str]] = None) -> None:
        self.rule = rule
        self.detail = detail
        self.items = items or []

    def lines(self) -> List[str]:
        out = [f"  [{'REJECTED':9s}] {self.rule:20s} {self.detail}"]
        for item in self.items[:MAX_LISTED]:
            out.append(f"                                    {item}")
        if len(self.items) > MAX_LISTED:
            out.append(
                f"                                    ...and {len(self.items) - MAX_LISTED} more"
            )
        return out


class Verdict:
    """The outcome of checking one annotation file."""

    __slots__ = ("path", "rejections", "declined", "counts")

    def __init__(self, path: str) -> None:
        self.path = path
        self.rejections: List[Rejection] = []
        #: The model's own `{"error": ...}` — it declined to annotate rather than returning
        #: something invalid. A different event, and the report says so.
        self.declined: Optional[str] = None
        self.counts: Dict[str, int] = {}

    @property
    def ok(self) -> bool:
        return not self.rejections and self.declined is None

    def report_lines(self) -> List[str]:
        lines = [f"Annotations — {os.path.basename(self.path)}", ""]
        if self.declined is not None:
            lines.append(f"  [{'DECLINED':9s}] {'runtime':20s} {self.declined}")
            lines.append("")
            lines.append(
                "  The runtime declined to annotate rather than returning something invalid — "
                "`_SHARED.md` tells it to do exactly this on truncated or malformed input. "
                "Re-send the slice; do not edit this file."
            )
            return lines
        if self.ok:
            summary = ", ".join(f"{n} {k}" for k, n in sorted(self.counts.items())) or "empty"
            lines.append(f"  [{'ok':9s}] {'accepted':20s} {summary}")
            return lines
        for rejection in self.rejections:
            lines.extend(rejection.lines())
        lines.append("")
        lines.append(
            "  Rejected. The report degrades to findings-only, which is still complete and "
            "still actionable — findings alone were always the deliverable. Re-run the prompt "
            "on the slice for this run; do not hand-edit the annotations."
        )
        return lines


def load_schema() -> dict:
    path = os.path.join(os.path.dirname(__file__), "schema", SCHEMA_FILENAME)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _branch_for(annotations: dict) -> dict:
    """The one schema branch this document is *claiming* to be, resolved before validating.

    Validating against the top-level `oneOf` is correct and useless: when a document fails, the
    only thing that failed is the `oneOf` itself, so jsonschema reports the error at the root
    and quotes the entire document as the offending value. The first real round trip produced a
    68KB rejection message naming one bad field. Choosing the branch first — `error` is present
    or it is not, and nothing else distinguishes them — makes the error land on
    `annotations/<candidate_id>/should_delete`, which is what an admin can act on.
    """
    schema = load_schema()
    kind = "error_document" if isinstance(annotations, dict) and "error" in annotations \
        else "annotation_document"
    branch = dict(schema["$defs"][kind])
    branch["$defs"] = schema["$defs"]
    return branch


def candidate_ids(document: dict) -> set:
    return {c["candidate_id"] for c in (document.get("candidates") or []) if "candidate_id" in c}


def check(annotations: dict, findings: dict, path: str = "annotations.json") -> Verdict:
    """CONTRACT.md:91-92, implemented. Reject a file that references a `candidate_id` absent
    from findings, carries a `run_id` that does not match, or contains any field the schema does
    not define.

    All three rules run — the file is not abandoned at the first failure. An admin re-running a
    prompt wants every reason at once, the same argument discovery makes for always exiting zero
    and reporting every problem in one pass.
    """
    import jsonschema

    verdict = Verdict(path)

    # Rule 3 first: the other two read fields, and reading a field off an unvalidated document
    # is how a validator ends up raising the traceback it exists to prevent.
    if not isinstance(annotations, dict):
        verdict.rejections.append(Rejection(
            "schema", f"the file holds a JSON {type(annotations).__name__}, not an object"))
        return verdict
    errors = sorted(
        jsonschema.Draft202012Validator(_branch_for(annotations)).iter_errors(annotations),
        key=lambda e: list(e.absolute_path),
    )
    if errors:
        verdict.rejections.append(Rejection(
            "schema",
            f"{len(errors)} field(s) the schema does not define or does not allow",
            [
                f"{'/'.join(str(p) for p in e.absolute_path) or '<document>'}: "
                f"{e.message[:MAX_MESSAGE]}"
                for e in errors
            ],
        ))
        return verdict

    if "error" in annotations:
        verdict.declined = annotations["error"]
        return verdict

    # Rule 2 — run_id.
    expected = (findings.get("run") or {}).get("run_id")
    actual = annotations.get("run_id")
    if actual != expected:
        verdict.rejections.append(Rejection(
            "run_id",
            f"annotations describe run {actual!r}, findings are run {expected!r}",
        ))

    # Rule 1 — every candidate_id must exist in the findings file. Checked in the annotation
    # keys AND inside every group, because a group naming an asset the run never found is the
    # same ADR-0001 violation wearing a different shape.
    #
    # `also_involves` is deliberately NOT checked: `redundancy.md` designs it to name an asset
    # the collector did NOT flag — the twin a human should go look at. Validating it would
    # reject the one field that carries a model's suspicion without violating additive-only.
    known = candidate_ids(findings)
    unknown: List[str] = []
    for cid in (annotations.get("annotations") or {}):
        if cid not in known:
            unknown.append(f"annotations/{cid}")
    for index, group in enumerate(annotations.get("groups") or []):
        for cid in group.get("candidate_ids") or []:
            if cid not in known:
                unknown.append(f"groups/{group.get('group_id') or index}/{cid}")
    if unknown:
        verdict.rejections.append(Rejection(
            "candidate_id",
            f"{len(unknown)} reference(s) to candidates this run never found",
            unknown,
        ))

    verdict.counts = {
        key: len(annotations[key])
        for key in ("annotations", "groups")
        if annotations.get(key) is not None
    }
    if annotations.get("narrative"):
        verdict.counts["narrative"] = 1
    return verdict


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------

def load_findings(runs_dir: str, run_id: Optional[str]) -> dict:
    """Newest canonical artifact by `started_at` unless `--run` names one.

    Same rule as `collector.verdict`, the other command an admin points at a specific run.
    """
    paths = sorted(glob.glob(os.path.join(runs_dir, "*.json")))
    best, best_started = None, ""
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
            best_started, best = started, document
    if best is None:
        if run_id:
            raise SystemExit(f"no findings file matching run_id={run_id!r} in {runs_dir}")
        raise SystemExit(f"no findings files in {runs_dir}. Run the collector first.")
    return best


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.annotate",
        description=(
            "Slice a findings file down to what the agent prompts can actually read, and "
            "validate the annotations they return. Touches no network."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    slicer = sub.add_parser(
        "slice", help="emit individual decisions and the projected population"
    )
    slicer.add_argument("--instance", required=True)
    slicer.add_argument("--run", default=None, help="run_id to slice (default: newest)")
    slicer.add_argument("--out", default=None, help="output directory (default: the instance's)")

    checker = sub.add_parser("check", help="validate a returned annotations file")
    checker.add_argument("--instance", required=True)
    checker.add_argument("--file", required=True, help="the annotations.json to validate")
    checker.add_argument("--run", default=None, help="run_id to validate against (default: newest)")

    args = parser.parse_args(argv)
    cfg = config_mod.load(args.instance)
    findings = load_findings(cfg.runs_dir, args.run)

    if args.command == "slice":
        for path in write_slices(cfg, findings, args.out):
            size = os.path.getsize(path)
            print(f"  {path}  {size:,} bytes (~{size // 4:,} tokens)")
        print()
        print("  The head pastes into a chat window; the population travels as a file upload.")
        print("  Prompts are in prompts/ — start with purpose-inference, finish with narrative.")
        return 0

    with open(args.file, "r", encoding="utf-8") as handle:
        annotations = json.load(handle)
    verdict = check(annotations, findings, args.file)
    print("\n".join(verdict.report_lines()))
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
