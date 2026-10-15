"""
HL7 v2 -> FHIR R4 mapper with a data-quality (semantic consistency) report.

Health-informatics idea: before an HL7 v2 message can be exchanged safely as FHIR,
its coded values must be consistent. This tool
  1. parses ADT messages (A01 / A04 / A08),
  2. maps PID -> Patient and PV1 -> Encounter (following the HL7 v2-to-FHIR IG rules),
  3. normalizes local variants through lookup tables,
  4. reports how consistent the coded data was BEFORE and AFTER normalization.

Only the Python standard library is used. All patient data below is FAKE.

Run:        python hl7_to_fhir.py          (maps the sample messages, writes JSON)
Self-test:  python hl7_to_fhir.py test
Reference:  https://build.fhir.org/ig/HL7/v2-to-fhir/   (a draft guide, it still changes)
"""
import json
import os
import re
import sys
import uuid
from datetime import datetime

# Timezone added to FHIR dateTime values that carry a time (FHIR requires an offset).
# HL7 v2 timestamps often have none. Change this to your hospital's timezone.
DEFAULT_TZ = "+03:00"

# ----------------------------------------------------------------------------------
# Lookup tables (the "semantic consistency" layer)
# ----------------------------------------------------------------------------------
# HL7 table 0001 (Administrative Sex) -> FHIR AdministrativeGender.
# A (Ambiguous) and N (Not applicable) are deliberately NOT mapped here: they have no
# clear FHIR equivalent, so they are flagged for human review instead of guessed.
STANDARD_GENDER = {"M": "male", "F": "female", "O": "other", "U": "unknown"}
# Local spellings seen in real systems -> standard HL7 code.
LOCAL_GENDER = {"male": "M", "female": "F", "m": "M", "f": "F", "man": "M", "woman": "F"}

# HL7 table 0004 (Patient Class) -> v3 ActCode (FHIR Encounter.class).
PATIENT_CLASS = {
    "I": ("IMP", "inpatient encounter"),
    "O": ("AMB", "ambulatory"),
    "E": ("EMER", "emergency"),
    "P": ("PRENC", "pre-admission"),
}
LOCAL_CLASS = {"inpatient": "I", "outpatient": "O", "er": "E", "emergency": "E", "ward": "I"}

SUPPORTED_EVENTS = {"A01", "A04", "A08"}
ID_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v2-0203"
ACT_CODE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
BIRTHTIME_EXT = "http://hl7.org/fhir/StructureDefinition/patient-birthTime"
UUID_NS = uuid.UUID("12345678-1234-5678-1234-567812345678")


# ----------------------------------------------------------------------------------
# HL7 v2 parsing
# ----------------------------------------------------------------------------------
class Segment:
    def __init__(self, raw, enc):
        self.enc = enc
        self.parts = raw.split(enc["field"])
        self.name = self.parts[0]

    def field(self, n):
        """Return the raw text of field n (e.g. PID-5 -> field(5)), or ''."""
        # MSH-1 is the field separator itself, so MSH numbering is shifted by one.
        i = n - 1 if self.name == "MSH" else n
        return self.parts[i] if 0 <= i < len(self.parts) else ""

    def repeats(self, n):
        """Field n split into its repetitions (~), each a list of components (^)."""
        raw = self.field(n)
        if not raw:
            return []
        return [r.split(self.enc["comp"]) for r in raw.split(self.enc["rep"]) if r != ""]

    def first(self, n):
        r = self.repeats(n)
        return r[0] if r else []


def unescape(text, enc):
    """Decode the common HL7 escape sequences (\\F\\ \\S\\ \\T\\ \\R\\ \\E\\)."""
    esc = enc["esc"]
    table = {"F": enc["field"], "S": enc["comp"], "T": enc["sub"], "R": enc["rep"], "E": esc}
    return re.sub(re.escape(esc) + r"([FSTRE])" + re.escape(esc), lambda m: table[m.group(1)], text)


def comp(components, i):
    """Component i (1-based) of a parsed field, or ''."""
    return components[i - 1].strip() if len(components) >= i else ""


def parse_message(text):
    lines = [l for l in re.split(r"\r\n|\r|\n", text.strip()) if l.strip()]
    if not lines or not lines[0].startswith("MSH") or len(lines[0]) < 8:
        raise ValueError("Message must start with an MSH segment.")
    msh = lines[0]
    fld = msh[3]
    enc_chars = msh.split(fld)[1]
    enc = {"field": fld, "comp": enc_chars[0], "rep": enc_chars[1],
           "esc": enc_chars[2], "sub": enc_chars[3]}
    segs = [Segment(l, enc) for l in lines]
    return {"enc": enc, "segments": segs,
            "by_name": {s.name: s for s in reversed(segs)}}  # first occurrence wins


