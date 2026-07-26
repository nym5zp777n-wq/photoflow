#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleanup-test-entries.py (v2) — 清理测试时误写入 Mac 的档期条目
================================================================

【背景】
之前测试 reconcile.py 时，往 Mac 的：
  - 提醒事项清单「档期管理」
  - 日历「摄影档期」
写入了 58 条档期（命名格式 "拍摄-MMDD 客户名" / 内容来自 data.json）。
用户自己在 Mac 上早就手动建了一套档期，现在两边重复了。

【v2 升级说明】
v1 只按 notes 里 `id:p-` 标记删，跑出 0 条匹配。
原因：早期版本的 reconcile.py 在新建条目时**不写 `id:p-` 标记**，所以那批测试条目是"裸"的。
v2 不再依赖标记，直接拿 data.json 的 58 条项目做"对照表"——扫到 Mac 端条目时，
只要能跟 data.json 某条 project 匹配上（日期相等 + 标题/client 模糊命中）就视为测试残留；
匹配不上的视为用户自建，绝对保留。
同时仍保留 v1 的 `id:p-` 标记兜底（v1 时代输出的也会被删）。

【匹配规则】
对每条 Mac 端 reminder / event：
  1) 取其 dueDate / startDate（YYYY-MM-DD）
  2) date 在 data.json 58 条里命中 → 候选 candidates
  3) 候选非空时，遍历验证（任一满足即视为命中）：
     - Mac 标题含 project.name 的非空子串
     - Mac 标题含 project.client 的非空子串
     - Mac 标题经"分词 + 修饰词去除"后能拼出 name 的所有有意义 token
  4) 命中 → 标记为待删 + 记录匹配原因
  5) 都不命中 → 视为用户自建，跳过

【用法】（用户在自己 Mac 终端执行，不在沙箱跑）
  1) 先预览（不真删），看清将要删除哪些：
       DRY_RUN=1 python3 mac-bridge/cleanup-test-entries.py
  2) 确认清单无误后，命令行询问 "确认要真删吗？输入 y 回车真删" → 输入 y 真删
  3) 也可直接双击桌面「清理测试档期.command」，流程：先预览→询问 y/n

【环境变量】
  DRY_RUN=1           强制预览（不真删）；不设置时仍默认预览，询问后真删
  REMINDERS_LIST      提醒清单名（默认「档期管理」）
  BRIDGE_CALENDAR     日历名（默认「摄影档期」）
  PHOTOFLOW_DATA      data.json 路径（默认 mac-bridge/../data.json）

【授权】首次运行需在 系统设置 → 隐私与安全性 → 自动化，给终端 / iTerm
勾选「提醒事项」「日历」。未授权会优雅跳过并打印原因，不崩溃。

仅依赖 Python 标准库（subprocess / os / re / sys / json / pathlib）。
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
ENV_DRY = os.environ.get("DRY_RUN", "0") in ("1", "true", "yes")
LIST_NAME = os.environ.get("REMINDERS_LIST", "档期管理")
CALENDAR_NAME = os.environ.get("BRIDGE_CALENDAR", "摄影档期")

# data.json 路径：优先用环境变量，否则用本脚本所在目录的 ../data.json
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA = SCRIPT_DIR.parent / "data.json"
DATA_PATH = Path(os.environ.get("PHOTOFLOW_DATA", str(DEFAULT_DATA)))

# v1 兼容：保留 id:p- 标记兜底
ID_MARKER_RE = re.compile(r"id:p-\S+")

# 标题归一化时要去掉的修饰词（空格分隔，每个词单独作为分词边界）
MODIFIER_TOKENS = [
    "上午", "下午", "全天", "半天", "上午半", "下午半",
    "双机位", "单机位", "多机位", "机位",
    "1.5h", "2h", "3h", "4h", "5h", "6h", "1h", "0.5h",
    "拍摄", "活动", "现场", "跟拍", "纪录", "花絮", "正片", "花絮片",
    "2026", "2025", "2024", "2027", "2028",  # 年份噪点
]


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _run_osascript(script):
    """执行 AppleScript，返回 (stdout, error)。"""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError as e:
        return None, "osascript 不存在: %s" % e
    except subprocess.TimeoutExpired as e:
        return None, "osascript 超时: %s" % e
    except Exception as e:  # noqa: BLE001
        return None, "执行异常: %s" % e
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "未知错误").strip()
    return r.stdout, None


