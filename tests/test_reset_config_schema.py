"""Patch 4A configuration and result-schema tests; no model calls."""
from __future__ import annotations

import json
import textwrap
from dataclasses import asdict
from itertools import product
from unittest.mock import patch

import pytest

from src.runner.config_loader import (
    ComparisonSpec,
    ExperimentConfig,
    load_config,
    validate_config,
)
from src.runner.runner import ExperimentRunner, RunResult


def _config(tmp_path, *, reset_conditions=None, models=None) -> ExperimentConfig:
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
        runs_per_condition=1,
        comparisons=[ComparisonSpec(condition_a="a", condition_b="b")],
        reset_conditions=reset_conditions,
        db_base_dir=str(tmp_path / "runs"),
    )


def _raw_config(*, reset_marker=False, reset_conditions=None, provider="ollama"):
    raw = {
        "attacks": [{"type": "no_attack"}],
        "defenses": [{"type": "none"}],
        "models": [{
            "provider": provider,
            "model_name": "qwen3:8b" if provider == "ollama" else "test-model",
        }],
        "runs_per_condition": 1,
        "comparisons": [{"condition_a": "a", "condition_b": "b"}],
    }
    if reset_marker:
        raw["reset_conditions"] = reset_conditions
    return raw


def _yaml_config(*, reset_block: str = "", provider: str = "ollama") -> str:
    model_name = "qwen3:8b" if provider == "ollama" else "test-model"
    return textwrap.dedent(f"""\
        attacks:
          - type: no_attack
        defenses:
          - type: none
        models:
          - provider: {provider}
            model_name: {model_name}
        runs_per_condition: 1
        comparisons:
          - condition_a: a
            condition_b: b
        {reset_block}
    """)


def _load_yaml(tmp_path, content: str) -> ExperimentConfig:
    path = tmp_path / "experiment.yaml"
    path.write_text(content)
    return load_config(str(path))


def _result_dict(condition: dict) -> dict:
    """Old-schema result with no Patch 4A fields."""
    return {
        "run_id": "legacy-run",
        "condition": condition,
        "attack_success": False,
        "btcr_success": True,
        "btcr_mean_session": 1.0,
        "injection_success": False,
        "tool_logs": [],
        "timing_ms": 1.0,
        "temperature_used": 0.0,
    }


def _reset_result(condition: dict) -> RunResult:
    return RunResult(
        **_result_dict(condition),
        reset_condition="C1",
        db_identity="/tmp/reset-run.db",
        session_threads=[
            {"session_index": 0, "session_type": "injection", "thread_id": "t0"},
            {"session_index": 1, "session_type": "trigger", "thread_id": "t1"},
        ],
        reset_boundaries=[{
            "boundary_index": 0,
            "pre_manifest": {"canonical_memory": [["rule", "value"]]},
            "post_manifest": {"canonical_memory": [["rule", "value"]]},
            "mutation": {
                "canonical_memory_clear_attempted": False,
                "canonical_rows_deleted": None,
            },
            "assertions": [{"name": "new_conversation_thread", "passed": True}],
            "reset_valid": True,
            "reasons": [],
        }],
        reset_valid=True,
        reset_invalid_reasons=[],
        withdrawn_source_ids=["malicious_doc", "monitoring_config"],
        email_records=[{
            "operation": "send",
            "to": ["auditor@example.com"],
            "session_index": 1,
        }],
    )


def test_legacy_enumeration_is_identical_to_baseline(tmp_path):
    config = _config(tmp_path)
    runner = ExperimentRunner(config)

    expected = [
        {"attack": attack, "defense": defense, "model": model}
        for attack, defense, model in product(
            config.attacks, config.defenses, config.models
        )
    ]

    assert runner._enumerate_conditions() == expected
    assert all("reset_condition" not in condition for condition in expected)


def test_known_legacy_condition_hash_is_unchanged(tmp_path):
    runner = ExperimentRunner(_config(tmp_path))
    condition = {
        "attack": {"type": "no_attack", "name": "a0"},
        "defense": {"type": "none", "name": "d0"},
        "model": {
            "provider": "ollama",
            "model_name": "qwen3:8b",
            "name": "m0",
        },
    }

    assert runner._get_condition_id(condition) == "2d6616f958dce4eb"


def test_legacy_positional_experiment_config_constructor_is_unchanged(tmp_path):
    attacks = [{"type": "no_attack"}]
    defenses = [{"type": "none"}]
    models = [{"provider": "bedrock", "model_name": "legacy-model"}]
    comparisons = [ComparisonSpec("a", "b")]
    config = ExperimentConfig(
        attacks,
        defenses,
        models,
        3,
        comparisons,
        0.20,
        0.01,
        0.90,
        "results/legacy.jsonl",
        str(tmp_path / "legacy-runs"),
        0.75,
        500,
        17,
        {"threshold": 0.8},
        {"benign": "email_draft"},
        {"legacy": True},
    )

    assert config.attacks is attacks
    assert config.defenses is defenses
    assert config.models is models
    assert config.runs_per_condition == 3
    assert config.comparisons is comparisons
    assert config.effect_size == 0.20
    assert config.alpha == 0.01
    assert config.power == 0.90
    assert config.results_path == "results/legacy.jsonl"
    assert config.db_base_dir == str(tmp_path / "legacy-runs")
    assert config.injection_similarity_threshold == 0.75
    assert config.n_bootstrap == 500
    assert config.bootstrap_seed == 17
    assert config.detection == {"threshold": 0.8}
    assert config.btcr_criteria == {"benign": "email_draft"}
    assert config.extra == {"legacy": True}
    assert config.reset_conditions is None
    assert ExperimentRunner(config).config is config


