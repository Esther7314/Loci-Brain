# Loci 网关 · 把记忆递到眼前，再加一层「现在」

> 术语跟 [技术实现](../docs/技术实现.md) 对齐。零依赖，Node 18+。

---

## 一、是什么

Loci 的核心**不主动说话** —— MCP 是被动协议，工具没被调用就没有它的份。
这个网关是一层 **OpenAI 兼容的转发**：客户端把聊天请求发到这儿，它转给真正的模型，
路上把该让他知道的东西放进这一轮，再把回话原样流回去。

它做两层事：

| 层 | 做什么 |
|---|---|
| **记忆**（每一轮） | 你每说一句新的，问一次 Loci 的卡片，贴在你那句后面 · 你很久没说话之后回来的第一句，递上昨夜的梦或者「该发呆了」 |
| **present**（「现在」那一层） | **滚动窗口**：快满了提醒他自己压一段、换个新窗口接着聊；他不压、到了强制线网关替他压 · **夜里**把当天的原话交给 Loci 切段、写一份日报、拿日报开第二天的窗口 · **自动唤醒**：你不在的时候隔一阵叫醒他一次 · 他那时说的话**推到你手机上**（Bark），你下次开口时出现在他回话的最前面 · Loci 要看一条记忆依据的**原话**时，来网关取 |

### 哪些客户端能用

| 客户端 | 得到什么 |
|---|---|
| **能填 base URL 的**（OpenAI 兼容）：Kelivo、Cherry Studio …… | 记忆 + present，全套 |
| **在浏览器里开的聊天网页**（网页版客户端，从别的网址连到网关） | 全套，但要先把那个网页的地址加进 `LOCI_GATEWAY_ORIGINS`（见二·3 末尾）；不加一律 403 |
| **官方 App**（ChatGPT、Claude 等）、**Claude Code** —— 填不了 base URL | **只有记忆**：照旧直连 Loci 的 MCP，自己调工具。没有这一层（窗口、日报、唤醒、推送、卡片和梦的递送都没有） |

📌 只认 OpenAI 兼容的 `/v1/chat/completions`。别的 `/v1/*` 原样转发，不做任何事；
不是 `/v1/*` 的路径一律本地 404，不往上游发。

📌 API key 还是客户端的：原样转给上游，**不存、不写日志**。网关自己那一轮（唤醒、日报、强制打包）
缺省借你最近一次请求带来的那把，只放在内存里（见三·1 的 `LOCI_UPSTREAM_KEY`）。

### 平常一轮长什么样

```
客户端 ──POST /v1/chat/completions（整段历史）──► 网关
  ① 认出是哪个对话、哪句是新的 → 你这句先落盘（先存后转）
  ② 问 Loci 的卡片 · 梦 / 发呆（久别第一句）· 水位过线了就在末尾递一句提醒
  ③ 组装发上游的那份：[客户端 system] [carry] [mark 之后的原话 + 之前塞过的东西原地重放] [你这句]
  ──► 上游
  ◄── 回给客户端：模型的字原样流回，只摘掉两样（他写的压缩块 · 你没要的 usage 块）
  ④ 回话落盘 · 记下这一轮用了多少 token（= 水位）· 他写了压缩块 → 这一轮说完换窗
```

**客户端那份历史一个字都不动** —— 发上游的是网关另组的一份。
carry = 换窗时留下来的那段（日报 / 他自己写的摘要），替掉 mark 之前的旧话。

---

## 二、装起来

一共四步：起网关 → Loci 那边加一个宿主 → 客户端建一个专用服务商 → 面板 present 页看一眼。

### 1. 起网关

```bash
LOCI_UPSTREAM=https://api.deepseek.com/v1 \
LOCI_GATEWAY_TOKEN=<随便一串够长的> \
LOCI_HOOK_TOKEN=<Loci 那边给网关的钥匙，见第 2 步> \
LOCI_GATEWAY_DAYS=/path/to/Loci-Brain/buckets/_hosts/gateway \
LOCI_TZ=Asia/Shanghai \
node gateway/server.js
```

起来之后第一行是 `[gateway] up on http://127.0.0.1:3100`，后面几行把每一项实际用的值都打出来
（原话落哪、按哪个时区、卡片超时、present 的口开没开）—— 配错了一眼能看出来。

**全部环境变量**（启动时读一次，改了要重启；面板不碰这些）：

