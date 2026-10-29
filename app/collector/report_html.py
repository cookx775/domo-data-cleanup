"""Deterministic HTML decision surface over one findings artifact.

The static file is portable and read-only.  When the localhost guide renders the same
document it supplies an :class:`Interaction`; that adds native HTML forms, but does not
change which candidates appear or how they are grouped.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from . import glossary as glossary_mod
from . import ledger as ledger_mod
from . import report as report_mod
from . import verdict as verdict_mod


@dataclass(frozen=True)
class Interaction:
    instance: str
    run_id: str
    reviewer: str
    csrf: str
    preview_path: str = "/action/decision-preview"
    revision: Optional[int] = None


def _h(value) -> str:
    return html.escape("unknown" if value is None or value == "" else str(value), quote=True)


def _condition_class(condition: Optional[str]) -> str:
    """The class that carries one condition's hue, or nothing for a condition we do not know.

    Colour here is a second encoding of a word that is always printed beside it, so an unknown
    condition simply renders in ink rather than borrowing a hue that would mean something else.
    """
    if condition in report_mod.CONDITION_ORDER:
        return f"cond-{condition}"
    return ""


def _row_condition(row: dict) -> Optional[str]:
    """The one condition an asset row is about, or None when it carries more than one.

    A dataset that is both stale and unconsumed has no single hue to fly, and inventing a
    dominant one would assert a priority the scoring never expressed.  Those rows stay in ink.
    """
    conditions = {candidate.get("condition") for candidate in row.get("candidates") or []}
    if len(conditions) == 1:
        only = conditions.pop()
        return only if only in report_mod.CONDITION_ORDER else None
    return None


def _definitions(cfg=None, document=None) -> Dict[str, str]:
    return {
        term: definition
        for _, entries in glossary_mod.sections(cfg, document)
        for term, _, definition in entries
    }


def _hidden(interaction: Interaction, mode: str) -> str:
    return (
        f'<input type="hidden" name="csrf" value="{_h(interaction.csrf)}">'
        f'<input type="hidden" name="instance" value="{_h(interaction.instance)}">'
        f'<input type="hidden" name="run_id" value="{_h(interaction.run_id)}">'
        f'<input type="hidden" name="revision" value="{_h(interaction.revision if interaction.revision is not None else "")}">'
        f'<input type="hidden" name="mode" value="{_h(mode)}">'
    )


def _choices(names: Dict[str, str], definitions: Dict[str, str]) -> str:
    groups = (
        ("Verdict", ledger_mod.VERDICT_VALUES),
        ("Lifecycle", ledger_mod.LIFECYCLE_VALUES),
    )
    fields: List[str] = []
    for label, values in groups:
        name = names[label.casefold()]
        radios = "".join(
            f'<label class="choice"><input type="radio" name="{_h(name)}" value="{_h(value)}">'
            f'<span><strong>{_h(value)}</strong><small>{_h(definitions[value])}</small></span></label>'
            for value in values
        )
        fields.append(
            f'<fieldset><legend>{label}</legend><div class="choices">{radios}</div></fieldset>'
        )
    return "".join(fields)


def _candidate(candidate: dict, interactive: bool, definitions: Dict[str, str]) -> str:
    candidate_id = str(candidate.get("candidate_id") or "")
    evidence = verdict_mod._evidence_line(candidate)
    cost = candidate.get("cost") or {}
    recurring = cost.get("recurring")
    unit = cost.get("recurring_unit") or cost.get("unit")
    cost_line = "not measured" if recurring is None else f"{recurring} {unit or ''}".strip()
    controls = ""
    if interactive:
        controls = (
            f'<div class="decision">{_choices({"verdict": "verdict:" + candidate_id, "lifecycle": "lifecycle:" + candidate_id}, definitions)}'
            f'<label class="reason">Reason<input type="text" name="reason:{_h(candidate_id)}" '
            'placeholder="Why is this the right decision?"></label></div>'
        )
    recorded = ""
    if (candidate.get("ledger") or {}).get("suppressed"):
        recorded = (
            '<p class="recorded"><strong>Recorded decision:</strong> '
            f'{_h(report_mod.recorded_decision_text(candidate))}</p>'
        )
    # The second column exists only where there is something to decide with; the read-only
    # export keeps the evidence in one measure-friendly column.
    shape = "candidate decidable" if interactive else "candidate"
    condition = candidate.get("condition")
    return f'''<article class="{shape}" data-candidate-id="{_h(candidate_id)}">
<div class="facts"><div><span class="condition {_condition_class(condition)}">{_h(condition)}</span> <code>{_h(candidate_id)}</code></div>
<p class="guidance"><strong>{_h(report_mod.recommended_action_text(candidate))}</strong></p>
{recorded}<p>{_h(evidence)}</p><p class="muted">Recurring: {_h(cost_line)}</p></div>{controls}</article>'''


def _entry_label(row: dict) -> str:
    """Give enough condition, owner and cost context to choose what to open."""
    candidates = row['candidates']
    conditions = ', '.join(sorted({str(c.get('condition') or 'unknown') for c in candidates}))
    actions = '; '.join(dict.fromkeys(report_mod.recommended_action_text(c) for c in candidates))
    costs = [(c.get('cost') or {}) for c in candidates]
    # Multiple conditions on one asset repeat its cost; do not sum them.
    cost = next((c for c in costs if c.get('recurring') is not None), {})
    amount = f"{cost['recurring']:g} {cost.get('recurring_unit') or cost.get('unit') or ''}" if cost else 'cost not measured'
    return (f"{row.get('asset_name') or 'unknown'} — {row.get('asset_type') or 'unknown'} · "
            f"{conditions} · owner {row.get('owner') or 'Unassigned'} · {row.get('band') or 'unbanded'} · "
            f"{amount} · {actions}")


def _collapse_after(entries: List[Tuple[str, str]]) -> str:
    """Every entry is independently collapsible; native forms retain hidden selections."""
    def label_markup(label):
        name, separator, detail = label.partition(' — ')
        return '<span class="asset-name">' + _h(name) + '</span>' + (_h(separator + detail) if separator else '')
    return ''.join(f'<details class="entry"><summary>{label_markup(label)}</summary>{body}</details>'
                   for label, body in entries)


def _duplicate_group(row: dict) -> Optional[str]:
    for candidate in row.get("candidates") or []:
        if candidate.get("condition") == "duplicated":
            evidence = candidate.get("evidence") or {}
            return str(evidence.get("duplicate_group") or candidate.get("candidate_id"))
    return None


def _head_blocks(rows: List[dict]) -> List[Tuple[Optional[str], List[dict]]]:
    """Keep every duplicate group contiguous without changing rank order between blocks."""
    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        group = _duplicate_group(row)
        if group:
            grouped.setdefault(group, []).append(row)
    emitted = set()
    blocks: List[Tuple[Optional[str], List[dict]]] = []
    for row in rows:
        group = _duplicate_group(row)
        if group:
            if group in emitted:
                continue
            emitted.add(group)
            blocks.append((group, grouped[group]))
        else:
            blocks.append((None, [row]))
    return blocks


def _asset(row: dict, interactive: bool, definitions: Dict[str, str]) -> str:
    title = _h(row.get("asset_name"))
    if row.get("deep_link"):
        title = f'<a href="{_h(row["deep_link"])}" target="_blank" rel="noreferrer">{title}</a>'
    rank = f'Rank {_h(row.get("best_rank"))} · ' if row.get("best_rank") is not None else ""
    candidates = "".join(_candidate(candidate, interactive, definitions) for candidate in row["candidates"])
    hue = _condition_class(_row_condition(row))
    return f'''<section class="asset {hue}"><h3>{title}</h3>
<p class="meta">{rank}{_h(row.get("asset_type"))} {_h(row.get("asset_id"))} · owner {_h(row.get("owner"))} · band {_h(row.get("band"))}</p>
{candidates}</section>'''


def _head(rows: List[dict], interaction: Optional[Interaction], definitions: Dict[str, str]) -> str:
    entries: List[Tuple[str, str]] = []
    for group, group_rows in _head_blocks(rows):
        contents = "".join(_asset(row, interaction is not None, definitions) for row in group_rows)
        if group:
            contents = _collapse_after([(_entry_label(row), _asset(row, interaction is not None, definitions)) for row in group_rows])
            candidates = sum(len(row["candidates"]) for row in group_rows)
            label = ('Twins: ' + ' / '.join(str(row.get('asset_name') or row.get('asset_id')) + ' [' + str(row.get('asset_id')) + ']' for row in group_rows)
                     + f' — {len(group_rows)} assets · {candidates} candidate(s) · Decide which survives')
            entries.append((label, f'<section class="twins"><h3>Twin group {_h(group)}</h3>{contents}</section>'))
        else:
            entries.append((_entry_label(group_rows[0]), contents))
    body = _collapse_after(entries) or '<p class="muted">No candidates require individual decisions in this selection.</p>'
    if interaction:
        body = (
            f'<form method="post" action="{_h(interaction.preview_path)}">'
            f'{_hidden(interaction, "head")}{body}'
            '<button type="submit">Preview selected decisions</button></form>'
        )
    return f'<section id="head"><h2>Individual decisions</h2><p>Work these candidates one at a time. Nothing is written until the next page is confirmed.</p>{body}</section>'


def _recorded(rows: List[dict], interaction: Optional[Interaction], definitions: Dict[str, str]) -> str:
    body = _collapse_after(
        [(_entry_label(row), _asset(row, interaction is not None, definitions)) for row in rows]
    )
    if not body:
        body = '<p class="muted">No candidates are covered by a recorded decision in this selection.</p>'
    elif interaction:
        body = (
            f'<form method="post" action="{_h(interaction.preview_path)}">'
            f'{_hidden(interaction, "recorded")}{body}'
            '<button type="submit">Preview revised decisions</button></form>'
        )
    return (
        '<section id="recorded"><h2>Decisions already recorded</h2>'
        '<p>Visible for audit and explicit revision; no current action is due.</p>'
        f'{body}</section>'
    )


def _backlog_groups(rows: Iterable[dict]) -> List[Tuple[str, str, List[dict]]]:
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for row in rows:
        groups.setdefault((row["owner"], row["band"]), []).append(row)
    return [
        (owner, band, groups[(owner, band)])
        for owner, band in sorted(groups, key=lambda key: (key[0].casefold(), key[1]))
    ]


def _backlog(rows: List[dict], interaction: Optional[Interaction], definitions: Dict[str, str]) -> str:
    sections = []
    for owner, band, group_rows in _backlog_groups(rows):
        candidate_count = sum(len(row["candidates"]) for row in group_rows)
        sample = "".join(
            f'<li><span class="asset-name">{_h(row.get("asset_name"))}</span> <span class="muted">({_h(len(row["candidates"]))} candidate(s))</span>'
            + "".join(
                f'<code data-candidate-id="{_h(candidate.get("candidate_id"))}">{_h(candidate.get("candidate_id"))}</code>'
                for candidate in row["candidates"]
            )
            + "</li>"
            for row in group_rows
        )
        controls = ""
        if interaction:
            controls = (
                f'<form method="post" action="{_h(interaction.preview_path)}">{_hidden(interaction, "backlog")}'
                f'<input type="hidden" name="owner" value="{_h(owner)}">'
                f'<input type="hidden" name="band" value="{_h(band)}">'
                f'{_choices({"verdict": "group_verdict", "lifecycle": "group_lifecycle"}, definitions)}'
                '<label class="reason">Reason for every member<input type="text" name="group_reason" '
                'placeholder="Why does this apply to the whole group?"></label>'
                '<label><input type="checkbox" name="revisit" value="yes"> Include candidates already decided</label>'
                '<button type="submit">Preview eligible ledger lines</button></form>'
            )
        sections.append(
            f'<details class="backlog"><summary>{_h(owner)} · {_h(band)} · {candidate_count} candidate(s) · '
            f'{_h(", ".join(sorted({str(c.get("condition")) for row in group_rows for c in row["candidates"]})))} · '
            f'<span class="asset-name">{_h(" / ".join(str(row.get("asset_name") or "unknown") for row in group_rows[:2]))}</span>'
            f'{" …" if len(group_rows) > 2 else ""}</summary>'
            f'<ul>{sample}</ul>{controls}</details>'
        )
    return '<section id="backlog"><h2>Owner-grouped review</h2><p>A group decision expands to one ordinary ledger line per candidate.</p>' + ("".join(sections) or '<p class="muted">No grouped-review candidates in this selection.</p>') + '</section>'


def _glossary(cfg=None, document=None) -> str:
    sections = []
    for heading, entries in glossary_mod.sections(cfg, document):
        items = "".join(f'<dt>{_h(term)}</dt><dd>{_h(definition)}</dd>' for term, _, definition in entries)
        sections.append(f'<h3>{_h(heading)}</h3><dl>{items}</dl>')
    return '<section id="glossary"><h2>Glossary</h2>' + "".join(sections) + '</section>'


def _summary(document: dict) -> str:
    summary = document.get("summary") or {}
    count = summary.get("candidate_count")
    if count is None:
        count = len(document.get("candidates") or [])
    by_condition = summary.get("by_condition") or {}
    # The total is set apart rather than tinted: the conditions do not sum to it (one asset that
    # is both stale and unconsumed is two candidates), and a matching tile would imply they do.
    tiles = [f'<div class="total"><strong>{_h(count)}</strong><span>Candidates</span></div>']
    for condition in report_mod.CONDITION_ORDER:
        tiles.append(
            f'<div class="{_condition_class(condition)}"><strong>{_h(by_condition.get(condition, 0))}</strong>'
            f'<span>{_h(condition)}</span></div>'
        )
    return '<section class="summary" aria-label="Run summary">' + "".join(tiles) + '</section>'


def _audit_section(anchor: str, title: str, lines: List[str]) -> str:
    """Carry the Markdown renderer's audit facts without introducing a second parser."""
    payload = "\n".join(line for line in lines if not line.startswith("## ")).strip()
    return (
        f'<section class="audit" id="{_h(anchor)}"><h2>{_h(title)}</h2>'
        f'<pre>{_h(payload)}</pre></section>'
    )


