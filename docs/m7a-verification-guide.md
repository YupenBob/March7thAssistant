# 验证操作手册

目标：把「Docker 云游戏 + GitHub Actions 定时刷日常」真正跑通。
在写业务代码之前，先解决唯一的硬骨头：**登录态的持久化**。

---

## 一、前车之鉴：`.github/workflows/march7th_daily.yml`（已废弃）

你的 fork 里曾有一个 `March7th Cloud Run` workflow，从 **2026-04-17 跑到 2026-10-10**，
cron `0 4 * * *`（= 北京时间 12:00），**107 次运行、全部失败**。

它已被删除，但 run 历史和源码可以恢复：

```bash
# workflow 元信息：state=deleted, 107 runs
curl -s ".../actions/workflows/262162188"
# 源码仍在旧 commit 里
curl -s ".../contents/.github/workflows/march7th_daily.yml?ref=3d385164"
```

### 它的失败步骤

```
[success] Prepare config
[success] Run March7th Assistant      ← 任务本身"成功"
[failure] Upload updated profile      ← 卡在这里，然后整个 job 判失败
```

### 三个叠加的致命错误

**错误 1：用了不存在的 CLI 参数**

```yaml
python main.py --task main || true
```

`main.py` 只有**位置参数**（`daily` / `power` / `currencywars` …）和 `-l/--list`，
**没有 `--task`**。这行一定报错，但被 `|| true` 吞掉了，
所以那步显示 success —— **这个 workflow 从头到尾没真正跑过一次日常**。

**错误 2：`git add -A` 加不到任何东西**

`.gitignore` 把这些全忽略了：

| 行 | 规则 |
|---|---|
| 185 | `/*config.yaml` |
| 188 | `/logs` |
| 192 | `/settings/*` |
| 205 | `/3rdparty/WebBrowser` |

而 workflow 的 `sparse-checkout` 恰好只拉 `config.yaml` 和 `3rdparty`。
→ 暂存区为空 → `git commit` 无内容可提交返回非零 → job 失败。

佐证：commit 历史里**一条 `Update browser profile` 都没有**。

**错误 3：方向本身是错的（最重要）**

它试图把**浏览器 profile 提交进仓库**。就算前两个错误修好，这条路也不能走：

- profile 含**等价于账号登录权限的凭据**，且仓库是 **public**
- Chrome profile 体积在数百 MB 量级，塞进 git 会迅速撑爆仓库
- 凭据进入 git 历史后**极难彻底清除**

**结论：这个方案从来没有持久化过登录态。** 每天都是"跑一遍、丢掉、明天重来"。
证据是 `Run March7th Assistant` 里必然走二维码分支、然后卡到超时。

---

## 二、因此，本方案与老方案的差异

| | 老方案（已废弃） | 本方案 |
|---|---|---|
| 登录态持久化 | git 提交 profile ❌ 从未生效 | Actions **artifact** 搬运 ✅ |
| 凭据是否进仓库 | 会进 public 仓库 ❌ | 不进仓库 ✅ |
| 体积限制 | git 硬上限 ❌ | artifact 上限宽松得多 ✅ |
| 容器运行方式 | `container:` 托管 | `docker run` 显式控制 ✅ |
| 登录超时 | 默认 20 分钟，与等待窗错配 ❌ | 注入 `config.yaml` 拉长到 25 分钟 ✅ |
| 挂载目录 | 工作区（会被 root 污染）❌ | `/tmp` ✅ |
| 失败可观测性 | `\|\| true` 吞掉错误 ❌ | 按要求精确 grep 日志判定 ✅ |

---

## 三、执行步骤

### 第一步：抓取登录态

打开：
https://github.com/YupenBob/March7thAssistant/actions/workflows/10.capture_login_state.yml

点 **Run workflow**（默认参数）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `login_wait_minutes` | 18 | 扫码时间窗（硬上限 22 分钟） |
| `keep_days` | 3 | 产物保留天数（**内含凭据，别设长**） |

**跑起来后：**

1. 等 1–3 分钟（拉镜像 + 起 Chrome）
2. 打开这次 run 的 **Summary** 页面 → 会出现**扫码链接**
3. 用手机**米游社 APP** 扫它
4. Summary 没抓到链接就去 run 页底部下载 `qrcode` 产物（图片）
5. 扫完在手机上点【确认登录】

**产物：** `qrcode`（图片，1 天）、`m7a-login-state`（**登录态压缩包**）

### 第二步：验证能否免扫码

打开：
https://github.com/YupenBob/March7thAssistant/actions/workflows/11.verify_login_persistence.yml

`state_artifact` 填 `m7a-login-state`。判定是二元的：

| 日志出现 | 判定 | 含义 |
|---|---|---|
| `检测到登录成功` / `进入云游戏成功` | `logged_in` ✅ | **登录态可移植，方案成立** |
| `请使用手机米游社 APP 扫描` | `needs_qr` ❌ | 该时间点已失效 |

### 第三步：测寿命（最有价值的数据）

**在第 1、3、7、15、30 天重复第二步**，记录：距抓取几天 → 判定结果。
因为 Actions 每次都是**全新机器 + 全新 IP**，这个序列同时回答了"时间"和"换环境"两个变量。

| 结果 | 结论 |
|---|---|
| 撑到 **30 天** | cookie 路线成立，每 30 天手动扫一次码即可 |
| 只撑 **1–3 天** | Actions 路线判死刑，转"常驻机器 + Docker" |
| **每次立刻失效** | 登录态绑设备指纹，需换思路 |

---

## 四、安全提醒

- `m7a-login-state` 含**等价于账号登录权限的凭据**。虽在 public 仓库，但 Actions 产物
  默认仅协作者可下载。仍需：保留天数设短、用完即删（run 页 → Artifacts → 删除）
- **绝不要**把它提交进仓库、或贴到任何聊天工具
- **不要**走老方案那条"profile 提交进 git"的路
- 我没有也不打算实现任何绕过滑块验证的代码

---

## 五、已做的正确性验证

| 项 | 方法 | 结果 |
|---|---|---|
| workflow 语法 | `actionlint` v1.7.12 | 通过 |
| YAML 解析 | PyYAML | 通过 |
| 5 个环境变量名 | 与 `config.py` 的 `_ENV_OVERRIDE_MAP` 逐个比对 | 全部存在 |
| 挂载路径 | 与 `Dockerfile` WORKDIR / `config/__init__.py` 常量比对 | 一致 |
| 配置注入正则 | 对真实 `config.example.yaml` 跑替换 | 各命中 1 次，行数不变，值正确 |
| profile 目录名 | 由 `cloud.py:202` 的 `browser_type.capitalize()` 推导 | `Integrated`（非 `Chrome`） |
| 日志匹配串 | 与 `cloud.py` 中的 `log_info` 原文比对 | 逐字一致 |
| 老方案根因 | run 历史 + 源码 + `.gitignore` 交叉验证 | 三处错误确认 |

---

## 六、仍未验证（必须靠真跑）

1. **`Run March7th Assistant` 那步到底会不会走二维码分支** —— 取决于 profile 是否有效
2. **登录态能活多久** —— 这正是整个方案要测的核心未知数
3. **免费 runner（2 核 / 7GB）够不够** —— headless Chrome + OCR 吃内存，
   若 `OOMKilled` 就把 `--shm-size=2g` 调大
4. **`config.yaml` 会不会被程序改写** —— workflow 里已加哈希检测，会直接报出来
