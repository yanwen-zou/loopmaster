# -*- coding: utf-8 -*-
"""
LoopViz —— LoopMaster 四角色（Handler / Strategist / Worker / Auditor）可视化
读取真实 run 产物（plan.md / trace.jsonl / summary.md / review.md / *_agent.json）
+ 技能注册表（SKILL.md），为每个 agent 渲染：拥有哪些 skills、做了什么、上下文。

启动：
    cd F:/探月计划黑客松/loopmaster/loopviz
    python app.py
默认扫描 run 目录：环境变量 LOOPMASTER_WORKSPACE_ROOT、../_viz_runs、~/.loopmaster/workspaces
"""
import os
import re
import ast
import json
from pathlib import Path
from flask import Flask, jsonify, render_template

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
SHIPPED_SKILL_ROOT = REPO_ROOT / "loopmaster_agentic" / "skills" / "base"

app = Flask(__name__)


# --------------------------- 角色静态元数据（取自框架 contract） ---------------------------
ROLES = {
    "handler": {
        "name": "Handler", "cn": "调度官", "icon": "🎛️", "color": "#f5a524",
        "duty": "Owns the run, workspace, robot connection, and role handoff.",
        "duty_cn": "掌管整轮运行、工作区、机器人连接，并按顺序移交给其余三个子代理。",
        "contract": ("You own the LoopMaster run and hand off to Strategist, Worker, and Auditor. "
                     "Do not execute shell commands or edit files."),
        "produces": ["handler_agent.json", "(workspace)"],
        "skill_relation": "拥有整个技能注册表，但自己不执行技能——只连接平台、分发任务。",
    },
    "strategist": {
        "name": "Strategist", "cn": "策略师", "icon": "🧭", "color": "#a855f7",
        "duty": "Selects registry-backed skills from the goal and writes plan.md.",
        "duty_cn": "从目标出发，在技能注册表里挑选可用技能，产出 plan.md。",
        "contract": ("Produce a registry-grounded plan for a real robot. Use only provided skill names. "
                     "Keep stop_motion at the end when any control skill appears."),
        "produces": ["plan.md", "strategist_agent.json"],
        "skill_relation": "从全部已注册技能中筛选并编排出计划步骤（selected）。",
    },
    "worker": {
        "name": "Worker", "cn": "执行官", "icon": "🦾", "color": "#22d3ee",
        "duty": "Executes the plan, observes after control actions, writes summary.md / trace.jsonl.",
        "duty_cn": "执行计划里的每个技能，控制动作后自动 observe，失败则 stop_motion，写轨迹与总结。",
        "contract": ("Review the plan before local code executes registered platform skills. "
                     "Return proceed=false only for a concrete safety or registry issue."),
        "produces": ["trace.jsonl", "summary.md", "worker_agent.json"],
        "skill_relation": "真正调用（execute）技能，并自动注入 observe / stop_motion 安全动作。",
    },
    "auditor": {
        "name": "Auditor", "cn": "审计官", "icon": "🔍", "color": "#34d399",
        "duty": "Reviews trace evidence, detects missing learned skills, writes review.md.",
        "duty_cn": "依据轨迹证据判定结果（done/retry/blocked/research_needed），发现缺失的可学习技能。",
        "contract": ("Independently review the plan and trace. Classify the run as done, retry, "
                     "blocked, or research_needed. Do not execute tools or edit files."),
        "produces": ["review.md", "auditor_agent.json"],
        "skill_relation": "不执行技能，只评估用了哪些技能、控制技能是否安全收尾、是否有仿真泄漏。",
    },
}

CONTROL_SKILLS = {"send_action", "move_arm_joints", "set_gripper", "set_base_velocity", "set_lift_height"}


# --------------------------- 技能注册表扫描 ---------------------------
def _parse_frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    m = re.search(r"\n---\s*\n", text[3:])
    if not m:
        return {}, text
    raw = text[3:m.start() + 3]
    body = text[m.end() + 3:]
    data, cur = {}, None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if v:
                data[k] = v
                cur = None
            else:
                data[k] = {}
                cur = data[k]
        elif cur is not None and ":" in line:
            k, v = line.split(":", 1)
            cur[k.strip()] = v.strip()
    return data, body


def load_skills():
    roots = [(SHIPPED_SKILL_ROOT, False)]
    user_root = Path(os.environ.get("LOOPMASTER_SKILL_ROOT", "~/.loopmaster/skills")).expanduser()
    roots.append((user_root, True))
    out = {}
    for root, is_user in roots:
        if not root.exists():
            continue
        for md in root.rglob("SKILL.md"):
            fm, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
            rel = md.parent.relative_to(root).parts
            name = fm.get("name") or md.parent.name
            if name in out:
                continue
            category = fm.get("category") or "/".join(rel[:-1]) or "base"
            out[name] = {
                "name": name,
                "category": category,
                "group": category.split("/")[-1],  # perception / control
                "description": fm.get("description") or "",
                "args": fm.get("args") if isinstance(fm.get("args"), dict) else {},
                "is_user": is_user,
                "is_control": name in CONTROL_SKILLS,
                "body": body.strip(),
            }
    return sorted(out.values(), key=lambda s: (s["group"], s["name"]))


# --------------------------- run 产物解析 ---------------------------
def _run_roots():
    seen, roots = set(), []
    for p in [os.environ.get("LOOPMASTER_WORKSPACE_ROOT"),
              str(REPO_ROOT / "_viz_runs"),
              str(Path("~/.loopmaster/workspaces").expanduser())]:
        if not p:
            continue
        rp = Path(p).expanduser()
        if rp.exists() and str(rp) not in seen:
            seen.add(str(rp))
            roots.append(rp)
    return roots


