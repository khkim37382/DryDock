"""
MaxCut QAOA Reference Script
A standalone example showing how to solve MaxCut with QAOA on Qiskit Aer.
Use this as your pluggable quantum kernel inside the routing framework.

Based on the IBM Qiskit QAOA tutorial:
https://learning.quantum.ibm.com/tutorial/quantum-approximate-optimization-algorithm

For PennyLane equivalent:
https://pennylane.ai/qml/demos/tutorial_qaoa_maxcut
"""

import itertools
import numpy as np

try:
    from qiskit import QuantumCircuit
    from qiskit_aer import AerSimulator

    QISKIT_AVAILABLE = True
except ImportError:
    QISKIT_AVAILABLE = False
    print("Qiskit not installed. Install with: pip install qiskit qiskit-aer")


def create_maxcut_graph(n_nodes: int = 8) -> list[tuple[int, int]]:
    """Create a demo interference graph (cycle + cross edges)."""
    edges = [(i, (i + 1) % n_nodes) for i in range(n_nodes)]
    edges += [(i, (i + n_nodes // 2) % n_nodes) for i in range(n_nodes // 2)]
    return edges


def classical_brute_force(edges, n_nodes):
    """Exact solution by exhaustive enumeration."""
    best_cut, best_partition = 0, None
    for bits in itertools.product([0, 1], repeat=n_nodes):
        cut = sum(1 for u, v in edges if bits[u] != bits[v])
        if cut > best_cut:
            best_cut = cut
            best_partition = list(bits)
    return best_cut, best_partition


def qaoa_circuit(edges, n_nodes, gamma, beta, p=1):
    """Build a p-layer QAOA circuit for MaxCut."""
    qc = QuantumCircuit(n_nodes)

    # Initial superposition
    for i in range(n_nodes):
        qc.h(i)

    for layer in range(p):
        g = gamma[layer] if isinstance(gamma, list) else gamma
        b = beta[layer] if isinstance(beta, list) else beta

        # Cost unitary: exp(-i * gamma * C)
        for u, v in edges:
            qc.cx(u, v)
            qc.rz(2 * g, v)
            qc.cx(u, v)

        # Mixer unitary: exp(-i * beta * B)
        for i in range(n_nodes):
            qc.rx(2 * b, i)

    qc.measure_all()
    return qc


def evaluate_cut(bitstring, edges):
    """Compute the cut value for a given partition."""
    bits = [int(b) for b in reversed(bitstring)]
    return sum(1 for u, v in edges if bits[u] != bits[v])


def run_qaoa(edges, n_nodes, gamma=0.5, beta=0.3, p=1, shots=4096):
    """Run QAOA and return results."""
    if not QISKIT_AVAILABLE:
        print("Qiskit not available. Skipping quantum execution.")
        return None

    qc = qaoa_circuit(edges, n_nodes, gamma, beta, p)
    simulator = AerSimulator()
    job = simulator.run(qc, shots=shots)
    counts = job.result().get_counts()

    # Evaluate all sampled bitstrings
    results = []
    for bitstring, count in counts.items():
        cut_val = evaluate_cut(bitstring, edges)
        results.append((bitstring, cut_val, count))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


if __name__ == "__main__":
    n_nodes = 8
    edges = create_maxcut_graph(n_nodes)

    print(f"Graph: {n_nodes} nodes, {len(edges)} edges")
    print(f"Edges: {edges}")
    print()

    # Classical baseline
    optimal_cut, optimal_partition = classical_brute_force(edges, n_nodes)
    print(f"Classical (brute force): MaxCut = {optimal_cut}, partition = {optimal_partition}")
    print()

    # QAOA
    if QISKIT_AVAILABLE:
        results = run_qaoa(edges, n_nodes, gamma=0.5, beta=0.3, p=1, shots=4096)
        if results:
            best = results[0]
            print(f"QAOA p=1 (Aer simulator, 4096 shots):")
            print(f"  Best bitstring: {best[0]}, cut = {best[1]}, sampled {best[2]} times")
            print(f"  Approximation ratio: {best[1] / optimal_cut:.3f}")
            print(f"  Top 5 bitstrings:")
            for bs, cut, count in results[:5]:
                print(f"    {bs}: cut={cut}, count={count}")
    else:
        print("Skipping QAOA (install qiskit and qiskit-aer)")
