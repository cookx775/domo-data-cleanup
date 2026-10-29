"""Local browser review backed by shared AppDB state; credentials stay on localhost."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml

from collector import appdb as appdb_mod
from collector import triage as triage_mod

LOG = logging.getLogger("domo_cleanup.web")


KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
REPORT_COMMAND_RE = re.compile(r"^python cleanup\.py review .+$", re.MULTILINE)


@dataclass(frozen=True)
class GuideState:
    name: str
    meaning: str
    action: str
    next_step: str
    instance_key: Optional[str] = None
    run_id: Optional[str] = None
    report_files: Tuple[str, ...] = ()
    decision_count: int = 0
    first_command: Optional[str] = None
    remediation: Tuple[str, ...] = ()


@dataclass
class SetupView:
    instance_key: str
    domain: str
    result: object
    candidates: Dict[str, List[object]] = field(default_factory=dict)
    manual_checks: Dict[str, Tuple[str, str, object]] = field(default_factory=dict)


@dataclass(frozen=True)
class PendingPreview:
    token: str
    instance_key: str
    run_id: str
    entries: Tuple[object, ...]
    lines: Tuple[str, ...]
    shared_base: Optional[dict] = None


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: str, value: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _config_path(root: str, key: str) -> str:
    return os.path.join(root, "instances", key, "config.yaml")


def _env_path(root: str, key: str) -> str:
    return os.path.join(root, "instances", key, ".env")


def _cleanup_dir(root: str, key: str) -> str:
    return os.path.join(root, "instances", key, "cleanup")


def setup_status_path(root: str, key: str) -> str:
    return os.path.join(_cleanup_dir(root, key), "setup-status.json")


def reviewer_path(root: str, key: str) -> str:
    return os.path.join(_cleanup_dir(root, key), "reviewer.json")


def walkthrough_path(root: str, key: str) -> str:
    return os.path.join(_cleanup_dir(root, key), "walkthrough.log")


def _config_digest(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def save_token(root: str, key: str, token: str, *, replace: bool = False) -> str:
    """Persist one token without ever copying it into logs or page URLs.

    A Domo access token is displayed once, so the saved file may be the only copy in
    existence. Replacing it is therefore an explicit argument and never a side effect of
    a form submission — the same rule `create_config` applies to configuration.
    """
    token = token.strip()
    if not token or "\n" in token or "\r" in token:
        raise ValueError("Enter one non-empty Domo API token.")
    path = _env_path(root, key)
    if os.path.exists(path) and not replace:
        raise ValueError(
            "This instance already has a saved token. Leave the token field blank to "
            "reconnect with it, or tick \u201cReplace the saved token\u201d to overwrite it."
        )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"DOMO_API_TOKEN={token}\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def save_reviewer(root: str, key: str, email: str) -> str:
    email = " ".join(email.split())
    if not email or "@" not in email:
        raise ValueError("Enter the email address that should be attached to each decision.")
    path = reviewer_path(root, key)
    _write_json(path, {"email": email})
    return path


def resolve_start_token(
    root: str,
    key: str,
    token: str,
    replace: bool,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Settle the token for one instance and report which route was taken.

    The welcome form cannot know the instance key until a hostname is typed, so it cannot
    decide whether a token is needed while rendering. That decision belongs here, after the
    key is derived: `saved`, `replaced`, `kept` (an existing `.env`), or `process-env`.
    Nothing is written on the `kept` and `process-env` routes.
    """
    environ = environ if environ is not None else os.environ
    saved = os.path.isfile(_env_path(root, key))
    token = token.strip()
    if token:
        save_token(root, key, token, replace=replace)
        return "replaced" if saved else "saved"
    if saved:
        return "kept"
    if environ.get("DOMO_API_TOKEN"):
        return "process-env"
    raise ValueError("Enter a developer token.")


def reviewer_email(root: str, key: str) -> Optional[str]:
    value = _read_json(reviewer_path(root, key)) or {}
    email = value.get("email")
    return str(email) if email else None


def _read_config_appdb(root, key):
    try:
        with open(_config_path(root, key), encoding="utf-8") as handle:
            return (yaml.safe_load(handle) or {}).get("appdb")
    except OSError:
        return None


def append_walkthrough(root: str, key: str, step: str, outcome: str, detail: str = "") -> None:
    """Append an audit event. Callers must never pass credentials in ``detail``."""
    profile = _read_config_appdb(root, key)
    if profile:
        return  # Shared operations are audited in AppDB, never in a growing local run log.
    path = walkthrough_path(root, key)
    if os.path.isfile(path) and os.path.getsize(path) > 262144:
        os.replace(path, path + ".previous")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "step": step,
        "outcome": outcome,
    }
    if detail:
        record["detail"] = detail
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def write_setup_status(root: str, key: str, result, acknowledged: Iterable[str] = ()) -> str:
    checks = [check.as_dict() for check in result.checks.values()]
    value = {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "config_sha256": _config_digest(_config_path(root, key)),
        "floor_ok": bool(result.ok),
        "acknowledged_unconfigured": sorted(set(acknowledged)),
        "reports": checks,
    }
    _write_json(setup_status_path(root, key), value)
    return setup_status_path(root, key)


def _configured_reports(config_path: str) -> Dict[str, str]:
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in (document.get("reports") or {}).items() if v}


def _latest_findings(api, root: str, key: str) -> Tuple[Optional[dict], Optional[str]]:
    cfg = api.config_mod.load(key, root=root)
    if appdb_mod.configured(cfg):
        document = appdb_mod.open_store(cfg).run()
        return (document, document['run']['run_id']) if document else (None, None)
    runs_dir = os.path.join(_cleanup_dir(root, key), "runs")
    try:
        document = api.verdict_mod.load_findings(runs_dir, None)
    except (SystemExit, OSError, ValueError):
        return None, None
    run_id = str((document.get("run") or {}).get("run_id") or "")
    return (document, run_id) if run_id else (None, None)


def _first_report_command(report_files: Sequence[str]) -> Optional[str]:
    ordered = sorted(report_files, key=lambda path: (os.path.basename(path) != "master.md", path))
    for path in ordered:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                match = REPORT_COMMAND_RE.search(handle.read())
        except OSError:
            continue
        if match:
            return match.group(0)
    return None


