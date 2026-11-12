import os
import unittest

# These tests empty every table, so they only run against a database set aside for them
if not os.environ.get("TEST_DATABASE_URL"):
    raise unittest.SkipTest("Set TEST_DATABASE_URL to a throwaway Postgres database to run these tests")
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["ADMIN_CODE"] = "99990000"
os.environ["SECRET_KEY"] = "test-secret"

import app as pos

ADMIN = "99990000"


def sql(query, params=()):
    with pos.get_db_connection() as conn:
        cursor = conn.execute(query, params)
        return cursor.fetchall() if cursor.description else None


class PosTestCase(unittest.TestCase):
    MODE = "charity"

    def setUp(self):
        sql("TRUNCATE events, login_failures RESTART IDENTITY CASCADE")
        self.admin = self.client_for(ADMIN)
        self.event = self.admin.post("/api/events", json={"name": "Drive", "mode": self.MODE}).json
        self.base = f"/api/events/{self.event['id']}"
        self.admin.post(f"{self.base}/cashiers", json={"name": "Pat", "pin": "123456"})
        self.methods = {m["label"]: m["id"] for m in self.admin.get(self.base).json["payment_methods"]}

    def client_for(self, code, event_code=None):
        client = pos.app.test_client()
        response = client.post("/api/login", json={"code": code, "event_code": event_code})
        self.assertEqual(response.status_code, 200, response.json)
        return client

    def cashier(self, pin="123456", event=None):
        return self.client_for(pin, (event or self.event)["code"])

    def add(self, setup_kind, **body):
        response = self.admin.post(f"{self.base}/{setup_kind}", json=body)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["id"]

    def sell(self, client, lines, method="Cash", **extra):
        return client.post(
            f"{self.base}/sales", json={"lines": lines, "payment_method_id": self.methods[method], **extra}
        )

    def backdate(self, sale_id, day):
        sql("UPDATE sales SET created_at = %s::date + interval '12 hours' WHERE id = %s", (day, sale_id))


class AuthTests(PosTestCase):
    def test_api_requires_login(self):
        client = pos.app.test_client()
        for path in ["/api/events", self.base, f"{self.base}/sales", f"{self.base}/report"]:
            self.assertEqual(client.get(path).status_code, 401, path)

    def test_health_is_public(self):
        self.assertEqual(pos.app.test_client().get("/api/health").status_code, 200)

    def test_wrong_codes_rejected(self):
        client = pos.app.test_client()
        bodies = [
            {"code": "0000"},
            {"code": 1234},
            {},
            [],
            {"code": "123456"},
            {"code": "123456", "event_code": "NOPE"},
        ]
        for body in bodies:
            self.assertEqual(client.post("/api/login", json=body).status_code, 401, body)

    def test_cashier_pin_only_works_at_their_event(self):
        other = self.admin.post("/api/events", json={"name": "Other", "mode": "charity"}).json
        response = pos.app.test_client().post(
            "/api/login", json={"code": "123456", "event_code": other["code"]}
        )
        self.assertEqual(response.status_code, 401)

    def test_event_code_is_case_insensitive(self):
        self.cashier(event={"code": self.event["code"].lower()})

    def test_login_and_logout(self):
        client = self.cashier()
        self.assertEqual(client.get("/api/session").json["event_id"], self.event["id"])
        client.post("/api/logout")
        self.assertEqual(client.get(self.base).status_code, 401)

    def test_private_address_locked_out_for_a_while(self):
        client = pos.app.test_client()
        for _ in range(pos.MAX_LOGIN_FAILURES):
            client.post("/api/login", json={"code": "0000"})
        response = client.post("/api/login", json={"code": "123456", "event_code": self.event["code"]})
        self.assertEqual(response.status_code, 429)
        sql("UPDATE login_failures SET locked_until = 0")
        self.cashier()


