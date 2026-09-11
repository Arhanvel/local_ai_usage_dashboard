"""Text heuristics shared by the ingester and the insight queries.

Everything here is a plain regex or a lookup table, kept in one place so the
rules that turn transcript text into categories are easy to audit and tune.
"""
import re

# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

# Only these prompt sources originate with a person at the keyboard. A "user"
# record also carries SDK-injected prompts, task notifications and the
# instructions handed to sub-agents.
HUMAN_SOURCES = ("typed", "queued", "suggestion_accepted")

# Harness-generated user turns that look like prompts but are not.
# (Interrupts and compaction summaries are routed to events before this list
# is consulted, so they do not appear here.)
NOT_HUMAN_PREFIXES = (
    "Caveat:", "<command-", "<local-command", "<system-reminder>",
    "<user-prompt-submit-hook>", "<task-notification",
    "<bash-stdout", "<bash-stderr", "<bash-input",
    # Skill / slash-command bodies are injected as a user turn right after
    # the command itself.
    "Base directory for this skill", "# /",
)

RE_SYSREM = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
RE_CMD_NAME = re.compile(r"<command-name>([^<]*)</command-name>")
RE_CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)

# Opener that only makes sense as a follow-up: the session started mid-thought.
RE_CONTINUATION = re.compile(
    r"^(yes|ok|okay|continue|go|proceed|do it|now |also |and |next|then |it |still|no[,. ])", re.I)

# Intent buckets, non-exclusive, matched against lowercased prompt text.
INTENTS = [
    ("bug/fix", re.compile(r"\b(bug|fix|broken|error|fail|crash|doesn'?t work|not work|exception|wrong)\b", re.I)),
    ("correction", re.compile(r"^(no[,. ]|nope|wrong|that'?s not|not what|i said|again|still |you (didn'?t|forgot|were))", re.I | re.M)),
    ("verify/test", re.compile(r"\b(test|verify|check|confirm|make sure|validate|run (it|the))\b", re.I)),
    ("commit/git", re.compile(r"\b(commit|push|branch|merge|rebase|pull request|\bpr\b|git)\b", re.I)),
    ("ticket", re.compile(r"\b(jira|ticket|issue|story|epic)\b", re.I)),
    ("build/deploy", re.compile(r"\b(build|deploy|release|docker|pipeline|ci\b|publish|install)\b", re.I)),
    ("explain/ask", re.compile(r"\b(what|why|how|explain|describe|tell me|show me|\?)", re.I)),
    ("continue/go", re.compile(r"^(continue|go|proceed|next|do it|yes|ok|okay|go ahead|carry on)\b", re.I)),
    ("design/ui", re.compile(r"\b(design|ui|ux|layout|css|style|theme|font|colou?r|responsive|mobile)\b", re.I)),
    ("refactor/clean", re.compile(r"\b(refactor|clean ?up|simplify|rename|reorganis|reorganiz|dedup|tidy)\b", re.I)),
    ("docs", re.compile(r"\b(readme|docs?|documentation|comment|changelog|write ?up)\b", re.I)),
    ("plan", re.compile(r"\b(plan|roadmap|architecture|approach|options|strategy|think (about|through))\b", re.I)),
    ("review", re.compile(r"\b(review|audit|critique|look over|check the code|code review)\b", re.I)),
    ("report", re.compile(r"\b(report|summary|summari[sz]e|overview|dashboard|chart)\b", re.I)),
]


def classify_intents(text):
    """Comma-joined intent buckets for a prompt ('' when nothing matches)."""
    if not text:
        return ""
    return ",".join(name for name, rx in INTENTS if rx.search(text))


def clean_prompt(text):
    """Strip harness-injected blocks and return (clean_text, slash_name).

    Returns ("", None) for text that is entirely harness noise (a bare
    command-stdout echo, an interrupt marker, a compaction summary...).
    """
    if not isinstance(text, str):
        return "", None
    raw = text
    slash = None
    m = RE_CMD_NAME.search(raw)
    if m and "<local-command-stdout>" not in raw:
        slash = m.group(1).strip() or None
        args = RE_CMD_ARGS.search(raw)
        text = (slash or "") + ((" " + args.group(1).strip()) if args and args.group(1).strip() else "")
    elif "<local-command-stdout>" in raw:
        return "", None
    text = RE_SYSREM.sub("", text).strip()
    return text, slash


