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

## 第二步：云端（BytePlus ECS `i-yeu80pr2tc3z47gon4sy` / `101.47.152.106`）

不需要把 Claude 的连接器搬上去。`ideagen/sources/olive.py` 自己就是一个
streamable-HTTP MCP 客户端，带 refresh_token 自动续期，本来就是为无人值守写的。

**这台机器没有 shell**：本机 Clash 拦 SSH（`kex_exchange_identification: Connection
closed`，TCP 通了但握手前被切断），Cloud Assistant 在这个镜像上始终 `InstallFailed`。
唯一可用的执行通道是 **UserData + reboot**——`deploy/instance_bootstrap.sh` 每次开机都跑，
所以**一次重启就是一次部署**。

配置投递已经打通（2026-09-05 改的）：

- `scripts/build_runtime_env.py` 现在会输出 Olive 的 4 个键 + token 路径，共 32 个键。
  取值来自本机 `~/.ideagen.env`，**不落地、不进 transcript**。
- `deploy/instance_bootstrap.sh` 原本「runtime.env 已存在就跳过投递」——在这台没 shell 的
  机器上等于**跑起来之后永远加不了新键**。已改成**拉取成功才合并**：投递的键覆盖旧值，
  机器自己有而投递里没有的键（比如它自己刷出来的 token）保留，拉取失败则完全不动。

送上去的两条命令（`secrets` 和 `reboot` 分开，正是因为它们动生产）：

```bash
cd ~/IdeaGen40 && python3 scripts/deploy_userdata.py secrets && python3 scripts/deploy_userdata.py reboot
```

冷启动要十分钟左右。验证：

```bash
cd ~/IdeaGen40 && python3 scripts/deploy_userdata.py status
```

`say: runtime.env merged (32 keys)` 出现即为配置到位。云端出网直连，不经 Clash。

⚠️ **同一时间只能有一个人 reboot**。仓库里常有多个会话在动部署，推之前先确认 :80
已经恢复（401/200），不要在别人引导到一半时重启。

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

## 深抓范围：已从 1 只改成全量（2026-09-05）

原来 `_sync_olive_daily` 用 `detail_limit=1`。这**不是限流，是覆盖 bug**：
`pull_snapshot` 永远取 `catalog[:N]` 的同一批头部，所以第 N 名之后的产品
**跑多少个晚上都不会有净值**。

对着实时网关实测：151 只全量深抓（604 次调用）**33 分钟**，一个每日任务负担得起。
（5 只样本外推是 9 分钟，实际吞吐比样本慢得多，别用小样本估这个数。）
收益是实的：**NAV 从 4 条涨到 129 条**。默认改成全量（负数 = 全部），
需要时用 `IDEAGEN_OLIVE_DETAIL_LIMIT` 调回。
