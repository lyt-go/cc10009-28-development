"""可重放翻页采集范围自检脚本。

直接跑服务层逻辑，覆盖：双模式边界校验、确定性重算、幂等、并发回执、
版本边界、冲突裁决、空页终止落库、重放校验、消费清单联动。

运行：cd backend && .venv/bin/python tests/pagerange_selfcheck.py
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.pagemarker import PagemarkerService  # noqa: E402
from app.services.pagerange import pagerange_service as svc  # noqa: E402
from app.store import store  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def compute(**overrides):
    values = {"规则编号": "PAGE-0001", "查询条件": {"用例": "默认"}}
    values.update(overrides)
    return svc.compute_range(values)


# ---------- 1. 页码模式：总条数覆盖 ----------
rng, created, errors = compute(规则版本="v1", 查询条件={"用例": "T1"}, 总条数=155)
check("T1 页码模式生成成功", created and not errors, f"errors={errors}")
check("T1 页数=ceil(155/20)=8", rng["页数"] == 8, f"实际{rng['页数']}")
check("T1 终止依据=总条数覆盖", rng["终止依据"] == "总条数覆盖")
check("T1 页面有序且偏移量连续", [p["偏移量"] for p in rng["页面清单"]] == [i * 20 for i in range(8)])
check("T1 页码参数落到请求参数", rng["页面清单"][2]["请求参数"] == {"page": 3})

# ---------- 2. 页码模式：达到最大页码 ----------
rng2, _, _ = compute(规则版本="v1", 查询条件={"用例": "T2"}, 总条数=10000)
check("T2 页数 capped 在最大页码 10", rng2["页数"] == 10)
check("T2 终止依据=达到最大页码", rng2["终止依据"] == "达到最大页码")

# ---------- 3. 同一查询条件重复计算 → 同一范围 ----------
again, created_again, _ = compute(规则版本="v1", 查询条件={"用例": "T1"}, 总条数=155)
check("T3 重复计算命中同一范围", not created_again and again["范围编号"] == rng["范围编号"])
check("T3 重复计算次数落库", again["重复计算次数"] == 1)
same_fp = [r for r in store.rows("pagerange") if r["范围指纹"] == rng["范围指纹"]]
check("T3 不产生第二份范围记录", len(same_fp) == 1)

# ---------- 4. 总数未知 + 连续空页：截断且不重复生成页面 ----------
rng4, _, _ = compute(规则版本="v1", 查询条件={"用例": "T4"}, 总条数="未知", 空页信号="4,5,7")
check("T4 连续空页截断为1-3页", [p["页码"] for p in rng4["页面清单"]] == [1, 2, 3])
check("T4 终止依据=连续空页", rng4["终止依据"] == "连续空页")
check("T4 空页终止原因落库", rng4["空页终止原因"] == "第4页起连续2页为空，探页终止")
page_nos = [p["页码"] for p in rng4["页面清单"]]
check("T4 页面无重复", len(page_nos) == len(set(page_nos)))

# ---------- 5. 偏移量模式：偏移量越界终止 ----------
rng5, _, errors5 = compute(规则编号="PAGE-0002", 规则版本="v2", 查询条件={"用例": "T5"}, 总条数=100)
check("T5 偏移量模式生成成功", not errors5)
check("T5 页数=3（offset 0/40/80）", rng5["页数"] == 3, f"实际{rng5['页数']}")
check("T5 终止依据=偏移量越界", rng5["终止依据"] == "偏移量越界")
check("T5 偏移量参数落到请求参数", rng5["页面清单"][1]["请求参数"] == {"offset": 40})

# ---------- 6. 双模式冲突：唯一可解释结果 ----------
store.rows("pagemarker").append({
    "id": 9001, "status": "已启用", "pending": False, "abnormal": False,
    "规则编号": "PAGE-9001", "目标站点": "conflict.example.com", "翻页类型": "双模式",
    "起始页码": "3", "最大页码": "10", "页码参数名": "page", "偏移量参数": "offset",
    "总条数定位器": "css:.total", "页大小": "10", "规则版本": "v1",
})
v1, _, _ = compute(规则编号="PAGE-9001", 规则版本="v1", 查询条件={"用例": "T6"}, 总条数=100)
check("T6 总条数100时双边界一致=8页", v1["页数"] == 8 and v1["冲突裁决说明"] is None)
# 总条数在采集途中缩小到45，最大页码仍有效：页码边界5页 vs 偏移量边界3页
v2, _, _ = compute(规则编号="PAGE-9001", 规则版本="v2", 查询条件={"用例": "T6"}, 总条数=45)
check("T6 缩小后冲突裁决取偏移量边界=3页", v2["页数"] == 3 and v2["终止依据"] == "冲突裁决：取偏移量边界")
check("T6 裁决说明留痕可解释", v2["冲突裁决说明"] is not None and "页码边界得5页" in v2["冲突裁决说明"])
check("T6 双边界对照落库", v2["双边界对照"] == {"页码边界页数": 5, "偏移量边界页数": 3, "页码边界终止": "总条数覆盖", "偏移量边界终止": "偏移量越界"})
check("T6 版本边界两侧各有一份", v1["范围编号"] != v2["范围编号"] and v1["status"] == "已生成" and v2["status"] == "已生成")

# ---------- 7. 规则换版：旧作业用原范围，新范围不能悄悄扩大 ----------
t7v1, _, _ = compute(规则版本="v1", 查询条件={"用例": "T7"}, 总条数=100)
check("T7 v1范围=5页", t7v1["页数"] == 5)
pagemarker = PagemarkerService()
rule_row = next(r for r in store.rows("pagemarker") if r["规则编号"] == "PAGE-0001")
_, msg = pagemarker.run_action(int(rule_row["id"]), "规则换版")
check("T7 规则换版为v2", rule_row["规则版本"] == "v2", msg)
t7v2, _, _ = compute(查询条件={"用例": "T7"}, 总条数=230)  # 不传版本 → 跟随规则v2
check("T7 新范围跟随规则v2", t7v2["规则版本"] == "v2")
check("T7 扩版进入待确认", t7v2["status"] == "待确认" and t7v2["扩版说明"] is not None)
receipt, _, _, consume_msg = svc.consume(t7v2["范围编号"], "crawljob")
check("T7 待确认范围不能消费", receipt is None)
t7v2_after, confirm_msg = svc.run_action(int(t7v2["id"]), "确认扩版")
check("T7 确认扩版后已生成", t7v2_after is not None and t7v2_after["status"] == "已生成")
check("T7 旧范围保持已生成可消费", t7v1["status"] == "已生成")
pagemarker.run_action(int(rule_row["id"]), "规则换版")  # → v3
t7v3, _, _ = compute(查询条件={"用例": "T7"}, 总条数=40)
check("T7 缩版范围直接生效", t7v3["规则版本"] == "v3" and t7v3["status"] == "已生成" and t7v3["页数"] == 2)

# ---------- 8. 消费幂等与并发回执 ----------
target = t7v1["范围编号"]
r1, pages1, created1, _ = svc.consume(target, "crawljob", job_code="CRAW-0001")
check("T8 首次消费生成回执", created1 and r1["回执编号"] == f"RC-{target}-crawljob")
check("T8 结果版本同步", r1["结果版本"] == f"RV-{target}-v1" and t7v1["结果版本"] == r1["结果版本"])
check("T8 只对去重后页面发起抓取", len(pages1) == len({(p["页码"], p["偏移量"]) for p in pages1}) == t7v1["页数"])
r2, _, created2, msg2 = svc.consume(target, "crawljob")
check("T8 重复消费只生效一次", not created2 and r2["回执编号"] == r1["回执编号"] and "重复" in msg2)

concurrent_receipts: list[dict] = []
def _worker():
    receipt, _, _, _ = svc.consume(target, "crawljob")
    concurrent_receipts.append(receipt)
threads = [threading.Thread(target=_worker) for _ in range(12)]
for t in threads:
    t.start()
for t in threads:
    t.join()
codes = {r["回执编号"] for r in concurrent_receipts}
check("T8 并发回执只有一份结果", len(codes) == 1 and len(t7v1["消费回执"]) == 1, f"回执集合={codes}")

r3, _, created3, _ = svc.consume(target, "urlfilter")
check("T8 第二消费方消费后范围已消费", created3 and t7v1["status"] == "已消费" and t7v1.get("完成时间"))

crawl_manifest = svc.manifest("crawljob")
filter_manifest = svc.manifest("urlfilter")
crawl_item = next(i for i in crawl_manifest["items"] if i["范围编号"] == target)
filter_item = next(i for i in filter_manifest["items"] if i["范围编号"] == target)
check("T8 抓取消费侧清单记录结果版本", crawl_item["结果版本"] == r1["结果版本"] and crawl_item["本侧回执"] is not None)
check("T8 另一业务面清单跟着变化", filter_item["本侧回执"] is not None and filter_item["对侧回执"] is not None)
check("T8 清单汇总双侧已完成", crawl_manifest["summary"]["双侧已完成"] >= 1)

# ---------- 9. 重放校验：范围完整 ----------
report = svc.replay_check(int(t7v1["id"]))
check("T9 重放校验完整", report["完整"] and report["有序"] and report["无重复"] and report["未越界"] and report["重放一致"])
report_conflict = svc.replay_check(int(v2["id"]))
check("T9 冲突裁决范围同样可重放", report_conflict["完整"])

# ---------- 10. 检索、筛选页码与定位 ----------
located, _ = svc.locate(int(t7v1["id"]), page_no=3)
check("T10 定位页码3", located["命中"] and located["序号"] == 3 and located["进度"] == "3/5" and located["下一页"] == 4)
miss, _ = svc.locate(int(t7v1["id"]), page_no=99)
check("T10 定位越界页码给出说明", not miss["命中"] and "不在范围" in miss["说明"])
by_offset, _ = svc.locate(int(t7v1["id"]), offset=40)
check("T10 按偏移量定位", by_offset["命中"] and by_offset["页码"] == 3)
rows, total_rows = svc.list_ranges(keyword="PAGE-9001")
check("T10 按规则编号检索", total_rows == 2)
rows_cond, total_cond = svc.list_ranges(condition="T6")
check("T10 按查询条件检索", total_cond == 2)
pages_filtered, pages_total = svc.list_pages(int(t7v1["id"]), page_no=4)
check("T10 筛选页码", pages_total == 1 and pages_filtered[0]["页码"] == 4)

# ---------- 11. 边界校验：错误必须可读 ----------
_, _, errs = svc.compute_range({"规则编号": "PAGE-0001", "查询条件": "{not-json"})
check("T11 非法查询条件被拦下", any("JSON" in e for e in errs))
_, _, errs2 = svc.compute_range({"规则编号": "PAGE-0001", "规则版本": "v9", "查询条件": {"用例": "T11"}, "空页信号": "99"})
check("T11 空页信号越界被拦下", any("边界" in e for e in errs2))
_, _, errs3 = svc.compute_range({"规则编号": "PAGE-404X", "查询条件": {}})
check("T11 规则不存在被拦下", any("不存在" in e for e in errs3))
receipt_none, _, _, msg_none = svc.consume("PR-9999", "crawljob")
check("T11 消费不存在范围给出说明", receipt_none is None and "不存在" in msg_none)
receipt_bad, _, _, msg_bad = svc.consume(target, "unknown-consumer")
check("T11 非法消费方被拦下", receipt_bad is None and "不在允许范围" in msg_bad)

print()
if FAILURES:
    print(f"共 {len(FAILURES)} 项未通过：{FAILURES}")
    sys.exit(1)
print("全部自检通过。")
