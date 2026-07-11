# -*- coding: utf-8 -*-
"""
麦西 Messi · 双臂销售机器人 自助售卖下单后端
Flask + SQLite，零额外依赖之外只需 flask。

功能：
- 扫码打开网页计数 / 下单次数 / 销售总金额
- 机械臂执行次数、成功次数、失败次数
- 每次下单时间点、明细
- 用户输入 ID 登录，记录 IP，新用户赠送 200 月亮币（1 月亮币 = 1 元）
- 商品库存、支持后台新增商品 / 补货
"""
import os
import re
import ast
import glob
import json
import time
import random
import sqlite3
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, render_template, g, send_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vending.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backup")

# LoopViz 数据源（只读扫描 loopmaster 产物，不修改其代码）
LOOP_REPO = Path(BASE_DIR).parent
# LoopViz 运行存活秒数：>0 时，run 超过该秒数自动从列表消失（演示用，脚本停推后页面自动留白）；
# 0=永不过期（真实 agent 框架用）。环境变量 LOOPVIZ_TTL 控制。
LOOPVIZ_TTL = float(os.environ.get("LOOPVIZ_TTL", "0") or 0)
LOOP_SKILL_ROOT = LOOP_REPO / "loopmaster_agentic" / "skills"

# 机械臂模拟参数
# ARM_SIMULATE：True=下单本地即时模拟机械臂（演示）；False=真实机器人模式，下单只建待执行
# 任务，交给 agent 轮询执行并 report 结算。可用环境变量 ARM_SIMULATE=0 切到真实机器人。
ARM_SIMULATE = os.environ.get("ARM_SIMULATE", "1").strip().lower() not in ("0", "false", "no")
ARM_SUCCESS_RATE = 0.90      # 单次抓取成功率
ARM_MAX_RETRY = 2            # 单个货品最多尝试次数
NEW_USER_COINS = 200.0       # 新用户赠送月亮币

# 开放给 agent 框架的写接口令牌：环境变量 LOOPMASTER_API_TOKEN 可覆盖默认令牌。
DEFAULT_API_TOKEN = "06de644db26bf26dc5fbef2657b5af6b"
API_TOKEN = os.environ.get("LOOPMASTER_API_TOKEN", DEFAULT_API_TOKEN).strip()

app = Flask(__name__)
# 静态文件（CSS/JS）不长期缓存：改了样式浏览器会重新拉取，避免旧样式卡住布局
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# ----------------------------- 商品仓库 -----------------------------
# 网站对顾客展示中文名(name)，与 agent 机器人交互只用英文小写下划线(name_en / sku)。
# 中英映射就存在 products.name_en 这一列，下单时据此把发给 agent 的字段全部转成英文。
# 字段顺序：(中文名, 英文名 name_en, 分类, 售价, 库存, emoji)
# 字段顺序：(中文名, 英文名 name_en, 分类, 售价, 库存, emoji, 图片文件名@static/assets/)
SEED_PRODUCTS = [
    ("可口可乐",      "cola",           "饮料",   3.0, 20, "🥤", "cola.jpg"),
    ("易拉罐装红牛",  "red_bull",       "饮料",   6.0, 15, "🐂", "red_bull.jpg"),
    ("农夫山泉矿泉水", "bottled_water",  "饮料",   2.0, 30, "💧", "bottled_water.jpg"),
    ("纯牛奶",        "milk",           "饮料",   5.0, 15, "🥛", "milk.jpg"),
    ("双汇火腿肠",    "ham_sausage",    "零食",   2.0, 25, "🌭", "ham_sausage.jpg"),
    ("干脆面",        "cracker_noodle", "零食",   3.0, 20, "🍜", "cracker_noodle.jpg"),
    ("旺仔小馒头",    "wangzai_bun",    "零食",   4.0, 18, "🍘", "wangzai_bun.jpg"),
    ("绿豆饼",        "mung_bean_cake", "零食",   3.0, 18, "🥮", "mung_bean_cake.jpg"),
    ("芝士夹心饼干",  "cheese_biscuit", "零食",   5.0, 18, "🧀", "cheese_biscuit.jpg"),
    ("巧克力棒",      "chocolate_bar",  "零食",   6.0, 15, "🍫", "chocolate_bar.jpg"),
    ("自定义",        "custom",         "自定义", 0.0, 99, "✨", ""),
]
# 分类中英映射（发给 agent / 存英文用）
CATEGORY_EN = {"饮料": "drink", "零食": "snack", "自定义": "custom"}


def slug_en(name, fallback="item"):
    """把商品名转成 agent 用的英文小写下划线标识：纯 ASCII 直接 slug，含中文则回退。"""
    s = (name or "").strip().lower()
    if s and re.fullmatch(r"[\x00-\x7f]+", s):
        s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
        return s or fallback
    return fallback


def check_token():
    """写接口令牌校验：未配置 API_TOKEN 时放行。"""
    if not API_TOKEN:
        return True
    tok = request.headers.get("X-API-Token", "") or request.args.get("token", "")
    return tok == API_TOKEN