# ----------------------------------------------------------------------------------
# Date / time (HL7 DTM -> FHIR date / dateTime), with real calendar validation
# ----------------------------------------------------------------------------------
_DTM = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.\d+)?([+-]\d{4})?$")


def convert_dtm(value, as_date=False):
    """'19900214' -> '1990-02-14'; '199002141030' -> '1990-02-14T10:30:00+03:00'."""
    m = _DTM.match(value.strip())
    if not m:
        raise ValueError(f"'{value}' is not a valid HL7 timestamp")
    y, mo, d, h, mi, s, tz = m.groups()
    datetime(int(y), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(s or 0))  # raises if impossible
    date = y + (f"-{mo}" if mo else "") + (f"-{d}" if mo and d else "")
    if as_date or not h:
        return date
    offset = f"{tz[:3]}:{tz[3:]}" if tz else DEFAULT_TZ
    return f"{date}T{h}:{mi or '00'}:{s or '00'}{offset}"


# ----------------------------------------------------------------------------------
# Data-quality tracking
# ----------------------------------------------------------------------------------
class Quality:
    """Counts coded values so we can report consistency before / after normalization."""

    def __init__(self):
        self.total = self.as_is = self.normalized = self.unmapped = 0
        self.issues = []  # (severity, field, message)

    def note(self, severity, field, message):
        self.issues.append((severity, field, message))

    def coded(self, status):
        self.total += 1
        if status == "ok":
            self.as_is += 1
        elif status == "normalized":
            self.normalized += 1
        else:
            self.unmapped += 1

    @property
    def before(self):
        return 100.0 * self.as_is / self.total if self.total else 100.0

    @property
    def after(self):
        return 100.0 * (self.as_is + self.normalized) / self.total if self.total else 100.0


def resolve_code(raw, standard, local, label, q):
    """Return (standard_code, status). Records the outcome in the quality tracker."""
    value = (raw or "").strip()
    if not value:
        return None, "missing"
    if value in standard:
        q.coded("ok")
        return value, "ok"
    key = value.lower()
    if key in local:
        q.coded("normalized")
        q.note("info", label, f"local value '{value}' normalized to '{local[key]}'")
        return local[key], "normalized"
    q.coded("unmapped")
    q.note("warning", label, f"value '{value}' has no mapping - needs human review")
    return None, "unmapped"


# ----------------------------------------------------------------------------------
# Mapping: PID -> Patient, PV1 -> Encounter
# ----------------------------------------------------------------------------------
def map_identifier(c, enc, kind="MR"):
    value = unescape(comp(c, 1), enc)
    if not value:
        return None
    authority = comp(c, 4).split(enc["sub"])[0]
    ident = {"value": value}
    if authority:
        ident["system"] = "urn:hospital:" + re.sub(r"[^a-z0-9]+", "-", authority.lower())
    type_code = comp(c, 5) or kind
    ident["type"] = {"coding": [{"system": ID_TYPE_SYSTEM, "code": type_code}]}
    return ident


def map_name(c, enc):
    family = unescape(comp(c, 1).split(enc["sub"])[0], enc)
    given = [unescape(g, enc) for g in (comp(c, 2), comp(c, 3)) if g]
    if not family and not given:
        return None
    name = {}
    if family:
        name["family"] = family
    if given:
        name["given"] = given
    if comp(c, 7) == "L":  # name type code L = legal
        name["use"] = "official"
    return name


def make_id(*parts):
    return str(uuid.uuid5(UUID_NS, "|".join(parts)))


def map_patient(pid, enc, q):
    patient = {"resourceType": "Patient"}

    ids = [i for i in (map_identifier(c, enc) for c in pid.repeats(3)) if i]
    if ids:
        patient["identifier"] = ids  # repeating field -> list
        patient["id"] = make_id(ids[0].get("system", ""), ids[0]["value"])
    else:
        q.note("error", "PID-3", "no patient identifier - message cannot be matched to a patient")

    names = [n for n in (map_name(c, enc) for c in pid.repeats(5)) if n]
    if names:
        patient["name"] = names
    else:
        q.note("error", "PID-5", "no patient name")

    dob = pid.field(7).strip()
    if dob:
        try:
            patient["birthDate"] = convert_dtm(dob, as_date=True)
            if len(re.match(r"\d*", dob).group()) > 8:  # a time of birth is present
                patient["_birthDate"] = {"extension": [
                    {"url": BIRTHTIME_EXT, "valueDateTime": convert_dtm(dob)}]}
        except ValueError as e:
            q.note("error", "PID-7", str(e))

    code, status = resolve_code(pid.first(8)[0] if pid.first(8) else "", STANDARD_GENDER,
                                LOCAL_GENDER, "PID-8", q)
    if code:
        patient["gender"] = STANDARD_GENDER[code]
    elif status == "missing":
        q.note("warning", "PID-8", "administrative sex is empty")

    # PID-29 death time wins over PID-30 indicator (same rule as the IG)
    death = pid.field(29).strip()
    if death:
        try:
            patient["deceasedDateTime"] = convert_dtm(death)
        except ValueError as e:
            q.note("error", "PID-29", str(e))
    elif pid.field(30).strip() in ("Y", "N"):
        patient["deceasedBoolean"] = pid.field(30).strip() == "Y"
    return patient


