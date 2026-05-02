"""
5-gate decision engine for the Quantum Routing Brain.
Implements the sequential routing logic from Annex B (Decision Rubric).

A task is routed to quantum only when ALL five gates pass.
Failure at any gate sends the task to the classical path.
"""

import time
import requests
from models import TaskRequest, RoutingDecision

# Gate thresholds (configure these)
MAX_CLASSIFICATION_FOR_OFFLOAD = {"UNCLASS", "CUI"}
QUANTUM_ELIGIBLE_VALUES = {"Y", "maybe"}
QUANTUM_BACKEND_URL = "http://quantum-backend:8002"
CLASSICAL_BACKEND_URL = "http://classical-backend:8001"

# Estimated round-trip overhead to quantum backend (ms)
# In production this would be measured dynamically
ESTIMATED_QPU_OVERHEAD_MS = 5000


def evaluate_gates(task: TaskRequest) -> dict:
    """
    Run the 5-gate sequential check. Returns a dict of gate results.
    Each gate is True (pass) or False (fail) with a reason string.
    """

    gates = {}

    # Gate 1: Problem class
    gate1_pass = task.quantum_candidate.value in QUANTUM_ELIGIBLE_VALUES
    gates["gate_1_problem_class"] = {
        "passed": gate1_pass,
        "reason": (
            f"quantum_candidate={task.quantum_candidate.value}"
            if gate1_pass
            else f"Task is not quantum-eligible (quantum_candidate={task.quantum_candidate.value})"
        ),
    }
    if not gate1_pass:
        return gates

    # Gate 2: Classification level
    gate2_pass = task.classification_level.value in MAX_CLASSIFICATION_FOR_OFFLOAD
    gates["gate_2_classification"] = {
        "passed": gate2_pass,
        "reason": (
            f"classification={task.classification_level.value}, within IL5 ceiling"
            if gate2_pass
            else f"classification={task.classification_level.value} exceeds IL5 ceiling for commercial QPU"
        ),
    }
    if not gate2_pass:
        return gates

    # Gate 3: Latency budget
    gate3_pass = task.latency_budget_ms > ESTIMATED_QPU_OVERHEAD_MS
    gates["gate_3_latency"] = {
        "passed": gate3_pass,
        "reason": (
            f"latency_budget={task.latency_budget_ms}ms > estimated overhead={ESTIMATED_QPU_OVERHEAD_MS}ms"
            if gate3_pass
            else f"latency_budget={task.latency_budget_ms}ms insufficient for QPU round trip ({ESTIMATED_QPU_OVERHEAD_MS}ms estimated)"
        ),
    }
    if not gate3_pass:
        return gates

    # Gate 4: Instance size (stub -- in production, inspect the actual problem)
    # For the demo, pass if the task has a quantum algorithm specified
    gate4_pass = task.quantum_algorithm is not None and task.quantum_algorithm != "none"
    gates["gate_4_instance_size"] = {
        "passed": gate4_pass,
        "reason": (
            f"quantum_algorithm={task.quantum_algorithm}, problem formulation available"
            if gate4_pass
            else "No quantum algorithm specified; cannot determine instance feasibility"
        ),
    }
    if not gate4_pass:
        return gates

    # Gate 5: Backend availability
    try:
        resp = requests.get(f"{QUANTUM_BACKEND_URL}/health", timeout=2)
        gate5_pass = resp.status_code == 200
        gates["gate_5_backend_available"] = {
            "passed": gate5_pass,
            "reason": "Quantum backend is reachable and healthy",
        }
    except (requests.ConnectionError, requests.Timeout):
        gates["gate_5_backend_available"] = {
            "passed": False,
            "reason": "Quantum backend unreachable (DDIL condition or service down). Falling back to classical.",
        }

    return gates


def route_task(task: TaskRequest) -> tuple[RoutingDecision, str, dict]:
    """
    Route a task through the 5-gate engine.
    Returns (decision, backend_url, gate_results).
    """

    gates = evaluate_gates(task)

    # Check if all evaluated gates passed
    all_passed = all(g["passed"] for g in gates.values())

    # Need all 5 gates to have been evaluated and passed
    if all_passed and len(gates) == 5:
        return RoutingDecision.QUANTUM, QUANTUM_BACKEND_URL, gates
    else:
        return RoutingDecision.CLASSICAL, CLASSICAL_BACKEND_URL, gates
