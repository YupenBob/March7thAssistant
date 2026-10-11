# 最终结论：March7thAssistant 云端自动化方案评估

调研与实测时间：2026-10-11
实测环境：GitHub Actions `ubuntu-latest`（Azure，出口 IP 见下）

---

## 一、结论摘要

| 目标 | 状态 | 证据 |
|---|---|---|
| Docker 云游戏模式 | ✅ 完全可行 | 上游官方支持，容器冒烟测试通过 |
| 登录态存到云端 | ✅ **已实现** | 16MB 加密 profile 存于 `m7a-state` 分支 |
| 登录态跨机器跨 IP 还原 | ✅ **已实现** | `_check_login -> True`，读到通行证 ID |
| 定时任务自动运行 | ✅ 已搭建 | 93 号 cron 北京 04:00 + 两层重试 |
| 走代理绕过 IP 限制 | ⚠️ **有推进但未成功** | 已进入节点测速阶段，仍止于串流 |
| **真正进入游戏刷日常** | ❌ **未达成** | 页面判「网络错误」，串流连接建立失败 |

**一句话**：登录这一关彻底解决了；代理能让流程前进到"节点测速"，
但最终建立游戏串流时仍然失败。

---

## 二、已彻底解决的问题

### 1. 登录态持久化（原本的最大未知数）

完整链路已跑通并验证：

```
本机小助手容器扫码登录
      ↓  34MB Chrome profile（Cookies / LocalStorage / IndexedDB 齐全）
AES-256-GCM 加密（PBKDF2 300k 轮）
      ↓  16MB 密文
GitHub Secret 存密钥 + m7a-state 分支存密文
      ↓
Azure runner 解密还原
      ↓
_check_login -> True    通行证ID：327459957    免费时长：600 分钟
```

**关键结论：mihoyo 的会话不绑定签发 IP，可以跨机器搬运。**
（此前我误判为"绑 IP"，那是因为 Chrome v20 加密导致 cookie 从未被正确解密。）

### 2. 顺带确认的事实

- 上游 Docker 部署**只支持云游戏模式**，登录**只支持扫码**
- 密码登录被极验滑块挡住，Actions 数据中心 IP 基本必触发 → 此路不通
- 二维码**每约 2 分钟轮换一次**（实测 7 次刷新间隔均为 121-122 秒）
- 普通 Chrome 的 cookie 搬不走：DPAPI 绑定本机账户，Chrome 127+ 更是
  v20 App-Bound Encryption（这是防窃取的设计，不应绕过）

---

## 三、为什么最终走不通：进入游戏失败

### 现象

点击「进入游戏」后，页面**根本进不了排队流程**，直接弹错误框：

```
van-dialog__content → 网络错误 / 当前网络异常,请稍后重试
```

DOM 里**完全没有排队相关元素**（`queue` / `waiting-in-queue` / `排队` 全为 0），
所以应用 `_wait_in_queue` 等的三个状态（cloud.py:678）永远不出现，
10 秒后抛空 `TimeoutException`。

### 关键证据：登录成功，但进不去游戏

截图与 DOM 都显示右栏**已登录**：

```
畅玩卡
星云币时长：0 分钟
免费时长：600 分钟
进入游戏
通行证ID：327459957
```

同时中央是错误弹窗。**即：身份认证通过，但调度/串流接口被拒。**

### 这是确定性的，不是抖动

| 运行 | 登录 | 进入游戏 |
|---|---|---|
| 诊断 #1 | ✅ | ❌ 网络错误弹窗 |
| 诊断 #2 | ✅ | ❌（卡页面加载） |
| 诊断 #3 | ✅ | ❌ 网络错误 ×6 |
| 93 号正式运行（3 次重试） | ✅ ×3 | ❌ ×3（耗时一致，约 26 秒） |

三次重试耗时完全相同 → **不是偶发网络抖动**，重试无法解决。

### 原因判断

`sr.mihoyo.com` / `user.mihoyo.com` 等**网页与接口可达**（HTTP 200），
但**云游戏调度/串流接口**对 Azure 数据中心 IP 拒绝服务。
属于地域+IP 类型限制。

