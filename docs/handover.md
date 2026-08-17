# IdeaGen40 交接与脱敏运行手册

写给接收方 BU 的工程师。目标是：这套系统在**你们自己的 BytePlus 账号**上跑起来，
并且转手过程中不带走上一家的凭证、不带走不该再发布的第三方内容。

一条命令跑完整张清单：

```bash
python3 scripts/preflight_handover.py          # 任一 FAIL 即退出码 1
python3 scripts/preflight_handover.py --quick  # 跳过 git 历史扫描（正式交接前必须跑完整版）
```

手册的每一节都对应脚本里的一段检查。**不要只读手册不跑脚本**——照着敲的清单会被
跳过，跳过的那一项就是出事的那一项。

---

## 0. 先理解这套系统怎么拿凭证

这决定了后面所有步骤为什么这么做。

```
调用方  →  platform.load()  →  SecretStore 端口  →  ┬ 进程环境变量
                                                    ├ ~/.ideagen.env（chmod 600，在仓库目录之外）
                                                    └ BytePlus KMS keyring（byteplus 适配器）
```

三条既有事实，请不要改动：

1. **仓库里没有凭证，一行都没有。** `ideagen/config.py:52` 把 env 文件定在
   `~/.ideagen.env`，故意放在工作树之外——放在仓库里，一次 `git add -A` 就发布出去了
   （这个仓库历史上一直是公开的）。
2. **`.gitignore` 里的 `*.env` 是第二道闸，不是第一道。** 第一道是路径本身在仓库外。
3. **KMS 降级到环境变量时必须能被看见。** `KmsSecretStore.check()`
   （`ideagen/platform/byteplus.py:403`）会把「有几个 secret 其实来自环境变量而不是
   KMS」打进健康检查明细。不要把这段去掉——静默降级会让 KMS 配错好几周都没人发现。

配置 vs 凭证的边界看 `ideagen/config.py`：里面全部是**配置**（时区、因子权重、仓位
上限、成本模型、账本定义）。唯一涉及凭证的是 `wisburg_token()`，它只做
`require("WISBURG_MCP_TOKEN")`，不读任何硬编码回退值。

> ⚠️ `ideagen/config.py:3-6` 的模块 docstring 说 Wisburg token「先读环境变量，读不到
> 就回退到本机已配好的值」。这句话与第 83 行的实际代码不符——代码里没有任何回退。
> 文档比代码更旧。审计时按代码为准，但接收方最好顺手把这句话改掉，否则下一个人会去
> 找一个不存在的硬编码值。

---

## 1. 交接前脱敏清单

按顺序做。每一项都给了验证命令，验证不过就别往下走。

### 1.1 仓库里没有凭证形状的字符串

```bash
# 项目自带的审计（只看 git 跟踪的文件）
python3 -c "import json;from ideagen.schema import secret_audit;print(json.dumps(secret_audit('.'),ensure_ascii=False,indent=1))"
```

期望 `"clean": true`。

**但这个函数不够，必须知道它的三个盲区**（`ideagen/schema.py:196-216`）：

| 盲区 | 为什么要紧 | 补救 |
|---|---|---|
| `ALLOW` 整目录跳过 `docs/`、`tests/` | 判断是 `rel.startswith(a) or a in rel`，也就是**任何路径里含 `docs/` 的文件**全跳。而 `docs/byteplus_platform.xml`、`docs/feishu_tech_doc.xml` 恰好是最可能被粘进 AK/SK 的两个文件——它们记录的就是「我用你给的 AK/SK 连上去了」的过程 | 用 `preflight_handover.py` 第 2 步，它不认这份白名单 |
| `tracked_only=True` 默认只看 git 跟踪的文件 | 交接时 `tar czf`、`rsync -a`、以及**没有 `.dockerignore` 的 `docker build`** 装走的是整个工作目录，不是 `git ls-files` 的结果。这个仓库现在就没有 `.dockerignore` | 同上，第 2 步扫整棵树 |
| 只看 HEAD，不看历史 | 一个 secret 从 HEAD 删掉之后，仍然活在删它的那次提交的父提交里。公开仓库上这等于永久发布 | 第 3 步扫所有 ref 的所有 blob |

```bash
# 三个盲区一起补上（这是必须跑的那条）
python3 scripts/preflight_handover.py
```

### 1.2 git 历史里没有凭证

