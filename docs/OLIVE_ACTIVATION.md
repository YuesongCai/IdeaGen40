# 点亮 Olive 这条腿

状态（2026-09-05）：代码侧已就绪并测试通过，**唯一缺口是 Olive MCP 的 endpoint URL**。
拿到 URL 后本地两条命令、云端四个环境变量即可。

## 背景：Olive 负责哪半个 universe

初心里 universe 是「公募 + ETF + 私募」。这三块的来源不是一处：

| 来源 | 覆盖 | 现状 |
|---|---|---|
| **futu OpenD** | ETF + 个股，日频 OHLC | 已在跑，`universe.LISTED` 95 个标的里 91 个是 ETF |
| **Olive MCP** | 私募 / PE / 信贷 / **UCITS 公募**，月频 NAV | 本文要点亮的那条腿 |

**Olive 里没有可交易 ETF**，别去那儿找。它的价值是 UCITS（日度申赎、符合初心的流动性
要求）和私募货架——`shelf_list` 现有 154 只。

## 第一步：拿 endpoint URL（只有用户能做）

claude.ai → 设置 → 连接器 → 找到 Olive → 复制它的服务器 URL。

本机拿不到：Claude 只暴露连接器的内部 UUID（`c2de1260-…`），上游地址存在服务端。
已确认排除的路径：`~/.claude.json`（只有本地 stdio/http server）、Chrome 扩展（未连接）、
内置浏览器（claude.ai 被拦）、Olive 服务端回显（只漏了 OSS 桶 `nbp-gopher-bucket`）。

## 第二步：本地授权 + 冒烟（两条命令）

```bash
cd ~/IdeaGen40 && python3 -m ideagen.cli olive-auth --url "https://<粘贴这里>" --env-file ~/.ideagen.env
```

会自己做三件事：按 RFC 9728 从 endpoint 发现 OAuth issuer（所以不用另外给 issuer）、
起 `127.0.0.1:8766` 回环等 Noah SSO 回调、把 access/refresh token 写回 env 文件。

```bash
cd ~/IdeaGen40 && python3 -m ideagen.cli olive-pull --detail-limit 3 --ingest
```

预期打印 `catalog=154` 左右。判成功不看这行，**看表**：

```bash
cd ~/IdeaGen40 && sqlite3 data/ideagen.db "SELECT 'snapshots',COUNT(*) FROM shelf_snapshots UNION ALL SELECT 'instruments',COUNT(*) FROM shelf_instruments UNION ALL SELECT 'navs',COUNT(*) FROM shelf_navs;"
```

三张表在此之前全是 0 行。`scheduler.py` 的 olive_sync 把异常吞掉当作降级
（"monitoring degrades, never blocks"），所以**面板上跑没跑不能作数，只有行数作数**。

## 第三步：云端

不需要把 Claude 的连接器搬上去。`ideagen/sources/olive.py` 自己就是一个
streamable-HTTP MCP 客户端，带 refresh_token 自动续期，本来就是为无人值守写的。
云端只要这四个键：

```bash
OLIVE_MCP_URL=https://<同一个地址>
OLIVE_OAUTH_ISSUER=<第二步打印的 discovered OAuth issuer>
OLIVE_OAUTH_CLIENT_ID=<第二步写回 env 的>
OLIVE_OAUTH_REFRESH_TOKEN=<第二步写回 env 的>
IDEAGEN_OLIVE_TOKEN_FILE=/var/lib/ideagen/olive_tokens.json   # 刷新后的 token 落这里，必须可写
```

access_token 不用带，客户端会用 refresh_token 换。token 文件按 0600 写。
`IDEAGEN_PUBLIC_SITE` 只有走面板里那条浏览器授权（`olive_web.py`）才需要，
命令行 loopback 这条路用不上。

注意云端出网：ECS 上直连即可；**本机**跑 `olive-pull` 若走 Clash，参照
`ideagen-cloud-access` 的结论——只有 443 放行，Olive 是 https 所以没问题。

## 代码侧改了什么（2026-09-05）

服务端契约漂了 4 处，都已对着实时报文验证修复：

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
