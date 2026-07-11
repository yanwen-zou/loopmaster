#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LoopViz 演示推送脚本
====================
运行后会持续把「模拟的机器人运行(run)」推送到网站 /loopviz 页面上展示，
让原本空白的智能体页面动起来。**Ctrl+C 停止脚本后即不再推送**（页面停留在最后状态）。

依赖：仅标准库（urllib），无需 pip 安装。

用法：
    # 默认推到线上（内置本地默认写接口令牌）
    python push_demo.py

    # 指定服务器 / 频率 / 只推固定份数
    python push_demo.py --base https://loopmaster.box2ai.com --token XXX --interval 5
    python push_demo.py --base http://127.0.0.1:5000 --token XXX --once 6

    # 停止时顺便删掉本脚本推送过的 run（清场）
    python push_demo.py --token XXX --clean

令牌来源：--token 参数 或 环境变量 LOOPMASTER_API_TOKEN；未设置时使用本地默认令牌。
"""
import os
import re
import json
import time
import random
import argparse
import urllib.request
import urllib.error
from datetime import datetime

DEFAULT_API_TOKEN = "06de644db26bf26dc5fbef2657b5af6b"

# 机械臂/底盘等控制类技能（与后端 LV_CONTROL_SKILLS 保持一致）
CONTROL = {"send_action", "move_arm_joints", "set_gripper", "set_base_velocity", "set_lift_height"}

# 启动时推送的技能注册表（让 /loopviz 的“注册技能”计数不为 0）
SKILLS = [
    ("observe", "perception", "读取机器人本体状态与相机图像", {"include_images": "bool", "include_state": "bool"}),
    ("capture_image", "perception", "抓取指定相机的一帧图像用于规划与审计", {"camera": "str", "required": "bool"}),
    ("scan_basket", "perception", "识别货篮中的商品与位置", {}),
    ("set_base_velocity", "control", "设置移动底盘的平移/旋转速度", {"x": "float", "y": "float", "theta": "float"}),
    ("set_lift_height", "control", "设置升降立柱高度(mm)", {"height_mm": "float"}),
    ("move_arm_joints", "control", "驱动指定手臂到目标关节角", {"side": "str", "joints": "list"}),
    ("set_gripper", "control", "开合指定夹爪", {"side": "str", "position": "float"}),
    ("stop_motion", "safety", "立即停止一切运动，安全收尾", {"reason": "str"}),
    ("oscillate_arm_joint", "control", "让指定手臂单关节围绕当前值来回摆动 N 次",
     {"side": "str", "joint": "int", "amp_rad": "float", "cycles": "int"}),
]

# 演示场景（围绕双臂销售机器人）——每个是一次完整运行
SCENARIOS = [
    {
        # ★ 明星场景：真实自进化循环（左手 joint5 摆动）——worker 预检拦截→策略修订→审计出招→重跑
        "goal": "让左手 joint5 来回摇摆 5 次（±0.5 弧度）",
        "steps": [
            ("observe", {"include_images": False, "include_state": True}, "先建立最新机器人状态"),
            ("oscillate_arm_joint", {"side": "left", "joint": 5, "amp_rad": 0.5, "cycles": 5}, "对 left joint5 做摆动"),
            ("stop_motion", {"reason": "safety abort"}, "安全收尾"),
        ],
        "verdict": "retry",
        "fail_at": 2,
        "root_cause": "learned oscillate_arm_joint 的 dispatch 损坏，未能从最新 observe 物化 7 值目标，摆动未执行",
        "next_action": "修复 oscillate_arm_joint：从最新 observe 构建 [j1..j6,gripper] 7 值向量，仅 joint5 偏移 ±0.5 rad 五个周期后回到起点并 stop_motion，然后重跑",
        "loop": [
            {"role": "handler", "type": "route", "title": "Handler 接收请求，分发子代理", "iter": 0},
            {"role": "handler", "type": "connect", "title": "connecting platform 连接机器人平台", "iter": 0},
            {"role": "strategist", "type": "plan", "steps": ["observe", "move_arm_joints ×5", "stop_motion"], "iter": 1},
            {"role": "worker", "type": "gate_block", "iter": 1, "notes": [
                "计划用旧 trace 的硬编码整臂数组，没有使用最新 observe 结果",
                "真实硬件上任何位姿变化都可能导致多关节意外运动——具体安全隐患",
                "应仅将 left joint5 偏移 ±0.5 rad，其余关节/夹爪保持 observed 值"]},
            {"role": "strategist", "type": "revise", "title": "按安全意见修订", "steps": ["observe", "stop_motion"], "iter": 1},
            {"role": "worker", "type": "skill", "skill": "observe", "ok": True, "iter": 1},
            {"role": "worker", "type": "skill", "skill": "stop_motion", "ok": True, "iter": 1},
            {"role": "auditor", "type": "verdict", "verdict": "retry", "iter": 1,
             "title": "未执行摆动；fresh observe 已含所需数值，却在观测后中止"},
            {"role": "auditor", "type": "skill_update", "title": "新增 oscillate_arm_joint 学习技能", "iter": 1},
            {"role": "strategist", "type": "plan", "steps": ["observe", "oscillate_arm_joint", "stop_motion"], "iter": 2},
            {"role": "worker", "type": "skill", "skill": "observe", "ok": True, "iter": 2},
            {"role": "worker", "type": "skill", "skill": "oscillate_arm_joint", "ok": False, "iter": 2,
             "error": "policy.py must define dispatch(context, args)"},
            {"role": "worker", "type": "skill", "skill": "stop_motion", "ok": True, "iter": 2},
            {"role": "auditor", "type": "verdict", "verdict": "retry", "iter": 2,
             "title": "learned oscillate_arm_joint dispatch 损坏，未能物化 7 值目标"},
            {"role": "auditor", "type": "skill_update", "title": "修复 dispatch 签名与 schema", "iter": 2},
            {"role": "strategist", "type": "plan", "steps": ["observe", "stop_motion"], "iter": 3},
            {"role": "worker", "type": "skill", "skill": "observe", "ok": True, "iter": 3},
            {"role": "worker", "type": "skill", "skill": "stop_motion", "ok": True, "iter": 3},
            {"role": "handler", "type": "final", "verdict": "retry", "iter": 3,
             "title": "The requested left_joint_5 oscillation was not executed（本轮未完成）",
             "next": "修复 oscillate_arm_joint 后重跑：从最新 observe 构建 7 值向量，仅 joint5 ±0.5 rad 五周期"},
        ],
    },
    {
        "goal": "从货篮取出可口可乐并递给顾客",
        "steps": [
            ("observe", {"include_images": True, "include_state": True}, "执行前先建立机器人实时状态"),
            ("scan_basket", {}, "定位货篮中可口可乐的位置"),
            ("set_lift_height", {"height_mm": 120.0}, "升到与货篮齐平的高度"),
            ("move_arm_joints", {"side": "right", "joints": [30, -45, 20, 0, 15, 0]}, "右臂移动到可乐上方"),
            ("set_gripper", {"side": "right", "position": 0.2}, "夹爪闭合抓取可乐罐"),
            ("move_arm_joints", {"side": "right", "joints": [0, -20, 10, 0, 0, 0]}, "把可乐移到交付位"),
            ("set_gripper", {"side": "right", "position": 0.8}, "松开夹爪交付给顾客"),
            ("stop_motion", {"reason": "handler end-of-run safety stop"}, "运行结束安全停机"),
        ],
        "verdict": "done",
    },
    {
        "goal": "驱动底盘靠近顾客并拍摄前置相机",
        "steps": [
            ("observe", {"include_images": True, "include_state": True}, "建立实时状态"),
            ("capture_image", {"camera": "front", "required": False}, "留存视觉证据"),
            ("set_base_velocity", {"x": 0.15, "y": 0.0, "theta": 0.0}, "向顾客方向前进"),
            ("stop_motion", {"reason": "arrived at customer"}, "到位后停止底盘"),
        ],
        "verdict": "done",
    },
    {
        "goal": "补货：把 3 瓶农夫山泉摆到货架",
        "steps": [
            ("observe", {"include_images": True, "include_state": True}, "建立实时状态"),
            ("scan_basket", {}, "识别待补货的水瓶"),
            ("set_lift_height", {"height_mm": 200.0}, "升到货架高度"),
            ("move_arm_joints", {"side": "left", "joints": [20, -30, 15, 0, 10, 0]}, "左臂抓取第 1 瓶"),
            ("set_gripper", {"side": "left", "position": 0.2}, "抓取水瓶"),
            ("move_arm_joints", {"side": "left", "joints": [-10, -25, 12, 0, 5, 0]}, "放置到货架"),
            ("stop_motion", {"reason": "handler end-of-run safety stop"}, "安全收尾"),
        ],
        "verdict": "retry",
        "root_cause": "第 2 次抓取时水瓶滑脱，夹持力不足",
        "next_action": "提高夹爪闭合位并重试补货动作",
        "fail_at": 5,
        "gate": ["计划的夹爪闭合位来自旧参数，未按当前水瓶尺寸校准",
                 "闭合力不足在真实硬件上会掉落——具体安全/成功率隐患",
                 "应先 observe 水瓶位姿，再据此设定夹爪闭合位后抓取"],
    },
    {
        "goal": "把现磨热咖啡递给顾客",
        "steps": [
            ("observe", {"include_images": True, "include_state": True}, "建立实时状态"),
            ("scan_basket", {}, "定位咖啡杯"),
        ],
        "verdict": "research_needed",
        "root_cause": "缺少「端持带热液体容器」的可学习技能，防洒策略未知",
        "next_action": "在用户技能根下学习 carry_hot_beverage 技能后再执行",
        "research": ["如何在移动中保持热饮杯水平以防洒出？", "是否需要专用的热饮夹持末端执行器？"],
    },
    {
        "goal": "检查机器人本体状态与双臂相机",
        "steps": [
            ("observe", {"include_images": True, "include_state": True}, "读取全身关节与底盘状态"),
            ("capture_image", {"camera": "front", "required": False}, "前置相机取证"),
            ("capture_image", {"camera": "wrist_right", "required": False}, "右腕相机取证"),
        ],
        "verdict": "done",
    },
    {
        "goal": "抓取德芙巧克力但货篮为空",
        "steps": [
            ("observe", {"include_images": True, "include_state": True}, "建立实时状态"),
            ("scan_basket", {}, "在货篮中查找德芙巧克力"),
            ("stop_motion", {"reason": "target not found, abort safely"}, "未找到目标，安全中止"),
        ],
        "verdict": "blocked",
        "root_cause": "货篮内未检出德芙巧克力，库存缺货",
        "next_action": "提示后台补货后再下单",
    },
]

BASE = ""
TOKEN = ""


def api(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("X-API-Token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "msg": e.read().decode("utf-8", "ignore")[:200]}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def slug(text):
    # 只保留 ASCII，避免 run id 带中文在 URL 里出问题（页面标题另从 plan.md 读，仍显示中文）
    s = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_")
    return s[:40] or "run"


def make_run_id(goal):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tail = "".join(random.choice("0123456789abcdef") for _ in range(6))
    return f"{slug(goal)}-{ts}-{tail}"


def build_loop_events(scn, trace):
    """把场景渲染成四角色循环事件流（概述里动态回放）。
    scn 若带显式 'loop' 则直接用；否则按 plan→(可选 gate→revise)→execute→verdict→final 生成。"""
    if scn.get("loop"):
        return scn["loop"]
    steps = scn["steps"]
    verdict = scn["verdict"]
    plan_skills = [s[0] for s in steps]
    ev = [
        {"role": "handler", "type": "route", "title": "Handler 接收请求，分发子代理", "iter": 0},
        {"role": "handler", "type": "connect", "title": "connecting platform 连接机器人平台", "iter": 0},
    ]
    if scn.get("gate"):   # 先来一轮被 worker 预检拦截 → 策略修订
        ev.append({"role": "strategist", "type": "plan", "steps": plan_skills, "iter": 1})
        ev.append({"role": "worker", "type": "gate_block", "notes": scn["gate"], "iter": 1})
        ev.append({"role": "strategist", "type": "revise", "title": "按安全意见修订", "steps": plan_skills, "iter": 1})
    else:
        ev.append({"role": "strategist", "type": "plan", "steps": plan_skills, "iter": 1})
    for t in trace:      # worker 逐技能执行（含自动注入的 observe）
        ev.append({"role": t["role"], "type": "skill", "skill": t["skill"], "ok": t["ok"],
                   "error": (t["result"].get("reason") if not t["ok"] else None), "iter": 1})
    ev.append({"role": "auditor", "type": "verdict", "verdict": verdict, "iter": 1,
               "title": scn.get("root_cause") or "所有动作有据可依，闭环安全收尾"})
    if verdict != "done":
        ev.append({"role": "auditor", "type": "skill_update", "title": "补充可学习技能", "iter": 1})
    ev.append({"role": "handler", "type": "final", "verdict": verdict, "iter": 1,
               "title": scn.get("root_cause") or "任务完成",
               "next": scn.get("next_action") or "—"})
    return ev


def build_files(scn):
    """把一个场景渲染成 plan.md / trace.jsonl / review.md / summary.md / *_agent.json。"""
    goal = scn["goal"]
    steps = scn["steps"]
    verdict = scn["verdict"]
    fail_at = scn.get("fail_at")  # 1-based 计划步序，模拟该步失败

    used = sorted({sk for sk, _, _ in steps})
    used_ctrl = sorted({sk for sk, _, _ in steps if sk in CONTROL})

    # plan.md
    plan_lines = [f"# Plan: {goal}", "", "## Goal", goal, "", "## Steps"]
    for i, (sk, args, why) in enumerate(steps, 1):
        plan_lines.append(f"{i}. `{sk}` args={args!r} - {why}")
    plan_lines += [
        "", "## Success Criteria",
        "- Every planned tool call is backed by the LoopMaster skill registry.",
        "- Worker records live observation or explicit platform feedback.",
        "- Worker stops the platform after any control-oriented run.",
        "", "## Risks",
        "- Low-level motion is only planned when the request includes explicit arguments.",
        "- Task-specific manipulation policies stay absent until learned under the user skill root.",
        "", "## Subagent Notes",
        f"- Strategist inspected {len(SKILLS)} registered skill(s).",
        "- Plan uses only discovered skills; no simulation-only predicate is assumed.",
    ]
    if scn.get("research"):
        plan_lines += ["", "## Research Questions"] + [f"- {q}" for q in scn["research"]]
    plan_md = "\n".join(plan_lines) + "\n"

    # trace.jsonl —— 逐步执行，控制类动作后注入 worker.monitor observe（闭环证据）
    now = time.time()
    trace = []
    idx = 0
    for i, (sk, args, why) in enumerate(steps, 1):
        idx += 1
        ok = not (fail_at and i == fail_at)
        result = {"ok": ok}
        if sk == "observe":
            result["observation"] = {"state_keys": ["height.pos", "left_gripper.pos", "right_gripper.pos"],
                                      "extras": {"platform": "live"}}
        elif sk in CONTROL:
            result["action_sent"] = args
        elif not ok:
            result["reason"] = scn.get("root_cause", "action failed")
        trace.append({"index": idx, "skill": sk, "args": args, "result": result,
                      "ok": ok, "why": why, "role": "worker", "timestamp": now + idx})
        if sk in CONTROL and sk != "stop_motion":
            idx += 1
            trace.append({"index": idx, "skill": "observe",
                          "args": {"include_images": True, "include_state": True},
                          "result": {"ok": True, "observation": {"extras": {"platform": "live"}}},
                          "ok": True, "why": f"observe live state after {sk}",
                          "role": "worker.monitor", "timestamp": now + idx})
    trace_jsonl = "\n".join(json.dumps(t, ensure_ascii=False) for t in trace) + "\n"

    total = len(trace)
    ok_n = sum(1 for t in trace if t["ok"])
    fail_n = total - ok_n

    # review.md
    rv = [f"# Audit: {goal}", "",
          f"**Verdict**: `{verdict}`",
          f"**Root cause**: {scn.get('root_cause') or '(none)'}",
          f"**Next action**: {scn.get('next_action') or '(none)'}",
          "", "## Evidence",
          f"- Used skills: {', '.join(used) or '(none)'}",
          f"- Used control skills: {', '.join(used_ctrl) or '(none)'}",
          "- Simulation leak terms: (none)"]
    if scn.get("research"):
        rv += ["", "## Research Needed"] + [f"- {q}" for q in scn["research"]]
    review_md = "\n".join(rv) + "\n"

    # summary.md
    sm = [f"# Worker Summary: {goal}", "", f"Executed {total} skill call(s).", "", "## Trace"]
    for t in trace:
        sm.append(f"- {t['index']}. `{t['skill']}` role={t['role']} ok={t['ok']} why='{t['why']}'")
    summary_md = "\n".join(sm) + "\n"

    # 四角色 agent.json
    handler = {"role": "handler", "goal": goal, "robot": "box2robot dual-arm",
               "platform": "live", "handoff": ["strategist", "worker", "auditor"]}
    strategist = {"role": "strategist", "selected": used, "plan_steps": len(steps),
                  "notes": "registry-grounded plan for a real robot"}
    worker = {"role": "worker", "executed": total, "ok": ok_n, "fail": fail_n,
              "auto_observe": True}
    auditor = {"role": "auditor", "verdict": verdict, "used_skills": used,
               "research_questions": scn.get("research", [])}

    return {
        "plan.md": plan_md,
        "trace.jsonl": trace_jsonl,
        "review.md": review_md,
        "summary.md": summary_md,
        "handler_agent.json": handler,
        "strategist_agent.json": strategist,
        "worker_agent.json": worker,
        "auditor_agent.json": auditor,
        "loop_events.json": build_loop_events(scn, trace),
    }


def push_skills():
    okc = 0
    for name, cat, desc, args in SKILLS:
        r = api("POST", "/api/loopviz/skill",
                {"name": name, "category": cat, "description": desc, "args": args,
                 "body": f"# {name}\n\n{desc}"})
        okc += 1 if r.get("ok") else 0
    print(f"  已推送技能注册表: {okc}/{len(SKILLS)}")


def main():
    global BASE, TOKEN
    ap = argparse.ArgumentParser(description="LoopViz 演示推送脚本")
    ap.add_argument("--base", default=os.environ.get("LOOPMASTER_BASE", "https://loopmaster.box2ai.com"),
                    help="网站地址，默认线上 https://loopmaster.box2ai.com")
    ap.add_argument("--token", default=os.environ.get("LOOPMASTER_API_TOKEN", DEFAULT_API_TOKEN),
                    help="写接口令牌（或设环境变量 LOOPMASTER_API_TOKEN）")
    ap.add_argument("--interval", type=float, default=9.0,
                    help="每隔几秒推送一次运行，默认 9s（够看完一轮 Agent Loop 回放；配合服务器 LOOPVIZ_TTL≈20）")
    ap.add_argument("--keep", type=int, default=10, help="页面上最多保留多少条最近运行，默认 10")
    ap.add_argument("--once", type=int, default=0, help="只推 N 份就退出（0=持续推送）")
    ap.add_argument("--clean", action="store_true", help="停止时删除本脚本推送过的所有运行")
    ap.add_argument("--all", action="store_true",
                    help="轮播全部演示场景（默认只复现 joint5 那一段自进化循环 log）")
    args = ap.parse_args()

    BASE = args.base.rstrip("/")
    TOKEN = args.token.strip()

    print("=" * 56)
    print("  LoopViz 演示推送 →", BASE)
    print("  Ctrl+C 停止后不再推送。查看效果:", BASE + "/loopviz")
    print("=" * 56)

    # 连通性探测
    ping = api("GET", "/api/loopviz/runs")
    if not ping.get("ok"):
        print("  ✗ 无法连接网站:", ping)
        return
    push_skills()

    # 明星运行：你贴的那段 joint5 自进化循环 log（带显式 loop 事件流）
    JOINT5 = next((s for s in SCENARIOS if s.get("loop")), SCENARIOS[0])

    pushed = []
    n = 0
    try:
        if not args.all:
            # —— 默认：只复现 joint5 这一段 log ——
            # 用固定 run id 反复推送刷新存活时间，让它一直留在页面上循环回放；
            # Ctrl+C 停止后不再刷新，约 LOOPVIZ_TTL 秒后页面自动留白。
            RID = "messi-joint5-loop-demo"
            files = build_files(JOINT5)
            print(f"  只推送这一段 log：{JOINT5['goal']}")
            while True:
                r = api("POST", "/api/loopviz/run", {"id": RID, "files": files})
                n += 1
                print(f"  [{n}] 推送 ✓ 刷新存活" if r.get("ok") else f"  [{n}] 推送 ✗ {r}")
                if args.once and n >= args.once:
                    break
                time.sleep(args.interval)
        else:
            # —— --all：轮播全部场景 ——
            while True:
                scn = SCENARIOS[n % len(SCENARIOS)] if n < len(SCENARIOS) else random.choice(SCENARIOS)
                run_id = make_run_id(scn["goal"])
                r = api("POST", "/api/loopviz/run", {"id": run_id, "files": build_files(scn)})
                n += 1
                if r.get("ok"):
                    pushed.append(run_id)
                    print(f"  [{n}] 推送 ✓ verdict={r.get('run',{}).get('verdict','?'):<16} {scn['goal']}")
                else:
                    print(f"  [{n}] 推送 ✗ {r}")
                while len(pushed) > args.keep:
                    api("DELETE", "/api/loopviz/run/" + pushed.pop(0))
                if args.once and n >= args.once:
                    break
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  已停止推送。")
    finally:
        if args.clean:
            for rid in (pushed or ["messi-joint5-loop-demo"]):
                api("DELETE", "/api/loopviz/run/" + rid)
            print("  已清理本次推送的运行。")
        else:
            print("  页面保留当前运行，停止刷新后约 LOOPVIZ_TTL 秒自动留白（--clean 可立即清）。")


if __name__ == "__main__":
    main()
