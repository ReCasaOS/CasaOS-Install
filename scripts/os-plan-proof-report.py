#!/usr/bin/env python3
"""Turn what os-plan-proof.sh wrote into verdicts, or say the run is invalid.

    os-plan-proof-report.py <out dir> <d11>

Prints a markdown report (and writes it to <out dir>/report.md).

Exit status: 0 only when no verdict is a FAIL and the run is valid. 1 for a FAIL (the plan changed something on the machine, said something that is not
true, did not refuse what it should) and 1 for an INVALID RUN (the harness could not do what it set out to: a call got no answer of the route, a status that says
unknown: the core could not tell whether a plan can be made, a step could not dirty the box or put it back, a plan failed in its update because a mirror was not reached, the times of a call
are not those of a call of this run (a time of this decade, the end after the beginning, both between the first marker and `finished`), the evidence is missing). An invalid run proves nothing and is never green. A step that did not run is INVALID,
not a verdict. NOT TESTABLE is neutral and visible: what this guest cannot do, with the reason; it is never a pass.

What the guest did (os-plan-proof.sh), in this order, on a fresh Debian 11 cloud image with ReCasaOS, Docker 28.0.4 and a few witness containers:
  - a control of about five minutes (CONTROL_SECONDS, 320 by default) between two snapshots, with PID 1's own journal of it: what the box changes by itself
    is not held against the plan (the same discipline as the readiness proof: a unit that has an entry in the journal of the control is noise, and a
    journal that cannot be believed leaves nothing out);
  - the WINDOW: a snapshot (every file of /etc, /var/lib/dpkg, /var/lib/apt (lists and periodic among them), /var/cache/apt, /var/lib/casaos, /var/log/apt and
    /var/log/casaos with its name, size, times, mode, owner and content hash; the service units with their invocation ids; the listening sockets; the
    listing of /var/tmp; the witness containers' start times), then GET of the plan before any plan, GET of the runbook before any plan, POST, a second
    POST at once, GET every second until the job is done (15 minutes at most), then the same snapshot. Only /var/log/casaos may differ, and /var/tmp
    must list the same: the plan writes a sandbox directory there, and removes it (/var/log/apt is looked at because the planner of apt writes its log
    there, which the sandbox's apt-config turns off). The hooks of the box (apt's update-success-stamp) are the named expected catch where the box has
    one: the plan neutralises them in the sandbox;
  - the runbook of that plan (every block read by sh -n on the guest; its tree against what this report computes itself from the box's real sources);
  - a second plan at once (same plan_id), a plan with a hold on base-files (another plan_id, blocked by dist_kept_back, no runbook: a plan that does not keep the held
    package back lost it, and is a FAIL, never NOT TESTABLE), the DIRTY matrix
    (a .sources file, a cdrom line, an alias, a line already at bookworm, a mixed file, a mirror that does not resolve, dpkg's lock held, a small tmpfs
    over /var/tmp: each asks the plan, asserts the exact code (the failed update also has to carry, in its data, the list of the files that the sandbox left out of apt's configuration,
    the plan's own), undoes the dirt, and looks for a sandbox directory left behind; and three CANARIES, a
    fragment of apt's configuration with a real hook of `apt-get update` in it that a filter of the text does not see (a comment spliced into a word, an
    #include of another file), and a fragment with the option of the gpgv that apt runs as `_apt` (Acquire::gpgv::Options, `--log-file=<a file of the box's own lists of apt>`):
    the plan has to leave the fragment out, name it with its reason, and the file the hook (or gpgv) makes must not be there when the plan is done), and a last plan after
    the undos, which has the plan_id of the first.

What a verdict cannot be silent about: a run that has no reason to be invalid and made no verdict that it owes says so (a verdict that goes quiet must not be a
pass), and any exception of this script, on evidence it did not expect (a number of four hundred digits, an answer nested a hundred thousand deep), ends as
an INVALID RUN that names the exception, with the verdicts made so far.

The sources of the plan are judged against an independent computation (expected_sources below): the rules of the rewrite for the one shape of box this
leg builds (one-line .list files, debian.org and Docker's own source), written again here from the rules of the core's rewrite and checked against the core's own golden for
the measured box (the harness pins hash_current 35d17f59... and hash_generated b5112f83...). A box in another shape is an INVALID RUN, not a guess.

What this guest cannot do is NOT TESTABLE, with the reason (the core runs as root: not_root; a box that is not Debian 11: hop_not_planned and
identity_unsupported; a protected package removed: a clean image does not remove one, the guard is the Go tests' and the removed names above).
"""
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

if len(sys.argv) != 3:
    print("usage: os-plan-proof-report.py <out dir> <d11>", file=sys.stderr)
    sys.exit(2)
out_dir, leg = sys.argv[1], sys.argv[2]

LEGS = {"d11": dict(family="debian", version="11", codename="bullseye", to_version="12", to_codename="bookworm", hop="debian-11-12")}
PHASES = ("sources", "update", "upgrade", "dist-upgrade", "estimate", "parse")
VERDICTS = ("ok", "needs_acceptance", "blocked")
# the problems of a plan and their severity (the contract: pkg/osplan/plan.go)
PROBLEMS = {"removal_protected": "blocked", "removal_essential": "blocked", "removal_manual": "needs_acceptance", "dist_kept_back": "blocked",
            "disk_short": "blocked", "downgrade": "blocked", "docker_held": "info"}
BLOCKS = ("verify", "timers", "snapshot", "sources", "download", "minimal", "full", "prereboot", "after")
EXPECTED_REMOVALS = ("fuse", "libsemanage1")                      # the measured removals of the Debian 11 to 12 plan (os-upgrade-measure-d11-12)
MEASURED = {"dist_upgrade": dict(upgraded=384, new=75, removed=2, kept_back=0), "upgrade": dict(upgraded=274, new=0, removed=0, kept_back=111)}
TOLERANCE = 0.15
DOWNLOAD_MIN, DOWNLOAD_MAX = 100 * 10 ** 6, 2 * 10 ** 9           # plausible, and nothing more (the numbers re-tare the floors of the plan)
EXTRA_MAX = 8 * 10 ** 9
SANDBOX_FLOOR = 640 << 20                                         # what /var/tmp must have free to make a plan (osplan.MinFreeBytes)
DISK_PATHS = ("/var/cache/apt/archives", "/usr", "/")
LIFETIME = 24 * 3600
MAX_RUNBOOK = 256 << 10        # the core's MaxRunbookBytes (262144): a longer text is refused by the core (409 no_runbook, reason runbook_too_large) and never handed out
MAX_LISTED = 600
MAX_LEFT_OUT = 200             # the core's MaxLeftOut: the files of apt's configuration that a plan, or a failure, names as left out (the others are cut, and the cut is said)
MAX_SHOWN = 400
DETAIL_LINE = 200              # a line of the detail of a failure that carries apt's words (the core cuts them at this: its maxDetailChars)
INT_LIMIT = 1 << 63            # a number of the answers is a whole number of this size at the most: a longer one is not a count, and must not reach a float
MAX_DEPTH = 32                 # the nesting of an answer: the core's own are four levels deep
NOISE_MIN, NOISE_SHARE = 3, 0.05   # the rows that only one of the control's two snapshots has: a few are the box's own, a twentieth of them is a snapshot that is cut short
CONF_REASONS = ("not_simple", "has_directive", "not_allowed_setting", "too_large", "not_regular", "proxy_auto_detect")   # why the core leaves a file of apt's configuration out
CONF_MAX_BYTES = 64 << 10
CONF_MAX_FRAGMENTS = 200
HOLD = "base-files"                                               # the package the hold step holds: the minimal upgrade touches it
WITNESSES = ("p-always", "p-unless-stopped", "p-db", "p-web")
STAMP = "/var/lib/apt/periodic/update-success-stamp"
SANDBOX_PREFIX = "/var/tmp/casaos-os-plan."
CLOCK_SLACK = 3.0
POLL_ALLOW = 5.0
CURL_TIMEOUT = 28
CONTROL_BEFORE = 2.0       # seconds before the marker control_start (the guest's journal of the control starts two seconds early)
CONTROL_AFTER = 1.0        # seconds after the marker invariance_start (it ends a second late)
NO_ANSWER = (0, 401, 403, 404, 502, 503, 504)   # the route gave no answer: the harness could not ask, or the release has no such route, or the core is not up
EXEMPT = "/var/log/casaos"                       # the one place the plan may write
TREES = ("/etc", "/var/lib/dpkg", "/var/lib/apt", "/var/cache/apt", "/var/lib/casaos", "/var/log/apt", "/var/log/casaos")
COLS = ("type", "size", "mtime", "ctime", "mode", "owner", "digest")
MAX_NAMED = 12
RUNS = ("p1", "p2", "p3", "p4")


class Step:
    """a way of dirtying the guest and what the plan must answer: id; kind job (a 202, then a job that fails: code, in phase, with needle in what it says of the
    failure, and kind_in for the kinds of a failed update), refused (a 409 at once: code, with data holding what `data` says) or canary (a 202, then a plan that
    is done, which names the fragment frag of apt's configuration as left out for the reason `reason`, and which has not made the file `file` that the fragment makes
    when it runs: a name in /var/lib/apt/periodic that a hook touches, or the full path that the option of gpgv writes its log to)"""

    def __init__(self, id, what, kind, code, phase="", needle="", kind_in=(), data=None, frag="", reason="", file=""):
        self.id, self.what, self.kind, self.code, self.phase, self.needle, self.kind_in, self.data = id, what, kind, code, phase, needle, kind_in, data or {}
        self.frag, self.reason, self.file = frag, reason, file


# in the order the guest runs them: the ones that fail in the first phase, then the refusals, then the one that fetches the lists of the target and fails
STEPS = [
    Step("format", "a .sources (deb822) file in sources.list.d", "job", "sources_format", phase="sources", needle="osp-deb822.sources"),
    Step("cdrom", "a cdrom: line", "job", "sources_cdrom", phase="sources", needle="osp-cdrom.list"),
    Step("alias", "a suite that is an alias (stable)", "job", "sources_alias", phase="sources", needle="osp-alias.list"),
    Step("target", "a line that is already at bookworm", "job", "sources_target", phase="sources", needle="osp-target.list"),
    Step("mixed", "a file with a line of the distribution and one of another repository", "job", "sources_mixed_file", phase="sources", needle="osp-mixed.list"),
    Step("busy", "dpkg's lock held by another process", "refused", "busy"),
    Step("noroom", "a small tmpfs over /var/tmp", "refused", "no_room", data={"path": "/var/tmp", "need": 671088640}),   # 671088640 = 640 MiB, what /var/tmp has to have free (SANDBOX_FLOOR)
    Step("deadmirror", "a country mirror of Debian's own list that does not exist (ftp9.zz.debian.org)", "job", "sandbox_update_failed", phase="update", needle="ftp9.zz.debian.org",
         kind_in=("network", "not_found")),
    Step("canarysplice", "a hook of apt-get update hidden by a comment spliced into its name", "canary", "", frag="99osp-canary-splice", reason="not_simple", file="osp-canary-splice"),
    Step("canaryinclude", "a hook of apt-get update held by a file that a fragment #includes", "canary", "", frag="99osp-canary-include", reason="has_directive", file="osp-canary-include"),
    Step("canarygpgv", "the option of the gpgv that apt runs, which names a file for it to write (Acquire::gpgv::Options)", "canary", "", frag="99osp-canary-gpgv", reason="not_allowed_setting",
         file="/var/lib/apt/lists/partial/osp-canary"),
]
# what this guest cannot do, and why (NOT TESTABLE is visible and never a pass)
NOT_TESTABLE = [
    ("not_root", "the core of the guest runs as root, as it does on every box: no request can be made of a core that does not"),
    ("hop_not_planned, identity_unsupported", "the guest is the one Debian 11 box the plan is made for: another system is another guest, and the Go tests of Plannable are the proof"),
    ("removal_protected, removal_essential", "a clean image has no protected package to remove: the guard is the Go tests', and the removed names above are the real data"),
    ("plan_unparsed, sandbox_solver_failed, timeout, canceled, internal", "nothing makes apt of the guest print what the parser refuses, or a solver fail, or the clock run out: the Go tests are the proof (the list of the files left out that the first two carry is held against the core's own answers in the fixtures, and on the guest only for the failed update)"),
]

lines = []
P = lines.append
verdicts = []   # (status, name, detail)
problems = []   # the run cannot be trusted: reasons


def read(name, default=""):
    try:
        with open(os.path.join(out_dir, name), encoding="utf-8", errors="replace", newline="") as f:
            return f.read().replace("\r\n", "\n")
    except OSError:
        return default


def read_bytes(name):
    try:
        with open(os.path.join(out_dir, name), "rb") as f:
            return f.read()
    except OSError:
        return None


def verdict(name, ok, detail=""):
    """ok None: the evidence is missing, which `invalid` has already said; no verdict is better than a made-up one"""
    if ok is not None:
        verdicts.append(("PASS" if ok else "FAIL", name, detail))


def not_testable(name, why):
    verdicts.append(("NOT TESTABLE", name, why))


def invalid(reason):
    if reason not in problems:
        problems.append(reason)


def finish():
    fails = [v for v in verdicts if v[0] == "FAIL"]
    head = "FAIL and INVALID RUN" if problems and fails else "INVALID RUN" if problems else "FAIL" if fails else "PASS"
    text = ["# OS plan proof, %s leg: %s" % (leg, head), ""]
    if problems:
        text += ["Nothing below that depends on the missing or untrustworthy evidence can be believed:", ""]
        text += ["- INVALID: " + p for p in problems] + [""]
    text += lines
    text += ["", "## Verdicts", ""]
    text += ["- **%s** %s%s" % (s, n, (": " + d) if d else "") for s, n, d in verdicts]
    skipped = [v[1] for v in verdicts if v[0] == "NOT TESTABLE"]
    passes = sum(1 for v in verdicts if v[0] == "PASS")
    text += ["", "%d of %d verdicts pass%s%s." % (passes, passes + len(fails), "; failing: " + "; ".join(v[1] for v in fails) if fails else "",
                                                "; %d not testable on this guest: %s" % (len(skipped), "; ".join(skipped)) if skipped else "")]
    out = "\n".join(text) + "\n"
    try:
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(out)
    except OSError:
        pass
    print(out)
    sys.exit(0 if not problems and not fails else 1)


URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\]]+")


def mask_url(u):
    scheme, _, rest = u.partition("://")
    query = ""
    if "?" in rest:
        rest, query = rest[:rest.index("?")], "?***"
    authority, tail = rest, ""
    if "/" in rest:
        authority, tail = rest[:rest.index("/")], rest[rest.index("/"):]
    if "@" in authority:
        authority = "***" + authority[authority.rindex("@"):]
    return scheme + "://" + authority + tail + query


def mask_text(s):
    return URL_IN_TEXT.sub(lambda m: mask_url(m.group(0)), s)


def cell(x, n=100):
    """a value of the box's own answer, for a cell of a table or a sentence: the user name, the password and the query of a URL masked (this text goes to the public
    summary of a run), printable ASCII, no bar, cut short"""
    return re.sub(r"[^\x20-\x7e]", "?", mask_text(str(x))).replace("|", "/")[:n]


def few(items, n=MAX_NAMED):
    items = list(items)
    return ", ".join(cell(i, 160) for i in items[:n]) + (" and %d more" % (len(items) - n) if len(items) > n else "")


def timestamp(s):
    """seconds since the epoch of an RFC 3339 time (no zone: UTC), or None"""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", s if isinstance(s, str) else "")
    if not m:
        return None
    base, frac, zone = m.groups()
    try:
        return datetime.fromisoformat(base + ("." + (frac + "000000")[:6] if frac else "") + ("+00:00" if zone in (None, "Z") else zone)).timestamp()
    except ValueError:
        return None


def number(name):
    try:
        t = float(read(name).strip())
    except ValueError:
        return None
    return t if math.isfinite(t) else None


def plausible(t):
    """a time in seconds since the epoch that a guest of this decade can have written: not NaN, not infinite, not before 2001 and not after 2096"""
    return isinstance(t, (int, float)) and 1e9 <= t <= 4e9


def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and abs(x) < INT_LIMIT


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and (abs(x) < INT_LIMIT if isinstance(x, int) else math.isfinite(x))


def too_deep(x, limit=MAX_DEPTH):
    """whether a value of an answer is nested deeper than the limit (looked at without recursion: a hostile answer is the case)"""
    stack = [(x, 1)]
    while stack:
        v, depth = stack.pop()
        if isinstance(v, (list, dict)):
            if depth > limit:
                return True
            stack.extend((c, depth + 1) for c in (v.values() if isinstance(v, dict) else v))
    return False


