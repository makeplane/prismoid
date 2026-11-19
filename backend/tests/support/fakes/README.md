# tests/support/fakes

In-memory stand-ins for what the code under test talks to, so a unit test needs no Docker, browser or
network: `fake_frames.py` (a `FrameDriver`), `frame_files.py` (a frame's file door and the tool record's
journal), `cdp_scripts.py` and `cdp_peer.py` (a scripted Chromium over CDP), `browser_fakes.py` (the browser toolset's sidecar and ports), `push_sender.py` (the FCM push transport). A fake keeps the real contract; each
module's docstring says which one.

All tiers and how to run them: [tests/README.md](../../README.md).
