"""
Quantum Backend - Qiskit Aer QAOA MaxCut Solver
Solves MaxCut using QAOA on the Qiskit Aer simulator.
This is the quantum kernel that the router dispatches to.
"""

from fastapi import FastAPI
import numpy as np

app = FastAPI(
    title="Quantum Backend",
    description="Qiskit Aer QAOA MaxCut solver for routing comparison",
    version="0.1.0",
)

# Default demo graph (same as classical backend)
DEFAULT_EDGES = [
    (0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 0),
    (0, 4), (1, 5), (2, 6), (3, 7),
]


def qaoa_maxcut(edges: list[tuple[int, int]], n_nodes: int, p: int = 1, shots: int = 1024) -> dict:
    """
    Solve MaxCut using QAOA on Qiskit Aer simulator.
    p = number of QAOA layers (depth).
    """
    try:
        from qiskit import QuantumCircuit
        from qiskit_aer import AerSimulator
        from qiskit.primitives import StatevectorEstimator
        import random

        # Build cost operator as a simple QAOA circuit
        qc = QuantumCircuit(n_nodes)

        # Initial superposition
        for i in range(n_nodes):
            qc.h(i)

        # QAOA layers (simplified: fixed angles for demo)
        gamma = 0.5  # In production, optimize these
        beta = 0.3

        for _ in range(p):
            # Cost layer: ZZ interactions on edges
            for u, v in edges:
                qc.cx(u, v)
                qc.rz(2 * gamma, v)
                qc.cx(u, v)

            # Mixer layer: X rotations
            for i in range(n_nodes):
                qc.rx(2 * beta, i)

        qc.measure_all()

        # Run on Aer simulator
        simulator = AerSimulator()
        job = simulator.run(qc, shots=shots)
        result = job.result()
        counts = result.get_counts()

        # Find best bitstring
        best_bitstring = max(counts, key=counts.get)
        best_partition = [int(b) for b in reversed(best_bitstring)]

        # Compute cut value
        cut_value = sum(1 for u, v in edges if best_partition[u] != best_partition[v])

        return {
            "max_cut_value": cut_value,
            "partition": best_partition,
            "n_nodes": n_nodes,
            "n_edges": len(edges),
            "method": f"qaoa_p{p}_aer_simulator",
            "optimal": False,
            "shots": shots,
            "top_bitstring": best_bitstring,
            "top_bitstring_count": counts[best_bitstring],
            "total_unique_bitstrings": len(counts),
        }

    except ImportError:
        # Qiskit not installed -- return a stub result
        return {
            "max_cut_value": -1,
            "partition": [],
            "method": "qaoa_stub_qiskit_not_installed",
            "error": "qiskit or qiskit-aer not installed. pip install qiskit qiskit-aer",
        }


@app.get("/health")
def health():
    return {"status": "ok", "service": "quantum-backend"}


@app.post("/solve")
def solve(request: dict):
    """
    Solve a MaxCut problem using QAOA on Qiskit Aer.
    In production, the graph would come from the task payload.
    """
    edges = request.get("edges", DEFAULT_EDGES)
    n_nodes = request.get("n_nodes", 8)
    p = request.get("qaoa_depth", 1)
    shots = request.get("shots", 1024)

    result = qaoa_maxcut(edges, n_nodes, p=p, shots=shots)
    result["task_id"] = request.get("task_id", "unknown")
    result["task_name"] = request.get("task_name", "unknown")

    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
