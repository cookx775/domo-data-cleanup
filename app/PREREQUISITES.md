# Prerequisite contract

What a Domo instance must provide before the cleanup collector can run against it. Written to
be handed to an administrator who has no context on this project.

The MVP is the **data pillar** — unconsumed, stale, failing and duplicated datasets and
dataflows, with metered credit cost. The content and user pillars are not in it.

## Two connectors, not one

| Connector | Reports the collector reads |
|---|---|
| **DomoStats** | `DataSets`, `Usage Metrics`, `Credit Usage`, `People`, `Activity Log` |
| **Domo Governance Datasets** | `Dataflow Details`, `Cards and Datasets` |

These are **separate Domo connectors**. Having one does not give you the other. Both are
standard Domo-provided connectors an instance administrator can add; neither requires custom
development. Adding them is typically minutes of work.

## Seven reports, two of them mandatory

The collector reads exactly seven reports. Two are **floor** reports: a floor report that is
missing, empty, archived or the wrong shape is a hard prerequisite failure with no fallback,
and the run aborts. The other five degrade independently — one blocked leg never stops the
others, and whatever did not happen is stated in the findings file as a **coverage note**
rather than being silently absent.

Two of the five are the **metering pair**, and they are alternatives rather than additions:
either one alone produces a full cost figure, and only losing **both** loses cost. Read the
metering rows together, not separately.

| Report | Connector | Floor | Carries | If it is missing |
|---|---|---|---|---|
| **Dataflow Details** | Governance | **yes** | the dataflow grain entire — engine, lineage edges, run and failure counts, enabled, draft | the run aborts |
| **DataSets** | DomoStats | **yes** | dataset identity, last run, status, schedule, import type, link | the run aborts |
| **Cards and Datasets** | Governance | no | the real card → dataset linkage | **no dataset is tested for `unconsumed`.** `Cards_Powered` is *not* substituted: it is an unreliable consumption signal, and treating not-knowing as no-consumers would recommend quarantining consumed datasets |
| **Usage Metrics** | DomoStats | no | metered *usage* per entity per day per SKU — executions, rows, tokens. The per-instance meter, and the one most instances have | see the two-step below |
| **Credit Usage** | DomoStats | no | the same meter **plus the credit conversion**. A *customer-level* report, held by whichever instance holds the contract — so most instances do not have one, and do not need one | see the two-step below |
| **People** | DomoStats | no | owner attribution | the owner-departed review stratum degrades; nothing else does |
| **Activity Log** | DomoStats | no | actor and time context for a current `DISABLED` dataflow | pause actor/time evidence is unavailable; current state, candidate membership and recommended action are unchanged |

Activity Log is historical context, not a lifecycle authority. The collector asks only for
events whose observed action is `DISABLED` or `ENABLED` and whose object type is
`DATAFLOW_TYPE`; those are values measured on the reference instance, not a claim that Domo's
action vocabulary is complete. A readable report can still carry no matching event for a
currently disabled dataflow, or its latest event can say `ENABLED`. Both outcomes are explicit
in candidate evidence, and the current `Dataflow Details`.`Run State` snapshot always wins.

### Two things the collector reads that are not reports

**The instance's own dataset list.** `duplicated` needs each dataset's **column schema**, and
no governance report carries one. `unconsumed` needs each dataset's **consumption counts**, and
no governance report carries those either. The collector reads both from the instance's own
dataset list — the same call it already makes to check the reports above are the shape it
expects — so there is **nothing extra to install** for either condition, and nothing extra to
grant: the token that reads the seven reports reads this too.

If that list cannot be read completely, **`duplicated` and `unconsumed` do not run** and the
findings file says so. Everything else runs normally. `unconsumed` is deliberately taken off the
run rather than falling back to the governance reports alone: on our own instance that fallback
mislabels 13 assets out of 803 as unused when they are being read, and a quiet fallback would
reproduce those without telling you.

