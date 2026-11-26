# herdr-bot

> **Disclaimer / 社区声明**：An unofficial, community-built always-on IM bot & memory orchestrator built on top of [Herdr](https://herdr.dev) (`https://github.com/herdrdev/herdr`). Not affiliated with or endorsed by the official Herdr team. 本项目为独立开源社区生态工具，不打包分发 Herdr 二进制，安装脚本仅引导从 Herdr 官方获取。

面向多仓库研发的 **7×24 确定性 0-LLM 编排引擎**，让你的原生 Coding Agent（如 `agy`）100% 常驻在 **[Herdr](https://herdr.dev)** 交互式终端工作区中，无缝打通 **飞书（WebSocket 免公网 IP）/ Telegram（Forum Topics）/ 本地终端对话** 与跨仓库持久记忆。

> 兼容性说明：命令行同时提供 `herdr-bot` 与短别名 `hq`，内部 Python 包保持 `hq_pilot` 以兼容既有脚本与插件生态。

---

## 为什么选择 `herdr-bot`？

1. **零 `-p` 黑盒，100% 原生 Herdr 交互式架构**
   - 守护进程自身 **0-LLM 开销**，不套壳第二层 Agent 框架，所有推理与编码完全交给原生 Agent CLI。
   - 彻底告别无状态 `agy -p` 单次调用：每一个会话都是真实的 **Herdr** 持久交互面板（未安装 Herdr 时自动降级 `tmux`），随时可在终端敲 `herdr` 切入现场围观、打断或人工协作。
   - 通过轻量级生命周期 Hook（`<10ms`）在每轮结束时精准提取干净回复，原位刷新飞书/Telegram「⏳ 正在处理中...」状态卡片。

2. **飞书单窗口只对 `#General` + 忙碌合并排队 + `#Topic` 独立面板**
   - **`#General`（`hq-general` 常驻总管面板）**：飞书单窗口统一对接 `#General`（在主窗口或任意话题内回复一律路由至 `#General` 并回复主窗口）。General 忙碌时仅回一张无按钮排队卡片并原地更新计数，空闲后将排队消息按时间顺序合并为一轮一次性回复。
   - **`#Topic`（`hq-<slug>` 独立任务面板）**：仅在用户明确要求（自然语言或 `/task`）时由 `herdr-bot task spawn` 创建独立 Git Worktree + Herdr 专属面板，多面板并行交互直接在 Herdr 终端中完成。
   - **静默封口与无感换新**：`#Topic` 完成后可手动执行 `herdr-bot seal` 静默封口（更新任务状态并等待夜间安全归档）。`#General` 同时支持夜间收口后静默 `60` 分钟与白天轮数/日志体量超标后静默 `30` 分钟原地 `/new` 带交接摘要换新。

3. **人工接管自动保护（`pinned`）与 4 态安全归档**
   - 会话完整生命周期：`active` → `sealed`（逻辑封口，保留分支与工作树）→ `archived`（每晚 `23:30` 自动蒸馏做梦后清理干净工作树，永久保留 Git 分支与对话记录，支持 `herdr-bot resume <id>` 一键重建工作树原地续聊）。
   - **四不删铁律与自动 `pinned`**：只要你在本地打开 `herdr` 附着面板或手动输入过任何指令，系统通过 SHA-256 输入指纹与终端附着检测自动将会话升级为 `pinned`——**绝不夜间自动归档、绝不删除存在未提交修改或未推送提交的工作树**。

---

## 2 步极速上手

### 第 1 步：一键安装

```bash
git clone https://github.com/your-org/herdr-bot.git
cd herdr-bot
./install.sh
```

> `./install.sh`（支持 `--yes` 非交互模式与 `--python <path>` 指定解释器）会自动检查 `agy` 与 `herdr`（缺失时支持一键调用官方 `curl -fsSL https://herdr.dev/install.sh | sh` 安装）、安装 `pyyaml`（及可选飞书 `lark-oapi`），并将 `herdr-bot` 与 `hq` 软链至 `~/.local/bin/`。

### 第 2 步：运行交互式初始化向导

```bash
herdr-bot setup
```

按提示输入工作区目录（默认 `~/herdr-hq`）、要托管的本地 Git 仓库路径以及消息通道（`feishu` / `telegram` / `local`）。向导会自动探测每个仓库的 `origin` 远程地址、默认分支以及本机 `python_bin` / `agy_bin` / `herdr_bin` 路径，一键生成：

- `hq.yaml`（项目、通道、仓库与调优参数配置）
- `repo-boundaries.yaml`（跨仓库契约监听路径）
- `AGENTS.md`（精简记忆索引与自动开/关 Topic 协作规范）
- `.hq/local.yaml`（本机仓库绝对路径与二进制映射）
- `.hq/secrets.env`（强制 `0600` 权限保存通道密钥）

也支持一条命令全参数非交互初始化（适合脚本自动化）：

```bash
herdr-bot setup \
  --non-interactive \
  --hq-dir ~/herdr-hq \
  --project-name my-platform \
  --repo api-svc=~/code/api-svc \
  --repo web-app=~/code/web-app \
  --channel feishu \
  --feishu-app-id cli_xxx \
  --feishu-app-secret sec_xxx \
  --feishu-chat-id oc_xxx
```

### 启动守护进程与随时接管 Herdr

```bash
# 前台启动 7x24 编排守护进程（自动预热 hq-general 交互面板）
herdr-bot up --hq-dir ~/herdr-hq

# 推荐：安装为 systemd --user 常驻服务（开机自启 + 异常自动拉起，KillMode=process 重启不杀 Herdr 面板；主机重启后自动原地恢复 hq-general 及关机前存活的手动 Herdr agy 面板，尊重用户主动退出/关窗）
herdr-bot service install --hq-dir ~/herdr-hq
herdr-bot service status
herdr-bot service restart

# 在任意终端打开 Herdr TUI，实时查看或随时接管 hq-general / hq-<slug> 面板
herdr
```

---

## 架构总览

```text
飞书单窗口 / Telegram / 本地终端 chat / Git 敏感提交 / 定时任务
                               │
                               ▼
         0-LLM 守护内核 (SQLite 租约队列 + 忙碌合并批次 + 游标状态)
                               │
         ┌─────────────────────┴─────────────────────┐
         ▼                                           ▼
  #General (hq-general)                      #Topic (hq-<slug>)
  Herdr 常驻总管面板 (HQ 记忆根)             独立 Git Worktree + Herdr 面板
  - 承接全部飞书消息，忙时合并排队回复        - 仅按用户明确要求 task spawn 拉起
  - 夜间 / 白天按体量静默带交接摘要 /new 换新  - 多面板并行交互在 Herdr 中进行，手动 seal 静默封口
  - 后台巡检 / 蒸馏 / 23:30 做梦由临时面板     - 人工切入 Herdr 自动转 pinned 免清理保护
    `hq-bg-*` 独立执行并即刻关闭，不污染主会话
```

---

## 通道接入说明

### 1. 飞书 / Lark（推荐，WebSocket 长连接免公网 Webhook）
1. 在飞书开放平台创建企业自建应用，开启**机器人能力**，事件订阅选择**使用长连接接收事件**，添加 `im.message.receive_v1` 与卡片回调 `card.action.trigger`。
2. 运行 `herdr-bot setup` 填入 `FEISHU_APP_ID` 与 `FEISHU_APP_SECRET`；若尚不清楚群聊 `chat_id`，把机器人拉入目标群后执行：
   ```bash
   herdr-bot feishu detect-chat --hq-dir ~/herdr-hq --timeout 30
   ```
3. 将探测到的 `oc_xxx` 填入 `hq.yaml` 的 `channel.allowed_chat_id`（或重跑 `herdr-bot setup` 更新）。

### 2. Telegram（Forum Topics 话题群）
1. 创建一个开启 **Topics（话题）** 功能的 Telegram Supergroup 并将 Bot 设为管理员（授予 Manage Topics 权限）。
2. 在群内发一条消息后执行自动探测：
   ```bash
   herdr-bot telegram detect-chat --hq-dir ~/herdr-hq
   ```

### 3. 纯本地终端对话（零外部依赖）
无需配置任何聊天平台密钥，`channel: local` 下直接通过内置终端聊天与守护进程交互：
```bash
herdr-bot chat --hq-dir ~/herdr-hq --standalone
```

---

## 常用命令速查

| 命令 | 说明 |
| --- | --- |
| `herdr-bot setup` | 交互式/非交互式配置向导（生成 `hq.yaml`、`.hq/local.yaml`、`0600` `secrets.env`） |
| `herdr-bot up --hq-dir <dir>` | 启动 7×24 守护进程（Ctrl+C 或 `herdr-bot stop` 优雅退出） |
| `herdr-bot service install / status / restart / uninstall` | 安装或管理 `systemd --user` 开机自启常驻服务（支持 `--render-only` 预览单元文件） |
| `herdr-bot digest --hq-dir <dir> [--no-llm] [--mark-sent]` | 手动采集只读研发事实并通过临时面板提炼推送每日研发晨报（`--mark-sent` 避免当日定时点重复推送） |
| `herdr-bot status --hq-dir <dir>` | 查看所有追踪中的 `#General` / `#Topic` 会话、分支与 4 态状态 |
| `herdr-bot task spawn <repo> <slug> "<需求>"` | 拉起独立 Git Worktree + Herdr 交互面板 |
| `herdr-bot seal <session_key> [--reason "..."]` | 静默封口指定会话（更新任务状态并保留 Worktree 至夜间 `23:30` 安全归档） |
| `herdr-bot resume <id_or_slug>` | 原地复活已归档会话（自动重建同分支 Worktree 并恢复原对话） |
| `herdr-bot pin / unpin <id_or_slug>` | 手动加锁豁免夜间归档 / 解锁转为 `sealed` |
| `herdr-bot config list / set <key> <val>` | 查看或热更新 20+ 项调优参数（如 `general_max_turns`） |
| `herdr-bot guard --hq-dir <dir>` | 校验 `AGENTS.md` / `today.md` 行数体积预算与 `<repo>:<relpath>` 路径有效性 |
| `herdr-bot doctor --hq-dir <dir>` | 检查本机 `git` / `agy` / `herdr` / `tmux` 与密钥环境状态 |

---

## License

MIT License — 详见 [LICENSE](LICENSE)。
