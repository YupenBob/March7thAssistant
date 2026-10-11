## 八、代理方案实测结果（2026-10-11 追加）

针对"用代理换出口 IP"的设想做了完整实测，结论是**有实质推进但未打通**。

### 代理配置

- 类型：VLESS + REALITY（xtls-rprx-vision，sni=www.amazon.com）
- 出口：`146.235.197.103`（直连时是 Azure 的 `20.127.247.148`）
- 实现：runner 上跑 xray-core，通过**上游已有的 `browser_launch_argument`**
  注入 Chrome 的 `--proxy-server=socks5://127.0.0.1:10809`，**未改一行上游代码**
- 代理链接存 GitHub Secret `M7A_PROXY_LINK`，不落仓库

### 已验证有效

代理确实生效并被页面使用（不是没走代理）：

```
Chrome 启动参数：... '--moode=headless=new', '--mute-audio',
  '--proxy-server=socks5://127.0.0.1:10809', '--proxy-bypass-list=<-loopback>'
```

xray 日志显示所有流量都经代理，且**页面成功加载了登录态**：

```
通行证ID：327459957    免费时长：600 分钟    # 走代理也是已登录
```

### 关键进展：流程比直连走得更远

**直连时**：点击进入游戏 → 立刻网络错误，DOM 里连排队元素都没有。

**走代理时**：点击后页面进入了**节点测速**阶段（xray 日志佐证）：

```
05:00:45  cg-hkrpg-api.mihoyo.com              ← 调度接口
05:00:47  cg-t-ensfz6-wssping.mhystatic.com    ← 五个节点测速
05:00:47  cg-t-tybj2-wssping.mhystatic.com
05:00:47  cg-t-qscq1-wssping.mhystatic.com
05:00:47  cg-t-tyyz1-wssping.mhystatic.com
05:00:47  cg-t-mhywh1-wssping.mhystatic.com
```

页面文本也印证了这个阶段：

```
点击后 3 秒：正在检测网络环境
点击后 8 秒：网络错误 / 当前网络异常,请稍后重试
```

**所以代理确实绕过了"数据中心 IP 被拒"这一层**，但止步于**建立串流会话**。

### 未打通的原因判断（**已被第九节推翻，保留作为排查记录**）

当时根据测速域名 `*-wssping.mhystatic.com` 推测是 UDP/WebRTC 走不通。
**这个判断是错的** —— 后续从 bundle 里读出了真正的机制（见第九节），
失败发生在**节点测速阶段**，根本还没走到建立串流那一步，
与 UDP 无关。

### 若要继续尝试的可能方向（未验证）

1. **TUN 模式代理**（如 sing-box 的 tun inbound）：在 runner 上建虚拟网卡，
   把 UDP 一并接管。但 Actions runner 需要 `NET_ADMIN` 权限，
   且需处理 `/dev/net/tun`，可行性未知。
2. **换支持 UDP 的代理协议**：如 Hysteria2 / TUIC（基于 QUIC，原生支持 UDP）。
3. **在真实出口机器上跑**：把容器放到境外 VPS（如那台 oracle2 所在机器），
   让网络路径完全不经代理转发。

### 结论

**代理方案让流程显著前进（越过 IP 拒绝，进入节点测速），
但因串流依赖 UDP，最终仍未打通。**

如果要落地，**最省事的仍是自建 runner / 本地常驻容器**：
住宅 IP、原生 UDP、无跨境转发，上面所有问题一次性消失。

---

## 九、根因修正 + 韩国节点实测（2026-10-11 追加）

### 真正的根因：SDK 的测速时限，不是 UDP

从 `cg-sdk` bundle 读出的机制：

```js
WebSocketRttTester.testRttMs(20, 1000)   // 20 次往返必须在 1000ms 内
  -> 超时则该节点 rtt = -1
  -> 页面过滤：ping_results = n.filter(e => e.rtt === undefined || Number(e.rtt) >= 0)
  -> ping_results 为空时只打一条 StartPingMayError 日志，然后照样发给服务端
  -> getNodesInfo 带空结果 -> 服务端 {"retcode":-110005,"message":"游戏服务器连接中断"}
  -> 页面弹通用「网络错误」，paasDispatch 永不发出
```

关键点：**判定条件是「20 次 WebSocket 往返总耗时 < 1000ms」**，
即单次 RTT 必须压到 **50ms 以内**（还得留抖动余量）。

**只要 ≥1 个节点达标，`ping_results` 就非空**，流程即可继续。
runner 上是 0 个达标，所以必然失败。

### 四条链路实测（同一套判据、同一批 5 个测速节点）

| 链路 | 达标 | 完成 | 平均 RTT | 单节点总耗时 |
|---|---|---|---|---|
| **本机直连**（北京移动，住宅 IP） | **1/5** | 5/5 | **58.0ms** | 643 ~ 3801ms |
| 本机 → 韩国节点（全局模式） | **0/5** | 5/5 | 199.0ms | 4097 ~ 6205ms |
| GHA runner 直连（Azure US） | **0/5** | 5/5 | 241 ~ 387ms | 5901 ~ 10700ms |
| GHA runner → 韩国节点 | **0/5** | 5/5 | 248.0ms | 6040 ~ 7948ms |

节点：`yys-t-enszjk6` / `cg-t-mhyfs1` / `cg-t-qscs1` / `cg-t-qshf1` / `cg-t-ensgy15`

### 结论：韩国节点对这个问题**没有任何帮助，本机用反而慢 3.4 倍**

1. **runner 上直连 vs 挂韩国节点：几乎一样**（0/5，248ms vs 241~387ms）。
   GitHub 的 US 出口到国内测速节点本来就是 ~250ms，
   绕道首尔（Alibaba，`47.80.23.1`）后 US→首尔→国内**还是 ~250ms**。
2. **本机上挂韩国节点反而更差**：直连 58ms → 经首尔 199ms。
   本地代理多绕一圈国际链路，是纯粹的负收益。
3. 所以"海外 IP 能玩"这个经验**不适用于云游戏测速这一关** ——
   云游戏看的是 RTT，而代理必然增加 RTT。

### 方法论提醒（踩过的坑）

- nekoray 的 `active_routing: "Default"` 预设会**绕过 CN**。
  用它测国内测速节点，流量会被判 direct，测出来的其实是直连数据。
  **要测真链路必须用全局模式**（`route.final = "proxy"`，无 CN 绕过规则）。
- sing-box 配置里 DNS 若设成 `detour: proxy`，解析代理服务器自己的域名
  会经代理递归，表现为疯狂重连。必须用直连 DNS 做 bootstrap。

### 筛选工具

- `check_proxy_latency.py`：批量测 `socks5://host:port` 到 5 个测速节点的 RTT
- `104.screen_vless.yml`：端到端测 `vless://` 链接（runner 上起 xray 再测）
