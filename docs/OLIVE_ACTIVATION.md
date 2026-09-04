# 点亮 Olive 这条腿

状态（2026-09-05）：**本地已点亮跑通**。151 只货架产品入库，0 错误。
云端待部署——把下面「第二步」的环境变量搬过去即可。

已核实的连接参数：

| 键 | 值 |
|---|---|
| `OLIVE_MCP_URL` | `https://mcp-gateway.noahgroup.sg/mcp/olive/olive-mcp` |
| `OLIVE_OAUTH_ISSUER` | `https://noahsso.noahgroup.hk`（由 endpoint 自动发现，未手填） |

## 背景：Olive 负责哪半个 universe

初心里 universe 是「公募 + ETF + 私募」。这三块的来源不是一处：

| 来源 | 覆盖 | 现状 |
|---|---|---|
| **futu OpenD** | ETF + 个股，日频 OHLC | 已在跑，`universe.LISTED` 95 个标的里 91 个是 ETF |
| **Olive MCP** | 私募 / PE / 信贷 / **UCITS 公募**，月频 NAV | 本文要点亮的那条腿 |

**Olive 里没有可交易 ETF**，别去那儿找。它的价值是 UCITS（日度申赎、符合初心的流动性
要求）和私募货架——`shelf_list` 实测 151 只。

## 第一步：本地授权 + 冒烟（两条命令）

```bash
cd ~/IdeaGen40 && python3 -m ideagen.cli olive-auth --url "https://mcp-gateway.noahgroup.sg/mcp/olive/olive-mcp" --env-file ~/.ideagen.env
```

会自己做三件事：按 RFC 9728 从 endpoint 发现 OAuth issuer（所以不用另外给 issuer）、
起 `127.0.0.1:8766` 回环等 Noah SSO 回调、把 access/refresh token 写回 env 文件。

```bash
cd ~/IdeaGen40 && python3 -m ideagen.cli olive-pull --details 5 --ingest
```

实测打印 `catalog=151 detailed=5 errors=0`，分组 `funds 76 / private 74 / cash 1`。
判成功不看这行，**看表**：

```bash
cd ~/IdeaGen40 && sqlite3 data/ideagen.db "SELECT 'instruments',COUNT(*) FROM instruments WHERE kind='fund' UNION ALL SELECT 'navs',COUNT(*) FROM navs UNION ALL SELECT 'shelf_instruments',COUNT(*) FROM shelf_instruments;"
```

注意有**两组表**：`olive-pull --ingest` 写 `instruments`/`navs`（实测 fund 211 行、
navs 21 行）；周跑的 `_sync_olive_daily` 走 `shelf_store` 写 `shelf_snapshots`/
`shelf_instruments`（实测 1 / 151 行）。两条路都已实跑验证。此前全部为 0 行。`scheduler.py` 的 olive_sync 把异常吞掉当作降级
（"monitoring degrades, never blocks"），所以**面板上跑没跑不能作数，只有行数作数**。

## 第二步：云端

不需要把 Claude 的连接器搬上去。`ideagen/sources/olive.py` 自己就是一个
streamable-HTTP MCP 客户端，带 refresh_token 自动续期，本来就是为无人值守写的。
云端只要这四个键：

```bash
OLIVE_MCP_URL=https://mcp-gateway.noahgroup.sg/mcp/olive/olive-mcp
OLIVE_OAUTH_ISSUER=https://noahsso.noahgroup.hk
OLIVE_OAUTH_CLIENT_ID=<第一步写回 ~/.ideagen.env 的>
OLIVE_OAUTH_REFRESH_TOKEN=<第一步写回 ~/.ideagen.env 的，切勿进仓库>
IDEAGEN_OLIVE_TOKEN_FILE=/var/lib/ideagen/olive_tokens.json   # 刷新后的 token 落这里，必须可写
```

access_token 不用带，客户端会用 refresh_token 换。token 文件按 0600 写。
`IDEAGEN_PUBLIC_SITE` 只有走面板里那条浏览器授权（`olive_web.py`）才需要，
命令行 loopback 这条路用不上。

注意云端出网：ECS 上直连即可；**本机**跑 `olive-pull` 若走 Clash，参照
`ideagen-cloud-access` 的结论——只有 443 放行，Olive 是 https 所以没问题。

## 代码侧改了什么（2026-09-05）

服务端契约漂了 5 处（第 5 处见下节），前 4 处对着实时报文验证修复：

1. 工具全改名 `get_fund_*` → `shelf_*`、`list_funds` → `shelf_list`；两套名字都试，
   老快照仍能合并。
2. 每个返回裹在 `{"result": "<json 字符串>"}` 里，`_unwrap_content` 之后还要再剥一层。
3. 目录表在「产品名称」和「市场类型」之间插了 **产品简称** 列 —— 按位取值全体右移一格。
   改成按表头取列，两代契约都吃。
4. `navSeries` 只标到月（`"2026-09"`），`date.fromisoformat` 会抛 ValueError。
   现锚到当月 1 号：真实观测在该日或之后，所以 `mark()` 只会高估陈旧度，不会假装新。

顺带：`_catalog_group` 原本认「货币/cash」，认不出货架上的「现金管理」——已补，
这影响初心里「剩余资金买 JPST」那条对现金类的识别。

**NAV 只有月粒度**（一个月二十来个点但都不标日），所以基金腿只能按月估值，不能当日频
P&L。`universe.py` 原本「分开报告 + `nav_stale_days`」的设计是对的，没动。

回归测试 `test_shelf_payloads_parse_after_the_get_fund_to_shelf_rename` 把实时契约
钉住，下次再改名会直接红。

## 实跑暴露的第 5 处漂移（2026-09-05，仅真连才会出现）

前四处是读报文看出来的，这一处只有真跑才暴露：**Olive 网关的 SSE 帧是
`data:{...}`，冒号后没有空格**。`wisburg.py` 的 `_parse_sse` 只认 `data: `（带空格），
于是每次 `tools/call` 都失败成 "unparseable response"。SSE 语法里那个空格本就是可选的，
已改成两种都吃（Wisburg 那条路不受影响，300 测试全过）。

同一次实跑也证实了 `list_funds` 确实已被服务端删除：
`-32602 Unknown tool: Tool not found: list_funds`。

## 一个待定的取舍

`_sync_olive_daily` 用 `detail_limit=1`——每天只深抓 1 只产品的净值。货架 151 只，
跑完一轮要 151 天，所以 NAV 历史积累极慢（`shelf_navs` 目前 0 行）。
这是原作者定的调用量取舍，没动它；如果要让基金腿真能进回测，这个数得往上调。
