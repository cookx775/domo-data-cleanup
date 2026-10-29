"""Configuration: shipped defaults overlaid by one instance's overrides.

`config/defaults.yaml` ships as-is so a subsidiary administrator can run untouched;
`instances/<instance>/config.yaml` overrides per instance and is **authoritative for
dataset ids** (ticket 04 decision 7). Discovery proposes into a separate file that nothing
reads — auto-selection on the instance hosting eleven companies' replicas would silently
bind the collector to the wrong company and produce confident, well-formed, wrong findings.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Optional

import yaml

# projects/instance-cleanup/collector/config.py -> projects/instance-cleanup
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULTS_PATH = os.path.join(PROJECT_ROOT, "config", "defaults.yaml")


class ConfigError(Exception):
    """The configuration is missing something the run cannot proceed without."""


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Overlay wins per leaf. Lists are REPLACED, never concatenated — a threshold list
    that grew by merge would be impossible to shrink from an instance override."""
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    def __init__(self, data: dict, repo_root: str, instance_key: str, sources: List[str]):
        self._data = data
        self.repo_root = repo_root
        self.instance_key = instance_key
        self.sources = sources

    # -- identity ------------------------------------------------------------------------

    @property
    def instance(self) -> str:
        return self._data.get("instance") or self.instance_key

    @property
    def domain(self) -> str:
        domain = self._data.get("domain")
        if not domain:
            raise ConfigError(f"{self.instance_key}: config declares no `domain`")
        return domain

    @property
    def credit_domain(self) -> str:
        """Which `domain` value to filter Credit Usage on.

        Granite's copy meters all twelve portfolio domains in one table, so the filter is
        not optional there. A subsidiary reading its own report would still carry one
        domain value, so this defaults to `domain` rather than to no filter — an absent
        filter on granite would attribute another company's spend to this instance.
        """
        return self._data.get("credit_domain") or self.domain

    @property
    def source(self) -> str:
        """Always `direct`, per the findings schema: one path, every instance read through
        its own connectors. The field survives the two-path design it was created for
        because the artifact of record should say how it was read, not assume."""
        return self._data.get("source", "direct")

    @property
    def version(self) -> str:
        return str(self._data.get("version", "0"))

    # -- dataset ids ---------------------------------------------------------------------

    @property
    def reports(self) -> Dict[str, str]:
        return dict(self._data.get("reports") or {})

    def report_id(self, key: str) -> Optional[str]:
        return self.reports.get(key)

    # -- thresholds ----------------------------------------------------------------------

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, *path: str) -> Any:
        sentinel = object()
        value = self.get(*path, default=sentinel)
        if value is sentinel:
            raise ConfigError(f"config is missing {'.'.join(path)}")
        return value

    # -- paths ---------------------------------------------------------------------------

    @property
    def instance_dir(self) -> str:
        return os.path.join(self.repo_root, "instances", self.instance_key)

    @property
    def ledger_path(self) -> str:
        relative = self.get("ledger", "path", default="cleanup/ledger.jsonl")
        return os.path.join(self.instance_dir, relative)

    @property
    def runs_dir(self) -> str:
        return os.path.join(self.instance_dir, "cleanup", "runs")

    @property
    def env_path(self) -> str:
        return os.path.join(self.instance_dir, ".env")

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)


def repo_root() -> str:
    """The directory holding `instances/`.

    In this repo that is two levels above `projects/instance-cleanup`; in the generated
    handoff package `collector/` sits at the package root, so it is one level above. Both
    are found by looking for `instances/`, which is the thing actually being addressed.
    """
    here = os.path.abspath(os.path.dirname(__file__))

    # `pyproject.toml` marks the root of BOTH trees — one level above `collector/` in the
    # handoff package, three in this repo — so it is asked first. Looking for `instances/`
    # first was wrong twice over: it does not exist yet in a freshly unzipped package (it is
    # created on first run, which is what makes re-extracting safe), and where an ancestor
    # directory happens to have one, the search walks straight out of the package into it.
    #
    # The hardcoded `../../..` this replaced was right in this repo and three levels too high
    # inside the package, which put a stranger's discovery proposal in their home directory.
    # Found by ticket 12 building `dist/` for real; fixed in ticket 13.
    candidate = here
    for _ in range(5):
        candidate = os.path.dirname(candidate)
        if os.path.isfile(os.path.join(candidate, "pyproject.toml")):
            return candidate

    # No pyproject: an installed-not-editable tree. Fall back to the directory that already
    # holds state, then to the repo layout rather than guessing silently.
    candidate = here
    for _ in range(5):
        candidate = os.path.dirname(candidate)
        if os.path.isdir(os.path.join(candidate, "instances")):
            return candidate
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def load(instance_key: str, root: Optional[str] = None) -> Config:
    root = root or repo_root()
    sources = [DEFAULTS_PATH]
    with open(DEFAULTS_PATH, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    instance_path = os.path.join(root, "instances", instance_key, "config.yaml")
    if os.path.exists(instance_path):
        with open(instance_path, "r", encoding="utf-8") as handle:
            data = _deep_merge(data, yaml.safe_load(handle) or {})
        sources.append(instance_path)
    else:
        raise ConfigError(
            f"no config at {instance_path}. Config is authoritative for dataset ids — run "
            f"`python -m collector.discover --instance {instance_key}` and move the ids it "
            f"proposes into that file."
        )

    return Config(data, repo_root=root, instance_key=instance_key, sources=sources)
