"""
Quantum Routing Brain - Router Service
Dispatches tasks to classical or quantum backends based on 5-gate decision logic.
"""

import time
import requests
from fastapi import FastAPI
from models import TaskRequest, RoutingResult, RoutingDecision
from decision_engine import route_task

app = FastAPI(
    title="Quantum Routing Brain",
    description="Dry Dock 2026 - Challenge 2: Task routing for hybrid quantum-classical defense compute",
    version="0.1.0",
)

# In-memory audit log (replace with persistent storage)
audit_log: list[dict] = []


@app.get("/health")
def health():
    return {"status": "ok", "service": "quantum-router"}


@app.post("/route", response_model=RoutingResult)
def route(task: TaskRequest):
    """
    Route a task through the 5-gate decision engine.
    Dispatches to the appropriate backend and returns the result with full audit trail.
    """

    start = time.time()

    # Run decision engine
    decision, backend_url, gate_results = route_task(task)

    # Build reasoning string from gate results
    failed_gates = [k for k, v in gate_results.items() if not v["passed"]]
    if decision == RoutingDecision.QUANTUM:
        reasoning = "All 5 gates passed. Routing to quantum backend."
    else:
        last_gate = list(gate_results.keys())[-1]
        reasoning = f"Failed at {last_gate}: {gate_results[last_gate]['reason']}. Routing to classical."

    # Dispatch to backend
    classical_result = None
    quantum_result = None

    try:
        # Always run classical baseline
        classical_resp = requests.post(
            "http://classical-backend:8001/solve",
            json={"task_id": task.task_id, "task_name": task.task_name},
            timeout=10,
        )
        if classical_resp.status_code == 200:
            classical_result = classical_resp.json()
    except (requests.ConnectionError, requests.Timeout):
        classical_result = {"error": "Classical backend unreachable"}

    if decision == RoutingDecision.QUANTUM:
        try:
            quantum_resp = requests.post(
                f"{backend_url}/solve",
                json={"task_id": task.task_id, "task_name": task.task_name},
                timeout=30,
            )
            if quantum_resp.status_code == 200:
                quantum_result = quantum_resp.json()
        except (requests.ConnectionError, requests.Timeout):
            quantum_result = {"error": "Quantum backend unreachable, classical result used"}
            decision = RoutingDecision.CLASSICAL
            reasoning += " (quantum backend failed, fell back to classical)"

    elapsed_ms = (time.time() - start) * 1000

    result = RoutingResult(
        task_id=task.task_id,
        task_name=task.task_name,
        decision=decision,
        routed_to="quantum-aer" if decision == RoutingDecision.QUANTUM else "classical-local",
        gate_results=gate_results,
        reasoning=reasoning,
        classical_result=classical_result,
        quantum_result=quantum_result,
        latency_ms=round(elapsed_ms, 1),
    )

    # Append to audit log
    audit_log.append(result.model_dump())

    return result


@app.get("/audit")
def get_audit_log():
    """Return the full audit log of routing decisions."""
    return {"total_decisions": len(audit_log), "log": audit_log}


@app.get("/stats")
def get_stats():
    """Summary statistics of routing decisions."""
    total = len(audit_log)
    quantum_count = sum(1 for r in audit_log if r["decision"] == "QUANTUM")
    classical_count = sum(1 for r in audit_log if r["decision"] == "CLASSICAL")
    return {
        "total_tasks_routed": total,
        "routed_quantum": quantum_count,
        "routed_classical": classical_count,
        "quantum_ratio": round(quantum_count / total, 3) if total > 0 else 0,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
