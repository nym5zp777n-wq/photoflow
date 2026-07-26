#!/bin/bash
# 档期守护进程 · 一键启动
# 你只需要在系统设置里把 python3.9 拖进「完全磁盘访问权限」并打开开关，
# 剩下的（bootstrap、校验状态）脚本全自动。

set -e

PLIST="$HOME/Library/LaunchAgents/com.studio.schedule-reconcile.plist"
PY="/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9"

clear
echo "═══════════════════════════════════════════"
echo "  档期守护进程 · 一键启动"
echo "═══════════════════════════════════════════"
echo

# 1. 检查文件
echo "▶ 第0步：检查就绪..."
if [ ! -f "$PY" ]; then
  echo "  ✗ 找不到 Python: $PY"
  echo "    先装命令行工具：xcode-select --install"
  read -p "  按回车退出..."; exit 1
fi
echo "  ✓ Python 已就位: $PY"

if [ ! -f "$PLIST" ]; then
  echo "  ✗ 找不到 plist: $PLIST"
  read -p "  按回车退出..."; exit 1
fi
echo "  ✓ plist 已就位: $PLIST"
echo

# 2. 打开系统设置到 FDA 页 + Finder 指向二进制
echo "▶ 第1步：打开「系统设置 → 隐私与安全性 → 完全磁盘访问权限」..."
open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || open -a "System Settings"
sleep 1
echo "  → 已尝试打开「完全磁盘访问权限」面板"
echo

# Finder 高亮 python3.9 ，方便直接拖到 FDA 列表
echo "▶ 第1.5步：在 Finder 里高亮 python3.9（待会儿拖它到列表里）..."
open -R "$PY"
echo "  → Finder 已弹出，python3.9 文件已高亮"
echo

echo "───────────────────────────────────────────"
echo "  现在去「系统设置」窗口做这两件事："
echo "    ① 点左下角 🔓 解锁（如需输密码就输）"
echo "    ② 点 + 后：把 Finder 里那个高亮的 python3.9"
echo "       （或按 Cmd+Shift+G 粘贴下面这行路径）"
echo "       拖进右侧列表，再把它的开关打开"
echo "       路径："
echo "       $PY"
echo "───────────────────────────────────────────"
read -p "  做完按回车继续（取消 Ctrl+C）..." _
echo

# 3. Bootstrap
echo "▶ 第2步：注册守护进程..."
launchctl bootout gui/501/com.studio.schedule-reconcile 2>/dev/null || true
BOOTSTRAP_OUT=$(launchctl bootstrap gui/501 "$PLIST" 2>&1 || true)
# 过滤掉 macOS Sonoma 那个误导性的 I/O error
if echo "$BOOTSTRAP_OUT" | grep -q "Input/output error"; then
  echo "  ✓ 已 bootstrap（macOS 报了假错 I/O error，无视就好）"
else
  echo "  ✓ bootstrap 返回: ${BOOTSTRAP_OUT:-无输出（正常）}"
fi
echo

# 4. 校验
echo "▶ 第3步：校验运行状态..."
sleep 2
STATE_LINE=$(launchctl print gui/501/com.studio.schedule-reconcile 2>/dev/null | grep "^state" | head -1)
if echo "$STATE_LINE" | grep -q "running"; then
  echo "  ✓ $STATE_LINE"
  echo
  echo "═══════════════════════════════════════════"
  echo "  🎉 守护进程已启动！"
  echo "  等 5 分钟左右，打开："
  echo "    · 「提醒事项」→ 清单「档期管理」"
  echo "    · 「日历」→ 日历「摄影档期」"
  echo "  你的档期会自动镜像过来。"
  echo "═══════════════════════════════════════════"
else
  echo "  ⚠️  当前状态: ${STATE_LINE:-未能读取}"
  echo
  echo "  可能 FDA 还没勾对。排查："
  echo "    1) 系统设置 → 完全磁盘访问权限 → 确认 python3.9 在列表里且开关开着"
  echo "    2) 看日志：cat /tmp/com.studio.schedule-reconcile.log"
  echo "    3) 双击本脚本重试"
fi
echo
read -p "按回车关闭..." _
