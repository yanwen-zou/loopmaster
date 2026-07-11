# LoopViz · LoopMaster 四角色可视化

为 LoopMaster 的四个角色 **Handler / Strategist / Worker / Auditor** 各做一个可视化界面，
展示：**每个 agent 拥有/关联哪些 skills、做了什么、上下文 I/O**。

界面直接读取真实 run 产物（`plan.md` / `trace.jsonl` / `summary.md` / `review.md` /
`*_agent.json`）与技能注册表（`SKILL.md`），不是假数据。

## 启动

```bash
cd F:/探月计划黑客松/loopmaster/loopviz
python app.py            # 仅需 flask
# 打开 http://127.0.0.1:5010/
```

## 界面结构

- **顶部**：run 选择器 + 审计结论徽章。
- **流水线条**：Handler → Strategist → Worker → Auditor，彩色角色卡，点击切换，卡上显示该角色本轮关键状态。
- **全景 & 技能库**：闭环概览、四角色职责、共享技能注册表（感知/控制分组、参数）。
- **四个角色页**（每个一套独立界面，三栏）：
  1. **拥有/关联的 Skills** —— Handler 拥有整表；Strategist 高亮 *selected*；Worker 显示 *executed ×次数*；Auditor 显示 *evaluated + control 标记 + 仿真泄漏检测*。
  2. **做了什么** —— Handler 交接链/产物；Strategist 计划步骤+判据+风险+研究问题；Worker 执行**时间线**（含控制后自动注入的 `worker.monitor` observe 闭环证据）；Auditor 判定+根因+证据。
  3. **上下文 I/O** —— 每个角色的输入/输出、契约 contract、Codex 子代理 JSON（若有）、summary/review 原文。

## 角色配色

| 角色 | 颜色 | 职责 |
|---|---|---|
| 🎛️ Handler 调度官 | 琥珀 | 掌管运行/工作区/连接/移交 |
| 🧭 Strategist 策略师 | 紫 | 选技能、写 plan.md |
| 🦾 Worker 执行官 | 青 | 执行、控制后 observe、写 trace/summary |
| 🔍 Auditor 审计官 | 绿 | 审证据、判 done/retry/blocked/research_needed |

## 数据从哪来

`app.py` 按顺序扫描 run 目录（存在即合并）：

1. 环境变量 `LOOPMASTER_WORKSPACE_ROOT`
2. `../_viz_runs`（仓库内，已含 4 个演示 run）
3. `~/.loopmaster/workspaces`（正式运行默认落盘处）

技能扫描：技能根 `LOOPMASTER_SKILL_ROOT`，默认 `loopmaster_agentic/skills/**/SKILL.md`。

## 生成更多 run 供可视化

在仓库根执行（`--local-agents` 免 Codex，`--dry-run` 免硬件）：

```bash
cd F:/探月计划黑客松/loopmaster
export LOOPMASTER_WORKSPACE_ROOT=./_viz_runs
python -m loopmaster_agentic "set_lift_height height_mm=120 then set_gripper side=left position=0.5" --dry-run --local-agents
python -m loopmaster_agentic "pick up the cola can from the basket" --dry-run --local-agents   # -> research_needed
```

刷新页面即可在 run 选择器看到新 run。接入真实 Codex（去掉 `--local-agents`）后，
`*_agent.json` 会出现，各角色页的「Codex 子代理」区块自动展示 run_intent / proceed /
verdict 等结构化上下文。
```
