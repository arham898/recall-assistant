# Architecture decisions

Record of design decisions made during this project, with context and reasoning.

## Format

**Date — Decision title**
Context, options considered, and why we chose this path.

---

## 2026-10-11 — Use uv with src layout

We need a Python project manager. Options: uv, poetry, pip+venv. Chose uv because it is fast, handles both dependency management and virtual environments, and is the direction the Python packaging ecosystem is moving. The src layout prevents accidental imports from the project root.