# ----------------------------- 数据库 -----------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_en TEXT NOT NULL DEFAULT '',   -- agent 用的英文小写下划线标识(sku)
            category TEXT NOT NULL DEFAULT '零食',
            price REAL NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0,
            emoji TEXT DEFAULT '📦',
            image TEXT NOT NULL DEFAULT '',      -- 商品图（static/assets/ 下的文件名，空则用 emoji）
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            ip TEXT,
            coins REAL NOT NULL DEFAULT 0,
            visits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            ip TEXT,
            items TEXT NOT NULL,          -- JSON: [{id,name,price,qty,delivered}]
            total REAL NOT NULL,          -- 实际成功交付金额
            arm_exec INTEGER DEFAULT 0,
            arm_success INTEGER DEFAULT 0,
            arm_fail INTEGER DEFAULT 0,
            status TEXT NOT NULL,         -- success / partial / failed
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stats (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL DEFAULT 0
        );

        -- 机器人任务队列：下单产生 task，agent 轮询→认领→执行→反馈
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            user_id TEXT,
            instruction TEXT,             -- 人类可读任务指令
            payload TEXT,                 -- JSON(纯英文): [{id,sku,name,category,price,qty,note?}] 下发给 agent
            status TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed
            agent_id TEXT,                -- 认领的 agent
            result TEXT,                  -- JSON: agent 反馈原文
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            finished_at TEXT
        );

        -- 执行/编码信息日志：时间戳、任务指令、状态、编码等销售售卖执行信息
        CREATE TABLE IF NOT EXISTS exec_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            order_id INTEGER,
            agent_id TEXT,
            ts TEXT NOT NULL,             -- 时间戳(客户端给或服务器补)
            instruction TEXT,             -- 任务指令
            status TEXT,                  -- 状态
            code TEXT,                    -- 编码信息(动作/机械臂指令编码)
            detail TEXT                   -- JSON 附加信息
        );
        """
    )
    # 初始化统计计数器
    for k in ("page_visits", "arm_exec", "arm_success", "arm_fail", "pickup_flag"):
        db.execute("INSERT OR IGNORE INTO stats(key, value) VALUES(?, 0)", (k,))

    # 老库迁移：给已存在的 products 表补 name_en / image 列（CREATE TABLE IF NOT EXISTS 不改旧表）
    cols = [r[1] for r in db.execute("PRAGMA table_info(products)").fetchall()]
    if "name_en" not in cols:
        db.execute("ALTER TABLE products ADD COLUMN name_en TEXT NOT NULL DEFAULT ''")
    if "image" not in cols:
        db.execute("ALTER TABLE products ADD COLUMN image TEXT NOT NULL DEFAULT ''")

    # 初始/重置商品仓库：新仓库的 SKU 不全在库、或已入库商品还没图，就清空重灌为 SEED_PRODUCTS
    rows = db.execute("SELECT name_en, image FROM products").fetchall()
    have = {r[0] for r in rows}
    want = {en for (_n, en, _c, _p, _s, _e, _img) in SEED_PRODUCTS}
    need_img = any((not r[1]) for r in rows if r[0] and r[0] != "custom")
    if (not want.issubset(have)) or need_img:
        db.execute("DELETE FROM products")
        db.execute("DELETE FROM sqlite_sequence WHERE name='products'")
        ts = now_str()
        db.executemany(
            "INSERT INTO products(name,name_en,category,price,stock,emoji,image,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(n, en, c, p, s, e, img, ts) for (n, en, c, p, s, e, img) in SEED_PRODUCTS],
        )
    db.commit()
    db.close()


def snapshot_db(keep=30):
    """用 SQLite 在线 backup API 生成一致性快照，保留最近 keep 份。"""
    if not os.path.exists(DB_PATH):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"vending_{ts}.db")
    src = sqlite3.connect(DB_PATH)
    bak = sqlite3.connect(dst)
    try:
        with bak:
            src.backup(bak)
    finally:
        bak.close()
        src.close()
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "vending_*.db")))
    while len(files) > keep:
        try:
            os.remove(files.pop(0))
        except OSError:
            break
    return dst


def bump_stat(db, key, delta=1):
    db.execute("UPDATE stats SET value = value + ? WHERE key = ?", (delta, key))


def set_stat(db, key, value):
    db.execute("INSERT OR REPLACE INTO stats(key, value) VALUES(?, ?)", (key, value))


PICKUP_TIMEOUT = 120   # 下单后最长等待取走秒数，超时自动结算并收回


def stat_val(db, key):
    row = db.execute("SELECT value FROM stats WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else 0


# ----------------------------- 页面路由 -----------------------------
@app.route("/")
def index():
    # 扫码打开主页 = 一次访问
    db = get_db()
    bump_stat(db, "page_visits", 1)
    db.commit()
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/admin")
def admin():
    # 后台已与数据大屏合并为同一页面
    return render_template("dashboard.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/research")
def research():
    return render_template("research.html")


# ----------------------------- API -----------------------------
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    uid = (data.get("user_id") or "").strip()
    if not uid:
        return jsonify(ok=False, msg="请输入用户 ID"), 400
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    ip = ip.split(",")[0].strip()

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    is_new = user is None
    if is_new:
        db.execute(
            "INSERT INTO users(id, ip, coins, visits, created_at, last_seen) VALUES(?,?,?,?,?,?)",
            (uid, ip, NEW_USER_COINS, 1, now_str(), now_str()),
        )
        coins = NEW_USER_COINS
    else:
        db.execute(
            "UPDATE users SET ip=?, visits=visits+1, last_seen=? WHERE id=?",
            (ip, now_str(), uid),
        )
        coins = user["coins"]
    db.commit()
    return jsonify(
        ok=True, user_id=uid, ip=ip, coins=coins, is_new=is_new,
        gift=NEW_USER_COINS if is_new else 0,
    )


@app.route("/api/products")
def api_products():
    db = get_db()
    rows = db.execute("SELECT * FROM products ORDER BY category DESC, id").fetchall()
    return jsonify(ok=True, products=[dict(r) for r in rows])


@app.route("/api/order", methods=["POST"])
def api_order():
    data = request.get_json(force=True, silent=True) or {}
    uid = (data.get("user_id") or "").strip()
    cart = data.get("items") or []
    if not uid:
        return jsonify(ok=False, msg="请先登录"), 400
    if not cart:
        return jsonify(ok=False, msg="购物篮为空"), 400

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not user:
        return jsonify(ok=False, msg="用户不存在，请重新登录"), 400

    # 校验库存 + 计算应付
    line_items = []
    need_total = 0.0
    for it in cart:
        pid = it.get("id")
        qty = int(it.get("qty") or 0)
        if qty <= 0:
            continue
        p = db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        if not p:
            return jsonify(ok=False, msg=f"商品 {pid} 不存在"), 400
        if p["stock"] < qty:
            return jsonify(ok=False, msg=f"「{p['name']}」库存不足（剩 {p['stock']}）"), 400
        name_en = p["name_en"] or slug_en(p["name"])
        li = {"id": p["id"], "name": p["name"], "name_en": name_en,
              "category": p["category"], "price": p["price"],
              "emoji": p["emoji"], "qty": qty}
        note = (it.get("note") or "").strip()   # 自定义商品：顾客填写的需求描述
        if note:
            li["note"] = note
        line_items.append(li)
        need_total += p["price"] * qty

    if not line_items:
        return jsonify(ok=False, msg="购物篮为空"), 400
    if user["coins"] < need_total:
        return jsonify(ok=False, msg=f"月亮币不足，需 {need_total:.0f}，余 {user['coins']:.0f}"), 400

    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    ip = ip.split(",")[0].strip()

    # ---- 真实机器人模式：建 pending 订单 + task，交给 agent 轮询执行，report 时结算 ----
    if not ARM_SIMULATE:
        # 现场只有一台机器人：已有未完成任务(待执行/执行中)时，不允许新用户下单
        busy = db.execute(
            "SELECT id FROM tasks WHERE status IN ('pending','running') ORDER BY id LIMIT 1"
        ).fetchone()
        if busy:
            return jsonify(ok=False, busy=True,
                           msg="机器人正在为其他顾客服务，请稍候再下单 🤖"), 409
        for li in line_items:
            li["delivered"] = 0
        # 网站侧订单快照：保留中文名 + emoji 供大屏/后台展示
        order_items_json = json.dumps(line_items, ensure_ascii=False)
        # 发给 agent 的任务载荷：纯英文小写下划线字段，不含任何中文
        agent_items = []
        for li in line_items:
            ai = {"id": li["id"], "sku": li["name_en"], "name": li["name_en"],
                  "category": CATEGORY_EN.get(li.get("category", ""), "snack"),
                  "price": li["price"], "qty": li["qty"]}
            if li.get("note"):
                ai["note"] = li["note"]   # 自定义需求(自由文本)
            agent_items.append(ai)
        payload_json = json.dumps(agent_items, ensure_ascii=False)
        instruction = "; ".join(
            f"pick {ai['sku']} x{ai['qty']} deliver_to_customer"
            + (f" note={ai['note']}" if ai.get("note") else "")
            for ai in agent_items)
        db.execute(
            """INSERT INTO orders(user_id, ip, items, total, arm_exec, arm_success,
               arm_fail, status, created_at) VALUES(?,?,?,?,0,0,0,?,?)""",
            (uid, ip, order_items_json, 0.0, "pending", now_str()),
        )
        order_id = db.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        db.execute(
            """INSERT INTO tasks(order_id, user_id, instruction, payload, status, created_at)
               VALUES(?,?,?,?,?,?)""",
            (order_id, uid, instruction, payload_json, "pending", now_str()),
        )
        task_id = db.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        set_stat(db, "pickup_flag", 1)   # 标签置 1：机器人前伸交付，等顾客取走
        db.commit()
        return jsonify(
            ok=True, order_id=order_id, task_id=task_id, status="pending",
            need_total=need_total, coins=user["coins"], items=line_items,
            pickup_timeout=PICKUP_TIMEOUT,
            msg="机械臂正在为你取货，取走后请点「确定取走」",
        )

    # ---- 模拟模式：本地即时模拟机械臂并结算（无真实机器人时演示用） ----
    arm_exec = arm_ok = arm_bad = 0
    delivered_total = 0.0
    for li in line_items:
        li["delivered"] = 0
        for _ in range(li["qty"]):
            success = False
            for _try in range(ARM_MAX_RETRY):
                arm_exec += 1
                if random.random() < ARM_SUCCESS_RATE:
                    success = True
                    arm_ok += 1
                    break
                else:
                    arm_bad += 1
            if success:
                li["delivered"] += 1
                delivered_total += li["price"]

    # 扣库存（按实际交付数量）+ 扣款
    for li in line_items:
        if li["delivered"] > 0:
            db.execute("UPDATE products SET stock = stock - ? WHERE id = ?",
                       (li["delivered"], li["id"]))
    db.execute("UPDATE users SET coins = coins - ? WHERE id = ?", (delivered_total, uid))

    all_delivered = all(li["delivered"] == li["qty"] for li in line_items)
    any_delivered = any(li["delivered"] > 0 for li in line_items)
    status = "success" if all_delivered else ("partial" if any_delivered else "failed")

    db.execute(
        """INSERT INTO orders(user_id, ip, items, total, arm_exec, arm_success,
           arm_fail, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (uid, ip, json.dumps(line_items, ensure_ascii=False), delivered_total,
         arm_exec, arm_ok, arm_bad, status, now_str()),
    )
    bump_stat(db, "arm_exec", arm_exec)
    bump_stat(db, "arm_success", arm_ok)
    bump_stat(db, "arm_fail", arm_bad)
    db.commit()

    new_coins = db.execute("SELECT coins FROM users WHERE id = ?", (uid,)).fetchone()["coins"]
    order_id = db.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    return jsonify(
        ok=True, order_id=order_id, status=status, paid=delivered_total,
        coins=new_coins, items=line_items,
        arm={"exec": arm_exec, "success": arm_ok, "fail": arm_bad},
    )


