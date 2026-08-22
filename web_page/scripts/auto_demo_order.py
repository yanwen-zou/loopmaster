# -*- coding: utf-8 -*-
"""展会无人值守演示：达到空闲阈值时随机创建一笔饮料订单。"""
import json
import sys
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parent.parent
if str(WEB_ROOT) not in sys.path:
    sys.path.insert(0, str(WEB_ROOT))

from app import maybe_create_auto_demo_order  # noqa: E402


if __name__ == "__main__":
    result = maybe_create_auto_demo_order()
    if result.get("created"):
        print(json.dumps(result, ensure_ascii=False), flush=True)