def map_encounter(pv1, enc, q):
    encounter = {"resourceType": "Encounter"}

    cls_raw = comp(pv1.first(2), 1)
    code, status = resolve_code(cls_raw, PATIENT_CLASS, LOCAL_CLASS, "PV1-2", q)
    if code:
        act, display = PATIENT_CLASS[code]
        encounter["class"] = {"system": ACT_CODE_SYSTEM, "code": act, "display": display}
    elif status == "missing":
        q.note("error", "PV1-2", "patient class is required for an Encounter")

    admit, discharge = pv1.field(44).strip(), pv1.field(45).strip()
    period = {}
    for key, raw, fld in (("start", admit, "PV1-44"), ("end", discharge, "PV1-45")):
        if raw:
            try:
                period[key] = convert_dtm(raw)
            except ValueError as e:
                q.note("error", fld, str(e))
    if period:
        encounter["period"] = period

    # Simplified from the IG: discharge time present -> finished.
    if "end" in period:
        encounter["status"] = "finished"
    elif code == "P":
        encounter["status"] = "planned"
    else:
        encounter["status"] = "in-progress"

    visit = map_identifier(pv1.first(19), enc, kind="VN")
    if visit:
        visit["type"]["text"] = "visit number"
        encounter["identifier"] = [visit]
        encounter["id"] = make_id("encounter", visit.get("system", ""), visit["value"])

    loc = pv1.first(3)
    if any(loc):
        bits = [comp(loc, i) for i in (1, 2, 3) if comp(loc, i)]
        encounter["location"] = [{"location": {"display": " / ".join(bits)},
                                  "status": "planned" if code == "P" else "active"}]
    return encounter


def convert(text):
    """Convert one HL7 v2 ADT message. Returns (bundle_or_None, Quality, info)."""
    q = Quality()
    msg = parse_message(text)
    enc, by = msg["enc"], msg["by_name"]
    msh = by["MSH"]
    mtype = msh.first(9)
    event = comp(mtype, 2)
    info = {"control_id": msh.field(10).strip() or "unknown",
            "type": "^".join(x for x in mtype[:2]) or "unknown"}

    if comp(mtype, 1) != "ADT" or event not in SUPPORTED_EVENTS:
        q.note("error", "MSH-9", f"unsupported message type {info['type']} "
                                  f"(supported: ADT^{'/'.join(sorted(SUPPORTED_EVENTS))})")
        return None, q, info
    if "PID" not in by:
        q.note("error", "PID", "message has no PID segment")
        return None, q, info

    patient = map_patient(by["PID"], enc, q)
    entries = [patient]
    if "PV1" in by:
        encounter = map_encounter(by["PV1"], enc, q)
        if "id" in patient:
            encounter["subject"] = {"reference": f"Patient/{patient['id']}"}
        entries.append(encounter)
    else:
        q.note("warning", "PV1", "no PV1 segment - no Encounter created")

    if any(sev == "error" for sev, _, _ in q.issues):
        return None, q, info  # never emit half-valid clinical data

    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"fullUrl": f"urn:uuid:{e['id']}" if "id" in e else None, "resource": e}
                        for e in entries]}
    for e in bundle["entry"]:
        if e["fullUrl"] is None:
            del e["fullUrl"]
    return bundle, q, info


# ----------------------------------------------------------------------------------
# Sample messages (FAKE data). PV1 is built field-by-field so positions are exact.
# ----------------------------------------------------------------------------------
def pv1(cls, location="", visit="", admit="", discharge=""):
    f = [""] * 46
    f[0], f[1], f[2], f[3] = "PV1", "1", cls, location
    f[19], f[44], f[45] = visit, admit, discharge
    return "|".join(f)


def msh(ctrl, event="A01"):
    return f"MSH|^~\\&|HIS|DEMO_HOSP|FHIRGW|DEMO|202610040815||ADT^{event}^ADT_{event}|{ctrl}|P|2.5.1"


