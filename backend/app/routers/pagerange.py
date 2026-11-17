"""翻页采集范围接口：生成可重放翻页采集范围，覆盖检索、页码筛选、定位、重放校验与扩版动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.pagerange import pagerange_service as service

router = APIRouter(prefix="/api/pagerange", tags=["翻页采集范围"])


@router.get("/summary")
def summary() -> dict[str, Any]:
    """范围状态汇总：给看板卡片用。"""
    return service.summary()


@router.get("", response_model=PageResult[dict])
def list_ranges(
    keyword: str | None = Query(default=None, description="按范围编号或规则编号检索"),
    status: str | None = Query(default=None, description="已生成、待确认、消费中、已消费、已废弃"),
    mode: str | None = Query(default=None, description="页码、偏移量、双模式"),
    version: str | None = Query(default=None, description="按规则版本筛选"),
    condition: str | None = Query(default=None, description="按查询条件包含内容检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按查询条件检索翻页采集范围；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_ranges(
        keyword=keyword, status=status, mode=mode, version=version, condition=condition, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/compute", response_model=ActionResult)
def compute_range(payload: EntryPayload) -> ActionResult:
    """生成可重放翻页采集范围：读取页码参数、偏移量参数与总条数，校验双模式边界。

    同一查询条件重复计算命中同一范围，只返回原范围，不生成第二份。
    """
    entry, created, errors = service.compute_range(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    assert entry is not None
    if created:
        message = f"已生成可重放翻页采集范围{entry['范围编号']}，共{entry['页数']}页，终止依据：{entry['终止依据']}"
        if entry.get("扩版说明"):
            message += f"；{entry['扩版说明']}"
    else:
        message = (
            f"同一查询条件重复计算命中已有范围{entry['范围编号']}，返回原范围"
            f"（累计重复{entry['重复计算次数']}次，只生效一次）"
        )
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{range_id}", response_model=dict)
def get_range(range_id: int) -> dict:
    """读取单份范围明细，含页面清单、终止依据与消费回执。"""
    entry = service.get_range(range_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"翻页采集范围 {range_id} 不存在或已归档")
    return entry


@router.get("/{range_id}/pages", response_model=PageResult[dict])
def list_pages(
    range_id: int,
    page_no: int | None = Query(default=None, description="按页码筛选"),
    offset: int | None = Query(default=None, description="按偏移量筛选"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """筛选范围内的页面：按页码或偏移量过滤有序页面清单。"""
    result = service.list_pages(range_id, page_no=page_no, offset=offset, page=page, size=size)
    if result is None:
        raise HTTPException(status_code=404, detail=f"翻页采集范围 {range_id} 不存在或已归档")
    items, total = result
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{range_id}/locate", response_model=dict)
def locate_page(
    range_id: int,
    page_no: int | None = Query(default=None, description="要定位的页码"),
    offset: int | None = Query(default=None, description="要定位的偏移量"),
) -> dict:
    """定位当前分页结果：返回页码在范围内的序号、进度与前后页。"""
    result, message = service.locate(range_id, page_no=page_no, offset=offset)
    if result is None:
        raise HTTPException(status_code=404, detail=message)
    return result


@router.get("/{range_id}/replay", response_model=dict)
def replay_range(range_id: int) -> dict:
    """重放校验：用落库的计算输入重算，确认范围完整（有序、无重复、未越界、重放一致）。"""
    report = service.replay_check(range_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"翻页采集范围 {range_id} 不存在或已归档")
    return report


@router.post("/{range_id}/actions", response_model=ActionResult)
def run_action(range_id: int, payload: EntryPayload) -> ActionResult:
    """对单份范围执行确认扩版、废弃范围；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(range_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
