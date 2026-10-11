#!/usr/bin/env bash
# 把当前二维码发布到仓库的 m7a-qr 分支，供用户用固定 URL 实时获取。
#
# 为什么不用 artifact：artifact 要打包成 zip、下载慢，而二维码每 2 分钟就刷新一次，
# 等下载完往往已经作废。这里改成把最新链接写进一个文件，
# 用户打开固定 URL 刷新即可拿到当前有效的码。
#
# 用法: publish_qr.sh <二维码链接> [<二维码图片路径>]
set -uo pipefail

LINK="${1:-}"
IMG="${2:-}"
[ -z "$LINK" ] && { echo "用法: publish_qr.sh <链接> [图片路径]"; exit 2; }

REPO_DIR="$(pwd)"
BRANCH="${QR_BRANCH:-m7a-qr}"
WORK="/tmp/qr-publish"

mkdir -p "$WORK"
cd "$WORK"

if [ ! -d .git ]; then
  git init -q
  git remote add origin "$(git -C "$REPO_DIR" config --get remote.origin.url)"
fi
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

# 远端分支可能还不存在（首次运行），失败属正常
git fetch -q origin "$BRANCH" 2>/dev/null || true
if git show-ref -q "refs/remotes/origin/$BRANCH"; then
  git checkout -q -B "$BRANCH" "origin/$BRANCH" 2>/dev/null \
    || git checkout -q --orphan "$BRANCH" 2>/dev/null || true
else
  git checkout -q --orphan "$BRANCH" 2>/dev/null || git checkout -q -B "$BRANCH" 2>/dev/null || true
fi

{
  echo "$LINK"
} > qrcode.txt

if [ -n "$IMG" ] && [ -f "$IMG" ]; then
  cp "$IMG" qrcode.png
fi

date -u +%FT%TZ > updated_at.txt

git add -A
if git diff --cached --quiet 2>/dev/null; then
  echo "内容无变化，跳过推送"
  exit 0
fi
git commit -q -m "qr update $(date -u +%FT%TZ)"
git push -f -q origin "$BRANCH" 2>&1 | tail -n 3
echo "已发布到 $BRANCH 分支"
