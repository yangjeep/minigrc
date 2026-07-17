from __future__ import annotations

import os

from tests.conftest import extract_csrf_token

VALID_PDF = b"%PDF-1.4\n%mock pdf content for tests\n%%EOF"
FAKE_PDF = b"this is not a real pdf"


def _create_policy_with_pdf(client, *, title="Security Policy") -> str:
    page = client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("policy.pdf", VALID_PDF, "application/pdf")}
    response = client.post(
        "/policies",
        data={"title": title, "status": "draft", "csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("?")[0].rsplit("/", 1)[-1]


def test_valid_pdf_upload(logged_in_client):
    policy_id = _create_policy_with_pdf(logged_in_client)
    detail = logged_in_client.get(f"/policies/{policy_id}")
    assert detail.status_code == 200
    assert b"policy.pdf" in detail.content


def test_valid_docx_upload(logged_in_client, tmp_path):
    import zipfile

    docx_path = tmp_path / "policy.docx"
    with zipfile.ZipFile(docx_path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")

    page = logged_in_client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    with open(docx_path, "rb") as fh:
        files = {
            "file": (
                "policy.docx",
                fh.read(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        }
    response = logged_in_client.post(
        "/policies",
        data={"title": "Acceptable Use Policy", "status": "draft", "csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "created" in response.headers["location"]


def test_invalid_extension_rejected(logged_in_client):
    page = logged_in_client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("policy.txt", b"plain text", "text/plain")}
    response = logged_in_client.post(
        "/policies",
        data={"title": "Bad Extension Policy", "status": "draft", "csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    listing = logged_in_client.get("/policies")
    assert b"Bad Extension Policy" not in listing.content


def test_spoofed_pdf_rejected(logged_in_client):
    page = logged_in_client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("fake.pdf", FAKE_PDF, "application/pdf")}
    response = logged_in_client.post(
        "/policies",
        data={"title": "Spoofed Policy", "status": "draft", "csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_oversize_upload_rejected(logged_in_client, app, monkeypatch):
    app.state.settings.max_upload_mb = 0  # any non-empty file now exceeds the cap

    page = logged_in_client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("policy.pdf", VALID_PDF, "application/pdf")}
    response = logged_in_client.post(
        "/policies",
        data={"title": "Too Big Policy", "status": "draft", "csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "maximum" in response.headers["location"].lower()


def test_path_traversal_filename_handled_safely(logged_in_client, app):
    page = logged_in_client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("../../../etc/passwd.pdf", VALID_PDF, "application/pdf")}
    response = logged_in_client.post(
        "/policies",
        data={"title": "Traversal Policy", "status": "draft", "csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    policy_id = response.headers["location"].split("?")[0].rsplit("/", 1)[-1]

    data_dir = app.state.settings.data_dir
    for root, _dirs, files_on_disk in os.walk(os.path.join(data_dir, "policies", policy_id)):
        for name in files_on_disk:
            assert ".." not in name
            assert "etc" not in os.path.join(root, name).replace(data_dir, "")


def test_immutable_version_numbering_and_history(logged_in_client):
    policy_id = _create_policy_with_pdf(logged_in_client)

    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("policy-v2.pdf", VALID_PDF, "application/pdf")}
    response = logged_in_client.post(
        f"/policies/{policy_id}/versions",
        data={"change_note": "second version", "csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Version+2" in response.headers["location"]

    detail = logged_in_client.get(f"/policies/{policy_id}")
    assert b"v1" in detail.content
    assert b"v2" in detail.content
    assert b"policy.pdf" in detail.content
    assert b"policy-v2.pdf" in detail.content


def test_download_requires_auth_and_has_correct_headers(logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    detail = logged_in_client.get(f"/policies/{policy_id}")
    version_id = detail.text.split("/versions/")[1].split("/download")[0]

    response = logged_in_client.get(f"/policies/{policy_id}/versions/{version_id}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "policy.pdf" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"

    from fastapi.testclient import TestClient

    anonymous_client = TestClient(app, follow_redirects=False)
    unauth = anonymous_client.get(f"/policies/{policy_id}/versions/{version_id}/download")
    assert unauth.status_code == 303
    assert unauth.headers["location"] == "/login"


def test_failed_upload_cleans_temporary_files(logged_in_client, app):
    page = logged_in_client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("fake.pdf", FAKE_PDF, "application/pdf")}
    logged_in_client.post(
        "/policies",
        data={"title": "Cleanup Policy", "status": "draft", "csrf_token": csrf_token},
        files=files,
    )

    tmp_dir = os.path.join(app.state.settings.data_dir, "tmp")
    leftover = os.listdir(tmp_dir) if os.path.isdir(tmp_dir) else []
    assert leftover == []
