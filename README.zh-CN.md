# mitmweb-mcp

**一个界面。你在看,你的 AI 也在读同一份。**

[English](README.md) · [简体中文](README.zh-CN.md)

一个 [MCP](https://modelcontextprotocol.io) 服务器,直接读**你正在运行的那个 mitmweb**
—— 同一份流量、同一个窗口、同一个时刻。

你照常开着 mitmweb 界面、自己操作浏览器。AI 看到的和你看到的**完全是同一份数据**,
并且能搜索它、对比它、重放它,最后把它变成一个能跑的爬虫脚本。

```
                     ┌──────────────► 浏览器 UI        (你看)
  浏览器 ──代理───► mitmweb (:8080 代理 / :8081 API+UI)
                     └──────────────► mitmweb-mcp ───► AI 助手
                          同一个进程 · 同一份流量
```

---

## 为什么要有它

其他的 mitmproxy MCP 都会**自己起一个无头代理**。于是你有了两份互不相干的抓包会话:
你在看的那份,和 AI 在看的那份。要把它们对上,要么把两个代理串起来(链路变长、
两跳 TLS、延迟翻倍),要么接受两边数据对不上。

mitmweb-mcp 走了另一条路。关键洞察是:**mitmweb 的前端本身就是个 HTTP 客户端** ——
你在浏览器里看到的流量列表来自 `GET /flows.json`,点开一条看正文调的是
`GET /flows/<id>/response/content.data`。这套 REST API 一直都在,只是没人拿它当 API 用。

所以本服务器**不起任何代理**,它就是你已有的那个 mitmweb 的第二个平行客户端。
三个性质直接从架构里掉出来,不需要任何同步机制去保证:

1. **UI 显示的 == AI 读到的**。同一个进程、同一份内存里的流量列表。
2. **零额外延迟**。请求路径里没有多插入任何东西,你的浏览体验和之前一模一样。
3. **MCP 挂了不影响抓包**。它只是个读端。

## 安全模型:只读 + 只追加

十个工具里有九个是纯 `GET`,不可能改动你的会话。`replay_flow` 也不修改已有流量 ——
它是重新发一次请求,结果**追加**成一条新流量。最坏情况是列表里多几行,
你正在看的东西不会悄悄变样或消失。

**刻意不提供 `clear_flows`**。清空会话是不可逆的破坏性操作,在 UI 里点一下就行,
没有任何理由把这个按钮交给 AI。

---

## 安装

需要 **Python ≥ 3.10**,以及 PATH 里有 [mitmproxy](https://mitmproxy.org/) ≥ 10。

```bash
pip install mitmweb-mcp
```

或从源码安装:

```bash
git clone https://github.com/numb747/mitmweb-mcp
cd mitmweb-mcp
pip install -e .
```

## 配置

### 1. 用固定 token 启动 mitmweb

mitmweb 每次启动都会生成一个**随机**的 web 密码,本服务器无从得知。所以要把它固定下来:

```bash
mitmweb --listen-port 8080 --set web_password=YOUR_SECRET_TOKEN
```

- `8080` 是**代理**端口 —— 浏览器指向这里(重放也穿这里)
- `8081` 是 **UI + API** —— 你看这个,MCP 也读这个

HTTPS 需要先信任 mitmproxy 的 CA 证书(只做一次)。**Firefox** 走 <http://mitm.it>
或导入 `~/.mitmproxy/mitmproxy-ca-cert.pem` 都行,**但 Chrome 在 Linux 上不吃这套** ——
它读的是共享 NSS 库,必须用 `certutil`:

```bash
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n mitmproxy \
         -i ~/.mitmproxy/mitmproxy-ca-cert.pem
```

想让界面一开始就清爽:`--set view_filter='!~a & !~d googleapis.com'`。注意
`view_filter` 同样作用于 `/flows.json`,所以它收窄的不只是 UI,本服务器看到的也一样。

[`contrib/`](contrib/) 里有个 mitmweb addon,能一并拉起指向该代理的专用 Chrome,
外加 `mitm-start` / `mitm-stop` 脚本 —— 可选,但能把整套流程变成一条命令。

### 2. 注册 MCP 服务器

**Claude Code:**

```bash
claude mcp add mitmweb -s user \
  -e MITMWEB_URL=http://127.0.0.1:8081 \
  -e MITMWEB_TOKEN=YOUR_SECRET_TOKEN \
  -e MITMPROXY_PORT=8080 \
  -- mitmweb-mcp
```

**Claude Desktop**(`claude_desktop_config.json`)**或任何 MCP 客户端:**

```json
{
  "mcpServers": {
    "mitmweb": {
      "command": "mitmweb-mcp",
      "env": {
        "MITMWEB_URL": "http://127.0.0.1:8081",
        "MITMWEB_TOKEN": "YOUR_SECRET_TOKEN",
        "MITMPROXY_PORT": "8080"
      }
    }
  }
}
```

| 环境变量 | 默认值 | 必须对应 |
|---|---|---|
| `MITMWEB_URL` | `http://127.0.0.1:8081` | mitmweb 的 `web_port` |
| `MITMWEB_TOKEN` | *(空)* | mitmweb 的 `web_password` |
| `MITMPROXY_PORT` | `8080` | mitmweb 的 `--listen-port` |

重启 MCP 客户端,然后让它调用 `status` 确认连上了。

---

## 工具清单

| 工具 | 作用 |
|---|---|
| `status` | 连通性检查 + 流量条数。排障从这里开始 |
| `flow_stats` | 域名分布、状态码分布、静态资源占比、最热接口(数字 id 归一为 `{n}`) |
| `list_flows` | 列出最近流量(新的在前)。可按域名/方法/状态码/URL/content-type/**时间窗**/**UI 标记** 过滤 |
| `inspect_flow` | 单条完整详情:query 参数、双向请求头、双向正文、延迟、可直接跑的 `curl` |
| `get_content` | 完整正文,gzip/brotli 已自动解码 |
| `search_flows` | 跨全部流量的全文搜索,支持正则 |
| `diff_flows` | 逐字段对比两条请求 |
| `detect_auth` | 识别站点用的是哪种鉴权、凭证藏在哪 |
| `generate_code` | 生成可直接运行的爬虫:`curl_cffi` / `httpx` / `requests` / shell 脚本 |
| `replay_flow` | 带浏览器 TLS 指纹重放,可改写 method / 头 / 正文 |

flow id 用 `list_flows` 返回的 8 位短 id 就行,内部按前缀匹配。

### 三个值得知道的设计细节

**默认剔除静态资源。** 一个现代页面产生几百条流量,真正有用的可能就五条。
`list_flows` 默认应用等价于 mitmproxy `!~a` 的过滤,除非你传 `include_assets=True`。
二进制正文也绝不会被解码成乱码,而是返回 `<binary image/png, 8090 bytes, omitted>`。

**你的 UI 操作可以直接当成给 AI 的输入。** 这是共享同一份会话才有的红利,
任何无头方案都做不到:

- `list_flows(marked_only=True)` —— 你在 mitmweb 界面里 Mark 几条,AI 只分析这些。
- `list_flows(since_seconds=15)` —— 你刚点了个按钮,这就精准圈出这次点击触发了什么。

**重放穿过你自己的代理。** mitmweb 原生的重放接口受 Tornado XSRF 保护,而那个 cookie
只下发给 `/updates` websocket。与其为此维护一条 websocket,`replay_flow` 选择
**重新发一次请求、穿过你自己的代理** —— 结果照样落进你的 UI,而且获得了原生重放
没有的能力:通过 [curl_cffi](https://github.com/lexiforest/curl_cffi) 做
**TLS/JA3 指纹伪装**、任意改写头和正文、以及 `allow_redirects=False` 让每一跳都看得清。

---

## 完整实战示例

**第一步 —— 从页面上看得见的东西反查接口**

> *「页面上这个订单号 SO20260910,是哪个请求返回的?」*

```
search_flows("SO20260910")   → POST /api/order/list
inspect_flow("a3f21b8c")     → 签名头、请求体结构、等价 curl
```

**第二步 —— 搞清楚哪些参数参与了签名**

把同一个操作触发两次,然后:

```
diff_flows("a3f21b8c", "c44a48f7")
```

```json
{
  "same_endpoint": true,
  "query_diff": { "changed": { "nonce": { "a": "aaa", "b": "bbb" } } },
  "body_diff":  { "changed": { "sign":    { "a": "1111", "b": "2222" },
                               "meta.ts": { "a": 1000,   "b": 2000   } } }
}
```

相同的字段被自动省略,所以**剩下的就是答案**:签名涉及一个 nonce 和一个时间戳。
`page` 和 `meta.ver` 两次都没变,说明它们不参与签名。

**第三步 —— 验证能否脱离浏览器复现**

```
replay_flow("a3f21b8c")                     → 同样 200,全程没有浏览器
replay_flow("a3f21b8c", body={"page": 2})   → 试探翻页和边界
```

每一次重放都会同步出现在你的界面里。

**第四步 —— 出代码**

```
generate_code(["a3f21b8c"], framework="curl_cffi")
```

```python
#!/usr/bin/env python3
"""Generated by mitmweb-mcp from captured traffic.

Adapt as needed: add paging loops, concurrency, retries, error handling."""
from curl_cffi.requests import Session

IMPERSONATE = 'chrome'


def main() -> None:
    with Session(impersonate=IMPERSONATE) as s:

        # --- 1. POST /api/order/list (originally returned 200) ---
        r1 = s.post(
            'https://example.com/api/order/list',
            params={'page': '1'},
            headers={'Authorization': 'Bearer ...', 'Content-Type': 'application/json'},
            json={'page': 1, 'sign': '1111'},
        )
        print("1.", r1.status_code, r1.text[:200])


if __name__ == "__main__":
    main()
```

传多个 id 就能生成多步脚本 —— 这些请求共用一个 `Session`,
所以「先登录拿 token、再调业务接口」这种链路能带着 cookie 原样复现。

---

## 边界:它不做什么

本服务器是**分析层**。抓包、实时拦截打断点、清空会话都留在 mitmweb 界面里 ——
那本来就是它们该待的地方。拦截尤其如此:它本质上是交互式的,
经过 AI 转一手没有任何好处。

无人值守的批量抓取请用 `mitmdump` 配 addon 脚本,那和本工具是两件不同的事。

---

## 开发

```bash
git clone https://github.com/numb747/mitmweb-mcp
cd mitmweb-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check .
python tests/test_e2e.py     # 需要 PATH 里有 mitmproxy,以及网络
```

端到端测试会在 18080/18081 端口拉起一个真实的 mitmweb,灌入带特征值的流量
(包括同一接口调用两次、只有 `nonce`/`sign`/`ts` 不同,用于验证 `diff_flows`),
然后通过真正的 MCP stdio 会话逐个驱动所有工具。共断言 **56 项行为**,
包括生成的代码能通过编译、以及 `diff_flows` 确实省略了没变化的字段。

### 两个会坑到你的地方

**`GET /flows/<id>` 返回 405。** `/flows.json` 是唯一的列表接口,而且一次返回全部,
每条约 2.6 KB。所以那个 2 秒 TTL 缓存是**成本正确性的必需品**,不是微优化。
同理,`search_flows` 并发拉取正文 —— 串行的话就是几百次往返。

**FastMCP 会预解析 JSON 字符串参数。** 一个标注为 `str` 的参数,若收到合法 JSON
字符串,会在校验**之前**被解析成 dict,然后报 `Input should be a valid string`。
这就是 `replay_flow` 把 `headers` 和 `body` 标注成 `dict | str | None` 的原因。

## 贡献

欢迎提 issue 和 PR。提 PR 前请先跑 `ruff check .` 和端到端测试。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。

## 致谢

构建于 [mitmproxy](https://mitmproxy.org/) 和
[curl_cffi](https://github.com/lexiforest/curl_cffi) 之上。
