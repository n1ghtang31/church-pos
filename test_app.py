import os
import tempfile
import unittest

_tmp = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_tmp.name, "test.db")
os.environ["ADMIN_CODE"] = "9999"
os.environ["SECRET_KEY"] = "test-secret"

import app as pos


class PosTestCase(unittest.TestCase):
    def setUp(self):
        pos.login_failures.clear()
        conn = pos.get_db_connection()
        conn.execute("DELETE FROM transaction_items")
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM cashiers")
        conn.execute(
            "INSERT INTO cashiers (name, pin_hash) VALUES (?, ?)", ("Pat", pos.hash_pin("1234"))
        )
        conn.commit()
        conn.close()
        self.client = pos.app.test_client()

    def login(self, code="1234"):
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

    def test_lockout_after_repeated_failures(self):
        for _ in range(pos.MAX_LOGIN_FAILURES):
            self.client.post("/api/login", json={"code": "0000"})
        response = self.client.post("/api/login", json={"code": "1234"})
        self.assertEqual(response.status_code, 429)


class CashierTests(PosTestCase):
    def setUp(self):
        super().setUp()
        self.login("9999")

    def cashier_client(self, code):
        client = pos.app.test_client()
        return client, client.post("/api/login", json={"code": code})

    def cashier_id(self, name):
        return next(c["id"] for c in self.client.get("/api/cashiers").json if c["name"] == name)

    def test_admin_code_logs_in_as_admin(self):
        self.assertEqual(self.client.get("/api/session").json["role"], "admin")

    def test_cashier_cannot_manage_cashiers(self):
        client, response = self.cashier_client("1234")
        self.assertEqual(response.json["name"], "Pat")
        self.assertEqual(client.get("/api/cashiers").status_code, 403)
        self.assertEqual(client.post("/api/cashiers", json={"name": "Eve", "pin": "5555"}).status_code, 403)
        self.assertEqual(client.delete(f"/api/cashiers/{self.cashier_id('Pat')}").status_code, 403)

    def test_logged_out_cannot_manage_cashiers(self):
        self.assertEqual(pos.app.test_client().get("/api/cashiers").status_code, 401)

    def test_admin_cannot_ring_up_sales(self):
        self.assertEqual(self.sell([{"id": 1, "price": 5}]).status_code, 403)

    def test_create_cashier_and_log_in(self):
        response = self.client.post("/api/cashiers", json={"name": " Sam ", "pin": "5555"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["name"], "Sam")
        self.assertNotIn("pin", response.json)
        self.assertNotIn("pin_hash", response.json)
        _, login = self.cashier_client("5555")
        self.assertEqual(login.json["role"], "cashier")

    def test_invalid_cashiers_rejected(self):
        bad = [
            {"name": "", "pin": "5555"},
            {"name": "Sam", "pin": "123"},
            {"name": "Sam", "pin": "12345678901"},
            {"name": "Sam", "pin": "abcd"},
            {"name": "Sam", "pin": 5555},
            {"name": "Sam", "pin": "9999"},
            {"name": "Sam"},
        ]
        for body in bad:
            self.assertEqual(self.client.post("/api/cashiers", json=body).status_code, 400, body)

    def test_duplicates_rejected(self):
        self.assertEqual(self.client.post("/api/cashiers", json={"name": "PAT", "pin": "5555"}).status_code, 409)
        self.assertEqual(self.client.post("/api/cashiers", json={"name": "Sam", "pin": "1234"}).status_code, 409)

    def test_edit_name_keeps_pin(self):
        cashier_id = self.cashier_id("Pat")
        response = self.client.put(f"/api/cashiers/{cashier_id}", json={"name": "Patricia"})
        self.assertEqual(response.json["name"], "Patricia")
        _, login = self.cashier_client("1234")
        self.assertEqual(login.json["name"], "Patricia")

    def test_edit_pin_replaces_old_pin(self):
        cashier_id = self.cashier_id("Pat")
        self.assertEqual(self.client.put(f"/api/cashiers/{cashier_id}", json={"pin": "7777"}).status_code, 200)
        self.assertEqual(self.cashier_client("1234")[1].status_code, 401)
        self.assertEqual(self.cashier_client("7777")[1].status_code, 200)

    def test_edit_missing_cashier(self):
        self.assertEqual(self.client.put("/api/cashiers/99999", json={"name": "Nobody"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/cashiers/99999").status_code, 404)

    def test_remove_logs_cashier_out_and_keeps_their_sales(self):
        client, _ = self.cashier_client("1234")
        client.post("/api/transactions", json={"items": [{"id": 1, "price": 5}], "tender": "cash"})

        self.assertEqual(self.client.delete(f"/api/cashiers/{self.cashier_id('Pat')}").status_code, 204)

        self.assertEqual(client.get("/api/items").status_code, 401)
        self.assertFalse(client.get("/api/session").json["authenticated"])
        self.assertEqual(self.cashier_client("1234")[1].status_code, 401)
        sales = self.client.get("/api/transactions").json
        self.assertEqual(sales[0]["cashier"], "Pat")


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
            "INSERT INTO cashiers (name, pin_hash) VALUES (?, ?)", ("Sam", pos.hash_pin("5555"))
        )
        conn.commit()
        conn.close()
        other = pos.app.test_client()
        other.post("/api/login", json={"code": "5555"})
        sale = other.post(
            "/api/transactions", json={"items": [{"id": 3, "price": 7.25}], "tender": "cash"}
        ).json
        self.backdate(sale["id"], "2026-11-02")

        report = self.report()
        self.assertEqual(
            report["by_cashier"],
            [
                {"cashier": "Pat", "count": 3, "total": 33.4},
                {"cashier": "Sam", "count": 1, "total": 7.25},
            ],
        )
        one_day = self.report(start="2026-11-01", end="2026-11-01")
        self.assertEqual(one_day["by_cashier"], [{"cashier": "Pat", "count": 2, "total": 33.1}])
        empty = self.report(start="2020-01-01", end="2020-01-01")
        self.assertEqual(empty["by_cashier"], [])

    def test_bad_dates_rejected(self):
        for params in [{"start": "nope"}, {"start": "2026-11-02", "end": "2026-11-01"}]:
            response = self.client.get("/api/reports/summary", query_string=params)
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
