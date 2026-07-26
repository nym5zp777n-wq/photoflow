#!/bin/bash
# cleanup.command — 一键清理测试时误写入的 Mac 档期
# v2 升级：按 data.json 58 条项目做模糊匹配（不依赖 id:p- 标记）
# 安全流程：先 DRY_RUN 预览 → 询问 y/n → 真删

set -e

# 终端.app 必须在 GUI 会话里跑 osascript（不然 TCC 弹窗出不来）
PY="/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9"
SCRIPT="/Users/l/Documents/workbuddy/2026-07-23-11-08-43/mac-bridge/cleanup-test-entries.py"

# 校验环境
if [ ! -x "$PY" ]; then
  echo "[错误] 找不到 Python 3.9: $PY"
  echo "请确认 Xcode Command Line Tools 已安装：xcode-select --install"
  exit 1
fi
if [ ! -f "$SCRIPT" ]; then
  echo "[错误] 找不到脚本: $SCRIPT"
  exit 1
fi

clear
echo "================================================================"
echo "  档期管理 · 清理测试残留"
echo "  按 data.json 58 条项目模糊匹配（不依赖 id:p- 标记）"
echo "================================================================"
echo ""
echo " 流程：① 预览（列出将删除哪些）  ② 询问 y/n  ③ 真删"
echo ""
echo "⚠️  首次运行会弹授权窗（终端→提醒事项、日历），点「好」即可。"
echo ""
echo "按 回车 开始预览..."
read -r

# 第 1 步：DRY_RUN 预览
echo ""
echo "================ 预览阶段（不真删）================"
echo ""
DRY_RUN=1 "$PY" "$SCRIPT"

# DRY_RUN 脚本已经内部询问过 y/n 了（如果有需要删的条目）
# 所以这里不需要再问
echo ""
echo "================================================================"
echo "  完成。按 回车 关闭窗口。"
echo "================================================================"
read -r