def _detail_tail(lines: List[str], keep_from: str) -> str:
    """Whatever the Markdown digest carries beyond the part rendered above, or nothing.

    The structured sections below present the figures a reader needs first.  The per-entry
    lists a reconciliation can also produce — returned, removed and retired assets, register
    and exception entries, rejected ledger lines — are unbounded prose, so they stay behind a
    disclosure in the renderer's own words rather than being dropped or re-parsed.
    """
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(keep_from))
    except StopIteration:
        return ""
    payload = "\n".join(lines[start:]).strip()
    if not payload:
        return ""
    return (
        '<details class="audit-detail"><summary>Full reconciliation detail</summary>'
        f'<pre>{_h(payload)}</pre></details>'
    )


def _change_section(document: dict, lines: List[str]) -> str:
    """The nine change counts as a labelled register, the one that moved given the weight."""
    prior = (document.get("run") or {}).get("prior_run_id")
    lead = (
        f'Compared against run <code>{_h(prior)}</code>.'
        if prior
        else "No prior run — this is the baseline."
    )
    rows = report_mod.change_count_rows(document)
    largest = max((count for _, count in rows), default=0)
    cells = []
    for name, count in rows:
        label, qualification = report_mod.change_label_parts(name)
        note = f'<span class="qualifies">{_h(qualification)}</span>' if qualification else ""
        lead_row = " lead" if count and count == largest else ""
        cells.append(
            f'<div class="count{lead_row}"><b>{_h(count)}</b>'
            f'<div><strong>{_h(label)}</strong>{note}</div></div>'
        )

    reconciliation = document.get("reconciliation") or {}
    ledger_coverage = ((document.get("coverage") or {}).get("ledger") or {})
    registers = [
        (len(reconciliation.get("retirement_register") or []), "terminal in the retirement register"),
        (len(reconciliation.get("lifecycle_exceptions") or []), "lifecycle exceptions"),
        (len(reconciliation.get("unmatched_verdicts") or []), "unmatched verdicts"),
        (len(ledger_coverage.get("malformed") or []), "rejected ledger lines"),
    ]
    register_row = "".join(
        f'<div><b>{_h(value)}</b> <span>{_h(text)}</span></div>' for value, text in registers
    )
    return (
        '<section id="change"><h2>Change since the prior run</h2>'
        f'<p class="lead">{lead}</p>'
        f'<div class="counts">{"".join(cells)}</div>'
        f'<div class="registers">{register_row}</div>'
        f'{_detail_tail(lines, "Returned candidates")}</section>'
    )