You may see a line in the log saying the dataset list was re-read and unioned: that is expected
and is not an error — Domo's dataset listing occasionally serves the same page twice and skips
another, so the collector reads it more than once and combines the results rather than trusting
a single read.

That complete live list is also the dataset **presence** authority between runs. `DataSets` is a
scheduled connector snapshot and can still list an asset an administrator removed after its last
refresh. The collector keeps the two facts separate: a prior candidate absent from the complete
live list is **removed**, never `resolved`; if an approved, quarantined or deleted ledger record
already covers it, the next run closes it as **retired** without requiring a second manual update.
A ledger line saying `deleted` while the live list still contains the dataset is reported as a
lifecycle exception. If the list cannot be completed, none of these presence conclusions is made.

**Domo's own lineage view, for a handful of datasets per run.** Some datasets are read through a
Domo **view or blend**, or by a card's **drill-through**, and *no governance report records
either edge*. A dataset can therefore look completely unused while a live dashboard depends on
it. To avoid recommending you quarantine one of those, the collector asks Domo's lineage view
about **only** the datasets where the two sources disagree — the governance reports say nothing
reads it, the dataset list says something does.

The collector never caps that set: one call each, however many there are. Its size is a
property of your instance on the day, not a constant — on our own instance two runs on the same
date saw **31** and **846** contested datasets out of 2,205.

> **How long a run takes: a base cost, plus a cost per contested dataset.**
>
> The **base** is the governance queries — about two dozen, a few seconds in total — plus the
> dataset list: one paged read of every dataset on the instance, about 12 requests and 15–20
> seconds per read at 2,200 datasets. When a read comes back incomplete the list is re-read and
> unioned (see above), so a bad day is two to five reads of it.
>
> On top of that, **one lineage call per contested dataset, roughly 0.1–0.25 seconds each**.
> Thirty contested datasets is a few seconds; several hundred is a few minutes, and that is the
> whole cost, per run, on a monthly run. If your instance is larger the figure scales with the
> number of *disagreements*, not with the number of datasets.
>
> You do not need to estimate it. The run ends with a timing block — wall time first, then the
> query API, the dataset enumeration and product lineage each with their request count and
> seconds, and the contested workload comparison — so the figure you quote is
> the one you saw. The dataset-list line also divides its own two figures by the number of
> reads, so a **slow instance** (one read, slow) and an **unstable enumeration** (several
> reads, each of normal cost) do not arrive as the same number of seconds with nothing to tell
> them apart. A workload increase of at least 25% against the immediately preceding comparable run
> produces a warning (the percentage is configurable). Both runs must have usable consumption
> coverage and matching collector/config versions. Otherwise the comparison is unavailable,
> with the reason shown; the first run establishes a baseline. Any increase from zero warns
> without a percentage. Counts and elapsed time remain visible on every run. This is a warning only,
> because truncating this list is how a tool ends up telling you to delete something that is
> in use.
>
> Two dated observations from our own instance, for scale rather than as a promise. 2026-09-01:
> 31 contested datasets, 37 seconds wall-clock across three consecutive runs. 2026-09-02, with
> the timing block: 846 contested datasets, **168 seconds wall**, 908 requests — 3.9 s on the
> query API, 44.3 s on the dataset list (three reads unioned), 117.3 s on the 846 lineage calls
> (0.14 s each).
>
> **That 0.14 s is one run, not a rate.** Across our recorded runs the cost of one contested
> dataset works out between **0.13 and 0.25 seconds**. So there is no per-call rate to plan with,
> and none is quoted here as one. The figure for your instance is the timing block your own run
> printed.
>
> **When a run is slow, look at the dataset list before you look at lineage.** Two of our runs
> adjudicating the same 846 datasets differed by **68 seconds** of wall clock, and one read the
> dataset list five times where the other read it twice: three extra reads, about 45 of those 68
> seconds. Another run took 64 seconds with **no lineage pass at all** — four reads of the list.
> Counted per read, both are ordinary. Lineage sets how long a run takes; the number of dataset
> list reads is what makes two runs of the same instance disagree.

