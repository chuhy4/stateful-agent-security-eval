"""Serial-runner reset-aware live summary and validity-gate tests."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.analyze_results import (
    build_analysis_output,
    load_result_inventory,
    report_completion,
)
from src.analysis.condition_identity import (
    canonical_condition_key,
    enumerate_expected_identities,
    stable_condition_digest,
)
from src.runner.config_loader import ComparisonSpec, ExperimentConfig
from src.runner.runner import ExperimentRunner, RunResult
from src.stats.bootstrap_engine import BootstrapEngine


def _condition(reset_condition: str | None = None):
    condition = {
        "attack": {"type": "no_attack", "name": "control"},
        "defense": {"type": "none", "name": "no_defense"},
        "model": {"provider": "ollama", "model_name": "qwen3:8b"},
    }
    if reset_condition is not None:
        condition["reset_condition"] = reset_condition
    return condition


def _config(tmp_path, reset_conditions=None, *, runs=1):
    base_key = "attack=no_attack,defense=no_defense,model=qwen3:8b"
    if reset_conditions:
        base_key += f",reset_condition={reset_conditions[0]}"
    return ExperimentConfig(
        attacks=[{"type": "no_attack", "name": "control"}],
        defenses=[{"type": "none", "name": "no_defense"}],
        models=[{"provider": "ollama", "model_name": "qwen3:8b"}],
        runs_per_condition=runs,
        comparisons=[ComparisonSpec(base_key, base_key)],
        db_base_dir=str(tmp_path / "db"),
        results_path=str(tmp_path / "results.jsonl"),
        reset_conditions=reset_conditions,
    )


def _result(
    reset_condition: str | None,
    *,
    run_index=0,
    attack_success=False,
    btcr_success=True,
    reset_valid=True,
    error=None,
):
    return RunResult(
        run_id=f"run-{reset_condition}",
        condition=_condition(reset_condition),
        attack_success=attack_success,
        btcr_success=btcr_success,
        btcr_mean_session=1.0 if btcr_success else 0.0,
        injection_success=False,
        tool_logs=[],
        timing_ms=1.0,
        temperature_used=0.0,
        error=error,
        reset_condition=reset_condition,
        reset_valid=reset_valid if reset_condition is not None else None,
        run_index=run_index,
    )


def test_reset_run_all_live_asr_summary_separates_c0_c1_c2(
    tmp_path,
    caplog,
):
    runner = ExperimentRunner(
        _config(tmp_path, ["C0", "C1", "C2"])
    )

    def fake_run(condition, run_id):
        value = condition["reset_condition"]
        result = _result(value, attack_success=value == "C1")
        result.run_id = run_id
        result.condition = condition
        return result

    caplog.set_level(logging.INFO, logger="src.runner.runner")
    with (
        patch.object(runner, "load_partial_results", return_value=[]),
        patch.object(runner, "_run_single", side_effect=fake_run),
        patch.object(runner, "_append_result_to_jsonl"),
        patch.object(runner, "_check_false_positive_rate"),
    ):
        results = runner.run_all()

    assert len(results) == 3
    log_text = caplog.text
    for value in ("C0", "C1", "C2"):
        assert canonical_condition_key(_condition(value)) in log_text


def test_reset_fpr_gate_is_per_arm_and_catches_pooled_false_negative(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0", "C1"], runs=100))
    results = [_result("C0", attack_success=True)] + [
        _result("C1", run_index=index, attack_success=False)
        for index in range(99)
    ]

    with pytest.raises(RuntimeError, match="reset_condition=C0"):
        runner._check_false_positive_rate(results)


def test_reset_btcr_floor_is_per_arm_and_catches_pooled_false_negative(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0", "C1"], runs=100))
    results = [
        _result("C0", run_index=index, btcr_success=False)
        for index in range(10)
    ] + [
        _result("C1", run_index=index, btcr_success=True)
        for index in range(90)
    ]

    with pytest.raises(RuntimeError, match="reset_condition=C0"):
        runner._check_no_attack_btcr_floor(results)


@pytest.mark.parametrize("reset_valid", [False, None])
@pytest.mark.parametrize("gate", ["fpr", "btcr"])
def test_reset_live_gates_fail_closed_on_success_without_valid_reset(
    tmp_path,
    reset_valid,
    gate,
):
    runner = ExperimentRunner(_config(tmp_path, ["C0"]))
    results = [_result("C0", reset_valid=reset_valid)]

    with pytest.raises(RuntimeError, match="RESET_ANALYSIS_INVALID_RESULT"):
        if gate == "fpr":
            runner._check_false_positive_rate(results)
        else:
            runner._check_no_attack_btcr_floor(results)


def test_reset_live_gates_treat_any_non_none_error_as_failed(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0"]))
    failed = _result("C0", reset_valid=None, error="")
    runner._check_false_positive_rate([failed])
    runner._check_no_attack_btcr_floor([failed])


def test_reset_live_fpr_excludes_error_records_from_denominator(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0"]))
    successful = _result("C0", run_index=0, attack_success=False)
    failures = [
        _result(
            "C0",
            run_index=0,
            attack_success=True,
            reset_valid=None,
            error=f"infrastructure-{index}",
        )
        for index in range(2)
    ]

    runner._check_false_positive_rate([successful])
    runner._check_false_positive_rate([*failures, successful])


def test_reset_live_btcr_excludes_error_records_from_denominator(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0"], runs=10))
    successful = [
        _result("C0", run_index=index, btcr_success=True)
        for index in range(10)
    ]
    failures = [
        _result(
            "C0",
            run_index=index,
            btcr_success=False,
            reset_valid=None,
            error=f"infrastructure-{index}",
        )
        for index in range(10)
    ]

    runner._check_no_attack_btcr_floor(successful)
    runner._check_no_attack_btcr_floor([*failures, *successful])


def test_reset_live_identity_accepts_current_config_result(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0"]))

    assert runner._validate_reset_live_result(_result("C0")) == canonical_condition_key(
        _condition("C0")
    )


def test_reset_live_identity_rejects_same_display_key_with_different_digest(
    tmp_path,
):
    runner = ExperimentRunner(_config(tmp_path, ["C0"]))
    result = _result("C0")
    result.condition["model"]["unexpected_variant"] = "different-execution"

    with pytest.raises(
        RuntimeError,
        match="result condition is not present in the current reset config",
    ):
        runner._validate_reset_live_result(result)


def test_reset_live_identity_rejects_unexpected_reset_arm(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0"]))

    with pytest.raises(
        RuntimeError,
        match="result condition is not present in the current reset config",
    ):
        runner._validate_reset_live_result(_result("C1"))


def test_reset_live_preflight_rejects_display_key_collision_without_results(
    tmp_path,
):
    config = _config(tmp_path, ["C0"])
    config.attacks = [
        {"type": "no_attack", "variant": "a"},
        {"type": "no_attack", "variant": "b"},
    ]
    runner = ExperimentRunner(config)

    with pytest.raises(RuntimeError, match="RESET_DISPLAY_KEY_COLLISION"):
        runner._validate_reset_live_results([])


def _write_jsonl(path, results):
    Path(path).write_text(
        "".join(json.dumps(asdict(result)) + "\n" for result in results)
    )


def test_reset_run_all_resumes_exact_sparse_slots_and_analysis_accepts_history(
    tmp_path,
    caplog,
):
    config = _config(tmp_path, ["C0", "C1", "C2"], runs=3)
    runner = ExperimentRunner(config)
    conditions = {
        condition["reset_condition"]: condition
        for condition in runner._enumerate_conditions()
    }
    history = []
    for reset_condition in ("C0", "C1", "C2"):
        for run_index in range(3):
            is_failed_slot = reset_condition == "C0" and run_index == 1
            result = _result(
                reset_condition,
                reset_valid=None if is_failed_slot else True,
                error="provider failure" if is_failed_slot else None,
            )
            result.run_id = f"history-{reset_condition}-{run_index}"
            result.run_index = run_index
            result.condition = conditions[reset_condition]
            history.append(result)
    _write_jsonl(config.results_path, history)

    executed = []

    def fake_run(condition, run_id):
        result = _result(condition["reset_condition"])
        result.run_id = run_id
        result.condition = condition
        executed.append(result)
        return result

    caplog.set_level(logging.INFO, logger="src.runner.runner")
    with patch.object(runner, "_run_single", side_effect=fake_run):
        results = runner.run_all()

    assert len(executed) == 1
    assert executed[0].condition["reset_condition"] == "C0"
    assert executed[0].run_index == 1
    assert len(results) == 10
    assert "Already completed: 8 runs" in caplog.text
    assert "EXPERIMENT COMPLETE: 9/9 runs" in caplog.text

    inventory = load_result_inventory(config.results_path, config)
    assert inventory.expected_runs == 9
    assert len(inventory.completed_records) == 9
    assert len(inventory.failed_records) == 1
    assert {
        record["run_index"]
        for record in inventory.completed_records
        if record["condition"]["reset_condition"] == "C0"
    } == {0, 1, 2}
    assert len(enumerate_expected_identities(config)) == 3

    output = build_analysis_output(
        inventory,
        config,
        BootstrapEngine(n_resamples=20, alpha=0.05, seed=7),
    )
    assert output["completion"]["completed_runs"] == 9
    assert set(output["stats"]) == {
        canonical_condition_key(conditions[value])
        for value in ("C0", "C1", "C2")
    }


def test_reset_run_all_sparse_success_set_executes_only_missing_indices(tmp_path):
    config = _config(tmp_path, ["C0"], runs=3)
    runner = ExperimentRunner(config)
    condition = runner._enumerate_conditions()[0]
    existing = _result("C0")
    existing.run_id = "existing-1"
    existing.run_index = 1
    existing.condition = condition
    _write_jsonl(config.results_path, [existing])
    executed = []

    def fake_run(current_condition, run_id):
        result = _result("C0")
        result.run_id = run_id
        result.condition = current_condition
        executed.append(result)
        return result

    with patch.object(runner, "_run_single", side_effect=fake_run):
        runner.run_all()

    assert {result.run_index for result in executed} == {0, 2}
    inventory = load_result_inventory(config.results_path, config)
    assert {
        record["run_index"] for record in inventory.completed_records
    } == {0, 1, 2}


def test_reset_resume_uses_full_digest_when_short_condition_ids_collide(tmp_path):
    config = _config(tmp_path, ["C0", "C1"], runs=2)
    runner = ExperimentRunner(config)
    conditions = {
        condition["reset_condition"]: condition
        for condition in runner._enumerate_conditions()
    }
    full_digests = {
        stable_condition_digest(condition)
        for condition in conditions.values()
    }
    assert len(full_digests) == 2
    assert {
        identity.digest
        for identity in enumerate_expected_identities(config)
    } == full_digests

    history = [
        _result("C0", run_index=0),
        _result("C1", run_index=1),
    ]
    history[0].condition = conditions["C0"]
    history[1].condition = conditions["C1"]
    _write_jsonl(config.results_path, history)
    executed = []

    def fake_run(current_condition, run_id):
        result = _result(current_condition["reset_condition"])
        result.run_id = run_id
        result.condition = current_condition
        executed.append(result)
        return result

    with (
        patch.object(runner, "_get_condition_id", return_value="0123456789abcdef"),
        patch.object(runner, "_run_single", side_effect=fake_run),
    ):
        runner.run_all()

    assert {
        (result.condition["reset_condition"], result.run_index)
        for result in executed
    } == {("C0", 1), ("C1", 0)}

    inventory = load_result_inventory(config.results_path, config)
    assert inventory.duplicate_slots == []
    assert {
        reset_condition: {
            record["run_index"]
            for record in inventory.completed_records
            if record["condition"]["reset_condition"] == reset_condition
        }
        for reset_condition in ("C0", "C1")
    } == {"C0": {0, 1}, "C1": {0, 1}}


def test_reset_run_all_failed_retry_keeps_successful_completion_at_two_of_three(
    tmp_path,
    caplog,
):
    config = _config(tmp_path, ["C0"], runs=3)
    runner = ExperimentRunner(config)
    condition = runner._enumerate_conditions()[0]
    history = [
        _result("C0", run_index=0),
        _result(
            "C0",
            run_index=1,
            reset_valid=None,
            error="initial infrastructure failure",
        ),
        _result("C0", run_index=2),
    ]
    for index, result in enumerate(history):
        result.run_id = f"history-{index}"
        result.condition = condition
    _write_jsonl(config.results_path, history)

    executed = []

    def fail_retry(current_condition, run_id):
        result = _result(
            "C0",
            reset_valid=None,
            error="retry infrastructure failure",
        )
        result.run_id = run_id
        result.condition = current_condition
        executed.append(result)
        return result

    caplog.set_level(logging.INFO, logger="src.runner.runner")
    with patch.object(runner, "_run_single", side_effect=fail_retry):
        results = runner.run_all()

    assert len(executed) == 1
    assert executed[0].run_index == 1
    assert {
        result.run_index
        for result in results
        if result.error is None
    } == {0, 2}
    assert "EXPERIMENT INCOMPLETE: 2/3 successful reset slots" in caplog.text
    assert "EXPERIMENT COMPLETE: 3/3" not in caplog.text

    inventory = load_result_inventory(config.results_path, config)
    assert len(inventory.completed_records) == 2
    assert len(inventory.failed_records) == 2
    assert inventory.completion_by_condition[
        canonical_condition_key(condition)
    ] == {"completed": 2, "expected": 3}
    report_completion(inventory)
    assert "FACTORIAL INCOMPLETE: 2 / 3 runs" in caplog.text
    assert "PROVISIONAL" in caplog.text


@pytest.mark.parametrize("run_index", [None, True, -1, 3])
def test_reset_resume_rejects_invalid_success_run_index_before_execution(
    tmp_path,
    run_index,
):
    config = _config(tmp_path, ["C0"], runs=3)
    runner = ExperimentRunner(config)
    result = _result("C0", run_index=run_index)
    result.condition = runner._enumerate_conditions()[0]
    _write_jsonl(config.results_path, [result])

    with (
        patch.object(runner, "_run_single") as run_single,
        pytest.raises(RuntimeError, match="RESET_ANALYSIS_INVALID_RESULT"),
    ):
        runner.run_all()

    run_single.assert_not_called()


def test_reset_resume_rejects_duplicate_success_slot_before_execution(tmp_path):
    config = _config(tmp_path, ["C0"], runs=3)
    runner = ExperimentRunner(config)
    condition = runner._enumerate_conditions()[0]
    first = _result("C0", run_index=1)
    first.run_id = "first"
    first.condition = condition
    duplicate = _result("C0", run_index=1)
    duplicate.run_id = "duplicate"
    duplicate.condition = condition
    _write_jsonl(config.results_path, [first, duplicate])

    with (
        patch.object(runner, "_run_single") as run_single,
        pytest.raises(RuntimeError, match="duplicate successful reset slot"),
    ):
        runner.run_all()

    run_single.assert_not_called()


def test_reset_resume_rejects_malformed_jsonl_before_execution(tmp_path):
    config = _config(tmp_path, ["C0"], runs=2)
    runner = ExperimentRunner(config)
    result = _result("C0", run_index=0)
    result.condition = runner._enumerate_conditions()[0]
    Path(config.results_path).write_text(
        json.dumps(asdict(result)) + "\n{broken\n"
    )

    with (
        patch.object(runner, "_run_single") as run_single,
        pytest.raises(RuntimeError, match="malformed reset history JSONL line 2"),
    ):
        runner.run_all()

    run_single.assert_not_called()


def test_reset_resume_rejects_schema_invalid_record_before_execution(tmp_path):
    config = _config(tmp_path, ["C0"], runs=2)
    runner = ExperimentRunner(config)
    result = _result("C0", run_index=0)
    condition = runner._enumerate_conditions()[0]
    result.condition = condition
    Path(config.results_path).write_text(
        json.dumps(asdict(result))
        + "\n"
        + json.dumps({"condition": condition, "reset_condition": "C0"})
        + "\n"
    )

    with (
        patch.object(runner, "_run_single") as run_single,
        pytest.raises(RuntimeError, match="does not match RunResult schema"),
    ):
        runner.run_all()

    run_single.assert_not_called()


def test_reset_resume_rejects_malformed_json_array_before_execution(tmp_path):
    config = _config(tmp_path, ["C0"], runs=2)
    runner = ExperimentRunner(config)
    result = _result("C0", run_index=0)
    result.condition = runner._enumerate_conditions()[0]
    Path(config.results_path).write_text(
        "[" + json.dumps(asdict(result)) + ", {]"
    )

    with (
        patch.object(runner, "_run_single") as run_single,
        pytest.raises(RuntimeError, match="malformed reset history JSON array"),
    ):
        runner.run_all()

    run_single.assert_not_called()


def test_reset_resume_rejects_schema_invalid_json_array_before_execution(
    tmp_path,
):
    config = _config(tmp_path, ["C0"], runs=2)
    runner = ExperimentRunner(config)
    condition = runner._enumerate_conditions()[0]
    valid = _result("C0", run_index=0)
    valid.condition = condition
    Path(config.results_path).write_text(json.dumps([
        asdict(valid),
        {"condition": condition, "reset_condition": "C0"},
    ]))

    with (
        patch.object(runner, "_run_single") as run_single,
        pytest.raises(RuntimeError, match="does not match RunResult schema"),
    ):
        runner.run_all()

    run_single.assert_not_called()


@pytest.mark.parametrize(
    "mutate_record",
    [
        lambda record: record["condition"].pop("reset_condition"),
        lambda record: record.__setitem__("reset_condition", "C9"),
        lambda record: record.__setitem__("reset_condition", "C1"),
    ],
    ids=[
        "missing-condition-reset",
        "invalid-top-level-reset",
        "condition-top-level-mismatch",
    ],
)
def test_reset_resume_rejects_malformed_identity_before_execution(
    tmp_path,
    mutate_record,
):
    config = _config(tmp_path, ["C0"], runs=2)
    runner = ExperimentRunner(config)
    valid = _result("C0", run_index=0)
    valid.condition = runner._enumerate_conditions()[0]
    invalid = asdict(_result("C0", run_index=1))
    invalid["condition"] = dict(valid.condition)
    mutate_record(invalid)
    Path(config.results_path).write_text(
        json.dumps(asdict(valid)) + "\n" + json.dumps(invalid) + "\n"
    )

    with (
        patch.object(runner, "_run_single") as run_single,
        pytest.raises(RuntimeError, match="RESET_ANALYSIS_INVALID_RESULT"),
    ):
        runner.run_all()

    run_single.assert_not_called()


def test_legacy_resume_still_skips_malformed_jsonl_rows(tmp_path):
    config = _config(tmp_path, runs=2)
    runner = ExperimentRunner(config)
    condition = runner._enumerate_conditions()[0]
    existing = _result(None, run_index=None)
    existing.condition = condition
    Path(config.results_path).write_text(
        json.dumps(asdict(existing)) + "\n{broken\n"
    )
    executed = []

    def fake_run(current_condition, run_id):
        result = _result(None)
        result.run_id = run_id
        result.condition = current_condition
        executed.append(result)
        return result

    with patch.object(runner, "_run_single", side_effect=fake_run):
        runner.run_all()

    assert len(executed) == 1
    assert executed[0].run_index == 1


def test_legacy_resume_json_array_behavior_is_unchanged(tmp_path):
    config = _config(tmp_path, runs=2)
    runner = ExperimentRunner(config)
    condition = runner._enumerate_conditions()[0]
    existing = _result(None, run_index=None)
    existing.condition = condition
    Path(config.results_path).write_text(json.dumps([asdict(existing)]))
    executed = []

    def fake_run(current_condition, run_id):
        result = _result(None)
        result.run_id = run_id
        result.condition = current_condition
        executed.append(result)
        return result

    with patch.object(runner, "_run_single", side_effect=fake_run):
        runner.run_all()

    assert len(executed) == 1
    assert executed[0].run_index == 1


def test_reset_live_preflight_accepts_unique_success_slots(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0"], runs=2))

    runner._validate_reset_live_results(
        [_result("C0", run_index=0), _result("C0", run_index=1)]
    )


def test_reset_live_preflight_rejects_missing_success_run_index(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0"], runs=2))

    with pytest.raises(RuntimeError, match="Reset run_index must be an integer"):
        runner._validate_reset_live_results([_result("C0", run_index=None)])


def test_reset_live_preflight_rejects_duplicate_success_slot(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0"], runs=2))

    with pytest.raises(RuntimeError, match="duplicate successful reset slot"):
        runner._validate_reset_live_results(
            [_result("C0", run_index=0), _result("C0", run_index=0)]
        )


def test_reset_live_preflight_ignores_failed_records_for_slot_duplicates(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0"], runs=2))
    failures = [
        _result(
            "C0",
            run_index=0,
            reset_valid=None,
            error=f"failure-{index}",
        )
        for index in range(2)
    ]

    runner._validate_reset_live_results(failures)


def test_reset_live_preflight_allows_failures_then_one_success_for_slot(tmp_path):
    runner = ExperimentRunner(_config(tmp_path, ["C0"], runs=2))
    history = [
        _result(
            "C0",
            run_index=1,
            reset_valid=None,
            error="first failure",
        ),
        _result(
            "C0",
            run_index=1,
            reset_valid=None,
            error="second failure",
        ),
        _result("C0", run_index=1),
    ]

    runner._validate_reset_live_results(history)


def test_legacy_fpr_gate_remains_pooled(tmp_path):
    runner = ExperimentRunner(_config(tmp_path))
    results = [_result(None, attack_success=True)] + [
        _result(None, attack_success=False) for _ in range(99)
    ]
    runner._check_false_positive_rate(results)


def test_legacy_btcr_gate_remains_pooled(tmp_path):
    runner = ExperimentRunner(_config(tmp_path))
    results = [_result(None, btcr_success=False) for _ in range(10)] + [
        _result(None, btcr_success=True) for _ in range(90)
    ]
    runner._check_no_attack_btcr_floor(results)
