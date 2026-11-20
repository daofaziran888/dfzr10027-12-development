"""实验管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "experiment"
LIST_FIELDS = ["实验编号", "实验假设", "分组方案", "对照组比例", "核心指标", "最小样本", "显著性水平", "实验周期"]
REQUIRED_FIELDS = ["实验编号", "实验假设", "分组方案"]
STATUS_ORDER = ["设计阶段", "运行中", "已完成", "已终止"]
ACTION_RULES = {"启动实验": "运行中", "终止实验": "已完成", "归档结果": "已终止"}
NEGATIVE_ACTIONS = []


class ExperimentService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("实验编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        # 最小样本按组分计，登记时就挡住非法值，避免运行中才发现门槛不成立。
        min_sample = values.get("最小样本")
        if min_sample not in (None, ""):
            try:
                if int(float(str(min_sample))) <= 0:
                    raise ValueError
            except ValueError:
                return None, ["最小样本（需为大于 0 的整数，按组计）"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def update_min_sample(self, entry_id: int, value: Any) -> tuple[dict[str, Any] | None, str]:
        """更新按组最小样本量；就绪校验会立刻按新门槛重算。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"AB实验 {entry_id} 不存在或已归档"
        try:
            parsed = int(float(str(value)))
        except (TypeError, ValueError):
            return None, "最小样本量必须是大于 0 的整数（按组计）"
        if parsed <= 0:
            return None, "最小样本量必须是大于 0 的整数（按组计）"
        entry["最小样本"] = str(parsed)
        return entry, f"最小样本量已设为每组 {parsed} 人"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"AB实验 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于实验管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"AB实验已{action}"