def list_runs():
    runs = []
    for root in _run_roots():
        for d in root.iterdir():
            if d.is_dir() and (d / "plan.md").exists():
                runs.append(d)
    runs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return runs


def _read(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _load_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def parse_plan_md(text):
    """从 plan.md 抽取结构化字段。"""
    sections = {}
    cur = None
    for line in text.splitlines():
        h = re.match(r"^##\s+(.*)", line)
        if h:
            cur = h.group(1).strip()
            sections[cur] = []
        elif cur:
            sections[cur].append(line)
    title = ""
    tm = re.match(r"^#\s+Plan:\s*(.*)", text)
    if tm:
        title = tm.group(1).strip()

    def bullets(key):
        out = []
        for ln in sections.get(key, []):
            m = re.match(r"^-\s+(.*)", ln.strip())
            if m:
                out.append(m.group(1).strip())
        return out

    steps = []
    for ln in sections.get("Steps", []):
        m = re.match(r"^\s*(\d+)\.\s+`([^`]+)`\s+args=(\{.*?\})(?:\s+-\s+(.*))?$", ln)
        if m:
            try:
                args = ast.literal_eval(m.group(3))
            except (ValueError, SyntaxError):
                args = {}
            steps.append({"idx": int(m.group(1)), "skill": m.group(2),
                          "args": args, "why": (m.group(4) or "").strip()})
    goal = " ".join(l.strip() for l in sections.get("Goal", []) if l.strip())
    return {
        "title": title, "goal": goal, "steps": steps,
        "success_criteria": bullets("Success Criteria"),
        "risks": bullets("Risks"),
        "assumptions": bullets("Assumptions"),
        "research_questions": bullets("Research Questions"),
        "subagent_notes": bullets("Subagent Notes"),
    }


def parse_review_md(text):
    def grab(label):
        m = re.search(rf"\*\*{label}\*\*:\s*`?([^`\n]*)`?", text)
        return (m.group(1).strip() if m else "").strip("`")

    def listline(label):
        m = re.search(rf"^-\s+{label}:\s*(.*)$", text, re.MULTILINE)
        if not m:
            return []
        val = m.group(1).strip()
        if val in ("(none)", ""):
            return []
        return [x.strip() for x in val.split(",") if x.strip()]

    research = []
    if "## Research Needed" in text:
        tail = text.split("## Research Needed", 1)[1]
        research = [m.group(1).strip() for m in re.finditer(r"^-\s+(.*)$", tail, re.MULTILINE)]
    return {
        "verdict": grab("Verdict") or "unknown",
        "root_cause": grab("Root cause"),
        "next_action": grab("Next action"),
        "used_skills": listline("Used skills"),
        "used_control_skills": listline("Used control skills"),
        "sim_leak": listline("Simulation leak terms"),
        "research_questions": research,
    }


def parse_trace(path):
    steps = []
    if not path.exists():
        return steps
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            steps.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return steps


def parse_run(run_dir):
    plan = parse_plan_md(_read(run_dir / "plan.md"))
    review = parse_review_md(_read(run_dir / "review.md"))
    trace = parse_trace(run_dir / "trace.jsonl")
    # 从目录名拆 run_id / task
    dirname = run_dir.name
    m = re.search(r"-(\d{8}-\d{6}-[0-9a-f]{6})$", dirname)
    run_id = m.group(1) if m else dirname
    task = plan.get("title") or dirname

    # worker 执行统计
    ok_n = sum(1 for s in trace if s.get("ok"))
    roles_seen = sorted({s.get("role", "worker") for s in trace})

    return {
        "run_id": run_id,
        "task": task,
        "dir": str(run_dir),
        "mtime": run_dir.stat().st_mtime,
        "verdict": review["verdict"],
        "success": review["verdict"] == "done",
        "plan": plan,
        "review": review,
        "trace": trace,
        "summary_md": _read(run_dir / "summary.md"),
        "trace_stats": {"total": len(trace), "ok": ok_n, "fail": len(trace) - ok_n, "roles": roles_seen},
        "agent_json": {
            "handler": _load_json(run_dir / "handler_agent.json"),
            "strategist": _load_json(run_dir / "strategist_agent.json"),
            "worker": _load_json(run_dir / "worker_agent.json"),
            "auditor": _load_json(run_dir / "auditor_agent.json"),
        },
        "artifacts": sorted(p.name for p in run_dir.iterdir() if p.is_file()),
    }


# --------------------------- 路由 ---------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/skills")
def api_skills():
    return jsonify(ok=True, skills=load_skills(), roles=ROLES)


@app.route("/api/runs")
def api_runs():
    runs = list_runs()
    brief = []
    for d in runs:
        r = parse_review_md(_read(d / "review.md"))
        pm = re.match(r"^#\s+Plan:\s*(.*)", _read(d / "plan.md"))
        brief.append({
            "id": d.name,
            "task": (pm.group(1).strip() if pm else d.name),
            "verdict": r["verdict"],
            "mtime": d.stat().st_mtime,
        })
    return jsonify(ok=True, runs=brief, roots=[str(x) for x in _run_roots()])


@app.route("/api/run/<path:run_id>")
def api_run(run_id):
    for root in _run_roots():
        cand = root / run_id
        if cand.exists() and (cand / "plan.md").exists():
            return jsonify(ok=True, run=parse_run(cand), roles=ROLES,
                           control_skills=sorted(CONTROL_SKILLS))
    return jsonify(ok=False, msg="run not found"), 404


if __name__ == "__main__":
    print("=" * 52)
    print("  LoopViz · LoopMaster 四角色可视化")
    print("  http://127.0.0.1:5010/")
    print("  扫描 run 目录:")
    for r in _run_roots():
        print("   -", r)
    print("=" * 52)
    app.run(host="0.0.0.0", port=5010, debug=True)
