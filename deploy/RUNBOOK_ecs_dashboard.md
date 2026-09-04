# ECS Dashboard Deployment

This runbook intentionally contains no account IDs, instance IDs, addresses,
bucket names, presigned URLs, image digests, or credentials.

## Prerequisites

- An x86_64 Linux host with Docker and the Compose plugin.
- A private MySQL-compatible database.
- An object-storage bucket.
- A public HTTPS hostname or address.
- Runtime credentials stored outside the repository.

## Bootstrapping Without A Shell

Use this when neither SSH nor the platform's command agent is available — for
example when the operator network filters SSH to every destination and the
instance image fails to register a command agent. cloud-init `UserData` is then
the only execution path, and it runs on every boot, which makes a reboot the
deploy.

`deploy/instance_bootstrap.sh` is the source of truth for what the instance does
at boot: install Docker, fetch `origin/main`, build the image, and start the
stack. It contains no credentials, because UserData is stored in clear text by
the cloud API and is readable from the console. Until the runtime configuration
exists on the instance, the script starts nothing and says so on a status page —
a stack that came up half-configured would be worse than an honest "waiting".

Before relying on this path, check which datasource the image uses. Some
images initialise from a *local* datasource, where UserData is baked in when the
instance is first created and is never re-read on reboot. On such an image
updating the instance's UserData changes only the record the console displays:
the running machine keeps executing whatever it was born with, and a bootstrap
that was never delivered is indistinguishable from one that crashed. Confirm it
in the boot log — the console output names the datasource on the line where
cloud-init reports finishing — before spending time debugging a script that the
instance has not actually received.

Where that is the case, the bootstrap can only be handed over by re-initialising
the system volume, which wipes it. That is a destructive operation on a running
host and belongs to a human with the context to say the disk is expendable.

```bash
python3 scripts/deploy_userdata.py push     # ship the bootstrap
python3 scripts/deploy_userdata.py secrets  # + one short-lived config URL
python3 scripts/deploy_userdata.py reboot   # run it, then watch
python3 scripts/deploy_userdata.py forget   # delete the temporary object
```

`secrets` and `reboot` are separate verbs on purpose: shipping a file must not
restart production as a side effect. The status page on port 80 is what tells
"still installing" apart from "unreachable" — with no shell those two look
identical from outside. Once the stack is up the proxy owns that port and
`/healthz` answers instead, so which of the two replies is itself the state.

The bootstrap also re-attempts the command-agent install on every boot. Once
that succeeds, `scripts/deploy_cloud.py` takes over and this path is only
needed again for a cold start.

## Build

```bash
git status --short
SHA=$(git rev-parse --short HEAD)
docker build --pull=never -t ideagen40:${SHA} -f deploy/Dockerfile .
docker run --rm --entrypoint python3 ideagen40:${SHA} \
  -m ideagen.cli poc-load-public-mock --verify-only
```

## Runtime Configuration

Create `/opt/ideagen/config/runtime.env` with mode `0600`. Start from
`.env.example` and populate values through the deployment owner's secret
management process. Do not paste credentials into shell history, image build
arguments, object-storage URLs, or this document.

Create the OAuth token directory separately:

```bash
sudo install -d -m 700 -o 10001 -g 10001 /opt/ideagen/oauth
```

Set non-secret release parameters and start the services:

```bash
export IMAGE_TAG=<git-short-sha>
export IDEAGEN_PUBLIC_SITE=https://<dashboard-host>
export IDEAGEN_DEFAULT_SNI=<dashboard-host>
cd <release-directory>/deploy
docker compose -p ideagen up -d scheduler dashboard proxy
docker compose -p ideagen ps
```

## Verification

```bash
docker compose -p ideagen exec -T dashboard \
  python3 -c "import urllib.request; print(urllib.request.urlopen(
  'http://127.0.0.1:8765/healthz', timeout=3).status)"
curl -fsS https://<dashboard-host>/healthz
```

Expected behavior:

- `/healthz` returns HTTP 200.
- An unauthenticated dashboard request returns HTTP 401.
- The business port is not exposed directly to the internet.
- Application state survives container replacement because it resides in the
  configured database and object store.

## Upgrade And Rollback

Build each release with an immutable tag. To upgrade, set `IMAGE_TAG` to the new
tag and recreate the services. To roll back, restore the previous tag and run:

```bash
docker compose -p ideagen up -d --force-recreate
```

---

## 当本机连不上实例时(2026-09-04 实测路径)

先分清是哪一类。本机代理会对**任何** TCP 连接假接受,所以「连上了又断开」不代表
实例有问题:

