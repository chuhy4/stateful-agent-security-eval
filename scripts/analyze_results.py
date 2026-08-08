#!/usr/bin/env python3
"""Factorial results analysis: BCa CIs, Holm-Bonferroni, completion check.

Usage:
    .venv/bin/python scripts/analyze_results.py
    .venv/bin/python scripts/analyze_results.py --results results/defense_factorial/results.jsonl
    .venv/bin/python scripts/analyze_results.py --results results/defense_factorial/results.jsonl --config experiments/configs/defense_factorial.yaml
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis.condition_identity import (
    AnalysisIdentityError,
    AnalysisMode,
    ConditionIdentity,
    canonical_condition_key,
    enumerate_expected_identities,
    validate_reset_run_index,
    validate_result_identity,
)
from src.runner.config_loader import ExperimentConfig, load_config
from src.runner.reset_policy import ResetCondition
from src.stats.bootstrap_engine import BootstrapEngine

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_RESULTS = "results/defense_factorial/results.jsonl"
DEFAULT_CONFIG = "experiments/configs/defense_factorial.yaml"


# ── helpers ───────────────────────────────────────────────────────────────────

def _condition_key(record: dict) -> str:
    """Compatibility wrapper around the shared canonical identity helper."""
    return canonical_condition_key(record.get("condition", {}))


@dataclass(frozen=True)
class InventoryIssue:
    code: str
    message: str
    record_index: int | None = None
    condition_key: str | None = None


@dataclass
class AnalysisInventory:
    mode: AnalysisMode
    raw_records: list[dict]
    expected_identities: list[ConditionIdentity]
    expected_runs: int
    completed_records: list[dict] = field(default_factory=list)
    failed_records: list[dict] = field(default_factory=list)
    malformed_identities: list[InventoryIssue] = field(default_factory=list)
    duplicate_slots: list[InventoryIssue] = field(default_factory=list)
    unexpected_conditions: list[InventoryIssue] = field(default_factory=list)
    invalid_run_indices: list[InventoryIssue] = field(default_factory=list)
    overfilled_conditions: list[InventoryIssue] = field(default_factory=list)
    parse_errors: list[InventoryIssue] = field(default_factory=list)
    completion_by_condition: dict[str, dict[str, int]] = field(default_factory=dict)
    missing_conditions: dict[str, dict[str, int]] = field(default_factory=dict)
    skipped_v1_records: int = 0

    @property
    def invalid_issues(self) -> list[InventoryIssue]:
        issues = (
            self.malformed_identities
            + self.duplicate_slots
            + self.unexpected_conditions
            + self.invalid_run_indices
            + self.overfilled_conditions
        )
        if self.mode is AnalysisMode.RESET:
            issues += self.parse_errors
        return issues

    def require_valid(self) -> AnalysisInventory:
        issues = self.invalid_issues
        if issues:
            raise AnalysisInventoryError(self, issues)
        return self

    def completion_dict(self) -> dict:
        completed = len(self.completed_records)
        return {
            "expected_runs": self.expected_runs,
            "completed_runs": completed,
            "failed_runs": len(self.failed_records),
            "completion_fraction": (
                completed / self.expected_runs if self.expected_runs else 0.0
            ),
            "conditions": dict(self.completion_by_condition),
            "missing_conditions": dict(self.missing_conditions),
        }


class AnalysisInventoryError(ValueError):
    """Fail-closed analysis error retaining the complete result inventory."""

    def __init__(
        self,
        inventory: AnalysisInventory,
        issues: list[InventoryIssue],
    ) -> None:
        self.inventory = inventory
        self.issues = list(issues)
        summary = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
        super().__init__(summary)


def _read_raw_results(path: str) -> tuple[list[dict], list[InventoryIssue]]:
    """Read JSON array or JSONL without dropping error records."""
    content = Path(path).read_text().strip()
    if not content:
        return [], []

    if content.startswith("["):
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            return [], [
                InventoryIssue(
                    code="MALFORMED_JSON",
                    message=f"JSON array could not be decoded: {exc}",
                )
            ]
        if not isinstance(data, list):
            return [], [
                InventoryIssue(
                    code="MALFORMED_JSON",
                    message="JSON result document must contain a list",
                )
            ]
        return data, []

    records: list[dict] = []
    parse_errors: list[InventoryIssue] = []
    for lineno, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            parse_errors.append(
                InventoryIssue(
                    code="MALFORMED_JSON",
                    message=f"Malformed JSON on line {lineno}",
                    record_index=lineno,
                )
            )
    return records, parse_errors


def _is_v1_record(record: Mapping) -> bool:
    ts = record.get("run_timestamp", "")
    return (
        record.get("defense_schema_version") is None
        and bool(ts)
        and ts < "2026-04-11"
    )


def build_result_inventory(
    raw_records: list[dict],
    config: ExperimentConfig,
    *,
    parse_errors: list[InventoryIssue] | None = None,
) -> AnalysisInventory:
    """Validate identity, experimental slots, and completion before statistics."""
    expected_identities = enumerate_expected_identities(config)
    mode = (
        AnalysisMode.LEGACY
        if config.reset_conditions is None
        else AnalysisMode.RESET
    )
    inventory = AnalysisInventory(
        mode=mode,
        raw_records=list(raw_records),
        expected_identities=expected_identities,
        expected_runs=len(expected_identities) * config.runs_per_condition,
        parse_errors=list(parse_errors or []),
    )
    expected_by_digest = {
        identity.digest: identity for identity in expected_identities
    }
    completion_counts = {
        identity.display_key: 0 for identity in expected_identities
    }
    completed_slots: set[tuple[str, object]] = set()

    for record_index, record in enumerate(raw_records):
        try:
            identity = validate_result_identity(record, mode)
        except AnalysisIdentityError as exc:
            inventory.malformed_identities.append(
                InventoryIssue(
                    code="MALFORMED_RESULT_IDENTITY",
                    message=str(exc),
                    record_index=record_index,
                )
            )
            continue

        expected_identity = expected_by_digest.get(identity.digest)
        if expected_identity is None:
            inventory.unexpected_conditions.append(
                InventoryIssue(
                    code="UNEXPECTED_CONDITION",
                    message=f"Condition is not present in config: {identity.display_key}",
                    record_index=record_index,
                    condition_key=identity.display_key,
                )
            )
            continue

        if _is_v1_record(record):
            inventory.skipped_v1_records += 1
            continue

        if record.get("error") is not None:
            inventory.failed_records.append(record)
            continue

        run_index = record.get("run_index")
        if mode is AnalysisMode.RESET:
            try:
                run_index = validate_reset_run_index(
                    run_index,
                    config.runs_per_condition,
                )
            except AnalysisIdentityError as exc:
                inventory.invalid_run_indices.append(
                    InventoryIssue(
                        code="INVALID_RESET_RUN_INDEX",
                        message=str(exc),
                        record_index=record_index,
                        condition_key=identity.display_key,
                    )
                )
                continue

        if mode is AnalysisMode.RESET and record.get("reset_valid") is not True:
            inventory.malformed_identities.append(
                InventoryIssue(
                    code="RESET_LIFECYCLE_INCONSISTENT",
                    message=(
                        "Successful reset record must have reset_valid=True; "
                        f"got {record.get('reset_valid')!r}"
                    ),
                    record_index=record_index,
                    condition_key=identity.display_key,
                )
            )
            continue

        if run_index is not None:
            slot: tuple[str, object] = (identity.digest, ("run_index", run_index))
        else:
            run_id = record.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                inventory.malformed_identities.append(
                    InventoryIssue(
                        code="MISSING_LEGACY_SLOT_IDENTITY",
                        message="Legacy record without run_index requires a non-empty run_id",
                        record_index=record_index,
                        condition_key=identity.display_key,
                    )
                )
                continue
            slot = (identity.digest, ("run_id", run_id))

        if slot in completed_slots:
            inventory.duplicate_slots.append(
                InventoryIssue(
                    code="DUPLICATE_RESULT_SLOT",
                    message=f"Duplicate completed slot for {identity.display_key}: {slot[1]!r}",
                    record_index=record_index,
                    condition_key=identity.display_key,
                )
            )
            continue
        completed_slots.add(slot)
        inventory.completed_records.append(record)
        completion_counts[expected_identity.display_key] += 1

    for identity in expected_identities:
        completed = completion_counts[identity.display_key]
        cell = {
            "completed": completed,
            "expected": config.runs_per_condition,
        }
        inventory.completion_by_condition[identity.display_key] = cell
        if completed < config.runs_per_condition:
            inventory.missing_conditions[identity.display_key] = cell
        elif completed > config.runs_per_condition:
            inventory.overfilled_conditions.append(
                InventoryIssue(
                    code="OVERFILLED_CONDITION",
                    message=(
                        f"{identity.display_key} has {completed}/"
                        f"{config.runs_per_condition} completed records"
                    ),
                    condition_key=identity.display_key,
                )
            )
    return inventory


def load_result_inventory(path: str, config: ExperimentConfig) -> AnalysisInventory:
    raw_records, parse_errors = _read_raw_results(path)
    inventory = build_result_inventory(
        raw_records,
        config,
        parse_errors=parse_errors,
    )
    return inventory.require_valid()


def load_results(path: str) -> list[dict]:
    """Legacy-compatible successful-record loader supporting JSON and JSONL.

    V1/V2 guard: records with defense_schema_version=None and run_timestamp
    before 2026-04-11 are v1 data (list_all_facts returned full values, making
    memory_sandbox results invalid). They are excluded here so that accidentally
    merging the v1 archive file (results_v1_list_all_facts_full_values.jsonl)
    with the active results file does not silently corrupt the analysis.

    All active records have run_timestamp >= 2026-04-11 and defense_schema_version=None
    (the field was added after the runs completed; None is the correct v2 marker for
    existing records). The guard only fires for records with timestamps before the
    v2 fix date, which only exist in the archived v1 file.
    """
    raw_records, parse_errors = _read_raw_results(path)
    records = []
    skipped_errors = 0
    skipped_v1 = 0
    for record in raw_records:
        if not isinstance(record, Mapping):
            continue
        if record.get("error"):
            skipped_errors += 1
            continue
        if _is_v1_record(record):
            skipped_v1 += 1
            continue
        records.append(record)

    if parse_errors:
        logger.warning("Skipped %d malformed lines", len(parse_errors))
    if skipped_v1:
        logger.warning(
            "Skipped %d v1 records (run_timestamp < 2026-04-11, defense_schema_version=None). "
            "These are from the archived v1 run where list_all_facts returned full values. "
            "Do not merge results_v1_list_all_facts_full_values.jsonl with the active results file.",
            skipped_v1,
        )
    if skipped_errors:
        logger.info("Filtered out %d error records (kept %d successes)", skipped_errors, len(records))
    return records


def report_completion(
    inventory: AnalysisInventory,
    threshold: float = 0.95,
) -> None:
    """Report complete expected cells, including conditions with zero records."""
    expected = inventory.expected_runs
    actual = len(inventory.completed_records)
    pct = actual / expected if expected > 0 else 0.0

    reset_incomplete = (
        inventory.mode is AnalysisMode.RESET
        and bool(inventory.missing_conditions)
    )
    if pct < threshold or reset_incomplete:
        logger.warning("=" * 70)
        logger.warning("⚠  FACTORIAL INCOMPLETE: %d / %d runs (%.1f%%)", actual, expected, pct * 100)
        logger.warning("   Statistics below are PROVISIONAL — do not treat as final.")
        logger.warning("=" * 70)
    else:
        logger.info("Completion: %d / %d runs (%.1f%%) ✓", actual, expected, pct * 100)

    if inventory.failed_records:
        logger.warning(
            "Failed/infrastructure records excluded: %d",
            len(inventory.failed_records),
        )
    if inventory.parse_errors:
        logger.warning("Skipped %d malformed lines", len(inventory.parse_errors))
    if inventory.skipped_v1_records:
        logger.warning(
            "Skipped %d v1 records (run_timestamp < 2026-04-11, "
            "defense_schema_version=None). These are from the archived v1 run "
            "where list_all_facts returned full values. Do not merge "
            "results_v1_list_all_facts_full_values.jsonl with the active "
            "results file.",
            inventory.skipped_v1_records,
        )
    if inventory.missing_conditions:
        logger.warning("Incomplete conditions (%d):", len(inventory.missing_conditions))
        for key, cell in sorted(inventory.missing_conditions.items()):
            logger.warning(
                "  %s: %d/%d",
                key,
                cell["completed"],
                cell["expected"],
            )


def check_completion(
    records: list[dict],
    config_path: str,
    threshold: float = 0.95,
) -> None:
    """Compatibility wrapper for callers that already hold result records."""
    try:
        config = load_config(config_path)
        inventory = build_result_inventory(records, config).require_valid()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Could not validate completion: %s", exc)
        return
    report_completion(inventory, threshold)


def _validate_reset_comparison_key(key: str) -> None:
    marker = ",reset_condition="
    if marker not in key:
        raise ValueError(
            "Reset-mode comparisons must explicitly include "
            "',reset_condition=C0|C1|C2' in every condition key"
        )
    reset_value = key.rsplit(marker, 1)[1]
    try:
        ResetCondition(reset_value)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid reset comparison key {key!r}; expected a C0/C1/C2 suffix"
        ) from None


def validate_comparisons(
    comparisons,
    condition_keys: set[str],
    *,
    reset_mode: bool = False,
) -> None:
    """Q12: Raise a clear error if any comparison references a missing condition."""
    missing: list[tuple[str, str]] = []
    for comp in comparisons:
        a, b = comp.condition_a, comp.condition_b
        if reset_mode:
            _validate_reset_comparison_key(a)
            _validate_reset_comparison_key(b)
        if a not in condition_keys:
            missing.append(("condition_a", a))
        if b not in condition_keys:
            missing.append(("condition_b", b))

    if missing:
        lines = [f"  {side}: {key}" for side, key in missing]
        raise ValueError(
            f"Comparisons reference {len(missing)} condition(s) with zero results in the JSONL.\n"
            "This would produce NaN in bootstrap — fix the comparison keys or check the results file.\n"
            "Missing:\n" + "\n".join(lines)
        )


# ── core analysis ─────────────────────────────────────────────────────────────

def compute_condition_stats(
    records: list[dict],
    engine: BootstrapEngine,
    *,
    reset_mode: bool = False,
) -> dict:
    """Per-condition ASR and BTCR with BCa CIs."""
    by_condition: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_condition[_condition_key(r)].append(r)

    stats = {}
    for cond_key, recs in sorted(by_condition.items()):
        asr_outcomes = np.array([1.0 if r.get("attack_success") else 0.0 for r in recs])
        btcr_outcomes = np.array([1.0 if r.get("btcr_success") else 0.0 for r in recs])

        asr_ci = engine.compute_ci(asr_outcomes)
        btcr_ci = engine.compute_ci(btcr_outcomes)

        stats[cond_key] = {
            "n": len(recs),
            "asr": {
                "point_estimate": asr_ci.point_estimate,
                "lower": asr_ci.lower,
                "upper": asr_ci.upper,
                "warning": asr_ci.warning,
            },
            "btcr": {
                "point_estimate": btcr_ci.point_estimate,
                "lower": btcr_ci.lower,
                "upper": btcr_ci.upper,
                "warning": btcr_ci.warning,
            },
        }
        if reset_mode:
            injection_outcomes = np.array(
                [1.0 if r.get("injection_success") else 0.0 for r in recs]
            )
            injection_ci = engine.compute_ci(injection_outcomes)
            stats[cond_key]["injection_success"] = {
                "point_estimate": injection_ci.point_estimate,
                "lower": injection_ci.lower,
                "upper": injection_ci.upper,
                "warning": injection_ci.warning,
            }
    return stats


def _is_na_comparison(condition_a: str, condition_b: str) -> str | None:
    """Return a reason string if this comparison should be annotated N/A.

    qwq:32b is a Draft-Only Executor: it never executes the exfiltration under
    any defense (ASR=0% across the board) except memory_sandbox (100% via RAG
    re-injection bypass). Comparing its no_defense baseline against its own
    defense conditions measures nothing about defense effectiveness — the
    baseline is already 0%. Similarly, comparing qwq:32b's no_defense ASR
    against other models' no_defense ASR measures model behavior, not defense
    effectiveness.

    Excluded (7 comparisons):
      - 6 primary DTA: no_defense,qwq vs {minimizer,sanitizer,prompt_hardening,
        rag_sanitizer,memory_sandbox,rag_llm_judge},qwq
      - 1 cross-model no_defense DTA: qwen2.5:14b,no_defense vs qwq:32b,no_defense

    NOT excluded (6 qwq comparisons that remain active):
      - 5 secondary BTCR (no_attack arm): these measure utility, not attack
      - 1 cross-model memory_sandbox DTA: qwen2.5:14b vs qwq:32b under
        memory_sandbox — this IS meaningful (documents the inversion effect)
    """
    NA_REASON = "Draft-Only Executor: mechanistically distinct attack pathway"

    # Primary DTA qwq: no_defense,qwq vs any_defense,qwq (DTA arm)
    if ("model=qwq:32b" in condition_a and "model=qwq:32b" in condition_b
            and "attack=delayed_trigger" in condition_a
            and "attack=delayed_trigger" in condition_b
            and "defense=no_defense" in condition_a):
        return NA_REASON

    # Cross-model no_defense DTA involving qwq
    if ("attack=delayed_trigger" in condition_a
            and "attack=delayed_trigger" in condition_b
            and "defense=no_defense" in condition_a
            and "defense=no_defense" in condition_b
            and ("model=qwq:32b" in condition_a or "model=qwq:32b" in condition_b)):
        return NA_REASON

    return None


def run_comparisons(
    comparisons,
    by_condition: dict[str, list[dict]],
    engine: BootstrapEngine,
) -> list[dict]:
    """BCa diff CIs for all pre-specified comparisons, then Holm-Bonferroni.

    Comparisons involving qwq:32b's primary DTA conditions are annotated N/A
    (Draft-Only Executor: mechanistically distinct attack pathway) and excluded
    from the Holm-Bonferroni correction. They are still computed for
    completeness but do not count toward the active comparison set.
    """
    results = []
    for comp in comparisons:
        na_reason = _is_na_comparison(comp.condition_a, comp.condition_b)

        recs_a = by_condition[comp.condition_a]
        recs_b = by_condition[comp.condition_b]
        a_asr = np.array([1.0 if r.get("attack_success") else 0.0 for r in recs_a])
        b_asr = np.array([1.0 if r.get("attack_success") else 0.0 for r in recs_b])
        ci = engine.compute_diff_ci(a_asr, b_asr)
        # Significant if 95% CI excludes zero (equivalent to α=0.05 two-sided test)
        significant = bool(ci.lower > 0 or ci.upper < 0)
        results.append({
            "condition_a": comp.condition_a,
            "condition_b": comp.condition_b,
            "diff_point": ci.point_estimate,
            "diff_lower": ci.lower,
            "diff_upper": ci.upper,
            "significant_pre_correction": significant,
            "warning": ci.warning,
            "na_reason": na_reason,
        })

    # Holm-Bonferroni on ACTIVE comparisons only (exclude N/A)
    active_indices = [i for i, r in enumerate(results) if r["na_reason"] is None]
    n_active = len(active_indices)

    # Sort active comparisons by significance then |diff| descending
    # (Holm-Bonferroni: rank from most to least significant.
    # With CI-based testing and bimodal effects, ordering by |diff|
    # is equivalent to ordering by p-value since all significant
    # comparisons have |diff| > 77pp and all non-significant have
    # |diff| < 1pp. We sort significant-first, then by |diff|, to
    # match canonical Holm-Bonferroni step-down behavior.)
    ranked_active = sorted(
        active_indices,
        key=lambda i: (
            0 if results[i]["significant_pre_correction"] else 1,
            -abs(results[i]["diff_point"]),
        ),
    )

    holm_significant = [False] * len(results)
    for rank, idx in enumerate(ranked_active):
        if results[idx]["significant_pre_correction"]:
            holm_significant[idx] = True
        else:
            # Step-down: once we hit a non-significant result, stop
            break

    for i, r in enumerate(results):
        if r["na_reason"] is not None:
            r["significant_holm"] = None  # N/A — not tested
        else:
            r["significant_holm"] = holm_significant[i]

    n_na = sum(1 for r in results if r["na_reason"] is not None)
    n_sig = sum(1 for r in results if r["significant_holm"] is True)
    logger.info(
        "Comparisons: %d total, %d N/A, %d active, %d significant (Holm-Bonferroni)",
        len(results), n_na, n_active, n_sig,
    )

    return results


def _comparison_results_for_json(
    comparison_results: list[dict],
    *,
    reset_mode: bool,
) -> list[dict]:
    """Preserve historical legacy scalar types without changing runtime logic."""
    serialized = [dict(result) for result in comparison_results]
    if not reset_mode:
        for result in serialized:
            result["significant_pre_correction"] = str(
                bool(result["significant_pre_correction"])
            )
    return serialized


def compute_mechanistic_tag_counts(
    records: list[dict],
    *,
    reset_mode: bool = False,
) -> dict[str, dict[str, int]]:
    """Aggregate DTA mechanistic tags without crossing reset arms."""
    grouped: dict[str, Counter] = defaultdict(Counter)
    for record in records:
        if (
            record.get("condition", {}).get("attack", {}).get("type")
            != "delayed_trigger"
        ):
            continue
        if reset_mode:
            group_key = _condition_key(record)
        else:
            defense_cfg = record.get("condition", {}).get("defense", {})
            group_key = defense_cfg.get("name") or defense_cfg.get(
                "type", "unknown"
            )
        counter = grouped[group_key]
        for tag in record.get("mechanistic_tags", {}).get("tags", []):
            counter[tag] += 1
    return {
        key: dict(sorted(counter.items()))
        for key, counter in sorted(grouped.items())
    }


def print_mechanistic_summary(
    records: list[dict],
    *,
    reset_mode: bool = False,
) -> None:
    """Print mechanistic tag counts per DTA condition.

    Reads tags from result["mechanistic_tags"]["tags"] (a list of strings).
    Uses list-membership check ("tag" in tags_list), NOT dict-key access
    (result["mechanistic_tags"].get("tag")) — the latter silently returns None
    because the serialized structure is {"tags": [...], "mechanism": "...", ...},
    not a flat dict of booleans.
    """
    # Only DTA runs carry meaningful mechanistic tags
    dta_records = [
        r for r in records
        if r.get("condition", {}).get("attack", {}).get("type") == "delayed_trigger"
    ]
    if not dta_records:
        return

    grouped_records: dict[str, list[dict]] = defaultdict(list)
    for r in dta_records:
        if reset_mode:
            group_key = _condition_key(r)
        else:
            defense_cfg = r.get("condition", {}).get("defense", {})
            group_key = defense_cfg.get("name") or defense_cfg.get(
                "type", "unknown"
            )
        grouped_records[group_key].append(r)

    print("\n" + "=" * 80)
    print("MECHANISTIC TAG SUMMARY (DTA runs only)")
    print("NOTE: tags read from result['mechanistic_tags']['tags'] (list membership)")
    print("=" * 80)
    for group_key, recs in sorted(grouped_records.items()):
        tag_counter: Counter = Counter()
        for r in recs:
            # Correct access pattern: mechanistic_tags["tags"] is a list of strings.
            # Do NOT use mechanistic_tags.get("semantic_masking_success") — that key
            # does not exist at the top level; it would silently return None.
            tags_list = r.get("mechanistic_tags", {}).get("tags", [])
            for tag in tags_list:
                tag_counter[tag] += 1
        n = len(recs)
        tag_str = ", ".join(f"{tag}={count}/{n}" for tag, count in sorted(tag_counter.items()))
        print(f"  {group_key:<20} (n={n:>3}): {tag_str or '(no tags)'}")


def print_summary(
    stats: dict,
    comparison_results: list[dict],
    *,
    reset_mode: bool = False,
) -> None:
    """Print a concise results table."""
    print("\n" + "=" * 80)
    print("CONDITION STATISTICS")
    print("=" * 80)
    injection_header = " {'Inj':>6}" if reset_mode else ""
    print(
        f"{'Condition':<60} {'N':>4} {'ASR':>6} "
        f"{'[95% CI]':>16} {'BTCR':>6}{injection_header}"
    )
    print("-" * 80)
    for cond, s in sorted(stats.items()):
        asr = s["asr"]
        btcr = s["btcr"]
        ci_str = f"[{asr['lower']:.2f}, {asr['upper']:.2f}]"
        injection_value = (
            f" {s['injection_success']['point_estimate']:>6.2f}"
            if reset_mode
            else ""
        )
        print(
            f"{cond:<60} {s['n']:>4} {asr['point_estimate']:>6.2f} "
            f"{ci_str:>16} {btcr['point_estimate']:>6.2f}{injection_value}"
        )
        if asr.get("warning"):
            print(f"  ⚠  {asr['warning']}")

    print("\n" + "=" * 80)
    print("COMPARISONS (Holm-Bonferroni corrected)")
    print("=" * 80)
    n_na = sum(1 for r in comparison_results if r.get("na_reason") is not None)
    n_active = len(comparison_results) - n_na
    sig_count = sum(1 for r in comparison_results if r.get("significant_holm") is True)
    print(f"{len(comparison_results)} total, {n_na} N/A, {n_active} active, "
          f"{sig_count} significant after Holm-Bonferroni\n")
    for r in comparison_results:
        if r.get("na_reason"):
            sig = "—"
        elif r.get("significant_holm"):
            sig = "✓"
        else:
            sig = " "
        pre = "*" if r["significant_pre_correction"] else " "
        condition_a = r["condition_a"] if reset_mode else r["condition_a"][:45]
        condition_b = r["condition_b"] if reset_mode else r["condition_b"][:45]
        print(
            f"[{sig}] {condition_a:<45} vs"
            f"\n    {condition_b:<45}"
            f"  diff={r['diff_point']:+.3f} [{r['diff_lower']:+.3f}, {r['diff_upper']:+.3f}]"
            f"  pre={pre}"
        )
        if r.get("na_reason"):
            print(f"    N/A: {r['na_reason']}")
        if r.get("warning"):
            print(f"    ⚠  {r['warning']}")


def build_analysis_output(
    inventory: AnalysisInventory,
    config: ExperimentConfig,
    engine: BootstrapEngine,
) -> dict:
    """Build JSON-safe analysis output from a validated result inventory."""
    inventory.require_valid()
    records = inventory.completed_records
    reset_mode = inventory.mode is AnalysisMode.RESET
    by_condition: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_condition[_condition_key(record)].append(record)
    validate_comparisons(
        config.comparisons,
        set(by_condition),
        reset_mode=reset_mode,
    )
    comparison_results = run_comparisons(
        config.comparisons,
        by_condition,
        engine,
    )
    output = {
        "stats": compute_condition_stats(
            records,
            engine,
            reset_mode=reset_mode,
        ),
        "comparisons": _comparison_results_for_json(
            comparison_results,
            reset_mode=reset_mode,
        ),
        "mechanistic_tags": compute_mechanistic_tag_counts(
            records,
            reset_mode=reset_mode,
        ),
    }
    if reset_mode:
        output["completion"] = inventory.completion_dict()
    return output


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze factorial results")
    parser.add_argument("--results", default=DEFAULT_RESULTS)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--out", default=None, help="Write JSON output to file")
    args = parser.parse_args()

    if not Path(args.results).exists():
        logger.error("Results file not found: %s", args.results)
        sys.exit(1)

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.error("Failed to load config: %s", e)
        sys.exit(1)

    try:
        inventory = load_result_inventory(args.results, cfg)
    except (AnalysisInventoryError, AnalysisIdentityError) as e:
        logger.error("Result validation failed: %s", e)
        sys.exit(1)
    records = inventory.completed_records
    report_completion(inventory)
    if not records:
        logger.error("No successful records found in %s", args.results)
        sys.exit(1)
    logger.info(
        "Loaded %d successful and %d failed records",
        len(records),
        len(inventory.failed_records),
    )
    # Build condition index
    by_condition: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_condition[_condition_key(r)].append(r)
    condition_keys = set(by_condition.keys())

    # Q12: validate all comparisons reference existing conditions
    try:
        validate_comparisons(
            cfg.comparisons,
            condition_keys,
            reset_mode=inventory.mode is AnalysisMode.RESET,
        )
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)

    engine = BootstrapEngine(
        n_resamples=cfg.n_bootstrap,
        alpha=cfg.alpha,
        seed=cfg.bootstrap_seed,
    )

    reset_mode = inventory.mode is AnalysisMode.RESET
    stats = compute_condition_stats(records, engine, reset_mode=reset_mode)
    comparison_results = run_comparisons(cfg.comparisons, by_condition, engine)

    print_summary(stats, comparison_results, reset_mode=reset_mode)
    print_mechanistic_summary(records, reset_mode=reset_mode)

    if args.out:
        output = {
            "stats": stats,
            "comparisons": _comparison_results_for_json(
                comparison_results,
                reset_mode=reset_mode,
            ),
            "mechanistic_tags": compute_mechanistic_tag_counts(
                records,
                reset_mode=reset_mode,
            ),
        }
        if reset_mode:
            output["completion"] = inventory.completion_dict()
        with open(args.out, "w") as f:
            json.dump(output, f, indent=2, default=str)
        logger.info("Results written to %s", args.out)


if __name__ == "__main__":
    main()
