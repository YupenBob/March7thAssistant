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
  # 注意：不能直接沿用 workspace 里的 origin —— actions/checkout 把凭据
  # 放在那里，但我们在独立目录，取不到；直接匿名推送会报
  # "could not read Username for 'https://github.com'"。
  # 用 GITHUB_TOKEN 自己拼一个带凭据的 URL。
  if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
    REMOTE_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
  else
    REMOTE_URL="$(git -C "$REPO_DIR" config --get remote.origin.url)"
    echo "::warning::GITHUB_TOKEN 不可用，将使用工作区 remote（可能无凭据）"
  fi
  git remote add origin "$REMOTE_URL"
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

# 注意：不要把 push 接到管道里。管道会让 $? 变成 tail 的退出码，
# 从而把失败当成成功上报（这个坑已经踩过一次）。
set +e
PUSH_OUT="$(git push -f origin "$BRANCH" 2>&1)"
PUSH_RC=$?
set -e
if [ "$PUSH_RC" -ne 0 ]; then
  echo "推送失败 (exit=$PUSH_RC):"
  echo "$PUSH_OUT" | sed 's/^/    /'
  exit "$PUSH_RC"
fi
echo "$PUSH_OUT" | tail -n 2
echo "已发布到 $BRANCH 分支"