def _coverage_section(document: dict) -> str:
    """Pillars and conditions with their status first, because status governs how to read them."""
    pillars = "".join(
        f'<div class="cov"><div>{_status_chip(status)}</div>'
        f'<div><strong>{_h(pillar)}</strong>'
        + (f'<span class="qualifies">{_h(detail)}</span>' if detail else f' <span class="muted">· {_h(examined)} assets examined</span>')
        + '</div></div>'
        for pillar, status, examined, detail in report_mod.coverage_pillar_rows(document)
    )
    conditions = "".join(
        f'<div class="cov {_condition_class(condition)}{" degraded" if cause else ""}">'
        f'<div>{_status_chip(status)}</div>'
        f'<div><strong class="named">{_h(condition)}</strong>'
        + (f'<span class="qualifies">{_h(cause)}</span>' if cause else '')
        + f'</div><div class="figure">{_h(count)}</div></div>'
        for condition, status, cause, count in report_mod.coverage_condition_rows(document)
    )
    qualifications = "".join(
        f'<div class="qual"><strong>{_h(label)}</strong> <span>{_h(prose)}</span></div>'
        for label, prose in report_mod.coverage_qualification_rows(document)
    )
    return (
        '<section id="coverage"><h2>Coverage digest</h2>'
        '<p class="lead">A pillar or condition that did not run is a named coverage exception, '
        'never a silent omission.</p>'
        f'<div class="covgrid"><div><h3>Pillars</h3><div class="covbox">{pillars}</div></div>'
        f'<div><h3>Conditions</h3><div class="covbox">{conditions}</div></div></div>'
        f'<h3>Qualifications</h3><div class="covbox">{qualifications}</div>'
        '<p class="muted">The full per-asset coverage arrays remain in the stored findings; this '
        'digest never reproduces them.</p></section>'
    )