def derive_state(api, root: str, key: Optional[str], environ: Optional[Mapping[str, str]] = None) -> GuideState:
    """Derive the next step from disk. It performs no Domo or other network operation."""
    environ = environ if environ is not None else os.environ
    if not key:
        return GuideState(
            "welcome",
            "No instance is selected yet.",
            "Enter your Domo address, API token, and reviewer email once.",
            "The guide will create private local state and move to setup.",
        )
    if not KEY_RE.match(key):
        return GuideState("error", "The instance key is invalid.", "Choose an instance again.", "No files were changed.")

    env_present = bool(environ.get("DOMO_API_TOKEN")) or os.path.isfile(_env_path(root, key))
    config_path = _config_path(root, key)
    if not env_present:
        return GuideState(
            "token",
            "This instance has no local API token yet.",
            "Create a developer token in Domo, then save it here.",
            "The guide will check the two governance connectors next.",
            instance_key=key,
        )
    if not os.path.isfile(config_path):
        return GuideState(
            "setup",
            "This instance has no local report configuration yet.",
            "Run Check setup; every proposed report ID will wait for your confirmation.",
            "A successful floor check unlocks the cleanup run.",
            instance_key=key,
        )

    cfg = api.config_mod.load(key, root=root)
    if appdb_mod.configured(cfg):
        store = appdb_mod.open_store(cfg)
        head = store.head()
        document = store.manifest(head['latest']) if head['latest'] else None
        if document:
            count = len(store.records(head))
            return GuideState("done" if count else "review", "Shared findings are ready.",
                              "Open the shared review.", "Submitted decisions are visible to other administrators.",
                              instance_key=key, run_id=document['run_id'],
                              report_files=("summary.html",), decision_count=count)
    configured = _configured_reports(config_path)
    status = _read_json(setup_status_path(root, key))
    status_current = bool(status and status.get("config_sha256") == _config_digest(config_path))
    acknowledged = set((status or {}).get("acknowledged_unconfigured") or []) if status_current else set()
    report_keys = set(api.manifest_mod.load().keys)
    unreviewed = sorted(report_keys - set(configured) - acknowledged)
    if not status_current or unreviewed:
        return GuideState(
            "setup",
            "Report setup has not been checked against the current configuration.",
            "Run Check setup, then explicitly confirm or leave each proposed report unconfigured.",
            "A successful floor check unlocks the cleanup run.",
            instance_key=key,
        )
    if not status.get("floor_ok"):
        remediation = tuple(
            str(check.get("detail") or check.get("report"))
            for check in status.get("reports") or []
            if check.get("floor") and not check.get("ok")
        )
        return GuideState(
            "remediation",
            "A mandatory floor report failed its last Check setup.",
            "Fix the listed report or connector in Domo, then run Check setup again.",
            "When both floor reports answer a real query, the cleanup run unlocks.",
            instance_key=key,
            remediation=remediation,
        )

    document, run_id = _latest_findings(api, root, key)
    if not document or not run_id:
        return GuideState(
            "run",
            "Setup passed and no findings file exists yet.",
            "Run the read-only collector; it usually takes about three minutes.",
            "The guide will render the reports from the findings file.",
            instance_key=key,
        )

    reports_dir = api.report_mod.reports_dir(api.config_mod.load(key, root=root), run_id)
    report_files = tuple(
        os.path.join(reports_dir, name)
        for name in sorted(os.listdir(reports_dir))
        if name.endswith((".md", ".html"))
    ) if os.path.isdir(reports_dir) else ()
    if not report_files:
        return GuideState(
            "render",
            "The newest findings file exists, but its reading copies do not.",
            "Render the local reports now.",
            "The guide will open the decision surface without rerunning the collector.",
            instance_key=key,
            run_id=run_id,
        )

    cfg = api.config_mod.load(key, root=root)
    ledger = api.ledger_mod.Ledger(
        cfg.ledger_path,
        cycle_days=int(cfg.get("ledger", "cycle_days", default=30)),
        quarantine_days=int(cfg.get("ledger", "quarantine_period_days", default=90)),
    ).load()
    decisions = len(ledger.entries)
    first_command = _first_report_command(report_files)
    reviewer = reviewer_email(root, key)
    if first_command and reviewer:
        first_command = first_command.replace("<you@example.com>", reviewer)
    if decisions == 0:
        return GuideState(
            "review",
            "The reports are ready and the decision ledger is still empty.",
            "Read your owner supplement, then record one decision.",
            "The next run will suppress any candidate covered by that decision.",
            instance_key=key,
            run_id=run_id,
            report_files=report_files,
            first_command=first_command,
        )
    return GuideState(
        "done",
        f"{decisions} decision{'s' if decisions != 1 else ''} recorded in the append-only ledger.",
        "You are done for today; back up the instance folder.",
        "The next run reads this ledger and suppresses covered candidates.",
        instance_key=key,
        run_id=run_id,
        report_files=report_files,
        decision_count=decisions,
        first_command=first_command,
    )


def existing_instances(root: str) -> List[str]:
    directory = os.path.join(root, "instances")
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(name for name in names if KEY_RE.match(name) and os.path.isdir(os.path.join(directory, name)))


def _normalize_domain(value: str) -> str:
    value = value.strip().replace("https://", "").replace("http://", "").rstrip("/")
    if not value or "/" in value or "." not in value:
        raise ValueError("Enter your Domo hostname, for example acme.domo.com.")
    return value


TOKEN_FIELD = (
    '<label>Developer token<input type="password" name="token" autocomplete="off"></label>'
    '<p class="muted">Leave this blank to reconnect with a token you already saved for this'
    ' instance.</p>'
    '<label class="inline"><input type="checkbox" name="replace" value="yes"> Replace the saved'
    ' token</label>'
    '<p class="muted">Only needed when the token has been rotated or has expired. A saved token'
    ' is never overwritten without this.</p>'
)