def _asc_escape(s):
    return str(s).replace('"', '\\"').replace("\n", " ")


def _normalize_title(title):
    """归一化标题：去前后空白、统一全/半角空格。"""
    if not title:
        return ""
    t = str(title).strip()
    t = t.replace("\u3000", " ")
    t = re.sub(r"\s+", " ", t)
    return t


def _title_tokens(title):
    """把标题切成有意义的 token 集合（长度>=2 的中文串、英文单词、数字）。"""
    t = _normalize_title(title)
    if not t:
        return set()
    # 1) 把修饰词替换成空格作为分词边界
    for m in MODIFIER_TOKENS:
        t = t.replace(m, " ")
    # 2) 按非字母数字中文切分
    parts = re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", t)
    tokens = set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 中文：长度>=2 才算有意义；英文/数字：原样
        if re.match(r"[\u4e00-\u9fff]+$", p):
            if len(p) >= 2:
                tokens.add(p)
        else:
            tokens.add(p)
    return tokens


# --------------------------------------------------------------------------- #
# 读取 data.json
# --------------------------------------------------------------------------- #
def _load_projects():
    """读 data.json，返回 projects 列表（同时构建 date -> candidates 索引）。"""
    if not DATA_PATH.exists():
        print("[WARN] data.json 不存在: %s" % DATA_PATH)
        return [], {}
    try:
        with DATA_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        print("[WARN] 解析 data.json 失败: %s" % e)
        return [], {}

    projects = data.get("projects", [])
    by_date = {}
    for p in projects:
        d = p.get("date", "")
        if d:
            by_date.setdefault(d, []).append(p)
    return projects, by_date


# --------------------------------------------------------------------------- #
# 匹配判定
# --------------------------------------------------------------------------- #
def _match_to_project(rec, by_date):
    """对单条 Mac 条目，判断能否匹配到 data.json 某条 project。
    返回 (matched_bool, project_or_None, reason_str)。
    """
    title = rec.get("name") or rec.get("summary") or ""
    date = rec.get("dueDate") or rec.get("startDate") or ""

    # 1) v1 兜底：notes 含 id:p- 标记
    notes = rec.get("notes") or ""
    if ID_MARKER_RE.search(notes):
        # 尝试从 notes 抽出 id，匹配 project
        m = ID_MARKER_RE.search(notes)
        sid = m.group(0).replace("id:", "").strip() if m else None
        if sid:
            # 通过 id 查
            return True, {"id": sid}, "notes 含 id 标记（id:%s）" % sid
        return True, None, "notes 含 id:p- 标记"

    # 2) 日期必须命中 data.json 候选集
    candidates = by_date.get(date, [])
    if not candidates:
        return False, None, "日期 %s 不在 data.json 中（视为用户自建）" % (date or "?")

    # 3) 遍历 candidates 验证标题/client 命中
    norm_title = _normalize_title(title)
    for p in candidates:
        name = (p.get("name") or "").strip()
        client = (p.get("client") or "").strip()

        # 3a) 标题含 name 非空子串
        if name and name in norm_title:
            return True, p, "日期 %s 命中 data.json + 标题含 name '%s' → %s" % (date, name, p.get("id"))

        # 3b) 标题含 client 非空子串
        if client and client in norm_title:
            return True, p, "日期 %s 命中 data.json + 标题含 client '%s' → %s" % (date, client, p.get("id"))

        # 3c) token 匹配：name 的所有有意义 token 都出现在 Mac 标题的 token 集合中
        name_tokens = _title_tokens(name)
        title_tokens = _title_tokens(norm_title)
        if name_tokens and name_tokens.issubset(title_tokens):
            return True, p, "日期 %s 命中 data.json + token 全集匹配 name '%s' → %s" % (date, name, p.get("id"))

    # 4) 日期命中但标题不匹配任何 candidate → 视为用户自建（同名同日的概率极低）
    return False, None, "日期 %s 在 data.json 中但标题不匹配任何候选（视为用户自建）" % date


