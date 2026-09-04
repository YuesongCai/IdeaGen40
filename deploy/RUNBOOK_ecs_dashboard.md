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
# 结论:本机只有 443 出得去。健康页放 80 永远看不到。
```

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