If the lineage view cannot be reached, those datasets are reported as **unknown** rather than as
unused, and the findings file names them and says why. Nothing else is affected.

**What your instance publishes, once per run.** A dataset copied into another Domo instance
through **Domo Publish** feeds that instance's cards, and *nothing on your instance records
that* — not a governance report, not the dataset list, not the lineage view. On the first
subsidiary run, the instance's own governance datasets led the unused list for exactly this
reason: their only reader was the copy on the parent instance. So the collector reads the
publish API — one list, one summary call, and one call per publication (a handful of requests;
seven publications is nine requests) — and a dataset in a publication that **at least one
instance subscribes to** is treated as in use. A dataset published to nobody stays on the list,
with the publication named beside it as a second thing to retire.

If the publish API cannot be read, the `unconsumed` condition **does not run** for that run and
the findings file says why. The tool never guesses about a reader it could not check. This read
has so far only been exercised with Admin-role tokens on both instances; the Domo authorities
that govern the publish area (`publish.publication.create`, `publish.subscribers.manage`) are
held by the Admin role alone in the default role set, so a non-admin token may be refused here.
If your token is refused, the fix is a token from a user who can open Admin > Publish, not a
change to the tool.

### What happens to cost when a metering report is missing

Two steps, not one, because the two reports are alternatives:

1. **No `Credit Usage`, but `Usage Metrics` present** — the normal case for an instance that does
   not hold the contract. You get a **full cost figure in executions per month** instead of
   credits per month. Every candidate is still costed, and the ranked head is still a ranked
   head: within one instance, ordering by executions reproduces the credit ordering almost
   exactly. What you lose is a *currency*, not an *ordering* — an execution count tells you what
   to look at first, and cannot tell you what you save in money or be added to another instance's
   total. Figures are labelled `metered-executions` and are **never converted** into credits;
   there is no conversion rate worth shipping, because credits consumed per execution varies
   nearly fivefold across instances at a single price and moves within a year inside one.
2. **Neither report** — candidates carry **no cost figure at all**, labelled `none`. Ranking
   falls through to storage, then age, so the findings are still ordered and every candidate
   still carries full evidence. Nothing is estimated in place of the missing meter.

Also note the storage figure changes shape, not just unit, between the two: `Credit Usage`
prices storage as a monthly charge, while `Usage Metrics` reports the rows themselves. So on
the executions basis "additional if deleted" is **rows under management**, not a per-month rate.
The findings file states the unit of every figure it carries.

Three reports earlier drafts of this contract named are **no longer read at all**: DomoStats
`DataFlow`, `DataFlow History` and `DataSets History` — the first two of them as hard
requirements. Run counts moved into `Dataflow Details`,
which keeps lifecycle and last-run outcome in separate columns instead of overloading one. The
two history tables are ~1.3M rows each and are exactly where the truncation hazard below bites,
so dropping them removed a class of failure as well as two prerequisites.

## Preflight verifies by querying, never by existence

**A dataset can exist, report a recent update time, and still be useless.** Four failure modes,
all seen live on real instances:

1. **Empty.** On one measured instance the `Domostats | People` dataset exists and returns **zero
   rows**. So do its `DataSets with Owners` and `People with Activity Log`. An existence check passes; the pillar
   then produces silently wrong output.
2. **Archived but fresh-looking.** A retired dataset can report a current `lastUpdated` and
   return `410 Gone` when queried.
3. **`rowCount` lies, in both directions.** The dataset-search API reports `0` for fully
   populated datasets — including one instance's entire 2,205-row dataset inventory — and reports
   fresh counts for archived ones. It is metadata, not an answer. Query, always.
4. **Silent truncation at 1,000,000 rows.** An unbounded `SELECT` returns exactly 1,000,000 rows
   with **HTTP 200 and no truncation flag anywhere in the response** — verified on a
   1,298,371-row table. Nothing in the payload says the answer is partial. Every query the
   collector issues therefore carries an explicit `LIMIT`, and the client raises when a result
   lands exactly on it rather than quietly analysing a prefix of your instance.