def is_human_prompt(source, origin_kind, text, is_meta=False, entrypoint=None):
    """Does this user turn originate with the person, not the harness?

    ``promptSource`` settles it when present. Older builds omit it; then a
    turn counts as typed only in an interactive (``cli``) session and only
    when the text is not a harness-injected block.
    """
    if is_meta:
        return False
    if origin_kind == "human":
        return True
    if source:
        return source in HUMAN_SOURCES
    if entrypoint and entrypoint != "cli":
        return False
    if not text:
        return False
    return not text.startswith(NOT_HUMAN_PREFIXES)


# --------------------------------------------------------------------------
# Assistant replies
# --------------------------------------------------------------------------

# Friction signals in reply text. Checked in order; first hit wins. These are
# the phrasings of the banners Claude Code itself emits, not topics - a reply
# that *discusses* rate limits must not count, so the caller also requires the
# reply to be short and tool-free (see claude_ingest).
ERROR_KINDS = [
    ("usage_limit", re.compile(r"usage limit reached|Claude usage limit|weekly limit|5-hour limit|limit will reset", re.I)),
    ("rate_limit", re.compile(r"rate limit(?:ed|s)? (?:exceeded|reached|hit)|\b429\b|overloaded_error|\bOverloaded\b")),
    ("billing", re.compile(r"credit balance|insufficient credit|billing (?:issue|error|problem)", re.I)),
    ("fallback", re.compile(r"falling back to|fallback model|switched to [\w.-]+ model", re.I)),
    ("api_error", re.compile(r"^API Error|\"type\":\"error\"|prompt is too long|context.?length exceeded|max_tokens", re.I)),
]
ERROR_MAX_CHARS = 400


def classify_error(text, tool_uses=0):
    """Error kind for a reply that looks like a harness banner, else None."""
    if not text or tool_uses or len(text) > ERROR_MAX_CHARS:
        return None
    for name, rx in ERROR_KINDS:
        if rx.search(text):
            return name
    return None


# --------------------------------------------------------------------------
# Git outcomes from recorded shell output
# --------------------------------------------------------------------------

