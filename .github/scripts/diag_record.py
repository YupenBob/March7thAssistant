"""进入云游戏全过程的录屏 + 网络抓包诊断。

为什么需要它：
  之前只能靠 DOM 截图和日志推断，看不到"点击进入游戏"到弹错误之间到底发生了什么。
  本脚本用 CDP 同时做三件事：
    1. Page.startScreencast 录帧 -> 拼成 mp4（能看到界面变化过程）
    2. Network 域监听 -> 记录点击后发出的每个请求与响应（含失败原因）
    3. Runtime.consoleAPICalled -> 抓页面 JS 的报错

用法（容器内）：
  python diag_record.py
产物：/m7a/logs/diag/ 下的 frames/、video.mp4、network.json、console.log
"""
import base64
import json
import os
import sys
import time
import traceback

sys.path.insert(0, "/m7a")
os.chdir("/m7a")

from module.config import cfg  # noqa: E402
from module.game.cloud import CloudGameController  # noqa: E402
from module.logger import log  # noqa: E402

OUT = "/m7a/logs/diag"
FRAMES = os.path.join(OUT, "frames")
os.makedirs(FRAMES, exist_ok=True)

WIDTH, HEIGHT = 1280, 720


def logmsg(m):
    print(m, flush=True)


# ---------------------------------------------------------------------------
# 关键：selenium 的 driver.get_log("performance") 必须在创建 driver 时
# 就带上 goog:loggingPrefs，否则取不到。应用已经写好自己的 options，
# 这里用 monkey-patch 在 webdriver.Chrome 被调用前注入该能力，
# 从而拿到完整的网络请求日志 —— 不修改上游任何代码。
# ---------------------------------------------------------------------------
_PATCHED = False


def patch_webdriver_for_netlog():
    global _PATCHED
    if _PATCHED:
        return
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions

        orig_init = ChromeOptions.__init__

        def init_with_logging(self, *a, **kw):
            orig_init(self, *a, **kw)
            try:
                self.set_capability("goog:loggingPrefs",
                                    {"performance": "ALL", "browser": "ALL"})
                logmsg("  [patch] 已注入 goog:loggingPrefs")
            except Exception as e:
                logmsg(f"  [patch] 注入失败: {e}")

        ChromeOptions.__init__ = init_with_logging
        _PATCHED = True
    except Exception as e:
        logmsg(f"  [patch] 无法 patch webdriver: {e}")


