"""Reset-aware result identity, inventory, and analysis regressions."""
from __future__ import annotations

import json
import sys

import pytest

from scripts.analyze_results import (
    AnalysisInventoryError,
    build_analysis_output,
    build_result_inventory,
    compute_condition_stats,
    compute_mechanistic_tag_counts,
    load_result_inventory,
    load_results,
    main,
    print_summary,
    report_completion,
    validate_comparisons,
)
from src.analysis.condition_identity import (
    AnalysisIdentityError,
    AnalysisMode,
    canonical_condition_key,
    enumerate_expected_identities,
    stable_condition_digest,
    validate_result_identity,
)
from src.runner.config_loader import ComparisonSpec, ExperimentConfig
from src.runner.runner import ExperimentRunner
from src.stats.bootstrap_engine import BootstrapEngine


def _condition(reset_condition: str | None = None, *, model: str = "qwen3:8b"):
    condition = {
        "attack": {"type": "no_attack", "name": "control"},
        "defense": {"type": "none", "name": "no_defense"},
        "model": {"provider": "ollama", "model_name": model},
    }
    if reset_condition is not None:
        condition["reset_condition"] = reset_condition
    return condition


def _key(reset_condition: str | None = None, *, model: str = "qwen3:8b"):
    return canonical_condition_key(_condition(reset_condition, model=model))


def _config(
    *,
    reset_conditions: list[str] | None = None,
    runs: int = 1,
    comparisons: list[ComparisonSpec] | None = None,
) -> ExperimentConfig:
    if comparisons is None:
        key = _key(reset_conditions[0] if reset_conditions else None)
        comparisons = [ComparisonSpec(key, key)]
    return ExperimentConfig(
        attacks=[{"type": "no_attack", "name": "control"}],
        defenses=[{"type": "none", "name": "no_defense"}],
        models=[{"provider": "ollama", "model_name": "qwen3:8b"}],
        runs_per_condition=runs,
        comparisons=comparisons,
        reset_conditions=reset_conditions,
    )


def _record(
    reset_condition: str | None = None,
    *,
    run_index: int | None = 0,
    run_id: str = "run-0",
    attack_success: bool = False,
    btcr_success: bool = True,
    injection_success: bool = False,
    reset_valid: bool | None = True,
    error: str | None = None,
    tags: list[str] | None = None,
):
    record = {
        "run_id": run_id,
        "condition": _condition(reset_condition),
        "attack_success": attack_success,
        "btcr_success": btcr_success,
        "injection_success": injection_success,
        "error": error,
        "run_index": run_index,
        "mechanistic_tags": {"tags": list(tags or [])},
    }
    if reset_condition is not None:
        record["reset_condition"] = reset_condition
        record["reset_valid"] = reset_valid
    return record


def _engine() -> BootstrapEngine:
    return BootstrapEngine(n_resamples=50, alpha=0.05, seed=7)


def _issue_codes(exc: AnalysisInventoryError) -> set[str]:
    return {issue.code for issue in exc.issues}


def test_canonical_identity_distinguishes_reset_arms_and_preserves_legacy_key():
    legacy = "attack=no_attack,defense=no_defense,model=qwen3:8b"
    assert canonical_condition_key(_condition()) == legacy

    conditions = [_condition(value) for value in ("C0", "C1", "C2")]
    assert {canonical_condition_key(condition) for condition in conditions} == {
        f"{legacy},reset_condition=C0",
        f"{legacy},reset_condition=C1",
        f"{legacy},reset_condition=C2",
    }
    assert len({stable_condition_digest(condition) for condition in conditions}) == 3


def test_analysis_digest_matches_serial_runner_condition_hash(tmp_path):
    config = _config(reset_conditions=["C0"])
    config.db_base_dir = str(tmp_path / "db")
    runner = ExperimentRunner(config)
    condition = runner._enumerate_conditions()[0]

    assert runner._get_condition_id(condition) == stable_condition_digest(
        condition
    )[:16]


def test_result_identity_rejects_invalid_and_mismatched_reset_values():
    invalid = _record("C0")
    invalid["condition"]["reset_condition"] = "C9"
    with pytest.raises(AnalysisIdentityError, match="invalid reset_condition"):
        validate_result_identity(invalid, AnalysisMode.RESET)

    mismatch = _record("C0")
    mismatch["reset_condition"] = "C1"
    with pytest.raises(AnalysisIdentityError, match="mismatch"):
        validate_result_identity(mismatch, AnalysisMode.RESET)


