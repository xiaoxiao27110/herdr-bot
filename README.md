# herdr-bot

**Always-On Coding Agent & Multi-Repo Orchestrator built on [Herdr](https://herdr.dev) & Antigravity CLI (`agy`).**  
A self-hosted **Feishu / Lark Bot & Telegram Bot** with one-tap mobile Remote Approval, Reboot Self-Healing, daily R&D digests, and a full production implementation of Anthropic's **Layered Memory + Auto-Dream** and **Always-On Agent Harness (4-Slot Memory)** practices.

让原生 **Antigravity CLI (`agy`)** 100% 常驻在 **[Herdr](https://herdr.dev)** 交互式终端工作区中的 **7×24 AI 研发总管**——无缝打通 **飞书 / Lark、Telegram 与本地终端**，把多仓库协作、手机端远程审批、跨重启无感自愈、长期记忆与夜间做梦（Auto-Dream）、每日研发晨报整合成一套开箱即用的个人研发中枢。

> **Community Disclaimer / 社区声明**：`herdr-bot` is an unofficial, independent open-source community tool built on top of [Herdr](https://herdr.dev) (`https://github.com/herdrdev/herdr`). It is not affiliated with, sponsored by, or endorsed by the official Herdr team. 本项目为独立开源社区工具，不打包或分发任何 Herdr 二进制文件，安装脚本仅引导从 Herdr 官方渠道获取。

> 命令行同时提供 `herdr-bot` 与短别名 `hq`（如 `hq status`、`hq digest`）。

---

## 核心亮点一览

| 维度 | `herdr-bot` 带给你的体验 |
| --- | --- |
| **原生交互式常驻** | 告别无状态 `-p` 单次调用；原生 `agy` 常驻 Herdr 终端工作区，飞书、Telegram 与本地 `herdr` TUI 共享同一个大脑 |
| **手机端远程指挥与审批** | 飞书单窗口直连总管、忙碌自动合并排队；终端权限/操作确认弹窗直推飞书卡片，手机一键放行或拒绝 |
| **零污染后台执行** | 跨仓巡检、记忆蒸馏、夜间做梦、每日晨报全部在独立临时面板运行并即刻关闭，不打断、不污染主会话 |
| **主机重启无感自愈** | 一键配置 `systemd --user` + `Linger` + `KillMode=process`，开机自动原地恢复总管与关机前存活的手动 Herdr `agy` 会话 |
| **分层长期记忆 & Auto-Dream** | 完整落地 Anthropic Memory & Auto-Dream 实践：空闲增量蒸馏 + 夜间 4 阶段对照代码核验纠偏 + 静默带摘要换新 |
| **常驻 Harness & 4 槽位记忆** | 完整落地 Anthropic Always-On Agent 实践：4 槽位上下文、主分支敏感提交核验、每日研发晨报、自主定时唤醒与清晰管辖边界 |

---

## 亮点一：原生 Herdr + Antigravity CLI (`agy`) 交互式常驻体验

传统 IM Coding Bot 往往把每次对话包装成无状态的一次性 CLI 调用（`-p`），不仅丢失交互现场、无法中途介入，也很难处理长任务中的权限确认与多仓库上下文。`herdr-bot` 选择了一条完全不同的路径：**不套壳第二层 LLM 框架，让原生 `agy` 直接常驻在 Herdr 终端工作区里**。

- **人机共享同一个大脑，随时切入现场**：你在飞书/Telegram 里和总管聊到一半，回到电脑只需敲一声 `herdr`，就能直接看到同一个正在运行的 `agy` 终端面板——实时围观推理过程、随时插话纠偏或亲手接管，不存在“聊天机器人一套、本地终端另一套”的割裂感。
- **飞书单窗口直连总管 + 忙碌合并排队**：所有日常问答、跨仓排查与需求推进都在一个飞书主窗口里连贯对接总管（`#General`）。当总管正忙于处理长任务时，后续消息不会丢、也不会刷屏报错——系统只维护一张实时更新计数的排队卡片，等当前轮次结束立即按序合并处理。
- **手机端一键远程审批（Remote Approval）**：长任务跑到关键步骤需要确认命令或授权操作时，终端里的确认弹窗会实时推送为飞书交互卡片。你无需守在电脑前，在地铁、会议室或手机上点一下按钮即可放行或拒绝，Agent 立刻继续推进。
- **按需并行独立面板 + 人工接管免清理保护**：当你需要单独拉一条线并行攻关时，可以随时让总管为指定仓库开启独立的 Git Worktree 与专属 Herdr 任务面板（`#Topic`）；只要你在本地终端切入或手动操作过某个面板，系统就会自动加锁保护（`pinned`），绝不误删未提交或未推送的代码。
- **后台任务全隔离，零打扰、不污染主会话**：无论是定时巡检、空闲记忆蒸馏、夜间做梦还是生成每日研发晨报，都在**独立临时面板**里悄然完成并即刻关闭，完全不占用、不污染你的主对话上下文。
- **主机重启无感自愈（Reboot Self-Healing）**：执行 `herdr-bot service install` 即可一键配置 `systemd --user`、`Linger` 与 `KillMode=process`：
  - 更新或重启守护服务时，不断开正在运行的 Herdr 终端工作区；
  - 主机重启或意外断电恢复后，开机自动在后台原地拉起总管会话，并自动恢复关机前仍存活的手动 Herdr `agy` 会话；
  - 严格尊重用户意图——你主动退出或关闭的面板不会被强行复活。

---

## 亮点二：完整落地 Anthropic 两篇经典 Agent 工程实践

让一个 Coding Agent “能对话”很容易，让它**连续工作几周、跨越多个仓库、记住你的偏好、不出现上下文腐化、也不胡乱越界**才是真正的工程难题。`herdr-bot` 将 Anthropic 公布的两套经典 Agent 工程实践深度融合为日常自动运转的产品能力。

### 实践一：分层长期记忆与夜间 Dreaming（Anthropic Memory & Auto-Dream）

1. **多层记忆体系 + 体积预算守卫**
   - 将知识按生命周期清晰分层：精简核心索引（`AGENTS.md`）+ 跨仓接口契约（`memory/contracts/`）+ 架构决策与避坑原则（`memory/decisions.md`）+ 当日热区日志（`notes/today.md`）。
   - 配套自动守卫检查（`herdr-bot guard`），严格控制索引与热区体积上限，并校验跨仓路径引用是否真实有效，避免记忆越积越胖、越用越脏。
2. **对话空闲自动增量蒸馏**
   - 白天高强度讨论和编码之后，每当会话进入空闲，系统自动在独立临时面板里回溯增量对话，提炼高信号的架构决策、跨仓改动与在途状态写入当日热区，无需你手动整理纪要。
3. **夜间 4 阶段 Dreaming（`Orient → Gather → Consolidate → Prune`）**
   - **Orient（定位）**：读取核心索引、跨仓边界、现有契约、决策库与当日热区；
   - **Gather（核验）**：逐条对照真实仓库源码与 Git 提交历史，核实当日记录是否真正落地，剔除已被推翻或与代码不符的过时说法；
   - **Consolidate（晋升）**：将经核验的跨仓契约、架构规则与在途任务晋升沉淀到长期记忆；
   - **Prune（修剪）**：归档当日热区、清空次日缓冲区并运行记忆守卫校验，保持长期记忆始终轻盈可信。
4. **静默带交接摘要换新，长期运行不腐化**
   - 夜间收口静默后，以及白天总管对话体量达到上限并空闲后，系统会自动提炼当前在途上下文与交接摘要，在原地无缝开启干净的新对话——既有记忆延续性，又始终保持最佳推理质量。

### 实践二：常驻型 Agent Harness 与 4 槽位记忆（Always-On Agent & 4-Slot Memory）

1. **4 槽位核心上下文（4-Slot Memory）**
   - **个人偏好与纠错红线（`User notes`）**：记住你的工程习惯、沟通风格与明确纠正过的红线，避免反复叮嘱；
   - **近期外发防复读（`Past messages`）**：感知近期已推送过的汇报与提醒，拒绝“车轱辘话”式重复打扰；
   - **任务进度（`Task progress`）**：持续追踪跨仓库在途事项与阻塞点；
   - **跨仓边界契约（`Repo boundaries & contracts`）**：按当前涉及的仓库精准带入关联契约，减少无关噪音。
2. **跨仓敏感提交自动核验、分级通知与每日研发晨报**
   - 自动监测已注册仓库主分支上涉及跨仓接口/边界路径的敏感提交，在独立临时面板核验下游影响；
   - 通知按重要程度分级，夜间自动启用免打扰降级，不打断休息；
   - **每日研发晨报**：每天早晨自动采集真实研发活动与 Git 事实，经临时面板抽象提炼为结构化晨报——清晰呈现**昨日核心推进与决策、待确认问题、各仓代码状态**，彻底告别流水账式的零碎 commit 列表。
3. **Agent 自主定时唤醒（Self-Scheduled Timers）**
   - 遇到“等远程构建 15 分钟后检查结果”或“每天固定时间复查构建状态”的场景，总管可自主设定一次性或周期性唤醒闹钟，并且闹钟会自动跟随会话换新迁移，绝不因为换新对话而遗忘跟进。
4. **明确可控的管辖边界（Strict Jurisdiction）**
   - 仅对你主动注册的业务仓库与记忆工作区具备读写和开发权限（改代码前优先切功能分支，不随意直推主分支）；
   - 排查问题时可将系统其他日志与外部文件作为只读参考；
   - 严禁越界修改未注册仓库、外部系统工具或对编排器自身进行自我迭代，确保常驻运行始终安全可控。

---

## 一天怎么用（典型工作流）

```text
09:30  收到「每日研发晨报」
       飞书自动推送昨日推进与关键决策、待拍板事项、各仓代码状态，30 秒掌握全局。
       │
10:30  通勤或开会间隙，飞书单窗口发消息给总管
       “帮我查一下 gateway-svc 昨天的改动有没有影响 worker-svc 的协议字段，顺便补个单测。”
       有补充想法直接继续发，总管忙时自动合并排队、不丢上下文。
       │
10:42  手机收到远程审批卡片
       总管在终端发起敏感操作确认，卡片直推飞书——手机一点「允许」，任务继续跑。
       │
14:00  回到工位，打开终端输入 `herdr`
       无缝切入同一个活着的 `agy` 面板查看现场或接管联调；需要并行推进另一条需求时，
       让总管按需拉起独立 Git Worktree 面板（`#Topic`），互不干扰。
       │
23:30  下班后自动蒸馏与夜间 Dreaming
       独立临时面板自动核验当天记忆与真实代码，晋升长期记忆、清理过期热区并做好交接；
       即便半夜宿主机重启，次日开机也会自动原地恢复工作区，随时待命。
```

---

## 适用场景 / Who Is This For

- **多仓库并行研发的工程师 / Tech Lead**：日常同时维护 2~10 个关联仓库，希望有一个熟悉跨仓接口契约与历史决策的常驻 AI 研发总管。
- **重度使用 Herdr + Antigravity CLI (`agy`) 的开发者**：不想被锁在封闭的 Web 聊天框里，希望 IM 远程交互与本地终端原生体验合二为一。
- **希望“离开电脑也能推进研发”的团队与个人**：需要通过飞书 / Lark 或 Telegram 随时派发排查与编码任务，并用手机一键完成远程审批。
- **关注长期记忆与 Always-On Agent 工程落地的实践者**：寻找一套开箱即用、可自托管的 Anthropic Memory / Auto-Dream / 4-Slot Agent Harness 参考实现。

---

## 2 步极速上手

### 第 1 步：一键安装

```bash
git clone https://github.com/xiaoxiao27110/herdr-bot.git
cd herdr-bot
./install.sh
```

> `./install.sh`（支持 `--yes` 非交互模式与 `--python <path>` 指定 Python）会自动检查本机 `agy` 与 `herdr`（缺失时支持一键引导调用 Herdr 官方脚本安装）、安装运行依赖，并将 `herdr-bot` 与短别名 `hq` 链接至 `~/.local/bin/`。

### 第 2 步：运行初始化向导

```bash
herdr-bot setup
```

按提示填写记忆工作区目录（默认 `~/herdr-hq`）、要托管的本地 Git 仓库路径及消息通道（`feishu` / `telegram` / `local`）。向导会自动探测仓库远程地址、默认分支与本机工具路径，一键生成工作区配置、跨仓边界模板、`AGENTS.md` 记忆索引及 `0600` 权限的密钥文件。

也支持一条命令非交互初始化（适合自动化部署）：

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

### 启动常驻服务与随时接管

```bash
# 推荐：一键安装为 systemd --user 常驻服务（开机自启 + Linger + 重启不杀 Herdr + 主机重启原地自愈）
herdr-bot service install --hq-dir ~/herdr-hq
herdr-bot service status

# 或在前台直接运行（适合首次体验与调试）
herdr-bot up --hq-dir ~/herdr-hq

# 随时在任意终端打开 Herdr TUI，查看现场或无缝接管总管 / 任务面板
herdr
```

---

## 通道接入

### 1. 飞书 / Lark（推荐，WebSocket 长连接免公网 IP）

1. 在飞书开放平台创建企业自建应用，开启**机器人能力**；
2. 事件订阅选择**使用长连接接收事件**，添加消息接收事件 `im.message.receive_v1` 与卡片回调 `card.action.trigger`（用于手机端远程审批）；
3. 运行 `herdr-bot setup` 填入 `FEISHU_APP_ID` 与 `FEISHU_APP_SECRET`；若不确定目标群 `chat_id`，把机器人拉入群聊后执行自动探测：
   ```bash
   herdr-bot feishu detect-chat --hq-dir ~/herdr-hq --timeout 30
   ```
4. 将探测到的 `oc_xxx` 写入 `hq.yaml` 的 `channel.allowed_chat_id`（或重新运行 `herdr-bot setup`）。

### 2. Telegram（Forum Topics 话题群）

1. 创建一个开启 **Topics（话题）** 功能的 Telegram Supergroup，并将 Bot 设为管理员（授予 Manage Topics 权限）；
2. 在群内发送任意消息后执行自动探测：
   ```bash
   herdr-bot telegram detect-chat --hq-dir ~/herdr-hq
   ```

### 3. 纯本地终端对话（零外部平台依赖）

无需注册任何聊天平台账号，配置 `channel: local` 即可在终端直接体验完整编排与记忆能力：

```bash
herdr-bot chat --hq-dir ~/herdr-hq --standalone
```

---

## 常用命令速查

| 命令 | 说明 |
| --- | --- |
| `herdr-bot setup` | 交互式 / 非交互式初始化向导，一键生成工作区与密钥配置 |
| `herdr-bot service install / status / restart / uninstall` | 管理 `systemd --user` 常驻自启服务与重启无感自愈 |
| `herdr-bot up --hq-dir <dir>` | 前台启动 7×24 守护进程（`Ctrl+C` 或 `herdr-bot stop` 优雅停止） |
| `herdr-bot digest --hq-dir <dir> [--no-llm] [--mark-sent]` | 手动采集真实研发事实并通过临时面板提炼推送每日研发晨报 |
| `herdr-bot status --hq-dir <dir>` | 查看总管与各任务面板的状态、分支与保护标记 |
| `herdr-bot task spawn <repo> <slug> "<需求>"` | 为指定仓库拉起独立 Git Worktree + 专属 Herdr 交互面板 |
| `herdr-bot seal <session_key> [--reason "..."]` | 静默封口任务面板，等待夜间安全归档 |
| `herdr-bot resume <id_or_slug>` | 原地复活已归档会话（自动重建同分支 Worktree 并恢复上下文） |
| `herdr-bot pin / unpin <id_or_slug>` | 手动加锁豁免夜间归档 / 解除锁定 |
| `herdr-bot timer set / list / cancel` | 管理一次性或周期性自主跟进提醒闹钟 |
| `herdr-bot guard --hq-dir <dir>` | 检查记忆索引体积预算与跨仓路径引用有效性 |
| `herdr-bot config list / set <key> <val>` | 查看或调整日常调优参数（如换新阈值、免打扰时段等） |
| `herdr-bot doctor --hq-dir <dir>` | 一键自检本机 `git`、`agy`、`herdr`、`tmux` 与通道环境 |

---

## License

MIT License — 详见 [LICENSE](LICENSE)。
