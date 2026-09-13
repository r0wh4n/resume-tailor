#!/usr/bin/env python3
"""Resume tailor: score master-resume bullets against a JD, render a 1-page resume.

Master resume lives in resume.json. Bullets may carry slots:
  {a|b|c}   -> pick the variant closest to the JD's own vocabulary
  [clause]  -> keep the clause only if its words appear in the JD
"""
import json, os, re, sys, tempfile, urllib.request, uuid
from collections import Counter
from html.parser import HTMLParser
import pathlib
from pathlib import Path

from flask import Flask, request, render_template, session, redirect, url_for

import auth
import store

HERE = Path(__file__).parent

# Local secrets: KEY=value lines in .env, real env vars win. ponytail: python-dotenv is 4 lines away.
for _line in (HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []:
    if "=" in _line and not _line.lstrip().startswith("#"):
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

# ponytail: hand-rolled stoplist beats pulling in nltk for 200 words.
STOP = set("""a about above after again against all also am an and any are as at be because been
before being below between both but by can cannot could did do does doing down during each etc few
for from further had has have having he her here hers him his how i if in include includes including
into is it its itself let me more most must my no nor not of off on once only or other ought our ours
out over own per same she should so some such than that the their theirs them then there these they
this those through to too under until up use used using very was we were what when where which while
who whom why will with within would you your yours ability across candidate candidates company
excellent experience good great job knowledge opportunity plus position preferred required
requirement requirements responsibilities responsible role skills strong team teams understanding
well work working year years new business applicant benefits equal employer diversity apply hiring hire seeking looking want need join us you'll
""".split())


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def strip_html(html):
    p = _Text()
    p.feed(html)
    return re.sub(r"\s+", " ", " ".join(p.out))


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return strip_html(r.read().decode("utf-8", "replace"))


def stem(w):
    # ponytail: crude suffix strip, not a real stemmer. Swap in one if matches read wrong.
    for suf in ("ing", "ed", "es", "s", "e"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text):
    raw = re.findall(r"[a-z0-9+#.]+", text.lower())
    return [stem(w.strip(".")) for w in raw if w.strip(".") not in STOP and len(w.strip(".")) > 1]


def jd_keywords(text, surface=None):
    if surface is not None:
        for w in re.findall(r"[a-z0-9+#.]+", text.lower()):
            surface.setdefault(stem(w.strip(".")), w.strip("."))
    c = Counter(tokens(text))
    top = c.most_common(150)
    if not top:
        return {}
    mx = top[0][1]
    return {w: n / mx for w, n in top}


def _near(w, vocab):
    """Longest-prefix overlap stands in for a real stemmer: secure~security, automat~automation."""
    if len(w) < 4:
        return []
    return [v for v in vocab if len(v) >= 4 and (v.startswith(w) or w.startswith(v))]


def score(text, kw):
    total = 0
    for w in set(tokens(text)):
        if w in kw:
            total += kw[w]
        else:
            total += max((kw[k] for k in _near(w, kw)), default=0)
    return total


# ---------------------------------------------------------------- AI mode
# Rewrites YOUR facts into the JD's language. The prompt forbids invention;
# ponytail: no framework, no agent loop - one call, schema-constrained.

USAGE = Counter()

QUOTA_MSG = {
    "sign-in": "Sign in to generate a résumé - you get {limit} free, no card needed.",
    "upgrade": "You have used your {limit} free résumé. Upgrade for unlimited, "
               "or add your own API key and keep using it for free.",
    "daily": "That is {used} résumés today - an anti-runaway cap, not a plan limit. "
             "It resets on a rolling basis.",
}


class NeedKey(Exception):
    """No usable OpenAI key for this request."""


# gpt-5.5 is a reasoning model: ~3x slower for ~1 point of score, which is inside the
# judge's own noise. All overridable per deployment.
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", MODEL)
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")



def chat(system, user, schema, name, model=None):
    """Every model call goes through here, so provider selection, key handling and
    token accounting each happen in exactly one place. Both providers are asked for
    schema-constrained JSON, so callers never see the difference."""
    key, _, provider = active_key()
    if not key:
        raise NeedKey()
    if provider == "anthropic":
        return _anthropic(key, system, user, schema, model)
    return _openai(key, system, user, schema, name, model)


def _openai(key, system, user, schema, name, model):
    from openai import OpenAI
    r = OpenAI(api_key=key).chat.completions.create(
        model=model or MODEL,
        response_format={"type": "json_schema",
                         "json_schema": {"name": name, "strict": True, "schema": schema}},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    if r.usage:
        USAGE.update(calls=1, tok_in=r.usage.prompt_tokens, tok_out=r.usage.completion_tokens)
    return json.loads(r.choices[0].message.content)


def _anthropic(key, system, user, schema, model):
    """output_config.format guarantees the first content block is valid JSON for the schema."""
    import anthropic
    r = anthropic.Anthropic(api_key=key).messages.create(
        model=model if model and model.startswith("claude") else ANTHROPIC_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema},
                       "effort": os.environ.get("ANTHROPIC_EFFORT", "low")},
    )
    if r.usage:
        USAGE.update(calls=1, tok_in=r.usage.input_tokens, tok_out=r.usage.output_tokens)
    return json.loads(next(b.text for b in r.content if b.type == "text"))


