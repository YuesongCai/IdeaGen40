# BytePlus 无人值守调度：AgentKit 沙箱 Runbook

这份文档只讲一件事：**怎么让 IdeaGen40 在没人盯着的时候按时自己跑，并且让"没跑"这件事
永远不会被误读成"这周很安静"。**

调度逻辑全部在 `ideagen/scheduler.py`，容器只负责按固定间隔调用它：

```
deploy/entrypoint.sh  →  python3 -m ideagen.scheduler tick   （每 300s 一次）
                              ├─ weekly   每周三 07:00 HKT，48h 内可补跑
                              └─ monitor  每 15 分钟：盯市 + 止损触发 + feed 健康
```

`tick` 是幂等的：同一个周期跑第二次会被拒绝。判断依据不是本地文件，而是
**Redis 分布式锁 + `orch_runs` 表**——两个沙箱不共享文件系统，文件锁在这里毫无意义，
两个沙箱都会认为"没人在跑"然后各跑一遍，也就是同一周下两套单。

---

## 一、需要的环境变量（**只写名字，永远不要把值写进任何文件、镜像层或本文档**）

镜像里不含任何凭证。凭证只在运行时进入进程：优先走 BytePlus KMS，取不到时退回环境变量，
而这个退回行为会在 `ideagen.cli platform` 的体检里被明确标出来——不会悄悄降级。

### 必填

| 变量名 | 作用 | 备注 |
|---|---|---|
| `IDEAGEN_PLATFORM` | 固定 `byteplus` | 决定用哪套适配器 |
| `IDEAGEN_VENUE` | 固定 `paper` | 见下方"交易场所守门" |
| `BYTEPLUS_ACCESS_KEY` | TOS / KMS 的 AK | 由沙箱注入，不落盘 |
| `BYTEPLUS_SECRET_KEY` | TOS / KMS 的 SK | 同上 |
| `BYTEPLUS_REGION` | 默认 `ap-southeast-1` | 新加坡，语料是订阅制研报，不进内地 |
| `IDEAGEN_TOS_BUCKET` | 存 run 产物 / journal 的桶 | 产物不可变，只追加 |
| `IDEAGEN_TOS_PREFIX` | 桶内前缀，如 `prod` / `staging` | staging 与 prod 必须分开，否则 replay 会撞 key |
| `IDEAGEN_TOS_ENDPOINT` | 可选的 TOS endpoint | 同 VPC 部署时可填写桶概览中的私网 endpoint |
| `IDEAGEN_STATE_ENGINE` | 固定 `mysql` | 当前 POC 使用 RDS MySQL |
| `IDEAGEN_MYSQL_HOST` / `IDEAGEN_MYSQL_PORT` | MySQL 连接地址与端口 | 端口默认 3306 |
| `IDEAGEN_MYSQL_DATABASE` / `IDEAGEN_MYSQL_USER` / `IDEAGEN_MYSQL_PASSWORD` | 数据库与账号 | 任一缺失则 state 端口不可用 |
| `IDEAGEN_REDIS_URL` | Cache for Redis | **不设就退回文件锁 = 幂等性失效**，见第五节 |

### 周策略要调模型时必填

| 变量名 | 作用 |
|---|---|
| `ARK_API_KEY` | ModelArk 密钥 |
| `IDEAGEN_ARK_MODEL` | ModelArk endpoint / model id |

`weekly()` 会先问策略注册表这次要不要调模型，需要而 `inference` 端口没配时**开跑之前**就拒绝，
不会把语料抓完、打完分才发现没有 key。

### 语料 / 观测 / 可选

| 变量名 | 作用 |
|---|---|
| `WISBURG_MCP_TOKEN` | 周策略跑之前深取语料；缺失则跳过 ingest，用库里已有语料跑并在报告里标出来 |
| `IDEAGEN_KAFKA_SERVERS` / `IDEAGEN_KAFKA_TOPIC` | run 生命周期事件与心跳事件；缺失则退回 JSONL 文件 |
| `IDEAGEN_KAFKA_USER` / `IDEAGEN_KAFKA_PASSWORD` | 实例要求 SASL 时才需要 |
| `IDEAGEN_KMS_KEYRING` | KMS keyring 名，默认 `ideagen` |
| `IDEAGEN_TICK_INTERVAL_S` | 循环间隔，默认 300；心跳 TTL = 3 × 该值 |
| `IDEAGEN_MAX_DEGRADED` | 连续降级多少次就退出（默认 12，约一小时），让故障暴露而不是假装活着 |