| 环境变量 | 缺省 | 干什么 |
|---|---|---|
| `LOCI_UPSTREAM` | 无，**必填** | 真正的模型在哪，OpenAI 兼容地址（`…/v1`）。不设起不来 |
| `PORT` | `3100` | 网关听哪个端口。`0` = 随便挑一个，启动第一行告诉你挑了哪个 |
| `LOCI_GATEWAY_BIND` | `127.0.0.1` | 网关听哪个地址。不是回环地址（比如 `0.0.0.0`，让手机从局域网连）时**必须**同时设 `LOCI_GATEWAY_PASSPHRASE`，不然起不来 |
| `LOCI_GATEWAY_PASSPHRASE` | 无 | 非回环绑定时，**每条路径**前面都要带 `/<口令>/`（`/<口令>/v1/…`、`/<口令>/present`、`/<口令>/loci/source`、`/<口令>/health`），没带一律 404。16–128 个字母、数字或 `. _ ~ -`。进门就摘掉，不往上游发、不进日志。回环绑定时不用带；设了也认带口令的路径 |
| `LOCI_GATEWAY_ORIGINS` | 无 | 哪些**网页**可以连网关，逗号隔开，照浏览器地址栏写 `协议://域名[:端口]`（如 `https://chat.example.com`）。不设也照常能用的：不带 Origin 的原生客户端（Kelivo、curl、各家 SDK）、本机网页（`http://localhost:…`、`http://127.0.0.1:…`）、桌面壳（`tauri://…`、`app://…` 这类不是 http(s) 的来源）。见二·3 末尾 |
| `LOCI_GATEWAY_HOSTS` | 无 | 回环绑定时，除了 `localhost` / `127.x.x.x` / `[::1]` / `*.localhost`，还认哪些名字寄来的请求（hosts 文件里自己起的名、本机的反向代理），逗号隔开。别的名字一律 403（防 DNS 重绑定）。非回环绑定时不看这个，靠口令 |
| `LOCI_MCP` | `http://127.0.0.1:18002/mcp` | Loci 在哪（`/mcp` 结尾；REST 口挂在同一个根上） |
| `LOCI_HOOK_TOKEN` | 无 | **网关 → Loci** 的钥匙：问卡片、取梦、夜里交原话、自己那一轮调 Loci 工具时，放在 `x-loci-hook-token` 请求头里（不进 URL）。值 = Loci 那边 `hosts.gateway.token_env` 指的那个环境变量的值 |
| `LOCI_GATEWAY_TOKEN` | 无 | **Loci → 网关** 的钥匙：`/present/*`（面板 present 页经 Loci 代问）和 `/loci/source`（Loci 来取原话）只认 `Authorization: Bearer <它>`，错了 401。**不设 = 这两族口关着**：`/present/*` 回 404，`/loci/source` 回 503（Loci 当「暂时取不到」，退回记忆自己的正文）。值 = Loci 那边 `hosts.gateway.fetch_token_env` 指的那个环境变量的值 |
| `LOCI_GATEWAY_NAME` | `gateway` | 这个网关在 Loci 那边叫什么：`/present` 回执里的 `host`，原话来源的 `instance`。跟 Loci `hosts:` 里那个宿主的名字写成一样 |
| `LOCI_UPSTREAM_KEY` | 无 | 网关自己那一轮（唤醒、日报、强制打包）用的上游钥匙，可以是一把限额的。**不设**就借你最近一次请求带来的那把（只在内存里），网关重启后要等你先说一句才能唤醒 / 写日报。只从环境变量读，面板只看得到「设了没有」 |
| `LOCI_GATEWAY_DATA` | `gateway/data` | 网关的**私账**目录：设置、窗口状态（含压缩文）、日志。见第八节 |
| `LOCI_GATEWAY_DAYS` | `<LOCI_GATEWAY_DATA>/host` | 原话（`days/<日期>.jsonl`）和日报（`reports/<日期>.md`）落哪。设成 `<buckets>/_hosts/<LOCI_GATEWAY_NAME>` 才会跟着 Loci 的导出走；不设就留在网关自己的目录里，不碰任何库 |
| `LOCI_TZ` | 本机时区 | 「一天」按哪个时区切（IANA 名，如 `Asia/Shanghai`）。日报时段、免打扰、唤醒时间段都按它。认不出的名字退回本机时区，启动时打一行 ⚠️ |
| `LOCI_OWNER_NAME` | `用户` | 你说的那几行署谁的名：夜里交给 Loci 切段时、Loci 来取原话时都署这个。跟 Loci 用的是同一个变量名，写成一样 |
| `LOCI_AI_NAME` | `AI_NAME`，再没有就 `AI` | 同上，他说的那几行署的名 |
| `POKE_IDLE_MINUTES` | `210` | 闲时闸：你多久没说话之后的第一句才递梦 / 发呆（第九节） |
| `LOCI_CUE_TIMEOUT_MS` | `3000` | 等 Loci 的卡片多久。等不到这一轮就不贴，照发 |
| `LOCI_PACK_WAIT_MS` | `90000` | 强制打包还在跑时，你的下一轮最多等多久（等到了就带着新窗口走，等不到就照原样发，打包接着跑） |
| `LOCI_BARK_BASE` | `https://api.day.app` | Bark 推送码拼在哪个服务器后面。自己搭了 Bark 服务器就改它（或者在 present 页直接贴整条 URL） |
| `LOCI_GATEWAY_TEST_CLOCK` | 无 | **只给测试用**：指向一个文件，网关的「现在」就是文件里那个毫秒数。平时别设 |

`RELEVANCE_STRONG_WORDS` · `RELEVANCE_WEAK` · `RELEVANCE_TIMEOUT_MS` · `RELEVANCE_MIN_SCORE` 只管
`auto_attach.js` 这个模块（给有自己网关的人用，第九节），这个外壳不读。

### 2. Loci 那边加一个宿主

在 Loci 的 `config.yaml`（Docker 起法是 `buckets/config.yaml`）里写：

```yaml
hosts:
  legacy:                                   # 你原来直连 Loci 的 MCP 客户端
    token_env: LOCI_HOOK_TOKEN
    scope_mode: open
  gateway:                                  # 名字跟网关的 LOCI_GATEWAY_NAME 一样
    token_env: LOCI_HOST_TOKEN_GATEWAY      # 网关进 Loci 的钥匙
    scope_mode: open                        # 必须是 open，见下
    authority:                              # 这个网关的原话归它管：夜里交切片要它
      - {system: gateway, instance: gateway}
    provides:                               # 这个网关的原话由它来给
      - {system: gateway, instance: gateway}
    fetch_url: "http://127.0.0.1:3100/loci/source"
    fetch_token_env: LOCI_FETCH_TOKEN_GATEWAY   # Loci 进网关的钥匙
    present_url: "http://127.0.0.1:3100"        # 只写到根，Loci 自己往后拼 /present/…
```

再在 **Loci 的** 环境变量（Docker 起法是 `.env`）里给这两个名字填值，然后重启 Loci：

