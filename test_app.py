import os
import re
import unittest

# These tests empty every table, so they only run against a database set aside for them
if not os.environ.get("TEST_DATABASE_URL"):
    raise unittest.SkipTest("Set TEST_DATABASE_URL to a throwaway Postgres database to run these tests")
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["SECRET_KEY"] = "test-secret"
os.environ["PUBLIC_URL"] = "https://sales.example.com"
os.environ.pop("SMTP_HOST", None)

import app as pos

OWNER_PASSWORD = "owner-password-1"
ORGANIZER_PASSWORD = "organizer-pass-1"


def sql(query, params=()):
    with pos.get_db_connection() as conn:
        cursor = conn.execute(query, params)
        return cursor.fetchall() if cursor.description else None


class PosTestCase(unittest.TestCase):
    MODE = "charity"

    def setUp(self):
        sql("TRUNCATE events, users, invites, password_resets, login_failures RESTART IDENTITY CASCADE")
        # Every email the app sends lands here instead
        self.outbox = []
        pos.mail.deliver = self.outbox.append
        pos.create_user("owner@example.com", "Owen", OWNER_PASSWORD, is_owner=True)
        pos.create_user("olive@example.com", "Olive", ORGANIZER_PASSWORD)
        self.owner = self.user_client("owner@example.com", OWNER_PASSWORD)
        # Most tests act as an ordinary organizer who owns the event
        self.admin = self.user_client("olive@example.com", ORGANIZER_PASSWORD)
        self.event = self.admin.post("/api/events", json={"name": "Drive", "mode": self.MODE}).json
        self.base = f"/api/events/{self.event['id']}"
        self.admin.post(f"{self.base}/cashiers", json={"name": "Pat", "pin": "123456"})
        self.methods = {m["label"]: m["id"] for m in self.admin.get(self.base).json["payment_methods"]}

    def user_client(self, email, password):
        client = pos.app.test_client()
        response = client.post("/api/login", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 200, response.json)
        return client

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
            {"email": "olive@example.com", "password": "wrong-password"},
            {"email": "nobody@example.com", "password": ORGANIZER_PASSWORD},
            {"email": "olive@example.com", "password": 12345},
            {"code": "99990000"},
        ]
        for body in bodies:
            self.assertEqual(client.post("/api/login", json=body).status_code, 401, body)
            # There are more bad bodies than the lockout allows
            sql("DELETE FROM login_failures")

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
        self.assertEqual(self.admin.get("/api/login-blocks").status_code, 403)
        blocks = self.owner.get("/api/login-blocks").json
        self.assertEqual([(b["ip"], b["banned"]) for b in blocks], [(self.VISITOR, True)])
        self.assertEqual(self.owner.delete(f"/api/login-blocks/{self.VISITOR}").status_code, 204)
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
        self.assertEqual((refund.json["total_cents"], refund.json["cashier"]), (-1000, "Olive"))
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



def link_token(message, prefix):
    """Pull the token out of a link like https://sales.example.com/<prefix>/<token> in an email."""
    body = message.get_body(("plain",)).get_content()
    match = re.search(rf"{re.escape(os.environ['PUBLIC_URL'])}/{prefix}/(\S+)", body)
    assert match, body
    return match.group(1)


def attachments(message):
    return {part.get_filename(): part.get_content() for part in message.iter_attachments()}


