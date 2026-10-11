"""诊断脚本：进入云游戏后停在哪个状态。

背景：应用在 _wait_in_queue 里等三个状态之一（game_running / in_queue /
select_queue），10 秒都没出现就抛空 TimeoutException。需要看清真实 DOM。

本脚本复用应用自身的模块（所以浏览器参数、profile、配置与正式运行完全一致），
复现「进入游戏」的点击，然后把页面结构与截图 dump 出来。

放在容器里以 `python diag_enter.py` 运行。
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

OUT = "/m7a/logs/diag"
os.makedirs(OUT, exist_ok=True)


def dump_state(driver, tag):
    """把当前页面的关键状态记录下来。"""
    info = {}
    try:
        info["url"] = driver.current_url
    except Exception as e:
        info["url"] = f"<{e}>"
    try:
        info["title"] = driver.title
    except Exception as e:
        info["title"] = f"<{e}>"

    # 应用关心的三个 selector
    probes = {
        "game_running": ".game-player",
        "in_queue": "[class*='waiting-in-queue']",
        "select_queue": "[aria-labelledby*='请选择排队队列']",
        "enter_btn": "div.wel-card__content--start",
        "user_aid": "div.user-aid.wel-card__aid",
        "login_iframe": "#mihoyo-login-platform-iframe",
    }
    found = {}
    for name, sel in probes.items():
        try:
            els = driver.find_elements("css selector", sel)
            found[name] = len(els)
        except Exception as e:
            found[name] = f"<{e}>"
    info["selectors"] = found

    # 收集页面可见文本，判断真实状态
    try:
        text = driver.execute_script(
            "return (document.body && document.body.innerText || '').slice(0, 3000);")
        info["body_text"] = text
    except Exception as e:
        info["body_text"] = f"<{e}>"

    # iframe 列表
    try:
        info["iframes"] = [f.get_attribute("id") or f.get_attribute("src")
                           for f in driver.find_elements("tag name", "iframe")]
    except Exception as e:
        info["iframes"] = f"<{e}>"

    with open(f"{OUT}/state_{tag}.json", "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    try:
        with open(f"{OUT}/page_{tag}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception:
        pass
    try:
        driver.save_screenshot(f"{OUT}/shot_{tag}.png")
    except Exception:
        pass

    print(f"\n===== [{tag}] =====")
    print("url      :", info.get("url"))
    print("title    :", info.get("title"))
    print("selectors:", json.dumps(found, ensure_ascii=False))
    print("iframes  :", json.dumps(info.get("iframes"), ensure_ascii=False)[:300])
    print("--- body text (前 1200 字) ---")
    print((info.get("body_text") or "")[:1200])
    print("=" * 60, flush=True)


def main():
    print(">>> 启动云游戏控制器", flush=True)
    controller = CloudGameController(cfg, log)

    ok = controller.start_game_process()
    print(f">>> start_game_process -> {ok}", flush=True)
    if not ok:
        return 1

    driver = controller.driver
    dump_state(driver, "01_after_start")

    # 复现应用启动时会做的关键动作，否则页面状态与正式运行不可比：
    #   _load_initial_local_storage() 注入云游戏所需的 localStorage
    print("\n>>> 注入 initial_local_storage", flush=True)
    try:
        loaded = controller._load_initial_local_storage()
        print(f">>> _load_initial_local_storage -> {loaded}", flush=True)
    except Exception:
        traceback.print_exc()
    dump_state(driver, "02_after_localstorage")

    # 用应用自己的判定逻辑问"现在算登录了吗"
    print("\n>>> 调用应用自身的登录判定 _check_login()", flush=True)
    try:
        logged = controller._check_login(timeout=15)
        print(f">>> _check_login -> {logged}", flush=True)
    except Exception:
        traceback.print_exc()
        logged = None
    dump_state(driver, "03_login_check")

    # 刷新一次再看（页面可能异步初始化）
    print("\n>>> 刷新页面后再判定一次", flush=True)
    try:
        driver.refresh()
        time.sleep(8)
    except Exception:
        traceback.print_exc()
    dump_state(driver, "04_after_refresh")
    try:
        logged2 = controller._check_login(timeout=15)
        print(f">>> 刷新后 _check_login -> {logged2}", flush=True)
    except Exception:
        traceback.print_exc()
    dump_state(driver, "05_login_check2")

    # 如果登录判定为真，才继续复现进入游戏的点击
    if not logged and not logged2:
        print("\n>>> 判定为未登录，跳过进入游戏复现（避免误判）", flush=True)
        print(">>> 诊断完成，产物在 /m7a/logs/diag", flush=True)
        return 0

    try:
        from selenium.webdriver.common.by import By
        guide = driver.find_elements(By.CSS_SELECTOR, "div.guide-close-btn__x")
        if guide:
            print(">>> 关闭引导弹窗", flush=True)
            driver.execute_script("arguments[0].click();", guide[0])
            time.sleep(1)
        btn = driver.find_elements(By.CSS_SELECTOR, "div.wel-card__content--start")
        print(f">>> 找到进入按钮 {len(btn)} 个", flush=True)
        if btn:
            driver.execute_script("arguments[0].click();", btn[0])
            print(">>> 已点击进入游戏", flush=True)
            for i in (3, 8, 15, 25):
                time.sleep(3 if i == 3 else 5)
                dump_state(driver, f"06_after_click_{i}s")
        else:
            dump_state(driver, "06_no_button")
    except Exception:
        traceback.print_exc()
        dump_state(driver, "99_exception")

    print("\n>>> 诊断完成，产物在 /m7a/logs/diag", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
