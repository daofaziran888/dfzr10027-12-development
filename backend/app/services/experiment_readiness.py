"""AB实验就绪校验与统计发布引擎。

业务口径：

- 一条采集记录属于「实验 × 观测期 × 分组」；同一次检验只取**同一个观测期**的两组记录，
  不同观测期迟到的批次各自留在自己的观测期里，绝不拼接。
- 对照组合实验组**各自**达到该实验设定的最小样本量（``最小样本``，按组计），
  本期才算就绪；未就绪不得发布显著性结果。
- 就绪后执行双比例 z 检验与 Cohen's h，产物归档到 ``experiment_stat_result``，
  并由统计管道按期同步到数据集市（``data_mart``）。
- 数据集中未就绪的实验保留「缺数组别」行，但不带提升量、不参与排名。
"""
from __future__ import annotations

import math
from datetime import datetime
from statistics import NormalDist
from typing import Any

from app.fixtures import EXPERIMENT_RESULTS
from app.store import store

CONTROL = "control"
TREATMENT = "treatment"
GROUPS = [CONTROL, TREATMENT]
GROUP_LABELS = {CONTROL: "对照组", TREATMENT: "实验组"}
GROUP_ALIASES = {
    "control": CONTROL,
    "treatment": TREATMENT,
    "对照组": CONTROL,
    "实验组": TREATMENT,
}

READY = "已就绪"
NOT_READY = "样本不足"
NO_DATA = "无采集数据"

REVIEW_NOT_READY = "样本未到齐"
REVIEW_SIGNIFICANT = "已检验-显著"
REVIEW_NOT_SIGNIFICANT = "已检验-不显著"
REVIEW_NO_DATA = "无采集数据"

DEFAULT_MIN_SAMPLE = 500
DEFAULT_ALPHA = 0.05

OBSERVATION_TABLE = "experiment_observation"
ARCHIVE_TABLE = "experiment_stat_result"
MART_TABLE = "data_mart"

_KEY_ALIASES = {
    "实验编号": ("实验编号", "experiment_code", "code"),
    "观测期": ("观测期", "period", "observation_period", "date", "日期"),
    "分组": ("分组", "group", "arm", "variant"),
    "批次号": ("批次号", "batch_id", "batch", "batch_no"),
    "样本量": ("样本量", "sample_size", "samples", "n"),
    "转化人数": ("转化人数", "conversions", "converted", "conversion"),
    "批次状态": ("批次状态", "batch_status", "status"),
}


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------

def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pick(payload: dict[str, Any], field: str) -> Any:
    for key in _KEY_ALIASES[field]:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def normalize_group(value: Any) -> str | None:
    """把中英文分组写法统一成 control / treatment，无法识别返回 None。"""
    text = str(value or "").strip()
    return GROUP_ALIASES.get(text) or GROUP_ALIASES.get(text.casefold())


