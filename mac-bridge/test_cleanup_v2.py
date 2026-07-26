#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2 cleanup-test-entries.py 单测（不执行 main()）。"""
import sys
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "cleanup-test-entries.py"

# 用 importlib 加载模块（不执行 main() 因为我们在它之前 if __name__==...)
spec = importlib.util.spec_from_file_location("cleanup", str(SRC))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

print("=" * 60)
print("v2 cleanup-test-entries.py 单测")
print("=" * 60)

# 1) 加载 data.json
projects, by_date = mod._load_projects()
print(f"[1] data.json projects: {len(projects)} 条, by_date 索引: {len(by_date)} 个不同日期")
assert len(projects) == 58, f"应 58 条，实 {len(projects)}"
assert len(by_date) >= 20, "by_date 应有 ≥20 个不同日期"

# 2) 自检：data.json 58 条每条都能匹配自己
mismatches = []
for p in projects:
    fake_rec = {
        "name": p.get("name", ""),
        "summary": p.get("name", ""),
        "dueDate": p.get("date", ""),
        "startDate": p.get("date", ""),
        "notes": "",
    }
    ok, proj, reason = mod._match_to_project(fake_rec, by_date)
    if not ok:
        mismatches.append((p.get("id"), p.get("name"), p.get("date"), reason))

print(f"\n[2] 自检：data.json 58 条每条都能匹配自己")
print(f"    FAIL count: {len(mismatches)}")
assert len(mismatches) == 0, f"自检失败 {len(mismatches)} 条: {mismatches[:3]}"
print("    ✓ PASS")

# 3) 不沾边条目应被保留
unrelated = [
    ("买菜", "2026-08-01", ""),
    ("去机场接人", "2026-09-15", ""),
    ("提醒吃药", "2026-07-25", ""),  # 同日但内容无关
    ("陪女儿去图书馆", "2026-05-13", ""),  # 同日 5-13
    ("2026 年计划", "2026-01-01", ""),  # 日期不在 data.json
]
print(f"\n[3] 不沾边条目应被保留（5 条）")
for name, date, notes in unrelated:
    fake = {"name": name, "summary": name, "dueDate": date, "startDate": date, "notes": notes}
    ok, proj, reason = mod._match_to_project(fake, by_date)
    expected = False
    status = "OK" if ok == expected else "BUG"
    print(f"    [{status}] \"{name}\" @ {date} → ok={ok}  reason={reason}")
    assert ok == expected, f"\"{name}\" 期望保留(ok=False) 实际 ok={ok}"

# 4) v1 id:p- 标记兜底
print(f"\n[4] v1 标记兜底（带 id:p- 标记应被标记为待删）")
fake = {
    "name": "任意名字",
    "summary": "任意名字",
    "dueDate": "2020-01-01",
    "startDate": "2020-01-01",
    "notes": "这是 v1 时代测试条目 id:p-20260725-05",
}
ok, proj, reason = mod._match_to_project(fake, by_date)
print(f"    ok={ok}  proj_id={(proj or {}).get('id', '—')}  reason={reason}")
assert ok is True, "v1 标记兜底应识别为待删"
print("    ✓ PASS")

# 5) 早期没标记的测试条目（核心场景）
print(f"\n[5] 早期没标记测试条目（核心：按 data.json 模糊匹配）")
test_cases = [
    ("拍摄-0513 伊莱瑞德春游", "2026-05-13", "p-20260513-45"),
    ("拍摄-0725 沃总活动", "2026-07-25", "p-20260725-05"),
    ("活动-沃总 7/25", "2026-07-25", "p-20260725-05"),
    ("2026-06-01 艾毅六一活动", "2026-06-01", "p-20260601-33"),
    ("7月18日 沃总活动拍摄", "2026-07-18", "p-20260718-10"),
    ("温莎幼儿园 7月14日", "2026-07-14", "p-20260714-13"),
    ("温莎自助餐 5月29", "2026-05-29", "p-20260529-38"),
    ("艾毅六一活动", "2026-06-01", "p-20260601-33"),
]
for name, date, expected_id in test_cases:
    fake = {"name": name, "summary": name, "dueDate": date, "startDate": date, "notes": ""}
    ok, proj, reason = mod._match_to_project(fake, by_date)
    actual_id = (proj or {}).get("id", "—")
    status = "OK" if (ok and actual_id == expected_id) else "BUG"
    print(f"    [{status}] \"{name}\" @ {date} → ok={ok}  proj_id={actual_id}  (期望 {expected_id})")
    assert ok and actual_id == expected_id, f"\"{name}\" 期望匹配到 {expected_id} 实际 {actual_id}"

# 6) 同日多项目场景：6-1 有 3 条 (伊莱瑞德六一活动/温莎六一活动/艾毅六一活动)
print(f"\n[6] 同日多项目（2026-06-01 共 3 条）")
for name in ["伊莱瑞德六一活动", "温莎六一活动", "艾毅六一活动"]:
    fake = {"name": name, "summary": name, "dueDate": "2026-06-01", "startDate": "2026-06-01", "notes": ""}
    ok, proj, reason = mod._match_to_project(fake, by_date)
    print(f"    \"{name}\" → {(proj or {}).get('id', '—')}  ({reason})")
    assert ok, f"\"{name}\" 应能匹配"

# 7) 边界：完全空的条目
print(f"\n[7] 边界：空标题/空日期")
edge_cases = [
    ("", "2026-07-25", ""),
    ("随便写", "", ""),
    ("随便写", "2026-07-25", ""),
]
for name, date, notes in edge_cases:
    fake = {"name": name, "summary": name, "dueDate": date, "startDate": date, "notes": notes}
    ok, proj, reason = mod._match_to_project(fake, by_date)
    print(f"    name='{name}' date='{date}' → ok={ok}  reason={reason}")
    # 边界不应误判

print("\n" + "=" * 60)
print("全部单测通过 ✓")
print("=" * 60)
