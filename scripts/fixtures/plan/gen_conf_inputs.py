#!/usr/bin/env python3
"""Writes the GENERATED files of conf-inputs.json: files of apt's configuration that the grammar of the sandbox is held against (the core's own, in Go, and the report's, in
Python: the harness checks that they say the same of each one, see conf-cases.json).

    python3 scripts/fixtures/plan/gen_conf_inputs.py

The hand-written files of conf-inputs.json (their names do not start with `generated:`) are kept as they are, in their order; the generated ones are made again (the same
every time: nothing here is random but through a fixed seed) and put after them. Then dump conf-cases.json from the core (see README.md). The corpus is every name that the
allow-list has or lacks, set against every method, every setting and a row of hosts and values and shapes of line, and a few hundred files made of pieces of all that put
together by chance: the names that decide are the ones that sit next to the allow-list.
"""
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "conf-inputs.json")
PREFIX = "generated: "

# what the allow-list of the core (sandboxAptSettings) has, and what is next to it. KEPT is what the http and https methods read; the ftp method reads Proxy and Timeout and none of the
# others (its binary has no more), so the core leaves the five others out for ftp: every setting is set against every one of the three methods, and the core says which are kept
METHODS = ["http", "https", "ftp", "file", "cdrom", "copy", "gpgv", "rsh", "ssh", "store", "mirror", "gzip", "xz", "lz4", "zstd", "h", "httpx", "http2", "ftps"]
KEPT = ["Proxy", "Timeout", "Pipeline-Depth", "Dl-Limit", "Max-Age", "No-Cache", "AllowRedirect"]
LEFT = ["Options", "Method", "Cmd", "Command", "Sandbox", "Changelogs", "Auto-Detect", "CompressionTypes", "Comp-Type", "Proxy-Auto-Detect", "ProxyAutoDetect", "Proxy-Auto-Detect-X",
        "ProxyAutoDetectX", "Verify-Peer", "SslCert", "CaInfo", "User-Agent", "Pipeline", "Proxy2", "Retries", "Languages", "PDiffs", "Options::0", "Proxy::", "::Proxy", "Time-out", "Timeout::0"]
TOP_KEPT = ["Retries", "Languages", "PDiffs", "By-Hash", "Check-Valid-Until", "ForceIPv4", "ForceIPv6", "Queue-Mode"]
TOP_LEFT = ["Changelogs", "gpgv", "http", "Proxy", "Timeout", "Max-Age", "CompressionTypes", "Source-Symlinks", "AllowInsecureRepositories", "AllowDowngradeToInsecureRepositories",
            "AllowWeakRepositories", "Acquire", "Force-IPv4", "ForceIPv5", "Retry", "Language", "PDiff", "ByHash", "QueueMode", "Check-Date", "Check-Valid", "Dl-Limit", "Sandbox", "Options", "Method"]
HOSTS = ["deb.debian.org", "a", "A-b_c.d", "a.", ".a", "a.b.", "host-1.example.lan", "1.2.3.4", ".", "..", "...", "a..b", "a...b", ":", "a:b", "a:", ":a", "", "\u00e9", "\u01c6", "K", "\u0130", "*", "a/b", "a b",
         "[::1]", "host.example:3142", "-", "_", "x" * 200]
HOST_METHODS = ["http", "https", "ftp", "gpgv", "file"]
HOST_SETTINGS = ["Proxy", "Timeout", "Pipeline-Depth", "Options", "Proxy-Auto-Detect", "ProxyAutoDetect"]
OTHERS = ["APT::Install-Recommends", "APT::Install-Suggests", "apt::install-recommends", "APT::INSTALL-SUGGESTS", "Apt::Install-Recommends", "APT::Install-Recommends::0", "APT::Install-Suggests::x",
          "APT::Install-Recommend", "APT::Install-Recommendss", "APT::Install-Recommends:", "APT::Install-Recommends.", "APT::Install", "APT::Install-", "APT::Get::Assume-Yes", "APT::Get::Trivial-Only",
          "APT::Update::Pre-Invoke", "APT::Update::Post-Invoke", "APT::Update::Post-Invoke-Success", "APT::Periodic::Update-Package-Lists", "APT::Architecture", "APT::Default-Release", "APT::Sandbox::User",
          "Dir::Etc::main", "Dir::Etc::parts", "Dir::Cache::pkgcache", "Dir::Log::Planner", "Dir::State::lists", "Dir", "Dir::Bin::gpgv", "DPkg::Pre-Invoke", "DPkg::Post-Invoke", "DPkg::Options::",
          "DPkg::Pre-Install-Pkgs", "Debug::NoLocking", "Debug::Acquire::http", "Unattended-Upgrade::Allowed-Origins", "Binary::apt::APT::Get::Assume-Yes", "Acquire", "APT", "acquire", "Acquire:",
          "Acquire:http::Proxy", "Acquire:xhttp::Proxy", "Acquire:xRetries", "Acquire:::Retries", "Acquire::::Retries", "Acquire::Retries::", "xAcquire::Retries", "Acquire.Retries", "Acquire::Retries.", "Acquire::Retries:", "\uff21PT::Install-Recommends",
          "APT::Install-Recommends\u00ad", "Acqu\u0130Re::http::Proxy-Auto-Detect", "Acqu\u0130Re::Retries", "Acquire::http::Proxy-Auto-Detect", "acquire::http::proxyautodetect", "ACQUIRE::HTTP::PROXY-AUTO-DETECT",
          "Acquire::http::Pro\u212axy", "Acquire::\u212bttp::Proxy"]
