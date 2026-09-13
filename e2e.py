#!/usr/bin/env python3
"""End-to-end test against a running server.

    .venv/bin/python e2e.py [BASE_URL] [RESUME_PDF]

Exercises the real user journey over HTTP, including the paths that cost API calls.
Roughly 20 OpenAI calls per full run - it is not free, so it is not run on every save.
"""
import os
import re
import sys
import time
import urllib.parse

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5111"
# The suite needs more than the free résumé, so it runs against DEV_LOGIN=1 and a
# generous FREE_RESUMES. Against production, point it at a paid account instead.
RESUME = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("E2E_RESUME", "")
if not RESUME or not os.path.exists(RESUME):
    sys.exit("Point this at a résumé PDF: python e2e.py <base-url> <resume.pdf>\n"
             "or set E2E_RESUME=/path/to/resume.pdf")

IN_FIELD = ("DevSecOps Engineer. Secure CI/CD with SAST, DAST and secret scanning in GitHub "
            "Actions. Harden Kubernetes with RBAC and network policies. Terraform IaC. AWS "
            "GuardDuty and Security Hub. SIEM alerting and incident response.")
OUT_FIELD = ("HR Executive. Own employee lifecycle: onboarding, induction, exit formalities "
             "and full and final settlement. Maintain HRIS records. Payroll inputs, attendance "
             "and leave. Employee grievances. Statutory compliance. MIS dashboards.")
STRETCH = ("Staff DevSecOps Engineer. SAST, DAST, SCA. Kubernetes admission control with OPA "
           "Gatekeeper. Service mesh (Istio). Chaos engineering. Threat modelling.")

PASS = FAIL = 0
t0 = time.time()


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {name}" + (f"  ({detail})" if detail else ""))


def score_of(html):
    m = re.search(r">(\d+)%<small>", html)
    return int(m.group(1)) if m else None


def gaps_of(html):
    """Every claimable chip - both no-evidence gaps and half-credit partials."""
    return re.findall(r'name="req" value="([^"]+)"', html)


def drafts_of(html):
    return [d.strip() for d in re.findall(r'<textarea name="line" rows="2">([^<]+)', html)]


def section(t):
    print(f"\n\033[1m{t}\033[0m")


# ---------------------------------------------------------------- accounts
section("Accounts and quota")
anon = requests.Session()
anon.get(BASE, timeout=30)
with open(RESUME, "rb") as fh:
    anon.post(f"{BASE}/upload", files={"resume": ("r.pdf", fh.read(), "application/pdf")}, timeout=60)
r = anon.post(f"{BASE}/tailor", data={"jd": IN_FIELD}, timeout=60)
check("anonymous visitor cannot generate", "Sign in to generate" in r.text)
_dev = requests.post(f"{BASE}/dev-login", data={"email": "x@y.com"},
                     timeout=30, allow_redirects=False)
check("dev login is gated by DEV_LOGIN", _dev.status_code in (302, 403),
      "enabled (test mode)" if _dev.status_code == 302 else "disabled")
check("forged payment webhook is rejected",
      requests.post(f"{BASE}/webhook/razorpay", data=b'{"event":"payment_link.paid"}',
                    headers={"X-Razorpay-Signature": "forged"}, timeout=30).status_code == 400)
check("unsigned payment webhook is rejected",
      requests.post(f"{BASE}/webhook/razorpay", data=b'{}', timeout=30).status_code == 400)

# ---------------------------------------------------------------- onboarding
section("Onboarding")
s = requests.Session()
r = s.get(BASE, timeout=30)
s.post(f"{BASE}/dev-login", data={"email": f"e2e-{int(time.time())}@test.com"}, timeout=30)
check("fresh visit asks for a resume", "Step 1" in r.text, f"http {r.status_code}")
check("JD form hidden until upload", "Step 2" not in r.text)

r = s.post(f"{BASE}/tailor", data={"jd": IN_FIELD}, timeout=60)
check("tailoring without a resume is refused", "Upload your r" in r.text)

section("Upload validation")
r = s.post(f"{BASE}/upload", files={"resume": ("x.exe", b"MZ\x00binary", "application/octet-stream")}, timeout=30)
check("rejects unsupported extension", "Unsupported file type" in r.text)
r = s.post(f"{BASE}/upload", files={"resume": ("big.pdf", b"0" * (5 << 20), "application/pdf")}, timeout=60)
check("rejects oversized upload", r.status_code == 413, f"http {r.status_code}")
r = s.post(f"{BASE}/upload", files={"resume": ("tiny.txt", b"too short", "text/plain")}, timeout=30)
check("rejects unreadable/short file", "Could not read" in r.text)

with open(RESUME, "rb") as fh:
    r = s.post(f"{BASE}/upload", files={"resume": ("resume.pdf", fh.read(), "application/pdf")},
               timeout=60, allow_redirects=True)
