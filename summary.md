# Xul Slack Bot 招魂流程总结

本文总结的是当前仓库里 Xul Slack bot 的“招魂”能力，也就是 `/summon` / `/招魂` 这条链路，以及招魂前必须准备的流程、每一步做了什么、为什么这样设计。

这里的“招魂”本质上不是把一个通用大模型直接切成不同 persona，而是把一个 Slack 身份和一个 GitHub 身份先绑定成一个 `linked necromancy`，然后围绕这个身份对，构造出：

1. 可检索的本地上下文库；
2. 一个描述此人说话风格的 `soul profile`；
3. 一个按 Slack thread 隔离的“当前激活 persona”状态。

后续在这个 thread 里继续 `@bot` 普通发言时，bot 才会按这个 persona 回答。

## 一、先说结论：Xul 的招魂到底在做什么

可以把整个机制理解成五段式流水线：

1. 先准备原始数据源。
   Slack 原始数据来自 `slackdump.sqlite`，GitHub 原始数据来自 `github_dump/*.json`。
2. 再把“可用身份”整理进本地索引库。
   通过 `scripts/list_dump_users.py` 把 Slack 用户和 GitHub 用户写入 `data/necromancy.sqlite` 的 `slack_users` / `github_users` 表。
3. 再显式建立 Slack 身份和 GitHub 身份之间的 link。
   只有 link 建好了，`/summon` 才知道“要招的是谁”。
4. 真正执行 `/summon` 时，为这个 link 导出专属上下文、生成 `soul profile`、重建独立 LanceDB 表，并把它激活到当前 thread。
5. 招魂成功后，这个 thread 里的后续普通 mention 不再走默认 Xul persona，而是走“本地检索 + soul profile + OpenAI 生成”的 persona 回复链路。

换句话说，招魂不是一个瞬时动作，而是“身份绑定 + 语料抽取 + 风格蒸馏 + 检索建索引 + thread 级状态切换”的组合。

## 二、招魂前必须准备什么

### 1. Slack bot 本身要能运行

最基本的运行条件是：

- 已安装依赖，项目用法是先执行 `uv sync`。
- Slack bot 启动入口是 `uv run slack-bot`。
- 必须有 `SLACK_BOT_TOKEN` 和 `SLACK_APP_TOKEN`。

这里的原理很简单：

- `SLACK_BOT_TOKEN` 让 bot 能调用 Slack Web API，例如发消息、加 reaction；
- `SLACK_APP_TOKEN` 用于 Socket Mode，让 bot 能持续接收 Slack 事件；
- bot 启动时会初始化 LanceDB 连接和本地 sqlite。

当前默认本地状态存储包括：

- `data/lancedb`：存 LanceDB；
- `data/necromancy.sqlite`：存 Slack/GitHub 用户和 link，以及招魂状态。

## 2. 必须先有原始 Slack / GitHub dump

招魂不是直接从线上 Slack 或 GitHub 实时抓人设，它依赖本地 dump。

### Slack 侧原始输入

Slack 原始输入是 `slackdump_archive_20260311/slackdump.sqlite` 这类 sqlite。  
其中项目代码会从这些表里读取信息：

- `S_USER`：用户信息；
- `CHANNEL`：频道信息；
- `MESSAGE`：消息信息。

### GitHub 侧原始输入

GitHub 原始输入是 `github_dump` 目录下每个 issue / PR 一个 JSON 文件。  
这些 JSON 一般来自 `scripts/export_github_issues_prs.py`，里面包含：

- issue / PR 主体；
- issue comments；
- pull request reviews；
- pull request review comments。

原理上，Xul 的招魂流程依赖“离线已采集语料”，而不是运行时去 API 拉全量历史。这样做有几个明显目的：

- 减少运行时网络依赖；
- 让上下文导出和 persona 构建可重复；
- 降低 Slack bot 在线响应时的复杂度；
- 把昂贵的数据整理步骤前移到本地离线处理。

## 3. 必须先把“可被 link 的身份”写进 necromancy sqlite

`/link` 和 `/summon` 都不是直接扫原始 dump 文件，而是依赖 `data/necromancy.sqlite` 中的索引表：

- `slack_users`
- `github_users`
- `necromancy_links`

这一步通常由 `scripts/list_dump_users.py` 完成。