def _page(title: str, body: str) -> bytes:
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Domo cleanup</title>
<style>
:root{{--ink:#17202a;--muted:#5b6470;--line:#dce2e8;--paper:#fff;--wash:#f4f7f9;--blue:#1769aa;--red:#a12b2b;--green:#27633e;--gold:#73501c;--tint:#e7f1f8}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--wash);color:var(--ink);font:16px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:860px;margin:0 auto;padding:48px 22px 80px}}
header{{border-top:3px double var(--ink);padding-top:20px;margin-bottom:26px}} h1{{font-size:2.1rem;line-height:1.12;letter-spacing:-.015em;margin:.14em 0 .08em}} h2{{margin-top:1.8em;font-size:1.22rem}}
.eyebrow{{color:var(--blue);font-size:.78rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase}}
/* Hairlines rather than radii and shadows, so a page of stacked cards reads as one document. */
.card{{background:var(--paper);border:1px solid var(--line);border-radius:3px;padding:22px 24px;margin:16px 0}}
.step{{display:grid;grid-template-columns:2rem 1fr;gap:12px;margin:16px 0}} .num{{width:2rem;height:2rem;border-radius:50%;display:grid;place-items:center;background:var(--tint);color:var(--blue);font-weight:800}}
label{{display:block;font-weight:700;margin-top:14px}} input[type=text],input[type=password],input[type=email]{{width:100%;padding:11px 12px;border:1px solid #9aa6b2;border-radius:3px;font:inherit}}
button,.button{{display:inline-block;margin-top:18px;padding:12px 18px;border:0;border-radius:3px;background:var(--blue);color:white;font:inherit;font-weight:750;text-decoration:none;cursor:pointer;min-height:44px}}
button.secondary,.button.secondary{{background:#edf2f5;color:var(--ink)}} .muted{{color:var(--muted)}}
/* A left rail rather than a tinted box: the same vocabulary the report uses for its notices. */
.error{{border-left:3px solid var(--red)}} .good{{border-left:3px solid var(--green)}}
.decision-summary{{display:grid;grid-template-columns:100px minmax(0,1fr);gap:8px 16px}} .decision-summary dt{{color:var(--muted)}} .decision-summary dd{{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#15212b;color:#eff6fa;padding:18px;border-radius:3px;overflow:auto}}
.candidate{{padding:13px 0;border-top:1px solid var(--line)}} .candidate label{{font-weight:500;margin:5px 0}} label.inline{{display:flex;align-items:center;gap:8px;font-weight:500}} details{{margin:10px 0}} a{{color:var(--blue)}}
/* The probe result is pass or fail, not a condition: it borrows the report's pigments and
   carries no condition meaning. */
.probe{{border-radius:99px;padding:3px 9px;font-size:.8rem;font-weight:750;white-space:nowrap}}
.probe.ok{{background:#dfece9;color:#1f5c52;border:1px solid #1f5c52}}
.probe.bad{{background:#f6e4e1;color:#8a2f2a;border:1px solid #8a2f2a}}
.floor{{background:var(--ink);color:var(--paper);border-radius:99px;padding:2px 9px;font-size:.72rem;font-weight:800;letter-spacing:.06em}}
.optional{{background:var(--wash);border:1px solid var(--line);color:var(--muted);border-radius:99px;padding:2px 9px;font-size:.72rem;font-weight:800;letter-spacing:.06em}}
</style></head><body><main><header><div class="eyebrow">Instance cleanup review</div><h1>{html.escape(title)}</h1><p class="muted">Findings and decisions are shared in your Domo AppDB. Credentials stay on this machine. The browser connects through localhost, with no analytics or telemetry.</p></header>{body}</main></body></html>"""
    return document.encode("utf-8")


def _state_body(state: GuideState, csrf: str, message: str = "", output: str = "") -> str:
    message_html = f'<section class="card good"><strong>{html.escape(message)}</strong></section>' if message else ""
    output_html = f'<details class="card"><summary>Action details</summary><pre>{html.escape(output)}</pre></details>' if output else ""
    state_card = f"""<section class="card">
<div class="eyebrow">Where you are</div><h2>{html.escape(state.name.replace('-', ' ').title())}</h2>
<div class="step"><span class="num">1</span><div><strong>What this means</strong><br>{html.escape(state.meaning)}</div></div>
<div class="step"><span class="num">2</span><div><strong>What to do</strong><br>{html.escape(state.action)}</div></div>
<div class="step"><span class="num">3</span><div><strong>What happens next</strong><br>{html.escape(state.next_step)}</div></div>
</section>"""
    key = html.escape(state.instance_key or "")
    controls = ""
    if state.name in {"setup", "remediation"}:
        remediation = "".join(f"<li>{html.escape(line)}</li>" for line in state.remediation)
        controls = f"""<section class="card {'error' if remediation else ''}">{f'<h2>Remediation</h2><ul>{remediation}</ul>' if remediation else ''}
<form method="post" action="/action/check-setup"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="instance" value="{key}"><button type="submit">Check setup</button></form></section>"""
    elif state.name == "run":
        controls = f"""<section class="card"><form method="post" action="/action/run"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="instance" value="{key}"><button type="submit">Run cleanup</button></form><p class="muted">Leave this page open. Large lineage checks can make the run take longer than three minutes.</p></section>"""
    elif state.name == "render":
        controls = f"""<section class="card"><form method="post" action="/action/render"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="instance" value="{key}"><button type="submit">Render reports</button></form></section>"""
    elif state.name in {"review", "done"}:
        ordered = sorted(
            state.report_files,
            key=lambda path: (os.path.basename(path) != "summary.html", os.path.basename(path)),
        )
        links = "".join(
            f'<li><a href="/report?instance={urllib.parse.quote(state.instance_key or "")}&file={urllib.parse.quote(os.path.basename(path))}">{html.escape(os.path.basename(path))}</a></li>'
            for path in ordered
        )
        command = f"<details><summary>Command-line fallback</summary><pre>{html.escape(state.first_command)}</pre></details>" if state.first_command else ""
        controls = f'<section class="card"><h2>Decision surface</h2><ul>{links}</ul>{command}</section>'
    return message_html + state_card + controls + output_html


class GuideApplication:
    def __init__(self, api, root: str, initial_instance: Optional[str] = None) -> None:
        self.api = api
        self.root = os.path.abspath(root)
        self.initial_instance = initial_instance
        self.csrf = secrets.token_urlsafe(24)
        self.setup_views: Dict[str, SetupView] = {}
        self.pending_previews: Dict[str, PendingPreview] = {}
        self.operation_lock = threading.Lock()
        self.evidence_sessions = {}
        self.evidence_lock = threading.Lock()

    def _shared_store(self, cfg):
        # Partition by credentials as well as instance/collection IDs; never reuse another
        # user's in-memory evidence after token rotation or an explicit store switch.
        token = appdb_mod.load_token(cfg.env_path)
        identity = (cfg.domain, json.dumps(cfg.get('appdb'), sort_keys=True),
                    hashlib.sha256(token.encode()).hexdigest())
        with self.evidence_lock:
            cached = self.evidence_sessions.get(identity)
            if cached is None:
                if len(self.evidence_sessions) >= 2:
                    self.evidence_sessions.clear()
                store = appdb_mod.open_store(cfg, cache=appdb_mod.EvidenceCache())
                self.evidence_sessions[identity] = (store.transport, store.cache)
                return store
            transport, cache = cached
        # Permissions and control are read afresh; immutable content stays in bounded memory.
        return appdb_mod.open_store(cfg, transport=transport, cache=cache)

    def render_shared_home(self, key, cfg, message='', output=''):
        esc = html.escape
        fields = f'<input type="hidden" name="csrf" value="{self.csrf}"><input type="hidden" name="instance" value="{esc(key)}">'
        def form(action, contents):
            return f'<form method="post" action="/action/{action}">{fields}{contents}</form>'
        body = '<p class="notice">' + esc(message) + '</p>' if message else ''
        if not appdb_mod.configured(cfg):
            body += '<section class="card"><h2>Preparing shared cleanup</h2><p>Finding Cleanup Runs and Cleanup Review in this instance. If they do not exist, the app creates them automatically.</p>'
            body += '<form id="shared-setup" method="post" action="/action/shared-auto">' + fields + '<noscript><button>Continue setup</button></noscript></form></section><script src="/shared-setup.js"></script>'
        else:
            store = self._shared_store(cfg)
            head = store.head()
            body += '<section class="card"><h2>Shared cleanup</h2><p>Shared store: <code>' + esc(store.datastore) + '</code></p>'
            if head['latest']:
                body += '<p><a href="/report?instance=' + urllib.parse.quote(key) + '&file=summary.html">Open latest review</a></p>'
                body += '<ul>'
                for row in store.history(limit=20, head=head):
                    url = '/report?' + urllib.parse.urlencode({'instance': key, 'file': 'summary.html', 'run': row['run_id']})
                    body += '<li><a href="' + esc(url) + '">' + esc(row['started_at']) + '</a> — ' + str(row['count']) + ' candidates</li>'
                body += '</ul><p><a href="/history?instance=' + urllib.parse.quote(key) + '">Browse all run history</a></p>'
            else:
                body += '<p>No shared run has been published yet. Import local history or check report setup and run cleanup.</p>'
            body += form('check-setup', '<button>Check report setup</button>')
            body += form('run', '<button>Run cleanup</button><p>Reads instance metadata and publishes shared findings. Candidate assets are never changed.</p>')
            body += '<p><a href="/backup?instance=' + urllib.parse.quote(key) + '">Download complete shared backup</a></p></section>'
            body += '<details class="card"><summary>Import existing local history</summary><p>Imports verified local runs and decisions; all originals are retained. Conflicting histories are refused for reconciliation.</p>'
            body += form('shared-migrate', '<label><input type="checkbox" name="confirm" value="yes" required>Import the local history into this shared store.</label><button>Import history</button>') + '</details>'
            body += '<details class="card"><summary>Give another administrator access</summary><p>Enter their numeric Domo user ID. This grants read and submit access to both cleanup collections.</p>'
            body += form('shared-access', '<label>Domo user ID<input required name="user_id" pattern="[0-9]+"></label><button>Grant cleanup access</button>') + '</details>'
        body += self.storage_recovery(key)
        body += '<details class="card"><summary>Restore a shared backup into a new store</summary><p>The existing store is preserved. A verified restore becomes this profile’s connection.</p>'
        body += form('shared-restore', '<input id="backup-file" type="file" accept="application/json"><textarea id="backup" name="backup" hidden></textarea><label><input type="checkbox" name="confirm" value="yes" required>Restore into a new store and connect this profile after verification.</label><button id="restore-submit" disabled>Restore backup</button>') + '</details><script src="/restore.js"></script>'
        if output:
            body += '<details><summary>Action details</summary><pre>' + esc(output) + '</pre></details>'
        return _page(cfg.instance + ' shared cleanup', body)

    def storage_recovery(self, key):
        return ('<details class="card"><summary>Storage recovery</summary>'
                '<p>Use only to resolve duplicate stores or select a verified restored store.</p>'
                '<form method="post" action="/action/shared-connect">'
                '<input type="hidden" name="csrf" value="' + html.escape(self.csrf) + '">'
                '<input type="hidden" name="instance" value="' + html.escape(key) + '">'
                '<label>Recovery store ID<input required name="datastore"></label>'
                '<label><input required type="checkbox" name="confirm" value="yes">'
                'Use this store for this local profile. Preserve the previous store.</label>'
                '<button>Select recovery store</button></form></details>')

    def shared_action(self, key, action, values):
        with self.operation_lock:
            cfg = self.api.config_mod.load(key, root=self.root)
            if action == 'shared-auto':
                appdb_mod.ensure_connection(cfg)
                return 'Shared cleanup is ready.'
            if action in ('shared-create', 'shared-migrate', 'shared-restore') and self._one(values, 'confirm') != 'yes':
                raise ValueError('Confirm the operation first.')
            if action == 'shared-create':
                if appdb_mod.configured(cfg):
                    raise ValueError('This profile already has shared storage.')
                store = appdb_mod.provision(cfg)
                appdb_mod.save_connection(cfg, store.connection)
                return 'Shared store created. Share its ID with other administrators.'
            if action == 'shared-connect':
                if self._one(values, 'confirm') != 'yes':
                    raise ValueError('Confirm the recovery store selection.')
                store = appdb_mod.connect(cfg, self._one(values, 'datastore'))
                appdb_mod.save_connection(cfg, store.connection, replace=True)
                return 'Connected to shared cleanup.'
            if action == 'shared-restore':
                backup = json.loads(self._one(values, 'backup'))
                plan = appdb_mod.inspect_backup(cfg, backup)
                store = appdb_mod.provision(cfg)
                try:
                    appdb_mod.restore(store, plan)
                    # Verify the complete reachable content from the restored store.
                    roundtrip = appdb_mod.inspect_backup(cfg, store.backup())
                    if [(k,v) for k,v,_ in plan] != [(k,v) for k,v,_ in roundtrip]:
                        raise ValueError('Restored content differs from the backup.')
                except Exception as exc:
                    raise ValueError(f'Restore stopped; original connection retained. New store {store.datastore}: {exc}') from exc
                appdb_mod.save_connection(cfg, store.connection, replace=True)
                return 'Backup restored and verified. The previous store was preserved.'
            store = self._shared_store(cfg)
            if action == 'shared-migrate':
                return 'Imported history: ' + json.dumps(appdb_mod.migrate_local(store))
            if action == 'shared-access':
                user = self._one(values, 'user_id')
                if not user.isdecimal():
                    raise ValueError('Enter a numeric Domo user ID.')
                for collection in (store.runs, store.review):
                    store.transport.call('PUT', f'/api/datastores/v1/collections/{collection}/permission/USER/{user}',
                                         params={'permissions': 'read,read_content,create_content,update_content', 'overwrite': 'false'})
                return 'Cleanup collection access granted. The other administrator can reopen the app to find it automatically.'
            raise ValueError('Unknown shared storage action.')

    def render_history(self, key, offset=0):
        cfg = self.api.config_mod.load(key, root=self.root)
        store = self._shared_store(cfg)
        offset = max(0, min(int(offset), 100000))
        rows = store.history(limit=21, offset=offset)
        body = '<h2>Run history</h2><ul>'
        for row in rows[:20]:
            url = '/report?' + urllib.parse.urlencode({'instance': key, 'file': 'summary.html', 'run': row['run_id']})
            body += '<li><a href="' + html.escape(url) + '">' + html.escape(row['started_at']) + '</a></li>'
        body += '</ul>'
        if len(rows) > 20:
            body += '<a href="/history?' + html.escape(urllib.parse.urlencode({'instance': key, 'offset': offset+20})) + '">Older runs</a>'
        return _page('Run history', body)

    def _setup_view(self, key: str) -> SetupView:
        cfg = self.api.config_mod.load(key, root=self.root)
        client = self.api.DomoClient(cfg.domain, self.api.load_token(cfg.env_path))
        result = self.api.preflight_mod.preflight(client, cfg)
        if result.discovery:
            self.api.write_proposal(result.discovery, self.api.proposal_path(self.root, key))
        candidates = {
            report_key: list(proposal.candidates)
            for report_key, proposal in ((result.discovery.proposals.items()) if result.discovery else [])
            if proposal and proposal.candidates
        }
        view = SetupView(key, cfg.domain, result, candidates)
        self.setup_views[key] = view
        return view

    def check_setup(self, key: str) -> Tuple[GuideState, Optional[SetupView], str]:
        with self.operation_lock:
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    view = self._setup_view(key)
            except Exception as exc:
                append_walkthrough(self.root, key, "check_setup", "failed", f"{type(exc).__name__}: {exc}")
                raise
            unresolved = [check.key for check in view.result.checks.values() if not check.ok]
            if not unresolved:
                write_setup_status(self.root, key, view.result)
                append_walkthrough(self.root, key, "check_setup", "passed" if view.result.ok else "failed_floor")
            else:
                append_walkthrough(self.root, key, "check_setup", "awaiting_report_confirmation", f"{len(unresolved)} report(s)")
            return derive_state(self.api, self.root, key), view, output.getvalue()

    def confirm_reports(self, key: str, form: Mapping[str, List[str]]) -> GuideState:
        view = self.setup_views.get(key)
        if not view:
            raise ValueError("Check setup again before confirming report IDs.")
        unresolved = [check.key for check in view.result.checks.values() if not check.ok]
        chosen: Dict[str, Optional[str]] = {}
        skipped: List[str] = []
        manual_keys: List[str] = []
        for report_key in unresolved:
            # One radio group per report: a proposed ID, "manual" or "skip". The manual-ID
            # text box is read only when "manual" is the picked option, so leftover text
            # never blocks switching to another choice.
            values = form.get(f"report:{report_key}", [])
            if not values:
                raise ValueError(f"Confirm a report ID or explicitly leave {report_key} unconfigured.")
            if len(values) != 1:
                raise ValueError(f"Choose exactly one option for {report_key}.")
            picked = values[0]
            if picked == "skip":
                skipped.append(report_key)
                chosen[report_key] = None
                continue
            if picked == "manual":
                entered = (form.get(f"manual:{report_key}") or [""])[0].strip()
                checked = view.manual_checks.get(report_key)
                if not checked or entered != checked[0] or not checked[2].ok or checked[2].missing_read:
                    raise ValueError(f"Check the manually entered ID for {report_key} before confirming it.")
                chosen[report_key] = entered
                manual_keys.append(report_key)
                continue
            allowed = {candidate.dataset_id for candidate in view.candidates.get(report_key, [])}
            if picked not in allowed:
                raise ValueError(f"The selected ID for {report_key} was not in the checked proposal.")
            chosen[report_key] = picked
        self.api.write_confirmed_reports(_config_path(self.root, key), chosen, manual_keys=manual_keys)
        with self.operation_lock:
            final = self._setup_view(key)
        write_setup_status(self.root, key, final.result, acknowledged=skipped)
        append_walkthrough(
            self.root,
            key,
            "confirm_reports",
            "passed" if final.result.ok else "failed_floor",
            f"{len(chosen) - len(skipped)} confirmed; {len(skipped)} left unconfigured; "
            f"{len(manual_keys)} entered manually: {', '.join(f'{item}={chosen[item]}' for item in manual_keys)}",
        )
        return derive_state(self.api, self.root, key)

    def check_manual_report(self, key: str, report_key: str, dataset_id: str) -> SetupView:
        view = self.setup_views.get(key)
        if not view or report_key not in view.result.checks or view.result.checks[report_key].ok:
            raise ValueError("Check setup again before entering a report ID.")
        view.manual_checks.pop(report_key, None)
        cfg = self.api.config_mod.load(key, root=self.root)
        with self.operation_lock:
            client = self.api.DomoClient(cfg.domain, self.api.load_token(cfg.env_path))
            name, check = self.api.inspect_report_id(client, report_key, dataset_id)
        view.manual_checks[report_key] = (dataset_id.strip(), name, check)
        return view

    def run_cleanup(self, key: str) -> Tuple[GuideState, str]:
        cfg = self.api.config_mod.load(key, root=self.root)
        if not appdb_mod.configured(cfg):
            raise ValueError("Connect shared cleanup storage before running.")
        with self.operation_lock:
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    code = self.api.run(type("Args", (), {"instance": key, "env": None, "prior": None})())
            except (Exception, SystemExit) as exc:
                append_walkthrough(self.root, key, "run", "failed", f"{type(exc).__name__}: {exc}")
                raise
        append_walkthrough(self.root, key, "run", "passed" if code == 0 else "failed", f"exit {code}")
        if code != 0:
            raise RuntimeError(f"The collector stopped with exit code {code}. See action details.")
        return derive_state(self.api, self.root, key), output.getvalue()

    def render_reports(self, key: str) -> Tuple[GuideState, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = self.api.write_reading_copies(key)
        append_walkthrough(self.root, key, "render_reports", "passed" if code == 0 else "failed", f"exit {code}")
        if code != 0:
            raise RuntimeError(f"Report rendering stopped with exit code {code}.")
        return derive_state(self.api, self.root, key), output.getvalue()

    def render_home(self, key: Optional[str], message: str = "", output: str = "") -> bytes:
        if key and os.path.isfile(_config_path(self.root, key)) and (
                os.environ.get("DOMO_API_TOKEN") or os.path.isfile(_env_path(self.root, key))):
            cfg = self.api.config_mod.load(key, root=self.root)
            return self.render_shared_home(key, cfg, message, output)
        state = derive_state(self.api, self.root, key)
        if state.name == "welcome":
            choices = "".join(
                f'<li><a href="/?instance={urllib.parse.quote(name)}">{html.escape(name)}</a></li>'
                for name in existing_instances(self.root)
            )
            token_required = not bool(os.environ.get("DOMO_API_TOKEN"))
            body = f"""{f'<section class="card"><h2>Resume an instance</h2><ul>{choices}</ul></section>' if choices else ''}
<section class="card"><h2>Start or reconnect an instance</h2><p>The token is written only to that instance's private <code>.env</code> file. Create it in Domo under More → Admin → Authentication → Access tokens.</p>
<form method="post" action="/action/start"><input type="hidden" name="csrf" value="{self.csrf}">
<label>Domo hostname<input required type="text" name="domain" placeholder="acme.domo.com"></label>
{TOKEN_FIELD if token_required else '<p class="muted">Using DOMO_API_TOKEN from this launcher process.</p>'}
<p>Decisions are attributed to the Domo user who owns your token.</p>
<button type="submit">Save locally and continue</button></form></section>"""
            return _page("Your cleanup guide", body)
        if state.name == "token":
            body = f"""{_state_body(state, self.csrf, message, output)}
<section class="card"><h2>Save the local profile</h2><p>Create the token in Domo under More → Admin → Authentication → Access tokens. It is stored only in this instance's private <code>.env</code> file.</p>
<form method="post" action="/action/token"><input type="hidden" name="csrf" value="{self.csrf}"><input type="hidden" name="instance" value="{html.escape(state.instance_key or '')}">
<label>Developer token<input required type="password" name="token" autocomplete="off"></label>
<p>Decisions are attributed to the Domo user who owns your token.</p>
<button type="submit">Save locally and continue</button></form></section>"""
            return _page(f"{state.instance_key} cleanup", body)
        if key and not reviewer_email(self.root, key):
            body = f"""<section class="card"><h2>Who is making the decisions?</h2><p>This is asked once and stored locally. The decision surface uses it on every ledger line.</p>
<form method="post" action="/action/reviewer"><input type="hidden" name="csrf" value="{self.csrf}"><input type="hidden" name="instance" value="{html.escape(key)}">
<p>Decisions are attributed to the Domo user who owns your token.</p>
<button type="submit">Save and continue</button></form></section>"""
            return _page(f"{state.instance_key} cleanup", body)
        return _page(f"{state.instance_key} cleanup", _state_body(state, self.csrf, message, output))

    def render_setup(self, view: SetupView, output: str = "") -> bytes:
        unresolved = [check for check in view.result.checks.values() if not check.ok]
        if not unresolved:
            return self.render_home(view.instance_key, "Check setup finished.", output)
        sections = []
        for check in unresolved:
            options = []
            for candidate in view.candidates.get(check.key, []):
                probe = self.api._probe_label(candidate)
                missing = f"; missing {candidate.missing_read}" if candidate.missing_read else ""
                # A dataset can exist, report a fresh update time and still return zero rows,
                # so the probe result is given a chip: the state is legible before the radio
                # is clicked rather than buried in a parenthesis.
                kind = "ok" if (candidate.probe and candidate.probe.ok) else "bad"
                options.append(
                    f'<label><input type="radio" name="report:{html.escape(check.key)}" value="{html.escape(candidate.dataset_id)}"> '
                    f'{html.escape(candidate.name)} <span class="probe {kind}">{html.escape(probe)}</span> '
                    f'<span class="muted">{html.escape(candidate.dataset_id)}{html.escape(missing)}</span></label>'
                )
            manual = view.manual_checks.get(check.key)
            manual_id, manual_name, manual_check = manual if manual else ("", "", None)
            manual_detail = ""
            if manual_check:
                status = "ready" if manual_check.ok and not manual_check.missing_read else "failed"
                if status == "ready":
                    detail = (f"{manual_name} ({manual_id}) — HTTP {manual_check.probe.status}, "
                              "required fields present")
                else:
                    detail = (f"{manual_name} ({manual_id}) — {manual_check.detail}; missing fields "
                              f"{manual_check.missing_identify + manual_check.missing_read}")
                manual_detail = f'<p class="muted">Check {status}: {html.escape(detail)}</p>'
            options.append(
                f'<label><input type="radio" name="report:{html.escape(check.key)}" value="manual"> '
                f'Enter another dataset ID</label>'
                f'<label>Dataset ID<input type="text" name="manual:{html.escape(check.key)}" '
                f'value="{html.escape(manual_id)}"></label>'
                f'<button type="submit" name="check-manual" value="{html.escape(check.key)}">Check this ID</button>'
                f'{manual_detail}'
            )
            options.append(
                f'<label><input type="radio" name="report:{html.escape(check.key)}" value="skip"> Leave this report unconfigured for this run</label>'
            )
            # Floor against optional, so the consequence of skipping is visible on the control
            # that skips it: a floor report left unconfigured keeps the run locked.
            badge = ('<span class="floor">FLOOR</span>' if check.floor
                     else '<span class="optional">OPTIONAL</span>')
            consequence = ('<p class="muted">A floor report left unconfigured keeps the run locked.</p>'
                           if check.floor else
                           '<p class="muted">Optional — this becomes a named coverage exception, not a silent omission.</p>')
            sections.append(
                f'<div class="candidate"><h3>{html.escape(check.key)} {badge}</h3>'
                f'<p class="muted">{html.escape(check.detail)}</p>{"".join(options)}{consequence}</div>'
            )
        body = f"""<section class="card"><h2>Confirm every report</h2><p>No choice is preselected, including a single high-confidence match. A floor report left unconfigured will keep the run locked; an optional report becomes an explicit coverage gap.</p>
<form method="post" action="/action/confirm-reports"><input type="hidden" name="csrf" value="{self.csrf}"><input type="hidden" name="instance" value="{html.escape(view.instance_key)}">
{''.join(sections)}<button type="submit">Confirm choices and check again</button></form></section>
{f'<details class="card"><summary>Check details</summary><pre>{html.escape(output)}</pre></details>' if output else ''}"""
        return _page(f"Confirm {view.instance_key} reports", body)

    def _decision_context(self, key: str, requested_run: str) -> Tuple[object, dict, str]:
        cfg = self.api.config_mod.load(key, root=self.root)
        document, current_run = _latest_findings(self.api, self.root, key)
        if not document or not current_run:
            raise ValueError("No findings run is available for decisions.")
        if requested_run and requested_run != current_run:
            raise ValueError("This report is not the newest run. Reload the decision surface.")
        reviewer = reviewer_email(self.root, key)
        if not reviewer:
            raise ValueError("Save the reviewer email before recording decisions.")
        return cfg, document, reviewer

    @staticmethod
    def _one(form: Mapping[str, List[str]], name: str, *, required: bool = True) -> str:
        values = form.get(name) or []
        if len(values) > 1 or (required and not values):
            raise ValueError(f"Expected one value for {name}.")
        return values[0].strip() if values else ""

    def preview_decisions(self, key: str, form: Mapping[str, List[str]]) -> PendingPreview:
        requested_run = self._one(form, "run_id")
        cfg = self.api.config_mod.load(key, root=self.root)
        shared = self._shared_store(cfg) if appdb_mod.configured(cfg) else None
        base = shared.head() if shared else None
        if shared:
            if self._one(form, 'revision') != str(base['revision']):
                raise appdb_mod.Conflict('Shared decisions changed since this page opened. Reload before previewing a replacement.')
            document = shared.run(head=base)
            if not document or document['run']['run_id'] != requested_run:
                raise ValueError("This report is not the newest run. Reload the decision surface.")
            reviewer = shared.actor()['email']
        else:
            cfg, document, reviewer = self._decision_context(key, requested_run)
        run_id = str((document.get("run") or {}).get("run_id") or "")
        candidates = {
            str(candidate.get("candidate_id")): candidate
            for candidate in document.get("candidates") or []
            if candidate.get("candidate_id")
        }
        names = {candidate_id: str(candidate.get("asset_name") or "") for candidate_id, candidate in candidates.items()}
        unmatched = {
            str(entry.get("candidate_id")): entry
            for entry in (document.get("reconciliation") or {}).get("unmatched_verdicts") or []
            if entry.get("candidate_id")
        }
        mode = self._one(form, "mode")
        assignments: List[Tuple[str, str, str]] = []
        if mode in {"head", "recorded"}:
            selected: Dict[str, List[str]] = {}
            for axis in ("verdict", "lifecycle"):
                prefix = axis + ":"
                for field_name in sorted(form):
                    if field_name.startswith(prefix):
                        candidate_id = field_name[len(prefix):]
                        selected.setdefault(candidate_id, []).append(self._one(form, field_name))
            for candidate_id, values in sorted(selected.items()):
                if candidate_id not in candidates:
                    raise ValueError(f"Unknown candidate id {candidate_id!r}; reload the report.")
                reason = self._one(form, f"reason:{candidate_id}")
                if not reason:
                    raise ValueError(f"A reason is required for {candidate_id}.")
                assignments.extend((candidate_id, value, reason) for value in values)
            if not assignments:
                raise ValueError("Select at least one candidate decision to preview.")
        elif mode == "backlog":
            owner = self._one(form, "owner")
            band = self._one(form, "band")
            values = [
                value
                for value in (
                    self._one(form, "group_verdict", required=False),
                    self._one(form, "group_lifecycle", required=False),
                )
                if value
            ]
            if not values:
                raise ValueError("Select a verdict, a lifecycle value, or both for the backlog group.")
            reason = self._one(form, "group_reason")
            if not reason:
                raise ValueError("A reason is required for every backlog group member.")
            existing = shared.ledger(base) if shared else self.api.ledger_mod.Ledger(
                cfg.ledger_path,
                cycle_days=int(cfg.get("ledger", "cycle_days", default=30)),
                quarantine_days=int(cfg.get("ledger", "quarantine_period_days", default=90)),
            ).load()
            try:
                selected = self.api.verdict_mod.select_backlog_group(
                    document,
                    cfg,
                    owner,
                    band,
                    self._one(form, "revisit", required=False) == "yes",
                    existing,
                )
            except SystemExit as exc:
                raise ValueError(str(exc)) from None
            if not selected:
                raise ValueError("That backlog group has no undecided candidates to record.")
            assignments.extend(
                (str(candidate["candidate_id"]), value, reason)
                for candidate in selected
                for value in values
            )
        elif mode == "withdraw":
            candidate_id = self._one(form, "candidate_id")
            if candidate_id not in unmatched:
                raise ValueError(
                    f"{candidate_id!r} is not an active unmatched record; reload the report."
                )
            reason = self._one(form, "withdraw_reason")
            if not reason:
                raise ValueError("A reason is required to withdraw an invalid candidate identity.")
            names[candidate_id] = str(
                unmatched[candidate_id].get("asset_name_at_verdict") or candidate_id
            )
            assignments.append((candidate_id, "withdrawn", reason))
        else:
            raise ValueError("Unknown decision form; reload the report.")

        entries: List[object] = []
        for candidate_id, value, reason in assignments:
            try:
                entries.extend(
                    self.api.verdict_mod.parse_set(
                        [f"{candidate_id}={value}"], reason, reviewer, cfg, names, run_id
                    )
                )
            except SystemExit as exc:
                raise ValueError(str(exc)) from None
        # Validate the complete batch before retaining a preview.  append() repeats this
        # validation, but no file is opened here.
        for entry in entries:
            entry.validate()
        token = secrets.token_urlsafe(24)
        preview = PendingPreview(
            token,
            key,
            run_id,
            tuple(entries),
            tuple(entry.as_line() for entry in entries),
            base,
        )
        self.pending_previews[token] = preview
        append_walkthrough(self.root, key, "decision_preview", "passed", f"{len(entries)} line(s)")
        return preview

    def confirm_decisions(self, key: str, token: str) -> PendingPreview:
        with self.operation_lock:
            preview = self.pending_previews.get(token)
            if not preview or preview.instance_key != key:
                raise ValueError("That decision preview is missing, expired, or already confirmed.")
            cfg = self.api.config_mod.load(key, root=self.root)
            if preview.shared_base is not None:
                self._shared_store(cfg).decide(list(preview.entries), preview.run_id,
                                                preview.shared_base, preview.token)
            else:
                cfg, _, _ = self._decision_context(key, preview.run_id)
                self.api.ledger_mod.append(cfg.ledger_path, list(preview.entries))
            del self.pending_previews[token]
        append_walkthrough(self.root, key, "decision_confirm", "passed", f"{len(preview.entries)} line(s)")
        return preview

    def render_preview(self, preview: PendingPreview) -> bytes:
        grouped = {}
        for line in preview.lines:
            row = json.loads(line)
            grouped.setdefault(row['candidate_id'], []).append(row)
        cards = []
        for candidate_id, rows in grouped.items():
            name = rows[0].get('asset_name_at_verdict') or candidate_id
            choices = ''.join('<dt>' + html.escape(row['axis'].title()) + '</dt><dd><strong>' +
                              html.escape(row['value'].replace('-', ' ').capitalize()) + '</strong></dd>'
                              for row in rows)
            reasons = '\n'.join(dict.fromkeys(row.get('reason') or '—' for row in rows))
            choices += '<dt>Reason</dt><dd>' + html.escape(reasons) + '</dd>'
            parts = candidate_id.split(':')
            context = (parts[0].title() + ' · ' + parts[-1].replace('-', ' ')) if len(parts) >= 3 else candidate_id
            cards.append('<article class="card"><h2>' + html.escape(name) + '</h2><p class="muted">' +
                         html.escape(context) + '</p><dl class="decision-summary">' + choices +
                         '<dt>Reviewer</dt><dd>' + html.escape(rows[0]['by']) + '</dd></dl></article>')
        records = '\n'.join(preview.lines)
        body = f'''<p><a href="/report?instance={urllib.parse.quote(preview.instance_key)}&file=summary.html">← Back without submitting</a></p>
<div class="eyebrow">Review before submitting</div><h2>Confirm decisions for {len(grouped)} candidate(s)</h2>
<p>Check the choices and reasons below. Nothing has been saved yet.</p>
{''.join(cards)}
<form method="post" action="/action/decision-confirm"><input type="hidden" name="csrf" value="{self.csrf}">
<input type="hidden" name="instance" value="{html.escape(preview.instance_key)}">
<input type="hidden" name="preview" value="{html.escape(preview.token)}">
<button type="submit">Submit decisions</button></form>
<details><summary>Technical record details</summary><pre>{html.escape(records)}</pre></details>'''
        return _page("Confirm decisions", body)

    def render_report(self, key: str, filename: str, message: str = "", run_id=None, owner=None, export=False) -> bytes:
        cfg = self.api.config_mod.load(key, root=self.root)
        if appdb_mod.configured(cfg):
            store = self._shared_store(cfg)
            head = store.head()
            document = store.run(run_id, head=head, owner=owner)
            if not document:
                raise ValueError("No shared run is available yet.")
            latest_id = store.manifest(head['latest'])['run_id']
            current = document['run']['run_id'] == latest_id
            interaction = None
            if current and not export:
                interaction = self.api.report_html_mod.Interaction(
                    instance=key, run_id=latest_id, reviewer=store.actor()['email'], csrf=self.csrf, revision=head['revision'])
            # Historical evidence is never rewritten with today's decisions.
            warnings = []
            if current:
                ledger = store.ledger(head)
                today = dt.date.fromisoformat(document['run']['as_of_date'])
                for candidate in document['candidates']:
                    folded = ledger.get(candidate['candidate_id'])
                    if folded:
                        candidate['ledger'] = folded.as_dict(today, int(cfg.get('ledger', 'cycle_days', default=30)))
                    else:
                        candidate['ledger'] = {}
                    candidate.update(triage_mod.guidance(candidate.get('condition', ''),
                        (candidate.get('cost') or {}).get('recurring'),
                        bool(candidate['ledger'].get('suppressed'))).as_dict())
                for entry in ledger.entries:
                    if owner and entry.candidate_id not in {c['candidate_id'] for c in document['candidates'] if (c.get('owner') or {}).get('name') == owner}:
                        continue
                    warnings.append(f"{entry.candidate_id}: {entry.value} — {entry.by} — {entry.at} — {entry.reason or ''}")
            if filename == "master.md":
                return self.api.report_mod.render(document, cfg, owner=owner).encode()
            contents = self.api.report_html_mod.render(document, cfg, owner=owner, interaction=interaction)
            notice = '<p><a href="/?instance=' + urllib.parse.quote(key) + '">Back to shared runs</a></p>'
            urlfields = {'instance': key, 'file': 'summary.html', 'run': document['run']['run_id']}
            notice += '<form method="get" action="/report">' + ''.join('<input type="hidden" name="'+k+'" value="'+html.escape(v)+'">' for k,v in urlfields.items())
            owners = document.get('_owners') or sorted({(c.get('owner') or {}).get('name') or 'Unassigned' for c in document['candidates']})
            notice += '<label>Owner<select name="owner"><option value="">All owners</option>' + ''.join('<option '+('selected ' if o == owner else '')+'value="'+html.escape(o)+'">'+html.escape(o)+'</option>' for o in owners) + '</select></label><button>Filter review</button></form>'
            exporturl = urllib.parse.urlencode({**urlfields, 'owner': owner or '', 'download': 'yes'})
            notice += '<p><a href="/report?' + html.escape(exporturl) + '">Optional read-only export</a></p>'
            notice += '<p>Evidence and totals reflect the selected run. Recorded decisions are current for the latest run.</p>'
            if message:
                notice += '<p class="notice">' + html.escape(message) + '</p>'
            if warnings:
                notice += '<details><summary>Recorded decisions — discuss changes with the reviewer before submitting a replacement</summary><pre>' + html.escape('\n'.join(warnings)) + '</pre></details>'
            contents = contents.replace('<main>', '<main>' + notice, 1)
            return contents.encode()
        state = derive_state(self.api, self.root, key)
        allowed = {os.path.basename(path): path for path in state.report_files}
        path = allowed.get(filename)
        if not path:
            raise FileNotFoundError(filename)
        if filename == "summary.html":
            cfg, document, reviewer = self._decision_context(key, state.run_id or "")
            interaction = self.api.report_html_mod.Interaction(
                instance=key,
                run_id=state.run_id or "",
                reviewer=reviewer,
                csrf=self.csrf,
            )
            contents = self.api.report_html_mod.render(document, cfg, interaction=interaction)
            if message:
                contents = contents.replace(
                    "<main>", f'<main><p class="notice"><strong>{html.escape(message)}</strong></p>', 1
                )
            return contents.encode("utf-8")
        with open(path, "r", encoding="utf-8") as handle:
            contents = handle.read()
        if filename.endswith(".html"):
            return contents.encode("utf-8")
        return _page(filename, f'<p><a href="/?instance={urllib.parse.quote(key)}">← Back to guide</a></p><pre>{html.escape(contents)}</pre>')


class GuideHandler(BaseHTTPRequestHandler):
    server_version = "DomoCleanup/1"

    @property
    def app(self) -> GuideApplication:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:
        return

    def _send(self, body: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _host_allowed(self) -> bool:
        """Refuse DNS rebinding: a hostile page that re-points its own name at 127.0.0.1
        reaches this server with its own name in `Host`. Only the loopback names pass."""
        port = self.server.server_address[1]
        if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return True
        self._send(_page("Wrong address", "<p>Open the address printed in the launcher window.</p>"), 421)
        return False

    def _key(self, values: Mapping[str, List[str]]) -> Optional[str]:
        key = (values.get("instance") or [self.app.initial_instance or ""])[0]
        if not key:
            return None
        if not KEY_RE.match(key):
            raise ValueError("Invalid instance key.")
        return key

    def do_GET(self) -> None:
        if not self._host_allowed():
            return
        parsed = urllib.parse.urlparse(self.path)
        values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            key = self._key(values)
            if parsed.path == '/shared-setup.js':
                self._send(b"document.getElementById('shared-setup').requestSubmit();", content_type='text/javascript; charset=utf-8')
                return
            if parsed.path == '/restore.js':
                self._send(b"document.getElementById('backup-file').addEventListener('change', async function(){const button=document.getElementById('restore-submit');button.disabled=true;document.getElementById('backup').value=await this.files[0].text();button.disabled=false;});", content_type='text/javascript; charset=utf-8')
                return
            if parsed.path == "/":
                self._send(self.app.render_home(key))
                return
            if parsed.path == "/report":
                filename = (values.get("file") or [""])[0]
                cfg = self.app.api.config_mod.load(key or "", root=self.app.root)
                if not appdb_mod.configured(cfg):
                    self._send(self.app.render_home(key))
                    return
                self._send(self.app.render_report(key or "", filename,
                    run_id=(values.get('run') or [None])[0], owner=(values.get('owner') or [None])[0] or None,
                    export=(values.get('download') or [''])[0] == 'yes'),
                    content_type='text/plain; charset=utf-8' if filename.endswith('.md') else 'text/html; charset=utf-8')
                return
            if parsed.path == '/history':
                self._send(self.app.render_history(key, (values.get('offset') or [0])[0]))
                return
            if parsed.path == '/backup':
                cfg = self.app.api.config_mod.load(key, root=self.app.root)
                backup = appdb_mod.open_store(cfg).backup()
                appdb_mod.inspect_backup(cfg, backup)
                data = json.dumps(backup, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Disposition', 'attachment; filename="cleanup-backup.json"')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send(_page("Not found", "<p>That local page does not exist.</p>"), 404)
        except Exception as exc:
            self._send(_page("Could not load", f'<section class="card error"><p>{html.escape(str(exc))}</p></section>'), 400)

    def do_POST(self) -> None:
        if not self._host_allowed():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            maximum = 100_000_000 if urllib.parse.urlparse(self.path).path == '/action/shared-restore' else 128_000
            if length < 0 or length > maximum:
                raise ValueError("Form submission is too large.")
            raw = self.rfile.read(length).decode("utf-8")
            values = urllib.parse.parse_qs(raw, keep_blank_values=True)
            if not secrets.compare_digest((values.get("csrf") or [""])[0].encode(), self.app.csrf.encode()):
                raise ValueError("This form belongs to an earlier launcher session. Reload and try again.")
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/action/start":
                domain = _normalize_domain((values.get("domain") or [""])[0])
                key = self.app.api.instance_key(domain)
                if not KEY_RE.match(key):
                    raise ValueError("The Domo hostname does not produce a safe local instance key.")
                route = resolve_start_token(
                    self.app.root,
                    key,
                    (values.get("token") or [""])[0],
                    bool((values.get("replace") or [""])[0]),
                )
                self.app.api.create_config(self.app.root, key, domain)
                append_walkthrough(self.app.root, key, "local_profile", "saved", f"token {route}")
                self.send_response(303)
                self.send_header("Location", f"/?instance={urllib.parse.quote(key)}")
                self.end_headers()
                return
            key = self._key(values)
            if not key:
                raise ValueError("Choose an instance first.")
            if parsed.path.startswith('/action/shared-'):
                message = self.app.shared_action(key, parsed.path.rsplit('/', 1)[1], values)
                if parsed.path == '/action/shared-auto':
                    self.send_response(303)
                    self.send_header('Location', '/?instance=' + urllib.parse.quote(key))
                    self.end_headers()
                    return
                self._send(self.app.render_home(key, message))
                return
            if parsed.path.startswith('/action/decision-'):
                cfg = self.app.api.config_mod.load(key, root=self.app.root)
                if not appdb_mod.configured(cfg):
                    raise ValueError('Connect shared cleanup before recording decisions.')
            if parsed.path == "/action/token":
                save_token(self.app.root, key, (values.get("token") or [""])[0])
                append_walkthrough(self.app.root, key, "local_profile", "saved")
                self.send_response(303)
                self.send_header("Location", f"/?instance={urllib.parse.quote(key)}")
                self.end_headers()
                return
            if parsed.path == "/action/reviewer":
                save_reviewer(self.app.root, key, (values.get("email") or [""])[0])
                append_walkthrough(self.app.root, key, "reviewer", "saved")
                self.send_response(303)
                self.send_header("Location", f"/?instance={urllib.parse.quote(key)}")
                self.end_headers()
                return
            if parsed.path == "/action/check-setup":
                state, view, output = self.app.check_setup(key)
                self._send(self.app.render_setup(view, output) if view else self.app.render_home(key, output=output))
                return
            if parsed.path == "/action/confirm-reports":
                if "check-manual" in values:
                    report_key = self.app._one(values, "check-manual")
                    view = self.app.check_manual_report(
                        key, report_key, self.app._one(values, f"manual:{report_key}")
                    )
                    self._send(self.app.render_setup(view))
                    return
                state = self.app.confirm_reports(key, values)
                self._send(self.app.render_home(key, "Report choices saved and checked."))
                return
            if parsed.path == "/action/run":
                state, output = self.app.run_cleanup(key)
                self._send(self.app.render_home(key, "Cleanup run finished.", output))
                return
            if parsed.path == "/action/render":
                state, output = self.app.render_reports(key)
                self._send(self.app.render_home(key, "Reports rendered.", output))
                return
            if parsed.path == "/action/decision-preview":
                preview = self.app.preview_decisions(key, values)
                self._send(self.app.render_preview(preview))
                return
            if parsed.path == "/action/decision-confirm":
                preview = self.app.confirm_decisions(key, self.app._one(values, "preview"))
                self._send(
                    self.app.render_report(
                        key,
                        "summary.html",
                        f"Recorded {len(preview.lines)} decision{'s' if len(preview.lines) != 1 else ''}.",
                    )
                )
                return
            self._send(_page("Not found", "<p>That local action does not exist.</p>"), 404)
        # The collector stops with SystemExit (credential custody, CLI guards). It is not an
        # Exception, and an uncaught one in a server thread closes the socket silently: the
        # browser shows an empty response and the terminal shows nothing.
        except (Exception, SystemExit) as exc:
            key = None
            try:
                key = self._key(locals().get("values", {}))
            except Exception:
                LOG.warning("Could not read the instance from a failed request; showing the generic error page.",
                            exc_info=True)
            decision_action = bool(
                locals().get("parsed")
                and getattr(locals()["parsed"], "path", "").startswith("/action/decision-")
            )
            if key and decision_action:
                try:
                    body = self.app.render_report(key, "summary.html", f"Stopped safely: {exc}")
                except Exception:
                    LOG.exception("Could not render the report after a stopped decision action.")
                    body = _page("Stopped safely", "<p>" + html.escape(str(exc)) + "</p>")
            elif key:
                body = _page("Stopped safely", '<p>' + html.escape(str(exc)) + '</p><p><a href="/?instance=' + urllib.parse.quote(key) + '">Retry</a></p>' + self.app.storage_recovery(key))
            else:
                body = _page("Stopped safely", f'<section class="card error"><p>{html.escape(str(exc))}</p></section>')
            self._send(body, 400)


def make_server(api, root: str, instance: Optional[str] = None, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), GuideHandler)
    server.daemon_threads = True
    server.app = GuideApplication(api, root, initial_instance=instance)  # type: ignore[attr-defined]
    return server


def serve(api, root: str, instance: Optional[str] = None, port: int = 0, open_browser: bool = True) -> int:
    server = make_server(api, root, instance=instance, port=port)
    host, actual_port = server.server_address[:2]
    query = f"?instance={urllib.parse.quote(instance)}" if instance else ""
    url = f"http://{host}:{actual_port}/{query}"
    print(f"Domo cleanup is open at {url}")
    print("Close this launcher window or press Ctrl+C when you are finished.")
    if instance:
        append_walkthrough(root, instance, "browser", "opened", f"localhost:{actual_port}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
