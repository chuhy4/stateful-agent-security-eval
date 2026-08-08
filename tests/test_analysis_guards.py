"""Guards for legacy-only analysis and fixed-dimensional plots."""
from __future__ import annotations

import json

import matplotlib.axes
import pytest

from scripts.analyze_sandbox_inversion import (
    SANDBOX_RESET_POLICY_UNSUPPORTED,
)
from scripts.analyze_sandbox_inversion import (
    load_results as load_sandbox_results,
)
from src.analysis.plots import (
    FACTORIAL_GRID_RESET_UNSUPPORTED,
    plot_asr_by_condition,
    plot_asr_vs_btcr,
    plot_btcr_by_condition,
    plot_factorial_grid,
)
from src.analysis.tables import render_stats_table


def _legacy_record():
    return {
        "condition": {
            "attack": {"type": "delayed_trigger"},
            "defense": {"type": "none", "name": "no_defense"},
            "model": {"model_name": "qwen3:8b"},
        },
        "error": None,
    }


def _reset_record(*, error=None):
    record = _legacy_record()
    record["condition"] = dict(record["condition"], reset_condition="C0")
    record["reset_condition"] = "C0"
    record["reset_valid"] = error is None
    record["error"] = error
    return record


@pytest.mark.parametrize(
    "records",
    [
        [_reset_record()],
        [_reset_record(error="infrastructure")],
        [_legacy_record(), _reset_record()],
    ],
    ids=["success", "error", "mixed"],
)
def test_sandbox_analysis_rejects_reset_before_error_filtering(tmp_path, records):
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))

    with pytest.raises(ValueError) as exc_info:
        load_sandbox_results(str(path))
    assert str(exc_info.value) == SANDBOX_RESET_POLICY_UNSUPPORTED


def test_factorial_grid_rejects_reset_fourth_dimension(tmp_path):
    stats = {
        "no_attack": {
            "qwen3:8b": {
                "no_defense": {
                    "C0": {
                        "asr": {
                            "point_estimate": 0.0,
                            "lower": 0.0,
                            "upper": 0.0,
                        }
                    }
                }
            }
        }
    }

    with pytest.raises(ValueError) as exc_info:
        plot_factorial_grid(stats, str(tmp_path / "grid.png"))
    assert str(exc_info.value) == FACTORIAL_GRID_RESET_UNSUPPORTED


def test_factorial_grid_legacy_structure_still_renders(tmp_path):
    output = tmp_path / "legacy-grid.png"
    stats = {
        "no_attack": {
            "qwen3:8b": {
                "no_defense": {
                    "asr": {
                        "point_estimate": 0.0,
                        "lower": 0.0,
                        "upper": 0.0,
                    }
                }
            }
        }
    }
    plot_factorial_grid(stats, str(output))
    assert output.exists()


@pytest.mark.parametrize(
    "plotter",
    [plot_asr_by_condition, plot_btcr_by_condition, plot_asr_vs_btcr],
)
def test_flat_plots_preserve_three_reset_labels(tmp_path, monkeypatch, plotter):
    keys = [
        "attack=no_attack,defense=none,model=test,reset_condition=C0",
        "attack=no_attack,defense=none,model=test,reset_condition=C1",
        "attack=no_attack,defense=none,model=test,reset_condition=C2",
    ]
    stats = {
        key: {
            "asr": {"point_estimate": 0.0, "lower": 0.0, "upper": 0.0},
            "btcr": {"point_estimate": 1.0, "lower": 1.0, "upper": 1.0},
        }
        for key in keys
    }
    observed_labels: list[str] = []
    original_xticklabels = matplotlib.axes.Axes.set_xticklabels
    original_annotate = matplotlib.axes.Axes.annotate

    def capture_xticklabels(self, labels, *args, **kwargs):
        observed_labels.extend(str(label) for label in labels)
        return original_xticklabels(self, labels, *args, **kwargs)

    def capture_annotate(self, text, *args, **kwargs):
        observed_labels.append(str(text))
        return original_annotate(self, text, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "set_xticklabels", capture_xticklabels)
    monkeypatch.setattr(matplotlib.axes.Axes, "annotate", capture_annotate)

    plotter(stats, str(tmp_path / f"{plotter.__name__}.png"))

    assert set(keys).issubset(observed_labels)


def test_stats_table_preserves_distinct_reset_labels():
    keys = [
        "attack=no_attack,defense=none,model=test,reset_condition=C0",
        "attack=no_attack,defense=none,model=test,reset_condition=C1",
        "attack=no_attack,defense=none,model=test,reset_condition=C2",
    ]
    stats = {
        key: {
            "asr": {"point_estimate": 0.0, "lower": 0.0, "upper": 0.0},
            "btcr": {"point_estimate": 1.0, "lower": 1.0, "upper": 1.0},
        }
        for key in keys
    }
    rendered = render_stats_table(stats, [])
    for value in ("C0", "C1", "C2"):
        assert rf"reset\_condition={value}" in rendered
