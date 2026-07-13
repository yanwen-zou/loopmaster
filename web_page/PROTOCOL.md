# 麦西 Messi · 网站 ↔ Agent 机器人 对接协议

本文档定义「下单网站(web_page)」与「loopmaster agent 机器人框架」之间的 HTTP 接口协议。
网站是服务端(数据源 + 结算方)，agent 框架是客户端(轮询任务 + 驱动机械臂 + 回传结果)。

- Base URL：`http://loopmaster.box2ai.com/`（或 `http://IP:5000/`）
- 编码：全部 JSON，UTF-8
- **鉴权**：所有**写接口**需带请求头 `X-API-Token: <令牌>`（或 `?token=`）。
  令牌不写在源码里，由服务端从环境变量 `LOOPMASTER_API_TOKEN` 或本地文件
  `web_page/api_token.txt` 读取，部署时自行设置。读接口不需要。
- 金额单位：月亮币（1 月亮币 = 1 元）
- **语言约定**：网站对顾客展示中文；**与 agent 机器人交互的一切标识/指令一律纯英文小写下划线**
  （`snake_case`）。商品用 `sku`（= `name_en`）标识，任务 `payload`/`instruction` 全英文，
  中文名只出现在网站侧展示，不下发给 agent。

---

## 0. 运行模式

`ARM_SIMULATE` 环境变量控制下单后的行为：

| 值 | 模式 | 下单 `/api/order` 行为 |
|---|---|---|
| `1`（默认） | 演示 | 本地即时模拟机械臂，直接结算返回成功/部分/失败 |
| `0` | 真实机器人 | 只建 `pending` 订单 + 任务，**交给 agent 轮询执行**，report 时才结算 |

真实机器人联调时在服务器设 `ARM_SIMULATE=0` 并重启服务。

---

## 0.1 商品仓库 & 中英映射（agent 只认英文 sku）

网站展示中文名，agent 只用英文 `sku`（即 `products.name_en`，小写下划线）。初始仓库：

| id | 中文名(name) | 英文 sku(name_en) | 分类(category) | 分类英文 | 价格 | 库存 | emoji |
|---|---|---|---|---|---|---|---|
| 1 | 可乐   | `cola`          | 饮料   | `drink`  | 3.0 | 20 | 🥤 |
| 2 | 红牛   | `red_bull`      | 饮料   | `drink`  | 6.0 | 15 | 🐂 |
| 3 | 瓶装水 | `bottled_water` | 饮料   | `drink`  | 2.0 | 30 | 💧 |
| 4 | 火腿肠 | `ham_sausage`   | 零食   | `snack`  | 2.0 | 25 | 🌭 |
| 5 | 香肠   | `sausage`       | 零食   | `snack`  | 3.0 | 20 | 🍖 |
| 6 | 饼干   | `biscuit`       | 零食   | `snack`  | 5.0 | 18 | 🍪 |
| 7 | 蛋糕   | `cake`          | 零食   | `snack`  | 8.0 | 12 | 🍰 |
| 8 | 自定义 | `custom`        | 自定义 | `custom` | 0.0 | 99 | ✨ |

- **`custom`（自定义）**：顾客在下单时填写自由文本需求，随该行 `payload` 项的 `note` 字段一起下发，
  例如 `note="spicy_chips"`。agent 据 `note` 现场决定抓什么/是否可满足。价格 0，report 时按实结算。
- 后台 `POST /api/products` 新增商品时可传 `name_en`；不传则由中文名自动 slug（含中文则回退 `item`），
  建议手动指定英文 sku 以便 agent 稳定识别。
- 分类中英映射：`饮料→drink`、`零食→snack`、`自定义→custom`。

---

## 1. 任务生命周期（核心链路）

```
顾客下单                 agent 轮询            agent 认领           机械臂执行中             agent 反馈
POST /api/order   →   GET /api/tasks/pending → POST .../claim → POST /api/exec_log(可多条) → POST .../report
   (建 pending 任务)      (取待办)              (running)         (上传编码/执行信息)         (结算订单)
```

状态机：`pending → running → done | failed`

---

## 2. 顾客下单（网站前端调用，非 agent）