RESUME_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "title", "email", "phone", "location", "links", "summary",
                 "skills", "experience", "projects", "education", "certifications", "notes"],
    "properties": {
        "name": {"type": "string"}, "title": {"type": "string"},
        "email": {"type": "string"}, "phone": {"type": "string"},
        "location": {"type": "string"},
        "links": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "skills": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["category", "items"],
            "properties": {"category": {"type": "string"},
                           "items": {"type": "array", "items": {"type": "string"}}}}},
        "experience": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["company", "title", "dates", "location", "bullets"],
            "properties": {"company": {"type": "string"}, "title": {"type": "string"},
                           "dates": {"type": "string"}, "location": {"type": "string"},
                           "bullets": {"type": "array", "items": {"type": "string"}}}}},
        "projects": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "bullets"],
            "properties": {"name": {"type": "string"},
                           "bullets": {"type": "array", "items": {"type": "string"}}}}},
        "education": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["school", "degree", "dates"],
            "properties": {"school": {"type": "string"}, "degree": {"type": "string"},
                           "dates": {"type": "string"}}}},
        "certifications": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"},
                  "description": "Things the JD asks for that the facts do NOT support."},
    },
}

SYSTEM = """You rewrite a candidate's resume to target one job description.

ABSOLUTE RULE - you may only use facts present in FACTS. You must never add, imply or
embellish: employers, job titles, dates, tools, technologies, certifications, degrees,
team sizes, seniority, scope, percentages, or any metric. If FACTS gives no number, your
output contains no number. If the JD wants a skill FACTS does not show, leave it out and
record it in "notes" instead. Inventing anything is a total failure of this task.

WITHIN that rule, tailor aggressively:
- Mirror the JD's own vocabulary and verbs where they honestly describe what FACTS says.
- Lead with the experience the JD cares about; drop or shorten what it does not.
- Merge, split and reorder bullets. Rewrite phrasing freely.
- Pick a "title" the candidate can honestly claim from their real title and target roles.
- Write one "summary" of 2-3 sentences aimed squarely at this role.
- Order skill categories and the items inside them by relevance to the JD.
- Keep it to one page: about 6-8 bullets for the most recent role, fewer for older ones.
- Keep "company", "dates", "location", "school" and "degree" character-for-character as
  FACTS gives them.

"notes" is your honesty log: every requirement in the JD that FACTS cannot back."""


# ---------------------------------------------------------------- AI mode
# Rewrites YOUR facts into the JD's language. The prompt forbids invention;
# ponytail: no framework, no agent loop - one call, schema-constrained.

USAGE = Counter()

QUOTA_MSG = {
    "sign-in": "Sign in to generate a résumé - you get {limit} free, no card needed.",
    "upgrade": "You have used your {limit} free résumé. Upgrade for unlimited, "
               "or add your own API key and keep using it for free.",
    "daily": "That is {used} résumés today - an anti-runaway cap, not a plan limit. "
             "It resets on a rolling basis.",
}


class NeedKey(Exception):
    """No usable OpenAI key for this request."""


def chat(system, user, schema, name, model=None):
    """Every model call goes through here, so provider selection, key handling and
    token accounting each happen in exactly one place. Both providers are asked for
    schema-constrained JSON, so callers never see the difference."""
    key, _, provider = active_key()
    if not key:
        raise NeedKey()
    if provider == "anthropic":
        return _anthropic(key, system, user, schema, model)
    return _openai(key, system, user, schema, name, model)


