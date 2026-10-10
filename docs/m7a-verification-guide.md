# 验证操作手册

目标：用 GitHub Actions 实测一件事——**登录态能不能被保存、搬运、恢复**。
这个答案决定整个方案能不能成立，所以在写任何业务代码之前先测它。

---

## 背景（为什么必须测这个）

上游 Docker 云游戏模式的登录方式与你的设想有出入，实测前的已知事实：

| 事实 | 来源 |
|---|---|
| 上游**只支持扫码登录**，不支持账号密码 | `module/game/cloud.py` 登录流程 |
| 米哈游密码登录接口**强制人机验证**（极验滑块，`risk_type: slide`） | [UIGF API 文档](https://uigf.org/zh/mihoyo-api-collection/hoyolab/login/password_passport.html) |
| `login_ticket` 只有 **30 分钟**有效 | [鉴权文档](https://uigf.org/zh/mihoyo-api-collection/other/authentication.html) |
| `stoken` / `ltoken` **改密码才变**（长期） | 同上 |
| 登录态落在 `Chrome --user-data-dir` 指向的 profile 目录 | `cloud.py` 第 330–334 行 |
| profile 路径 = `3rdparty/WebBrowser/UserProfile/<browser_type.capitalize()>` | `cloud.py` 第 202 行，默认 → `Integrated` |

推论：GitHub 托管 runner 是一次性虚拟机、无持久盘，**默认情况下每晚都要重新扫码**。
所以要测的就是：把 profile 目录整体搬走再搬回来，能不能免扫码。

---

## 第一步：抓取登录态（workflow 10）

打开：
https://github.com/YupenBob/March7thAssistant/actions/workflows/10.capture_login_state.yml

点 **Run workflow**（参数用默认值即可）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `login_wait_minutes` | 15 | 给你扫码的时间窗 |
| `keep_days` | 3 | 产物保留天数（**里面是账号凭据，别设太长**） |

### 跑起来之后你要做的

1. 等 1–3 分钟，让容器把镜像拉下来、Chrome 起来
2. 打开这个 run，看 **Summary** 页面 —— 里面会出现一个**扫码链接**
3. 用手机**米游社 APP** 扫它（链接页面本身就是二维码）
4. 如果 Summary 里没抓到链接，去 run 页面底部的 **Artifacts** 下载 `qrcode`，里面是 `qrcode_login.png`
5. 扫完在手机上点【确认登录】

> 如果 15 分钟内没扫上也没关系 —— workflow 仍会把当前状态打包出来，
> 只是那份状态是未登录的，没法用于第二步。

### 产物

- `qrcode` — 二维码图片（保留 1 天）
- `m7a-login-state` — **登录态压缩包**（这就是我们要的东西）

---

## 第二步：验证能否免扫码（workflow 11）

打开：
https://github.com/YupenBob/March7thAssistant/actions/workflows/11.verify_login_persistence.yml

点 **Run workflow**，`state_artifact` 填 `m7a-login-state`。

这个 workflow 的判定逻辑是**二元的**：

| 日志里出现 | 判定 | 含义 |
|---|---|---|
| `检测到登录成功` / `进入云游戏成功` | `logged_in` ✅ | **登录态可移植，cookie 路线成立** |
| `请使用手机米游社 APP 扫描` | `needs_qr` ❌ | 登录态没恢复，该时间点已失效或绑环境 |

它在 Summary 里还会打出 runner 的公网 IP，方便对比"换 IP 会不会掉登录"。

---

## 第三步：测寿命（这才是有价值的数据）

**在之后的第 1、3、7、15、30 天，重复跑第二步。**

每次都记录：距抓取过了几天 → 判定结果。因为 Actions 每次都是**全新 IP、全新机器**，
所以这个序列同时回答了"时间"和"换环境"两个变量。

判断标准：

- 能撑到 **30 天** → cookie 路线完全成立，每 30 天手动扫一次码即可，成本可接受
- 只撑 **1–3 天** → Actions 路线基本判死刑，应转向"自己一台常驻机器 + Docker"
- **每次都立刻失效** → 登录态被绑定在设备/环境上（device_fp 之类），需要换思路

---

## 安全提醒

- `m7a-login-state` 里含有**等于账号登录权限的凭据**。它存在 GitHub Artifacts 里，
  仓库是 public，但 **Actions 产物默认只有仓库协作者能下载**。即便如此：
  - 保留天数设短（默认 3 天）
  - 验证完及时删除：Actions run 页面 → Artifacts → 删除，或删掉整个 run
- **不要**把这个压缩包丢进仓库、提交、或贴到任何聊天工具里
- 我没有也不打算实现任何绕过滑块验证的代码

---

## 已知风险 / 可能的失败点

1. **容器可能起不来**：免费 runner 只有 2 核 / 7GB，headless Chrome + OCR 吃内存。
   若日志显示 `OOMKilled` 或 `tab crashed`，把 workflow 里的 `--shm-size=2g` 再调大
2. **首次运行可能不直接进二维码流程**：上游有"未登录则以有窗口模式重启"的分支。
   本 workflow 已把 `MARCH7TH_BROWSER_HEADLESS_RESTART_ON_NOT_LOGGED_IN=false` 关掉，
   强制走 headless 二维码分支
3. **登录成功后容器会开始跑完整任务**，默认行为，属预期。容器以 `MARCH7TH_AFTER_FINISH=Exit` 启动，
   跑完会自行退出
4. **workflow 10 是手动触发**，不会自动跑，也不会影响上游任何 workflow

---

## 这两个 workflow 与"合并冲突"的关系

只做了**新增文件**，没有修改上游任何文件：

```
.github/workflows/10.capture_login_state.yml   （新增）
.github/workflows/11.verify_login_persistence.yml （新增）
```

所以 `git merge upstream/main` 不会产生冲突。命名前缀 `10.` / `11.` 是刻意的——
上游已占用 `1.` 到 `9.`，我们用两位数避开，视觉上也好区分。

> ⚠️ 有个小问题要提醒：`10.` 和上游的 `1.` 前缀会一起排序，`10.` 会排到 `2.` 前面。
> 不影响功能，只是 Actions 列表里的顺序。若你觉得碍眼，后续可以改成 `90.` / `91.`。