`POST /api/order`
```json
{ "user_id": "u001", "items": [ {"id": 1, "qty": 2},
                                {"id": 8, "qty": 1, "note": "spicy_chips"} ] }
```
`items[].note` 仅自定义商品(`custom`)需要，是顾客填写的自由文本需求。真实机器人模式返回：
```json
{ "ok": true, "order_id": 12, "task_id": 5, "status": "pending",
  "need_total": 6.0, "coins": 200.0,
  "items": [ {"id":1,"name":"可乐","name_en":"cola","category":"饮料",
              "price":3.0,"emoji":"🥤","qty":2,"delivered":0} ] }
```
> 下单时校验库存与余额，但**不扣款、不扣库存**；等 agent `report` 按实际交付结算。
> 返回的 `items` 是**网站侧展示快照**（含中文 `name`）；下发给 agent 的是英文 `payload`（见 §3）。

---

## 3. Agent 轮询待执行任务

`GET /api/tasks/pending` — 列出所有 `pending` 任务（读接口，无需令牌）
```json
{ "ok": true, "tasks": [
  { "id": 5, "order_id": 12, "user_id": "u001",
    "instruction": "pick cola x2 deliver_to_customer; pick custom x1 deliver_to_customer note=spicy_chips",
    "payload": "[{\"id\":1,\"sku\":\"cola\",\"name\":\"cola\",\"category\":\"drink\",\"price\":3.0,\"qty\":2},{\"id\":8,\"sku\":\"custom\",\"name\":\"custom\",\"category\":\"custom\",\"price\":0.0,\"qty\":1,\"note\":\"spicy_chips\"}]",
    "status": "pending", "created_at": "2026-07-10 18:00:00" } ] }
```
其它查询：
- `GET /api/tasks?status=running&limit=20` — 按状态过滤
- `GET /api/tasks/<id>` — 单个任务详情

**`payload` 与 `instruction` 全部纯英文小写下划线**（agent 直接消费，无需翻译）。
`payload` 是 JSON 字符串，解析后每项字段：
`id`(商品 id，report 用它回传交付数)、`sku`/`name`(英文标识 = name_en)、
`category`(英文分类)、`price`、`qty`、`note?`(仅 `custom` 有，自由文本需求)。

---

## 4. Agent 认领任务

`POST /api/tasks/<id>/claim` 🔒
```json
{ "agent_id": "robot_01" }
```
返回认领后的任务（`status` 变 `running`，记录 `agent_id`、`claimed_at`）。
仅 `pending`/`running` 可认领，其它状态返回 409。

---

## 5. Agent 上传执行/编码信息（可多次）

`POST /api/exec_log` 🔒 — 记录时间戳、任务指令、状态、编码等销售售卖执行信息
```json
{ "task_id": 5, "order_id": 12, "agent_id": "robot_01",
  "ts": "2026-07-10 18:00:03",          // 缺省用服务器时间
  "instruction": "move_arm pick cola",   // 任务指令
  "status": "running",                   // 状态
  "code": "J1=30,J2=-45,GRIP=0.5",       // 编码信息(动作/机械臂指令编码)
  "detail": { "joints": [30,-45,0,0,0.5] } }  // 附加(对象自动转 JSON)
```
返回 `{ "ok": true, "id": 42 }`。执行过程中可多次调用（每个动作一条）。

查询：`GET /api/exec_log?task_id=5&limit=100` —（读接口）按 task_id/order_id 过滤。

---

## 6. Agent 反馈结果并结算

`POST /api/tasks/<id>/report` 🔒
```json
{ "agent_id": "robot_01",
  "status": "partial",                       // done/success | partial | failed（可选）
  "items": [ {"id":1,"delivered":2}, {"id":8,"delivered":0} ],  // 每件实际交付数
  "arm": { "exec": 4, "success": 2, "fail": 2 },                 // 机械臂计数(累计到大屏)
  "code": "DONE", "result": { "note": "biscuit_jammed" } }        // result 原样存 tasks.result
```
服务端据此**一次性结算**：按 `delivered` 扣库存、扣款（月亮币）、累计机械臂统计、定订单状态。
交付数量优先取 `items`；若不给 `items` 则 `status=done/success`→全交付、其余→全 0。

返回：
```json
{ "ok": true, "task_id": 5, "order_id": 12,
  "order_status": "partial", "task_status": "done",
  "paid": 6.0, "coins": 194.0,
  "items": [ {"id":1,...,"delivered":2}, {"id":8,...,"delivered":0} ] }
```
已结算(`done`/`failed`)的任务重复 report 返回 409。

---

## 7. Agent 上传自身信息与技能（LoopViz）