```bash
# 通过代理的 CONNECT 隧道对照测试
#   github.com:443 → 拿到 SSH banner   = 隧道本身正常
#   github.com:22  → 零字节            = 22 端口被封,与目标无关
#   example.com:80 → 零字节            = 80 也被封
# 结论:**这条通道**只有 443 出得去。
```

> 2026-09-05 更正:上面这条只对 `127.0.0.1:7897` 这条显式 CONNECT 代理成立。
> 浏览器走的是 TUN,`http://<eip>/review` 在 80 端口**能正常打开**(实测)。
> 两条出口路径的封禁规则不一样,别把 curl 的结果当成「用户也打不开」——
> 我差点因此去改安全组和端口映射,而用户那边一直是通的。
> 排障时 curl 用 `:443`(容器 80/443 都映射到 8765),验收时用浏览器走 80。

不经网络的诊断(零风险,先用这些):

```bash
ve ecs GetConsoleScreenshot --InstanceId <id>   # 看屏幕,确认系统起没起
ve ecs GetConsoleOutput     --InstanceId <id>   # 启动日志(base64 + ANSI)
ve ecs DescribeUserData     --InstanceId <id>   # 确认 UserData 写进去了
```

**执行通道的优先级**(从省事到麻烦):

1. `ve ecs RunCommand` —— 需要 Cloud Assistant。本账号的 Ubuntu 22.04 镜像上
   装了三次都失败(InstallFailed ↔ ReadyReboot 来回),别在这上面耗太久。
2. **改 UserData + 重启** —— API 接受、`DescribeUserData` 能读回,但实测
   cloud-init **没有执行**新的 bootcmd。不要假设它生效。
3. **建一台新实例带 UserData** —— cloud-init 在首次启动时必定执行 UserData,
   这是唯一验证过一定会跑的路径。不需要 SSH、不需要停机权限:

   ```bash
   ve ecs RunInstances --ZoneId ap-southeast-1a --ImageId <id> \
     --InstanceType ecs.t2-c1m2.large --InstanceChargeType PostPaid \
     --NetworkInterfaces.1.SubnetId <subnet> \
     --NetworkInterfaces.1.SecurityGroupIds.1 <sg> \
     --KeyPairName ideagen-ecs --Volumes.1.VolumeType ESSD_PL0 --Volumes.1.Size 40 \
     --UserData "$(base64 -i deploy/bootcmd_userdata.yaml)"
   # EIP 要单独申请并绑定：AllocateEipAddress --BillingType 3（不是 Billing.BillingType）
   ```

   注意参数陷阱:`--SubnetId` / `--SecurityGroupIds.1` 会被拒,必须写成
   `--NetworkInterfaces.1.*`;先 `--DryRun true` 验证再真建。

**密钥怎么进去**:不写进 UserData(云 API 会存下来且可读)。要么等 Cloud
Assistant 起来后用 RunCommand + TOS 预签名 URL,要么在 UserData 里放一个
短期预签名 URL 让实例自己拉 —— 后者的 URL 本身是凭证,过期即失效,可接受。

---

## 展示节点怎么保持最新（两条腿）

这台是**显示节点**,不跑周更;本机每天跑 daily,而好几个 agent 都在往 main 推。
两样东西都会漂:**数据**和**代码**。少任何一条,页面都会在「看起来完全正常」的
状态下停在过去——这才是危险所在,没人会因为「看着不对」去查它。

```
数据腿                                代码腿
本机 daily.sh                         几个 agent → origin/main
 └ push_state_to_cloud.py              │
   → tos://.../deploy/state/           │
        <UTC>-<sha12>.db               │
实例 ideagen-sync.timer               实例 ideagen-code.timer
  (开机+10min，此后每 15min)            (开机+6min，此后每 5min)
 └ sync_state.sh                      └ sync_code.sh
   容器内 pull_state.py 校验 sha         git fetch；有变化才动
   → 停容器 → 换库 → 删 -wal/-shm       → 构建到临时 tag
   → chown 10001 → 起容器               → **在新镜像里跑完整测试**
                                        → 过了才 tag :live 并重建容器
                                        → 没过就保留正在跑的那版
        \____ 共用 /var/lock/ideagen-deploy.lock ____/
```

**代码腿为什么必须有测试闸门**:好几个 agent 都能推 main,中间没有人。没有这道闸,
「谁能 push 谁就能改生产」同时也意味着「谁能 push 谁就能弄坏生产」。构建先用
临时 tag,过了测试才 `docker tag` 成 `:live` —— 否则 `:live` 会指向一个没人
担保过的镜像,而下一次重启就会悄悄用上它。

**为什么两条腿要共用一把锁**:换库要停容器,换代码要重建容器。交错执行时,
重建会把旧容器拉起来盖在换了一半的库上。