def normalize_period(value: Any) -> str | None:
    """观测期统一成 ISO 日期（YYYY-MM-DD）；非法日期返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_batch_status(value: Any) -> str:
    text = str(value or "").strip()
    return text or "已到达"


def _is_arrived(batch: dict[str, Any]) -> bool:
    status = str(batch.get("批次状态") or "已到达")
    return status not in {"已撤回", "撤回", "作废", "cancelled", "canceled"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 实验配置
# ---------------------------------------------------------------------------

def find_experiment(experiment_code: str) -> dict[str, Any] | None:
    for row in store.rows("experiment"):
        if str(row.get("实验编号") or "") == experiment_code:
            return row
    return None


def experiment_name(experiment_code: str, record: dict[str, Any] | None = None) -> str:
    record = record if record is not None else find_experiment(experiment_code)
    if record:
        return str(record.get("实验假设") or record.get("实验编号") or experiment_code)
    return experiment_code


def min_sample_per_group(experiment_code: str, record: dict[str, Any] | None = None) -> int:
    """读取该实验按组的最小样本门槛；配置缺失或非法时回退默认值。"""
    record = record if record is not None else find_experiment(experiment_code)
    if not record:
        return DEFAULT_MIN_SAMPLE
    raw = record.get("最小样本")
    value = _to_int(raw, 0)
    return value if value > 0 else DEFAULT_MIN_SAMPLE


def alpha_level(experiment_code: str, record: dict[str, Any] | None = None) -> float:
    record = record if record is not None else find_experiment(experiment_code)
    if not record:
        return DEFAULT_ALPHA
    value = _to_float(record.get("显著性水平"), DEFAULT_ALPHA)
    return value if 0 < value < 1 else DEFAULT_ALPHA


def set_min_sample(entry_id: int, value: Any) -> tuple[dict[str, Any] | None, str]:
    """修改某条实验记录的按组最小样本门槛。"""
    from app.services.experiment import ExperimentService

    return ExperimentService().update_min_sample(entry_id, value)


# ---------------------------------------------------------------------------
# 采集数据
# ---------------------------------------------------------------------------

def ingest_observation(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """登记一批采集数据。

    相同「实验 + 观测期 + 分组 + 批次号」视为同一批次的重复到达，覆盖而不是累加，
    这样重试不会把样本量算重；不同批次号才累加。
    """
    experiment_code = str(_pick(payload, "实验编号") or "").strip()
    if not experiment_code:
        return None, "缺少实验编号"
    period = normalize_period(_pick(payload, "观测期"))
    if period is None:
        return None, "观测期格式应为 YYYY-MM-DD"
    group = normalize_group(_pick(payload, "分组"))
    if group is None:
        return None, "分组只能是 control/treatment（或 对照组/实验组）"

    sample_size = _to_int(_pick(payload, "样本量"), -1)
    if sample_size < 0:
        return None, "样本量必须是非负整数"
    # 转化人数缺省按 0 处理（只采集了曝光、还没回传转化也合法）。
    conversions = _to_int(_pick(payload, "转化人数"), 0)
    if conversions < 0:
        return None, "转化人数必须是非负整数"
    if conversions > sample_size:
        return None, "转化人数不能大于样本量"

    batch_id = str(_pick(payload, "批次号") or "").strip()
    if not batch_id:
        batch_id = f"{period}-{group}-{sample_size}-{conversions}-{_now()}"

    row = {
        "实验编号": experiment_code,
        "观测期": period,
        "分组": group,
        "批次号": batch_id,
        "样本量": sample_size,
        "转化人数": conversions,
        "批次状态": normalize_batch_status(_pick(payload, "批次状态")),
        "updated_at": _now(),
    }

    rows = store.rows(OBSERVATION_TABLE)
    for existing in rows:
        if (
            str(existing.get("实验编号")) == experiment_code
            and str(existing.get("观测期")) == period
            and normalize_group(existing.get("分组")) == group
            and str(existing.get("批次号")) == batch_id
        ):
            existing.update(row)
            return existing, "采集批次已更新（同一批次不重复累加）"

    row["id"] = max((_to_int(item.get("id")) for item in rows), default=0) + 1
    rows.append(row)
    return row, "采集批次已登记"


def _fixture_batches(experiment_code: str) -> list[dict[str, Any]]:
    """没有真实采集记录时，用 fixtures 的分组结果作为历史观测批次。"""
    batches: list[dict[str, Any]] = []
    for index, item in enumerate(EXPERIMENT_RESULTS):
        if str(item.get("实验编号")) != experiment_code:
            continue
        period = normalize_period(item.get("日期"))
        if period is None:
            continue
        batches.append({
            "实验编号": experiment_code,
            "观测期": period,
            "分组": normalize_group(item.get("分组")),
            "批次号": f"fixture-{experiment_code}-{period}-{item.get('分组')}-{index}",
            "样本量": _to_int(item.get("样本量")),
            "转化人数": _to_int(item.get("转化人数")),
            "批次状态": "已到达",
            "来源": "fixture",
        })
    return batches


def _observation_table_has_data() -> bool:
    return any(str(row.get("实验编号") or "").strip() for row in store.rows(OBSERVATION_TABLE))


def observation_batches(experiment_code: str) -> tuple[list[dict[str, Any]], str]:
    """返回实验的采集批次与来源标记（observation / fixture）。

    按实验回退：某个实验登记过采集批次就用采集表，没有才用 fixtures 的历史分组结果。
    这样只给部分实验补采集数据，不会把其它实验的历史结果一起抹掉。
    """
    rows = [
        row for row in store.rows(OBSERVATION_TABLE)
        if str(row.get("实验编号")) == experiment_code
    ]
    if rows:
        return [row for row in rows if _is_arrived(row)], "observation"
    return _fixture_batches(experiment_code), "fixture"


# ---------------------------------------------------------------------------
# 统计检验
# ---------------------------------------------------------------------------

def compute_statistics(
    sample_control: int,
    conv_control: int,
    sample_treatment: int,
    conv_treatment: int,
    alpha: float,
) -> dict[str, Any] | None:
    """双比例 z 检验 + Cohen's h；两组缺一不可，否则返回 None。"""
    if sample_control <= 0 or sample_treatment <= 0:
        return None

    p_control = conv_control / sample_control
    p_treatment = conv_treatment / sample_treatment
    pooled = (conv_control + conv_treatment) / (sample_control + sample_treatment)
    variance = pooled * (1 - pooled) * (1 / sample_control + 1 / sample_treatment)
    se = math.sqrt(variance) if variance > 0 else 0.0
    z_score = (p_treatment - p_control) / se if se > 0 else 0.0
    normal = NormalDist()
    p_value = 2 * (1 - normal.cdf(abs(z_score))) if se > 0 else 1.0
    cohen_h = 2 * (math.asin(math.sqrt(p_treatment)) - math.asin(math.sqrt(p_control)))

    return {
        "control_rate": round(p_control, 6),
        "treatment_rate": round(p_treatment, 6),
        "absolute_lift": round(p_treatment - p_control, 6),
        "relative_lift": round((p_treatment - p_control) / p_control, 6) if p_control else None,
        "z_score": round(z_score, 6),
        "p_value": round(p_value, 6),
        "cohen_h": round(cohen_h, 6),
        "significant": p_value < alpha,
        "alpha": alpha,
        "critical_z": round(normal.inv_cdf(1 - alpha / 2), 4),
    }