它的职责不是导出上下文，而是先做“身份目录整理”：

- 从 Slack dump 中抽取用户列表；
- 从 GitHub dump 中统计出现过的 login；
- 把这两类用户写入一个轻量 sqlite，供 bot 做检索和 link。

原理上，这一步是在做“身份控制平面”：

- 原始 dump 很大，直接拿它做 Slack 命令检索成本高；
- bot 的 `/slack`、`/github`、`/link` 只需要用户级索引，不需要完整上下文；
- 所以先把“有哪些人可选”整理成一个小型控制数据库。

## 4. 必须先建立 Slack/GitHub link

这是招魂前最关键的业务前置条件。

### link 是什么

Xul 认为一个 persona 不是单一 Slack 用户，也不是单一 GitHub 用户，而是一个已经确认过的身份对：

- 一个 Slack user；
- 一个 GitHub login。

这个关系写入 `necromancy_links` 表，结构大致是：

- `slack_user_id`
- `github_login`
- `created_at`
- `updated_at`

### link 怎么建立

通过 `/link "<slack selector>" <github_login>` 建立。

其中 Slack selector 支持：

- `user_id`
- `username`
- `email`
- `real_name`
- `display_name`

GitHub 侧则按 `login` 精确匹配。

### link 时做了什么

建立 link 时，代码会：

1. 在 `slack_users` 中解析并唯一定位 Slack 用户；
2. 在 `github_users` 中精确定位 GitHub 用户；
3. 如果任一侧不存在，拒绝建立 link；
4. 删除这个 Slack user 或这个 GitHub login 上旧的 link；
5. 插入新的 `necromancy_links` 记录。

### 为什么招魂前必须 link

因为 `/summon <linked_necromancy>` 并不是让用户临时传两个身份，也不是在现场做模糊猜测；它只接受已经存在的 link。

这样做的好处是：

- 避免 Slack 用户和 GitHub 用户错绑；
- 保证 persona 的身份边界稳定；
- 让 `/summon` 的输入只有一个 selector，交互更简单；
- 把高歧义的身份确认问题放在 `/link` 阶段解决，而不是在正式对话时解决。

## 三、招魂前的“语料准备”到底是什么

严格说，真正的“上下文导出”并不一定要在 `/summon` 之前手工执行，因为 `/summon` 里会自动补做。  
但从流程上看，这仍然属于“招魂前准备的核心组成部分”，因为没有这些导出，后面根本无法构造 persona。

它分成两路：

- Slack 用户上下文导出；
- GitHub 用户上下文导出。

### 1. Slack 用户上下文导出

对应脚本是 `scripts/export_slack_user_contexts.py`，核心逻辑在 `xul_slackbot/user_context_export.py`。

#### 它做了什么

它会从整个 Slack dump 中，抽取“和目标用户相关的消息上下文”，然后写入一个专属 sqlite：

- 输出文件名形如 `slack_user_<slug>.sqlite`；
- 默认输出目录是 `user_context_exports/slack`，而 bot 实际约定目录是 `data/user_context_exports/slack`。

导出后的 sqlite 包含：

- `metadata`
- `users`
- `channels`
- `contexts`
- `messages`

#### 它怎么判断“和用户相关”

匹配规则有两类：

1. 用户本人发的消息；
2. 消息文本里出现 `<@USER_ID>`，即有人提到该用户。

这一步对应的是 `build_slack_context_matches`。

#### 它怎么选上下文范围

如果命中消息属于 thread：

- 直接导出整条 thread。

如果命中消息不是 thread，而是普通频道消息：

- 导出命中消息前后 `N` 条消息，默认 `3` 条。

这一步对应 `collect_slack_context_messages`。

#### 为什么这样设计

因为 Slack 语境高度依赖局部会话结构。

如果只抽用户本人说过的话，会丢掉两个很重要的信息：

- 他是在回应什么；
- 别人是怎么提到他、怎么和他互动的。

而把 thread 整体导出、把普通消息扩成窗口，有几个明显作用：

- 保留最小必要会话上下文；
- 降低无关全量历史的噪音；
- 让后续检索文档既包含目标人的原话，也包含周围语境。

#### 导出的数据结构意味着什么

每一个 `context_key` 表示一段可检索上下文：

- 可能是一整个 thread；
- 也可能是一段消息窗口。

