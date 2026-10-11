"""在 runner 上装 xray 并测试 VLESS 代理出口。

用途：判断「换代理」能否解决云游戏进不去的问题。
关键看两件事：
  1. 代理能否连通、出口 IP 是什么国家
  2. 该出口 IP 访问米哈游云游戏相关端点是否正常

用法（在 workflow 里）：
  python3 setup_proxy.py --link "vless://..." --out-port 10809
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, unquote, urlparse


def log(msg):
    print(msg, flush=True)


def parse_vless(link: str) -> dict:
    """把 vless:// 链接解析成 xray outbound 配置。"""
    if not link.startswith("vless://"):
        raise ValueError("只支持 vless:// 链接")

    u = urlparse(link)
    uuid = u.username
    host = u.hostname
    port = u.port
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    tag = unquote(u.fragment) if u.fragment else "proxy"

    log(f"解析: uuid={uuid[:8]}... host={host} port={port} tag={tag}")
    log(f"参数: {json.dumps(q, ensure_ascii=False)}")

    if q.get("security") != "reality":
        raise ValueError(f"本脚本只处理 reality，实际 security={q.get('security')}")
    if not q.get("pbk"):
        raise ValueError("reality 缺少 pbk（公钥）")

    stream = {
        "network": q.get("type", "tcp"),
        "security": "reality",
        "realitySettings": {
            "serverName": q.get("sni", ""),
            "fingerprint": q.get("fp", "chrome"),
            "publicKey": q["pbk"],
            "shortId": q.get("sid", ""),
            "spiderX": q.get("spx", ""),
        },
    }
    if q.get("flow"):
        user_flow = q["flow"]
    else:
        user_flow = ""

    outbound = {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": host,
                "port": int(port),
                "users": [{
                    "id": uuid,
                    "encryption": q.get("encryption", "none"),
                    "flow": user_flow,
                }],
            }]
        },
        "streamSettings": stream,
    }
    return outbound


def build_config(outbound: dict, socks_port: int, http_port: int) -> dict:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks",
                "port": socks_port,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"udp": True, "auth": "noauth"},
            },
            {
                "tag": "http",
                "port": http_port,
                "listen": "127.0.0.1",
                "protocol": "http",
            },
        ],
        "outbounds": [
            outbound,
            {"tag": "direct", "protocol": "freedom"},
        ],
    }


def install_xray(version="1.8.24"):
    """下载 xray-core。"""
    url = (f"https://github.com/XTLS/Xray-core/releases/download/"
           f"v{version}/Xray-linux-64.zip")
    log(f"下载 xray-core v{version} ...")
    subprocess.run(["curl", "-sSL", "-o", "/tmp/xray.zip", url], check=True)
    sz = os.path.getsize("/tmp/xray.zip")
    log(f"下载完成 {sz} 字节")
    if sz < 1_000_000:
        raise RuntimeError("下载文件过小，可能失败")

    os.makedirs("/tmp/xray", exist_ok=True)
    # 优先用 unzip；runner 上可能没有，则退回 Python 的 zipfile
    if subprocess.run(["sh", "-c", "command -v unzip"],
                      capture_output=True).returncode == 0:
        subprocess.run(["unzip", "-o", "-q", "/tmp/xray.zip", "-d", "/tmp/xray"],
                       check=True)
    else:
        log("未找到 unzip，改用 Python zipfile 解压")
        import zipfile
        with zipfile.ZipFile("/tmp/xray.zip") as z:
            z.extractall("/tmp/xray")

    subprocess.run(["chmod", "+x", "/tmp/xray/xray"], check=True)
    out = subprocess.run(["/tmp/xray/xray", "version"],
                         capture_output=True, text=True).stdout
    log("xray 版本: " + out.strip().split("\n")[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--link", default=os.environ.get("M7A_PROXY_LINK", ""))
    ap.add_argument("--socks-port", type=int, default=10809)
    ap.add_argument("--http-port", type=int, default=10810)
    ap.add_argument("--keep-alive", action="store_true",
                    help="保持代理进程运行，供后续步骤（含容器）使用")
    args = ap.parse_args()

    if not args.link:
        log("错误: 未提供 vless 链接（--link 或环境变量 M7A_PROXY_LINK）")
        return 2

    try:
        outbound = parse_vless(args.link)
    except Exception as e:
        log(f"解析失败: {e}")
        return 2

    cfg = build_config(outbound, args.socks_port, args.http_port)
    # 目录必须先建好 —— 之前写在 install_xray() 里，导致这里写配置就报
    # FileNotFoundError（实测踩过）。
    os.makedirs("/tmp/xray", exist_ok=True)
    with open("/tmp/xray/config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    log("配置已写入 /tmp/xray/config.json")

    install_xray()

    log("启动 xray ...")
    proc = subprocess.Popen(
        ["/tmp/xray/xray", "run", "-c", "/tmp/xray/config.json"],
        stdout=open("/tmp/xray/xray.log", "w"),
        stderr=subprocess.STDOUT)
    log(f"xray pid={proc.pid}")
    with open("/tmp/xray/pid", "w") as f:
        f.write(str(proc.pid))
    time.sleep(5)

    if proc.poll() is not None:
        log("xray 已退出，日志：")
        log(open("/tmp/xray/xray.log", encoding="utf-8", errors="replace").read()[-3000:])
        return 1

    # 用代理查出口 IP
    import requests
    proxies = {"http": f"http://127.0.0.1:{args.http_port}",
               "https": f"http://127.0.0.1:{args.http_port}"}
    ok = False
    for url, label in [("https://api.ipify.org", "出口 IP"),
                       ("https://ipinfo.io/json", "归属信息")]:
        for attempt in (1, 2, 3):
            try:
                r = requests.get(url, proxies=proxies, timeout=25)
                log(f"{label}: {r.text.strip()[:300]}")
                ok = True
                break
            except Exception as e:
                log(f"{label} 第 {attempt} 次失败: {type(e).__name__} {str(e)[:150]}")
                time.sleep(3)

    try:
        r = requests.get("https://api.ipify.org", timeout=20)
        log(f"直连出口 IP: {r.text.strip()}")
    except Exception as e:
        log(f"直连查询失败: {e}")

    if not ok:
        log("代理无法访问外网，视为失败")
        proc.terminate()
        return 1

    # 保持常驻：后续步骤（含容器）都要用这个代理
    # --keep-alive 时把进程留下，由 workflow 的清理步骤负责结束
    if "--keep-alive" in sys.argv:
        log("代理保持运行中（供后续步骤使用）")
    else:
        proc.terminate()
        log("xray 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
