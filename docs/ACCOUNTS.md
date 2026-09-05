# 账号与登录

运行台是给几个具体的人看的，不是给一把共享钥匙看的。这一页说清：谁有账号、
怎么加人、忘了口令怎么办，以及「登不进去」时先看哪里。

## 两种凭证，分别给谁

| | 给谁 | 长什么样 | 能说出「是谁」吗 |
|---|---|---|---|
| 账号口令 | 人 | `/login` 登录页，会话 cookie 存 30 天 | 能 |
| `IDEAGEN_DASH_KEY` | 机器 | `X-Dash-Key` 头或 `dashkey=` cookie | 不能 |

钥匙留给探针、脚本、部署工具。它是一个能力位：拿到就是进得来，服务端永远
说不出那是谁，也没有「改口令 / 加人 / 踢人」这些动作。所以人一律走账号。

本机（127.0.0.1 直连）不需要任何凭证；只要请求带了转发头（`X-Forwarded-For`
等），就按公网请求对待，必须有账号或钥匙。

## 两种角色，只有两种

- **成员（member）**：运行台的全部内容。
- **管理员（admin）**：另外还能加人、删人、改角色、替别人重置口令。

页面内容两者完全一样。参照的那套系统按租户带一串角色码、由网关校验——那是
给多客户多条线的产品用的；这里是五个人看一个面板，多一个角色就是多一条
没人会去测的规则。

## 现在有谁

名册是 `scripts/seed_accounts.py` 里的 `ROSTER`，不是某台机器上的一个文件。
一台重建出来的节点应该带着所有人回来，而不是只带运维自己。

| 账号 | 角色 | 是谁 |
|---|---|---|
| `yuesong` | 管理员 | Yuesong Cai |
| `jon` | 成员 | Jon，方法论评审 |
| `carl` | 成员 | Carl |
| `bytedance` | 成员 | 字节，合作方 |
| `yaojiaqi` | 成员 | Yao Jiaqi 姚佳琪 |

用户名大小写不敏感（`Jon` 和 `jon` 是同一个账号），只能用英文字母、数字和
`. _ -`。口令至少 8 位，服务端只存 scrypt 散列，**没有任何人能读回原文**。

## 日常动作

都在 `/account`：

- **加人** —— 填用户名、口令、备注、角色。
- **替人重置口令** —— 这台机器没有邮件也没有找回链接，忘了口令只能这样。
  重置会同时踢掉那个人所有设备上的登录。
- **改角色 / 删人** —— 最后一个管理员不能被降级，也不能被删。
- **改自己的口令 / 踢掉自己所有设备** —— 页面下半部分。

批量或新建节点用脚本：

```bash
# 本机
python3 scripts/seed_accounts.py --local --passwords data/seed_passwords.json

# 一台没有 shell 的节点：以管理员身份走登录页
python3 scripts/seed_accounts.py --http http://<IP> \
  --admin yuesong --password '<口令>' \
  --passwords data/seed_passwords.json

# 生产节点是 Caddy 自签兜底证书，且本机直连 TLS 被掐
export https_proxy=http://127.0.0.1:7897
python3 scripts/seed_accounts.py --http https://101.47.152.106 --insecure \
  --admin yuesong --password '<口令>' \
  --passwords data/seed_passwords.json
```

脚本是幂等的：**已存在的账号一律不动，口令也不动**。重跑它永远不该是某人
今天下午登不进去的原因。要给某个人换新口令，明确写 `--reset jon`。

`--passwords` 那个记事本让两台节点用同一套凭证——否则同一个人在两个长得
一模一样的地址上会有两个口令。它是明文，`.gitignore` 里已经排除，交付完删掉。

## 账号存在哪（这里出过一次大问题）

存的是一个 JSON 文件，不是数据库——这件事在数据库正是坏掉的那个东西的时候
必须还能用，而那也恰恰是最需要能登进运行台的时候。

**选路必须落在挂载上，不能落在容器里。** 曾经的默认回退是镜像里的
`/app/data`，而代码腿每次 `origin/main` 一动就 `docker rm -f` 重建容器
（main 上有好几个 agent 在推），于是**每次部署都把账号表清空一次**，下一个
请求时 bootstrap 又从 `runtime.env` 把那一个管理员重新建出来。症状是站点一直
正常、运维自己一直登得进去，只有「上周加的同事今天登不进去」这一条，
而且没有任何日志说过。

现在按顺序挑，每一档都自报「换容器还在不在」：

1. `IDEAGEN_ACCOUNTS_FILE`
2. `/run/ideagen-oauth/`（compose 部署挂的宿主机目录，**要可写才算**）
3. 数据库所在目录（展示节点是 `/data`，宿主机挂载）
4. `config.DATA` —— 在镜像里就是容器内路径，会被明确标成不持久

挑中的那一档在三个地方说出来：`/healthz` 的 `accounts` 字段、启动日志、
`/account` 页面顶部。不持久的时候那一行是页面上最响的东西。

```bash
curl -s http://<IP>/healthz | python3 -m json.tool   # 看 accounts.durable
```

生产节点另外开了 `IDEAGEN_ACCOUNTS_MIRROR=1`，每次写入顺带镜像一份到对象
存储，新容器起来时先找它再谈初始化。**镜像读不到时会拒绝新建管理员**，
而不是造一个新的把已有账号盖掉。

## 登不进去，按这个顺序查

1. **`/healthz` 有没有 `accounts.durable: true`** —— false 就是上面那个坑，
   加的人会被下一次部署清掉。
2. **是不是被限流了** —— 连错 3 次以后按 2 的幂退避，最多 5 分钟。页面会
   明说还要等几秒。换个网络或者等一下。
3. **口令对不对** —— 无法从服务端读回，只能让管理员在 `/account` 重置。
4. **是不是 403 `cross-origin request rejected`** —— 说明中间某一跳把
   `Origin` / `Referer` 都吃掉了。`Referrer-Policy` 必须是 `same-origin`，
   不能是 `no-referrer`（有测试盯着，见 `tests/test_login_reachable.py`）。
5. **登录成功但立刻又被弹回登录页** —— 会话 cookie 没发出来，或者签发的
   用户名跟存储里的对不上。`tests/test_account_lifecycle.py` 盯着这一条。

会话签名用的密钥是 `hmac(IDEAGEN_DASH_KEY, "session-v1")`，**不是钥匙本身**
——钥匙到处流动（查询串、请求头、cookie、runtime.env），拿它签会话等于把
「他们能进来」变成「他们能伪造任何人任何时长」。轮换钥匙会让所有会话失效，
这是正确行为不是副作用。

## 相关文件

- `ideagen/accounts.py` —— 存储、散列、会话、限流、角色
- `ideagen/authpages.py` —— 登录页、账号页、部署状态页（服务端渲染，
  它们必须在运行台渲染不出来的时候还能渲染）
- `ideagen/serve.py` —— `_authorized` / `_session_user` / `_auth_post`
- `scripts/seed_accounts.py` —— 名册与两种投递方式
- `tests/test_accounts.py`、`tests/test_account_lifecycle.py`、
  `tests/test_login_reachable.py`