每条消息还会标注 `is_direct_match`，表示这条消息是否是直接命中的那条。  
这使后续构造检索文档时，能区分：

- 哪些是“目标原始命中”；
- 哪些只是“上下文陪衬”。

### 2. GitHub 用户上下文导出

对应脚本是 `scripts/export_github_user_contexts.py`，核心逻辑同样在 `xul_slackbot/user_context_export.py`。

#### 它做了什么

它会从 `github_dump/*.json` 中扫描每个 issue / PR 记录，抽取“和目标 login 有关的完整上下文”，写入专属 sqlite：

- 输出文件名形如 `github_user_<slug>.sqlite`；
- 输出目录约定为 `data/user_context_exports/github`。

导出后的 sqlite 包含：

- `metadata`
- `contexts`
- `events`

#### 它怎么判断“和用户相关”

一个 issue / PR 上下文会命中的情况包括：

1. issue / PR 作者就是目标用户；
2. issue comment 作者是目标用户；
3. PR review 作者是目标用户；
4. PR review comment 作者是目标用户；
5. issue / PR body 或上述任意评论 / review 中出现 `@login` 提及目标用户。

#### 它怎么组织导出结果

只要一个 issue / PR 命中，就把整条上下文保留下来：

- `contexts` 表存 issue / PR 级别信息；
- `events` 表存 body、comment、review、review comment 等事件序列。

#### 为什么 GitHub 是“命中即保留整条上下文”

因为 GitHub 讨论天然是以 issue / PR 为中心的结构化讨论。

相比 Slack 的线性消息流，GitHub 上更合理的单位不是“前后几条”，而是“整条 issue / PR 讨论串”。  
这样做的目的在于：

- 保留设计讨论和 code review 的完整上下文；
- 让 persona 不只学习聊天习惯，也学习此人在工程讨论中的表达习惯；
- 后续检索时能直接命中一整段相关工程语境。

## 四、真正执行 `/summon` 时，完整流水线是什么

核心入口是 `xul_slackbot/summon.py` 里的 `handle_summon_command`。

它接受：

- `db_path`：necromancy sqlite；
- `lancedb`：LanceDB 连接；
- `text`：命令参数；
- `scope_key`：当前作用域，实际就是 Slack thread 维度；
- `progress`：进度回调；
- `locale`：英文或中文。

下面按真实执行顺序展开。

### 第 1 步：解析命令参数

`/summon` 或 `/招魂` 后面只允许一个参数，也就是 `linked_necromancy` selector。

代码用 `shlex.split` 做解析，目的是：

- 支持带引号输入；
- 保持命令行式分词语义；
- 在参数格式错误时能明确报错。

如果参数不是恰好一个：

- 英文返回 `/summon <linked_necromancy>` 用法；
- 中文返回 `/招魂 <linked_necromancy>` 用法。

### 第 2 步：连接本地 necromancy sqlite，并初始化招魂相关 schema

这里会确保两个表存在：

- `summoned_necromancies`
- `summon_state`

它们的职责分别是：

- `summoned_necromancies`：记录一个已构建 persona 的静态资产位置；
- `summon_state`：记录某个作用域当前激活的是哪个 summon。

这一步的原理是把“persona 资产”和“作用域状态”分离：

- persona 资产是可复用、可覆盖更新的；
- 当前 thread 正在用哪个 persona，是另外一层映射。

### 第 3 步：根据 selector 找到已 link 的身份对

这一步调用 `_find_linked_necromancy`。

它会在 `necromancy_links`、`slack_users`、`github_users` 三张表上做 join，然后按以下字段匹配 selector：

- Slack `user_id`
- Slack `username`
- Slack `display_name`
- Slack `real_name`
- Slack `email`
- GitHub `login`

如果没有匹配：

- 直接报 `Linked necromancy not found`。

如果出现多个匹配：

- 直接报歧义错误。

原理上，这一步是在把用户输入收敛成唯一身份对，避免后面导出上下文时“招错魂”。

### 第 4 步：计算该身份对应的预期上下文文件路径

代码通过 `_expected_context_paths` 计算：

- Slack dump 路径：`data/user_context_exports/slack/slack_user_<slack_slug>.sqlite`
- GitHub dump 路径：`data/user_context_exports/github/github_user_<github_slug>.sqlite`

这样做的目的有两个：

