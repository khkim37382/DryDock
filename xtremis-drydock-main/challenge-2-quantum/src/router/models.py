"""
Pydantic models for the Quantum Routing Brain.
Field names align with the task_profile.csv schema (see Annex A).
"""

from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum
from datetime import datetime


class ClassificationLevel(str, Enum):
    UNCLASS = "UNCLASS"
    CUI = "CUI"
    SECRET = "SECRET"
    TS_SCI = "TS-SCI"


class DeadlineClass(str, Enum):
    HARD_REALTIME = "hard-realtime"
    SOFT_REALTIME = "soft-realtime"
    BATCH = "batch"


class QuantumCandidate(str, Enum):
    YES = "Y"
    MAYBE = "maybe"
    NO = "N"


class RoutingDecision(str, Enum):
    CLASSICAL = "CLASSICAL"
    QUANTUM = "QUANTUM"
    REJECTED = "REJECTED"


class TaskRequest(BaseModel):
    """A compute task submitted to the router for dispatch."""

    task_id: str = Field(..., description="Task identifier from task_profile.csv")
    task_name: str = Field(..., description="Human-readable task name")
    task_category: Optional[str] = Field(default=None, description="RF Sensor Fusion, C-UAS, etc.")
    classification_level: ClassificationLevel = Field(
        ..., description="UNCLASS, CUI, SECRET, or TS-SCI"
    )
    latency_budget_ms: int = Field(
        ..., description="Maximum allowable end-to-end latency in ms"
    )
    deadline_class: DeadlineClass = Field(
        default=DeadlineClass.BATCH, description="hard-realtime, soft-realtime, or batch"
    )
    quantum_candidate: QuantumCandidate = Field(
        ..., description="Y, maybe, or N from task profile"
    )
    quantum_algorithm: Optional[str] = Field(
        default=None, description="QAOA, QSVM, HHL, QRNG, etc."
    )
    input_size_bytes: Optional[int] = Field(
        default=None, description="Approximate input payload size"
    )
    priority: int = Field(default=2, description="1 (highest) to 3 (lowest)")


class RoutingResult(BaseModel):
    """The router's dispatch decision for a single task."""

    task_id: str
    task_name: str
    decision: RoutingDecision
    routed_to: str = Field(
        ..., description="Backend identifier: classical-local, quantum-aer, quantum-ibm, etc."
    )
    gate_results: dict = Field(
        ..., description="Pass/fail for each of the 5 gates"
    )
    reasoning: str = Field(
        ..., description="Human-readable explanation of the routing decision"
    )
    classical_result: Optional[dict] = Field(
        default=None, description="Result from classical backend (if executed)"
    )
    quantum_result: Optional[dict] = Field(
        default=None, description="Result from quantum backend (if executed)"
    )
    latency_ms: Optional[float] = Field(
        default=None, description="Actual end-to-end latency of the dispatch"
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="Decision timestamp UTC"
    )
