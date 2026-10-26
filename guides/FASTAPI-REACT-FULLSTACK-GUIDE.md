# 全栈实战指南:把一个 LLM CLI 变成 Web 应用,中间到底要补哪几层

> 一份给所有正在做 LLM 应用的朋友看的、共同进步的策略对比手册。
>
> 本文聚焦一件事:**一个在终端里跑得好好的 LLM 对话/RAG/agent 工具,要让它跑在浏览器里、流式输出、能多会话切换,中间到底要加哪几层?难点又藏在哪?**
> 适合已经写过 LLM CLI(chat / RAG / agent),想给它套一层 Web 界面的工程师。结论先行:**难点几乎全不在前端 React,而在"流式协议 + 同步阻塞 + async event loop"这个三角的边界上**。

---

## 目录

- [前言:Web 化的真正成本不在界面,在流式](#前言web-化的真正成本不在界面在流式)
- [第一部分:四种"服务器推送"通信范式](#第一部分四种服务器推送通信范式)
  - [1. 一次性请求 / 响应](#1-一次性请求--响应)
  - [2. 轮询与长轮询](#2-轮询与长轮询)
  - [3. SSE(Server-Sent Events)](#3-sseserver-sent-events)
  - [4. WebSocket](#4-websocket)
  - [5. 选型决策矩阵](#5-选型决策矩阵)
- [第二部分:LLM Web 后端框架横评](#第二部分llm-web-后端框架横评)
  - [1. FastAPI](#1-fastapi)
  - [2. Flask / Quart](#2-flask--quart)
  - [3. Django / Channels](#3-django--channels)
  - [4. Node:Express / Fastify / Hono](#4-nodeexpress--fastify--hono)
  - [5. Go / Rust](#5-go--rust)
  - [6. 选型决策矩阵](#6-选型决策矩阵-1)
- [第三部分:SSE 流式的核心范式](#第三部分sse-流式的核心范式)
  - [1. event loop 是什么,为什么会被冻](#1-event-loop-是什么为什么会被冻)
  - [2. 同步阻塞调用 vs async 协程](#2-同步阻塞调用-vs-async-协程)
  - [3. 三种把同步塞进 async 的办法](#3-三种把同步塞进-async-的办法)
  - [4. 心跳与代理超时](#4-心跳与代理超时)
- [第四部分:会话状态与持久化](#第四部分会话状态与持久化)
  - [1. 无状态后端 + 客户端带历史](#1-无状态后端--客户端带历史)
  - [2. 服务端 session:文件 / Redis / 数据库](#2-服务端-session文件--redis--数据库)
  - [3. 并发写与文件锁](#3-并发写与文件锁)
- [第五部分:错误协议与全链路可观测](#第五部分错误协议与全链路可观测)
  - [1. 业务错误码:别让前端猜](#1-业务错误码别让前端猜)
  - [2. 流中途出错怎么办](#2-流中途出错怎么办)
  - [3. request_id 全链路追踪](#3-request_id-全链路追踪)
- [第六部分:生产真实做法(规模 / SLA / 成本 / 运维)](#第六部分生产真实做法规模--sla--成本--运维)
- [第七部分:小型实现的常见坑 → 生产对策](#第七部分小型实现的常见坑--生产对策)
- [第八部分:补齐路线(按 ROI 排序)](#第八部分补齐路线按-roi-排序)
- [附录 A:术语小词典](#附录-a术语小词典)
- [附录 B:进一步阅读](#附录-b进一步阅读)

---

## 前言:Web 化的真正成本不在界面,在流式

你已经有一个 CLI:在终端敲一句话,模型流式吐字,可能还会调几个工具、查个知识库。现在产品同学说"能不能放到浏览器里给非技术同事用"。

直觉上这是个前端活儿:画三个区域(会话列表 / 对话区 / 设置),接个接口,把流式文字渲染出来。**真做起来你会发现工作量严重倒挂**:

- **静态接口(列会话、建会话、读历史、改设置)**:一天就通。就是普通的 CRUD,GET/POST,JSON 进 JSON 出,前端 fetch 一把梭。
- **流式接口(把模型边生成边推给浏览器)**:能烧掉你大半的时间,而且坑全在你看不见的地方——单元测试绿、本地 curl 也正常,只有真人在真浏览器上点,才暴露出来。

为什么?因为 CLI 时代你回避了一整类问题:

| 维度 | CLI | Web |
|---|---|---|
| 执行模型 | 单线程同步,阻塞就阻塞,没人等你 | 异步 event loop,一个阻塞冻结所有连接 |
| 输出通道 | 直接写 stdout,操作系统兜着 | 经过应用→框架→反向代理→浏览器,任何一层超时都断 |
| 并发 | 一个进程服务一个人 | 一个进程服务 N 个人,状态要隔离 |
| 错误 | 抛异常打印 traceback 完事 | 流已经开始推了,中途出错怎么通知前端? |
| 中断 | Ctrl-C | 用户关页面 / 连点按钮 / 切会话,旧请求怎么收尾? |

这份指南就是把这几层逐一拆开。**核心论点:把一个同步阻塞的 LLM 调用,正确地暴露在一个异步流式接口上,是整个 Web 化里唯一真正难的事。** 其余的(框架选型、session 存储、错误码)都是有标准答案的工程活儿。

---

## 第一部分:四种"服务器推送"通信范式

LLM 应用的核心交互是"模型边想边说",所以你需要一种**服务器主动、持续地往客户端推数据**的通道。HTTP 默认是"请求一次答一次",不天然支持。有四种办法。

### 1. 一次性请求 / 响应

最朴素:前端 POST 一句话,后端等模型**全部生成完**再一次性返回整段。

- **优点**:实现零难度,就是普通 REST。可缓存、可重试、对代理友好。
- **致命缺点**:用户要盯着转圈等 20-60 秒才看到第一个字。LLM 响应慢,这个体验不可接受。
- **什么时候用**:后台批处理、不直接面向用户的 API、响应很短的分类/抽取任务。

### 2. 轮询与长轮询

前端定时问"好了吗?好了吗?"(轮询),或者后端 hold 住请求直到有新数据才返回(长轮询)。

- **轮询**:实现简单,但要么延迟高(间隔长)要么浪费请求(间隔短)。LLM 逐字输出场景下基本不用。
- **长轮询**:是 WebSocket 普及前的主流方案。每收到一块数据就要重建连接,开销大。今天除非环境极度受限,否则不选。

### 3. SSE(Server-Sent Events)

基于 HTTP 的**单向**(服务器→客户端)流式推送。一个长连接,服务器按 `text/event-stream` 格式持续写,浏览器用 `EventSource` 或 fetch-stream 接收。

```
HTTP/1.1 200 OK
Content-Type: text/event-stream

event: token
data: {"text": "你"}

event: token
data: {"text": "好"}

event: done
data: {"finish_reason": "stop"}
```

- **优点**:
  - 就是 HTTP,穿透防火墙/代理无障碍,不需要协议升级。
  - 浏览器原生支持自动重连(`EventSource` 自带)。
  - 实现比 WebSocket 简单一半:服务器只管 yield,不管双向握手。
  - 对 LLM 逐 token 输出是**天作之合**——本来就是单向的"模型说,用户看"。
- **缺点**:
  - **单向**。用户中途想打断、想发新消息,得另开一个普通请求,不能复用这条流。
  - 浏览器原生 `EventSource` **只支持 GET**,且不能自定义请求头(没法带 `Authorization`)。所以生产里通常不用原生 `EventSource`,改用 `fetch` + `ReadableStream` 手动解析(社区库如 `@microsoft/fetch-event-source`),才能用 POST + 自定义头。
  - 同一域名下浏览器对并发 SSE 连接数有限制(HTTP/1.1 下约 6 个;HTTP/2 缓解)。

### 4. WebSocket

**全双工**的独立协议(从 HTTP 升级而来),客户端服务器都能随时发消息。

- **优点**:真正双向。适合需要客户端频繁主动推送的场景(协同编辑、实时游戏、多人聊天室、语音流)。
- **缺点**:
  - 协议更重,要处理握手、心跳(ping/pong)、断线重连**全部自己写**(不像 SSE 浏览器兜着)。
  - 不是普通 HTTP,某些企业代理 / CDN 不友好,要额外配置。
  - 有状态长连接,横向扩展时要解决"用户连在哪个实例"的粘性问题(sticky session 或共享状态)。

### 5. 选型决策矩阵

| 场景 | 推荐 | 理由 |
|---|---|---|
| **LLM 逐 token 流式输出** | **SSE** | 单向就够,实现简单,代理友好,浏览器自动重连 |
| 模型生成 + 用户随时打断 | SSE(主流) + 单独的 POST 控制接口 | 打断走旁路请求,不必为双向上 WebSocket |
| 实时协同 / 双向高频 | WebSocket | 真正需要全双工 |
| 后台批处理 / 短响应 | 一次性请求 | 没有流式需求,别自找麻烦 |
| 极度受限的老环境 | 长轮询 | 兜底 |

**给 LLM 应用的结论:90% 的场景 SSE 是正解。** 它的"单向"限制对"模型说用户看"恰好不是限制;打断/重发这类低频动作走普通 POST 完全够。只有当你的产品本质上是双向高频(比如实时语音 agent),才值得上 WebSocket 那套复杂度。

> **本项目当前怎么做的**:选了 SSE,前端用 fetch-stream 库(支持 POST + 自定义头)而非原生 `EventSource`。打断走前端 `AbortController` 直接掐连接,没做服务端打断接口(退化点,见第七部分)。

---

## 第二部分:LLM Web 后端框架横评

LLM 流式后端对框架的核心要求只有一条:**对 async / 流式响应的支持是否原生、顺手**。因为你迟早要面对"同步阻塞调用塞进异步流"这个问题(第三部分),框架在这里帮不帮你,差别巨大。

### 1. FastAPI

Python 异步 Web 框架,基于 Starlette + Pydantic,ASGI。

- **流式支持**:原生。`StreamingResponse` 直接吃 async generator;社区有 `sse-starlette` 专门封装 SSE(自带心跳 `ping`、断连清理)。
- **优点**:类型注解驱动、自动生成 OpenAPI 文档、Pydantic 校验请求体几乎零成本、async 一等公民。
- **适合**:Python 技术栈、已经有一堆 Python LLM 代码(LangChain / LiteLLM / 向量库 SDK 大多是 Python)想直接复用。
- **代价**:async 心智负担——一旦在 `async def` 里写了同步阻塞调用,event loop 就冻(这正是 LLM 场景的头号坑,第三部分详述)。

### 2. Flask / Quart

- **Flask**:老牌同步框架。流式靠 generator + `Response(stream_with_context(...))`,能做但对 SSE 心跳、并发这些要自己补。WSGI 同步模型下,流式期间会占着一个 worker 线程。
- **Quart**:Flask 的 async 兄弟(API 几乎一致,ASGI)。如果喜欢 Flask 的写法又要 async,Quart 是平滑迁移路径。
- **适合**:已有 Flask 项目要加流式,改造成本最低选 Quart。新项目做 LLM 流式,FastAPI 生态更对口。

### 3. Django / Channels

- 重型全栈框架,自带 ORM / admin / auth。流式要靠 `StreamingHttpResponse`(同步)或 Channels(为 WebSocket/async 设计)。
- **适合**:已经是 Django 单体、要在里面加个 LLM 功能、且看重 Django 的 auth/admin/ORM 一条龙。
- **代价**:为了一个流式接口引入 Channels 偏重;Django 的 async 支持是后加的,不如 FastAPI 原生顺。

### 4. Node:Express / Fastify / Hono

- JS 单线程事件循环天生异步,流式很自然(`res.write()` 持续写)。
- **优点**:前后端同语言(TypeScript),团队心智统一;Vercel AI SDK 等把 LLM 流式封装得极顺,前端配套最好。
- **适合**:前端重、后端逻辑轻、团队 JS 为主。Next.js 全栈一体时尤其顺。
- **代价**:CPU 密集任务(本地 embedding、重排序模型推理)在 Node 里别扭,Python 生态在这块碾压。

### 5. Go / Rust

- **Go**:goroutine 模型,高并发流式天然强,`http.Flusher` 直接刷 SSE。部署成一个静态二进制,运维省心。
- **Rust**(axum / actix):极致性能与内存安全,适合做高吞吐 LLM 网关 / 代理层。
- **适合**:LLM **网关 / 代理 / 路由**这种高并发中间层(很多生产 LLM proxy 用 Go/Rust 写);或团队已是 Go/Rust 栈。
- **代价**:LLM 应用层(prompt 工程、RAG 编排)的库生态远不如 Python/JS 丰富,多数时候是"网关用 Go,应用逻辑用 Python"分层。

### 6. 选型决策矩阵

| 你的情况 | 推荐 | 理由 |
|---|---|---|
| 已有大量 Python LLM 代码要复用 | **FastAPI** | async 原生 + 复用现成 Python 链路 + Pydantic 校验 |
| 前端重、全栈 TS、Next.js | Node(Hono / Next API) | 前后端同语言,Vercel AI SDK 配套 |
| 已有 Flask 项目加流式 | Quart | 迁移成本最低 |
| 已有 Django 单体 | Django + StreamingHttpResponse | 复用 auth/ORM |
| 高并发 LLM 网关层 | Go / Rust | 并发与运维优势,应用逻辑另起一层 |

> **本项目当前怎么做的**:FastAPI + `sse-starlette`。理由是项目主体是 Python(RAG 链路、agent loop、本地 embedding/rerank 模型全是 Python),Web 层选 FastAPI 才能直接复用这些链路,不必跨语言重写。这也踩上了 async 头号坑(第三部分),但那是 LLM Web 化绕不开的必修课。

---

## 第三部分:SSE 流式的核心范式

这是整份指南的核心。**如果你只读一节,读这节。**

### 1. event loop 是什么,为什么会被冻

async 框架(FastAPI / Node / asyncio)的核心是一个 **event loop**:单线程,不停地在多个"协程任务"之间切换。它高效的前提是——**每个任务在等 I/O 时主动让出控制权**(`await`),让 loop 去跑别的任务。

```
正常:  [请求A 等网络] → loop 去跑 [请求B] → 跑 [心跳task] → 回到 A...
        单线程,但没人傻等,吞吐很高
```

但如果某个任务里有一段**同步阻塞**(CPU 算 / 同步 I/O 没 await),它就霸占住唯一的线程不放,event loop **整个冻住**,所有其他任务(包括别的用户的请求、心跳)全部停摆:

```
出事:  [请求A 同步阻塞 30s] → loop 卡死 → 心跳task 跑不了 → B 也卡 → 全停
```

### 2. 同步阻塞调用 vs async 协程

LLM 场景里这个坑几乎必踩,因为很多 LLM SDK 的调用是**同步阻塞**的:

```python
# 同步 SDK 调用:这一行会卡住 20-30 秒直到模型生成完
response = client.completion(model=..., messages=...)
```

你把它写进一个 async 流式接口里:

```python
async def stream():
    for event in run_agent(...):   # ← run_agent 内部是同步 client.completion()
        yield to_sse(event)        # 看起来在流式 yield
```

**这段代码语法合法、单元测试能过、本地 curl 短响应也像没事**。但真相是:`run_agent` 内部每次 `client.completion()` 同步阻塞那 20-30 秒,**整个 event loop 被冻住**。如果你的框架用心跳防超时(见下文),心跳是个 async task,event loop 冻了它就发不出去。客户端几十秒收不到任何字节 → 判定连接死 → 前端报"网络中断"。

> **为什么单测抓不到**:测试客户端(如 FastAPI 的 `TestClient`)通常是**同步**拉流的,它根本不跑真正的 event loop 并发,所以"event loop 被冻"这种问题在单测里完全不显形。**这类 bug 只能靠真浏览器 + 真慢的模型暴露**——这也是"UI/流式改动必须真前端取证"成为铁律的原因。

### 3. 三种把同步塞进 async 的办法

| 办法 | 做法 | 适用 | 代价 |
|---|---|---|---|
| **A. 全链路改 async** | 把 `client.completion` 换成 `client.acompletion`,一路 `await` 到底 | 新项目、SDK 有 async 版、不复用同步代码 | 改造面大;若同步版被 CLI 等复用,会牵连大改;部分 SDK 的 async 实现有自己的坑(连接清理竞态) |
| **B. 线程池隔离** | 同步调用丢进 `run_in_executor` / `asyncio.to_thread` | 单次同步调用(非 generator) | generator 逐 yield 的场景不直接适用 |
| **C. daemon 线程 + Queue 桥接** | 同步 generator 跑在后台线程,通过 `asyncio.Queue` 把事件投递回主协程 | **同步 generator + 要复用、不能改签名** | 要正确处理跨线程投递、异常传递、结束哨兵 |

办法 C 是"同步流式 generator 不能改"时的标准解法,核心骨架:

```python
async def aiter_sync_generator(sync_gen_factory):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def worker():
        try:
            for ev in sync_gen_factory():
                # Queue 不是线程安全的,必须投到 loop 线程执行 put
                loop.call_soon_threadsafe(queue.put_nowait, ev)
        except BaseException as e:
            loop.call_soon_threadsafe(queue.put_nowait, ExcMarker(e))
        else:
            loop.call_soon_threadsafe(queue.put_nowait, DONE_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        item = await queue.get()       # ← 非阻塞,event loop 在这期间自由跑心跳
        if item is DONE_SENTINEL:
            return
        if isinstance(item, ExcMarker):
            raise item.exc              # 把后台线程的异常透传回主协程
        yield item
```

三个关键点:

1. **同步阻塞挪到 daemon 线程**:主协程只 `await queue.get()`,等待时让出控制权,event loop 在 LLM 生成那 20-30 秒里能正常跑心跳和别的请求。
2. **`call_soon_threadsafe` 跨线程投递**:`asyncio.Queue` 本身不是线程安全的,后台线程不能直接 `put`,必须用 `loop.call_soon_threadsafe` 把 put 操作投回 loop 所在线程执行。
3. **两个哨兵区分三态**:正常事件、流正常结束(`DONE_SENTINEL`)、异常(`ExcMarker` 包着原始异常透传)。事件本身是结构化对象,不会和哨兵撞身份。

**为什么不无脑选 A(全 async)?** 如果同步 generator 被别的入口(比如同一份代码的 CLI 模式)复用,改 async 会逼着那条线一起大改,违背"改动最小面"原则;而且部分 SDK 的 async 路径有自己的连接清理竞态坑。**在边界加一层适配(办法 C),老签名一行不动**,是"接口稳定性"的实践。这正是工程判断:不是哪个"更先进"就选哪个,而是哪个改动面小、不引新债。

### 4. 心跳与代理超时

为什么需要心跳?因为 LLM 在"思考"(还没吐第一个 token)时,这条 SSE 连接上**一个字节都没有**。而中间的反向代理(Nginx / 云网关)有空闲超时(常见 30-60s),长时间无数据会被代理掐断。

解法:每隔 N 秒(常见 15s)往流里发一个**注释行心跳**(`: ping\n\n`),保持连接活着。`sse-starlette` 的 `ping` 参数就是干这个的。

**但注意**——心跳是个 async task,它依赖 event loop 还在转。如果你犯了 §3.2 的错(同步阻塞冻了 loop),**心跳 task 也跑不了**,等于没设。所以心跳和"别冻 event loop"是一套组合拳,缺一不可。

> **本项目当前怎么做的**:用办法 C(daemon 线程 + `asyncio.Queue` 桥接),`sse-starlette` 心跳设 15s。同步的 agent 流式 generator 一行没改(它还被 CLI 复用),全部在 Web 边界包一层线程适配。这是本阶段最硬的一课,详见 notes。

---

## 第四部分:会话状态与持久化

Web 应用要支持"多个会话、关页面再回来还在"。CLI 时代根本没这问题(进程退出就没了,要么自己存文件)。Web 化后,"会话状态存哪"是个明确的架构决策。

### 1. 无状态后端 + 客户端带历史

最简单:后端**不存任何东西**,每次请求前端把完整对话历史发上来。

- **优点**:后端纯无状态,横向扩展零成本(任何实例都能处理任何请求),没有持久化层。
- **缺点**:历史全在前端,刷新/换设备就丢;历史长了每次请求体巨大;无法做服务端分析/审计。
- **适合**:demo、隐私敏感(不想存用户对话)、无限水平扩展且不需要历史的场景。

### 2. 服务端 session:文件 / Redis / 数据库

后端给每个会话分配一个 ID(用 UUID,别用自增——自增可枚举、可猜),把消息持久化:

| 存储 | 优点 | 缺点 | 适合规模 |
|---|---|---|---|
| **JSON / JSONL 文件**(每会话一个文件) | 零依赖、可直接看、易调试 | 并发写要加锁;无法跨机器共享;量大后目录爆炸 | 单机、个人、原型 |
| **SQLite** | 单文件、事务、SQL 查询、零运维 | 单写者瓶颈;跨机器要靠网络文件系统(不可靠) | 单机中小量 |
| **Redis** | 快、自带 TTL 过期、原生支持分布式 | 内存贵,要落盘配置;复杂查询弱 | 多实例、要会话过期 |
| **PostgreSQL / MySQL** | 事务、复杂查询、成熟运维生态 | 要起 DB、写 schema/迁移 | 生产标准 |

**生产标准答案**:消息正文进关系库(Postgres)或文档库;热会话/分布式锁/限流计数放 Redis;大体量历史归档进对象存储(S3)。

### 3. 并发写与文件锁

如果你选了"每会话一个文件"(原型常见),要小心**并发写**:同一会话两个请求同时写(用户快速连发、或多标签页),不加锁会**互相覆盖、写坏 JSON**。

跨平台文件锁可以用 `portalocker` 这类库(封装了 Windows / POSIX 的差异)。但要清楚:**文件锁只在单机有效**。一旦多实例部署,文件锁形同虚设——这正是"文件存储无法水平扩展"的根因,也是生产为什么用 Redis/DB 做并发控制的原因。

> **本项目当前怎么做的**:每会话一个 JSONL 文件,UUID v4 当 ID,`portalocker` 做跨平台文件锁。这是典型的**原型退化点**:单机能用、易调试,但无法多实例。生产要换 Postgres + Redis(见第六部分)。退化点当场记清,没假装它能扩展。

---

## 第五部分:错误协议与全链路可观测

CLI 出错就打 traceback 完事。Web 出错要"让前端知道发生了什么、用户该怎么办、工程师怎么排查",这是三个不同的诉求,对应三件事。

### 1. 业务错误码:别让前端猜

HTTP 状态码(400/500)太粗——前端拿到 500 不知道是"模型超时该重试"还是"知识库为空该去上传文档"。**定义一套业务错误码**,前端按码做差异化交互:

```json
{ "error": { "code": "CORPUS_EMPTY", "message": "知识库为空,请先上传文档",
             "request_id": "req-a1b2c3" } }
```

- 模型超时 -> 提示"重试"按钮
- 知识库空 -> 引导"去上传"
- 参数非法 -> 高亮表单
- 限流 -> 提示"稍后再试" + 倒计时

错误码是**前后端契约**,要文档化、要稳定(像 API 一样不能随便改含义)。

### 2. 流中途出错怎么办

这是流式独有的难题。普通请求出错,你还没开始发响应,可以干净地返回一个 4xx/5xx + 错误体。**但 SSE 流已经 `yield` 出去几个 token 了,HTTP 200 头早发了,这时候模型调用突然抛异常,怎么办?**

错误做法:让异常**逃出 generator**。多数 SSE 框架遇到 generator 抛异常会**直接静默关闭连接**,前端只看到"连接断了",误判成网络问题,既不知道真实原因也拿不到 request_id。

正确做法:在 generator 里 `try/except` 兜住**所有**异常,把错误**作为一个 SSE 事件发出去**:

```python
async def stream():
    try:
        async for ev in produce_events():
            yield sse("token", ev)
        yield sse("done", {...})
    except Exception as e:                  # 必须兜所有, 不只是预期的那种
        log_error(e, request_id)            # 服务端留痕
        yield sse("error", {                # 改成一个 error 事件发给前端
            "code": classify(e),
            "request_id": request_id,
        })
```

前端收到 `error` 事件,就能在对话框里画一个红框 + request_id,而不是误报断网。**"流中途异常 -> 转成 error 事件"是 SSE 健壮性的必修项**,极易漏(因为正常路径和单测都不触发它)。

### 3. request_id 全链路追踪

每个请求入口生成一个唯一 `request_id`,让它出现在**三个地方**:① 服务端每条日志、② 返回给前端的错误体、③ 流式 error 事件。这样用户截图报错带着 request_id,工程师拿它一搜就串起整条链路。

实现上常用 `contextvars.ContextVar` 做隐式透传(不必每个函数手动传参)。**但有个大坑**:`ContextVar` 的 token 绑定在**特定的 async context** 上。如果你用了第三部分的"daemon 线程 + Queue"方案,generator 跑在和请求 handler **不同的 context**(甚至不同线程)里,这时 `var.set()` / `token.reset()` 会抛:

```
ValueError: <Token> was created in a different Context
```

**对策**:把 request_id 的 set/reset 操作全部 `try` 兜住,跨 context 失败就跳过——request_id 是排查**辅助**,丢一条不致命,但绝不能因为它把主流程搞崩。**任何在 async + 多线程混用环境里的 ContextVar 操作,都要假设它可能跨 context 失败。** ContextVar 不是线程局部变量的平替。

> **本项目当前怎么做的**:一套业务错误码 + 流中途异常转 `error` 事件(踩过"非预期异常逃出 generator -> 静默关流 -> 前端误报网络中断"的坑后补的);request_id 用 ContextVar 三处对齐,跨 context 操作全 try 兜底。这几条都是真前端复测炸出来的,不是设计时想到的。

---

## 第六部分:生产真实做法(规模 / SLA / 成本 / 运维)

把原型推到生产,差距集中在这几块:

**1. 架构分层**

```
[浏览器] -> [CDN/静态托管:前端] -> [API 网关/负载均衡] -> [N 个无状态后端实例]
                                                          |
                              [Redis:会话/限流/锁]  [Postgres:消息/用户]  [对象存储:归档]
                                                          |
                                        [LLM 网关:路由/缓存/限流/重试] -> [各模型 provider]
```

- 后端**无状态**,所有状态外置到 Redis/DB,才能随意水平扩展。
- 前后端**分离部署**:前端构建成静态资源上 CDN,后端只管 API。

**2. 流式 + 反向代理**

- Nginx 等代理要**关掉对 SSE 路径的响应缓冲**(`proxy_buffering off`),否则代理攒着不发,流式失去意义。
- 调好空闲超时,配合应用层心跳。
- HTTP/2 缓解浏览器并发连接数限制。

**3. SLA / 可靠性**

- 模型超时、重试、降级到备用模型,全在 **LLM 网关层**统一做(不在每个业务接口里散写)。
- 流式接口要监控:**首 token 延迟(TTFT)**、token 间隔、完整率(多少流正常 `done` vs 中途断)。
- 优雅停机:部署滚动更新时,正在跑的流要给时间收尾,别硬切。

**4. 鉴权与多用户**

- 原型常常**无鉴权**(单机自己用)。生产必须有:OAuth / JWT / session cookie,会话按用户隔离,接口按用户限流。
- 别用可枚举的会话 ID(自增 id 能被遍历盗取别人会话)——UUID / 随机 token。

**5. 成本**

- LLM 调用是大头。**语义缓存**(相同/相似问题命中缓存不调模型)、prompt 压缩、按场景路由到便宜模型,是主要省钱手段。
- 流式不省 token,但**省的是用户感知延迟**——TTFT 低,用户觉得快。

**6. 可观测**

- 结构化日志(JSON)+ 集中收集(ELK / Loki)。
- 分布式追踪(OpenTelemetry),request_id 贯穿前端->网关->后端->LLM 调用。
- LLM 专用观测(LangSmith / Helicone 等)看 token 消耗、调用链、成本。

---

## 第七部分:小型实现的常见坑 -> 生产对策

### 7.1 常见坑

#### 坑 1:同步阻塞调用塞进 async 流式接口

最大的坑。`async def` 里直接 `for ev in sync_generator()`,LLM 那 20-30s 同步阻塞冻结整个 event loop,心跳发不出,前端误报断网。**单测(同步 TestClient)抓不到,只有真浏览器 + 真慢模型暴露。** 对策见第三部分办法 C。

#### 坑 2:SSE 流中途的非预期异常静默关流

流已开始 yield 后,任何**没被预期 `except` 接住**的异常逃出 generator,SSE 框架直接静默关连接,前端 `onerror` 误判网络断。对策:`except Exception` 兜所有,转成 SSE `error` 事件 + request_id(§5.2)。

#### 坑 3:ContextVar 跨 async context / 线程抛 ValueError

request_id 用 ContextVar 透传,一旦 generator 被挪到 worker 线程(坑 1 的修法引入),`token.reset()` 跨 context 就炸,而且这个异常本身又会变成坑 2 的"非预期异常"。**两个坑会咬尾巴。** 对策:ContextVar 所有 set/reset 收敛 try,跨 context 失败静默跳过(§5.3)。

#### 坑 4:前端连点导致多流并发污染状态

用户急了连点"重试"/"发送",旧 SSE 流没人 abort 还在往状态机写,新流也在写,两条流的 token/done/error 交错回填,UI 状态机错乱。**单测和"老实人点一次"都不触发,只有真人连点暴露。** 对策:前端持一个 `AbortController`,每次重连/切会话前先 `abort()` 旧流再开新流。**AbortController 是流式前端的必备件,不是优化。**

#### 坑 5:依赖版本随手 `@latest` 导致漂移

脚手架命令写 `create-xxx@latest` / `add pkg@latest`,跑出来的实际版本和计划锁定的不一致(大版本都可能差),埋下兼容问题。对策:**计划/脚本里写死版本号**;脚手架跑完第一件事核对 `package.json` 实际版本。

#### 坑 6:组合态 bug —— 单测全绿,真前端炸

坑 1-4 有个共性:**单独都不致命,凑到一个特定组合(慢模型 × 多轮 × 心跳 task × 用户连点 × 跨 context)才集体发作**。单元测试覆盖单条路径,真实交互是多条路径在时间维度叠加。这类 bug 的唯一克星是**真前端 + 真后端 + 真模型的端到端取证**,外加排查时**先取证证伪假设**(别一上来就猜"网络问题",先三路 probe 看到底断没断),再动手。

> **debug 警示**:排查到"三路取证全通、症状消失"时,要警惕 **false fix**——你修的东西可能和真症状不是一回事(真因也许是上游服务那一下抖动)。但只要修法本身有**独立的工程价值**(比如"别冻 event loop"是硬收益),保留就是对的,同时把不确定性如实写进 commit/notes,比假装"已彻底根治"诚实。

### 7.2 生产对策汇总

| 坑 | 原型表现 | 生产对策 |
|---|---|---|
| 同步阻塞冻 loop | 长响应偶发"断网" | 全 async 或线程池隔离 + 压测覆盖慢响应 |
| 流中途静默关流 | 前端误报网络断 | 标准化"流内 error 事件"协议 + 网关侧统一兜底 |
| ContextVar 跨 context | 偶发 500 | 用结构化日志 + OpenTelemetry trace 注入, 少手搓 ContextVar |
| 连点多流污染 | UI 状态错乱 | 前端状态机 + AbortController + 请求去重/防抖 |
| 版本漂移 | 偶发兼容问题 | lockfile 锁定 + CI 校验 + 依赖更新走 PR |
| 组合态 bug | 单测绿线上炸 | E2E 测试(真浏览器, 如 Playwright)+ 混沌/压力测试 |

### 7.3 综合对照表(原型 -> 生产)

| 维度 | 原型(本项目当前) | 生产标准 |
|---|---|---|
| 通信 | SSE | SSE(主流)+ 代理调优 |
| 后端 | FastAPI 单实例 dev server | FastAPI 多实例 + 网关 + 滚动部署 |
| 会话存储 | 每会话 JSONL + 文件锁 | Postgres + Redis + 对象存储归档 |
| 鉴权 | 无 | OAuth/JWT + 按用户隔离/限流 |
| 打断 | 前端 abort 连接 | 前端 abort + 服务端取消传播到模型调用 |
| 错误 | 业务码 + 流内 error 事件 | 同 + 集中告警 + SLO 看板 |
| 追踪 | request_id + JSONL 日志 | OpenTelemetry 全链路 + LLM 专用观测 |
| 部署 | 本机 dev server | 容器 + 编排 + CDN 托管前端 |
| 测试 | 后端单测 + 人工真前端取证 | + E2E(Playwright)+ 压测 + 混沌 |

---

## 第八部分:补齐路线(按 ROI 排序)

从原型往生产走,按性价比排序:

1. **E2E 自动化测试(最高 ROI)**:用 Playwright 跑真浏览器,把"连点重试""慢响应""中途断流"这些组合态坑变成自动回归。当前靠人工真前端取证,一次性、不可重复——这是最该先补的。
2. **鉴权 + 多用户隔离**:接 OAuth/JWT,会话按用户隔离。这是"能不能给第二个人用"的门槛。
3. **会话存储换 Redis/Postgres**:摆脱"文件锁只在单机有效"的天花板,才能多实例部署。
4. **LLM 网关层**:把超时/重试/降级/缓存/限流从业务接口里抽出来,统一治理。省成本 + 提稳定性。
5. **反向代理 + 容器化部署**:Nginx 关 SSE 缓冲 + 心跳调优;前后端分离部署,前端上 CDN。
6. **全链路可观测**:OpenTelemetry + LLM 专用观测,request_id 贯穿全链路。
7. **服务端打断传播**:用户 abort 时,把取消信号一路传到模型调用(省被放弃的 token 成本)。

前 3 项决定"能不能上线给多人用",后 4 项决定"上线后稳不稳、贵不贵、好不好查"。

---

## 附录 A:术语小词典

- **SSE(Server-Sent Events)**:基于 HTTP 的单向(服务器->客户端)流式推送协议,`Content-Type: text/event-stream`。
- **WebSocket**:从 HTTP 升级的全双工独立协议,双向实时通信。
- **event loop(事件循环)**:async 运行时的核心,单线程在多个协程间切换;靠每个协程 I/O 时 `await` 让出控制权维持并发。
- **协程 / async**:可暂停可恢复的函数,等 I/O 时让出 event loop。
- **同步阻塞**:一段代码占住线程直到完成、期间不让出;塞进 async 会冻 event loop。
- **ASGI / WSGI**:Python Web 服务器与应用的接口规范,ASGI 支持 async/流式,WSGI 是老的同步规范。
- **心跳(heartbeat / ping)**:流式空闲时定期发的占位数据,防反向代理空闲超时掐连接。
- **TTFT(Time To First Token)**:从发请求到收到第一个 token 的延迟,流式体验的核心指标。
- **ContextVar**:Python 的上下文局部变量,用于隐式透传请求级数据;token 绑定特定 context,跨 context 操作会报错。
- **AbortController**:浏览器 API,用于主动取消 fetch / 流式请求。
- **portalocker**:跨平台(Windows/POSIX)文件锁库。
- **反向代理 buffering**:代理攒满一块再转发的行为;对 SSE 必须关闭,否则破坏流式。
- **false fix**:看似修好了实则没修到真因的修复;症状消失可能是别的变量变了。
- **组合态 bug**:多个单独不致命的缺陷在特定时序组合下才发作的 bug,单测难覆盖。
- **UUID v4**:随机生成的全局唯一 ID,不可枚举,适合做会话/资源 ID(对比自增 ID 可被遍历)。

## 附录 B:进一步阅读

**协议 / 标准**:

- MDN — Server-Sent Events / `EventSource` 文档
- WHATWG HTML 规范 — Server-sent events 章节
- RFC 6455 — The WebSocket Protocol

**框架 / 库**:

- FastAPI 官方文档 — `StreamingResponse` 与依赖注入
- Starlette 文档 — ASGI 底层与 `BackgroundTask`
- `sse-starlette` — FastAPI 的 SSE 封装(心跳 / 断连清理)
- `@microsoft/fetch-event-source` — 支持 POST + 自定义头的 SSE 客户端
- Vercel AI SDK — JS 全栈流式 LLM 的配套封装

**异步 / 并发**:

- Python `asyncio` 官方文档 — event loop / `call_soon_threadsafe` / `run_in_executor`
- `contextvars` 官方文档 — ContextVar 与 Context 的绑定语义

**可观测 / 测试**:

- OpenTelemetry — 分布式追踪标准
- Playwright — 端到端浏览器自动化测试
- LangSmith / Helicone — LLM 应用专用观测

**生产架构**:

- 各云厂商的 LLM 网关 / API 网关最佳实践文档
- 反向代理(Nginx)流式响应配置:`proxy_buffering off` 与超时调优

---

## 写在最后

把一个 LLM CLI 搬上 Web,真正的难点不在你看得见的界面,而在你看不见的那几层边界:

1. **流式协议选 SSE 就够** —— 单向限制对"模型说用户看"恰恰不是限制,别为了"双向"无脑上 WebSocket
2. **同步阻塞别塞进 async 流** —— 这是头号坑,会冻 event loop、废掉心跳、让前端误报断网;要么全 async,要么 daemon 线程 + Queue 隔离
3. **单测抓不到流式 bug** —— 同步测试客户端跑不出 event loop 并发,流式/UI 改动必须真浏览器 + 真模型取证
4. **流中途异常要转成 error 事件** —— 别让它逃出 generator 静默关流,前端会误判网络断
5. **ContextVar 跨 context 必炸** —— async + 多线程混用时所有 set/reset 都要 try 兜底
6. **AbortController 是必备件** —— 真人会连点,旧流不 abort 就多流污染状态机
7. **会话状态外置才能扩展** —— 文件锁只在单机有效,多实例必须 Redis/DB
8. **组合态 bug 靠 E2E 治** —— 单独不致命的缺陷凑一起才发作,真前端取证 + 自动化 E2E 是唯一解药

最后一句送给所有要把 LLM 工具 Web 化的朋友:**Web 化给你的不是"更好看的 CLI",而是逼你直面一整类 CLI 时代被回避的问题——并发、流式边界、状态隔离、跨进程错误传播。** 把这几层边界一件件搞清楚,比急着把界面画漂亮重要得多。漂亮的界面卡在第一个 token 上转 30 秒圈,照样没人用。

希望这份指南对你有帮助。

— *本文档遵循 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 协议, 可自由引用、修改、再发布, 保留出处即可。欢迎根据你自己的实战经验提 issue 或 PR。*