1. 路径可预测，可以直接判断是否已存在；
2. 同一个 persona 的导出结果有稳定命名，不需要额外查索引表。

### 第 5 步：检查上下文 dump 是否已经存在，不存在就自动导出

这一步是 `ensure_context_dumps`。

#### 做了什么

1. 检查 Slack 导出库是否存在；
2. 检查 GitHub 导出库是否存在；
3. 哪边不存在，就调用对应脚本去生成；
4. 生成后再次校验文件确实落地。

调用方式是通过 `subprocess.run(...)` 执行：

- `scripts/export_slack_user_contexts.py`
- `scripts/export_github_user_contexts.py`

#### 为什么用脚本子进程，而不是直接函数调用

当前设计更偏“工具链式编排”而不是“单进程全内嵌”：

- 导出脚本本身就是独立 CLI，便于单独运行和调试；
- bot 只是 orchestration 层，需要时补做导出；
- 出错时可以拿到清晰的 stdout/stderr；
- 工具链边界更清楚。

#### 为什么招魂时允许自动补导出

因为这能把使用门槛压低到：

1. 先准备原始 dump；
2. 建 link；
3. 直接 `/summon`。

用户不需要事先手工跑完每个人的上下文导出。  
同时又保留了缓存语义，因为文件存在就不会重复导出。

### 第 6 步：生成 `summon_slug`

`summon_slug` 的格式是：

- `<slack_slug>__<github_slug>`

这是 persona 的稳定主键。

它的作用是：

- 作为 `summoned_necromancies` 主键；
- 作为 soul 文件名的一部分；
- 作为当前 summon 的统一身份标识。

### 第 7 步：构建 `soul profile`

这一步是 `ensure_soul_profile`，输出文件为：

- `data/souls/soul_<summon_slug>.md`

这是招魂流程里非常关键的一层。

#### 7.1 先收集 quotes

`collect_soul_quotes` 会同时从 Slack 和 GitHub 导出库里抽“目标本人亲自说过的话”。

Slack 侧：

- 从 `messages` 表中取 `user_id == target_user_id` 的消息；
- 只保留非空文本；
- 按时间倒序取最近内容。

GitHub 侧：

- 从 `events` 表中取 `author_login == target_login` 的事件；
- 只保留有 body 的内容；
- 按时间倒序取最近内容。

然后它会做三件事：

1. 文本归一化，压平空白；
2. 去重；
3. 过滤太短文本。

最后要求至少有 `20` 条 quote，否则直接报错，不生成 soul。

#### 7.2 再把 quotes 变成风格画像

`render_soul_markdown` 会生成一个 Markdown 文件，内容有两部分：

- `Voice Summary`
- `Original Quotes`

`Voice Summary` 的生成有两种路径：

1. 如果配置了 `OPENAI_API_KEY`，调用 OpenAI 让模型根据 quotes 总结写作和说话风格；
2. 如果没配 key，或者调用失败，就退回到启发式 `_fallback_soul_summary`。

#### 7.3 fallback 版本是怎么工作的

fallback 并不是空壳，它会从 quotes 里估算风格统计量，例如：

- 平均句长；
- 短句占比；
- 长句占比；
- 问句比例；
- 标点使用比例；
- 全小写比例；
- 第一人称比例。

再把这些统计量翻译成一些 imitation rules。

#### 为什么要有 `soul profile`

因为单纯做 RAG 检索，只能给模型局部事实和局部话语片段，但“风格”往往是分布式特征，不一定在一次检索命中的几条上下文里完整出现。  
`soul profile` 的作用就是把分散在大量原话中的风格模式压缩成一份长期稳定的提示材料。

可以把它理解成：

- LanceDB 提供“局部相关记忆”；
- `soul profile` 提供“全局说话风格 prior”。

两者配合，persona 才比较稳定。

### 第 8 步：构建这个 persona 的独立 LanceDB 表

这一步是 `ensure_summon_lancedb_table`。

#### 它做了什么

1. 计算表名 `summon_<slack_name>_<github_login>`；
2. 从 Slack 导出库读取上下文文档；
3. 从 GitHub 导出库读取上下文文档；
4. 合并后写入 LanceDB；
5. 用 `mode=\"overwrite\"` 重建整张表；
6. 为 `searchable_text` 建 FTS 索引。

