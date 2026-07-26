#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立验证 cleanup-test-entries.py —— mock subprocess(osascript) 返回条目。

验证点（需求 #1）：
  - 删除条件严格限定为 notes 含 `id:p-` 标记
  - 带标记的进"待删"列表；用户自建(无标记)被排除
  - DRY_RUN=1 时只打印预览，绝不调用删除
"""
import io
import os
import sys
import contextlib
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "cleanup_test_entries",
    os.path.join(HERE, "cleanup-test-entries.py"),
)
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

# 模拟 osascript 返回的 tab 分隔结果：
# R<tab>rid<tab>name<tab>due<tab>notes
FAKE = (
    "R\tR1\t用户档期A\t2025-07-01\t用户自建无标记\n"
    "R\tR2\t测试档期B\t2025-07-02\t备注 id:p-20250702-01 结尾\n"
    "R\tR3\t测试档期C\t2025-07-03\tid:p-20250703-02\n"
    "E\tE1\t用户日历A\t2025-07-01\t\n"
    "E\tE2\t测试日历B\t2025-07-02\tid:p-20250702-99\n"
)


class _Cap:
    def __init__(self):
        self.calls = []
    def __call__(self, script):
        self.calls.append(script)
        return (FAKE, None)  # 读取调用需要真实内容；删除调用返回值被忽略


def run_with(dry):
    cap = _Cap()
    C._run_osascript = cap
    C.DRY_RUN = dry
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            C.main()
        except SystemExit:
            pass
    return cap.calls, buf.getvalue()


def test_dry_run_no_delete_and_selection():
    calls, out = run_with(True)
    # 计数断言
    assert "带 id:p- 标记的测试条目 2 条" in out, "应识别出 2 条带标记提醒"
    assert "带 id:p- 标记的测试条目 1 条" in out, "应识别出 1 条带标记日历"
    # 含标记的进入待删列表
    assert "[将删除] 提醒 — 测试档期B" in out
    assert "[将删除] 提醒 — 测试档期C" in out
    assert "[将删除] 日历 — 测试日历B" in out
    # 用户自建(无标记)被排除
    assert "[将删除] 提醒 — 用户档期A" not in out, "用户自建条目不应被删"
    assert "[将删除] 日历 — 用户日历A" not in out, "用户自建条目不应被删"
    # DRY_RUN 绝不调用删除
    assert not any("delete r" in s or "delete e" in s for s in calls), \
        "DRY_RUN 不应调用任何删除脚本"
    print("  [PASS] test_dry_run_no_delete_and_selection")


def test_real_run_deletes_only_marked():
    calls, out = run_with(False)
    dels = [s for s in calls if "delete r" in s or "delete e" in s]
    assert len(dels) == 3, "应只对 3 条带标记条目调用删除（实际 %d）" % len(dels)
    # 删除脚本只针对 R2/R3/E2，绝不针对用户自建 R1/E1
    assert any('pid to "R2"' in s for s in dels)
    assert any('pid to "R3"' in s for s in dels)
    assert any('eid to "E2"' in s for s in dels)
    assert not any('pid to "R1"' in s for s in dels), "绝不应删除用户自建 R1"
    assert not any('eid to "E1"' in s for s in dels), "绝不应删除用户自建 E1"
    print("  [PASS] test_real_run_deletes_only_marked")


if __name__ == "__main__":
    print("=== cleanup-test-entries.py 独立验证 ===")
    fails = 0
    for fn in (test_dry_run_no_delete_and_selection, test_real_run_deletes_only_marked):
        try:
            fn()
        except AssertionError as e:
            fails += 1
            print("  [FAIL] %s: %s" % (fn.__name__, e))
    print("=== 结果: %d 个失败 ===" % fails)
    sys.exit(1 if fails else 0)
