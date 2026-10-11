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

### 未打通的原因判断

测速域名是 `*-wssping.mhystatic.com`（**WSS = WebSocket Secure**），
而云游戏串流**强依赖 UDP/WebRTC**。

- SOCKS5 代理的 UDP 支持与 Chrome WebRTC 配合通常不稳定或不可用
- 即使信令（WSS/TCP）通过，媒体流（UDP）也大概率走不通

**这是代理方案的固有短板，不是配置问题。** 靠调参数难以解决。

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
