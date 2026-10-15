"""
Demo: send the sample HL7 messages to the running FHIR Gateway and print what comes back.
Start the server first (run_server.bat), then run:   python demo_client.py
Standard library only.
"""
import json
import sys
import urllib.error
import urllib.request

from hl7_to_fhir import SAMPLES

BASE = "http://127.0.0.1:5000"


def call(method, path, data=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=data.encode() if data else None,
                                 headers={"Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    for name, message in SAMPLES.items():
        status, body = call("POST", "/hl7", message)
        print(f"\n{name}: HTTP {status}")
        if status in (200, 201):
            q = body["quality"]
            print("  stored:", [f"{r['type']} v{r['version']} ({r['change']})" for r in body["resources"]])
            print(f"  consistency {q['consistencyBeforePct']}% -> {q['consistencyAfterPct']}%")
        else:
            for issue in body["issue"]:
                print(f"  {issue['severity']}: {issue['diagnostics']}")

    status, patients = call("GET", "/fhir/Patient?family=doe")
    print(f"\nSearch Patient?family=doe -> HTTP {status}, {patients['total']} found")
    pid = patients["entry"][0]["resource"]["id"]
    status, everything = call("GET", f"/fhir/Patient/{pid}/$everything")
    print(f"$everything for that patient -> {everything['total']} resources")
    status, stats = call("GET", "/stats")
    print("\nData-quality dashboard:")
    print(json.dumps(stats, indent=2))
    print("\nOpen http://127.0.0.1:5000/docs in your browser to explore the API.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError:
        print("Cannot reach the server. Start it first with run_server.bat")
    if sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close...")