#### Slack 文档是怎么构造的

每个 Slack `context_key` 会变成一条 LanceDB document。  
文档内容会包含：

- 目标用户 id；
- channel 名；
- context type；
- match reason；
- 上下文里每条消息的时间、作者、正文；
- 哪些是 direct match，哪些只是 context。

#### GitHub 文档是怎么构造的

每个 GitHub `context_id` 会变成一条 LanceDB document。  
文档内容会包含：

- repo；
- issue / PR 编号；
- 标题；
- 作者；
- match reasons；
- 事件流中的每个 event。

#### 为什么是“每段上下文一条文档”

因为 Xul 这里追求的不是 embedding 级语义切块，而是“结构化上下文整体检索”：

- Slack 以 thread 或消息窗口为单位；
- GitHub 以 issue / PR 为单位。

这样检索命中时，返回的是一段成型语境，而不是零碎句子，更适合让模型模仿这个人对某类问题的表达方式。

#### 为什么每个 summon 单独一张表

这是当前设计里非常重要的一点。  
每个 persona 都有自己的 LanceDB table，而不是所有人共用一张大表后再加过滤条件。

这样做的好处是：

- 检索边界非常清楚，不会串 persona；
- 建索引和查询逻辑简单；
- 数据隔离天然成立；
- thread 激活某 persona 后，查询时不需要再额外做复杂过滤。

代价是：

- 人越多，表越多；
- 重建成本按 persona 线性增长。

但在当前项目规模下，这个取舍显然是偏正确性和简洁性优先。

### 第 9 步：把该 summon 激活到当前 thread

这一步是 `activate_summoned_necromancy`。

#### 它做了什么

1. 先删除同一 Slack user 或同一 GitHub login 的其他 summon 记录；
2. upsert 当前 `summoned_necromancies` 记录；
3. 在 `summon_state` 中写入 `scope_key -> summon_slug` 映射。

其中 `scope_key` 会做归一化：

- 有 thread_ts 就用 thread_ts；
- 没有就落到默认全局 key `__global__`。

#### 为什么按 thread 隔离

Slack 对话天然是 thread 化的。  
如果不按 thread 隔离，同一个频道里一旦有人招了 A，别人再和 bot 说话也会被迫进入 A persona，这会非常混乱。

按 thread 隔离后：

- thread A 可以招某个工程师；
- thread B 可以招另一个人；
- 不同 thread 互不干扰。

这就是 `summon_state` 存在的根本原因：它不是“系统当前只有一个 persona”，而是“每个 thread 各自有自己的当前 persona”。

### 第 10 步：把结果和进度回报给 Slack

整个 `/summon` 过程中，会通过进度回调持续输出关键阶段消息。

英文 `/summon` 和中文 `/招魂` 的区别主要不在逻辑，而在进度文案本地化：

- `/summon` 使用英文仪式风格文本；
- `/招魂` 使用中文仪式风格文本。

进度节点大致按 10% 粒度推进：

- 10：解析并定位 linked necromancy
- 30：检查本地 dump
- 40/50：导出 Slack / GitHub dump
- 70：构建 soul profile
- 80：构建 LanceDB 表
- 90：激活 summon
- 100：完成

原理上，这一层是为了掩盖招魂流程的多步耗时特征，避免用户在 Slack 里看到一个长时间无响应的 slash command。

## 五、招魂成功后，后续普通对话是怎么工作的

这部分是“招魂完成后真正生效”的部分，入口是 `build_summoned_reply`。

### 1. bot 如何判断当前是不是在招魂命令

`app_mention` 事件进来后，bot 会先去掉 `<@BOT_ID>` mention 前缀，然后判断文本是否以这些前缀开头：

- `/slack`
- `/github`
- `/link`
- `/links`
- `/summon`
- `/招魂`

如果是 `/summon` 或 `/招魂`：

- 走命令处理链路，也就是前面那条招魂流水线。

如果不是 direct command：

- 走 `build_summoned_reply`。

这意味着：

- “招魂”是一个显式命令；
- “招魂后说话”是普通 mention；
- 二者复用同一个 `app_mention` 入口，但分流到不同逻辑。

### 2. 先看当前 thread 有没有 active summon

`build_summoned_reply` 首先会在 `summon_state` 里按 `scope_key` 查当前激活 persona。

如果没有 active summon：