脚本第 3 步会遍历 `git rev-list --objects --all` 的每个 blob。**在这份交接的当下，
这一步是干净的**：266 个历史 blob，0 处凭证形状命中（唯一两处 grep 命中是
`ideagen/platform/byteplus.py` 与 `platform/__init__.py` 里的
`password=...` 形参名与 `IDEAGEN_KAFKA_PASSWORD` 变量名，不是值）。

**如果将来这一步报出命中，不要自己 `git commit --amend` 了事。** 正确顺序是：

1. **先轮换那个凭证**，不是先改历史。历史重写不会把已经被 clone 或被爬过的副本收回来，
   所以泄露的那一刻起该凭证就必须视为已失效。BytePlus AK/SK 在控制台禁用后新建。
2. 再重写历史，由**人**执行、在确认过没有其他人正在基于该分支工作之后：
   ```bash
   # 不要由自动化跑这条。会改写所有 commit sha，所有 clone 都要重新拉
   git filter-repo --replace-text <(echo 'literal:<那个值>==><REMOVED>')
   git push --force-with-lease origin main gh-pages
   ```
3. 让所有已有 clone 重新克隆，不是 `git pull`。

### 1.3 身份痕迹

不是凭证，但会把上一任写进一个要转手的仓库。**这一批目前是 FAIL 状态**，交接前必须清掉：

| 位置 | 内容 |
|---|---|
| `scripts/com.ideagen40.daily.plist:9,11,21,22` | 上一任的 macOS 用户绝对路径 |
| `scripts/com.ideagen40.serve.plist:12,15,16` | 同上 |
| `README.md:48,375,384,392` | 上一任的 GitHub 账号与 Pages 地址 |
| `scripts/publish_pages.sh:4,11,46` | 同上 |
| `ideagen/sources/wisburg.py:29` | User-Agent 里写着上一任仓库地址 |
| `docs/byteplus_platform.xml` | 上一家的 TOS bucket 名，**名字里带着 BytePlus 账号 ID** |
| `docs/feishu_tech_doc.xml:6` | 上一任 GitHub 地址 |

```bash
# 验证：这一节全部清掉后，第 5 步应当只剩 [ OK ]
python3 scripts/preflight_handover.py --quick 2>&1 | sed -n '/5\. 身份痕迹/,/^$/p'
```

另外三处是**运行时**才产生的身份痕迹，改代码改不掉，只能清数据：

- `data/artifacts/blobs/**/journal.json` 的 `port_health[].detail` 里含
  `/Users/<上一任>/...` 的绝对路径（local 适配器把 env 文件与库文件的完整路径写进了
  健康检查明细）。
- 同上 `journal.json` 顶层的 `host` 字段是 `socket.gethostname()`——上一任的机器名。
- `data/logs/daily.log` 里同样有这些路径。

处理：交接前直接删掉 `data/artifacts/`、`data/logs/`（两者都已在 `.gitignore` 里，
且都是可重新生成的），不要试图逐行擦。

### 1.4 一个必须知道的凭证外泄路径

`RedisCache.check()`（`ideagen/platform/byteplus.py:346`）把 `self.url` **原文**放进
健康检查的 `detail`：

```python
return Health(True, "cache", f"redis {info.get('redis_version')} @ {self.url}", ...)
```

而 `RunJournal.__init__` 会 `self.health = [h.__dict__ for h in platform.check()]`，
`close()` 再把它整段写进 TOS 的 `journal.json`，同时 publish 到 Kafka。

**后果**：如果你们的 `IDEAGEN_REDIS_URL` 写成 `redis://:<口令>@host:6379/0`（Cache for
Redis 默认就带口令），那么这个口令会被写进每一次 run 的产物、以及事件流。产物是
不可变的，写进去就删不掉。

两个选择，选一个：

- **推荐**：Redis 口令不放在 URL 里，走 KMS 取，URL 只留 `redis://host:6379/0`。
- 或者改一行 `byteplus.py`，让 `detail` 只输出 host:port。这属于修改既有源码，需要
  你们自己决定要不要动。

`PostgresStateStore.check()` 只回显 `version()`，不回显 DSN——那个是对的。
`KafkaEventBus.check()` 回显 `servers` 与 `topic`，不回显 SASL 口令——也是对的。
只有 Redis 这一处漏。

### 1.5 交接前必须物理删除的目录

