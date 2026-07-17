from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Risk
from tests.conftest import extract_csrf_token


def test_risk_creation(logged_in_client):
    page = logged_in_client.get("/risks")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/risks",
        data={
            "title": "Unpatched laptops",
            "likelihood": 3,
            "impact": 4,
            "owner": "it@example.com",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    listing = logged_in_client.get("/risks")
    assert b"Unpatched laptops" in listing.content


def test_blank_title_rejected(logged_in_client, app):
    page = logged_in_client.get("/risks")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/risks",
        data={"title": "   ", "likelihood": 1, "impact": 1, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        count = session.scalar(select(Risk).where(Risk.title == "   "))
    assert count is None


def test_out_of_range_likelihood_rejected(logged_in_client):
    page = logged_in_client.get("/risks")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/risks",
        data={"title": "Bad likelihood", "likelihood": 9, "impact": 1, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    listing = logged_in_client.get("/risks")
    assert b"Bad likelihood" not in listing.content


def test_out_of_range_impact_rejected(logged_in_client):
    page = logged_in_client.get("/risks")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/risks",
        data={"title": "Bad impact", "likelihood": 1, "impact": 0, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_db_level_check_constraints_reject_direct_orm_insert(app):
    with app.state.session_factory() as session:
        session.add(Risk(title="Direct insert", likelihood=99, impact=1))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
