"""The administrator-facing front door for the generated cleanup package.

This is intentionally thin: discovery, preflight, collection, and the append-only ledger stay
in their existing modules. The front door makes their sequence legible without creating a second
implementation that could drift from them.

    python cleanup.py open
    python cleanup.py setup  --instance acme.domo.com
    python cleanup.py run    --instance acme
    python cleanup.py review --instance acme
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Callable, Dict, Iterable, Optional

# ``cleanup_web.py`` is copied beside this file in the generated package. Tests also load
# this source file directly, where Python does not automatically add its directory to
# ``sys.path`` as it does for ``python cleanup.py``.
_FRONT_DOOR_DIR = os.path.abspath(os.path.dirname(__file__))
if _FRONT_DOOR_DIR not in sys.path:
    sys.path.insert(0, _FRONT_DOOR_DIR)

import yaml

from collector import config as config_mod
from collector import ledger as ledger_mod
from collector import manifest as manifest_mod
from collector import report as report_mod
from collector import report_html as report_html_mod
from collector import run as run_mod
from collector import verdict as verdict_mod
from collector import appdb as appdb_mod
from collector.discover import proposal_path, write_proposal
from shared.domo import AuthError, DomoClient, load_token
from shared import preflight as preflight_mod


def instance_key(instance: str, explicit: Optional[str] = None) -> str:
    """The local directory name; the network target remains the full supplied host."""
    if explicit:
        return explicit
    host = instance.strip().replace("https://", "").replace("http://", "").rstrip("/")
    return host.split(".", 1)[0]


def package_root() -> str:
    """Work in the extracted package when present, otherwise in the source checkout."""
    here = os.path.abspath(os.path.dirname(__file__))
    if os.path.isfile(os.path.join(here, "pyproject.toml")):
        return here
    return config_mod.repo_root()


def create_config(root: str, key: str, instance: str) -> tuple:
    """Create only local state. Existing configuration is never overwritten."""
    path = os.path.join(root, "instances", key, "config.yaml")
    if os.path.exists(path):
        return path, False
    host = instance.strip().replace("https://", "").replace("http://", "").rstrip("/")
    if "." not in host:
        host = f"{host}.domo.com"
    manifest = manifest_mod.load()
    document = {
        "instance": key,
        "domain": host,
        "source": "direct",
        "reports": {report.key: None for report in manifest},
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# Created by cleanup.py. Report ids become authoritative only after you confirm them.\n")
        yaml.safe_dump(document, handle, sort_keys=False)
    return path, True


def _probe_label(candidate) -> str:
    probe = candidate.probe
    if not probe:
        return "not probed"
    if probe.ok:
        return f"HTTP {probe.status}, returns rows"
    return f"HTTP {probe.status}, {probe.detail or 'does not return usable rows'}"


def inspect_report_id(client: DomoClient, key: str, dataset_id: str):
    """Inspect a manually entered ID without making it authoritative."""
    dataset_id = dataset_id.strip()
    if not dataset_id:
        raise ValueError("Enter a dataset ID to check.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", dataset_id):
        raise ValueError("Dataset IDs may contain only letters, digits, hyphens, and underscores.")
    manifest = manifest_mod.load()
    try:
        name = client.dataset_name(dataset_id)
    except Exception as exc:
        raise ValueError(f"Could not read the dataset name: {exc}") from exc
    if not name:
        raise ValueError("Domo returned no dataset name for this ID.")
    check = preflight_mod.check_report_id(client, manifest, manifest[key], dataset_id)
    return name, check


def choose_reports(discovery, missing: Iterable[str], ask: Callable[[str], str] = input,
                   client: Optional[DomoClient] = None, manual_keys: Optional[set] = None) -> Dict[str, str]:
    """Ask for every selection, including a lone high-confidence candidate.

    `Discovery.selected` is a proposal, not a configuration value. A default-yes prompt would
    erase the distinction that protects an instance containing replicas and derivatives.
    """
    chosen: Dict[str, str] = {}
    for key in missing:
        proposal = discovery.proposals.get(key)
        if not proposal or not proposal.candidates:
            print(f"\n{key}: no high-confidence candidate to confirm.")
            if client is not None:
                entered = ask("  Enter another dataset ID, or Enter to leave unconfigured: ").strip()
                if entered:
                    _choose_manual(chosen, client, key, entered, ask, manual_keys)
            continue
        print(f"\n{key}:")
        for index, candidate in enumerate(proposal.candidates, start=1):
            missing_read = f"; missing {candidate.missing_read}" if candidate.missing_read else ""
            print(f"  {index}. {candidate.name} ({candidate.dataset_id}) — {_probe_label(candidate)}{missing_read}")
        if client is not None:
            print("  m. Enter another dataset ID")
        if len(proposal.candidates) == 1 and client is None:
            candidate = proposal.candidates[0]
        else:
            answer = ask(f"  Choose a candidate [1-{len(proposal.candidates)}], m for another ID, or Enter to leave unconfigured: ").strip()
            if not answer:
                print("  Left unconfigured.")
                continue
            if answer.lower() == "m" and client is not None:
                entered = ask("  Enter another dataset ID: ").strip()
                _choose_manual(chosen, client, key, entered, ask, manual_keys)
                continue
            try:
                candidate = proposal.candidates[int(answer) - 1]
            except (ValueError, IndexError):
                print("  Not a listed candidate; left unconfigured.")
                continue
        answer = ask(f"  Use {candidate.name!r}? [y/N] ").strip().lower()
        if answer != "y":
            print("  Not selected.")
            continue
        chosen[key] = candidate.dataset_id
        print("  Confirmed.")
    return chosen


def _choose_manual(chosen, client, key, dataset_id, ask, manual_keys=None):
    try:
        name, check = inspect_report_id(client, key, dataset_id)
    except ValueError as exc:
        print(f"  Could not validate {dataset_id}: {exc}")
        return
    if not check.ok or check.missing_read:
        print(f"  {dataset_id}: {check.detail}; missing fields: "
              f"{check.missing_identify + check.missing_read}. Left unconfigured.")
        return
    print(f"  {name} ({dataset_id}) — {_probe_label(check)}; required fields present.")
    if ask(f"  Use {name!r}? [y/N] ").strip().lower() == "y":
        chosen[key] = dataset_id
        if manual_keys is not None:
            manual_keys.add(key)
        print("  Confirmed manually entered ID.")


def write_confirmed_reports(path: str, chosen: Dict[str, Optional[str]],
                            manual_keys: Iterable[str] = ()) -> None:
    with open(path, "r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    reports = document.setdefault("reports", {})
    reports.update(chosen)
    sources = document.setdefault("report_selection", {})
    manually_entered = set(manual_keys)
    for key, dataset_id in chosen.items():
        sources[key] = "manual" if key in manually_entered else "proposed" if dataset_id else "unconfigured"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# Created by cleanup.py. Only explicitly confirmed ids are authoritative.\n")
        yaml.safe_dump(document, handle, sort_keys=False)


def print_result(result) -> None:
    print(f"\nCheck setup — {result.instance}\n")
    for line in result.report_lines():
        print(line)
    if result.custody:
        print("\nCREDENTIAL CUSTODY:")
        for problem in result.custody:
            print(f"  {problem}")
    if result.discovery_hint:
        print("\nCandidates for reports that need attention:")
        for line in result.discovery_hint:
            print(line)


def auth_stop(exc: AuthError, key: str, env_path: str) -> int:
    print("\nAUTHENTICATION STOP")
    print(f"  {exc}")
    print("  No report candidates were displayed, selected, or written.")
    print(f"  Put a token for this exact instance in {env_path}, or export DOMO_API_TOKEN.")
    print(f"  Then retry: python cleanup.py setup --instance {key}")
    print("  The API cannot identify an expiry date for the token in hand; a successful check is the only honest test.")
    return 2


def setup(args, ask: Callable[[str], str] = input) -> int:
    root = package_root()
    key = instance_key(args.instance, args.instance_key)
    path, created = create_config(root, key, args.instance)
    if created:
        print(f"Created {path}. It is local state; re-extracting the package cannot overwrite it.")
    cfg = config_mod.load(key, root=root)
    try:
        client = DomoClient(cfg.domain, load_token(args.env or cfg.env_path))
        initial = preflight_mod.preflight(client, cfg)
    except AuthError as exc:
        return auth_stop(exc, key, args.env or cfg.env_path)

    print_result(initial)
    if initial.ok and not any(not check.ok for check in initial.checks.values()):
        print("\nRequired reports are ready. Next: python cleanup.py run --instance " + key)
        return 0
    if initial.discovery:
        proposal = proposal_path(root, key)
        write_proposal(initial.discovery, proposal)
        print(f"\nFull evidence, including rejected look-alikes: {proposal}")
    missing = [check.key for check in initial.checks.values() if not check.ok]
    discovery = initial.discovery or type("NoDiscovery", (), {"proposals": {}})()
    manual_keys: set = set()
    chosen = choose_reports(discovery, missing, ask=ask, client=client, manual_keys=manual_keys)
    if not chosen:
        print("\nNo report ids changed. Setup remains incomplete; the proposal is available for review.")
        return 1

    write_confirmed_reports(path, chosen, manual_keys=manual_keys)
    print(f"\nWrote {len(chosen)} explicitly confirmed report id(s) to {path}. Checking them now.")
    cfg = config_mod.load(key, root=root)
    try:
        final = preflight_mod.preflight(client, cfg, offer_discovery=False)
    except AuthError as exc:
        return auth_stop(exc, key, args.env or cfg.env_path)
    print_result(final)
    if final.ok:
        print("\nRequired reports are ready. Next: python cleanup.py run --instance " + key)
        return 0
    print("\nSetup is incomplete. The chosen ids were preserved; correct them and run setup again.")
    return 1


def write_reading_copies(instance: str, run_id: Optional[str] = None) -> int:
    """Render the master report and owner supplements for a run, and say where to start.

    The findings JSON stays the artifact of record; these are reading copies, byte-identical for
    the same findings file. Called by `run` so the administrator's next move after a run is to
    open a document, and by `review` with no flags. Granite rehearsal, 2026-09-02: deciding
    before reading felt backwards because the front door made it look required. It never was.
    """
    cfg = config_mod.load(instance, root=package_root())
    if appdb_mod.configured(cfg):
        store = appdb_mod.open_store(cfg)
        document = store.run(run_id)
        if document is None:
            raise ValueError("No shared run is available yet.")
        print("Shared review is ready. Open the browser launcher; downloads are optional.")
        return 0
    document = verdict_mod.load_findings(cfg.runs_dir, run_id)
    written = report_mod.write_reports(cfg, document)
    out_dir = os.path.dirname(written[0])
    print(f"\nReports written to {out_dir}/")
    print(f"  summary.html                 the portable decision surface — start here")
    print(f"  master.md                    everything, in document order — large on a first run")
    print(f"  owner-<name>.md              one person's head and backlog; the readable unit")
    print("Read first. Open the local cleanup launcher to record decisions from summary.html after an exact preview.")
    print("The portable file remains read-only; Markdown keeps the audited command-line fallback.")
    print("The findings JSON remains the artifact of record; no verdict was recorded.")
    return 0


def run(args) -> int:
    forwarded = ["--instance", args.instance]
    if args.env:
        forwarded.extend(["--env", args.env])
    if args.prior:
        forwarded.extend(["--prior", args.prior])
    try:
        code = run_mod.main(forwarded)
    except AuthError as exc:
        key = instance_key(args.instance)
        return auth_stop(exc, key, args.env or os.path.join(package_root(), "instances", key, ".env"))
    if code != 0:
        return code
    # Run, then READ, then record. The reading copies are derived from the findings file the
    # run just wrote, so they cannot disagree with it, and re-rendering later is free.
    cfg = config_mod.load(args.instance, root=package_root())
    if appdb_mod.configured(cfg):
        print("Published to shared cleanup storage. Open the browser review.")
        return 0
    return write_reading_copies(args.instance)


def review(args) -> int:
    group_options = {
        "owner": getattr(args, "owner", None),
        "band": getattr(args, "band", None),
        "verdict": getattr(args, "verdict", None),
        "reason": getattr(args, "reason", None),
    }
    dry_run = bool(getattr(args, "dry_run", False))
    explicit_sets = list(getattr(args, "set", None) or [])
    if explicit_sets:
        # The head's record path: the command each head entry prints. One --set per candidate,
        # the collector's own verdict module enforces the values and the reason rule.
        if getattr(args, "owner", None) or getattr(args, "band", None) or getattr(args, "verdict", None):
            raise SystemExit("--set records named candidates; --owner/--band/--verdict select a backlog group. Use one path.")
        forwarded = ["--instance", args.instance]
        if args.run:
            forwarded.extend(["--run", args.run])
        if args.by:
            forwarded.extend(["--by", args.by])
        for assignment in explicit_sets:
            forwarded.extend(["--set", assignment])
        if getattr(args, "reason", None):
            forwarded.extend(["--reason", args.reason])
        if dry_run:
            forwarded.append("--dry-run")
        return verdict_mod.main(forwarded)
    group_requested = any(value is not None for value in group_options.values()) or dry_run
    if group_requested and not args.backlog:
        raise SystemExit("--owner, --band, --verdict, --reason, and --dry-run require --backlog (or --set)")
    if args.backlog:
        if group_requested:
            missing = [
                f"--{name}" for name, value in group_options.items()
                if value is None or (isinstance(value, str) and not value.strip())
            ]
            if missing:
                raise SystemExit(
                    "a bulk backlog verdict requires --owner, --band, --verdict, and --reason; "
                    f"missing {', '.join(missing)}"
                )
            allowed = ledger_mod.VERDICT_VALUES + ledger_mod.LIFECYCLE_VALUES
            if args.verdict not in allowed:
                raise SystemExit(
                    f"unknown verdict {args.verdict!r}; choose one of {', '.join(allowed)}"
                )
        cfg = config_mod.load(args.instance, root=package_root())
        shared = appdb_mod.open_store(cfg) if appdb_mod.configured(cfg) else None
        document = shared.run(args.run) if shared else verdict_mod.load_findings(cfg.runs_dir, args.run)
        if group_requested:
            existing = shared.ledger() if shared else ledger_mod.Ledger(
                cfg.ledger_path,
                cycle_days=int(cfg.get("ledger", "cycle_days", default=30)),
                quarantine_days=int(cfg.get("ledger", "quarantine_period_days", default=90)),
            ).load()
            candidates = verdict_mod.select_backlog_group(
                document, cfg, args.owner, args.band, args.revisit, existing
            )
            assets = {}
            for candidate in candidates:
                key = (candidate.get("asset_type") or "", candidate.get("asset_id") or "")
                row = assets.setdefault(
                    key,
                    {"name": candidate.get("asset_name") or key[1], "candidate_ids": []},
                )
                row["candidate_ids"].append(candidate["candidate_id"])
            print(
                f"Backlog group — owner {args.owner!r}, band {args.band!r}: "
                f"{len(candidates)} candidate(s) across {len(assets)} asset(s)."
            )
            for (asset_type, asset_id), row in sorted(
                assets.items(), key=lambda item: (str(item[1]["name"]).casefold(), item[0])
            ):
                print(
                    f"  - {row['name']} ({asset_type}:{asset_id}) — "
                    + ", ".join(row["candidate_ids"])
                )
            if not candidates:
                print("Nothing appended. Use --revisit to include members that already carry ledger state.")
                return 0

            forwarded = ["--instance", args.instance, "--run", document["run"]["run_id"]]
            if args.by:
                forwarded.extend(["--by", args.by])
            for candidate in candidates:
                forwarded.extend(["--set", f"{candidate['candidate_id']}={args.verdict}"])
            forwarded.extend(["--reason", args.reason])
            if dry_run:
                forwarded.append("--dry-run")
            return verdict_mod.main(forwarded)

        return write_reading_copies(args.instance, args.run)
    if getattr(args, "head", False):
        # The one-at-a-time terminal loop, opt-in. It is the right tool for a short individual
        # decision set and the wrong one for a long one; the report's per-entry commands cover both.
        print("Individual decisions — one verdict at a time. The same decisions can be recorded from the report's per-entry commands.")
        forwarded = ["--instance", args.instance]
        if args.run:
            forwarded.extend(["--run", args.run])
        if args.by:
            forwarded.extend(["--by", args.by])
        if args.revisit:
            forwarded.append("--revisit")
        return verdict_mod.main(forwarded)
    return write_reading_copies(args.instance, args.run)


def open_browser(args) -> int:
    """Start the local-only browser guide. The CLI functions above remain its workers."""
    import cleanup_web

    return cleanup_web.serve(
        sys.modules[__name__],
        package_root(),
        instance=args.instance,
        port=args.port,
        open_browser=not args.no_browser,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python cleanup.py",
        description="Read-only Domo cleanup: check setup, run, read the reports, then record decisions.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="create local config, check reports, and explicitly confirm candidates")
    setup_parser.add_argument("--instance", required=True, help="instance host, e.g. acme.domo.com")
    setup_parser.add_argument("--instance-key", help="local directory name (default: first host label)")
    setup_parser.add_argument("--env", help="path to a file holding DOMO_API_TOKEN")
    run_parser = commands.add_parser("run", help="publish findings to shared cleanup storage")
    run_parser.add_argument("--instance", required=True, help="local instance key")
    run_parser.add_argument("--env", help="path to a file holding DOMO_API_TOKEN")
    run_parser.add_argument("--prior", help="path to the prior findings file")
    review_parser = commands.add_parser("review", help="render the reports (default), or record decisions with --set / --backlog / --head")
    review_parser.add_argument("--instance", required=True, help="local instance key")
    review_parser.add_argument("--run", help="run id to review (default: newest)")
    review_parser.add_argument("--by", help="reviewer identity (default: git config user.email)")
    review_parser.add_argument("--revisit", action="store_true", help="include candidates with prior verdicts")
    review_parser.add_argument("--head", action="store_true", help="walk individual decisions one verdict at a time in the terminal")
    review_parser.add_argument("--set", action="append", default=[], metavar="CANDIDATE_ID=VALUE",
                               help="record one decision on a named candidate; repeatable — the command each individual entry prints")
    review_parser.add_argument("--backlog", action="store_true", help="the owner-grouped path; alone it renders, with --owner/--band/--verdict/--reason it records")
    review_parser.add_argument("--owner", help="with --backlog, select this owner group")
    review_parser.add_argument("--band", help="with --backlog, select this recorded candidate band")
    review_parser.add_argument(
        "--verdict",
        help="with --backlog, expand this verdict or lifecycle value to every selected candidate",
    )
    review_parser.add_argument("--reason", help="the reason for --set or a bulk backlog verdict")
    review_parser.add_argument("--dry-run", action="store_true", help="print expanded JSONL and append nothing")
    open_parser = commands.add_parser("open", help="start the localhost guide and open it in your browser")
    open_parser.add_argument("--instance", help="local instance key to resume (otherwise choose in the browser)")
    open_parser.add_argument("--port", type=int, default=0, help="localhost port (default: choose an available port)")
    open_parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "setup":
        return setup(args)
    if args.command in ("run", "review"):
        cfg = config_mod.load(args.instance, root=package_root())
        if not appdb_mod.configured(cfg):
            appdb_mod.ensure_connection(cfg)
    if args.command == "run":
        return run(args)
    if args.command == "open":
        return open_browser(args)
    return review(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
