#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reconcile.py — Mac 对账守护进程（提醒事项 + 日历 ↔ data.json，以用户自建为主）
================================================================================

【核心原则：以日期为主键，手机档期自动镜像到 Mac，已存在不重复创建】
  - 对 data.json 的每条 project：
      * 以 日期(date) 为主键，在 Mac 提醒/日历条目里找匹配；
        标题(拍摄-MMDD 客户)做模糊兜底。
      * 匹配到 → 把 `id:p-YYYYMMDD-XX` 标记写进该条目的 notes
        （标注更新，不重命名），并按 data.json 更新其 4 个子任务完成状态
        （拍摄 / 交付 / 开票 / 结款）；保留用户原名称，写回 data.json
        的 `displayName`（不覆盖 `name`）。
      * 未匹配到（手机有、Mac 没有）→ 自动在提醒事项清单与日历中
        新建对应条目（提醒含 4 个子任务、事件含日期区间），使手机档期
        自动镜像到 Mac；新建时写入 `id:p-<id>` 标记，下次按日期命中
        已建条目、不会重复创建。
  - 删除：data.json 中已移除的档期，会从 Mac 提醒/日历中删除（仅删除带 `id:p-`
    标记的镜像条目，绝不误删用户在 Mac 上自建、无标记的条目）。

【数据真源】
  data.json 形如：
    { "updatedAt": "ISO 时间", "projects": [ {id, name, client, date, type,
        payment:{date,amount,method}, publicAccount:{date,amount},
        invoice:{no,date,sent}, shootDone, deliverDone, deliverMethod, notes,
        source, displayName, mirror:{calendar:{exists}, reminders:{matched}}}, ... ] }

  4 个状态点（与 schedule-viewer.html 完全一致）：
    拍摄 = shootDone 非空
    交付 = deliverDone 非空
    开票 = invoice.no 非空
    结款 = publicAccount.amount > 0

【读取顺序】
  1) 若设置了 GIT_LOCAL：先 `git -C <GIT_LOCAL> pull`，再读 <GIT_LOCAL>/data.json
  2) 若设置了 BAIDU_SYNC_DIR：读 <BAIDU_SYNC_DIR>/data.json
  3) 两者都有时，取 updatedAt 较新的一份
  读不到则降级为空 {projects:[]} 并打印警告，不崩溃。

【匹配键】
  - 主键：project.date（YYYY-MM-DD）→ 用户条目 dueDate / startDate。
  - 兜底：title_of(p) = "拍摄-MMDD 客户/事件" 与用户条目名称做包含/前缀匹配。
  （若用户在 Mac 上建的条目恰好也带 `id:p-` 标记，会优先被同一日期匹配命中，
    不影响「自动镜像」原则：匹配命中则标注更新、不重复新建。）

【环境变量】
  REMINDERS_LIST   提醒清单名（默认「档期管理」）
  BRIDGE_CALENDAR  日历名（默认「摄影档期」）
  GIT_LOCAL        git 本地仓库路径（含 data.json）；留空则不走 git
  BAIDU_SYNC_DIR   百度网盘同步文件夹路径（含 data.json）；留空则不走百度
  DRY_RUN=1        不真正执行 osascript，仅打印将要执行的动作与 AppleScript
  RECONCILE_LOG    日志文件路径（追加写入对账摘要）

【用法】
  # 干跑（只看将要做什么，不真写）
  DRY_RUN=1 GIT_LOCAL=~/photoflow-schedule BAIDU_SYNC_DIR=~/BaiduNetdisk/档期 \\
      /usr/bin/python3 mac-bridge/reconcile.py

  # 真正对账（通常由 LaunchAgent 每 5 分钟调用）
  GIT_LOCAL=~/photoflow-schedule /usr/bin/python3 mac-bridge/reconcile.py

  # 去重清理（清理因超时重复创建的条目）
  GIT_LOCAL=~/photoflow-schedule /usr/bin/python3 mac-bridge/reconcile.py --deduplicate

