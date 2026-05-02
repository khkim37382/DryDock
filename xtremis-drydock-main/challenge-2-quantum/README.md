# Challenge 2: The Quantum Routing Brain

Orchestrating quantum microservices in tactical defense systems.

## Quick Start

    # Build and run all three services (router, classical backend, quantum backend)
    docker compose up --build

    # The router is at http://localhost:8000
    # Classical backend at http://localhost:8001
    # Quantum backend at http://localhost:8002

    # Try routing a task:
    curl http://localhost:8000/route -X POST \
      -H "Content-Type: application/json" \
      -d '{"task_id": "SM-01", "task_name": "Frequency Assignment", "classification_level": "CUI", "latency_budget_ms": 10000, "quantum_candidate": "Y"}'

## Documentation

All PDFs are in docs/:

- Challenge Brief: Problem statement, four mission threads, evaluation criteria
- Annex A - Task Profile Reference: Schema docs for the 47-task CSV
- Annex B - Decision Rubric: "When is quantum worth the round trip?" -- the 5-gate framework
- Annex C - Quantum Resource Guide: Free-tier QPU access, simulators, recommended hackathon stack
- Annex D - DDIL and Classification: Tactical link budgets, IL levels, CoT format, JADC2 context
- Annex E - DevSecOps Pointers: Platform One, Big Bang, K3s, container signing toolchain
- Annex F - Evaluation Rubric: Full rubric with detailed expectations (shared across both challenges)

## Data

- data/task_profile.csv: 47 canonical defense compute tasks with classical baselines, quantum candidacy flags, classification levels, and latency budgets

## Starter Code

- examples/maxcut_qaoa_reference.py: Thin wrapper around Qiskit QAOA MaxCut, callable as a REST endpoint
- src/router/main.py: FastAPI router service with /route endpoint
- src/router/decision_engine.py: 5-gate routing logic from Annex B
- src/router/models.py: Pydantic task schemas matching CSV columns
- src/classical_backend/main.py: Brute-force MaxCut solver behind /solve
- src/quantum_backend/main.py: Qiskit Aer QAOA MaxCut behind /solve

## Architecture

    +-------------+     +------------------+     +------------------+
    |   Task      |---->|     Router       |---->| Classical Backend |
    |   Stream    |     |  (decision       |     | (brute force)     |
    |             |     |   engine)        |     +------------------+
    +-------------+     |                  |     +------------------+
                        |                  |---->| Quantum Backend   |
                        |                  |     | (Qiskit Aer QAOA) |
                        +------------------+     +------------------+
                               |
                               v
                        +------------------+
                        |   Audit Log      |
                        |  (decisions +    |
                        |   results)       |
                        +------------------+

## Point of Contact

B. Thomas Edman -- btedman@xtremis.ai
Available remotely during Build Week for office hours.
