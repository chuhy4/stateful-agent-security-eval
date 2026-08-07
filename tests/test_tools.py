"""
Property-based tests for instrumented tools.

Property 4: Tool Operation Logging Invariant (Task 2.6)
Property 3: Memory_Tool Cross-Session Round-Trip (Task 2.7)
Property 5: Tool Configured Data Retrieval (Task 2.8)
"""
import os
import sqlite3
import tempfile
import threading

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.tools.calendar_tool import CalendarEntry, CalendarTool
from src.tools.email_tool import EmailTool
from src.tools.memory_tool import MemoryTool
from src.tools.rag_tool import RAGDocument, RAGTool
from src.tools.search_tool import SearchTool
from tests.strategies import non_empty_text, user_ids


def test_memory_internal_lifecycle_helpers():
    """Canonical facts can be cleared without deleting reserved DB rows."""
    db_path = tempfile.mktemp(suffix=".db")
    tool = MemoryTool(db_path=db_path)
    try:
        tool.save_fact(key="visible", value="agent-visible")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO memory_facts (user_id, key, value, created_at) VALUES (?, ?, ?, ?)",
            ("__reset_probe__", "db_canary", "present", 0.0),
        )
        conn.commit()
        conn.close()

        assert tool._clear_canonical_facts() == 1
        assert tool.recall_fact("visible") == {"value": ""}

        check = sqlite3.connect(db_path)
        row = check.execute(
            "SELECT value FROM memory_facts WHERE user_id = ? AND key = ?",
            ("__reset_probe__", "db_canary"),
        ).fetchone()
        check.close()
        assert row == ("present",)

        active_conn = tool._get_conn()
        tool.close()
        with pytest.raises(sqlite3.ProgrammingError):
            active_conn.execute("SELECT 1")
    finally:
        tool.close()
        for suffix in ("", "-wal", "-shm"):
            path = db_path + suffix
            if os.path.exists(path):
                os.remove(path)


def test_memory_close_closes_quiescent_worker_connection():
    """Terminal close handles worker connections after all worker use has stopped."""
    db_path = tempfile.mktemp(suffix=".db")
    tool = MemoryTool(db_path=db_path)
    worker_state = {}

    def use_memory_from_worker():
        conn = tool._get_conn()
        conn.execute("SELECT 1").fetchone()
        worker_state["conn"] = conn

    try:
        worker = threading.Thread(target=use_memory_from_worker)
        worker.start()
        worker.join()
        assert not worker.is_alive()

        # close() is only supported once callers are quiescent; it is not a
        # concurrent runtime operation and the tool is not used after this call.
        tool.close()

        with pytest.raises(sqlite3.ProgrammingError):
            worker_state["conn"].execute("SELECT 1")
    finally:
        tool.close()
        for suffix in ("", "-wal", "-shm"):
            path = db_path + suffix
            if os.path.exists(path):
                os.remove(path)


def test_email_internal_session_clear_preserves_evaluator_log():
    """Session records clear independently of evaluator-only logs."""
    tool = EmailTool()
    tool.draft_email(["a@example.com"], "subject", "body")
    tool.send_email(["a@example.com"], "subject", "body")

    tool._clear_session_records()
    assert tool.get_emails() == []
    assert len(tool.get_log()) == 2


def test_email_reset_resets_send_counter():
    """A full reset restores the per-session send allowance."""
    tool = EmailTool()
    for index in range(3):
        tool.send_email(["a@example.com"], f"subject-{index}", "body")

    tool.reset()
    assert tool._send_call_count == 0
    assert tool.get_log() == []
    assert tool.send_email(["a@example.com"], "after-reset", "body")["status"] == "sent"


def test_rag_internal_remove_documents_is_not_an_agent_tool():
    """Internal source withdrawal removes only requested IDs and stays private."""
    tool = RAGTool(corpus=[
        RAGDocument(doc_id="malicious", content="bad"),
        RAGDocument(doc_id="benign", content="good"),
    ])

    assert tool._remove_documents({"malicious"}) == ["malicious"]
    assert [doc.doc_id for doc in tool.corpus] == ["benign"]
    agent_visible_methods = {
        name
        for name in dir(tool)
        if not name.startswith("_") and callable(getattr(tool, name))
    }
    assert "_remove_documents" not in agent_visible_methods


def test_internal_lifecycle_helpers_are_not_registered_as_agent_tools(tmp_path):
    """The real registration path excludes lifecycle and reset-only helpers."""
    from src.agent.agent import _make_lc_tools

    memory = MemoryTool(db_path=str(tmp_path / "registration.db"))
    try:
        registered_names = {
            tool.name
            for tool in _make_lc_tools({
                "memory": memory,
                "email": EmailTool(),
                "rag": RAGTool(),
            })
        }
    finally:
        memory.close()

    assert {
        "memory_close",
        "memory__clear_canonical_facts",
        "email__clear_session_records",
        "rag__remove_documents",
    }.isdisjoint(registered_names)

# ---------------------------------------------------------------------------
# Property 4: Tool Operation Logging Invariant
# Validates: Requirements 3.1, 3.3
# ---------------------------------------------------------------------------

operation_counts = st.integers(min_value=1, max_value=20)


@given(n=operation_counts)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_email_tool_logging_invariant(n):
    """**Validates: Requirements 3.1, 3.3**
    For N send_email calls, get_log() returns exactly N entries in
    chronological order.
    """
    tool = EmailTool()
    for i in range(n):
        tool.reset_call_count()  # reset governor between calls for logging invariant test
        tool.send_email(
            to=[f"user{i}@example.com"],
            subject=f"Subject {i}",
            body=f"Body {i}",
        )

    log = tool.get_log()
    assert len(log) == n

    # Timestamps must be monotonically non-decreasing
    for i in range(1, len(log)):
        assert log[i].timestamp >= log[i - 1].timestamp


