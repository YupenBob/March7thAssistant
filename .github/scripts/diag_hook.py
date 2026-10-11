"""抓取关键接口的响应体 + 页面 JS 的报错，定位 paasDispatch 为何不发出。

已确认的事实：
  dispatcher 流程走到 getNodesInfo 200 之后就没有下一步，
  paasDispatch（真正分配节点）从未发出，直接弹「网络错误」。

本脚本用 CDP 做三件事：
  1. 通过 performance 日志拿到所有请求，对关键接口用
     Network.getResponseBody 取回响应体（判断服务端到底返回了什么）
  2. 注入 JS 钩子：包住 XMLHttpRequest 与 WebSocket，
     记录云游戏 SDK 实际发出的请求与收到的响应/错误
  3. 监听 window.onerror / unhandledrejection，抓页面自身报错

产物：/m7a/logs/diag3/ 下的 responses/、hooks.json、jserrors.json、final.html
"""
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

OUT = "/m7a/logs/diag3"
RESP = os.path.join(OUT, "responses")
os.makedirs(RESP, exist_ok=True)

# 关注这些接口
WATCH = ("getNodesInfo", "listPingServer", "paasDispatch", "statusCheck",
         "dispatch", "gamer/api/login", "wallet", "getUIConfig",
         "getSpeedLimitConfig", "leaveDispatchQueue")

# 注入到页面的钩子：记录 XHR 与 WebSocket 的完整往返
HOOK_JS = r"""
(function () {
  if (window.__m7aHooked) return "already";
  window.__m7aHooked = true;
  window.__m7aLog = [];

  function rec(kind, obj) {
    try {
      obj.ts = Date.now();
      window.__m7aLog.push(Object.assign({ kind: kind }, obj));
      if (window.__m7aLog.length > 800) window.__m7aLog.shift();
    } catch (e) {}
  }

  // ---- XHR ----
  var OX = XMLHttpRequest.prototype.open;
  var OS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__m7a = { method: m, url: String(u) };
    return OX.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    var self = this;
    var info = this.__m7a || {};
    info.body = body ? String(body).slice(0, 600) : null;
    rec("xhr_send", info);
    this.addEventListener("loadend", function () {
      var text = "";
      try {
        if (self.responseType === "" || self.responseType === "text") {
          text = (self.responseText || "").slice(0, 3000);
        } else {
          text = "<responseType=" + self.responseType + ">";
        }
      } catch (e) { text = "<" + e + ">"; }
      rec("xhr_done", {
        method: info.method, url: info.url,
        status: self.status, response: text,
        headers: (function () {
          try { return self.getAllResponseHeaders().slice(0, 600); }
          catch (e) { return ""; }
        })()
      });
    });
    this.addEventListener("error", function () {
      rec("xhr_error", { url: info.url });
    });
    this.addEventListener("timeout", function () {
      rec("xhr_timeout", { url: info.url });
    });
    return OS.apply(this, arguments);
  };

  // ---- WebSocket ----
  var OWS = window.WebSocket;
  function Wrapped(url, protocols) {
    var ws = protocols === undefined ? new OWS(url) : new OWS(url, protocols);
    rec("ws_open_attempt", { url: String(url) });
    try {
      ws.addEventListener("open", function () { rec("ws_open", { url: String(url) }); });
      ws.addEventListener("error", function (e) {
        rec("ws_error", { url: String(url), detail: String(e && e.type) });
      });
      ws.addEventListener("close", function (e) {
        rec("ws_close", { url: String(url), code: e && e.code, reason: e && e.reason });
      });
      ws.addEventListener("message", function (e) {
        rec("ws_message", { url: String(url),
                            data: String(e.data).slice(0, 600) });
      });
    } catch (e) {}
    return ws;
  }
  Wrapped.prototype = OWS.prototype;
  Wrapped.CONNECTING = 0; Wrapped.OPEN = 1;
  Wrapped.CLOSING = 2; Wrapped.CLOSED = 3;
  window.WebSocket = Wrapped;

  // ---- RTCPeerConnection（串流依赖）----
  if (window.RTCPeerConnection) {
    var ORP = window.RTCPeerConnection;
    window.RTCPeerConnection = function (cfg, con) {
      rec("rtc_create", { config: JSON.stringify(cfg || {}).slice(0, 500) });
      var pc = new ORP(cfg, con);
      try {
        pc.addEventListener("iceconnectionstatechange", function () {
          rec("rtc_ice_state", { state: pc.iceConnectionState });
        });
        pc.addEventListener("connectionstatechange", function () {
          rec("rtc_conn_state", { state: pc.connectionState });
        });
      } catch (e) {}
      return pc;
    };
    window.RTCPeerConnection.prototype = ORP.prototype;
  }

  // ---- 页面自身报错 ----
  window.__m7aErrors = [];
  window.addEventListener("error", function (e) {
    window.__m7aErrors.push({
      type: "error",
      message: String(e.message || ""),
      filename: String(e.filename || ""),
      lineno: e.lineno, colno: e.colno,
      stack: e.error && e.error.stack ? String(e.error.stack).slice(0, 800) : ""
    });
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    window.__m7aErrors.push({
      type: "unhandledrejection",
      message: r && r.message ? String(r.message) : String(r),
      stack: r && r.stack ? String(r.stack).slice(0, 800) : ""
    });
  });

  return "ok";
})();
"""


def logmsg(m):
    print(m, flush=True)