- 不扮演任何被招魂对象；
- 退回默认 Xul persona；
- 调用 `build_xul_reply`，让模型按 Xul 角色说话。

所以系统的默认行为不是“永远扮演某个人”，而是：

- 无 summon 时，bot 是 Xul；
- 有 summon 时，bot 才是被招的那个人。

### 3. 如果有 active summon，先查本地 LanceDB

找到 active summon 后，bot 会拿当前用户消息作为 query，在该 summon 对应的 LanceDB table 上做全文检索：

- 用的是 FTS；
- 默认取前 `5` 条。

得到结果后，会通过 `format_summon_context` 压缩成简短上下文片段列表。

这里的关键点是：

- 模型本身不会直接访问 LanceDB；
- 检索发生在 bot 本地；
- bot 把检索结果转成 prompt 文本后，再发给 OpenAI。

这就是一个标准的“本地检索 + 远端生成”的 RAG 结构。

### 4. 再读取 `soul profile`

如果当前 summon 记录里有 `soul_path`，并且文件存在：

- bot 会把整个 `soul profile` 读出来，拼进 prompt。

所以 persona 回复的 prompt 由三部分组成：

1. system prompt：强约束“你就是这个人”；
2. soul profile：全局风格摘要；
3. local context：局部相关上下文。

### 5. 构造 persona prompt

`build_summon_prompts` 的 system prompt 很明确，核心意图包括：

- 你就是这个 Slack 用户 / GitHub 用户本人；
- 不是 AI assistant；
- 用第一人称；
- 尽量模仿对方在本地语料中的用词、句长、标点、直接程度、幽默感和节奏；
- 默认回答要短，通常 1 到 4 句；
- 不要自动变得过分礼貌、服务化、总结化；
- 不要提 prompt、模型、检索、也不要承认自己是被 summon 的。

这说明项目的 persona 目标不是“安全、稳定、通用客服式回答”，而是更偏真实聊天风格复现。

### 6. 调用 OpenAI 生成回复

实际调用是 `_call_openai_chat_completion`，通过 HTTP 请求 OpenAI 兼容接口完成。

这里有几个关键实现点：

- API key 来自环境变量或 `.env`；
- 默认模型是 `gpt-4.1-mini`；
- 支持自定义 `OPENAI_BASE_URL`；
- 会对部分瞬时错误做最多 3 次指数退避重试。

#### 为什么这里还要调用大模型

因为 LanceDB 和 soul profile 只能提供：

- 可检索语料；
- 风格约束；
- 局部事实。

真正把这些材料融合成一段自然回复，仍然需要生成模型。

### 7. 最终回复格式

成功时，bot 会返回：

- `<slack_username>: <reply>`

也就是说，最终在 Slack 里看到的回复会显式带上 persona 名字前缀。

这样设计的原因很直接：

- 用户能清楚知道现在是谁在“说话”；
- 即使 thread 里多轮对话，也能持续强化 persona 感；
- 也方便区分默认 Xul 回复和被招魂 persona 回复。

## 六、默认 Xul persona 和招魂 persona 的关系

这套系统实际上有两个层级的人格：

### 默认人格：Xul

当 thread 没有 active summon 时，bot 用 `build_xul_reply`。

它的 prompt 会要求模型：

- 以 Diablo / Heroes of the Storm 里的 Xul 身份说话；
- 语气阴冷、干燥、略带轻蔑；
- 如果用户问“怎么招魂”，要解释 `/summon` 和 `/招魂` 的用法。

### 临时人格：被招魂对象

当 thread 有 active summon 时，bot 不再按 Xul 说话，而是按具体 persona 说话。

所以 Xul 在架构上更像：

- 默认外壳；
- 仪式主持人；
- 没招魂时的 fallback speaker。

而真正被招出来的人，是 thread 作用域里的覆盖层。

## 七、这套设计的核心原理是什么

如果把细节抽象掉，Xul 的招魂机制依赖五个核心原理。

### 1. 先绑定身份，再构造 persona

persona 的基础不是 prompt 文案，而是一个被确认过的 Slack/GitHub 身份对。  
这保证了 persona 不是凭空捏造，而是建立在可追溯数据源之上。

### 2. 先离线整理语料，再在线做轻量编排

重活在离线阶段完成：

- dump；
- 用户索引；
- 上下文导出。

