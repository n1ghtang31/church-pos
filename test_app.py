import os
import tempfile
import unittest

_tmp = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_tmp.name, "test.db")
os.environ["ADMIN_CODE"] = "99990000"
os.environ["SECRET_KEY"] = "test-secret"

import app as pos


class PosTestCase(unittest.TestCase):
    def setUp(self):
        conn = pos.get_db_connection()
        conn.execute("DELETE FROM login_failures")
        conn.execute("DELETE FROM transaction_items")
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM cashiers")
        conn.execute(
            "INSERT INTO cashiers (name, pin_hash) VALUES (?, ?)", ("Pat", pos.hash_pin("123456"))
        )
        conn.commit()
        conn.close()
        self.client = pos.app.test_client()

    def login(self, code="123456"):
        response = self.client.post("/api/login", json={"code": code})
        self.assertEqual(response.status_code, 200)
        return response.json

    def sell(self, items, tender="cash"):
        return self.client.post("/api/transactions", json={"items": items, "tender": tender})

    def backdate(self, transaction_id, day):
        conn = pos.get_db_connection()
        conn.execute(
            "UPDATE transactions SET timestamp = ? WHERE id = ?",
            (f"{day}T12:00:00", transaction_id),
        )
        conn.commit()
        conn.close()


class AuthTests(PosTestCase):
    def test_api_requires_login(self):
        for path in ["/api/items", "/api/transactions", "/api/stats", "/api/item-sales", "/api/reports/summary"]:
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(self.sell([{"id": 1, "price": 5}]).status_code, 401)

    def test_health_is_public(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_wrong_password_rejected(self):
        for body in [{"code": "0000"}, {"code": 1234}, {}, []]:
            self.assertEqual(self.client.post("/api/login", json=body).status_code, 401)
        self.assertEqual(self.client.get("/api/items").status_code, 401)

    def test_login_and_logout(self):
        self.login()
        self.assertEqual(self.client.get("/api/items").status_code, 200)
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/items").status_code, 401)

    def test_private_address_locked_out_for_a_while(self):
        for _ in range(pos.MAX_LOGIN_FAILURES):
            self.client.post("/api/login", json={"code": "0000"})
        response = self.client.post("/api/login", json={"code": "123456"})
        self.assertEqual(response.status_code, 429)

        conn = pos.get_db_connection()
        conn.execute("UPDATE login_failures SET locked_until = 0")
        conn.commit()
        conn.close()
        self.assertEqual(self.client.post("/api/login", json={"code": "123456"}).status_code, 200)


class CashierTests(PosTestCase):
    def setUp(self):
        super().setUp()
        self.login("99990000")

    def cashier_client(self, code):
        client = pos.app.test_client()
        return client, client.post("/api/login", json={"code": code})

    def cashier_id(self, name):
        return next(c["id"] for c in self.client.get("/api/cashiers").json if c["name"] == name)

    def test_admin_code_logs_in_as_admin(self):
        self.assertEqual(self.client.get("/api/session").json["role"], "admin")

    def test_cashier_cannot_manage_cashiers(self):
        client, response = self.cashier_client("123456")
        self.assertEqual(response.json["name"], "Pat")
        self.assertEqual(client.get("/api/cashiers").status_code, 403)
        self.assertEqual(client.post("/api/cashiers", json={"name": "Eve", "pin": "555555"}).status_code, 403)
        self.assertEqual(client.delete(f"/api/cashiers/{self.cashier_id('Pat')}").status_code, 403)

    def test_logged_out_cannot_manage_cashiers(self):
        self.assertEqual(pos.app.test_client().get("/api/cashiers").status_code, 401)

    def test_admin_cannot_ring_up_sales(self):
        self.assertEqual(self.sell([{"id": 1, "price": 5}]).status_code, 403)

    def test_create_cashier_and_log_in(self):
        response = self.client.post("/api/cashiers", json={"name": " Sam ", "pin": "555555"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["name"], "Sam")
        self.assertNotIn("pin", response.json)
        self.assertNotIn("pin_hash", response.json)
        _, login = self.cashier_client("555555")
        self.assertEqual(login.json["role"], "cashier")

    def test_four_digit_pin_allowed(self):
        response = self.client.post("/api/cashiers", json={"name": "Sam", "pin": "4321"})
        self.assertEqual(response.status_code, 201)
        _, login = self.cashier_client("4321")
        self.assertEqual(login.json["name"], "Sam")

    def test_invalid_cashiers_rejected(self):
        bad = [
            {"name": "", "pin": "555555"},
            {"name": "Sam", "pin": "123"},
            {"name": "Sam", "pin": "12345678901"},
            {"name": "Sam", "pin": "abcd"},
            {"name": "Sam", "pin": 5555},
            {"name": "Sam", "pin": "99990000"},
            {"name": "Sam"},
        ]
        for body in bad:
            self.assertEqual(self.client.post("/api/cashiers", json=body).status_code, 400, body)

    def test_duplicates_rejected(self):
        self.assertEqual(self.client.post("/api/cashiers", json={"name": "PAT", "pin": "555555"}).status_code, 409)
        self.assertEqual(self.client.post("/api/cashiers", json={"name": "Sam", "pin": "123456"}).status_code, 409)

    def test_edit_name_keeps_pin(self):
        cashier_id = self.cashier_id("Pat")
        response = self.client.put(f"/api/cashiers/{cashier_id}", json={"name": "Patricia"})
        self.assertEqual(response.json["name"], "Patricia")
        _, login = self.cashier_client("123456")
        self.assertEqual(login.json["name"], "Patricia")

    def test_edit_pin_replaces_old_pin(self):
        cashier_id = self.cashier_id("Pat")
        self.assertEqual(self.client.put(f"/api/cashiers/{cashier_id}", json={"pin": "777777"}).status_code, 200)
        self.assertEqual(self.cashier_client("123456")[1].status_code, 401)
        self.assertEqual(self.cashier_client("777777")[1].status_code, 200)

    def test_edit_missing_cashier(self):
        self.assertEqual(self.client.put("/api/cashiers/99999", json={"name": "Nobody"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/cashiers/99999").status_code, 404)

    def test_remove_logs_cashier_out_and_keeps_their_sales(self):
        client, _ = self.cashier_client("123456")
        client.post("/api/transactions", json={"items": [{"id": 1, "price": 5}], "tender": "cash"})

        self.assertEqual(self.client.delete(f"/api/cashiers/{self.cashier_id('Pat')}").status_code, 204)

        self.assertEqual(client.get("/api/items").status_code, 401)
        self.assertFalse(client.get("/api/session").json["authenticated"])
        self.assertEqual(self.cashier_client("123456")[1].status_code, 401)
        sales = self.client.get("/api/transactions").json
        self.assertEqual(sales[0]["cashier"], "Pat")


class BanTests(PosTestCase):
    VISITOR = "1.1.1.1"

    def attempt(self, code, ip=VISITOR, client=None):
        client = client or self.client
        return client.post("/api/login", json={"code": code}, environ_base={"REMOTE_ADDR": ip})

    def fail(self, times, ip=VISITOR):
        return [self.attempt("000000", ip).status_code for _ in range(times)]

    def admin(self):
        client = pos.app.test_client()
        client.post("/api/login", json={"code": "99990000"})
        return client

    def test_public_address_banned_after_ten_wrong_codes(self):
        codes = self.fail(pos.MAX_LOGIN_FAILURES)
        self.assertEqual(codes[:-1], [401] * (pos.MAX_LOGIN_FAILURES - 1))
        self.assertEqual(codes[-1], 403)
        response = self.attempt("123456")
        self.assertEqual(response.status_code, 403)
        self.assertIn("blocked", response.json["error"])

    def test_ban_does_not_expire(self):
        self.fail(pos.MAX_LOGIN_FAILURES)
        conn = pos.get_db_connection()
        conn.execute("UPDATE login_failures SET last_failure = 0")
        conn.commit()
        conn.close()
        self.fail(1, ip="2.2.2.2")
        self.assertEqual(self.attempt("123456").status_code, 403)

    def test_other_visitors_unaffected(self):
        self.fail(pos.MAX_LOGIN_FAILURES)
        self.assertEqual(self.attempt("123456", ip="2.2.2.2").status_code, 200)

    def test_correct_code_resets_count(self):
        self.fail(pos.MAX_LOGIN_FAILURES - 1)
        self.assertEqual(self.attempt("123456").status_code, 200)
        self.assertEqual(self.fail(pos.MAX_LOGIN_FAILURES - 1)[-1], 401)

    def test_old_wrong_codes_forgotten(self):
        self.fail(pos.MAX_LOGIN_FAILURES - 1)
        conn = pos.get_db_connection()
        conn.execute("UPDATE login_failures SET last_failure = 0")
        conn.commit()
        conn.close()
        self.assertEqual(self.fail(pos.MAX_LOGIN_FAILURES - 1)[-1], 401)

    def test_admin_lists_and_unblocks(self):
        self.fail(pos.MAX_LOGIN_FAILURES)
        admin = self.admin()
        blocks = admin.get("/api/login-blocks").json
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["ip"], self.VISITOR)
        self.assertTrue(blocks[0]["banned"])
        self.assertEqual(blocks[0]["failures"], pos.MAX_LOGIN_FAILURES)

        self.assertEqual(admin.delete(f"/api/login-blocks/{self.VISITOR}").status_code, 204)
        self.assertEqual(admin.get("/api/login-blocks").json, [])
        self.assertEqual(self.attempt("123456").status_code, 200)
        self.assertEqual(admin.delete(f"/api/login-blocks/{self.VISITOR}").status_code, 404)

    def test_ipv6_address_unblocks(self):
        ip = "2606:4700:4700::1111"
        self.fail(pos.MAX_LOGIN_FAILURES, ip=ip)
        admin = self.admin()
        self.assertEqual(admin.delete("/api/login-blocks/2606:4700:4700::1111").status_code, 204)
        self.assertEqual(self.attempt("123456", ip=ip).status_code, 200)

    def test_cashiers_cannot_see_or_unblock(self):
        self.fail(pos.MAX_LOGIN_FAILURES)
        cashier = pos.app.test_client()
        cashier.post("/api/login", json={"code": "123456"})
        self.assertEqual(cashier.get("/api/login-blocks").status_code, 403)
        self.assertEqual(cashier.delete(f"/api/login-blocks/{self.VISITOR}").status_code, 403)
        self.assertEqual(pos.app.test_client().get("/api/login-blocks").status_code, 401)

    def test_command_line_unban(self):
        self.fail(pos.MAX_LOGIN_FAILURES)
        self.fail(pos.MAX_LOGIN_FAILURES, ip="2.2.2.2")
        runner = pos.app.test_cli_runner()
        listing = runner.invoke(args=["bans"]).output
        self.assertIn(self.VISITOR, listing)
        self.assertIn("2.2.2.2", listing)

        self.assertIn("Unblocked 1", runner.invoke(args=["unban", self.VISITOR]).output)
        self.assertEqual(self.attempt("123456").status_code, 200)
        self.assertNotEqual(runner.invoke(args=["unban"]).exit_code, 0)
        self.assertIn("Unblocked 1", runner.invoke(args=["unban", "--all"]).output)
        self.assertIn("No addresses are blocked", runner.invoke(args=["bans"]).output)


class SecurityTests(PosTestCase):
    def tearDown(self):
        pos.TRUSTED_PROXY_IPS = []

    def fail_login(self, ip, forwarded=None):
        headers = {"X-Forwarded-For": forwarded} if forwarded else {}
        return self.client.post(
            "/api/login", json={"code": "000000"}, headers=headers, environ_base={"REMOTE_ADDR": ip}
        )

    def lock_out(self, ip, forwarded=None):
        for _ in range(pos.MAX_LOGIN_FAILURES):
            self.fail_login(ip, forwarded)

    def test_forwarded_header_ignored_without_trusted_proxy(self):
        self.lock_out("10.0.0.5", forwarded="1.1.1.1")
        self.assertEqual(self.fail_login("10.0.0.5", forwarded="2.2.2.2").status_code, 429)

    def test_trusted_proxy_locks_out_only_the_visitor(self):
        pos.TRUSTED_PROXY_IPS = [pos.ipaddress.ip_network("10.0.0.5/32")]
        self.lock_out("10.0.0.5", forwarded="1.1.1.1")
        self.assertEqual(self.fail_login("10.0.0.5", forwarded="1.1.1.1").status_code, 403)
        self.assertEqual(self.fail_login("10.0.0.5", forwarded="2.2.2.2").status_code, 401)

    def test_trusted_proxy_uses_the_entry_it_added(self):
        pos.TRUSTED_PROXY_IPS = [pos.ipaddress.ip_network("10.0.0.0/24")]
        self.lock_out("10.0.0.5", forwarded="9.9.9.9, 1.1.1.1")
        self.assertEqual(self.fail_login("10.0.0.5", forwarded="8.8.8.8, 1.1.1.1").status_code, 403)

    def test_untrusted_sender_cannot_fake_forwarded_header(self):
        pos.TRUSTED_PROXY_IPS = [pos.ipaddress.ip_network("10.0.0.5/32")]
        self.lock_out("10.0.0.9", forwarded="1.1.1.1")
        self.assertEqual(self.fail_login("10.0.0.9", forwarded="2.2.2.2").status_code, 429)

    def test_proxy_address_never_banned(self):
        # If the proxy's own address shows up, it only gets a short lockout
        self.lock_out("10.0.0.5")
        self.assertEqual(self.fail_login("10.0.0.5").status_code, 429)
        conn = pos.get_db_connection()
        banned = conn.execute("SELECT banned FROM login_failures WHERE ip = '10.0.0.5'").fetchone()[0]
        conn.close()
        self.assertEqual(banned, 0)

    def test_security_headers(self):
        response = self.client.get("/api/health")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_large_requests_rejected(self):
        response = self.client.post("/api/login", data="x" * 100_000, content_type="application/json")
        self.assertEqual(response.status_code, 413)


class TransactionTests(PosTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_donation_uses_entered_amount(self):
        donation = next(i for i in self.client.get("/api/items").json if i["name"] == "Donation")
        response = self.sell([{"id": donation["id"], "price": 37.5, "quantity": 1}])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["total"], 37.5)

    def test_invalid_items_rejected_and_not_saved(self):
        bad_carts = [
            [],
            "nope",
            [{"id": 999, "price": 5}],
            [{"id": 1, "price": 0}],
            [{"id": 1, "price": -5}],
            [{"id": 1, "price": "5"}],
            [{"id": 1}],
            [{"id": 1, "price": 5, "quantity": 0}],
            [{"id": 1, "price": 5, "quantity": 1.5}],
        ]
        for cart in bad_carts:
            self.assertEqual(self.sell(cart).status_code, 400, cart)
        self.assertEqual(self.client.get("/api/transactions").json, [])

    def test_sale_records_logged_in_cashier(self):
        self.sell([{"id": 1, "price": 5}])
        self.assertEqual(self.client.get("/api/transactions").json[0]["cashier"], "Pat")

    def test_saved_name_comes_from_server(self):
        self.sell([{"id": 1, "name": "Spoofed", "price": 5}])
        self.assertEqual(self.client.get("/api/transactions").json[0]["items"][0]["name"], "Nuts")


class ReportTests(PosTestCase):
    def setUp(self):
        super().setUp()
        self.login()
        first = self.sell(
            [{"id": 1, "price": 5, "quantity": 2}, {"id": 9, "price": 20.1}], "cash"
        ).json
        second = self.sell([{"id": 1, "price": 3}], "venmo").json
        third = self.sell([{"id": 2, "price": 0.1, "quantity": 3}], "check").json
        self.backdate(first["id"], "2026-11-01")
        self.backdate(second["id"], "2026-11-01")
        self.backdate(third["id"], "2026-11-02")

    def report(self, **params):
        response = self.client.get("/api/reports/summary", query_string=params)
        self.assertEqual(response.status_code, 200)
        return response.json

    def test_all_dates(self):
        report = self.report()
        self.assertEqual(report["overall"], {"count": 3, "total": 33.4})
        self.assertEqual([d["date"] for d in report["by_day"]], ["2026-11-01", "2026-11-02"])

    def test_daily_multi_total(self):
        report = self.report(start="2026-11-01", end="2026-11-01")
        self.assertEqual(report["overall"], {"count": 2, "total": 33.1})
        self.assertEqual(report["by_tender"]["cash"], {"count": 1, "total": 30.1})
        self.assertEqual(report["by_tender"]["venmo"], {"count": 1, "total": 3})
        self.assertEqual(report["by_tender"]["check"], {"count": 0, "total": 0})
        self.assertEqual(report["by_day"][0]["tenders"], {"cash": 30.1, "check": 0, "venmo": 3})

    def test_single_item_totals(self):
        report = self.report(start="2026-11-01", end="2026-11-01")
        items = {i["name"]: i for i in report["items"]}
        self.assertEqual(items["Nuts"]["quantity"], 3)
        self.assertEqual(items["Nuts"]["total"], 13)
        self.assertEqual(items["Donation"]["total"], 20.1)
        self.assertEqual(items["Donation"]["department"], "Misc")
        self.assertNotIn("Rada", items)

    def test_item_totals_match_tender_totals(self):
        report = self.report()
        self.assertEqual(round(sum(i["total"] for i in report["items"]), 2), report["overall"]["total"])
        self.assertEqual(round(sum(d["total"] for d in report["by_department"]), 2), report["overall"]["total"])

    def test_cashier_totals(self):
        conn = pos.get_db_connection()
        conn.execute(
            "INSERT INTO cashiers (name, pin_hash) VALUES (?, ?)", ("Sam", pos.hash_pin("555555"))
        )
        conn.commit()
        conn.close()
        other = pos.app.test_client()
        other.post("/api/login", json={"code": "555555"})
        sale = other.post(
            "/api/transactions", json={"items": [{"id": 3, "price": 7.25}], "tender": "cash"}
        ).json
        self.backdate(sale["id"], "2026-11-02")

        report = self.report()
        self.assertEqual(
            report["by_cashier"],
            [
                {"cashier": "Pat", "count": 3, "refunds": 0, "total": 33.4},
                {"cashier": "Sam", "count": 1, "refunds": 0, "total": 7.25},
            ],
        )
        one_day = self.report(start="2026-11-01", end="2026-11-01")
        self.assertEqual(one_day["by_cashier"], [{"cashier": "Pat", "count": 2, "refunds": 0, "total": 33.1}])
        empty = self.report(start="2020-01-01", end="2020-01-01")
        self.assertEqual(empty["by_cashier"], [])

    def test_bad_dates_rejected(self):
        for params in [{"start": "nope"}, {"start": "2026-11-02", "end": "2026-11-01"}]:
            response = self.client.get("/api/reports/summary", query_string=params)
            self.assertEqual(response.status_code, 400)


class RefundTests(PosTestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.sale = self.sell(
            [{"id": 1, "price": 5, "quantity": 2}, {"id": 9, "price": 20}], "cash"
        ).json
        self.other_sale = self.sell([{"id": 1, "price": 3}], "venmo").json

    def refund(self, transaction_id=None):
        return self.client.post(f"/api/transactions/{transaction_id or self.sale['id']}/refund")

    def report(self):
        return self.client.get("/api/reports/summary").json

    def test_refund_creates_linked_negative_transaction(self):
        response = self.refund()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["total"], -30)
        self.assertEqual(response.json["refund_of"], self.sale["id"])

        transactions = {t["id"]: t for t in self.client.get("/api/transactions").json}
        original = transactions[self.sale["id"]]
        refund = transactions[response.json["id"]]
        self.assertEqual(original["total"], 30)
        self.assertEqual(original["refund_id"], refund["id"])
        self.assertEqual(original["refund_cashier"], "Pat")
        self.assertEqual(refund["tender"], "cash")
        self.assertEqual(refund["refund_of"], original["id"])
        self.assertEqual(
            [(i["name"], i["quantity"], i["price"]) for i in refund["items"]],
            [("Nuts", -2, 5), ("Donation", -1, 20)],
        )

    def test_refund_reduces_every_report_total(self):
        self.refund()
        report = self.report()
        self.assertEqual(report["overall"], {"count": 1, "total": 3})
        self.assertEqual(report["refunds"], {"count": 1, "total": -30})
        self.assertEqual(report["by_tender"]["cash"], {"count": 0, "total": 0})
        self.assertEqual(report["by_tender"]["venmo"], {"count": 1, "total": 3})
        self.assertEqual(report["by_day"][0]["refunds"], -30)
        self.assertEqual(report["by_day"][0]["tenders"]["cash"], 0)
        self.assertEqual(report["by_day"][0]["total"], 3)

        items = {i["name"]: i for i in report["items"]}
        self.assertEqual((items["Nuts"]["quantity"], items["Nuts"]["total"]), (1, 3))
        self.assertEqual((items["Donation"]["quantity"], items["Donation"]["total"]), (0, 0))
        departments = {d["department"]: d for d in report["by_department"]}
        self.assertEqual((departments["Food"]["quantity"], departments["Food"]["total"]), (1, 3))
        self.assertEqual((departments["Misc"]["quantity"], departments["Misc"]["total"]), (0, 0))
        self.assertEqual(report["by_cashier"], [{"cashier": "Pat", "count": 1, "refunds": -30, "total": 3}])

    def test_refund_counts_on_the_day_it_happened(self):
        self.backdate(self.sale["id"], "2026-11-01")
        refund = self.refund().json
        self.backdate(refund["id"], "2026-11-02")

        first = self.client.get("/api/reports/summary?start=2026-11-01&end=2026-11-01").json
        self.assertEqual(first["overall"], {"count": 1, "total": 30})
        self.assertEqual(first["refunds"], {"count": 0, "total": 0})

        second = self.client.get("/api/reports/summary?start=2026-11-02&end=2026-11-02").json
        self.assertEqual(second["refunds"], {"count": 1, "total": -30})
        self.assertEqual(second["by_tender"]["cash"], {"count": -1, "total": -30})

    def test_refund_shows_who_processed_it(self):
        self.client.post("/api/logout")
        self.login("99990000")
        response = self.refund()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["cashier"], "Admin")
        names = {c["cashier"]: c for c in self.report()["by_cashier"]}
        self.assertEqual(names["Pat"]["total"], 33)
        self.assertEqual(names["Admin"]["total"], -30)
        self.assertEqual(names["Admin"]["count"], -1)

    def test_cannot_refund_twice(self):
        self.assertEqual(self.refund().status_code, 201)
        self.assertEqual(self.refund().status_code, 409)
        self.assertEqual(self.report()["refunds"]["count"], 1)

    def test_cannot_refund_a_refund(self):
        refund = self.refund().json
        self.assertEqual(self.refund(refund["id"]).status_code, 400)

    def test_refund_missing_transaction(self):
        self.assertEqual(self.refund(99999).status_code, 404)

    def test_refund_requires_login(self):
        client = pos.app.test_client()
        response = client.post(f"/api/transactions/{self.sale['id']}/refund")
        self.assertEqual(response.status_code, 401)

    def test_transactions_filtered_by_date(self):
        self.backdate(self.sale["id"], "2026-11-01")
        response = self.client.get("/api/transactions?start=2026-11-01&end=2026-11-01")
        self.assertEqual([t["id"] for t in response.json], [self.sale["id"]])
        bad = self.client.get("/api/transactions?start=nope")
        self.assertEqual(bad.status_code, 400)

    def test_existing_database_is_upgraded(self):
        import sqlite3

        path = os.path.join(_tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
               tender TEXT NOT NULL, total REAL NOT NULL, user TEXT NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO transactions (timestamp, tender, total, user) VALUES ('2025-01-01T09:00:00', 'cash', 5, 'Old')"
        )
        conn.commit()
        conn.close()

        original = pos.DB_FILE
        pos.DB_FILE = path
        try:
            pos.init_db()
            pos.init_db()
            conn = pos.get_db_connection()
            row = conn.execute("SELECT total, refund_of FROM transactions").fetchone()
            conn.close()
        finally:
            pos.DB_FILE = original
        self.assertEqual((row["total"], row["refund_of"]), (5, None))


if __name__ == "__main__":
    unittest.main()
