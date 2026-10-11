"""sing-box TUN 模式代理：接管全部 TCP/UDP 流量。

为什么不用 SOCKS5：
  Cloudflare/CDN 后面的 WebSocket 升级在部分 SOCKS5 实现下会失败，
  而云游戏的节点测速正是用 wss:// 做的。TUN 模式在网络层接管流量，
  不依赖 SOCKS5 的 HTTP 升级语义，且能一并处理 UDP（WebRTC 需要）。

本脚本：
  1. 下载并安装 sing-box
  2. 用 VLESS 链接生成 TUN 入站 + 代理出站配置
  3. 启动（需要 /dev/net/tun 与 NET_ADMIN 权限）
  4. 校验出口 IP

用法：python setup_tun.py [--keep-alive]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from urllib.parse import parse_qs, unquote, urlparse

SINGBOX_VER = "1.10.7"
WORK = "/tmp/singbox"


def log(m):
    print(m, flush=True)


def parse_vless_outbound(link: str) -> dict:
    """把 vless 链接转成 sing-box 的 outbound。支持 reality 与 ws+tls。"""
    if not link.startswith("vless://"):
        raise ValueError("只支持 vless://")
    u = urlparse(link)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    security = q.get("security", "none")
    net = q.get("type", "tcp")

    tls = {"enabled": True, "server_name": q.get("sni", u.hostname),
           "utls": {"enabled": True, "fingerprint": q.get("fp", "chrome")}}
    if security == "reality":
        tls = {
            "enabled": True,
            "server_name": q.get("sni", ""),
            "utls": {"enabled": True, "fingerprint": q.get("fp", "chrome")},
            "reality": {"enabled": True, "public_key": q.get("pbk", ""),
                        "short_id": q.get("sid", "")},
        }
    elif security in ("none", ""):
        tls = {"enabled": False}

    transport = {"type": net}
    if net == "ws":
        transport = {"type": "ws", "path": q.get("path", "/")}
        if q.get("host"):
            transport["headers"] = {"Host": q["host"]}
    elif net == "grpc":
        transport = {"type": "grpc", "service_name": q.get("serviceName", "")}

    out = {
        "type": "vless",
        "tag": "proxy",
        "server": u.hostname,
        "server_port": int(u.port),
        "uuid": u.username,
        "packet_encoding": "xudp",
        "tls": tls,
        "transport": transport,
    }
    if q.get("flow") and security == "reality":
        out["flow"] = q["flow"]
    return out


def build_config(outbound: dict) -> dict:
    """TUN 入站 + auto_route，全部流量走代理。"""
    return {
        "log": {"level": "warn", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "proxy-dns", "address": "https://1.1.1.1/dns-query",
                 "detour": "proxy"},
                {"tag": "local-dns", "address": "local", "detour": "direct"},
            ],
            "rules": [{"outbound": "any", "server": "local-dns"}],
            "final": "proxy-dns",
            "strategy": "prefer_ipv4",
        },
        "inbounds": [{
            "type": "tun",
            "tag": "tun-in",
            "interface_name": "singtun0",
            "address": ["172.19.0.1/30"],
            "mtu": 9000,
            "auto_route": True,
            "strict_route": False,
            "stack": "system",
            "sniff": True,
            "sniff_override_destination": False,
        }],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "outbound": "direct"},
            ],
            "final": "proxy",
            "auto_detect_interface": True,
        },
    }


def install_singbox():
    ver = SINGBOX_VER
    url = (f"https://github.com/SagerNet/sing-box/releases/download/"
           f"v{ver}/sing-box-{ver}-linux-amd64.tar.gz")
    log(f"下载 sing-box {ver} ...")
    subprocess.run(["curl", "-sSL", "-o", "/tmp/sb.tar.gz", url], check=True)
    sz = os.path.getsize("/tmp/sb.tar.gz")
    log(f"下载 {sz} 字节")
    if sz < 1_000_000:
        raise RuntimeError("下载文件过小")
    os.makedirs(WORK, exist_ok=True)
    subprocess.run(["tar", "-xzf", "/tmp/sb.tar.gz", "-C", WORK,
                    "--strip-components=1"], check=True)
    subprocess.run(["chmod", "+x", f"{WORK}/sing-box"], check=True)
    out = subprocess.run([f"{WORK}/sing-box", "version"],
                         capture_output=True, text=True).stdout
    log("版本: " + out.strip().split("\n")[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--link", default=os.environ.get("M7A_PROXY_LINK", ""))
    ap.add_argument("--keep-alive", action="store_true")
    args = ap.parse_args()

    if not args.link:
        log("未提供链接")
        return 2

    log("=== 环境检查 ===")
    log(f"  /dev/net/tun 存在: {os.path.exists('/dev/net/tun')}")
    caps = subprocess.run(["sh", "-c", "grep CapEff /proc/self/status"],
                          capture_output=True, text=True).stdout.strip()
    log(f"  {caps}")

    outbound = parse_vless_outbound(args.link)
    log(f"出站: {outbound['server']}:{outbound['server_port']} "
        f"transport={outbound['transport']['type']}")

    cfg = build_config(outbound)
    os.makedirs(WORK, exist_ok=True)
    with open(f"{WORK}/config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    log(f"配置已写入 {WORK}/config.json")

    install_singbox()

    log("=== 启动 sing-box（TUN 模式）===")
    proc = subprocess.Popen([f"{WORK}/sing-box", "run", "-c", f"{WORK}/config.json"],
                            stdout=open(f"{WORK}/sb.log", "w"),
                            stderr=subprocess.STDOUT)
    with open(f"{WORK}/pid", "w") as f:
        f.write(str(proc.pid))
    time.sleep(8)

    if proc.poll() is not None:
        log("sing-box 已退出，日志：")
        log(open(f"{WORK}/sb.log", encoding="utf-8", errors="replace").read()[-3000:])
        return 1

    log(f"sing-box pid={proc.pid}，运行中")
    log("=== 校验出口 ===")
    for url in ("https://api.ipify.org", "https://ipinfo.io/json"):
        try:
            r = subprocess.run(["curl", "-sS", "--max-time", "25", url],
                               capture_output=True, text=True)
            log(f"  {url}: {r.stdout.strip()[:250]}")
        except Exception as e:
            log(f"  {url} 失败: {e}")

    if args.keep_alive:
        log("保持运行中（后续步骤可直接使用，无需配置代理）")
    else:
        proc.terminate()
        log("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
