"""翻页管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

import re
from typing import Any

from app.store import store

MODULE = "pagemarker"
REQUIRED_FIELDS = ["规则编号", "目标站点", "翻页类型"]
OPTIONAL_FIELDS = ["起始页码", "最大页码", "页码参数名", "偏移量参数", "总条数定位器", "页大小", "规则版本"]
STATUS_ORDER = ["草稿", "规则验证中", "已启用", "已废弃"]
ACTION_RULES = {"提交规则验证": "规则验证中", "启用规则": "已启用", "废弃规则": "已废弃"}
VERSION_ACTION = "规则换版"
NEGATIVE_ACTIONS = []


def _next_version(current: str) -> str:
    """版本号递增：v1→v2；没有数字结尾时追加 -v2。"""
    match = re.match(r"^(.*?)(\d+)$", current)
    if match:
        return f"{match.group(1)}{int(match.group(2)) + 1}"
    return f"{current}-v2"


class PagemarkerService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) is not None and str(values.get(field)).strip() != "":
                entry[field] = values.get(field)
        if not str(entry.get("规则版本") or "").strip():
            entry["规则版本"] = "v1"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"翻页规则 {entry_id} 不存在或已归档"
        if action == VERSION_ACTION:
            current = str(entry.get("规则版本") or "v1").strip() or "v1"
            entry["规则版本"] = _next_version(current)
            return entry, f"翻页规则已换版为{entry['规则版本']}，历史采集范围保持不变，旧作业继续使用原范围"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于翻页管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"翻页规则已{action}"