> `~/.ideagen.env` 是笔记本上的便利路径，**容器里不存在也不应该存在**。

---

## 二、起沙箱

```bash
# 1) 构建并推到 BytePlus 容器镜像仓库（CR）
docker build -f deploy/Dockerfile -t <your-cr-endpoint>/ideagen/scheduler:<tag> .
docker push <your-cr-endpoint>/ideagen/scheduler:<tag>

# 2) AgentKit：新建一个 recurring sandbox / 定时任务
#    - 镜像：上面那个 tag
#    - 入口：镜像自带 ENTRYPOINT（deploy/entrypoint.sh），不需要额外命令
#    - 触发方式：常驻循环（推荐）。sandbox 一直在，tick 自己判断什么到期。
#      如果只能用"定时拉起一次性沙箱"，把命令设为：
#          deploy/entrypoint.sh once
#      触发频率设成 UTC 的 每 15 分钟。不要在 cron 里表达"周三"——
#      星期几由 scheduler 按 HKT 判断，见第三节。
#    - 环境变量：按第一节注入（值只在 AgentKit 的 secret 配置里）
#    - 网络：需要能访问已配置的对象存储、数据库、缓存、推理与 MCP 端点
```

`weekly` 与 `monitor` 共用一个沙箱，不要为周策略单独再配一个定时器：两个触发源就是
两条互不知情的路径，而幂等性只在 `orch_runs` + 锁这一层保证，多一个入口只是多一次撞锁。

---

## 三、时区规则（这条错了不会报错，只会安静地换掉一周的语料）

调度只做一次时区转换：**UTC 瞬间 → 香港日历日**，之后所有东西都用这个 `as_of`。

* 触发点：**周三 07:00 HKT**（= 周二 23:00 UTC，HKT 是 UTC+8 且无夏令时）。
* `as_of` = 触发时刻的**香港日期**，而 `as_of` 同时决定语料窗口：
  `feeds_impl/wisburg_corpus.py` 取的是"截止 `as_of` 的 3 天"，即周一–周二–周三。

如果 cron 用 UTC 日期算 `as_of`，周二 23:00 UTC 会被记成"周二"，语料窗口就变成
周日–周一–周二：**丢掉周三、多算周日**。没有任何东西会失败——0 行和错位的行都能通过 schema
校验，分数照样算得出来，只是从这周起跨周比较不再是同类比同类。所以：

* 容器时钟保持 UTC（`TZ=UTC`），转换只由代码做；
* cron / 触发器**不表达星期几**，只负责高频拉起，是否到期由 `tick` 判断；
* 触发小时也是同一个决定的一部分——改它就改了"周三自己那天有多少研报已经发出来"，
  但 `as_of` 不会跟着变。所以它是 `scheduler.py` 里一个具名常量，不在任何 crontab 里。

实测（同一个周三，差一小时，是两个不同的周期）：

```
2026-08-18T22:30:00+00:00 UTC = 2026-08-19 06:30 HKT (Wed) → as_of=2026-08-12  语料窗口 08-10..08-12
2026-08-18T23:30:00+00:00 UTC = 2026-08-19 07:30 HKT (Wed) → as_of=2026-08-19  语料窗口 08-17..08-19
```

---

## 四、怎么验证第一次跑起来了

```bash
# 0) 端口先体检：先看清楚哪个端口没配，而不是等跑到第 6 步才发现
python3 -m ideagen.cli platform --env --probe

# 1) 先干跑一次，不写任何东西，确认调度判断正确
python3 -m ideagen.scheduler tick --dry-run --json

# 2) 真跑一次（一次性，不进循环）
deploy/entrypoint.sh once --json

# 3) 一条命令看全局：活着吗？这周的 run 完成了吗？有没有缺口？
python3 -m ideagen.scheduler health

# 4) 声明的调度是什么
python3 -m ideagen.scheduler schedule
```

