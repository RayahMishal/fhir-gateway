"""
FHIR Gateway - a small FHIR-style REST API that ingests HL7 v2 ADT messages.

POST an HL7 v2 admission message  ->  it is validated, normalized and converted to
FHIR R4 Patient / Encounter resources, stored in SQLite with version history, and an
audit record with a semantic-consistency score is kept for every message.

Endpoints (interactive docs with "Try it out" at  http://127.0.0.1:5000/docs ):
  POST /hl7                          ingest an HL7 v2 message (text/plain or JSON)
  GET  /fhir/metadata                CapabilityStatement
  GET  /fhir/Patient/<id>            read      GET /fhir/Patient?family=&identifier=&gender=
  GET  /fhir/Patient/<id>/_history   all versions
  GET  /fhir/Patient/<id>/$everything  patient + encounters
  GET  /fhir/Encounter/<id>          read      GET /fhir/Encounter?patient=&status=&class=
  GET  /messages                     audit log of every message received
  GET  /stats                        data-quality dashboard numbers

All patient data in the samples is FAKE. This is a learning project: it follows a draft
HL7 v2-to-FHIR guide and is not validated or secured for real patient data.
"""
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request

from hl7_to_fhir import SUPPORTED_EVENTS, convert, msh, pv1

FHIR_JSON = "application/fhir+json"
HERE = os.path.dirname(os.path.abspath(__file__))


def create_app(db_path=None):
    app = Flask(__name__)
    app.json.sort_keys = False
    app.config["DB_PATH"] = db_path or os.path.join(HERE, "fhir_gateway.db")
    init_db(app.config["DB_PATH"])
    register_routes(app)
    return app


# ----------------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    type TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
    body TEXT NOT NULL, last_updated TEXT NOT NULL, control_id TEXT,
    PRIMARY KEY (type, id, version));
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL,
    control_id TEXT, msg_type TEXT, status TEXT NOT NULL,
    coded_total INTEGER, as_is INTEGER, normalized INTEGER, unmapped INTEGER,
    issues TEXT NOT NULL, raw TEXT NOT NULL);