```bash
# 都在 .gitignore 里，都可重新生成，但都会被 tar / rsync / docker build 装走
rm -rf data/ideagen.db data/ideagen.db-wal data/ideagen.db-shm \
       data/briefings data/snapshots data/logs data/artifacts \
       web/index.html web/report.json
```

理由见 §4。特别注意 **`data/ideagen.db` 里有 7,592 条 Wisburg 文档，其中
`documents.body` 45 万字符是订阅研究原文**，`documents.summary` 另有 47 万字符。
`-wal` 文件（约 4.7 MB）里是还没 checkpoint 的页面，只删 `.db` 不删 `-wal`
既不安全也不完整。

关于镜像：`deploy/Dockerfile` 写得是对的——它按路径逐个
`COPY ideagen / prompts / seed / deploy/entrypoint.sh`，**不拷 `data/`**，也没有把任何
凭证写成 `ENV` 或 `ARG`。这两点必须保持，理由写在它自己的注释里：镜像层是**删不掉**的，
后一层删了文件、前一层照样叠在下面，而 `docker history` 连 build arg 都留着。

仍然建议补一个 `.dockerignore`（缩小上下文，并且防住将来有人图省事写 `COPY . .`）：

```
.git
data/
web/index.html
web/report.json
*.env
__pycache__/
```

注意 `COPY seed` 会把 `seed/pack_2026-07-27.json` 连同它里面的 3 个合作方货架产品代码
一起烙进镜像层。要不要接受，属于 §4.2 那个判断。

---

## 2. 凭证清单

**全部只写变量名。手册里、仓库里、任何提交里都不要出现值。**

### 2.1 平台层（`ideagen/platform/__init__.py:36` 的 `ENV_KEYS`）

| 变量名 | 用途 | 必需？ | 缺了会怎样 |
|---|---|---|---|
| `IDEAGEN_PLATFORM` | `local` 或 `byteplus` | 否，默认 `local` | 全部走本机文件系统 + SQLite，云端口一个都不启用 |
| `BYTEPLUS_ACCESS_KEY` | TOS 与 KMS 的 AK | **是**（byteplus 模式） | `blobs` 与 `secrets` 端口双双 `NotConfigured`；`ready()` 为 false，run 拒绝启动 |
| `BYTEPLUS_SECRET_KEY` | 对应 SK | **是** | 同上 |
| `BYTEPLUS_REGION` | 默认 `ap-southeast-1` | 否 | 落到 `ap-southeast-1`。这个默认是有意的：新加坡节点延迟最低且在内地数据规则之外，而语料是有授权约束的订阅研究 |
| `IDEAGEN_TOS_BUCKET` | 产物 bucket | **是** | `blobs` 端口不可用，run 拒绝启动（`DEFAULT_NEED` 含 `blobs`） |
| `IDEAGEN_TOS_PREFIX` | bucket 内前缀，如 `prod` / `staging` | 否 | 产物写在 bucket 根下。**强烈建议设**，这是 prod/staging 共用一个 bucket 时唯一的隔离手段 |
| `IDEAGEN_PG_DSN` | RDS for PostgreSQL DSN | 否 | **不设就继续用 SQLite**，这是有意设计的迁移路径（`platform/__init__.py:104`）：一个端口一个端口搬 |
| `ARK_API_KEY` | ModelArk API key | 视用法 | `inference` 端口不可用。**不在 `DEFAULT_NEED` 里**，所以纯机械策略的 run 照跑；需要模型判断的打分与 idea 生成会在调用点抛 `NotConfigured` |
| `IDEAGEN_ARK_MODEL` | ModelArk endpoint / model id | 否 | 默认 `seed-1-6-flash` |
| `IDEAGEN_KAFKA_SERVERS` | MQ for Kafka bootstrap | 否 | 自动退回文件事件流 `data/artifacts/events.jsonl`。`events` **故意**不计入 `ready()`：丢监控只是少看见，拒绝运行会丢掉这一周的语料，而那一周的语料事后取不回同样深度 |
| `IDEAGEN_KAFKA_TOPIC` | 事件 topic | 否 | 默认 `ideagen.runs` |
| `IDEAGEN_KAFKA_USER` / `IDEAGEN_KAFKA_PASSWORD` | SASL_PLAINTEXT / PLAIN 凭证 | 视实例 | 不设就不启用 SASL；实例要求鉴权时连不上，事件静默丢弃（`publish` 只累加 `errors` 计数，永不抛进 pipeline） |
| `IDEAGEN_REDIS_URL` | Cache for Redis | 否，但**生产强烈建议** | 退回文件锁 `FileCache`。文件锁协调不了两个沙箱，而「同一周跑两次会重复下单」是真实风险——分布式锁是用 Redis 的**唯一**理由。⚠️ 口令别写在 URL 里，见 §1.4 |
| `IDEAGEN_KMS_KEYRING` | KMS keyring 名 | 否 | 默认 `ideagen` |
| `IDEAGEN_ARTIFACT_ROOT` | 本地产物根目录 | 否 | 默认 `data/artifacts` |

