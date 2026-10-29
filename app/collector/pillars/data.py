"""The data pillar: `unconsumed`, `stale` and `failing` over datasets and dataflows.

Three structural rules govern everything here, and all three came from ADR-0003.

**1. The population filter runs before any condition.** A dataflow whose run history is
*absent* does not execute by design, and leaves the population entirely — not just the
run-based tests, but `unconsumed` too. That last part is the correction that matters:
`unconsumed` is not run-based, so a rule scoped to run-based conditions would have let
DataFusions through, and because they carry zero output-dataset rows a naive "outputs feed
nothing" test flags all three. The filter is structural (`Execution Count IS NULL`), and
the engine name `DATAFUSION` is retained only as an **assertion** — a mismatch on another
instance raises a coverage exception naming the new object class instead of shipping false
positives.

**The filter is dataflow-grain only, deliberately.** Datasets carry no execution count,
and an absent `Last_Run_Date` on a dataset means "never loaded", which is a real finding:
on granite 45 datasets have never loaded, 18 of them reporting `Status = 'ERROR'` — nine
netsuite-jdbc connections that have never once succeeded. Those are the most actionable
findings in the pillar, not objects to filter out.

**2. There is no `never-run` condition.** Consumption is a property of the present, so an
object that never ran is `unconsumed` under the same word as one that ran for four years
and lost its last reader last month. Because one word now covers arrivals from two very
different directions, every candidate carries `evidence.run_history` so a reviewer can tell
them apart — that field, not a sub-tag on the id, because `candidate_id` is the ledger's
fold key and an asset flipping from `unconsumed/never-run` to `unconsumed/went-quiet` would
silently lose its verdict.

**3. `stale` presupposes a refresh history; `failing` reads the last ATTEMPT.** A never-run
object has an *undefined* age, not an infinite one, so it produces no `stale` candidate. But
an object that errored on every attempt and therefore never completed a run IS failing —
`failing` and `unconsumed` co-occur freely, and asset+condition identity exists precisely so
both survive.

**4. `duplicated` is a property of a SET, and it is the only one here that is** (ticket 21,
built by ticket 32). The other three ask a question about one asset; this one asks whether
two assets do the same job, and it is reconciled with `asset + condition` identity by making
**every member a candidate** carrying its `duplicate_group`. The rule itself lives in
`shared.duplication`, which is pure — the pillar supplies the graph and reads back
twin-sets. It co-occurs with the other three **freely**, exactly as they do with each other:
on granite 20 of 26 duplicated dataflows are also `stale`, and suppressing a condition
because another fired would be a new rule with no precedent, and would let an asset gain a
`duplicated` candidate on the run where it stopped being stale.
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Set, Tuple

from shared import consumption as consumption_mod
from shared import conditions
from shared import duplication as duplication_mod
from shared import publications as publications_mod
from shared.typed import CoverageError, Result

CONDITION_UNCONSUMED = "unconsumed"
CONDITION_STALE = "stale"
CONDITION_FAILING = "failing"
CONDITION_DUPLICATED = "duplicated"

ASSET_DATASET = "dataset"
ASSET_DATAFLOW = "dataflow"

RUN_NEVER = "never"
RUN_OBSERVED = "observed"

#: `evidence.landing_history` (ticket 43): whether data has ever LANDED on a dataset, as
#: distinct from whether a run was ever ATTEMPTED (`run_history`). `never` is the finding —
#: a connector that runs on schedule returning nothing has attempts and no landing.
#: `unknown` means the enumeration did not describe the dataset (or carried no landing
#: field), which is not `never`: not-knowing is never reported as never-landed.
LANDING_NEVER = "never"
LANDING_OBSERVED = "observed"
LANDING_UNKNOWN = "unknown"

#: `evidence.schedule` and the twin block's `schedule` are DATASET facts, and the null on a
#: dataflow is a recorded decision rather than an oversight — ticket 56, decided against a
#: live census of all 371 granite dataflows on 2026-09-04.
#:
#: `Dataflow Details` carries no schedule column (35 columns, none schedule- or
#: trigger-shaped). The product path `dataprocessing/v1/dataflows/{id}` carries no
#: `schedule` field either: what it carries is `triggerSettings.triggers[].triggerEvents[]`,
#: a TRIGGER MODEL of two mutually exclusive kinds — `DATASET_UPDATED` (163 dataflows, each
#: event naming a dataset id) and `SCHEDULE` (59, each carrying a quartz-shaped
#: `{second,minute,hour,dayOfMonth,month,dayOfWeek,year}` object interpretable only with the
#: block's `zoneId`). Neither is a DomoStats `Schedule` string, and putting either into a
#: slot whose dataset sibling holds `DAY` would invite a comparison the two cannot support.
#:
#: And **unscheduled is spelled by OMISSION**: 149 of 371 carry no `triggerSettings` key at
#: all — never `null`, never `{"triggers": []}`, no positive `MANUAL` value anywhere. So on
#: this surface a dataflow that is unscheduled and one whose trigger block was not served
#: are the same bytes. ADR-0003's answer to that is `absent`, not `unscheduled`.
#:
#: The value it would add was measured on the population that wanted it: of granite's 26
#: `duplicated` dataflows, **0 carry a time schedule**, 23 share one trigger kind, and the
#: kind is identical within 9 of the 10 groups. See ADR-0004 amendment (e).
#:
#: If a read here is ever built anyway, two traps on the same payload are already measured
#: and belong to ticket 49, which owns the figures: the output-count field that agrees with
#: the governance report is **`numOutputs`** (273/273 of granite's dataflow candidates),
#: while the **`PublishToVault` action count does not** (235/273) — an action is a publish
#: STEP and survives its target dataset's deletion. Ticket 56 confirmed the shapes on 2026-09-04:
#: `outputs[]` entries carry `dataSourceId`, a `PublishToVault` action instead nests a
#: `dataSource` OBJECT, that object is **`{}` on a publish step that never materialised**
#: (dataflows 58, 82), and the `outputs` key is **absent entirely** when `numOutputs` is 0 —
#: so a reader chasing a target id there gets absence, an empty object, or a guid depending
#: on which of three states it is in. Action key sets also vary between dataflows (183 carries
#: `query` and no `name`, 58 the reverse), which is a further reason not to count them.
#:
#: What the guid IDENTIFIES was left open by ticket 49 and is settled here (2026-09-04): it is
#: a **dataset id**, and `outputs` is the live subset of it. Dataflow 122 carries 8 publish
#: actions whose 8 guids resolve 1 live / 7 `404` against `datasources/{guid}`, and the single
#: live one IS `outputs[0].dataSourceId`; 183's one action agrees the same way. So `outputs`
#: is the CURRENT target list and the actions are the HISTORICAL step list, differing by
#: deleted datasets. The trap is the sibling `type`, which reads `"DataFlow"` on live and dead
#: alike: it describes the dataset's PROVENANCE — this dataset is produced by a dataflow — and
#: never says the guid is a dataflow id. Reading it as one turns a dataset id into the wrong
#: identifier space (`CONTEXT.md`, identifier space).
DATAFLOW_SCHEDULE_UNREAD = (
    "Null on a dataflow: `Dataflow Details` carries no schedule column and the product "
    "path carries a trigger model rather than a schedule string, so this null is NOT-READ "
    "and never `unscheduled` (ticket 56)."
)

#: Ticket 64's first calibration, from Altimate's existing cleanup vocabulary. Matching is
#: case-insensitive and exact: a project label containing the word "active" is not a human
#: assertion that this asset should remain active, and guessing would turn context into a
#: verdict. These tags only annotate evidence; they never score, rank, or suppress.
ACTIVE_STYLE_TAGS = frozenset({"active"})
DUPLICATE_STYLE_TAGS = frozenset({"duplicate?"})


def tag_relationships(
    condition: str,
    tags: Optional[List[str]] = None,
    output_tags: Optional[List[dict]] = None,
) -> List[dict]:
    """How calibrated human tags relate to one deterministic finding.

    ``Active`` contradicts ``unconsumed``; ``Duplicate?`` agrees with ``duplicated``.
    Neither becomes a condition, a verdict, or a suppression. Dataflow tags are always
    attributed to the output dataset that actually carries them.
    """
    sources: List[Tuple[str, Optional[str], str]] = []
    for tag in tags or []:
        sources.append(("dataset", None, tag))
    for output in output_tags or []:
        for tag in output.get("tags") or []:
            sources.append(("output_dataset", output.get("dataset_id"), tag))

    relationships: List[dict] = []
    for source, dataset_id, tag in sources:
        folded = tag.casefold()
        relationship = None
        if condition == CONDITION_UNCONSUMED and folded in ACTIVE_STYLE_TAGS:
            relationship = "contradiction"
            meaning = (
                "a human assertion that the dataset is active contradicts this "
                "`unconsumed` finding; it does not suppress the finding"
            )
        elif condition == CONDITION_DUPLICATED and folded in DUPLICATE_STYLE_TAGS:
            relationship = "agreement"
            meaning = (
                "a human suspicion of duplication agrees with this `duplicated` finding; "
                "it is not a verdict and names no survivor"
            )
        if relationship is None:
            continue
        subject = "the dataset" if source == "dataset" else f"output dataset {dataset_id}"
        relationships.append(
            {
                "relationship": relationship,
                "tag": tag,
                "source": source,
                "dataset_id": dataset_id,
                "detail": f"{subject} carries `{tag}`: {meaning}",
            }
        )
    return relationships

#: The UI route a dataflow opens at, relative to the instance host (ticket 35). Kept as ONE
#: pattern with the host substituted from config, never typed into a string elsewhere: the
#: findings file is operational and may carry the real link, the package's documentation
#: describes this pattern and never a real host or id.
DATAFLOW_ROUTE = "https://{host}/datacenter/dataflows/{dataflow_id}/details"

#: Why a dataflow in the population carries `deep_link: null`. Recorded per object in
#: `coverage.deep_links`, never collapsed into a count, because on the instance where it
#: first happens the reviewer needs to know WHICH rows they cannot open.
LINK_WITHHELD_NOT_DECIMAL = "`Dataflow ID` is not a decimal id, so it is not the UI's numeric id"
LINK_WITHHELD_NO_DAP = "`DAP Dataflow ID` is absent, so the row's identifier space is unconfirmed"


def _host(domain: str) -> str:
    """The instance host as `DomoClient` normalises it: scheme and trailing slash stripped,
    a bare instance name completed to `<name>.domo.com`. Same rule in both places so the link
    the findings carry points at the host the collector actually read."""
    host = (domain or "").strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if host and "." not in host:
        host = f"{host}.domo.com"
    return host


def dataflow_deep_link(domain: str, dataflow_id: str, dap_dataflow_id: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """`(link, why_withheld)` for one dataflow row. Exactly one of the two is None.

    Ticket 35's ruling, mechanised. `Dataflow Details` carries two identifier spaces, and
    `Dataflow ID` is itself two of them: 371 granite rows hold the numeric id the UI routes on
    with a UUID beside it in `DAP Dataflow ID`, and 3 hold a UUID with `DAP Dataflow ID`
    absent. A link built from the second kind would open the wrong object — or nothing — on
    exactly the rows that enter the population on an instance where a DataFusion-shaped
    object executes. So BOTH guards must hold, and a row failing either gets no link and a
    named reason, never a guess. No name join, no new read: both columns are in the row.

    The host comes from config (`domain`), never from a literal, because the same collector
    runs on every instance in the portfolio and a hardcoded host would be the first place the
    package carried one company's address into another's findings.
    """
    if not (dataflow_id or "").isdecimal():
        return None, LINK_WITHHELD_NOT_DECIMAL
    if not dap_dataflow_id:
        return None, LINK_WITHHELD_NO_DAP
    host = _host(domain)
    if not host:
        return None, "the instance config declares no `domain` to build the link on"
    return DATAFLOW_ROUTE.format(host=host, dataflow_id=dataflow_id), None


def _iso_utc(stamp: Optional[_dt.datetime]) -> Optional[str]:
    """The document's timestamp spelling, applied inside evidence the pillar builds itself.
    `run.py` formats the top-level fields; the twin block is nested under `evidence` and is
    serialised as built, so it has to arrive already a string."""
    if stamp is None:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return stamp.isoformat(timespec="seconds") + "Z"


def name_stem_difference(this: str, twin: str) -> dict:
    """What differs between two names, and what they share.

    The `GP |` prefix on granite's three real pairs is itself the business fact — the
    DomoStats quickstart installed twice — so the difference is carried as data rather than
    left for a reader to spot across two long strings. Longest common prefix and suffix,
    non-overlapping; what remains in the middle is each member's own. `identical` is stated
    outright, because two identical names are the case where a reader can least tell the rows
    apart and most needs to be told nothing in the name will help.
    """
    this = this or ""
    twin = twin or ""
    limit = min(len(this), len(twin))
    prefix = 0
    while prefix < limit and this[prefix] == twin[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and this[-1 - suffix] == twin[-1 - suffix]:
        suffix += 1
    shared_prefix = this[:prefix]
    shared_suffix = this[len(this) - suffix:] if suffix else ""
    return {
        "identical": this == twin,
        "shared_prefix": shared_prefix,
        "shared_suffix": shared_suffix,
        "this_only": this[prefix:len(this) - suffix],
        "twin_only": twin[prefix:len(twin) - suffix],
    }


def _unit_words(recurring_unit: Optional[str]) -> str:
    return (recurring_unit or "").replace("_per_month", "/month").replace("_", " ") or "units"


def cost_comparison_sentence(figures: List[Tuple[str, Optional[float]]], recurring_unit: Optional[str]) -> str:
    """The figures of a duplicate set, read out as a sentence that refuses to choose.

    Rule one (ticket 25 decision 8): EQUAL figures are stated as equal — "both bill 180.0
    credits/month; cost does not choose here" — because every granite group is an equal-cost
    pair and the first instance whose twins differ will be a stranger's; a silent repetition
    lets a reader assume a symmetry the next instance will not have. Rule two: a difference
    is evidence for the human decision and is never turned into a survivor. Rule three: an
    unmeasured figure is said to be unmeasured, never shown as zero or skipped.
    """
    unit = _unit_words(recurring_unit)
    unmeasured = [name for name, value in figures if value is None]
    if unmeasured:
        return (
            f"cost not measured for {', '.join(unmeasured)}; cost cannot be compared here "
            f"and which member survives remains a business judgment"
        )
    values = [value for _, value in figures]
    if len(set(values)) == 1:
        who = "Both" if len(values) == 2 else f"All {len(values)}"
        return f"{who} bill {values[0]} {unit}; cost does not choose here"
    listed = "; ".join(f"{name} bills {value} {unit}" for name, value in figures)
    return (
        f"The figures differ: {listed}. A cost difference is evidence for the human "
        f"decision, not a survivor: which member survives is a business judgment this "
        f"record does not make"
    )


REALISABLE_CONVENTION = (
    "sum minus max over the compared members: an arithmetic convention for the aggregate, "
    "stating the least the set recovers if any one member is retained. It is not a "
    "recommendation and names no survivor; which member survives is a business judgment."
)


def realisable_by_convention(values: List[Optional[float]]) -> Optional[float]:
    """`sum - max`, the same convention `Duplication.aggregate` reports, over one compared set.
    None — never 0.0 — if any member is unmeasured, so an unknown is not reduced by nothing."""
    if not values or any(v is None for v in values):
        return None
    return round(sum(values) - max(values), 3)


#: Moved to `shared.conditions` (D2 ticket 09); kept under its old name for callers here.
_days_since = conditions.days_since


class Asset:
    """One examined object, before any condition is applied.

    Carries typed, already-resolved values so a condition never touches a raw column and
    the same asset can be scored by several conditions without re-reading anything.
    """

    __slots__ = (
        "asset_type",
        "asset_id",
        "name",
        "owner_id",
        "owner_name",
        "deep_link",
        "run_history",
        "last_run",
        "landing_history",
        "last_data_at",
        "last_execution_status",
        "executions_retained",
        "success_count",
        "run_state",
        "pause_history",
        "enabled",
        "schedule",
        "outputs_declared",
        "outputs_observed",
        "inputs_observed",
        "engine",
        "cards",
        "cards_with_no_page_recorded",
        "pages",
        "downstream_dataflows",
        "output_dataset_ids",
        "source_datasets",
        "days_stale",
        "status",
        "import_type",
        "data_provider",
        "cost_entity_ids",
        "tags",
        "output_tags",
    )

    def __init__(self, asset_type: str, asset_id: str, name: str) -> None:
        self.asset_type = asset_type
        self.asset_id = asset_id
        self.name = name
        self.owner_id: Optional[str] = None
        self.owner_name: Optional[str] = None
        self.deep_link: Optional[str] = None
        self.run_history = RUN_OBSERVED
        self.last_run: Optional[_dt.datetime] = None
        # Ticket 43. `last_run` is an ATTEMPT; this pair is the LANDING, and the two are
        # never merged. A dataset starts `unknown` — the enumeration has not spoken yet —
        # and `load_consumption` resolves it. A dataflow carries None on both: its landing
        # is its outputs', which are dataset candidates in their own right.
        self.landing_history: Optional[str] = (
            LANDING_UNKNOWN if asset_type == ASSET_DATASET else None
        )
        self.last_data_at: Optional[_dt.datetime] = None
        self.last_execution_status: Optional[str] = None
        # Ticket 33. `Execution Count` is the number of executions Domo still RETAINS for the
        # dataflow — every execution inside a rolling ~6-month window plus at least the 50
        # most recent regardless of age — never a lifetime count. Named for what it counts.
        self.executions_retained: Optional[int] = None
        self.success_count: Optional[int] = None
        self.run_state: Optional[str] = None
        #: Optional Activity Log context for a current ``DISABLED`` state (ticket 87).
        #: ``None`` on every other current state.  The history is evidence only: no
        #: condition reads it, and the current Dataflow Details snapshot remains the
        #: lifecycle authority when the two sources disagree.
        self.pause_history: Optional[dict] = None
        self.enabled: Optional[bool] = None
        self.schedule: Optional[str] = None
        self.outputs_declared: Optional[int] = None
        self.outputs_observed: Optional[int] = None
        self.inputs_observed: Optional[int] = None
        self.engine: Optional[str] = None
        self.cards: Optional[int] = None
        #: Placement FLOOR, evidence only (ticket 39; widened to cards with no POSITIVE page
        #: id by ticket 58, which changes the figure and no verdict). Summed over outputs
        #: for a dataflow; a
        #: sum of floors is still a floor. Never read by a condition: `unconsumed` reads
        #: lineage, and a card consumes its dataset whether or not it sits on a page.
        self.cards_with_no_page_recorded: Optional[int] = None
        self.pages: Optional[int] = None
        self.downstream_dataflows: Optional[int] = None
        self.output_dataset_ids: Set[str] = set()
        self.source_datasets: List[str] = []
        self.days_stale: Optional[int] = None
        self.status: Optional[str] = None
        self.import_type: Optional[str] = None
        self.data_provider: Optional[str] = None
        self.cost_entity_ids: Set[str] = set()
        #: Dataset tags are an enumeration fact. ``None`` is unknown (the enumeration or
        #: field was unavailable); ``[]`` is Domo explicitly saying this dataset is untagged.
        self.tags: Optional[List[str]] = None
        #: Dataflows carry no tags of their own. This is their outputs' tag evidence, with
        #: each tag kept beside the dataset id that owns it. ``None`` means unavailable.
        self.output_tags: Optional[List[dict]] = None

    @property
    def key(self) -> str:
        return f"{self.asset_type}:{self.asset_id}"


class Candidate:
    __slots__ = ("asset", "condition", "evidence_extra")

    def __init__(self, asset: Asset, condition: str, evidence_extra: Optional[dict] = None):
        self.asset = asset
        self.condition = condition
        self.evidence_extra = evidence_extra or {}

    @property
    def candidate_id(self) -> str:
        return f"{self.asset.asset_type}:{self.asset.asset_id}:{self.condition}"


class DataPillar:
    def __init__(self, cfg, lineage, cost_model, as_of: Optional[_dt.date] = None) -> None:
        self.cfg = cfg
        self.lineage = lineage
        self.cost = cost_model
        #: The run's single reference DATE (ticket 26), never an instant. Every age in the
        #: document is measured from it, it is the same date `run_id`, the cost window and
        #: every ledger evaluation already take, and it ships as `run.as_of_date` so a
        #: reader can recompute any age from the document alone. A `datetime` is normalised
        #: rather than rejected: passing the run instant is the ordinary caller mistake and
        #: its date is exactly the right answer, so silently keeping the instant -- the one
        #: outcome this ticket exists to prevent -- must not be reachable.
        if isinstance(as_of, _dt.datetime):
            as_of = as_of.date()
        self.as_of: _dt.date = as_of or _dt.datetime.now(_dt.timezone.utc).date()

        self.datasets: Dict[str, Asset] = {}
        self.dataflows: Dict[str, Asset] = {}
        self.people: Dict[str, dict] = {}

        #: Objects removed by the population filter, with the engine that tripped it.
        self.non_executing: List[dict] = []
        #: Dataflows IN the population that carry `deep_link: null`, each with the guard that
        #: withheld it (ticket 44, after ticket 35). Coverage, never a guessed link: on granite
        #: this is empty, because the only mixed-space rows are the three the filter removes.
        self.deep_links_withheld: List[dict] = []
        #: Dataflows in the population that received a constructed link.
        self.deep_links_granted = 0
        #: Named, explicit statements that something did not run or could not be judged.
        self.coverage_notes: List[dict] = []
        #: Declared-vs-observed disagreements, recorded rather than resolved either way.
        self.count_disagreements: List[dict] = []
        #: Twin-sets at both grains, or None when no schema source was supplied — which is
        #: NOT the same as "no duplication was found" and must not read as it downstream.
        self.duplication: Optional[duplication_mod.Duplication] = None
        #: Conditions that could not be evaluated AT ALL this run. Reconciliation needs it:
        #: without it a prior candidate of an unevaluable condition reads as `resolved`,
        #: because its asset is still sitting in the population.
        self.conditions_unavailable: Set[str] = set()
        #: Candidate-grain abstentions. A condition can run for the instance while remaining
        #: unknown for one asset (an unreachable lineage call, a missing output schema).
        #: Reconciliation must receive that fact or it will read the absent candidate as
        #: resolved merely because the DomoStats grain still contains the asset.
        self.unevaluable_candidate_ids: Set[str] = set()

        self._card_linkage_available = False
        self._schemas = None

        #: The dataset-enumeration consumption counts (ticket 31), and the bounded lineage
        #: verdicts over the contested set. `_consumption_declared` separates "the caller
        #: said the enumeration was unavailable" from "the caller never spoke about it at
        #: all" — the second is a legacy/partial construction, and `stats()` reports which,
        #: because governance alone is now KNOWN to score 13 of granite's 803 candidates
        #: wrongly and a silent revert to it must be visible in the artifact of record.
        self._consumption: Optional[consumption_mod.ConsumptionCounts] = None
        self._consumption_declared = False
        #: The complete live dataset inventory, distinct from the scheduled DomoStats
        #: dataset grain. ``None`` means the enumeration was unavailable; an empty set is a
        #: complete live inventory containing no datasets.
        self._enumerated_dataset_ids: Optional[Set[str]] = None
        #: Public like `duplication`, and for the same reason: `build_document` carries it
        #: into `coverage.consumption` rather than being handed it a second time.
        self.adjudication: Optional[consumption_mod.Adjudication] = None
        #: What this instance publishes (ticket 65): the FIFTH consumer type. Same triad as
        #: the enumeration — declared-and-present, declared-and-unavailable, never declared —
        #: because a publish API that could not be read is a coverage exception that takes
        #: `unconsumed` off the run, and a caller that never asked is a partial construction
        #: the artifact must name. Public for the same reason `adjudication` is.
        self.publications: Optional[publications_mod.Publications] = None
        self._publications_declared = False
        self.publications_error: Optional[str] = None

    # -- loading -------------------------------------------------------------------------

    def load_people(self, result: Result) -> None:
        for row in result:
            user_id = row.get("User ID").as_str()
            self.people[user_id] = {
                "name": row.get("Display Name").as_str(absent=None),
                "email": row.get("Email").as_str(absent=None),
                "role": row.get("Role").as_str(absent=None),
                "last_login": row.get("Last Login").as_str(absent=None),
                "department": row.get("Department").as_str(absent=None),
            }

    def load_dataflows(self, result: Result) -> None:
        """Applies the population filter while loading, so no downstream code can forget it."""
        assert_engines = {
            str(e).upper()
            for e in (self.cfg.get("population", "assert_non_executing_engines", default=[]) or [])
        }
        filter_enabled = bool(self.cfg.get("population", "exclude_non_executing", default=True))
        tripped_engines: Set[str] = set()

        for row in result:
            dataflow_id = row.get("Dataflow ID").as_str()
            execution_count = row.get("Execution Count")
            engine = row.get("Database ProcessingType").as_str(absent=None)

            if filter_enabled and execution_count.is_absent:
                # ABSENT run history => the object does not execute => out of the
                # population before any condition runs. Not `0`, which means it executes
                # and never has, and is a real finding.
                tripped_engines.add(str(engine or "").upper())
                self.non_executing.append(
                    {
                        "dataflow_id": dataflow_id,
                        "name": row.get("Name").as_str(absent=None),
                        "engine": engine,
                        "declared_outputs": row.get("Number of Outputs").as_int(absent=None),
                        "observed_outputs": row.get("outputs_observed").as_int(absent=0),
                        "last_execution_state": row.get("Last Execution State").as_str(
                            absent=None
                        ),
                        "why": "run history absent — non-executing object",
                    }
                )
                continue

            asset = Asset(ASSET_DATAFLOW, dataflow_id, row.get("Name").as_str(absent="(unnamed)"))
            asset.engine = engine
            asset.owner_id = row.get("Owner ID").as_str(absent=None)
            asset.owner_name = row.get("Owner").as_str(absent=None)
            # The constructed deep link (ticket 35, built by ticket 44). Ticket 05's "no deep
            # link for a dataflow" rested on the two identifier spaces sharing no key; ticket
            # 19 found they share a row — `Dataflow ID` is the UI's numeric id and
            # `DAP Dataflow ID` the Governance UUID — so the link needs no name join and no
            # read. Two guards, both required; a row failing either keeps `deep_link: null`
            # and is recorded in `coverage.deep_links` by name, never linked by guess. The
            # link's usability was accepted by ticket 47's authenticated click-through.
            dap = row.get("DAP Dataflow ID")
            asset.deep_link, withheld = dataflow_deep_link(
                self.cfg.get("domain"), dataflow_id, None if dap.is_absent else dap.as_str()
            )
            if withheld is not None:
                self.deep_links_withheld.append(
                    {"dataflow_id": dataflow_id, "name": asset.name, "why": withheld}
                )
            else:
                self.deep_links_granted += 1

            asset.executions_retained = execution_count.as_int(absent=None)
            asset.success_count = row.get("Execution Success Count").as_int(absent=None)
            asset.run_state = row.get("Run State").as_str(absent=None)
            asset.enabled = row.get("Enabled").as_bool(absent=None)
            asset.last_execution_status = row.get("Last Execution State").as_str(absent=None)
            asset.outputs_declared = row.get("Number of Outputs").as_int(absent=None)
            asset.outputs_observed = row.get("outputs_observed").as_int(absent=0)
            asset.inputs_observed = row.get("inputs_observed").as_int(absent=0)

            begin = row.get("Last Execution Begin Time")
            if begin.is_absent:
                asset.last_run = None
            else:
                asset.last_run = begin.as_datetime()

            # `run_history` is `never` when the count is PRESENT and zero. An absent count
            # never reaches here — it left the population above. Zero still means never ran
            # even though the count is a RETAINED count: retention keeps the 50 most recent
            # executions regardless of age, so a dataflow that ever ran retains at least one
            # (ticket 33; 14 granite dataflows last run in 2021 all read >= 1).
            asset.run_history = (
                RUN_NEVER if (asset.executions_retained == 0) else RUN_OBSERVED
            )

            failed = row.get("Last Execution Failed")
            asset.status = None if failed.is_absent else str(failed.as_bool())

            asset.output_dataset_ids = set(self.lineage.outputs_of(dataflow_id))
            asset.cost_entity_ids = set(asset.output_dataset_ids)
            asset.source_datasets = sorted(self.lineage.inputs_of(dataflow_id))

            # Roll the outputs' consumption up onto the dataflow. Without this a dataflow
            # candidate shows all-null consumers and a reviewer cannot see WHY it is
            # unconsumed without chasing every output by hand — which is exactly the kind
            # of missing evidence ticket 08's `uncertain` bucket exists to measure.
            # `cards` stays None rather than 0 when the linkage report was unavailable: not
            # knowing is not the same as none.
            downstream: Set[str] = set()
            for output in asset.output_dataset_ids:
                downstream |= self.lineage.downstream_dataflows(output)
            downstream.discard(dataflow_id)   # a dataflow feeding itself is not a consumer
            asset.downstream_dataflows = len(downstream)
            if self.lineage.card_linkage_available:
                asset.cards = sum(
                    self.lineage.card_count(o) or 0 for o in asset.output_dataset_ids
                )
                asset.cards_with_no_page_recorded = sum(
                    self.lineage.cards_with_no_page_recorded(o) or 0
                    for o in asset.output_dataset_ids
                )
                asset.pages = sum(
                    self.lineage.page_count(o) or 0 for o in asset.output_dataset_ids
                )

            # ADR-0003 decision 4: a declared count is cross-checked against its edge rows
            # and a disagreement is RECORDED, never resolved silently in favour of either
            # side. Granite's DataFusions declare 1 output while carrying zero — a present,
            # non-zero number that is simply wrong.
            if (
                asset.outputs_declared is not None
                and asset.outputs_declared != asset.outputs_observed
            ):
                self.count_disagreements.append(
                    {
                        "asset": asset.key,
                        "name": asset.name,
                        "declared_outputs": asset.outputs_declared,
                        "observed_outputs": asset.outputs_observed,
                    }
                )

            self.dataflows[dataflow_id] = asset

        if self.deep_links_withheld:
            # Visible as coverage. A reviewer handed `deep_link: null` on an in-population
            # dataflow must be able to tell "the collector declined to guess" from "the
            # collector forgot", and the difference lives here, not in the null.
            self.coverage_notes.append(
                {
                    "reason": "shape-mismatch",
                    "detail": (
                        f"{len(self.deep_links_withheld)} dataflow(s) in the population carry "
                        f"no deep link because their `Dataflow Details` row failed a link "
                        f"guard (a non-decimal `Dataflow ID`, or an absent `DAP Dataflow "
                        f"ID`). The rows are keyed from an identifier space the UI route "
                        f"does not take, so a constructed link would point at the wrong "
                        f"object; the link is withheld and the rows are named in "
                        f"coverage.deep_links.dataflows_unlinked."
                    ),
                }
            )

        if filter_enabled and self.non_executing:
            self._assert_non_executing(tripped_engines, assert_engines)
            # ADR-0003 decision 4 is unconditional, so the declared-vs-observed check is
            # ALSO applied to the objects the filter removed. They never reach the
            # in-loop check, and theirs is the disagreement the ADR was written about:
            # granite's DataFusions declare `Number of Outputs: 1` while carrying zero
            # output-dataset rows — a present, non-zero number that is simply wrong.
            for excluded in self.non_executing:
                declared = excluded.get("declared_outputs")
                observed = excluded.get("observed_outputs")
                if declared is not None and declared != observed:
                    self.count_disagreements.append(
                        {
                            "asset": f"dataflow:{excluded['dataflow_id']}",
                            "name": excluded.get("name"),
                            "declared_outputs": declared,
                            "observed_outputs": observed,
                            "note": "object excluded by the population filter",
                        }
                    )

    def _assert_non_executing(self, tripped: Set[str], asserted: Set[str]) -> None:
        """State that the set tripping the structural filter is exactly the asserted class.

        Same posture as "discovery proposes and never selects": the collector makes a claim
        about the data it expects, and a mismatch surfaces a NEW OBJECT CLASS rather than
        shipping false positives about it. The filter itself is structural, so a new class
        is still correctly excluded — this is a report, not a gate.
        """
        unexpected = {e for e in tripped if e and e not in asserted}
        if not unexpected:
            return
        note = {
            "reason": "population-filter-assertion",
            "detail": (
                f"objects with ABSENT run history were excluded from the dataflow population "
                f"with engine(s) {sorted(unexpected)}, which the config does not assert as "
                f"non-executing ({sorted(asserted)}). They were correctly excluded — the "
                f"filter is structural — but this is an object class this design has not "
                f"seen. Review before trusting the dataflow counts."
            ),
            "engines": sorted(unexpected),
        }
        self.coverage_notes.append(note)

    def load_pause_history(
        self, result: Optional[Result], exception: Optional[dict] = None
    ) -> None:
        """Attach optional Activity Log actor/time context to currently disabled flows.

        Activity Log is historical and incomplete, while ``run_state`` is the current
        snapshot.  Therefore no event can create a paused state: this method only annotates
        assets already reading ``DISABLED``.  A missing report is one run-level coverage
        exception; a readable report with no event is a per-asset ``not-observed`` state.
        """
        if result is None:
            detail = (exception or {}).get("detail")
            self.coverage_notes.append(
                {
                    "reason": (exception or {}).get("reason", "dataset-missing"),
                    "detail": (
                        "Activity Log could not be read, so pause actor/time evidence is "
                        "unavailable. Current run state, candidate membership, ranking and "
                        "recommended action are unchanged."
                        + (f" Preflight: {detail}" if detail else "")
                    ),
                }
            )
            return

        by_object: Dict[str, List[dict]] = {}
        for row in result:
            object_type = row.get("Object_Type").as_str()
            action = row.get("Action").as_str()
            if object_type != "DATAFLOW_TYPE" or action not in ("DISABLED", "ENABLED"):
                raise CoverageError(
                    "activity_log query returned a row outside its DATAFLOW_TYPE / "
                    "DISABLED-or-ENABLED guard"
                )
            object_id = row.get("Object_ID").as_str()
            event_at = row.get("Event_Time").as_datetime()
            by_object.setdefault(object_id, []).append(
                {
                    "action": action,
                    "event_at": event_at,
                    "actor_user_id": row.get("User_ID").as_str(absent=None),
                    "actor_name": row.get("Name").as_str(absent=None),
                }
            )

        for asset in self.dataflows.values():
            if asset.run_state != "DISABLED":
                continue
            events = by_object.get(asset.asset_id, [])
            if not events:
                asset.pause_history = {
                    "state": "not-observed",
                    "latest_action": None,
                    "event_at": None,
                    "actor_user_id": None,
                    "actor_name": None,
                }
                continue

            latest_at = max(event["event_at"] for event in events)
            latest = [event for event in events if event["event_at"] == latest_at]
            actions = {event["action"] for event in latest}
            actors = {
                (event["actor_user_id"], event["actor_name"])
                for event in latest
            }
            unambiguous_disable = actions == {"DISABLED"} and len(actors) == 1
            representative = sorted(
                latest,
                key=lambda event: (
                    event["action"],
                    event["actor_user_id"] or "",
                    event["actor_name"] or "",
                ),
            )[0]
            asset.pause_history = {
                "state": "observed" if unambiguous_disable else "conflict",
                "latest_action": (
                    next(iter(actions)) if len(actions) == 1 else "CONFLICTING"
                ),
                "event_at": _iso_utc(latest_at),
                "actor_user_id": (
                    representative["actor_user_id"] if len(actors) == 1 else None
                ),
                "actor_name": (
                    representative["actor_name"] if len(actors) == 1 else None
                ),
            }

    def load_datasets(self, result: Result) -> None:
        for row in result:
            dataset_id = row.get("ID").as_str()
            asset = Asset(ASSET_DATASET, dataset_id, row.get("Name").as_str(absent="(unnamed)"))
            asset.deep_link = row.get("Link").as_str(absent=None)
            asset.owner_id = row.get("Owner_User_ID").as_str(absent=None)
            asset.schedule = row.get("Schedule").as_str(absent=None)
            asset.import_type = row.get("Import_Type").as_str(absent=None)
            asset.data_provider = row.get("Data_Provider").as_str(absent=None)

            # `Status` here is a LAST-RUN STATE, not a lifecycle state: SUCCESS 1005,
            # absent 597, IDLE 490, ERROR 98, ACTIVE 15. It is NOT the same column as
            # `Status` on the DomoStats DataFlow report, which overloads lifecycle and
            # last-run outcome and must never be read for `failing`.
            asset.status = row.get("Status").as_str(absent=None)

            last_run = row.get("Last_Run_Date")
            if last_run.is_absent:
                # Never loaded. A REAL finding, not a population exclusion: 45 on granite,
                # 18 of them reporting ERROR having never once succeeded.
                asset.run_history = RUN_NEVER
                asset.last_run = None
            else:
                asset.run_history = RUN_OBSERVED
                asset.last_run = last_run.as_datetime()

            asset.last_execution_status = asset.status
            asset.cards = self.lineage.card_count(dataset_id)
            asset.cards_with_no_page_recorded = self.lineage.cards_with_no_page_recorded(
                dataset_id
            )
            asset.pages = self.lineage.page_count(dataset_id)
            asset.downstream_dataflows = len(self.lineage.downstream_dataflows(dataset_id))
            asset.cost_entity_ids = {dataset_id}
            asset.source_datasets = sorted(self.lineage.produced_by.get(dataset_id, set()))

            self.datasets[dataset_id] = asset

        self._card_linkage_available = self.lineage.card_linkage_available
        if not self._card_linkage_available:
            self.conditions_unavailable.add(CONDITION_UNCONSUMED)
            self.coverage_notes.append(
                {
                    "reason": "dataset-missing",
                    "detail": (
                        "the card->dataset linkage report was unavailable, so no dataset "
                        "could be tested for `unconsumed`. `Cards_Powered` was NOT "
                        "substituted: ticket 02 found it unreliable, and treating "
                        "not-knowing as no-consumers would recommend quarantining consumed "
                        "datasets."
                    ),
                }
            )

    def load_schemas(self, schemas) -> None:
        """Typed column sets, and the twin-sets that follow from them.

        Called AFTER `load_dataflows`, because the population filter has to have run first:
        a non-executing object has no job to duplicate, and letting one pair with a real
        dataflow would put a DataFusion at the top of the cost ranking.

        A `None` here is a real state and it is honest about itself: `self.duplication`
        stays None, `duplicated` scores nothing, and a coverage note says the condition did
        not run. That is the `unconsumed`-without-card-linkage shape — not-knowing is never
        reported as none-found.
        """
        self._schemas = schemas
        if schemas is None:
            self.conditions_unavailable.add(CONDITION_DUPLICATED)
            self.coverage_notes.append(
                {
                    "reason": "dataset-missing",
                    "detail": (
                        "no dataset enumeration was available, so `duplicated` did not run "
                        "at either grain. The condition reads typed column sets from "
                        "`datasources/search`; without them, no two outputs can be compared "
                        "on shape and nothing is reported rather than something being "
                        "reported on the two axes that remain."
                    ),
                }
            )
            return

        shapes = [
            duplication_mod.DataflowShape(
                dataflow_id=asset.asset_id,
                inputs=asset.source_datasets,
                engine=asset.engine,
                outputs=asset.output_dataset_ids,
            )
            for asset in self.dataflows.values()
        ]
        self.duplication = duplication_mod.find(shapes, schemas.typed_columns)

        # Only the UNKNOWNS get a coverage note. The structurally excluded ones — a producer
        # with no observed inputs — are a ruling rather than a gap, they are recorded in
        # `coverage.duplication.excluded_*`, and calling them uncertain would report 18
        # uncertainties on granite where there are 8.
        #
        # The two unknown lists are separate because they fail differently: an untestable
        # OUTPUT loses one comparison, while an untestable DATAFLOW loses the "every output
        # is twinned" test entirely and would otherwise be judged on a subset of its own
        # outputs. Granite has seven of the first and none of the MIXED case today, which is
        # precisely the shape that ships a silent false positive elsewhere.
        if self.duplication.unknown_outputs:
            self.coverage_notes.append(
                {
                    "reason": "shape-mismatch",
                    "detail": (
                        f"{len(self.duplication.unknown_outputs)} dataflow output(s) could "
                        f"not be tested for `duplicated` and are reported as unknown rather "
                        f"than as not-duplicated: see coverage.duplication.unknown_outputs. "
                        f"Absence of a schema is not an empty schema (ADR-0003), so two "
                        f"datasets the instance describes no columns for are not twins."
                    ),
                }
            )
        if self.duplication.unknown_dataflows:
            self.coverage_notes.append(
                {
                    "reason": "shape-mismatch",
                    "detail": (
                        f"{len(self.duplication.unknown_dataflows)} dataflow(s) carry at "
                        f"least one untestable output, so `duplicated` could not be decided "
                        f"for them at the dataflow grain either way. A dataflow judged on "
                        f"only the outputs that happen to be readable would be reported as "
                        f"fully duplicated on evidence that does not cover it."
                    ),
                }
            )

    def load_consumption(self, counts) -> None:
        """The dataset enumeration's consumption counts — `unconsumed`'s FOURTH input.

        Ticket 28 decision 4: one consumption model, not a correction pass. The counts feed
        `_dataset_is_unconsumed`, whose existing three-state return already means the right
        thing; a post-scoring rewrite was rejected because it puts two things in the codebase
        that decide consumption, and ticket 19 is a 44-against-0 demonstration of what happens
        when two reads of one fact drift.

        `None` means the enumeration could not be read completely. That takes `unconsumed`
        OFF this run rather than falling back to governance alone: governance alone is exactly
        the state ticket 19 measured at 13 false positives in 803, and quietly reverting to it
        would reintroduce them without saying so. `duplicated` sets the same precedent, and
        reconciliation reads a prior candidate of an unevaluable condition as `unverifiable`
        rather than as resolved.
        """
        self._consumption_declared = True
        self._consumption = counts
        self._enumerated_dataset_ids = set(counts) if counts is not None else None
        self._resolve_tags(counts)
        if counts is not None:
            self._resolve_landing(counts)
            return
        self.conditions_unavailable.add(CONDITION_UNCONSUMED)
        self.coverage_notes.append(
            {
                "reason": "dataset-missing",
                "detail": (
                    "the dataset enumeration could not be read completely, so no dataset "
                    "could be tested for `unconsumed`. The condition needs the enumeration's "
                    "consumption counts: `DOMO | Cards and Datasets` and the dataflow edge "
                    "table together miss both a view/blend edge and a card hanging directly "
                    "off a dataset, which is 13 false positives in 803 on granite. Scoring "
                    "the condition on governance alone would reproduce them silently."
                ),
            }
        )

    def _resolve_tags(self, counts) -> None:
        """Attach raw dataset tags and labelled output-dataset tags (ticket 64).

        Dataflows do not expose a tag field. They therefore carry only their outputs' tags,
        never a flattened list that could be mistaken for tags on the dataflow itself.
        """
        for dataset_id, asset in self.datasets.items():
            asset.tags = counts.tags(dataset_id) if counts is not None else None
        for asset in self.dataflows.values():
            if counts is None:
                asset.output_tags = None
                continue
            asset.output_tags = [
                {"dataset_id": dataset_id, "tags": counts.tags(dataset_id)}
                for dataset_id in sorted(asset.output_dataset_ids)
            ]

    def _resolve_landing(self, counts) -> None:
        """`evidence.last_data_at` / `landing_history` for every loaded dataset (ticket 43).

        Zero new reads: the enumeration `load_consumption` is handed already carries
        `lastUpdated`, and `ConsumptionCounts.landing` hands it over as a typed value with
        the surface's `0`-means-never spelling already resolved to `absent`. Here the
        typed value is read the ADR-0003 way — branch on `.is_absent`, cast only a present
        one — so `never` is a positive statement the enumeration made, and a dataset it did
        not describe stays `unknown` rather than borrowing either answer.

        Dataflows are not touched. A dataflow's landing is its outputs', and every output
        is a dataset candidate carrying its own.
        """
        for dataset_id, asset in self.datasets.items():
            landed = counts.landing(dataset_id)
            if landed is None:
                asset.landing_history = LANDING_UNKNOWN
                asset.last_data_at = None
            elif landed.is_absent:
                asset.landing_history = LANDING_NEVER
                asset.last_data_at = None
            else:
                asset.landing_history = LANDING_OBSERVED
                asset.last_data_at = landed.as_datetime()

    def load_publications(self, publications, error: Optional[str] = None) -> None:
        """What this instance publishes — `unconsumed`'s FIFTH input (ticket 65).

        A dataset in a publication with a live subscriber feeds cards on another instance.
        No other input can see that: `Cards and Datasets` and the dataflow edge table are
        instance-local, and so are the enumeration's union fields and product lineage's
        impact index — on Altimate all seven union fields and lineage read 0 for every one
        of its 24 published head candidates, so the pre-filter never flags them and the
        adjudication pass is never asked. It has to be its own read, and it is a cheap one:
        bounded by the number of publications, not by the population.

        `None` means the publish API could not be read. Following `load_consumption`, that
        takes `unconsumed` OFF the run: the dataset whose publication went unread is exactly
        the one that would be scored wrongly, and scoring the rest as if publication had been
        checked is the silent revert the enumeration case forbids.
        """
        self._publications_declared = True
        self.publications = publications
        self.publications_error = error
        if publications is not None:
            return
        self.conditions_unavailable.add(CONDITION_UNCONSUMED)
        self.coverage_notes.append(
            {
                "reason": "connection-unavailable",
                "detail": (
                    "the publish API could not be read, so no dataset could be tested for "
                    "`unconsumed`. A dataset in a publication another instance subscribes to "
                    "is consumed — it feeds that instance's cards — and no governance report, "
                    "enumeration count or lineage call carries the edge. Scoring the "
                    "condition without this read would put a publisher's own governance "
                    "reports at the top of the head, which is what the first Altimate run did"
                    + (f": {error}" if error else "")
                ),
            }
        )

    def contested_datasets(self) -> Set[str]:
        """Datasets with no known card and an enumeration consumer signal.

        This is the set the bounded lineage pass adjudicates, and it is deliberately computed
        HERE rather than in `run.py`: a downstream dataflow is no longer a terminal answer.
        The closure needs lineage to distinguish a real consumer from another dead link, and
        the population filter has already removed non-executing objects by this point.

        Granite: 31 of 659 dataset candidates.
        """
        if self._consumption is None:
            return set()
        out: Set[str] = set()
        for dataset_id in self.datasets:
            if self.lineage.card_count(dataset_id) != 0:
                continue
            if self._consumption.flagged(dataset_id):
                out.add(dataset_id)
        return out

    def load_adjudication(self, adjudication) -> None:
        """The bounded lineage pass's verdicts over `contested_datasets`."""
        self.adjudication = adjudication
        if adjudication is None:
            return

        # The dead-chain evidence is SCOPED, and the scope has to be stated rather than left
        # for a reader to infer from a count. `unconsumed` is right for every dead chain
        # whether or not it was adjudicated; only the sentence naming the downstream dataset
        # depends on the pre-filter having flagged it, and on granite the filter reaches 1 of
        # the 6 dataset-grain chains. A reviewer who sees one chain described must not read
        # that as "there is one".
        described = sum(
            1 for e in adjudication.evidence.values() if e.get("reason") == "dead-chain"
        )
        if described:
            self.coverage_notes.append(
                {
                    "reason": "shape-mismatch",
                    "detail": (
                        f"{described} candidate(s) carry dead-chain evidence naming a "
                        f"downstream dataset that reaches no card. That evidence covers only "
                        f"the candidates the dataset enumeration flagged: a chain whose "
                        f"downstream is invisible to the enumeration's counts still scores "
                        f"`unconsumed` — the correct verdict — but gains no such sentence. "
                        f"The count here is therefore a floor on the instance's dead chains, "
                        f"never a total; the population census measures all of them."
                    ),
                }
            )

        if not adjudication.unreachable:
            return
        self.coverage_notes.append(
            {
                "reason": "endpoint-unreachable",
                "detail": (
                    f"product lineage was unreachable for "
                    f"{len(adjudication.unreachable)} contested dataset(s), which are "
                    f"reported as unknown rather than as unconsumed. `None` is not `False`: "
                    f"the enumeration flagged a consumer the collector could not then confirm "
                    f"or refute, and calling that no-consumer is the defect this pass closes."
                ),
            }
        )

    def attach_owner_names(self) -> None:
        for asset in list(self.datasets.values()) + list(self.dataflows.values()):
            if asset.owner_name or not asset.owner_id:
                continue
            person = self.people.get(asset.owner_id)
            if person:
                asset.owner_name = person.get("name")

    # -- conditions ----------------------------------------------------------------------

    def _governance_says_unconsumed(self, dataset_id: str) -> Optional[bool]:
        """The local, pre-ticket-22 condition: no cards and no downstream dataflow.

        It remains the bounded-lineage pre-filter's historical name. Scoring now separates
        direct consumers from dataflow edges, which lets the latter close over dead chains.
        """
        cards = self.lineage.card_count(dataset_id)
        if cards is None:
            return None
        return cards == 0 and not self.lineage.downstream_dataflows(dataset_id)

    def _dataset_direct_state(self, dataset_id: str) -> Optional[bool]:
        """Whether a dataset has no *terminal* consumer: True, False, or unknown.

        Dataflow edges intentionally do not decide this value. They are graph edges, not a
        reason to stop the question: a staging layer is consumed only when some path through
        it reaches a real consumer. Cards, views and alerts are terminal consumers. ``None``
        is contagious rather than optimistic, preserving the no-false-positive rule.
        """
        # The decision is shared with instance-documentation (D2 ticket 10); the pillar only
        # gathers its facts. Undeclared sources are skipped, declared-but-unread ones abstain.
        publication_live = conditions.NOT_DECLARED
        if self._publications_declared:
            publication_live = (
                None if self.publications is None else self.publications.live(dataset_id)
            )
        flagged = conditions.NOT_DECLARED
        if self._consumption_declared:
            flagged = None if self._consumption is None else self._consumption.flagged(dataset_id)
        verdict = reason = None
        if self.adjudication is not None:
            verdict = self.adjudication.verdict(dataset_id)
            reason = (self.adjudication.evidence.get(dataset_id) or {}).get("reason")
        return conditions.direct_consumption_state(
            self.lineage.card_count(dataset_id),
            publication_live=publication_live,
            flagged=flagged,
            verdict=verdict,
            verdict_reason=reason,
            has_downstream_dataflows=bool(self.lineage.downstream_dataflows(dataset_id)),
        )

    @staticmethod
    def _combine_successors(states: List[Optional[bool]]) -> Optional[bool]:
        """All paths must be dead: a real consumer wins, uncertainty abstains second."""
        return conditions.combine_successors(states)

    def _unconsumed_states(self, retained_unconsumed_ids: Set[str]) -> Dict[str, Optional[bool]]:
        """Greatest fixed point of the dataset -> dataflow -> dataset consumer graph.

        Starting every decidable node at ``True`` makes a closed component with no terminal
        consumer unconsumed. Repeatedly propagating terminal consumers and unknown edges then
        reaches the deterministic fixed point for chains, branches and cycles alike.
        """
        # The closure is shared with instance-documentation's consumption signal (D2 ticket
        # 09); which facts decide a dataset's DIRECT state stays here.
        retained = {
            candidate_id[: -len(f":{CONDITION_UNCONSUMED}")]
            for candidate_id in retained_unconsumed_ids
            if candidate_id.endswith(f":{CONDITION_UNCONSUMED}")
        }
        return conditions.unconsumed_states(
            dataset_ids=self.datasets,
            dataflow_outputs={
                dataflow_id: asset.output_dataset_ids
                for dataflow_id, asset in self.dataflows.items()
            },
            direct_state=self._dataset_direct_state,
            downstream_dataflows=self.lineage.downstream_dataflows,
            pinned_consumed=retained,
        )

    def _dataset_is_unconsumed(self, dataset_id: str) -> Optional[bool]:
        """Compatibility view of the graph result for one dataset.

        Consumption-completeness tests and callers that need the three-state answer still
        ask this question directly; the answer now comes from the same closure used by
        ``score()``, so there is no second consumption rule to drift from it.
        """
        return self._unconsumed_states(set()).get(f"{ASSET_DATASET}:{dataset_id}")

    def _unconsumed_evidence(self, dataset_id: str) -> Optional[dict]:
        """Why a contested candidate KEPT `unconsumed`, in the reviewer's own terms.

        Ticket 28 decision 5: the dead chains keep the condition and gain evidence naming the
        downstream dataset that terminates without a consumer. That hands ticket 22 its
        population — 7 on granite — and deliberately does not resolve it: whether a chain
        collapses into a single candidate is a scoring decision this ticket does not settle.

        Returns None for the uncontested majority, which need no extra sentence: governance
        saw no consumer and the enumeration agreed.

        Ticket 65 adds the one publication fact a KEPT candidate can carry: it is published
        to nobody. A publication with a live subscriber removes the candidate and never
        reaches here; one with no subscribers is evidence, not a consumer, and the reviewer
        deciding whether to quarantine should know a publication exists to be retired too.
        """
        extra: dict = {}
        evidence = self.adjudication.evidence.get(dataset_id) if self.adjudication else None
        if evidence:
            extra["unconsumed_adjudication"] = evidence.get("reason")
            extra["unconsumed_adjudication_detail"] = evidence.get("detail")
            if evidence.get("reason") == "dead-chain":
                extra["downstream_datasets_reaching_no_card"] = evidence.get(
                    "lineage_downstream_datasets"
                )
            if self._consumption is not None:
                # The counts that made it contested. A reviewer told the collector kept a
                # candidate DESPITE a consumption signal needs to see which signal spoke.
                signals = self._consumption.signals(dataset_id)
                if signals:
                    extra["enumeration_signals"] = signals
        if self.publications is not None:
            unsubscribed = self.publications.published_without_subscribers(dataset_id)
            if unsubscribed:
                extra["published_without_subscribers"] = unsubscribed
        return extra or None

    def _score_unconsumed(self, retained_unconsumed_ids: Optional[Set[str]] = None) -> List[Candidate]:
        retained_unconsumed_ids = retained_unconsumed_ids or set()
        baseline_states = self._unconsumed_states(set())
        states = self._unconsumed_states(retained_unconsumed_ids)
        out: List[Candidate] = []

        for dataset_id, asset in self.datasets.items():
            candidate_id = f"{asset.key}:{CONDITION_UNCONSUMED}"
            verdict = states.get(asset.key)
            if verdict is None:
                self.unevaluable_candidate_ids.add(candidate_id)
            if verdict or (
                candidate_id in retained_unconsumed_ids and baseline_states.get(asset.key) is True
            ):
                out.append(
                    Candidate(asset, CONDITION_UNCONSUMED, self._unconsumed_evidence(dataset_id))
                )

        for dataflow_id, asset in self.dataflows.items():
            candidate_id = f"{asset.key}:{CONDITION_UNCONSUMED}"
            verdict = states.get(asset.key)
            if verdict is None:
                self.unevaluable_candidate_ids.add(candidate_id)
            if not verdict and not (
                candidate_id in retained_unconsumed_ids and baseline_states.get(asset.key) is True
            ):
                continue
            outputs = asset.output_dataset_ids
            basis = "no outputs" if not outputs else f"all {len(outputs)} output path(s) end dead"
            out.append(Candidate(asset, CONDITION_UNCONSUMED, {"unconsumed_basis": basis}))
        return out

    def _score_stale(self) -> List[Candidate]:
        dataset_days = int(
            self.cfg.get("conditions", "stale", "dataset_days", default=conditions.STALE_DATASET_DAYS)
        )
        dataflow_days = int(
            self.cfg.get(
                "conditions", "stale", "dataflow_days", default=conditions.STALE_DATAFLOW_DAYS
            )
        )
        never_is_stale = bool(
            self.cfg.get(
                "conditions", "stale", "never_run_is_stale", default=conditions.NEVER_RUN_IS_STALE
            )
        )
        out: List[Candidate] = []

        for group, window in ((self.datasets, dataset_days), (self.dataflows, dataflow_days)):
            for asset in group.values():
                # Undefined age, not an infinite one, for a never-run object: `unconsumed`
                # already catches it, and a candidate reading "has not refreshed in 90 days"
                # about a thing that never refreshed at all is a false positive against a
                # gate with zero tolerance in the cost census. The rule is shared.
                days = conditions.stale_age(
                    asset.last_run, asset.run_history == RUN_NEVER, self.as_of, never_is_stale
                )
                asset.days_stale = days
                if conditions.is_stale(days, window):
                    out.append(Candidate(asset, CONDITION_STALE, {"stale_window_days": window}))
        return out

    def _score_failing(self) -> List[Candidate]:
        out: List[Candidate] = []

        # Datasets: the last ATTEMPT errored. Deliberately not gated on a completed run —
        # 18 granite datasets report ERROR with no load date at all, nine of them
        # netsuite-jdbc connections created Mar-Jun 2026 that have never once succeeded.
        # Those also score `unconsumed`; two candidates for one asset is the asset+condition
        # model working, not a duplicate.
        for asset in self.datasets.values():
            if conditions.dataset_failing(asset.status):
                out.append(
                    Candidate(
                        asset,
                        CONDITION_FAILING,
                        {"failing_basis": "Status = ERROR (last load attempt)"},
                    )
                )

        # Dataflows: `Last Execution Failed`. Absent means no attempt was ever made, which
        # is not a failure — those are the never-run population and `unconsumed` has them.
        for asset in self.dataflows.values():
            if conditions.dataflow_failing(asset.status):
                out.append(
                    Candidate(
                        asset,
                        CONDITION_FAILING,
                        {
                            "failing_basis": (
                                f"Last Execution Failed = true "
                                f"({asset.last_execution_status or 'state absent'})"
                            )
                        },
                    )
                )
        return out

    def _twin_cards(self, twins: List[str], is_dataflow: bool) -> Optional[int]:
        """Cards on the twins, or None when the linkage report was unavailable.

        None rather than 0, for the same reason `consumers.cards` is: not knowing is not the
        same as none, and a reviewer told "the alternative has no cards" would move traffic
        onto a dataset that has plenty.
        """
        if not self.lineage.card_linkage_available:
            return None
        total = 0
        for twin in twins:
            if is_dataflow:
                other = self.dataflows.get(twin)
                if other is None:
                    continue
                total += sum(
                    self.lineage.card_count(o) or 0 for o in other.output_dataset_ids
                )
            else:
                total += self.lineage.card_count(twin) or 0
        return total

    # -- the twin block (ticket 44, ticket 25 decision 8) -------------------------------------

    def _recurring_of(self, asset: Optional[Asset]) -> Optional[float]:
        """One asset's own recurring figure from the cost model — the same call `run.py`
        makes for the candidate row, so the twin's figure and the twin's own row agree."""
        if asset is None:
            return None
        if asset.asset_type == ASSET_DATAFLOW:
            return self.cost.dataflow_saving(asset.cost_entity_ids)
        return self.cost.dataset_saving(asset.asset_id)

    def _consumer_counts(self, asset: Optional[Asset]) -> dict:
        """Card and page COUNTS, null when the linkage report was unavailable. Page NAMES are
        deliberately absent: they need a per-card read the collector does not make."""
        if asset is None:
            return {"cards": None, "pages": None}
        return {"cards": asset.cards, "pages": asset.pages}

    def _output_detail(self, dataset_id: str) -> dict:
        """One output of a dataflow twin, with its own link: a dataflow's cards hang off its
        outputs, so 'card counts with links' for a dataflow twin means its outputs' links."""
        output = self.datasets.get(dataset_id)
        detail = {
            "dataset_id": dataset_id,
            "asset_name": output.name if output else None,
            "deep_link": output.deep_link if output else None,
            "recurring": self.cost.dataset_saving(dataset_id),
            "landing_history": output.landing_history if output else None,
            "last_data_at": _iso_utc(output.last_data_at) if output else None,
        }
        detail.update(self._consumer_counts(output))
        return detail

    def _twin_detail(self, this: Asset, twin_id: str, grain: str, producer: Optional[str]) -> dict:
        """Everything the record knows about ONE twin, so a reviewer comparing two rows is
        never handed a bare id. Every field is the twin's OWN — `consumers` on the candidate
        already carries this member's.

        `utilization` is null on purpose and the schema says so: the MVP measures no
        utilization for any asset, and the slot is carried so the twin block has the shape
        ticket 25 specified rather than silently omitting the axis. `schedule` is null on a
        dataflow twin by the recorded decision of ticket 56 — see
        `DATAFLOW_SCHEDULE_UNREAD` above for the census it was decided against — and a
        dataset twin carries its DomoStats `Schedule`. `last_data_at` is a dataset fact — a
        dataflow twin's landing is its outputs', which travel under `outputs` with their own
        links.
        """
        registry = self.dataflows if grain == duplication_mod.GRAIN_DATAFLOW else self.datasets
        twin = registry.get(twin_id)
        asset_type = ASSET_DATAFLOW if grain == duplication_mod.GRAIN_DATAFLOW else ASSET_DATASET
        detail = {
            "asset_type": asset_type,
            "asset_id": twin_id,
            "asset_name": twin.name if twin else None,
            "deep_link": twin.deep_link if twin else None,
            "owner": {
                "user_id": twin.owner_id if twin else None,
                "name": twin.owner_name if twin else None,
            },
            "recurring": self._recurring_of(twin),
            "recurring_unit": self.cost.recurring_unit,
            "schedule": twin.schedule if twin else None,
            "landing_history": twin.landing_history if twin else None,
            "last_data_at": _iso_utc(twin.last_data_at) if twin else None,
            "utilization": None,
            "name_stem_difference": name_stem_difference(this.name, twin.name if twin else ""),
        }
        detail.update(self._consumer_counts(twin))
        if grain == duplication_mod.GRAIN_OUTPUT:
            detail["producer_dataflow_id"] = producer
        else:
            detail["outputs"] = [
                self._output_detail(o) for o in sorted(twin.output_dataset_ids)
            ] if twin else []
        return detail

    def _twin_block(self, this: Asset, twin_ids: List[str], group, grain: str) -> dict:
        """The block both members of a duplicate set carry (ticket 25 decision 8).

        Symmetric by construction: A's block describes B and B's describes A, built by the
        same code from the same assets, so the two rows can never disagree about each other.
        Three set-level facts ride with it — group size, the shared-input count, and the
        set's `realisable` under the `sum - max` convention — plus the cost sentence, which
        states equal figures as equal and a difference as a difference.

        NOTHING here names a survivor. The convention's `max` is not surfaced as a member;
        `realisable` is a number about the set. Ticket 25 decision 9 ruled the survivor a
        human business judgment and its AI half out of the MVP.
        """
        producers = getattr(group, "producers", {}) or {}
        details = [
            self._twin_detail(this, twin_id, grain, producers.get(twin_id))
            for twin_id in twin_ids
        ]
        figures: List[Tuple[str, Optional[float]]] = [
            (f"{this.name} ({this.asset_id})", self._recurring_of(this))
        ] + [
            (f"{d['asset_name'] or d['asset_id']} ({d['asset_id']})", d["recurring"])
            for d in details
        ]
        return {
            "duplicate_twin_detail": details,
            # Identical across the set BY DEFINITION of the condition — the rule requires an
            # identical input dataset-id set — so this member's count is the set's.
            "duplicate_group_shared_inputs": len(this.source_datasets),
            "duplicate_group_realisable": {
                "recurring": realisable_by_convention([value for _, value in figures]),
                "recurring_unit": self.cost.recurring_unit,
                "compared_members": [this.asset_id] + list(twin_ids),
                "convention": REALISABLE_CONVENTION,
            },
            "duplicate_cost_comparison": cost_comparison_sentence(
                figures, self.cost.recurring_unit
            ),
        }

    def _score_duplicated(self) -> List[Candidate]:
        """Both grains, both members, every candidate carrying its group.

        Two grains because the remediations differ: a duplicated *dataflow* is unscheduled
        whole, while a duplicated *output* is disabled inside a dataflow that stays — and
        that second case is the one the dataflow grain cannot see (an operational dataflow
        carrying a redundant test output).

        Every candidate carries its own true metered cost, because that is what the entity
        bills and suppressing it would be its own lie. What it also carries is
        `counted_in_aggregate: false`, because the GROUP owns the saving: on granite three
        pairs display 1,320 credits/month across 14 candidates against 330 realisable.
        """
        if self.duplication is None:
            return []
        out: List[Candidate] = []

        for dataset_id, asset in self.datasets.items():
            group = self.duplication.output_set(dataset_id)
            if group is None:
                continue
            twins = group.twins_of(dataset_id)
            extra = {
                        "duplicate_group": group.group_id,
                        "duplicate_grain": group.grain,
                        "duplicate_group_size": len(group.members),
                        "duplicate_twins": twins,
                        "duplicate_twin_producers": [
                            group.producers[t] for t in twins if t in group.producers
                        ],
                        # The TWIN'S consumers, not this candidate's — `consumers` already
                        # carries those. "Decide which survives" is unanswerable without
                        # knowing what moving each way would cost: on granite the three pairs
                        # carry identical card counts on both sides, so realising the 990
                        # credits/90d means repointing 33 cards either way. That is the
                        # sentence the number has to support.
                        "duplicate_twin_cards": self._twin_cards(twins, is_dataflow=False),
                        "duplicated_basis": (
                            f"{len(twins)} twin output(s) from a different dataflow with an "
                            f"identical input dataset-id set, engine and typed column set"
                        ),
            }
            # The twin block (ticket 44): the twin by name, link and evidence, on BOTH
            # members, so `decide-which-survives` is performable from the record.
            extra.update(self._twin_block(asset, twins, group, group.grain))
            out.append(Candidate(asset, CONDITION_DUPLICATED, extra))

        for dataflow_id, asset in self.dataflows.items():
            group = self.duplication.dataflow_set(dataflow_id)
            if group is None:
                continue
            partners = self.duplication.dataflow_partners.get(dataflow_id, [])
            extra = {
                        "duplicate_group": group.group_id,
                        "duplicate_grain": group.grain,
                        "duplicate_group_size": len(group.members),
                        # The PARTNERS, not the group members. Where one dataflow's whole job
                        # is a strict subset of a bigger one's, retiring the smaller is the
                        # action and the bigger is not itself redundant — so its group has
                        # one member and the twin still has to be nameable.
                        "duplicate_twins": partners,
                        "duplicate_twin_cards": self._twin_cards(partners, is_dataflow=True),
                        "duplicated_basis": (
                            f"every one of {len(asset.output_dataset_ids)} output(s) is "
                            f"twinned by {'one' if len(partners) == 1 else len(partners)} "
                            f"other dataflow reading an identical input dataset-id set on "
                            f"the same engine"
                        ),
            }
            # Over the PARTNERS, as `duplicate_twins` is: in the asymmetric case the group
            # holds one member and the twin still has to be described, not just named.
            extra.update(self._twin_block(asset, partners, group, group.grain))
            out.append(Candidate(asset, CONDITION_DUPLICATED, extra))
        return out

    def score(self, retained_unconsumed_ids: Optional[Set[str]] = None) -> List[Candidate]:
        """Score findings, with active human keeps as chain endpoints only.

        A retained tail remains a candidate so the ledger can display its suppression and cost.
        The keep only stops new upstream ``unconsumed`` findings from being emitted.
        """
        self.unevaluable_candidate_ids = set()
        if self.duplication is not None:
            self.unevaluable_candidate_ids.update(
                f"{ASSET_DATASET}:{record['dataset_id']}:{CONDITION_DUPLICATED}"
                for record in self.duplication.unknown_outputs
            )
            self.unevaluable_candidate_ids.update(
                f"{ASSET_DATAFLOW}:{record['dataflow_id']}:{CONDITION_DUPLICATED}"
                for record in self.duplication.unknown_dataflows
            )
        candidates = (
            self._score_unconsumed(retained_unconsumed_ids)
            + self._score_stale()
            + self._score_failing()
            + self._score_duplicated()
        )
        # The live inventory is the presence authority for datasets. A scheduled DomoStats
        # row can legitimately outlive the object between connector refreshes; it remains
        # evidence of what the prior snapshot knew, but it must not keep scoring the removed
        # object under stale/failing while reconciliation is trying to close it.
        if self._enumerated_dataset_ids is not None:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.asset.asset_type != ASSET_DATASET
                or candidate.asset.asset_id in self._enumerated_dataset_ids
            ]
        # Deterministic ordering before ranking, so two runs over identical data produce an
        # identical document (ADR-0001).
        candidates.sort(key=lambda c: c.candidate_id)
        return candidates

    # -- what the run observed -----------------------------------------------------------

    @property
    def observed_keys(self) -> Set[str]:
        """`<asset_type>:<asset_id>` for every asset this run POSITIVELY OBSERVED,
        candidate or not.

        Reconciliation needs the full observed set rather than just the candidates:
        `resolved` requires positive observation, so a dataflow missing from
        `Dataflow Details` because the Governance report failed to refresh must never be
        reported as a resolved candidate. That is a real interface constraint on this
        class, which is why it is a property here rather than something derived from the
        findings afterwards.
        """
        return {a.key for a in list(self.datasets.values()) + list(self.dataflows.values())}

    @property
    def presence_keys(self) -> Set[str]:
        """Assets present on the source authoritative for current existence.

        Datasets come from the asserted-complete live enumeration, never the scheduled
        DomoStats snapshot. Dataflows have no equivalent live inventory, so their complete
        ``Dataflow Details`` grain remains the presence source.
        """
        keys = {asset.key for asset in self.dataflows.values()}
        if self._enumerated_dataset_ids is not None:
            keys.update(f"{ASSET_DATASET}:{dataset_id}" for dataset_id in self._enumerated_dataset_ids)
        return keys

    @property
    def presence_complete_types(self) -> Set[str]:
        """Asset types for which absence from ``presence_keys`` is meaningful this run."""
        complete = {ASSET_DATAFLOW}
        if self._enumerated_dataset_ids is not None:
            complete.add(ASSET_DATASET)
        return complete

    @property
    def consumption_counts(self):
        """The enumeration's consumption counts as loaded, or None. Read by the input
        fingerprint (ticket 42), which hashes what the condition was decided FROM; pair
        it with `stats()["consumption_signal"]` to tell unavailable from never declared."""
        return self._consumption

    @property
    def cost_entities(self) -> Set[str]:
        """Billed entity ids the pillar recognised — the attributed-coverage numerator."""
        out: Set[str] = set()
        for asset in list(self.datasets.values()) + list(self.dataflows.values()):
            out |= asset.cost_entity_ids
        return out

    @staticmethod
    def candidate_cost_entities(
        candidates: List[Candidate], suppressed_ids: Optional[Set[str]] = None
    ) -> Tuple[Set[str], Set[str]]:
        """`(actionable, fully_suppressed)` billed-entity ids under at least one candidate.

        The union of both grains, and it needs **no new query and no `Dataflow Details`
        join**: `cost_entity_ids` is already `{dataset_id}` for a dataset and the output set
        for a dataflow, so one union collapses the overlap that made ticket 34's naive row
        sum 49% too big.

        The split is by ANY, not by ALL, on purpose: an entity is actionable if *any* of its
        candidates is unsuppressed, and fully suppressed only if *every* one of them is.
        Deciding it per candidate would put an asset that is stale-suppressed and
        unconsumed-actionable on BOTH sides and count its spend twice — which is the same
        per-candidate-id mistake one level down.
        """
        suppressed_ids = suppressed_ids or set()
        actionable: Set[str] = set()
        suppressed: Set[str] = set()
        for candidate in candidates:
            target = suppressed if candidate.candidate_id in suppressed_ids else actionable
            target |= candidate.asset.cost_entity_ids
        return actionable, suppressed - actionable

    def stats(self) -> dict:
        return {
            "datasets_examined": len(self.datasets),
            "dataflows_examined": len(self.dataflows),
            "dataflows_excluded_non_executing": len(self.non_executing),
            "declared_vs_observed_disagreements": len(self.count_disagreements),
            "people_loaded": len(self.people),
            "card_linkage_available": self._card_linkage_available,
            "consumption_signal": (
                "unavailable" if self._consumption_declared and self._consumption is None
                else "present" if self._consumption_declared
                else "not-declared"
            ),
            "consumption": self._consumption.stats() if self._consumption else None,
            "adjudication": self.adjudication.stats() if self.adjudication else None,
            "publications_signal": (
                "unavailable" if self._publications_declared and self.publications is None
                else "present" if self._publications_declared
                else "not-declared"
            ),
            "publications": (
                dict(
                    self.publications.stats(),
                    # What governance alone would have scored wrongly: loaded datasets with
                    # a live subscriber and zero governance cards. 24 on Altimate's first run.
                    candidate_datasets_removed=sum(
                        1 for dataset_id in self.datasets
                        if self.lineage.card_count(dataset_id) == 0
                        and self.publications.live(dataset_id) is True
                    ),
                )
                if self.publications else None
            ),
            "duplication_available": self.duplication is not None,
            "conditions_unavailable": sorted(self.conditions_unavailable),
            "duplicate_groups_dataset": (
                len(self.duplication.output_sets) if self.duplication else None
            ),
            "duplicate_groups_dataflow": (
                len(self.duplication.dataflow_sets) if self.duplication else None
            ),
        }