def _status_chip(status: str) -> str:
    """`degraded` is a property of the run, not of a condition, so it never takes a condition hue."""
    kind = {"ran clean": "clean", "degraded": "warn", "did not run": "absent"}.get(status, "absent")
    return f'<span class="status {kind}">{_h(status)}</span>'


def _watch_section(document: dict, owner: Optional[str]) -> str:
    """Elapsed quarantine as a meter, because `N / M days elapsed` is a progress statement."""
    if owner is not None:
        return (
            '<section id="watch"><h2>Watch list</h2><p>Owner supplements do not repeat the watch '
            'list: watch-list records carry no owner, so filtering them would invent an '
            'attribution. The master report carries it.</p></section>'
        )
    rows = report_mod.watch_rows(document)
    if not rows:
        return (
            '<section id="watch"><h2>Watch list</h2>'
            '<p class="muted">No assets are waiting out a quarantine period.</p></section>'
        )
    items = []
    for row in rows:
        period = row["period"] or 1
        pct = min(100, max(0, round(100 * row["elapsed"] / period)))
        remaining = max(0, row["period"] - row["elapsed"])
        items.append(
            f'<div class="wrow"><div class="whead"><strong>{_h(row["asset_name"])}</strong>'
            f'<span>{_h(row["elapsed"])} / {_h(row["period"])} days</span></div>'
            f'<div class="meter"><i style="width:{pct}%"></i></div>'
            f'<p class="muted">Quarantined {_h(row["quarantined"])} · <code>{_h(row["candidate_id"])}</code> · '
            f'{_h(remaining)} day(s) before deletion becomes eligible. Elapsed time is the only '
            'signal here; it is never evidence that nobody complained.</p></div>'
        )
    return (
        '<section id="watch"><h2>Watch list</h2>'
        f'<div class="watch">{"".join(items)}</div></section>'
    )


