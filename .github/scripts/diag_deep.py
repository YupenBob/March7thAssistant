"""深度诊断：进入游戏失败时，页面内部到底是什么状态。

已确认的事实链：
  - cookie 有效（能读到通行证 ID 327459957、剩余时长 600 分钟）
  - dispatcher 流程走到 listPingServer -> WS x5 -> getNodesInfo 全部 200
  - 之后没有 paasDispatch（真正分配节点的调用），直接弹「网络错误」
  - 页面还在请求 logo_home_unlogin.png，说明 UI 认为未登录

本脚本把这些内部状态全部导出，用于定位"为什么到了最后一步却不发 dispatch"：
  1. localStorage / sessionStorage 全量
  2. cookie 清单（只看名字与长度）
  3. 页面全局变量里的登录/用户信息
  4. 错误弹窗的完整文本
  5. 完整 HTML（供离线分析）
  6. 重试点击若干次，观察是否能偶发成功
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

OUT = "/m7a/logs/diag2"
os.makedirs(OUT, exist_ok=True)


def logmsg(m):
    print(m, flush=True)


def dump_storage(driver):
    out = {}
    try:
        out["localStorage"] = driver.execute_script("""
            var o = {};
            try {
              for (var i = 0; i < localStorage.length; i++) {
                var k = localStorage.key(i);
                var v = localStorage.getItem(k) || "";
                o[k] = v.length > 300 ? v.slice(0, 300) + "...(" + v.length + ")" : v;
              }
            } catch (e) { o.__error = String(e); }
            return o;
        """)
    except Exception as e:
        out["localStorage"] = f"<{e}>"
    try:
        out["sessionStorage"] = driver.execute_script("""
            var o = {};
            try {
              for (var i = 0; i < sessionStorage.length; i++) {
                var k = sessionStorage.key(i);
                var v = sessionStorage.getItem(k) || "";
                o[k] = v.length > 200 ? v.slice(0, 200) + "...(" + v.length + ")" : v;
              }
            } catch (e) { o.__error = String(e); }
            return o;
        """)
    except Exception as e:
        out["sessionStorage"] = f"<{e}>"
    try:
        out["cookies"] = [
            {"name": c.get("name"), "domain": c.get("domain"),
             "len": len(c.get("value") or "")}
            for c in driver.get_cookies()
        ]
    except Exception as e:
        out["cookies"] = f"<{e}>"
    return out


def dump_globals(driver):
    """探测页面里可能存放登录/用户信息的全局变量。"""
    return driver.execute_script("""
        var res = {};
        var names = ["__NUXT__","__INITIAL_STATE__","__STORE__","store",
                     "window.__cg","cgSdk","CGSDK","__CLOUD_GAME__"];
        names.forEach(function(n){
          try {
            var v = (n.indexOf('.') === 0 || n.indexOf('window.') === 0)
                    ? window.eval(n) : window[n];
            if (v !== undefined && v !== null) {
              var s = "";
              try { s = JSON.stringify(v); } catch(e) { s = String(v); }
              res[n] = s ? s.slice(0, 400) : String(v);
            }
          } catch(e) {}
        });
        // 扫一遍 window 上名字里带 login/user/account 的键
        var keys = [];
        try {
          for (var k in window) {
            if (/login|user|account|uuinfo|aid/i.test(k)) keys.push(k);
          }
        } catch(e) {}
        res.__matchedKeys = keys.slice(0, 60);
        return res;
    """)


def dialogs(driver):
    """取出页面所有弹窗的完整文本。"""
    try:
        return driver.execute_script("""
            var out = [];
            document.querySelectorAll(
              '.van-dialog, .van-popup, [class*="dialog"], [class*="modal"]'
            ).forEach(function(el){
              if (el.offsetParent !== null || (el.style && el.style.display !== 'none')) {
                var t = (el.innerText || '').trim();
                if (t) out.push(t.slice(0, 300));
              }
            });
            return out;
        """)
    except Exception as e:
        return [f"<{e}>"]


def user_aid_text(driver):
    try:
        return driver.execute_script("""
            var el = document.querySelector('div.user-aid.wel-card__aid');
            return el ? (el.innerText || '').trim() : null;
        """)
    except Exception:
        return None


def start_with_retry(controller, attempts=4, wait=20):
    """带重试地启动。实测跨境链路会偶发 ERR_TIMED_OUT 导致页面加载失败。"""
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

    logmsg(">>> 注入 localStorage")
    try:
        controller._load_initial_local_storage()
    except Exception:
        traceback.print_exc()

    time.sleep(6)
    try:
        logmsg(">>> 刷新以稳定登录态")
        driver.refresh()
        time.sleep(8)
    except Exception:
        traceback.print_exc()

    logged = None
    try:
        logged = controller._check_login(timeout=15)
    except Exception:
        traceback.print_exc()
    logmsg(f">>> _check_login -> {logged}")

    logmsg(f">>> 通行证区域文本: {user_aid_text(driver)!r}")

    logmsg(">>> 导出存储与全局状态")
    snap = {
        "check_login": logged,
        "user_aid": user_aid_text(driver),
        "storage": dump_storage(driver),
    }
    try:
        snap["globals"] = dump_globals(driver)
    except Exception as e:
        snap["globals"] = f"<{e}>"
    with open(os.path.join(OUT, "state_before.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    logmsg(f"    localStorage 键: {list((snap['storage'].get('localStorage') or {}).keys())[:20]}")
    logmsg(f"    cookie 数: {len(snap['storage'].get('cookies') or [])}")

    from selenium.webdriver.common.by import By

    # 关闭可能的引导弹窗
    try:
        g = driver.find_elements(By.CSS_SELECTOR, "div.guide-close-btn__x")
        if g:
            driver.execute_script("arguments[0].click();", g[0])
            time.sleep(1)
    except Exception:
        pass

    logmsg(">>> 多次尝试点击进入游戏（观察是否偶发成功）")
    for attempt in range(1, 6):
        btn = driver.find_elements(By.CSS_SELECTOR, "div.wel-card__content--start")
        logmsg(f"  --- 第 {attempt} 次：找到按钮 {len(btn)} 个 ---")
        if not btn:
            logmsg("      没有按钮，停止")
            break
        try:
            driver.execute_script("arguments[0].click();", btn[0])
        except Exception:
            traceback.print_exc()
            break

        # 观察 12 秒
        for t in (3, 6, 9, 12):
            time.sleep(3)
            d = dialogs(driver)
            gr = len(driver.find_elements(By.CSS_SELECTOR, ".game-player"))
            iq = len(driver.find_elements(By.CSS_SELECTOR, "[class*='waiting-in-queue']"))
            sq = len(driver.find_elements(
                By.CSS_SELECTOR, "[aria-labelledby*='请选择排队队列']"))
            logmsg(f"      [{t}s] game={gr} queue={iq} select={sq} dialog={d[:2]}")
            if gr or iq or sq:
                logmsg("      ✅ 出现游戏/排队状态！")
                break

        # 关掉错误弹窗再试下一次
        try:
            btns = driver.find_elements(
                By.CSS_SELECTOR, ".van-dialog__confirm, .van-dialog__footer button")
            if btns:
                driver.execute_script("arguments[0].click();", btns[0])
                logmsg("      已关闭弹窗，准备下一次")
                time.sleep(2)
        except Exception:
            pass

    logmsg(">>> 导出最终页面")
    try:
        with open(os.path.join(OUT, "final.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        driver.save_screenshot(os.path.join(OUT, "final.png"))
    except Exception:
        traceback.print_exc()

    final = {
        "url": getattr(driver, "current_url", None),
        "dialogs": dialogs(driver),
        "user_aid": user_aid_text(driver),
        "storage": dump_storage(driver),
    }
    with open(os.path.join(OUT, "state_after.json"), "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)

    logmsg(">>> 诊断完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
