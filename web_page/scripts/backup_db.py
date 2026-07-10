# -*- coding: utf-8 -*-
"""
麦西 Messi 数据库备份脚本（可复用）
用法：
    python scripts/backup_db.py            # 备份到 backup/vending_<时间戳>.db
    python scripts/backup_db.py --keep 50  # 保留最近 50 份（默认 30）
使用 SQLite 在线 backup API，服务器运行中也能安全生成一致性快照。
"""
import os
import sys
import glob
import sqlite3
import argparse
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "vending.db")
BACKUP_DIR = os.path.join(BASE, "backup")


def backup(keep=30):
    if not os.path.exists(DB):
        print(f"⚠️ 数据库不存在：{DB}（还没人下过单？）")
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"vending_{ts}.db")

    src = sqlite3.connect(DB)
    bak = sqlite3.connect(dst)
    try:
        with bak:
            src.backup(bak)   # 一致性快照
    finally:
        bak.close()
        src.close()

    # 校验
    v = sqlite3.connect(dst)
    users = v.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    orders = v.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    v.close()
    size = os.path.getsize(dst)
    print(f"✅ 备份完成：{dst} ({size} bytes) | 用户 {users} 订单 {orders}")

    # 轮转：只保留最近 keep 份
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "vending_*.db")))
    removed = 0
    while len(files) > keep:
        old = files.pop(0)
        try:
            os.remove(old)
            removed += 1
        except OSError:
            break
    if removed:
        print(f"🧹 已清理 {removed} 份旧备份，保留最近 {keep} 份")
    return dst


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=30, help="保留最近几份备份")
    args = ap.parse_args()
    backup(keep=args.keep)
