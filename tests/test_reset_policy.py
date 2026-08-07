"""Deterministic C0/C1/C2 reset-policy tests with no external model calls."""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from src.agent.agent import Agent, AgentConfig
from src.agent.model_interface import ChatMessage, ChatResponse, ModelInterface
from src.runner.reset_assertions import (
    BoundaryMutation,
    assert_transition,
    capture_state_manifest,
)
from src.runner.reset_policy import (
    BoundaryContext,
    ResetCondition,
    ResetPolicyController,
    ResetValidationError,
)
from src.tools.calendar_tool import CalendarEntry, CalendarTool
from src.tools.email_tool import EmailTool
from src.tools.memory_tool import MemoryTool
from src.tools.rag_tool import RAGDocument, RAGTool
from src.tools.search_tool import SearchTool


class MockModel(ModelInterface):
    """Local fixed-response model; never performs network or provider calls."""

    def __init__(self) -> None:
        self.call_count = 0

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        self.call_count += 1
        return ChatResponse(
            content="mock response", tool_calls=None, temperature_used=0.0
        )


@pytest.fixture
def seeded_runtime(tmp_path):
    db_path = str(tmp_path / "reset-policy.db")
    tools = {
        "email": EmailTool(),
        "memory": MemoryTool(db_path=db_path),
        "rag": RAGTool(corpus=[RAGDocument(doc_id="source-1", content="fixture")]),
        "calendar": CalendarTool(entries=[
            CalendarEntry(id="event-1", title="Fixture", start="2026-01-01", end="2026-01-01")
        ]),
        "search": SearchTool(response_set=[{"title": "fixture"}]),
    }
    model = MockModel()
    agent = Agent(AgentConfig(model=model, db_path=db_path, tools=tools))
    thread_id = "injection-thread"
    agent.run_session(thread_id, "seed checkpoint state")
    tools["memory"].save_fact("retained-rule", "canonical-memory-value")
    tools["memory"]._ratg_user_message = "previous session request"
    tools["email"].draft_email(["a@example.com"], "subject", "body")
    tools["email"].send_email(["a@example.com"], "subject", "body")
    tools["rag"].query("fixture")
    tools["calendar"].get_schedule("2026-01-01")
    tools["search"].search("fixture")

    try:
        yield db_path, tools, model, agent, thread_id
    finally:
        agent.close()
        tools["memory"].close()


def _apply(
    seeded_runtime, condition: ResetCondition | str, next_thread: str = "next-thread"
):
    db_path, tools, _model, _agent, thread_id = seeded_runtime
    return ResetPolicyController().apply_boundary(BoundaryContext(
        condition=condition,
        db_path=db_path,
        current_thread_id=thread_id,
        tools=tools,
        thread_id_factory=lambda: next_thread,
    ))


def test_c0_conversation_and_memory_retained(seeded_runtime):
    result = _apply(seeded_runtime, ResetCondition.C0)

    assert result.reset_valid
    assert result.next_thread_id == result.previous_thread_id
    assert result.post_manifest.checkpoint_table_present
    assert result.post_manifest.active_checkpoint_reachable
    assert result.post_manifest.prior_checkpoint_physical_rows > 0
    assert result.post_manifest.canonical_memory == result.pre_manifest.canonical_memory
    assert not result.mutation.canonical_memory_clear_attempted
    assert result.mutation.canonical_rows_deleted is None


def test_c1_conversation_inaccessible_and_memory_retained(seeded_runtime):
    result = _apply(seeded_runtime, ResetCondition.C1)

    assert result.reset_valid
    assert result.next_thread_id != result.previous_thread_id
    assert not result.post_manifest.active_checkpoint_reachable
    assert result.post_manifest.prior_checkpoint_physical_rows > 0
    assert result.post_manifest.canonical_memory == result.pre_manifest.canonical_memory
    assert not result.mutation.canonical_memory_clear_attempted
    assert result.mutation.canonical_rows_deleted is None


def test_c2_conversation_inaccessible_and_memory_cleared(seeded_runtime):
    result = _apply(seeded_runtime, ResetCondition.C2)

    assert result.reset_valid
    assert result.next_thread_id != result.previous_thread_id
    assert not result.post_manifest.active_checkpoint_reachable
    assert result.post_manifest.prior_checkpoint_physical_rows > 0
    assert result.post_manifest.canonical_memory == ()
    assert result.mutation.canonical_memory_clear_attempted
    assert result.mutation.canonical_rows_deleted == 1