`health` 的关键字段：

| 字段 | 含义 |
|---|---|
| `alive` | 心跳键还在不在（心跳 TTL = 3 × 间隔）。**只看心跳的存在，不看调度器自己怎么说** |
| `heartbeat_age_s` | 上一次 tick 距今多少秒 |
| `weekly.state` | `done` / `open` / `in_flight` / `exhausted` / `recorded_missed` |
| `last_weekly` | 最近一次真正完成的周策略 run |
| `gaps` | 已被记为"永久错过"的周期 |
| `last_monitor_utc` | 最近一次盯市，证明沙箱在两个周三之间也是活的 |

数据库侧的两条查询（RDS 上直接能跑）：

```sql
-- 这周的 run 到底完成了没有（gap 记录不会混进来：kind 不同、ok=0）
SELECT run_id, as_of, ok, ended_at FROM orch_runs WHERE kind='weekly' ORDER BY as_of DESC;

-- 调度器还活着吗（盯市每 15 分钟一行）
SELECT MAX(started_at) FROM orch_runs WHERE kind='monitor';
```

---

## 五、"失败"和"没跑"怎么区分（这是本文档存在的理由）

四种状态，长得都像"这周没有新组合"，但要做的事完全不同：

| 状态 | 现场证据 | 含义 / 该做什么 |
|---|---|---|
| **跑了** | `orch_runs` 有 `kind='weekly' AND ok=1`；TOS 上有 `runs/<as_of>/<run_id>/journal.json` | 正常 |
| **跑失败** | `kind='weekly' AND ok=0`；`error` 有内容；journal 里 `ok=false`；tick 返回 `action=failed`、退出码 1 | 同一周期最多重试 3 次（`MAX_WEEKLY_ATTEMPTS`），之后转 `exhausted` 要人工介入。**注意**：如果失败发生在"平台没准备好"这种开跑前拒绝上（`run_id="-"`），不计入重试次数、也不写行——因为它没花任何成本，而且下一 tick 可能就好了；这种情况靠容器的连续降级计数升级为退出码 1 |
| **被跳过（正常）** | tick 返回 `action=declined`，原因是 `已完成` 或 `lock held by another run`；**没有**新的 weekly 行 | 幂等性在起作用。撞锁时故意不写记录：万一持锁那个沙箱死了，下一 tick 还能重试 |
| **没跑（永久错过）** | `orch_runs` 有 `kind='weekly_missed' AND ok=0`，且 TOS 上有 `runs/<as_of>/gap-weekly-<as_of>/gap.json` | 沙箱当时是停的，且已经超过 48h 补跑窗口。**不会补跑**，见下 |

排查顺序：

```bash
python3 -m ideagen.scheduler health          # 1. alive？weekly.state 是什么？
python3 -m ideagen.cli platform --env --probe       # 2. 哪个端口坏了 / 没配
# 3. 沙箱日志里搜 "[entrypoint]"：连续降级次数、退出码 2 的配置错
```

退出码约定（`entrypoint.sh` 依赖它）：`0` 正常，`1` 降级（下次 tick 可能就好），
`2` 不可恢复的配置错——**立刻退出，不重启**，因为重启不会让缺失的 DSN 出现。

---

## 六、停机之后：能补的和不能补的

```bash
python3 -m ideagen.scheduler catch-up --since 2026-07-29 --json
```

它会把区间里每个周三分类，并且**只记录、不伪造**：

```
2026-07-29  permanently_missed   迟 531.0h，超过 48h
2026-08-05  permanently_missed   迟 363.0h，超过 48h
2026-08-12  permanently_missed   迟 195.0h，超过 48h
2026-08-19  recoverable          仍在 48h 补跑窗口内（迟 27.0h）
监控        21/23 天没有盯市记录，可完整补算
```

**盯市可以完整恢复**：成交、止损、告警都是已收盘 K 线的确定性函数，跑一次盯市就会把
上次盯市到现在的每个交易日走完。

**周策略不能**，两个独立原因：