# `[branch sha] subject` is what `git commit` prints and nothing else does.
RE_COMMIT = re.compile(r"^\[([^\s\]]+)\s+([0-9a-f]{7,40})\]\s*(.*)$", re.M)
RE_STAT = re.compile(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?")
RE_PUSH = re.compile(r"^To\s+(\S+)[ \t\r]*$", re.M)  # tolerate CRLF from native shells
RE_PR = re.compile(r"https?://\S+?/(?:pull|pull-requests|merge_requests)/\d+")
RE_GIT_SUBCMD = re.compile(r"\bgit\s+(?:--?\S+\s+)*([a-z][a-z\-]+)")


def scan_git_output(text):
    """Commits, pushes and PR links found in one shell result."""
    out = {"commits": [], "pushes": [], "prs": []}
    if not text or ("[" not in text and "To " not in text and "/pull" not in text
                    and "merge_requests" not in text):
        return out
    seen = set()
    for m in RE_COMMIT.finditer(text):
        branch, sha, subject = m.group(1), m.group(2), m.group(3).strip()
        if sha in seen:
            continue
        seen.add(sha)
        files = ins = dels = 0
        st = RE_STAT.search(text, m.end(), min(len(text), m.end() + 400))
        if st:
            files = int(st.group(1) or 0)
            ins = int(st.group(2) or 0)
            dels = int(st.group(3) or 0)
        out["commits"].append({"sha": sha, "branch": branch, "subject": subject[:200],
                               "files": files, "insertions": ins, "deletions": dels})
    out["pushes"] = [m.group(1) for m in RE_PUSH.finditer(text)]
    out["prs"] = sorted(set(RE_PR.findall(text)))
    return out


# --------------------------------------------------------------------------
# Issue keys
# --------------------------------------------------------------------------

RE_TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")
TICKET_BLOCKLIST = frozenset("""
UTF UTF8 ISO SHA MD RFC CVE CWE HTTP HTTPS IPV AES RSA TLS SSL PEP ES UTC GMT ASCII
BASE SP WCAG ARIA CSS HTML JSON XML SQL API3 X WIN MACOS PYTHON NODE JAVA NET PHP GPT
CLAUDE COVID GB MB KB TB USD EUR AM PM Q H FY T V REV PART STEP FIG TABLE SECTION
CHAPTER PR SHA1 SHA256 MD5 ID UUID GUID CSV PDF PNG JPG SVG RGB HEX DPI FPS MS
""".split())


def find_tickets(text):
    """Issue keys in text, blocklist applied. Returns a set of 'ABC-123'."""
    if not text:
        return set()
    # Real trackers use word-like prefixes (REQ, DPP, ABC2); a lone letter plus
    # digits is almost always a regex range or an identifier fragment.
    return {f"{p}-{n}" for p, n in RE_TICKET.findall(text)
            if p not in TICKET_BLOCKLIST and sum(c.isalpha() for c in p) >= 2}


# --------------------------------------------------------------------------
# File categories: what counts as "code written"
# --------------------------------------------------------------------------

FILE_CATEGORIES = {
    "code": """py js ts tsx jsx go rs java kt swift c cc cpp h hpp cs rb php pl sh ps1 psm1
               bash zsh sql lua dart scala ex exs erl hs ml vue svelte astro r m mm""".split(),
    "markup": "html htm css scss sass less xml svg tpl twig blade jinja jinja2 ejs".split(),
    "config": """json yaml yml toml ini env conf cfg gradle tf tfvars properties lock
                 dockerfile editorconfig gitignore npmrc""".split(),
    "docs": "md mdx txt rst adoc org".split(),
    "data": "csv tsv jsonl ndjson parquet db sqlite".split(),
}
_EXT_TO_CATEGORY = {"." + e: cat for cat, exts in FILE_CATEGORIES.items() for e in exts}
SOURCE_CATEGORIES = ("code", "markup")


def file_category(ext):
    return _EXT_TO_CATEGORY.get((ext or "").lower(), "other")


def ext_of(path):
    """Lower-cased extension with the dot ('.py'), or None."""
    if not path:
        return None
    base = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in base:
        return None
    return "." + base.rsplit(".", 1)[-1].lower()


# --------------------------------------------------------------------------
# Prompt clustering helpers
# --------------------------------------------------------------------------

STOPWORDS = frozenset("""
a an the and or but if then else of to in on at by for with from as is are was were be been
being it its this that these those i you we they he she me my your our their them him her
do does did done doing have has had having can could should would will shall may might must
not no yes so just also very really please thanks thank ok okay now here there what which who
whom whose when where why how all any some each more most other such only own same than too
s t don ve ll re d m into over under again further once about up down out off above below
between through during before after let make made use using used want need like get got
one two first new file files code line lines thing things way still""".split())

_RE_URL = re.compile(r"https?://\S+")
_RE_TICKET_TOKEN = re.compile(r"\b[A-Za-z]+-\d+\b")
_RE_NUM = re.compile(r"\b\d+\b")
_RE_PATH = re.compile(r"(?:[a-z]:)?[\\/][\w\-.\\/]+", re.I)
_RE_FENCE = re.compile(r"```.*?```", re.S)
_RE_TOKEN = re.compile(r"[a-z0-9_\-/\.]+")


def normalize_prompt(text):
    """Lowercase, mask volatile details (urls, numbers, paths, keys)."""
    t = (text or "").lower()
    t = _RE_FENCE.sub(" ", t)
    t = _RE_URL.sub(" URL ", t)
    t = _RE_TICKET_TOKEN.sub(" TICKET ", t)
    t = _RE_PATH.sub(" PATH ", t)
    t = _RE_NUM.sub(" N ", t)
    return " ".join(t.split())


def prompt_tokens(text, limit=120):
    """Content-word set used for Jaccard similarity."""
    toks = []
    for tok in _RE_TOKEN.findall(normalize_prompt(text)):
        if len(tok) <= 2 or tok in STOPWORDS:
            continue
        toks.append(tok)
        if len(toks) >= limit:
            break
    return frozenset(toks)


def family_key(text):
    """Exact-repeat key: digits masked, whitespace collapsed, 160 chars."""
    return _RE_NUM.sub("N", " ".join((text or "").lower().split()))[:160]