class Recorder:
    """通过 CDP 录帧 + 抓网络。"""

    def __init__(self, driver):
        self.driver = driver
        self.frames = []
        self.requests = {}
        self.console = []
        self._n = 0

    def enable(self):
        d = self.driver
        try:
            d.execute_cdp_cmd("Page.enable", {})
        except Exception as e:
            logmsg(f"  Page.enable 失败: {e}")
        try:
            d.execute_cdp_cmd("Network.enable", {"maxTotalBufferSize": 100_000_000})
        except Exception as e:
            logmsg(f"  Network.enable 失败: {e}")

        # 注入事件收集：selenium 的 execute_cdp_cmd 是同步命令，
        # 无法直接收事件，因此改用 performance log 抓网络（见 enable_perf）。
        logmsg("  CDP 域已启用")

    def enable_perf(self):
        """用 performance log 抓网络请求（selenium 4 支持事件驱动）。"""
        try:
            self.driver.execute_cdp_cmd("Performance.enable", {})
        except Exception:
            pass
        try:
            caps = self.driver.capabilities
            caps["goog:loggingPrefs"] = {"performance": "ALL", "browser": "ALL"}
            logmsg("  已请求 performance/browser 日志")
        except Exception:
            pass

    def grab_frame(self, tag=""):
        """抓一帧并落盘。"""
        try:
            data = self.driver.execute_cdp_cmd("Page.captureScreenshot", {
                "format": "png",
                "captureBeyondViewport": False,
            })
            raw = base64.b64decode(data["data"])
            self._n += 1
            name = f"{self._n:04d}_{tag}.png"
            with open(os.path.join(FRAMES, name), "wb") as f:
                f.write(raw)
            self.frames.append(name)
            return len(raw)
        except Exception as e:
            logmsg(f"  抓帧失败: {type(e).__name__} {e}")
            return 0

    def drain_logs(self):
        """读取浏览器/性能日志。"""
        self._log_err = getattr(self, "_log_err", {})
        for logtype in ("performance", "browser"):
            try:
                entries = self.driver.get_log(logtype)
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:120]}"
                if self._log_err.get(logtype) != msg:
                    self._log_err[logtype] = msg
                    logmsg(f"  get_log('{logtype}') 不可用: {msg}")
                continue
            if logtype == "performance":
                for e in entries:
                    try:
                        msg = json.loads(e["message"])["message"]
                    except Exception:
                        continue
                    self._handle_perf(msg)
            else:
                for e in entries:
                    self.console.append({"level": e.get("level"),
                                         "message": str(e.get("message"))[:500]})

    def _handle_perf(self, msg):
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method == "Network.requestWillBeSent":
            rid = params.get("requestId")
            req = params.get("request", {})
            self.requests[rid] = {
                "url": req.get("url"),
                "method": req.get("method"),
                "type": params.get("type"),
                "ts": params.get("timestamp"),
                "status": None,
                "error": None,
            }
        elif method == "Network.responseReceived":
            rid = params.get("requestId")
            if rid in self.requests:
                self.requests[rid]["status"] = params.get("response", {}).get("status")
                self.requests[rid]["mime"] = params.get("response", {}).get("mimeType")
        elif method == "Network.loadingFailed":
            rid = params.get("requestId")
            if rid in self.requests:
                self.requests[rid]["error"] = params.get("errorText")
                self.requests[rid]["blocked"] = params.get("blockedReason")
        elif method == "Network.webSocketCreated":
            self.requests[f"ws-{params.get('requestId')}"] = {
                "url": params.get("url"), "method": "WS", "type": "WebSocket",
                "status": "created", "error": None,
            }

    def dump(self):
        with open(os.path.join(OUT, "network.json"), "w", encoding="utf-8") as f:
            json.dump(list(self.requests.values()), f, ensure_ascii=False, indent=2)
        with open(os.path.join(OUT, "console.log"), "w", encoding="utf-8") as f:
            for c in self.console:
                f.write(f"[{c['level']}] {c['message']}\n")
        logmsg(f"  网络请求 {len(self.requests)} 条，浏览器日志 {len(self.console)} 条")


def state_of(driver):
    probes = {
        "game_running": ".game-player",
        "in_queue": "[class*='waiting-in-queue']",
        "select_queue": "[aria-labelledby*='请选择排队队列']",
        "enter_btn": "div.wel-card__content--start",
        "user_aid": "div.user-aid.wel-card__aid",
        "dialog": ".van-dialog__content",
    }
    out = {}
    for k, sel in probes.items():
        try:
            out[k] = len(driver.find_elements("css selector", sel))
        except Exception:
            out[k] = -1
    try:
        txt = driver.execute_script(
            "return (document.body&&document.body.innerText||'').slice(-400);")
    except Exception:
        txt = ""
    out["tail"] = (txt or "").replace("\n", " | ")[-300:]
    return out


