# 麦西 Messi · 双臂销售机器人自助售卖系统

移动升降底盘 + 双臂抓取的销售机器人配套下单网站。扫码 → 填 ID → 逛货篮 → 呼叫机械臂下单。
黑橙主题、白色字体。1 月亮币 = 1 元人民币，售价按上海行情。

## 快速启动

```bash
pip install -r requirements.txt      # 或 python -m venv 后再装
python web_page/app.py
python web_page/test/push_demo.py
```

启动后打开：

| 页面 | 地址 | 用途 |
|---|---|---|
| 下单页 | http://127.0.0.1:5000/ | 顾客扫码进入，填 ID 购物 |
| 数据大屏 | http://127.0.0.1:5000/dashboard | 路演实时展示各项指标 |
| 后台管理 | http://127.0.0.1:5000/admin | 新增商品 / 补货 |

局域网 / 手机扫码：用本机 IP 替换 127.0.0.1（服务已监听 0.0.0.0:5000）。
可用 `http://本机IP:5000/` 生成二维码贴到机器人上。

## 记录的数据（全部落库 SQLite `vending.db`）

- **扫码打开网页次数** —— 每次打开下单页 +1（`stats.page_visits`）
- **下单次数、销售总金额** —— `orders` 表聚合
- **机械臂执行/成功/失败次数** —— 每件货逐次抓取，成功率可调
- **每笔订单的下单时间点、用户 ID、IP、明细、金额、状态**
- **用户钱包** —— 新用户注册送 200 月亮币，记录 IP

## 关键可调参数（`app.py` 顶部）

```python
ARM_SIMULATE     = True    # True 自动模拟机械臂；False 等真实机器人上报
ARM_SUCCESS_RATE = 0.90    # 单次抓取成功率
ARM_MAX_RETRY    = 2       # 单件最多尝试次数
NEW_USER_COINS   = 200.0   # 新用户赠币
```

## 硬件联调（真实机器人上报机械臂结果）

把 `ARM_SIMULATE` 设为 `False`，机器人每完成一次抓取动作调用：

```bash
POST /api/arm/report   {"success": true}   # 或 false
```

系统即累计真实的执行/成功/失败次数。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | `{user_id}` 登录，记录 IP，新用户送币 |
| GET  | `/api/products` | 商品列表（含库存） |
| POST | `/api/order` | `{user_id, items:[{id,qty}]}` 下单 |
| GET  | `/api/stats` | 全部运营指标 + 最近订单 |
| POST | `/api/products` | 新增商品 |
| POST | `/api/products/<id>/restock` | `{add}` 补货 |
| POST | `/api/arm/report` | `{success}` 真实机械臂上报 |

## 开放给 agent 框架的写接口

供 loopmaster agent 框架直接改「数据库内容」和「智能体（LoopViz）内容」。
**写接口带令牌保护**：令牌不写在源码里，服务端从环境变量 `LOOPMASTER_API_TOKEN`
或本地文件 `web_page/api_token.txt`（已 gitignore）读取；请求头 `X-API-Token`
（或 `?token=`）必须匹配。**部署时自行设一个随机长串**，例如：
`export LOOPMASTER_API_TOKEN=$(python -c "import secrets;print(secrets.token_hex(16))")`
未配置时写接口无鉴权（仅便于本地开发），线上务必设置。

> **网站 ↔ agent 机器人完整对接协议见 [`PROTOCOL.md`](PROTOCOL.md)**（下单→轮询→认领→执行→反馈）。

### 机器人任务协议（真实机器人模式）

设 `ARM_SIMULATE=0` 后，顾客下单只建 `pending` 任务，交给 agent 轮询执行：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET    | `/api/tasks/pending` | agent 轮询待执行任务 |
| GET    | `/api/tasks` / `/api/tasks/<id>` | 列表/详情（可 `?status=`） |
| POST 🔒| `/api/tasks/<id>/claim` | `{agent_id}` 认领→running |
| POST 🔒| `/api/tasks/<id>/report` | `{status,items,arm,...}` 反馈并结算订单 |
| POST 🔒| `/api/exec_log` | 上传执行/编码信息（时间戳/指令/状态/编码） |
| GET    | `/api/exec_log` | 查执行日志（`?task_id=`） |

