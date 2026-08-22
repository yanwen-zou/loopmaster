from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

EXPECTED_SKUS = {
    "cola", "red_bull", "nongfu", "milk", "loopmaster_water",
    "cestbon_water", "ad_calcium_milk", "oriental_leaf_jasmine_tea",
    "mingren_soda_water", "ham_sausage", "cracker_noodle", "wangzai_bun",
    "mung_bean_cake", "cheese_biscuit", "chocolate_bar",
    "spicy_gluten_strip", "cake_bread", "oreo_cookie", "tissue", "custom",
}


def _load_app(db_path: Path):
    os.environ["LOOPMASTER_DB_PATH"] = str(db_path)
    os.environ["LOOPMASTER_API_TOKEN"] = "catalog-test-token"
    spec = importlib.util.spec_from_file_location("loopmaster_web_catalog_test", APP_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config["TESTING"] = True
    return module


def test_full_catalog_is_visible_and_init_is_idempotent() -> None:
    with tempfile.TemporaryDirectory(prefix="loopmaster-product-catalog-") as temp_dir:
        db_path = Path(temp_dir) / "vending.db"
        module = _load_app(db_path)
        client = module.app.test_client()

        products = client.get("/api/products").get_json()["products"]
        assert {product["name_en"] for product in products} == EXPECTED_SKUS
        assert {product["category"] for product in products} == {"饮料", "零食", "日用品", "自定义"}
        assert all(
            not product["image"] or (APP_PATH.parent / "static" / "assets" / product["image"]).is_file()
            for product in products
        )

        module.init_db()
        db = sqlite3.connect(db_path)
        assert db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == len(EXPECTED_SKUS)
        db.close()


if __name__ == "__main__":
    test_full_catalog_is_visible_and_init_is_idempotent()
    print("product_catalog_ok")
