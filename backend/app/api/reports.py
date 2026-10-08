from datetime import datetime, time, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.models import Shift, User
from app.security import get_current_user
from app.services.reports import ReportPeriod, export_workbook, order_report, order_sheets, period_report, report_sheets
from app.services.review import generate_summary


router = APIRouter(prefix="/reports", tags=["reports"])
LOCAL_TIMEZONE = timezone(timedelta(hours=5))


def get_period(
    from_at: datetime | None = Query(None),
    to_at: datetime | None = Query(None),
    section_id: str | None = Query(None),
    equipment_id: str | None = Query(None),
    employee_id: str | None = Query(None),
    brigade_id: str | None = Query(None),
    shift_id: str | None = Query(None),
    session: Session = Depends(get_db),
) -> ReportPeriod:
    if shift_id:
        shift = session.get(Shift, shift_id)
        if shift is None:
            raise HTTPException(404, "Смена не найдена")
        if from_at is not None or to_at is not None:
            raise HTTPException(422, "Выберите смену или произвольный период")
        # This shift supplies the period and roster; carryover orders are included.
        start, end = shift.starts_at, shift.ends_at
    else:
        if (from_at is None) != (to_at is None):
            raise HTTPException(422, "Укажите обе границы периода")
        start = from_at or datetime.combine(utcnow().astimezone(LOCAL_TIMEZONE).date(), time.min, LOCAL_TIMEZONE)
        end = to_at or start + timedelta(days=1)
    try:
        return ReportPeriod(start, end, section_id, equipment_id, employee_id, brigade_id, shift_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _build(session: Session, user: User, period: ReportPeriod) -> dict:
    try:
        return period_report(session, user, period, utcnow())
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None


def _order(session: Session, user: User, order_id: str) -> dict:
    try:
        return order_report(session, order_id, user, utcnow())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None


@router.get("/orders/{order_id}")
def get_order_report(order_id: str, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _order(session, user, order_id)


@router.get("/orders/{order_id}/export.xlsx")
def export_order_report(order_id: str, session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    report = _order(session, user, order_id)
    return _xlsx(export_workbook(order_sheets(report)), f"order-{report['number']}.xlsx")


@router.get("/shift")
def get_shift_report(period: ReportPeriod = Depends(get_period), session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _build(session, user, period)


@router.post("/summary")
def generate_shift_summary(period: ReportPeriod = Depends(get_period), session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    report = _build(session, user, period)
    facts = {"counts": report["counts"], "anomaly_count": len(report["anomalies"]), "synthetic": report["synthetic"]}
    session.commit()
    summary = generate_summary(facts)
    return {"period": report["period"], "filters": report["filters"], "scope": report["scope"], "counts": report["counts"], "summary": summary}


@router.post("/ratings/{target_employee_id}/explanation")
def generate_rating_explanation(target_employee_id: str, period: ReportPeriod = Depends(get_period), session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    employee_id = target_employee_id
    if user.role == "worker" and employee_id != user.id:
        raise HTTPException(403, "Исполнитель может видеть только свой рейтинг")
    if period.employee_id and period.employee_id != employee_id:
        raise HTTPException(422, "Исполнитель не совпадает с фильтром отчёта")
    selected = ReportPeriod(period.start, period.end, period.section_id, period.equipment_id, employee_id, period.brigade_id, period.shift_id)
    report = _build(session, user, selected)
    row = next((item for item in report["ratings"]["employees"] if item["employee_id"] == employee_id), None)
    if row is None:
        raise HTTPException(404, "Нет данных рейтинга за этот период")
    facts = {key: row[key] for key in ("score", "earned_points", "available_weight", "quality_average", "counts", "components", "complexity", "provisional")}
    facts["synthetic"] = report["synthetic"]
    session.commit()
    return {"period": report["period"], "filters": report["filters"], "scope": report["scope"], "employee_id": employee_id, "rating": row, "explanation": generate_summary(facts)}


@router.get("/ratings")
def get_ratings(period: ReportPeriod = Depends(get_period), session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    report = _build(session, user, period)
    return {"period": report["period"], "filters": report["filters"], "scope": report["scope"], **report["ratings"]}


@router.get("/materials")
def get_materials_report(period: ReportPeriod = Depends(get_period), session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    report = _build(session, user, period)
    return {"period": report["period"], "filters": report["filters"], "scope": report["scope"], **report["materials"]}


@router.get("/downtime")
def get_downtime_report(period: ReportPeriod = Depends(get_period), session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    report = _build(session, user, period)
    return {"period": report["period"], "filters": report["filters"], "scope": report["scope"], "rows": report["downtime"], "total_minutes": report["counts"]["equipment_downtime_minutes"]}


@router.get("/anomalies")
def get_anomalies_report(period: ReportPeriod = Depends(get_period), session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    report = _build(session, user, period)
    return {"period": report["period"], "filters": report["filters"], "scope": report["scope"], "findings": report["anomalies"], "numeric_engine": "deterministic", "association_is_not_blame": True}


def _xlsx(content: bytes, filename: str) -> Response:
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@router.get("/export.xlsx")
def export_period_report(
    report: Literal["shift", "ratings", "materials", "downtime", "anomalies"] = "shift",
    period: ReportPeriod = Depends(get_period),
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    value = _build(session, user, period)
    return _xlsx(export_workbook(report_sheets(value, report)), f"{report}-{period.start.date().isoformat()}.xlsx")