### 改数据库内容（表名/列名白名单，防注入）

可操作表：`products` / `users` / `orders` / `stats` / `tasks` / `exec_logs`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET    | `/api/db/<table>` | 读某表全部行 |
| POST   | `/api/db/<table>` | 上表：带主键且已存在→只更新提供的列，否则插入 |
| DELETE | `/api/db/<table>/<key>` | 按主键删一行 |

```bash
# 改商品价格/库存（只动提供的列，name/emoji 保留）
curl -X POST http://IP:5000/api/db/products \
  -H "X-API-Token: 你的令牌" -H "Content-Type: application/json" \
  -d '{"id":1,"price":2.5,"stock":99}'
# 给某用户改余额（不存在则新建）
curl -X POST http://IP:5000/api/db/users -H "X-API-Token: 你的令牌" \
  -d '{"id":"agent_bot","coins":500}'
```

### 改智能体（LoopViz）内容

| 方法 | 路径 | 说明 |
|---|---|---|
| POST   | `/api/loopviz/run` | 推送一次运行 `{id, files:{"plan.md":..,"trace.jsonl":..,"*_agent.json":{..}}}`（dict/list 自动转 JSON，文件名白名单） |
| DELETE | `/api/loopviz/run/<id>` | 删除一次运行目录 |
| POST   | `/api/loopviz/skill` | 注册/更新技能 `{name,category,description,args,body}`，写入 `LOOPMASTER_SKILL_ROOT`（默认 `loopmaster_agentic/skills`） |

- 运行默认写到 `LOOPMASTER_WORKSPACE_ROOT`，未设则写 `../loopmaster/_viz_runs/<id>/`。
- 只要目录里有 `plan.md` 就会被 `/loopviz` 页与 `/api/loopviz/runs` 列出。

## 阿里云部署（Linux）

```bash
cd ~/web_page            # 服务器上的实际路径
pip install -r requirements.txt
export LOOPMASTER_API_TOKEN=换成随机长串   # 设置写接口令牌
python app.py            # 已监听 0.0.0.0:5000
```

- 阿里云安全组放行 **TCP 5000**，用 `http://公网IP:5000/` 访问 / 生成二维码。
- 生产建议用 `gunicorn -w 2 -b 0.0.0.0:5000 app:app` + nginx 反代，并关掉 `app.py` 里的 `debug=True`。
- 可选环境变量：`LOOPMASTER_WORKSPACE_ROOT`（运行产物目录）、`LOOPMASTER_SKILL_ROOT`（技能目录）。

## 数据持久化（重要）

- 所有数据（用户余额、订单、下单时间、机械臂统计、访问次数、商品库存）都写入
  磁盘文件 **`vending.db`（SQLite）**，进程重启、关网页、关机都不会丢失。
- 老用户输入原用户名即登录并**保留原余额**，不重复送币；新名字则自动注册送 200。
- **想永久备份就复制 `vending.db` 这个文件**；只有删除它才会清空一切。

## 备份（重要）

数据库 `vending.db` 有多重备份保护，备份文件都在 `backup/vending_<时间戳>.db`（保留最近 30 份）：

- **启动自动快照**：每次 `python app.py` 启动即自动备份一次。
- **后台一键备份**：`/admin` 页「数据备份」区 → 「📦 立即备份数据库」，或「⬇ 下载 vending.db」离线保存。
- **命令行备份**（可加进定时任务）：
  ```bash
  python scripts/backup_db.py            # 保留最近 30 份
  python scripts/backup_db.py --keep 50
  ```
- 备份用 SQLite 在线 backup API，**服务器运行中也能安全生成一致性快照**。

### 从备份恢复

停掉服务，把某份备份覆盖回去即可：
```bash
cp backup/vending_2026-07-10_122920.db vending.db
```

## 重置数据

删除 `vending.db` 后重启即可恢复初始 12 款商品、清空所有统计（谨慎操作，**先备份**）。