def test_reset_display_key_collision_fails_closed():
    config = _config(reset_conditions=["C0"])
    config.attacks = [
        {"type": "no_attack", "variant": "a"},
        {"type": "no_attack", "variant": "b"},
    ]
    with pytest.raises(AnalysisIdentityError, match="RESET_DISPLAY_KEY_COLLISION"):
        enumerate_expected_identities(config)


@pytest.mark.parametrize("file_format", ["json", "jsonl"])
def test_legacy_json_and_jsonl_load_with_null_top_level_reset(
    tmp_path,
    file_format,
):
    record = _record(None, run_index=None)
    record["reset_condition"] = None
    path = tmp_path / f"legacy.{file_format}"
    if file_format == "json":
        path.write_text(json.dumps([record]))
    else:
        path.write_text(json.dumps(record) + "\n")

    assert load_results(str(path)) == [record]
    inventory = load_result_inventory(str(path), _config())
    assert inventory.completed_records == [record]


@pytest.mark.parametrize(
    "config",
    [_config(reset_conditions=["C0"]), _config()],
    ids=["reset-config", "legacy-config"],
)
def test_mixed_legacy_and_reset_input_is_rejected(config):
    with pytest.raises(AnalysisInventoryError) as exc_info:
        build_result_inventory([_record("C0"), _record(None)], config).require_valid()
    assert "MALFORMED_RESULT_IDENTITY" in _issue_codes(exc_info.value)


def test_reset_malformed_identity_is_rejected():
    record = _record("C0")
    record.pop("reset_condition")
    with pytest.raises(AnalysisInventoryError) as exc_info:
        build_result_inventory(
            [record],
            _config(reset_conditions=["C0"]),
        ).require_valid()
    assert "MALFORMED_RESULT_IDENTITY" in _issue_codes(exc_info.value)


@pytest.mark.parametrize("reset_valid", [False, None])
def test_successful_reset_record_requires_reset_valid_true(reset_valid):
    with pytest.raises(AnalysisInventoryError) as exc_info:
        build_result_inventory(
            [_record("C0", reset_valid=reset_valid)],
            _config(reset_conditions=["C0"]),
        ).require_valid()
    assert "RESET_LIFECYCLE_INCONSISTENT" in _issue_codes(exc_info.value)


def test_reset_error_record_is_retained_but_excluded_from_completion_and_stats():
    record = _record("C0", reset_valid=None, error="infrastructure failure")
    inventory = build_result_inventory(
        [record],
        _config(reset_conditions=["C0"]),
    ).require_valid()

    assert inventory.failed_records == [record]
    assert inventory.completed_records == []
    assert inventory.completion_by_condition[_key("C0")] == {
        "completed": 0,
        "expected": 1,
    }
    assert compute_condition_stats([], _engine(), reset_mode=True) == {}


def test_failed_attempt_does_not_duplicate_later_successful_slot():
    failed = _record("C0", reset_valid=None, error="provider failure")
    succeeded = _record("C0", reset_valid=True, error=None)
    inventory = build_result_inventory(
        [failed, succeeded],
        _config(reset_conditions=["C0"]),
    ).require_valid()
    assert inventory.failed_records == [failed]
    assert inventory.completed_records == [succeeded]
    assert inventory.duplicate_slots == []


def test_failed_history_never_claims_or_duplicates_a_successful_slot():
    failed_without_slot = _record(
        "C0",
        run_index=None,
        run_id="failed-without-slot",
        reset_valid=None,
        error="provider failure",
    )
    failed_with_slot = _record(
        "C0",
        run_index=0,
        run_id="failed-with-slot",
        reset_valid=None,
        error="provider failure",
    )
    succeeded = _record("C0", run_index=0, run_id="succeeded")

    inventory = build_result_inventory(
        [failed_without_slot, failed_with_slot, succeeded],
        _config(reset_conditions=["C0"]),
    ).require_valid()

    assert inventory.failed_records == [failed_without_slot, failed_with_slot]
    assert inventory.completed_records == [succeeded]
    assert inventory.duplicate_slots == []
    assert inventory.completion_by_condition[_key("C0")] == {
        "completed": 1,
        "expected": 1,
    }


