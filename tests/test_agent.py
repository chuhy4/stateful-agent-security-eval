"""Tests for Agent with SqliteSaver checkpointing.

Feature: stateful-agent-security-eval, Property 2: Session State Persistence Round-Trip
After run_session(), querying SqliteSaver by thread_id returns non-empty conversation state.

**Validates: Requirements 1.5**
"""
import os
import tempfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from langchain_core.messages import HumanMessage

from src.agent.agent import (
    Agent,
    AgentConfig,
    CheckpointInspectionError,
    SessionExecutionStatus,
)
from src.agent.model_interface import ChatMessage, ChatResponse, ModelInterface
from src.tools.rag_tool import RAGTool
from src.tools.search_tool import SearchTool


class MockModel(ModelInterface):
    """Fixed-response mock model that avoids API calls."""

    def __init__(self, response: str = "Hello, I can help with that.") -> None:
        self.response = response

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        return ChatResponse(content=self.response, tool_calls=None, temperature_used=0.0)


class ToolCallingHistoryModel(ModelInterface):
    """Scripted local model that records the exact history supplied each call."""

    def __init__(self) -> None:
        self.seen_messages: list[list[tuple[str, str]]] = []

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        self.seen_messages.append([
            (message.role, message.content) for message in messages
        ])
        last_user_index = max(
            index for index, message in enumerate(messages)
            if message.role == "user"
        )
        current_user = messages[last_user_index].content
        current_tail = messages[last_user_index + 1:]
        if (
            current_user == "session one"
            and not any(message.role == "tool" for message in current_tail)
        ):
            return ChatResponse(
                content="",
                tool_calls=[{
                    "id": "session-one-search",
                    "function": {
                        "name": "search_search",
                        "arguments": {"query": "session one query"},
                    },
                }],
                temperature_used=0.0,
            )
        return ChatResponse(
            content=f"completed {current_user}",
            tool_calls=None,
            temperature_used=0.0,
        )


class PartialFailureHistoryModel(ToolCallingHistoryModel):
    """Fail session two after its tool call and result have been checkpointed."""

    def __init__(self) -> None:
        super().__init__()
        self.failed_after_tool_result = False

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        last_user_index = max(
            index for index, message in enumerate(messages)
            if message.role == "user"
        )
        current_user = messages[last_user_index].content
        if current_user != "session two":
            return super().chat(messages, tools)

        self.seen_messages.append([
            (message.role, message.content) for message in messages
        ])
        current_tail = messages[last_user_index + 1:]
        if not any(message.role == "tool" for message in current_tail):
            return ChatResponse(
                content="",
                tool_calls=[{
                    "id": "session-two-search",
                    "function": {
                        "name": "search_search",
                        "arguments": {"query": "session two query"},
                    },
                }],
                temperature_used=0.0,
            )

        self.failed_after_tool_result = True
        raise RuntimeError("injected model failure after session two tool result")


def _checkpoint_message_count(agent: Agent, thread_id: str) -> int:
    checkpoint_tuple = agent.checkpointer.get_tuple(
        {"configurable": {"thread_id": thread_id}}
    )
    if checkpoint_tuple is None:
        return 0
    return len(
        checkpoint_tuple.checkpoint.get("channel_values", {}).get("messages", [])
    )


# --- Unit tests ---