def test_c2_empty_memory_records_clear_operation_evidence(seeded_runtime):
    _db_path, tools, _model, _agent, _thread_id = seeded_runtime
    assert tools["memory"]._clear_canonical_facts() == 1

    result = _apply(seeded_runtime, ResetCondition.C2)

    assert result.pre_manifest.canonical_memory == ()
    assert result.post_manifest.canonical_memory == ()
    assert result.mutation.canonical_memory_clear_attempted
    assert result.mutation.canonical_rows_deleted == 0
    assert result.reset_valid


def test_reserved_db_canary_survives_c2(seeded_runtime):
    result = _apply(seeded_runtime, ResetCondition.C2)

    assert result.pre_manifest.db_canaries
    assert result.post_manifest.db_canaries == result.pre_manifest.db_canaries
    with sqlite3.connect(result.post_manifest.db_identity) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM reset_assertion_canaries"
        ).fetchone() == (1,)


def test_manifest_capture_is_deterministic(seeded_runtime):
    db_path, tools, _model, _agent, thread_id = seeded_runtime
    first = capture_state_manifest(db_path, thread_id, thread_id, tools)
    second = capture_state_manifest(db_path, thread_id, thread_id, tools)

    assert first == second


@pytest.mark.parametrize("condition", list(ResetCondition))
def test_procedural_state_resets_equally_across_conditions(
    seeded_runtime, condition
):
    result = _apply(seeded_runtime, condition)

    assert result.reset_valid
    assert all(
        value == 0
        for _tool_name, counters in result.post_manifest.procedural_counters
        for _counter_name, value in counters
    )
    assert result.post_manifest.email_record_count == 0
    assert result.post_manifest.session_local_values == (("memory._ratg_user_message", ""),)
    assert result.post_manifest.evaluator_log_lengths == result.pre_manifest.evaluator_log_lengths
    assert result.post_manifest.rag_document_ids == result.pre_manifest.rag_document_ids


def test_assertion_failure_is_structured_and_blocks_next_mock_invocation(
    seeded_runtime,
):
    db_path, tools, model, agent, thread_id = seeded_runtime
    calls_before_boundary = model.call_count
    result = ResetPolicyController().apply_boundary(BoundaryContext(
        condition=ResetCondition.C1,
        db_path=db_path,
        current_thread_id=thread_id,
        tools=tools,
        thread_id_factory=lambda: thread_id,
    ))

    with pytest.raises(ResetValidationError) as error:
        result.require_valid()
        agent.run_session(result.next_thread_id, "must not be invoked")

    assert not result.reset_valid
    assert error.value.result is result
    assert result.reasons
    failed = [assertion for assertion in result.assertions if not assertion.passed]
    assert any(assertion.name == "new_conversation_thread" for assertion in failed)
    assert all(assertion.reason for assertion in failed)
    assert result.reasons == tuple(failed)
    assert model.call_count == calls_before_boundary


def test_invalid_condition_fails_before_any_mutation(seeded_runtime):
    db_path, tools, _model, _agent, thread_id = seeded_runtime
    before = capture_state_manifest(db_path, thread_id, thread_id, tools)
    factory_called = False

    def forbidden_thread_factory():
        nonlocal factory_called
        factory_called = True
        return "must-not-be-created"

    with pytest.raises(ValueError):
        ResetPolicyController().apply_boundary(BoundaryContext(
            condition="C9",
            db_path=db_path,
            current_thread_id=thread_id,
            tools=tools,
            thread_id_factory=forbidden_thread_factory,
        ))

    after = capture_state_manifest(db_path, thread_id, thread_id, tools)
    assert after == before
    assert not factory_called


def test_string_condition_is_normalized_before_application(seeded_runtime):
    result = _apply(seeded_runtime, "C1")

    assert result.reset_condition is ResetCondition.C1
    assert result.reset_valid


def test_procedural_counter_inventory_change_is_detected(seeded_runtime):
    valid = _apply(seeded_runtime, ResetCondition.C0)
    altered_post = replace(
        valid.post_manifest,
        procedural_counters=valid.post_manifest.procedural_counters[1:],
    )

    assertions = assert_transition(
        "C0", valid.pre_manifest, altered_post, valid.mutation
    )

    inventory = next(
        assertion
        for assertion in assertions
        if assertion.name == "procedural_counter_inventory_preserved"
    )
    assert not inventory.passed
    assert inventory.reason


def test_assert_transition_rejects_unknown_condition(seeded_runtime):
    valid = _apply(seeded_runtime, ResetCondition.C0)
    with pytest.raises(ValueError, match="Unknown reset condition"):
        assert_transition(
            "C9",
            valid.pre_manifest,
            valid.post_manifest,
            BoundaryMutation(False, None),
        )
