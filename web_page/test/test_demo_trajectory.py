from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def _load_test_app(db_path: Path):
    os.environ["ARM_SIMULATE"] = "0"
    os.environ["LOOPMASTER_DB_PATH"] = str(db_path)
    os.environ["LOOPMASTER_ADMIN_PASSWORD"] = "demo-test-password"
    spec = importlib.util.spec_from_file_location("loopmaster_web_demo_trajectory_test", APP_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config["TESTING"] = True
    return module


def _admin_client(module):
    client = module.app.test_client()
    response = client.post(
        "/admin/login",
        data={"password": "demo-test-password", "next": "/dashboard"},
    )
    assert response.status_code == 302
    return client


def test_demo_run_cancels_active_task_and_enqueues_direct_episode() -> None:
    with tempfile.TemporaryDirectory(prefix="loopmaster-demo-trajectory-") as temp_dir:
        db_path = Path(temp_dir) / "vending.db"
        module = _load_test_app(db_path)
        client = _admin_client(module)

        db = sqlite3.connect(db_path)
        created_at = module.now_str()
        db.execute(
            "INSERT INTO users(id, ip, coins, visits, created_at) VALUES(?,?,?,?,?)",
            ("customer", "test", 200, 1, created_at),
        )
        db.execute(
            "INSERT INTO orders(user_id, ip, items, total, status, created_at) VALUES(?,?,?,?,?,?)",
            ("customer", "test", '[{"id":1,"qty":1}]', 0, "pending", created_at),
        )
        order_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO tasks(order_id,user_id,instruction,payload,status,created_at) VALUES(?,?,?,?,?,?)",
            (order_id, "customer", "old task", "[]", "running", created_at),
        )
        old_task_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()
        db.close()

        response = client.post("/api/demo-trajectory/run", json={"episode": 3})
        assert response.status_code == 200, response.get_json()
        data = response.get_json()
        assert data["episode"] == 3
        assert data["cancelled_task_ids"] == [old_task_id]

        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        old_task = db.execute("SELECT * FROM tasks WHERE id=?", (old_task_id,)).fetchone()
        old_order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        new_task = db.execute("SELECT * FROM tasks WHERE id=?", (data["task_id"],)).fetchone()
        assert old_task["status"] == "failed"
        assert json.loads(old_task["result"])["reason"] == "replaced_by_demo_trajectory"
        assert old_order["status"] == "failed"
        assert new_task["status"] == "pending"
        assert new_task["order_id"] is None
        assert json.loads(new_task["payload"]) == [
            {"type": "demo_trajectory", "episode": 3, "qty": 0}
        ]
        assert db.execute(
            "SELECT COUNT(*) FROM exec_logs WHERE task_id=? AND code='CANCELLED_BY_ADMIN'",
            (old_task_id,),
        ).fetchone()[0] == 1
        db.close()


def test_demo_endpoints_require_admin_and_validate_episode() -> None:
    with tempfile.TemporaryDirectory(prefix="loopmaster-demo-auth-") as temp_dir:
        module = _load_test_app(Path(temp_dir) / "vending.db")
        anonymous = module.app.test_client()
        assert anonymous.get("/api/demo-trajectory").status_code == 401

        client = _admin_client(module)
        status = client.get("/api/demo-trajectory")
        assert status.status_code == 200
        assert status.get_json()["episodes"] == [0, 1, 2, 3, 4]
        invalid = client.post("/api/demo-trajectory/run", json={"episode": 99})
        assert invalid.status_code == 400


if __name__ == "__main__":
    test_demo_run_cancels_active_task_and_enqueues_direct_episode()
    test_demo_endpoints_require_admin_and_validate_episode()
    print("demo_trajectory_ok")