"""


def init_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def connect(app):
    conn = sqlite3.connect(app.config["DB_PATH"], timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def current(conn, rtype, rid=None):
    """Latest version of one resource, or of all resources of a type."""
    sql = ("SELECT r.* FROM resources r JOIN (SELECT type, id, MAX(version) v FROM resources "
           "GROUP BY type, id) m ON r.type=m.type AND r.id=m.id AND r.version=m.v WHERE r.type=?")
    args = [rtype]
    if rid:
        sql += " AND r.id=?"
        args.append(rid)
    rows = conn.execute(sql, args).fetchall()
    return rows[0] if rid and rows else (None if rid else rows)


def with_meta(row):
    res = json.loads(row["body"])
    res["meta"] = {"versionId": str(row["version"]), "lastUpdated": row["last_updated"]}
    return res


def save_resource(conn, resource, control_id):
    """Insert a new version only if the content changed. Returns (version, change)."""
    rtype, rid = resource["resourceType"], resource["id"]
    body = json.dumps(resource, sort_keys=True)
    row = current(conn, rtype, rid)
    if row is None:
        conn.execute("INSERT INTO resources VALUES (?,?,?,?,?,?)",
                     (rtype, rid, 1, body, now(), control_id))
        return 1, "created"
    if json.dumps(json.loads(row["body"]), sort_keys=True) == body:
        return row["version"], "unchanged"
    conn.execute("INSERT INTO resources VALUES (?,?,?,?,?,?)",
                 (rtype, rid, row["version"] + 1, body, now(), control_id))
    return row["version"] + 1, "updated"


# ----------------------------------------------------------------------------------
# FHIR helpers
# ----------------------------------------------------------------------------------
def fhir(body, status=200):
    return Response(json.dumps(body, indent=2), status=status, mimetype=FHIR_JSON)


def outcome(status, code, diagnostics, expression=None, severity="error"):
    issue = {"severity": severity, "code": code, "diagnostics": diagnostics}
    if expression:
        issue["expression"] = [expression]
    return fhir({"resourceType": "OperationOutcome", "issue": [issue]}, status)


def searchset(rows, base):
    entries = []
    for row in rows:
        res = with_meta(row)
        entries.append({"fullUrl": f"{base}/{res['resourceType']}/{res['id']}", "resource": res})
    return {"resourceType": "Bundle", "type": "searchset", "total": len(entries), "entry": entries}


def base_url():
    return request.host_url.rstrip("/") + "/fhir"


# ----------------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------------
def register_routes(app):
    @app.get("/")
    def index():
        return jsonify({
            "service": "FHIR Gateway (HL7 v2 -> FHIR R4)",
            "docs": request.host_url + "docs",
            "try": "POST an HL7 v2 ADT message to /hl7",
            "supported_events": sorted("ADT^" + e for e in SUPPORTED_EVENTS)})

    # ---- ingest -------------------------------------------------------------------
    @app.post("/hl7")
    def ingest():
        if request.is_json:
            raw = (request.get_json(silent=True) or {}).get("message", "")
        else:
            raw = request.get_data(as_text=True)
        if not raw.strip():
            return outcome(400, "required", "Send an HL7 v2 message as text/plain, "
                           "or as JSON {\"message\": \"...\"}.")
        conn = connect(app)
        try:
            try:
                bundle, q, info = convert(raw)
            except Exception as exc:  # unparseable message
                conn.execute("INSERT INTO messages (received_at,status,issues,raw) VALUES (?,?,?,?)",
                             (now(), "rejected",
                              json.dumps([["error", "MSH", str(exc)]]), raw))
                return outcome(422, "structure", f"Cannot parse message: {exc}", "MSH")

            issues = [list(i) for i in q.issues]
            status = "accepted" if bundle else "rejected"
            conn.execute("BEGIN IMMEDIATE")
            changes = []
            if bundle:
                for entry in bundle["entry"]:
                    res = entry["resource"]
                    version, change = save_resource(conn, res, info["control_id"])
                    changes.append({"type": res["resourceType"], "id": res["id"],
                                    "version": version, "change": change})
            conn.execute(
                "INSERT INTO messages (received_at,control_id,msg_type,status,coded_total,as_is,"
                "normalized,unmapped,issues,raw) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now(), info["control_id"], info["type"], status, q.total, q.as_is,
                 q.normalized, q.unmapped, json.dumps(issues), raw))
            conn.execute("COMMIT")
        finally:
            conn.close()

        if not bundle:
            errors = [i for i in issues if i[0] == "error"]
            body = {"resourceType": "OperationOutcome", "issue": [
                {"severity": s, "code": "invalid" if s == "error" else "business-rule",
                 "diagnostics": m, "expression": [f]} for s, f, m in issues]}
            body["issue"].insert(0, {"severity": "error", "code": "processing",
                                     "diagnostics": f"Message {info['control_id']} rejected: "
                                                    f"{len(errors)} error(s) - nothing was stored."})
            return fhir(body, 422)

        return jsonify({
            "status": "accepted", "controlId": info["control_id"], "messageType": info["type"],
            "resources": changes,
            "quality": {"codedValues": q.total, "standardAsIs": q.as_is,
                        "normalized": q.normalized, "unmapped": q.unmapped,
                        "consistencyBeforePct": round(q.before, 1),
                        "consistencyAfterPct": round(q.after, 1)},
            "issues": [{"severity": s, "field": f, "message": m} for s, f, m in issues],
            "bundle": bundle}), (201 if any(c["change"] == "created" for c in changes) else 200)

    # ---- FHIR reads / searches ----------------------------------------------------
    @app.get("/fhir/metadata")
    def metadata():
        res = lambda t, params: {"type": t, "interaction": [{"code": "read"}, {"code": "search-type"},
                                                             {"code": "vread"}],
                                 "versioning": "versioned",
                                 "searchParam": [{"name": n, "type": "string"} for n in params]}
        return fhir({"resourceType": "CapabilityStatement", "status": "draft", "kind": "instance",
                     "fhirVersion": "4.0.1", "format": ["json"],
                     "software": {"name": "FHIR Gateway (learning project)"},
                     "rest": [{"mode": "server", "resource": [
                         res("Patient", ["family", "identifier", "gender"]),
                         res("Encounter", ["patient", "status", "class"])]}]})

    @app.get("/fhir/Patient/<pid>")
    def read_patient(pid):
        return read_one(app, "Patient", pid)

    @app.get("/fhir/Encounter/<eid>")
    def read_encounter(eid):
        return read_one(app, "Encounter", eid)

    @app.get("/fhir/<rtype>/<rid>/_history")
    def history(rtype, rid):
        if rtype not in ("Patient", "Encounter"):
            return outcome(404, "not-supported", f"Unknown resource type {rtype}")
        conn = connect(app)
        rows = conn.execute("SELECT * FROM resources WHERE type=? AND id=? ORDER BY version DESC",
                            (rtype, rid)).fetchall()
        conn.close()
        if not rows:
            return outcome(404, "not-found", f"{rtype}/{rid} not found")
        return fhir({"resourceType": "Bundle", "type": "history", "total": len(rows), "entry": [
            {"fullUrl": f"{base_url()}/{rtype}/{rid}/_history/{r['version']}",
             "resource": with_meta(r)} for r in rows]})

    @app.get("/fhir/Patient")
    def search_patients():
        conn = connect(app)
        rows = current(conn, "Patient")
        conn.close()
        fam, ident = request.args.get("family", "").lower(), request.args.get("identifier", "")
        gender = request.args.get("gender", "").lower()
        hits = []
        for r in rows:
            p = json.loads(r["body"])
            if fam and not any(fam in n.get("family", "").lower() for n in p.get("name", [])):
                continue
            if ident and not any(ident in (i["value"], i.get("system", "") + "|" + i["value"])
                                 for i in p.get("identifier", [])):
                continue
            if gender and p.get("gender") != gender:
                continue
            hits.append(r)
        return fhir(searchset(hits, base_url()))

    @app.get("/fhir/Encounter")
    def search_encounters():
        conn = connect(app)
        rows = current(conn, "Encounter")
        conn.close()
        patient = request.args.get("patient", "").replace("Patient/", "")
        status, cls = request.args.get("status", ""), request.args.get("class", "").upper()
        hits = []
        for r in rows:
            e = json.loads(r["body"])
            if patient and e.get("subject", {}).get("reference") != f"Patient/{patient}":
                continue
            if status and e.get("status") != status:
                continue
            if cls and e.get("class", {}).get("code") != cls:
                continue
            hits.append(r)
        return fhir(searchset(hits, base_url()))

    @app.get("/fhir/Patient/<pid>/$everything")
    def everything(pid):
        conn = connect(app)
        patient = current(conn, "Patient", pid)
        if patient is None:
            conn.close()
            return outcome(404, "not-found", f"Patient/{pid} not found")
        encs = [r for r in current(conn, "Encounter")
                if json.loads(r["body"]).get("subject", {}).get("reference") == f"Patient/{pid}"]
        conn.close()
        return fhir(searchset([patient] + encs, base_url()))

    # ---- audit & stats ------------------------------------------------------------
    @app.get("/messages")
    def messages():
        limit = min(int(request.args.get("limit", 50)), 500)
        conn = connect(app)
        rows = conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        out = []
        for r in rows:
            total = r["coded_total"] or 0
            out.append({"id": r["id"], "receivedAt": r["received_at"], "controlId": r["control_id"],
                        "messageType": r["msg_type"], "status": r["status"],
                        "consistencyBeforePct": round(100 * r["as_is"] / total, 1) if total else None,
                        "consistencyAfterPct": round(100 * (r["as_is"] + r["normalized"]) / total, 1)
                        if total else None,
                        "issues": [{"severity": s, "field": f, "message": m}
                                   for s, f, m in json.loads(r["issues"])]})
        return jsonify(out)

    @app.get("/stats")
    def stats():
        conn = connect(app)
        msgs = conn.execute("SELECT * FROM messages").fetchall()
        n_pat = len(current(conn, "Patient"))
        n_enc = len(current(conn, "Encounter"))
        conn.close()
        total = sum(r["coded_total"] or 0 for r in msgs)
        as_is = sum(r["as_is"] or 0 for r in msgs)
        norm = sum(r["normalized"] or 0 for r in msgs)
        issue_counts = Counter()
        for r in msgs:
            for sev, field, _ in json.loads(r["issues"]):
                issue_counts[(field, sev)] += 1
        accepted = sum(1 for r in msgs if r["status"] == "accepted")
        return jsonify({
            "messagesReceived": len(msgs), "accepted": accepted,
            "rejected": len(msgs) - accepted,
            "rejectionRatePct": round(100 * (len(msgs) - accepted) / len(msgs), 1) if msgs else 0,
            "patients": n_pat, "encounters": n_enc, "codedValues": total,
            "consistencyBeforePct": round(100 * as_is / total, 1) if total else None,
            "consistencyAfterPct": round(100 * (as_is + norm) / total, 1) if total else None,
            "topIssues": [{"field": f, "severity": s, "count": c}
                          for (f, s), c in issue_counts.most_common(5)]})

    # ---- docs ---------------------------------------------------------------------
    @app.get("/openapi.json")
    def openapi():
        return jsonify(OPENAPI)

    @app.get("/docs")
    def docs():
        return Response(DOCS_HTML, mimetype="text/html")


def read_one(app, rtype, rid):
    conn = connect(app)
    row = current(conn, rtype, rid)
    conn.close()
    if row is None:
        return outcome(404, "not-found", f"{rtype}/{rid} not found")
    return fhir(with_meta(row))


# ----------------------------------------------------------------------------------
# API docs (Swagger UI loaded from a CDN, so the browser needs internet)
# ----------------------------------------------------------------------------------
DOCS_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>FHIR Gateway API</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui.css"></head>
<body><div id="ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url: "/openapi.json", dom_id: "#ui"});</script></body></html>"""

