# Xul

一个用于整理 Slack / GitHub 上下文并提供 Slack Bot 能力的工具集。

![Xul](assets/xul.png)

## Slack Bot

先执行 `uv sync` 安装依赖，再用 `uv run slack-bot` 启动机器人。

启动时可以指定 LanceDB 本地目录：

```bash
uv run slack-bot --lancedb-dir ./data/lancedb
```

也可以同时指定 mecromancy 用的 sqlite：

```bash
uv run slack-bot \
  --lancedb-dir ./data/lancedb \
  --necromancy-sqlite ./data/necromancy.sqlite
```

默认路径：

- LanceDB：`./data/lancedb`
- mecromancy sqlite：`./data/necromancy.sqlite`

如果目录不存在，程序会先创建目录再连接 LanceDB / 初始化 sqlite。

Slash command：

- `/mecromancy slack <query>`：查询 `slack_users` source
- `/mecromancy github <query>`：查询 `github_users` source
- `/mecromancy links`：查看当前所有 Slack/GitHub link
- `/mecromancy link "<slack selector>" <github_login>`：建立或更新 Slack/GitHub link

`link` 在更新前会先检查两个 source 是否都存在对应用户；只有两边都存在时才会写入 `necromancy_links` 表。

必需环境变量：

- `SLACK_BOT_TOKEN`
- `SLACK_APP_TOKEN`

可选环境变量：

- `LANCEDB_DIR`
- `NECROMANCY_SQLITE`

## Scripts

### `scripts/list_dump_users.py`

列出当前本地 dump 中可用于后续导出的用户。

支持的数据源：

- Slack `slackdump.sqlite`
- GitHub `github_dump/*.json`

基本用法：

```bash
uv run python3 scripts/list_dump_users.py --source all --no-stdout --output ./data/necromancy.sqlite
```

这是当前项目约定的默认执行方式，会把 Slack 和 GitHub 两侧可用用户写入 `./data/necromancy.sqlite`。

常用参数：

- `--source {slack,github,all}`：选择查看哪个数据源，默认 `all`
- `--slack-input`：Slack sqlite 路径
- `--github-input-dir`：GitHub dump 目录
- `--output`：输出 sqlite 路径，默认 `user_context_exports/dump_users.sqlite`
- `--format {table,csv}`：输出格式，默认 `table`
- `--limit`：每个数据源最多输出多少行，默认 `0` 表示不限制
- `--contains`：只保留主标识中包含该子串的用户
- `--no-stdout`：只写 sqlite，不在终端打印

示例：

```bash
uv run python3 scripts/list_dump_users.py --source slack --limit 20
uv run python3 scripts/list_dump_users.py --source github --contains xiang
uv run python3 scripts/list_dump_users.py --source all --output user_context_exports/dump_users.sqlite
uv run python3 scripts/list_dump_users.py --source all --no-stdout --output /tmp/dump_users.sqlite
```

Slack 输出字段：

- `user_id`
- `username`
- `display_name`
- `real_name`
- `email`

GitHub 输出字段：

- `login`
- `issue_or_pr_authored`
- `issue_comments_authored`
- `pr_reviews_authored`
- `pr_review_comments_authored`
- `mentions`

输出 SQLite 包含这些表：

- `metadata`
- `slack_users`
- `github_users`

### `scripts/export_github_issues_prs.py`

从 GitHub API 抓取某个仓库的 issue / PR 及其相关讨论，并将每个 issue 或 PR 保存为单独的 JSON 文件。

输入：

- GitHub 仓库名，格式为 `owner/repo` 或完整 URL
- GitHub Token，来自 `--token` 或环境变量 `GITHUB_TOKEN`

输出：

- 目标目录下的 `issue_*.json` 与 `pr_*.json`

基本用法：

```bash
python3 scripts/export_github_issues_prs.py risingwavelabs/risingwave github_dump
```

常用参数：

- `--state {open,closed,all}`：按状态过滤，默认 `all`
- `--verbose`：输出 retry / rate limit 日志
- `--no-resume`：忽略已有 JSON，强制重新抓取
- `--max-retries`：设置最大重试次数
- `--backoff-seconds`：设置初始退避时间
- `--max-backoff-seconds`：设置最大退避时间

示例：

```bash
GITHUB_TOKEN=xxx python3 scripts/export_github_issues_prs.py \
  risingwavelabs/risingwave \
  github_dump \
  --state all \
  --verbose
```

### `scripts/export_slack_user_contexts.py`

从 `slackdump` 导出的 SQLite 中，提取与指定 Slack 用户有关的消息上下文，并将每个目标用户保存到单独的 SQLite。

匹配规则：

- 用户本人发送的消息
- 消息中出现 `<@USER_ID>` 提及该用户
- 如果命中在线程中，则导出整条线程
- 如果命中的是普通消息，则导出该消息前后 `N` 条频道消息

输入：

- `slackdump` 导出的 SQLite，默认 `slackdump_archive_20260311/slackdump.sqlite`
- 一个或多个 `--user`

输出：

- 目标目录下的 `slack_user_<slug>.sqlite`

基本用法：

```bash
python3 scripts/export_slack_user_contexts.py \
  --user xiangyu \
  --output-dir user_context_exports/slack
```

常用参数：

- `--input`：输入 SQLite 路径
- `--output-dir`：输出目录
- `--user`：目标用户，可重复指定
- `--context-window`：普通消息命中时，保留前后多少条上下文，默认 `3`

`--user` 支持以下几种 selector：

- Slack user id
- `username`
- email
- `real_name`
- `display_name`

示例：

```bash
python3 scripts/export_slack_user_contexts.py \
  --input slackdump_archive_20260311/slackdump.sqlite \
  --user U030D3DQDE3 \
  --user xiangyu@risingwave-labs.com \
  --context-window 5 \
  --output-dir user_context_exports/slack
```

输出 SQLite 包含这些表：

- `metadata`
- `users`
- `channels`
- `contexts`
- `messages`

### `scripts/export_github_user_contexts.py`

从 `github_dump` 目录中的 JSON 文件里，提取与指定 GitHub 用户有关的 issue / PR / comment / review 上下文，并将每个目标用户保存到单独的 SQLite。

匹配规则：

- issue / PR 作者是目标用户
- issue comment 作者是目标用户
- PR review 作者是目标用户
- PR review comment 作者是目标用户
- issue / PR body 或任意 comment / review 中出现 `@login` 提及目标用户

一旦命中，会把整条 issue / PR 的完整上下文写入该用户的导出库。

输入：

- `github_dump` 目录，默认 `github_dump`
- 一个或多个 `--user`，按 GitHub login 精确匹配

输出：

- 目标目录下的 `github_user_<slug>.sqlite`

基本用法：

```bash
python3 scripts/export_github_user_contexts.py \
  --user tabVersion \
  --output-dir user_context_exports/github
```

常用参数：

- `--input-dir`：输入 JSON 目录
- `--output-dir`：输出目录
- `--user`：目标 GitHub login，可重复指定

示例：

```bash
python3 scripts/export_github_user_contexts.py \
  --input-dir github_dump \
  --user tabVersion \
  --user bob \
  --output-dir user_context_exports/github
```

输出 SQLite 包含这些表：

- `metadata`
- `contexts`
- `events`
