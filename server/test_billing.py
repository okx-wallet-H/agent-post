#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""billing.py 自测（P1-B）：临时 DB 全流程断言，只依赖标准库 + fastapi/pydantic。"""

import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import billing  # noqa: E402


def main():
    try:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # agents 表按 main.py 口径（自测临时库，main 的建表由主服务负责）
        conn.executescript(
            "CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL,"
            " token TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);"
        )
        billing.init_db(conn)

        # 1) 建账号
        conn.execute(
            "INSERT INTO agents (id, name, token, created_at) VALUES (?,?,?,?)",
            ("ag_test", "测试号", "tok_test", billing.now_iso()),
        )
        conn.commit()

        # 2) 建券（2 天 / 3 条）
        code = billing.create_coupon(conn, days=2, msgs=3)
        assert code, "建券应返回 code"

        # 3) 兑换
        r = billing.redeem_coupon(conn, code, "ag_test")
        assert r["ok"] is True and r["days"] == 2 and r["msgs"] == 3 and r["expires_at"], r

        # 4) usage 断言：quota_msgs = 3
        u = billing.get_usage(conn, "ag_test")
        assert u["quota_msgs"] == 3, "quota_msgs=%s 应为 3" % u["quota_msgs"]
        assert u["remaining_msgs"] == 3, "remaining_msgs=%s 应为 3" % u["remaining_msgs"]
        assert u["quota_agents"] == billing.AGENTS_QUOTA_DEFAULT, u

        # 5) 记 3 条用量 → 第 4 条 check_quota 返回 False 且带「额度用尽」
        for _ in range(3):
            billing.record_usage(conn, "ag_test", "msg", 1)
        ok, info = billing.check_quota(conn, "ag_test", "msg")  # 这就是第 4 条
        assert ok is False, "第 4 条应超限，但返回了 True"
        assert "额度用尽" in info["detail"], info
        assert info["remaining"] == 0, info

        # 6) 同一 code 二次兑换 → 失败
        try:
            billing.redeem_coupon(conn, code, "ag_test")
            raise AssertionError("同一 code 二次兑换应失败，但没有报错")
        except Exception as e:
            assert getattr(e, "status_code", None) == 409, "二次兑换应 409，实际 %r" % e

        # 7) 顺带：不存在的券 404、usage 跨账号不串
        try:
            billing.redeem_coupon(conn, "cp_notexist", "ag_test")
            raise AssertionError("不存在的券应 404")
        except Exception as e:
            assert getattr(e, "status_code", None) == 404, "应 404，实际 %r" % e
        u2 = billing.get_usage(conn, "ag_other")
        assert u2["quota_msgs"] == 0 and u2["remaining_msgs"] == 0, u2

        print("TEST: PASS")
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("TEST: FAIL:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
