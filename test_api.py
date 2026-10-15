"""API tests - run with:  python -m unittest test_api -v   (uses a temporary database)"""
import json
import os
import tempfile
import unittest

from api_server import create_app
from hl7_to_fhir import SAMPLES


class ApiTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.client = create_app(self.path).test_client()

    def tearDown(self):
        os.remove(self.path)

    def post(self, key):
        return self.client.post("/hl7", data=SAMPLES[key], content_type="text/plain")

    def test_ingest_clean_message(self):
        r = self.post("clean_inpatient_admission")
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertEqual({c["type"] for c in body["resources"]}, {"Patient", "Encounter"})
        self.assertEqual(body["quality"]["consistencyAfterPct"], 100.0)

    def test_normalization_score(self):
        q = self.post("messy_local_values").get_json()["quality"]
        self.assertEqual((q["consistencyBeforePct"], q["consistencyAfterPct"]), (0.0, 100.0))

    def test_bad_message_rejected_with_operation_outcome(self):
        r = self.post("bad_data_rejected")
        self.assertEqual(r.status_code, 422)
        body = json.loads(r.data)
        self.assertEqual(body["resourceType"], "OperationOutcome")
        self.assertTrue(any("PID-7" in i.get("expression", []) for i in body["issue"]))
        # nothing stored
        self.assertEqual(self.client.get("/fhir/Patient").get_json()["total"], 0)

    def test_versioning_and_idempotency(self):
        self.post("clean_inpatient_admission")
        again = self.post("clean_inpatient_admission").get_json()
        self.assertTrue(all(c["change"] == "unchanged" for c in again["resources"]))
        upd = self.post("discharge_a08_update").get_json()  # same patient + visit, now discharged
        enc = next(c for c in upd["resources"] if c["type"] == "Encounter")
        self.assertEqual((enc["change"], enc["version"]), ("updated", 2))
        hist = self.client.get(f"/fhir/Encounter/{enc['id']}/_history").get_json()
        self.assertEqual([e["resource"]["meta"]["versionId"] for e in hist["entry"]], ["2", "1"])
        current = self.client.get(f"/fhir/Encounter/{enc['id']}").get_json()
        self.assertEqual(current["status"], "finished")

    def test_search_and_everything(self):
        self.post("clean_inpatient_admission")
        self.post("messy_local_values")
        pats = self.client.get("/fhir/Patient?family=doe").get_json()
        self.assertEqual(pats["total"], 1)
        pid = pats["entry"][0]["resource"]["id"]
        self.assertEqual(self.client.get("/fhir/Patient?gender=female").get_json()["total"], 2)
        self.assertEqual(self.client.get("/fhir/Patient?identifier=10002").get_json()["total"], 1)
        encs = self.client.get(f"/fhir/Encounter?patient={pid}&status=in-progress").get_json()
        self.assertEqual(encs["total"], 1)
        every = self.client.get(f"/fhir/Patient/{pid}/$everything").get_json()
        self.assertEqual(every["total"], 2)

    def test_not_found_and_bad_input(self):
        self.assertEqual(self.client.get("/fhir/Patient/nope").status_code, 404)
        self.assertEqual(self.client.post("/hl7", data="").status_code, 400)
        self.assertEqual(self.client.post("/hl7", data="hello").status_code, 422)

    def test_stats_audit_and_docs(self):
        for key in SAMPLES:
            self.post(key)
        s = self.client.get("/stats").get_json()
        self.assertEqual((s["messagesReceived"], s["accepted"], s["rejected"]), (4, 3, 1))
        self.assertEqual(s["patients"], 2)
        self.assertTrue(s["consistencyAfterPct"] > s["consistencyBeforePct"])
        self.assertEqual(len(self.client.get("/messages").get_json()), 4)
        self.assertEqual(self.client.get("/fhir/metadata").get_json()["resourceType"],
                         "CapabilityStatement")
        self.assertIn("swagger", self.client.get("/docs").get_data(as_text=True).lower())
        self.assertIn("/hl7", self.client.get("/openapi.json").get_json()["paths"])


if __name__ == "__main__":
    unittest.main()
