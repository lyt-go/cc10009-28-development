"""可重放翻页采集范围业务规则。

把翻页规则（页码参数、偏移量参数、总条数）展开成一份有序、去重、可重放的
页面清单，解决抓取作业重复页、越过最大页码的问题：

- 双模式边界校验：页码边界与偏移量边界分别计算，冲突时按保守口径取较小边界，
  裁决过程留痕，保证结果唯一可解释；
- 幂等：同一查询条件（同一规则版本）重复计算命中同一范围，不生成第二份；
- 版本边界：规则换版后旧范围保留给旧作业，新范围页数变多时进入待确认，
  不允许悄悄扩大；
- 消费回执：URL过滤与抓取作业凭回执消费，回执按（范围, 消费方）幂等，
  并发回执不会多出第二份结果；
- 可重放：计算输入随范围落库，随时可重算校验范围是否完整。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "pagerange"
RULE_MODULE = "pagemarker"

CONSUMERS = ["urlfilter", "crawljob"]
CONSUMER_NAMES = {"urlfilter": "URL过滤", "crawljob": "抓取作业"}
RANGE_STATUSES = ["已生成", "待确认", "消费中", "已消费", "已废弃"]
MODES = ["页码", "偏移量", "双模式"]
MODE_ALIASES = {
    "页码": "页码",
    "页码递增": "页码",
    "偏移量": "偏移量",
    "偏移量递增": "偏移量",
    "双模式": "双模式",
}
EMPTY_RUN_THRESHOLD = 2  # 连续空页终止阈值
DEFAULT_PAGE_SIZE = 20
ACTION_RULES = {"确认扩版": "已生成", "废弃范围": "已废弃"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]


def _parse_int(value: Any, field: str, errors: list[str], default: int | None = None) -> int | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        errors.append(f"{field}「{value}」不是有效数字")
        return default


def _parse_total(value: Any, errors: list[str]) -> int | None:
    """总条数允许留空或填「未知」，表示采集时探页确定边界。"""
    if value is None or str(value).strip() in ("", "未知"):
        return None
    total = _parse_int(value, "总条数", errors)
    if total is not None and total < 0:
        errors.append(f"总条数不能为负数：{total}")
        return None
    return total


def _parse_empty_signals(value: Any, errors: list[str]) -> list[int]:
    """空页信号支持数组或逗号分隔字符串，返回去重排序后的页码列表。"""
    if value is None or value == "":
        return []
    raw_items = value if isinstance(value, list | tuple) else str(value).split(",")
    signals: set[int] = set()
    for item in raw_items:
        page = _parse_int(item, "空页信号", errors)
        if page is not None:
            signals.add(page)
    return sorted(signals)


def _parse_conditions(value: Any, errors: list[str]) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            errors.append("查询条件需为 JSON 对象，例如 {\"类目\": \"图书\"}")
            return {}
        if isinstance(parsed, dict):
            return parsed
    errors.append("查询条件需为 JSON 对象，例如 {\"类目\": \"图书\"}")
    return {}


def _page_boundary(start: int, max_page: int, size: int, total: int | None) -> tuple[list[int], str]:
    """页码边界：按总条数折算页数，页码 capped 在最大页码内。"""
    if total is None:
        return list(range(start, max_page + 1)), "达到最大页码"
    if total <= 0:
        return [], "总条数覆盖：共0条，无需翻页"
    needed = -(-total // size)
    last = min(max_page, start + needed - 1)
    reason = "总条数覆盖" if start + needed - 1 <= max_page else "达到最大页码"
    return list(range(start, last + 1)), reason


def _offset_boundary(start: int, max_page: int, size: int, total: int | None) -> tuple[list[int], str]:
    """偏移量边界：偏移量必须落在总条数内，且对应页码不超过最大页码。"""
    if total is None:
        return list(range(start, max_page + 1)), "达到最大页码"
    base = (start - 1) * size
    pages: list[int] = []
    while base + len(pages) * size < total and start + len(pages) <= max_page:
        pages.append(start + len(pages))
    if not pages:
        return [], f"偏移量越界：起始偏移{base}不在总条数{total}内"
    reason = "偏移量越界" if base + len(pages) * size >= total else "达到最大页码"
    return pages, reason


def _apply_empty_signals(pages: list[int], empty_signals: list[int]) -> tuple[list[int], str | None]:
    """总数未知时按连续空页截断：截断只裁页面，不会重复生成页面。"""
    if not empty_signals:
        return pages, None
    known = set(pages)
    run_start: int | None = None
    run_length = 0
    previous: int | None = None
    for page in sorted(p for p in empty_signals if p in known):
        if previous is not None and page == previous + 1:
            run_length += 1
        else:
            run_start, run_length = page, 1
        if run_length >= EMPTY_RUN_THRESHOLD and run_start is not None:
            reason = f"第{run_start}页起连续{run_length}页为空，探页终止"
            return [p for p in pages if p < run_start], reason
        previous = page
    return pages, None


def _build_pages(cfg: dict[str, Any], page_numbers: list[int]) -> list[dict[str, Any]]:
    """生成有序页面清单：按页码升序、去重，页码与偏移量一一对应。"""
    size = cfg["页大小"]
    pages: list[dict[str, Any]] = []
    seen: set[int] = set()
    for number in sorted(page_numbers):
        if number in seen:
            continue
        seen.add(number)
        offset = (number - 1) * size
        params: dict[str, int] = {}
        if cfg["页码参数名"]:
            params[cfg["页码参数名"]] = number
        if cfg["偏移量参数"]:
            params[cfg["偏移量参数"]] = offset
        pages.append({"序号": len(pages) + 1, "页码": number, "偏移量": offset, "请求参数": params})
    return pages


def _compute_plan(cfg: dict[str, Any], total: int | None, empty_signals: list[int]) -> dict[str, Any]:
    """核心计算：双边界对照 + 模式裁决。同一输入必然得到同一输出（可重放）。"""
    start, max_page, size, mode = cfg["起始页码"], cfg["最大页码"], cfg["页大小"], cfg["翻页模式"]
    page_pages, page_reason = _page_boundary(start, max_page, size, total)
    offset_pages, offset_reason = _offset_boundary(start, max_page, size, total)
    empty_reason: str | None = None
    if total is None:
        # 总数未知：两种边界一致，统一按连续空页截断
        page_pages, empty_reason = _apply_empty_signals(page_pages, empty_signals)
        offset_pages = list(page_pages)
        if empty_reason:
            page_reason = offset_reason = "连续空页"
    compare = {
        "页码边界页数": len(page_pages),
        "偏移量边界页数": len(offset_pages),
        "页码边界终止": page_reason,
        "偏移量边界终止": offset_reason,
    }
    ruling: str | None = None
    if mode == "页码":
        chosen, reason = page_pages, page_reason
    elif mode == "偏移量":
        chosen, reason = offset_pages, offset_reason
    elif page_pages == offset_pages:
        chosen = page_pages
        if empty_reason:
            reason = "连续空页"
        elif page_reason.startswith("总条数") or offset_reason.startswith("偏移量越界"):
            reason = "总条数覆盖"
        else:
            reason = "达到最大页码"
    else:
        # 页码、偏移量、页大小、总条数冲突：取较小边界，结果唯一可解释
        if len(page_pages) != len(offset_pages):
            winner = "页码边界" if len(page_pages) < len(offset_pages) else "偏移量边界"
        else:
            winner = "页码边界" if (page_pages[-1] if page_pages else 0) <= (offset_pages[-1] if offset_pages else 0) else "偏移量边界"
        chosen = page_pages if winner == "页码边界" else offset_pages
        reason = f"冲突裁决：取{winner}"
        ruling = (
            f"页码边界得{len(page_pages)}页（{page_reason}），"
            f"偏移量边界得{len(offset_pages)}页（{offset_reason}），"
            f"按保守口径取{winner}{len(chosen)}页，避免重复页或越过最大页码"
        )
    return {
        "页面": chosen,
        "终止依据": reason,
        "空页终止原因": empty_reason,
        "冲突裁决说明": ruling,
        "双边界对照": compare,
    }


def _dedupe_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """消费侧只取去重后的页面：按（页码, 偏移量）去重并保持原有顺序。"""
    seen: set[tuple[Any, Any]] = set()
    unique: list[dict[str, Any]] = []
    for page in pages:
        key = (page.get("页码"), page.get("偏移量"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(page)
    return unique


class PagerangeService:
    """翻页采集范围的生成、检索、消费。回执幂等依赖同一把锁，请使用模块底部单例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ---------- 查询 ----------

    def list_ranges(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        version: str | None = None,
        condition: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(MODULE))
        if keyword:
            rows = [r for r in rows if keyword in str(r.get("范围编号", "")) or keyword in str(r.get("规则编号", ""))]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if mode:
            rows = [r for r in rows if r.get("翻页模式") == mode]
        if version:
            rows = [r for r in rows if str(r.get("规则版本", "")) == version]
        if condition:
            rows = [r for r in rows if condition in _canonical(r.get("查询条件", {}))]
        rows.sort(key=lambda r: int(r.get("id", 0)))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def summary(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        return {
            "范围总数": len(rows),
            **{status: sum(1 for r in rows if r.get("status") == status) for status in RANGE_STATUSES},
        }

    def get_range(self, range_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, range_id)

    def list_pages(
        self,
        range_id: int,
        *,
        page_no: int | None = None,
        offset: int | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int] | None:
        rng = self.get_range(range_id)
        if rng is None:
            return None
        pages = list(rng.get("页面清单", []))
        if page_no is not None:
            pages = [p for p in pages if p.get("页码") == page_no]
        if offset is not None:
            pages = [p for p in pages if p.get("偏移量") == offset]
        total = len(pages)
        start = max(page - 1, 0) * size
        return pages[start:start + size], total

    def locate(self, range_id: int, *, page_no: int | None = None, offset: int | None = None) -> tuple[dict[str, Any] | None, str]:
        """定位当前分页结果：给出页码在范围内的序号、进度与前后页。"""
        rng = self.get_range(range_id)
        if rng is None:
            return None, f"翻页采集范围 {range_id} 不存在"
        pages = rng.get("页面清单", [])
        if page_no is None and offset is None:
            return None, "请提供页码或偏移量用于定位"
        index: int | None = None
        for i, item in enumerate(pages):
            if page_no is not None and item.get("页码") == page_no:
                index = i
                break
            if offset is not None and item.get("偏移量") == offset:
                index = i
                break
        if index is None:
            target = f"页码{page_no}" if page_no is not None else f"偏移量{offset}"
            return {
                "命中": False,
                "范围编号": rng["范围编号"],
                "说明": f"{target}不在范围{rng['范围编号']}内，可能已被空页终止或边界裁剪（{rng['终止依据']}）",
            }, "定位完成"
        current = pages[index]
        return {
            "命中": True,
            "范围编号": rng["范围编号"],
            "序号": current["序号"],
            "页码": current["页码"],
            "偏移量": current["偏移量"],
            "请求参数": current["请求参数"],
            "总页数": len(pages),
            "进度": f"{index + 1}/{len(pages)}",
            "上一页": pages[index - 1]["页码"] if index > 0 else None,
            "下一页": pages[index + 1]["页码"] if index + 1 < len(pages) else None,
        }, "定位完成"

    # ---------- 计算 ----------

    def _rule_config(self, rule: dict[str, Any], values: dict[str, Any], errors: list[str]) -> dict[str, Any]:
        mode_raw = str(values.get("翻页模式") or "").strip()
        if not mode_raw or mode_raw == "跟随规则":
            mode_raw = str(rule.get("翻页类型") or "").strip()
        mode = MODE_ALIASES.get(mode_raw)
        page_param = str(rule.get("页码参数名") or "").strip()
        offset_param = str(rule.get("偏移量参数") or "").strip()
        if mode is None:
            if mode_raw and mode_raw not in MODE_ALIASES and str(values.get("翻页模式") or "").strip():
                errors.append(f"翻页模式「{mode_raw}」需为页码、偏移量或双模式")
            if page_param and offset_param:
                mode = "双模式"
            elif offset_param:
                mode = "偏移量"
            elif page_param:
                mode = "页码"
            else:
                errors.append(f"翻页类型「{mode_raw}」无法识别，且规则未配置页码参数名或偏移量参数")
                mode = "页码"
        start = _parse_int(rule.get("起始页码"), "起始页码", errors, default=1)
        start = 1 if start is None else start
        max_page = _parse_int(rule.get("最大页码"), "最大页码", errors)
        size = _parse_int(values.get("页大小") or rule.get("页大小"), "页大小", errors, default=DEFAULT_PAGE_SIZE)
        size = DEFAULT_PAGE_SIZE if size is None else size
        if start < 1:
            errors.append("起始页码不能小于1")
        if max_page is None:
            errors.append("规则缺少最大页码，无法校验模式边界")
            max_page = start
        elif max_page < start:
            errors.append(f"最大页码{max_page}小于起始页码{start}，模式边界不成立")
        if size < 1:
            errors.append("页大小需为正整数")
        return {
            "翻页模式": mode,
            "起始页码": start,
            "最大页码": max_page,
            "页大小": size,
            "页码参数名": page_param,
            "偏移量参数": offset_param,
        }

    def compute_range(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, list[str]]:
        """生成可重放翻页采集范围。同一查询条件重复计算命中同一范围，只生效一次。"""
        code = str(values.get("规则编号") or "").strip()
        if not code:
            return None, False, ["缺少必填字段：规则编号"]
        rule = next((r for r in store.rows(RULE_MODULE) if str(r.get("规则编号")) == code), None)
        if rule is None:
            return None, False, [f"翻页规则 {code} 不存在，请先在翻页管理登记"]
        version = str(values.get("规则版本") or rule.get("规则版本") or "v1").strip() or "v1"
        errors: list[str] = []
        cfg = self._rule_config(rule, values, errors)
        total = _parse_total(values.get("总条数"), errors)
        empty_signals = _parse_empty_signals(values.get("空页信号"), errors)
        conditions = _parse_conditions(values.get("查询条件"), errors)
        if not errors:
            for signal in empty_signals:
                if signal < cfg["起始页码"] or signal > cfg["最大页码"]:
                    errors.append(f"空页信号第{signal}页不在[{cfg['起始页码']}, {cfg['最大页码']}]边界内")
        if errors:
            return None, False, errors
        cond_fp = _digest({"规则编号": code, "翻页模式": cfg["翻页模式"], "页大小": cfg["页大小"], "查询条件": conditions})
        range_fp = _digest({"规则编号": code, "规则版本": version, "翻页模式": cfg["翻页模式"], "页大小": cfg["页大小"], "查询条件": conditions})
        with self._lock:
            rows = store.rows(MODULE)
            for existing in rows:
                if existing.get("范围指纹") == range_fp:
                    existing["重复计算次数"] = int(existing.get("重复计算次数", 0)) + 1
                    return existing, False, []
            plan = _compute_plan(cfg, total, empty_signals)
            pages = _build_pages(cfg, plan["页面"])
            # 版本边界：旧版本范围保留给旧作业；新范围页数变多需确认，不允许悄悄扩大
            predecessors = [
                r for r in rows
                if r.get("条件指纹") == cond_fp and r.get("规则版本") != version and r.get("status") != "已废弃"
            ]
            latest_prev = max(predecessors, key=lambda r: int(r.get("id", 0)), default=None)
            status = "已生成"
            expansion: str | None = None
            if latest_prev is not None and len(pages) > int(latest_prev.get("页数", 0)):
                status = "待确认"
                expansion = (
                    f"较上一版本{latest_prev['范围编号']}（{latest_prev['规则版本']}，{latest_prev['页数']}页）"
                    f"新增{len(pages) - int(latest_prev['页数'])}页，需确认扩版后生效，新范围不允许悄悄扩大"
                )
            seq = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            entry = {
                "id": seq,
                "范围编号": f"PR-{seq:04d}",
                "规则编号": code,
                "规则版本": version,
                "翻页模式": cfg["翻页模式"],
                "页码参数名": cfg["页码参数名"],
                "偏移量参数": cfg["偏移量参数"],
                "起始页码": cfg["起始页码"],
                "最大页码": cfg["最大页码"],
                "页大小": cfg["页大小"],
                "总条数": total,
                "查询条件": conditions,
                "条件指纹": cond_fp,
                "范围指纹": range_fp,
                "页面清单": pages,
                "页数": len(pages),
                "终止依据": plan["终止依据"],
                "空页终止原因": plan["空页终止原因"],
                "冲突裁决说明": plan["冲突裁决说明"],
                "双边界对照": plan["双边界对照"],
                "计算输入": {
                    "规则快照": dict(cfg),
                    "总条数": total,
                    "空页信号": list(empty_signals),
                    "查询条件": conditions,
                },
                "扩版说明": expansion,
                "消费回执": {},
                "结果版本": None,
                "重复计算次数": 0,
                "status": status,
                "pending": True,
                "abnormal": False,
                "创建时间": _now(),
            }
            rows.append(entry)
            return entry, True, []

    def replay_check(self, range_id: int) -> dict[str, Any] | None:
        """重放校验：用落库的计算输入重算，核对范围是否完整（有序、无重复、未越界、重放一致）。"""
        rng = self.get_range(range_id)
        if rng is None:
            return None
        snapshot = rng.get("计算输入", {})
        cfg = snapshot.get("规则快照", {})
        plan = _compute_plan(cfg, snapshot.get("总条数"), list(snapshot.get("空页信号", [])))
        replayed = [(p["页码"], p["偏移量"]) for p in _build_pages(cfg, plan["页面"])]
        stored = [(p.get("页码"), p.get("偏移量")) for p in rng.get("页面清单", [])]
        ordered = all(stored[i][1] < stored[i + 1][1] for i in range(len(stored) - 1))
        unique = len(set(stored)) == len(stored)
        within = all(cfg.get("起始页码", 1) <= no <= cfg.get("最大页码", 0) for no, _ in stored)
        consistent = stored == replayed and rng.get("终止依据") == plan["终止依据"]
        complete = ordered and unique and within and consistent
        return {
            "范围编号": rng["范围编号"],
            "完整": complete,
            "有序": ordered,
            "无重复": unique,
            "未越界": within,
            "重放一致": consistent,
            "存储页数": len(stored),
            "重放页数": len(replayed),
            "终止依据": rng.get("终止依据"),
            "空页终止原因": rng.get("空页终止原因"),
            "说明": "范围完整，可安全重放" if complete else "范围与重放结果不一致，请检查计算输入或规则快照",
        }

    # ---------- 动作 ----------

    def run_action(self, range_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        rng = self.get_range(range_id)
        if rng is None:
            return None, f"翻页采集范围 {range_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于翻页采集范围可执行范围"
        if action == "确认扩版":
            if rng.get("status") != "待确认":
                return None, f"范围{rng['范围编号']}当前状态为{rng.get('status')}，无需确认扩版"
            rng["status"] = "已生成"
            rng["pending"] = True
            return rng, f"范围{rng['范围编号']}扩版已确认（{rng.get('扩版说明')}），可以投入消费"
        if rng.get("status") == "已废弃":
            return None, f"范围{rng['范围编号']}已废弃，重复操作只生效一次"
        rng["status"] = "已废弃"
        rng["pending"] = False
        return rng, f"范围{rng['范围编号']}已废弃，历史回执保留备查"

    # ---------- 消费 ----------

    def consume(
        self,
        range_code: str,
        consumer: str,
        *,
        job_code: str | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool, str]:
        """消费一份范围：返回去重后的页面与回执。同一消费方重复消费只生效一次。"""
        if consumer not in CONSUMERS:
            return None, [], False, f"消费方「{consumer}」不在允许范围（{ '、'.join(CONSUMER_NAMES.values()) }）"
        code = str(range_code or "").strip()
        rng = next((r for r in store.rows(MODULE) if str(r.get("范围编号")) == code), None)
        if rng is None:
            return None, [], False, f"翻页采集范围 {code or '(空)'} 不存在"
        if rng.get("status") == "待确认":
            return None, [], False, f"范围{code}扩版待确认，确认前不能消费"
        if rng.get("status") == "已废弃":
            return None, [], False, f"范围{code}已废弃，不能消费"
        pages = _dedupe_pages(list(rng.get("页面清单", [])))
        name = CONSUMER_NAMES[consumer]
        with self._lock:
            receipts: dict[str, Any] = rng.setdefault("消费回执", {})
            if consumer in receipts:
                receipt = receipts[consumer]
                return receipt, pages, False, f"{name}已消费过范围{code}，重复操作只生效一次，返回原回执{receipt['回执编号']}"
            if not receipts:
                rng["结果版本"] = f"RV-{rng['范围编号']}-{rng['规则版本']}"
            receipt = {
                "回执编号": f"RC-{rng['范围编号']}-{consumer}",
                "消费方": consumer,
                "消费方名称": name,
                "范围编号": rng["范围编号"],
                "结果版本": rng["结果版本"],
                "页数": rng["页数"],
                "去重后页面数": len(pages),
                "作业编号": job_code or None,
                "消费时间": _now(),
            }
            receipts[consumer] = receipt
            done = all(c in receipts for c in CONSUMERS)
            rng["status"] = "已消费" if done else "消费中"
            rng["pending"] = not done
            if done:
                rng["完成时间"] = _now()
            return receipt, pages, True, (
                f"{name}已消费范围{code}，结果版本{rng['结果版本']}，"
                f"仅对{len(pages)}个去重页面发起抓取"
            )

    def manifest(self, consumer: str) -> dict[str, Any]:
        """消费侧清单：本侧与对侧回执同源，一侧消费后另一侧清单跟着变化。"""
        other = next(c for c in CONSUMERS if c != consumer)
        items: list[dict[str, Any]] = []
        for rng in store.rows(MODULE):
            receipts = rng.get("消费回执") or {}
            if not receipts:
                continue
            items.append({
                "范围编号": rng.get("范围编号"),
                "规则编号": rng.get("规则编号"),
                "规则版本": rng.get("规则版本"),
                "翻页模式": rng.get("翻页模式"),
                "页数": rng.get("页数"),
                "终止依据": rng.get("终止依据"),
                "结果版本": rng.get("结果版本"),
                "范围状态": rng.get("status"),
                "本侧回执": receipts.get(consumer),
                "对侧消费方": CONSUMER_NAMES[other],
                "对侧回执": receipts.get(other),
            })
        items.sort(key=lambda item: str(item["范围编号"]))
        return {
            "消费方": consumer,
            "消费方名称": CONSUMER_NAMES[consumer],
            "summary": {
                "消费范围数": len(items),
                "本侧已消费": sum(1 for item in items if item["本侧回执"]),
                "对侧已消费": sum(1 for item in items if item["对侧回执"]),
                "双侧已完成": sum(1 for item in items if item["范围状态"] == "已消费"),
            },
            "items": items,
        }


# 全局单例：回执幂等与并发互斥依赖同一把锁，各路由统一使用这个实例。
pagerange_service = PagerangeService()
