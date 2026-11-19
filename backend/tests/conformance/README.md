# tests/conformance

Contracts that every implementation of a seam must pass, written once and run against each one.

- `test_frame_driver.py`: one test per clause of `prismoid/ai/frames/driver.py`, run against every frame
  driver (Docker always; sandbar and the device when configured, see the module docstring).
- `test_model_conformance.py`: what a model row must do to serve the judge and the Lead (offline on
  `FunctionModel`; the live row only with `-m live`).

These run with the integration tests: `just test-integration`. A new driver or model row passes here
before it ships. A test of one driver's own behaviour goes in `tests/integration/frames/` or
`tests/unit/ai/frames/` instead.

All tiers and how to run them: [tests/README.md](../README.md).
