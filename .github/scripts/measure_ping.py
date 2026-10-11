"""精确测量：云游戏节点测速能否在 SDK 的时限内完成。

从 cg-sdk bundle 读到的关键逻辑：
  WebSocketRttTester.testRttMs = (count, timeoutMs) => {
      if (this.ws !== null) return -1;          // 复用同一 tester 直接失败
      const timer = setTimeout(() => { this.ws?.close(); resolve(-1); }, timeoutMs);
      ws.onopen    -> for (i<count) this.ping()
      ws.onmessage -> 收满 count 次后计算平均 rtt 并 resolve
      ws.onerror   -> resolve(-1)
      ws.onclose   -> clearTimeout; this.ws = null
  }
  ping() 发送字符串 "X"；onPong 用 Date.now() 差值

调用侧：doSpeedTest(..., 1000, ...) => testRttMs(20, 1000)
  即：20 次往返必须挤在 1000ms 内完成，否则该节点 rtt = -1。

而页面过滤 rtt < 0 的结果，全为 -1 时 ping_results 变空，
服务端随即返回 retcode -110005「游戏服务器连接中断」。

本脚本直接用 Python 复刻这个测速，测量真实耗时，判断 1000ms 是否够。
"""
import json
import socket
import ssl
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


def ws_handshake(host, timeout=10):
    """手工完成 WebSocket 握手，返回已连接的 socket。"""
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
    resp = tls.recv(4096)
    if b"101" not in resp.split(b"\r\n")[0]:
        raise RuntimeError(f"握手失败: {resp[:80]!r}")
    return tls


def ws_send_text(sock, payload: str):
    """发送一个未掩码的文本帧（客户端应掩码，这里服务端一般不校验）。"""
    data = payload.encode()
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    else:
        header.append(0x80 | 126)
        header += n.to_bytes(2, "big")
    mask = b"\x00\x00\x00\x00"
    header += mask
    sock.sendall(bytes(header) + data)


def ws_recv_frame(sock):
    """读一帧，返回 payload（仅处理小帧）。"""
    b1 = sock.recv(1)
    if not b1:
        return None
    b2 = sock.recv(1)
    if not b2:
        return None
    ln = b2[0] & 0x7F
    if ln == 126:
        ln = int.from_bytes(sock.recv(2), "big")
    elif ln == 127:
        ln = int.from_bytes(sock.recv(8), "big")
    payload = b""
    while len(payload) < ln:
        chunk = sock.recv(ln - len(payload))
        if not chunk:
            break
        payload += chunk
    return payload


def measure(host, count=COUNT, timeout_ms=1000):
    """复刻 SDK 的测速：连上后连发 count 个 ping，测总耗时。"""
    t0 = time.time()
    try:
        sock = ws_handshake(host, timeout=timeout_ms / 1000 + 5)
    except Exception as e:
        return {"host": host, "ok": False, "err": f"{type(e).__name__}: {e}",
                "rtt": -1, "elapsed_ms": int((time.time() - t0) * 1000)}

    connect_ms = int((time.time() - t0) * 1000)
    rtts = []
    deadline = t0 + timeout_ms / 1000
    try:
        for i in range(count):
            s = time.time()
            ws_send_text(sock, "X")
            ws_recv_frame(sock)
            rtts.append((time.time() - s) * 1000)
            if time.time() > deadline:
                break
    except Exception as e:
        return {"host": host, "ok": False,
                "err": f"{type(e).__name__}: {e}", "rtt": -1,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "connect_ms": connect_ms, "got": len(rtts)}
    finally:
        try:
            sock.close()
        except Exception:
            pass

    elapsed = int((time.time() - t0) * 1000)
    done = len(rtts) == count
    avg = round(sum(rtts) / len(rtts)) if rtts else -1
    return {
        "host": host,
        "ok": done,
        "got": len(rtts),
        "need": count,
        "rtt": avg if done else -1,
        "connect_ms": connect_ms,
        "elapsed_ms": elapsed,
        "within_1000ms": elapsed <= timeout_ms,
        "per_ping_ms": round(sum(rtts) / len(rtts), 1) if rtts else None,
    }


def main():
    print("=" * 84)
    print("复刻 SDK 的节点测速：20 次往返 / 1000ms 时限")
    print("=" * 84)
    print()

    results = []
    for h in HOSTS:
        r = measure(h)
        results.append(r)
        if r["ok"]:
            print(f"  {h:44} 建连={r['connect_ms']:>5}ms "
                  f"完成{r['got']}/{r['need']} 总耗时={r['elapsed_ms']:>6}ms "
                  f"rtt={r['rtt']}ms   {'✅ 在时限内' if r['within_1000ms'] else '❌ 超时'}")
        else:
            extra = f"仅完成 {r.get('got')}/{r.get('need')}" if r.get("got") is not None else r.get("err", "")[:60]
            print(f"  {h:44} ❌ 失败 耗时={r['elapsed_ms']}ms  {extra}")

    print()
    print("=" * 84)
    print("结论")
    print("=" * 84)
    ok = [r for r in results if r["ok"]]
    in_time = [r for r in results if r.get("within_1000ms")]
    print(f"  成功完成 20 次往返的节点: {len(ok)}/{len(results)}")
    print(f"  能在 1000ms 内完成的节点: {len(in_time)}/{len(results)}")
    if ok:
        avg_el = round(sum(r["elapsed_ms"] for r in ok) / len(ok))
        print(f"  平均总耗时: {avg_el}ms（SDK 时限 1000ms）")
    print()
    if len(in_time) == 0:
        print("  ❗ 没有任何节点能在 1000ms 内完成测速 —— 与页面现象完全吻合：")
        print("     SDK 超时兜底把 rtt 置为 -1，页面过滤掉后 ping_results 为空，")
        print("     服务端返回 retcode -110005「游戏服务器连接中断」。")
    elif len(in_time) < len(results):
        print("  部分节点超时。SDK 取超时兜底值时该节点为 -1。")
    else:
        print("  全部节点都在时限内 —— 说明问题不在耗时，需要继续查其它环节。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