def test_reset_completion_includes_factor_and_reports_missing_c2():
    inventory = build_result_inventory(
        [_record("C0"), _record("C1")],
        _config(reset_conditions=["C0", "C1", "C2"]),
    ).require_valid()

    assert inventory.expected_runs == 3
    assert inventory.completion_by_condition[_key("C2")] == {
        "completed": 0,
        "expected": 1,
    }
    assert inventory.missing_conditions[_key("C2")]["completed"] == 0


def test_duplicate_completed_slot_is_rejected():
    record = _record("C0")
    with pytest.raises(AnalysisInventoryError) as exc_info:
        build_result_inventory(
            [record, dict(record)],
            _config(reset_conditions=["C0"]),
        ).require_valid()
    assert "DUPLICATE_RESULT_SLOT" in _issue_codes(exc_info.value)


def test_unexpected_reset_condition_is_rejected():
    with pytest.raises(AnalysisInventoryError) as exc_info:
        build_result_inventory(
            [_record("C1")],
            _config(reset_conditions=["C0"]),
        ).require_valid()
    assert "UNEXPECTED_CONDITION" in _issue_codes(exc_info.value)


@pytest.mark.parametrize("run_index", [None, -1, 1, True, "0"])
def test_invalid_reset_run_index_is_rejected(run_index):
    with pytest.raises(AnalysisInventoryError) as exc_info:
        build_result_inventory(
            [_record("C0", run_index=run_index)],
            _config(reset_conditions=["C0"]),
        ).require_valid()
    assert "INVALID_RESET_RUN_INDEX" in _issue_codes(exc_info.value)


def test_overfilled_legacy_condition_is_rejected():
    records = [
        _record(None, run_index=None, run_id="one"),
        _record(None, run_index=None, run_id="two"),
    ]
    with pytest.raises(AnalysisInventoryError) as exc_info:
        build_result_inventory(records, _config(runs=1)).require_valid()
    assert "OVERFILLED_CONDITION" in _issue_codes(exc_info.value)


def test_legacy_without_run_index_falls_back_to_run_id():
    records = [
        _record(None, run_index=None, run_id="one"),
        _record(None, run_index=None, run_id="two"),
    ]
    inventory = build_result_inventory(records, _config(runs=2)).require_valid()
    assert inventory.completed_records == records


def test_reset_statistics_and_false_positives_are_separate_by_arm():
    records = [
        _record("C0", attack_success=False, btcr_success=True, injection_success=False),
        _record("C1", attack_success=True, btcr_success=False, injection_success=True),
        _record("C2", attack_success=False, btcr_success=False, injection_success=True),
    ]
    stats = compute_condition_stats(records, _engine(), reset_mode=True)

    assert len(stats) == 3
    assert stats[_key("C0")]["asr"]["point_estimate"] == 0.0
    assert stats[_key("C1")]["asr"]["point_estimate"] == 1.0
    assert stats[_key("C0")]["btcr"]["point_estimate"] == 1.0
    assert stats[_key("C2")]["btcr"]["point_estimate"] == 0.0
    assert stats[_key("C0")]["injection_success"]["point_estimate"] == 0.0
    assert stats[_key("C1")]["injection_success"]["point_estimate"] == 1.0


def test_reset_mechanistic_tags_do_not_merge_arms():
    c0 = _record("C0", tags=["tag_c0"])
    c1 = _record("C1", tags=["tag_c1"])
    for record in (c0, c1):
        record["condition"]["attack"]["type"] = "delayed_trigger"
    counts = compute_mechanistic_tag_counts([c0, c1], reset_mode=True)

    assert counts[canonical_condition_key(c0["condition"])] == {"tag_c0": 1}
    assert counts[canonical_condition_key(c1["condition"])] == {"tag_c1": 1}


