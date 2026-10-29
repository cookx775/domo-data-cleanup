"""Loads `reports.yaml` — the canonical governance-report manifest.

One loader, so the collector's queries, discovery's fingerprints and the drift test all
read the same declaration. Two implementations of shape matching would drift, and the one
that drifts is the one that runs on a stranger's instance.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, List, Optional

import yaml

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports.yaml")


class ManifestError(Exception):
    """The manifest is internally inconsistent. Raised at load time, so a bad manifest
    cannot reach an instance."""


class Report:
    """One governance report's declared identity and read surface."""

    __slots__ = (
        "key",
        "name_hint",
        "connector",
        "provider_prefix",
        "floor",
        "grain",
        "if_missing",
        "conditions",
        "purpose",
        "identify",
        "read",
        "nullable",
    )

    def __init__(self, key: str, raw: dict) -> None:
        self.key = key
        self.name_hint = raw.get("name_hint", "")
        self.connector = raw.get("connector", "")
        self.provider_prefix = raw.get("provider_prefix", "")
        self.floor = bool(raw.get("floor", False))
        self.grain = raw.get("grain", "")
        # One line, admin-facing: what breaks if this report is absent. Lives here rather
        # than in the generator so the shipped config template cannot drift from the roster.
        self.if_missing = (raw.get("if_missing") or "").strip()
        self.conditions = list(raw.get("conditions") or [])
        self.purpose = (raw.get("purpose") or "").strip()
        self.identify = list(raw.get("identify") or [])
        self.read = list(raw.get("read") or [])
        self.nullable = list(raw.get("nullable") or [])

        if not self.identify:
            raise ManifestError(f"{key}: empty identify set — discovery would match everything")
        if not self.read:
            raise ManifestError(f"{key}: empty read set")

        missing = [c for c in self.identify if c not in self.read]
        if missing:
            raise ManifestError(
                f"{key}: identify columns {missing} are not in the read set. Discovery would "
                f"match on a column the collector never reads, which a connector release can "
                f"drop for no analytical gain."
            )
        stray = [c for c in self.nullable if c not in self.read]
        if stray:
            raise ManifestError(f"{key}: nullable columns {stray} are not in the read set")

    def is_nullable(self, column: str) -> bool:
        return column in self.nullable

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Report({self.key}, floor={self.floor}, read={len(self.read)})"


class Manifest:
    def __init__(self, raw: dict) -> None:
        self.version = str(raw.get("version", "0"))
        self.ignore_columns = list(raw.get("ignore_columns") or [])
        reports = raw.get("reports") or {}
        if not reports:
            raise ManifestError("manifest declares no reports")
        self.reports: Dict[str, Report] = {
            key: Report(key, value) for key, value in reports.items()
        }

    # -- access -------------------------------------------------------------------------

    def __getitem__(self, key: str) -> Report:
        try:
            return self.reports[key]
        except KeyError:
            raise ManifestError(
                f"no report {key!r} in the manifest (have: {sorted(self.reports)})"
            ) from None

    def __contains__(self, key: str) -> bool:
        return key in self.reports

    def __iter__(self) -> Iterable[Report]:
        return iter(self.reports.values())

    @property
    def keys(self) -> List[str]:
        return list(self.reports)

    @property
    def floor(self) -> List[Report]:
        """The required floor. A missing floor report is a hard prerequisite failure with
        no fallback — rehearsed by fixture, never discovered live."""
        return [r for r in self.reports.values() if r.floor]

    def for_condition(self, condition: str) -> List[Report]:
        return [r for r in self.reports.values() if condition in r.conditions]

    def strip_ignored(self, columns: Iterable[str]) -> List[str]:
        """Drop the connector-injected columns before fingerprinting."""
        ignored = set(self.ignore_columns)
        return [c for c in columns if c not in ignored]


_cached: Optional[Manifest] = None


def load(path: Optional[str] = None, force: bool = False) -> Manifest:
    global _cached
    if _cached is not None and path is None and not force:
        return _cached
    target = path or MANIFEST_PATH
    with open(target, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    manifest = Manifest(raw)
    if path is None:
        _cached = manifest
    return manifest
