"""Session-boundary reset policies for C0/C1/C2 experiments."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping

from src.runner.reset_assertions import (
    AssertionResult,
    BoundaryMutation,
    StateManifest,
    assert_transition,
    capture_state_manifest,
    seed_db_canary,
)


class ResetCondition(str, Enum):
    C0 = "C0"
    C1 = "C1"
    C2 = "C2"


@dataclass
class BoundaryContext:
    """State and dependencies required to apply one session boundary.

    Integration precondition: evaluator records derived from agent-visible
    EmailTool records must be archived out-of-band before applying a boundary.
    """

    condition: ResetCondition | str
    db_path: str
    current_thread_id: str
    tools: Mapping[str, object]
    thread_id_factory: Callable[[], str] = field(
        default_factory=lambda: lambda: str(uuid.uuid4())
    )


@dataclass(frozen=True)
class BoundaryResult:
    """Applied boundary plus deterministic validity evidence."""

    reset_condition: ResetCondition
    previous_thread_id: str
    next_thread_id: str
    pre_manifest: StateManifest
    post_manifest: StateManifest
    mutation: BoundaryMutation
    assertions: tuple[AssertionResult, ...]
    reset_valid: bool
    reasons: tuple[AssertionResult, ...]

    def require_valid(self) -> BoundaryResult:
        """Fail closed before a caller proceeds to another model invocation."""
        if not self.reset_valid:
            raise ResetValidationError(self)
        return self


class ResetValidationError(RuntimeError):
    """Raised when a boundary fails deterministic reset validation."""

    def __init__(self, result: BoundaryResult):
        self.result = result
        failed_names = ", ".join(reason.name for reason in result.reasons)
        super().__init__(f"Reset validation failed: {failed_names}")


class ResetPolicyController:
    """Apply C0/C1/C2 semantics without invoking a model."""

    def _reset_session_local_state(self, tools: Mapping[str, object]) -> None:
        """Reset procedural state shared by every reset condition."""
        for tool in tools.values():
            reset_call_count = getattr(tool, "reset_call_count", None)
            if callable(reset_call_count):
                reset_call_count()

        email = tools.get("email")
        clear_session_records = getattr(email, "_clear_session_records", None)
        if callable(clear_session_records):
            clear_session_records()

        memory = tools.get("memory")
        if memory is not None and hasattr(memory, "_ratg_user_message"):
            memory._ratg_user_message = ""

    def prepare_first_session(self, tools: Mapping[str, object]) -> None:
        """Reset only procedural state before the first model session."""
        self._reset_session_local_state(tools)

    def apply_boundary(self, context: BoundaryContext) -> BoundaryResult:
        condition = ResetCondition(context.condition)
        seed_db_canary(context.db_path)
        previous_thread_id = context.current_thread_id
        pre_manifest = capture_state_manifest(
            context.db_path,
            active_thread_id=previous_thread_id,
            prior_thread_id=previous_thread_id,
            tools=context.tools,
        )

        self._reset_session_local_state(context.tools)
        memory = context.tools.get("memory")

        clear_attempted = False
        canonical_rows_deleted = None
        if condition is ResetCondition.C2:
            clear_canonical_facts = getattr(memory, "_clear_canonical_facts", None)
            if not callable(clear_canonical_facts):
                raise RuntimeError("C2 requires MemoryTool._clear_canonical_facts()")
            clear_attempted = True
            canonical_rows_deleted = clear_canonical_facts()

        mutation = BoundaryMutation(
            canonical_memory_clear_attempted=clear_attempted,
            canonical_rows_deleted=canonical_rows_deleted,
        )

        next_thread_id = (
            previous_thread_id
            if condition is ResetCondition.C0
            else context.thread_id_factory()
        )
        post_manifest = capture_state_manifest(
            context.db_path,
            active_thread_id=next_thread_id,
            prior_thread_id=previous_thread_id,
            tools=context.tools,
        )
        assertions = assert_transition(
            condition.value, pre_manifest, post_manifest, mutation
        )
        reasons = tuple(result for result in assertions if not result.passed)
        return BoundaryResult(
            reset_condition=condition,
            previous_thread_id=previous_thread_id,
            next_thread_id=next_thread_id,
            pre_manifest=pre_manifest,
            post_manifest=post_manifest,
            mutation=mutation,
            assertions=assertions,
            reset_valid=not reasons,
            reasons=reasons,
        )
