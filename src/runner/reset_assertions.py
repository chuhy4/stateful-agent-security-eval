"""Deterministic state manifests and reset-transition assertions."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_CANARY_ID = "reset-policy-db-canary"
DEFAULT_CANARY_VALUE = "evaluator-only-preserved"

_PROCEDURAL_COUNTERS = (
    "_call_count",
    "_draft_call_count",
    "_list_call_count",
    "_query_call_count",
    "_save_call_count",
    "_send_call_count",
)


@dataclass(frozen=True)
class StateManifest:
    """Deterministic snapshot of reset-relevant state."""

    db_identity: str
    active_thread_id: str
    prior_thread_id: str
    checkpoint_table_present: bool
    prior_checkpoint_physical_rows: int
    active_checkpoint_reachable: bool
    canonical_memory: tuple[tuple[str, str], ...]
    db_canaries: tuple[tuple[str, str], ...]
    procedural_counters: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    session_local_values: tuple[tuple[str, str], ...]
    evaluator_log_lengths: tuple[tuple[str, int], ...]
    email_record_count: int
    rag_document_ids: tuple[str, ...]


@dataclass(frozen=True)
class AssertionResult:
    """One structured, independently inspectable transition assertion."""

    name: str
    passed: bool
    expected: object
    observed: object
    reason: str = ""


@dataclass(frozen=True)
class BoundaryMutation:
    """Operation evidence produced while applying a boundary."""

    canonical_memory_clear_attempted: bool
    canonical_rows_deleted: int | None


def seed_db_canary(
    db_path: str,
    canary_id: str = DEFAULT_CANARY_ID,
    value: str = DEFAULT_CANARY_VALUE,
) -> None:
    """Seed evaluator-only state in a table unavailable to agent tools."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reset_assertion_canaries "
            "(canary_id TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO reset_assertion_canaries (canary_id, value) "
            "VALUES (?, ?)",
            (canary_id, value),
        )


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _checkpoint_count(conn: sqlite3.Connection, thread_id: str) -> int:
    if not _table_exists(conn, "checkpoints"):
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    return int(row[0]) if row else 0


def _checkpoint_reachable(conn: sqlite3.Connection, thread_id: str) -> bool:
    """Match SqliteSaver lookup with its default empty checkpoint namespace."""
    if not _table_exists(conn, "checkpoints"):
        return False
    return conn.execute(
        "SELECT 1 FROM checkpoints "
        "WHERE thread_id = ? AND checkpoint_ns = '' LIMIT 1",
        (thread_id,),
    ).fetchone() is not None