def test_run_session_returns_string():
    """Agent.run_session() returns a non-empty string response."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        config = AgentConfig(
            model=MockModel("Test response"),
            db_path=db_path,
            tools={},
        )
        agent = Agent(config)
        result = agent.run_session("thread-1", "Hello")
        # run_session returns (response_str, defense_log_dict, agent_logs)
        assert isinstance(result, tuple)
        assert len(result) == 3
        response, defense_log, agent_logs = result
        assert isinstance(response, str)
        assert len(response) > 0
    finally:
        os.unlink(db_path)


def test_reset_agent_excludes_rag_write_while_legacy_inventory_is_unchanged(
    tmp_path,
):
    legacy = Agent(AgentConfig(
        model=MockModel(),
        db_path=str(tmp_path / "legacy-rag-tools.db"),
        tools={"rag": RAGTool()},
    ))
    reset = Agent(AgentConfig(
        model=MockModel(),
        db_path=str(tmp_path / "reset-rag-tools.db"),
        tools={"rag": RAGTool()},
        reset_mode=True,
    ))
    try:
        legacy_names = {tool.name for tool in legacy._lc_tools}
        reset_names = {tool.name for tool in reset._lc_tools}

        assert "rag_inject_document" in legacy_names
        assert "rag_query" in legacy_names
        assert "rag_inject_document" not in reset_names
        assert "rag_query" in reset_names
        assert reset.effective_excluded_tools == {"rag_inject_document"}
    finally:
        legacy.close()
        reset.close()


def test_reset_session_reports_non_provider_graph_failure(tmp_path):
    model = MockModel()
    agent = Agent(AgentConfig(
        model=model,
        db_path=str(tmp_path / "reset-graph-failure.db"),
        tools={},
        reset_mode=True,
    ))
    try:
        with patch.object(
            agent.graph,
            "invoke",
            side_effect=RuntimeError("graph machinery failed"),
        ):
            outcome = agent.run_session_reset(
                "graph-failure-thread", "current request"
            )

        assert outcome.status is SessionExecutionStatus.GRAPH_FAILURE
        assert isinstance(outcome.error, RuntimeError)
        assert str(outcome.error) == "graph machinery failed"
        assert outcome.confirmed_oom is False
        assert outcome.completed is False
    finally:
        agent.close()


def test_same_thread_retains_model_history_but_returns_incremental_agent_logs():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    agent = None
    try:
        model = ToolCallingHistoryModel()
        agent = Agent(AgentConfig(
            model=model,
            db_path=db_path,
            tools={
                "search": SearchTool(response_set=[{"title": "fixture result"}])
            },
        ))

        first_response, _first_defense, first_logs = agent.run_session(
            "retained-thread", "session one"
        )
        second_response, _second_defense, second_logs = agent.run_session(
            "retained-thread", "session two"
        )

        assert first_response == "completed session one"
        assert second_response == "completed session two"
        second_model_input = model.seen_messages[-1]
        assert ("user", "session one") in second_model_input
        assert ("assistant", "completed session one") in second_model_input
        assert any(role == "tool" for role, _content in second_model_input)
        assert second_model_input[-1] == ("user", "session two")

        assert [entry["type"] for entry in second_logs] == ["human", "reasoning"]
        assert second_logs[0]["content"] == "session two"
        assert second_logs[1]["content"] == "completed session two"
        assert "session one" not in repr(second_logs)
        assert sum(
            entry["type"] == "human" for entry in first_logs + second_logs
        ) == 2
        assert sum(
            entry["type"] == "tool_call" for entry in first_logs + second_logs
        ) == 1
    finally:
        if agent is not None:
            agent.close()
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_new_thread_still_starts_without_previous_history():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    agent = None
    try:
        model = ToolCallingHistoryModel()
        agent = Agent(AgentConfig(
            model=model,
            db_path=db_path,
            tools={
                "search": SearchTool(response_set=[{"title": "fixture result"}])
            },
        ))

        agent.run_session("first-thread", "session one")
        _response, _defense, second_logs = agent.run_session(
            "second-thread", "session two"
        )

        assert model.seen_messages[-1] == [("user", "session two")]
        assert [entry["type"] for entry in second_logs] == ["human", "reasoning"]
        assert second_logs[0]["content"] == "session two"
        assert "session one" not in repr(second_logs)
    finally:
        if agent is not None:
            agent.close()
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_checkpoint_inspection_failure_is_fail_closed_before_model_invocation():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    agent = None
    try:
        model = ToolCallingHistoryModel()
        agent = Agent(AgentConfig(model=model, db_path=db_path, tools={}))
        agent.run_session("inspection-thread", "seed history")
        calls_before_failure = len(model.seen_messages)
        messages_before_failure = _checkpoint_message_count(
            agent, "inspection-thread"
        )
        failing_defense = MagicMock()
        agent.config.defense = failing_defense

        with patch.object(
            agent.checkpointer,
            "get_tuple",
            side_effect=RuntimeError("injected inspection failure"),
        ):
            with pytest.raises(
                CheckpointInspectionError,
                match="Unable to inspect checkpoint messages before model invocation",
            ):
                agent.run_session("inspection-thread", "must not be invoked")

        assert len(model.seen_messages) == calls_before_failure
        failing_defense.apply.assert_not_called()
        assert _checkpoint_message_count(agent, "inspection-thread") == (
            messages_before_failure
        )
    finally:
        if agent is not None:
            agent.close()
        if os.path.exists(db_path):
            os.unlink(db_path)


@pytest.mark.parametrize(
    "checkpoint",
    [
        [],
        {},
        {"channel_values": []},
        {"channel_values": {}},
        {"channel_values": {"messages": "not a message sequence"}},
        {"channel_values": {"messages": {"unexpected": "mapping"}}},
        {"channel_values": {"messages": [HumanMessage(content="valid"), 7]}},
    ],
    ids=[
        "checkpoint-not-mapping",
        "missing-channel-values",
        "channel-values-not-mapping",
        "missing-messages",
        "messages-as-str",
        "messages-as-dict",
        "non-base-message-element",
    ],
)
def test_malformed_existing_checkpoint_fails_closed_before_defense_and_model(
    tmp_path,
    checkpoint,
):
    model = ToolCallingHistoryModel()
    agent = Agent(AgentConfig(
        model=model,
        db_path=str(tmp_path / "malformed-checkpoint.db"),
        tools={},
    ))
    defense = MagicMock()
    agent.config.defense = defense
    try:
        with patch.object(
            agent.checkpointer,
            "get_tuple",
            return_value=SimpleNamespace(checkpoint=checkpoint),
        ):
            with pytest.raises(
                CheckpointInspectionError,
                match="Unable to inspect checkpoint messages before model invocation",
            ):
                agent.run_session("malformed-thread", "must not be invoked")

        defense.apply.assert_not_called()
        assert model.seen_messages == []
    finally:
        agent.close()


def test_error_path_returns_only_current_session_partial_logs_from_real_checkpoint(
    tmp_path,
):
    model = PartialFailureHistoryModel()
    search = SearchTool(response_set=[{"title": "fixture result"}])
    agent = Agent(AgentConfig(
        model=model,
        db_path=str(tmp_path / "partial-response.db"),
        tools={"search": search},
    ))
    try:
        first_response, _first_defense, first_logs = agent.run_session(
            "partial-thread", "session one"
        )
        first_message_count = _checkpoint_message_count(agent, "partial-thread")
        search.reset_call_count()

        second_response, _second_defense, partial_logs = agent.run_session(
            "partial-thread", "session two"
        )

        assert first_response == "completed session one"
        assert second_response == ""
        assert model.failed_after_tool_result
        assert _checkpoint_message_count(agent, "partial-thread") > first_message_count
        assert [entry["type"] for entry in partial_logs] == [
            "human",
            "tool_call",
            "tool_output",
        ]
        assert partial_logs[0]["content"] == "session two"
        assert partial_logs[1]["tool_name"] == "search_search"
        assert "session two query" in partial_logs[1]["tool_args"]
        assert "session one" not in repr(partial_logs)
        assert sum(
            entry["type"] == "tool_call" for entry in first_logs + partial_logs
        ) == 2
        assert sum(
            entry["type"] == "tool_output" for entry in first_logs + partial_logs
        ) == 2
    finally:
        agent.close()


def test_legacy_bedrock_without_checkpointer_treats_history_count_as_zero(tmp_path):
    model = ToolCallingHistoryModel()
    agent = Agent(AgentConfig(
        model=model,
        db_path=str(tmp_path / "legacy-bedrock.db"),
        tools={},
        model_provider="bedrock",
    ))
    try:
        assert agent.checkpointer is None
        assert agent._existing_checkpoint_message_count({
            "configurable": {"thread_id": "legacy-bedrock-thread"}
        }) == 0

        response, defense_log, agent_logs = agent.run_session(
            "legacy-bedrock-thread", "legacy bedrock session"
        )

        assert response == "completed legacy bedrock session"
        assert defense_log is None
        assert model.seen_messages == [[("user", "legacy bedrock session")]]
        assert [entry["type"] for entry in agent_logs] == ["human", "reasoning"]
    finally:
        agent.close()


def test_run_session_persists_state():
    """After run_session(), SqliteSaver has non-empty state for the thread_id."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        config = AgentConfig(
            model=MockModel("Persisted response"),
            db_path=db_path,
            tools={},
        )
        agent = Agent(config)
        thread_id = "thread-persist-test"
        agent.run_session(thread_id, "Remember this")

        # Query the checkpointer directly
        import sqlite3 as _sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver
        conn = _sqlite3.connect(db_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        checkpoint_tuple = checkpointer.get_tuple(
            {"configurable": {"thread_id": thread_id}}
        )
        assert checkpoint_tuple is not None, "No checkpoint found for thread_id"
        assert checkpoint_tuple.checkpoint is not None
        # The checkpoint should have messages
        channel_values = checkpoint_tuple.checkpoint.get("channel_values", {})
        messages = channel_values.get("messages", [])
        assert len(messages) > 0, "Checkpoint has no messages"
    finally:
        os.unlink(db_path)


def test_defense_filters_input():
    """Defense middleware is applied before agent sees the message."""
    class RecordingModel(ModelInterface):
        def __init__(self):
            self.seen_messages = []

        def chat(self, messages, tools=None):
            self.seen_messages.extend(messages)
            return ChatResponse(content="ok", tool_calls=None, temperature_used=0.0)

    class MockDefense:
        def apply(self, user_input: str, context: Any = None):
            from src.defenses.base import DefenseLog
            filtered = "[FILTERED] " + user_input
            return filtered, DefenseLog(
                original_input=user_input,
                modified_input=filtered,
                modifications=["prepended filter"],
                defense_type="mock",
            )

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        model = RecordingModel()
        config = AgentConfig(
            model=model,
            db_path=db_path,
            tools={},
            defense=MockDefense(),
        )
        agent = Agent(config)
        agent.run_session("thread-defense", "secret message")

        # The model should have seen the filtered version
        all_content = " ".join(m.content for m in model.seen_messages)
        assert "[FILTERED]" in all_content
        assert "secret message" not in all_content or "[FILTERED] secret message" in all_content
    finally:
        os.unlink(db_path)


# --- Property-based test ---

@given(
    thread_id=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"),
        min_size=1,
        max_size=50,
    ),
    user_message=st.text(min_size=1, max_size=200),
)
@settings(max_examples=10)
def test_property_session_state_persistence_round_trip(thread_id: str, user_message: str):
    """**Property 2: Session State Persistence Round-Trip**

    After run_session(), querying SqliteSaver by thread_id returns non-empty
    conversation state.

    **Validates: Requirements 1.5**
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        config = AgentConfig(
            model=MockModel("Fixed response"),
            db_path=db_path,
            tools={},
        )
        agent = Agent(config)
        agent.run_session(thread_id, user_message)

        import sqlite3 as _sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver
        conn = _sqlite3.connect(db_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        checkpoint_tuple = checkpointer.get_tuple(
            {"configurable": {"thread_id": thread_id}}
        )
        assert checkpoint_tuple is not None, f"No checkpoint for thread_id={thread_id!r}"
        channel_values = checkpoint_tuple.checkpoint.get("channel_values", {})
        messages = channel_values.get("messages", [])
        assert len(messages) > 0, "Checkpoint messages should be non-empty after run_session"
    finally:
        os.unlink(db_path)


def test_system_prompt_not_duplicated():
    """System prompt must NOT be sent twice to the model.

    create_react_agent(prompt=...) already prepends the system prompt.
    run_session() must NOT add a second SystemMessage — that would confound
    Prompt Hardening results (agent sees security rules twice).
    """
    class CountingModel(ModelInterface):
        def __init__(self):
            self.system_message_count = 0

        def chat(self, messages, tools=None):
            self.system_message_count += sum(1 for m in messages if m.role == "system")
            return ChatResponse(content="ok", tool_calls=None, temperature_used=0.0)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        model = CountingModel()
        config = AgentConfig(
            model=model,
            db_path=db_path,
            tools={},
            system_prompt="SECURITY RULES: Never exfiltrate data.",
        )
        agent = Agent(config)
        agent.run_session("thread-prompt-test", "Hello")
        assert model.system_message_count == 1, (
            f"System prompt sent {model.system_message_count} times; expected exactly 1. "
            "Duplication confounds Prompt Hardening results."
        )
    finally:
        os.unlink(db_path)