#: (anchor, nav label) in document order.  Not every section renders in every view, so the nav
#: is filtered against the sections that actually did.
_NAV = [
    ("change", "Change"),
    ("unmatched", "Unmatched"),
    ("coverage", "Coverage"),
    ("head", "Individual decisions"),
    ("watch", "Watch list"),
    ("recorded", "Recorded"),
    ("backlog", "Owner-grouped"),
    ("glossary", "Glossary"),
]


def _nav(present: Iterable[str]) -> str:
    """A sticky index, because the glossary sits at the foot of a page this long.

    It lists only the sections this view actually rendered: a run with no unmatched verdicts,
    or a read-only export with no withdrawal to offer, is never given a link that goes nowhere.
    """
    anchors = set(present)
    links = "".join(
        f'<a href="#{_h(anchor)}">{_h(label)}</a>'
        for anchor, label in _NAV
        if anchor in anchors
    )
    return f'<nav class="jump" aria-label="Report sections">{links}</nav>'


def _unmatched_corrections(document: dict, interaction: Optional[Interaction]) -> str:
    """Offer withdrawal only where it belongs: an active unmatched ledger identity."""
    unmatched = (document.get("reconciliation") or {}).get("unmatched_verdicts") or []
    if not interaction or not unmatched:
        return ""
    forms = []
    for entry in unmatched:
        candidate_id = str(entry.get("candidate_id") or "")
        forms.append(
            '<details class="entry"><summary>'
            f'<span class="asset-name">{_h(entry.get("asset_name_at_verdict") or candidate_id)}</span>'
            f' — unmatched record · {_h(entry.get("axis"))}: {_h(entry.get("value"))}</summary>'
            f'<form class="asset" method="post" action="{_h(interaction.preview_path)}">'
            f'{_hidden(interaction, "withdraw")}<input type="hidden" name="candidate_id" '
            f'value="{_h(candidate_id)}"><h3>{_h(entry.get("asset_name_at_verdict") or candidate_id)}</h3>'
            f'<p><code>{_h(candidate_id)}</code> · {_h(entry.get("axis"))}={_h(entry.get("value"))}</p>'
            '<label class="reason">Why was this candidate identity invalid?'
            '<input type="text" name="withdraw_reason" placeholder="For example: asset id was mistyped"></label>'
            '<button type="submit">Preview withdrawal</button></form></details>'
        )
    return (
        '<section id="unmatched"><h2>Correct unmatched records</h2>'
        '<p>Withdraw only a candidate identity that was never valid. Investigate a real asset whose id changed.</p>'
        + "".join(forms)
        + "</section>"
    )