@given(n=operation_counts)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_rag_tool_logging_invariant(n):
    """**Validates: Requirements 3.1, 3.3**
    For N query calls, get_log() returns exactly N entries in chronological order.
    Note: RAGTool has a governor limit of 15 calls per session, so we cap n at 15.
    """
    tool = RAGTool()
    n_capped = min(n, 15)
    for i in range(n_capped):
        tool.query(f"query {i}")

    log = tool.get_log()
    assert len(log) == n_capped

    for i in range(1, len(log)):
        assert log[i].timestamp >= log[i - 1].timestamp


@given(n=operation_counts)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_search_tool_logging_invariant(n):
    """**Validates: Requirements 3.1, 3.3**
    For N search calls, get_log() returns exactly N entries in chronological order.
    Note: SearchTool has a governor limit of 2 calls per session, so we cap n at 2.
    """
    tool = SearchTool()
    # Cap n at 2 because SearchTool has a governor limit
    n_capped = min(n, 2)
    for i in range(n_capped):
        tool.search(f"query {i}")

    log = tool.get_log()
    assert len(log) == n_capped

    for i in range(1, len(log)):
        assert log[i].timestamp >= log[i - 1].timestamp


# ---------------------------------------------------------------------------
# Property 3: Memory_Tool Cross-Session Round-Trip
# Validates: Requirements 2.1, 2.2
# ---------------------------------------------------------------------------

memory_keys = st.text(min_size=1, max_size=50).filter(lambda s: s.strip() != "")
memory_values = st.text(min_size=0, max_size=200)


@given(user_id=user_ids, key=memory_keys, value=memory_values)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_memory_tool_round_trip(user_id, key, value):
    """**Validates: Requirements 2.1, 2.2**
    For any user_id, key, value: save_fact then recall_fact returns the
    saved value unchanged.
    """
    db_path = tempfile.mktemp(suffix=".db")
    try:
        tool = MemoryTool(db_path=db_path)
        tool.save_fact(user_id=user_id, key=key, value=value)
        result = tool.recall_fact(user_id=user_id, key=key)
        assert result["value"] == value
    finally:
        tool.reset()
        if os.path.exists(db_path):
            os.remove(db_path)


@given(user_id=user_ids, key=memory_keys)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_memory_tool_missing_key_returns_empty(user_id, key):
    """**Validates: Requirements 2.1, 2.2**
    recall_fact for a non-existent key returns {"value": ""} without error.
    """
    db_path = tempfile.mktemp(suffix=".db")
    try:
        tool = MemoryTool(db_path=db_path)
        result = tool.recall_fact(user_id=user_id, key=key)
        assert result == {"value": ""}
    finally:
        tool.reset()
        if os.path.exists(db_path):
            os.remove(db_path)


# ---------------------------------------------------------------------------
# Property 5: Tool Configured Data Retrieval
# Validates: Requirements 3.2, 3.4, 3.5
# ---------------------------------------------------------------------------

doc_words = st.lists(
    st.text(
        alphabet=st.characters(min_codepoint=97, max_codepoint=122),
        min_size=3,
        max_size=10,
    ),
    min_size=3,
    max_size=10,
).map(lambda ws: " ".join(ws))

search_result_items = st.fixed_dictionaries({
    "title": non_empty_text,
    "url": non_empty_text,
})

date_strings = st.dates().map(lambda d: d.strftime("%Y-%m-%d"))


@given(content=doc_words)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_rag_tool_injected_doc_retrievable(content):
    """**Validates: Requirements 3.2**
    RAGTool: injected document is retrievable on a matching query (using a
    word from the content).
    """
    tool = RAGTool()
    doc = RAGDocument(doc_id="test-doc", content=content)
    tool.inject_document(doc)

    # Query with the first word from the content — guaranteed overlap
    first_word = content.split()[0]
    results = tool.query(first_word, top_k=5)
    # query() returns a string (joined doc contents), so check content is present
    assert content in results or content.split()[0] in results


@given(
    results=st.lists(search_result_items, min_size=1, max_size=10),
    query=non_empty_text,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_search_tool_returns_configured_response_set(results, query):
    """**Validates: Requirements 3.4**
    SearchTool: search() returns results from the configured response_set.
    """
    tool = SearchTool(response_set=results)
    returned = tool.search(query)
    # First call returns a dict with results key + stop hint
    assert returned["results"] == results
    assert returned["status"] == "complete"


@given(date=date_strings)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_calendar_tool_returns_entries_for_date(date):
    """**Validates: Requirements 3.5**
    CalendarTool: get_schedule(date) returns only entries whose start begins
    with that date.
    """
    matching = [
        CalendarEntry(
            id="e1",
            title="Meeting",
            start=f"{date}T09:00:00",
            end=f"{date}T10:00:00",
        ),
        CalendarEntry(
            id="e2",
            title="Lunch",
            start=f"{date}T12:00:00",
            end=f"{date}T13:00:00",
        ),
    ]
    non_matching = [
        CalendarEntry(
            id="e3",
            title="Other",
            start="1999-01-01T08:00:00",
            end="1999-01-01T09:00:00",
        ),
    ]
    tool = CalendarTool(entries=matching + non_matching)
    schedule = tool.get_schedule(date)

    assert len(schedule) == 2
    for entry in schedule:
        assert entry.start.startswith(date)
