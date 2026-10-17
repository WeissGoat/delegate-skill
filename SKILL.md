---
name: delegate
description: Hand a long, mechanical, well-specified task to a cheaper worker agent (an engine/model pair: Codex or Hermes Agent running DeepSeek or Gemini flash on the user's relay, Claude Haiku headless, or any other headless CLI) and read back only its report, so the lead agent saves tokens and keeps its context clean. Use for build or app walkthroughs driven by a command tool, simulation and parameter sweeps, test-failure and log triage, config or doc consistency checks, broad read-only code searches with a verdict, and re-running a self-check after a fix. Not for design or feel judgments, work that needs the lead's conversation context, or anything a single command answers.
---

# 派活给廉价 worker

一条路：**判断值不值得派 → 写 brief → 后台跑 `delegate.py` → 只读报告 → 抽查一条证据 → 处理改动警告**。

脚本：`python <skill>/scripts/delegate.py`（Python 3.8+，无依赖）。它把 brief 加上通用规则写进一个 UTF-8 任务文件，命令行只带一个 ASCII 指针（Windows 的 `.cmd` 包装会把命令行里的中文弄乱），再调起 worker；worker 的最终回答就是报告。

## 1. 值不值得派

| 派出去 | 自己做 |
|---|---|
| 过程长、输出多，但结论短：走查打包版、跑一组模拟扫参、翻几千行日志找第一个异常 | 一两条命令就能回答的检查（写 brief 的 token 比省下的还多） |
| 步骤和判定都写得清楚，换一个人照做结果一样 | 设计取舍、手感、"好不好看"这类判断 |
| 只读或只写 scratch 文件 | 需要改代码、提交，或者依赖本会话已有的上下文 |
| 等它的时候主 agent 还有别的事可做 | 主 agent 下一步就要用结果、又没别的事做（自己做通常更快） |

同一份资源（Unity Editor、一个打包版的命令口、某个端口）同一时间只给一个 agent 用，用 `--lock` 互斥。

## 2. 选 worker

```bash
python <skill>/scripts/delegate.py profiles     # 列出引擎、模型、登记的组合、别名，以及 CLI 在不在
python <skill>/scripts/delegate.py probe        # 每个登记的组合发一句话，看谁现在能用；--profile 只测指定的
```

profile 就是 **`壳/模型`**：壳（引擎）负责跑 agent 循环、调工具，模型负责想。两者独立，任意组合：

- **引擎**是内置的：`codex`、`claude`、`hermes`，脚本知道各自的命令行、只读怎么开、报告从哪拿。别的 CLI 用 `cmd` 引擎（见下）。
- **模型**写短名（`deepseek`、`gemini`、`deepseek-pro`、`haiku`，在 `profiles.json` 的 `models` 里）或完整 id，例如 `--profile hermes/gemini-3.8-flash-high`、`--profile codex/some-new-model`，不用先登记。
- `profiles.json` 的 `profiles` 只登记**某个组合需要的特殊处理**，例如 `codex/gemini` 要换短系统提示。
- 旧名字是别名：`deepseek`=`codex/deepseek`，`gemini`=`codex/gemini`，`deepseek-pro`，`codex`=`codex/default`（用 Codex 配置里的模型），`haiku`=`claude/haiku`，`hermes`=`hermes/gemini`。

**不写 `--profile` 时只用 `codex/deepseek` 一个**，失败不会自动换。任务更适合别的组合时（见下表），用 `--profile` 写明。想要备选时写成逗号分隔的链，前一个遇到 429、CLI 缺失或没有回答就换下一个。失败有两类，换法不同：

- **模型那边**（中转站限流、额度）：同一个壳换模型，例如 `--profile codex/deepseek,codex/gemini`。
- **壳本身**（Codex 升级坏了、没装）：换壳，例如在链尾加 `hermes/gemini`（只读任务会跳过它）。

除了 `claude/haiku`，都在用户的中转站上免费。

| 组合 | 特点（2026-10-17 实测） | 什么时候用 |
|---|---|---|
| `codex/deepseek` | 快：同一份只读核对 88 秒、8 次 shell 调用，命令会合并着跑；对含糊的地方会讨论口径；数字偶尔给概数（"10+"） | 首选。走查、复跑、日志排查、扫参 |
| `codex/gemini` | 慢：同一份核对 416 秒、41 次 shell 调用，一条一条地查；更穷举、更字面，计数精确，还多查了 brief 没点名的路径 | 要逐项穷举、精确计数的清单核对；DeepSeek 结论可疑时做第二意见 |
| `codex/deepseek-pro` | 推理更多，更慢 | brief 需要多步推理 |
| `claude/haiku` | Claude Code headless，花 Claude 额度 | 别的都不能用时 |
| `hermes/gemini`、`hermes/deepseek` | Hermes Agent。**只有完整模式**：它只能按工具集选工具，`file` 工具集读写绑在一起，`--mode read` 时自动跳过 | Codex 本身出问题时兜底 |

- **模型都不能判断图片。** 同一张截图，DeepSeek 和 Gemini 都说"看到了"，却各自编出了不存在的按钮。看画面的结论由主 agent 自己看截图得出（640 宽很省 token），worker 只负责截图、给出路径，事实从文字输出（`Describe` 之类）里拿。
- 同一份只读任务可以同时派给两个模型交叉核对，结论不一致的地方就是要你亲自抽查的地方。
- `codex/gemini` 带了 `"instructions": "worker_instructions.md"`，用一段短的系统提示替换 Codex 自带的约 2 万字系统提示。原因：中转站上的 Gemini 一收到 Codex 那段提示就回 `RESOURCE_EXHAUSTED`（429），之后整个凭据池冷却约 40 秒；同样的任务换成短提示就正常。Hermes 自带的提示短，不需要。别的组合遇到"小请求正常、经 Codex 的请求 429"时，也可以试这个字段（只对 codex 引擎有效）。
- 只读必须是壳真的拦得住写入，只靠提示词说"别改"不算。引擎没有只读时，脚本跳过它，并在页脚写明。
- Hermes 的安装和配置：
  - Windows 上用官方 `install.ps1`。
  - 从 Claude 桌面版里装时要加 `-HermesHome C:\Users\<你>\.hermes`。原因：桌面版是 MSIX 应用，它启动的进程写进 `AppData\Local` 的东西会被重定向到应用自己的目录，装完在应用外找不到。
  - 再把用户环境变量 `HERMES_HOME` 设成同一路径。
  - 中转站配成 `model.provider: custom` 和 `model.base_url`，key 放进 `$HERMES_HOME/.env`，`model.key_env` 指向它。
- 接别的 CLI（opencode、qwen 等）：在 `profiles.local.json`（不进 git）里登记一个 `"engine": "cmd"` 的组合。`argv` 里可以用 `{prompt}`、`{model}`、`{cwd}`、`{report}` 占位，不写 `{report}` 时以 stdout 作为报告。它没有真正的只读时写 `"modes": ["full"]`。例：`{"profiles": {"oc/deepseek": {"engine": "cmd", "model": "relay/deepseek-4.1-flash", "modes": ["full"], "argv": ["opencode", "run", "-m", "{model}", "{prompt}"]}}}`。`profiles.local.json` 里也可以覆盖 `default`、`models` 和 `aliases`。
- Gemini CLI 自带的个人 OAuth 免费额度已经停用（报 `IneligibleTierError`），所以 Gemini 走中转站。

## 3. 写 brief

模板见 [references/brief-template.md](references/brief-template.md)。要点：

- **目标一句话**，然后编号步骤，每步写出要跑的**确切命令**。
- **判定写死**：什么算 PASS、什么算 FAIL、哪些已知现象不算问题。worker 没有你的上下文，判定含糊它就会自己发挥。
- **停止条件**：最多跑几轮、连续几次没进展就停、卡住时附上什么。
- 只要结论和证据，不要过程。通用规则已经要求了格式（结论 / 发现 / 没做到的，中文，约 80 行以内），brief 里不必重复。
- brief 写成文件，放在被 git 忽略的目录里（例如 `tmp/agent_reports/brief_<名字>.md`）；短的也可以 `-` 从 stdin 传（stdin 里的中文要是 UTF-8）。

## 4. 跑

```bash
python <skill>/scripts/delegate.py run <名字> <brief.md> [--profile <壳/模型>[,备选...]] [--mode read|full] [--rules 项目规则.md] [--lock unity] [--timeout 30] [--quiet]
```

- `--mode read`：Codex 用 read-only 沙箱、Claude 用 plan 模式，worker 什么都写不了。适合日志排查、代码或文档审查。
- `--mode full`（默认）：可以运行程序、往报告目录写 scratch 文件，例如驱动打包版、跑模拟脚本。规则禁止它改 git 跟踪的文件，脚本会在事后核对。
- `--rules`：项目自己的 worker 规则（怎么驱动游戏、什么不能碰），可以写多个，也可以用环境变量 `DELEGATE_RULES`。项目一般会提供一个包装脚本把它带上，项目的 AGENTS.md 会写明。
- 走查和扫参一般要几分钟到十几分钟：**在 Claude Code 里用 Bash 的 `run_in_background: true` 跑**，完成时会自动通知，不要轮询。能并行的只读任务可以同时派几个；`--mode full` 且碰同一资源的不要并行。
- 退出码：0 PASS，1 FAIL，2 无法完成或没有 worker 回答，3 用法错误或锁被占用。

产物都在 `<项目>/tmp/agent_reports/`（`--out` 可改，必须被 git 忽略）：

| 文件 | 内容 |
|---|---|
| `<时间>_<名字>.md` | 报告：worker 的回答 + 页脚（用了哪个 profile、耗时、改动警告） |
| `.task.md` | worker 实际收到的全部指令 |
| `.log` | worker 的完整输出，只在排查 worker 本身时看 |
| `.changes.patch` | 运行期间工作区的改动（只在有改动时生成） |

## 5. 读报告

- 默认把整份报告打印出来；`--quiet` 只打印路径和结论一行，需要细节时再读文件。
- **抽查**：挑最关键的一条发现，自己跑一遍它给的命令或打开它引的文件行，对得上再采信。廉价模型偶尔会把"推测"写成"看到"，对图片尤其如此。
- 结论是 `无法完成` 时先看"没做到的"和页脚：是 brief 写得不清楚（改 brief 重派），是环境问题（自己修），还是所有 profile 都 429（稍后再派，或换 `--profile`）。
- 不把 worker 的结论原样转述成"已验证"：向用户报告时写明是 worker 查的、你抽查了哪条。

## 6. 改动警告

页脚出现 `WARNING: the working tree changed during the run` 时：

- 它比对的是运行前后的整个工作区，所以**你自己或别的会话**在这期间的改动也会出现在这里，先排除这些。
- 剩下的才是 worker 改的。看 `.changes.patch`：有价值的（比如它顺手写的修复）由你决定要不要采用，然后自己按正常流程改；不要的手动还原那几个文件。
- 不要用 `git checkout .` 或 `git stash` 一把还原：工作区里可能有用户或别的会话的改动。
- `HEAD moved` 表示有人提交或切了分支，同样先确认是谁。

## 7. 接入一个项目

在项目里放一份 worker 规则（例如 `tools/agent/delegate_rules.md`），写：

- 怎么驱动这个项目（命令工具、测试命令），哪些资源同一时间只能一个 agent 用；
- 这个项目里 worker 不能碰的东西（Editor、生产配置、外部服务）；
- 截图、日志等的尺寸和位置约定。

再在项目的 AGENTS.md 里写一行怎么派活，最好给一个包装脚本，把 `--rules` 和常用的 `--lock` 带上。