### 2.2 数据源与本地推理

| 变量名 | 用途 | 必需？ | 缺了会怎样 |
|---|---|---|---|
| `WISBURG_MCP_TOKEN` | Wisburg 订阅研究 MCP 的开发者 key | **是，功能上** | `ideagen doctor` 报 WARN 而非致命（有意为之：一次网络抖动只损失一天新语料，中止整个 run 却会连当天的盯市、告警、归因一起丢掉）。但**连续缺失就等于系统空转**——没有新语料就没有打分，没有打分就没有 idea |
| `WISBURG_MCP_URL` | MCP 端点 | 否 | 有默认值 |
| `FUTU_HOST` / `FUTU_PORT` | 本地 Futu OpenD 网关 | **是** | `ideagen doctor` 退出码 1 并中止 `daily.sh`。没有行情就没有盯市、没有归因、没有 M/C 因子。注意：OpenD 是**本机进程**，需要登录，云沙箱里跑不起来——这是这套系统上云最硬的一处约束 |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | local 适配器的推理 key | 否 | local 模式下 `inference` 端口不可用 |
| `IDEAGEN_INFERENCE_BASE_URL` / `IDEAGEN_INFERENCE_MODEL` | 指向任意 OpenAI 兼容端点 | 否 | 设了 base_url 时会改用 `ARK_API_KEY`，所以本机开发直连 ModelArk 不需要改代码 |
| `IDEAGEN_DB` / `IDEAGEN_ENV` | 覆盖库路径 / env 文件路径 | 否 | 默认 `data/ideagen.db` 与 `~/.ideagen.env` |

### 2.3 飞书

**这个仓库里没有任何飞书凭证，也没有任何飞书 SDK 调用。** 全仓搜索
`feishu|lark|webhook|open_id` 只命中三处，全是注释或文档：
`ideagen/monitor.py:152` 的 docstring（"for the Feishu message"）、
`README.md:421-428`、`docs/feishu_tech_doc.xml`。

日报推送是**仓库外**的一个 Claude Code slash command 调 `lark-cli` 做的，凭证在那个
工具的配置里，不在这里。接收方如果要保留这个能力，需要：

- 自建飞书自建应用，拿到 `APP_ID` / `APP_SECRET`（**放 KMS 或环境变量，不放仓库**）；
- 收件人用你们自己的 `open_id` 或群 `chat_id`；
- 不要沿用上一任的 `open_id`——那是个人身份标识。

`ENV_KEYS` 里也没有飞书变量，所以 `ideagen platform --env` 不会提示你。这一项要靠这份
手册记住。

### 2.4 `ENV_KEYS` 漏了什么（换账号时容易漏掉的）

`ideagen platform --env` 只列 `ENV_KEYS` 里那 20 个。**以下这些代码在读、但那张表里
没有**，所以不会被提示：

`WISBURG_MCP_TOKEN`、`WISBURG_MCP_URL`、`FUTU_HOST`、`FUTU_PORT`、
`IDEAGEN_DB`、`IDEAGEN_ENV`。

另外，上一任的 `~/.ideagen.env` 里有一个 `BYTEPLUS_ACCOUNT_ID`，**代码里从未读取过**，
`ENV_KEYS` 里也没有。别照抄它，也别以为设了它有用。

---

## 3. 换账号步骤

### 3.1 凭证放哪里

按优先级，从最好到可接受：

1. **BytePlus KMS keyring**（生产）。`KmsSecretStore.get()` 先查 KMS，查不到才回退
   环境变量，且回退会在 `check()` 的明细里显式列出来。
2. **容器/沙箱的环境变量注入**（CI、VKE、AgentKit）。
3. **`~/.ideagen.env`，chmod 600，在仓库目录之外**（本机开发）。