在线招魂阶段只做：

- 缺失导出补齐；
- soul 构建；
- LanceDB 建表；
- state 激活。

这让 bot 在线逻辑保持相对简单。

### 3. 把“风格”和“记忆”拆开建模

风格靠 `soul profile`。  
局部语境和内容事实靠 LanceDB 检索。

这是一个很合理的分层：

- 风格是低频、全局的；
- 记忆是高频、局部的。

如果只靠其中一层，效果都不稳定。

### 4. 按 persona 独立建索引，按 thread 独立激活

这是整个系统隔离性的核心：

- persona 级隔离靠独立 LanceDB table；
- 对话级隔离靠 `thread_ts -> summon_slug`。

这两个维度分开后，串味风险会显著降低。

### 5. 模型不直接连本地数据，bot 自己做 RAG orchestration

模型只看到 prompt，不直接访问 LanceDB 或 sqlite。  
所有本地数据访问都发生在 bot 侧。

这样设计的意义是：

- 可控；
- 可审计；
- 好调试；
- 更容易换模型或换 endpoint；
- 不把数据访问权限交给模型。

## 八、从操作角度看，完整的招魂前置流程应该怎么理解

如果按“从零到可招魂”的顺序整理，最合理的理解是：

### 阶段 A：准备原始数据

1. 准备 Slack dump sqlite。
2. 准备 GitHub issue / PR dump JSON。

这是原始语料层。

### 阶段 B：准备身份索引

1. 运行 `scripts/list_dump_users.py`。
2. 把 Slack 用户与 GitHub 用户写入 `data/necromancy.sqlite`。

这是用户控制面。

### 阶段 C：建立 link

1. 用 `/slack` 和 `/github` 找人；
2. 用 `/link` 建立 Slack/GitHub 身份对。

这是 persona 身份确认层。

### 阶段 D：首次招魂时自动补全 persona 资产

1. `/summon` 定位 link；
2. 检查专属 Slack/GitHub context dump；
3. 缺失则自动导出；
4. 抽 quote 生成 soul profile；
5. 构建 persona 独立 LanceDB 表；
6. 激活到当前 thread。

这是 persona 资产构建层。

### 阶段 E：进入对话态

1. thread 内后续普通 mention 命中 active summon；
2. bot 本地检索 LanceDB；
3. 读取 soul profile；
4. 调 OpenAI 生成该 persona 风格回复。

这是 persona 对话执行层。

## 九、当前实现的一些重要边界与限制

### 1. 没有 OpenAI key 时，招魂“激活”仍可能成功，但真正对话会失败

当前实现里：

- 构建 `soul profile` 时如果没有 `OPENAI_API_KEY`，会 fallback 到启发式摘要，因此这一环不一定失败；
- 但后续 `build_summoned_reply` 生成真正 persona 回复时，`OPENAI_API_KEY` 是硬要求，没有就会直接返回错误文本。

所以严格说：

- 没 key，可以完成部分招魂资产构建；
- 但不能完成真正“开口说话”的 persona 回复。

### 2. 上下文导出基于静态 dump，不会自动跟随线上增量变化

也就是说，persona 的知识和语气是基于当前本地 dump 快照。  
如果原始 Slack / GitHub 数据更新了，得重新导出或重新招魂，才能纳入新语料。

### 3. 每次招魂会 overwrite 对应 LanceDB 表

这意味着当前策略不是增量更新，而是重建式更新。  
好处是简单、确定；缺点是 persona 多起来后重建成本会上升。

### 4. Slack 与 GitHub 只是当前 persona 的两个固定来源

当前代码没有接入更多来源，也没有跨来源权重控制。  
它默认认为：

- Slack 更像即时聊天人格；
- GitHub 更像工程讨论人格；
- 两者共同构成一个人的可模仿画像。

## 十、最终一句话概括

Xul 的 Slack bot “招魂”并不是简单切 prompt，而是先把 Slack 与 GitHub 身份绑定成一个确定的人，再从两侧离线语料中抽取上下文与本人原话，构建 `soul profile` 和独立 LanceDB 检索库，最后把这个 persona 激活到当前 Slack thread；之后该 thread 中的普通 mention，就通过“thread 级状态 + 本地 RAG + 风格摘要 + OpenAI 生成”的方式，以这个人的口吻回答。
