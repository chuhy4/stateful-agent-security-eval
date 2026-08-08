"""Canonical condition and result identity for experiment analysis."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from itertools import product
from typing import TYPE_CHECKING

from src.runner.reset_policy import ResetCondition

if TYPE_CHECKING:
    from src.runner.config_loader import ExperimentConfig


class AnalysisIdentityError(ValueError):
    """Raised when an experiment result cannot be assigned a safe identity."""


class AnalysisMode(str, Enum):
    LEGACY = "legacy"
    RESET = "reset"


@dataclass(frozen=True)
class ConditionIdentity:
    display_key: str
    digest: str
    mode: AnalysisMode
    reset_condition: str | None


def _normalized_condition(condition: Mapping) -> dict:
    if not isinstance(condition, Mapping):
        raise AnalysisIdentityError("Result condition must be a mapping")

    normalized = dict(condition)
    if "reset_condition" in normalized:
        try:
            normalized["reset_condition"] = ResetCondition(
                normalized["reset_condition"]
            ).value
        except (TypeError, ValueError):
            raise AnalysisIdentityError(
                f"Invalid reset condition {normalized['reset_condition']!r}; "
                "expected one of C0, C1, C2"
            ) from None
    return normalized


def canonical_condition_key(condition: Mapping) -> str:
    """Return the stable human-readable key used by configs and reports."""
    normalized = _normalized_condition(condition)
    attack_cfg = normalized.get("attack", {})
    defense_cfg = normalized.get("defense", {})
    model_cfg = normalized.get("model", {})
    if not isinstance(attack_cfg, Mapping):
        attack_cfg = {}
    if not isinstance(defense_cfg, Mapping):
        defense_cfg = {}
    if not isinstance(model_cfg, Mapping):
        model_cfg = {}

    attack = attack_cfg.get("type", "unknown")
    defense = defense_cfg.get("name") or defense_cfg.get("type", "unknown")
    model = model_cfg.get("model_name", "unknown")
    key = f"attack={attack},defense={defense},model={model}"
    if "reset_condition" in normalized:
        key += f",reset_condition={normalized['reset_condition']}"
    return key


def stable_condition_digest(condition: Mapping) -> str:
    """Hash the complete normalized condition dict, including reset treatment."""
    normalized = _normalized_condition(condition)
    encoded = json.dumps(normalized, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_reset_run_index(
    run_index: object,
    runs_per_condition: int,
) -> int:
    """Return a valid reset experimental-slot index or fail closed."""
    if (
        isinstance(run_index, bool)
        or not isinstance(run_index, int)
        or run_index < 0
        or run_index >= runs_per_condition
    ):
        raise AnalysisIdentityError(
            "Reset run_index must be an integer in "
            f"[0, {runs_per_condition - 1}], got {run_index!r}"
        )
    return run_index


def validate_result_identity(
    record: Mapping,
    expected_mode: AnalysisMode | str,
) -> ConditionIdentity:
    """Validate top-level/condition reset identity without mutating the record."""
    try:
        mode = AnalysisMode(expected_mode)
    except (TypeError, ValueError):
        raise AnalysisIdentityError(
            f"Invalid expected analysis mode {expected_mode!r}"
        ) from None

    if not isinstance(record, Mapping):
        raise AnalysisIdentityError("Result record must be a mapping")
    condition = record.get("condition")
    if not isinstance(condition, Mapping):
        raise AnalysisIdentityError("Result record must contain a condition mapping")

    condition_has_reset = "reset_condition" in condition
    top_level_has_reset = "reset_condition" in record
    top_level_reset = record.get("reset_condition")

    if mode is AnalysisMode.LEGACY:
        if condition_has_reset or top_level_reset is not None:
            raise AnalysisIdentityError(
                "Legacy analysis received reset-policy result identity"
            )
        return ConditionIdentity(
            display_key=canonical_condition_key(condition),
            digest=stable_condition_digest(condition),
            mode=mode,
            reset_condition=None,
        )

    if not condition_has_reset:
        raise AnalysisIdentityError(
            "Reset result condition is missing reset_condition"
        )
    if not top_level_has_reset or top_level_reset is None:
        raise AnalysisIdentityError(
            "Reset result top-level reset_condition is missing"
        )
    try:
        condition_reset = ResetCondition(condition["reset_condition"]).value
        normalized_top_level = ResetCondition(top_level_reset).value
    except (TypeError, ValueError):
        raise AnalysisIdentityError(
            "Reset result contains an invalid reset_condition; expected C0, C1, or C2"
        ) from None
    if condition_reset != normalized_top_level:
        raise AnalysisIdentityError(
            "Reset result condition/top-level reset_condition mismatch: "
            f"{condition_reset!r} != {normalized_top_level!r}"
        )

    return ConditionIdentity(
        display_key=canonical_condition_key(condition),
        digest=stable_condition_digest(condition),
        mode=mode,
        reset_condition=condition_reset,
    )


def enumerate_expected_identities(
    config: ExperimentConfig,
) -> list[ConditionIdentity]:
    """Enumerate the complete configured factorial with collision validation."""
    if config.reset_conditions is None:
        combinations = (
            {
                "attack": attack,
                "defense": defense,
                "model": model,
            }
            for attack, defense, model in product(
                config.attacks,
                config.defenses,
                config.models,
            )
        )
        mode = AnalysisMode.LEGACY
    else:
        from src.runner.config_loader import normalize_reset_conditions

        reset_conditions = normalize_reset_conditions(config.reset_conditions)
        combinations = (
            {
                "attack": attack,
                "defense": defense,
                "model": model,
                "reset_condition": reset_condition,
            }
            for attack, defense, model, reset_condition in product(
                config.attacks,
                config.defenses,
                config.models,
                reset_conditions,
            )
        )
        mode = AnalysisMode.RESET

    identities: list[ConditionIdentity] = []
    display_to_digest: dict[str, str] = {}
    seen_digests: set[str] = set()
    for condition in combinations:
        identity = ConditionIdentity(
            display_key=canonical_condition_key(condition),
            digest=stable_condition_digest(condition),
            mode=mode,
            reset_condition=condition.get("reset_condition"),
        )
        if mode is AnalysisMode.RESET:
            previous_digest = display_to_digest.get(identity.display_key)
            if previous_digest is not None and previous_digest != identity.digest:
                raise AnalysisIdentityError(
                    "RESET_DISPLAY_KEY_COLLISION: distinct conditions map to "
                    f"{identity.display_key!r}"
                )
            if identity.digest in seen_digests:
                raise AnalysisIdentityError(
                    "RESET_EXPECTED_CONDITION_DUPLICATE: the reset factorial "
                    f"contains the same condition more than once: {identity.display_key}"
                )
        display_to_digest[identity.display_key] = identity.digest
        seen_digests.add(identity.digest)
        identities.append(identity)
    return identities