class EventTests(PosTestCase):
    def test_new_event_has_code_and_default_payment_methods(self):
        self.assertRegex(self.event["code"], r"^[A-Z2-9]{6}$")
        self.assertEqual(list(self.methods), ["Cash", "Check", "Venmo"])
        yard = self.admin.post("/api/events", json={"name": "Yard", "mode": "yard_sale"}).json
        labels = [m["label"] for m in self.admin.get(f"/api/events/{yard['id']}").json["payment_methods"]]
        self.assertEqual(labels, ["Cash"])

    def test_invalid_events_rejected(self):
        for body in [{"name": "", "mode": "charity"}, {"name": "X", "mode": "auction"},
                     {"name": "X", "mode": "charity", "timezone": "Mars/Base"}]:
            self.assertEqual(self.admin.post("/api/events", json=body).status_code, 400, body)

    def test_cashier_sees_only_their_event(self):
        other = self.admin.post("/api/events", json={"name": "Other", "mode": "charity"}).json
        client = self.cashier()
        self.assertEqual([e["id"] for e in client.get("/api/events").json], [self.event["id"]])
        for path in ["", "/sales", "/report"]:
            self.assertEqual(client.get(f"/api/events/{other['id']}{path}").status_code, 404, path)
        self.assertEqual(client.post("/api/events", json={"name": "X", "mode": "charity"}).status_code, 403)

    def test_cashier_cannot_change_setup(self):
        client = self.cashier()
        self.assertEqual(client.post(f"{self.base}/items", json={"name": "Pie"}).status_code, 403)
        self.assertEqual(client.get(f"{self.base}/cashiers").status_code, 403)
        self.assertEqual(client.post(f"{self.base}/close").status_code, 403)
        self.assertEqual(client.delete(self.base).status_code, 403)

    def test_items_only_at_charity_and_sellers_only_at_yard_sale(self):
        self.assertEqual(self.admin.post(f"{self.base}/sellers", json={"name": "Mary"}).status_code, 400)
        self.assertEqual(
            self.admin.post(f"{self.base}/payment-methods",
                            json={"label": "Venmo", "kind": "venmo", "holder_seller_id": 1}).status_code,
            400,
        )
        self.assertEqual(self.admin.post(f"{self.base}/unknown", json={}).status_code, 404)

    def test_removed_item_used_in_a_sale_is_hidden_not_deleted(self):
        pie = self.add("items", name="Pie", department="Food")
        unused = self.add("items", name="Unused")
        self.sell(self.cashier(), [{"item_id": pie, "price_cents": 500}])
        self.assertEqual(self.admin.delete(f"{self.base}/items/{pie}").status_code, 204)
        self.assertEqual(self.admin.delete(f"{self.base}/items/{unused}").status_code, 204)
        self.assertEqual(self.admin.get(self.base).json["items"], [])
        self.assertEqual(sql("SELECT name, active FROM items"), [{"name": "Pie", "active": False}])
        self.assertEqual(self.admin.get(f"{self.base}/report").json["lines"][0]["name"], "Pie")

    def test_closed_event_refuses_sales(self):
        pie = self.add("items", name="Pie")
        client = self.cashier()
        sale = self.sell(client, [{"item_id": pie, "price_cents": 500}]).json
        self.assertEqual(self.admin.post(f"{self.base}/close").json["status"], "closed")
        self.assertEqual(self.sell(client, [{"item_id": pie, "price_cents": 500}]).status_code, 409)
        self.assertEqual(client.post(f"{self.base}/sales/{sale['id']}/refund").status_code, 409)
        self.assertEqual(self.admin.post(f"{self.base}/reopen").json["status"], "open")
        self.assertEqual(self.sell(client, [{"item_id": pie, "price_cents": 500}]).status_code, 201)

    def test_deleting_event_removes_everything(self):
        pie = self.add("items", name="Pie")
        self.sell(self.cashier(), [{"item_id": pie, "price_cents": 500}])
        self.assertEqual(self.admin.delete(self.base).status_code, 204)
        for table in ["events", "items", "cashiers", "payment_methods", "sales", "sale_lines"]:
            self.assertEqual(sql(f"SELECT count(*) AS n FROM {table}")[0]["n"], 0, table)