def test_reset_conditions_expand_factorial_exactly_threefold(tmp_path):
    legacy = ExperimentRunner(_config(tmp_path))._enumerate_conditions()
    reset_runner = ExperimentRunner(
        _config(tmp_path, reset_conditions=["C0", "C1", "C2"])
    )
    reset = reset_runner._enumerate_conditions()

    assert len(reset) == len(legacy) * 3
    assert {condition["reset_condition"] for condition in reset} == {
        "C0", "C1", "C2"
    }

    same_base = [
        condition for condition in reset
        if condition["attack"]["name"] == "a0"
        and condition["defense"]["name"] == "d0"
        and condition["model"]["name"] == "m0"
    ]
    assert len({reset_runner._get_condition_id(condition) for condition in same_base}) == 3


def test_reset_condition_execution_dispatches_to_integrated_runner(tmp_path):
    runner = ExperimentRunner(
        _config(tmp_path, reset_conditions=["C0", "C1", "C2"])
    )
    condition = runner._enumerate_conditions()[0]
    expected = _reset_result(condition)

    with patch.object(runner, "_run_single_impl", return_value=expected) as run_impl:
        result = runner._run_single(condition, "integrated-reset-run")

    assert result is expected
    run_impl.assert_called_once()


def test_invalid_direct_reset_condition_fails_before_runtime_construction(tmp_path):
    runner = ExperimentRunner(_config(tmp_path))
    condition = runner._enumerate_conditions()[0] | {"reset_condition": "C9"}

    with (
        patch.object(runner, "_run_single_impl") as run_impl,
        patch.object(runner, "_build_model") as build_model,
        patch.object(runner, "_build_attack") as build_attack,
        patch.object(runner.state_isolator, "create_fresh_state") as create_state,
    ):
        with pytest.raises(ValueError, match="C9"):
            runner._run_single(condition, "invalid-reset-run")

    run_impl.assert_not_called()
    build_model.assert_not_called()
    build_attack.assert_not_called()
    create_state.assert_not_called()


def test_legacy_condition_remains_executable(tmp_path):
    runner = ExperimentRunner(_config(tmp_path))
    condition = runner._enumerate_conditions()[0]
    expected = RunResult(**_result_dict(condition))

    with patch.object(runner, "_run_single_impl", return_value=expected) as run_impl:
        result = runner._run_single(condition, "legacy-run")

    assert result is expected
    run_impl.assert_called_once()


@pytest.mark.parametrize(
    ("reset_conditions", "message"),
    [
        ([], "must not be empty"),
        (["C0", "C0"], "must not contain duplicates"),
        (["C0", "C9"], "Invalid reset condition"),
    ],
)
def test_invalid_explicit_reset_conditions_fail_early(
    tmp_path, reset_conditions, message
):
    raw = _raw_config(reset_marker=True, reset_conditions=reset_conditions)
    assert any(message in error for error in validate_config(raw))

    with pytest.raises(ValueError, match=message):
        ExperimentRunner(_config(tmp_path, reset_conditions=reset_conditions))


def test_reset_with_bedrock_fails_before_model_construction(tmp_path):
    bedrock_models = [{"provider": "bedrock", "model_name": "test-model"}]
    raw = _raw_config(
        reset_marker=True,
        reset_conditions=["C0", "C1", "C2"],
        provider="bedrock",
    )
    assert any("do not support Bedrock" in error for error in validate_config(raw))

    with patch.object(ExperimentRunner, "_build_model") as build_model:
        with pytest.raises(RuntimeError, match="do not support Bedrock"):
            ExperimentRunner(
                _config(
                    tmp_path,
                    reset_conditions=["C0", "C1", "C2"],
                    models=bedrock_models,
                )
            )
    build_model.assert_not_called()


def test_direct_reset_bedrock_condition_fails_before_runtime_construction(tmp_path):
    runner = ExperimentRunner(_config(
        tmp_path,
        models=[{"provider": "bedrock", "model_name": "test-model"}],
    ))
    condition = runner._enumerate_conditions()[0] | {"reset_condition": "C1"}

    with (
        patch.object(runner, "_run_single_impl") as run_impl,
        patch.object(runner, "_build_model") as build_model,
        patch.object(runner, "_build_attack") as build_attack,
        patch.object(runner.state_isolator, "create_fresh_state") as create_state,
    ):
        with pytest.raises(RuntimeError, match="do not support Bedrock"):
            runner._run_single(condition, "direct-reset-bedrock")

    run_impl.assert_not_called()
    build_model.assert_not_called()
    build_attack.assert_not_called()
    create_state.assert_not_called()