# ---------------------------------------------------------------------------
# 观测期校验
# ---------------------------------------------------------------------------

def _empty_group() -> dict[str, Any]:
    return {"sample_size": 0, "conversions": 0, "batch_count": 0}


def evaluate_period(
    experiment_code: str,
    period: str,
    batches: list[dict[str, Any]],
    threshold: int,
    alpha: float,
) -> dict[str, Any]:
    """对单个观测期独立汇总两组样本并判断就绪；不跨期合并。"""
    groups = {group: _empty_group() for group in GROUPS}
    for batch in batches:
        group = normalize_group(batch.get("分组"))
        if group is None:
            continue
        sample = max(0, _to_int(batch.get("样本量")))
        conversions = min(max(0, _to_int(batch.get("转化人数"))), sample)
        groups[group]["sample_size"] += sample
        groups[group]["conversions"] += conversions
        groups[group]["batch_count"] += 1

    missing_groups = [g for g in GROUPS if groups[g]["sample_size"] <= 0]
    insufficient_groups = [
        g for g in GROUPS
        if 0 < groups[g]["sample_size"] < threshold
    ]
    missing_or_short = [g for g in GROUPS if g in missing_groups or g in insufficient_groups]
    has_both = not missing_groups
    ready = has_both and not insufficient_groups

    statistics = None
    if has_both:
        statistics = compute_statistics(
            groups[CONTROL]["sample_size"], groups[CONTROL]["conversions"],
            groups[TREATMENT]["sample_size"], groups[TREATMENT]["conversions"],
            alpha,
        )

    group_detail: dict[str, Any] = {}
    for group in GROUPS:
        sample = groups[group]["sample_size"]
        conversions = groups[group]["conversions"]
        group_detail[group] = {
            "label": GROUP_LABELS[group],
            "sample_size": sample,
            "conversions": conversions,
            "rate": round(conversions / sample, 6) if sample else None,
            "batch_count": groups[group]["batch_count"],
            "threshold": threshold,
            "threshold_met": sample >= threshold,
            "gap": max(0, threshold - sample),
            "present": sample > 0,
        }

    if not any(groups[g]["batch_count"] for g in GROUPS):
        status = NO_DATA
    elif ready:
        status = READY
    else:
        status = NOT_READY

    return {
        "experiment_code": experiment_code,
        "observation_period": period,
        "min_sample_per_group": threshold,
        "alpha": alpha,
        "groups": group_detail,
        "missing_groups": [GROUP_LABELS[g] for g in missing_groups],
        "missing_group_codes": missing_groups,
        "insufficient_groups": [GROUP_LABELS[g] for g in insufficient_groups],
        "insufficient_group_codes": insufficient_groups,
        "缺数组别": missing_or_short,
        "缺数组别名称": [GROUP_LABELS[g] for g in missing_or_short],
        "ready": ready,
        "readiness_status": status,
        "statistics": statistics,
    }