def _openai(key, system, user, schema, name, model):
    from openai import OpenAI
    r = OpenAI(api_key=key).chat.completions.create(
        model=model or MODEL,
        response_format={"type": "json_schema",
                         "json_schema": {"name": name, "strict": True, "schema": schema}},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    if r.usage:
        USAGE.update(calls=1, tok_in=r.usage.prompt_tokens, tok_out=r.usage.completion_tokens)
    return json.loads(r.choices[0].message.content)


def _anthropic(key, system, user, schema, model):
    """output_config.format guarantees the first content block is valid JSON for the schema."""
    import anthropic
    r = anthropic.Anthropic(api_key=key).messages.create(
        model=model if model and model.startswith("claude") else ANTHROPIC_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema},
                       "effort": os.environ.get("ANTHROPIC_EFFORT", "low")},
    )
    if r.usage:
        USAGE.update(calls=1, tok_in=r.usage.input_tokens, tok_out=r.usage.output_tokens)
    return json.loads(next(b.text for b in r.content if b.type == "text"))


RESUME_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "title", "email", "phone", "location", "links", "summary",
                 "skills", "experience", "projects", "education", "certifications", "notes"],
    "properties": {
        "name": {"type": "string"}, "title": {"type": "string"},
        "email": {"type": "string"}, "phone": {"type": "string"},
        "location": {"type": "string"},
        "links": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "skills": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["category", "items"],
            "properties": {"category": {"type": "string"},
                           "items": {"type": "array", "items": {"type": "string"}}}}},
        "experience": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["company", "title", "dates", "location", "bullets"],
            "properties": {"company": {"type": "string"}, "title": {"type": "string"},
                           "dates": {"type": "string"}, "location": {"type": "string"},
                           "bullets": {"type": "array", "items": {"type": "string"}}}}},
        "projects": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "bullets"],
            "properties": {"name": {"type": "string"},
                           "bullets": {"type": "array", "items": {"type": "string"}}}}},
        "education": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["school", "degree", "dates"],
            "properties": {"school": {"type": "string"}, "degree": {"type": "string"},
                           "dates": {"type": "string"}}}},
        "certifications": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"},
                  "description": "Things the JD asks for that the facts do NOT support."},
    },
}

SYSTEM = """You rewrite a candidate's resume to target one job description.

ABSOLUTE RULE - you may only use facts present in FACTS. You must never add, imply or
embellish: employers, job titles, dates, tools, technologies, certifications, degrees,
team sizes, seniority, scope, percentages, or any metric. If FACTS gives no number, your
output contains no number. If the JD wants a skill FACTS does not show, leave it out and
record it in "notes" instead. Inventing anything is a total failure of this task.

WITHIN that rule, tailor aggressively:
- Mirror the JD's own vocabulary and verbs where they honestly describe what FACTS says.
- Lead with the experience the JD cares about; drop or shorten what it does not.
- Merge, split and reorder bullets. Rewrite phrasing freely.
- Pick a "title" the candidate can honestly claim from their real title and target roles.
- Write one "summary" of 2-3 sentences aimed squarely at this role.
- Order skill categories and the items inside them by relevance to the JD.
- Keep it to one page: about 6-8 bullets for the most recent role, fewer for older ones.
- Keep "company", "dates", "location", "school" and "degree" character-for-character as
  FACTS gives them.

"notes" is your honesty log: every requirement in the JD that FACTS cannot back."""


PIVOT = """

PIVOT MODE - the candidate is targeting a field different from their background.
Their FACTS will not contain this field's experience. You still invent nothing.
What you DO change is vocabulary: describe their real work in the target field's
language wherever the mapping is honest and a practitioner would accept it. For example
identity/access administration is employee access lifecycle work; runbooks and RCA are
process documentation and operational metrics; control validation is compliance
operations. Do not stretch a mapping that a hiring manager in the target field would
call false. Lead with whatever transfers; keep the untransferable work brief but present,
since unexplained gaps read worse than an honest adjacent background. State the pivot
plainly in the summary. In "notes", say outright which core requirements of this role the
candidate has never done.

STRIP THE JARGON. Omit tool, product, vendor and technology names a hiring manager in the
TARGET field would not recognise or value, and drop the skill categories that only exist to
hold them. Name the work in the target field's plain operational language instead - what was
administered, coordinated, documented, reconciled, approved, audited. Keep a specific tool
name only where the target field genuinely uses it. Omit whole sections that serve only the
old field - a project or certification that does nothing for the target role is better left
out than translated. Never rename the role itself: job title, employer and dates stay exactly
as FACTS gives them, because those are verifiable."""