Preflight issues a real query against every required report and confirms a non-zero row count
and an expected column shape. Anything else is not a check.

A report found but **short a column** is reported by name — "found it, it is missing
`Execution Success Count`" — never as "nothing found". A connector release that drops one column
must not look identical to a connector that was never installed.

Preflight also **warns on staleness**: a report last refreshed more than three days ago is
called out. It is a warning, not a failure — but a run against a five-month-stale replica would
report April as if it were today, and the honest response is to say so loudly and let you decide.

**Match on column shape, not on dataset name.** Several instances carry
`Cards and Datasets (Deprecated)` after a Domo-side rename. The data is fine; the name moved.
Prefixes are no better: one instance prefixes `RTC` where its own naming convention says `RTP`,
and an instance's own reports are often unprefixed while copies of other instances' reports
sitting beside them are prefixed.

## One data path

**Every instance is read through its own DomoStats and Domo Governance connectors.** There is no
privileged path, no central source, and no version of this that someone else runs on your behalf.
Whoever runs it reads exactly what you read.

That is worth stating because the alternative was tried and abandoned: a design where some
instances were read from a centrally hosted pack and the rest got a poorer "second path". A path
only one instance can walk cannot be handed to anyone, and it turned out the governance
connector's `Dataflow Details` carries engine, lineage edges and run counts in one table — richer
than the central pack, not poorer. Nothing was given up by dropping it.

`Credit Usage` is the one report you may legitimately not have, and that is expected rather
than a gap: it is a **customer-level** report — one table carrying every domain under the
contract — so it lives with the instance that holds the contract, not with each instance under
it. `Usage Metrics` is the per-instance half of the same meter and is what your run will use.
See the two-step above: you get a fully costed, fully ordered findings file in executions, not
a broken run and not a degraded one.

Two consequences of reading each instance on its own terms:

- **Credits are comparable; credits-per-execution is not.** A credit costs the same everywhere in
  the portfolio, so a credit saved in your instance is a credit saved in anyone's. What differs is
  how many credits one execution *consumes* — 0.285–0.612 in one instance against 0.98–1.00 in
  another — because Domo splits every SKU into `legacy` / `standard` / `manual` categories at
  different rates and your workload decides the mix. That is also why published list rates are
  roughly 3.5x wrong for one instance we measured, and why the figure on a candidate is read from
  your own meter rather than modelled.
- **Cost is metered, not modelled — and never estimated.** The figure on a candidate is what
  Domo actually metered for that entity, read back from your own instance's meter. There is no
  rate card and no modelled figure anywhere in the package: a figure is `metered-credits`,
  `metered-executions`, or absent. Nothing is ever inferred from another instance's rates,
  including the parent's.

## Finding the dataset ids

`config.yaml` is authoritative — it is the only thing the collector reads ids from. Discovery
**proposes** ids by column shape and provenance, and never selects.

```bash
python -m collector.discover --instance <name>
```

writes `instances/<name>/cleanup/discovery.proposed.yaml`. **Review it, then move the entries you
accept into `config.yaml`.** You are not expected to hand-collect a dozen UUIDs unaided — but
three things must not be inferred:

1. **The proposal is never read by the collector.** Nothing happens until you move the ids across.
   The gap is deliberate: on an instance that hosts replicas of other companies' reports, many
   tables match the same shape, and the collector must not pick one for you.
2. **A report can come back `ambiguous`, and choosing is genuinely yours.** Discovery refuses to
   break a tie rather than binding the collector to a confident, well-formed, wrong table. Both
   obvious tiebreaks are disproven: *most rows* picks a 429,747-row migration extract over the
   real 1,812-row `Dataflow Details`, and a 2.9M-row usage derivative over the true dataset
   inventory — which reports `rowCount: 0` while being fully populated. *Most recently refreshed*
   ties ten ways. Every surviving candidate is probed with a real query, and the probe result in
   the file is the evidence to choose on.