class CashierTests(PosTestCase):
    def cashier_id(self, name):
        return next(c["id"] for c in self.admin.get(f"{self.base}/cashiers").json if c["name"] == name)

    def test_create_cashier_and_log_in(self):
        response = self.admin.post(f"{self.base}/cashiers", json={"name": " Sam ", "pin": "4321"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json, {"id": response.json["id"], "name": "Sam"})
        self.assertEqual(self.cashier("4321").get("/api/session").json["name"], "Sam")

    def test_invalid_cashiers_rejected(self):
        bad = [
            {"name": "", "pin": "555555"},
            {"name": "Sam", "pin": "123"},
            {"name": "Sam", "pin": "123456789"},
            {"name": "Sam", "pin": "abcd"},
            {"name": "Sam", "pin": 5555},
            {"name": "Sam", "pin": ADMIN},
            {"name": "Sam"},
        ]
        for body in bad:
            self.assertEqual(self.admin.post(f"{self.base}/cashiers", json=body).status_code, 400, body)

    def test_duplicates_rejected_within_an_event_only(self):
        response = self.admin.post(f"{self.base}/cashiers", json={"name": "PAT", "pin": "555555"})
        self.assertEqual((response.status_code, response.json["error"]), (409, "A cashier with that name already exists"))
        response = self.admin.post(f"{self.base}/cashiers", json={"name": "Sam", "pin": "123456"})
        self.assertEqual((response.status_code, response.json["error"]), (409, "That PIN is not available"))

        other = self.admin.post("/api/events", json={"name": "Other", "mode": "charity"}).json
        response = self.admin.post(f"/api/events/{other['id']}/cashiers", json={"name": "Pat", "pin": "123456"})
        self.assertEqual(response.status_code, 201)

    def test_edit_name_keeps_pin_and_edit_pin_replaces_it(self):
        cashier_id = self.cashier_id("Pat")
        self.admin.put(f"{self.base}/cashiers/{cashier_id}", json={"name": "Patricia"})
        self.assertEqual(self.cashier().get("/api/session").json["name"], "Patricia")
        self.admin.put(f"{self.base}/cashiers/{cashier_id}", json={"name": "Patricia", "pin": "777777"})
        failed = pos.app.test_client().post("/api/login", json={"code": "123456", "event_code": self.event["code"]})
        self.assertEqual(failed.status_code, 401)
        self.cashier("777777")

    def test_remove_logs_cashier_out_and_keeps_their_sales(self):
        pie = self.add("items", name="Pie")
        client = self.cashier()
        self.sell(client, [{"item_id": pie, "price_cents": 500}])
        self.assertEqual(self.admin.delete(f"{self.base}/cashiers/{self.cashier_id('Pat')}").status_code, 204)
        self.assertEqual(client.get(self.base).status_code, 401)
        self.assertEqual(self.admin.get(f"{self.base}/sales").json[0]["cashier"], "Pat")


class BanTests(PosTestCase):
    VISITOR = "1.1.1.1"

    def attempt(self, code="123456", ip=VISITOR):
        return pos.app.test_client().post(
            "/api/login", json={"code": code, "event_code": self.event["code"]}, environ_base={"REMOTE_ADDR": ip}
        )

    def fail(self, times, ip=VISITOR):
        return [self.attempt("000000", ip).status_code for _ in range(times)]

    def test_public_address_banned_after_ten_wrong_codes(self):
        codes = self.fail(pos.MAX_LOGIN_FAILURES)
        self.assertEqual(codes, [401] * (pos.MAX_LOGIN_FAILURES - 1) + [403])
        self.assertEqual(self.attempt().status_code, 403)
        self.assertEqual(self.attempt(ip="2.2.2.2").status_code, 200)

    def test_correct_code_resets_count_and_old_codes_are_forgotten(self):
        self.fail(pos.MAX_LOGIN_FAILURES - 1)
        self.assertEqual(self.attempt().status_code, 200)
        self.fail(pos.MAX_LOGIN_FAILURES - 1)
        sql("UPDATE login_failures SET last_failure = 0")
        self.assertEqual(self.fail(pos.MAX_LOGIN_FAILURES - 1)[-1], 401)

    def test_admin_lists_and_unblocks(self):
        self.fail(pos.MAX_LOGIN_FAILURES)
        blocks = self.admin.get("/api/login-blocks").json
        self.assertEqual([(b["ip"], b["banned"]) for b in blocks], [(self.VISITOR, True)])
        self.assertEqual(self.admin.delete(f"/api/login-blocks/{self.VISITOR}").status_code, 204)
        self.assertEqual(self.attempt().status_code, 200)
        self.assertEqual(self.cashier().get("/api/login-blocks").status_code, 403)

    def test_command_line_unban(self):
        self.fail(pos.MAX_LOGIN_FAILURES)
        runner = pos.app.test_cli_runner()
        self.assertIn(self.VISITOR, runner.invoke(args=["bans"]).output)
        self.assertIn("Unblocked 1", runner.invoke(args=["unban", self.VISITOR]).output)
        self.assertIn("No addresses are blocked", runner.invoke(args=["bans"]).output)

    def test_trusted_proxy_uses_the_entry_it_added(self):
        pos.TRUSTED_PROXY_IPS = [pos.ipaddress.ip_network("10.0.0.0/24")]
        try:
            client = pos.app.test_client()
            for _ in range(pos.MAX_LOGIN_FAILURES):
                client.post("/api/login", json={"code": "0"}, headers={"X-Forwarded-For": "9.9.9.9, 1.1.1.1"},
                            environ_base={"REMOTE_ADDR": "10.0.0.5"})
            self.assertEqual(self.attempt().status_code, 403)
        finally:
            pos.TRUSTED_PROXY_IPS = []

    def test_security_headers_and_size_limit(self):
        client = pos.app.test_client()
        response = client.get("/api/health")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        big = client.post("/api/login", data="x" * 100_000, content_type="application/json")
        self.assertEqual(big.status_code, 413)


class CharitySaleTests(PosTestCase):
    def setUp(self):
        super().setUp()
        self.nuts = self.add("items", name="Nuts", department="Food")
        self.donation = self.add("items", name="Donation", department="Misc")
        self.client = self.cashier()

    def test_sale_uses_entered_amount_and_server_names(self):
        response = self.sell(self.client, [{"item_id": self.donation, "price_cents": 3750, "description": ""}])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["total_cents"], 3750)
        sale = self.client.get(f"{self.base}/sales").json[0]
        self.assertEqual((sale["cashier"], sale["payment_method"]), ("Pat", "Cash"))
        self.assertEqual(sale["lines"][0]["description"], "Donation")

    def test_admin_cannot_ring_up_sales(self):
        self.assertEqual(self.sell(self.admin, [{"item_id": self.nuts, "price_cents": 500}]).status_code, 403)

    def test_invalid_sales_rejected_and_not_saved(self):
        other = self.admin.post("/api/events", json={"name": "Other", "mode": "charity"}).json
        other_item = self.admin.post(f"/api/events/{other['id']}/items", json={"name": "X"}).json["id"]
        bad_lines = [
            [],
            "nope",
            [{"item_id": 999, "price_cents": 500}],
            [{"item_id": other_item, "price_cents": 500}],
            [{"item_id": True, "price_cents": 500}],
            [{"item_id": self.nuts, "price_cents": 0}],
            [{"item_id": self.nuts, "price_cents": -5}],
            [{"item_id": self.nuts, "price_cents": 5.5}],
            [{"item_id": self.nuts, "price_cents": "5"}],
            [{"item_id": self.nuts}],
            [{"item_id": self.nuts, "price_cents": 500, "quantity": 0}],
            [{"item_id": self.nuts, "price_cents": pos.MAX_CENTS + 1}],
        ]
        for lines in bad_lines:
            self.assertEqual(self.sell(self.client, lines).status_code, 400, lines)
        good = [{"item_id": self.nuts, "price_cents": 500}]
        self.assertEqual(self.sell(self.client, good, total_cents=600).status_code, 400)
        response = self.client.post(f"{self.base}/sales", json={"lines": good, "payment_method_id": 999})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(f"{self.base}/sales").json, [])

    def test_report_totals_by_day_method_item_and_department(self):
        first = self.sell(self.client, [{"item_id": self.nuts, "price_cents": 500, "quantity": 2},
                                        {"item_id": self.donation, "price_cents": 2010}]).json
        second = self.sell(self.client, [{"item_id": self.nuts, "price_cents": 300}], "Venmo").json
        self.backdate(first["id"], "2026-11-01")
        self.backdate(second["id"], "2026-11-02")

        report = self.client.get(f"{self.base}/report").json
        self.assertEqual(report["overall"], {"count": 2, "total_cents": 3310, "discount_cents": 0})
        self.assertEqual([d["date"] for d in report["by_day"]], ["2026-11-01", "2026-11-02"])
        methods = {m["label"]: m["total_cents"] for m in report["by_method"]}
        self.assertEqual(methods, {"Cash": 3010, "Venmo": 300})
        items = {i["name"]: (i["quantity"], i["total_cents"]) for i in report["lines"]}
        self.assertEqual(items, {"Nuts": (3, 1300), "Donation": (1, 2010)})
        departments = {d["department"]: d["total_cents"] for d in report["by_department"]}
        self.assertEqual(departments, {"Food": 1300, "Misc": 2010})
        self.assertEqual(report["by_cashier"], [{"cashier": "Pat", "count": 2, "total_cents": 3310, "refund_cents": 0}])

        one_day = self.client.get(f"{self.base}/report?start=2026-11-02&end=2026-11-02").json
        self.assertEqual(one_day["overall"]["total_cents"], 300)
        listed = self.client.get(f"{self.base}/sales?start=2026-11-01&end=2026-11-01").json
        self.assertEqual([s["id"] for s in listed], [first["id"]])
        self.assertEqual(self.client.get(f"{self.base}/report?start=nope").status_code, 400)

    def test_refund(self):
        sale = self.sell(self.client, [{"item_id": self.nuts, "price_cents": 500, "quantity": 2}]).json
        refund = self.admin.post(f"{self.base}/sales/{sale['id']}/refund")
        self.assertEqual(refund.status_code, 201)
        self.assertEqual((refund.json["total_cents"], refund.json["cashier"]), (-1000, "Admin"))
        self.assertEqual(self.admin.post(f"{self.base}/sales/{sale['id']}/refund").status_code, 409)
        self.assertEqual(self.admin.post(f"{self.base}/sales/{refund.json['id']}/refund").status_code, 400)
        self.assertEqual(self.admin.post(f"{self.base}/sales/99999/refund").status_code, 404)

        sales = {s["id"]: s for s in self.client.get(f"{self.base}/sales").json}
        self.assertEqual(sales[sale["id"]]["refund_id"], refund.json["id"])
        self.assertEqual(sales[refund.json["id"]]["lines"][0]["quantity"], -2)
        report = self.client.get(f"{self.base}/report").json
        self.assertEqual(report["overall"]["count"], 0)
        self.assertEqual(report["overall"]["total_cents"], 0)
        self.assertEqual(report["refunds"], {"count": 1, "total_cents": -1000})


