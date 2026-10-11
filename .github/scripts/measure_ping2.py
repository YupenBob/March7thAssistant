"""精确测量云游戏节点测速：正确实现 WebSocket 帧掩码。

背景：SDK 的 WebSocketRttTester.testRttMs(20, 1000) 要求
20 次往返在 1000ms 内完成，否则该节点 rtt 记为 -1；
页面随后过滤掉 rtt < 0 的项，全部为 -1 时 ping_results 变空，
服务端返回 retcode -110005「游戏服务器连接中断」。

前一版手工实现漏了「客户端帧必须掩码」（RFC 6455），
导致测量被严重拉长、数据不可信。本版正确实现掩码。

用法：python measure_ping2.py
"""
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


def ws_handshake(host, timeout=10):
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
    raw = socket.create_connection((host, 443), timeout=timeout)
    tls = ctx.wrap_socket(raw, server_hostname=host)
    tls.sendall(req.encode())
    tls.settimeout(timeout)
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = tls.recv(4096)
        if not chunk:
            break
        resp += chunk
    if b"101" not in resp.split(b"\r\n")[0]:
        raise RuntimeError(f"握手失败: {resp[:100]!r}")
    return tls


def ws_send_text(sock, payload: str):
    """发送文本帧。客户端必须掩码（RFC 6455 §5.3）——上一版漏了这一步。"""
    data = payload.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

    header = bytearray([0x81])  # FIN + text
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    sock.sendall(bytes(header) + masked)


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接关闭")
        buf += chunk
    return buf


def ws_recv_frame(sock):
    """读一帧，返回 (opcode, payload)；服务端帧不掩码。"""
    b1 = recv_exact(sock, 1)[0]
    b2 = recv_exact(sock, 1)[0]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else None
    payload = recv_exact(sock, ln) if ln else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def measure(host, count=COUNT):
    t0 = time.time()
    try:
        sock = ws_handshake(host, timeout=12)
    except Exception as e:
        return {"host": host, "ok": False, "err": f"{type(e).__name__}: {e}",
                "rtt": -1, "elapsed": int((time.time() - t0) * 1000)}

    connect_ms = int((time.time() - t0) * 1000)
    rtts = []
    try:
        for _ in range(count):
            s = time.time()
            ws_send_text(sock, "X")
            while True:
                op, payload = ws_recv_frame(sock)
                if op == 0x1:  # text
                    break
                if op == 0x8:  # close
                    raise ConnectionError("服务端关闭")
            rtts.append((time.time() - s) * 1000)
    except Exception as e:
        return {"host": host, "ok": False, "err": f"{type(e).__name__}: {e}",
                "rtt": -1, "elapsed": int((time.time() - t0) * 1000),
                "connect_ms": connect_ms, "got": len(rtts)}
    finally:
        try:
            sock.close()
        except Exception:
            pass

    elapsed = int((time.time() - t0) * 1000)
    return {
        "host": host,
        "ok": len(rtts) == count,
        "got": len(rtts),
        "rtt": round(sum(rtts) / len(rtts)) if rtts else -1,
        "connect_ms": connect_ms,
        "elapsed": elapsed,
        "median_ms": round(sorted(rtts)[len(rtts) // 2], 1) if rtts else None,
        "max_ms": round(max(rtts), 1) if rtts else None,
    }


def main():
    print("=" * 88)
    print(f"节点测速实测（正确掩码）—— SDK 时限 {LIMIT_MS}ms / {COUNT} 次往返")
    print("=" * 88)
    print()

    results = []
    for h in HOSTS:
        r = measure(h)
        results.append(r)
        if r["ok"]:
            verdict = "✅ 达标" if r["elapsed"] <= LIMIT_MS else "❌ 超时"
            print(f"  {h:44} 建连={r['connect_ms']:>4}ms "
                  f"rtt均值={r['rtt']:>3}ms 中位={r['median_ms']:>5}ms "
                  f"最大={r['max_ms']:>6}ms 总耗时={r['elapsed']:>5}ms  {verdict}")
        else:
            print(f"  {h:44} ❌ 未完成 {r.get('got')}/{COUNT} "
                  f"建连={r.get('connect_ms')}ms 耗时={r['elapsed']}ms {r.get('err','')[:50]}")

    print()
    print("=" * 88)
    print("结论")
    print("=" * 88)
    ok = [r for r in results if r["ok"]]
    fit = [r for r in ok if r["elapsed"] <= LIMIT_MS]
    print(f"  完成 {COUNT} 次往返: {len(ok)}/{len(results)}")
    print(f"  在 {LIMIT_MS}ms 内完成: {len(fit)}/{len(results)}")
    if ok:
        avg = round(sum(r["elapsed"] for r in ok) / len(ok))
        print(f"  完成者平均总耗时: {avg}ms")
        print(f"  各节点总耗时: {[r['elapsed'] for r in ok]}ms")
    print()
    if len(fit) == 0:
        print("  ❗ 无一节点达标 —— 与页面现象吻合：")
        print("     SDK 超时兜底把 rtt 置 -1，页面过滤后 ping_results 为空，")
        print("     服务端返回 -110005「游戏服务器连接中断」。")
    elif len(fit) < len(results):
        print(f"  仅 {len(fit)} 个节点达标。SDK 只取小值前若干，")
        print("  若达标数不足，仍可能回 -110005。")
    else:
        print("  全部达标 —— 说明瓶颈不在测速耗时，需继续查其它环节。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
