"""
Classical Backend - Brute Force MaxCut Solver
Solves MaxCut by exhaustive enumeration. Serves as the classical baseline
that the quantum backend is compared against.
"""

import itertools
from fastapi import FastAPI

app = FastAPI(
    title="Classical Backend",
    description="Brute-force MaxCut solver for routing comparison",
    version="0.1.0",
)

# Default demo graph: 8-node cycle + cross edges (interference graph)
DEFAULT_EDGES = [
    (0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 0),
    (0, 4), (1, 5), (2, 6), (3, 7),
]


def brute_force_maxcut(edges: list[tuple[int, int]], n_nodes: int) -> dict:
    """Solve MaxCut by exhaustive enumeration. O(2^n) -- only for small n."""
    best_cut = 0
    best_partition = None

    for bits in itertools.product([0, 1], repeat=n_nodes):
        cut = sum(1 for u, v in edges if bits[u] != bits[v])
        if cut > best_cut:
            best_cut = cut
            best_partition = list(bits)

    return {
        "max_cut_value": best_cut,
        "partition": best_partition,
        "n_nodes": n_nodes,
        "n_edges": len(edges),
        "method": "brute_force_exhaustive",
        "optimal": True,
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "classical-backend"}


@app.post("/solve")
def solve(request: dict):
    """
    Solve a MaxCut problem on the default demo graph.
    In production, the graph would come from the task payload.
    """
    edges = request.get("edges", DEFAULT_EDGES)
    n_nodes = request.get("n_nodes", 8)

    result = brute_force_maxcut(edges, n_nodes)
    result["task_id"] = request.get("task_id", "unknown")
    result["task_name"] = request.get("task_name", "unknown")

    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