```
LOCI_HOST_TOKEN_GATEWAY=<一串>      ← 网关那边 LOCI_HOOK_TOKEN 填同一串
LOCI_FETCH_TOKEN_GATEWAY=<另一串>   ← 网关那边 LOCI_GATEWAY_TOKEN 填同一串
```

两把钥匙，一个方向一把，**不能是同一串**（Loci 发现两边一样就不用那把取原话的）：

```
网关 ──LOCI_HOOK_TOKEN──────────► Loci      Loci 认：hosts.gateway.token_env
Loci ──LOCI_GATEWAY_TOKEN───────► 网关      Loci 拿：hosts.gateway.fetch_token_env
```

⚠️ 网关那边的变量也叫 `LOCI_HOOK_TOKEN`，但填的是 **gateway 这个宿主**的那串，不是 Loci 自己
`LOCI_HOOK_TOKEN`（legacy）那串。填成 legacy 的，网关在 Loci 眼里就成了 legacy：卡片照有，
夜里交切片却被拒（legacy 不管网关的原话）。两个宿主也不能共用一串，共用的话哪个都不认。

每一行在干什么：

| 键 | 干什么 | 不写会怎样 |
|---|---|---|
| `token_env` | 网关来问卡片、取梦、交切片、调工具时，Loci 凭它认出「这是网关」 | 这个宿主不生效（Loci 日志里记一行） |
| `scope_mode: open` | 网关的请求不带 `Loci-Scope`，只有 open 的宿主不带它也能读 | 缺省是 restricted：问卡片、他自己那一轮调 `recall`，什么都拿不到 |
| `authority` | 这个网关的原话，改动归它报、切片归它交 | 夜里交切片被 Loci 回 403；被拒的那几批记下原因，**不再重交** |
| `provides` + `fetch_url` | 模型要看原话（`recall(view="original")`）时，Loci 去这儿取 | 原话取不到，模型只看记忆自己的正文 |
| `fetch_token_env` | Loci 去取原话、代问 present 页时带的钥匙 | present 页显示「Loci 对宿主 gateway 没有钥匙」 |
| `present_url` | 面板 present 页去哪儿代问 | present 页「还没接上」 |

网关设了口令（`LOCI_GATEWAY_PASSPHRASE`）的话，两个地址都带上它：
`http://127.0.0.1:3100/<口令>/loci/source`、`http://127.0.0.1:3100/<口令>`。

⚠️ **写 `hosts:` 这一节之前，知道三件事**（Loci 那边的规矩，`core/scope.py`）：

1. **写了这一节就只认表上的。** 原来不带钥匙就能进的（直连 Loci 的 MCP 客户端、没上锁的面板）
   都是 `legacy`；要留着就像上面那样自己写进来。
2. **面板必须先设密码。** 写了表、面板没上锁，面板的路由一律 401。
3. **MCP 鉴权关着（Docker 缺省 `LOCI_MCP_REQUIRE_AUTH=false`）时，不带宿主钥匙的 MCP 请求一律拒。**
   你直连 Loci 的 MCP 客户端（Claude Code 等）要在请求头带上 legacy 那把：
   `x-loci-hook-token: <LOCI_HOOK_TOKEN 的值>`，或者 `Authorization: Bearer <同一串>`。

📌 **为什么是 `scope_mode: open`、为什么不写 `max_grant`**：网关问卡片、取梦、自己那一轮调 Loci 的工具，
请求里都**不带 `Loci-Scope`**（`own_turn.js` 只带 `x-loci-hook-token` 和写入键 `Loci-Turn`）。
Loci 那边只有 open 的宿主不带 `Loci-Scope` 也能读全库，别的宿主不带就什么都不给
（`core/scope.py` `RequestScope.resolve`）。卡片要在你整个库里找，所以它就该是 open。
写了 `max_grant`（哪怕是 open 的宿主）Loci 会把它当成「有天花板的宿主」，要求 MCP 鉴权打开、
面板上锁，两样都满足之前拒它 —— 网关用不着天花板，就别写。

### 3. 客户端：给他单独建一个服务商

1. **新建一个自定义服务商**（OpenAI 兼容），base URL 填 `http://127.0.0.1:3100/v1`，
   API key 填你那家上游的 key，模型照常选。
   **从这个服务商进来的每一条请求都算你跟他的对话** —— 落盘、算水位、会被叫醒。
2. 客户端里**起标题、翻译、总结**这类杂活用的模型，选**别的服务商**。
   不然这些请求也会被当成对话记下来。
3. **上下文条数开到最大 / 不限。** 窗口归网关管：客户端自己截掉的历史要是截过了 mark，
   这一轮 carry 就带不上。
4. **客户端自己的「自动压缩 / 上下文压缩」关掉。** 不然两边一起压，你的历史被剪两遍；
   客户端拿摘要换掉的旧话，网关还会当成你改了历史。
5. 系统提示照常贴 [`docs/系统提示-中文.md`](../docs/系统提示-中文.md)（或 [English](../docs/系统提示-英文.md)）。
   不用加任何标记。

手机上的客户端要连电脑上的网关：`LOCI_GATEWAY_BIND=0.0.0.0` ＋ `LOCI_GATEWAY_PASSPHRASE=<口令>`，
base URL 填 `http://<电脑的局域网 IP>:3100/<口令>/v1`。**别把网关裸开在局域网上 —— 卡片里是你的记忆。**
口令是门，不是加密：出了家门要用，走你自己的 VPN / 隧道。

**网页也是一扇门。** 网关只听本机，挡得住别的电脑，挡不住你浏览器里开着的别的网站：
任何网页都能悄悄往 `127.0.0.1:3100` 发一条请求，借 DNS 重绑定甚至能读到回话。而一条聊天请求
还没到上游验钥匙，就已经落了一句原话、开了一个对话、打断了唤醒、重置了你的「静了多久」。所以网关进门先看两样：

