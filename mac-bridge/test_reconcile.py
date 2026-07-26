#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立验证 reconcile.py（当前实现：镜像模式，新建+更新+删除）。

mock 读取，不真跑 osascript。验证点：
  A. 有匹配(按日期) -> 更新提醒 body（含 id:p- 标记 + 4 checkbox 状态）+ 更新日历；
     mirror.reminders.matched=true
  B. 无匹配 -> 新建提醒+日历事件；新条目含 id:p- 标记；mirror.matched=true
"""
import io
import os
import sys
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import reconcile as R


class _Capture:
    def __init__(self):
        self.scripts = []
    def __call__(self, script):
        self.scripts.append(script)
        return ("OK", None)


def _mk_project(pid, date, *, client="张三", shoot="", deliver="", inv_no="",
                pub=0, name=None, displayName=None, source=None, notes=""):
    return {
        "id": pid,
        "name": name or ("客户" + pid),
        "client": client,
        "date": date,
        "type": "拍摄",
        "payment": {"date": date, "amount": 1000, "method": "微信"},
        "publicAccount": {"date": date, "amount": pub},
        "invoice": {"no": inv_no, "date": date, "sent": bool(inv_no)},
        "shootDone": shoot,
        "deliverDone": deliver,
        "deliverMethod": "网盘",
        "notes": notes,
        **({"displayName": displayName} if displayName is not None else {}),
        **({"source": source} if source is not None else {}),
    }


def _run(projects, reminders, events, dry_run=False):
    data = {"updatedAt": "2025-07-01T00:00:00", "projects": projects}
    cap = _Capture()
    R._run_osascript = cap
    R._read_reminders = lambda: list(reminders)
    R._read_calendar = lambda: list(events)
    R.load_data = lambda: (data, "/tmp/_qa_fake_data.json")
    R.DRY_RUN = dry_run
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        summary = R.run()
    return summary, cap.scripts, buf.getvalue(), data


def test_match_updates_status():
    """匹配到已有条目时，应更新 body（id 标记+checkbox）和日历 notes，绝不新建。"""
    p = _mk_project("p-1", "2025-07-01", client="张三", shoot="2025-07-01")
    reminders = [{"rem_id": "R1", "name": "拍摄-0701 张三", "date": "2025-07-01",
                  "notes": "用户原有备注", "subtasks": {}}]
    events = [{"ev_id": "E1", "summary": "拍摄-0701 张三", "date": "2025-07-01", "notes": ""}]
    summary, scripts, log, data = _run([p], reminders, events, dry_run=False)

    # 匹配导致更新（非新建）
    assert summary["updated_rem"] == 1, "匹配应更新提醒（实际 %d）" % summary["updated_rem"]
    assert summary["updated_ev"] == 1, "匹配应更新日历（实际 %d）" % summary["updated_ev"]
    assert summary["created_rem"] == 0, "匹配不应新建提醒"
    assert summary["created_ev"] == 0, "匹配不应新建日历"

    # 绝不新建：不出现 make new reminder/event
    assert not any("make new reminder" in s or "make new event" in s for s in scripts), \
        "匹配时不应出现 make new（新建）逻辑"

    # 追加 id:p- 标记
    assert any("id:p-1" in s for s in scripts), "应把 id:p-1 标记写进条目"

    # mirror 状态
    assert data["projects"][0]["mirror"]["reminders"]["matched"] is True
    assert data["projects"][0]["mirror"]["calendar"]["exists"] is True
    print("  [PASS] test_match_updates_status")


def test_no_match_creates_new():
    """未匹配到时，应新建提醒和日历事件（自动镜像）。"""
    p = _mk_project("p-2", "2025-07-02", client="李四")
    summary, scripts, log, data = _run([p], [], [], dry_run=False)

    # 无匹配导致新建
    assert summary["created_rem"] == 1, "无匹配应新建提醒（实际 %d）" % summary["created_rem"]
    assert summary["created_ev"] == 1, "无匹配应新建日历（实际 %d）" % summary["created_ev"]
    assert summary["updated_rem"] == 0, "无匹配不应更新提醒"
    assert summary["updated_ev"] == 0, "无匹配不应更新日历"

    # 应出现 make new
    assert any("make new reminder" in s for s in scripts), \
        "无匹配时应出现 make new reminder"
    assert any("make new event" in s for s in scripts), \
        "无匹配时应出现 make new event"

    # mirror 标记为匹配（新建后应有镜像）
    assert data["projects"][0]["mirror"]["reminders"]["matched"] is True
    assert data["projects"][0]["mirror"]["calendar"]["exists"] is True
    print("  [PASS] test_no_match_creates_new")


def test_read_protection_skips_create_when_empty():
    """当读取返回空且已有 mirror 记录时，跳过新建（读取保护）。"""
    p = _mk_project("p-3", "2025-07-03", client="王五")
    # 预先设置 mirror 状态，模拟之前已有镜像
    p["mirror"] = {
        "reminders": {"matched": True, "last": "0000"},
        "calendar": {"exists": True},
    }
    summary, scripts, log, data = _run([p], [], [], dry_run=False)

    # 读取返回空(reminders=[])，且有 mirror → 跳过新建
    assert summary["created_rem"] == 0, "读取保护应跳过新建提醒（实际 %d）" % summary["created_rem"]
    assert summary["created_ev"] == 0, "读取保护应跳过新建日历（实际 %d）" % summary["created_ev"]
    assert summary["skipped_rem"] == 1, "应记录跳过提醒数（实际 %d）" % summary["skipped_rem"]
    assert summary["skipped_ev"] == 1, "应记录跳过日历数（实际 %d）" % summary["skipped_ev"]

    # 不应出现 make new
    assert not any("make new reminder" in s or "make new event" in s for s in scripts), \
        "读取保护下不应出现 make new"
    print("  [PASS] test_read_protection_skips_create_when_empty")


def test_first_run_allows_create_even_with_empty_read():
    """首次运行（无 mirror）时，即使读取为空也应允许新建。"""
    p = _mk_project("p-4", "2025-07-04", client="赵六")
    summary, scripts, log, data = _run([p], [], [], dry_run=False)

    # 首次运行：没有 mirror 记录，应允许新建
    assert summary["created_rem"] == 1, "首次运行即使读取为空也应新建提醒（实际 %d）" % summary["created_rem"]
    assert summary["created_ev"] == 1, "首次运行即使读取为空也应新建日历（实际 %d）" % summary["created_ev"]
    assert summary["skipped_rem"] == 0, "首次运行不应跳过"
    assert summary["skipped_ev"] == 0, "首次运行不应跳过"
    print("  [PASS] test_first_run_allows_create_even_with_empty_read")


if __name__ == "__main__":
    print("=== reconcile.py 独立验证（镜像模式）===")
    fails = 0
    for fn in (test_match_updates_status,
               test_no_match_creates_new,
               test_read_protection_skips_create_when_empty,
               test_first_run_allows_create_even_with_empty_read):
        try:
            fn()
        except AssertionError as e:
            fails += 1
            print("  [FAIL] %s: %s" % (fn.__name__, e))
    print("=== 结果: %d 个失败 ===" % fails)
    sys.exit(1 if fails else 0)