3. **Discovery always exits zero, even when it found nothing.** A clean exit is not a green light.
   It reports every problem in one pass rather than aborting on the first, so that you fix them
   together instead of running the loop once per report. **Preflight is the gate:**

```bash
python -m shared.preflight --instance <name>
```

which fails on a missing floor report and reports everything else at once.

## What the administrator needs to supply

1. **Both connectors added, with their reports scheduled.**
2. **An API token belonging to a user who can read the governance datasets.** An Admin role is
   *not* required for the reports — those datasets can be shared to a non-admin, and demanding
   Admin would block a legitimate setup. Preflight asserts no role; it checks that the queries
   actually return rows, which is the only thing that matters and the only thing that can be
   checked. It also cannot check token expiry: nothing maps the token *in hand* back to its
   expiry record, so "does it work right now" is the honest test. The one read that may need
   more is the publish API (above): if it is refused, `unconsumed` does not run and the run says
   so, and the remedy is a token from a user who can see Admin > Publish.
3. **The dataset ids in `config.yaml`**, obtained by running discovery and reviewing its proposal
   as above.
4. **A computer with Python 3.10 to 3.15** and internet access to `pypi.org` during installation.
   The package's **Install Domo Cleanup** file creates a private `.venv` environment beside
   `app/` and installs into it, so nothing is installed system-wide and no C compiler is needed
   (the package carries a pure-Python PyYAML for 3.15). Without the installer, install from the
   `domo-cleanup` folder with `pip install --prefer-binary --find-links ./app/wheels -e ./app`,
   never `-e .`. To update, extract the new release into a new folder, copy `app/instances/`
   (each instance's `config.yaml` and `.env`) into it, and run its installer again.

That is the whole contract. No custom development, no Domo Workflow, no AI tooling.

## What the MVP will not give you

**A dataflow link that has been clicked through on your instance.** Dataset findings carry the
link the DomoStats report supplies. Dataflow findings carry a *constructed* link: `Dataflow
Details` holds both the numeric dataflow id the UI routes on and the Governance UUID beside it
(`DAP Dataflow ID`), so the collector builds
`https://<instance host>/datacenter/dataflows/<numeric id>/details` from your configured
`domain` and the row — no name join, no extra read. It does so only when the row's `Dataflow
ID` is a number and its `DAP Dataflow ID` is present; a row failing either test ships with no
link, and `coverage.deep_links` names it. The route is the one an administrator sees in the
browser; whether it opens the intended object under your account and access is confirmed by an
authenticated click-through, not by the collector.

**The user pillar.** Not in the MVP. The gap is real and worth knowing about: an instance's own
People report can be present and empty — we have measured instances where it is — and the only
alternative that ever worked was a centrally hosted rollup no administrator can reach from their
own instance. Here, `People` is a
non-floor input to the review protocol only. If yours is empty, the owner-departed review stratum
degrades and nothing else breaks — and being told your People pipeline is empty is itself useful
output, because most administrators do not know.

**The content pillar** — low-reach and orphaned cards, pages and apps. Out of the MVP; it follows
a working data pillar.

## What the system will never do

Candidate datasets and dataflows are read-only. Cleanup findings and decisions are written to AppDB.
Every output is a recommendation for a human to act on. See
[`adr/0002-recommend-only-quarantine-as-deprecate.md`](adr/0002-recommend-only-quarantine-as-deprecate.md).


## Shared cleanup storage

Normal cleanup runs require a shared AppDB store in the target instance. The app automatically discovers
the standard Cleanup Runs and Cleanup Review collections, or creates them in a Domo Instance
Cleanup store when absent. Each reviewer's token
needs read, read-content, create-content and update-content access to both collections.
Creating the store and granting access require the corresponding Domo permissions. These
cleanup documentation writes do not authorize changes to candidate datasets or dataflows.
Use the package README for connection, migration, backup and restore instructions.
