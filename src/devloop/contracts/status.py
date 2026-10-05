"""The DevLoop status enum.

This is the single source of truth for what state a task can be in. The
orchestrator (`devloop.graph.routing.decide`) is the only code allowed to
transition `dev_tasks.status` from one of these to another — agents never
choose the next status themselves, they only produce validated artifacts
that `decide()` reads as facts.
"""

from __future__ import annotations

from enum import StrEnum


class DevLoopStatus(StrEnum):
    RECEIVED = "RECEIVED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    REQUIREMENTS_READY = "REQUIREMENTS_READY"
    PLANNING = "PLANNING"
    CONSULTING_PO = "CONSULTING_PO"
    PLAN_READY = "PLAN_READY"
    ENV_BOOTSTRAP = "ENV_BOOTSTRAP"
    BASELINE = "BASELINE"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    QA_TESTING = "QA_TESTING"
    REVIEWING = "REVIEWING"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    REPLANNING = "REPLANNING"
    PUBLISHING = "PUBLISHING"
    READY_FOR_FINALIZE = "READY_FOR_FINALIZE"
    FINALIZED = "FINALIZED"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DevLoopStatus.FINALIZED,
            DevLoopStatus.CANCELLED,
            DevLoopStatus.FAILED,
        }

    @property
    def needs_human(self) -> bool:
        return self in {
            DevLoopStatus.CLARIFICATION_REQUIRED,
            DevLoopStatus.READY_FOR_FINALIZE,
            DevLoopStatus.ESCALATED,
        }