【授权】首次运行需在 系统设置→隐私与安全性→自动化 给运行解释器（终端/iTerm）
勾「提醒事项」「日历」。未授权会优雅跳过并打印原因，不崩溃。
"""

import datetime
import json
import os
import re
import subprocess
import sys

# --------------------------------------------------------------------------- #
# 配置（环境变量）
# --------------------------------------------------------------------------- #
DRY_RUN = os.environ.get("DRY_RUN", "0") in ("1", "true", "yes")
LIST_NAME = os.environ.get("REMINDERS_LIST", "档期管理")
CALENDAR_NAME = os.environ.get("BRIDGE_CALENDAR", "摄影档期")
GIT_LOCAL = os.environ.get("GIT_LOCAL", "")
BAIDU_SYNC_DIR = os.environ.get("BAIDU_SYNC_DIR", "")
RECONCILE_LOG = os.environ.get("RECONCILE_LOG", "")

STEP_CN = ["拍摄", "交付", "开票", "结款"]          # 4 个步骤的中文名
STEP_KEY = {"拍摄": "shoot", "交付": "deliver", "开票": "invoice", "结款": "settle"}


def _body_for_status(st, sid=""):
    """把 4 状态压成 body 文本（id 标记 + 4 行 checkbox）。

    macOS 26 上 Reminders AppleScript 没有 subtask 类（仅 macOS 27+ 计划支持），
    所以状态存进 body 文本里：每行 `<步骤>:<☑|☐>`。
    """
    lines = []
    if sid:
        lines.append("id:%s" % sid)
    for cn in STEP_CN:
        mark = "☑" if st[STEP_KEY[cn]] else "☐"
        lines.append("%s:%s" % (cn, mark))
    return "\n".join(lines)


def _parse_status_from_body(body):
    """从 body 文本解析 4 个状态（☑ = True，☐ = False），返回 {步骤: bool}。

    没匹配上的步骤视为 False（未勾）。兼容缺行/乱序/多余文本。
    """
    out = {cn: False for cn in STEP_CN}
    if not body:
        return out
    for line in body.split("\n"):
        for cn in STEP_CN:
            # 找形如 "拍摄:☑" "拍摄: ☐" "拍摄 ☑" 等
            if line.startswith(cn):
                tail = line[len(cn):].lstrip(": ： \t")
                if "☑" in tail:
                    out[cn] = True
                elif "☐" in tail:
                    out[cn] = False
                break
    return out


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _expand(p):
    return os.path.expanduser(p) if p else ""


def _run_osascript(script, timeout=30):
    """执行 AppleScript，返回 (stdout, error)。任何异常都转为 error。

    timeout 默认 30 秒：读取提醒/日历的超时设为短一点（调用方传 timeout 覆盖），
    避免被授权卡住的进程挂住整个对账流程。

    关键：用 `launchctl asuser 501 osascript` 而不是直接 `osascript`，
    否则 launchd 后台进程调起的 osascript 子进程不被 TCC 允许（-1700）。
    """
    try:
        r = subprocess.run(
            ["launchctl", "asuser", "501", "/usr/bin/osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
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
    """转义 AppleScript 双引号。"""
    return str(s).replace('"', '\\"')


def _asc_str_list(lines):
    """把多行文本拼成 AppleScript 字符串表达式（用 linefeed 连接，避免字面换行报错）。"""
    parts = ['"%s"' % _asc_escape(ln) for ln in lines]
    return " & linefeed & ".join(parts) if parts else '""'


def _to_iso_date(s):
    """YYYY-MM-DD -> (y, m, d) 整数；无效返回 None。"""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(s or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _mmdd(s):
    """YYYY-MM-DD -> MMDD 字符串，供标题使用。"""
    t = _to_iso_date(s)
    if not t:
        return "0000"
    return "%02d%02d" % (t[1], t[2])


def _asc_date_expr(date_str, hour=0, minute=0, var_name="d"):
    """返回一段 AppleScript 语句，把变量 d 设为 YYYY-MM-DD HH:MM（locale 无关）。

    用 `current date` + 逐字段赋值，避免 `date "..."` 的本地化解析问题。
    var_name 允许调用方指定变量名（如事件脚本里分别用 s 和 e）。
    """
    t = _to_iso_date(date_str)
    if not t:
        today = datetime.date.today()
        y, mo, d = today.year, today.month, today.day
    else:
        y, mo, d = t
    secs = hour * 3600 + minute * 60
    v = var_name
    return (
        'set year of {v} to {y}\n'
        'set month of {v} to {mo}\n'
        'set day of {v} to {d}\n'
        'set time of {v} to {s}\n'
    ).format(v=v, y=y, mo=mo, d=d, s=secs)


# --------------------------------------------------------------------------- #
# 状态推导（与 schedule-viewer.html 完全一致）
# --------------------------------------------------------------------------- #
def status_of(p):
    return {
        "shoot":   bool(p.get("shootDone") and str(p.get("shootDone")).strip() != ""),
        "deliver": bool(p.get("deliverDone") and str(p.get("deliverDone")).strip() != ""),
        "invoice": bool(p.get("invoice", {}).get("no") and str(p.get("invoice", {}).get("no")).strip() != ""),
        "settle":  bool(float(p.get("publicAccount", {}).get("amount") or 0) > 0),
    }


def title_of(p):
    """锁屏标题：拍摄-MMDD 客户/事件。"""
    display = (p.get("client") or p.get("name") or "事件")
    return "拍摄-%s %s" % (_mmdd(p.get("date")), display)


# --------------------------------------------------------------------------- #
# 名称校准与回写（Mac 端优先）
# --------------------------------------------------------------------------- #
def _extract_source(name):
    """从 Mac 端名称里提取客户来源关键词。

    含「直客」→ 直客；含「介绍」→ 介绍；否则返回 None（保持原推断/标注）。
    """
    if not name:
        return None
    s = str(name)
    if "直客" in s:
        return "直客"
    if "介绍" in s:
        return "介绍"
    return None


def _write_data(path, data):
    """把校准后的 data 写回磁盘（保留原有字段，仅补充 displayName/source/mirror）。

    返回是否成功写入。
    """
    if not path:
        return False
    try:
        with open(os.path.expanduser(path), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:  # noqa: BLE001
        print("[WARN] 回写 data.json 失败: %s" % e)
        return False


# --------------------------------------------------------------------------- #
# 读取数据真源
# --------------------------------------------------------------------------- #
def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "projects" in d:
            return d
    except Exception as e:  # noqa: BLE001
        print("[WARN] 读取 %s 失败: %s" % (path, e))
    return None


def _parse_updated_at(s):
    try:
        d = datetime.datetime.fromisoformat(str(s))
        if d.tzinfo is not None:
            d = d.replace(tzinfo=None)  # 统一为无时区，避免与 datetime.min 比较报 naive/aware 错
        return d
    except Exception:  # noqa: BLE001
        return datetime.datetime.min


def load_data():
    """读取数据真源，返回 (data, source_path)。

    source_path 为实际采用的那份 data.json 的磁盘路径，用于回写校准结果
    （displayName / source / mirror）。无任何候选时返回 ({...}, "")。
    """
    candidates = []  # 元素为 (data, path)
    gl = _expand(GIT_LOCAL)
    if gl:
        try:
            subprocess.run(["git", "-C", gl, "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=60)
        except Exception as e:  # noqa: BLE001
            print("[WARN] git pull 失败（继续用本地副本）: %s" % e)
        p = os.path.join(gl, "data.json")
        if os.path.isfile(p):
            d = _read_json_file(p)
            if d is not None:
                candidates.append((d, p))
    bd = _expand(BAIDU_SYNC_DIR)
    if bd:
        p = os.path.join(bd, "data.json")
        if os.path.isfile(p):
            d = _read_json_file(p)
            if d is not None:
                candidates.append((d, p))

    if not candidates:
        print("[WARN] 未找到任何 data.json（GIT_LOCAL/BAIDU_SYNC_DIR 均未提供或不存在），降级为空数据。")
        return {"updatedAt": "", "projects": []}, ""
    best, best_path = max(candidates, key=lambda c: _parse_updated_at(c[0].get("updatedAt", "")))
    print("[INFO] 采用 updatedAt=%s 的 data.json（共 %d 份候选，路径 %s）" %
          (best.get("updatedAt") or "?", len(candidates), best_path))
    return best, best_path


# --------------------------------------------------------------------------- #
# 读取现有镜像（提醒事项 + 日历）—— 仅读取，用于匹配与标注
# --------------------------------------------------------------------------- #
def _read_reminders():
    """返回列表，元素为 {rem_id, name, dueDate, date, notes, subtasks:{cn:bool}}。"""
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
            set rnotes to body of r
        on error
            set rnotes to ""
        end try
        set out to out & "R" & tab & rid & tab & rname & tab & rdue & tab & rnotes & linefeed
    end repeat
    return out
end tell
'''.format(list=_asc_escape(LIST_NAME))
    out, err = _run_osascript(script, timeout=120)
    reminders = []
    if out is None:
        print("[WARN] 读取提醒事项失败: %s" % (err or "未知"))
        return reminders
    if out.strip() == "NO_LIST":
        print("[WARN] 提醒清单「%s」不存在，跳过。" % LIST_NAME)
        return reminders
    rems, order = {}, []
    for raw in out.split("\n"):
        line = raw.rstrip("\r")
        if not line:
            continue
        parts = line.split("\t")
        if parts[0] == "R" and len(parts) >= 5:
            rid, rname, rdue, rnotes = parts[1], parts[2], parts[3], parts[4]
            # 从 body 文本解析 4 个状态（macOS 26 上 subtask API 不可用，改用 body 文本）
            subs = _parse_status_from_body(rnotes)
            rems[rid] = {"rem_id": rid, "name": rname, "dueDate": rdue,
                          "date": rdue, "notes": rnotes, "subtasks": subs}
            order.append(rid)
    return [rems[k] for k in order]


def _read_calendar():
    """返回列表，元素为 {ev_id, summary, startDate, date, notes}。"""
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
            set enotes to description of e
        on error
            set enotes to ""
        end try
        set out to out & "E" & tab & eid & tab & esum & tab & ds & tab & enotes & linefeed
    end repeat
    return out
end tell
'''.format(cal=_asc_escape(CALENDAR_NAME))
    out, err = _run_osascript(script, timeout=120)
    events = []
    if out is None:
        print("[WARN] 读取日历失败: %s" % (err or "未知"))
        return events
    if out.strip() == "NO_CAL":
        print("[WARN] 日历「%s」不存在，跳过。" % CALENDAR_NAME)
        return events
    evs, order = {}, []
    for raw in out.split("\n"):
        line = raw.rstrip("\r")
        if not line:
            continue
        parts = line.split("\t")
        if parts[0] == "E" and len(parts) >= 5:
            eid, esum, ds, enotes = parts[1], parts[2], parts[3], parts[4]
            evs[eid] = {"ev_id": eid, "summary": esum, "startDate": ds,
                         "date": ds, "notes": enotes}
            order.append(eid)
    return [evs[k] for k in order]


# --------------------------------------------------------------------------- #
# 匹配：日期为主键，标题做模糊兜底（绝不新建，匹配不上就标注未找到）
# --------------------------------------------------------------------------- #
def _best_title(cands, p):
    """在日期命中的候选里，挑名称最贴近的一条。"""
    name = (p.get("name") or "").strip()
    client = (p.get("client") or "").strip()
    title = title_of(p)
    for r in cands:
        n = (r.get("name") or r.get("summary") or "").strip()
        if n and (n == title or n == name):
            return r
    for r in cands:
        n = (r.get("name") or r.get("summary") or "").strip()
        if (name and name in n) or (client and client in n):
            return r
    return cands[0]


def _match_by_date(items, p):
    date = (p.get("date") or "").strip()
    if not date:
        return None
    cands = [r for r in items if (r.get("date") or "").strip() == date]
    if not cands:
        return None
    return _best_title(cands, p)


def _match_fuzzy(items, p):
    name = (p.get("name") or "").strip()
    client = (p.get("client") or "").strip()
    if not name and not client:
        return None
    for r in items:
        n = (r.get("name") or r.get("summary") or "").strip()
        if (name and name in n) or (client and client in n):
            return r
    # 更宽松：项目名称前 2 个字包含匹配
    if name and len(name) >= 2:
        sub = name[:2]
        for r in items:
            n = (r.get("name") or r.get("summary") or "").strip()
            if sub in n:
                return r
    return None


# --------------------------------------------------------------------------- #
# 标注脚本构造（只改用户已有条目的 notes + 子任务，绝不新建）
# --------------------------------------------------------------------------- #
def _build_annotate_reminder(rem, p):
    """在用户已有提醒里写 body（含 id 标记 + 4 行 checkbox 状态），并把标题同步成 data.json 的标题。

    macOS 26 上 Reminders AppleScript 没有 subtask 类，所以状态存进 body 文本。
    """
    sid = p.get("id", "")
    st = status_of(p)
    new_body = _body_for_status(st, sid)
    body_expr = _asc_str_list(new_body.split("\n"))
    return (
        'set listName to "{list}"\n'
        'set pid to "{pid}"\n'
        'tell application "Reminders"\n'
        '    try\n'
        '        set theList to list listName\n'
        '    on error\n'
        '        return "NO_LIST"\n'
        '    end try\n'
        '    repeat with r in reminders of theList\n'
        '        if (id of r as string) is pid then\n'
        '            set body of r to {body}\n'
        '            set name of r to "{name}"\n'
        '            return "OK"\n'
        '        end if\n'
        '    end repeat\n'
        '    return "NOT_FOUND"\n'
        'end tell\n'
    ).format(list=_asc_escape(LIST_NAME), pid=_asc_escape(rem["rem_id"]),
             body=body_expr, name=_asc_escape(title_of(p)))


def _build_annotate_event(ev, p):
    """在用户已有日历事件里追加 `id:p-...` 标记，并按 data.json 同步标题（日历事件无子任务）。"""
    sid = p.get("id", "")
    cur = ev.get("notes") or ""
    marker = "id:%s" % sid
    new_notes = cur if marker in cur else ((cur + "\n" + marker) if cur.strip() else marker)
    notes_expr = _asc_str_list(new_notes.split("\n"))
    return (
        'set calName to "{cal}"\n'
        'set eid to "{eid}"\n'
        'tell application "Calendar"\n'
        '    try\n'
        '        set targetCal to first calendar whose name is calName\n'
        '    on error\n'
        '        return "NO_CAL"\n'
        '    end try\n'
        '    repeat with e in events of targetCal\n'
        '        if uid of e is eid then\n'
        '            set description of e to {notes}\n'
        '            set summary of e to "{name}"\n'
        '            return "OK"\n'
        '        end if\n'
        '    end repeat\n'
        '    return "NOT_FOUND"\n'
        'end tell\n'
    ).format(cal=_asc_escape(CALENDAR_NAME), eid=_asc_escape(ev["ev_id"]),
             notes=notes_expr, name=_asc_escape(title_of(p)))


# --------------------------------------------------------------------------- #
# 新建脚本构造（手机有、Mac 没有：自动镜像新建提醒/事件）
# --------------------------------------------------------------------------- #
def _build_create_reminder(p):
    """在 REMINDERS_LIST 清单新建一条提醒（清单不存在则先创建清单）。

    名称 title_of(p)；到期日 p["date"] 09:00；4 个子任务按 status_of(p)
    设完成态（存进 body 文本，macOS 26 上 Reminders 没有 subtask 类）。
    notes 写入 `id:p-<id>` 标记，便于下次按日期命中、避免重复建。
    """
    sid = p.get("id", "")
    st = status_of(p)
    name = title_of(p)
    duelines = _asc_date_expr(p.get("date"), hour=9, minute=0)
    body_text = _body_for_status(st, sid)
    body_expr = _asc_str_list(body_text.split("\n"))

    return (
        'set listName to "{list}"\n'
        'tell application "Reminders" to activate\n'
        'tell application "Reminders"\n'
        '    try\n'
        '        set theList to list listName\n'
        '    on error\n'
        '        try\n'
        '            set theList to make new list with properties {{name:listName}}\n'
        '        on error\n'
        '            return "NO_LIST"\n'
        '        end try\n'
        '    end try\n'
        '    set d to current date\n'
        '{duelines}'
        '    set r to make new reminder at end of reminders of theList '
        'with properties {{name:"{name}", due date:d, body:{body}}}\n'
        '    return "OK"\n'
        'end tell\n'
    ).format(list=_asc_escape(LIST_NAME), name=_asc_escape(name),
             duelines=duelines, body=body_expr)


def _build_create_event(p):
    """在 BRIDGE_CALENDAR 日历新建一个事件（日历不存在则尝试创建，失败返回 NO_CAL）。

    事件无子任务：summary=title_of(p)，start/end 为 p["date"] 09:00–10:00，
    notes 写入 `id:p-<id>` 标记。日历无法创建时脚本返回 NO_CAL，由调用方
    WARN 并跳过（提醒仍建）。
    """
    sid = p.get("id", "")
    name = title_of(p)
    startlines = _asc_date_expr(p.get("date"), hour=9, minute=0, var_name="s")
    endlines = _asc_date_expr(p.get("date"), hour=10, minute=0, var_name="e")
    note_marker = "id:%s" % sid
    notes_expr = _asc_str_list([note_marker])

    return (
        'set calName to "{cal}"\n'
        'tell application "Calendar" to activate\n'
        'tell application "Calendar"\n'
        '    try\n'
        '        set targetCal to first calendar whose name is calName\n'
        '    on error\n'
        '        try\n'
        '            set targetCal to make new calendar with properties {{name:calName}}\n'
        '        on error\n'
        '            return "NO_CAL"\n'
        '        end try\n'
        '    end try\n'
        '    set s to current date\n'
        '{startlines}'
        '    set e to current date\n'
        '{endlines}'
        '    make new event at end of events of targetCal with properties '
        '{{summary:"{name}", start date:s, end date:e, description:{desc}}}\n'
        '    return "OK"\n'
        'end tell\n'
    ).format(cal=_asc_escape(CALENDAR_NAME), name=_asc_escape(name),
             startlines=startlines, endlines=endlines, desc=notes_expr)


# --------------------------------------------------------------------------- #
# 批量删除脚本构造（用于去重清理，单进程删多条减少开销）
# --------------------------------------------------------------------------- #
def _build_batch_delete_reminders(rem_ids):
    """批量删除提醒，用单次 osascript 调用删多条。

    用一轮遍历 + 字符串包含匹配（|id1|id2|...|）实现 O(n) 删除，
    避免为每个 ID 单独执行 `whose` 查询（O(n²)）导致的严重超时。
    """
    if not rem_ids:
        return None
    # 把所有 ID 用竖线包裹拼接成一个查找字符串，实现 O(1) 匹配
    id_ref = "|" + "|".join(_asc_escape(rid) for rid in rem_ids) + "|"
    return (
        'set listName to "{list}"\n'
        'set idRef to "{id_ref}"\n'
        'tell application "Reminders"\n'
        '    try\n'
        '        set theList to list listName\n'
        '    on error\n'
        '        return "NO_LIST"\n'
        '    end try\n'
        '    repeat with r in reminders of theList\n'
        '        try\n'
        '            set rid to id of r as string\n'
        '            if idRef contains ("|" & rid & "|") then\n'
        '                delete r\n'
        '            end if\n'
        '        end try\n'
        '    end repeat\n'
        '    return "OK"\n'
        'end tell\n'
    ).format(list=_asc_escape(LIST_NAME), id_ref=id_ref)


def _build_batch_delete_events(ev_ids):
    """批量删除日历事件，用单次 osascript 调用删多条。"""
    if not ev_ids:
        return None
    lines = []
    for eid in ev_ids:
        lines.append(
            '    try\n'
            '        delete (first event of targetCal whose uid is "{eid}")\n'
            '    end try\n'.format(eid=_asc_escape(eid))
        )
    return (
        'set calName to "{cal}"\n'
        'tell application "Calendar"\n'
        '    try\n'
        '        set targetCal to first calendar whose name is calName\n'
        '    on error\n'
        '        return "NO_CAL"\n'
        '    end try\n'
        '{ops}'
        '    return "OK"\n'
        'end tell\n'
    ).format(cal=_asc_escape(CALENDAR_NAME), ops="\n".join(lines))


# --------------------------------------------------------------------------- #
# 执行（含 DRY_RUN）
# --------------------------------------------------------------------------- #
_LAST_OSA_ERR = ""     # 最近一次 osascript 失败的真实原因（供 summary 写入日志）


def run_script(script, label):
    global _LAST_OSA_ERR
    if DRY_RUN:
        print("\n[DRY-RUN] === %s ===\n%s\n" % (label, script))
        _LAST_OSA_ERR = ""
        return True
    out, err = _run_osascript(script)
    if out is None:
        _LAST_OSA_ERR = (err or "").strip() or "未知错误"
        print("[WARN] %s 失败: %s" % (label, _LAST_OSA_ERR))
        return False
    _LAST_OSA_ERR = ""
    return True


def _err_suffix():
    """把最近一次 osascript 的真实错误拼成 ' — 原因'，没有就空串。"""
    return (" — " + _LAST_OSA_ERR) if _LAST_OSA_ERR else ""


# --------------------------------------------------------------------------- #
# 去重清理（命令行 --deduplicate）
# --------------------------------------------------------------------------- #
def _read_reminders_slow():
    """读取全部提醒事项（用于去重，超时放宽到 600s）。

    macOS Reminders 条目较多时（>500 条）AppleScript 遍历很慢，
    去重专用的读取用更长超时确保能完整读完。
    """
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
            set rnotes to body of r
        on error
            set rnotes to ""
        end try
        set out to out & "R" & tab & rid & tab & rname & tab & rdue & tab & rnotes & linefeed
    end repeat
    return out
end tell
'''.format(list=_asc_escape(LIST_NAME))
    out, err = _run_osascript(script, timeout=600)
    reminders = []
    if out is None:
        print("[WARN] 读取提醒事项失败: %s" % (err or "未知"))
        return reminders
    if out.strip() == "NO_LIST":
        print("[WARN] 提醒清单「%s」不存在，跳过。" % LIST_NAME)
        return reminders
    rems, order = {}, []
    for raw in out.split("\n"):
        line = raw.rstrip("\r")
        if not line:
            continue
        parts = line.split("\t")
        if parts[0] == "R" and len(parts) >= 5:
            rid, rname, rdue, rnotes = parts[1], parts[2], parts[3], parts[4]
            subs = _parse_status_from_body(rnotes)
            rems[rid] = {"rem_id": rid, "name": rname, "dueDate": rdue,
                          "date": rdue, "notes": rnotes, "subtasks": subs}
            order.append(rid)
    return [rems[k] for k in order]


def _deduplicate():
    """清理因超时重复创建的 reminders 和 events。

    1. 按 marker 分组，每 mid 保留 1 条，删除多余重复；
    2. 无 marker 条目：若与任何 project 不匹配，删除。
    """
    print("[INFO] 开始去重清理...")

    # 注意：提醒事项读取较慢（685+条时需约 240s），用专用函数 + 600s 超时
    reminders = _read_reminders_slow()
    events = _read_calendar()
    data, data_path = load_data()
    projects = data.get("projects", []) or []
    current_ids = set(p.get("id") for p in projects if p.get("id"))

    print("[INFO] 当前: 提醒 %d 条, 日历 %d 条, data.json %d 个项目" %
          (len(reminders), len(events), len(projects)))

    # ============ Reminders ============
    rem_by_marker = {}
    rem_no_marker = []
    for r in reminders:
        mid = _parse_marker(r.get("notes"))
        if mid:
            rem_by_marker.setdefault(mid, []).append(r)
        else:
            rem_no_marker.append(r)

    rem_to_delete = []
    # 带 marker 的重复：每 mid 留 1 条
    for mid, items in sorted(rem_by_marker.items()):
        if len(items) > 1:
            keep = items[0]
            dupes = items[1:]
            rem_to_delete.extend(dupes)
            print("[DEDUP] 提醒 %s: %d 条重复, 保留 1 条, 删 %d 条" %
                  (mid, len(items), len(dupes)))

    # 无 marker 条目：若匹配不到 project 则删除
    for r in rem_no_marker:
        matched = False
        for p in projects:
            if _match_by_date([r], p) or _match_fuzzy([r], p):
                matched = True
                break
        if not matched:
            rem_to_delete.append(r)
            print("[DEDUP] 提醒无标记且无匹配(待删): %s" % (r.get("name", "?")))

    # ============ Events ============
    ev_by_marker = {}
    ev_no_marker = []
    for e in events:
        mid = _parse_marker(e.get("notes"))
        if mid:
            ev_by_marker.setdefault(mid, []).append(e)
        else:
            ev_no_marker.append(e)

    ev_to_delete = []
    for mid, items in sorted(ev_by_marker.items()):
        if len(items) > 1:
            keep = items[0]
            dupes = items[1:]
            ev_to_delete.extend(dupes)
            print("[DEDUP] 日历 %s: %d 条重复, 保留 1 条, 删 %d 条" %
                  (mid, len(items), len(dupes)))

    for e in ev_no_marker:
        matched = False
        for p in projects:
            if _match_by_date([e], p) or _match_fuzzy([e], p):
                matched = True
                break
        if not matched:
            ev_to_delete.append(e)
            print("[DEDUP] 日历无标记且无匹配(待删): %s" % (e.get("summary", "?")))

    if not rem_to_delete and not ev_to_delete:
        print("[INFO] 无需清理，所有条目已唯一")
        return

    print("\n[INFO] 准备删除: 提醒 %d 条, 日历 %d 条" %
          (len(rem_to_delete), len(ev_to_delete)))

    if DRY_RUN:
        print("[DRY-RUN] 跳过实际删除")
        return

    # 批量删除提醒（分批删除，每批最多 200 条）
    if rem_to_delete:
        rem_ids = [r["rem_id"] for r in rem_to_delete]
        batch_size = 200
        total_deleted = 0
        for i in range(0, len(rem_ids), batch_size):
            batch = rem_ids[i:i + batch_size]
            script = _build_batch_delete_reminders(batch)
            if script:
                out, err = _run_osascript(script, timeout=300)
                if out is None:
                    print("[WARN] 批量删除提醒失败(%d-%d): %s" %
                          (i, i + len(batch), err))
                else:
                    total_deleted += len(batch)
                    print("[INFO] 已批量删除提醒 %d-%d (%d 条)" %
                          (i, i + len(batch), len(batch)))
        print("[INFO] 提醒删除完成: 共删除 %d / %d 条" % (total_deleted, len(rem_ids)))

    # 批量删除日历
    if ev_to_delete:
        ev_ids = [e["ev_id"] for e in ev_to_delete]
        script = _build_batch_delete_events(ev_ids)
        if script:
            out, err = _run_osascript(script, timeout=300)
            if out is None:
                print("[WARN] 批量删除日历失败: %s" % err)
            else:
                print("[INFO] 已批量删除 %d 条日历事件" % len(ev_ids))

    # 验证清理结果
    print("[INFO] 去重完成，执行后验证...")
    after_rem = _read_reminders_slow()
    after_ev = _read_calendar()
    rem_with_marker = sum(1 for r in after_rem if _parse_marker(r.get("notes")))
    ev_with_marker = sum(1 for e in after_ev if _parse_marker(e.get("notes")))
    print("[INFO] 清理后: 提醒 %d 条(带标记 %d), 日历 %d 条(带标记 %d)" %
          (len(after_rem), rem_with_marker, len(after_ev), ev_with_marker))


# --------------------------------------------------------------------------- #
# 主对账（data.json 为唯一真源：增→新建 / 改→更新 / 删→移除，Mac 端忠实镜像）
# --------------------------------------------------------------------------- #
def _parse_marker(notes):
    """从 notes 中解析出 `id:p-XXX` 标记，返回项目 id 或 None。"""
    if not notes:
        return None
    m = re.search(r"id:(p-[A-Za-z0-9_\-]+)", notes)
    return m.group(1) if m else None


def _build_delete_reminder(rem):
    """删除指定提醒（按 rem_id）。"""
    return (
        'set listName to "{list}"\n'
        'set pid to "{pid}"\n'
        'tell application "Reminders"\n'
        '    try\n'
        '        set theList to list listName\n'
        '    on error\n'
        '        return "NO_LIST"\n'
        '    end try\n'
        '    repeat with r in reminders of theList\n'
        '        if (id of r as string) is pid then\n'
        '            delete r\n'
        '            return "OK"\n'
        '        end if\n'
        '    end repeat\n'
        '    return "NOT_FOUND"\n'
        'end tell\n'
    ).format(list=_asc_escape(LIST_NAME), pid=_asc_escape(rem["rem_id"]))


def _build_delete_event(ev):
    """删除指定日历事件（按 ev_id）。"""
    return (
        'set calName to "{cal}"\n'
        'set eid to "{eid}"\n'
        'tell application "Calendar"\n'
        '    try\n'
        '        set targetCal to first calendar whose name is calName\n'
        '    on error\n'
        '        return "NO_CAL"\n'
        '    end try\n'
        '    repeat with e in events of targetCal\n'
        '        if uid of e is eid then\n'
        '            delete e\n'
        '            return "OK"\n'
        '        end if\n'
        '    end repeat\n'
        '    return "NOT_FOUND"\n'
        'end tell\n'
    ).format(cal=_asc_escape(CALENDAR_NAME), eid=_asc_escape(ev["ev_id"]))


def _state_str(p):
    """把 project 的 4 个状态（拍摄/交付/开票/结款）压成 4 位 0/1 串。"""
    st = status_of(p)
    return "".join("1" if st[k] else "0" for k in ("shoot", "deliver", "invoice", "settle"))


def _mac_state_str(rem):
    """从 Mac 提醒的子任务完成态，按 STEP_CN 顺序压成 4 位 0/1 串。"""
    subs = rem.get("subtasks") or {}
    return "".join("1" if subs.get(cn, False) else "0" for cn in STEP_CN)


def _apply_mac_state(p, mac):
    """把 Mac 提醒的勾选状态写回 project（反向同步）。
    注意：开票/结款在 Mac 只是勾选，没有真实单号/金额，故用占位值，
    用户在网页里可再更正真实单号与数额。"""
    today = datetime.date.today().isoformat()
    p["shootDone"] = today if mac[0] == "1" else ""
    p["deliverDone"] = today if mac[1] == "1" else ""
    inv = p.setdefault("invoice", {})
    if mac[2] == "1":
        if not str(inv.get("no") or "").strip():
            inv["no"] = today  # 占位：标记「需开票」
    else:
        inv["no"] = ""
    pa = p.setdefault("publicAccount", {})
    try:
        amt = float(pa.get("amount") or 0)
    except Exception:  # noqa: BLE001
        amt = 0
    pa["amount"] = (amt if amt > 0 else 1) if mac[3] == "1" else 0


def _git_push():
    """把更新后的 data.json 提交并推回 GitHub（remote URL 含 token，无需交互）。"""
    if DRY_RUN or not GIT_LOCAL:
        return False
    d = _expand(GIT_LOCAL)
    try:
        subprocess.run(["git", "-C", d, "pull", "--rebase", "--autostash", "--no-edit"],
                       capture_output=True, text=True, timeout=90)
        subprocess.run(["git", "-C", d, "add", "data.json"],
                       capture_output=True, text=True, timeout=30)
        r = subprocess.run(["git", "-C", d, "-c", "user.name=photoflow-bridge",
                            "-c", "user.email=bridge@local", "commit", "-m",
                            "sync: 提醒事项状态回写 %s" % datetime.date.today().isoformat()],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            print("[INFO] git commit 无新变更（%s）" % (r.stdout or r.stderr).strip()[:80])
            return True
        p = subprocess.run(["git", "-C", d, "push"],
                           capture_output=True, text=True, timeout=90)
        if p.returncode != 0:
            print("[WARN] git push 失败: %s" % (p.stderr or p.stdout).strip()[:200])
            return False
        print("[INFO] 已把提醒事项状态变更推送回 GitHub")
        return True
    except Exception as e:  # noqa: BLE001
        print("[WARN] git push 异常: %s" % e)
        return False


def run():
    summary = {
        "created_rem": 0, "created_ev": 0,
        "updated_rem": 0, "updated_ev": 0,
        "deleted_rem": 0, "deleted_ev": 0,
        "skipped_rem": 0, "skipped_ev": 0,
        "reverse": 0, "errors": [], "dryRun": DRY_RUN,
    }

    data, data_path = load_data()
    projects = data.get("projects", []) or []
    current_ids = set(p.get("id") for p in projects if p.get("id"))
    dirty = False          # mirror 状态是否需要回写
    reverse_dirty = False  # 反向同步（Mac→data.json）是否改动了数据
    reminders = _read_reminders()
    events = _read_calendar()

    # ---------------------------------------------------------------- #
    # 读取保护：检测是否因超时/失败导致空结果
    # ---------------------------------------------------------------- #
    rem_read_ok = len(reminders) > 0
    ev_read_ok = len(events) > 0

    # 判断此前是否已经有 mirror 记录（即不是首次运行）
    has_any_rem_mirror = any(
        p.get("mirror", {}).get("reminders", {}).get("matched")
        for p in projects
    )
    has_any_ev_mirror = any(
        p.get("mirror", {}).get("calendar", {}).get("exists")
        for p in projects
    )

    if not rem_read_ok and has_any_rem_mirror:
        print("[WARN] 提醒事项读取返回空(可能超时)，已有 %d 个项目有 mirror 记录，将跳过本轮提醒新建" %
              sum(1 for p in projects if p.get("mirror", {}).get("reminders", {}).get("matched")))
    if not ev_read_ok and has_any_ev_mirror:
        print("[WARN] 日历读取返回空(可能超时)，已有 %d 个项目有 mirror 记录，将跳过本轮日历新建" %
              sum(1 for p in projects if p.get("mirror", {}).get("calendar", {}).get("exists")))

    handled_rem = set()
    handled_ev = set()

    rem_by_marker = {}
    for r in reminders:
        mid = _parse_marker(r.get("notes"))
        if mid:
            rem_by_marker[mid] = r
    ev_by_marker = {}
    for e in events:
        mid = _parse_marker(e.get("notes"))
        if mid:
            ev_by_marker[mid] = e

    print("[INFO] 镜像模式（双向）：data.json 为基准，Mac 提醒/日历忠实镜像；"
          "在 Mac 提醒事项里勾选状态会反向写回 data.json 并推送 GitHub，两端 HTML 同步更新。")

    for p in projects:
        pid = p.get("id", "")

        # ===================== 提醒事项（双向） =====================
        # 优先按 id 标记精确匹配，再按日期/名称模糊匹配
        rem = rem_by_marker.get(pid) or _match_by_date(reminders, p) or _match_fuzzy(reminders, p)
        rem_exists = False
        if rem is not None:
            data_s = _state_str(p)
            mac_s = _mac_state_str(rem)
            mac_has = bool(rem.get("subtasks"))
            rmo = p.setdefault("mirror", {}).setdefault("reminders", {})
            last = rmo.get("last", "")
            if mac_s != data_s and mac_has:
                if data_s == last:
                    # 仅 Mac 改了 → 反向：写回 data.json
                    _apply_mac_state(p, mac_s)
                    reverse_dirty = True
                    summary["reverse"] += 1
                    rmo["last"] = mac_s
                elif mac_s == last:
                    # 仅 data.json 改了（网页端）→ 正向：状态写到 Mac
                    if run_script(_build_annotate_reminder(rem, p), "更新提醒状态: " + title_of(p)):
                        summary["updated_rem"] += 1
                    else:
                        summary["errors"].append("更新提醒状态失败: " + title_of(p) + _err_suffix())
                    rmo["last"] = data_s
                else:
                    # 两边都改（冲突）→ Mac 优先
                    _apply_mac_state(p, mac_s)
                    reverse_dirty = True
                    summary["reverse"] += 1
                    rmo["last"] = mac_s
            else:
                # 已同步，或 Mac 子任务未知 → 归一到 data 状态（正向）
                if mac_s != data_s and not mac_has:
                    if run_script(_build_annotate_reminder(rem, p), "更新提醒状态: " + title_of(p)):
                        summary["updated_rem"] += 1
                    else:
                        summary["errors"].append("更新提醒状态失败: " + title_of(p) + _err_suffix())
                rmo["last"] = data_s
            handled_rem.add(rem["rem_id"])
            rem_exists = True
        else:
            # ---- 读取保护：如果提醒读取失败(空)且已有 mirror，跳过新建 ----
            if not rem_read_ok and has_any_rem_mirror:
                print("[WARN] 提醒读取返回空，跳过新建: %s" % title_of(p))
                summary["skipped_rem"] += 1
            else:
                stale = rem_by_marker.get(pid)
                if stale is not None and stale["rem_id"] not in handled_rem:
                    if run_script(_build_delete_reminder(stale), "删除过期提醒: " + title_of(p)):
                        summary["deleted_rem"] += 1
                    else:
                        summary["errors"].append("删除过期提醒失败: " + title_of(p) + _err_suffix())
                    handled_rem.add(stale["rem_id"])
                if run_script(_build_create_reminder(p), "新建提醒: " + title_of(p)):
                    summary["created_rem"] += 1
                    rem_exists = True
                    p.setdefault("mirror", {}).setdefault("reminders", {})["last"] = _state_str(p)
                else:
                    summary["errors"].append("新建提醒失败: " + title_of(p) + _err_suffix())

        # ===================== 日历事件（仅正向镜像） =====================
        # 优先按 id:p-<id> 标记精确匹配（1:1），避免模糊匹配把多个 project 错误命中同一 event。
        ev = ev_by_marker.get(pid) or _match_by_date(events, p) or _match_fuzzy(events, p)
        ev_exists = False
        if ev is not None:
            if run_script(_build_annotate_event(ev, p), "更新日历: " + title_of(p)):
                summary["updated_ev"] += 1
                handled_ev.add(ev["ev_id"])
                ev_exists = True
            else:
                summary["errors"].append("更新日历失败: " + title_of(p) + _err_suffix())
        else:
            # ---- 读取保护：如果日历读取失败(空)且已有 mirror，跳过新建 ----
            if not ev_read_ok and has_any_ev_mirror:
                print("[WARN] 日历读取返回空，跳过新建: %s" % title_of(p))
                summary["skipped_ev"] += 1
            else:
                stale = ev_by_marker.get(pid)
                if stale is not None and stale["ev_id"] not in handled_ev:
                    if run_script(_build_delete_event(stale), "删除过期日历: " + title_of(p)):
                        summary["deleted_ev"] += 1
                    else:
                        summary["errors"].append("删除过期日历失败: " + title_of(p) + _err_suffix())
                    handled_ev.add(stale["ev_id"])
                if run_script(_build_create_event(p), "新建日历: " + title_of(p)):
                    summary["created_ev"] += 1
                    ev_exists = True
                else:
                    summary["errors"].append("新建日历失败: " + title_of(p) + _err_suffix())

        # ---------- 镜像状态回写（供档期总表展示）----------
        mirror = p.setdefault("mirror", {})
        cal = mirror.setdefault("calendar", {})
        if cal.get("exists") != ev_exists:
            cal["exists"] = ev_exists
            dirty = True
        rem_obj = mirror.setdefault("reminders", {})
        if rem_obj.get("matched") != rem_exists:
            rem_obj["matched"] = rem_exists
            dirty = True

    # ---------- 清理：data.json 已删除、但 Mac 仍留有带 id: 标记的条目 ----------
    for r in reminders:
        if r["rem_id"] in handled_rem:
            continue
        mid = _parse_marker(r.get("notes"))
        if mid and mid not in current_ids:
            if run_script(_build_delete_reminder(r), "清理提醒: " + (r.get("name") or mid)):
                summary["deleted_rem"] += 1
            else:
                summary["errors"].append("清理提醒失败: " + (r.get("name") or mid) + _err_suffix())
    for e in events:
        if e["ev_id"] in handled_ev:
            continue
        mid = _parse_marker(e.get("notes"))
        if mid and mid not in current_ids:
            if run_script(_build_delete_event(e), "清理日历: " + (e.get("summary") or mid)):
                summary["deleted_ev"] += 1
            else:
                summary["errors"].append("清理日历失败: " + (e.get("summary") or mid) + _err_suffix())

    # ---------- 回写 data.json ----------
    if (dirty or reverse_dirty) and data_path and not DRY_RUN:
        if _write_data(data_path, data):
            print("[INFO] 已回写状态到 %s" % data_path)
        else:
            summary["errors"].append("回写 data.json 失败: " + (data_path or "?"))
    elif (dirty or reverse_dirty) and DRY_RUN:
        print("[DRY-RUN] 将回写状态变更（mirror %s / 反向 %d 条）" %
              ("是" if dirty else "否", summary["reverse"]))

    # 反向同步改动需推送回 GitHub，使两端 HTML 都更新
    if reverse_dirty and not DRY_RUN:
        _git_push()

    prefix = "[DRY-RUN] " if DRY_RUN else ""
    print("%s[对账结果] 新建提醒:%d 更新:%d 删除:%d 跳过:%d | 新建日历:%d 更新:%d 删除:%d 跳过:%d | 反向:%d | 失败:%d"
          % (prefix, summary["created_rem"], summary["updated_rem"], summary["deleted_rem"],
             summary["skipped_rem"],
             summary["created_ev"], summary["updated_ev"], summary["deleted_ev"],
             summary["skipped_ev"],
             summary["reverse"], len(summary["errors"])))
    if summary["errors"]:
        print("%s[对账警告] %d 处失败：" % (prefix, len(summary["errors"])))
        for e in summary["errors"]:
            print("   - " + e)
    return summary


def main():
    # 检查 --deduplicate 命令行参数
    if "--deduplicate" in sys.argv:
        _deduplicate()
        sys.exit(0)

    try:
        summary = run()
    except Exception as e:  # noqa: BLE001
        print("[ERROR] 对账异常: %s" % e)
        summary = {"errors": [str(e)]}
    try:
        if RECONCILE_LOG:
            with open(os.path.expanduser(RECONCILE_LOG), "a", encoding="utf-8") as f:
                f.write(datetime.datetime.now().isoformat() + " " +
                        json.dumps(summary, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print("[WARN] 写日志失败: %s" % e)
    # 始终退出 0，便于 LaunchAgent 持续运行
    sys.exit(0)


if __name__ == "__main__":
    main()