MINIMAL = """

MINIMAL MODE - the candidate's history has essentially nothing in common with this role.
Do not pad it out. Write a short, honest, career-change resume:
- Compress each unrelated role to AT MOST two bullets covering only what genuinely
  transfers to this field. Keep its title, employer and dates intact and visible.
- Lead with skills that transfer, named in this field's language. Do not list a skill
  the candidate cannot demonstrate.
- Keep projects only if they bear on this role.
- A short resume is the correct output here. Do not invent volume, and do not inflate
  adjacent work into something it was not. Padding is the failure mode to avoid."""


def ai_tailor(facts, jd, model=None, pivot=False, minimal=False, gaps=None, draft=None):
    system = SYSTEM + (PIVOT if pivot else "") + (MINIMAL if minimal else "")
    if gaps:
        system += IMPROVE.format(draft=json.dumps(draft, indent=1) if draft else "(none)",
                                 gaps="\n".join(f"- {g}" for g in gaps))
    return chat(system, f"FACTS:\n{facts}\n\n---\n\nJOB DESCRIPTION:\n{jd}",
                RESUME_SCHEMA, "resume", model)


DRAFT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["lines"],
    "properties": {"lines": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["requirement", "line"],
        "properties": {"requirement": {"type": "string"}, "line": {"type": "string"}}}}},
}

DRAFT = """The candidate has ticked requirements from a job description that they say they
have done, but which their resume does not currently state. Draft one resume bullet for each.

Ground every draft in the RESUME where it supports the requirement - reuse its real tools,
systems, scale and context so the line reads as part of the same career, not a generic
template. Where the resume says nothing relevant, write the plainest honest form of the
requirement and nothing more.

Hard rules: no metrics, percentages, durations, team sizes or employer names unless they
already appear in the RESUME. No superlatives. One sentence, past tense, 12-25 words, the
concrete thing that was done. These drafts are shown to the candidate to correct before
anything is used - your job is to save them typing, not to decide what is true."""


def draft_lines(facts, reqs):
    out = chat(DRAFT,
               f"RESUME:\n{facts}\n\n---\n\nREQUIREMENTS:\n" +
               "\n".join(f"- {q}" for q in reqs),
               DRAFT_SCHEMA, "drafts")["lines"]
    got = {d["requirement"]: d["line"] for d in out}
    return [{"requirement": q, "line": got.get(q, "")} for q in reqs]


def resume_text(r):
    """Everything the JD is actually scored against - what the page will show."""
    parts = [r.get("title", ""), r.get("summary", "")]
    parts += [i for c in r.get("skills", []) for i in c.get("items", [])]
    parts += [c.get("category", "") for c in r.get("skills", [])]
    parts += [b for j in r.get("experience", []) for b in j.get("bullets", [])]
    parts += [b for pr in r.get("projects", []) for b in pr.get("bullets", [])]
    return " ".join(parts)


JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["requirements"],
    "properties": {"requirements": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["requirement", "covered", "evidence"],
        "properties": {
            "requirement": {"type": "string"},
            "covered": {"type": "string", "enum": ["yes", "partial", "no"]},
            "evidence": {"type": "string",
                         "description": "The resume wording that evidences it, or why not."},
        }}}},
}

JUDGE = """You are screening a resume against a job description, the way a technical hiring
manager would.

List every distinct requirement the job description states - skills, tools, responsibilities,
domains. For each, judge whether the RESUME evidences it:
  yes     - the resume shows this work, even under different wording. Judge the substance,
            not the vocabulary: "container and dependency scanning with Trivy and Grype"
            evidences SCA; "Terraform policy validation" evidences policy-as-code; "RBAC,
            network policies, secure ingress" evidences Kubernetes hardening.
  partial - adjacent or implied, but a screener would want to ask about it.
  no      - the resume shows nothing that evidences this.

Quote the resume wording in "evidence" for yes and partial. Be strict: do not credit a
requirement because the resume sounds generally impressive. Equally, do not mark something
"no" merely because the resume used a different word for the same work."""


def judge(resume_str, jd, model=None):
    """LLM screen: coverage per requirement. Far better than keyword overlap, which
    penalises a correct resume for naming the same work differently."""
    reqs = chat(JUDGE, f"JOB DESCRIPTION:\n{jd}\n\n---\n\nRESUME:\n{resume_str}",
                JUDGE_SCHEMA, "screen", model or JUDGE_MODEL)["requirements"]
    if not reqs:
        return 0, [], []
    pts = sum({"yes": 1.0, "partial": 0.5}.get(q["covered"], 0.0) for q in reqs)
    gaps = [q["requirement"] for q in reqs if q["covered"] == "no"]
    weak = [{"requirement": q["requirement"], "evidence": q["evidence"]}
            for q in reqs if q["covered"] == "partial"]
    return round(100 * pts / len(reqs)), gaps, weak


