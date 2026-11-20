"""实验就绪接口：采集批次登记、就绪校验、统计同步、复盘记录与试投排名。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.schemas import ActionResult
from app.services.experiment_readiness import (
    OBSERVATION_TABLE,
    build_ranking,
    build_readiness,
    build_review,
    ingest_observation,
    list_readiness,
    sync_all,
    sync_experiment,
)
from app.store import store

router = APIRouter(prefix="/api/experiment-readiness", tags=["实验就绪"])


@router.get("/readiness")
def get_readiness(experiment_code: str | None = Query(default=None, description="实验编号，缺省返回全部")) -> dict[str, Any]:
    """返回实验就绪数据集：按组样本、缺数组别、本期观测期与能否发布显著性。"""
    if experiment_code:
        return build_readiness(experiment_code)
    items = list_readiness()
    return {
        "items": items,
        "total": len(items),
        "ready": len([item for item in items if item["ready"]]),
        "not_ready": len([item for item in items if not item["ready"]]),
    }


@router.post("/observations", response_model=ActionResult)
def create_observation(payload: dict[str, Any]) -> ActionResult:
    """登记一批采集数据（实验、观测期、分组、样本量、转化人数）。"""
    row, message = ingest_observation(payload)
    if row is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=row)


@router.get("/observations")
def list_observations(
    experiment_code: str | None = Query(default=None),
    period: str | None = Query(default=None),
) -> dict[str, Any]:
    """查询采集批次；可按实验编号与观测期过滤。"""
    rows = list(store.rows(OBSERVATION_TABLE))
    if experiment_code:
        rows = [row for row in rows if str(row.get("实验编号")) == experiment_code]
    if period:
        rows = [row for row in rows if str(row.get("观测期")) == period]
    return {"items": rows, "total": len(rows)}


@router.post("/sync", response_model=ActionResult)
def sync_stat(experiment_code: str | None = Query(default=None, description="实验编号，缺省同步全部")) -> ActionResult:
    """执行就绪校验并同步统计到数据集市；未就绪实验不写提升量、不发布显著性。"""
    if experiment_code:
        item = sync_experiment(experiment_code)
        readiness = item["readiness"]
        return ActionResult(
            ok=True,
            message=(
                f"{experiment_code} 已就绪并同步集市"
                if readiness["ready"]
                else f"{experiment_code} {readiness['readiness_status']}，已保留缺数组别"
            ),
            entry={
                "readiness": readiness,
                "mart_updated": item["mart_row"] is not None,
            },
        )
    batch = sync_all()
    return ActionResult(
        ok=True,
        message=f"已同步 {batch['experiment_count']} 个实验的就绪状态",
        entry=batch,
    )


@router.get("/review/{experiment_code}")
def get_review(experiment_code: str) -> dict[str, Any]:
    """复盘记录：显式区分样本未到齐与已检验不显著。"""
    return build_review(experiment_code)


@router.get("/ranking")
def get_ranking() -> dict[str, Any]:
    """按数据集市提升量生成试投排名、可安排名单与未参加排名的实验。"""
    return build_ranking()