@app.route("/api/stats")
def api_stats():
    db = get_db()
    orders = db.execute("SELECT COUNT(*) c, COALESCE(SUM(total),0) t FROM orders").fetchone()
    recent = db.execute(
        "SELECT id, user_id, ip, total, status, arm_exec, arm_success, arm_fail, items, created_at "
        "FROM orders ORDER BY id DESC LIMIT 15"
    ).fetchall()
    users_c = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    # 展示基数：order/user 是表行数，不能直接改；用 stats 里的 *_base 抬高展示值（真实数据仍在其上累加）
    return jsonify(
        ok=True,
        page_visits=int(stat_val(db, "page_visits")),
        order_count=orders["c"] + int(stat_val(db, "orders_base")),
        total_sales=round(orders["t"] + stat_val(db, "sales_base"), 2),
        arm_exec=int(stat_val(db, "arm_exec")),
        arm_success=int(stat_val(db, "arm_success")),
        arm_fail=int(stat_val(db, "arm_fail")),
        user_count=users_c + int(stat_val(db, "users_base")),
        recent_orders=[dict(r) for r in recent],
    )


# ------------------ 后台：新增商品 / 补货 / 上报机械臂 ------------------
@app.route("/api/products", methods=["POST"])
def api_add_product():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, msg="商品名必填"), 400
    try:
        price = float(data.get("price"))
        stock = int(data.get("stock"))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="价格/库存格式错误"), 400
    category = (data.get("category") or "零食").strip()
    emoji = (data.get("emoji") or "📦").strip()
    # agent 用的英文标识：后台没填就按商品名自动 slug（含中文则回退 item）
    name_en = (data.get("name_en") or "").strip().lower() or slug_en(name)

    db = get_db()
    db.execute(
        "INSERT INTO products(name,name_en,category,price,stock,emoji,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (name, name_en, category, price, stock, emoji, now_str()),
    )
    db.commit()
    pid = db.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    return jsonify(ok=True, id=pid)


