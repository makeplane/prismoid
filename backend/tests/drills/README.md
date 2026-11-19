# tests/drills

Durability drills for the call log (ADR-089): a real step runs in its own process, is killed with
SIGKILL at a chosen point, and must resume without buying anything twice. Only the model is scripted.

Drills carry the `drill` marker, which the default run deselects. Run them with the stack up:

    uv run pytest tests/drills -m drill -n 4

- `durability/rig.py` is the child process each drill starts (`python -m tests.drills.durability.rig`).
- The shared harness is `tests/support/rigs/durability_drill.py`.

Put a new drill in the folder of what it proves (`durability/` today) and mark it `drill`.

All tiers and how to run them: [tests/README.md](../README.md).
