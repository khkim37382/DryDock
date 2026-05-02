# Quantum Routing Brain - Vanderbilt Dry Dock 2026
**Challenge 2** | Xtremis AI

## Overview & Thesis

**Challenge Name:** The Quantum Routing Brain  
**Sponsor:** Xtremis AI  
**Goal:** Build the orchestration layer (middleware/broker) that intelligently routes compute tasks between classical and quantum backends in tactical defense environments.

### Core Thesis
- Quantum will eventually be useful for certain defense problems (optimization, kernels, QRNG).
- Quantum resources will be **remote, scarce, expensive, and classification-limited**.
- The real value is in the **smart routing & policy layer** — not the algorithms themselves.
- Honest framing is rewarded: In 2026, default should be **classical**.

**Key Constraints:**
- DDIL networks (Disconnected, Degraded, Intermittent, Limited)
- Classification ceiling: Commercial QPUs = max **IL5 (CUI)**
- Tactical latency & bandwidth limits

---

## Four Mission Threads (Pick One)

### Thread 1: Counter-UAS
- Forward-deployed drone detection, classification, geolocation, tracking, and effector assignment.
- Quantum candidates: Specific Emitter ID (QSVM), Weapon-Target Assignment (QUBO), Geolocation (HHL).

### Thread 2: Satellite Constellation Tasking & Orbital Deconfliction
- Scheduling collection requests across many satellites with constraints.
- Quantum candidates: Task scheduling (QUBO), collision avoidance optimization.

### Thread 3: Cryptographic Key Distribution & PQC Migration
- Managing keys and migrating to post-quantum cryptography in coalition environments.
- **Strongest for 2026** — QRNG is actually operational today.

### Thread 4: Contested Logistics & Resupply Optimization
- Vehicle routing with time windows, threats, and constraints (VRP).
- Quantum candidates: VRP as QUBO.

---

## What You Must Build (Core Deliverable)

### 1. The Router
- Ingests tasks (from CSV)
- Applies **5-Gate Decision Engine**
- Routes to Classical or Quantum backend
- Full audit logging + explainable decisions

### 2. Backends
- **Classical Backend** (brute force, OR-Tools, etc.)
- **Quantum Backend** (Qiskit Aer minimum, IBM/AWS stretch)

### 3. Data Guard (Stretch)
- Redact classification-sensitive data before sending to commercial QPUs

---

## Provided Starter Code & Files

**Key Files:**
- `task_profile.csv` — 47 defense tasks with metadata
- `docker-compose.yml`
- `Dockerfile.router`, `Dockerfile.classical`, `Dockerfile.quantum`
- `src/router/` — Main router + decision engine
- `src/classical_backend/main.py` — Brute force MaxCut
- `src/quantum_backend/main.py` — Qiskit Aer QAOA MaxCut
- `maxcut_qaoa_reference.py`
- `requirements.txt`

**Main Services:**
- Router → `http://localhost:8000`
- Classical → `http://localhost:8001`
- Quantum → `http://localhost:8002`

---

## The 5 Gates (Annex B) - Most Important

A task routes to quantum **only if ALL 5 gates pass**:

1. **Gate 1: Problem Class** — Maps to known quantum formulation? (QUBO, QAOA, QSVM, QRNG, etc.)
2. **Gate 2: Classification** — UNCLASS or CUI only (no SECRET/TS-SCI)
3. **Gate 3: Latency Budget** — Does round-trip fit within task’s latency requirement?
4. **Gate 4: Instance Size** — Is the problem size feasible on current quantum hardware?
5. **Gate 5: Backend Availability** — Is the quantum backend reachable? (DDIL handling)

---

## Key Annexes Summary

### Annex A: Task Profile Reference
- Documents all columns in `task_profile.csv`
- Explains `quantum_candidate`, `classification_level`, `latency_budget_ms`, `deadline_class`, etc.

### Annex B: Decision Rubric
- Details the 5-Gate framework
- Current reality: Almost everything should run classical in 2026

### Annex C: Free Quantum Resources
- IBM Quantum Open Plan (limited free QPU time)
- AWS Braket (good free simulators)
- Qiskit Aer for local development

### Annex D: DDIL & Classification
- Tactical link budgets (GEO SATCOM ~500-600ms RTT, LEO better, etc.)
- Commercial QPUs limited to IL5/CUI

### Annex E: DevSecOps Pointers
- DoD DevSecOps Reference Design
- Platform One, Big Bang, Iron Bank
- Container security, GitOps, cATO

### Annex F: Evaluation Rubric

**Scoring Categories:**
- **Technical Substance (35%)**
- **Venture Strategy (25%)**
- Defense Realism, Engineering Quality, Presentation, etc.

---

## Tactical Constraints (Critical)

- **Classification:** UNCLASS → CUI = OK | SECRET+ = No commercial quantum
- **Networks:** GEO SATCOM (high latency), LEO Starshield, Tactical Mesh, Link 16, etc.
- **Deadlines:** Hard-realtime tasks almost never go to quantum

---

## Evaluation Tips (Annex F)

**Strong Submissions:**
- Working router with 5-gate logic
- Honest quantum advantage analysis + roadmap
- Clean architecture + Docker
- Ties demo to one mission thread
- Thoughtful venture path (SBIR, etc.)

**Avoid:**
- Overclaiming quantum supremacy
- Ignoring classification/DDIL constraints

---

## Quick Start Commands (from README)

```bash
docker compose up --build
curl http://localhost:8000/route -X POST -H "Content-Type: application/json" -d '{"task_id": "SM-01", ...}'