class AccountTests(PosTestCase):
    def test_session_shows_organizer(self):
        session = self.admin.get("/api/session").json
        self.assertEqual(
            {k: session[k] for k in ["role", "name", "email", "is_owner"]},
            {"role": "organizer", "name": "Olive", "email": "olive@example.com", "is_owner": False},
        )
        self.assertTrue(self.owner.get("/api/session").json["is_owner"])

    def test_email_is_not_case_sensitive(self):
        self.user_client(" Olive@Example.com ", ORGANIZER_PASSWORD)

    def test_passwords_are_hashed(self):
        stored = sql("SELECT password_hash FROM users WHERE email = 'olive@example.com'")[0]["password_hash"]
        self.assertNotIn(ORGANIZER_PASSWORD, stored)

    def test_wrong_passwords_count_toward_lockout(self):
        client = pos.app.test_client()
        for _ in range(pos.MAX_LOGIN_FAILURES):
            client.post("/api/login", json={"email": "olive@example.com", "password": "nope-nope-nope"})
        response = client.post("/api/login", json={"email": "olive@example.com", "password": ORGANIZER_PASSWORD})
        self.assertEqual(response.status_code, 429)

    def test_create_owner_command(self):
        runner = pos.app.test_cli_runner()
        result = runner.invoke(args=["create-owner", "boss@example.com", "--name", "Boss"],
                               input="boss-password-1\nboss-password-1\n")
        self.assertEqual(result.exit_code, 0, result.output)
        client = self.user_client("boss@example.com", "boss-password-1")
        self.assertTrue(client.get("/api/session").json["is_owner"])

        # Running it again for an existing account resets the password
        result = runner.invoke(args=["create-owner", "boss@example.com"], input="new-password-22\nnew-password-22\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.user_client("boss@example.com", "new-password-22")

    def test_create_owner_rejects_short_password(self):
        result = pos.app.test_cli_runner().invoke(args=["create-owner", "boss@example.com"], input="short\nshort\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(sql("SELECT count(*) AS n FROM users WHERE email = 'boss@example.com'")[0]["n"], 0)


class OwnershipTests(PosTestCase):
    def setUp(self):
        super().setUp()
        pos.create_user("rita@example.com", "Rita", "rita-password-1")
        self.rita = self.user_client("rita@example.com", "rita-password-1")

    def test_organizer_sees_only_their_own_events(self):
        mine = self.rita.post("/api/events", json={"name": "Rita's", "mode": "yard_sale"}).json
        self.assertEqual([e["id"] for e in self.rita.get("/api/events").json], [mine["id"]])
        self.assertEqual([e["id"] for e in self.admin.get("/api/events").json], [self.event["id"]])
        for path in ["", "/sales", "/report", "/cashiers"]:
            self.assertEqual(self.rita.get(f"{self.base}{path}").status_code, 404, path)
        self.assertEqual(self.rita.post(f"{self.base}/close").status_code, 404)
        self.assertEqual(self.rita.delete(self.base).status_code, 404)

    def test_owner_sees_every_event(self):
        self.rita.post("/api/events", json={"name": "Rita's", "mode": "yard_sale"})
        self.assertEqual(len(self.owner.get("/api/events").json), 2)
        self.assertEqual(self.owner.get(f"{self.base}/report").status_code, 200)

    def test_events_list_shows_owner(self):
        listed = self.owner.get("/api/events").json[0]
        self.assertEqual(listed["owner_email"], "olive@example.com")

    def test_organizer_cannot_manage_site(self):
        for path in ["/api/invites", "/api/users", "/api/login-blocks"]:
            self.assertEqual(self.admin.get(path).status_code, 403, path)
        self.assertEqual(self.admin.post("/api/invites", json={"email": "x@example.com"}).status_code, 403)


class InviteTests(PosTestCase):
    def invite(self, email="new@example.com"):
        response = self.owner.post("/api/invites", json={"email": email})
        self.assertEqual(response.status_code, 201, response.json)
        return link_token(self.outbox[-1], "invite")

    def test_invite_email_and_accept(self):
        token = self.invite()
        self.assertEqual(self.outbox[-1]["To"], "new@example.com")
        self.assertEqual(pos.app.test_client().get(f"/api/invites/accept/{token}").json["email"], "new@example.com")

        client = pos.app.test_client()
        response = client.post(f"/api/invites/accept/{token}", json={"name": "Nina", "password": "nina-password-1"})
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(client.get("/api/session").json["name"], "Nina")
        self.assertFalse(client.get("/api/session").json["is_owner"])
        self.user_client("new@example.com", "nina-password-1")

        # Each invite works once
        again = pos.app.test_client().post(f"/api/invites/accept/{token}", json={"name": "X", "password": "another-pass-1"})
        self.assertEqual(again.status_code, 404)

    def test_pending_invites_listed_and_cancelled(self):
        self.invite()
        pending = self.owner.get("/api/invites").json
        self.assertEqual([i["email"] for i in pending], ["new@example.com"])
        self.assertNotIn("token", pending[0])
        self.assertEqual(self.owner.delete(f"/api/invites/{pending[0]['id']}").status_code, 204)
        self.assertEqual(self.owner.get("/api/invites").json, [])

    def test_cancelled_or_expired_invite_does_not_work(self):
        token = self.invite()
        sql("UPDATE invites SET expires_at = now() - interval '1 minute'")
        self.assertEqual(pos.app.test_client().get(f"/api/invites/accept/{token}").status_code, 404)
        self.assertEqual(pos.app.test_client().get("/api/invites/accept/made-up-token").status_code, 404)

    def test_invalid_invites_rejected(self):
        for email in ["", "not-an-email", "olive@example.com"]:
            self.assertEqual(self.owner.post("/api/invites", json={"email": email}).status_code,
                             409 if email == "olive@example.com" else 400, email)

    def test_short_password_rejected_on_accept(self):
        token = self.invite()
        response = pos.app.test_client().post(f"/api/invites/accept/{token}", json={"name": "Nina", "password": "short"})
        self.assertEqual(response.status_code, 400)

    def test_users_listed_for_owner(self):
        users = self.owner.get("/api/users").json
        self.assertEqual(sorted(u["email"] for u in users), ["olive@example.com", "owner@example.com"])
        self.assertNotIn("password_hash", users[0])


class PasswordResetTests(PosTestCase):
    def request_reset(self, email="olive@example.com"):
        response = pos.app.test_client().post("/api/password-reset", json={"email": email})
        self.assertEqual(response.status_code, 200)

    def test_reset_password_by_email(self):
        self.request_reset("OLIVE@example.com")
        token = link_token(self.outbox[-1], "reset")
        client = pos.app.test_client()
        self.assertEqual(client.post(f"/api/password-reset/{token}", json={"password": "brand-new-pass-1"}).status_code, 200)
        self.user_client("olive@example.com", "brand-new-pass-1")
        failed = pos.app.test_client().post("/api/login", json={"email": "olive@example.com", "password": ORGANIZER_PASSWORD})
        self.assertEqual(failed.status_code, 401)
        # Each link works once
        self.assertEqual(client.post(f"/api/password-reset/{token}", json={"password": "another-pass-12"}).status_code, 400)

    def test_unknown_email_gets_same_answer_and_no_email(self):
        self.request_reset("nobody@example.com")
        self.assertEqual(self.outbox, [])

    def test_expired_link_rejected(self):
        self.request_reset()
        token = link_token(self.outbox[-1], "reset")
        sql("UPDATE password_resets SET expires_at = now() - interval '1 minute'")
        response = pos.app.test_client().post(f"/api/password-reset/{token}", json={"password": "brand-new-pass-1"})
        self.assertEqual(response.status_code, 400)


class CloseEventTests(PosTestCase):
    MODE = "yard_sale"

    def setUp(self):
        super().setUp()
        self.mary = self.add("sellers", name="Mary")
        self.paul = self.add("sellers", name="Paul")
        self.sell(self.cashier(), [{"seller_id": self.mary, "price_cents": 1000},
                                   {"seller_id": self.paul, "price_cents": 500, "description": 'Chair, "blue"'}])

    def close(self):
        response = self.admin.post(f"{self.base}/close")
        self.assertEqual(response.status_code, 200)
        return response.json

    def test_close_sets_purge_date_six_months_out(self):
        event = self.close()
        closed = pos.datetime.fromisoformat(event["closed_at"])
        purge = pos.datetime.fromisoformat(event["purge_after"])
        self.assertTrue(180 <= (purge - closed).days <= 184)
        self.assertIsNone(self.admin.post(f"{self.base}/reopen").json["purge_after"])

    def test_close_emails_owner_with_csvs_and_link(self):
        self.close()
        self.assertEqual(len(self.outbox), 1)
        message = self.outbox[0]
        self.assertEqual(message["To"], "olive@example.com")
        self.assertIn("Drive", message["Subject"])
        files = attachments(message)
        self.assertEqual(set(files), {"sales.csv", "payouts.csv"})
        self.assertIn('"Chair, ""blue"""', files["sales.csv"].decode() if isinstance(files["sales.csv"], bytes) else files["sales.csv"])
        payouts = files["payouts.csv"]
        payouts = payouts.decode() if isinstance(payouts, bytes) else payouts
        self.assertIn("Cash box,Mary,10.00", payouts)
        self.assertIn("/r/", message.get_body(("plain",)).get_content())

    def test_charity_close_has_no_payouts_csv(self):
        charity = self.admin.post("/api/events", json={"name": "C", "mode": "charity"}).json
        self.admin.post(f"/api/events/{charity['id']}/close")
        self.assertEqual(set(attachments(self.outbox[-1])), {"sales.csv"})

    def test_closing_twice_sends_one_email(self):
        self.close()
        self.close()
        self.assertEqual(len(self.outbox), 1)

    def test_failed_email_does_not_stop_close(self):
        def broken(message):
            raise OSError("mail server down")
        pos.mail.deliver = broken
        self.assertEqual(self.close()["status"], "closed")


class SharedReportTests(CloseEventTests):
    def shared_token(self):
        self.close()
        return link_token(self.outbox[-1], "r")

    def test_link_shows_report_without_login(self):
        token = self.shared_token()
        client = pos.app.test_client()
        response = client.get(f"/api/shared/{token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["event"]["name"], "Drive")
        self.assertEqual(response.json["report"]["overall"]["total_cents"], 1500)
        self.assertEqual(len(response.json["settlement"]["transfers"]), 2)
        self.assertIn("purge_after", response.json["event"])
        csv = client.get(f"/api/shared/{token}/sales.csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("text/csv", csv.headers["Content-Type"])
        self.assertEqual(client.get(f"/api/shared/{token}/payouts.csv").status_code, 200)
        self.assertEqual(client.get(f"/api/shared/{token}/other.csv").status_code, 404)

    def test_link_stops_after_purge_date(self):
        token = self.shared_token()
        sql("UPDATE events SET purge_after = now() - interval '1 minute'")
        self.assertEqual(pos.app.test_client().get(f"/api/shared/{token}").status_code, 404)

    def test_link_stops_when_reopened(self):
        token = self.shared_token()
        self.admin.post(f"{self.base}/reopen")
        self.assertEqual(pos.app.test_client().get(f"/api/shared/{token}").status_code, 404)

    def test_tampered_link_rejected(self):
        token = self.shared_token()
        self.assertEqual(pos.app.test_client().get(f"/api/shared/{token[:-2]}xx").status_code, 404)

    def test_organizer_can_get_csv_while_logged_in(self):
        response = self.admin.get(f"{self.base}/sales.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Mary", response.get_data(as_text=True))
        self.assertEqual(self.cashier().get(f"{self.base}/sales.csv").status_code, 403)


class PurgeTests(PosTestCase):
    def run_purge(self):
        result = pos.app.test_cli_runner().invoke(args=["purge-expired"])
        self.assertEqual(result.exit_code, 0, result.output)
        return result.output

    def test_expired_events_are_deleted_with_everything_in_them(self):
        pie = self.add("items", name="Pie")
        self.sell(self.cashier(), [{"item_id": pie, "price_cents": 500}])
        keep = self.admin.post("/api/events", json={"name": "Keep", "mode": "charity"}).json
        self.admin.post(f"{self.base}/close")
        sql("UPDATE events SET purge_after = now() - interval '1 day' WHERE id = %s", (self.event["id"],))

        self.assertIn("Deleted 1 event", self.run_purge())
        self.assertEqual([r["id"] for r in sql("SELECT id FROM events")], [keep["id"]])
        for table in ["items", "cashiers", "sales", "sale_lines"]:
            self.assertEqual(sql(f"SELECT count(*) AS n FROM {table}")[0]["n"], 0, table)

    def test_not_yet_expired_events_are_kept(self):
        self.admin.post(f"{self.base}/close")
        self.run_purge()
        self.assertEqual(len(sql("SELECT id FROM events")), 1)

    def test_idle_open_event_is_closed_and_emailed(self):
        sql("UPDATE events SET created_at = now() - interval '61 days'")
        self.run_purge()
        event = sql("SELECT status, purge_after FROM events")[0]
        self.assertEqual(event["status"], "closed")
        self.assertIsNotNone(event["purge_after"])
        self.assertEqual(len(self.outbox), 1)

    def test_event_with_recent_sales_stays_open(self):
        pie = self.add("items", name="Pie")
        self.sell(self.cashier(), [{"item_id": pie, "price_cents": 500}])
        sql("UPDATE events SET created_at = now() - interval '90 days'")
        self.run_purge()
        self.assertEqual(sql("SELECT status FROM events")[0]["status"], "open")

    def test_expired_invites_and_resets_are_cleared(self):
        self.owner.post("/api/invites", json={"email": "new@example.com"})
        pos.app.test_client().post("/api/password-reset", json={"email": "olive@example.com"})
        sql("UPDATE invites SET expires_at = now() - interval '1 day'")
        sql("UPDATE password_resets SET expires_at = now() - interval '1 day'")
        self.run_purge()
        self.assertEqual(sql("SELECT count(*) AS n FROM invites")[0]["n"], 0)
        self.assertEqual(sql("SELECT count(*) AS n FROM password_resets")[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()