def _canonical_memory(conn: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    if not _table_exists(conn, "memory_facts"):
        return ()
    rows = conn.execute(
        "SELECT key, value FROM memory_facts WHERE user_id = ? ORDER BY key, value",
        ("user",),
    ).fetchall()
    return tuple((str(key), str(value)) for key, value in rows)


def _db_canaries(conn: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    if not _table_exists(conn, "reset_assertion_canaries"):
        return ()
    rows = conn.execute(
        "SELECT canary_id, value FROM reset_assertion_canaries "
        "ORDER BY canary_id, value"
    ).fetchall()
    return tuple((str(canary_id), str(value)) for canary_id, value in rows)


def _procedural_counters(
    tools: Mapping[str, object],
) -> tuple[tuple[str, tuple[tuple[str, int], ...]], ...]:
    snapshots = []
    for tool_name, tool in sorted(tools.items()):
        counters = tuple(
            (attribute, int(getattr(tool, attribute)))
            for attribute in _PROCEDURAL_COUNTERS
            if hasattr(tool, attribute)
        )
        if counters:
            snapshots.append((tool_name, counters))
    return tuple(snapshots)


def capture_state_manifest(
    db_path: str,
    active_thread_id: str,
    prior_thread_id: str,
    tools: Mapping[str, object],
) -> StateManifest:
    """Capture reset-relevant state without timestamps or nondeterministic ordering."""
    with sqlite3.connect(db_path) as conn:
        checkpoint_table_present = _table_exists(conn, "checkpoints")
        prior_checkpoint_physical_rows = _checkpoint_count(conn, prior_thread_id)
        active_checkpoint_reachable = _checkpoint_reachable(conn, active_thread_id)
        canonical_memory = _canonical_memory(conn)
        db_canaries = _db_canaries(conn)

    evaluator_log_lengths = tuple(
        (name, len(tool.get_log()))
        for name, tool in sorted(tools.items())
        if hasattr(tool, "get_log")
    )
    email = tools.get("email")
    rag = tools.get("rag")
    memory = tools.get("memory")
    return StateManifest(
        db_identity=str(Path(db_path).resolve()),
        active_thread_id=active_thread_id,
        prior_thread_id=prior_thread_id,
        checkpoint_table_present=checkpoint_table_present,
        prior_checkpoint_physical_rows=prior_checkpoint_physical_rows,
        active_checkpoint_reachable=active_checkpoint_reachable,
        canonical_memory=canonical_memory,
        db_canaries=db_canaries,
        procedural_counters=_procedural_counters(tools),
        session_local_values=(
            ("memory._ratg_user_message", str(memory._ratg_user_message)),
        ) if memory is not None and hasattr(memory, "_ratg_user_message") else (),
        evaluator_log_lengths=evaluator_log_lengths,
        email_record_count=len(email.get_emails()) if email is not None else 0,
        rag_document_ids=tuple(
            sorted(str(document.doc_id) for document in getattr(rag, "corpus", ()))
        ),
    )


def _result(name: str, expected: object, observed: object, reason: str) -> AssertionResult:
    passed = observed == expected
    return AssertionResult(
        name=name,
        passed=passed,
        expected=expected,
        observed=observed,
        reason="" if passed else reason,
    )


def assert_transition(
    condition: str,
    before: StateManifest,
    after: StateManifest,
    mutation: BoundaryMutation,
) -> tuple[AssertionResult, ...]:
    """Validate one C0/C1/C2 transition independently of model behavior."""
    if condition not in {"C0", "C1", "C2"}:
        raise ValueError(f"Unknown reset condition: {condition}")

    assertions = [
        AssertionResult(
            name="pre_boundary_checkpoint_table_present",
            passed=before.checkpoint_table_present,
            expected=True,
            observed=before.checkpoint_table_present,
            reason=(
                "" if before.checkpoint_table_present
                else "The checkpoint table is absent before the boundary."
            ),
        ),
        AssertionResult(
            name="pre_boundary_checkpoint_reachable",
            passed=before.active_checkpoint_reachable,
            expected=True,
            observed=before.active_checkpoint_reachable,
            reason=(
                "" if before.active_checkpoint_reachable
                else "No default-namespace checkpoint is reachable for the active thread."
            ),
        ),
        AssertionResult(
            name="db_canary_seeded",
            passed=bool(before.db_canaries),
            expected="at least one evaluator-only DB canary",
            observed=before.db_canaries,
            reason="" if before.db_canaries else "No evaluator-only DB canary was seeded.",
        ),
        _result(
            "same_db_within_run",
            before.db_identity,
            after.db_identity,
            "Database identity changed across the session boundary.",
        ),
        _result(
            "checkpoint_table_preserved",
            before.checkpoint_table_present,
            after.checkpoint_table_present,
            "Checkpoint table presence changed across the boundary.",
        ),
        _result(
            "reserved_db_canaries_preserved",
            before.db_canaries,
            after.db_canaries,
            "Evaluator-only DB canary state changed across the boundary.",
        ),
        _result(
            "procedural_counter_inventory_preserved",
            tuple(
                (tool_name, tuple(name for name, _value in counters))
                for tool_name, counters in before.procedural_counters
            ),
            tuple(
                (tool_name, tuple(name for name, _value in counters))
                for tool_name, counters in after.procedural_counters
            ),
            "Procedural counter names changed across the boundary.",
        ),
        _result(
            "procedural_counters_reset",
            tuple(
                (tool_name, tuple((name, 0) for name, _ in counters))
                for tool_name, counters in after.procedural_counters
            ),
            after.procedural_counters,
            "One or more procedural/session-local counters were not reset.",
        ),
        _result(
            "session_local_values_reset",
            tuple((name, "") for name, _value in after.session_local_values),
            after.session_local_values,
            "One or more session-local values survived the boundary.",
        ),
        _result(
            "evaluator_logs_preserved",
            before.evaluator_log_lengths,
            after.evaluator_log_lengths,
            "Evaluator-only tool logs changed during reset.",
        ),
        _result(
            "session_email_records_cleared",
            0,
            after.email_record_count,
            "Agent-visible session email records survived the boundary.",
        ),
        _result(
            "rag_sources_unchanged",
            before.rag_document_ids,
            after.rag_document_ids,
            "RAG source lifecycle changed during Patch 2 reset handling.",
        ),
    ]

    if condition == "C0":
        assertions.extend((
            _result(
                "conversation_thread_retained",
                before.active_thread_id,
                after.active_thread_id,
                "C0 did not retain the current thread_id.",
            ),
            _result(
                "conversation_checkpoint_retained",
                True,
                after.active_checkpoint_reachable,
                "C0 conversation checkpoint is not reachable after the boundary.",
            ),
            _result(
                "previous_checkpoint_not_deleted",
                before.prior_checkpoint_physical_rows,
                after.prior_checkpoint_physical_rows,
                "C0 physically changed or deleted previous checkpoint rows.",
            ),
            _result(
                "canonical_memory_retained",
                before.canonical_memory,
                after.canonical_memory,
                "C0 canonical memory changed at the boundary.",
            ),
            _result(
                "canonical_memory_clear_not_attempted",
                (False, None),
                (
                    mutation.canonical_memory_clear_attempted,
                    mutation.canonical_rows_deleted,
                ),
                "C0 unexpectedly attempted to clear canonical memory.",
            ),
        ))
    else:
        assertions.extend((
            AssertionResult(
                name="new_conversation_thread",
                passed=after.active_thread_id != before.active_thread_id,
                expected="different thread_id",
                observed=after.active_thread_id,
                reason=(
                    "" if after.active_thread_id != before.active_thread_id
                    else f"{condition} reused the previous thread_id."
                ),
            ),
            _result(
                "new_thread_has_no_checkpoint",
                False,
                after.active_checkpoint_reachable,
                f"{condition} new thread already has accessible conversation state.",
            ),
            _result(
                "previous_checkpoint_not_deleted",
                before.prior_checkpoint_physical_rows,
                after.prior_checkpoint_physical_rows,
                "Previous conversation checkpoints were physically changed or deleted.",
            ),
        ))
        expected_memory = before.canonical_memory if condition == "C1" else ()
        assertions.append(_result(
            "canonical_memory_retained" if condition == "C1" else "canonical_memory_cleared",
            expected_memory,
            after.canonical_memory,
            (
                "C1 canonical memory changed at the boundary."
                if condition == "C1"
                else "C2 canonical memory was not fully cleared."
            ),
        ))
        if condition == "C1":
            assertions.append(_result(
                "canonical_memory_clear_not_attempted",
                (False, None),
                (
                    mutation.canonical_memory_clear_attempted,
                    mutation.canonical_rows_deleted,
                ),
                "C1 unexpectedly attempted to clear canonical memory.",
            ))
        else:
            assertions.extend((
                _result(
                    "canonical_memory_clear_attempted",
                    True,
                    mutation.canonical_memory_clear_attempted,
                    "C2 did not execute the canonical-memory clear operation.",
                ),
                _result(
                    "canonical_rows_deleted",
                    len(before.canonical_memory),
                    mutation.canonical_rows_deleted,
                    "C2 SQLite delete rowcount did not match pre-boundary canonical rows.",
                ),
            ))

    return tuple(assertions)
