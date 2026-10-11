"""探测云游戏调度后端的可达性，判断"网络错误"是偶发还是地域限制。

背景：登录态在 Azure 上有效（能读到通行证 ID 与剩余时长），
但点击"进入游戏"后页面弹出「网络错误 / 当前网络异常,请稍后重试」。
该请求由页面内的 XHR 发起，应用代码不直接调用，所以需要独立探测。

云游戏前端 bundle 里出现的接口路径（来自 web.4f0790db.js）：
  /gamer/api/login
  /dispatcher/api/getNodesInfo
  /dispatcher/api/paasDispatch
  /dispatcher/api/statusCheck
  /dispatcher/api/listPingServer
  /wallet/wallet/get
"""
import json
import sys
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

BASES = [
    "https://api-cloudgame.mihoyo.com",
    "https://cg-hkrpg-api.mihoyo.com",
]

PATHS = [
    "/gamer/api/login",
    "/dispatcher/api/getNodesInfo",
    "/dispatcher/api/statusCheck",
    "/wallet/wallet/get",
]

H = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://sr.mihoyo.com",
    "Referer": "https://sr.mihoyo.com/",
    "x-rpc-client_type": "4",
    "x-rpc-app_id": "ddxf5dufpuy2",
}

print(f"探测起点（本机出口）: ", end="")
try:
    r = requests.get("https://api.ipify.org", timeout=15)
    print(r.text.strip())
except Exception as e:
    print(f"<{e}>")
print()

for base in BASES:
    print("=" * 70)
    print(f"BASE {base}")
    print("=" * 70)
    for path in PATHS:
        url = base + path
        for method in ("POST", "GET"):
            try:
                t0 = time.time()
                if method == "POST":
                    r = requests.post(url, headers=H, json={}, timeout=25)
                else:
                    r = requests.get(url, headers=H, timeout=25)
                dt = time.time() - t0
                body = r.text.strip().replace("\n", " ")[:200]
                print(f"[{r.status_code}] {method:4} {path}  ({dt:.2f}s)")
                print(f"        {body}")
            except Exception as e:
                print(f"[ERR] {method:4} {path}  {type(e).__name__}: {str(e)[:120]}")
        print()