def gaps_for(r, jd):
    """Top JD keywords the generated resume does not cover, in the JD's own spelling."""
    surface = {}
    kw = jd_keywords(jd, surface)
    have = set(tokens(resume_text(r)))
    top = sorted(kw, key=lambda w: -kw[w])[:30]
    missed = [w for w in top if w not in have and not _near(w, have)]
    covered = sum(kw[w] for w in top if w in have or _near(w, have))
    score = round(100 * covered / (sum(kw[w] for w in top) or 1))
    return score, [surface.get(w, w) for w in missed]


IMPROVE = """

REVISION PASS. Below is your best draft so far. It already scored well - your job is to
IMPROVE IT, not to start over. Keep its structure, its wording and its ordering intact
except where a change closes one of the gaps listed.

PREVIOUS DRAFT:
{draft}

A hiring manager screened it and found these requirements unmet or only half-evidenced:

{gaps}

For each, check FACTS. If the candidate genuinely did that work under a different name, say
it using the job description's wording. If FACTS shows related work you left out or
compressed too far, bring it back. If FACTS does not support it, leave it out and record it
in "notes" - a lower score is the correct outcome for work the candidate has not done, and
inventing coverage is the failure this whole task is defined against.

Change only what closes a gap. Every edit that does not close one risks losing a point you
already earned."""


def tailor_best(facts, jd, pivot, minimal, target=95, tries=4):
    """Re-tailor until the resume covers the JD, or we run out of honest material."""
    best, best_score, best_gaps, best_weak, history = None, -1, [], [], []
    feedback = []
    for _ in range(tries):
        r = ai_tailor(facts, jd, pivot=pivot, minimal=minimal, gaps=feedback, draft=best)
        score, gaps, weak = judge(resume_text(r), jd)
        history.append(score)
        if score > best_score:
            best, best_score, best_gaps, best_weak = r, score, gaps, weak
        if score >= target:
            break
        # Feed back both the misses and the half-credits: upgrading a "partial" is usually
        # the cheapest honest point available.
        feedback = gaps + [f'{w["requirement"]} - {w["evidence"]}' for w in weak]
        if not feedback:
            break
        if len(history) >= 3 and score <= max(history[:-1]):
            break          # plateaued: further passes cost money and add nothing
    best["score"], best["gaps"], best["weak"] = best_score, best_gaps, best_weak
    best["history"] = history
    return best


def diagnostics(out, source_text, jd_text):
    """Match score + gap list, computed deterministically for either mode."""
    surface = {}
    kw = jd_keywords(jd_text, surface)
    have = set(tokens(source_text))
    top = sorted(kw, key=lambda w: -kw[w])[:30]
    covered = {w for w in kw if w in have or _near(w, have)}
    out.setdefault("missing",
                   [surface.get(w, w) for w in sorted(kw, key=lambda w: -kw[w])[:40]
                    if w not in covered][:12])
    out["hits"] = [surface.get(w, w) for w in top if w in covered]
    out["match"] = round(100 * sum(kw[w] for w in top if w in covered) / (sum(kw[w] for w in top) or 1))
    return out