供 agent 框架把运行记录、技能注册表推给网站可视化。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST 🔒 | `/api/loopviz/run` | 推送一次运行 `{id, files:{"plan.md":..,"trace.jsonl":..,"loop_events.json":[..],"*_agent.json":{..}}}`（dict/list 自动转 JSON） |
| DELETE 🔒 | `/api/loopviz/run/<id>` | 删除一次运行 |
| POST 🔒 | `/api/loopviz/skill` | 注册/更新技能 `{name, category, description, args:{..}, body}`，写入 `LOOPMASTER_SKILL_ROOT`（默认 `loopmaster_agentic/skills`） |
| GET | `/api/loopviz/skills` | 读技能注册表 + 四角色定义 |
| GET | `/api/loopviz/runs` | 读运行列表 |
| GET | `/api/loopviz/run/<id>` | 读单次运行详情 |

页面 `http://域名/loopviz` 实时展示这些内容。

### 7.1 loop_events.json —— 四角色循环事件流（概述里动态回放）

在 `/api/loopviz/run` 的 `files` 里附带 `loop_events.json`（一个事件数组），
LoopViz 概述顶部会**逐条动画回放** Handler→Strategist→Worker→Auditor 的自进化循环
（含 worker 预检拦截、策略修订、审计出招、重跑）。每个事件：

```json
{ "role": "handler|strategist|worker|worker.monitor|auditor",
  "type": "route|connect|plan|revise|skill|gate_block|verdict|skill_update|final",
  "iter": 1,                       // 第几轮循环
  "title": "一句话说明",            // route/connect/verdict/final 用
  "steps": ["observe","move_arm_joints ×5","stop_motion"],  // plan/revise 用
  "notes": ["安全隐患1","安全隐患2"],  // gate_block 用（worker preflight proceed=false 的理由）
  "skill": "observe", "ok": true, "error": "…",             // skill 用
  "verdict": "retry", "next": "下一步建议" }                 // verdict/final 用
```

各 `type` 的语义：`route/connect`=Handler 分发/连接；`plan/revise`=Strategist 规划/修订（带 `steps`）；
`skill`=Worker 执行一个技能（`ok`/`error`）；`gate_block`=Worker 预检拦截 proceed=false（带 `notes`）；
`verdict`=Auditor 判定（`verdict`+`title` 根因）；`skill_update`=Auditor 提出并应用可学习技能；
`final`=Handler 收束（`verdict`+`next`）。

---

## 8. 通用改库接口（补充）

`products / users / orders / stats / tasks / exec_logs` 六张表可直接读写（表名/列名白名单）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/db/<table>` | 读全表 |
| POST 🔒 | `/api/db/<table>` | 带主键且存在→只更新提供的列，否则插入 |
| DELETE 🔒 | `/api/db/<table>/<key>` | 按主键删一行 |

---

## 9. 数据表速查

- **tasks**：`id, order_id, user_id, instruction, payload(JSON), status, agent_id, result(JSON), created_at, claimed_at, finished_at`
- **exec_logs**：`id, task_id, order_id, agent_id, ts, instruction, status, code, detail(JSON)`
- **orders**：`id, user_id, ip, items(JSON), total, arm_exec, arm_success, arm_fail, status, created_at`
  （真实模式下单时 `status=pending`，report 后变 `success/partial/failed`）

---

## 10. 最小对接示例（Python）

```python
import requests
BASE = "http://loopmaster.box2ai.com"
H = {"X-API-Token": "你的令牌"}

# 1) 轮询
tasks = requests.get(f"{BASE}/api/tasks/pending").json()["tasks"]
for t in tasks:
    tid = t["id"]
    # 2) 认领
    requests.post(f"{BASE}/api/tasks/{tid}/claim", json={"agent_id": "robot_01"}, headers=H)
    items = __import__("json").loads(t["payload"])   # 纯英文：每项含 id/sku/qty/note?
    delivered = []
    for it in items:
        # 3) 执行 + 上传编码信息（sku 是英文标识；custom 项读 it.get("note") 决定抓什么）
        requests.post(f"{BASE}/api/exec_log", headers=H, json={
            "task_id": tid, "agent_id": "robot_01",
            "instruction": f"pick {it['sku']} x{it['qty']}"
                           + (f" note={it['note']}" if it.get("note") else ""),
            "status": "running", "code": "J1=..,GRIP=.."})
        delivered.append({"id": it["id"], "delivered": it["qty"]})  # 假设全成功
    # 4) 反馈结算
    requests.post(f"{BASE}/api/tasks/{tid}/report", headers=H, json={
        "agent_id": "robot_01", "status": "done",
        "items": delivered, "arm": {"exec": len(items), "success": len(items), "fail": 0}})
```

---

> 已知限制：下单到 report 之间不预扣款/库存，理论上同一用户可并发下多单造成超卖/透支，
> 演示够用；如需严格可在下单时冻结额度、report 时核销（后续迭代）。