VALUES = ["", "3", "none", "DIRECT", "http://cache.example.lan:3142", "a b", "a;b", ";", " ", "\t", "x" * 511, "x" * 512, "x" * 513, "~", "\x7f", "\x01", "\u00e9", "\u212a", "\U0001f600", "\x80", "a\\b", "\\", "a\x00b"]
GOOD = 'Acquire::Retries "3";'
BAD = ['#include "/etc/apt/x.conf";', "# comment", "\t# comment", "#clear APT::Update;", 'APT { Install-Recommends "false"; };', "Acquire { http { Proxy \"x\"; }; };", "/* a */", "/* a",
       'Acquire::gpgv::Options::0 "--log-file=/tmp/x";', 'Acquire::http::Proxy-Auto-Detect "/bin/x";', 'Dir::Cache "/tmp";', 'APT::Update::Post-Invoke-Success {"touch /x";};', 'Acquire::Retries "3"; // a',
       'Acquire::Retries "3"; Acquire::Languages "none";', 'Acquire::Retries "3";;', 'Acquire::Retries "3"', 'Acquire::Retries 3;', 'Acquire::Retries"3";', 'Acquire::Retries "3', 'Acquire::Retries "a"b";',
       'Acquire::Retries "3" "4";', "Acquire::Retries", "Acquire::Retries;", '"Acquire::Retries" "3";', "Acquire::Retr/**/ies \"3\";", '{', '}', ';', "x" * 600]
LINE_SHAPES = ["{s}", " {s}", "\t{s}", "{s} ", "{s}\t", "\t {s} \t", "{s}\r", "{s} // c", "{s}//c", "// c\n{s}", "{s}\n// c", "\n\n{s}\n\n", "{s}\n{s}", "{s}\r\n{s}\r\n", "{s}\n\r{s}", "\ufeff{s}", "{s}\x00",
               "{s}\x0b", "{s}\x0c", "{s}\x1f", "{s}\x7f", "{s}\u2028", "{s}\u0085", "{s}\u00a0", "\u00a0{s}", "{s}\xff"]
TOKENS = ["Acquire", "acquire", "ACQUIRE", "APT", "http", "https", "ftp", "gpgv", "Proxy", "Options", "Timeout", "Retries", "Languages", "Install-Recommends", "Install-Suggests", "Dir", "foo", "a.b",
          "..", ":", "-", "_", "0", "Proxy-Auto-Detect", "ProxyAutoDetect", "deb.debian.org", "Max-Age", "Pipeline-Depth", "Method", "Cmd", "", "x"]
NOISE = ["", "   ", "\t", "// a comment", "  // another", "# a directive", "#include \"/x\";", "/* a */", "{", "};", "x"]


def entry(name, text):
    data = text.encode("utf-8", "surrogatepass") if isinstance(text, str) else text
    return {"name": PREFIX + name, "hex": data.hex()}


def statement(name, value="v"):
    return '%s "%s";' % (name, value)