```bash
# 3 的正确做法
umask 077
: > ~/.ideagen.env          # 新建，不要 cp 上一任的
chmod 600 ~/.ideagen.env
$EDITOR ~/.ideagen.env      # 逐行填 KEY=值
```

**永远不要**：写进仓库任何文件、写进 `Dockerfile` 的 `ENV`、写进 plist 的
`EnvironmentVariables`、贴进 commit message、贴进 issue。

验证（只看是否设置，不打印值）：

```bash
python3 -m ideagen.cli platform --env
```

### 3.2 指向你们自己的资源

```bash
# ~/.ideagen.env 或注入的环境变量。所有右侧都是占位，填你们自己的
IDEAGEN_PLATFORM=byteplus
BYTEPLUS_ACCESS_KEY=<你们的 AK>
BYTEPLUS_SECRET_KEY=<你们的 SK>
BYTEPLUS_REGION=<ap-southeast-1 或 ap-southeast-3>
IDEAGEN_TOS_BUCKET=<你们自己新建的 bucket>
IDEAGEN_TOS_PREFIX=prod
IDEAGEN_PG_DSN=<你们的 RDS DSN>
IDEAGEN_REDIS_URL=redis://<你们的 host>:6379/0
IDEAGEN_KAFKA_SERVERS=<你们的 bootstrap>
IDEAGEN_KAFKA_TOPIC=ideagen.runs
ARK_API_KEY=<你们的 ModelArk key>
IDEAGEN_ARK_MODEL=<你们账号里可用的 endpoint id>
IDEAGEN_KMS_KEYRING=ideagen
```

三个容易踩的点：

- **TOS endpoint 不用填。** `TOS_ENDPOINTS`（`byteplus.py:37`）按 region 查表。只支持
  `ap-southeast-1` / `ap-southeast-3`；用别的 region 会静默落到
  `ap-southeast-1`，然后 `check()` 报连不上。要加 region 就往那张表里加。
- **bucket 必须是你们自己新建的。** `docs/byteplus_platform.xml` 里记着一个
  `ideagen-<数字>` 形式的 bucket 名——那是上一家的，名字里还带着他们的账号 ID。别沿用，
  你们的 AK 也访问不到。
- **ModelArk 推理面。** 管理面用 AK/SK 就通，推理面需要 `ARK_API_KEY`，而且账号可能
  报 `must set project_name when get preset endpoint token`——这时要在控制台把
  endpoint 绑到 project，或改用自建 endpoint id 填进 `IDEAGEN_ARK_MODEL`。

### 3.3 逐个端口独立验证

健康检查入口是 `ideagen platform`（实现在 `ideagen/cli.py:162`，`cmd_platform`）。
六个端口各有 `check()`，一次全打出来：

```bash
python3 -m ideagen.cli platform --env
```

输出每行一个端口，`OK` / `FAIL` 加人读明细。退出码规则：**除 `events` 之外任一端口
FAIL 即为 1**。

**只看 `check()` 不够**——它只证明端点会应答。真正的往返自检要加 `--probe`：

```bash
python3 -m ideagen.cli platform --probe
```

`--probe` 做三件事，第三件才是重点：

1. 往 blob store 写一个 `selftest/<时间戳>.json`；
2. 读回来，逐字节比对；
3. **再往同一个 key 写一次，确认被拒**。产物不可变是这套系统的硬规则——历史上一次
   「替换批次」把 58 笔持仓挂到了别的标的上，把发布的等权收益从 +0.96% 抬到了
   +5.70%。`put` 没有 overwrite 路径，所以这一项必须 OK。

想单独验一个端口，用 `--platform` 强制适配器，配合只设该端口的变量：

```bash
# 只验 TOS：设 AK/SK/BUCKET，其余留空
IDEAGEN_PLATFORM=byteplus python3 -m ideagen.cli platform --probe

# 只验 RDS：设了 DSN 就走 Postgres，没设就是 SQLite。看 state 那一行的 detail
#   OK  postgres 16.x (N tables)   ← 走上了 RDS
#   OK  sqlite 3.42.0 at ...       ← DSN 没生效，还在本机库上
python3 -m ideagen.cli platform | grep state

# 只验 Redis：看 cache 那一行是 "redis <版本>" 还是 "local fs"
python3 -m ideagen.cli platform | grep cache
```

搬完 RDS 之后建表：

```bash
python3 -c "
from ideagen import platform as P
from ideagen.schema import migrate, verify
p = P.load()
print('migrated stmts:', migrate(p.state))
print('verify problems:', verify(p.state))
"
```