---

## 四、当前可用的产出

### Workflow

| 编号 | 名称 | 状态 |
|---|---|---|
| 88 | 容器冒烟测试 | ✅ 实测通过 |
| 89 | 端点连通性探测 | ✅ |
| 90 | 登录态抓取（扫码） | ✅ 实测成功 |
| 91 | 登录态可移植性验证 | ✅ 实测 `_check_login -> True` |
| 92 | profile 云端验证 | 备用 |
| 93 | **每日定时运行**（含 3 次重试） | ⚠️ 登录成功、进游戏失败 |
| 94 | 进入游戏失败诊断 | ✅ 定位到根因 |
| 95 | 每日任务兜底补跑 | ✅ 判定逻辑 7/7 单测通过 |

### 脚本

| 文件 | 作用 |
|---|---|
| `m7a_profile_crypto.py` | AES-256-GCM 加解密（往返测试通过） |
| `catchup_verdict.py` | 补跑判定（7 个用例含负向测试全过） |
| `diag_enter.py` | 逐阶段 DOM/截图诊断 |
| `qr_popup.py` | 二维码实时弹窗（解决 2 分钟轮换的投递延迟） |
| `probe_cloudgame_api.py` | 调度后端可达性探测 |

### 工具（本机）

- `pack_profile.py` — 打包加密 profile（回环自检）
- `create_secret.py` — 生成并写入 GitHub Secret
- `check_run_blocks.py` — workflow 内置 bash 的结构检查器
- `test_catchup_verdict.py` — 判定逻辑单测

---

## 五、建议

### 若坚持用 GitHub Actions

**没有可行解**。Azure IP 进不去云游戏，这一点无法通过代码绕过。

### 推荐方案：自建 runner 或本地常驻容器

装在家里/常开的机器上，一次登录长期有效：

```bash
docker run -d --restart unless-stopped --shm-size=2g \
  -v /path/m7a/UserProfile:/m7a/3rdparty/WebBrowser/UserProfile \
  -v /path/m7a/logs:/m7a/logs \
  -e MARCH7TH_AFTER_FINISH=Loop \
  ghcr.io/moesnow/march7thassistant:latest
```

上游默认行为：**每天凌晨 4:00 自动执行完整运行**，容器常驻循环等待。
`UserProfile` 挂载在本地盘 → 登录一次长期有效，无需任何搬运。

优势：住宅 IP、无跨境链路问题、无 profile 加密搬运、无 artifact 开销。

### 中间方案

自建 runner（self-hosted）注册到 GitHub，仍由 Actions 调度，
但执行在你的机器上 —— 兼得"云端调度"与"住宅 IP"。

---

## 六、安全提醒

- `m7a-state` 分支存的是**加密后**的 profile，密钥在 GitHub Secret（不可读回）
- 但 GitHub 托管 runner 是 GitHub 的机器，执行期间明文 profile 会存在于其磁盘
- 若要更强隔离，用自建 runner
- 本地 `SECRET_M7A_PROFILE_KEY.txt` 是口令备份，**不要提交、不要外传**

---

## 七、过程中修正过的自身误判（供后人参考）

这些问题**靠推理全错了，只有实测才对**：

| 误判 | 真相 |
|---|---|
| 云游戏站点是 Vue SPA，静态 HTML 无 selector → 以为被拦截 | selector 由 JS 运行时注入，静态页面本就没有 |
| `wc -l` 输出 0 → 以为文件为空 | 单行 HTML 无换行符 |
| cookie 长度 27 → 以为解密成功 | 那是 `<unsupported prefix b'v20'>` 占位符的长度 |
| 日志里 `--user` 只是权限问题 | `config.py:400` 的 `mkstemp` 在 `try` 外，异常直接打死进程 |
| `git push \| tail` 报"已发布" | 管道吞掉退出码，失败被当成成功 |
| tar 打出 89KB → 以为没登录 | 容器 root 属主 + 700 权限，runner 读不进去 |
| cookie 绑签发 IP | Chrome v20 加密导致从未正确解密，结论无效 |