def _group_batches_by_period(batches: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for batch in batches:
        period = normalize_period(batch.get("观测期"))
        if period is None:
            continue
        grouped.setdefault(period, []).append(batch)
    return grouped


def build_readiness(experiment_code: str) -> dict[str, Any]:
    """生成单个实验的就绪数据集。

    「本期」的判定：
    1. 优先取**最新一个两组都有到达记录**的观测期；
    2. 若没有任何观测期同时存在两组（例如对照组整批迟到），取最早的观测期作为本期，
       更晚的观测期只可能是提前到达的下一期数据，不能反客为主。
    任何情况下都只使用该观测期自己的批次，不跨期合并。
    """
    record = find_experiment(experiment_code)
    threshold = min_sample_per_group(experiment_code, record)
    alpha = alpha_level(experiment_code, record)
    batches, source = observation_batches(experiment_code)
    grouped = _group_batches_by_period(batches)

    periods: list[dict[str, Any]] = []
    for period in sorted(grouped, reverse=True):
        periods.append(evaluate_period(experiment_code, period, grouped[period], threshold, alpha))

    with_both = [item for item in periods if not item["missing_group_codes"]]
    if with_both:
        current = with_both[0]
    elif periods:
        current = periods[-1]  # periods 倒序，最后一项即最早观测期
    else:
        current = None

    pending_periods: list[dict[str, Any]] = []
    for item in periods:
        if current is not None and item["observation_period"] == current["observation_period"]:
            continue
        pending_periods.append({
            "observation_period": item["observation_period"],
            "missing_groups": item["missing_groups"],
            "missing_group_codes": item["missing_group_codes"],
            "insufficient_groups": item["insufficient_groups"],
            "groups": item["groups"],
            "ready": item["ready"],
            "note": "不同观测期的批次不并入本期检验" if item["missing_group_codes"] else "独立观测期，单独校验",
        })

    if current is None:
        readiness = {
            "experiment_code": experiment_code,
            "experiment_name": experiment_name(experiment_code, record),
            "min_sample_per_group": threshold,
            "alpha": alpha,
            "observation_period": None,
            "period_source": source,
            "groups": {group: {
                "label": GROUP_LABELS[group], "sample_size": 0, "conversions": 0,
                "rate": None, "batch_count": 0, "threshold": threshold,
                "threshold_met": False, "gap": threshold, "present": False,
            } for group in GROUPS},
            "missing_groups": [GROUP_LABELS[g] for g in GROUPS],
            "missing_group_codes": list(GROUPS),
            "insufficient_groups": [],
            "insufficient_group_codes": [],
            "缺数组别": list(GROUPS),
            "缺数组别名称": [GROUP_LABELS[g] for g in GROUPS],
            "ready": False,
            "readiness_status": NO_DATA,
            "statistics": None,
            "effective_rates": None,
            "lift": None,
            "relative_lift": None,
            "published_significance": None,
            "review_status": REVIEW_NO_DATA,
            "conclusion": "无采集数据，尚不能进入转化率检验",
            "periods": [],
            "pending_periods": [],
            "cross_period_batches": [],
        }
        return readiness

    statistics = current["statistics"]
    ready = current["ready"]
    effective_rates = None
    lift = None
    relative_lift = None
    if ready and statistics is not None:
        total_sample = sum(current["groups"][g]["sample_size"] for g in GROUPS)
        total_conv = sum(current["groups"][g]["conversions"] for g in GROUPS)
        # 有效转化率与提升量都是「按期」口径：两组都达标才成立，
        # 未达标时只保留各组样本进度，不对外给出期级结论。
        effective_rates = {
            "control": statistics["control_rate"],
            "treatment": statistics["treatment_rate"],
            "overall": round(total_conv / total_sample, 6) if total_sample else None,
        }
        lift = statistics["absolute_lift"]
        relative_lift = statistics["relative_lift"]

    published_significance = statistics["significant"] if (ready and statistics) else None

    if ready and statistics:
        review_status = REVIEW_SIGNIFICANT if statistics["significant"] else REVIEW_NOT_SIGNIFICANT
        conclusion = (
            "检验完成：提升显著，可发布显著性结果"
            if statistics["significant"]
            else "检验完成：提升不显著（样本已达标，结论是检验结果，不是样本不足）"
        )
    elif current["readiness_status"] == NO_DATA:
        review_status = REVIEW_NO_DATA
        conclusion = "无采集数据，尚不能进入转化率检验"
    else:
        review_status = REVIEW_NOT_READY
        parts: list[str] = []
        for code in current["缺数组别"]:
            detail = current["groups"][code]
            if not detail["present"]:
                parts.append(f"{detail['label']}缺数据")
            else:
                parts.append(f"{detail['label']}样本不足（{detail['sample_size']}/{threshold}）")
        conclusion = f"样本未到齐：{'、'.join(parts)}，暂不能发布显著性结果"

    cross_period = [
        {
            "observation_period": item["observation_period"],
            "groups": {
                g: {"sample_size": item["groups"][g]["sample_size"], "threshold_met": item["groups"][g]["threshold_met"]}
                for g in GROUPS
            },
            "missing_group_codes": item["missing_group_codes"],
        }
        for item in pending_periods
    ]

    return {
        "experiment_code": experiment_code,
        "experiment_name": experiment_name(experiment_code, record),
        "min_sample_per_group": threshold,
        "alpha": alpha,
        "observation_period": current["observation_period"],
        "period_source": source,
        "groups": current["groups"],
        "missing_groups": current["missing_groups"],
        "missing_group_codes": current["missing_group_codes"],
        "insufficient_groups": current["insufficient_groups"],
        "insufficient_group_codes": current["insufficient_group_codes"],
        "缺数组别": current["缺数组别"],
        "缺数组别名称": current["缺数组别名称"],
        "ready": ready,
        "readiness_status": current["readiness_status"],
        "statistics": statistics,
        "effective_rates": effective_rates,
        "lift": lift,
        "relative_lift": relative_lift,
        "published_significance": published_significance,
        "review_status": review_status,
        "conclusion": conclusion,
        "periods": periods,
        "pending_periods": pending_periods,
        "cross_period_batches": cross_period,
        "updated_at": _now(),
    }


def analysis_codes() -> list[str]:
    """参与本期实验分析的全部实验编号。

    取采集表、fixtures、检验档案、集市实验行的并集；未采集且未同步的实验不进入分析，
    避免把从未参与本期的实验算进排名覆盖分母。
    """
    codes: set[str] = set()
    for row in store.rows(OBSERVATION_TABLE):
        code = str(row.get("实验编号") or "").strip()
        if code:
            codes.add(code)
    for row in EXPERIMENT_RESULTS:
        code = str(row.get("实验编号") or "").strip()
        if code:
            codes.add(code)
    for table in (ARCHIVE_TABLE, MART_TABLE):
        for row in store.rows(table):
            code = str(row.get("实验编号") or "").strip()
            if code:
                codes.add(code)
    return sorted(codes)


def list_readiness() -> list[dict[str, Any]]:
    return [build_readiness(code) for code in analysis_codes()]


# ---------------------------------------------------------------------------
# 归档与集市同步
# ---------------------------------------------------------------------------

def _upsert(table: str, key_fields: list[str], payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    rows = store.rows(table)
    for row in rows:
        if all(str(row.get(field)) == str(payload.get(field)) for field in key_fields):
            row.update(payload)
            row["updated_at"] = _now()
            return row, False
    entry = {"id": max((_to_int(row.get("id")) for row in rows), default=0) + 1}
    entry.update(payload)
    entry["updated_at"] = _now()
    rows.append(entry)
    return entry, True


def _archive_payload(readiness: dict[str, Any]) -> dict[str, Any]:
    stats = readiness.get("statistics") or {}
    return {
        "实验编号": readiness["experiment_code"],
        "实验假设": readiness["experiment_name"],
        "观测期": readiness["observation_period"],
        "就绪状态": readiness["readiness_status"],
        "复盘状态": readiness["review_status"],
        "结论": readiness["conclusion"],
        "最小样本": readiness["min_sample_per_group"],
        "显著性水平": readiness["alpha"],
        "对照组样本量": readiness["groups"][CONTROL]["sample_size"],
        "实验组样本量": readiness["groups"][TREATMENT]["sample_size"],
        "对照组转化人数": readiness["groups"][CONTROL]["conversions"],
        "实验组转化人数": readiness["groups"][TREATMENT]["conversions"],
        "缺数组别": readiness["缺数组别"],
        "缺数组别名称": readiness["缺数组别名称"],
        "可发布显著性": readiness["published_significance"] is not None,
        "显著": stats.get("significant") if readiness["ready"] else None,
        "提升量": readiness["lift"] if readiness["ready"] else None,
        # 组级观测转化率是原始事实，始终保留；期级「有效转化率」未就绪时不给。
        "控制组转化率": stats.get("control_rate"),
        "实验组转化率": stats.get("treatment_rate"),
        "有效转化率": (readiness.get("effective_rates") or {}).get("overall"),
        "z值": stats.get("z_score"),
        "p值": stats.get("p_value"),
        "Cohen_h": stats.get("cohen_h"),
        "periods": [
            {
                "观测期": item["observation_period"],
                "ready": item["ready"],
                "缺数组别": item["缺数组别"],
            }
            for item in readiness.get("periods", [])
        ],
    }


def _mart_payload(readiness: dict[str, Any]) -> dict[str, Any]:
    ready = readiness["ready"]
    stats = readiness.get("statistics") or {}
    rates = readiness.get("effective_rates") or {}
    total_sample = sum(readiness["groups"][g]["sample_size"] for g in GROUPS)
    return {
        "status": "已上线" if ready else "建设中",
        "pending": not ready,
        "abnormal": False,
        "主题编号": f"DATA-STAT-{readiness['experiment_code']}",
        "主题名称": f"{readiness['experiment_code']} 实验统计",
        "业务域": "增长域",
        "事实表": "fact_experiment",
        "维度集合": "DIM-CHANNEL",
        "刷新周期": "日",
        "数据量": str(total_sample),
        "负责人": "实验平台",
        "region": "all",
        "channel": "all",
        "日期": readiness["observation_period"],
        "观测期": readiness["observation_period"],
        "实验编号": readiness["experiment_code"],
        "实验假设": readiness["experiment_name"],
        "就绪状态": readiness["readiness_status"],
        "复盘状态": readiness["review_status"],
        "结论": readiness["conclusion"],
        "最小样本": readiness["min_sample_per_group"],
        "显著性水平": readiness["alpha"],
        "对照组样本量": readiness["groups"][CONTROL]["sample_size"],
        "实验组样本量": readiness["groups"][TREATMENT]["sample_size"],
        "对照组转化人数": readiness["groups"][CONTROL]["conversions"],
        "实验组转化人数": readiness["groups"][TREATMENT]["conversions"],
        "控制组转化率": stats.get("control_rate"),
        "实验组转化率": stats.get("treatment_rate"),
        "有效转化率": rates.get("overall"),
        "提升量": readiness["lift"] if ready else None,
        "相对提升": readiness["relative_lift"] if ready else None,
        "显著性": stats.get("significant") if ready else None,
        "缺数组别": readiness["缺数组别"],
        "缺数组别名称": readiness["缺数组别名称"],
        "可参与排名": ready,
        "排名分数": readiness["lift"] if ready else None,
        "统计状态": readiness["review_status"],
        "z值": stats.get("z_score"),
        "p值": stats.get("p_value"),
        "Cohen_h": stats.get("cohen_h"),
    }


def sync_experiment(experiment_code: str) -> dict[str, Any]:
    """计算一个实验的就绪状态，归档检验结果并同步数据集市（同一观测期幂等覆盖）。"""
    readiness = build_readiness(experiment_code)
    archive, _ = _upsert(
        ARCHIVE_TABLE,
        ["实验编号", "观测期"],
        _archive_payload(readiness),
    )
    mart_row = None
    mart_created = False
    if readiness["observation_period"] is not None:
        mart_row, mart_created = _upsert(
            MART_TABLE,
            ["实验编号", "观测期"],
            _mart_payload(readiness),
        )
    return {
        "readiness": readiness,
        "archive": archive,
        "mart_row": mart_row,
        "mart_created": mart_created,
    }


def sync_all() -> dict[str, Any]:
    results = [sync_experiment(code) for code in analysis_codes()]
    return {
        "generated_at": _now(),
        "experiment_count": len(results),
        "synced": len([item for item in results if item["mart_row"] is not None]),
        "results": results,
    }


# ---------------------------------------------------------------------------
# 复盘记录
# ---------------------------------------------------------------------------

def build_review(experiment_code: str) -> dict[str, Any]:
    """复盘记录：显式区分「样本未到齐」与「已检验但不显著」。"""
    readiness = build_readiness(experiment_code)
    stats = readiness.get("statistics") if readiness["ready"] else None
    gaps: list[dict[str, Any]] = []
    for group in GROUPS:
        detail = readiness["groups"][group]
        if group not in readiness["缺数组别"]:
            continue
        gaps.append({
            "分组": group,
            "分组名称": detail["label"],
            "原因": "无数据" if not detail["present"] else "样本不足",
            "样本量": detail["sample_size"],
            "门槛": readiness["min_sample_per_group"],
            "缺口": detail["gap"],
        })

    rank = None
    for item in build_ranking().get("items", []):
        if item["实验编号"] == experiment_code:
            rank = item["排名"]
            break

    return {
        "实验编号": experiment_code,
        "实验假设": readiness["experiment_name"],
        "观测期": readiness["observation_period"],
        "复盘状态": readiness["review_status"],
        "结论": readiness["conclusion"],
        "可发布显著性": readiness["published_significance"] is not None,
        "就绪状态": readiness["readiness_status"],
        "最小样本": readiness["min_sample_per_group"],
        "显著性水平": readiness["alpha"],
        "缺数组别": readiness["缺数组别"],
        "缺数组别名称": readiness["缺数组别名称"],
        "缺口明细": gaps,
        "样本": {
            group: {
                "label": readiness["groups"][group]["label"],
                "sample_size": readiness["groups"][group]["sample_size"],
                "conversions": readiness["groups"][group]["conversions"],
                "rate": readiness["groups"][group]["rate"],
                "threshold_met": readiness["groups"][group]["threshold_met"],
            }
            for group in GROUPS
        },
        "有效转化率": readiness.get("effective_rates"),
        "提升量": readiness["lift"] if readiness["ready"] else None,
        "统计": {
            "z值": stats["z_score"],
            "p值": stats["p_value"],
            "显著": stats["significant"],
            "Cohen_h": stats["cohen_h"],
        } if stats else None,
        "跨期数据": {
            "说明": "不同观测期的迟到批次各自保留，不并入本期检验",
            "批次": readiness.get("cross_period_batches", []),
        },
        "集市排名": rank,
        "说明": (
            "样本未达按组门槛，本期未进入转化率检验，不能发布显著性结果。"
            if readiness["review_status"] == REVIEW_NOT_READY
            else "样本已达标并完成检验，结论为不显著；这是检验结论，不是样本未到齐。"
            if readiness["review_status"] == REVIEW_NOT_SIGNIFICANT
            else "样本已达标并完成检验，提升显著，可发布。"
            if readiness["review_status"] == REVIEW_SIGNIFICANT
            else "尚无采集数据。"
        ),
    }


# ---------------------------------------------------------------------------
# 试投排名（读集市）
# ---------------------------------------------------------------------------

def _find_mart_row(experiment_code: str, period: str) -> dict[str, Any] | None:
    for row in store.rows(MART_TABLE):
        if str(row.get("实验编号") or "") != experiment_code:
            continue
        if str(row.get("观测期") or "") == period:
            return row
    return None


def _has_any_mart_row(experiment_code: str) -> bool:
    return any(str(row.get("实验编号") or "") == experiment_code for row in store.rows(MART_TABLE))


def build_ranking() -> dict[str, Any]:
    """按数据集市中的提升量排名，返回可试投名单与未参加排名的实验。

    只认本期（各实验最新的完整观测期）已同步到集市、且两组都达标的实验；
    缺少任一分组的实验保留在 ``unranked`` 中，不能贡献排名分数。
    """
    experiments = analysis_codes()
    ranked: list[dict[str, Any]] = []
    unranked: list[dict[str, Any]] = []

    for code in experiments:
        readiness = build_readiness(code)
        period = readiness["observation_period"]
        if period is None:
            unranked.append({
                "实验编号": code,
                "实验假设": readiness["experiment_name"],
                "原因": "无采集数据",
                "缺数组别": readiness["缺数组别"],
                "观测期": None,
            })
            continue

        mart = _find_mart_row(code, period)
        if mart is None:
            stale = _has_any_mart_row(code)
            unranked.append({
                "实验编号": code,
                "实验假设": readiness["experiment_name"],
                "原因": "本期统计尚未同步到集市" if not stale else "集市仅有其他观测期数据，本期未同步",
                "缺数组别": readiness["缺数组别"],
                "观测期": period,
            })
            continue

        if not readiness["ready"] or not mart.get("可参与排名"):
            unranked.append({
                "实验编号": code,
                "实验假设": readiness["experiment_name"],
                "原因": "样本未到齐" if not readiness["ready"] else "集市标记为不可排名",
                "缺数组别": readiness["缺数组别"] or mart.get("缺数组别") or [],
                "缺数组别名称": readiness["缺数组别名称"],
                "观测期": period,
                "对照组样本量": mart.get("对照组样本量"),
                "实验组样本量": mart.get("实验组样本量"),
            })
            continue

        ranked.append({
            "实验编号": code,
            "实验假设": readiness["experiment_name"],
            "观测期": period,
            "有效转化率": mart.get("有效转化率"),
            "控制组转化率": mart.get("控制组转化率"),
            "实验组转化率": mart.get("实验组转化率"),
            "提升量": mart.get("排名分数"),
            "相对提升": mart.get("相对提升"),
            "显著性": mart.get("显著性"),
            "对照组样本量": mart.get("对照组样本量"),
            "实验组样本量": mart.get("实验组样本量"),
            "缺数组别": [],
        })

    ranked.sort(key=lambda item: (-(_to_float(item.get("提升量"), 0.0)), item["实验编号"]))
    for index, item in enumerate(ranked, start=1):
        item["排名"] = index

    total = len(experiments)
    ranked_count = len(ranked)
    coverage_rate = round(ranked_count / total, 4) if total else 0.0
    return {
        "generated_at": _now(),
        "scope": "本期（每个实验各自最新一个两组齐全的观测期）",
        "items": ranked,
        "scheduled": [item["实验编号"] for item in ranked],
        "unranked": unranked,
        "coverage": {
            "total_experiments": total,
            "ranked": ranked_count,
            "unranked": len(unranked),
            "coverage_rate": coverage_rate,
        },
        "can_arrange_ready": ranked_count > 0,
        "covers_all_experiments": len(unranked) == 0,
        "ranking_usable": ranked_count > 0,
        "message": (
            f"本期可据提升量排名安排 {ranked_count} 个已就绪实验"
            + (f"；另有 {len(unranked)} 个实验未参加排名" if unranked else "；全部实验均已参加排名")
            if ranked_count
            else "本期没有已就绪并同步到集市的实验，暂不能据此安排试投"
        ),
    }