#: One stylesheet, kept out of the f-string so the CSS reads as CSS.  Two rules earn their
#: complexity: the candidate is a two-column grid on a wide screen, so the evidence and the
#: controls that judge it are read side by side rather than one below the other; and the jump
#: nav is sticky, so the glossary is one click away from anywhere on the page.
_STYLE = """
:root{--ink:#17202a;--muted:#5b6470;--line:#dce2e8;--paper:#fff;--wash:#f4f7f9;--blue:#1769aa;--gold:#9a6700;--red:#a12b2b;--green:#27633e}
/* One hue per condition, as a second encoding of a word that is always printed beside it.
   Each clears 4.5:1 on both its own tint and the page wash; a row whose condition we cannot
   name simply inherits ink, so colour never carries meaning on its own. */
.cond-unconsumed{--hue:#2f3f7a;--tint:#e6e8f4}
.cond-stale{--hue:#73501c;--tint:#f2e9da}
.cond-failing{--hue:#8a2f2a;--tint:#f6e4e1}
.cond-duplicated{--hue:#1f5c52;--tint:#dfece9}
*{box-sizing:border-box}
body{margin:0;background:var(--wash);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1520px;margin:auto;padding:42px 24px 90px}
header{border-top:3px double var(--ink);padding-top:20px}
.titling{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;align-items:flex-end}
h1{font-size:2.1rem;line-height:1.12;letter-spacing:-.015em;margin:.14em 0 .08em}
.instance{font-size:1.2rem;color:var(--muted);margin:0}
.runmeta{text-align:right;color:var(--muted);font-size:.88rem;line-height:1.8}
.runmeta code{color:var(--ink)}
h2{margin-top:2.2em;border-bottom:2px solid var(--ink);padding-bottom:.3em;font-size:1.08rem}
h3{margin-bottom:.25em}
a{color:var(--blue)}
code{overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere}
header p,section>p,dd{max-width:80ch}
[id]{scroll-margin-top:72px}
.eyebrow{color:var(--blue);font-size:.78rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase;margin:0}
.jump{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 4px;padding:10px 2px;background:var(--wash);border-bottom:1px solid var(--line)}
.jump a{background:var(--paper);border:1px solid var(--line);border-radius:99px;padding:5px 13px;font-size:.82rem;font-weight:750;text-decoration:none;white-space:nowrap}
.notice,.asset,.twins,.backlog,.audit pre,.audit-detail{background:var(--paper);border:1px solid var(--line);border-radius:3px;padding:18px;margin:16px 0}
.notice{border-left:3px solid var(--gold);padding:13px 16px}
.notice.recording{border-left-color:var(--green)}
.lead{color:var(--muted)}
/* Hairlines rather than radii and shadows: at 127 stacked asset sections a card edge per row
   reads as noise, and the rail is doing the work the border used to. */
.asset{border-left:3px solid var(--hue,var(--ink));border-radius:0 3px 3px 0}
.summary{display:grid;grid-template-columns:1.35fr repeat(4,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:3px;max-width:1100px;margin-top:24px}
.summary div{background:var(--tint,var(--paper));padding:15px 17px}
.summary .total{background:#e8f0f6}
.summary strong,.summary span{display:block}
.summary strong{font-size:1.65rem;font-weight:700;line-height:1.05;letter-spacing:-.02em;color:var(--hue,var(--ink))}
.summary .total strong{font-size:2.1rem}
.summary span{color:var(--hue,var(--muted));font-size:.88rem;font-weight:750;margin-top:2px}
.summary .total span{color:var(--ink);font-weight:700}
.meta,.muted,small{color:var(--muted)}
/* The change digest: a labelled register, with the count that actually moved given the weight. */
.counts{border:1px solid var(--line);border-radius:3px;background:var(--paper);max-width:900px}
.count{display:grid;grid-template-columns:76px minmax(0,1fr);gap:2px 16px;align-items:baseline;padding:10px 17px;border-bottom:1px solid var(--line)}
.count:last-child{border-bottom:0}
.count>b{text-align:right;font-size:1.02rem;color:var(--muted)}
.count.lead{background:#e8f0f6}
.count.lead>b{font-size:1.5rem;color:var(--ink)}
.qualifies{display:block;color:var(--muted);font-size:.875rem;line-height:1.45;font-weight:400}
.registers{display:flex;flex-wrap:wrap;border:1px solid var(--line);border-radius:3px;background:var(--paper);margin:14px 0;max-width:900px}
.registers div{padding:11px 17px;border-right:1px solid var(--line)}
.registers div:last-child{border-right:0}
.registers b{font-weight:700}
.registers span{color:var(--muted);font-size:.9rem}
.audit-detail>summary{font-weight:750}
/* Coverage: status first, because status governs how to read the figure beside it. */
.covgrid{display:grid;gap:24px;max-width:1240px}
@media(min-width:1040px){.covgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}
.covbox{border:1px solid var(--line);border-radius:3px;background:var(--paper);max-width:1240px}
.cov{display:grid;grid-template-columns:132px minmax(0,1fr);gap:3px 14px;align-items:baseline;padding:12px 16px;border-bottom:1px solid var(--line);border-left:4px solid var(--hue,transparent)}
.cov:last-child{border-bottom:0}
.cov .named{color:var(--hue,var(--ink))}
.cov .figure{text-align:right;font-weight:700}
.covbox .cov:has(.figure){grid-template-columns:132px minmax(0,1fr) 68px}
.cov.degraded{background:#f2e9da}
.status{border-radius:99px;padding:3px 9px;font-weight:750;font-size:.8rem;white-space:nowrap}
.status.clean{background:#e8f0f6;color:var(--green)}
.status.warn{background:#73501c;color:#fff}
.status.absent{background:var(--wash);border:1px solid var(--line);color:var(--muted)}
.qual{padding:11px 17px;border-bottom:1px solid var(--line)}
.qual:last-child{border-bottom:0}
.qual span{color:var(--muted)}
/* Watch list: `N / M days elapsed` is a progress statement, so it is drawn as one. */
.watch{border:1px solid var(--line);border-radius:3px;background:var(--paper);max-width:900px}
.wrow{padding:14px 17px;border-bottom:1px solid var(--line)}
.wrow:last-child{border-bottom:0}
.whead{display:flex;justify-content:space-between;gap:16px;align-items:baseline}
.whead span{color:var(--muted);font-size:.85rem;white-space:nowrap}
.meter{height:6px;background:#e8f0f6;border-radius:99px;margin:9px 0 0;overflow:hidden}
.meter>i{display:block;height:100%;background:var(--blue)}
.wrow p{margin:7px 0 0;font-size:.86rem}
.candidate{border-top:1px solid var(--line);padding:16px 0;display:grid;gap:6px 30px;align-items:start}
@media(min-width:1040px){.decidable{grid-template-columns:minmax(0,1fr) minmax(0,1.15fr)}}
.facts,.decision{min-width:0}
.facts p{max-width:88ch}
.condition{background:var(--tint,#e8f0f6);color:var(--hue,var(--ink));border:1px solid var(--hue,var(--line));border-radius:99px;padding:3px 9px;font-weight:750;font-size:.82rem}
.twins{border-left:7px solid #1f5c52;border-radius:0 3px 3px 0}
.twins>h3{color:#1f5c52}
.twins>.asset{box-shadow:none}
.entry{background:var(--paper);border:1px solid var(--line);border-left:4px solid var(--hue,var(--ink));border-radius:0 3px 3px 0;margin:8px 0;padding:2px 18px}
.entry>summary{padding:11px 0;font-weight:750;overflow-wrap:anywhere}
.entry[open]{padding-bottom:14px}
.entry[open]>summary{border-bottom:1px solid var(--line)}
.entry>.asset,.entry>.twins{border:0;border-radius:0;padding:0;margin:0}
.entry>.twins{border-left:7px solid #1f5c52;padding-left:14px;margin-top:14px}
fieldset{border:1px solid var(--line);border-radius:3px;margin:12px 0;padding:8px 12px;min-width:0}
legend{font-weight:800}
.choices{display:grid;grid-template-columns:repeat(auto-fit,minmax(195px,1fr));gap:2px 18px}
.choice{display:grid;grid-template-columns:1.4rem 1fr;gap:7px;padding:7px 0;align-items:start}
.choice input{margin-top:5px}
.choice small{display:block;font-weight:400;font-size:.87rem;line-height:1.4}
.reason{display:block;font-weight:750;margin:12px 0}
input[type=text]{display:block;width:100%;padding:10px;border:1px solid #9aa6b2;border-radius:3px;font:inherit}
button{padding:11px 16px;border:0;border-radius:3px;background:var(--blue);color:#fff;font:inherit;font-weight:800;min-height:44px}
.term-hint{text-decoration:underline dotted;text-underline-offset:3px;cursor:help}
.term-hint:focus{outline:2px solid var(--blue);outline-offset:2px}
details summary{cursor:pointer;font-weight:800}
dt{font-weight:800;margin-top:.8em}
dd{margin-left:0;color:var(--muted)}
"""