def test_reset_mechanistic_tags_separate_model_defense_and_reset_dimensions():
    records = []
    expected = {}
    for model in ("qwen3:8b", "llama3.1:8b"):
        for defense in ("no_defense", "prompt_hardening"):
            for reset_condition in ("C0", "C2"):
                tag = f"tag-{model}-{defense}-{reset_condition}"
                record = _record(reset_condition, tags=[tag])
                record["condition"]["attack"]["type"] = "delayed_trigger"
                record["condition"]["model"]["model_name"] = model
                record["condition"]["defense"] = {
                    "type": "none" if defense == "no_defense" else defense,
                    "name": defense,
                }
                records.append(record)
                expected[canonical_condition_key(record["condition"])] = {tag: 1}

    assert compute_mechanistic_tag_counts(records, reset_mode=True) == expected


def test_reset_comparison_requires_suffix_and_explicit_c1_stays_c1():
    legacy_key = _key(None)
    with pytest.raises(ValueError, match="explicitly include"):
        validate_comparisons(
            [ComparisonSpec(legacy_key, legacy_key)],
            {_key("C0")},
            reset_mode=True,
        )

    c1 = _key("C1")
    comparisons = [ComparisonSpec(c1, c1)]
    validate_comparisons(comparisons, {c1}, reset_mode=True)
    assert comparisons == [ComparisonSpec(c1, c1)]


def test_reset_comparison_labels_are_not_truncated(capsys):
    key = _key("C2")
    stats = {
        key: {
            "n": 1,
            "asr": {"point_estimate": 0.0, "lower": 0.0, "upper": 0.0},
            "btcr": {"point_estimate": 1.0, "lower": 1.0, "upper": 1.0},
            "injection_success": {
                "point_estimate": 0.0,
                "lower": 0.0,
                "upper": 0.0,
            },
        }
    }
    comparison = {
        "condition_a": key,
        "condition_b": key,
        "diff_point": 0.0,
        "diff_lower": 0.0,
        "diff_upper": 0.0,
        "significant_pre_correction": False,
        "significant_holm": False,
        "warning": None,
        "na_reason": None,
    }
    print_summary(stats, [comparison], reset_mode=True)
    assert key in capsys.readouterr().out


def test_legacy_analysis_output_shape_and_grouping_are_unchanged():
    record = _record(None, run_index=None)
    record["condition"]["attack"]["type"] = "delayed_trigger"
    record["mechanistic_tags"] = {"tags": []}
    config = _config(
        comparisons=[
            ComparisonSpec(
                "attack=delayed_trigger,defense=no_defense,model=qwen3:8b",
                "attack=delayed_trigger,defense=no_defense,model=qwen3:8b",
            )
        ]
    )
    config.attacks = [{"type": "delayed_trigger", "name": "control"}]
    inventory = build_result_inventory([record], config).require_valid()
    output = build_analysis_output(inventory, config, _engine())

    assert set(output) == {"stats", "comparisons", "mechanistic_tags"}
    stat = next(iter(output["stats"].values()))
    assert set(stat) == {"n", "asr", "btcr"}
    assert output["mechanistic_tags"] == {"no_defense": {}}
    assert output["comparisons"][0]["significant_pre_correction"] == "False"
    assert isinstance(
        output["comparisons"][0]["significant_pre_correction"],
        str,
    )


def test_reset_incomplete_report_is_provisional_above_legacy_threshold(caplog):
    records = []
    for reset_condition, stop in (("C0", 20), ("C1", 19)):
        for run_index in range(stop):
            records.append(
                _record(
                    reset_condition,
                    run_index=run_index,
                    run_id=f"{reset_condition}-{run_index}",
                )
            )
    inventory = build_result_inventory(
        records,
        _config(reset_conditions=["C0", "C1"], runs=20),
    ).require_valid()

    report_completion(inventory)

    assert "FACTORIAL INCOMPLETE: 39 / 40 runs (97.5%)" in caplog.text
    assert "PROVISIONAL" in caplog.text
    assert "Completion: 39 / 40 runs (97.5%) ✓" not in caplog.text


def _run_analysis_cli(monkeypatch, path, config):
    monkeypatch.setattr("scripts.analyze_results.load_config", lambda _: config)
    monkeypatch.setattr(
        sys,
        "argv",
        ["analyze_results.py", "--results", str(path), "--config", "unused"],
    )
    main()


