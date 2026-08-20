"""Static regression guard for issue #15's hard authority boundary: the
Digital Compliance TPM must never call a material-mutation domain
command (policy approval/effective transitions, control-effectiveness/
attestation writes, finding closure, evidence capture, framework
adoption changes).

An AST scan (not an import-time check) so it has no import-order/side-
effect risk and cannot be defeated by a code path that is merely
unreachable in today's tests — the identifier must not appear at all.
Mirrors tests/test_backup.py's `ast`-based regression-guard precedent.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Every material-mutation domain command in the codebase as of this
# writing (grepped across app/finding_lifecycle.py, app/policy_lifecycle.py,
# app/control_occurrences.py, app/control_tests.py,
# app/framework_adoption.py, app/evidence_repository.py). If a future
# domain adds a new mutation command, add it here too — the point of this
# test is that the TPM module set must never grow a reference to any of
# these, not that this list is exhaustive forever.
FORBIDDEN_MUTATION_SYMBOLS = frozenset(
    {
        "open_finding",
        "close_finding",
        "record_remediation_update",
        "request_retest",
        "record_retest",
        "submit_for_review",
        "review_version",
        "make_version_effective",
        "withdraw_version",
        "record_occurrence_manually",
        "perform_occurrence",
        "link_evidence",
        "link_evidence_artifact_version",
        "freeze_population",
        "draw_sample",
        "record_test",
        "link_test_evidence",
        "adopt_framework",
        "upgrade_framework_adoption",
        "capture_evidence_version",
    }
)

TPM_MODULE_PATHS = (
    "app/digital_tpm.py",
    "app/ai_provider.py",
    "app/ai_provider_client.py",
    "app/ai_egress.py",
    "app/routers/digital_tpm.py",
    "app/routers/admin_ai_settings.py",
)


def _referenced_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_tpm_modules_never_reference_a_material_mutation_command():
    project_root = Path(__file__).resolve().parent.parent
    for relative_path in TPM_MODULE_PATHS:
        path = project_root / relative_path
        referenced = _referenced_names(path)
        overlap = referenced & FORBIDDEN_MUTATION_SYMBOLS
        assert not overlap, (
            f"{relative_path} references material-mutation command(s) {sorted(overlap)} — "
            "the Digital Compliance TPM must never call a compliance-mutating domain command "
            "directly (issue #15's hard authority boundary)."
        )


def test_ai_task_execution_output_is_never_written_into_a_domain_model():
    """`AiTaskExecution.output_text`/`ProviderCallResult.content` must
    never be assigned onto a domain (non-AI) model attribute anywhere in
    the TPM module set — the only sink for AI output is the
    `AiTaskExecution` row itself."""
    project_root = Path(__file__).resolve().parent.parent
    domain_model_targets = {"description", "closure_note", "scope_note", "evidence_note", "status"}
    for relative_path in ("app/digital_tpm.py",):
        path = project_root / relative_path
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in domain_model_targets:
                    raise AssertionError(
                        f"{relative_path} assigns to domain attribute '.{target.attr}' — "
                        "AI output must never be written directly onto a domain model."
                    )
