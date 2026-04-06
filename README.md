# Wallet Session Insights

分析 Cobo Agent Wallets 会话日志，生成结构化的 Markdown 报告。

支持三种会话格式：
- **OpenClaw** JSONL 日志
- **Claude Code CLI** JSONL 日志
- **Langfuse trace** JSON 数组

提供会话文件，获得智能代理的行为分析：做了什么、卡在哪里、时间分配、成本统计，**并针对钱包操作进行特定优化**。

---

## 功能特色

### 🎯 Cobo Agent Wallets 专用优化

- **钱包操作识别**：自动检测 MPC 签名、转账、智能合约调用等钱包特定操作
- **交易指标**：统计成功转账数、失败交易、gas 费用等
- **性能基准**：MPC 签名延迟、区块链确认时间等钱包特定的性能指标
- **错误分类**：区分区块链错误（RPC 失败）、签名错误、授权错误等

### 📊 通用分析

- **会话概览**：模型、用户、工作目录、总耗时
- **工具使用统计**：各工具调用次数、成功率、平均耗时
- **性能分布**：LLM 推理、CLI 执行、用户响应、空闲时间的占比
- **错误检测**：命令失败、重复执行、循环检测（polling/error loops）
- **成本分析**：token 数、费用、每分钟成本

---

## 使用示例

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Session: wallet-agent-001
Date:    2026-04-06T10:30:00Z
Model:   claude-opus-4-6 (anthropic)
User:    alice
CWD:     /home/alice/agentic-wallet
Duration: 15m 32s

Stats
  Turns: 22  Tool calls: 45  Errors: 2
  Tokens: 67,200  Cost: $0.134

Wallet Operations
  Successful transfers: 8
  Failed transactions: 1
  Contract calls: 3
  MPC signatures: 12 (avg 2.3s)

Timing
  LLM:      8m 15s  (53%)  avg 5100ms  max 21400ms
  CLI:      4m 02s  (26%)  avg 1200ms  max 8900ms
  Blockchain: 2m 44s (18%)  avg 3100ms  max 15200ms
  Idle:     0m 31s  (3%)

Loops detected: 1
  • caw transfer --wait-confirm × 4 (polling_loop) — 2m 15s

Errors: 2
  • [1] RPC timeout - chain response exceeded 30s
  • [1] Insufficient balance for gas
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

技能会根据发现的问题（循环、错误率、异常终止）提出 2–3 个针对性问题，并生成完整的 Markdown 分析报告。

---

## 需求

- Python 3.10+ (仅使用标准库)
- Claude Code（支持 skill）
- 会话文件格式之一：
  - OpenClaw JSONL (`.jsonl`)
  - Claude Code CLI JSONL (`.jsonl`)
  - Langfuse trace JSON 数组 (`.json`)

---

## 安装

```bash
git clone https://github.com/cobo/wallet-session-insights.git ~/.claude/skills/wallet-session-insights
```

> **注意**：目标路径必须是 `~/.claude/skills/wallet-session-insights`。技能中硬编码了此路径。

安装后重启 Claude Code。

---

## 使用方法

在 Claude Code 会话中：

```
/wallet-session-insights path/to/session.jsonl
```

技能将：

1. **解析**会话文件，在终端显示统计摘要
2. **提问** — 根据数据提出 2–3 个有针对性的问题（重复命令、高错误率、成本等）
3. **生成报告** — 输出至 `path/to/session_analysis.md`，包含你的回答

---

## 报告内容

| 部分 | 覆盖内容 |
|------|---------|
| **摘要** | 用户的目标、执行过程、是否成功 |
| **会话概览** | 模型、用户、工作目录、总耗时 |
| **统计数据** | Turn 数、工具调用数、错误数、token 数、成本 |
| **钱包指标** | 成功转账、失败交易、MPC 签名延迟、gas 费用 |
| **性能分布** | LLM / CLI / 区块链 / 空闲 时间占比 |
| **工具使用** | 各工具调用次数和成功率 |
| **循环检测** | 重复命令（polling loops 或 error loops） |
| **错误日志** | 失败命令、错误信息、时间戳 |
| **对话日志** | 时序的用户/助手交互记录 |

---

## 工作原理

`analyze_session.py` 是一个无依赖的 Python 脚本，解析 OpenClaw/Claude Code CLI/Langfuse 生成的事件流。提取：

- **会话元数据** — 模型、用户、工作目录、耗时
- **对话内容** — 用户和助手的文本交互（去掉工具调用）
- **命令执行** — 所有 exec 工具调用、退出码、耗时
- **钱包操作** — MPC 签名、转账、合约调用（特定优化）
- **时间分析** — LLM 推理、CLI 执行、用户响应、空闲时间
- **循环检测** — 滑动窗口检测重复的标准化命令
- **统计数据** — turn 数、token 用量、成本

脚本输出单个 JSON 对象到 stdout。技能读取此输出并驱动交互式报告生成。

---

## 支持的工具

### 钱包相关
- `caw` — Cobo Agentic Wallet CLI（转账、签名、余额查询）
- `ethers.js` — 以太坊交互
- `web3.py` — Web3 Python 库

### 通用工具
- `exec` — Shell 命令执行
- `edit` — 文件编辑
- `read` — 文件读取
- `web_search` — 网络搜索
- `web_fetch` — 网页获取

---

## 许可证

MIT

---

## 相关资源

- [Cobo Agent Wallets 文档](https://docs.cobo.com/agentic-wallets)
- [OpenClaw 项目](https://github.com/jarosik9/openclaw)
- [Langfuse](https://langfuse.com)
