"""Parallel-runner reset fail-closed and legacy compatibility tests."""
from __future__ import annotations

import hashlib
import json
from itertools import product
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.runner.config_loader import ComparisonSpec, ExperimentConfig
from src.runner.parallel_runner import (
    ParallelExperimentRunner,
    _run_condition_batch,
)


UNAVAILABLE_ERROR = (
    "PARALLEL_RESET_POLICY_UNAVAILABLE: use serial ExperimentRunner"
)


def _config(
    tmp_path: Path,
    *,
    reset_conditions=None,
    models=None,
    runs_per_condition: int = 1,
) -> ExperimentConfig:
    return ExperimentConfig(
        attacks=[
            {"type": "no_attack", "name": "a0"},
            {"type": "no_attack", "name": "a1"},
        ],
        defenses=[
            {"type": "none", "name": "d0"},
            {"type": "none", "name": "d1"},
        ],
        models=models or [
            {"provider": "ollama", "model_name": "qwen3:8b", "name": "m0"},
            {"provider": "ollama", "model_name": "qwen3:14b", "name": "m1"},
        ],
        runs_per_condition=runs_per_condition,
        comparisons=[ComparisonSpec(condition_a="a", condition_b="b")],
        results_path=str(tmp_path / "results" / "results.jsonl"),
        db_base_dir=str(tmp_path / "runs"),
        reset_conditions=reset_conditions,
    )


def _worker_config_dict(config: ExperimentConfig) -> dict:
    """Mirror the unchanged legacy worker payload built by run_all()."""
    return {
        "attacks": config.attacks,
        "defenses": config.defenses,
        "models": config.models,
        "runs_per_condition": config.runs_per_condition,
        "results_path": config.results_path,
        "db_base_dir": config.db_base_dir,
        "effect_size": config.effect_size,
        "alpha": config.alpha,
        "power": config.power,
        "n_bootstrap": config.n_bootstrap,
        "bootstrap_seed": config.bootstrap_seed,
        "injection_similarity_threshold": config.injection_similarity_threshold,
        "detection": config.detection,
        "btcr_criteria": config.btcr_criteria,
        "comparisons": config.comparisons,
    }


def test_valid_reset_config_fails_closed_before_parallel_side_effects(tmp_path):
    config = _config(tmp_path, reset_conditions=["C0", "C1", "C2"])
    results_parent = Path(config.results_path).parent

    with (
        patch("src.runner.parallel_runner.mp.cpu_count") as cpu_count,
        patch("src.runner.parallel_runner.mp.get_context") as get_context,
        patch.object(Path, "mkdir") as mkdir,
        patch("src.runner.parallel_runner.ExperimentRunner") as serial_runner,
        patch("src.runner.runner.ExperimentRunner._build_model") as build_model,
        patch("src.runner.parallel_runner._run_condition_batch") as worker,
    ):
        with pytest.raises(RuntimeError) as exc_info:
            ParallelExperimentRunner(config)

    assert str(exc_info.value) == UNAVAILABLE_ERROR
    assert not results_parent.exists()
    cpu_count.assert_not_called()
    get_context.assert_not_called()
    mkdir.assert_not_called()
    serial_runner.assert_not_called()
    build_model.assert_not_called()
    worker.assert_not_called()


@pytest.mark.parametrize(
    ("reset_conditions", "message"),
    [
        ([], "must not be empty"),
        (["C0", "C0"], "must not contain duplicates"),
        (["C0", "C9"], "Invalid reset condition"),
    ],
)
def test_invalid_reset_config_keeps_configuration_error(
    tmp_path,
    reset_conditions,
    message,
):
    config = _config(tmp_path, reset_conditions=reset_conditions)

    with patch("src.runner.parallel_runner.mp.cpu_count") as cpu_count:
        with pytest.raises(ValueError, match=message) as exc_info:
            ParallelExperimentRunner(config)

    assert UNAVAILABLE_ERROR not in str(exc_info.value)
    cpu_count.assert_not_called()


