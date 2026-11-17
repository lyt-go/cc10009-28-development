"""抓取作业接口：维护作业定义，覆盖提交作业审批、启动作业、停止作业等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.crawljob import CrawljobService
from app.services.pagerange import pagerange_service

router = APIRouter(prefix="/api/crawljob", tags=["抓取作业"])

service = CrawljobService()

LIST_FIELDS = ["作业编号", "目标站点", "起始URL", "爬取深度", "URL去重策略", "页面解析器", "下载超时秒", "最大页面数"]
STATUSES = ["草稿", "作业审批中", "执行中", "已完成", "已停止"]


@router.get("/range-manifest")
def range_manifest() -> dict[str, Any]:
    """抓取消费侧翻页范围清单：结果版本随消费回执同步进清单，确认范围已被消费。"""
    return pagerange_service.manifest("crawljob")


@router.post("/consume-range", response_model=ActionResult)
def consume_range(payload: EntryPayload) -> ActionResult:
    """消费一份可重放翻页采集范围：只对去重后的页面发起抓取；并发回执不会多出第二份结果。"""
    receipt, pages, created, message = pagerange_service.consume(
        str(payload.values.get("范围编号") or ""),
        "crawljob",
        job_code=str(payload.values.get("作业编号") or "") or None,
    )
    if receipt is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"回执": receipt, "去重后页面": pages, "是否新回执": created})


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按作业编号检索"),
    status: str | None = Query(default=None, description="草稿、作业审批中、执行中、已完成、已停止"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按作业编号与状态过滤抓取作业列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条作业定义明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"作业定义 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条作业定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="作业定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条作业定义执行提交作业审批、启动作业、停止作业；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出抓取作业清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "crawljob", "total": total, "items": items}
