"""Deterministic lineage workload comparison; never a request budget or latency diagnosis."""
from __future__ import annotations

import math


def _count(document):
    coverage = document.get("coverage") or {}
    block = coverage.get("consumption") or {}
    ids = block.get("contested")
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        return None, "contested dataset ids unavailable"
    count = len(set(ids))
    if block.get("signal") != "present" or not isinstance(block.get("counts"), dict):
        return count, "consumption coverage unavailable"
    if (block.get("publications") or {}).get("signal") != "present":
        return count, "publication coverage unavailable"
    adjudication = block.get("adjudication") or {}
    if block.get("unreachable") or adjudication.get("unknown", 0):
        return count, "lineage coverage incomplete"
    if count and adjudication.get("adjudicated") != count:
        return count, "adjudication incomplete"
    reports = coverage.get("reports") or []
    # The condition needs governance linkage as well as the product enumeration.
    checked = {report.get("report"): report.get("ok") for report in reports}
    if any(checked.get(key) is not True for key in ("cards_and_datasets", "datasets", "dataflow_details")):
        return count, "governance coverage unavailable"
    return count, None


def compare(current, prior, growth_pct=25):
    threshold = float(growth_pct)
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("adjudication growth percentage must be finite and greater than zero")
    count, error = _count(current)
    previous, prior_error = _count(prior) if prior is not None else (None, None)
    result = dict(status="unavailable", reason=None, current_count=count,
                  prior_count=previous, prior_run_id=(prior or {}).get("run", {}).get("run_id"),
                  growth_pct=None, threshold_pct=threshold, warned=False)
    if prior is None:
        error = "no prior run; baseline established" if error is None else "current run: " + error
    else:
        current_run, prior_run = current.get("run", {}), prior.get("run", {})
        if error:
            error = "current run: " + error
        elif prior_error:
            error = "prior run: " + prior_error
        else:
            for key in ("instance", "collector_version", "config_version"):
                if not current_run.get(key) or current_run.get(key) != prior_run.get(key):
                    error = key + " missing or changed"
                    break
    if error:
        result["reason"] = error
        return result
    result["status"] = "comparable"
    if previous == 0:
        result["warned"] = count > 0
    else:
        result["growth_pct"] = (count - previous) * 100.0 / previous
        result["warned"] = count > previous and (count - previous) * 100 >= previous * threshold
    return result


def describe(result):
    if not result:
        return "comparison unavailable: not recorded in this older findings file"
    if result["status"] != "comparable":
        return "comparison unavailable: " + result["reason"]
    before, after = result["prior_count"], result["current_count"]
    change = ("workload increased from zero" if after else "unchanged at zero") if before == 0 else f"{result['growth_pct']:+.1f}%"
    prefix = "WARNING: " if result["warned"] else ""
    return (f"{prefix}{before} -> {after} contested dataset(s), {change}; "
            f"growth warning at {result['threshold_pct']:g}%")