- **寄给谁（Host）**：回环绑定时，请求必须是寄给本机名字的（`localhost`、`127.x.x.x`、`[::1]`、`*.localhost`），
  或者你在 `LOCI_GATEWAY_HOSTS` 里列过的名字。重绑定的网页用的是它自己的域名，在这儿就被挡了。
- **从哪个网页来（Origin）**：浏览器发请求会带上来源网页，原生客户端不带。
  - 不带 Origin → 原生客户端，照常进（Kelivo、curl、SDK 都是这样）。
  - 来源在 `LOCI_GATEWAY_ORIGINS` 里 → 进。
  - 来源是本机网页（`http://localhost:…`、`http://127.0.0.1:…`、`http://xxx.localhost`）或者不是 http(s) 的桌面壳
    （`tauri://localhost`、`app://…`）→ 进：别的网站冒充不了这些。
  - 别的一律 403，**包括 `null`**（沙盒 iframe、`data:` 网页发的就是 `null`）。
  - 浏览器没带 Origin、但 `Sec-Fetch-Site` 说是别的网站发来的写请求 → 403。

**用网页版客户端**（在浏览器里打开、从别的网址连网关的那种）：把它的地址照浏览器地址栏抄进
`LOCI_GATEWAY_ORIGINS`（`https://chat.example.com`，带端口就带上端口），重启网关。被挡的那条在网关日志里是一行
`403 (origin https://…)`，抄那个就对。只有确定要用、并且信得过那个网站的才加 —— 加进去的网站就能替你跟他说话。
某个桌面客户端被挡、日志里写的是 `origin null`，可以把 `null` 加进去，但这同时放进了所有沙盒网页，能不加就别加。

聊天请求还必须带 `Content-Type: application/json`（OpenAI 兼容的客户端都带）。不带的回 415、不往上游发 ——
网页不打招呼就能发的只有 `text/plain` 这类，网关不当它是聊天。

### 4. 面板 present 页

面板只跟 Loci 说话，Loci 把 present 页的请求转给网关（`present_url`）。在这儿：

- **自助压缩**：上下文窗口多大（见第三节）· 水位线 · 留多少条原话 · 「现在压」
- **昨日日报**：几点到几点写 · 你静够多久才写 · 换窗方式（每天 / 每 N 天 / 手动）· 日报原文 · 「补一份」
- **自动唤醒**：开关（缺省**关**）· 多久一次 · 免打扰 · 时间段
- **推送**：Bark 推送码（或整条 URL）· 「试推一条」
- **高级设置**：三张提示词卡（压缩 · 日报 · 唤醒），改了存网关，随时能恢复缺省

设置存在网关的 `<LOCI_GATEWAY_DATA>/present.json`，下一拍（一分钟内）生效，不用重启。
面板上没有的几项（`wake.daily_cap` 每天最多叫几次 · `wake.dry_run` 只记账不真叫 ·
`wake.allow_short` 允许间隔小于 15 分钟 · `own.tool_rounds` · `own.models` 模型降级链 ·
`push.text` 推全文还是只推「ta 说了句话」）直接改这个文件。

### 5. 看它在不在干活

```bash
curl http://127.0.0.1:3100/health
```

问的不是「进程在不在」，是**「上一次真干成是什么时候」**：卡片最近问了几次、Loci 回没回；
`present` 那一节是上次日报 / 唤醒 / 压缩 / 推送各自多久之前、之后连败几次、攥着几条他说的话。
**别看聊天界面判断 —— 失败从来不挡聊天，界面上永远是正常的。**

每一次唤醒 / 被挡下 / 打包 / 撞墙 / 交切片在 `<LOCI_GATEWAY_DATA>/logs/present.jsonl` 里都有一行
（只有数字和原因，没有字）。

---

## 三、要记住的几行

- 客户端的「上下文条数」开到最大 / 不限，「自动压缩 / 上下文压缩」关掉 —— 不然两边一起压，你的历史被剪两遍。
- 在客户端里给他**单独建一个服务商**指向网关：从这个服务商进来的都算他。起标题、翻译那类在客户端里选别的服务商。
- **上下文窗口多大，网关自己认**，依次取：你在 present 页填的 → 撞过墙后记住的上限（报错里写了数就记那个数；没写、
  而且按原来认的窗口说不通，就先按撞墙前最后一次成功的那一轮记着 —— 这是估的，一天后作废，中间有更大的一轮成功就往上调）→ 服务商模型列表报的 →
  网关自带的常见模型表（按模型名对）→ 都没有就当 1M。present 页「自助压缩」那格标着这个数从哪来
  （`user` / `learned` / `provider` / `table` / `default`）。不对就在那格填你自己的数，填了以你的为准；清空就回到自动认。
- 自动唤醒缺省关。打开之前想清楚：每醒一次就是一整轮的钱；每天最多 16 次。
- 他压出来的那段字你永远看不到，这是故意的。
- 夜里电脑要开着，日报在 4–8 点、你静够 30 分钟之后写。
- 网关重启后，唤醒和日报要等你先说一句（借你那把钥匙）；不想等就配 `LOCI_UPSTREAM_KEY`。
- 想让他在你不在的时候说的话推到手机上：装 Bark，把推送码贴进 present 页，点「试推一条」。
- 手机上的客户端要连电脑上的网关：先看 `LOCI_GATEWAY_BIND` 那一行，别把网关裸开在局域网上 —— 卡片里是你的记忆。
- 在浏览器里用的网页版客户端，先把它的地址加进 `LOCI_GATEWAY_ORIGINS`；Kelivo 这类原生客户端不用管。

