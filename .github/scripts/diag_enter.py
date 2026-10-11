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

    dump_state(controller.driver, "01_after_start")

    # 复现 _click_enter_game 的逻辑，但每一步都记录状态
    driver = controller.driver
    try:
        from selenium.webdriver.common.by import By
        guide = driver.find_elements(By.CSS_SELECTOR, "div.guide-close-btn__x")
        if guide:
            print(">>> 关闭引导弹窗", flush=True)
            driver.execute_script("arguments[0].click();", guide[0])
            time.sleep(1)
        dump_state(driver, "02_after_guide_close")

        btn = driver.find_elements(By.CSS_SELECTOR, "div.wel-card__content--start")
        print(f">>> 找到进入按钮 {len(btn)} 个", flush=True)
        if btn:
            driver.execute_script("arguments[0].click();", btn[0])
            print(">>> 已点击进入游戏", flush=True)
            for i in (2, 5, 10, 20):
                time.sleep(i if i == 2 else i - 2)
                dump_state(driver, f"03_after_click_{i}s")
        else:
            dump_state(driver, "03_no_button")
    except Exception:
        traceback.print_exc()
        dump_state(driver, "99_exception")

    print("\n>>> 诊断完成，产物在 /m7a/logs/diag", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