`verify()` 存在的理由值得知道：`CREATE TABLE IF NOT EXISTS` 撞上一个**同名但不同结构**
的表时是静默 no-op，错误要到第一次 insert 才以「缺列」的形式冒出来，离原因很远。
`verify()` 把它变成启动时的一行可读信息。期望 `verify problems: []`。

最后是业务侧依赖：

```bash
python3 -m ideagen.cli doctor
```

它查 OpenD、Wisburg、语料覆盖、行情覆盖、registry、主题字典。退出码只反映**真正跑不下去
的东西**：行情源与数据库。Wisburg 报 WARN 不致命。

---

## 4. 数据边界

### 4.1 可以随仓库走的

- 全部源码 `ideagen/`、`scripts/`、`prompts/`、`tests/`
- 方法论文档 `docs/`（**清掉 §1.3 那些身份痕迹之后**）
- 主题注册表 `themes/registry.jsonl`
- `data/batches/*.json`——已核对：不含合作方产品代码、不含订阅原文，最长中文块 133 字符，
  都是自己写的 thesis 文本

### 4.2 不可以再发布的

| 内容 | 在哪 | 为什么 |
|---|---|---|
| Wisburg 订阅研究原文 | `data/briefings/`（已 gitignore）；`data/ideagen.db` 的 `documents.body` / `.summary` | 是订阅服务的正文，不是我们的东西。`.gitignore` 的注释写得很清楚：这是**授权问题**，不只是隐私问题 |
| Nexus / Olive 货架数据（产品代码、NAV、收益率） | `data/snapshots/`（已 gitignore） | 合作方的产品货架数据 |
| Wisburg 图表 | 从其 CDN 热链。`config.py:194` 的 `EMBED_IMAGES_LOCAL=True` / `EMBED_IMAGES_PUBLIC=False` | 本地看是「看」，发布到可索引页面上是「转载」，是两回事。**这个区分不要改** |

**⚠️ `.gitignore` 挡住了原始快照，没挡住派生物。** 已核实的泄漏路径：

- **跟踪的源码里就有货架产品代码**：`ideagen/universe.py:195-196`、`ideagen/seed.py:39`
  各含 1 个 `L0xxxx` 形式的 Olive 产品代码与产品全名；`seed/pack_2026-07-27.json` 含 3 个；
  `web/artifact.html` 含 1 个。
- **发布出去的 dashboard 里有 3 个**。当前 `gh-pages` 顶端的 `report.json`（6.1 MB）含
  3 个货架产品代码、85 处 `Olive` 字样，并且**这 16 次 gh-pages 提交里有 14 次都带着**
  （最早一次是 2026-08-07）。自己复核：
  ```bash
  git cat-file -p origin/gh-pages:report.json | python3 -c "
  import sys,re; raw=sys.stdin.read()
  print('产品代码个数:', len(set(re.findall(r'L0\d{4}', raw))), ' Olive 字样:', raw.count('Olive'))"
  ```

接收方必须自己重新做一次判断：这些代码与产品名是否在你们与合作方的协议下可以对外。
判断完了在 preflight 里显式确认：

```bash
HANDOVER_ACK_THIRD_PARTY_DATA=1 python3 scripts/preflight_handover.py
```

好消息一条：**溯源信息本身是干净的。** `report.json` 的 `evidence` 只有
标题（最长 66 字符）、机构名、检索式、内容 hash、字符数，**没有正文摘录**
（`ideagen/report.py:20` 就是这么写的，实测也确实如此）。这一条设计是对的，别改坏。

### 4.3 公开持仓明细 —— 接收方必须重新做的决定

**当前状态：完整持仓明细正在被发布到一个公开、可被搜索引擎索引的 URL 上。**

链路：`scripts/daily.sh` 最后一步无条件调用
`scripts/publish_pages.sh --yes`（`--yes` **跳过那个交互式确认**），把
`web/index.html` + `web/report.json` 推到公开仓库的 `gh-pages` 分支。

暴露的具体内容（已从 `gh-pages` 顶端的 blob 实测确认）：

- **829 条持仓记录**，每条含：`code`、`avg_px` 成交均价、`qty` 数量、`cost` 成本、
  `mv` 市值、`pnl_usd` 与 `pnl_pct` 盈亏、`stop_px` 止损价、`take_px` 止盈价、
  `opened_d` / `closed_d`、`exit_reason` 离场原因、`grade` 评级