---

## 四、花多少钱

**一句话：你的那一轮永远不等网关；网关自己的那一轮每次都是真钱。** 下面只给式子，数你自己代。

记号：

| | |
|---|---|
| `P_in` · `P_cache` · `P_out` | 上游每 token 的输入价 · 缓存命中价 · 输出价 |
| `r` | 这一轮里他调了几圈工具（0 到 `own.tool_rounds`，缺省最多 6）。每多一圈 = 再发一整轮 |
| `O` | 他这一轮写出来的 token |

```
一次唤醒   ≈ (1 + r) × (命中缓存的部分 × P_cache + 没命中的部分 × P_in) + O × P_out
             输入 = 上一轮聊天发上游的那份（一个窗口）+ 他上一句 + 之前几次唤醒的信和话 + 这次的信（+ 梦全文）
             前缀跟上一轮一字不差，上游缓存还在就大半按 P_cache；隔太久缓存过期，就全按 P_in

一份日报   ≈ (1 + r) × (日报信 + 待写的切片 + 上一份日报之后的全部原话) × P_in + 日报 × P_out
             新窗口，第一圈没有缓存

一次强制打包 ≈ (1 + r) × (system + 压缩信 + 旧摘要 + 要压掉的那段原话) × P_in + 摘要 × P_out
             打包自己撞墙时，砍掉最老的几行再发，每砍一次多发一次

一次撞墙   ≈ 被拒的那一次（收不收钱看上游）+ 砍过之后重发的一次（一个窗口的输入）

一天       ≈ 唤醒次数 × 一次唤醒 + 日报份数 × 一份日报 + 强制打包次数 × 一次打包 + 撞墙次数 × 一次撞墙
```

- 唤醒次数 ≤ `wake.daily_cap`（16）；他说了还没回的话到 `wake.held_cap`（缺省不限）也停叫。
- 日报份数跟换窗方式走：每天换 = 每天一份；每 N 天 = 每 N 天一份；手动 = 你按一次一份。
- **花了钱的失败也算钱**：请求发出去了才失败（上游回了任何错误、流断了、10 分钟总闹钟到点），
  照样计入上限，下次间隔 ×2、×4 退避。没发出去的（没钥匙、连不上上游）不算。
- 几乎不花钱的：**自助压缩**（提醒几十个字，摘要是他在你那一轮里多写的一段输出）、卡片、梦 ——
  这些都在你自己那一轮里，跟你的消息一起付。
- 夜里切段花的是 **Loci 打标模型**的钱（`LOCI_COMPRESS_*`），不是上游的。
- 缓存保不保得住看上游：多久过期、要不要显式开，每家不一样。唤醒间隔缺省 60 分钟，是照「一小时之内
  缓存还在」定的，你那家不是这样就自己调。

---

## 五、做不到的（老实说）

1. **客户端看不见压缩**，也看不见 carry —— 你没法读、没法改他压出来的那段（故意的，第八节）。
2. **推不进客户端**：他不在你眼前说的话，只能 Bark 推一条、等你下次开口时以「（你不在的时候我说过：……）」进历史。你一直不开口，客户端里就一直没有。
3. **分不清删除和截断**：客户端少发了几条，网关不知道是你删的还是客户端自己截的，所以从不据此去改记忆。
   只有「改了」才报：已经交给 Loci 的一句，客户端里显示成改过的样子（你编辑了他的回话），网关就告诉 Loci
   这一句换成了新版本（`revised`）。这一条先记在网关账上再发，Loci 那会儿没开、网关重启了都不丢，下一拍接着发。
   **重新生成不报**：被换掉的那句字没变，Loci 那边留着它也是真说过的话。
4. **从这个服务商进来的全算他**：网关不看请求里写了什么，只看它是从哪进来的。杂活混进来也会被记下 ——
   所以要单独建服务商。同时开好几个对话，唤醒只接着最近一个他回过话的对话（刚开、第一句就失败的新对话跳过）；
   他那时说的话，你在哪个对话里开口就出现在哪个对话里。
   两分钟内用一模一样的第一句开两个新对话，网关分不出「第二个对话」和「把第一句的回话重新生成」，
   就当成两个对话：两边的回话都留着。代价是真重新生成第一句的回话时，旧的那句也会作为一段一句话的对话交给 Loci。
5. **窗口大小是认出来的，不是量出来的**：服务商不报、模型表里没有，就当 1M。认大了会撞墙（有出路，但那一轮要多等）；
   认小了会早压、白丢字。不对就在 present 页填。
6. **缓存保不保得住看上游**：工具表一变、system prompt 一改、隔太久，缓存就没了。
7. **电脑得开着**：夜里关机 = 不写日报（白天你静下来时补昨天那一份，只补昨天）；唤醒、推送都停。
8. **只有时钟**：唤醒只看「过了多久」，没有「他想你了」那种触发。
9. **附件只留占位**：原话里记 `[image]`，换窗以后图不会再发给模型。
10. **Bark 只有 iPhone**；推送内容经过 Bark 的服务器（自己搭一个就只经过自己的）。
11. **Loci 在 Docker 里来问原话**，要能连到网关：Docker Desktop 上写 `host.docker.internal`；Linux 没实测过（见第六节）。
12. **官方 App、Claude Code**（填不了 base URL）：只有记忆，没有这一层。
13. **一个数据目录只能跑一个网关**：两个网关共用一份 `LOCI_GATEWAY_DATA` 会互相踩账，而且两个都会叫醒他 —— 双倍的钱、两个他。所以网关一起来先在数据目录里占一个 `gateway.lock`（里面是它的 pid），占不到第二个就不启动；上一个崩了、被强杀留下的锁，pid 已经不在了，下一个起来时直接接过去。