# --------------------------------------------------------------------------- #
# 读取 Mac 端提醒 / 日历
# --------------------------------------------------------------------------- #
def _read_reminders():
    script = '''
set listName to "{list}"
tell application "Reminders"
    try
        set theList to list listName
    on error
        return "NO_LIST"
    end try
    set out to ""
    repeat with r in reminders of theList
        try
            set rid to id of r as string
        on error
            set rid to ""
        end try
        try
            set rname to name of r
        on error
            set rname to ""
        end try
        try
            set d to due date of r
            set yr to year of d as integer
            set mo to month of d as integer
            set da to day of d as integer
            set rdue to (yr as string) & "-" & (text -2 thru -1 of ("0" & mo)) & "-" & (text -2 thru -1 of ("0" & da))
        on error
            set rdue to ""
        end try
        try
            set rnotes to notes of r
        on error
            set rnotes to ""
        end try
        set out to out & "R" & tab & rid & tab & rname & tab & rdue & tab & rnotes & linefeed
    end repeat
    return out
end tell
'''.format(list=_asc_escape(LIST_NAME))
    out, err = _run_osascript(script)
    reminders = []
    if out is None:
        print("[WARN] 读取提醒事项失败: %s" % (err or "未知"))
        return reminders
    if out.strip() == "NO_LIST":
        print("[WARN] 提醒清单「%s」不存在，跳过。" % LIST_NAME)
        return reminders
    for raw in out.split("\n"):
        line = raw.rstrip("\r")
        if not line:
            continue
        parts = line.split("\t")
        if parts[0] == "R" and len(parts) >= 5:
            reminders.append({
                "rem_id": parts[1],
                "name": parts[2],
                "dueDate": parts[3],
                "notes": parts[4],
            })
    return reminders


def _read_calendar():
    script = '''
set calName to "{cal}"
tell application "Calendar"
    try
        set targetCal to first calendar whose name is calName
    on error
        return "NO_CAL"
    end try
    set out to ""
    repeat with e in events of targetCal
        try
            set eid to uid of e
        on error
            set eid to ""
        end try
        try
            set esum to summary of e
        on error
            set esum to ""
        end try
        try
            set d to start date of e
            set yr to year of d as integer
            set mo to month of d as integer
            set da to day of d as integer
            set ds to (yr as string) & "-" & (text -2 thru -1 of ("0" & mo)) & "-" & (text -2 thru -1 of ("0" & da))
        on error
            set ds to ""
        end try
        try
            set enotes to notes of e
        on error
            set enotes to ""
        end try
        set out to out & "E" & tab & eid & tab & esum & tab & ds & tab & enotes & linefeed
    end repeat
    return out
end tell
'''.format(cal=_asc_escape(CALENDAR_NAME))
    out, err = _run_osascript(script)
    events = []
    if out is None:
        print("[WARN] 读取日历失败: %s" % (err or "未知"))
        return events
    if out.strip() == "NO_CAL":
        print("[WARN] 日历「%s」不存在，跳过。" % CALENDAR_NAME)
        return events
    for raw in out.split("\n"):
        line = raw.rstrip("\r")
        if not line:
            continue
        parts = line.split("\t")
        if parts[0] == "E" and len(parts) >= 5:
            events.append({
                "ev_id": parts[1],
                "summary": parts[2],
                "startDate": parts[3],
                "notes": parts[4],
            })
    return events


# --------------------------------------------------------------------------- #
# 删除脚本构造
# --------------------------------------------------------------------------- #
def _build_delete_reminder(rec):
    return '''
set listName to "{list}"
set pid to "{pid}"
tell application "Reminders"
    try
        set theList to list listName
    on error
        return "NO_LIST"
    end try
    repeat with r in reminders of theList
        if (id of r as string) is pid then
            delete r
            return "OK"
        end if
    end repeat
    return "NOT_FOUND"
end tell
'''.format(list=_asc_escape(LIST_NAME), pid=_asc_escape(rec["rem_id"]))


def _build_delete_event(rec):
    return '''
set calName to "{cal}"
set eid to "{eid}"
tell application "Calendar"
    try
        set targetCal to first calendar whose name is calName
    on error
        return "NO_CAL"
    end try
    repeat with e in events of targetCal
        if uid of e is eid then
            delete e
            return "OK"
        end if
    end repeat
    return "NOT_FOUND"
end tell
'''.format(cal=_asc_escape(CALENDAR_NAME), eid=_asc_escape(rec["ev_id"]))


