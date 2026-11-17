"""URL过滤接口：维护过滤规则，覆盖提交规则评审、生效规则、废弃规则等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.pagerange import pagerange_service
from app.services.urlfilter import UrlfilterService

router = APIRouter(prefix="/api/urlfilter", tags=["URL过滤"])

service = UrlfilterService()

LIST_FIELDS = ["规则编号", "包含模式", "排除模式", "域名白名单", "路径黑名单", "参数剥离规则", "大小写处理", "尾部斜杠策略"]
STATUSES = ["草稿", "规则评审中", "已生效", "已废弃"]


@router.get("/range-manifest")
def range_manifest() -> dict[str, Any]:
    """URL过滤侧翻页范围消费清单：本侧与抓取侧回执同源，对侧消费后本清单跟着变化。"""
    return pagerange_service.manifest("urlfilter")


@router.post("/consume-range", response_model=ActionResult)
def consume_range(payload: EntryPayload) -> ActionResult:
    """消费一份可重放翻页采集范围：只对去重后的页面发起抓取；重复消费返回原回执，不生成第二份。"""
    receipt, pages, created, message = pagerange_service.consume(
        str(payload.values.get("范围编号") or ""),
        "urlfilter",
        job_code=str(payload.values.get("作业编号") or "") or None,
    )
    if receipt is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"回执": receipt, "去重后页面": pages, "是否新回执": created})


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤URL过滤列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条过滤规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"过滤规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条过滤规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="过滤规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条过滤规则执行提交规则评审、生效规则、废弃规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出URL过滤清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "urlfilter", "total": total, "items": items}