check("accepts a real PDF", "loaded" in r.text, f"http {r.status_code}")
check("JD form now shown", "Step 2" in r.text)

# ---------------------------------------------------------------- matching
section("Matching")
r = s.post(f"{BASE}/tailor", data={"jd": IN_FIELD, "mode": "auto"}, timeout=300)
in_score = score_of(r.text)
check("in-field JD scores high", in_score is not None and in_score >= 85, f"{in_score}%")
check("resume rendered", "Professional Summary" in r.text)
# the résumé's own email must survive into the output, whatever it is
_email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", open(RESUME, "rb").read().decode("latin-1"))
check("contact details carried over", "@" in r.text.split('class="contact"')[1][:300])
paper = r.text.split('class="paper"')[1]
check("no unrendered template markers", "{{" not in paper and "{%" not in paper)

r = s.post(f"{BASE}/tailor", data={"jd": OUT_FIELD, "mode": "auto"}, timeout=300)
out_score = score_of(r.text)
check("out-of-field JD scores low", out_score is not None and out_score < 60, f"{out_score}%")
check("auto-pivot engaged", "reframed" in r.text or "career change" in r.text)
check("honesty log present", "don&#39;t back" in r.text or "don't back" in r.text)
check("in-field beats out-of-field", (in_score or 0) > (out_score or 100),
      f"{in_score}% vs {out_score}%")

section("Gap capture")
r = s.post(f"{BASE}/tailor", data={"jd": STRETCH, "mode": "auto"}, timeout=300)
stretch_score, gaps = score_of(r.text), gaps_of(r.text)
check("stretch JD surfaces claimable items", len(gaps) >= 2, f"{stretch_score}%, gaps: {', '.join(gaps[:3])}")
check("chips post to the draft step", 'action="/draft-facts"' in r.text)
check("chips are a real multi-select", r.text.count('type="checkbox" name="req"') == len(gaps))
check("submit is gated until something is ticked", 'id="pickbtn" style="margin-top:12px;width:auto" disabled' in r.text)

if gaps:
    picks = gaps[:3]
    r = s.post(f"{BASE}/draft-facts", timeout=300,
               data=[("req", q) for q in picks] + [("jd", STRETCH), ("mode", "auto")])
    drafts = drafts_of(r.text)
    check("ticking chips drafts the wording", len(drafts) == len(picks),
          f"{len(drafts)} lines for {len(picks)} ticked")
    check("drafts are editable and pre-ticked", r.text.count('type="checkbox" checked') == len(picks))
    check("drafts invent no metrics", not any(re.search(r"\d+\s*%|\b\d{2,}\b", d) for d in drafts),
          (drafts[0][:60] + "...") if drafts else "")
    check("confirm screen warns the user owns the truth", "truth of it is yours" in r.text)

    r = s.post(f"{BASE}/add-facts", timeout=900,
               data=[("line", d) for d in drafts] + [("jd", STRETCH), ("mode", "auto"), ("fix", "1")])
    after, gaps_after = score_of(r.text), gaps_of(r.text)
    check("confirmed lines raise the score", (after or 0) > (stretch_score or 100),
          f"{stretch_score}% -> {after}%")
    check("claimed requirements are closed", len(gaps_after) < len(gaps),
          f"{len(gaps)} -> {len(gaps_after)} open")

section("Improve loop")
r = s.post(f"{BASE}/tailor", data={"jd": STRETCH, "mode": "auto", "fix": "1"}, timeout=900)
check("fix loop reports its passes", "PASSES" in r.text,
      (re.search(r"PASSES ([\d →%]+)", r.text) or [None, "?"])[1])

section("Modes")
for mode, marker in (("pivot", "reframed"), ("minimal", "career change")):
    r = s.post(f"{BASE}/tailor", data={"jd": IN_FIELD, "mode": mode}, timeout=300)
    check(f"mode={mode} forced on an in-field JD", marker in r.text, f"{score_of(r.text)}%")

section("Loader")
for page, name in ((s.get(BASE, timeout=30).text, "index"), (r.text, "resume")):
    check(f"{name} shows a loader on submit", 'id="ld"' in page and "onsubmit=" in page)

section("Print + privacy")
check("print CSS hides the control panel", "@media print" in r.text and ".bar{display:none}" in r.text)
check("data handling disclosed", "Your data" in s.get(BASE, timeout=30).text)

section("Persistence + deletion")
check("session survives across requests", "loaded" in s.get(BASE, timeout=30).text)
s.post(f"{BASE}/reset", timeout=30)
check("reset deletes the resume", "Step 1" in s.get(BASE, timeout=30).text)
r2 = requests.Session()
check("a new visitor sees no one else's data", "Step 1" in r2.get(BASE, timeout=30).text)

print(f"\n\033[1m{PASS} passed, {FAIL} failed\033[0m  in {time.time()-t0:.0f}s")
sys.exit(1 if FAIL else 0)