def main():
    logmsg(">>> 启动控制器")
    patch_webdriver_for_netlog()
    controller = CloudGameController(cfg, log)
    if not controller.start_game_process():
        logmsg(">>> start_game_process 失败")
        return 1
    driver = controller.driver

    rec = Recorder(driver)
    rec.enable()
    # get_log 只返回"自上次调用之后"的新条目，必须从此刻起持续读取，
    # 否则点击前发生的请求会被丢掉（实测踩过这个坑）。
    rec.drain_logs()

    logmsg(">>> 注入 localStorage")
    try:
        controller._load_initial_local_storage()
    except Exception:
        traceback.print_exc()
    rec.drain_logs()

    logmsg(">>> 等页面就绪")
    time.sleep(6)
    rec.grab_frame("ready")
    rec.drain_logs()
    logmsg("    " + json.dumps(state_of(driver), ensure_ascii=False))

    logged = None
    try:
        logged = controller._check_login(timeout=15)
    except Exception:
        traceback.print_exc()
    rec.drain_logs()
    logmsg(f">>> _check_login -> {logged}")

    # 刻意刷新一次（之前实测刷新后判定才稳定为 True）。
    # --no-refresh 可跳过，用于验证刷新是否是导致后续失败的诱因。
    no_refresh = "--no-refresh" in sys.argv or os.environ.get("M7A_NO_REFRESH") == "1"
    if no_refresh:
        logmsg(">>> 按参数跳过刷新")
    else:
        logmsg(">>> 刷新页面")
        try:
            driver.refresh()
            time.sleep(8)
        except Exception:
            traceback.print_exc()
    rec.grab_frame("after_refresh")
    rec.drain_logs()

    logmsg(">>> 关闭引导弹窗（如有）")
    try:
        from selenium.webdriver.common.by import By
        g = driver.find_elements(By.CSS_SELECTOR, "div.guide-close-btn__x")
        if g:
            driver.execute_script("arguments[0].click();", g[0])
            time.sleep(1)
    except Exception:
        pass
    rec.drain_logs()

    logmsg(">>> 点击进入游戏，并录制 40 秒")
    t0 = time.time()
    clicked = False
    try:
        from selenium.webdriver.common.by import By
        btn = driver.find_elements(By.CSS_SELECTOR, "div.wel-card__content--start")
        logmsg(f"    找到进入按钮 {len(btn)} 个")
        if btn:
            driver.execute_script("arguments[0].click();", btn[0])
            clicked = True
            logmsg("    已点击")
    except Exception:
        traceback.print_exc()

    seq = 0
    while time.time() - t0 < 40:
        seq += 1
        rec.grab_frame(f"t{int(time.time()-t0):02d}")
        rec.drain_logs()
        st = state_of(driver)
        if seq % 3 == 0 or st.get("dialog", 0) > 0 or st.get("game_running", 0) > 0:
            logmsg(f"    [{int(time.time()-t0):2d}s] " +
                   json.dumps({k: st[k] for k in
                               ("game_running", "in_queue", "select_queue",
                                "enter_btn", "dialog")}, ensure_ascii=False) +
                   f"  tail=...{st['tail'][-120:]}")
        time.sleep(2)

    rec.drain_logs()
    rec.dump()
    # 汇总失败请求
    bad = [r for r in rec.requests.values()
           if r.get("error") or (r.get("status") and int(r["status"]) >= 400)]
    logmsg("")
    logmsg(f">>> 失败/异常请求 {len(bad)} 条：")
    for r in bad[:40]:
        logmsg(f"    [{r.get('status') or r.get('error')}] {r.get('url')}")

    # 只列云游戏相关域名，便于聚焦
    logmsg("")
    logmsg(">>> 云游戏相关请求：")
    for r in rec.requests.values():
        u = r.get("url") or ""
        if any(k in u for k in ("cloudgame", "cg-hkrpg", "dispatch", "wallet",
                                "mhystatic", "wss")):
            logmsg(f"    [{r.get('status') or r.get('error') or '?'}] {u[:130]}")

    logmsg("")
    logmsg(f">>> 录得 {len(rec.frames)} 帧，目录 {FRAMES}")
    final = os.path.join(OUT, "final.png")
    try:
        driver.save_screenshot(final)
        logmsg(f">>> 末帧截图 {final}")
    except Exception:
        pass
    logmsg(">>> 诊断完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
