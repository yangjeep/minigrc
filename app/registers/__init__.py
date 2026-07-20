"""Reusable spreadsheet-style register framework.

A "register" is a tabular entity (Controls, later Risks/Assets/Framework
requirements) editable inline as a grid. `RegisterConfig` describes one
entity's fields/permissions; `build_register_router` turns that config into
a JSON API mounted at `/api/registers/<name>` consumed by
`app/static/js/register-grid.js`. See
docs/superpowers/specs/2026-07-20-feature2-register-grid-design.md.
"""