---

## 六、常见错误

| 你看到的 | 多半是因为 | 怎么办 |
|---|---|---|
| 网关起不来：`LOCI_UPSTREAM is not set` | 没设上游 | 设 `LOCI_UPSTREAM=<那家的 /v1>` |
| 网关起不来：`LOCI_GATEWAY_BIND=… is not a loopback address` | 绑了非回环地址（`0.0.0.0`、局域网 IP）却没设口令 | 设 `LOCI_GATEWAY_PASSPHRASE`（16 位以上）；或者改回 `127.0.0.1` |
| 网关起不来：`Another gateway (pid …) is already running on LOCI_GATEWAY_DATA=…` | 这个数据目录上已经有一个网关在跑（第五节第 13 条） | 留一个就够：停掉那个，或者给这个另设一个 `LOCI_GATEWAY_DATA`。那个 pid 根本不是网关（锁是很久以前留下的、pid 又被别的程序用上了），就手动删掉报错里写的那个 `gateway.lock` |
| 网关起不来：`LOCI_GATEWAY_PASSPHRASE must be 16–128 characters` | 口令太短，或者有 URL 路径里会被改写的字符 | 只用字母、数字和 `. _ ~ -`，16–128 位 |
| 网页版客户端连网关回 **403**，「这个请求来自网页……不在 LOCI_GATEWAY_ORIGINS 里」 | 网关只放原生客户端和本机网页进来（二·3 末尾） | 把网关日志里 `403 (origin …)` 那个地址加进 `LOCI_GATEWAY_ORIGINS`，重启网关 |
| 回 **403**，「这个请求是寄给……的，不是本机的名字」 | 用了 hosts 文件里自己起的名字或者反向代理访问回环绑定的网关 | 把那个名字加进 `LOCI_GATEWAY_HOSTS`，重启网关 |
| 聊天回 **415** | 请求没带 `Content-Type: application/json`（多半是手写的 `curl -d`） | 加上 `-H "Content-Type: application/json"` |
| 面板 present 页整页「还没接上」 | Loci 的 `hosts:` 里没有一个宿主写了 `present_url`（或者写了但 Loci 没重启） | 照第二节第 2 步加上，重启 Loci |
| present 页「连不上网关」/「5 秒内没回话」 | 网关没起、`present_url` 写错、Loci 在 Docker 里连不到本机的 `127.0.0.1` | 先 `curl <present_url>/health`；Docker 见最后一行 |
| present 页「Loci 对宿主 gateway 没有钥匙」 | 没写 `fetch_token_env`，或者那个环境变量没值，或者值跟 `token_env` 那把一样 | 写上、填值（跟网关的 `LOCI_GATEWAY_TOKEN` 一样，跟 `token_env` 那把不一样），重启 Loci |
| present 页「网关不认 Loci 的钥匙（HTTP 401）」 | 网关设了 `LOCI_GATEWAY_TOKEN`，但 Loci 带来的不是这一串 | 两边对一下：Loci 的 `fetch_token_env` 那个变量 = 网关的 `LOCI_GATEWAY_TOKEN` |
| 直接打 `/present` 回 **401** | 门开着，钥匙不对或者没带 `Authorization: Bearer` | 同上 |
| 直接打 `/present` 回 **404**，正文说「关着：网关没设 LOCI_GATEWAY_TOKEN」 | 网关没设 `LOCI_GATEWAY_TOKEN`，这族口关着 | 设上，重启网关 |
| 直接打 `/present` 回 **404**，正文只有 `not found` | 网关绑在非回环地址上，路径里没带口令 | 路径前面加 `/<口令>`；`present_url` / `fetch_url` 也要带 |
| 面板一打开就 401「写了 hosts 表，面板就必须先上锁」 | 写了 `hosts:`，面板还没设密码 | 面板「账号」里设一把密码 |
| 写了 `hosts:` 之后，直连 Loci 的 MCP 客户端连不上（`No host credential`） | MCP 鉴权关着时，表上的人才进得来 | 那个客户端请求头带上 legacy 的钥匙（第二节第 2 步那三件事） |
| 没有卡片；`/health` 说一直失败 | Loci 没起 / `LOCI_MCP` 不对 / 网关的 `LOCI_HOOK_TOKEN` 跟 Loci 那边 `token_env` 的值对不上 / 宿主不是 `scope_mode: open` | 照 `/health` 里的错误看；钥匙两边对一下；`scope_mode: open` |
| `/health` 说 Loci 回了但「no card」 | 不是故障：库里没有跟这几句对得上的 | 不用管 |
| 日报那行一直写着 `no_key` | 网关重启后还没钥匙，该写的日报在等：夜里照交原话，日报等你先说一句、再静够了才写（不会每分钟白试一次） | 说一句就好；不想等就配 `LOCI_UPSTREAM_KEY` |
| **一直不唤醒** | ① 唤醒没开（缺省关）② **网关重启后还没钥匙**：要等你先说一句 ③ 在免打扰里（缺省 23:00–08:00）或不在你设的时间段里 ④ `wake.dry_run` 开着：到点只记一行 `would_wake`，不真叫 ⑤ **今天的日报该写还没写**：早上第一声等日报 ⑥ 今天到上限了 / 他说的话攥到 `held_cap` 了 ⑦ 还没有一个能接着说下去的对话（刚装好、一句都没聊过） ⑧ `present.json` 读不出来或唤醒那节填坏了：读不出来就不叫 ⑨ **唤醒撞墙了**：上一轮对话加上唤醒信已经超过窗口，先丢掉前面几次唤醒的来回再试一次，还撞就停在这一轮，等你下次开口或者换窗 | 看 present 页「自动唤醒」那行的「下一次为什么不准点」，或者 `logs/present.jsonl` 里的 `wake_gate`。② 配 `LOCI_UPSTREAM_KEY` 就不用等 |
| 间隔填 2 分钟被拒 | 间隔最小 15 分钟 | 试的时候在 `present.json` 里开 `wake.allow_short`，试完关掉 |
| 「试推一条」失败：`skipped_config_missing` | 推送码没填 | present 页填推送码或整条 Bark URL |
| 「试推一条」失败：`error`，「超时：Bark 没回」或「连不上 Bark：……」 | 网关这台机器连不上 Bark 服务器（Node 自带的 fetch 不走系统代理）；自建服务器地址写错 | 在网关那台机器上 `curl https://api.day.app` 试试；自建的检查 `LOCI_BARK_BASE` 或整条 URL |
| 「试推一条」失败：HTTP 400 / 4xx | 多半是推送码不对（重装过 Bark 会换码） | Bark App 里重新复制推送码 |
| 试推能到，他唤醒说的话推不到 | 那会儿在免打扰里（试推不管免打扰，真推送管）；或者失败重试 15 分钟后放弃了 | 话还在：你下次开口时出现在他回话最前面 |
| 某一轮特别慢，`logs/present.jsonl` 里有一行「撞墙」 | **撞墙**：上游说上下文太长了（窗口认大了，或者服务商把窗口切得比纸面小） | 不用管：网关砍掉最老的几轮重发一次，记住这个模型的上限（来处变成 `learned`；报错里没写数时是估的，那一行的 `words` 说怎么估的），再排一次打包。老撞就在 present 页把窗口填小一点。自助压缩关着时每次撞都只砍不压 |
| 到了强制线却不压了，`/health` 的 compress 里有 `held_back_words` | 压了也降不下来：客户端的 system、摘要和「压缩完留多少条原话」已经快占满窗口（窗口小、人设长，或者窗口认小了），每压一次都是一整轮的钱 | 照那句话改：调大上下文窗口、调少留的原话或调高强制压缩线；「现在压」照样能压。撞墙的出路不受影响 |
| 夜里交切片一直失败，原因是 403；或者 `/health` 里 `report.changes` 说改过的句子报给 Loci 被拒（`forbidden` · `not_change_authority`） | Loci 那边这个宿主没写 `authority`，或者网关的 `LOCI_HOOK_TOKEN` 填成了 legacy 那串 | 照第二节第 2 步改好，重启 Loci。被拒过的那几批 / 那几句不会重发，之后的照常发 |
| 导出包里没有原话 | 没设 `LOCI_GATEWAY_DAYS`，原话留在网关自己的目录里 | 设成 `<buckets>/_hosts/<LOCI_GATEWAY_NAME>`；已经写下的从 `<LOCI_GATEWAY_DATA>/host/` 挪过去 |
| 他回话最前面多了「（你不在的时候我说过：……）」 | 不是错：他唤醒时说过话，这是那几句 | — |
| **Loci 在 Docker 里，取原话 / present 页连不上网关** | 容器里的 `127.0.0.1` 是容器自己 | **Docker Desktop**（Windows / Mac）：`fetch_url`、`present_url` 里的 `127.0.0.1` 换成 `host.docker.internal`。**Linux：没实测过**，思路是 compose 里给 Loci 加 `extra_hosts: ["host.docker.internal:host-gateway"]`，网关绑到容器够得着的地址 —— 那就不是回环了，得设口令。试通没有：`docker exec loci-brain python -c "import urllib.request as u; print(u.urlopen('http://host.docker.internal:3100/health').status)"` |