def _delete(rec, builder, label, reason, project):
    label_text = rec.get("name") or rec.get("summary") or "?"
    date_text = rec.get("dueDate") or rec.get("startDate") or "?"
    pid = (project or {}).get("id", "—")
    print("  [将删除] %s — %s @ %s" % (label, label_text, date_text))
    print("           └─ %s" % reason)
    if ENV_DRY:
        return True
    out, err = _run_osascript(builder(rec))
    if out is None:
        print("[WARN] 删除%s失败: %s" % (label, err))
        return False
    return True


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    print("=" * 60)
    print("cleanup-test-entries.py (v2)  ·  按 data.json 模糊匹配删测试档期")
    print("data.json: %s" % DATA_PATH)
    print("模式: %s" % ("DRY_RUN 预览（不真删）" if ENV_DRY else "正式删除（已确认）"))
    print("=" * 60)

    projects, by_date = _load_projects()
    if not projects:
        print("[错误] data.json 没读取到 projects，停止。")
        sys.exit(1)
    print("已加载 data.json projects: %d 条，跨 %d 个不同日期" % (len(projects), len(by_date)))

    reminders = _read_reminders()
    events = _read_calendar()

    print("\n扫描结果：")
    print("  提醒事项「%s」: 共 %d 条" % (LIST_NAME, len(reminders)))
    print("  日历「%s」: 共 %d 条" % (CALENDAR_NAME, len(events)))

    # 分类
    to_delete_reminders = []  # [(rec, project, reason)]
    to_delete_events = []
    skip_reminders = []
    skip_events = []

    for r in reminders:
        ok, proj, reason = _match_to_project(r, by_date)
        if ok:
            to_delete_reminders.append((r, proj, reason))
        else:
            skip_reminders.append((r, reason))

    for e in events:
        ok, proj, reason = _match_to_project(e, by_date)
        if ok:
            to_delete_events.append((e, proj, reason))
        else:
            skip_events.append((e, reason))

    print("\n分类结果：")
    print("  提醒事项 → 待删 %d 条 / 保留 %d 条" % (len(to_delete_reminders), len(skip_reminders)))
    print("  日历事件 → 待删 %d 条 / 保留 %d 条" % (len(to_delete_events), len(skip_events)))

    if not to_delete_reminders and not to_delete_events:
        print("\n✓ 没有发现匹配 data.json 的测试条目，无需清理。")
        sys.exit(0)

    print("\n%s以下条目将被删除（仅 data.json 58 条里能匹配上的；用户自建条目全部保留）：" %
          ("[预览] " if ENV_DRY else ""))
    for r, proj, reason in to_delete_reminders:
        _delete(r, _build_delete_reminder, "提醒", reason, proj)
    for e, proj, reason in to_delete_events:
        _delete(e, _build_delete_event, "日历", reason, proj)

    if not ENV_DRY:
        print("\n✓ 已执行删除：提醒 %d 条，日历 %d 条。" %
              (len(to_delete_reminders), len(to_delete_events)))
        print("  用户自己在 Mac 上手动建的档期（不在 data.json 的）已全部保留。")
        sys.exit(0)

    # DRY_RUN 模式：询问是否真删
    print("\n" + "-" * 60)
    print("以上为预览，未执行删除。")
    if not sys.stdin.isatty():
        print("[INFO] 非交互式 stdin（如 .command 自动跑），请用 y/n 环境变量控制真删。")
        print("       y: 真删 / n: 退出（默认）。")
        choice = os.environ.get("CONFIRM_DELETE", "n").lower()
    else:
        try:
            choice = input("确认要真删吗？输入 y 回车真删，其他任意键退出: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "n"

    if choice != "y":
        print("→ 已退出，未删除任何内容。")
        sys.exit(0)

    # 真删
    print("\n开始真删...")
    ok_r, ok_e = 0, 0
    for r, proj, reason in to_delete_reminders:
        out, err = _run_osascript(_build_delete_reminder(r))
        if out is not None:
            ok_r += 1
        else:
            print("[WARN] 删除失败: %s" % err)
    for e, proj, reason in to_delete_events:
        out, err = _run_osascript(_build_delete_event(e))
        if out is not None:
            ok_e += 1
        else:
            print("[WARN] 删除失败: %s" % err)

    print("\n✓ 清理完成：成功删提醒 %d 条、日历 %d 条。" % (ok_r, ok_e))
    print("  用户自建条目（不在 data.json）全部保留。")


if __name__ == "__main__":
    main()
