"""验证 mhystatic.com 的 WebSocket 测速端点能否连通。

背景：录屏+抓包发现失败的真正位置是
  wss://cg-t-*-wssping.mhystatic.com/wsspingsvr/echo
  Chrome 报 "WebSocket is closed before the connection is established"

本脚本从 runner 侧独立测试：
  1. DNS 解析
  2. TCP 443 连通性
  3. 真实 WebSocket 握手（HTTP 101）
  4. TLS 证书情况
用于判断是网络层不通，还是只有 Chrome 内部建连失败。
"""
import socket
import ssl
import sys
import time

HOSTS = [
    "cg-t-tybj2-wssping.mhystatic.com",
    "cg-t-ensfz6-wssping.mhystatic.com",
    "cg-t-tyyz1-wssping.mhystatic.com",
    "cg-t-ensgy15-wssping.mhystatic.com",
    "cg-t-mhywh1-wssping.mhystatic.com",
]
PATH = "/wsspingsvr/echo"


def log(m):
    print(m, flush=True)


def test_dns(host):
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        return True, f"{time.time()-t0:.2f}s -> {', '.join(ips)}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def test_tcp(host, timeout=10):
    t0 = time.time()
    try:
        s = socket.create_connection((host, 443), timeout=timeout)
        s.close()
        return True, f"{time.time()-t0:.2f}s"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def test_ws(host, timeout=15):
    """手工做 WebSocket 握手，看是否返回 101。"""
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    req = (
        f"GET {PATH} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: https://sr.mihoyo.com\r\n"
        "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36\r\n"
        "\r\n"
    )
    t0 = time.time()
    try:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((host, 443), timeout=timeout)
        tls = ctx.wrap_socket(raw, server_hostname=host)
        tls.sendall(req.encode())
        tls.settimeout(timeout)
        data = tls.recv(1024).decode("utf-8", "replace")
        dt = time.time() - t0
        first = data.split("\r\n")[0] if data else "(空响应)"
        ok = "101" in first
        tls.close()
        return ok, f"{dt:.2f}s  {first}"
    except Exception as e:
        return False, f"{time.time()-t0:.2f}s  {type(e).__name__}: {e}"


def main():
    log("=" * 76)
    log("mhystatic.com WebSocket 测速端点连通性测试")
    log("=" * 76)
    log("")

    # 先看主域能否解析
    for base in ("mhystatic.com", "cg-hkrpg-api.mihoyo.com", "sr.mihoyo.com"):
        ok, detail = test_dns(base)
        log(f"[DNS] {base:34} {'OK ' if ok else 'FAIL'}  {detail}")
    log("")

    results = []
    for h in HOSTS:
        log("-" * 76)
        log(f"HOST {h}")
        dns_ok, dns_d = test_dns(h)
        log(f"  DNS : {'OK ' if dns_ok else 'FAIL'}  {dns_d}")
        tcp_ok, tcp_d = test_tcp(h)
        log(f"  TCP : {'OK ' if tcp_ok else 'FAIL'}  {tcp_d}")
        ws_ok, ws_d = test_ws(h)
        log(f"  WSS : {'OK ' if ws_ok else 'FAIL'}  {ws_d}")
        results.append((h, dns_ok, tcp_ok, ws_ok))

    log("")
    log("=" * 76)
    log("汇总")
    log("=" * 76)
    ok_count = sum(1 for _, _, _, w in results if w)
    log(f"  5 个测速端点中，WebSocket 握手成功 {ok_count} 个")
    for h, d, t, w in results:
        log(f"    {h:40} dns={'Y' if d else 'N'} tcp={'Y' if t else 'N'} ws={'Y' if w else 'N'}")
    log("")
    if ok_count == 0:
        log("结论：WebSocket 全部失败 —— 与 Chrome 内的现象一致，属网络层问题")
    elif ok_count < len(results):
        log("结论：部分成功 —— 网络存在不稳定，可能是偶发")
    else:
        log("结论：网络层全部可达 —— 说明问题出在 Chrome 内部（代理/WebGL/其他）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
