# Xtremis AI -- Vanderbilt Dry Dock 2026

Two challenge briefs for the Vanderbilt Dry Dock Build Week (April 30 - May 6, 2026), sponsored by Xtremis AI.

## Challenges

### Challenge 1: Bloomberg Terminal for Spectrum
Real-time pricing, liquidity, and risk for the world's most valuable unowned asset class. Define a unit of spectrum access, build a pricing methodology grounded in real data, and deliver a working prototype.

See [challenge-1-spectrum/](./challenge-1-spectrum/)

### Challenge 2: The Quantum Routing Brain
A containerized orchestration layer that decides, in real time, which compute tasks in a tactical defense workflow should be offloaded to a remote quantum backend versus run classically. Pick a mission thread, map its computational steps, and build the middleware.

See [challenge-2-quantum/](./challenge-2-quantum/)

## About Xtremis

Xtremis AI is a Vanderbilt University defense technology spinout specializing in RF sensing, electromagnetic spectrum management, and orchestration for DoD customers. See [about/](./about/) for the executive primer.

**Point of contact during Build Week:**
B. Thomas Edman -- btedman@xtremis.ai

## Repository Structure

    xtremis-drydock/
    ├── README.md
    ├── about/                          # Xtremis executive primer
    ├── challenge-1-spectrum/
    │   ├── README.md
    │   ├── docs/                       # Challenge brief + Annexes A-E (PDFs)
    │   ├── data/                       # Data source download scripts + links
    │   ├── examples/                   # Starter Jupyter notebook
    │   ├── src/                        # Skeleton FastAPI pricing service
    │   ├── docker-compose.yml
    │   ├── Dockerfile
    │   └── requirements.txt
    ├── challenge-2-quantum/
    │   ├── README.md
    │   ├── docs/                       # Challenge brief + Annexes A-F (PDFs)
    │   ├── data/                       # Task profile CSV
    │   ├── examples/                   # MaxCut QAOA reference script
    │   ├── src/                        # Router + classical/quantum backends
    │   ├── docker-compose.yml
    │   ├── Dockerfile.router
    │   ├── Dockerfile.classical
    │   ├── Dockerfile.quantum
    │   └── requirements.txt
    └── shared/
        └── docs/                       # Shared evaluation rubric PDF

## License

This repository and its contents are provided for use by Vanderbilt Dry Dock 2026 participants. All rights reserved by Xtremis, Inc.