@app.route("/api/products/<int:pid>/restock", methods=["POST"])
def api_restock(pid):
    data = request.get_json(force=True, silent=True) or {}
    try:
        add = int(data.get("add"))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="补货数量错误"), 400
    db = get_db()
    p = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        return jsonify(ok=False, msg="商品不存在"), 404
    db.execute("UPDATE products SET stock = stock + ? WHERE id=?", (add, pid))
    db.commit()
    return jsonify(ok=True, stock=p["stock"] + add)


@app.route("/api/backup", methods=["POST"])
def api_backup():
    """后台手动触发一次数据库备份。"""
    dst = snapshot_db()
    if not dst:
        return jsonify(ok=False, msg="数据库尚不存在"), 404
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "vending_*.db")), reverse=True)
    return jsonify(ok=True, file=os.path.basename(dst), total=len(files))


@app.route("/api/backups")
def api_backups():
    """列出已有备份。"""
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "vending_*.db")), reverse=True)
    out = [{"name": os.path.basename(f), "size": os.path.getsize(f),
            "mtime": datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M:%S")}
           for f in files]
    return jsonify(ok=True, backups=out)


@app.route("/api/download-db")
def api_download_db():
    """下载当前数据库文件（可离线保存/迁移）。"""
    if not os.path.exists(DB_PATH):
        return jsonify(ok=False, msg="数据库尚不存在"), 404
    return send_file(DB_PATH, as_attachment=True, download_name="vending.db")


@app.route("/api/arm/report", methods=["POST"])
def api_arm_report():
    """真实机器人上报一次机械臂动作结果（可选，供硬件联调）。"""
    data = request.get_json(force=True, silent=True) or {}
    success = bool(data.get("success"))
    db = get_db()
    bump_stat(db, "arm_exec", 1)
    bump_stat(db, "arm_success", 1 if success else 0)
    bump_stat(db, "arm_fail", 0 if success else 1)
    db.commit()
    return jsonify(ok=True)


# ============================================================================
# 通用数据库读写接口 —— 开放给 agent 框架直接改库（表名/列名白名单，防注入）
# ============================================================================
DB_TABLES = {
    "products": {"pk": "id",  "auto": True,
                 "cols": {"name", "name_en", "category", "price", "stock", "emoji", "image", "created_at"}},
    "users":    {"pk": "id",  "auto": False,
                 "cols": {"id", "ip", "coins", "visits", "created_at", "last_seen"}},
    "orders":   {"pk": "id",  "auto": True,
                 "cols": {"user_id", "ip", "items", "total", "arm_exec", "arm_success",
                          "arm_fail", "status", "created_at"}},
    "stats":    {"pk": "key", "auto": False, "cols": {"key", "value"}},
    "tasks":    {"pk": "id",  "auto": True,
                 "cols": {"order_id", "user_id", "instruction", "payload", "status",
                          "agent_id", "result", "created_at", "claimed_at", "finished_at"}},
    "exec_logs": {"pk": "id", "auto": True,
                  "cols": {"task_id", "order_id", "agent_id", "ts", "instruction",
                           "status", "code", "detail"}},
}


@app.route("/api/db/<table>", methods=["GET"])
def api_db_list(table):
    """读取某张表的全部行。"""
    spec = DB_TABLES.get(table)
    if not spec:
        return jsonify(ok=False, msg="未知数据表"), 404
    db = get_db()
    rows = db.execute(f"SELECT * FROM {table}").fetchall()
    return jsonify(ok=True, table=table, rows=[dict(r) for r in rows])


@app.route("/api/db/<table>", methods=["POST"])
def api_db_upsert(table):
    """新增或更新一行：带主键且已存在 → 更新提供的列；否则插入。"""
    if not check_token():
        return jsonify(ok=False, msg="未授权（需 X-API-Token）"), 401
    spec = DB_TABLES.get(table)
    if not spec:
        return jsonify(ok=False, msg="未知数据表"), 404
    data = request.get_json(force=True, silent=True) or {}
    fields = {k: v for k, v in data.items() if k in spec["cols"]}
    if not fields:
        return jsonify(ok=False, msg="没有合法列"), 400

    pk = spec["pk"]
    db = get_db()
    pk_val = data.get(pk)
    exists = pk_val is not None and db.execute(
        f"SELECT 1 FROM {table} WHERE {pk}=?", (pk_val,)).fetchone() is not None

    if exists:
        setcols = {k: v for k, v in fields.items() if k != pk}
        if setcols:
            assigns = ", ".join(f"{k}=?" for k in setcols)
            db.execute(f"UPDATE {table} SET {assigns} WHERE {pk}=?",
                       (*setcols.values(), pk_val))
        row_key = pk_val
    else:
        if "created_at" in spec["cols"] and "created_at" not in fields:
            fields["created_at"] = now_str()
        cols = list(fields.keys())
        ph = ",".join("?" for _ in cols)
        db.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES({ph})",
                   tuple(fields[c] for c in cols))
        row_key = (pk_val if pk_val is not None
                   else db.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    db.commit()
    row = db.execute(f"SELECT * FROM {table} WHERE {pk}=?", (row_key,)).fetchone()
    return jsonify(ok=True, table=table, row=dict(row) if row else None)


@app.route("/api/db/<table>/<key>", methods=["DELETE"])
def api_db_delete(table, key):
    """按主键删除一行。"""
    if not check_token():
        return jsonify(ok=False, msg="未授权（需 X-API-Token）"), 401
    spec = DB_TABLES.get(table)
    if not spec:
        return jsonify(ok=False, msg="未知数据表"), 404
    db = get_db()
    cur = db.execute(f"DELETE FROM {table} WHERE {spec['pk']}=?", (key,))
    db.commit()
    return jsonify(ok=True, deleted=cur.rowcount)


# ============================================================================
# 机器人任务协议 —— 下单产生任务，agent 轮询→认领→执行→反馈；并上传执行/编码信息
# 完整说明见同目录 PROTOCOL.md
# ============================================================================
def _settle_order(db, order, line_items, delivered_map, arm):
    """按 agent 反馈的实际交付数量结算：扣库存、扣款、累计机械臂统计、定订单状态。"""
    delivered_total = 0.0
    for li in line_items:
        d = delivered_map.get(str(li["id"]), delivered_map.get(li["id"], 0))
        d = max(0, min(int(d), li["qty"]))
        li["delivered"] = d
        if d > 0:
            db.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (d, li["id"]))
            delivered_total += li["price"] * d
    db.execute("UPDATE users SET coins = coins - ? WHERE id = ?",
               (delivered_total, order["user_id"]))
    all_d = all(li["delivered"] == li["qty"] for li in line_items)
    any_d = any(li["delivered"] > 0 for li in line_items)
    status = "success" if all_d else ("partial" if any_d else "failed")
    a_exec, a_ok, a_bad = int(arm.get("exec", 0)), int(arm.get("success", 0)), int(arm.get("fail", 0))
    db.execute(
        "UPDATE orders SET items=?, total=?, arm_exec=?, arm_success=?, arm_fail=?, status=? WHERE id=?",
        (json.dumps(line_items, ensure_ascii=False), delivered_total,
         a_exec, a_ok, a_bad, status, order["id"]),
    )
    bump_stat(db, "arm_exec", a_exec)
    bump_stat(db, "arm_success", a_ok)
    bump_stat(db, "arm_fail", a_bad)
    return status, delivered_total