class _TermHints(HTMLParser):
    """Annotate prose only: never change form values, links, IDs, names or the glossary."""
    def __init__(self, definitions):
        super().__init__(convert_charrefs=False)
        self.definitions = {key.casefold(): value for key, value in definitions.items()}
        terms = sorted(self.definitions, key=len, reverse=True)
        self.pattern = re.compile(r'(?<![\w-])(' + '|'.join(map(re.escape, terms)) + r')(?![\w-])', re.I)
        self.stack = []
        self.output = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        skip = (tag in {'a', 'code', 'pre', 'textarea', 'script', 'style', 'h3', 'small', 'abbr'} or
                attrs_dict.get('id') == 'glossary' or attrs_dict.get('class') in {'meta', 'instance', 'runmeta', 'asset-name'})
        if tag not in {'input', 'br', 'hr', 'meta', 'link', 'img'}:
            self.stack.append((tag, skip or bool(self.stack and self.stack[-1][1])))
        self.output.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.output.append('</' + tag + '>')
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            self.output.append(data)
        else:
            self.output.append(self.pattern.sub(lambda m: '<abbr class="term-hint" tabindex="0" title="' +
                _h(self.definitions[m[0].casefold()]) + '">' + m[0] + '</abbr>', data))

    def handle_entityref(self, name):
        self.output.append('&' + name + ';')

    def handle_charref(self, name):
        self.output.append('&#' + name + ';')