SAMPLE_MESSAGE = "\n".join([
    msh("MSG100"),
    "PID|1||20001^^^DEMO_HOSP^MR||Khan^Sara^^^^^L||19950412|Female",
    pv1("Inpatient", "Ward 5^Room 12^Bed 1", "V7001^^^DEMO_HOSP", "202610040800")])


def _get(summary, params=None, desc=""):
    return {"summary": summary, "description": desc, "parameters": params or [],
            "responses": {"200": {"description": "OK"}}}


def _q(name, desc):
    return {"name": name, "in": "query", "schema": {"type": "string"}, "description": desc}


def _p(name):
    return {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}


OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "FHIR Gateway", "version": "1.0",
             "description": "Ingests HL7 v2 ADT messages and serves FHIR R4 Patient and Encounter "
                            "resources with version history and data-quality scoring. "
                            "Learning project - fake data only."},
    "paths": {
        "/hl7": {"post": {
            "summary": "Ingest an HL7 v2 ADT message (A01 / A04 / A08)",
            "description": "Returns 201/200 with the stored FHIR resources and a consistency report, "
                           "or 422 with an OperationOutcome if the message is unsafe to convert.",
            "requestBody": {"required": True, "content": {
                "text/plain": {"schema": {"type": "string"}, "example": SAMPLE_MESSAGE}}},
            "responses": {"201": {"description": "New resources created"},
                          "200": {"description": "Existing resources updated or unchanged"},
                          "422": {"description": "Rejected - OperationOutcome lists the problems"}}}},
        "/fhir/metadata": {"get": _get("CapabilityStatement")},
        "/fhir/Patient": {"get": _get("Search patients", [
            _q("family", "family name contains"), _q("identifier", "MRN or system|MRN"),
            _q("gender", "male | female | other | unknown")])},
        "/fhir/Patient/{id}": {"get": _get("Read a patient", [_p("id")])},
        "/fhir/Patient/{id}/$everything": {"get": _get("Patient plus all encounters", [_p("id")])},
        "/fhir/Encounter": {"get": _get("Search encounters", [
            _q("patient", "patient id"), _q("status", "planned | in-progress | finished"),
            _q("class", "IMP | AMB | EMER | PRENC")])},
        "/fhir/Encounter/{id}": {"get": _get("Read an encounter", [_p("id")])},
        "/fhir/{type}/{id}/_history": {"get": _get("Version history", [
            {"name": "type", "in": "path", "required": True,
             "schema": {"type": "string", "enum": ["Patient", "Encounter"]}}, _p("id")])},
        "/messages": {"get": _get("Audit log of received messages", [_q("limit", "max 500")])},
        "/stats": {"get": _get("Data-quality numbers (consistency before/after, rejection rate)")},
    },
}

if __name__ == "__main__":
    app = create_app()
    print("FHIR Gateway running.  API docs:  http://127.0.0.1:5000/docs")
    app.run(host="127.0.0.1", port=5000, debug=False)
