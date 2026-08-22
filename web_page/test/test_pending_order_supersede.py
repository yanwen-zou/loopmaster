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
    os.environ["LOOPMASTER_API_TOKEN"] = "order-supersede-test-token"
    spec = importlib.util.spec_from_file_location("loopmaster_web_order_supersede_test", APP_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config["TESTING"] = True
    return module


def _post_order(client, *, user_id: str, product_id: int, request_id: str):
    return client.post(
        "/api/order",
        headers={"Idempotency-Key": request_id},
        json={
            "user_id": user_id,
            "request_id": request_id,
            "items": [{"id": product_id, "qty": 1}],
        },
    )


def test_new_order_supersedes_pending_but_not_running() -> None:
    with tempfile.TemporaryDirectory(prefix="loopmaster-order-supersede-") as temp_dir:
        db_path = Path(temp_dir) / "vending.db"
        module = _load_test_app(db_path)
        client = module.app.test_client()

        for user_id in ("first-user", "second-user", "third-user"):
            response = client.post("/api/login", json={"user_id": user_id})
            assert response.status_code == 200

        products = client.get("/api/products").get_json()["products"]
        product_id = next(p["id"] for p in products if p["name_en"] == "nongfu")

        first = _post_order(
            client, user_id="first-user", product_id=product_id, request_id="replace-test-1"
        )
        assert first.status_code == 200, first.get_json()
        first_data = first.get_json()
        assert first_data["superseded_task_ids"] == []

        second = _post_order(
            client, user_id="second-user", product_id=product_id, request_id="replace-test-2"
        )
        assert second.status_code == 200, second.get_json()
        second_data = second.get_json()
        assert first_data["task_id"] in second_data["superseded_task_ids"]

        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        first_order = db.execute(
            "SELECT * FROM orders WHERE id=?", (first_data["order_id"],)
        ).fetchone()
        first_task = db.execute(
            "SELECT * FROM tasks WHERE id=?", (first_data["task_id"],)
        ).fetchone()
        first_user = db.execute("SELECT coins FROM users WHERE id='first-user'").fetchone()
        assert first_order["status"] == "failed"
        assert first_order["total"] == 0
        assert all(item["delivered"] == 0 for item in json.loads(first_order["items"]))
        assert first_task["status"] == "failed"
        assert json.loads(first_task["result"])["reason"] == "superseded_by_new_order"
        assert first_user["coins"] == module.NEW_USER_COINS
        assert db.execute(
            "SELECT COUNT(*) FROM exec_logs WHERE task_id=? AND code='SUPERSEDED_BY_NEW_ORDER'",
            (first_data["task_id"],),
        ).fetchone()[0] == 1
        db.close()

        cancelled_claim = client.post(
            f"/api/tasks/{first_data['task_id']}/claim",
            headers={"X-API-Token": "order-supersede-test-token"},
            json={"agent_id": "test-robot"},
        )
        assert cancelled_claim.status_code == 409, cancelled_claim.get_json()

        db = sqlite3.connect(db_path)
        db.execute(
            "UPDATE tasks SET status='running', claimed_at=? WHERE id=?",
            (module.now_str(), second_data["task_id"]),
        )
        db.commit()
        order_count_before = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        db.close()

        blocked = _post_order(
            client, user_id="third-user", product_id=product_id, request_id="replace-test-3"
        )
        assert blocked.status_code == 409, blocked.get_json()
        assert blocked.get_json()["busy"] is True

        duplicate = _post_order(
            client, user_id="second-user", product_id=product_id, request_id="replace-test-2"
        )
        assert duplicate.status_code == 200, duplicate.get_json()
        assert duplicate.get_json()["duplicate"] is True

        db = sqlite3.connect(db_path)
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == order_count_before
        assert db.execute(
            "SELECT status FROM tasks WHERE id=?", (second_data["task_id"],)
        ).fetchone()[0] == "running"
        db.close()


if __name__ == "__main__":
    test_new_order_supersedes_pending_but_not_running()
    print("pending_order_supersede_ok")