app = Flask(__name__)
def _secret():
    """A key that survives restarts, or every redeploy logs out every user.
    Set SECRET_KEY in production - a file-backed key can't be shared across machines."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    f = pathlib.Path(os.environ.get("DB_PATH", HERE / "data.db")).with_name(".secret_key")
    if not f.exists():
        f.write_text(os.urandom(32).hex())
        f.chmod(0o600)
    return f.read_text().strip()


app.secret_key = _secret()
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024        # 4 MB: no resume is bigger
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"             # survives the OAuth redirect back
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("HTTPS", "").lower() in ("1", "true", "yes")
ALLOWED = {".pdf", ".docx", ".txt", ".md", ".html", ".pptx", ".xlsx"}


@app.errorhandler(413)
def too_big(_):
    return render_template("index.html", need_resume=True,
                           error="That file is over 4 MB. Upload a text-based PDF or DOCX."), 413

store.init()

# BYOK: a visitor's own OpenAI key, held in memory only and dropped when the process
# exits. Never written to the database, never put in the cookie, never logged.
# ponytail: a plain dict. It does not survive a restart or span workers - the user
# re-enters the key, which is the correct tradeoff against storing other people's
# credentials at rest.
KEYS = {}

# When set, the shared key is disabled and every visitor must bring their own.
BYOK_ONLY = os.environ.get("BYOK_ONLY", "").lower() in ("1", "true", "yes")
SERVER_KEY = os.environ.get("OPENAI_API_KEY", "")


def provider_of(key):
    """OpenAI keys start sk-, Anthropic keys sk-ant-. Order matters: check ant first."""
    return "anthropic" if key.startswith("sk-ant-") else "openai"


def active_key():
    """(key, whose, provider) for this request."""
    own = KEYS.get(session.get("sid"))
    if own:
        return own, "own", provider_of(own)
    if SERVER_KEY and not BYOK_ONLY:
        return SERVER_KEY, "shared", provider_of(SERVER_KEY)
    return None, "none", None


def extract(path, filename):
    """Any résumé format -> text, in pure Python.

    MarkItDown covers pdf/docx/pptx/xlsx/html/txt, so there is no poppler-utils system
    package, no python-docx special case and no macOS-only textutil branch. It also
    drops the column padding pdftotext leaves behind: ~12% fewer input tokens.
    """
    from markitdown import MarkItDown
    try:
        return MarkItDown().convert(path).text_content
    except Exception as e:
        raise RuntimeError(f"Could not read {pathlib.Path(filename).suffix or 'that file'} "
                           f"({type(e).__name__}). Try a PDF or DOCX with selectable text.")


def me():
    """The signed-in user, or None."""
    return store.get_user(session.get("uid"))


def my_facts():
    row = store.get_facts(session.get("sid"))
    return row["facts"] if row else ""


@app.post("/upload")
def upload():
    f = request.files.get("resume")
    if not f or not f.filename:
        return render_template("index.html", need_resume=True, error="Pick a resume file first.")
    if pathlib.Path(f.filename).suffix.lower() not in ALLOWED:
        return render_template("index.html", need_resume=True,
                               error="Unsupported file type. Use PDF, DOCX or TXT.")
    with tempfile.NamedTemporaryFile(suffix=pathlib.Path(f.filename).suffix, delete=False) as tmp:
        f.save(tmp.name)
        try:
            text = extract(tmp.name, f.filename).strip()
        except Exception as e:
            os.unlink(tmp.name)
            return render_template("index.html", need_resume=True, error=str(e))
    os.unlink(tmp.name)
    text = text[:60000]                                    # a resume is never longer
    if len(text) < 200:
        return render_template("index.html",
                               error="Could not read that file - it may be a scanned image. "
                                     "Try a text-based PDF, a .docx, or paste the text.")
    session["sid"] = session.get("sid") or uuid.uuid4().hex
    store.put_facts(session["sid"], text, f.filename, uid=session.get("uid"))
    session["filename"] = f.filename
    return redirect(url_for("index"))


@app.post("/draft-facts")
def draft_facts():
    """Ticked requirements -> suggested wording, for the user to correct."""
    reqs = [q for q in request.form.getlist("req") if q.strip()][:15]
    facts = my_facts()
    if not reqs or not facts:
        return do_tailor()
    key, whose, provider = active_key()
    if not key:
        return render_template("index.html", need_key=True, whose=whose,
                               error="Add an API key first.")
    user = me()
    ok, used, limit, reason = store.quota(user, own_key=(whose == "own"))
    if not ok:
        return render_template("index.html", whose=whose, user=user, used=used, limit=limit,
                               upgrade=(reason == "upgrade"), need_login=(reason == "sign-in"),
                               error=QUOTA_MSG[reason].format(used=used, limit=limit))
    USAGE.clear()
    try:
        drafts = draft_lines(facts, reqs)
    except Exception as e:
        return render_template("index.html", error=f"{type(e).__name__}: {e}")
    store.record(session["sid"], "draft", calls=USAGE["calls"],
                 tok_in=USAGE["tok_in"], tok_out=USAGE["tok_out"], uid=session.get("uid"))
    return render_template("confirm.html", drafts=drafts,
                           jd=request.form.get("jd", ""),
                           mode=request.form.get("mode", "auto"))


@app.post("/add-facts")
def add_facts():
    """Confirmed lines land in the user's facts, then the resume is rebuilt."""
    sid = session.get("sid")
    if store.get_facts(sid):
        for line in request.form.getlist("line"):
            line = line.strip()[:400]
            if line:
                store.append_fact(sid, line)
    return do_tailor()


DEV_LOGIN = os.environ.get("DEV_LOGIN", "").lower() in ("1", "true", "yes")