def test_legacy_bedrock_remains_valid(tmp_path):
    raw = _raw_config(provider="bedrock")
    assert "reset_conditions" not in raw
    assert validate_config(raw) == []

    runner = ExperimentRunner(_config(
        tmp_path,
        models=[{"provider": "bedrock", "model_name": "test-model"}],
    ))
    assert runner.config.reset_conditions is None
    assert "reset_condition" not in runner._enumerate_conditions()[0]


def test_load_config_places_valid_reset_conditions_in_dataclass_field(tmp_path):
    config = _load_yaml(
        tmp_path,
        _yaml_config(reset_block="reset_conditions: [C0, C1, C2]"),
    )

    assert config.reset_conditions == ["C0", "C1", "C2"]
    assert "reset_conditions" not in config.extra


def test_yaml_null_and_programmatic_none_are_both_legacy_mode(tmp_path):
    yaml_config = _load_yaml(
        tmp_path,
        _yaml_config(reset_block="reset_conditions: null", provider="bedrock"),
    )
    programmatic_config = _config(
        tmp_path,
        reset_conditions=None,
        models=[{"provider": "bedrock", "model_name": "test-model"}],
    )

    for config in (yaml_config, programmatic_config):
        runner = ExperimentRunner(config)
        assert config.reset_conditions is None
        assert "reset_conditions" not in config.extra
        assert "reset_condition" not in runner._enumerate_conditions()[0]


@pytest.mark.parametrize(
    ("reset_block", "provider", "message"),
    [
        ("reset_conditions: [C0, C9]", "ollama", "Invalid reset condition"),
        ("reset_conditions: [C0, C0]", "ollama", "must not contain duplicates"),
        ("reset_conditions: [C0]", "bedrock", "do not support Bedrock"),
    ],
)
def test_load_config_rejects_invalid_reset_yaml(
    tmp_path, reset_block, provider, message
):
    with pytest.raises(ValueError, match=message):
        _load_yaml(
            tmp_path,
            _yaml_config(reset_block=reset_block, provider=provider),
        )


def test_load_config_accepts_legacy_bedrock_yaml(tmp_path):
    config = _load_yaml(tmp_path, _yaml_config(provider="bedrock"))

    assert config.reset_conditions is None
    assert "reset_conditions" not in config.extra
    assert ExperimentRunner(config).config is config


@pytest.mark.parametrize("file_format", ["json", "jsonl"])
def test_old_runresult_files_load_with_schema_defaults(tmp_path, file_format):
    runner = ExperimentRunner(_config(tmp_path))
    condition = runner._enumerate_conditions()[0]
    old_result = _result_dict(condition)
    path = tmp_path / f"old-results.{file_format}"
    if file_format == "json":
        path.write_text(json.dumps([old_result]))
    else:
        path.write_text(json.dumps(old_result) + "\n")

    loaded = runner.load_partial_results(str(path))

    assert len(loaded) == 1
    assert loaded[0].reset_condition is None
    assert loaded[0].db_identity is None
    assert loaded[0].reset_valid is None
    assert loaded[0].session_threads == []
    assert loaded[0].reset_boundaries == []
    assert loaded[0].reset_invalid_reasons == []
    assert loaded[0].withdrawn_source_ids == []
    assert loaded[0].email_records == []


@pytest.mark.parametrize("file_format", ["json", "jsonl"])
def test_reset_runresult_round_trip_preserves_nested_lifecycle_fields(
    tmp_path, file_format
):
    runner = ExperimentRunner(
        _config(tmp_path, reset_conditions=["C0", "C1", "C2"])
    )
    result = _reset_result(runner._enumerate_conditions()[1])
    path = tmp_path / f"reset-results.{file_format}"
    if file_format == "json":
        runner.save_results([result], str(path))
    else:
        runner._append_result_to_jsonl(result, str(path))

    restored = runner.load_partial_results(str(path))[0]

    assert asdict(restored) == asdict(result)
    assert restored.reset_boundaries[0]["mutation"] == {
        "canonical_memory_clear_attempted": False,
        "canonical_rows_deleted": None,
    }


def test_runresult_collection_defaults_are_not_shared():
    condition = {
        "attack": {"type": "no_attack"},
        "defense": {"type": "none"},
        "model": {"provider": "ollama", "model_name": "qwen3:8b"},
    }
    first = RunResult(**_result_dict(condition))
    second = RunResult(**_result_dict(condition))

    first.session_threads.append({"thread_id": "only-first"})
    first.reset_boundaries.append({"boundary_index": 0})
    first.email_records.append({"operation": "draft"})

    assert second.session_threads == []
    assert second.reset_boundaries == []
    assert second.email_records == []