class YardSaleTests(PosTestCase):
    MODE = "yard_sale"

    def setUp(self):
        super().setUp()
        self.johnny = self.add("sellers", name="Johnny")
        self.paul = self.add("sellers", name="Paul")
        self.mary = self.add("sellers", name="Mary")
        self.methods["Venmo (Mary)"] = self.add(
            "payment-methods", label="Venmo (Mary)", kind="venmo", holder_seller_id=self.mary
        )
        self.client = self.cashier()

    def lot(self):
        return [
            {"seller_id": self.johnny, "price_cents": 1000, "description": "Lamp"},
            {"seller_id": self.paul, "price_cents": 1500},
            {"seller_id": self.mary, "price_cents": 2500},
        ]

    def settlement(self):
        response = self.client.get(f"{self.base}/settlement")
        self.assertEqual(response.status_code, 200)
        return response.json

    def test_offer_for_the_lot_is_split_across_sellers(self):
        response = self.sell(self.client, self.lot(), "Venmo (Mary)", total_cents=3000)
        self.assertEqual(response.status_code, 201)
        self.assertEqual((response.json["subtotal_cents"], response.json["total_cents"]), (5000, 3000))
        self.assertEqual([line["final_cents"] for line in response.json["lines"]], [600, 900, 1500])
        self.assertEqual(response.json["lines"][0]["description"], "Lamp")
        self.assertEqual(self.client.get(f"{self.base}/report").json["overall"]["discount_cents"], 2000)

    def test_settlement_venmo_holder_pays_other_sellers(self):
        self.sell(self.client, self.lot(), "Venmo (Mary)", total_cents=3000)
        self.sell(self.client, [{"seller_id": self.johnny, "price_cents": 400}], "Cash")

        result = self.settlement()
        sellers = {s["name"]: (s["sold_cents"], s["held_cents"]) for s in result["sellers"]}
        self.assertEqual(sellers, {"Johnny": (1000, 0), "Paul": (900, 0), "Mary": (1500, 3000)})
        self.assertEqual(result["cash_box_cents"], 400)
        self.assertEqual(
            [(t["from"], t["to"], t["cents"]) for t in result["transfers"]],
            [("Cash box", "Johnny", 400), ("Mary", "Johnny", 600), ("Mary", "Paul", 900)],
        )

    def test_refund_comes_out_of_settlement(self):
        sale = self.sell(self.client, self.lot(), "Cash").json
        self.sell(self.client, [{"seller_id": self.paul, "price_cents": 700}], "Cash")
        self.client.post(f"{self.base}/sales/{sale['id']}/refund")
        result = self.settlement()
        self.assertEqual(result["cash_box_cents"], 700)
        self.assertEqual([(t["from"], t["to"], t["cents"]) for t in result["transfers"]], [("Cash box", "Paul", 700)])

    def test_payment_method_holder_must_be_a_seller_here(self):
        other = self.admin.post("/api/events", json={"name": "Other", "mode": "yard_sale"}).json
        foreign = self.admin.post(f"/api/events/{other['id']}/sellers", json={"name": "Zed"}).json["id"]
        for holder in [foreign, 99999, "1"]:
            response = self.admin.post(
                f"{self.base}/payment-methods", json={"label": "V", "kind": "venmo", "holder_seller_id": holder}
            )
            self.assertEqual(response.status_code, 400, holder)

    def test_seller_with_a_payment_method_cannot_be_removed(self):
        self.assertEqual(self.admin.delete(f"{self.base}/sellers/{self.mary}").status_code, 409)
        self.admin.delete(f"{self.base}/payment-methods/{self.methods['Venmo (Mary)']}")
        self.assertEqual(self.admin.delete(f"{self.base}/sellers/{self.mary}").status_code, 204)

    def test_removed_seller_still_settled(self):
        self.sell(self.client, [{"seller_id": self.paul, "price_cents": 700}], "Cash")
        self.admin.delete(f"{self.base}/sellers/{self.paul}")
        self.assertEqual([s["name"] for s in self.client.get(self.base).json["sellers"]], ["Johnny", "Mary"])
        self.assertEqual([t["to"] for t in self.settlement()["transfers"]], ["Paul"])

    def test_items_refused_and_settlement_only_for_yard_sales(self):
        self.assertEqual(self.sell(self.client, [{"item_id": 1, "price_cents": 100}]).status_code, 400)
        charity = self.admin.post("/api/events", json={"name": "C", "mode": "charity"}).json
        self.assertEqual(self.admin.get(f"/api/events/{charity['id']}/settlement").status_code, 400)


if __name__ == "__main__":
    unittest.main()