- 两个 1,000 万美元账本的完整权益曲线与归因
- 15 个交易日、每日 40 条 idea 的完整论点与情景概率
- 上述 3 个合作方货架产品代码

也就是说：**任何人都可以看到完整的策略逻辑、每一笔的进出价位与止损止盈位。**
上一任是明确要求过这个公开 URL 的（`publish_pages.sh` 的注释里写了）。接收方不能
「继承」这个决定——那是别人替你做的决定。

三条路，选一条：

```bash
# A. 停止发布（最保守，推荐先这样，跑通再谈）
#    把 daily.sh 末尾那段 publish_pages.sh 调用删掉

# B. 仓库转私有（gh-pages 随之不可公开访问）
gh repo edit <你们的组织>/<你们的仓库> --visibility private \
  --accept-visibility-change-consequences

# C. 明确决定继续公开发布
HANDOVER_ACK_PUBLIC_BLOTTER=1 python3 scripts/preflight_handover.py
```

选 A 或 B 的，别忘了旧的 `gh-pages` 历史：那 16 次提交里的持仓明细还在。
换了新 remote 就不要把 `gh-pages` 一起推过去；如果沿用同一个仓库，删分支
（`git push origin --delete gh-pages`）只是让它不再被服务，历史上的内容对已经
clone 过的人仍然可见。

### 4.4 remote

```bash
git remote -v      # 交接时仍指向上一任的公开仓库
```

**第一次 push 之前先换掉**，否则会把代码推回上一家的仓库：

```bash
git remote set-url origin <你们自己的仓库地址>
git remote -v      # 确认
```

---

## 5. 首次部署验证

按顺序，前一步不过不要做下一步。**最后一步是唯一能区分「真跑通」和「跑完了但什么都
没做」的检查**，不要跳。

```bash
# 步 0  脱敏与配置体检。必须退出码 0
python3 scripts/preflight_handover.py
echo "exit=$?"

# 步 1  依赖装齐。云 SDK 全部是惰性 import，缺哪个 check() 会明确告诉你缺哪个
pip install -r requirements.txt
pip install tos 'psycopg[binary]' redis kafka-python volcengine

# 步 2  单元测试。132 个用例，不需要任何云依赖也不需要网络
python3 -m pytest tests/ -q

# 步 3  端口健康。除 events 外全 OK，退出码 0
python3 -m ideagen.cli platform --env
echo "exit=$?"

# 步 4  产物往返 + 不可变性。三行都必须 OK
python3 -m ideagen.cli platform --probe

# 步 5  建表并校验结构
python3 -c "
from ideagen import platform as P
from ideagen.schema import migrate, verify
p = P.load(); print('migrated:', migrate(p.state)); print('problems:', verify(p.state))"

# 步 6  业务依赖。OpenD 必须活；Wisburg 报 WARN 可以先往下走
python3 -m ideagen.cli doctor

# 步 7  跑一次完整 daily。它有 8 个 stage，每个 stage 单独记状态，
#       一个 stage 挂掉不会丢掉整个 run
python3 -m ideagen.cli daily
echo "exit=$?"        # 0 = 8/8 ok；1 = partial，看输出里哪个 stage failed

# 步 8  ★ 静默空跑检测 ★
python3 scripts/preflight_handover.py 2>&1 | sed -n '/9\. 静默空跑/,/^$/p'
```

**为什么必须有步 8。** 步 7 的退出码只说「每个 stage 没抛异常」。如果
Wisburg 授权没开通、或者网络策略拦了出站，`ingest` 会抓到 0 条，后面 7 个 stage 会
对着一个空语料库全部「成功」，`daily` 退出 0，看板照样生成——只是里面什么都没有。
**退出码 0 不等于系统在工作。**

所以步 8 看的是行数不是退出码，三条硬指标：

| 指标 | 期望 | 不满足意味着 |
|---|---|---|
| `runs` 表最近一条的 `stages` 全 ok | 9/9 | 有 stage 失败，看 `note` |
| 该次 run 之后 `documents` 新增行数 | **> 0** | 这一次跑等于什么都没做。最常见原因：`WISBURG_MCP_TOKEN` 无效 / 未授权 / 出站被拦 |
| `prices` 表行数 | **> 0** | OpenD 从来没取到行情，盯市与归因全是空的 |