def test_post_construction_reset_mutation_fails_before_run_all_side_effects(
    tmp_path,
):
    config = _config(tmp_path)
    runner = ParallelExperimentRunner(config, num_workers=1)
    runner.config.reset_conditions = ["C0"]
    results_parent = Path(config.results_path).parent

    with (
        patch.object(Path, "mkdir") as mkdir,
        patch("src.runner.parallel_runner.mp.get_context") as get_context,
        patch("src.runner.parallel_runner.logger.info") as log_info,
        patch("src.runner.parallel_runner._run_condition_batch") as worker,
        patch("src.runner.runner.ExperimentRunner._build_model") as build_model,
        patch("src.runner.parallel_runner._append_result_worker") as append_result,
        patch.object(runner, "_load_partial_results") as load_results,
        patch.object(runner, "_enumerate_conditions") as enumerate_conditions,
        patch.object(runner, "_append_result_to_jsonl") as append_jsonl,
    ):
        with pytest.raises(RuntimeError) as exc_info:
            runner.run_all()

    assert str(exc_info.value) == UNAVAILABLE_ERROR
    assert not results_parent.exists()
    mkdir.assert_not_called()
    get_context.assert_not_called()
    log_info.assert_not_called()
    worker.assert_not_called()
    build_model.assert_not_called()
    append_result.assert_not_called()
    load_results.assert_not_called()
    enumerate_conditions.assert_not_called()
    append_jsonl.assert_not_called()


@pytest.mark.parametrize(
    ("reset_conditions", "message"),
    [
        ([], "must not be empty"),
        (["C0", "C0"], "must not contain duplicates"),
        (["C9"], "Invalid reset condition"),
    ],
)
def test_post_construction_invalid_reset_mutation_keeps_validation_error(
    tmp_path,
    reset_conditions,
    message,
):
    config = _config(tmp_path)
    runner = ParallelExperimentRunner(config, num_workers=1)
    runner.config.reset_conditions = reset_conditions

    with (
        patch.object(Path, "mkdir") as mkdir,
        patch("src.runner.parallel_runner.mp.get_context") as get_context,
        patch("src.runner.parallel_runner.logger.info") as log_info,
        patch("src.runner.parallel_runner._run_condition_batch") as worker,
        patch("src.runner.runner.ExperimentRunner._build_model") as build_model,
        patch("src.runner.parallel_runner._append_result_worker") as append_result,
        patch.object(runner, "_load_partial_results") as load_results,
        patch.object(runner, "_enumerate_conditions") as enumerate_conditions,
        patch.object(runner, "_append_result_to_jsonl") as append_jsonl,
    ):
        with pytest.raises(ValueError, match=message) as exc_info:
            runner.run_all()

    assert UNAVAILABLE_ERROR not in str(exc_info.value)
    mkdir.assert_not_called()
    get_context.assert_not_called()
    log_info.assert_not_called()
    worker.assert_not_called()
    build_model.assert_not_called()
    append_result.assert_not_called()
    load_results.assert_not_called()
    enumerate_conditions.assert_not_called()
    append_jsonl.assert_not_called()


def test_direct_worker_condition_with_reset_factor_fails_before_construction(
    tmp_path,
):
    config = _config(tmp_path)
    condition = {
        "attack": config.attacks[0],
        "defense": config.defenses[0],
        "model": config.models[0],
        "reset_condition": "C1",
    }
    results_path = tmp_path / "worker-results" / "results.jsonl"

    with (
        patch("src.runner.parallel_runner.ExperimentConfig") as config_type,
        patch("src.runner.parallel_runner.ExperimentRunner") as serial_runner,
        patch("src.runner.parallel_runner._append_result_worker") as append_result,
    ):
        with pytest.raises(RuntimeError) as exc_info:
            _run_condition_batch(
                _worker_config_dict(config),
                condition,
                1,
                str(results_path),
                0,
            )

    assert str(exc_info.value) == UNAVAILABLE_ERROR
    assert not results_path.parent.exists()
    config_type.assert_not_called()
    serial_runner.assert_not_called()
    append_result.assert_not_called()