def _term_hints(markup, definitions):
    parser = _TermHints(definitions)
    parser.feed(markup)
    return ''.join(parser.output)


def render(document: dict, cfg, owner: Optional[str] = None, interaction: Optional[Interaction] = None) -> str:
    """Render one static or localhost-interactive view through the report's row selector."""
    individual_rows, recorded_rows, grouped_rows = report_mod._asset_rows(document, cfg, owner)
    run = document.get("run") or {}
    definitions = _definitions(cfg, document)
    reviewer = f'<div>Decisions by {_h(interaction.reviewer)}</div>' if interaction else ""
    mode_note = (
        "This localhost view can append decisions after an exact preview and confirmation."
        if interaction
        else "This portable export is read-only. Open it through the local cleanup launcher to record decisions."
    )
    instance = _h(run.get("instance") or cfg.instance_key)
    subtitle = instance if owner is None else f'{instance} — {_h(owner)}'
    # (anchor, markup) in document order.  A part that renders empty is simply absent, and the
    # nav above is built from the same list, so the two can never disagree.
    parts = [
        (None, _summary(document)),
        ("change", _change_section(document, report_mod._change_lines(document))),
        ("unmatched", _unmatched_corrections(document, interaction)),
        ("coverage", _coverage_section(document)),
        ("head", _head(individual_rows, interaction, definitions)),
        ("watch", _watch_section(document, owner)),
        ("recorded", _recorded(recorded_rows, interaction, definitions)),
        ("backlog", _backlog(grouped_rows, interaction, definitions)),
        ("glossary", _glossary(cfg, document)),
    ]
    sections = "\n".join(markup for _, markup in parts if markup)
    nav = _nav(anchor for anchor, markup in parts if anchor and markup)
    body = f'''<header><div class="titling"><div>
<p class="eyebrow">Recommend-only cleanup</p><h1>Cleanup report</h1><p class="instance">{subtitle}</p></div>
<div class="runmeta"><div>Run <code>{_h(run.get("run_id"))}</code></div>
<div>As of {_h(run.get("as_of_date"))}</div>{reviewer}</div></div>
<p class="notice{" recording" if interaction else ""}">{_h(mode_note)}</p></header>
{nav}
{sections}'''
    body = _term_hints(body, definitions)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cleanup report · {_h(run.get("instance") or cfg.instance_key)}</title>
<style>{_STYLE}</style></head><body><main>{body}</main></body></html>\n'''
