"""Headless UAT harness and scenarios (issue #38).

Excluded from the default `pytest` run (see pyproject.toml's
`addopts = "-m 'not uat'"` and this package's auto-applied `uat` marker in
tests/uat/conftest.py). Run explicitly via:

    GRC_UAT_MODE=1 pytest -m uat tests/uat
    python -m app.cli uat

See docs/superpowers/specs/2026-08-18-issue38-headless-uat-design.md.
"""