1. 当时那份语料已经取不回同样的深度——深取的是每条线**最新的** Tier-1/2 条目，
   它的内容是"你什么时候问"的函数；
2. 周策略下的是对着**当时还没印出来的 K 线**的进场区间单。现在用已经印出来的 K 线去成交，
   那不是迟到的运行，是带后见之明的运行，而它会写进和诚实运行同一批表里。

所以超窗的周期只写一行 `kind='weekly_missed' / ok=0` 加一个 `gap.json`：

```json
{
 "as_of": "2026-08-05",
 "kind": "weekly_missed",
 "recorded_at": "2026-08-17T10:10:00.488122+00:00",
 "platform": "local",
 "reason": "迟 363.0h，超过 48h：当时的语料已无法按同等深度取回，且此时补跑会用已经印出来的 K 线去成交进场区间——那是带后见之明的运行，不是迟到的运行",
 "backfilled": false
}
```

窗口内的周期要真补跑，得显式要求：

```bash
python3 -m ideagen.scheduler catch-up --since 2026-08-19 --run-recoverable
```

默认不自动补，是因为一个停了三周才回来的沙箱不应该自己决定连开三个组合。

---

## 七、已知降级项（在沙箱里是常态，不是故障）

| 现象 | 原因 | 后果 |
|---|---|---|
| `prices: OpenD 不可达` | Futu OpenD 是桌面网关，云沙箱连不到 | 只用库里已有的 K 线盯市，组合会显示落后若干交易日。要在云上盯市，需要一个常驻行情代理（见 `docs/byteplus_platform.xml` 里的 MCP 网关那条） |
| `必需 feed wisburg：从未运行过 / 上次返回 0 行` | 语料库是空的，或 ingest 通道断了 | 周策略会跑完但 corpus 为 0 行——**这会被专门标出来**（`scheduler.weekly.thin_corpus` 事件），因为 0 行能通过所有 schema 校验，看起来就像"安静的一周" |
| `盯市跳过：state 端口不是 SQLite` | `paper` / `monitor` 是手写 SQLite | 启用 RDS MySQL 后，legacy 盯市仍不迁移；POC 新表与编排结果不受影响 |

## 八、上生产之前必须先解决的三件事

1. **legacy `db.py` 仍是 SQLite。** 新编排层的六张 POC 表已经支持 MySQL
   （专用 DDL + `ON DUPLICATE KEY UPDATE`），可落 `orch_runs`、feed、候选和 verdict；
   `paper` / `monitor` / `wisburg` 的历史表仍需要 `sqlite3`，本期不迁移。
2. **`IDEAGEN_REDIS_URL` 必须设。** 不设时 byteplus 适配器会退回文件锁，而文件锁只在
   单个沙箱内有效——幂等性的另一半（`orch_runs` 记录）还在，但"两个沙箱同时在跑"这一半
   就没了。心跳同理：退回文件缓存时，心跳会随沙箱一起消失，只剩 `orch_runs` 里
   `kind='monitor'` 的行还能证明活着。
3. **闭环还差最后一步：`weekly()` 不下单。** `orchestrator.weekly()` 跑到 verdicts 为止，
   把 verdicts 变成 batch 和订单的是 `ideas.build_batch` + `paper.open_batch`，目前只有
   CLI 会调。所以无人值守跑出来的是"这周的决策"，还不是"这周的组合"。
   要真正闭环，得在 `orchestrator.weekly` 里补这一步（或在 `SCHEDULE` 里加第三个 job）——
   在那之前，"下重复单"的风险落在一个**没有任何调度器在管**的步骤上，而这一层的幂等性
   （锁 + `orch_runs`）保护不到它。这条按设计边界如实记录，不在本次改动范围内。

### 交易场所守门

`IDEAGEN_VENUE` 只接受 `paper`。这个仓库里没有任何实盘执行适配器（只有 `paper.py`），
所以写成别的值时调度器直接拒绝启动，退出码 2：

```
exit_code=2  fatal=IDEAGEN_VENUE='live' is not supported ('paper',); this repository
has no live-execution adapter, so an unattended run must refuse
```

无人值守的系统面对"配了一个不存在的东西"时，唯一诚实的反应是停下来，而不是猜。
