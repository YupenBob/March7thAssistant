"""二维码实时弹窗 —— 轮询仓库分支，一有新码就立刻置顶弹出。

解决的问题：
  之前二维码要经过 GitHub 页面 -> artifact 打包 -> 下载 -> 解压，
  延迟 1~2 分钟，而米游社的码每 2 分钟就轮换一次，等于永远慢一步。
  本程序直接读仓库里的纯文本链接并【本地生成】二维码图片，
  延迟降到一次 HTTP 请求。

用法：
  python qr_popup.py                 # 默认轮询仓库
  python qr_popup.py --interval 3    # 自定义轮询间隔（秒）
  python qr_popup.py --once          # 只弹一次，不持续轮询
"""
import argparse
import io
import sys
import time
import tkinter as tk
import webbrowser
from datetime import datetime

import qrcode
import requests
from PIL import Image, ImageTk

DEFAULT_URL = ("https://raw.githubusercontent.com/YupenBob/March7thAssistant/"
               "m7a-qr/qrcode.txt")
UA = {"User-Agent": "m7a-qr-popup/1.0",
      "Cache-Control": "no-cache",
      "Pragma": "no-cache"}
PATTERN = "login-platform"


class QrPopup:
    def __init__(self, url, interval, once=False, size=420):
        self.url = url
        self.interval = interval
        self.once = once
        self.size = size
        self.last_link = None
        self.seen_count = 0
        self.error_streak = 0
        self.session = requests.Session()

        self.root = tk.Tk()
        self.root.title("M7A 二维码实时弹窗")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#1e1e1e")
        self.root.geometry("+40+40")

        self._build_ui()
        self.root.after(100, self.poll)

    # ---------- 界面 ----------
    def _build_ui(self):
        pad = {"padx": 12}

        self.status = tk.Label(
            self.root, text="等待二维码…",
            font=("Microsoft YaHei", 12, "bold"),
            fg="#dddddd", bg="#1e1e1e")
        self.status.pack(pady=(12, 4), **pad)

        self.sub = tk.Label(
            self.root, text="", font=("Microsoft YaHei", 9),
            fg="#999999", bg="#1e1e1e", justify="center")
        self.sub.pack(pady=(0, 8), **pad)

        self.img_label = tk.Label(self.root, bg="#1e1e1e")
        self.img_label.pack(**pad)

        self.link_label = tk.Label(
            self.root, text="", font=("Consolas", 8),
            fg="#7fb3ff", bg="#1e1e1e", wraplength=420, justify="center",
            cursor="hand2")
        self.link_label.pack(pady=(8, 4), **pad)
        self.link_label.bind("<Button-1>", self._open_link)

        btns = tk.Frame(self.root, bg="#1e1e1e")
        btns.pack(pady=(4, 12))
        tk.Button(btns, text="打开链接", command=self._open_link,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=4)
        tk.Button(btns, text="立即刷新", command=self._force_poll,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=4)
        tk.Button(btns, text="置顶开关", command=self._toggle_top,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=4)

        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    # ---------- 交互 ----------
    def _open_link(self, _evt=None):
        if self.last_link:
            webbrowser.open(self.last_link)

    def _toggle_top(self):
        cur = bool(self.root.attributes("-topmost"))
        self.root.attributes("-topmost", not cur)

    def _force_poll(self):
        self.poll()

    # ---------- 轮询 ----------
    def _fetch(self):
        # 加时间戳绕过 CDN 缓存，否则 raw.githubusercontent 可能返回旧内容
        bust = f"{self.url}{'&' if '?' in self.url else '?'}t={int(time.time())}"
        r = self.session.get(bust, headers=UA, timeout=12)
        r.raise_for_status()
        return r.text.strip()

    def poll(self):
        try:
            text = self._fetch()
            self.error_streak = 0
            if PATTERN in text:
                if text != self.last_link:
                    self._on_new_link(text)
                else:
                    age = int(time.time() - self.last_seen_ts)
                    self.sub.config(
                        text=f"已是当前码 · {age}s 前获取 · 每 {self.interval}s 刷新"
                             f" · 累计 {self.seen_count} 个")
            else:
                self.status.config(text="等待二维码生成…", fg="#dddddd")
                self.sub.config(text=f"分支文件暂无有效链接 · 已轮询 "
                                     f"{int(time.time() - self.start_ts)}s")
        except Exception as e:
            self.error_streak += 1
            if self.error_streak in (1, 5, 20):
                self.status.config(text="轮询异常（继续重试）", fg="#ffbb55")
                self.sub.config(text=f"{type(e).__name__}: {str(e)[:90]}")

        if self.once and self.last_link:
            return
        self.root.after(self.interval * 1000, self.poll)
        # 首次标记时间
        if not hasattr(self, "start_ts"):
            self.start_ts = time.time()

    def _on_new_link(self, link):
        self.last_link = link
        self.last_seen_ts = time.time()
        self.seen_count += 1

        # 本地生成二维码，不依赖远端图片
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10, border=2)
        qr.add_data(link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black",
                            back_color="white").convert("RGB")
        img = img.resize((self.size, self.size), Image.NEAREST)

        self.photo = ImageTk.PhotoImage(img)
        self.img_label.config(image=self.photo)

        self.status.config(text="✅ 新二维码 —— 立即用米游社 APP 扫码", fg="#7CFC00")
        self.sub.config(
            text=f"第 {self.seen_count} 个 · {datetime.now():%H:%M:%S} 生成"
                 f" · 约 2 分钟后轮换")
        self.link_label.config(text=link[:110] + ("…" if len(link) > 110 else ""))

        if not hasattr(self, "start_ts"):
            self.start_ts = time.time()

        # 抢注意力：响铃 + 置顶 + 轻微前移
        try:
            self.root.bell()
        except Exception:
            pass
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.deiconify()

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--interval", type=int, default=3)
    ap.add_argument("--size", type=int, default=420)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if args.interval < 1:
        print("间隔至少 1 秒")
        return 2

    app = QrPopup(args.url, args.interval, once=args.once, size=args.size)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