**代价**:这台是 1 vCPU。origin/main 真动了的时候,构建加跑测试要几分钟,
那几分钟页面会变慢(测试跑在 `nice -n 15 --cpus 0.6` 下,让服务优先)。
空转时只是一次 `git fetch`。

**UserData 有个看不见的 8KB 天花板**(base64 后)。超了之后网关回 HTML 错误页,
`ve` 报 `invalid character '<'` —— 一个**完全不提体积**的解析错误。加两个
systemd 单元就把它顶到了 7572/8192。所以现在 UserData 只是个引导器
(装 docker → clone → 跑 `deploy/display_node_bootstrap.sh`),2060 字节,
真正的步骤都在仓库脚本里,想多长都行。

> **配置不在任何一条腿上。** `runtime.env` 只在开机时写一次。代码腿重建容器时
> 会重新读它,但**不会重新拉取它** —— 所以改了配置内容,得重新部署一台,
> 光等自更新是等不来的。这一点和另一个会话在 compose 那台踩到的是同一个形状:
> 代码一直在上线,配置停在第一次引导那一刻,而所有健康信号都分辨不出这两者。


这台是**显示节点**,不跑周更;本机每天跑 daily,所以两边必然分叉。分叉的危险
不在于数据旧,而在于**页面看起来完全正常**:它照样有净值、有持仓、有回测,只是
停在部署那晚。没人会因为「看着不对」去查它。

```
本机 daily.sh
  └─ push_state_to_cloud.py   → tos: deploy/state/<时间>-<sha12>.db
                                        （每次新键,put 不可覆盖）
实例 systemd timer (15 分钟)
  └─ /opt/ideagen/sync_state.sh
       └─ 容器内 pull_state.py  → 列出前缀取最新,比对 sha,写 /data/ideagen.db.new
       └─ 停容器 → 换文件 → 删 -wal/-shm → chown 10001 → 起容器
```

几个不能省的地方:

- **快照必须走 `sqlite3.backup()`,不能拷文件。** 本机常驻着 `com.ideagen40.serve`,
  库是 WAL 模式,最近的写入还在 `ideagen.db-wal` 里。直接拷 `ideagen.db` 会得到
  一个结构完好、但少了最新几次运行的库 —— 又是那种「看着正常」的错。
- **换库前要停容器并删掉 `-wal`/`-shm`。** 新库配旧 WAL 读出来的东西两边都不是。
- **哈希标记跟着数据一起搬,不能提前写。** 拉成功但搬运失败时,若标记已前移,
  下次会认为「已是最新」而永远不再拉。所以 `pull_state.py` 把标记写在 dest 旁边,
  由宿主机在库真正落位后一起 mv。
- **拉取用实例自己的凭证,不用预签名 URL。** 预签名几小时就过期,同步链路会在
  部署两小时后安静地死掉 —— 和没有同步是同一个结果,只是更晚更难发现。
- **键名里带 sha。** 一次 list 同时给出顺序和内容身份,不需要额外的指针对象,
  也就不存在指针和数据互相矛盾的状态。

验证(部署时就跑一次,不等 15 分钟的第一次触发):开机日志里应有
`IG_TIMER enabled` 和 `IG_SYNC PULL_OK` / `IG_SYNC_OK <sha>`。

已知待办,两条:

1. **页面不会说自己的数据旧了。** dash.html 里那条「⚠ 该快照已 N 天未更新」只在
   静态模式(`window.__STATIC__`,GitHub Pages)下生效;云端跑的是活服务模式,
   `generated_at` 是**请求时**算的,所以同步哪怕断掉一周,页面照样显示当下时间。
   真正的数据新鲜度只能从 `weekly.as_of` 间接看出来。要修得让服务端把快照装载
   时间(`/data/.state-sha` 的 mtime,或快照键名里的时间戳)放进 `/api/state`,
   再让页面显示 —— 涉及 `ideagen/serve.py` 和 `web/dash.html`。
   在此之前,同步是否还活着只能看 `GetConsoleOutput` 里的 `IG_SYNC_*`。
2. `deploy/state/` 只增不删,每天约 48MB。`BlobStore` 故意没有 delete
   (见 base.py 的 immutability 注释),要清理得先决定是否给接口开这个口子。
3. **`/api/state` 的单条查询偏慢。** 2026-09-05 实测:云端全程 1.7–2.0s
   (`/healthz` 基线 0.38–0.95s),够用;本机首次 3.38s、第二次 0.87s。
   剖出来 0.83s 是 93 次 execute 分摊在 33 个 `db.q` 里,约 9ms/次 ——
   **不是 N+1,是单条查询在 48MB 库上慢,像缺索引**。本机偶发的 9–69s 是
   并发写(scheduler + daily 写同一个库)造成的,展示节点没有写入方,量不出来。
   要动就动索引,不要先加缓存掩盖。
