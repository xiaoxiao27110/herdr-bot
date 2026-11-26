# herdr-bot

**把 Anthropic 的长期记忆（Auto-Dream）与常驻 Agent 架构，跑在原生的 [Herdr](https://herdr.dev) + Coding Agent CLI（`agy` / `codex` / 自定义适配器）终端上。**  
*An open-source implementation of Anthropic's [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory) and [Building always-on agents](https://claude.com/resources/articles/building-always-on-agents/), running natively on [Herdr](https://herdr.dev) with pluggable Coding Agent CLIs (`agy`, `codex`, or custom adapters).*

---

## 面向多仓库研发：它解决什么问题？

当你日常同时维护多个关联代码仓库，希望 Coding Agent 能**跨天、跨周持续协作**时，本质上面对的是 Anthropic 两篇工程文章讨论的两个核心问题：

1. **多仓长期记忆与记忆腐化**（[The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory)）  
   换对话、重启或上下文变长后，Agent 要么忘掉跨仓接口契约和历史决策，要么笔记越堆越臃肿、与真实代码脱节。
2. **常驻推进、主动反馈与安全边界**（[Building always-on agents](https://claude.com/resources/articles/building-always-on-agents/)）  
   多仓研发天然是异步和事件驱动的——需要 Agent 平时休眠零开销，在主分支有敏感改动、构建定时到期或每日早晨时主动醒来汇报与核验，同时克制不刷屏、严守管辖边界。

**`herdr-bot` 完整落地了这两篇文章的工程架构，并在此之上额外补齐了两层贴近日常开发习惯的交互：**
- **飞书 / Telegram 随手交互**：离开电脑也能单窗口派活、看每日研发晨报、手机一键远程审批；
- **Herdr 人工直接介入**：所有会话原生跑在 Herdr + `agy` 终端里，回到电脑敲 `herdr` 就能直接切入同一个活着的对话现场围观、插话或接管。

---

## 核心设计

### 1. 落地 [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory)：四层记忆与夜间 Dreaming

- **四层记忆栈 + 体积守卫**：对应文章中的 `Transcripts → Memory Notes → Memory Representations → Injected Context`，沉淀为完整对话日志、当日热区（`notes/today.md`）、跨仓契约与决策库（`memory/contracts/`、`memory/decisions.md`）以及精简入口索引（`AGENTS.md`），自动校验体积上限与跨仓路径有效性。
- **空闲增量蒸馏 + 夜间 4 阶段 Dreaming**：对话空闲时在独立后台自动提炼高信号决策与在途事项；每晚按 `Orient → Gather → Consolidate → Prune` 流程对照真实仓库源码与 Git 历史核验纠偏，把验证过的事实晋升为长期记忆、剔除过时记录。
- **静默带交接摘要换新**：夜间收口静默后，以及白天上下文体量超标并空闲后，自动汇总交接摘要并在原地开启干净的新对话——长期运行不腐化、记忆不断档。

### 2. 落地 [Building always-on agents](https://claude.com/resources/articles/building-always-on-agents/)：常驻五原语与 4 槽位上下文

- **4 槽位常驻上下文**：持续维护个人偏好与纠错红线（`User notes`）、近期外发防复读（`Past messages`）、跨仓任务进度（`Task progress`）以及已注册仓库的边界契约，避免反复叮嘱同一件事。
- **事件驱动、自主定时唤醒与每日研发晨报**：
  - 仅监听已注册仓库主分支上的敏感路径提交，静默核验下游契约影响；
  - 支持 Agent 自主设定一次性或周期性唤醒闹钟跟进构建与验收，换新对话自动迁移；
  - 结合持久外发与夜间免打扰降级，每天早晨基于真实研发记录与各仓 Git 状态抽象提炼一封「每日研发晨报」（昨日推进与决策、待确认问题、当前代码状态、系统提醒），不堆砌零碎 commit。
- **清晰可控的管辖边界**：仅对你注册的业务仓库与记忆工作区拥有读写开发权限，外部文件仅作只读参考，严禁越界修改未注册仓库或对编排器自身进行自我迭代。

### 3. 两层交互扩展：飞书远程 + Herdr 人工直接介入

- **飞书单窗口 + 忙碌合并排队 + 手机一键审批**：手机上只对着一个总管窗口聊，总管忙时自动合并排队、忙完一次性回复；终端遇到权限或命令确认弹窗时直推飞书交互卡片，手机点一下即可放行或拒绝。
- **Herdr 原生终端随时介入 + 按需并行面板**：回到电脑敲 `herdr` 直接面对同一个活着的 `agy` 对话现场；需要单独拉一条线并行开发时，按需开启独立 Git Worktree 任务面板（`#Topic`），人工切入过的面板自动加锁免清理。
- **后台全隔离 + 主机重启原地自愈**：巡检、蒸馏、Dreaming 与晨报全在一次性临时面板跑完即关，绝不污染主会话；配好常驻服务后，主机重启自动原地恢复总管以及关机前仍存活的手动 Herdr `agy` 会话（你主动关掉的绝不乱复活）。

---

## 典型的一天

- **09:30 晨报**：飞书收到抽象提炼后的「每日研发晨报」，30 秒看清昨天推进、待拍板问题和各仓代码状态。
- **白天协作**：出门在飞书单窗口随口交办，遇到权限确认手机一点放行；回工位敲 `herdr` 直接介入同一个活着的 `agy` 现场。
- **23:30 做梦**：独立临时面板自动完成记忆蒸馏、代码对账 Dreaming 与干净工作树归档，随后无感带摘要换新对话。
- **机器重启**：`systemd --user` 自动拉起服务，原地恢复总管与关机前存活的手动面板，开箱继续干活。

---

## 2 步极速上手

```bash
# 1. 安装（自动检查 agy / herdr，注册 herdr-bot 与短别名 hq）
git clone https://github.com/xiaoxiao27110/herdr-bot.git
cd herdr-bot
./install.sh

# 2. 交互式初始化工作区、托管仓库与消息通道（feishu / telegram / local）
herdr-bot setup
```

启动常驻服务（推荐）与随时介入现场：

```bash
# 安装为 systemd --user 开机自启服务（含重启无感自愈，重启服务不断开 Herdr 面板）
herdr-bot service install --hq-dir ~/herdr-hq

# 随时打开 Herdr TUI 查看现场或直接介入对话
herdr
```

<details>
<summary><b>非交互初始化与通道接入说明（飞书 / Telegram / 本地终端）</b></summary>

**非交互一键初始化：**
```bash
herdr-bot setup --non-interactive \
  --hq-dir ~/herdr-hq --project-name my-platform \
  --repo api-svc=~/code/api-svc --repo web-app=~/code/web-app \
  --channel feishu --feishu-app-id cli_xxx --feishu-app-secret sec_xxx --feishu-chat-id oc_xxx
```

- **飞书 / Lark（推荐，WebSocket 长连接免公网 IP）**：创建企业自建应用开启机器人，事件订阅选「长连接」并添加 `im.message.receive_v1` 与 `card.action.trigger`；不知道群 ID 时可运行 `herdr-bot feishu detect-chat --hq-dir ~/herdr-hq`。
- **Telegram**：创建开启 Topics 的 Supergroup 并设为管理员，运行 `herdr-bot telegram detect-chat --hq-dir ~/herdr-hq` 自动探测。
- **纯本地体验（零外部平台）**：`channel: local` 下运行 `herdr-bot chat --hq-dir ~/herdr-hq --standalone`。

</details>

---

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `herdr-bot setup` | 初始化工作区、托管仓库与消息通道 |
| `herdr-bot service install / status / restart` | 管理开机自启常驻服务 |
| `herdr-bot digest --hq-dir <dir>` | 手动生成并推送「每日研发晨报」 |
| `herdr-bot status --hq-dir <dir>` | 查看总管与各任务面板状态 |
| `herdr-bot task spawn <repo> <slug> "<需求>"` | 按需开启独立 Git Worktree 与 Herdr 任务面板 |
| `herdr-bot seal / resume <id>` | 静默封口任务面板 / 原地复活已归档会话 |
| `herdr-bot timer set / list / cancel` | 管理一次性或周期性自主跟进闹钟 |
| `herdr-bot config list / set <key> <val>` | 热调整换新阈值、免打扰时段等调优参数 |
| `herdr-bot guard / doctor --hq-dir <dir>` | 校验记忆体积边界 / 自检本机运行环境 |

---

## 支持的 Agent（`agy` / `codex` / 自定义接入）

`herdr-bot` 通过开放的 `CodingAgentAdapter` 适配层支持多种 Coding Agent CLI，默认使用 `agy`，也可切换为 OpenAI Codex CLI 或接入自定义适配器：

- **Antigravity CLI（`agent.backend: agy`，默认）**：开箱支持交互式会话、生命周期 Hook（`.agents/hooks.json`）与增量 transcript 同步。
- **OpenAI Codex CLI（`agent.backend: codex`）**：详见下方「使用 Codex CLI」小节。
- **自定义 Agent 接入**：无需改动核心代码，继承 `CodingAgentAdapter` 后可通过 `hq.yaml` 中的 `agent.backend: "your_pkg.adapter:YourAdapter"` 动态加载，或使用 `@register_agent_adapter("name")` 注册。详见 [docs/agent-adapters.md](docs/agent-adapters.md)。

### 使用 Codex CLI

1. **前置条件**：本机已安装并登录 Codex CLI（运行 `codex --version` 确认，推荐 `0.163+`），并已安装 `herdr`。
2. **启用方式**：
   - 新建工作区并直接启用 `codex` 后端：
     ```bash
     ./install.sh --agent codex
     herdr-bot setup --agent-backend codex --hq-dir ~/herdr-hq
     ```
   - 或在现有 `hq.yaml` 中将 `agent.backend` 改为 `codex`；若 `codex` 不在默认 `PATH` 中，可传 `herdr-bot setup --agent-backend codex --agent-bin /path/to/codex` 或在 `.hq/local.yaml` 中设置 `bin_overrides.codex`。
3. **自动信任与 Hook 信任跳过（及风险提示）**：
   - Codex 0.163+ 仅在 `$CODEX_HOME/config.toml`（未设 `CODEX_HOME` 时为 `~/.codex/config.toml`）中存在 `[projects."<abs>"] trust_level = "trusted"` 时才会加载项目级 `.codex/hooks.json`，且子目录与独立 Git Worktree 不继承父目录信任。
   - 默认 `agent.auto_trust_project: true`：`herdr-bot setup` 及 daemon 在拉起会话 / 创建 worktree 前，会自动且幂等地将 `$HQ_DIR`、托管仓库与 worktree 根写入 `$CODEX_HOME/config.toml`（仅在实际改动前创建 `config.toml.bak-herdr-<时间戳>` 备份，不改动或重排原有配置与注释，原子写入并校验 TOML 合法性）。
   - 启动会话时 `CodexAdapter` 会自动附带 `--dangerously-bypass-hook-trust`，使 `herdr-bot` 自动写入的项目级 `.codex/hooks.json` 得以免交互触发；在 `#Topic` 独立 Git Worktree 场景下，为适配 Codex 0.163 的 worktree Hook 查找机制，`herdr-bot` 会同时合并写入 worktree 与其主仓的 `.codex/hooks.json`，并将未跟踪的 `/.codex/hooks.json` 幂等加入 `<git-common-dir>/info/exclude` 以免弄脏 `git status`（若主仓直接手动运行 `codex`，`hq-agent-hook` 会安全忽略外部目录，不会误绑定或误推消息）。
   - **风险提示与手动模式**：自动信任与跳过 Hook 校验意味着上述托管目录中的项目级 `.codex/hooks.json` 会在无人值守下自动执行；如不希望自动修改 `config.toml`，可在 `hq.yaml` 中设 `agent.auto_trust_project: false`（或 `setup --no-auto-trust-project`），此时仅打印需手动粘贴的 TOML 片段。
4. **权限模式映射**（`hq.yaml` 中 `agent.permission_mode`）：

   | `agent.permission_mode` | 传入 `codex` 的参数 | 行为说明 |
   | --- | --- | --- |
   | `default`（默认） | *(无额外权限参数)* | 沿用 `~/.codex/config.toml` 默认策略 |
   | `accept-edits` | `-s workspace-write -a on-request` | 工作区可写，按需请求审批 |
   | `turbo` / `always-proceed` | `-s workspace-write -a never` | 工作区可写且免确认（适合飞书无人值守） |
   | `skip-all` | `--dangerously-bypass-approvals-and-sandbox` | 跳过全部审批与沙箱（仅限受控隔离环境） |

5. **切回 `agy`**：将 `hq.yaml` 中的 `agent.backend` 改回 `agy`（或运行 `herdr-bot setup --agent-backend agy --hq-dir ~/herdr-hq`），然后重启 `herdr-bot` 服务即可。
6. **接入其他 Agent CLI**：参见 [docs/agent-adapters.md](docs/agent-adapters.md)。

---

## License & Disclaimer

MIT License — see [LICENSE](LICENSE).

> **Community Disclaimer**：`herdr-bot` is an unofficial, community-built open-source tool built on top of [Herdr](https://herdr.dev) (`https://github.com/herdrdev/herdr`), not affiliated with or endorsed by the official Herdr team. 本项目不打包 Herdr 二进制，安装脚本仅引导从官方渠道获取。