def corpus():
    out = []
    # the methods of the list against every setting, kept or not, the ones that are not on it against three, and a spelling of the case of the kept ones
    for m in METHODS:
        for s in (KEPT + LEFT if m in ("http", "https", "ftp") else ["Proxy", "Timeout", "Options"]):
            out.append(entry("Acquire::%s::%s" % (m, s), statement("Acquire::%s::%s" % (m, s)) + "\n"))
    for m in ("http", "https", "ftp"):
        for s in KEPT:
            for name in ("acquire::%s::%s" % (m, s.lower()), "ACQUIRE::%s::%s" % (m.upper(), s.upper()), "Acquire::%s::%s" % (m.capitalize(), s)):
                out.append(entry("the case: " + name, statement(name) + "\n"))
    # the proxy of one host (and the settings that have none for a host) against every host, and every method and setting against two hosts
    for m, s in (("http", "Proxy"), ("http", "Timeout"), ("gpgv", "Proxy"), ("http", "Proxy-Auto-Detect")):
        for h in HOSTS:
            n = "Acquire::%s::%s::%s" % (m, s, h)
            out.append(entry("host: " + n[:70], statement(n) + "\n"))
    for m in HOST_METHODS:
        for s in HOST_SETTINGS:
            for h in ("deb.debian.org", "a..b"):
                n = "Acquire::%s::%s::%s" % (m, s, h)
                out.append(entry("host: " + n, statement(n) + "\n"))
    # the settings of Acquire:: itself
    for s in TOP_KEPT + TOP_LEFT:
        out.append(entry("Acquire::" + s, statement("Acquire::" + s) + "\n"))
        out.append(entry("Acquire::%s::0" % s, statement("Acquire::%s::0" % s) + "\n"))
    for s in TOP_KEPT:
        for name in ("acquire::" + s.lower(), "ACQUIRE::" + s.upper()):
            out.append(entry("the case: " + name, statement(name) + "\n"))
    for n in OTHERS:
        out.append(entry("name: " + n, statement(n) + "\n"))
    # the values, and the line shapes, around a name that is kept and one that is not
    for v in VALUES:
        for n in ("Acquire::http::Proxy", "Acquire::gpgv::Options"):
            out.append(entry("value %r of %s" % (v[:12], n), statement(n, v) + "\n"))
    for shape in LINE_SHAPES:
        for s in (GOOD, 'Acquire::gpgv::Options::0 "--log-file=/x";'):
            out.append(entry("shape %r of %s" % (shape[:14], s[:20]), shape.replace("{s}", s) + "\n"))
            out.append(entry("shape %r of %s, no end of line" % (shape[:14], s[:20]), shape.replace("{s}", s)))
    # the first line that is not kept gives the reason: each bad line before a good one, after it, and between two
    for b in BAD:
        out.append(entry("alone: " + b[:40], b + "\n"))
        out.append(entry("after a good line: " + b[:40], GOOD + "\n" + b + "\n"))
        out.append(entry("before a good line: " + b[:40], b + "\n" + GOOD + "\n"))
    for a in BAD[:8]:
        for b in BAD[12:20]:
            out.append(entry("two: %s / %s" % (a[:20], b[:20]), a + "\n" + b + "\n"))
    # a line of nothing but a space that is not a space or a tab (a no-break space, a line separator, a next line, an ideographic space, a zero width space): it is a line of text, and not a blank one
    for code in (0xA0, 0x85, 0x2028, 0x2029, 0x3000, 0x200B, 0xFEFF, 0x1680):
        out.append(entry("a line of U+%04X alone" % code, chr(code) + "\n"))
        out.append(entry("a line of U+%04X after a good line" % code, GOOD + "\n" + chr(code) + "\n"))
    # bytes that are not UTF-8 (a lone continuation, a byte that is never one, an overlong form, a surrogate), and a good statement before and after them
    for name, raw in (("a byte that is never UTF-8", bytes.fromhex("ff")), ("a lone continuation byte", bytes.fromhex("80")), ("an overlong slash", bytes.fromhex("c0af")),
                      ("a surrogate", bytes.fromhex("eda080")), ("a cut sequence", bytes.fromhex("e282"))):
        out.append(entry(name + " in a comment after a good line", GOOD.encode() + b"\n// " + raw + b"\n"))
        out.append(entry(name + " in a value", b'Acquire::Retries "' + raw + b'";\n'))
        out.append(entry(name + " alone", raw))
    # pieces put together by chance (the seed is fixed: the same files every time)
    rnd = random.Random(20261013)
    for i in range(300):
        lines = []
        for _ in range(rnd.randint(1, 4)):
            kind = rnd.random()
            if kind < 0.75:
                name = rnd.choice(["::", "::", "::", ":", ".", ""]).join(rnd.choice(TOKENS) for _ in range(rnd.randint(1, 4)))
                value = rnd.choice(VALUES[:14] + ["x" * rnd.randint(0, 40)])
                gap = rnd.choice([" ", " ", " ", "\t", "  ", ""])
                lines.append("%s%s%s\"%s\"%s%s" % (rnd.choice(["", "", "", " ", "\t"]), name, gap, value, rnd.choice([";", ";", ";", "", ";;", " ;", "; "]), rnd.choice(["", "", "", " // c"])))
            else:
                lines.append(rnd.choice(NOISE))
        out.append(entry("chance %d" % i, "\n".join(lines) + rnd.choice(["\n", "\n", ""])))
    # one file once (the same bytes under two names are one), and one name once (the names are cut, so that two files can have the same: the second is numbered)
    done, named, unique = set(), {}, []
    for e in out:
        if e["hex"] in done:
            continue
        done.add(e["hex"])
        named[e["name"]] = named.get(e["name"], 0) + 1
        unique.append(e if named[e["name"]] == 1 else dict(e, name="%s #%d" % (e["name"], named[e["name"]])))
    return unique


def main():
    with open(PATH, encoding="utf-8", newline="") as f:
        kept = [e for e in json.load(f) if not e["name"].startswith(PREFIX)]
    made = corpus()
    if len({e["name"] for e in made}) != len(made):
        raise SystemExit("two generated files have the same name")
    with open(PATH, "w", encoding="utf-8") as f:     # (the line ends of the system: a worktree with CRLF keeps CRLF, git stores LF)
        json.dump(kept + made, f, indent=2, ensure_ascii=True)
        f.write("\n")
    print("%d files of conf-inputs.json: %d written by hand, %d generated" % (len(kept) + len(made), len(kept), len(made)))


if __name__ == "__main__":
    main()