@app.get("/login")
def login():
    if not auth.google_enabled:
        return render_template("index.html", whose=active_key()[1],
                               error="Sign-in is not configured on this deployment. "
                                     "Add your own API key instead.")
    session["oauth_state"] = auth.new_state()
    return redirect(auth.login_url(url_for("callback", _external=True), session["oauth_state"]))


@app.get("/auth/callback")
def callback():
    if not request.args.get("state") or request.args["state"] != session.pop("oauth_state", None):
        return render_template("index.html", error="Sign-in expired or was tampered with. "
                                                   "Please try again."), 400
    if request.args.get("error"):
        return redirect(url_for("index"))
    try:
        who = auth.exchange(request.args["code"], url_for("callback", _external=True))
    except Exception as e:
        return render_template("index.html", error=f"Google sign-in failed: {type(e).__name__}."), 400
    session["uid"] = store.upsert_user(who["sub"], who["email"], who["name"])
    session.permanent = True
    return redirect(url_for("index"))


@app.post("/dev-login")
def dev_login():
    """Local testing only - never enabled unless DEV_LOGIN=1 is set explicitly."""
    if not DEV_LOGIN:
        return "disabled", 403
    email = (request.form.get("email") or "dev@example.com").strip().lower()
    session["uid"] = store.upsert_user("dev-" + email, email, email.split("@")[0])
    session.permanent = True
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    session.pop("uid", None)
    return redirect(url_for("index"))


@app.post("/upgrade")
def upgrade():
    user = me()
    if not user:
        return redirect(url_for("login"))
    if not auth.razorpay_enabled:
        return render_template("index.html", whose=active_key()[1], user=user,
                               error="Payments are not configured on this deployment.")
    try:
        link = auth.payment_link(user["uid"], user["email"], user["name"],
                                 url_for("index", _external=True))
    except Exception as e:
        return render_template("index.html", whose=active_key()[1], user=user,
                               error=f"Could not start checkout: {type(e).__name__}.")
    return redirect(link)


@app.post("/webhook/razorpay")
def razorpay_webhook():
    """Razorpay signs the raw body; verify before trusting a single field of it."""
    raw = request.get_data()
    if not auth.verify_webhook(raw, request.headers.get("X-Razorpay-Signature", "")):
        app.logger.warning("razorpay webhook: bad signature")
        return "invalid signature", 400
    parsed = auth.payment_from_event(json.loads(raw))
    if not parsed:
        return "ignored", 200
    pid, uid, amount, currency = parsed
    if store.record_payment(pid, uid, amount, currency, "captured"):
        until = store.mark_paid(uid)
        app.logger.info("payment %s -> uid %s paid until %.0f", pid, uid, until)
    return "ok", 200


@app.post("/key")
def set_key():
    """Accept a visitor's own OpenAI key. Verified before it is accepted, kept in memory."""
    key = request.form.get("key", "").strip()
    session["sid"] = session.get("sid") or uuid.uuid4().hex
    if not key:
        KEYS.pop(session["sid"], None)
        return redirect(url_for("index"))
    if not key.startswith("sk-"):
        return render_template("index.html", need_resume=not my_facts(),
                               error="That does not look like an API key - OpenAI keys start "
                                     "with sk- and Anthropic keys with sk-ant-.")
    provider = provider_of(key)
    try:
        if provider == "anthropic":
            import anthropic
            anthropic.Anthropic(api_key=key).models.list()
        else:
            from openai import OpenAI
            OpenAI(api_key=key).models.list()
    except Exception as e:
        return render_template("index.html", need_resume=not my_facts(),
                               error=f"That key was rejected by "
                                     f"{'Anthropic' if provider == 'anthropic' else 'OpenAI'}: "
                                     f"{type(e).__name__}.")
    KEYS[session["sid"]] = key
    return redirect(url_for("index"))


@app.post("/reset")
def reset():
    sid = session.pop("sid", None)
    store.drop(sid)
    KEYS.pop(sid, None)
    session.pop("filename", None)
    return redirect(url_for("index"))


@app.get("/healthz")
def healthz():
    """Liveness for the platform: checks the app answers and the database is reachable."""
    try:
        store.run("SELECT 1 AS ok", (), "one")
    except Exception as e:
        return {"ok": False, "db": f"{type(e).__name__}"}, 503
    return {"ok": True, "db": "postgres" if store.PG else "sqlite"}, 200


