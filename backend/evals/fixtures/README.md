# evals/fixtures

Data, not a package (no `__init__.py`). `defects/build.py` runs inside a frame (python-docx, python-pptx,
openpyxl and reportlab live only in the frame image): it builds each deliverable clean and with one planted
defect. `tests/integration/verification/test_planted_defects.py` copies it into a frame and runs it.