SAMPLES = {
    "clean_inpatient_admission": "\r".join([
        msh("MSG001"),
        "PID|1||10001^^^DEMO_HOSP^MR||Doe^Jane^Marie^^^^L~Doe^Janie||199002141030|F",
        pv1("I", "ICU^Room 3^Bed 2", "V9001^^^DEMO_HOSP", "202610040800"),
    ]),
    "messy_local_values": "\r".join([
        msh("MSG002"),
        "PID|1||10002^^^DEMO_HOSP^MR||Roe^Alex||19851130|Female",
        pv1("Inpatient", "Ward 5^Room 12", "V9002^^^DEMO_HOSP", "202610030930"),
    ]),
    "bad_data_rejected": "\r".join([
        msh("MSG003"),
        "PID|1||10003^^^DEMO_HOSP^MR||Poe^Sam||19900231|N",
        pv1("X", "", "V9003^^^DEMO_HOSP", "202610041300"),
    ]),
    "discharge_a08_update": "\r".join([
        msh("MSG004", "A08"),
        "PID|1||10001^^^DEMO_HOSP^MR||Doe^Jane||199002141030|F",
        pv1("I", "Ward 2^Room 8", "V9001^^^DEMO_HOSP", "202610040800", "202610070900"),
    ]),
}


# ----------------------------------------------------------------------------------
# Report + main
# ----------------------------------------------------------------------------------
def report(name, bundle, q, info):
    print("=" * 72)
    print(f"{name}   [{info['type']}  control id {info['control_id']}]")
    print("-" * 72)
    for sev, field, message in q.issues:
        print(f"  {sev.upper():8} {field:7} {message}")
    if q.total:
        print(f"  Coded values: {q.total}  | standard as-is: {q.as_is}  | "
              f"normalized: {q.normalized}  | unmapped: {q.unmapped}")
        print(f"  Semantic consistency: {q.before:.0f}% before -> {q.after:.0f}% after normalization")
    print("  RESULT:", "FHIR bundle created" if bundle else "REJECTED (not safe to convert)")


class Tee:
    """Print to the console AND keep a copy, so the report is saved as report.txt."""

    def __init__(self, stream):
        self.stream, self.lines = stream, []

    def write(self, s):
        self.lines.append(s)
        return self.stream.write(s)

    def flush(self):
        self.stream.flush()


def main():
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fhir_output")
    os.makedirs(out_dir, exist_ok=True)
    tee = Tee(sys.stdout)
    sys.stdout = tee
    try:
        _run(out_dir)
    finally:
        sys.stdout = tee.stream
        with open(os.path.join(out_dir, "report.txt"), "w", encoding="utf-8") as fh:
            fh.write("".join(tee.lines))


def _run(out_dir):
    for name, text in SAMPLES.items():
        bundle, q, info = convert(text)
        report(name, bundle, q, info)
        if bundle:
            path = os.path.join(out_dir, name + ".json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(bundle, fh, indent=2, ensure_ascii=True)
            print("  saved:", path)
    print("=" * 72)
    print("Note: this is a learning tool built on a draft guide. Validate real output")
    print("with an official FHIR validator before using it with real patient data.")


def run_tests():
    b, q, _ = convert(SAMPLES["clean_inpatient_admission"])
    p, e = b["entry"][0]["resource"], b["entry"][1]["resource"]
    assert p["birthDate"] == "1990-02-14"
    assert p["_birthDate"]["extension"][0]["valueDateTime"] == "1990-02-14T10:30:00+03:00"
    assert p["gender"] == "female" and len(p["name"]) == 2 and p["name"][0]["use"] == "official"
    assert e["class"]["code"] == "IMP" and e["status"] == "in-progress"
    assert e["period"]["start"] == "2026-10-04T08:00:00+03:00"
    assert e["subject"]["reference"] == "Patient/" + p["id"]
    assert q.before == q.after == 100.0

    b, q, _ = convert(SAMPLES["messy_local_values"])
    assert b and q.before == 0.0 and q.after == 100.0
    assert b["entry"][0]["resource"]["gender"] == "female"

    b, q, _ = convert(SAMPLES["bad_data_rejected"])
    assert b is None and any(f == "PID-7" for _, f, _ in q.issues)  # 31 Feb is impossible

    b, q, _ = convert(SAMPLES["discharge_a08_update"])
    assert b["entry"][1]["resource"]["status"] == "finished"
    assert b["entry"][0]["resource"]["id"] == convert(SAMPLES["clean_inpatient_admission"])[0]["entry"][0]["resource"]["id"]

    assert unescape(r"Smith\T\Sons", parse_message(SAMPLES["clean_inpatient_admission"])["enc"]) == "Smith&Sons"
    b, q, _ = convert("MSH|^~\\&|A|B|C|D|202610040815||ORU^R01|X|P|2.5.1\rPID|1")
    assert b is None and q.issues[0][1] == "MSH-9"  # lab results are out of scope
    print("All tests passed.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        run_tests()
    else:
        main()
        if sys.stdin and sys.stdin.isatty():
            input("\nPress Enter to close...")
