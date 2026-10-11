"""判定每日任务是否需要补跑。

读取 /tmp/runs.json（GitHub API 返回的 93 号 workflow 运行列表），
在标准输出打印分析过程，并把结论作为最后一行输出：
  in_progress   - 有运行正在进行，不要干预
  ok            - 最近一次已成功
  needs_catchup - 最近一次失败
  missing       - 窗口内没有运行记录（cron 可能未触发）
"""
import datetime
import json
import sys

WINDOW_HOURS = 20
RUNS_FILE = "/tmp/runs.json"

ACTIVE_STATUSES = ("in_progress", "queued", "requested", "waiting", "pending")


def parse_ts(value):
    return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def list_runs(entries):
    print(f"窗口 = 最近 {WINDOW_HOURS} 小时")
    print(f"窗口内运行次数 = {len(entries)}")
    for created, r in sorted(entries, key=lambda x: x[0], reverse=True):
        print(f"  {created:%Y-%m-%d %H:%M}Z  status={r['status']:<12} "
              f"conclusion={r['conclusion']}")


def main():
    list_only = "--list-only" in sys.argv

    try:
        runs = json.load(open(RUNS_FILE, encoding="utf-8")).get("workflow_runs", [])
    except Exception as e:
        print(f"读取 {RUNS_FILE} 失败: {e}", file=sys.stderr)
        print("unknown")
        return 1

    now = datetime.datetime.now(datetime.timezone.utc)
    window = datetime.timedelta(hours=WINDOW_HOURS)

    entries = []
    for r in runs:
        try:
            created = parse_ts(r["created_at"])
        except Exception:
            continue
        if now - created <= window:
            entries.append((created, r))

    list_runs(entries)
    if list_only:
        return 0

    if any(r.get("status") in ACTIVE_STATUSES for _, r in entries):
        print("判定：有运行正在进行，不干预")
        print("in_progress")
        return 0

    if not entries:
        print("判定：窗口内无运行记录")
        print("missing")
        return 0

    entries.sort(key=lambda x: x[0], reverse=True)
    latest = entries[0][1]
    if latest.get("conclusion") == "success":
        print(f"判定：最近一次成功（{latest['created_at']}）")
        print("ok")
    else:
        print(f"判定：最近一次未成功（conclusion={latest.get('conclusion')}）")
        print("needs_catchup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