手工复核同一件事：

```bash
python3 -c "
import sqlite3, json
c = sqlite3.connect('data/ideagen.db'); c.row_factory = sqlite3.Row
r = c.execute('SELECT * FROM runs ORDER BY started_at DESC LIMIT 1').fetchone()
print(r['run_id'], r['status'])
for s in json.loads(r['stages']): print(' ', s['stage'], s['status'], s.get('note') or '')
print('documents:', c.execute('SELECT COUNT(*) FROM documents').fetchone()[0])
print('prices   :', c.execute('SELECT COUNT(*) FROM prices').fetchone()[0])
print('ideas    :', c.execute('SELECT COUNT(*) FROM ideas').fetchone()[0])
"
```

**只有步 8 三项全绿，才可以把 daily 挂上定时器无人值守。**

---

## 6. 回滚

第一次部署失败时，回到上一个已知可用状态。**这套系统的回滚是配置回滚，不是代码回滚**
——六个端口的存在就是为了这个。

### 6.1 一步回到本机全栈（最快、最稳）

```bash
unset IDEAGEN_PLATFORM        # 或 export IDEAGEN_PLATFORM=local
python3 -m ideagen.cli platform --probe
```

`local` 适配器是文件系统 + SQLite + 直连 API，不碰任何云资源。这就是为什么整套测试
不装任何云依赖也能跑——回滚路径天天在被验证。

### 6.2 逐个端口回滚（推荐，能定位到是哪个端口的问题）

端口可以混搭（`platform/__init__.py:11-13`），所以可以一个一个退：

```bash
unset IDEAGEN_PG_DSN          # 状态退回 SQLite，产物仍在 TOS
unset IDEAGEN_REDIS_URL       # 缓存与锁退回文件；⚠️ 只在确认没有并发 run 时才这么做
unset IDEAGEN_KAFKA_SERVERS   # 事件退回 data/artifacts/events.jsonl
unset IDEAGEN_TOS_BUCKET      # 产物退回本地文件系统
python3 -m ideagen.cli platform   # 每退一步验一次，看是哪一行变绿了
```

### 6.3 回到上一个账号 / 上一套凭证

```bash
# 换账号之前先留一份键名快照（只留名，不留值），回滚时知道该找回哪些
cut -d= -f1 ~/.ideagen.env | grep -v '^#' | grep -v '^$' > ~/.ideagen.env.keys.bak

# 回滚：重新填旧凭证，或把 KMS keyring 切回去
export IDEAGEN_KMS_KEYRING=<上一个 keyring 名>
python3 -m ideagen.cli platform --env      # 确认键都在
python3 -m ideagen.cli platform --probe    # 确认真能读写
```

### 6.4 回滚时不需要担心的、和必须担心的

**不需要担心**：TOS 产物不可变，没有 overwrite 路径。失败的 run 只会多写一个
`runs/<as_of>/<run_id>/` 目录，不会破坏任何已有产物。删掉那个目录就干净了。

**必须担心的三件**：

1. **RDS 里的表。** `migrate()` 是幂等的，回滚不用回退 DDL；但如果失败的 run 已经写进
   过 `orch_runs` / `feed_runs` / `verdicts`，按 `run_id` 删掉那些行，别留半截记录。
2. **Redis 锁。** 锁是 `SET NX EX`，TTL 默认 3600 秒。异常退出后锁会挂到过期为止，
   期间下一次 run 拿不到锁会直接跳过。急着重跑就手工删 `lock:<key>`。
3. **不要回滚 §1 的脱敏。** 脱敏是单向的。回滚配置不代表把上一任的凭证、路径、账号名
   重新写回仓库。

---

## 7. 交接判定

以下全部满足，才算交接完成：

- [ ] `python3 scripts/preflight_handover.py` 退出码 **0**（不带 `--quick`）
- [ ] §1.3 的身份痕迹全部清掉
- [ ] §1.5 的目录全部物理删除，`.dockerignore` 已补
- [ ] §1.4 的 Redis URL 口令外泄路径已按其中一种方式处理
- [ ] §4.3 公开持仓明细的决定由接收方**重新做过**，并已用 ACK 环境变量确认
- [ ] `git remote -v` 指向接收方自己的仓库
- [ ] §5 步 8 三项指标全绿
- [ ] 上一家的 BytePlus AK/SK 已在其控制台**禁用**（不是「不用了」，是禁用）