def test_direct_worker_reset_config_payload_also_fails_closed(tmp_path):
    config = _config(tmp_path)
    config_dict = _worker_config_dict(config) | {"reset_conditions": ["C2"]}
    condition = {
        "attack": config.attacks[0],
        "defense": config.defenses[0],
        "model": config.models[0],
    }

    with (
        patch("src.runner.parallel_runner.ExperimentConfig") as config_type,
        patch("src.runner.parallel_runner.ExperimentRunner") as serial_runner,
    ):
        with pytest.raises(RuntimeError) as exc_info:
            _run_condition_batch(
                config_dict,
                condition,
                1,
                str(tmp_path / "must-not-exist" / "results.jsonl"),
                0,
            )

    assert str(exc_info.value) == UNAVAILABLE_ERROR
    config_type.assert_not_called()
    serial_runner.assert_not_called()


def test_legacy_parallel_enumeration_order_and_shape_are_unchanged(tmp_path):
    config = _config(tmp_path)
    runner = ParallelExperimentRunner(config, num_workers=2)
    expected = [
        {"attack": attack, "defense": defense, "model": model}
        for attack, defense, model in product(
            config.attacks,
            config.defenses,
            config.models,
        )
    ]

    assert runner._enumerate_conditions() == expected
    assert all("reset_condition" not in condition for condition in expected)


def test_legacy_parallel_resume_and_condition_hash_are_unchanged(tmp_path):
    config = _config(
        tmp_path,
        models=[{
            "provider": "ollama",
            "model_name": "qwen3:8b",
            "name": "m0",
        }],
    )
    config.attacks = [config.attacks[0]]
    config.defenses = [config.defenses[0]]
    runner = ParallelExperimentRunner(config, num_workers=1)
    condition = runner._enumerate_conditions()[0]
    condition_bytes = json.dumps(
        condition,
        sort_keys=True,
        default=str,
    ).encode()
    assert hashlib.sha256(condition_bytes).hexdigest()[:16] == "2d6616f958dce4eb"

    results_path = Path(config.results_path)
    results_path.parent.mkdir(parents=True)
    completed = {"run_id": "done", "condition": condition, "error": None}
    results_path.write_text(json.dumps(completed) + "\n")
    real_sha256 = hashlib.sha256

    with (
        patch("hashlib.sha256", wraps=real_sha256) as sha256,
        patch("src.runner.parallel_runner.mp.get_context") as get_context,
    ):
        results = runner.run_all()

    assert results == [completed]
    assert any(
        call.args == (condition_bytes,)
        for call in sha256.call_args_list
    )
    get_context.assert_not_called()


def test_legacy_partial_resume_keeps_worker_payload_and_remaining_count(tmp_path):
    config = _config(
        tmp_path,
        models=[{
            "provider": "ollama",
            "model_name": "qwen3:8b",
            "name": "m0",
        }],
        runs_per_condition=3,
    )
    config.attacks = [config.attacks[0]]
    config.defenses = [config.defenses[0]]
    runner = ParallelExperimentRunner(config, num_workers=1)
    condition = runner._enumerate_conditions()[0]
    results_path = Path(config.results_path)
    results_path.parent.mkdir(parents=True)
    completed = {"run_id": "done", "condition": condition, "error": None}
    results_path.write_text(json.dumps(completed) + "\n")

    pool = MagicMock()
    pool.__enter__.return_value = pool
    pool.imap_unordered.return_value = []
    context = MagicMock()
    context.Pool.return_value = pool

    with patch(
        "src.runner.parallel_runner.mp.get_context",
        return_value=context,
    ):
        results = runner.run_all()

    assert results == [completed]
    worker_args = pool.imap_unordered.call_args.args[1]
    assert len(worker_args) == 1
    config_dict, queued_condition, remaining, queued_path, worker_id = worker_args[0]
    assert config_dict == _worker_config_dict(config)
    assert "reset_conditions" not in config_dict
    assert queued_condition == condition
    assert "reset_condition" not in queued_condition
    assert remaining == 2
    assert queued_path == config.results_path
    assert worker_id == 0


def test_legacy_parallel_bedrock_remains_allowed(tmp_path):
    config = _config(
        tmp_path,
        models=[{"provider": "bedrock", "model_name": "legacy-model"}],
    )

    runner = ParallelExperimentRunner(config, num_workers=1)

    assert runner.config is config
    assert runner.config.reset_conditions is None
    assert all(
        condition["model"]["provider"] == "bedrock"
        for condition in runner._enumerate_conditions()
    )
    assert all(
        "reset_condition" not in condition
        for condition in runner._enumerate_conditions()
    )
