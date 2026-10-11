"""筛选代理节点：测它们到云游戏测速节点的 RTT，判断能否满足 SDK 要求。

背景（已实测确认）：
  cg-sdk 用 WebSocketRttTester.testRttMs(20, 1000) 测速 ——
  20 次往返必须在 1000ms 内完成，否则该节点 rtt = -1；
  页面过滤 rtt < 0，全为 -1 时 ping_results 为空；
  getNodesInfo 带空结果 -> 服务端 -110005「游戏服务器连接中断」。

实测对比：
  用户本机   rtt 23~98ms   20 次 679~2251ms   -> 1/5 达标
  GHA runner rtt 241~387ms 20 次 5901~10700ms -> 0/5 达标

结论：能否成功取决于「到国内测速节点的 RTT」。
本脚本测两条路径：
  A. 直连（作为基线）
  B. 经指定代理（socks5 / http）
并给出是否可能达标的判定。

用法：
  # 只测直连
  python check_proxy_latency.py

  # 测一个代理
  python check_proxy_latency.py --proxy socks5://127.0.0.1:1080

  # 批量测多个候选（每行一个，格式 scheme://host:port）
  python check_proxy_latency.py --list proxies.txt
"""
import argparse
import os
import socket
import ssl
import struct
import sys
import time

HOSTS = [
    "yys-t-enszjk6-wssping.mhystatic.com",
    "cg-t-mhyfs1-wssping.mhystatic.com",
    "cg-t-qscs1-wssping.mhystatic.com",
    "cg-t-qshf1-wssping.mhystatic.com",
    "cg-t-ensgy15-wssping.mhystatic.com",
]
PATH = "/wsspingsvr/echo"
COUNT = 20
LIMIT_MS = 1000

# 单次往返的上限：20 次总耗时必须 < 1000ms，
# 因此单次必须远低于 50ms（还要算建连与抖动）。
PER_PING_BUDGET_MS = LIMIT_MS / COUNT


def log(m):
    print(m, flush=True)


# ---------------------------------------------------------------- 代理连接
def socks5_connect(proxy_host, proxy_port, dst_host, dst_port, timeout=15):
    """经 SOCKS5 建立到目标的 TCP 连接。"""
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    s.settimeout(timeout)
    # 握手：无认证
    s.sendall(b"\x05\x01\x00")
    resp = s.recv(2)
    if len(resp) < 2 or resp[0] != 5 or resp[1] != 0:
        raise RuntimeError(f"SOCKS5 握手失败: {resp!r}")
    # 用域名方式请求连接（让代理解析 DNS）
    host_b = dst_host.encode()
    req = b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + \
        struct.pack(">H", dst_port)
    s.sendall(req)
    r = s.recv(4)
    if len(r) < 4 or r[1] != 0:
        raise RuntimeError(f"SOCKS5 连接被拒: code={r[1] if len(r) > 1 else '?'}")
    atyp = r[3]
    if atyp == 1:
        s.recv(4)
    elif atyp == 3:
        n = s.recv(1)[0]
        s.recv(n)
    elif atyp == 4:
        s.recv(16)
    s.recv(2)
    return s