def test_cli_reports_completely_missing_reset_arm_as_provisional(
    tmp_path,
    monkeypatch,
    caplog,
):
    config = _config(reset_conditions=["C0", "C1", "C2"])
    config.n_bootstrap = 20
    path = tmp_path / "missing-arm.jsonl"
    path.write_text(
        "".join(json.dumps(_record(value)) + "\n" for value in ("C0", "C1"))
    )

    _run_analysis_cli(monkeypatch, path, config)

    assert "FACTORIAL INCOMPLETE: 2 / 3 runs" in caplog.text
    assert f"{_key('C2')}: 0/1" in caplog.text
    assert "PROVISIONAL" in caplog.text


def test_cli_reports_malformed_legacy_jsonl_row(tmp_path, monkeypatch, caplog):
    config = _config()
    config.n_bootstrap = 20
    path = tmp_path / "malformed-legacy.jsonl"
    path.write_text(json.dumps(_record(None, run_index=None)) + "\n{broken\n")

    _run_analysis_cli(monkeypatch, path, config)

    assert "Skipped 1 malformed lines" in caplog.text


def test_cli_reports_skipped_v1_legacy_record(
    tmp_path,
    monkeypatch,
    caplog,
):
    record = _record(None, run_index=None)
    record["run_timestamp"] = "2026-04-10T12:00:00"
    record["defense_schema_version"] = None
    path = tmp_path / "v1.jsonl"
    path.write_text(json.dumps(record) + "\n")

    with pytest.raises(SystemExit):
        _run_analysis_cli(monkeypatch, path, _config())

    assert "Skipped 1 v1 records" in caplog.text
    assert "FACTORIAL INCOMPLETE: 0 / 1 runs" in caplog.text


def test_cli_reset_malformed_row_fails_closed(
    tmp_path,
    monkeypatch,
    caplog,
):
    path = tmp_path / "malformed-reset.jsonl"
    path.write_text("{broken\n")

    with pytest.raises(SystemExit):
        _run_analysis_cli(
            monkeypatch,
            path,
            _config(reset_conditions=["C0"]),
        )

    assert "Result validation failed" in caplog.text
    assert "MALFORMED_JSON" in caplog.text


def test_cli_empty_reset_file_reports_inventory_before_stopping(
    tmp_path,
    monkeypatch,
    caplog,
):
    path = tmp_path / "empty-reset.jsonl"
    path.write_text("")

    with pytest.raises(SystemExit):
        _run_analysis_cli(
            monkeypatch,
            path,
            _config(reset_conditions=["C0"]),
        )

    assert "FACTORIAL INCOMPLETE: 0 / 1 runs" in caplog.text
    assert f"{_key('C0')}: 0/1" in caplog.text
    assert "No successful records found" in caplog.text


def test_cli_all_error_reset_file_reports_inventory_before_stopping(
    tmp_path,
    monkeypatch,
    caplog,
):
    path = tmp_path / "all-error-reset.jsonl"
    path.write_text(
        json.dumps(
            _record(
                "C0",
                reset_valid=None,
                error="infrastructure failure",
            )
        )
        + "\n"
    )

    with pytest.raises(SystemExit):
        _run_analysis_cli(
            monkeypatch,
            path,
            _config(reset_conditions=["C0"]),
        )

    assert "FACTORIAL INCOMPLETE: 0 / 1 runs" in caplog.text
    assert "Failed/infrastructure records excluded: 1" in caplog.text
    assert f"{_key('C0')}: 0/1" in caplog.text


def test_synthetic_reset_analysis_path_round_trips_json(tmp_path):
    comparisons = [ComparisonSpec(_key("C0"), _key("C1"))]
    config = _config(
        reset_conditions=["C0", "C1", "C2"],
        comparisons=comparisons,
    )
    records = [
        _record("C0", attack_success=False),
        _record("C1", attack_success=True, injection_success=True),
        _record("C2", btcr_success=False),
    ]
    path = tmp_path / "reset.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))

    expected = enumerate_expected_identities(config)
    inventory = load_result_inventory(str(path), config)
    output = build_analysis_output(inventory, config, _engine())
    serialized = json.loads(json.dumps(output))

    assert len(expected) == 3
    assert inventory.expected_runs == 3
    assert set(serialized["stats"]) == {_key("C0"), _key("C1"), _key("C2")}
    assert serialized["comparisons"][0]["condition_a"] == _key("C0")
    assert serialized["comparisons"][0]["condition_b"] == _key("C1")
    assert serialized["completion"]["completed_runs"] == 3