def inject_hook(driver):
    """注入钩子。

    注意：execute_script 的返回值不一定是字符串 —— 实测本机 Chrome 返回 None，
    CI 上可能返回 "ok"。因此不能用 == "ok" 判定，改为注入后
    直接检查 window.__m7aHooked 标记（这才是可靠依据）。
    """
    try:
        ret = driver.execute_script(HOOK_JS)
        alive = driver.execute_script("return !!(window.__m7aHooked)")
        logmsg(f">>> 注入钩子: 返回={ret!r}  标记存活={alive}")
        return bool(alive)
    except Exception as e:
        logmsg(f">>> 注入失败: {type(e).__name__} {e}")
        return False


def collect_hook(driver):
    try:
        return driver.execute_script("return window.__m7aLog || [];")
    except Exception as e:
        return [{"kind": "collect_error", "detail": str(e)}]


def collect_errors(driver):
    try:
        return driver.execute_script("return window.__m7aErrors || [];")
    except Exception:
        return []


def start_with_retry(controller, attempts=4, wait=20):
    """带重试地启动游戏。

    实测：runner 到米哈游的跨境链路会偶发 ERR_TIMED_OUT，
    导致 _wait_game_page_loaded 三次刷新后放弃。
    这正是正式 workflow 里加容器级重试的原因，诊断脚本同样需要，
    否则一次抖动就白跑一轮。
    """
    for i in range(1, attempts + 1):
        logmsg(f">>> start_game_process 第 {i}/{attempts} 次")
        try:
            if controller.start_game_process():
                logmsg("    成功")
                return True
        except Exception as e:
            logmsg(f"    异常: {type(e).__name__} {e}")
        logmsg(f"    失败，{wait}s 后重试")
        try:
            controller.stop_game()
        except Exception:
            pass
        time.sleep(wait)
    return False


def main():
    logmsg(">>> 启动控制器")
    controller = CloudGameController(cfg, log)
    if not start_with_retry(controller):
        logmsg(">>> 启动失败（已重试）")
        return 1
    driver = controller.driver

    # 钩子必须在页面加载前注入才有效，因此注入后刷新一次
    logmsg(">>> 首次注入钩子（可能因页面未加载而被清掉）")
    inject_hook(driver)

    logmsg(">>> 注入 localStorage（云游戏需要）")
    try:
        controller._load_initial_local_storage()
    except Exception:
        traceback.print_exc()

    logmsg(">>> 刷新页面以让钩子在干净上下文里生效")
    try:
        driver.refresh()
        time.sleep(3)
        inject_hook(driver)
        time.sleep(3)
        # 刷新会清掉 localStorage 注入，补一次并再刷新
        try:
            controller._load_initial_local_storage()
        except Exception:
            pass
        driver.refresh()
        time.sleep(3)
        inject_hook(driver)
    except Exception:
        traceback.print_exc()

    time.sleep(8)
    try:
        logged = controller._check_login(timeout=15)
    except Exception:
        logged = None
    logmsg(f">>> _check_login -> {logged}")

    # 确认钩子仍在
    ok = driver.execute_script("return !!(window.__m7aHooked)")
    logmsg(f">>> 钩子存活: {ok}")
    if not ok:
        logmsg(">>> 钩子丢失，重新注入（此前的早期请求可能抓不到）")
        inject_hook(driver)

    from selenium.webdriver.common.by import By
    try:
        g = driver.find_elements(By.CSS_SELECTOR, "div.guide-close-btn-x, div.guide-close-btn__x")
        if g:
            driver.execute_script("arguments[0].click();", g[0])
            time.sleep(1)
    except Exception:
        pass

    logmsg(">>> 点击进入游戏")
    try:
        btn = driver.find_elements(By.CSS_SELECTOR, "div.wel-card__content--start")
        logmsg(f"    按钮数 {len(btn)}")
        if btn:
            driver.execute_script("arguments[0].click();", btn[0])
    except Exception:
        traceback.print_exc()

    # 观察 45 秒，期间多次采样钩子
    t0 = time.time()
    while time.time() - t0 < 45:
        time.sleep(5)
        n = driver.execute_script("return (window.__m7aLog||[]).length")
        errs = len(collect_errors(driver))
        logmsg(f"    [{int(time.time()-t0):2d}s] 钩子记录 {n} 条，JS 错误 {errs} 条")

    logmsg(">>> 导出钩子数据")
    hooklog = collect_hook(driver)
    errors = collect_errors(driver)
    with open(os.path.join(OUT, "hooks.json"), "w", encoding="utf-8") as f:
        json.dump(hooklog, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT, "jserrors.json"), "w", encoding="utf-8") as f:
        json.dump(errors, f, ensure_ascii=False, indent=2)
    logmsg(f"    钩子 {len(hooklog)} 条，JS 错误 {len(errors)} 条")

    logmsg(">>> 关键接口往返")
    for e in hooklog:
        u = str(e.get("url") or "")
        if any(k in u for k in WATCH):
            kind = e.get("kind")
            st = e.get("status", "")
            extra = ""
            if kind == "xhr_done":
                extra = f" resp={str(e.get('response'))[:220]}"
            elif kind in ("ws_error", "ws_close", "xhr_error", "xhr_timeout"):
                extra = f" {e}"
            logmsg(f"    [{kind}] {st} {u[:100]}{extra}")

    logmsg(">>> JS 错误")
    for e in errors[:25]:
        logmsg(f"    [{e.get('type')}] {str(e.get('message'))[:200]}")
        if e.get("stack"):
            logmsg(f"        {str(e['stack'])[:300]}")

    logmsg(">>> RTCPeerConnection / WebSocket 事件")
    for e in hooklog:
        if str(e.get("kind", "")).startswith(("rtc_", "ws_")):
            logmsg(f"    {json.dumps(e, ensure_ascii=False)[:260]}")

    try:
        with open(os.path.join(OUT, "final.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        driver.save_screenshot(os.path.join(OUT, "final.png"))
    except Exception:
        pass

    logmsg(">>> 诊断完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
