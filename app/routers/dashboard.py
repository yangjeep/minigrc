from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models import Framework, InternalControl, Risk

router = APIRouter()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    framework_count = db.scalar(select(func.count()).select_from(Framework)) or 0
    control_count = db.scalar(select(func.count()).select_from(InternalControl)) or 0
    open_risk_count = (
        db.scalar(select(func.count()).select_from(Risk).where(Risk.status.in_(["open", "mitigating"]))) or 0
    )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "framework_count": framework_count,
            "control_count": control_count,
            "open_risk_count": open_risk_count,
        },
    )
