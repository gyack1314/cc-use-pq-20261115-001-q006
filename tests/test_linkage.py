import csv
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from autumn_ledger.application import Ledger
from autumn_ledger.api.server import create_app
from autumn_ledger.demo import request_data, synthetic_event
from autumn_ledger.values import date_range
from tests.support import LedgerCase


class LinkageTests(LedgerCase):
    def ingest_beta(self, *events, batch="beta-orders"):
        return self.ledger.intake.ingest("beta", batch, list(events))

    def beta_coverage(self):
        for city in ("sz", "nj"):
            for day in date_range("2026-11-09", "2026-11-22"):
                self.ledger.coverage.put({"source": "beta", "city": city,
                                          "day": day.isoformat(), "status": "complete"})

    def submit(self, evidence_id="ev-1", members=(("alpha", "a1"), ("beta", "bd1")), **kwargs):
        payload = [{"source": source, "order_id": order_id} for source, order_id in members]
        return self.ledger.linkage.submit_evidence(evidence_id, payload, **kwargs)

    def both_request(self, **changes):
        return request_data(sources=["alpha", "beta"], **changes)

    def recon(self, **changes):
        result = self.ledger.analysis.run(self.both_request(**changes))
        return result, result["periods"]["current"]["sale_reconciliation"]

    def test_evidence_accepted_creates_pending_group_and_replay_is_free(self):
        self.ingest_beta(synthetic_event("bd1"))
        before = self.ledger.database.revision()
        result = self.submit()
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["replayed"])
        group_id = result["group_id"]
        self.assertEqual(self.ledger.database.revision(), before + 1)
        group = self.ledger.linkage.get_group(group_id)
        self.assertEqual(group["status"], "pending")
        self.assertEqual(len(group["members"]), 2)
        replay = self.submit()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["group_id"], group_id)
        self.assertEqual(self.ledger.database.revision(), before + 1)
        self.assertEqual(self.ledger.linkage.list_groups()["total"], 1)
        actions = [row["action"] for row in self.ledger.database.audit()]
        self.assertEqual(actions.count("linkage.group"), 1)

    def test_same_evidence_id_with_different_content_conflicts(self):
        self.ingest_beta(synthetic_event("bd1"), synthetic_event("bd2"), batch="beta-two")
        self.submit()
        error = self.assertError("conflict", self.submit, "ev-1", (("alpha", "a1"), ("beta", "bd2")))
        self.assertEqual(error.status, 409)
        self.assertEqual(self.ledger.linkage.list_groups()["total"], 1)

    def test_one_group_holds_at_most_one_order_per_source(self):
        error = self.assertError("source_repeated", self.submit, "ev-x",
                                 (("alpha", "a1"), ("alpha", "a2")))
        self.assertEqual(error.status, 422)
        self.assertEqual(self.ledger.linkage.list_groups()["total"], 0)

    def test_evidence_requires_existing_orders(self):
        self.assertError("not_found", self.submit, "ev-x", (("alpha", "a1"), ("beta", "ghost")))
        self.assertEqual(self.ledger.linkage.list_groups()["total"], 0)

    def test_conflicting_evidence_is_held_not_grouped(self):
        self.ingest_beta(synthetic_event("bd1"), batch="beta-one")
        first = self.submit("ev-1", (("alpha", "a1"), ("beta", "bd1")))
        held = self.submit("ev-2", (("beta", "bd1"), ("alpha", "a2")))
        self.assertEqual(held["status"], "held")
        self.assertIsNone(held["group_id"])
        self.assertEqual(self.ledger.linkage.list_groups()["total"], 1)
        self.assertEqual([row["id"] for row in self.ledger.linkage.list_evidence("held")], ["ev-2"])
        self.assertError("conflict", self.ledger.linkage.settle, "ev-2", "accept")
        self.ledger.linkage.dissolve(first["group_id"], "reconciler", "原证据有误")
        settled = self.ledger.linkage.settle("ev-2", "accept", "reconciler", "确认同团")
        self.assertEqual(settled["status"], "accepted")
        self.assertIsNotNone(settled["group_id"])
        self.assertEqual(self.ledger.linkage.list_groups(status="dissolved")["total"], 1)
        self.assertEqual(self.ledger.linkage.list_groups(status="pending")["total"], 1)

    def test_held_evidence_can_be_rejected_once(self):
        self.ingest_beta(synthetic_event("bd1"), batch="beta-one")
        self.submit("ev-1", (("alpha", "a1"), ("beta", "bd1")))
        self.submit("ev-2", (("beta", "bd1"), ("alpha", "a2")))
        rejected = self.ledger.linkage.settle("ev-2", "reject", "reconciler", "证据不足")
        self.assertEqual(rejected["status"], "rejected")
        self.assertError("invalid_transition", self.ledger.linkage.settle, "ev-2", "accept")
        self.assertError("invalid_transition", self.ledger.linkage.settle, "ev-1", "reject")

    def test_same_membership_submitted_twice_is_duplicate_not_new_group(self):
        self.ingest_beta(synthetic_event("bd1"))
        first = self.submit("ev-1")
        again = self.submit("ev-2")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["group_id"], first["group_id"])
        self.assertEqual(self.ledger.linkage.list_groups()["total"], 1)

    def test_adoption_deduplicates_and_explains_difference(self):
        self.beta_coverage()
        self.ingest_beta(synthetic_event("bd1", "2026-11-16", gross=51000))
        group_id = self.submit()["group_id"]
        self.ledger.linkage.adopt(group_id, "alpha", "reconciler", "采信平台账")
        result, recon = self.recon()
        self.assertEqual(recon["source_view"]["totals"]["orders"], 4)
        self.assertEqual(recon["dedup_view"]["totals"]["orders"], 3)
        self.assertEqual(recon["difference"]["measures"]["orders"], 1)
        self.assertEqual(recon["difference"]["measures"]["net_cents"], 51000)
        excluded = recon["difference"]["excluded"][0]
        self.assertEqual(excluded["reason"], "duplicate_of_adopted")
        self.assertEqual((excluded["source"], excluded["order_id"]), ("beta", "bd1"))
        self.assertTrue(any("重复" in text for text in recon["difference"]["explanations"]))
        group = recon["groups"][0]
        self.assertEqual(group["status"], "adopted")
        self.assertEqual(group["counted"], {"source": "alpha", "order_id": "a1", "sequence": 1})
        self.assertEqual({item["field"] for item in group["differences"]}, {"gross_cents"})
        sale = next(row for row in result["sale_lineage"] if row["sale_group"] == group_id)
        self.assertEqual(sale["basis"], "adopted")
        self.assertEqual(sale["adoption_revision"], group["decision"]["revision"])
        self.assertTrue(result["quality"]["publishable"])
        self.assertTrue(result["statistical_checks"]["ok"])

    def test_unresolved_group_previewable_but_not_publishable(self):
        self.beta_coverage()
        self.ingest_beta(synthetic_event("bd1", "2026-11-16"))
        group_id = self.submit()["group_id"]
        result, recon = self.recon()
        self.assertEqual(recon["source_view"]["totals"]["orders"], 4)
        self.assertEqual(recon["dedup_view"]["totals"]["orders"], 2)
        self.assertEqual(recon["unresolved_groups"], [group_id])
        self.assertFalse(result["quality"]["publishable"])
        codes = {row["code"] for row in result["quality"]["issues"]}
        self.assertIn("unresolved_reconciliation", codes)
        preview = self.ledger.analysis.preview(self.both_request())
        self.assertTrue(preview["unresolved_reconciliation"])
        edition = self.ledger.editions.create("pending-edition", self.both_request())
        self.assertError("quality_blocked", self.ledger.review.decide,
                         edition["id"], "approve", edition["version"], "analyst", "尝试通过")

    def test_held_evidence_blocks_publish_until_settled(self):
        self.beta_coverage()
        self.ingest_beta(synthetic_event("bd1", "2026-11-16"), synthetic_event("bd2", "2026-11-17"),
                         batch="beta-two")
        group_id = self.submit("ev-1", (("alpha", "a1"), ("beta", "bd1")))["group_id"]
        self.ledger.linkage.adopt(group_id, "alpha")
        held = self.submit("ev-2", (("beta", "bd1"), ("alpha", "a2")))
        self.assertEqual(held["status"], "held")
        result, recon = self.recon()
        self.assertEqual(recon["unresolved_groups"], [])
        self.assertEqual([row["id"] for row in recon["held_evidence"]], ["ev-2"])
        self.assertFalse(result["quality"]["publishable"])
        self.assertEqual(recon["dedup_view"]["totals"]["orders"], 4)
        preview = self.ledger.analysis.preview(self.both_request())
        self.assertTrue(preview["unresolved_reconciliation"])
        edition = self.ledger.editions.create("held-edition", self.both_request())
        self.ledger.linkage.settle("ev-2", "reject", "reconciler", "证据不足")
        settled, _ = self.recon()
        self.assertTrue(settled["quality"]["publishable"])
        self.assertTrue(self.ledger.editions.verify(edition["id"])["ok"])
        stored = self.ledger.editions.get(edition["id"])["result"]["periods"]["current"]["sale_reconciliation"]
        self.assertEqual(stored["held_evidence"][0]["status"], "held")
        self.assertIsNone(stored["held_evidence"][0]["settled_revision"])

    def test_adopted_revision_change_returns_group_to_review(self):
        self.beta_coverage()
        self.ingest_beta(synthetic_event("bd1", "2026-11-16"))
        group_id = self.submit()["group_id"]
        self.ledger.linkage.adopt(group_id, "alpha")
        edition = self.ledger.editions.create("adopted", self.both_request())
        self.ledger.intake.ingest("alpha", "a1-refund", [synthetic_event("a1", sequence=2, refund=5000)])
        self.assertEqual(self.ledger.linkage.get_group(group_id)["status"], "review")
        result, recon = self.recon()
        self.assertEqual(recon["unresolved_groups"], [group_id])
        self.assertEqual(recon["dedup_view"]["totals"]["orders"], 2)
        self.assertFalse(result["quality"]["publishable"])
        self.assertTrue(self.ledger.editions.verify(edition["id"])["ok"])
        stored = self.ledger.editions.get(edition["id"])["result"]["periods"]["current"]["sale_reconciliation"]
        self.assertEqual(stored["groups"][0]["status"], "adopted")
        self.assertEqual(stored["dedup_view"]["totals"]["orders"], 3)
        self.ledger.linkage.adopt(group_id, "alpha", "reconciler", "采信第二版")
        result2, recon2 = self.recon()
        self.assertTrue(result2["quality"]["publishable"])
        self.assertEqual(recon2["dedup_view"]["totals"]["orders"], 3)
        self.assertEqual(recon2["dedup_view"]["totals"]["refund_cents"], 5000)

    def test_revoke_and_dissolve_restore_recomputed_reports(self):
        self.beta_coverage()
        self.ingest_beta(synthetic_event("bd1", "2026-11-16"))
        group_id = self.submit()["group_id"]
        self.ledger.linkage.adopt(group_id, "alpha")
        _, recon = self.recon()
        self.assertEqual(recon["dedup_view"]["totals"]["orders"], 3)
        self.ledger.linkage.revoke(group_id, "reconciler", "撤销采信")
        self.assertEqual(self.ledger.linkage.get_group(group_id)["status"], "pending")
        _, recon = self.recon()
        self.assertEqual(recon["dedup_view"]["totals"]["orders"], 2)
        self.ledger.linkage.dissolve(group_id, "reconciler", "证据有误")
        self.assertEqual(self.ledger.linkage.get_group(group_id)["status"], "dissolved")
        result, recon = self.recon()
        self.assertEqual(recon["dedup_view"]["totals"]["orders"], 4)
        self.assertEqual(recon["groups"], [])
        self.assertTrue(result["quality"]["publishable"])
        history = self.ledger.linkage.group_history(group_id)
        self.assertEqual([row["action"] for row in history["decisions"]], ["adopt", "revoke"])
        self.assertEqual(history["evidence"][0]["id"], "ev-1")

    def test_adoption_uses_one_complete_revision_without_field_stitching(self):
        self.beta_coverage()
        changed = synthetic_event("bd1", "2026-11-16", gross=61000, refund=1000)
        changed["adults"] = 3
        self.ingest_beta(changed)
        group_id = self.submit()["group_id"]
        self.ledger.linkage.adopt(group_id, "beta", "reconciler", "采信代理账")
        _, recon = self.recon()
        sale = next(row for row in recon["dedup_view"]["sales"] if row["sale_group"] == group_id)
        head = self.ledger.intake.history("beta", "bd1")[-1]
        for field in ("event_id", "sequence", "state", "origin", "destination", "travel_day",
                      "family", "adults", "children", "gross_cents", "refund_cents"):
            self.assertEqual(sale[field], head[field])
        self.assertEqual(recon["dedup_view"]["totals"]["gross_cents"], 131000)
        self.assertEqual(recon["dedup_view"]["totals"]["travelers"], 10)
        self.assertEqual(recon["source_view"]["totals"]["gross_cents"], 181000)

    def test_adopted_member_outside_request_sources_explains_exclusion(self):
        self.beta_coverage()
        self.ingest_beta(synthetic_event("bd1", "2026-11-16"))
        group_id = self.submit()["group_id"]
        self.ledger.linkage.adopt(group_id, "beta", "reconciler", "采信代理账")
        result = self.ledger.analysis.run(request_data())
        recon = result["periods"]["current"]["sale_reconciliation"]
        self.assertEqual(recon["source_view"]["totals"]["orders"], 3)
        self.assertEqual(recon["dedup_view"]["totals"]["orders"], 2)
        excluded = recon["difference"]["excluded"][0]
        self.assertEqual(excluded["reason"], "adopted_out_of_scope")
        self.assertTrue(result["quality"]["publishable"])

    def test_adopt_revoke_dissolve_validation(self):
        self.ingest_beta(synthetic_event("bd1"))
        group_id = self.submit()["group_id"]
        self.assertError("not_a_member", self.ledger.linkage.adopt, group_id, "gamma")
        self.assertError("conflict", self.ledger.linkage.adopt, group_id, "alpha",
                         "reconciler", "说明", 99)
        self.assertError("no_active_adoption", self.ledger.linkage.revoke, group_id)
        adopted = self.ledger.linkage.adopt(group_id, "alpha", "reconciler", "说明", 1)
        self.assertEqual(adopted["status"], "adopted")
        self.ledger.linkage.dissolve(group_id)
        self.assertError("invalid_transition", self.ledger.linkage.dissolve, group_id)
        self.assertError("invalid_transition", self.ledger.linkage.adopt, group_id, "alpha")
        self.assertError("invalid_transition", self.ledger.linkage.revoke, group_id)
        self.assertError("not_found", self.ledger.linkage.get_group, "grp-missing")

    def test_snapshot_replay_shows_historical_group_state(self):
        self.ingest_beta(synthetic_event("bd1"))
        group_id = self.submit()["group_id"]
        self.ledger.linkage.adopt(group_id, "alpha")
        adopted_revision = self.ledger.database.revision()
        self.ledger.linkage.revoke(group_id)
        historical = self.ledger.linkage.get_group(group_id, revision=adopted_revision)
        self.assertEqual(historical["status"], "adopted")
        self.assertEqual(historical["decision"]["action"], "adopt")
        self.assertEqual(self.ledger.linkage.get_group(group_id)["status"], "pending")
        listed = self.ledger.linkage.list_groups(status="adopted", revision=adopted_revision)
        self.assertEqual([row["id"] for row in listed["items"]], [group_id])

    def test_parallel_evidence_submission_creates_exactly_one_group(self):
        self.ingest_beta(synthetic_event("bd1"))
        members = [{"source": "alpha", "order_id": "a1"}, {"source": "beta", "order_id": "bd1"}]

        def submit(_):
            return Ledger(self.path).linkage.submit_evidence("ev-parallel", members)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(4)))
        self.assertEqual(sum(not row["replayed"] for row in results), 1)
        self.assertEqual(len({row["group_id"] for row in results}), 1)
        self.assertEqual(self.ledger.linkage.list_groups()["total"], 1)
        self.assertTrue(self.ledger.database.integrity()["ok"])

    def test_export_bundle_contains_consistent_sales_and_lineage(self):
        self.beta_coverage()
        self.ingest_beta(synthetic_event("bd1", "2026-11-16", gross=51000))
        group_id = self.submit()["group_id"]
        self.ledger.linkage.adopt(group_id, "alpha")
        edition = self.ledger.editions.create("dedup-edition", self.both_request())
        rows = list(csv.DictReader(io.StringIO(self.ledger.export.sales(edition["id"]))))
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(int(row["orders"]) for row in rows), 3)
        self.assertEqual(sum(int(row["travelers"]) for row in rows), 9)
        groups = list(csv.DictReader(io.StringIO(self.ledger.export.sale_groups(edition["id"]))))
        self.assertEqual(groups[0]["group_id"], group_id)
        self.assertEqual(groups[0]["status"], "adopted")
        self.assertEqual(groups[0]["adopted_source"], "alpha")
        self.assertEqual(groups[0]["differing_fields"], "gross_cents")
        bundle = self.ledger.export.bundle(edition["id"])
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            names = archive.namelist()
            self.assertIn("current/sales.csv", names)
            self.assertIn("current/sale_groups.csv", names)
            self.assertIn("sale_lineage.csv", names)
            document = json.loads(archive.read("edition.json"))
            sales_rows = list(csv.DictReader(io.StringIO(archive.read("current/sales.csv").decode("utf-8"))))
            lineage_rows = list(csv.DictReader(io.StringIO(archive.read("sale_lineage.csv").decode("utf-8"))))
        dedup = document["result"]["periods"]["current"]["sale_reconciliation"]["dedup_view"]["totals"]
        self.assertEqual(sum(int(row["orders"]) for row in sales_rows), dedup["orders"])
        self.assertEqual(sum(int(row["travelers"]) for row in sales_rows), dedup["travelers"])
        self.assertEqual(sum(int(row["net_cents"]) for row in sales_rows), dedup["net_cents"])
        adopted_rows = [row for row in lineage_rows if row["sale_group"] == group_id]
        self.assertEqual(len(adopted_rows), 1)
        self.assertEqual(adopted_rows[0]["source"], "alpha")
        self.assertTrue(self.ledger.export.verify_bundle(bundle)["ok"])

    def test_explain_exposes_dedup_view_and_difference(self):
        self.beta_coverage()
        self.ingest_beta(synthetic_event("bd1", "2026-11-16"))
        group_id = self.submit()["group_id"]
        self.ledger.linkage.adopt(group_id, "alpha")
        edition = self.ledger.editions.create("explained", self.both_request())
        explained = self.ledger.editions.explain(edition["id"])
        current = explained["periods"]["current"]
        self.assertEqual(current["totals"]["orders"], 4)
        self.assertEqual(current["dedup_totals"]["orders"], 3)
        self.assertEqual(current["unresolved_groups"], [])
        self.assertTrue(current["difference_explanations"])


class LinkageApiTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.path)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_http_linkage_flow(self):
        ingest = self.client.post("/sources/beta/batches",
                                  json={"batch_id": "b1", "events": [synthetic_event("bd1", "2026-11-16")]})
        self.assertEqual(ingest.status_code, 200, ingest.text)
        submission = {"evidence_id": "ev-http", "actor": "reconciler", "note": "同团销售",
                      "members": [{"source": "alpha", "order_id": "a1"},
                                  {"source": "beta", "order_id": "bd1"}]}
        created = self.client.post("/reconciliation/evidence", json=submission)
        self.assertEqual(created.status_code, 200, created.text)
        group_id = created.json()["group_id"]
        replay = self.client.post("/reconciliation/evidence", json=submission).json()
        self.assertTrue(replay["replayed"])
        conflict = self.client.post("/reconciliation/evidence",
                                    json={**submission, "members": [{"source": "alpha", "order_id": "a2"},
                                                                    {"source": "beta", "order_id": "bd1"}]})
        self.assertEqual(conflict.status_code, 409)
        adopted = self.client.post(f"/reconciliation/groups/{group_id}/adopt",
                                   json={"source": "alpha", "actor": "reconciler"})
        self.assertEqual(adopted.status_code, 200, adopted.text)
        self.assertEqual(adopted.json()["status"], "adopted")
        group = self.client.get(f"/reconciliation/groups/{group_id}").json()
        self.assertEqual(group["decision"]["source"], "alpha")
        history = self.client.get(f"/reconciliation/groups/{group_id}/history").json()
        self.assertEqual([row["action"] for row in history["decisions"]], ["adopt"])
        listed = self.client.get("/reconciliation/groups", params={"status": "adopted"}).json()
        self.assertEqual(listed["total"], 1)
        evidence = self.client.get("/reconciliation/evidence/ev-http").json()
        self.assertEqual(evidence["status"], "accepted")
        bad = self.client.post("/reconciliation/evidence",
                               json={"evidence_id": "x", "members": [{"source": "alpha", "order_id": "a1"}]})
        self.assertEqual(bad.status_code, 422)
        missing = self.client.get("/reconciliation/groups/grp-missing")
        self.assertEqual(missing.status_code, 404)