---

## 七、隐私

- 🔴 **他压出来的那段字（carry）谁都看不到，这是设计。** 它只住在网关的私账里
  （`<LOCI_GATEWAY_DATA>/threads/<对话>.json`）：不进原话文件、不进库、不随 Loci 导出、
  任何口（`/present/*`、`/health`、`/loci/source`）都不回它，客户端收到的流里也被摘掉了。
  present 页只看得到「几点、怎么压的」，一个字都没有。
- **原话和日报挨着库放，跟着 Loci 导出走**：`LOCI_GATEWAY_DAYS` 设成 `<buckets>/_hosts/<名>` 时，
  `days/*.jsonl` 和 `reports/*.md` 在你的库目录里，Loci 的导出包带着它们（日报失败的原因 `.err.json` 不带）。
  日报原文 present 页看得见。Loci 来取原话时，原文只进那一次回复，不存、不缓存、不写日志。
- **私账目录 `<LOCI_GATEWAY_DATA>`**（缺省 `gateway/data/`）不进库、不导出、任何口都不给，但里面有要紧的东西，
  当成你的聊天记录一样看管：
  - `threads/` —— 每个对话的窗口：carry、塞进去的卡片和梦、**上一轮发上游的那一整份**（唤醒要照着它拼前缀）
  - `held.json` —— 他唤醒时说了、你还没看到的话
  - `present.json` —— 设置，**Bark 推送码是明文**（任何口都只回打码的尾巴）
  - `prompts.json` · `counters.json` · `context_windows.json` · `logs/`（日志只有数字、名字和原因，没有字）
  - `gateway.lock` —— 只有正在跑的那个网关的 pid（第五节第 13 条）
- **钥匙**：客户端的 API key 只路过；借来给网关自己那一轮用的那把只在内存里，不落盘、不进日志，重启就没了。
  `LOCI_UPSTREAM_KEY` 只从环境变量读。
- **Bark 推送的字经过 Bark 的服务器**。缺省推全文；不想让字经过别人的服务器，`present.json` 里
  `push.text` 改成 `notice`（只推「ta 说了句话」），或者自己搭一个 Bark 服务器。