@app.get("/")
def index():
    _, whose, provider = active_key()
    user = me()
    ok, used, limit, _ = store.quota(user, own_key=(whose == "own"))
    ctx = dict(whose=whose, provider=provider, user=user, used=used, limit=limit,
               can_generate=ok, google=auth.google_enabled, razorpay=auth.razorpay_enabled,
               dev_login=DEV_LOGIN)
    if not my_facts():
        return render_template("index.html", need_resume=True, **ctx)
    return render_template("index.html", **ctx)


@app.post("/tailor")
def do_tailor():
    jd = request.form.get("jd", "").strip()
    url = request.form.get("url", "").strip()
    if url and not jd:
        try:
            jd = fetch(url)
        except Exception as e:
            return render_template("index.html", error=f"Could not fetch that URL ({e}). Paste the JD text instead.")
    if not jd:
        return render_template("index.html", error="Give me a JD - paste the text or a link.")

    facts = my_facts()
    if not facts:
        return render_template("index.html", need_resume=True,
                               error="Upload your resume first.")
    mode = request.form.get("mode", "auto")
    ceiling = diagnostics({}, facts, jd)["match"]      # what the FACTS could ever support
    pivot = mode in ("pivot", "minimal") or (mode == "auto" and ceiling < 35)
    minimal = mode == "minimal" or (mode == "auto" and ceiling < 15)
    fix = bool(request.form.get("fix"))
    sid = session["sid"]
    key, whose, provider = active_key()
    if not key:
        return render_template("index.html", whose=whose, need_key=True,
                               error="Add an API key to generate a résumé.")
    user = me()
    ok, used, limit, reason = store.quota(user, own_key=(whose == "own"))
    if not ok:
        return render_template("index.html", whose=whose, user=user, used=used, limit=limit,
                               upgrade=(reason == "upgrade"), need_login=(reason == "sign-in"),
                               error=QUOTA_MSG[reason].format(used=used, limit=limit))

    USAGE.clear()
    try:
        if fix:
            r = tailor_best(facts, jd, pivot, minimal)
        else:
            r = ai_tailor(facts, jd, pivot=pivot, minimal=minimal)
            r["score"], r["gaps"], r["weak"] = judge(resume_text(r), jd)
            r["history"] = [r["score"]]
    except NeedKey:
        return render_template("index.html", need_key=True,
                               error="Add an API key to generate a résumé.")
    except Exception as e:
        return render_template("index.html", error=f"{type(e).__name__}: {e}")

    store.record(sid, "fix" if fix else "tailor", calls=USAGE["calls"],
                 tok_in=USAGE["tok_in"], tok_out=USAGE["tok_out"], score=r["score"],
                 uid=session.get("uid"))
    r["notes"] = r.pop("notes", [])
    r["usage"] = dict(USAGE)
    r.update(pivot=pivot, minimal=minimal, mode=mode, ceiling=ceiling, fixed=fix, jd=jd,
             whose=whose, provider=provider, user=user,
             remaining=(None if limit is None else max(0, limit - used - 1)))
    app.logger.info("ceiling %d%% score %s pivot=%s minimal=%s", ceiling, r["history"], pivot, minimal)
    return render_template("resume.html", r=r)


def test():
    kw = jd_keywords("secure the pipeline, harden kubernetes and automate scanning")
    assert kw, "no keywords extracted"
    assert score("security hardening", kw) > 0, "secure/security must match"
    assert score("supercalifragilistic", kw) == 0, "unrelated words must not match"
    d = diagnostics({}, "I harden kubernetes and automate scanning", "harden kubernetes scanning")
    assert d["match"] > 50 and d["hits"], d
    fake = {"title": "SRE", "summary": "I harden kubernetes clusters",
            "skills": [{"category": "Infra", "items": ["terraform"]}],
            "experience": [{"bullets": ["automated scanning"]}], "projects": []}
    sc, gaps = gaps_for(fake, "harden kubernetes terraform scanning pagerduty oncall")
    assert 0 < sc < 100, sc
    assert "pagerduty" in " ".join(gaps), gaps
    assert "kubernetes" not in " ".join(gaps), "covered term must not be listed as a gap"
    assert diagnostics({}, "I bake bread", "harden kubernetes")["match"] == 0, "unrelated must score 0"
    assert strip_html("<p>hi<script>x=1</script></p>").strip() == "hi", "html strip broken"
    print("ok")


if __name__ == "__main__":
    if "test" in sys.argv:
        test()
    elif "models" in sys.argv:
        from openai import OpenAI
        for m in sorted(x.id for x in OpenAI().models.list()):
            print(m)
    else:
        app.run(port=5111, debug=True)
