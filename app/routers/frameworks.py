from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models import Framework

router = APIRouter(prefix="/frameworks", tags=["frameworks"])


@router.get("")
def list_frameworks(request: Request, db: Session = Depends(get_db)):
    frameworks = db.scalars(select(Framework)).all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "frameworks/list.html", {"frameworks": frameworks})


@router.get("/{framework_id}")
def view_framework(framework_id: str, request: Request, db: Session = Depends(get_db)):
    framework = db.get(Framework, framework_id)
    if framework is None:
        raise HTTPException(status_code=404, detail="Framework not found")

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "frameworks/detail.html", {"framework": framework})