---

## 八、它自己守的边界

- 🔴 **失败不挡聊天。** Loci 没起、超时、返回不是 JSON、present 哪一步炸了 —— 全部照常转发，只记一行日志。
  卡片等 3 秒、梦 / 发呆等 8 秒，等不到这一轮就不带。
- 🔴 **卡片带字，判断留给他。** 卡片说的是「哪条对得上、哪些还悬着」，要不要接、算不算真发生过，是他读完自己定。
- 🔴 **网关自己不写记忆。** 平常那一轮只读（外加告诉 Loci「这张卡这扇窗递过了」的记账）；
  夜里交给 Loci 的是等他处理的切片，不是记忆。唤醒、日报、打包那一轮里，写不写是他自己调工具决定的。
- 🔴 **不替他 `breath()`。** 「开口之前先睁眼」是他自己该伸的手，写在系统提示里。
- 🔴 **他压的那段字不给任何人**（第七节）。

---

## 九、你已经有自己的网关 → 只拿两个模块

不想用这个外壳、只想把梦和相关提醒接进你自己的转发层：拿 `poke_delivery.js` 和 `auto_attach.js`，
都**零依赖**（只用 `fs` / `path` / 全局 `fetch`）、**零 import 这个项目**。这条路只有记忆，没有 present。

```js
const 桥 = require("./poke_delivery.js");

// 转发之前调一次。它会**就地**改 messages（插一条 system），也可能什么都不做。
await 桥.attach_once({
  messages: body.messages,              // 会被就地修改
  requestId: "随便什么能对上日志的字符串",
  地址: "http://127.0.0.1:18002/mcp",    // Loci 在哪（/mcp 结尾，它自己会转成 REST 根）
  闲时阈值分钟: 210,
});
```

```js
const 自动贴 = require("./auto_attach.js");

// 强弱提醒：**最后一步**。它自己不碰 messages，只把 patch 算出来还给你——
// 位置要离模型开口最近，得等请求体都组完了再推进去。
const 提醒 = await 自动贴.build_relevance_notice({ messages: body.messages, requestId, 地址, 最低分: 50 });
if (提醒 && 提醒.patch) 自动贴.attach_at_true_tail(body.messages, 提醒.patch);
```

**两处都会抛异常 —— 你要接住，然后照常转发。** 宁可这次没插上，也不能让人的对话卡住。
它们读的环境变量：`LOCI_MCP`、`LOCI_HOOK_TOKEN`（Loci 上了锁时要带）、`LOCI_GATEWAY_DATA`（状态和日志落哪），
`auto_attach.js` 另外读下面那四个 `RELEVANCE_*`。

老的英文别名还在，指的是同一个函数（向后兼容，新写的直接用正式名）：

```js
自动贴.computeReminder({ ... })          // = build_relevance_notice
自动贴.appendToTail(messages, patch)     // = attach_at_true_tail
```

**两个位置不一样，是故意的：**

- **强弱提醒贴真尾巴** —— 每轮内容都不同，放前缀等于每轮都把 prompt 缓存打穿
- **发呆/做梦贴稳定前缀区** —— 闲够 3.5 小时才出现一次，那时候缓存本来就过期了；
  插进去之后它就是新前缀的一部分，后面每轮都能复用

### 闲时闸

发呆和做梦**不是每个窗口开头都戳**，是**人长时间没说话之后的第一句**才戳（默认 210 分钟）。
它挡的是插嘴：在连着说话的空档里递一句「昨夜织了个梦」，那不是提醒，是打断。

### 强弱提醒：什么时候才去查一次记忆

> 只管 `auto_attach.js`。这个外壳不走它：每句新话都问一次 Loci 的卡片，问什么、给什么是 Loci 那边定的。
> 这个模块只报数量，不报内容：「〔记忆提醒〕和这句有关：事件 3 条 · 认知 1 条」。

**不是每句话都去查** —— 查一次要几秒，而且大部分话根本不需要。先在本地判断值不值得查：

| | 判据 | 例子 | 默认 |
|---|---|---|---|
| **强档** | 出现了明说要翻旧账的词 | 「**上次**你说的那个方案」 | 开 |
| **弱档** | 没有那种词，但这句话有实质内容（不是应声话） | 「明天去医院复查」 | **关** |
| 两档都不过 | 应声话、问候语 | 「嗯」「好的」「在吗」 | 不查 |

命中了才去 Loci 跑一次 `recall`，过分数线（`RELEVANCE_MIN_SCORE`，缺省 50）的条数拼成那一行贴回去；
一条都没过就一个字都不说。

**强档词表可以整表换**（默认是中文）：

```bash
RELEVANCE_STRONG_WORDS="remember,last time,earlier,you said"
```

不分大小写。**不换的话，不说中文的人一次都不会触发** —— 而且不会收到任何提示。

**弱档为什么默认关**：它太宽，日常说话几乎每句都过，每轮都响的提醒等于没有提醒；
而且每次触发都要等一次搜索，那个时间跟你的库有多大走。更怕漏、不怕吵，就 `RELEVANCE_WEAK=1`。

⚠️ **超时给太少 = 这功能悄悄不工作。** 缺省 12 秒（`RELEVANCE_TIMEOUT_MS`），是照一个 1300 条的库定的：
平时约 3 秒，Loci 刚重启后的第一次 15 秒上下（索引是冷的），库更大就更慢。
装完先自己跑一次 `recall(query="随便什么")` 掐个表，把它设成两倍 ——
**给少了的后果不是报错，是它安静地什么都不做。** 确认它在工作：看日志里 `relevance_reminder_observed`
那条的 `injected` 和 `error`，别看聊天界面。