@app.route("/api/tasks/pending")
def api_tasks_pending():
    """agent 轮询：列出待执行(pending)任务。"""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM tasks WHERE status='pending' ORDER BY id LIMIT 50").fetchall()
    return jsonify(ok=True, tasks=[dict(r) for r in rows])


@app.route("/api/tasks")
def api_tasks_list():
    """列出任务，可 ?status= & ?limit= 过滤。"""
    db = get_db()
    status = request.args.get("status")
    limit = int(request.args.get("limit") or 50)
    if status:
        rows = db.execute("SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?",
                          (status, limit)).fetchall()
    else:
        rows = db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify(ok=True, tasks=[dict(r) for r in rows])


@app.route("/api/tasks/<int:tid>")
def api_task_get(tid):
    db = get_db()
    r = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        return jsonify(ok=False, msg="任务不存在"), 404
    return jsonify(ok=True, task=dict(r))


@app.route("/api/tasks/<int:tid>/claim", methods=["POST"])
def api_task_claim(tid):
    """agent 认领任务：pending → running。"""
    if not check_token():
        return jsonify(ok=False, msg="未授权（需 X-API-Token）"), 401
    data = request.get_json(force=True, silent=True) or {}
    agent_id = (data.get("agent_id") or "agent").strip()
    db = get_db()
    r = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        return jsonify(ok=False, msg="任务不存在"), 404
    if r["status"] not in ("pending", "running"):
        return jsonify(ok=False, msg=f"任务状态为 {r['status']}，不可认领"), 409
    db.execute("UPDATE tasks SET status='running', agent_id=?, claimed_at=? WHERE id=?",
               (agent_id, now_str(), tid))
    db.commit()
    return jsonify(ok=True, task=dict(db.execute(
        "SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()))


@app.route("/api/tasks/<int:tid>/report", methods=["POST"])
def api_task_report(tid):
    """agent 反馈执行结果并结算订单。
    body: {agent_id, status?, items:[{id,delivered}], arm:{exec,success,fail}, code?, result?}"""
    if not check_token():
        return jsonify(ok=False, msg="未授权（需 X-API-Token）"), 401
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not task:
        return jsonify(ok=False, msg="任务不存在"), 404
    if task["status"] in ("done", "failed"):
        return jsonify(ok=False, msg="任务已结算"), 409
    order = db.execute("SELECT * FROM orders WHERE id=?", (task["order_id"],)).fetchone()
    if not order:
        return jsonify(ok=False, msg="关联订单不存在"), 404

    # 结算用网站侧订单快照(含中文名/emoji)，payload 是纯英文的 agent 载荷仅供机器人读
    line_items = json.loads(order["items"] or task["payload"] or "[]")
    reported = (data.get("status") or "").strip()
    # 交付数量：优先取 items 明细；否则 done/success→全交付，其余→全 0
    delivered_map = {}
    if data.get("items"):
        for it in data["items"]:
            delivered_map[str(it.get("id"))] = it.get("delivered", 0)
    elif reported in ("done", "success"):
        delivered_map = {str(li["id"]): li["qty"] for li in line_items}

    arm = data.get("arm") or {}
    order_status, paid = _settle_order(db, order, line_items, delivered_map, arm)
    task_status = "done" if order_status in ("success", "partial") else "failed"
    db.execute("UPDATE tasks SET status=?, result=?, finished_at=? WHERE id=?",
               (task_status, json.dumps(data.get("result") or data, ensure_ascii=False),
                now_str(), tid))
    set_stat(db, "pickup_flag", 0)   # agent 上报完成 = 交付结束，机器人收回
    # 落一条执行日志
    db.execute(
        """INSERT INTO exec_logs(task_id, order_id, agent_id, ts, instruction, status, code, detail)
           VALUES(?,?,?,?,?,?,?,?)""",
        (tid, task["order_id"], data.get("agent_id"), now_str(), task["instruction"],
         order_status, data.get("code"),
         json.dumps({"paid": paid, "arm": arm, "items": line_items}, ensure_ascii=False)),
    )
    db.commit()
    new = db.execute("SELECT coins FROM users WHERE id=?", (task["user_id"],)).fetchone()
    return jsonify(ok=True, task_id=tid, order_id=task["order_id"],
                   order_status=order_status, task_status=task_status, paid=paid,
                   coins=(new["coins"] if new else None), items=line_items)


@app.route("/api/exec_log", methods=["POST"])
def api_exec_log_post():
    """上传一条执行/编码信息：{task_id?, order_id?, agent_id?, ts?, instruction?, status?, code?, detail?}
    ts 缺省用服务器时间；detail 可为对象(自动转 JSON)。"""
    if not check_token():
        return jsonify(ok=False, msg="未授权（需 X-API-Token）"), 401
    data = request.get_json(force=True, silent=True) or {}
    detail = data.get("detail")
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False)
    db = get_db()
    db.execute(
        """INSERT INTO exec_logs(task_id, order_id, agent_id, ts, instruction, status, code, detail)
           VALUES(?,?,?,?,?,?,?,?)""",
        (data.get("task_id"), data.get("order_id"), data.get("agent_id"),
         (data.get("ts") or now_str()), data.get("instruction"), data.get("status"),
         data.get("code"), detail),
    )
    db.commit()
    lid = db.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    return jsonify(ok=True, id=lid)


@app.route("/api/exec_log")
def api_exec_log_get():
    """查询执行日志，可 ?task_id= & ?order_id= & ?limit= 。"""
    db = get_db()
    conds, params = [], []
    for k in ("task_id", "order_id"):
        if request.args.get(k):
            conds.append(f"{k}=?")
            params.append(request.args.get(k))
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    limit = int(request.args.get("limit") or 100)
    rows = db.execute(f"SELECT * FROM exec_logs {where} ORDER BY id DESC LIMIT ?",
                      (*params, limit)).fetchall()
    return jsonify(ok=True, logs=[dict(r) for r in rows])


# ============================================================================
# 取货确认 —— 真机模式：下单后 pickup_flag=1（机械臂交付），顾客点「确定取走」或超时
# 120s 后结算(扣款/扣库存) + pickup_flag=0（机器人收回）。修复"买完不扣款"。
# ============================================================================
def _settle_full(db, order):
    """按订单明细全额结算(视为全部成功交付)：扣款/扣库存/记机械臂统计，返回 (status, paid)。"""
    line_items = json.loads(order["items"] or "[]")
    delivered_map = {str(li["id"]): li.get("qty", 0) for li in line_items}
    total_qty = sum(int(li.get("qty", 0)) for li in line_items)
    status, paid = _settle_order(db, order, line_items, delivered_map,
                                 {"exec": total_qty, "success": total_qty, "fail": 0})
    db.execute("UPDATE tasks SET status='done', finished_at=? "
               "WHERE order_id=? AND status IN ('pending','running')",
               (now_str(), order["id"]))
    set_stat(db, "pickup_flag", 0)
    return status, paid


@app.route("/api/order/confirm", methods=["POST"])
def api_order_confirm():
    """顾客点「确定取走」：结算订单并让机器人收回(pickup_flag=0)。顾客动作，无需令牌。"""
    data = request.get_json(force=True, silent=True) or {}
    oid = data.get("order_id")
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        return jsonify(ok=False, msg="订单不存在"), 404
    if order["status"] != "pending":     # 幂等：已结算直接返回
        set_stat(db, "pickup_flag", 0)
        db.commit()
        u = db.execute("SELECT coins FROM users WHERE id=?", (order["user_id"],)).fetchone()
        return jsonify(ok=True, already=True, paid=order["total"],
                       coins=(u["coins"] if u else None))
    status, paid = _settle_full(db, order)
    db.commit()
    u = db.execute("SELECT coins FROM users WHERE id=?", (order["user_id"],)).fetchone()
    return jsonify(ok=True, order_id=oid, order_status=status, paid=paid,
                   coins=(u["coins"] if u else None), msg="已取走，扣款完成，谢谢惠顾")


@app.route("/api/pickup")
def api_pickup():
    """机器人/前端轮询取货标签。flag=1: 有订单待取(机械臂前伸交付)；flag=0: 空闲/已取(收回)。
    下单超过 PICKUP_TIMEOUT 秒未确认 → 自动结算并置 0（机器人收回）。无需令牌。"""
    db = get_db()
    task = db.execute(
        "SELECT * FROM tasks WHERE status IN ('pending','running') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not task:
        set_stat(db, "pickup_flag", 0)
        db.commit()
        return jsonify(ok=True, flag=0)
    order = db.execute("SELECT * FROM orders WHERE id=?", (task["order_id"],)).fetchone()
    try:
        waited = (datetime.now() - datetime.strptime(task["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (ValueError, TypeError):
        waited = 0
    remaining = max(0, int(PICKUP_TIMEOUT - waited))
    if remaining <= 0:                   # 超时：自动结算并收回
        if order and order["status"] == "pending":
            _settle_full(db, order)
        else:
            db.execute("UPDATE tasks SET status='done', finished_at=? WHERE id=?",
                       (now_str(), task["id"]))
            set_stat(db, "pickup_flag", 0)
        db.commit()
        return jsonify(ok=True, flag=0, timed_out=True, order_id=task["order_id"])
    amount = 0.0
    if order:
        amount = sum(li.get("price", 0) * li.get("qty", 0)
                     for li in json.loads(order["items"] or "[]"))
    set_stat(db, "pickup_flag", 1)
    db.commit()
    return jsonify(ok=True, flag=1, order_id=task["order_id"], task_id=task["id"],
                   remaining=remaining, amount=round(amount, 2),
                   instruction=task["instruction"])


@app.route("/api/order/status")
def api_order_status():
    """前端下单后轮询：等待 agent 上报完成。返回订单是否已结算(settled)+ 最终金额/余额。
    settled=False 表示仍在等待机器人/agent 执行；无需令牌。"""
    oid = request.args.get("order_id")
    if not oid:
        return jsonify(ok=False, msg="缺少 order_id"), 400
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        return jsonify(ok=False, msg="订单不存在"), 404
    # 超时兜底：pending 超过 PICKUP_TIMEOUT 秒仍未被 agent 结算，就自动结算(扣款+收回)，
    # 避免前端一直卡在「等待完成」——尤其自定义商品 agent 无法履约时。
    if order["status"] == "pending":
        try:
            waited = (datetime.now() - datetime.strptime(order["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        except (ValueError, TypeError):
            waited = 0
        if waited >= PICKUP_TIMEOUT:
            _settle_full(db, order)
            db.commit()
            order = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    task = db.execute("SELECT status FROM tasks WHERE order_id=? ORDER BY id DESC LIMIT 1",
                      (oid,)).fetchone()
    u = db.execute("SELECT coins FROM users WHERE id=?", (order["user_id"],)).fetchone()
    return jsonify(
        ok=True, order_id=int(oid), order_status=order["status"],
        task_status=(task["status"] if task else None),
        settled=(order["status"] != "pending"),
        paid=order["total"],
        arm={"exec": order["arm_exec"], "success": order["arm_success"], "fail": order["arm_fail"]},
        coins=(u["coins"] if u else None),
    )


# ============================================================================
# LoopViz —— LoopMaster 四角色（Handler/Strategist/Worker/Auditor）可视化
# 只读扫描 ../loopmaster 的 run 产物与技能注册表，整合进本站，不改动 loopmaster。
# ============================================================================
LV_ROLES = {
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
        "duty": "Reviews trace evidence, detects missing task-level skills, writes review.md.",
        "duty_cn": "依据轨迹证据判定结果（done/retry/blocked/research_needed），发现缺失的任务级技能。",
        "contract": ("Independently review the plan and trace. Classify the run as done, retry, "
                     "blocked, or research_needed. Do not execute tools or edit files."),
        "produces": ["review.md", "auditor_agent.json"],
        "skill_relation": "不执行技能，只评估用了哪些技能、控制技能是否安全收尾、是否有仿真泄漏。",
    },
}
LV_CONTROL_SKILLS = {"send_action", "move_arm_joints", "set_gripper", "set_base_velocity", "set_lift_height"}


def _lv_parse_frontmatter(text):
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


def lv_load_skills():
    roots = [(Path(os.environ.get("LOOPMASTER_SKILL_ROOT", str(LOOP_SKILL_ROOT))).expanduser(), False)]
    out = {}
    for root, is_user in roots:
        if not root.exists():
            continue
        for md in root.rglob("SKILL.md"):
            fm, body = _lv_parse_frontmatter(md.read_text(encoding="utf-8"))
            rel = md.parent.relative_to(root).parts
            name = fm.get("name") or md.parent.name
            if name in out:
                continue
            category = fm.get("category") or "/".join(rel[:-1]) or "misc"
            out[name] = {
                "name": name,
                "category": category,
                "group": category.split("/")[-1],
                "description": fm.get("description") or "",
                "args": fm.get("args") if isinstance(fm.get("args"), dict) else {},
                "is_user": is_user,
                "is_control": name in LV_CONTROL_SKILLS,
                "body": body.strip(),
            }
    return sorted(out.values(), key=lambda s: (s["group"], s["name"]))


def _lv_run_roots():
    seen, roots = set(), []
    for p in [os.environ.get("LOOPMASTER_WORKSPACE_ROOT"),
              str(LOOP_REPO / "_viz_runs"),
              str(Path("~/.loopmaster/workspaces").expanduser())]:
        if not p:
            continue
        rp = Path(p).expanduser()
        if rp.exists() and str(rp) not in seen:
            seen.add(str(rp))
            roots.append(rp)
    return roots


def _lv_list_runs():
    runs = []
    for root in _lv_run_roots():
        for d in root.iterdir():
            if d.is_dir() and (d / "plan.md").exists():
                runs.append(d)
    if LOOPVIZ_TTL > 0:                # 演示：过期的 run 不再列出
        now = time.time()
        runs = [d for d in runs if now - d.stat().st_mtime <= LOOPVIZ_TTL]
    runs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return runs


def _lv_read(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _lv_load_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _lv_parse_plan_md(text):
    sections, cur = {}, None
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


def _lv_parse_review_md(text):
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


def _lv_parse_trace(path):
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


def _lv_parse_run(run_dir):
    plan = _lv_parse_plan_md(_lv_read(run_dir / "plan.md"))
    review = _lv_parse_review_md(_lv_read(run_dir / "review.md"))
    trace = _lv_parse_trace(run_dir / "trace.jsonl")
    dirname = run_dir.name
    m = re.search(r"-(\d{8}-\d{6}-[0-9a-f]{6})$", dirname)
    run_id = m.group(1) if m else dirname
    task = plan.get("title") or dirname
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
        "summary_md": _lv_read(run_dir / "summary.md"),
        "trace_stats": {"total": len(trace), "ok": ok_n, "fail": len(trace) - ok_n, "roles": roles_seen},
        "agent_json": {
            "handler": _lv_load_json(run_dir / "handler_agent.json"),
            "strategist": _lv_load_json(run_dir / "strategist_agent.json"),
            "worker": _lv_load_json(run_dir / "worker_agent.json"),
            "auditor": _lv_load_json(run_dir / "auditor_agent.json"),
        },
        "artifacts": sorted(p.name for p in run_dir.iterdir() if p.is_file()),
        "loop_events": _lv_load_json(run_dir / "loop_events.json") or [],
    }


@app.route("/loopviz")
def loopviz():
    return render_template("loopviz.html")


@app.route("/api/loopviz/skills")
def api_lv_skills():
    return jsonify(ok=True, skills=lv_load_skills(), roles=LV_ROLES)


@app.route("/api/loopviz/runs")
def api_lv_runs():
    runs = _lv_list_runs()
    brief = []
    for d in runs:
        r = _lv_parse_review_md(_lv_read(d / "review.md"))
        pm = re.match(r"^#\s+Plan:\s*(.*)", _lv_read(d / "plan.md"))
        brief.append({
            "id": d.name,
            "task": (pm.group(1).strip() if pm else d.name),
            "verdict": r["verdict"],
            "mtime": d.stat().st_mtime,
        })
    return jsonify(ok=True, runs=brief, roots=[str(x) for x in _lv_run_roots()])


@app.route("/api/loopviz/run/<path:run_id>")
def api_lv_run(run_id):
    for root in _lv_run_roots():
        cand = root / run_id
        if cand.exists() and (cand / "plan.md").exists():
            if LOOPVIZ_TTL > 0 and time.time() - cand.stat().st_mtime > LOOPVIZ_TTL:
                return jsonify(ok=False, msg="run expired"), 404
            return jsonify(ok=True, run=_lv_parse_run(cand), roles=LV_ROLES,
                           control_skills=sorted(LV_CONTROL_SKILLS))
    return jsonify(ok=False, msg="run not found"), 404


# ------------------ LoopViz 写接口：供 agent 框架推送运行/技能 ------------------
LV_RUN_FILES = {"plan.md", "trace.jsonl", "review.md", "summary.md",
                "handler_agent.json", "strategist_agent.json",
                "worker_agent.json", "auditor_agent.json",
                "loop_events.json"}   # 四角色循环事件流（供概述动态回放）


def _lv_write_root():
    env = os.environ.get("LOOPMASTER_WORKSPACE_ROOT")
    root = Path(env).expanduser() if env else (LOOP_REPO / "_viz_runs")
    root.mkdir(parents=True, exist_ok=True)
    return root


@app.route("/api/loopviz/run", methods=["POST"])
def api_lv_write_run():
    """agent 框架推送一次运行：{id, files:{"plan.md":..,"trace.jsonl":..,"*_agent.json":{..}}}。
    dict/list 内容自动转 JSON；文件名白名单过滤。"""
    if not check_token():
        return jsonify(ok=False, msg="未授权（需 X-API-Token）"), 401
    data = request.get_json(force=True, silent=True) or {}
    run_id = (data.get("id") or "").strip()
    files = data.get("files") or {}
    if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        return jsonify(ok=False, msg="非法 run id"), 400

    run_dir = _lv_write_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for fname, content in files.items():
        if fname not in LV_RUN_FILES:
            continue
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        (run_dir / fname).write_text(str(content), encoding="utf-8")
        written.append(fname)
    # 保底 plan.md，确保运行能被列表识别
    if not (run_dir / "plan.md").exists():
        (run_dir / "plan.md").write_text(f"# Plan: {run_id}\n", encoding="utf-8")
    # 刷新目录 mtime：覆盖已存在文件不会更新目录 mtime，而 TTL 用的是目录 mtime，
    # 故显式 touch，让「固定 id 反复重推保活」在 LOOPVIZ_TTL 下生效。
    os.utime(run_dir, None)
    return jsonify(ok=True, id=run_id, dir=str(run_dir), written=written,
                   run=_lv_parse_run(run_dir))


@app.route("/api/loopviz/run/<path:run_id>", methods=["DELETE"])
def api_lv_delete_run(run_id):
    """删除一次运行目录。"""
    if not check_token():
        return jsonify(ok=False, msg="未授权（需 X-API-Token）"), 401
    import shutil
    for root in _lv_run_roots():
        cand = root / run_id
        if cand.exists() and cand.is_dir():
            shutil.rmtree(cand)
            return jsonify(ok=True, deleted=run_id)
    return jsonify(ok=False, msg="run not found"), 404


@app.route("/api/loopviz/skill", methods=["POST"])
def api_lv_write_skill():
    """agent 框架注册/更新一个技能，写入用户技能目录（LOOPMASTER_SKILL_ROOT，默认 loopmaster_agentic/skills）。
    body: {name, category, description, args:{..}, body, }"""
    if not check_token():
        return jsonify(ok=False, msg="未授权（需 X-API-Token）"), 401
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name or not re.match(r"^[A-Za-z0-9_.-]+$", name):
        return jsonify(ok=False, msg="非法技能名"), 400
    category = (data.get("category") or "control").strip().strip("/") or "control"
    description = (data.get("description") or "").replace("\n", " ").strip()
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    body = str(data.get("body") or "").strip()

    root = Path(os.environ.get("LOOPMASTER_SKILL_ROOT", str(LOOP_SKILL_ROOT))).expanduser()
    skill_dir = root / category / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", f"category: {category}"]
    if description:
        lines.append(f"description: {description}")
    if args:
        lines.append("args:")
        lines += [f"  {k}: {v}" for k, v in args.items()]
    lines += ["---", "", body, ""]
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return jsonify(ok=True, name=name, category=category, path=str(skill_dir / "SKILL.md"))


# 模块级建表：gunicorn/uwsgi 等 WSGI 服务器是 import app:app，不走 __main__，
# 必须在导入时就把库建好，否则首个请求就 no such table。init_db 幂等，可反复调用。
init_db()


if __name__ == "__main__":
    snap = snapshot_db()          # 启动即自动快照，防止误删
    if snap:
        print("  启动快照:", os.path.basename(snap))
    print("=" * 48)
    print("  麦西 Messi · 双臂销售机器人售卖系统")
    print("  下单页 : http://127.0.0.1:5000/")
    print("  数据后台: http://127.0.0.1:5000/dashboard")
    print("  智能体页: http://127.0.0.1:5000/loopviz")
    print("=" * 48)
    app.run(host="0.0.0.0", port=5000, debug=True)