def http_connect(proxy_host, proxy_port, dst_host, dst_port, timeout=15):
    """经 HTTP CONNECT 建立隧道。"""
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    s.settimeout(timeout)
    req = (f"CONNECT {dst_host}:{dst_port} HTTP/1.1\r\n"
           f"Host: {dst_host}:{dst_port}\r\n\r\n")
    s.sendall(req.encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("CONNECT 无响应")
        data += chunk
    if b"200" not in data.split(b"\r\n")[0]:
        raise RuntimeError(f"CONNECT 失败: {data.split(chr(13).encode())[0]!r}")
    return s


def open_target(host, port, proxy=None, timeout=15):
    if not proxy:
        return socket.create_connection((host, port), timeout=timeout)
    scheme, rest = proxy.split("://", 1)
    ph, _, pp = rest.rpartition(":")
    ph = ph.strip("[]")
    pp = int(pp)
    if scheme.startswith("socks"):
        return socks5_connect(ph, pp, host, port, timeout)
    if scheme.startswith("http"):
        return http_connect(ph, pp, host, port, timeout)
    raise ValueError(f"不支持的代理协议: {scheme}")


# ---------------------------------------------------------------- WS 测速
def ws_handshake(sock, host, timeout=15):
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    req = (
        f"GET {PATH} HTTP/1.1\r\nHost: {host}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        "Origin: https://sr.mihoyo.com\r\n"
        "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36\r\n\r\n"
    )
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(sock, server_hostname=host)
    tls.sendall(req.encode())
    tls.settimeout(timeout)
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = tls.recv(4096)
        if not chunk:
            break
        resp += chunk
    if b"101" not in resp.split(b"\r\n")[0]:
        raise RuntimeError(f"握手失败: {resp[:80]!r}")
    return tls


def ws_send_text(sock, payload="X"):
    data = payload.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    else:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    header += mask
    sock.sendall(bytes(header) + masked)


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError("连接关闭")
        buf += c
    return buf


def ws_recv(sock):
    b1 = recv_exact(sock, 1)[0]
    b2 = recv_exact(sock, 1)[0]
    op = b1 & 0x0F
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", recv_exact(sock, 8))[0]
    return op, recv_exact(sock, ln) if ln else b""


def measure(host, proxy=None):
    t0 = time.time()
    try:
        sock = open_target(host, 443, proxy)
        tls = ws_handshake(sock, host)
    except Exception as e:
        return {"host": host, "ok": False, "err": f"{type(e).__name__}: {e}",
                "rtt": None, "total": int((time.time() - t0) * 1000)}
    connect_ms = int((time.time() - t0) * 1000)
    rtts = []
    try:
        for _ in range(COUNT):
            s = time.time()
            ws_send_text(tls)
            while True:
                op, _ = ws_recv(tls)
                if op == 0x1:
                    break
                if op == 0x8:
                    raise ConnectionError("服务端关闭")
            rtts.append((time.time() - s) * 1000)
    except Exception as e:
        return {"host": host, "ok": False, "err": f"{type(e).__name__}: {e}",
                "rtt": None, "total": int((time.time() - t0) * 1000),
                "connect_ms": connect_ms, "got": len(rtts)}
    finally:
        try:
            tls.close()
        except Exception:
            pass
    total = int((time.time() - t0) * 1000)
    avg = sum(rtts) / len(rtts)
    jitter = max(rtts) - min(rtts)
    return {
        "host": host, "ok": len(rtts) == COUNT,
        "rtt": round(avg, 1), "min": round(min(rtts), 1),
        "max": round(max(rtts), 1), "jitter": round(jitter, 1),
        "connect_ms": connect_ms, "total": total, "got": len(rtts),
        "pass": total < LIMIT_MS and len(rtts) == COUNT,
    }


def eval_proxy(label, proxy):
    log("")
    log("=" * 86)
    log(f"候选：{label}")
    log("=" * 86)
    results = []
    for h in HOSTS:
        r = measure(h, proxy)
        results.append(r)
        if r.get("ok"):
            flag = "✅达标" if r.get("pass") else "❌超时"
            log(f"  {h:44} rtt均{r['rtt']:>6}ms 抖{r['jitter']:>6}ms "
                f"总{r['total']:>6}ms  {flag}")
        else:
            log(f"  {h:44} ❌ {r.get('err','')[:60]} "
                f"(完成{r.get('got')}/{COUNT})")
    passed = sum(1 for r in results if r.get("pass"))
    done = sum(1 for r in results if r.get("ok"))
    if done:
        avg_rtt = round(sum(r["rtt"] for r in results if r.get("ok")) / done, 1)
    else:
        avg_rtt = None
    log("")
    log(f"  完成 {COUNT} 次往返: {done}/{len(HOSTS)}   "
        f"达标(<{LIMIT_MS}ms): {passed}/{len(HOSTS)}   "
        f"平均 rtt: {avg_rtt}ms")
    if passed == 0:
        log(f"  → 判定：不可用（SDK 会把 rtt 记为 -1，导致 -110005）")
    elif passed < 2:
        log(f"  → 判定：勉强（仅 {passed} 个达标，抖动可能导致失败）")
    else:
        log(f"  → 判定：可用（{passed} 个达标）")
    return {"label": label, "proxy": proxy, "passed": passed,
            "done": done, "avg_rtt": avg_rtt}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", help="单个代理，如 socks5://127.0.0.1:1080")
    ap.add_argument("--list", help="代理列表文件，每行一个")
    args = ap.parse_args()

    log("=" * 86)
    log("云游戏测速节点 RTT 筛选")
    log(f"判定标准：{COUNT} 次 WebSocket 往返总耗时 < {LIMIT_MS}ms")
    log(f"即单次 rtt 需 < {PER_PING_BUDGET_MS:.0f}ms（含建连与抖动余量）")
    log("=" * 86)

    reports = []
    reports.append(eval_proxy("直连（基线）", None))

    candidates = []
    if args.proxy:
        candidates.append(args.proxy)
    if args.list and os.path.exists(args.list):
        for ln in open(args.list, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                candidates.append(ln)

    for c in candidates:
        try:
            reports.append(eval_proxy(c, c))
        except Exception as e:
            log(f"  代理 {c} 测试异常: {e}")

    log("")
    log("=" * 86)
    log("汇总")
    log("=" * 86)
    log(f"  {'候选':<44} {'达标':>6} {'完成':>6} {'平均rtt':>10}")
    for r in reports:
        log(f"  {r['label']:<44} {r['passed']:>4}/{len(HOSTS)} "
            f"{r['done']:>4}/{len(HOSTS)} {str(r['avg_rtt']):>9}ms")
    log("")
    best = max(reports, key=lambda r: (r["passed"], -(r["avg_rtt"] or 9999)))
    if best["passed"] == 0:
        log("  所有候选均无节点达标。")
        log("  提示：达标数取决于「到国内测速节点的 rtt」，")
        log("        仅换出口 IP 而不改善链路延迟是没用的。")
    else:
        log(f"  最佳候选：{best['label']}（{best['passed']} 个节点达标）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