def strings(x, limit=MAX_LISTED):
    """a list of short printable strings"""
    return isinstance(x, list) and len(x) <= limit and all(isinstance(s, str) and len(s) <= MAX_SHOWN and not re.search(r"[\x00-\x1f\x7f]", s) for s in x)


try:
    if leg not in LEGS:
        invalid("the leg is d11, not %r" % leg)
        finish()
    L = LEGS[leg]


    # ---- the guest and what it recorded of itself ----------------------------------------------------------------------------------------

    def osrelease():
        out = {}
        for line in read("os-release").splitlines():
            m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
            if m:
                out[m.group(1)] = m.group(2).strip().strip("\"'")
        return out


    OSREL = osrelease()
    FACTS = {}
    for _line in read("box-facts").splitlines():
        _k, _, _v = _line.partition(" ")
        if _k:
            FACTS[_k] = _v.strip()

    if read("aborted").strip():
        # first: it is why the rest of the evidence is missing
        invalid("the guest stopped early: " + " ".join(read("aborted").split())[:300])
    if not OSREL.get("ID"):
        invalid("os-release is missing or empty: the guest did not say what system it is")
    elif (OSREL.get("ID"), OSREL.get("VERSION_ID")) != (L["family"], L["version"]):
        invalid("the guest is %s %s, the %s leg needs %s %s" % (OSREL.get("ID"), OSREL.get("VERSION_ID"), leg, L["family"], L["version"]))
    if read("leg").strip() != leg:
        invalid("the guest says it ran leg %r, the report is asked about %r" % (read("leg").strip(), leg))
    if not read("finished").strip():
        invalid("the guest did not reach the end of its run (no `finished` file)")
    if not read("timeline.tsv").strip():
        invalid("timeline.tsv is missing or empty: the run left no marker")


    def run_span():
        """(the time of the first marker of the timeline, the time the guest wrote in `finished`, to the second): every call of the run is between them. None when either is not a time"""
        marks = []
        for ln in read("timeline.tsv").splitlines():
            r = ln.split("\t")
            if len(r) == 3 and r[0] == "#":
                try:
                    marks.append(float(r[1]))
                except ValueError:
                    pass
        end = timestamp(read("finished").strip())
        marks = [t for t in marks if plausible(t)]
        return (min(marks), end) if marks and end is not None else None


    if read("timeline.tsv").strip() and read("finished").strip() and run_span() is None:
        invalid("timeline.tsv has no marker with a plausible time, or `finished` is not a time: the calls cannot be placed in the run")


    # ---- the calls ---------------------------------------------------------------------------------------------------------------------

    def headers_of(name):
        """the headers of the answer (the last block of what curl -D wrote: a redirect or a 100 has its own), lower case, or None when the call left none"""
        text = read(name + ".hdr", None)
        if text is None:
            return None
        blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
        out = {}
        for ln in (blocks[-1].splitlines() if blocks else [])[1:]:
            k, _, v = ln.partition(":")
            if k.strip():
                out[k.strip().lower()] = v.strip()
        return out


    def call(name, plain=False):
        """what the guest kept of one call, or None when it left no usable record of itself (INVALID): dict(name, http, curl, t0, t1, secs, kind, body, data, headers)
        where kind is '' for an answer that was read (whatever its status: 202 and 409 are answers of the plan), 'timeout' (curl gave up on it), 'cut' (curl got a
        status and then an error), 'unreadable' (not a JSON object). plain is a call whose answer is text and not the envelope (GET /v1/sys/version/current says the
        bare version): its text is its body"""
        code, curl = read(name + ".code").strip(), read(name + ".curl").strip()
        if not code:
            invalid("no answer was recorded for %s: that step did not run" % name)
            return None
        t0, t1 = number(name + ".t0"), number(name + ".t1")
        if not re.fullmatch(r"[0-9]{1,6}", curl) or not re.fullmatch(r"[0-9]{1,6}", code) or t0 is None or t1 is None:
            invalid("%s.curl, .code, .t0 or .t1 is missing or unreadable: the call left no usable record of itself" % name)
            return None
        # a time of a guest of this decade, a call that ends after it began, and both inside the run: a t0 that is damaged would switch off the check of a plan made before its POST
        span = run_span()
        if not (plausible(t0) and plausible(t1) and t0 <= t1) or (span is not None and not (span[0] - CLOCK_SLACK <= t0 and t1 <= span[1] + CLOCK_SLACK)):
            invalid("%s.t0 and .t1 are not the times of a call of this run (%s, %s): the call left no usable record of itself" % (name, cell(t0, 20), cell(t1, 20)))
            return None
        c = dict(name=name, http=int(code), curl=int(curl), t0=t0, t1=t1, secs=t1 - t0, kind="", body=None, data=None, headers=headers_of(name))
        if c["curl"] == CURL_TIMEOUT:
            c["kind"] = "timeout"
            return c
        if c["http"] in NO_ANSWER:
            err = " ".join(read(name + ".err").split())[:120]
            invalid("%s got no answer of the route: curl exit %s, HTTP %s%s%s" % (name, c["curl"], c["http"], " (%s)" % err if err else "",
                                                                                 ": the release under test has no such route" if c["http"] == 404 else ""))
            return None
        if c["curl"] != 0:
            c["kind"] = "cut"
            return c
        if plain:
            c["body"] = read(name + ".json")
            return c
        try:
            body = json.loads(read(name + ".json"))
        except (ValueError, RecursionError):
            body = None
        if not isinstance(body, dict) or too_deep(body):
            c["kind"] = "unreadable"
            return c
        c["body"], c["data"] = body, body.get("data")
        return c


    CALLS = {}


    def get(name, plain=False):
        if name not in CALLS:
            CALLS[name] = call(name, plain)
        return CALLS[name]


    def dd(c):
        """the data of an answer that was read, as an object ({} when it is not one)"""
        return c["data"] if c and isinstance(c["data"], dict) else {}


    def left_behind(call):
        """the directory that the core says it could not remove after a job (sandbox_left_behind of the status that ended it), shown as text; '' when the status says none or is not one.
        The core leaves the key out when there is none, so that a key that is there says it, whatever it holds"""
        d = dd(call) if call and not call["kind"] else {}
        if "sandbox_left_behind" not in d:
            return ""
        return cell(d["sandbox_left_behind"], 80) or "(an empty name)"


    def inner(d, *keys):
        for k in keys:
            d = d.get(k) if isinstance(d, dict) else None
        return d


    def envelope(c, status):
        """what is wrong with the envelope of an answer: success is the status, message is a string, data an object"""
        bad = []
        if c["body"].get("success") != status:
            bad.append("success is %s, not %d" % (cell(c["body"].get("success"), 20), status))
        if not isinstance(c["body"].get("message"), str) or not c["body"]["message"]:
            bad.append("no message")
        if not isinstance(c["body"].get("data"), dict):
            bad.append("data is not an object")
        return bad


    def refusal_of(c, status, code):
        """the 409 of the plan routes: data.error_code, data.error (the message too), and what makes the code precise in data.data. (reasons, the data object)"""
        if c is None or c["kind"]:
            return ["no usable answer (%s)" % (c["kind"] if c else "none")], {}
        bad = []
        if c["http"] != status:
            bad.append("HTTP %d, not %d" % (c["http"], status))
        bad += envelope(c, status)
        d = dd(c)
        if d.get("error_code") != code:
            bad.append("error_code is %s, not %s" % (cell(d.get("error_code"), 40), code))
        if not isinstance(d.get("error"), str) or not d["error"] or d["error"] != c["body"].get("message"):
            bad.append("error is not the message")
        return bad, d.get("data") if isinstance(d.get("data"), dict) else {}


    # ---- the box ---------------------------------------------------------------------------------------------------------------------

    P("## The box")
    P("")
    P("| | |")
    P("|---|---|")
    P("| system | %s |" % cell(read("os").strip() or OSREL.get("PRETTY_NAME", "?")))
    P("| leg | %s: %s %s to %s %s |" % (leg, L["family"], L["version"], L["family"], L["to_version"]))
    for k in ("kernel", "cpus", "memory_mb", "boot", "root", "boot_on", "virt", "docker", "docker_offer", "apt", "systemd", "smartctl", "packagekit_after_apt", "packagekit"):
        if FACTS.get(k):
            P("| %s | %s |" % (k, cell(FACTS[k])))
    P("")


    # ---- what the box's own files say: the independent computation of the sources and of the hooks left out -----------------------------------

    class NotComputable(Exception):
        pass


    SRC_NAME = re.compile(r"^[A-Za-z0-9_:-][A-Za-z0-9_:.-]*\.(?:list|sources)$")
    URI = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/\s]+)(/\S*)?$")
    DISTRO_SUFFIX = {"": "", "-updates": "-updates", "-security": "-security"}
    DROPPED_SUFFIX = ("-backports", "-backports-sloppy", "/updates", "-proposed-updates")
    DOCKER_HOST = "download.docker.com"


    def shown(s):
        s = mask_text(s)
        return s if len(s) <= MAX_SHOWN else s[:MAX_SHOWN - 3] + "..."


    def host_of(uri):
        m = URI.match(uri)
        if not m:
            return ""
        h = m.group(2).rsplit("@", 1)[-1]
        return m.group(1).lower() + "://" + h.lower()


    # Debian's own archive hosts, by their NAME and nothing else (the core's archiveHost): deb.debian.org and its CDN names, security, archive, httpredir, ftp, and the country mirrors of
    # Debian's own list (ftp.<cc>, ftp2.<cc>...). Not every host under debian.org is one (snapshot.debian.org is a frozen copy, people.debian.org is a person's directory): any other
    # host is the distribution's only by the Release file that apt kept of the suite, which this leg does not read: a box that has one is not computed here
    ARCHIVE_HOST = re.compile(r"^(?:(?:deb|security|archive|httpredir|ftp)\.debian\.org|ftp[0-9]?\.[a-z]{2}\.debian\.org|cdn-[a-z]+\.deb\.debian\.org)$")


    def official(host):
        """whether a host is one of Debian's own archive hosts, by its name (the core's archiveHost)"""
        return ARCHIVE_HOST.match(host) is not None


    def split_suite(suite):
        for suffix in ("-backports-sloppy", "-backports", "-proposed-updates", "-updates", "-security", "/updates"):
            if suite.endswith(suffix):
                return suite[:-len(suffix)], suffix
        return suite, ""


    def parse_line(file, n, raw):
        """(text as the core keeps it, repo or None): a line is blank, a comment, or one `deb|deb-src [options] URI SUITE [components]` with a comment after it; any
        other shape is not computed here, and the run is invalid (the core would refuse it, or read it in a way this leg does not know)"""
        text = raw.rstrip(" \t")
        code, hash_, comment = raw.partition("#")
        comment = ("#" + comment).rstrip(" \t") if hash_ else ""
        code = code.strip()
        if not code:
            return text, None
        if re.search(r"[^\x20-\x7e\t]", raw):
            raise NotComputable("%s line %d has a character that is not printable ASCII" % (file, n))
        m = re.match(r"^(deb|deb-src)[ \t]+(\[[^\[\]]*\][ \t]+)?(\S+)[ \t]+(\S+)((?:[ \t]+\S+)*)$", code)
        if not m or "[" in (m.group(5) or "") or "]" in (m.group(5) or "") or not URI.match(m.group(3)) or m.group(3).lower().startswith(("cdrom:", "file:", "copy:")):
            raise NotComputable("%s line %d is not of the shape `deb|deb-src [options] URI SUITE components` that this leg computes" % (file, n))
        return text, dict(typ=m.group(1), opts=(m.group(2) or "").strip(), uri=m.group(3), suite=m.group(4), comps=m.group(5).split(), comment=comment)


    def expected_sources(files):
        """what the core has to say of the sources of this box, from their text: (files [{file, action, lines}], paused [{file, line?, hosts, docker}], tree {path:
        content}, dropped, firmware, hash_current, hash_generated). `files` is {path: bytes} in apt's order (sources.list, then sources.list.d/<name>)"""
        hop_from, hop_to = L["codename"], L["to_codename"]
        out_files, paused, tree, dropped, firmware, kept = [], [], {}, 0, 0, 0
        for path, raw_bytes in files.items():
            name = path.split("/")[-1]
            in_parts = path.startswith("sources.list.d/")
            if name.endswith(".sources"):
                raise NotComputable("%s is a deb822 file: the core refuses it (sources_format), and this leg does not compute that" % path)
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise NotComputable("%s is not UTF-8" % path)
            if "\r" in text or "\x00" in text:
                raise NotComputable("%s has a carriage return or a NUL" % path)
            raw = text.split("\n")
            if raw[-1] == "":
                raw = raw[:-1]
            parsed = [parse_line(path, i + 1, r) for i, r in enumerate(raw)]
            repos = [(i, rec) for i, (_, rec) in enumerate(parsed) if rec]
            distro = [rec is not None and official(host_of(rec["uri"]).split("://")[1]) for _, rec in parsed]
            for i, rec in repos:
                h = host_of(rec["uri"]).split("://")[1]
                if not distro[i] and h != DOCKER_HOST:
                    raise NotComputable("%s line %d is of the host %s: this leg computes Debian's own archive hosts and Docker's own source only" % (path, i + 1, h))
            n_distro = sum(1 for i, _ in repos if distro[i])
            if in_parts and repos and n_distro == 0:
                hosts = sorted({host_of(rec["uri"]) for _, rec in repos})
                paused.append(dict(file=name, hosts=hosts, docker=True))   # a host that is not debian.org's is refused above unless it is Docker's: what is put aside is Docker's
                out_files.append(dict(file=name, action="paused", lines=[dict(n=i + 1, before=shown(parsed[i][0].strip()), after="", why="paused") for i, _ in repos]))
                continue
            if in_parts and repos and n_distro < len(repos):
                raise NotComputable("%s has lines of the distribution and of others: the core refuses it (sources_mixed_file)" % path)
            changes, new_lines = [], [t for t, _ in parsed]
            for i, rec in repos:
                old = parsed[i][0]
                if not distro[i]:
                    new = "# casaos-paused: " + old.strip()
                    paused.append(dict(file=name, line=i + 1, hosts=[host_of(rec["uri"])], docker=True))
                    why = "paused"
                else:
                    base, suffix = split_suite(rec["suite"])
                    if base != hop_from:
                        raise NotComputable("%s line %d is of the suite %s: this leg computes %s only" % (path, i + 1, rec["suite"], hop_from))
                    if suffix in DROPPED_SUFFIX:
                        new, why = "# casaos-dropped: " + old.strip(), "dropped"
                        dropped += 1
                    elif suffix in DISTRO_SUFFIX:
                        why = "suite"
                        uri = rec["uri"]
                        scheme, _, rest = uri.partition("://")
                        authority, slash, tail = rest.partition("/")
                        if authority.lower() == "archive.debian.org":
                            uri, why = scheme + "://deb.debian.org" + (slash + tail if slash else ""), why + ",host"
                        comps = list(rec["comps"])
                        if "non-free" in comps and "non-free-firmware" not in comps:
                            comps.append("non-free-firmware")
                            why += ",firmware"
                            firmware += 1
                        new = " ".join([rec["typ"]] + ([rec["opts"]] if rec["opts"] else []) + [uri, hop_to + DISTRO_SUFFIX[suffix]] + comps)
                        if rec["comment"]:
                            new += " " + rec["comment"]
                        kept += 1
                    else:
                        raise NotComputable("%s line %d has the suite %s: not one this leg computes" % (path, i + 1, rec["suite"]))
                new_lines[i] = new
                changes.append(dict(n=i + 1, before=shown(old.strip()), after=shown(new), why=why))
            out_files.append(dict(file=name, action="rewritten" if changes else "unchanged", lines=changes))
            content = "\n".join(new_lines) + "\n" if new_lines else ""
            if in_parts:
                tree["parts/" + name] = content
            else:
                tree["sources.list"] = content
        if kept == 0:
            raise NotComputable("no line of the distribution is left to plan against: the core refuses that (sources_format)")
        tree.setdefault("sources.list", "")
        return out_files, paused, tree, dropped, firmware, canonical_hash({p: b for p, b in files.items()}), canonical_hash({p: c.encode() for p, c in tree.items()})


    def canonical_hash(files):
        """the SHA-256 of files sorted by path, each as its path, its length and its bytes, each ended by a NUL (the core's, and the runbook's `srchash`)"""
        h = hashlib.sha256()
        for p in sorted(files):
            h.update(p.encode() + b"\x00" + str(len(files[p])).encode() + b"\x00" + files[p] + b"\x00")
        return h.hexdigest()


    def box_sources():
        """{path: bytes} of the box's own sources as the guest copied them, in apt's order, or None (INVALID)"""
        index = read("sources.index", None)
        if index is None or not index.strip():
            invalid("sources.index is missing or empty: the guest did not copy the box's sources, and what the plan says of them cannot be checked")
            return None
        names = [ln.strip() for ln in index.splitlines() if ln.strip()]
        files = {}
        for n in names:
            if n != "sources.list" and not (n.startswith("sources.list.d/") and SRC_NAME.match(n[len("sources.list.d/"):])):
                invalid("sources.index has a line that is not a file of apt's sources: %s" % cell(n, 60))
                return None
            data = read_bytes("src-main" if n == "sources.list" else "src-d-" + n[len("sources.list.d/"):])
            if data is None:
                invalid("the copy of %s is missing" % n)
                return None
            files[n] = data
        return dict(sorted(files.items(), key=lambda kv: (kv[0] != "sources.list", kv[0])))


    def apt_fragment_name(name):
        """a name apt reads in apt.conf.d (the core's aptFragmentName, which TestRealAptReadsTheFragmentsOfTheNamesWeSay asks apt itself about): letters, digits, `_`, `-` and `:` (apt 2.2.4 lets
        a colon in), not starting with a dot, a dot only before the extension `conf`"""
        if not name or name[0] == "." or not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
            return False
        return name.rsplit(".", 1)[1] == "conf" if "." in name else True


    # What the sandbox keeps of the box's configuration of apt (the core's decision, aptconf.go): apt reads its configuration as a LANGUAGE (a comment spliced into a word, `#include`,
    # `#clear`, a setting that moves the directories of apt itself), so no filter of its text is believed. A file is kept only when EVERY line of it is blank, a `//` comment, or ONE
    # statement `NAME "VALUE";` with NAME on the allow-list below (apt reads a name in any case), and VALUE printable ASCII without quotes or backslashes, 512 characters at the most. What is
    # kept is written again, never copied. Anything else leaves the WHOLE file out, and the plan names it with the reason of the FIRST line that is not kept: not_simple, has_directive (a `#`
    # line), not_allowed_setting (a name that is not on the list: an open `Acquire::` would let `Acquire::gpgv::Options` through, which makes the gpgv that apt runs as `_apt` write
    # a file wherever it was told), proxy_auto_detect (a name that runs a program); and too_large (more than 64 KiB, or the 201st fragment of apt.conf.d), not_regular (a link, a device, a fifo)
    # for the file as a whole. A directory of apt.conf.d is passed over without a word, as apt does it (it counts among the 200).
    CONF_STATEMENT = re.compile(r'^[ \t]*([^ \t"\\;{}]+)[ \t]+"([^"\\]*)"[ \t]*;[ \t]*$')

    # THE ONE TABLE of the settings that are kept: a copy of the core's `sandboxAptSettings` (pkg/osplan/aptconf.go), which is changed first, and then this one (the harness holds this table
    # against the core's own, entry by entry, and this grammar against the core's on the files of fixtures/plan/conf-inputs.json that the core said its mind of in conf-cases.json).
    # per_method: the download methods that have settings kept, each with what ITS program reads (Acquire::<method>::<setting>): http and https are one program and read the seven; ftp reads
    # Proxy and Timeout and no other (the strings of its program in apt 2.2.4 and 2.6.1), so the five others are left out for it; per_host: the ones of them that apt also reads for ONE host
    # (Acquire::<method>::<setting>::<host>, the proxy of a host: a host has letters, digits, `-`, `_` and `.` in it, never two dots in a row, and is not only dots); top: Acquire::<setting>
    # itself; other: whole names outside Acquire
    HTTP_SETTINGS = ("Proxy", "Timeout", "Pipeline-Depth", "Dl-Limit", "Max-Age", "No-Cache", "AllowRedirect")
    SANDBOX_APT_SETTINGS = dict(
        per_method={"http": HTTP_SETTINGS, "https": HTTP_SETTINGS, "ftp": ("Proxy", "Timeout")},
        per_host=("Proxy",),
        top=("Retries", "Languages", "PDiffs", "By-Hash", "Check-Valid-Until", "ForceIPv4", "ForceIPv6", "Queue-Mode"),
        other=("APT::Install-Recommends", "APT::Install-Suggests"),
    )


    def kept_apt_name(name):
        """the core's keptAptName: whether a setting is one of SANDBOX_APT_SETTINGS. A name is ASCII letters, digits, `-`, `_`, `.` and `:` and nothing else (so that the folding of the case is
        ASCII's: the Kelvin sign is not a `k`, the long s is not an `s`); it is one of the whole names, or `Acquire::` and then one, two or three segments between `::`: a top setting; a method
        and one of its settings; a method, a setting that has a host, and the host (no `:` in it, no `..`, not only dots)"""
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
            return False
        t, low = SANDBOX_APT_SETTINGS, name.lower()
        if low in {n.lower() for n in t["other"]}:
            return True
        if not low.startswith("acquire::"):
            return False
        segs = low[len("acquire::"):].split("::")
        if len(segs) == 1:
            return segs[0] in {n.lower() for n in t["top"]}
        settings = {m.lower(): {s.lower() for s in ss} for m, ss in t["per_method"].items()}.get(segs[0])
        if settings is None:
            return False
        if len(segs) == 2:
            return segs[1] in settings
        if len(segs) == 3:
            return segs[1] in settings and segs[1] in {n.lower() for n in t["per_host"]} and ":" not in segs[2] and ".." not in segs[2] and segs[2].strip(".") != ""
        return False


    def conf_reason(data):
        """why the grammar leaves a file of apt's configuration out of the sandbox, the reason of the first line it does not keep ('' when it keeps the file: nothing in it but blank
        lines, `//` comments and statements of the kinds that are kept)"""
        if len(data) > CONF_MAX_BYTES:
            return "too_large"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "not_simple"
        for line in text.split("\n"):
            if line.endswith("\r"):
                line = line[:-1]
            if re.search(r"[\x00-\x08\x0a-\x1f\x7f]", line):
                return "not_simple"
            t = line.strip(" \t")
            if not t or t.startswith("//"):
                continue
            if t.startswith("#"):
                return "has_directive"
            m = CONF_STATEMENT.match(line)
            if not m:
                return "not_simple"
            name, value = m.groups()
            if len(value) > 512 or re.search(r"[^\x20-\x7e]", value):
                return "not_simple"
            low = name.replace(chr(0x130), "i").lower()    # (Go's ToLower reads U+0130, a capital I with a dot, as an `i` and U+212A as a `k`, which lower() does too: it is `acquire::` to the core)
            if low.startswith("acquire::") and ("proxy-auto-detect" in low or "proxyautodetect" in low):
                return "proxy_auto_detect"      # (named as such before the list is asked: it is not on it either)
            if not kept_apt_name(name):
                return "not_allowed_setting"
        return ""


    def expected_left_out():
        """{file: the reason the grammar gives for leaving it out} for the files of apt's configuration of the box that the sandbox does not keep (the fragments of apt.conf.d that apt
        reads, at most 200 of them, links and directories among them, and apt.conf), from the copies the guest made. None (INVALID) when the guest did not copy them. A directory of
        apt.conf.d is one of the 200 and is passed over without a word (apt does that), unless it is the 201st"""
        index = read("aptconf.index", None)
        if index is None:
            invalid("aptconf.index is missing: the guest did not copy apt's configuration, and the files the plan says it left out cannot be checked")
            return None
        large = {ln.strip() for ln in read("aptconf.large").splitlines() if ln.strip()}
        nonregular = {ln.strip() for ln in read("aptconf.nonregular").splitlines() if ln.strip()}
        dirs = {ln.strip() for ln in read("aptconf.dirs").splitlines() if ln.strip()}
        out, seen = {}, 0
        for name in [ln.strip() for ln in index.splitlines() if ln.strip()]:
            if not apt_fragment_name(name):
                continue
            seen += 1
            if seen > CONF_MAX_FRAGMENTS:
                out[name] = "too_large"
            elif name in dirs:
                continue
            elif name in nonregular:
                out[name] = "not_regular"
            elif name in large:
                out[name] = "too_large"
            else:
                data = read_bytes("aptconf-d-" + name)
                if data is None:
                    invalid("the copy of apt.conf.d/%s is missing" % name)
                    return None
                reason = conf_reason(data)
                if reason:
                    out[name] = reason
        main = read_bytes("aptconf-main")
        if "apt.conf" in nonregular:
            out["apt.conf"] = "not_regular"
        elif main is not None and conf_reason(main):
            out["apt.conf"] = conf_reason(main)
        return out


    def capped_left_out(left):
        """([{file, reason}], cut) of what the core names of the files it left out ({file: reason}): the list in the core's order (by file, then reason), at most MAX_LEFT_OUT of it, and whether
        there were more (the core's capLeftOut: a plan, and a failure, say the first 200 and that they are cut)"""
        rows = [{"file": f, "reason": r} for f, r in sorted(left.items())]
        return rows[:MAX_LEFT_OUT], len(rows) > MAX_LEFT_OUT


    BOX = box_sources()
    EXPECT = None
    if BOX is not None:
        try:
            EXPECT = expected_sources(BOX)
        except NotComputable as e:
            invalid("the box's sources are not of a shape this leg computes (%s): the plan cannot be checked against them" % e)
    LEFT_OUT = expected_left_out()


    # ---- a plan run: the POST, the watching, the final GET -------------------------------------------------------------------------------

    def polls_of(run):
        """[(t0, t1, http, curl, state, phase)] of the watching of a run, or None (INVALID) when the guest left none"""
        text = read("plan-%s.polls.tsv" % run, None)
        rows = []
        for ln in (text or "").splitlines():
            f = ln.split("\t")
            try:
                row = (float(f[0]), float(f[1]), int(f[2]), int(f[3]), f[4], f[5])
                if not (math.isfinite(row[0]) and math.isfinite(row[1]) and re.fullmatch(r"[0-9]{1,6}", f[2]) and re.fullmatch(r"[0-9]{1,6}", f[3])):
                    raise ValueError("a time that is not a time, or a status of more than six digits")
            except (IndexError, ValueError):
                invalid("plan-%s.polls.tsv has a row that is not one of the watching: %s" % (run, cell(ln, 80)))
                return None
            rows.append(row)
        if not rows:
            invalid("plan-%s.polls.tsv is missing or empty: the job was not watched" % run)
            return None
        return rows


    class Run:
        """one plan: the POST that began it, the GET that said it was done (or failed), how it was watched. A run whose POST was refused (the second, third and fourth: the
        first is judged by V_POST, and the guest stops when it is refused) began nothing: the plan the core answers is the one it made before, and is not this run's"""

        def __init__(self, name):
            self.name = name
            self.post = get("plan-%s-post" % name)
            self.refused = ""
            if name != "p1" and self.post is not None and (self.post["kind"] or self.post["http"] != 202):
                d = dd(self.post)
                self.refused = "%s (%s)" % ("HTTP %d" % self.post["http"] if not self.post["kind"] else self.post["kind"], cell(d.get("error_code") or d.get("error") or "no reason", 60))
                invalid("the POST of the plan %s did not begin a job: %s. What the core answers after it is a plan of an earlier run, and is not judged as this one's" % (name, self.refused))
            self.final = get("plan-%s-get" % name) if not self.refused else None
            self.polls = polls_of(name) if not self.refused else None
            self.timeout = read("plan-%s.timeout" % name, None) is not None     # the guest says so when the job did not end in its 15 minutes
            d = dd(self.final)
            self.state = d.get("state")
            self.usable = self.final is not None and not self.final["kind"] and self.final["http"] == 200
            # a job that is done has a plan: one that has none is a plan with nothing in it, which the shape of the first says, and the ids of the others fail on
            self.plan = (d.get("plan") if isinstance(d.get("plan"), dict) else {}) if self.usable and self.state == "done" else None
            self.id = self.plan.get("plan_id") if self.plan else None

        @property
        def done(self):
            return self.plan is not None


    R = {r: Run(r) for r in RUNS}
    P1 = R["p1"]


    def stale(run):
        """why the plan that a run ended with is not the job that its POST began ('' when it is): made before the POST, it is the one the core made earlier"""
        if run.post is None and run.done and run.plan:
            return "its POST left no usable record of itself, so the plan cannot be told from the plan of an earlier run"
        if run.post is None or run.post["kind"] or not run.done or not run.plan:
            return ""
        started, made = timestamp(dd(run.final).get("started_at")), timestamp(run.plan.get("made_at"))
        if started is None or made is None:
            return "started_at or made_at is not a time, so it cannot be told from the plan of an earlier run"
        if not (run.post["t0"] - CLOCK_SLACK <= started <= run.post["t1"] + CLOCK_SLACK):
            return "started_at is %+.0f s from its POST" % (started - run.post["t0"])
        if made < run.post["t0"] - CLOCK_SLACK:
            return "made_at is %.0f s before its POST" % (run.post["t0"] - made)
        return ""


    for _r in RUNS:
        # the guest watched the job end, and what it kept as the end has to be that: an answer that is cut, a status that is not 200, or another state is no evidence of the end
        _run = R[_r]
        if _run.refused or _run.final is None or _run.polls is None:
            continue
        _last = _run.polls[-1][4]
        if _last in ("done", "failed"):
            _why = ("not usable (%s)" % _run.final["kind"]) if _run.final["kind"] else "HTTP %d, not 200" % _run.final["http"] if _run.final["http"] != 200 else "of the state %s" % cell(_run.state, 12) if _run.state != _last else ""
            if _why:
                invalid("the last poll of the plan %s said %s, and the answer kept as its end is %s: the end of the run cannot be judged" % (_r, _last, _why))
        elif _r != "p1":
            invalid("the plan %s was still %s when the guest stopped watching it: it did not end within 15 minutes" % (_r, cell(_last, 10)))
        if _r != "p1" and _run.done and stale(_run):
            invalid("the plan %s that the core answers at the end is not the job its POST began: %s" % (_r, stale(_run)))
            _run.plan, _run.id = None, None     # it is an earlier run's plan: nothing is judged of it as this one's
    idle, rb_idle = get("plan-idle"), get("runbook-idle")
    post2 = get("plan-p1-post-2")
    rb1 = get("runbook-p1")
    rb3 = get("runbook-p3")
    ctl = [get("ctl-core-1", plain=True), get("ctl-core-2", plain=True)]
    for c in ctl:
        # the core answers its bare version as text (outside the envelope): what is asked of the control is that it answered
        if c and (c["kind"] or c["http"] != 200):
            invalid("%s answered HTTP %s: the control of the window did not run" % (c["name"], c["http"]))
        elif c and not c["body"].strip():
            invalid("%s answered HTTP 200 with no text: the control of the window did not run" % c["name"])

    for s in STEPS:
        if not os.path.exists(os.path.join(out_dir, "step-%s.skipped" % s.id)):
            get("step-%s-post" % s.id)
            c = CALLS.get("step-%s-post" % s.id)
            if s.kind in ("job", "canary") and c and not c["kind"] and c["http"] == 202:
                get("step-%s-get" % s.id)
                if s.kind == "job":
                    get("step-%s-runbook" % s.id)

    # every answer of the plan and runbook routes, the ones that ran (the polls are not here: they are the guest's own tsv)
    ANSWERS = {n: c for n, c in CALLS.items() if c and not n.startswith("ctl-")}
    # a status that says `unknown` (the core's question to the machine went wrong or took too long: no reason, and the page asks again) is no answer to "can a plan be made": it says nothing against the feature
    UNKNOWN = sorted(n for n, c in ANSWERS.items() if not c["kind"] and c["http"] == 200 and dd(c).get("unknown") is True)
    for _n in UNKNOWN:
        invalid("%s says unknown true: the core could not tell whether a plan can be made for this box (a question to the machine went wrong or took too long). That is no answer, and it says nothing against the feature: run the leg again" % _n)


    def needles():
        """[(kind, value)] the guest says must not be in an answer: its own name, addresses and user (none when it did not say them: the run is invalid)"""
        out = []
        for line in read("leak-needles.tsv").splitlines():
            kind, _, value = line.partition("\t")
            if kind and value.strip():
                out.append((kind, value.strip()))
        if not any(k == "hostname" for k, _ in out) or not any(k == "ipv4" for k, _ in out) or not any(k == "user" for k, _ in out):
            invalid("leak-needles.tsv has no hostname, no IPv4 address or no user name: what the answers must not say is not known")
            return []
        return out


    NEEDLES = needles()


    # ---- the contract: routes, verdicts, shapes -----------------------------------------------------------------------------------------

    V_IDLE = "before any plan: GET answers 200 with plannable true and state idle (the box is one a plan can be made for)"
    V_RB_IDLE = "with no plan, the runbook answers 409 no_plan with the reason idle"
    V_POST = "POST starts the job: 202, state running, phase sources"
    V_POST_TWICE = "a second POST while the job runs answers 200 with already_running true and the running job's state"
    V_POLL = "the status answered every poll while the job ran, at once (200, 5 s at most)"
    V_PHASES = "the job went through its phases in order (sources, update, upgrade, dist-upgrade, estimate, parse) and ended done within 15 minutes"
    V_SANDBOX_SEEN = "while the job ran there was one sandbox directory, casaos-os-plan.<id> in /var/tmp (a second POST made no second one)"
    V_SHAPE = "the plan has the contract's shape, no tree of the sources, and counts that agree with its lists"
    V_TIMES = "made_at is the time of the job, the plan expires 24 hours later, and the times of the job are in order"
    V_VERDICT = "the plan's verdict is ok or needs_acceptance (not blocked)"
    V_VERDICT_RULE = "the verdict follows the problems, and every problem is one of the contract's, with its severity and its data"
    V_REMOVALS = "no removal is protected or essential, and a protected one is named with its pattern"
    V_COUNTS_DIST = "the counts of the full upgrade are within 15 % (rounded up to a whole package) of the measured 384 upgraded, 75 new, 2 removed, 0 kept back"
    V_COUNTS_UP = "the counts of the minimal upgrade are within 15 % (rounded up to a whole package) of the measured 274 upgraded, 0 new, 0 removed, 111 kept back"
    V_ESTIMATES = "apt's sizes are known and plausible: a download of 100 MB to 2 GB, and some room taken"
    V_DISK = "the disk has its three rows (apt's archives, /usr and /), each with what it needs and what it has, and all of them are met"
    V_SANDBOX = "the sandbox took less than the 640 MiB that is asked to be free, and each of the six phases has its time"
    V_SOURCES = "the sources of the plan are what the leg computes itself from the box's real sources: the lines changed, the files put aside, the dropped lines and both hashes"
    V_DOCKER = "the plan sees Docker: present, its source paused, nothing of it on hold"
    V_HOOKS = "the plan names the files of apt's configuration it left out, each with its reason, and they are the ones the sandbox's grammar leaves out (a `//` comment, or one statement a line of the allow-list of the proxy, the time to wait, the tries, the languages and the two Install settings, is all it keeps)"
    V_ID_STABLE = "two plans made back to back have the same plan_id"
    V_ID_HOLD = "a hold on base-files changes the plan_id and is in holds"
    V_ID_BACK = "after the dirty matrix and its undos a plan has the same plan_id as the first: the box is as it was"
    V_NOSTORE = "every answer of the plan and the runbook routes says Cache-Control: no-store"
    V_RB_SHAPE = "the runbook is the plan's: its plan_id and hop, a text of 256 KiB at most, and the nine blocks in order with one point of no return and one restart"
    V_RB_SHN = "every block of the runbook is read by sh -n on the guest"
    V_RB_TREE = "the runbook writes the plan's tree byte for byte, puts aside the plan's paused files, and checks the plan's hashes of the sources and of what it wrote"
    V_RB_BLOCKED = "a plan that is blocked (a hold on base-files) says dist_kept_back and has no runbook: 409 no_plan, reason blocked"
    V_STEP_LEFTOVER = "no sandbox directory is left in /var/tmp after the plan of any step, and no plan says it left one behind"
    V_STEP_NORUNBOOK = "a job that failed has no runbook: 409 no_plan with the reason failed"
    V_INV_FILES = "invariance: the plan created, removed or changed no file of /etc, /var/lib/dpkg, /var/lib/apt, /var/cache/apt or /var/lib/casaos (only /var/log/casaos may differ)"
    V_INV_UNITS = "invariance: the plan started, stopped or restarted no service"
    V_INV_PORTS = "invariance: the listening ports are the same after the plan"
    V_INV_TMP = "invariance: /var/tmp lists the same after the plan: no sandbox directory is left"
    V_INV_WITNESS = "invariance: the witness containers ran through the plan untouched (the same start times)"
    V_STAMP = "the hooks of the box did not run in the sandbox's update: apt's update-success-stamp is untouched"
    V_LEAK = "no answer of the plan and runbook routes says the box's own hostname, address, MAC or user name"
    F_JOB = "step %s: the plan fails in the %s phase with `%s`%s"
    F_REFUSED = "step %s: the POST is refused with a 409 and `%s`%s"
    F_CANARY = "step %s: the plan leaves %s out of apt's configuration (%s), and what is in it does not run: %s is not made"


    def plan_problems(plan, status):
        """what is wrong with the shape of a plan that GET says is done: a list of sentences. Keys the core added are not wrong (they are said in the facts)"""
        bad = []

        def need(cond, what):
            if not cond:
                bad.append(what)
        need(status.get("plannable") is True or status.get("unknown") is True, "plannable is not true%s" % (", reason %s" % cell(status.get("reason"), 30) if status.get("reason") else ""))
        need("unknown" not in status or isinstance(status["unknown"], bool), "unknown is not true or false")
        need(status.get("unknown") is not True or (status.get("plannable") is not True and not status.get("reason")), "unknown is true with plannable true or with a reason: the core says that it could not tell, and tells")
        gone = status.get("sandbox_left_behind")
        need("sandbox_left_behind" not in status or (isinstance(gone, str) and 0 < len(gone) <= MAX_SHOWN and not re.search(r"[\x00-\x1f\x7f]", gone)), "sandbox_left_behind is not a short text")
        for k in ("started_at", "finished_at", "expires_at"):
            need(timestamp(status.get(k)) is not None, "%s is not an RFC 3339 time" % k)
        need(isinstance(plan.get("plan_id"), str) and re.match(r"^p1-[0-9a-f]{32}$", plan.get("plan_id")), "plan_id is not p1- and 32 hex")
        need(plan.get("hop") == L["hop"], "hop is %s, not %s" % (cell(plan.get("hop"), 30), L["hop"]))
        need(timestamp(plan.get("made_at")) is not None and timestamp(plan.get("expires_at")) is not None, "made_at or expires_at is not an RFC 3339 time")
        need(plan.get("verdict") in VERDICTS, "verdict is not one of ok, needs_acceptance, blocked")
        pr = plan.get("problems")
        need(isinstance(pr, list) and all(isinstance(p, dict) and isinstance(p.get("code"), str) and isinstance(p.get("severity"), str) and isinstance(p.get("data"), dict) for p in pr),
             "problems is not a list of {code, severity, data}")
        counts = plan.get("counts")
        for k in ("dist_upgrade", "upgrade"):
            c = inner(counts, k)
            need(isinstance(c, dict) and sorted(c) == ["kept_back", "new", "removed", "upgraded"] and all(is_int(v) and v >= 0 for v in c.values()), "counts.%s is not four whole numbers" % k)
        rem = plan.get("removals")
        need(isinstance(rem, list) and all(isinstance(r, dict) and isinstance(r.get("name"), str) and isinstance(r.get("version"), str) and all(isinstance(r.get(f), bool) for f in ("manual", "protected", "essential"))
                                           and ("pattern" not in r or isinstance(r["pattern"], str)) for r in rem), "removals is not a list of {name, version, manual, protected, essential}")
        for k in ("new_packages", "kept_back", "autoremovable", "holds"):
            need(strings(plan.get(k)), "%s is not a list of short strings (at most %d)" % (k, MAX_LISTED))
        for k in ("new_truncated", "kept_back_truncated", "autoremovable_truncated"):
            need(k not in plan or isinstance(plan[k], bool), "%s is not true or false" % k)
        need("disk_note" not in plan or isinstance(plan["disk_note"], str), "disk_note is not a text")
        cl = plan.get("apt_conf_left_out")
        cl_ok = isinstance(cl, list) and len(cl) <= MAX_LEFT_OUT and all(isinstance(e, dict) and sorted(e) == ["file", "reason"] and strings([e["file"]]) and e["reason"] in CONF_REASONS for e in cl)
        need(cl_ok, "apt_conf_left_out is not a list of at most %d {file, reason}, with a reason of the contract's six" % MAX_LEFT_OUT)
        need(not cl_ok or [(e["file"], e["reason"]) for e in cl] == sorted((e["file"], e["reason"]) for e in cl), "apt_conf_left_out is not in the core's order (by file, then reason)")
        need("apt_conf_left_out_truncated" not in plan or (plan["apt_conf_left_out_truncated"] is True and cl_ok and len(cl) == MAX_LEFT_OUT),
             "apt_conf_left_out_truncated is there, and is not true on a list of %d: it is said only when the list is cut" % MAX_LEFT_OUT)
        src = plan.get("sources")
        need(isinstance(src, dict) and isinstance(src.get("files"), list) and all(isinstance(f, dict) and isinstance(f.get("file"), str) and f.get("action") in ("rewritten", "paused", "unchanged")
                                                                                  and isinstance(f.get("lines"), list) for f in src["files"]), "sources.files is not a list of {file, action, lines}")
        need(isinstance(src, dict) and all(isinstance(src.get(k), str) and re.match(r"^[0-9a-f]{64}$", src.get(k)) for k in ("hash_current", "hash_generated")), "the hashes of the sources are not SHA-256")
        need(isinstance(src, dict) and is_int(src.get("dropped_backports")) and is_int(src.get("firmware_added")), "dropped_backports or firmware_added is not a whole number")
        need(isinstance(src, dict) and "tree" not in src, "the plan has the tree of the sources: it is the runbook's alone (the box's own lines, as the files have them)")
        pa = plan.get("paused")
        need(isinstance(pa, list) and all(isinstance(p, dict) and isinstance(p.get("file"), str) and strings(p.get("hosts")) and isinstance(p.get("docker"), bool) and ("line" not in p or is_int(p["line"])) for p in pa),
             "paused is not a list of {file, hosts, docker}")
        dk = plan.get("docker")
        need(isinstance(dk, dict) and isinstance(dk.get("present"), bool) and isinstance(dk.get("source_paused"), bool) and strings(dk.get("packages_held")), "docker is not {present, source_paused, packages_held}")
        es = plan.get("estimates")
        need(isinstance(es, dict) and isinstance(es.get("known"), bool) and (not es.get("known") or (is_int(es.get("download_bytes")) and is_int(es.get("extra_bytes")))),
             "estimates is not {known, download_bytes, extra_bytes}")
        dk_ = plan.get("disk")
        need(isinstance(dk_, list) and all(isinstance(r, dict) and isinstance(r.get("path"), str) and is_int(r.get("need")) and isinstance(r.get("ok"), bool)
                                           and ("unknown" not in r or isinstance(r["unknown"], bool)) and (is_int(r.get("free")) or (r.get("unknown") is True and "free" not in r)) for r in dk_),
             "disk is not a list of {path, need, free, ok}, with unknown true instead of free for a room that could not be read")
        sb = plan.get("sandbox")
        need(isinstance(sb, dict) and is_int(sb.get("bytes")) and isinstance(sb.get("seconds_by_phase"), dict) and all(is_num(v) and v >= 0 for v in sb["seconds_by_phase"].values()),
             "sandbox is not {bytes, seconds_by_phase}")
        return bad


    def unlisted_of(plan):
        """how many packages the plan says that apt keeps back and does not name (data.unlisted of its dist_kept_back problem: a package on hold whose candidate is a downgrade): 0 when it
        says none, or says it in a form that is not a whole number (the rule of the problems says that)"""
        for p in plan["problems"]:
            if p["code"] == "dist_kept_back" and is_int(p["data"].get("unlisted")) and p["data"]["unlisted"] > 0:
                return p["data"]["unlisted"]
        return 0


    def problem_data_bad(plan):
        """what is wrong with the data of the problems of a plan, code by code (the contract: pkg/osplan/plan.go; disk_short has a rule of its own): a list of sentences"""
        bad = []
        for p in plan["problems"]:
            code, x = p["code"], p["data"]
            if code in ("removal_protected", "removal_essential", "removal_manual"):
                keys = {"removal_protected": ("name", "pattern"), "removal_essential": ("name",), "removal_manual": ("name", "version")}[code]
                if not all(strings([x.get(k)]) for k in keys):
                    bad.append("%s has not %s as short texts: %s" % (code, " and ".join(keys), cell(json.dumps(x, sort_keys=True), 100)))
            elif code in ("dist_kept_back", "downgrade", "docker_held"):
                if not strings(x.get("names")) or (code != "dist_kept_back" and not x["names"]):
                    bad.append("%s has not names as a list of short texts%s: %s" % (code, "" if code == "dist_kept_back" else " (and one that has a name in it)", cell(json.dumps(x, sort_keys=True), 100)))
                    continue
                if code == "dist_kept_back":
                    if "unlisted" in x and not (is_int(x["unlisted"]) and x["unlisted"] >= 1):
                        bad.append("dist_kept_back says unlisted %s, which is not a whole number of one or more" % cell(x["unlisted"], 20))
                    # the names are the packages that the full upgrade keeps back, and the ones that only the minimal upgrade kept back and the full one names in no list and counts nowhere (the
                    # core's leftBehind: its new version cannot be installed, and apt says nothing): all of them, sorted, once each, cut at 600 together. So the plan's own list is inside them,
                    # unless the cut took some, and the others are named by no other list of the plan
                    names, kept = x["names"], plan["kept_back"]
                    if names != sorted(set(names)):
                        bad.append("dist_kept_back names %s, which is not sorted and without repeats" % few(names, 4))
                    elif len(names) < MAX_LISTED and not set(kept) <= set(names):
                        bad.append("dist_kept_back names %s and the plan says that %s is kept back" % (few(names, 4) or "nothing", few(kept, 4) or "nothing"))
                    else:
                        named = {r["name"] for r in plan["removals"]} | set(plan["new_packages"]) | {n for q in plan["problems"] if q["code"] == "downgrade" and strings(q["data"].get("names")) for n in q["data"]["names"]}
                        clash = [n for n in names if n not in kept and n in named]
                        if clash:
                            bad.append("dist_kept_back names %s, which the plan says it removes, installs or downgrades: a package that the full upgrade names is not left behind by it" % few(clash))
        return bad


    def plan_consistency(plan):
        """what a plan of the shape above says twice and does not agree with itself: the counts of the full upgrade and the lists that go with them (a list that was cut at 600 names
        says so, and is not longer than its count)"""
        c, bad, unlisted = plan["counts"]["dist_upgrade"], [], unlisted_of(plan)
        for key, lst, cut in (("removed", "removals", False), ("new", "new_packages", plan.get("new_truncated")), ("kept_back", "kept_back", plan.get("kept_back_truncated"))):
            n = len(plan[lst])
            # apt counts a package that is on hold and whose candidate is a downgrade as not upgraded and does not name it: the list is shorter than the count by the ones the core says it did not name
            named = c[key] - (unlisted if key == "kept_back" else 0)
            if (n > named) if cut else (n != named):
                bad.append("counts.dist_upgrade.%s is %d and %s has %d%s" % (key, c[key], lst, n, " (and %d that apt does not name)" % unlisted if key == "kept_back" and unlisted else ""))
        # sizes that apt did not say leave no byte floor to check, and the plan says so in a word of its own, not only in an empty disk
        if plan["estimates"]["known"] is False and plan.get("disk_note") != "estimates_unknown":
            bad.append("estimates.known is false and disk_note is %s, not estimates_unknown" % cell(plan.get("disk_note"), 30))
        if plan["estimates"]["known"] is True and plan.get("disk_note"):
            bad.append("estimates.known is true and disk_note says %s" % cell(plan.get("disk_note"), 30))
        return bad


    KNOWN_KEYS = {"plan_id", "hop", "made_at", "expires_at", "verdict", "problems", "counts", "removals", "new_packages", "new_truncated", "kept_back", "kept_back_truncated", "autoremovable",
                  "autoremovable_truncated", "sources", "paused", "holds", "docker", "estimates", "disk", "disk_note", "sandbox", "apt_conf_left_out", "apt_conf_left_out_truncated"}


    # ---- before any plan, the POST, the second POST ----------------------------------------------------------------------------------------

    if idle is not None and "plan-idle" not in UNKNOWN:
        d = dd(idle)
        bad = ([] if not idle["kind"] else ["no usable answer (%s)" % idle["kind"]]) + (envelope(idle, 200) if not idle["kind"] else [])
        if not idle["kind"] and (idle["http"] != 200 or d.get("plannable") is not True or d.get("state") != "idle" or "plan" in d or "error" in d):
            bad.append("HTTP %d, plannable %s, state %s%s" % (idle["http"], cell(d.get("plannable"), 10), cell(d.get("state"), 10), ", reason " + cell(d.get("reason"), 30) if d.get("reason") else ""))
        verdict(V_IDLE, not bad, few(bad) if bad else "plannable, idle")
    if rb_idle is not None:
        bad, data = refusal_of(rb_idle, 409, "no_plan")
        if data.get("reason") != "idle":
            bad.append("reason is %s, not idle" % cell(data.get("reason"), 20))
        verdict(V_RB_IDLE, not bad, few(bad) if bad else "409 no_plan, idle")

    p1_post = P1.post
    if p1_post is not None:
        d = dd(p1_post)
        if not p1_post["kind"] and p1_post["http"] == 409 and d.get("error_code") == "busy":
            invalid("the first plan was refused as `busy` on the clean box (%s): a package manager was at work on the box, and the guest did not wait for it. This says nothing against the feature"
                    % cell(d.get("error"), 120))
            P1_BLOCKED = True
        else:
            P1_BLOCKED = False
            bad = [] if not p1_post["kind"] else ["no usable answer (%s)" % p1_post["kind"]]
            if not p1_post["kind"]:
                bad += envelope(p1_post, 202)
                if p1_post["http"] != 202:
                    bad.append("HTTP %d, not 202" % p1_post["http"])
                if d.get("state") != "running" or d.get("phase") != "sources" or d.get("plannable") is not True or timestamp(d.get("started_at")) is None:
                    bad.append("state %s, phase %s, plannable %s, started_at %s" % (cell(d.get("state"), 10), cell(d.get("phase"), 12), cell(d.get("plannable"), 10), cell(d.get("started_at"), 30)))
            verdict(V_POST, not bad, few(bad) if bad else "202, running, sources")
    else:
        P1_BLOCKED = True
    if post2 is not None and not P1_BLOCKED:
        d = dd(post2)
        bad = [] if not post2["kind"] else ["no usable answer (%s)" % post2["kind"]]
        if not post2["kind"]:
            bad += envelope(post2, 200)
            if post2["http"] != 200 or d.get("already_running") is not True or d.get("state") != "running" or d.get("plannable") is not True or d.get("phase") not in PHASES:
                bad.append("HTTP %d, already_running %s, state %s, phase %s" % (post2["http"], cell(d.get("already_running"), 10), cell(d.get("state"), 10), cell(d.get("phase"), 12)))
            first = dd(p1_post).get("started_at") if p1_post is not None and not p1_post["kind"] else None
            if timestamp(d.get("started_at")) is None or d.get("started_at") != first:
                bad.append("started_at is %s, and the job the first POST began has %s: it is not the same job" % (cell(d.get("started_at"), 30), cell(first, 30)))
        verdict(V_POST_TWICE, not bad, few(bad) if bad else "200, already_running, %s" % d.get("phase"))


    # ---- the watching of the first job ----------------------------------------------------------------------------------------------------

    def plan_error(run):
        e = dd(run.final).get("error") if run.final and not run.final["kind"] else None
        return e if isinstance(e, dict) else {}


    if P1.polls is not None and P1.final is not None and not P1_BLOCKED:
        rows = P1.polls
        bad = []
        slow = [r for r in rows if r[2] != 200 or r[3] != 0 or r[1] - r[0] > POLL_ALLOW]
        if slow:
            bad.append("%d of %d polls: HTTP %s, curl %s, %.1f s" % (len(slow), len(rows), slow[0][2], slow[0][3], slow[0][1] - slow[0][0]))
        verdict(V_POLL, not bad, few(bad) if bad else "%d polls, the slowest %.1f s" % (len(rows), max(r[1] - r[0] for r in rows)))
        seen = [r[5] for r in rows if r[4] == "running"]
        order = [PHASES.index(p) if p in PHASES else -1 for p in seen]
        bad = []
        if -1 in order:
            bad.append("a phase that is not one of the six: %s" % few(sorted({p for p in seen if p not in PHASES})))
        elif order != sorted(order):
            bad.append("the phases are not in order: %s" % " ".join(seen)[:120])
        if not seen:
            bad.append("no poll saw the job running")
        if P1.timeout:
            bad.append("the job did not end within 15 minutes")
        elif rows[-1][4] != "done":
            e = plan_error(P1)
            bad.append("the job ended %s%s" % (cell(rows[-1][4], 10), " with %s in %s: %s" % (cell(e.get("code"), 30), cell(e.get("phase"), 12), cell(e.get("detail"), 160)) if e else ""))
        elif any(r[4] == "done" for r in rows[:-1]):
            bad.append("a poll said done and the watching went on")
        verdict(V_PHASES, not bad, few(bad) if bad else "%s, then done" % " ".join(dict.fromkeys(seen)))
        e = plan_error(P1)
        if rows[-1][4] == "failed" and e.get("code") == "sandbox_update_failed" and inner(e, "data", "kind") == "network":
            invalid("the first plan failed in its update for a reason of the network (%s): the mirrors of the target were not reached, and this says nothing against the feature" % cell(e.get("detail"), 120))
        if rows[-1][4] == "failed" and e.get("code") == "busy":
            invalid("the first plan failed as `busy` in its %s phase: a package manager was at work on the box. This says nothing against the feature" % cell(e.get("phase"), 12))

    seen_dirs = read("plan-p1.sandbox-seen", None)
    if seen_dirs is None:
        invalid("plan-p1.sandbox-seen is missing: the guest did not look for the sandbox while the first job ran")
    elif not P1_BLOCKED and P1.polls is not None:
        dirs = sorted({ln.strip() for ln in seen_dirs.splitlines() if ln.strip()})
        bad = []
        if not dirs:
            bad.append("no sandbox directory was seen in /var/tmp while the job ran")
        elif len(dirs) > 1:
            bad.append("%d sandbox directories were seen: %s" % (len(dirs), few(dirs)))
        elif not re.match(r"^/var/tmp/casaos-os-plan\.[A-Za-z0-9]{1,32}$", dirs[0]):
            bad.append("the directory seen is %s" % cell(dirs[0], 80))
        verdict(V_SANDBOX_SEEN, not bad, few(bad) if bad else dirs[0])

    PLAN = P1.plan if P1.done and not P1_BLOCKED else None
    STATUS = dd(P1.final) if PLAN is not None else {}
    P1_DONE = PLAN is not None


    # ---- the shape and the contents of the first plan -------------------------------------------------------------------------------------

    def ordinal(n):
        return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


    def tolerance(want):
        return int(math.ceil(TOLERANCE * want))


    SHAPE_OK = False
    if P1_DONE:
        shape_bad = plan_problems(PLAN, STATUS)
        bad = shape_bad + (envelope(P1.final, 200) if P1.final else []) + ([] if shape_bad else plan_consistency(PLAN))
        verdict(V_SHAPE, not bad, few(bad, 4) if bad else "%d keys" % len(PLAN))
        SHAPE_OK = not bad
        made, expires = timestamp(PLAN.get("made_at")), timestamp(PLAN.get("expires_at"))
        started, finished = timestamp(STATUS.get("started_at")), timestamp(STATUS.get("finished_at"))
        bad = []
        if made is None or expires is None or started is None or finished is None:
            bad.append("a time that is not a time")
        else:
            if expires - made != LIFETIME:
                bad.append("expires_at is %d s after made_at, not %d" % (expires - made, LIFETIME))
            if STATUS.get("expires_at") != PLAN.get("expires_at"):
                bad.append("the status says another expires_at than the plan")
            if P1.post is not None and not (P1.post["t0"] - CLOCK_SLACK <= started <= P1.post["t1"] + CLOCK_SLACK):
                bad.append("started_at is %+.0f s from the POST" % (started - P1.post["t0"]))
            if not (started - CLOCK_SLACK <= made <= finished + CLOCK_SLACK) or finished > P1.final["t1"] + CLOCK_SLACK:
                bad.append("made_at is not between the start and the end of the job")
        verdict(V_TIMES, not bad, few(bad) if bad else "made at the end of a job of %d s" % (finished - started))
        if SHAPE_OK:
            verdict(V_VERDICT, PLAN["verdict"] != "blocked", "%s%s" % (PLAN["verdict"], "; the problems: " + few("%s %s" % (p["code"], json.dumps(p["data"], sort_keys=True)[:100]) for p in PLAN["problems"]) if PLAN["problems"] else ""))
            bad = []
            for p in PLAN["problems"]:
                if p["code"] not in PROBLEMS:
                    bad.append("a problem of a code that is not the contract's: %s" % cell(p["code"], 30))
                elif p["severity"] != PROBLEMS[p["code"]]:
                    bad.append("%s has the severity %s, not %s" % (p["code"], cell(p["severity"], 20), PROBLEMS[p["code"]]))
            sev = {p["severity"] for p in PLAN["problems"]}
            want = "blocked" if "blocked" in sev else "needs_acceptance" if "needs_acceptance" in sev else "ok"
            if PLAN["verdict"] != want:
                bad.append("the verdict is %s with problems of the severities %s (it is %s)" % (PLAN["verdict"], few(sorted(sev)) or "none", want))
            for r in PLAN["removals"]:
                if r["protected"] and not any(p["code"] == "removal_protected" and p["data"].get("name") == r["name"] for p in PLAN["problems"]):
                    bad.append("%s is protected and there is no removal_protected for it" % r["name"])
                if r["essential"] and not any(p["code"] == "removal_essential" and p["data"].get("name") == r["name"] for p in PLAN["problems"]):
                    bad.append("%s is essential and there is no removal_essential for it" % r["name"])
                if r["manual"] and not any(p["code"] == "removal_manual" and p["data"].get("name") == r["name"] for p in PLAN["problems"]):
                    bad.append("%s was installed by hand and there is no removal_manual for it" % r["name"])
            for p in PLAN["problems"]:
                if p["code"] == "disk_short":
                    x = p["data"]
                    if not (isinstance(x.get("path"), str) and is_int(x.get("need")) and ((is_int(x.get("free")) and x.get("unknown") is not True) or (x.get("unknown") is True and "free" not in x))):
                        bad.append("disk_short has not path, need and free (or unknown true and no free): %s" % cell(json.dumps(x, sort_keys=True), 100))
            kept_count, kept_problems = PLAN["counts"]["dist_upgrade"]["kept_back"], [p for p in PLAN["problems"] if p["code"] == "dist_kept_back"]
            if PLAN["kept_back"] and not kept_problems:
                bad.append("%s is kept back and there is no dist_kept_back problem" % few(PLAN["kept_back"]))
            elif kept_problems and not PLAN["kept_back"] and kept_count == 0 and not any(strings(p["data"].get("names")) and p["data"]["names"] for p in kept_problems):
                # (a package that the full upgrade leaves behind and says nothing of is named by the problem alone: no list and no count have it)
                bad.append("there is a dist_kept_back problem and nothing is kept back, and it names no package that is left behind")
            bad += problem_data_bad(PLAN)
            verdict(V_VERDICT_RULE, not bad, few(bad) if bad else "%d problems" % len(PLAN["problems"]))
            bad = ["%s%s" % (r["name"], " (%s)" % r.get("pattern") if r.get("pattern") else "") for r in PLAN["removals"] if r["protected"] or r["essential"]]
            bad += ["%s is protected and has no pattern" % r["name"] for r in PLAN["removals"] if r["protected"] and not r.get("pattern")]
            unexpected = [r["name"] for r in PLAN["removals"] if r["name"] not in EXPECTED_REMOVALS]
            verdict(V_REMOVALS, not bad, "protected or essential: " + few(bad) if bad else "removes %s%s" % (few(r["name"] for r in PLAN["removals"]) or "nothing",
                                                                                                                "; not of the measured set (%s): a finding, not a failure" % few(unexpected) if unexpected else ""))
            for key, name in (("dist_upgrade", V_COUNTS_DIST), ("upgrade", V_COUNTS_UP)):
                got, want = PLAN["counts"][key], MEASURED[key]
                off = ["%s %d (measured %d, %d to %d)" % (f.replace("_", " "), got[f], want[f], max(0, want[f] - tolerance(want[f])), want[f] + tolerance(want[f]))
                       for f in ("upgraded", "new", "removed", "kept_back") if abs(got[f] - want[f]) > tolerance(want[f])]
                verdict(name, not off, "off by more than 15 %% (rounded up to a whole package): %s" % few(off) if off else "%d upgraded, %d new, %d removed, %d kept back (the range allowed: %s)" % (
                    got["upgraded"], got["new"], got["removed"], got["kept_back"], ", ".join("%d to %d" % (max(0, want[f] - tolerance(want[f])), want[f] + tolerance(want[f])) for f in ("upgraded", "new", "removed", "kept_back"))))
            es = PLAN["estimates"]
            bad = []
            if not es["known"]:
                bad.append("estimates.known is false: apt did not say its sizes")
            else:
                if not (DOWNLOAD_MIN <= es["download_bytes"] <= DOWNLOAD_MAX):
                    bad.append("a download of %.0f MB, not between 100 MB and 2 GB" % (es["download_bytes"] / 1e6))
                if not (0 < es["extra_bytes"] <= EXTRA_MAX):
                    bad.append("%.0f MB of room taken, not between 0 and 8 GB" % (es["extra_bytes"] / 1e6))
            verdict(V_ESTIMATES, not bad, few(bad) if bad else "download %.0f MB, room taken %.0f MB" % (es["download_bytes"] / 1e6, es["extra_bytes"] / 1e6))
            rows = PLAN["disk"]
            bad = []
            if [r["path"] for r in rows] != list(DISK_PATHS):
                bad.append("the rows are %s, not %s" % (few(r["path"] for r in rows) or "none", ", ".join(DISK_PATHS)))
            for r in rows:
                free = r.get("free", 0)
                if r["need"] <= 0 or r["ok"] != (free >= r["need"] and not r.get("unknown")):
                    bad.append("%s: need %d, free %d, ok %s%s" % (cell(r["path"], 30), r["need"], free, r["ok"], ", unknown" if r.get("unknown") else ""))
                elif not r["ok"]:
                    bad.append("%s needs %d MB and %s" % (cell(r["path"], 30), r["need"] >> 20, "its room could not be read" if r.get("unknown") else "has %d MB" % (free >> 20)))
            verdict(V_DISK, not bad, few(bad) if bad else "; ".join("%s needs %d MB, has %d MB" % (r["path"], r["need"] >> 20, r["free"] >> 20) for r in rows))
            sb = PLAN["sandbox"]
            bad = []
            if not (0 < sb["bytes"] < SANDBOX_FLOOR):
                bad.append("the sandbox is %.0f MiB, not under 640 MiB (the floor of free room to make a plan)" % (sb["bytes"] / (1 << 20)))
            if sorted(sb["seconds_by_phase"]) != sorted(PHASES):
                bad.append("the phases with a time are %s" % (few(sorted(sb["seconds_by_phase"])) or "none"))
            verdict(V_SANDBOX, not bad, few(bad) if bad else "%.0f MiB, %s" % (sb["bytes"] / (1 << 20), ", ".join("%s %.1f s" % (p, sb["seconds_by_phase"][p]) for p in PHASES)))
            dk = PLAN["docker"]
            verdict(V_DOCKER, dk["present"] is True and dk["source_paused"] is True and dk["packages_held"] == [],
                    "present %s, source paused %s, held %s" % (dk["present"], dk["source_paused"], few(dk["packages_held"]) or "none"))
            if LEFT_OUT is not None:
                # the plan names the first MAX_LEFT_OUT of what the grammar leaves out, by name, and says when it is cut
                want_list, want_cut = capped_left_out(LEFT_OUT)
                want = {e["file"]: e["reason"] for e in want_list}
                got, bad = {}, []
                for e in PLAN["apt_conf_left_out"]:
                    if e["file"] in got:
                        bad.append("%s is named twice" % cell(e["file"], 40))
                    got.setdefault(e["file"], e["reason"])
                for f in sorted(set(got) - set(want)):
                    if f in LEFT_OUT:
                        bad.append("%s is named, and it is past the %d that the plan names (the first by name: the list is cut there)" % (cell(f, 40), MAX_LEFT_OUT))
                    else:
                        bad.append("%s is left out (%s), and the grammar keeps it" % (cell(f, 40), cell(got[f], 24)))
                for f in sorted(set(want) - set(got)):
                    bad.append("%s is not named as left out, and the grammar leaves it out (%s)" % (cell(f, 40), want[f]))
                for f in sorted(set(got) & set(want)):
                    if got[f] != want[f]:
                        bad.append("%s is left out for %s, and the grammar says %s" % (cell(f, 40), cell(got[f], 24), want[f]))
                if ("apt_conf_left_out_truncated" in PLAN) != want_cut:
                    bad.append("apt_conf_left_out_truncated is %s, and the grammar leaves out %d files (the plan names %d at most)" % ("there" if "apt_conf_left_out_truncated" in PLAN else "not there", len(LEFT_OUT), MAX_LEFT_OUT))
                verdict(V_HOOKS, not bad, few(bad) if bad else "left out: %s%s" % (few("%s (%s)" % (cell(f, 40), got[f]) for f in sorted(got)) or "nothing", " (the list is cut at %d, of %d)" % (MAX_LEFT_OUT, len(LEFT_OUT)) if want_cut else ""))
            if EXPECT is not None:
                ex_files, ex_paused, ex_tree, ex_dropped, ex_firmware, ex_hcur, ex_hgen = EXPECT
                src = PLAN["sources"]
                bad = []
                got_files = {f["file"]: f for f in src["files"]}
                if sorted(got_files) != sorted(f["file"] for f in ex_files) or len(got_files) != len(src["files"]):
                    bad.append("the files are %s, the box has %s" % (few(f["file"] for f in src["files"]), few(f["file"] for f in ex_files)))
                for f in ex_files:
                    g = got_files.get(f["file"])
                    if g is None:
                        continue
                    if g["action"] != f["action"]:
                        bad.append("%s is %s, it should be %s" % (cell(f["file"], 30), cell(g["action"], 12), f["action"]))
                    if g["lines"] != f["lines"]:
                        first = next((i for i, (a, b) in enumerate(zip(g["lines"], f["lines"])) if a != b), min(len(g["lines"]), len(f["lines"])))
                        bad.append("the lines of %s differ at the %s one: the plan says %s, the leg computes %s" % (
                            cell(f["file"], 30), ordinal(first + 1), cell(json.dumps(g["lines"][first]) if first < len(g["lines"]) else "nothing", 150),
                            cell(json.dumps(f["lines"][first]) if first < len(f["lines"]) else "nothing", 150)))
                key = lambda p: (p.get("file"), p.get("line", 0))
                if sorted(PLAN["paused"], key=key) != sorted(ex_paused, key=key):
                    bad.append("paused is %s, the leg computes %s" % (cell(json.dumps(PLAN["paused"]), 150), cell(json.dumps(ex_paused), 150)))
                for k, want in (("dropped_backports", ex_dropped), ("firmware_added", ex_firmware), ("hash_current", ex_hcur), ("hash_generated", ex_hgen)):
                    if src[k] != want:
                        bad.append("%s is %s, the leg computes %s" % (k, cell(src[k], 70), cell(want, 70)))
                verdict(V_SOURCES, not bad, few(bad, 4) if bad else "%d files, %d lines changed, %d put aside (%s), hash_current %s, hash_generated %s" % (
                    len(ex_files), sum(len(f["lines"]) for f in ex_files if f["action"] == "rewritten"), len(ex_paused), few(p["file"] for p in ex_paused) or "none", ex_hcur[:8], ex_hgen[:8]))
    extra_keys = sorted(set(PLAN) - KNOWN_KEYS) if P1_DONE else []


    # ---- the runbook of the first plan ----------------------------------------------------------------------------------------------------

    def split_blocks(text):
        """[(index, id, code)] of the blocks of a runbook text, by its own markers"""
        out, cur = [], None
        for ln in text.split("\n"):
            m = re.match(r"^# >>> BLOCK (\d+) (\S+)$", ln)
            if m:
                cur = [int(m.group(1)), m.group(2), []]
            elif cur is not None and re.match(r"^# <<< END BLOCK \d+$", ln):
                out.append((cur[0], cur[1], "\n".join(cur[2])))
                cur = None
            elif cur is not None:
                cur[2].append(ln)
        return out


    def tree_of(text):
        """{path: content} of the files that block 3 writes (a quoted heredoc each, written under a new name and moved in place), and the files it puts aside"""
        tree = {}
        for m in re.finditer(r"""cat > "\$APT_ETC/([^"]+)\.casaos-new" <<'CASAOS_EOF'\n(.*?)CASAOS_EOF\n""", text, re.S):
            dest = m.group(1)
            tree["sources.list" if dest == "sources.list" else "parts/" + dest[len("sources.list.d/"):]] = m.group(2)
        aside = re.findall(r'mv(?: -f)? "\$APT_ETC/sources\.list\.d/([^"]+)" "\$APT_ETC/sources\.list\.d/\1\.casaos-paused"', text)
        return tree, aside


    RB_TEXT = ""
    if SHAPE_OK and PLAN["verdict"] != "blocked":
        if rb1 is not None:
            bad = []
            d = dd(rb1)
            if rb1["kind"]:
                bad.append("no usable answer (%s)" % rb1["kind"])
            else:
                bad += envelope(rb1, 200)
                if rb1["http"] != 200:
                    bad.append("HTTP %d, not 200" % rb1["http"])
                text, blocks = d.get("text"), d.get("blocks")
                if d.get("plan_id") != PLAN["plan_id"] or d.get("hop") != PLAN["hop"]:
                    bad.append("plan_id %s and hop %s are not the plan's" % (cell(d.get("plan_id"), 40), cell(d.get("hop"), 20)))
                if not isinstance(text, str):
                    bad.append("text is not a string")
                else:
                    RB_TEXT = text
                    if len(text.encode("utf-8")) > MAX_RUNBOOK:
                        bad.append("the text is %d bytes, %d at most" % (len(text.encode("utf-8")), MAX_RUNBOOK))
                    if "# Plan: %s " % PLAN["plan_id"] not in text:
                        bad.append("the text does not say it is for this plan_id")
                ok_blocks = isinstance(blocks, list) and all(isinstance(b, dict) and isinstance(b.get("id"), str) and isinstance(b.get("title"), str) and isinstance(b.get("reboot"), bool)
                                                             and isinstance(b.get("point_of_no_return"), bool) for b in blocks)
                if not ok_blocks:
                    bad.append("blocks is not a list of {id, title, reboot, point_of_no_return}")
                else:
                    if tuple(b["id"] for b in blocks) != BLOCKS:
                        bad.append("the blocks are %s, not %s" % (few(b["id"] for b in blocks), ", ".join(BLOCKS)))
                    if [b["id"] for b in blocks if b["point_of_no_return"]] != ["minimal"]:
                        bad.append("the point of no return is %s, not minimal alone" % (few(b["id"] for b in blocks if b["point_of_no_return"]) or "nowhere"))
                    if [b["id"] for b in blocks if b["reboot"]] != ["prereboot"]:
                        bad.append("the restart is after %s, not prereboot alone" % (few(b["id"] for b in blocks if b["reboot"]) or "nowhere"))
                    if isinstance(text, str) and [b[1] for b in split_blocks(text)] != [b["id"] for b in blocks]:
                        bad.append("the blocks of the text (%s) are not the ones listed" % (few(b[1] for b in split_blocks(text)) or "none"))
            verdict(V_RB_SHAPE, not bad, few(bad) if bad else "%d blocks, %d bytes of text" % (len(BLOCKS), len(RB_TEXT.encode("utf-8"))))
            if RB_TEXT:
                shn = read("runbook-p1.shn.tsv", None)
                rows = [ln.split("\t") for ln in (shn or "").splitlines() if ln.strip()]
                mine = split_blocks(RB_TEXT)
                if shn is None or not rows:
                    invalid("runbook-p1.shn.tsv is missing or empty: the guest did not read the blocks of the runbook with sh -n")
                else:
                    got = [(r[0], r[1], r[2]) for r in rows if len(r) == 3]
                    if len(got) != len(rows):
                        invalid("runbook-p1.shn.tsv has a row that is not `index id status`")
                    else:
                        bad = []
                        if [(str(i), b) for i, b, _ in mine] != [(g[0], g[1]) for g in got]:
                            bad.append("the guest read blocks %s, the text has %s" % (few("%s %s" % (g[0], g[1]) for g in got), few("%d %s" % (i, b) for i, b, _ in mine)))
                        broken = [g[1] for g in got if g[2] != "0"]
                        if broken:
                            bad.append("sh -n does not read: %s" % few(broken))
                        verdict(V_RB_SHN, not bad, few(bad) if bad else "%d blocks, sh -n says 0 for each" % len(got))
                if EXPECT is not None:
                    tree, aside = tree_of(RB_TEXT)
                    bad = []
                    if tree != EXPECT[2]:
                        for p in sorted(set(tree) | set(EXPECT[2])):
                            if tree.get(p) != EXPECT[2].get(p):
                                bad.append("%s: the runbook writes %s, the leg computes %s" % (p, cell(json.dumps(tree.get(p)), 90), cell(json.dumps(EXPECT[2].get(p)), 90)))
                                break
                        if len(tree) != len(EXPECT[2]) or sorted(tree) != sorted(EXPECT[2]):
                            bad.append("the files written are %s, the leg computes %s" % (few(sorted(tree)) or "none", few(sorted(EXPECT[2]))))
                    if sorted(aside) != sorted(p["file"] for p in PLAN["paused"] if not p.get("line")):
                        bad.append("the files put aside are %s, the plan's are %s" % (few(sorted(aside)) or "none", few(sorted(p["file"] for p in PLAN["paused"] if not p.get("line"))) or "none"))
                    if canonical_hash({p: c.encode() for p, c in tree.items()}) != PLAN["sources"]["hash_generated"]:
                        bad.append("the tree it writes is not the one of the plan's hash_generated")
                    if "[ \"$now\" = '%s' ]" % PLAN["sources"]["hash_current"] not in RB_TEXT:
                        bad.append("the hash of the sources that block 0 checks is not the plan's hash_current")
                    if "[ \"$written\" = '%s' ]" % PLAN["sources"]["hash_generated"] not in RB_TEXT:
                        bad.append("the hash of the files written for apt that block 3 checks is not the plan's hash_generated")
                    verdict(V_RB_TREE, not bad, few(bad) if bad else "%s, put aside: %s, hash_current %s" % (few(sorted(tree)), few(aside) or "nothing", PLAN["sources"]["hash_current"][:8]))
    elif SHAPE_OK:
        not_testable("the runbook of the first plan (shape, sh -n, tree)", "the plan is blocked, and a blocked plan has no runbook: the problems above say why")


    # ---- the second plan, the plan with a hold, the last plan -------------------------------------------------------------------------------

    def step_state(sid):
        """(dirty-rc, undo-rc, undone) of a step as the guest wrote them, '' for what is not there"""
        return read("step-%s.dirty-rc" % sid).strip(), read("step-%s.undo-rc" % sid).strip(), read("step-%s.undone" % sid).strip()


    # a step whose undo failed leaves the box as it was not: what comes after is not held against the feature (the plan after the matrix would have another id), and the run is invalid
    HOLD_STATE = step_state("hold")
    UNDO_BROKEN = []
    HOLD_MADE = False
    if not all(HOLD_STATE):
        invalid("the hold step did not run to its end (dirty-rc, undo-rc or undone is missing)")
    else:
        HOLD_MADE = HOLD_STATE[0] == "0"
        if not HOLD_MADE:
            invalid("the hold step: %s could not be put on hold (exit %s)" % (HOLD, HOLD_STATE[0]))
        if HOLD_STATE[1] != "0" or HOLD_STATE[2] != "yes":
            invalid("the hold step: the undo failed (exit %s, box back: %s): the later plans start from a box that is not the clean one" % (HOLD_STATE[1], HOLD_STATE[2]))
            UNDO_BROKEN.append("hold")
    for _s in STEPS:
        _st = step_state(_s.id)
        if all(_st) and (_st[1] != "0" or _st[2] != "yes"):
            UNDO_BROKEN.append(_s.id)

    for r in ("p2", "p4"):
        run = R[r]
        if run.final is not None and not run.final["kind"] and run.state == "failed":
            e = plan_error(run)
            invalid("the plan %s failed with %s in its %s phase: %s" % (r, cell(e.get("code"), 30), cell(e.get("phase"), 12), cell(e.get("detail"), 120)))

    def id_form(x):
        return isinstance(x, str) and re.fullmatch(r"p1-[0-9a-f]{32}", x) is not None

    if P1_DONE and R["p2"].done:
        verdict(V_ID_STABLE, id_form(R["p2"].id) and R["p2"].id == P1.id, "%s and %s" % (cell(P1.id, 40), cell(R["p2"].id, 40)))
    if P1_DONE and R["p4"].done and not UNDO_BROKEN:
        verdict(V_ID_BACK, id_form(R["p4"].id) and R["p4"].id == P1.id, "%s, then %s after the matrix" % (cell(P1.id, 40), cell(R["p4"].id, 40)))
    p3 = R["p3"]
    if P1_DONE and p3.done and HOLD_MADE:
        holds = p3.plan.get("holds")
        # the plan with the hold is a plan: its id is of the form of one, and its shape and its counts are what the first plan's are (the id of a plan that is not one is any text)
        shape3 = plan_problems(p3.plan, dd(p3.final))
        shape3 += [] if shape3 else plan_consistency(p3.plan)
        if not id_form(p3.id):
            shape3.insert(0, "plan_id is not p1- and 32 hex")
        verdict(V_ID_HOLD, p3.id != P1.id and holds == [HOLD] and not shape3, few(shape3, 4) if shape3 else "%s with %s on hold, against %s" % (cell(p3.id, 40), few(holds) if isinstance(holds, list) and holds else "nothing", cell(P1.id, 40)))
        kept = p3.plan.get("kept_back")
        if isinstance(kept, list) and HOLD in kept:
            bad = []
            if p3.plan.get("verdict") != "blocked":
                bad.append("the verdict is %s, not blocked, with %s kept back" % (cell(p3.plan.get("verdict"), 20), HOLD))
            if not any(isinstance(pr, dict) and pr.get("code") == "dist_kept_back" and HOLD in (inner(pr, "data", "names") or []) for pr in p3.plan.get("problems") or []):
                bad.append("no dist_kept_back names %s" % HOLD)
            if rb3 is not None:
                b2, data = refusal_of(rb3, 409, "no_plan")
                bad += ["the runbook of the blocked plan: " + b for b in b2]
                if data.get("reason") != "blocked":
                    bad.append("the runbook's reason is %s, not blocked" % cell(data.get("reason"), 20))
            verdict(V_RB_BLOCKED, not bad, few(bad) if bad else "blocked by dist_kept_back (%s), the runbook says no_plan, blocked" % HOLD)
        else:
            # apt keeps back a package that is on hold and has a newer candidate (measured, apt 2.2.4, in -s upgrade and in -s dist-upgrade), and no package of bookworm needs a newer base-files than the held one:
            # a plan that does not name the held package as kept back lost it, whatever else it says. It is not a premise that is missing, and it is never NOT TESTABLE, which leaves the run green
            verdict(V_RB_BLOCKED, False, "the guest put %s on hold; the plan lists the holds %s and keeps back %s: it lost the held package, and it is not blocked by dist_kept_back" % (
                HOLD, few(holds) if isinstance(holds, list) and holds else "nothing", few(kept) if isinstance(kept, list) and kept else "nothing"))
    elif HOLD_MADE and p3.final is not None and not p3.final["kind"] and p3.state == "failed":
        e = plan_error(p3)
        invalid("the plan with the hold failed with %s in its %s phase: %s" % (cell(e.get("code"), 30), cell(e.get("phase"), 12), cell(e.get("detail"), 120)))


    # ---- Cache-Control: no-store on every answer of the routes ----------------------------------------------------------------------------------------

    bad, seen = [], 0
    for n, c in sorted(ANSWERS.items()):
        if c["kind"]:
            continue
        if c["headers"] is None:
            invalid("%s.hdr is missing: the guest did not keep the headers of the answer" % n)
            continue
        seen += 1
        if "no-store" not in c["headers"].get("cache-control", "").lower().replace(" ", "").split(","):
            bad.append("%s says %s" % (n, cell(c["headers"].get("cache-control", "nothing"), 40)))
    if seen:
        verdict(V_NOSTORE, not bad, few(bad, 4) if bad else "%d answers" % seen)


    # ---- the box's own name, in no answer ----------------------------------------------------------------------------------------------------------

    if NEEDLES:
        bad, looked_at = [], 0
        for n, c in sorted(ANSWERS.items()):
            looked_at += 1
            text = read(n + ".json")
            for kind, value in NEEDLES:
                if re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(value) + r"(?![A-Za-z0-9_-])", text, re.I):
                    bad.append("%s says the box's %s (%s)" % (n, kind, cell(value, 40)))
        verdict(V_LEAK, not bad, few(bad, 4) if bad else "%d answers looked at, for %d names of the box" % (looked_at, len(NEEDLES)))


    # ---- invariance ------------------------------------------------------------------------------------------------------------------------

    def manifest(tag):
        text = read("inv-%s.tsv" % tag)
        if not text.strip():
            invalid("inv-%s.tsv is missing or empty: the snapshot of the files did not run" % tag)
            return None
        rows = {}
        for line in text.splitlines():
            f = line.split("\t")
            try:
                path = json.loads(f[0])
            except ValueError:
                path = None
            if len(f) != 8 or not isinstance(path, str):
                invalid("inv-%s.tsv has a row that is not a manifest row: %s" % (tag, cell(line, 80)))
                return None
            rows[path] = tuple(f[1:])
        for tree in TREES:
            if tree not in rows:
                invalid("inv-%s.tsv has no row for %s: that tree was not looked at" % (tag, tree))
                return None
        return rows


    def exempt(path):
        return path == EXEMPT or path.startswith(EXEMPT + "/")


    def diff_rows(a, b):
        """(changed [(path, [columns])], created [path], removed [path]) between two manifests"""
        changed = [(p, [COLS[i] for i in range(len(COLS)) if a[p][i] != b[p][i]]) for p in sorted(a.keys() & b.keys()) if a[p] != b[p]]
        return changed, sorted(b.keys() - a.keys()), sorted(a.keys() - b.keys())


    def units(tag):
        text = read("units-%s.tsv" % tag)
        if not text.strip():
            invalid("units-%s.tsv is missing or empty: the list of the units did not run" % tag)
            return None
        rows = {}
        for line in text.splitlines():
            f = line.split("\t")
            if len(f) != 5 or not f[0]:
                invalid("units-%s.tsv has a row that is not a unit row: %s" % (tag, cell(line, 80)))
                return None
            rows[f[0]] = tuple(f[1:])
        return rows


    def ports(tag):
        rows = [ln.strip() for ln in read("listen-%s.txt" % tag).splitlines() if ln.strip()]
        if not rows:
            invalid("listen-%s.txt is missing or empty: no listening port was recorded (ss did not run)" % tag)
            return None
        return set(rows)


    def listing(tag):
        """the paths of /var/tmp as the guest listed them (an empty listing is a listing), or None (INVALID)"""
        text = read("tmp-%s.txt" % tag, None)
        if text is None:
            invalid("tmp-%s.txt is missing: the listing of /var/tmp was not made" % tag)
            return None
        return {ln for ln in text.splitlines() if ln.strip()}


    ma, mb, m1, m2 = manifest("ctl-a"), manifest("ctl-b"), manifest("before"), manifest("after")
    ua, ub, u1, u2 = units("ctl-a"), units("ctl-b"), units("before"), units("after")
    pa, pb, p1_, p2_ = ports("ctl-a"), ports("ctl-b"), ports("before"), ports("after")
    ta, tb, t1, t2 = listing("ctl-a"), listing("ctl-b"), listing("before"), listing("after")
    timers = read("timer-units.txt", None)
    if timers is None:
        invalid("timer-units.txt is missing: the units that a timer starts are not known")
        timers = ""
    TIMER_UNITS = {ln.strip() for ln in timers.splitlines() if ln.strip()}

    def control_noise(what, a, b, exempt=lambda k: False):
        """what changed by itself between the two snapshots of the control (dicts of rows, or sets): its keys, which the verdicts leave out. A key that only one of the two has is a row
        that appeared or went away: a few are the box's own (a socket, a unit that ran), but a snapshot that is cut short has most of them, and would excuse whatever the window
        changes. More than a few of them, or a noise of more than a quarter of the rows, and the control is not believed: the run is invalid and nothing is left out"""
        ka, kb = set(a), set(b)
        only = {k for k in ka ^ kb if not exempt(k)}
        changed = {k for k in ka & kb if not exempt(k) and isinstance(a, dict) and a[k] != b[k]}
        rows = max(len(ka), len(kb))
        if len(only) > max(NOISE_MIN, NOISE_SHARE * rows):
            invalid("the two snapshots of the control (%s) differ in %d rows that only one of them has, out of %d: one of them is cut short, or the box was not quiet. Nothing is left out of the verdicts for it" % (what, len(only), rows))
            return set()
        if len(only) + len(changed) > max(10, rows // 4):
            invalid("%s changed by themselves in %d of %d rows during the control: the box was not quiet. Nothing is left out of the verdicts for it" % (what, len(only) + len(changed), rows))
            return set()
        return only | changed


    noisy_files = noisy_units = noisy_ports = noisy_tmp = set()
    if ma is not None and mb is not None:
        noisy_files = control_noise("the files", ma, mb, exempt)
    if ua is not None and ub is not None:
        noisy_units = control_noise("the units", ua, ub)
    if pa is not None and pb is not None:
        noisy_ports = control_noise("the listening sockets", pa, pb)
    if ta is not None and tb is not None:
        noisy_tmp = control_noise("the paths of /var/tmp", ta, tb)

    if m1 is not None and m2 is not None:
        ch, cr, rm = diff_rows(m1, m2)
        ch = [(p, c) for p, c in ch if not exempt(p) and p not in noisy_files]
        cr = [p for p in cr if not exempt(p) and p not in noisy_files]
        rm = [p for p in rm if not exempt(p) and p not in noisy_files]
        said = ["changed %s (%s)" % (cell(p, 100), "/".join(c)) for p, c in ch] + ["created %s" % cell(p, 100) for p in cr] + ["removed %s" % cell(p, 100) for p in rm]
        verdict(V_INV_FILES, not said, few(said) if said else "%d paths looked at (these %d trees only, not /tmp, /run or the rest of the machine), %d of them changed by themselves in the control and left out" % (len(m1), len(TREES), len(noisy_files)))
        row = m1.get(STAMP)
        if row is None:
            not_testable(V_STAMP, "this box has no %s: Debian 11 has no 15update-stamp (Ubuntu has), so the hook that touches it cannot be caught here; the canary steps below (a hook of apt-get update that "
                                  "a filter of text does not see, and the option of gpgv) are what shows that the box's hooks and options do not run in the sandbox" % STAMP)
        elif STAMP in noisy_files:
            not_testable(V_STAMP, "the stamp changed by itself during the control (a timer of the box touches it): this leg cannot tell the plan's touch from the box's own")
        else:
            verdict(V_STAMP, m2.get(STAMP) == row, "the stamp is %s" % ("untouched" if m2.get(STAMP) == row else "changed: the sandbox's update ran a hook of the box"))


    def clock(t, digits=0):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S.%f")[:8 + (digits + 1 if digits else 0)]


    def journal_time(us):
        if not isinstance(us, str) or not re.fullmatch(r"[0-9]{1,20}", us):
            return None
        t = int(us) / 1e6
        return t if plausible(t) else None


    def marker_time(name):
        """the time of the marker of that name in timeline.tsv, None when it is not there exactly once or its time is not a plausible one"""
        times = [r[1] for r in (ln.split("\t") for ln in read("timeline.tsv").splitlines()) if len(r) == 3 and r[0] == "#" and r[2] == name]
        try:
            t = float(times[0]) if len(times) == 1 else None
        except ValueError:
            return None
        return t if plausible(t) else None


    def pid1_entries(name):
        """([(unit, message, time or None)] of the entries about a unit in a file of `journalctl _PID=1 -o json`, why that file cannot be believed ('' when it can))"""
        text = read(name, None)
        if text is None:
            return [], "there is no %s" % name
        out, seen = [], 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except (ValueError, RecursionError):
                return [], "a line of %s is not JSON" % name
            if not isinstance(e, dict):
                return [], "a line of %s is not a JSON object" % name
            seen += 1
            if "UNIT" in e:
                if not isinstance(e["UNIT"], str):
                    return [], "an entry of %s has a UNIT that is not a string" % name
                out.append((e["UNIT"], e["MESSAGE"] if isinstance(e.get("MESSAGE"), str) else "", journal_time(e.get("__REALTIME_TIMESTAMP"))))
        return out, "" if seen else "%s has no entry" % name


    def control_events():
        """({unit: (time, message) of its first entry in PID 1's journal of the control}, why none is believed ('')): the entries from CONTROL_BEFORE s before the marker
        control_start to CONTROL_AFTER s after the marker invariance_start. FAILS CLOSED: a file that is missing or empty, a line that is not a JSON object, a UNIT
        that is not a string, a time that is not a plausible one, markers that are not there or not in that order: no unit is believed of it"""
        entries, why = pid1_entries("pid1-control.jsonl")
        c0, c1 = marker_time("control_start"), marker_time("invariance_start")
        if not why and (c0 is None or c1 is None or c1 < c0):
            why = "the timeline has no marker control_start and invariance_start, in that order: the interval of the control is not known"
        if not why and any(t is None for _, _, t in entries):
            why = "an entry of pid1-control.jsonl has a time that is not a plausible one"
        found = {}
        if not why:
            for u, msg, t in sorted(entries, key=lambda x: x[2]):
                if c0 - CONTROL_BEFORE <= t <= c1 + CONTROL_AFTER:
                    found.setdefault(u, (t, msg))
        return found, why


    SELF_STARTED, SELF_WHY = control_events()


    def by_itself(u):
        t, msg = SELF_STARTED[u]
        return "%s: %s by itself at %s during the control" % (u, "started" if msg.startswith("Start") else "stopped" if msg.startswith("Stop") or "Deactivated" in msg else "changed", clock(t))


    if u1 is not None and u2 is not None:
        said, excused = [], []
        for u in sorted(u1.keys() | u2.keys()):
            # a unit that a timer starts may run in the window by itself
            if u in noisy_units or u in TIMER_UNITS:
                continue
            a, b = u1.get(u), u2.get(u)
            if a is None or b is None:
                what = "%s %s" % (u, "appeared" if a is None else "disappeared")
            elif a != b:
                what = "%s went from %s/%s (pid %s, invocation %s) to %s/%s (pid %s, invocation %s)" % (u, a[0], a[1], a[2], a[3][:8], b[0], b[1], b[2], b[3][:8])
            else:
                continue
            # a unit that started or stopped by itself during the control is noise wherever else it changes
            if u in SELF_STARTED:
                excused.append(by_itself(u))
            else:
                said.append(what)
        verdict(V_INV_UNITS, not said, (few(said) + (" | left out, started or stopped by themselves during the control (PID 1's own journal): " + few(excused) if excused else "")
                                        + (" | PID 1's journal of the control was not used: %s" % SELF_WHY if SELF_WHY else "")) if said
                else "%d units looked at, %d changed by themselves in the control and left out%s" % (len(u1), len(noisy_units), "; started or stopped by themselves during the control (PID 1's own journal) and left out: " + few(excused) if excused else ""))

    if p1_ is not None and p2_ is not None:
        said = ["%s %s" % ("stopped listening" if x in p1_ else "started listening", x) for x in sorted((p1_ ^ p2_) - noisy_ports)]
        verdict(V_INV_PORTS, not said, few(said) if said else "%d listening sockets, the same before and after" % len(p1_))

    if t1 is not None and t2 is not None:
        left = sorted(p for p in t2 if p.startswith(SANDBOX_PREFIX))
        said = ["a sandbox directory is left: %s" % cell(p, 80) for p in left]
        said += ["the core says it could not remove %s after the plan" % left_behind(P1.final)] if left_behind(P1.final) else []
        said += ["created %s" % cell(p, 100) for p in sorted(t2 - t1 - noisy_tmp) if p not in left] + ["removed %s" % cell(p, 100) for p in sorted(t1 - t2 - noisy_tmp)]
        verdict(V_INV_TMP, not said, few(said) if said else "%d paths listed, the same before and after%s" % (len(t1), "; %d changed by themselves in the control and left out" % len(noisy_tmp) if noisy_tmp else ""))


    def containers(tag):
        text = read("witnesses-%s.txt" % tag, None)
        if text is None:
            invalid("witnesses-%s.txt is missing: the witness containers were not looked at" % tag)
            return None
        rows = sorted(ln for ln in text.splitlines() if ln.strip())
        names = {r.split("\t")[0] for r in rows}
        if not all(w in names for w in WITNESSES):
            invalid("witnesses-%s.txt does not have the four witness containers (%s): the box is not the one the proof is for" % (tag, ", ".join(WITNESSES)))
            return None
        return rows


    w1, w2 = containers("before"), containers("after")
    if w1 is not None and w2 is not None:
        verdict(V_INV_WITNESS, w1 == w2, "%d containers, the same start times" % len(w1) if w1 == w2 else "the rows differ: %s" % few(sorted(set(w1) ^ set(w2))))

    P("## What the invariance window looks at")
    P("")
    P("The names, sizes, times, mode, owner and content of every file of %s; every service unit with its state, main pid and invocation id; the listening sockets; the listing of /var/tmp; "
      "the start times of the witness containers. It does not look at /tmp, /run, /home, /root, /proc or /sys, nor at anything else." % ", ".join(TREES))
    P("")
    P("The window is: GET of the plan, GET of the runbook, POST, a second POST, and GET every second until the job is done (plan-idle, runbook-idle, plan-p1-post, plan-p1-post-2, the polls, plan-p1-get). "
      "Every other call (the runbook of the plan, the later plans, the dirty matrix) is outside it, and is looked at for no more than what the verdicts say.")
    P("")
    P("## What changed by itself (the control)")
    P("")
    c0_, c1_ = marker_time("control_start"), marker_time("invariance_start")
    P("Two snapshots, with two calls of `GET /v1/sys/version/current` and a wait between them%s, as long as the cadence of what a box starts by itself. What differs between the two snapshots is left out of the verdicts above, and named here. So "
      "is every unit that PID 1's own journal of the control (`journalctl _PID=1 -o json`) has an entry of from %d s before the marker control_start to %d s after the marker invariance_start: a unit that started or stopped by itself "
      "during the control is noise wherever else it changes. Only units are left out so, never a file or a listening socket." % (
          " (it lasted %d s)" % round(c1_ - c0_) if c0_ is not None and c1_ is not None and c1_ >= c0_ else "", CONTROL_BEFORE, CONTROL_AFTER))
    P("")
    P("- files: %s" % (few(sorted(noisy_files)) if noisy_files else "none"))
    P("- units: %s" % (few(sorted(noisy_units)) if noisy_units else "none"))
    P("- units that started or stopped by themselves during the control, by PID 1's own journal: %s" % (few(by_itself(u) for u in sorted(SELF_STARTED)) if SELF_STARTED
                                                                                                        else "none (pid1-control.jsonl was not used, and no unit is left out for it: %s)" % SELF_WHY if SELF_WHY else "none"))
    P("- listening sockets: %s" % (few(sorted(noisy_ports)) if noisy_ports else "none"))
    P("- paths of /var/tmp: %s" % (few(sorted(noisy_tmp)) if noisy_tmp else "none"))
    P("- units a timer starts (their invocation ids are not held against the plan): %d" % len(TIMER_UNITS))
    P("")


    # ---- the dirty matrix ----------------------------------------------------------------------------------------------------------------------

    def step_name(s):
        """the name of the verdict of a step"""
        if s.kind == "refused":
            return F_REFUSED % (s.id, s.code, " with %s" % ", ".join("%s %s" % kv for kv in sorted(s.data.items())) if s.data else "")
        if s.kind == "canary":
            return F_CANARY % (s.id, s.frag, s.reason, s.file)
        return F_JOB % (s.id, s.phase, s.code, " naming %s" % s.needle if s.needle else "") + (" (kind %s)" % " or ".join(s.kind_in) if s.kind_in else "")


    def detail_line_cap(code):
        """the longest line of the detail of a failure of this code: apt's words are cut by the core at DETAIL_LINE, but a source that it refuses (sources_*) is its own sentence, a file, a line and
        the reason (the one for a source that is already at the target release says where the way back is), and is a short text"""
        return MAX_SHOWN if isinstance(code, str) and code.startswith("sources_") else DETAIL_LINE


    def detail_problems(detail, code):
        """what is wrong with the `detail` of a job that failed with this code: at most 6 lines, DETAIL_LINE characters each (MAX_SHOWN for the sources_* codes), printable ASCII, and a URL no more
        than its scheme and host (what a mirror says is not for the dashboard to repeat: the user name, the password, the path and the query are cut)"""
        if not isinstance(detail, str):
            return ["the detail is not a string"]
        bad, rows, cap = [], detail.split("\n"), detail_line_cap(code)
        if len(rows) > 6:
            bad.append("the detail has %d lines, 6 at most" % len(rows))
        if any(len(r) > cap for r in rows):
            bad.append("a line of the detail is longer than %d characters" % cap)
        if re.search(r"[^\x20-\x7e\n]", detail):
            bad.append("the detail is not printable ASCII")
        for u in URL_IN_TEXT.findall(detail):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9.-]+(?::[0-9]+)?", u.rstrip(".,;:'\")")):
                bad.append("the detail has a URL that is more than its scheme and host: %s" % cell(u, 60))
                break
        return bad


    # the failures that the box's apt configuration can explain (a proxy that was left out is a network that cannot be reached): they carry the files that the sandbox left out
    LEFT_OUT_CODES = ("sandbox_update_failed", "sandbox_solver_failed", "plan_unparsed")


    def failure_left_out_problems(e):
        """what is wrong with the list of the files left out in the data of a job that failed (`e`, the error of its status): a failure of LEFT_OUT_CODES carries the plan's own list when the sandbox left
        any file out (data.apt_conf_left_out: the same files, reasons and order as the plan names, at most MAX_LEFT_OUT, and data.apt_conf_left_out_truncated true when it is cut), and none when
        nothing was left out; a failure of another code carries neither key"""
        if LEFT_OUT is None:
            return []
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        want, cut = capped_left_out(LEFT_OUT) if e.get("code") in LEFT_OUT_CODES else ([], False)
        bad, got = [], data.get("apt_conf_left_out")
        if want:
            if got != want:
                bad.append("data.apt_conf_left_out is %s, and the plan's list is %s" % (cell(json.dumps(got), 110) if "apt_conf_left_out" in data else "not there", cell(json.dumps(want), 110)))
        elif "apt_conf_left_out" in data:
            bad.append("data.apt_conf_left_out is there, and %s (it says %s)" % ("nothing was left out of apt's configuration" if e.get("code") in LEFT_OUT_CODES else "a failure of this code does not carry it", cell(json.dumps(got), 60)))
        if ("apt_conf_left_out_truncated" in data) != cut or (cut and data["apt_conf_left_out_truncated"] is not True):
            bad.append("data.apt_conf_left_out_truncated is %s, and the list is %s" % (cell(json.dumps(data["apt_conf_left_out_truncated"]), 20) if "apt_conf_left_out_truncated" in data else "not there", "cut" if cut else "not cut"))
        return bad


    def run_step(s):
        """(judged, the call of the POST) for one step, or (False, None)"""
        skip = read("step-%s.skipped" % s.id).strip()
        if skip:
            invalid("step %s: the guest skipped it (%s), and it exists on this system" % (s.id, cell(skip, 100)))
            return None
        dirty_rc, undo_rc, undone = read("step-%s.dirty-rc" % s.id).strip(), read("step-%s.undo-rc" % s.id).strip(), read("step-%s.undone" % s.id).strip()
        if not dirty_rc or not undo_rc or not undone:
            invalid("step %s: it did not run to its end (dirty-rc, undo-rc or undone is missing)" % s.id)
            return None
        return dirty_rc, undo_rc, undone


    P("## The dirty matrix")
    P("")
    P("| step | what it does | the plan says | after the undo |")
    P("|---|---|---|---|")
    tainted_by = ""    # the first step whose undo failed: what comes after it was made on a box that is not the clean one, and is not held against the feature
    leftovers, no_runbook, looked = [], [], 0
    for _r in ("p2", "p3", "p4"):
        if left_behind(R[_r].final):
            leftovers.append("%s: the core says it could not remove %s" % (_r, left_behind(R[_r].final)))
    for s in STEPS:
        ran = run_step(s)
        if ran is None:
            continue
        dirty_rc, undo_rc, undone = ran
        trusted = True
        if dirty_rc != "0":
            invalid("step %s: the box could not be dirtied (exit %s): %s" % (s.id, dirty_rc, cell(" ".join(read("step-%s.dirty.log" % s.id).split())[-160:], 160)))
            trusted = False
        undo_failed = undo_rc != "0" or undone != "yes"
        was_tainted = tainted_by
        if undo_failed:
            invalid("step %s: the undo failed (exit %s, box back: %s): the leg is invalid, and the later steps start from a box that is not the clean one" % (s.id, undo_rc, undone))
            tainted_by = tainted_by or s.id
        if was_tainted:
            P("| %s | %s | not judged: the undo of step %s failed | |" % (s.id, cell(s.what, 70), was_tainted))
            continue
        if not trusted:
            P("| %s | %s | not run | |" % (s.id, cell(s.what, 70)))
            continue
        post = CALLS.get("step-%s-post" % s.id)
        if post is None:
            P("| %s | %s | no answer | |" % (s.id, cell(s.what, 70)))
            continue
        left = read("step-%s.leftover" % s.id, None)
        if left is None:
            invalid("step %s: step-%s.leftover is missing: /var/tmp was not looked at after the plan" % (s.id, s.id))
        else:
            looked += 1
            leftovers += ["%s: %s" % (s.id, cell(ln, 60)) for ln in left.splitlines() if ln.strip()]
        if left_behind(CALLS.get("step-%s-get" % s.id)):
            leftovers.append("%s: the core says it could not remove %s" % (s.id, left_behind(CALLS.get("step-%s-get" % s.id))))
        said = ""
        if s.kind == "refused":
            bad, data = refusal_of(post, 409, s.code)
            for k, v in s.data.items():
                if data.get(k) != v:
                    bad.append("data.%s is %s, not %s" % (k, cell(data.get(k), 40), v))
            if s.code == "no_room" and not (is_int(data.get("free")) and is_int(data.get("need")) and 0 <= data["free"] < data["need"]):
                bad.append("free is %s: not a room that is below the %s that the plan needs" % (cell(data.get("free"), 20), cell(data.get("need"), 20)))
            verdict(step_name(s), not bad, few(bad) if bad else "409 %s" % s.code)
            said = "409 %s" % cell(dd(post).get("error_code"), 30)
        elif s.kind == "canary":
            final = CALLS.get("step-%s-get" % s.id)
            bad, net = [], False
            if post["kind"] or post["http"] != 202:
                bad.append("the POST answered %s, not 202 (%s)" % ("HTTP %d" % post["http"] if not post["kind"] else post["kind"], cell(dd(post).get("error_code") or dd(post).get("error"), 60)))
            elif final is None or final["kind"]:
                bad.append("no usable GET at the end of the job")
            else:
                d_ = dd(final)
                bad += (["HTTP %d, not 200" % final["http"]] if final["http"] != 200 else []) + envelope(final, 200)
                if d_.get("state") != "done" or not isinstance(d_.get("plan"), dict):
                    e = d_.get("error") if isinstance(d_.get("error"), dict) else {}
                    if d_.get("state") == "failed" and e.get("code") == "sandbox_update_failed" and inner(e, "data", "kind") == "network":
                        # as the first plan's: a mirror that was not reached says nothing against the feature, and the plan that was not made says nothing of the fragment
                        net = True
                        invalid("step %s: the plan failed in its update for a reason of the network (%s): the mirrors of the target were not reached, and this says nothing against the feature" % (s.id, cell(e.get("detail"), 120)))
                    else:
                        bad.append("the job is %s, not done%s" % (cell(d_.get("state"), 10), " (%s in %s)" % (cell(e.get("code"), 30), cell(e.get("phase"), 12)) if e else ""))
                else:
                    listed = d_["plan"].get("apt_conf_left_out")
                    left = {e["file"]: e.get("reason") for e in listed if isinstance(e, dict) and isinstance(e.get("file"), str)} if isinstance(listed, list) else {}
                    if s.frag not in left:
                        bad.append("the plan does not name %s as left out (it names %s)" % (s.frag, few(sorted(left)) or "nothing"))
                    elif left[s.frag] != s.reason:
                        bad.append("%s is left out for %s, not for %s" % (s.frag, cell(left[s.frag], 24), s.reason))
                    said = "done: %s left out (%s)" % (s.frag, cell(left.get(s.frag), 24))
            made = read("step-%s.canary" % s.id, None)
            if made is None:
                invalid("step %s: the guest did not look for the file %s that the fragment makes" % (s.id, s.file))
            elif made.strip() != "absent":
                bad.append("%s is there after the plan: what the fragment says ran in the sandbox's update" % s.file)
            if bad or not net:     # a plan that was not made for the network has no verdict, unless the hook's file is there: that is a FAIL whatever else happened
                verdict(step_name(s), not bad, few(bad) if bad else "%s is not made" % s.file)
        else:
            final = CALLS.get("step-%s-get" % s.id)
            bad = []
            if post["kind"] or post["http"] != 202:
                bad.append("the POST answered %s, not 202 (%s)" % ("HTTP %d" % post["http"] if not post["kind"] else post["kind"], cell(dd(post).get("error_code") or dd(post).get("error"), 60)))
            elif final is None or final["kind"]:
                bad.append("no usable GET at the end of the job")
            else:
                e = dd(final).get("error") if isinstance(dd(final).get("error"), dict) else {}
                bad += (["HTTP %d, not 200" % final["http"]] if final["http"] != 200 else []) + envelope(final, 200)
                if dd(final).get("state") != "failed" or not e:
                    bad.append("the job is %s, not failed" % cell(dd(final).get("state"), 10))
                else:
                    bad += detail_problems(e.get("detail"), e.get("code"))
                    bad += failure_left_out_problems(e)
                    if e.get("code") != s.code:
                        bad.append("the code is %s, not %s (%s)" % (cell(e.get("code"), 30), s.code, cell(e.get("detail"), 100)))
                    if e.get("phase") != s.phase or dd(final).get("phase") != s.phase:
                        bad.append("the phase is %s, not %s" % (cell(e.get("phase"), 12), s.phase))
                    if s.needle and s.needle not in json.dumps(e):
                        bad.append("it does not say %s" % s.needle)
                    if s.kind_in and inner(e, "data", "kind") not in s.kind_in:
                        bad.append("data.kind is %s, not %s" % (cell(inner(e, "data", "kind"), 20), " or ".join(s.kind_in)))
                    said = "failed: %s in %s%s" % (cell(e.get("code"), 30), cell(e.get("phase"), 12), " (%s)" % cell(inner(e, "data", "kind"), 20) if inner(e, "data", "kind") else "")
                rbk = CALLS.get("step-%s-runbook" % s.id)
                if rbk is not None:
                    b2, data = refusal_of(rbk, 409, "no_plan")
                    if data.get("reason") != "failed":
                        b2.append("reason is %s, not failed" % cell(data.get("reason"), 20))
                    no_runbook += ["%s: %s" % (s.id, b) for b in b2]
            verdict(step_name(s), not bad, few(bad) if bad else said)
        P("| %s | %s | %s | %s |" % (s.id, cell(s.what, 70), said or "no usable answer", "the undo failed" if undo_failed else "undone"))
    P("")
    if any(CALLS.get("step-%s-runbook" % s.id) is not None for s in STEPS):
        verdict(V_STEP_NORUNBOOK, not no_runbook, few(no_runbook) if no_runbook else "409 no_plan, failed, after each job that failed")
    if looked:
        verdict(V_STEP_LEFTOVER, not leftovers, few(leftovers) if leftovers else "nothing of the sandbox is left after any of the %d steps" % looked)
    for what, why in NOT_TESTABLE:
        not_testable(what, why)


    # ---- the facts: the first real-guest data of the rung ----------------------------------------------------------------------------------------

    P("## The plans, run by run (the first real-guest data of the rung: the numbers re-tare the floors of the disk and of /var/tmp)")
    P("")
    P("| run | verdict | plan_id | upgraded / new / removed / kept back (full) | upgraded / new / removed / kept back (minimal) | download | room taken | sandbox | wall |")
    P("|---|---|---|---|---|---|---|---|---|")
    for r in RUNS:
        run = R[r]
        if not run.done or not isinstance(run.plan, dict):
            P("| %s | %s | | | | | | | |" % (r, "no plan (%s)" % cell(run.state or "no usable answer", 20)))
            continue
        pl = run.plan
        try:
            c1, c2, es, sb = pl["counts"]["dist_upgrade"], pl["counts"]["upgrade"], pl["estimates"], pl["sandbox"]
            wall = (run.final["t1"] - run.post["t0"]) if run.post and not run.post["kind"] else None
            P("| %s | %s | %s | %s | %s | %s | %s | %.0f MiB | %s |" % (
                r, cell(pl.get("verdict"), 20), cell(pl.get("plan_id"), 40), " / ".join(str(c1[k]) for k in ("upgraded", "new", "removed", "kept_back")), " / ".join(str(c2[k]) for k in ("upgraded", "new", "removed", "kept_back")),
                "%.0f MB" % (es["download_bytes"] / 1e6) if es.get("known") else "unknown", "%.0f MB" % (es["extra_bytes"] / 1e6) if es.get("known") else "unknown", sb["bytes"] / (1 << 20),
                "%.0f s" % wall if wall is not None else "?"))
        except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
            P("| %s | %s | | the plan is not of the shape the table reads | | | | | |" % (r, cell(pl.get("verdict"), 20)))
    P("")
    if SHAPE_OK:
        P("Seconds by phase (first plan): %s." % ", ".join("%s %s" % (cell(k, 14), cell(v, 8)) for k, v in sorted(PLAN["sandbox"]["seconds_by_phase"].items(), key=lambda kv: PHASES.index(kv[0]) if kv[0] in PHASES else 9)))
        P("")
        P("Removals: %s." % (few("%s %s%s" % (cell(r.get("name"), 30), cell(r.get("version"), 30), " (manual)" if r.get("manual") else "") for r in PLAN["removals"]) or "none"))
        P("Kept back after the full upgrade: %s. New packages: %d. Autoremovable: %d." % (few(PLAN["kept_back"]) or "none", len(PLAN["new_packages"]), len(PLAN["autoremovable"])))
        P("Problems: %s." % (few("%s %s" % (cell(p.get("code"), 30), cell(json.dumps(p.get("data"), sort_keys=True), 100)) for p in PLAN["problems"]) or "none"))
        P("Left out of the sandbox's apt configuration: %s. Keys of the plan that the contract does not list: %s." % (
            few("%s (%s)" % (cell(e.get("file"), 60), cell(e.get("reason"), 24)) for e in PLAN["apt_conf_left_out"]) or "nothing", few(extra_keys) if extra_keys else "none"))
        P("")
    if EXPECT is not None:
        P("What this leg computes of the box's sources, independently: hash_current %s, hash_generated %s, %d lines dropped as backports, %d components added." % (EXPECT[5], EXPECT[6], EXPECT[3], EXPECT[4]))
        P("")

    P("## What this proof did not exercise")
    P("")
    P("Codes of the plan that nothing here makes happen (the Go tests of `pkg/osplan` and the service are their proof): %s. Problems that a clean image cannot make: removal_protected, removal_essential, disk_short, docker_held. "
      "The token in the query (`?token=`) of the POST is not tried: the guest has no dashboard token, and the loopback secret of the services is what asks. The first plan and the dirty matrix ran on this one box: "
      "a real box has things the image did not have." % ("not_root, hop_not_planned, identity_unsupported, plan_unparsed, sandbox_solver_failed, timeout, canceled, internal"))
    P("")


    def owed_verdicts():
        """the verdicts that a run owes when nothing makes it invalid: every one of these is made, as a PASS, a FAIL or a NOT TESTABLE, whatever the evidence says. A verdict that is
        not made when it is owed is a judgement that went quiet, and a run that says nothing of it is no pass"""
        owed = [V_IDLE, V_RB_IDLE, V_POST, V_POST_TWICE, V_POLL, V_PHASES, V_SANDBOX_SEEN, V_NOSTORE, V_LEAK, V_INV_FILES, V_INV_UNITS, V_INV_PORTS, V_INV_TMP, V_INV_WITNESS, V_STAMP, V_STEP_LEFTOVER]
        owed += [step_name(s) for s in STEPS]
        if P1.done:
            owed += [V_SHAPE, V_TIMES, V_ID_STABLE, V_ID_BACK, V_ID_HOLD, V_RB_BLOCKED]
        if SHAPE_OK:
            owed += [V_VERDICT, V_VERDICT_RULE, V_REMOVALS, V_COUNTS_DIST, V_COUNTS_UP, V_ESTIMATES, V_DISK, V_SANDBOX, V_DOCKER, V_HOOKS, V_SOURCES]
            if PLAN["verdict"] != "blocked":
                owed.append(V_RB_SHAPE)
        if any(v[0] == "PASS" and v[1] == V_RB_SHAPE for v in verdicts):
            owed += [V_RB_SHN, V_RB_TREE]
        return owed


    if not problems:
        silent = [n for n in owed_verdicts() if not any(v[1] == n for v in verdicts)]
        if silent:
            invalid("%d verdicts that the run owes were not made, though nothing else is wrong with the evidence: %s" % (len(silent), few((cell(n, 70) for n in silent), 3)))

    finish()
except SystemExit:
    raise
except Exception as e:   # evidence that the script did not expect (a number of four hundred digits, an answer nested a hundred thousand deep): a run that cannot be read is invalid, and says which exception
    invalid("the report could not be made of this evidence: %s (%s)" % (type(e).__name__, cell(e, 100)))
    finish()
