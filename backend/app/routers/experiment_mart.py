"""数据集市实验统计汇总接口：按期汇总有效转化率与提升量，并给出排名覆盖。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.services.experiment_readiness import build_ranking, sync_all
from app.store import store

router = APIRouter(prefix="/api/experiment-mart", tags=["实验集市汇总"])

_GROUP_CODES = ("control", "treatment")
_GROUP_LABELS = {"control": "对照组", "treatment": "实验组"}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _experiment_mart_rows() -> list[dict[str, Any]]:
    return [row for row in store.rows("data_mart") if str(row.get("实验编号") or "").strip()]


def summarize_periods() -> dict[str, Any]:
    """把集市中每个「实验 + 观测期」汇总成一行，未就绪的保留缺数组别。"""
    summaries: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _experiment_mart_rows():
        code = str(row.get("实验编号"))
        period = str(row.get("观测期") or row.get("日期") or "")
        key = (code, period)
        entry = summaries.get(key)
        if entry is None:
            entry = {
                "实验编号": code,
                "实验假设": row.get("实验假设"),
                "观测期": period,
                "就绪状态": row.get("就绪状态"),
                "复盘状态": row.get("统计状态") or row.get("复盘状态"),
                "最小样本": row.get("最小样本"),
                "对照组样本量": row.get("对照组样本量"),
                "实验组样本量": row.get("实验组样本量"),
                "对照组转化人数": row.get("对照组转化人数"),
                "实验组转化人数": row.get("实验组转化人数"),
                "控制组转化率": row.get("控制组转化率"),
                "实验组转化率": row.get("实验组转化率"),
                "有效转化率": row.get("有效转化率"),
                "提升量": row.get("提升量"),
                "相对提升": row.get("相对提升"),
                "显著性": row.get("显著性"),
                "缺数组别": row.get("缺数组别") or [],
                "缺数组别名称": row.get("缺数组别名称") or [],
                "可参与排名": bool(row.get("可参与排名")),
                "是否就绪": row.get("就绪状态") == "已就绪",
            }
            summaries[key] = entry
        # 同一实验同一观测期理论上只有一行；防御性合并取第一行口径。

    items = sorted(summaries.values(), key=lambda item: (item["观测期"] or "", item["实验编号"]), reverse=True)

    ready_items = [item for item in items if item["是否就绪"]]
    valid_rates = [item["有效转化率"] for item in ready_items if item["有效转化率"] is not None]
    lifts = [item["提升量"] for item in ready_items if item["提升量"] is not None]
    overall_rate = round(sum(valid_rates) / len(valid_rates), 6) if valid_rates else None
    overall_lift = round(sum(lifts) / len(lifts), 6) if lifts else None

    return {
        "items": items,
        "total": len(items),
        "ready": len(ready_items),
        "not_ready": len(items) - len(ready_items),
        "aggregate": {
            "valid_rate_mean": overall_rate,
            "lift_mean": overall_lift,
            "valid_rate_rows": len(valid_rates),
            "lift_rows": len(lifts),
        },
        "note": "样本不足的实验保留缺数组别，不贡献有效转化率与提升量的汇总。",
    }


@router.get("")
def get_experiment_mart(experiment_code: str | None = Query(default=None)) -> dict[str, Any]:
    """按期汇总的实验统计行；样本不足的行保留缺数组别但不带提升量。"""
    summary = summarize_periods()
    if experiment_code:
        summary["items"] = [item for item in summary["items"] if item["实验编号"] == experiment_code]
        summary["total"] = len(summary["items"])
    return summary


@router.post("/sync")
def sync_mart() -> dict[str, Any]:
    """重新执行就绪校验并同步集市，返回同步后的排名覆盖。"""
    batch = sync_all()
    ranking = build_ranking()
    return {
        "synced_experiments": batch["experiment_count"],
        "summary": summarize_periods(),
        "ranking": ranking,
    }


@router.get("/coverage")
def get_coverage() -> dict[str, Any]:
    """排名覆盖：已就绪可安排 / 未参加排名及其原因。"""
    ranking = build_ranking()
    return {
        "coverage": ranking["coverage"],
        "can_arrange_ready": ranking["can_arrange_ready"],
        "covers_all_experiments": ranking["covers_all_experiments"],
        "scheduled": ranking["scheduled"],
        "unranked": ranking["unranked"],
        "message": ranking["message"],
    }
