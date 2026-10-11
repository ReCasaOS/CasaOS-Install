#!/usr/bin/env bash
# What an in-place upgrade of the operating system does to a running ReCasaOS box, measured on a real machine.
#
# Runs as root inside a DISPOSABLE Debian or Ubuntu machine (a QEMU guest in CI), next to install.sh (the published
# installer). It installs Docker and ReCasaOS on the OLD release, starts the witnesses (a container of each restart
# policy, a database that acknowledges each write after a sync, a container that publishes a port), then does ONE hop to
# the next release, the documented way and the way the future button would run it: in a transient unit
# (systemd-run --no-block --collect --unit=casaos-os-upgrade-measure) with DEBIAN_FRONTEND=noninteractive, the apt options
# of the design, and no reboot by the unit: the harness reboots the guest afterwards. Nothing here judges: it writes what it saw
# into $OUT, and os-upgrade-measure-report.py turns that into facts and the go/no-go table, or says the run is invalid.
#
# The sources of every command (the research files of the OS upgrade work, osresearch-debian.md, osresearch-ubuntu.md,
# osresearch-risks.md, and the design os-upgrade-design.md, section 8) are cited where the command is.
#
# One script, three kinds of caller:
#   the guest, as root, one phase per ssh call (a phase that aborts says why in $OUT/aborted and ends with status 0: the
#   later phases then do nothing, and the report says INVALID RUN);
#   systemd, for the two long-lived pieces (`poller`, the sampler of the witnesses, and `hop-unit`, the hop itself);
#   the CI runner, which sources this file and calls the runner_* functions (the guest has to be waited for while it reboots).
#
#   prepare       Docker and ReCasaOS are installed on the old release, the witnesses start
#   inhibit <v>   root's `systemctl reboot` (v=reboot) and `systemctl reboot --force` (v=force) under
#                 `systemd-inhibit --what=shutdown --mode=block`: refused or not (the guest goes down when it is not)
#   reboot <t>    t=pre: the reboot before the hop (a rehearsal of the current boot, as the design's reboot_first asks);
#                 t=refresh: the reboot that the refresh asked for (a new kernel, a reboot-required file);
#                 t=post: the reboot after the hop, which the unit never does. Status 7 when the hop was skipped.
#   refresh       the old release is brought to its last state, as the product's own refresh step will do it before any hop (apt update and a
#                 full upgrade of the CURRENT release, Docker's source paused): its own durations and package lists, and whether it needs a reboot
#   baseline      the state of the box before the hop (units, packages, containers, health, a token of the API)
#   probe         read-only experiments on the old release: Debian, the sandbox plan and --allow-releaseinfo-change-*;
#                 Ubuntu, what `do-release-upgrade -c` says and what Canonical's metadata says
#   hop           starts the unit (an Ubuntu hop Canonical has not opened is skipped and says so)
#   hop-wait      waits for the unit to end (status 0), or for HOP_LIMIT seconds (status 3); safe to call again
#   after         after the reboot: nothing is started by hand; what came back, what did not
#
# Needs: MEASURE_DISPOSABLE=1 and LEG (d11-12, d12-13, u22-24 or u24-26) for prepare; INSTALLER_ARGS (options for install.sh,
# unquoted on purpose: a list); DOCKER_FROM (the Docker the box starts on, d11-12 only); UBUNTU_VARIANT (hold or keep, Ubuntu
# legs); OUT (default ./osm-out). A phase after prepare needs OUT only: prepare keeps the rest in $OUT/config.env.

# ---- the parts that are only text, and can be tested without a machine -----------------------------------

# leg_facts <leg>: "<id> <version it starts on> <version it goes to> <codename it goes to>", or status 1
leg_facts() {
    case "$1" in
    d11-12) echo "debian 11 12 bookworm" ;;
    d12-13) echo "debian 12 13 trixie" ;;
    u22-24) echo "ubuntu 22.04 24.04 noble" ;;
    u24-26) echo "ubuntu 24.04 26.04 resolute" ;;
    *) return 1 ;;
    esac
}

# major_of <apt version>: "5:29.8.2-1~ubuntu.24.04~noble" -> 29
major_of() { sed -E 's/^[0-9]+://; s/\..*//' <<<"$1"; }

# offered <version> <file.txt>: status 0 when `do-release-upgrade -c` offered that release: its status (the .rc beside the file) is 0 AND it names the
# version (osresearch-ubuntu.md, section 2 and step 6: exit 0 alone is not enough, a box on Prompt=normal can be offered a release that is
# not an LTS, 26.10 on a 26.04 box; the exit status of the tool is not a result)
offered() {
    local v="${1//./\\.}"
    [ "$(cat "${2%.txt}.rc" 2>/dev/null)" = 0 ] && grep -Eq "New release '${v}(\.[0-9]+)? LTS' available" "$2"
}

# converged <file>: apt's simulation in <file> has nothing left to do (osresearch-debian.md, step 17)
converged() { grep -q '^0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded\.$' "$1"; }

# jwt_claims <token>: the claims of a JWT as JSON, and nothing else of it (the token never goes into the evidence)
jwt_claims() {
    local p
    p="$(cut -d. -f2 <<<"$1" | tr '_-' '/+')"
    while [ $((${#p} % 4)) -ne 0 ]; do p="${p}="; done
    base64 -d <<<"${p}" 2>/dev/null || true
}

# sha_tree <dir>: one hash of the names and the contents of every file under <dir>
sha_tree() { find "$1" -type f -print0 | sort -z | xargs -0 -r sha256sum | sha256sum | awk '{print $1}'; }

# sha_lists: one hash of the names, sizes and times of apt's package lists (a plan made in a sandbox must leave them as they were)
sha_lists() { find /var/lib/apt/lists -type f -printf '%p %s %T@\n' | sort | sha256sum | awk '{print $1}'; }

# containerd_17: the first (newest, as apt-cache madison lists them) containerd.io of the 1.7 line, nothing when apt has none. Every producer is read to
# its end: under pipefail a consumer that quits at its first match kills its producer with SIGPIPE, which is status 141 (the Debian 11 leg of the Docker
# proof died so)
containerd_17() { apt-cache madison containerd.io | awk '$3 ~ /^1\.7\./ && !found {print $3; found = 1}'; }

# The tool that rewrites apt's sources, written where the guest keeps it. Python, for the two formats (one-line and deb822) and for what a
# line of either says. osresearch-debian.md, step 11 (REWRITE SOURCES): the suite and its -updates and -security, the backports and
# proposed-updates lines dropped, the archive's hosts moved back to the mirror (the archive has no next release), non-free-firmware beside
# non-free. A line that is not Debian's is left alone (Docker's is paused, not rewritten).
write_sources_tool() {
    cat >"$1" <<'PY'
#!/usr/bin/env python3
# rewrite FROM TO FILE: FILE as the hop leaves it (stdout). foreign FAMILY FILE: status 0 when the file has repositories and none is the distribution's.
import re
import sys

SUFFIXES = ("", "-updates", "-security")
DROPPED = ("-backports", "-backports-sloppy", "-proposed-updates")
LINE = re.compile(r"^(\s*)(deb(?:-src)?)((?:\s+\[[^\]]*\])?)(\s+)(\S+)(\s+)(\S+)((?:\s+\S+)*)\s*$")
URI = re.compile(r"^[A-Za-z0-9+.-]+://([^/:\s]+)(?::[0-9]+)?(/\S*)?$")


def distro_uri(uri, family):
    m = URI.match(uri)
    if not m:
        return False
    host, path = m.group(1).lower(), m.group(2) or "/"
    if family == "debian":
        return host.endswith("debian.org") or path == "/debian" or path.startswith(("/debian/", "/debian-security"))
    return host.endswith(("ubuntu.com", "canonical.com")) or path == "/ubuntu" or path.startswith("/ubuntu/")


def move_host(uri):
    return re.sub(r"^(https?://)archive\.debian\.org/", r"\1deb.debian.org/", uri)


def new_suites(tokens, src, dst):
    out = []
    for t in tokens:
        if any(t == src + d for d in DROPPED):
            continue
        for s in SUFFIXES:
            if t == src + s:
                out.append(dst + s)
                break
        else:
            out.append(t)
    return out


def add_firmware(comps):
    if "non-free" in comps and "non-free-firmware" not in comps:
        return comps + ["non-free-firmware"]
    return comps


def rewrite_line(line, src, dst):
    m = LINE.match(line)
    if not m or not distro_uri(m.group(5), "debian"):
        return line
    ind, kind, opts, s1, uri, s2, suite, rest = m.groups()
    suites = new_suites([suite], src, dst)
    if not suites:
        return "# osm-removed: " + line
    comps = add_firmware(rest.split())
    return ind + kind + opts + s1 + move_host(uri) + s2 + suites[0] + ((" " + " ".join(comps)) if comps else "")


def parse_822(block):
    items = []
    for ln in block.split("\n"):
        if ln[:1] in (" ", "\t") and items and items[-1][0]:
            items[-1][1].append(ln)
            items[-1][2] += " " + ln.strip()
            continue
        m = re.match(r"^([A-Za-z0-9-]+):\s*(.*)$", ln)
        items.append([m.group(1) if m else None, [ln], m.group(2) if m else ""])
    return items


def rewrite_822(block, src, dst):
    items = parse_822(block)
    by = {i[0]: i for i in items if i[0]}
    uris = by["URIs"][2].split() if "URIs" in by else []
    if not uris or not all(distro_uri(u, "debian") for u in uris) or "Suites" not in by:
        return block
    suites = new_suites(by["Suites"][2].split(), src, dst)
    if not suites:
        return "\n".join("# osm-removed: " + ln for ln in block.split("\n"))
    by["URIs"][1] = ["URIs: " + " ".join(move_host(u) for u in uris)]
    by["Suites"][1] = ["Suites: " + " ".join(suites)]
    if "Components" in by:
        by["Components"][1] = ["Components: " + " ".join(add_firmware(by["Components"][2].split()))]
    return "\n".join(ln for i in items for ln in i[1])


def rewrite(text, src, dst, deb822):
    if deb822:
        return "\n\n".join(rewrite_822(b, src, dst) for b in re.split(r"\n[ \t]*\n", text.strip("\n"))) + "\n"
    return "\n".join(rewrite_line(ln, src, dst) for ln in text.split("\n"))


def uris_of(text, deb822):
    if deb822:
        return [u for ln in text.split("\n") if ln.startswith("URIs:") for u in ln.split(":", 1)[1].split()]
    return [m.group(5) for m in (LINE.match(ln) for ln in text.split("\n")) if m]


def main(argv):
    if len(argv) == 5 and argv[1] == "rewrite":
        with open(argv[4], encoding="utf-8") as f:
            sys.stdout.write(rewrite(f.read(), argv[2], argv[3], argv[4].endswith(".sources")))
        return 0
    if len(argv) == 4 and argv[1] == "foreign":
        with open(argv[3], encoding="utf-8") as f:
            uris = uris_of(f.read(), argv[3].endswith(".sources"))
        return 0 if uris and all(URI.match(u) for u in uris) and not any(distro_uri(u, argv[2]) for u in uris) else 1
    sys.stderr.write("usage: rewrite FROM TO FILE | foreign debian|ubuntu FILE\n")
    return 2


sys.exit(main(sys.argv))
PY
    chmod 755 "$1"
}

# ---- the machine ------------------------------------------------------------------------------------------

UNIT=casaos-os-upgrade-measure
LIBDIR=/usr/local/lib/osm
# where apt keeps its sources and its configuration (the tests of this script point it at a directory of their own)
APT_DIR="${APT_DIR:-/etc/apt}"
UM_DIR="${UM_DIR:-/etc/update-manager}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/os-upgrade-measure}"
BOOT_ID_FILE="${BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"
DPKG_LOG="${DPKG_LOG:-/var/log/dpkg.log}"
OS_RELEASE="${OS_RELEASE:-/etc/os-release}"
REBOOT_REQUIRED="${REBOOT_REQUIRED:-/var/run/reboot-required}"
APT_LOG_DIR="${APT_LOG_DIR:-/var/log/apt}"
DISTUP_LOG_DIR="${DISTUP_LOG_DIR:-/var/log/dist-upgrade}"
AUTH_DIR=/root/.osm-auth
# what the installer installs (install.sh, CASA_DEPANDS_PACKAGE) and its optional SMB discovery (wsdd: not in Debian 13, osresearch-debian.md section G)
CASA_DEPS=(wget curl smartmontools parted ntfs-3g net-tools udevil samba cifs-utils mergerfs unzip git wsdd)
# the witnesses that start by themselves after a reboot (docker's own rule for always and unless-stopped)
COME_BACK=(p-always p-unless-stopped p-db p-web p-host)

# EPOCHREALTIME and printf's %()T are the shell's own: a clock that does not need `date` to be installed, or to work, while an upgrade replaces it
now() { printf '%s' "${EPOCHREALTIME}"; }
log() { printf '[measure %(%H:%M:%S)T] %s\n' -1 "$*" | tee -a "${OUT}/steps.log"; }
# a line of the timeline: "#", the time, the name (and what goes with it)
marker() { printf '#\t%s\t%s\n' "${EPOCHREALTIME}" "$1" >>"${OUT}/timeline.tsv"; }
retry() { local i; for i in 1 2 3; do "$@" && return 0; sleep 10; done; return 1; }
# a run that cannot go on says why and ends; the report turns that into INVALID RUN
abort() { echo "$*" >>"${OUT}/aborted"; log "ABORTED: $*"; exit 0; }
not_proven() { abort "NOT PROVEN: $*"; }

# rec <name> <command...>: its output to ${OUT}/<name>.txt, its status to ${OUT}/<name>.rc. A status is evidence here, not an error.
rec() {
    local name="$1" rc=0
    shift
    "$@" >"${OUT}/${name}.txt" 2>&1 || rc=$?
    echo "${rc}" >"${OUT}/${name}.rc"
    return 0
}

installed() { # <package>
    local st
    st="$(dpkg-query -W -f='${db:Status-Abbrev}' "$1" 2>/dev/null || true)"
    [[ "${st}" == ?i* ]]
}

wait_docker() { # [seconds]
    local end=$((SECONDS + ${1:-300}))
    while [ "${SECONDS}" -lt "${end}" ]; do
        if timeout -s KILL 5 docker info >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    log "docker did not answer within ${1:-300} s"
    return 1
}

cleanup() {
    local rc=$?
    if [ "${rc}" -ne 0 ] && [ ! -s "${OUT}/aborted" ]; then
        echo "the guest script ended on a failing command (exit ${rc}): ${FAILED_AT:-no command recorded}" >>"${OUT}/aborted"
    fi
    exit "${rc}"
}

# guard_run: from here a command that fails ends the phase (in functions too), and the report is told which one. A tool of the harness that
# fails (jq, curl, apt, docker) is not a result: a run that ended on one is an invalid run, and says so.
guard_run() {
    set -Eeuo pipefail
    trap 'FAILED_AT="line ${LINENO}: ${BASH_COMMAND}"' ERR
    trap cleanup EXIT
}

init_apt() {
    export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_SUSPEND=1
    APT=(env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3)
    # what the notes say to answer, without anyone to answer (osresearch-debian.md, section E): keep the file the owner edited, take the default otherwise
    CONF=(-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
}

write_config() {
    local v
    {
        for v in LEG FAMILY HOP_FROM TARGET_VERSION_ID TARGET_CODENAME INSTALLER_ARGS DOCKER_FROM UBUNTU_VARIANT WORK SELF CASA_PORT; do
            printf '%s=%q\n' "${v}" "${!v:-}"
        done
    } >"${OUT}/config.env"
}

load_config() {
    [ -s "${OUT}/config.env" ] || { echo "no ${OUT}/config.env: run prepare first" >&2; exit 2; }
    set -a
    # shellcheck source=/dev/null
    . "${OUT}/config.env"
    set +a
    SOURCES_PY="${LIBDIR}/sources.py"
    HOP_UNIT_LOG="${OUT}/hop-unit.log"
}

# note_boot: a phase that starts on another boot than the last one ended on says so (the harness reboots the guest, and so may an
# inhibitor that does not refuse root)
note_boot() {
    local cur
    cur="$(cat "${BOOT_ID_FILE}")"
    if [ -s "${OUT}/boot-id" ] && [ "$(cat "${OUT}/boot-id")" != "${cur}" ]; then
        marker "boot_changed $(cat "${OUT}/boot-id") ${cur}"
    fi
    printf '%s\n' "${cur}" >"${OUT}/boot-id"
}

# phase_begin <name>: an earlier phase that aborted ends the run here, quietly
phase_begin() {
    if [ -s "${OUT}/aborted" ]; then
        echo "skipped ${1}: an earlier phase aborted" | tee -a "${OUT}/steps.log"
        exit 0
    fi
    note_boot
    marker "${1}_start"
}

# ---- what is written down ---------------------------------------------------------------------------------

# box_facts: the tools the hop leans on, for the report to show: apt (the sandbox is its own, and 2.2.4 and 2.6.1 are what the legs run), dpkg, systemd,
# libc, bash, python, the kernel, the machine
box_facts() {
    {
        echo "apt $(apt-get --version 2>&1 | sed -n 1p)"
        echo "dpkg $(dpkg --version 2>&1 | sed -n 1p)"
        echo "systemd $(systemctl --version 2>&1 | sed -n 1p)"
        echo "libc6 $(dpkg-query -W -f='${Version}' libc6 2>&1)"
        echo "bash ${BASH_VERSION}"
        echo "python3 $(python3 --version 2>&1)"
        echo "kernel $(uname -r)"
        echo "sh $(readlink -f /bin/sh)"
        echo "tz $(date +%z)"
        echo "cpus $(nproc)"
        echo "memory_mb $(free -m | awk '$1 == "Mem:" {print $2}')"
        echo "cloud-init $(cloud-init --version 2>&1 | sed -n 1p)"
        echo "needrestart $(dpkg-query -W -f='${Version}' needrestart 2>/dev/null || echo none)"
        echo "upgrader $(dpkg-query -W -f='${Version}' ubuntu-release-upgrader-core 2>/dev/null || echo none)"
    } >"${OUT}/box-facts" 2>&1 || true
}

df_dump() { df -P -m / /boot /var >"${OUT}/df-$1.txt" 2>&1 || true; }

# units_dump <tag>: every service unit with its state, its main pid and its InvocationID (which changes each time systemd starts the unit again:
# a unit whose InvocationID differs between two dumps was restarted in between)
units_dump() {
    local names=()
    mapfile -t names < <(systemctl list-units --type=service --all --no-legend --plain 2>/dev/null | awk '{print $1}')
    if [ "${#names[@]}" -gt 0 ]; then
        systemctl show -p Id -p ActiveState -p SubState -p MainPID -p InvocationID "${names[@]}" 2>/dev/null |
            awk -F= 'BEGIN {OFS = "\t"}
                /^$/ { if (id != "") print id, a, s, m, inv; id = a = s = m = inv = ""; next }
                $1 == "Id" { id = substr($0, 4) }
                $1 == "ActiveState" { a = $2 }
                $1 == "SubState" { s = $2 }
                $1 == "MainPID" { m = $2 }
                $1 == "InvocationID" { inv = $2 }
                END { if (id != "") print id, a, s, m, inv }' >"${OUT}/units-$1.tsv" || true
    else
        : >"${OUT}/units-$1.tsv"
    fi
    ss -Hltn 2>/dev/null | awk '{print $4}' | sort >"${OUT}/listen-$1.txt" || true
}

containers_dump() { # <tag>
    # shellcheck disable=SC2046
    timeout -s KILL 20 docker inspect --format '{{.Name}} policy={{.HostConfig.RestartPolicy.Name}} state={{.State.Status}} started={{.State.StartedAt}} restarts={{.RestartCount}}' \
        $(timeout -s KILL 10 docker ps -aq) >"${OUT}/containers-$1.txt" 2>&1 || true
}

docker_dump() { # <tag>
    {
        echo "server $(timeout -s KILL 10 docker version --format '{{.Server.Version}}' 2>&1 || echo unavailable)"
        echo "driver $(timeout -s KILL 10 docker info --format '{{.Driver}}' 2>&1 || echo unavailable)"
        echo "active $(systemctl is-active docker.service 2>&1 || true)"
        echo "packages $(dpkg -l 'docker*' 'containerd*' 2>/dev/null | awk '$1 == "ii" {printf "%s=%s ", $2, $3}')"
    } >"${OUT}/docker-$1.txt" 2>&1 || true
}

# dpkg_dump <tag>: every package dpkg knows, with its version and its state (`ii` is installed, `rc` removed with its configuration kept): the
# report sets two of them against each other, and against what apt said it would do
dpkg_dump() { dpkg-query -W -f='${Package}\t${Version}\t${db:Status-Abbrev}\n' >"${OUT}/dpkg-$1.tsv" || abort "dpkg-query could not list the packages"; }

deps_dump() { # <tag>
    local d
    for d in "${CASA_DEPS[@]}"; do
        printf '%s %s\n' "${d}" "$(dpkg-query -W -f='${db:Status-Abbrev}' "${d}" 2>/dev/null || echo none)"
    done >"${OUT}/deps-$1.txt"
}

# health_dump <tag>: what the notes ask to be clean before and after (osresearch-debian.md, steps 03 and 17): dpkg --audit, apt-get check, the failed units
health_dump() {
    rec "audit-$1" dpkg --audit
    rec "aptcheck-$1" apt-get -o Debug::NoLocking=true check
    rec "failed-$1" systemctl --failed --no-legend --plain
    rec "sysstate-$1" systemctl is-system-running
    rec "holds-$1" apt-mark showhold
}

# kernel_dump <tag>: the kernel that runs, what /boot holds and how big it is, the kernel packages, a reboot being asked for
kernel_dump() {
    {
        echo "uname $(uname -r)"
        find /boot -maxdepth 1 -type f -printf 'boot-file %p %s\n' 2>/dev/null | sort
        echo "boot-total $(du -sb /boot 2>/dev/null | awk '{print $1}')"
        df -P -m /boot 2>/dev/null | awk 'NR == 2 {print "df-boot", $2, $3, $4, $6}'
        echo "images $(dpkg -l 'linux-image-*' 2>/dev/null | awk '$1 == "ii" {printf "%s=%s ", $2, $3}')"
        if [ -e /var/run/reboot-required ]; then echo "reboot-required yes"; else echo "reboot-required no"; fi
    } >"${OUT}/kernel-$1.txt" 2>&1 || true
}

# apt_config_dump <file>: every file apt reads its sources and its configuration from, as text
apt_config_dump() {
    local f
    for f in "${APT_DIR}/sources.list" "${APT_DIR}/sources.list.d"/* "${APT_DIR}/apt.conf.d"/* "${APT_DIR}/preferences" "${APT_DIR}/preferences.d"/*; do
        [ -f "${f}" ] || continue
        printf '==> %s <==\n' "${f}"
        cat "${f}"
    done >"$1" 2>&1 || true
}

# db_dump <tag>: what the database container acknowledged (its log, with the time Docker gives each line) and what its volume holds. The report
# sets them against each other and finds the longest silence between two acknowledged writes. A docker that could not run leaves a file with nothing
# in it, which is not a database that lost its writes: the report reads the exit status before it reads the file.
db_dump() {
    local rc=0
    timeout -s KILL 60 docker logs --timestamps p-db >"${OUT}/db-$1-logs.txt" 2>&1 || rc=$?
    echo "${rc}" >"${OUT}/db-$1-logs.exit"
    rc=0
    timeout -s KILL 60 docker run --rm -v pdb:/data busybox cat /data/log >"${OUT}/db-$1-file.txt" 2>"${OUT}/db-$1-file.err" || rc=$?
    echo "${rc}" >"${OUT}/db-$1-file.exit"
}

# ---- talking to the box, from bash alone ---------------------------------------------------------------

# http_code <port> <path> [header line]: the HTTP status the loopback answers with, or 000. Only bash and /dev/tcp: while a distribution
# upgrade replaces curl and its libraries, a probe made with curl would measure the upgrade of curl.
http_code() {
    local port="$1" path="$2" hdr="${3:-}" line
    line="$(
        exec 2>/dev/null
        exec 3<>"/dev/tcp/127.0.0.1/${port}" || exit 0
        printf 'GET %s HTTP/1.0\r\nHost: 127.0.0.1\r\n' "${path}" >&3
        if [ -n "${hdr}" ]; then printf '%s\r\n' "${hdr}" >&3; fi
        printf '\r\n' >&3
        IFS= read -r -t 3 l <&3 || true
        printf '%s' "${l:-}"
    )" || line=""
    case "${line}" in
    HTTP/*" "[0-9][0-9][0-9]*)
        line="${line#* }"
        echo "${line:0:3}"
        ;;
    *) echo 000 ;;
    esac
}

# tcp_banner <port>: the first line the port says (up to 2 s), nothing when it does not answer
tcp_banner() {
    (
        exec 2>/dev/null
        exec 3<>"/dev/tcp/127.0.0.1/$1" || exit 0
        IFS= read -r -t 2 l <&3 || true
        printf '%s' "${l:-}"
    ) || true
}

tcp_open() { (exec 2>/dev/null; exec 3<>"/dev/tcp/127.0.0.1/$1"); }

internal_header() { # the gateway rewrites the secret at each start: read it again before every call
    local s=""
    IFS= read -r s 2>/dev/null </var/run/casaos/internal.secret || true
    printf 'Authorization: Internal %s' "${s}"
}

jwt_header() { # the line the harness keeps: "Authorization: <token>"
    local h=""
    IFS= read -r h 2>/dev/null <"${AUTH_DIR}/${1:-jwt}" || true
    printf '%s' "${h}"
}

# login <file>: logs in as the harness's own user, keeps the access token as an Authorization header line in ${AUTH_DIR}/<file> and its claims in the evidence.
# The token and the password never go anywhere near $OUT.
login() {
    local pw tok claims
    pw="$(cat "${AUTH_DIR}/pw")"
    curl -fsS -X POST "http://127.0.0.1:${CASA_PORT}/v1/users/login" -H 'content-type: application/json' \
        -d "$(jq -nc --arg u measure --arg p "${pw}" '{username: $u, password: $p}')" >"${AUTH_DIR}/login.json" || return 1
    tok="$(jq -r '.data.token.access_token // empty' "${AUTH_DIR}/login.json")"
    [ -n "${tok}" ] || return 1
    (umask 077; printf 'Authorization: %s\n' "${tok}" >"${AUTH_DIR}/$1")
    claims="$(jwt_claims "${tok}")"
    printf '{"issued":%s,"claims":%s}\n' "$(now)" "${claims:-null}" >"${OUT}/jwt-claims-$1.json"
}

# ---- the poller: about once a second, the probes in parallel -------------------------------------------------

# One line per second: the time, Docker's version (or -), ssh (it says its banner), samba (445 accepts), the gateway, the core, AppManagement with the
# dashboard's token and with the loopback secret, the published port of the container, the container on the host network, the names of the running
# containers. The probes are bash's own (see http_code) except Docker's CLI, and `timeout` around that: what the box answers while it upgrades,
# not what the probe could not run.
poller() {
    local dir t t0 t1 dv names ssh smb gw core am ami web host us delay jh ih
    dir="$(mktemp -d)"
    export LC_ALL=C
    while :; do
        t0="${EPOCHREALTIME}"
        jh="$(jwt_header jwt)"
        ih="$(internal_header)"
        (timeout -s KILL 3 docker version --format '{{.Server.Version}}' >"${dir}/dv" 2>/dev/null || echo - >"${dir}/dv") &
        (timeout -s KILL 3 docker ps --format '{{.Names}}' 2>/dev/null | sort | paste -sd, - >"${dir}/names" || true) &
        (if [[ "$(tcp_banner 22)" == SSH-* ]]; then echo up >"${dir}/ssh"; else echo down >"${dir}/ssh"; fi) &
        (if tcp_open 445; then echo up >"${dir}/smb"; else echo down >"${dir}/smb"; fi) &
        (http_code "${CASA_PORT}" / >"${dir}/gw") &
        (http_code "${CASA_PORT}" /v1/sys/version/current >"${dir}/core") &
        (http_code "${CASA_PORT}" /v2/app_management/categories "${jh}" >"${dir}/am") &
        (http_code "${CASA_PORT}" /v2/app_management/categories "${ih}" >"${dir}/ami") &
        (http_code 18081 / >"${dir}/web") &
        (http_code 18082 / >"${dir}/host") &
        wait
        t="${t0}"
        IFS= read -r dv <"${dir}/dv" || dv=-
        IFS= read -r names <"${dir}/names" || names=
        read -r ssh <"${dir}/ssh" || ssh=down
        read -r smb <"${dir}/smb" || smb=down
        read -r gw <"${dir}/gw" || gw=000
        read -r core <"${dir}/core" || core=000
        read -r am <"${dir}/am" || am=000
        read -r ami <"${dir}/ami" || ami=000
        read -r web <"${dir}/web" || web=000
        read -r host <"${dir}/host" || host=000
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${t}" "${dv:--}" "${ssh}" "${smb}" "${gw}" "${core}" "${am}" "${ami}" "${web}" "${host}" "${names:--}" >>"${OUT}/timeline.tsv"
        t1="${EPOCHREALTIME}"
        us=$(((${t1/./} - ${t0/./})))
        delay=$((1000000 - us))
        if [ "${delay}" -lt 0 ]; then delay=0; fi
        printf -v delay '%d.%06d' $((delay / 1000000)) $((delay % 1000000))
        sleep "${delay}" 2>/dev/null || sleep 1
    done
}

install_poller() {
    cat >/etc/systemd/system/osm-poller.service <<EOF
[Unit]
Description=os-upgrade-measure: samples the box about once a second
After=network.target

[Service]
Type=simple
Environment=OUT=${OUT}
Environment=LC_ALL=C
ExecStart=/bin/bash ${SELF} poller
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now osm-poller.service >/dev/null 2>&1
}

# ---- Docker, ReCasaOS, the witnesses -------------------------------------------------------------------

install_docker() {
    local cur cprev
    retry curl -fsSL https://get.docker.com -o get-docker.sh
    if [ -n "${DOCKER_FROM:-}" ]; then
        # d11-12: the owner's box, Docker 28.0.4 from Docker's own repository, with a 29 on offer. A box on 28.0.4 has the containerd.io of that day,
        # which is on the 1.7 line (the Docker update proof does the same)
        retry sh get-docker.sh --version "${DOCKER_FROM}" >>"${OUT}/get-docker.log" 2>&1
        START_VERSION="${DOCKER_FROM}"
        cur="$(dpkg-query -W -f='${Version}' containerd.io)"
        cprev="$(containerd_17)"
        if [ "$(major_of "${cur}")" -ge 2 ]; then
            [ -n "${cprev}" ] || not_proven "Docker's repository has no containerd.io 1.7 to start from"
            systemctl stop docker.socket docker.service containerd.service
            "${APT[@]}" install -y -q --allow-downgrades "containerd.io=${cprev}"
            systemctl stop docker.socket docker.service containerd.service
            rm -rf /var/lib/docker /var/lib/containerd
            systemctl start docker
        fi
    else
        retry sh get-docker.sh >>"${OUT}/get-docker.log" 2>&1
        wait_docker || abort "the Docker that was installed does not start"
        START_VERSION="$(docker version --format '{{.Server.Version}}')"
    fi
    wait_docker || abort "the Docker this leg starts on does not start"
    [ "$(docker version --format '{{.Server.Version}}')" = "${START_VERSION}" ] || abort "docker is not the version it was put on (${START_VERSION})"
    echo "${START_VERSION}" >"${OUT}/start-version"
    log "starting on docker ${START_VERSION}, $(dpkg-query -W -f='${Package} ${Version}' containerd.io)"
}

install_casaos() {
    local port key pw tok
    # shellcheck disable=SC2086
    bash "${WORK}/install.sh" --no-telemetry ${INSTALLER_ARGS:-} 2>&1 | tee "${OUT}/install.log" >/dev/null
    test "${PIPESTATUS[0]}" -eq 0
    port="$(sed -nE 's/^[[:space:]]*HttpPort[[:space:]]*=[[:space:]]*([0-9]+).*/\1/p' /etc/casaos/gateway.ini | sed -n 1p)"
    CASA_PORT="${port:-80}"
    for _ in $(seq 1 30); do
        if [ "$(http_code "${CASA_PORT}" /)" = 200 ]; then break; fi
        sleep 2
    done
    # a password of this run's own, kept for the logins after the reboot in a directory of root's, never in the evidence
    pw="Aa1-$(head -c12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    (umask 077; printf '%s' "${pw}" >"${AUTH_DIR}/pw")
    key="$(curl -fsS "http://127.0.0.1:${CASA_PORT}/v1/users/status" | jq -r '.data.key')"
    curl -fsS -X POST "http://127.0.0.1:${CASA_PORT}/v1/users/register" -H 'content-type: application/json' \
        -d "$(jq -nc --arg u measure --arg p "${pw}" --arg k "${key}" '{username: $u, password: $p, key: $k}')" >/dev/null
    login jwt || abort "the login returned no access token"
    tok="$(jwt_header jwt)"
    test -n "${tok}"
    log "ReCasaOS is up on port ${CASA_PORT}"
}

start_things() {
    local policy
    retry docker pull -q busybox >/dev/null
    retry docker pull -q nginx:alpine >/dev/null
    for policy in always unless-stopped no on-failure; do
        docker run -d --init --name "p-${policy}" --restart "${policy}" busybox sleep 100000 >/dev/null
    done
    docker volume create pdb >/dev/null
    # A database that writes a record of its own identity, makes it durable (sync), and only then says "ack <id>". After the hop the file must hold
    # every id that was acknowledged. It detects a volume that is lost or rolled back, not a power cut.
    docker run -d --init --name p-db --restart unless-stopped -v pdb:/data busybox sh -c \
        'trap "exit 0" TERM; i=0; while :; do i=$((i+1)); id="$(date +%s)-$i"; echo "$id" >> /data/log; sync; echo "ack $id"; sleep 0.1 & wait $!; done' >/dev/null
    docker run -d --init --name p-web --restart unless-stopped -p 18081:80 nginx:alpine >/dev/null
    docker run -d --init --name p-host --restart unless-stopped --network host busybox sh -c 'echo ok >/tmp/index.html; exec httpd -f -p 18082 -h /tmp' >/dev/null
    sleep 5
}

# install_controls: two packages no repository has, one of them held. What an upgrade does with a package it does not know is then written down for
# a package that is not Docker's: on Ubuntu the tool answers yes to "remove obsolete packages?" (osresearch-ubuntu.md, section 5).
install_controls() {
    local n d
    for n in osm-orphan osm-orphan-held; do
        installed "${n}" && continue
        d="$(mktemp -d)"
        mkdir -p "${d}/DEBIAN"
        printf 'Package: %s\nVersion: 1.0\nArchitecture: all\nMaintainer: os-upgrade-measure <root@localhost>\nDescription: no repository has this package\n' "${n}" >"${d}/DEBIAN/control"
        dpkg-deb --build "${d}" "${d}.deb" >/dev/null
        dpkg -i "${d}.deb" >/dev/null
        rm -rf "${d}" "${d}.deb"
    done
    apt-mark hold osm-orphan-held >/dev/null
}

settle() { # <file>: seconds until the containers that must come back are running, or "never" after 150 s
    local start=${SECONDS} have n ok
    while [ $((SECONDS - start)) -lt 150 ]; do
        have="$(timeout -s KILL 5 docker ps --format '{{.Names}}' 2>/dev/null || true)"
        ok=1
        for n in "${COME_BACK[@]}"; do
            if ! grep -qxF "${n}" <<<"${have}"; then ok=0; fi
        done
        if [ "${ok}" = 1 ]; then
            echo "$((SECONDS - start))" >"${OUT}/$1"
            return 0
        fi
        sleep 2
    done
    echo never >"${OUT}/$1"
}

# ---- prepare --------------------------------------------------------------------------------------------------

# update_old: apt-get update of the old release, as tolerant as the old release needs and no more: a changed Label, Origin or Codename is accepted (apt 2.2.4 and 2.6.1 take a
# changed Suite or Version by themselves, as a notice that -qq does not print: pkgAcqMetaBase::VerifyVendor; osresearch-debian.md, pitfalls, said bullseye flipped to oldoldstable
# and was refused), and a backports line that its mirror no longer serves is commented out. What was done goes to $OUT/prepare-notes.
update_old() {
    local rc=0 log="${OUT}/prepare-update.log" f
    "${APT[@]}" update -qq >"${log}" 2>&1 || rc=$?
    if [ "${rc}" -ne 0 ] && grep -Eq "^E: .*changed its '(Label|Origin|Codename)' value" "${log}"; then
        echo "update: --allow-releaseinfo-change was needed" >>"${OUT}/prepare-notes"
        rc=0
        "${APT[@]}" update -qq --allow-releaseinfo-change >>"${log}" 2>&1 || rc=$?
    fi
    if [ "${rc}" -ne 0 ] && grep -q 'backports' "${log}"; then
        echo "update: the backports lines were commented out (their repository answers 404)" >>"${OUT}/prepare-notes"
        for f in "${APT_DIR}/sources.list" "${APT_DIR}/sources.list.d"/*.list; do
            [ -f "${f}" ] && sed -i -E 's/^([[:space:]]*deb(-src)?[[:space:]].*-backports.*)$/# \1/' "${f}"
        done
        rc=0
        "${APT[@]}" update -qq >>"${log}" 2>&1 || rc=$?
    fi
    [ "${rc}" -eq 0 ] || abort "apt-get update of the old release fails (${log})"
}

phase_prepare() {
    [[ "${MEASURE_DISPOSABLE:-}" == 1 ]] || { echo "This installs Docker, takes the box through an operating system upgrade and reboots it: run it on a disposable machine (MEASURE_DISPOSABLE=1)." >&2; exit 2; }
    [ "$(id -u)" -eq 0 ] || { echo "Run it as root." >&2; exit 2; }
    LEG="${LEG:-}"
    local facts
    facts="$(leg_facts "${LEG}")" || { echo "usage: $0 prepare, with LEG=d11-12|d12-13|u22-24|u24-26" >&2; exit 2; }
    read -r FAMILY HOP_FROM_VERSION TARGET_VERSION_ID TARGET_CODENAME <<<"${facts}"
    UBUNTU_VARIANT="${UBUNTU_VARIANT:-hold}"
    case "${UBUNTU_VARIANT}" in hold | keep) ;; *) echo "UBUNTU_VARIANT is hold or keep" >&2; exit 2 ;; esac
    if [ -e /etc/casaos ] || [ -e /var/lib/casaos ] || command -v docker >/dev/null 2>&1; then
        echo "This machine already runs ReCasaOS or Docker: not a fresh one, not touching it." >&2
        exit 2
    fi
    . "${OS_RELEASE}"
    test "${ID}" = "${FAMILY}"
    test "${VERSION_ID}" = "${HOP_FROM_VERSION}"
    HOP_FROM="${VERSION_CODENAME}"
    OUT="${OUT:-${PWD}/osm-out}"
    mkdir -p "${OUT}" "${AUTH_DIR}" "${LIBDIR}"
    chmod 700 "${AUTH_DIR}"
    : >"${OUT}/timeline.tsv"
    echo "${LEG}" >"${OUT}/leg"
    cp "${OS_RELEASE}" "${OUT}/os-before"
    WORK="${PWD}"
    SELF="$(readlink -f "${BASH_SOURCE[0]}")"
    CASA_PORT=80
    write_config
    guard_run
    # the markers and the poller lean on bash's own clock and on /dev/tcp (the bash of Debian and Ubuntu has both): a bash without them measures nothing, and says so now
    [ -n "${EPOCHREALTIME:-}" ] || abort "this bash has no EPOCHREALTIME (bash 5 is needed)"
    tcp_open 22 || abort "this bash cannot open /dev/tcp (or sshd is not listening on 22): the poller would measure nothing"
    init_apt
    note_boot
    marker prepare_start
    df_dump start
    cloud-init status --wait >/dev/null 2>&1 || true
    # a first boot of Ubuntu is still seeding its snaps for a while (osresearch-ubuntu.md, section 8)
    if command -v snap >/dev/null 2>&1; then timeout 300 snap wait system seed.loaded >/dev/null 2>&1 || true; fi
    # apt's own timers would run in the middle of the hop, and hold the lock
    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
    if [ "${ID}" = debian ] && [ "${VERSION_ID}" = 11 ] &&
        grep -Eq '^[[:space:]]*deb(-src)?[[:space:]].*//(deb|security)\.debian\.org/debian-security' "${APT_DIR}/sources.list" 2>/dev/null; then
        # out of support: the change --use-debian-archive makes (install.sh, Use_Debian_Archive), made first because Docker's own installer runs apt before ReCasaOS's does
        cp -p "${APT_DIR}/sources.list" "${APT_DIR}/sources.list.measure.bak"
        sed -i -E 's#//(deb|security)\.debian\.org/debian-security#//archive.debian.org/debian-security#' "${APT_DIR}/sources.list"
    fi
    update_old
    "${APT[@]}" install -y -q curl jq ca-certificates python3 procps iproute2 >/dev/null
    write_sources_tool "${LIBDIR}/sources.py"
    install_docker
    install_casaos
    write_config
    start_things
    "${APT[@]}" update -qq
    install_poller
    box_facts
    df_dump prepared
    marker prepare_done
    log "prepared: ${LEG}"
}

# ---- the inhibitor ----------------------------------------------------------------------------------------

# phase_inhibit <reboot|force>: does root's reboot get refused under systemd-inhibit --what=shutdown --mode=block (design section 8: the lock reduces the
# risk of an accidental reboot, root can go round it; the design does not count it as a guard). `systemctl reboot` as root is checked against inhibitors only
# by logind, `--force` not at all: what is recorded is what happened. A box that goes down here is the answer (the next phase says it was another boot).
phase_inhibit() {
    local variant="${1:-}" cmd=(systemctl reboot) f rc=0
    case "${variant}" in reboot) ;; force) cmd+=(--force) ;; *) echo "usage: $0 inhibit reboot|force" >&2; exit 2 ;; esac
    phase_begin "inhibit_${variant}"
    f="${OUT}/inhibit-${variant}.txt"
    {
        echo "variant ${variant}"
        echo "command systemd-inhibit --what=shutdown --mode=block -- ${cmd[*]}"
        echo "boot_id $(cat "${BOOT_ID_FILE}")"
        echo "start $(now)"
    } >"${f}"
    sync
    timeout 60 systemd-inhibit --what=shutdown --mode=block --who=os-upgrade-measure --why='refusal probe' -- "${cmd[@]}" >"${OUT}/inhibit-${variant}.out" 2>&1 || rc=$?
    echo "rc ${rc}" >>"${f}"
    # a reboot that was not refused takes the box down within seconds: only a box that is still here gets past the sleep
    sleep 40
    echo "survived yes" >>"${f}"
    marker "inhibit_${variant}_done"
}

# ---- the reboot -------------------------------------------------------------------------------------------

phase_reboot() {
    local tag="${1:-}" method=timer
    case "${tag}" in pre | refresh | post) ;; *) echo "usage: $0 reboot pre|refresh|post" >&2; exit 2 ;; esac
    if [ "${tag}" = post ] && [ -e "${OUT}/hop-skipped" ]; then
        echo "skipped reboot post: the hop was skipped" | tee -a "${OUT}/steps.log"
        exit 7
    fi
    phase_begin "reboot_${tag}"
    {
        echo "boot_id $(cat "${BOOT_ID_FILE}")"
        echo "issued $(now)"
    } >"${OUT}/reboot-${tag}.txt"
    sync
    # the reboot is asked for by a timer, so that this ssh session ends before the box goes down; the unit of the hop is gone by now, and the lock it held with it
    if ! systemd-run --quiet --no-block --on-active=3 --unit="osm-reboot-${tag}" systemctl reboot >/dev/null 2>&1; then
        method=direct
        systemctl reboot --force >/dev/null 2>&1 || true
    fi
    echo "method ${method}" >>"${OUT}/reboot-${tag}.txt"
}

# ---- refresh ---------------------------------------------------------------------------------------------

# refresh_reboot_wanted: "yes <why>" when the refresh leaves the box wanting a reboot (a reboot-required file; a kernel package that is new or has another version
# than before the refresh), "no" otherwise. The Ubuntu launcher refuses to start while libc6, linux-base or a kernel waits for one (osresearch-ubuntu.md, section 3).
refresh_reboot_wanted() {
    local why="" k
    if [ -e "${REBOOT_REQUIRED}" ]; then why="reboot-required"; fi
    k="$(awk -F'\t' 'FNR == NR { if ($1 ~ /^linux-image-/) before[$1] = $2; next } $1 ~ /^linux-image-/ && before[$1] != $2 { n++ } END { print n + 0 }' \
        "${OUT}/dpkg-refresh-before.tsv" "${OUT}/dpkg-refresh-after.tsv")"
    if [ "${k}" -gt 0 ]; then why="${why:+${why}, }kernel (${k} linux-image package(s) new or changed)"; fi
    if [ -n "${why}" ]; then echo "yes ${why}"; else echo "no"; fi
}

# phase_refresh: the old release is brought to its last state, as the product's own "refresh" step will do it before any hop: the release notes of bookworm and
# trixie and the Ubuntu launcher both want the box up to date (the launcher refuses when a package is upgradable and not phased). apt update, then a full upgrade of the
# CURRENT release, with Docker's source paused: the engine is the Docker update's business, and the d11-12 leg starts on a Docker with a newer one on offer. It is its own
# phase, with its own durations and package lists, after the rehearsal reboot and just before the baseline (so that little can become upgradable before the hop starts), and it
# says whether it needs a reboot, which the runner then makes.
phase_refresh() {
    phase_begin refresh
    guard_run
    init_apt
    dpkg_dump refresh-before
    kernel_dump refresh-before
    pause_sources sources-paused-refresh.txt
    "${APT[@]}" update >"${OUT}/refresh-update.log" 2>&1 || abort "apt-get update of the old release fails before its refresh (${OUT}/refresh-update.log)"
    marker refresh_update_done
    "${APT[@]}" -y -q dist-upgrade "${CONF[@]}" -o APT::Get::Always-Include-Phased-Updates=true >"${OUT}/refresh-upgrade.log" 2>&1 ||
        abort "the old release could not be brought to its last state (${OUT}/refresh-upgrade.log)"
    resume_sources
    dpkg_dump refresh-after
    kernel_dump refresh-after
    rec upgradable-refresh sh -c 'LC_ALL=C apt list --upgradable 2>/dev/null'
    refresh_reboot_wanted >"${OUT}/refresh-reboot.txt"
    marker refresh_done
    log "refreshed: $(cat "${OUT}/refresh-reboot.txt")"
}

# ---- baseline --------------------------------------------------------------------------------------------

phase_baseline() {
    local n
    phase_begin baseline
    guard_run
    init_apt
    wait_docker 300 || abort "docker does not answer before the hop"
    settle settle-baseline
    # the reboots before this phase (the inhibitor's, the rehearsal's) left stopped the containers whose policy does not start them again by themselves: the hop's
    # reboot is where the report looks at that, so they are running when it begins, and what was started is written down
    : >"${OUT}/baseline-started"
    for n in p-no p-on-failure; do
        if [ "$(timeout -s KILL 10 docker inspect --format '{{.State.Status}}' "${n}" 2>/dev/null || true)" != running ]; then
            docker start "${n}" >/dev/null && echo "${n}" >>"${OUT}/baseline-started"
        fi
    done
    install_controls
    cp "${OS_RELEASE}" "${OUT}/os-before"
    units_dump before
    containers_dump before
    docker_dump before
    dpkg_dump before
    health_dump before
    kernel_dump before
    deps_dump before
    df_dump before
    apt_config_dump "${OUT}/apt-config-before.txt"
    # a token of the API issued now, for the report to ask whether it is still accepted after the hop (the file stays in root's directory)
    login jwt-before
    http_code "${CASA_PORT}" /v1/sys/utilization "$(jwt_header jwt-before)" >"${OUT}/jwt-before.code"
    http_code "${CASA_PORT}" /v1/sys/utilization "$(internal_header)" >"${OUT}/internal-before.code"
    http_code "${CASA_PORT}" /v1/sys/version/current >"${OUT}/core-before.code"
    http_code "${CASA_PORT}" / >"${OUT}/gateway-before.code"
    http_code "${CASA_PORT}" /v2/app_management/categories "$(jwt_header jwt)" >"${OUT}/am-before.code"
    db_dump before
    marker baseline_done
}

# ---- probe: Debian ---------------------------------------------------------------------------------------

# sb_opts <dir>: apt's options for a sandbox: its own list of sources, its own state and cache, no locking, and the retries of the hop's own apt (apt 2.2.4 retries nothing by default:
# a sandbox that is stricter than the hop would measure a hiccup of the mirror)
sb_opts() {
    SB_OPTS=(-o Dir::Etc::sourcelist="$1/sources.list" -o Dir::Etc::sourceparts="$1/parts" -o Dir::State::lists="$1/lists" -o Dir::Cache="$1/cache" -o Acquire::Retries=3 -o Debug::NoLocking=true)
}

# sandbox <variant> <dir>: a plan made without touching /etc/apt: osresearch-risks.md R10 and design section 5 ("Sandbox"): the target sources in a temporary
# directory, apt's own state and cache in others, no locking, and the real transaction simulated against it (the old apt reading the new release's
# InRelease is the unknown). variant paused: only Debian's sources, as the hop leaves them; kept: Docker's source too, as it stands on the box.
sandbox() {
    local v="$1" sb="$2" f base dest
    mkdir -p "${sb}/lists/partial" "${sb}/cache/archives/partial" "${sb}/parts"
    : >"${sb}/sources.list"
    for f in "${APT_DIR}/sources.list" "${APT_DIR}/sources.list.d"/*; do
        [ -f "${f}" ] || continue
        base="${f##*/}"
        dest="${sb}/parts/${base}"
        if [ "${f}" = "${APT_DIR}/sources.list" ]; then dest="${sb}/sources.list"; fi
        if python3 "${SOURCES_PY}" foreign debian "${f}"; then
            if [ "${v}" = kept ]; then cp "${f}" "${dest}"; fi
        else
            python3 "${SOURCES_PY}" rewrite "${HOP_FROM}" "${TARGET_CODENAME}" "${f}" >"${dest}"
        fi
    done
    for f in "${sb}/sources.list" "${sb}/parts"/*; do
        [ -f "${f}" ] || continue
        printf '==> %s <==\n' "${f#"${sb}"/}"
        cat "${f}"
    done >"${OUT}/sandbox-${v}-sources.txt"
    sb_opts "${sb}"
    rec "sandbox-${v}-update" apt-get "${SB_OPTS[@]}" update --error-on=any
    rec "sandbox-${v}-upgrade" apt-get "${SB_OPTS[@]}" -s upgrade
    rec "sandbox-${v}-dist-upgrade" apt-get "${SB_OPTS[@]}" -s dist-upgrade
    du -sm "${sb}" >"${OUT}/sandbox-${v}-size.txt" 2>&1 || true
}

# allow_matrix <sandbox dir>: how --allow-releaseinfo-change-{suite,version,label,codename,origin} behave on this apt (design section 8). The stored InRelease of the target
# release gets one field changed and a time of 0 (so that apt fetches the file again instead of being told 304: the comparison is made on a fresh one), then
# apt-get update is run without the option, with the option of that field, and, on a second change, with the option for all.
allow_matrix() {
    local sb="$1" rel="" f field i
    sb_opts "${sb}"
    for i in "${sb}/lists/"*"_dists_${TARGET_CODENAME}_InRelease"; do rel="${i}"; done
    if [ ! -f "${rel}" ]; then
        echo "no InRelease of ${TARGET_CODENAME} in the sandbox's lists: the update of the sandbox failed" >"${OUT}/allow-skipped"
        return 0
    fi
    for f in suite version label codename origin; do
        field="${f^}"
        sed -i -E "s/^${field}: .*/${field}: osm-changed/" "${rel}"
        touch -d @0 "${rel}"
        rec "allow-${f}-plain" apt-get "${SB_OPTS[@]}" update --error-on=any
        sed -i -E "s/^${field}: .*/${field}: osm-changed/" "${rel}"
        touch -d @0 "${rel}"
        rec "allow-${f}-field" apt-get "${SB_OPTS[@]}" "--allow-releaseinfo-change-${f}" update --error-on=any
        sed -i -E "s/^${field}: .*/${field}: osm-changed/" "${rel}"
        touch -d @0 "${rel}"
        rec "allow-${f}-all" apt-get "${SB_OPTS[@]}" --allow-releaseinfo-change update --error-on=any
    done
}

probe_debian() {
    local sb_paused sb_kept
    # the notes write `apt upgrade --without-new-pkgs`; apt-get's upgrade never installs a new package, and the unit runs that. Does apt-get take the notes' spelling too?
    rec without-new-pkgs apt-get -o Debug::NoLocking=true --without-new-pkgs -s upgrade
    sha_tree "${APT_DIR}" >"${OUT}/etc-apt-before.sha"
    sha_lists >"${OUT}/lists-before.sha"
    sb_paused="$(mktemp -d /var/tmp/osm-sandbox.XXXXXX)"
    sb_kept="$(mktemp -d /var/tmp/osm-sandbox.XXXXXX)"
    chmod 755 "${sb_paused}" "${sb_kept}"
    sandbox paused "${sb_paused}"
    sandbox kept "${sb_kept}"
    allow_matrix "${sb_paused}"
    sha_tree "${APT_DIR}" >"${OUT}/etc-apt-after-sandbox.sha"
    sha_lists >"${OUT}/lists-after-sandbox.sha"
    rm -rf "${sb_paused}" "${sb_kept}"
}

# ---- probe: Ubuntu ---------------------------------------------------------------------------------------

probe_ubuntu() {
    # what Canonical says is on offer, from this box (osresearch-ubuntu.md, section 2): the exit status AND the version it names
    rec ubuntu-check env LC_ALL=C do-release-upgrade -c
    rec release-upgrades-before cat /etc/update-manager/release-upgrades
    rec meta-release curl -fsS --max-time 60 https://changelogs.ubuntu.com/meta-release-lts
    # the tool's tarball moves (osresearch-ubuntu.md, section 3): its date, and the version of the launcher the box has
    rec upgrader-tarball curl -fsSI --max-time 60 "http://archive.ubuntu.com/ubuntu/dists/${TARGET_CODENAME}-updates/main/dist-upgrader-all/current/${TARGET_CODENAME}.tar.gz"
    rec pro-status pro status --format json
    rec upgradable-before sh -c "LC_ALL=C apt list --upgradable 2>/dev/null"
}

phase_probe() {
    phase_begin probe
    guard_run
    init_apt
    if [ "${FAMILY}" = debian ]; then probe_debian; else probe_ubuntu; fi
    df_dump probed
    marker probe_done
}

# ---- the hop ----------------------------------------------------------------------------------------------

phase_hop() {
    phase_begin hop_phase
    guard_run
    init_apt
    if [ "${FAMILY}" = ubuntu ] && ! offered "${TARGET_VERSION_ID}" "${OUT}/ubuntu-check.txt"; then
        # Canonical has not opened this upgrade (or has suspended it): neutral, and said so (osresearch-ubuntu.md, section 2 and 15)
        echo "not_offered" >"${OUT}/hop-skipped"
        marker hop_skipped
        log "do-release-upgrade -c does not offer ${TARGET_VERSION_ID}: the hop is skipped"
        return 0
    fi
    marker hop_start
    local env_args=(--setenv=OUT="${OUT}" --setenv=DEBIAN_FRONTEND=noninteractive --setenv=APT_LISTCHANGES_FRONTEND=none --setenv=NEEDRESTART_SUSPEND=1
        --setenv=PATH=/usr/sbin:/usr/bin:/sbin:/bin)
    if [ "${FAMILY}" = ubuntu ]; then
        # the environment of the unit of osresearch-ubuntu.md, section 12, without RELEASE_UPGRADER_NO_SCREEN: whether the tool needs it is a question of the run
        env_args+=(--setenv=LC_ALL=C.UTF-8 --setenv=UCF_FORCE_CONFFOLD=1)
    else
        env_args+=(--setenv=LC_ALL=C)
    fi
    : >"${HOP_UNIT_LOG}"
    # the command line of the design (os-upgrade-design.md, section 5): a transient unit that holds a block inhibitor, and never reboots
    systemd-run --quiet --no-block --collect --unit="${UNIT}" --property=Type=exec "${env_args[@]}" -- \
        systemd-inhibit --what=shutdown:sleep --who=casaos-os-upgrade-measure --why='an operating system upgrade is running' --mode=block \
        /bin/bash "${SELF}" hop-unit
    local i
    for i in $(seq 1 60); do
        if grep -q ' START$' "${HOP_UNIT_LOG}" 2>/dev/null; then return 0; fi
        sleep 1
    done
    abort "the unit of the hop did not start within 60 s"
}

# first_time <file> <word>: the time of the first line of a unit log that ends with the word
first_time() { awk -v w="$2" '$2 == w && !found {print $1; found = 1}' "$1"; }

phase_hop_wait() {
    local limit="${HOP_LIMIT:-4200}" start=${SECONDS} state t0 t1
    if [ -z "${HOP_LIMIT:-}" ] && [ "${FAMILY}" = ubuntu ]; then limit=4800; fi
    if [ -s "${OUT}/aborted" ] || [ -e "${OUT}/hop-skipped" ]; then return 0; fi
    if grep -q 'hop_done' "${OUT}/timeline.tsv" 2>/dev/null; then return 0; fi
    while :; do
        state="$(systemctl is-active "${UNIT}.service" 2>/dev/null || true)"
        case "${state}" in
        inactive | failed)
            # a unit that is gone (collected) says inactive too, so the end is confirmed by a second look; and anything else that systemctl says, or nothing at all (it cannot reach the
            # manager while dpkg replaces systemd, dbus or libc6), is not an answer: the wait goes on, to its limit
            sleep 2
            state="$(systemctl is-active "${UNIT}.service" 2>/dev/null || true)"
            case "${state}" in inactive | failed) break ;; esac
            ;;
        esac
        if [ $((SECONDS - start)) -ge "${limit}" ]; then
            marker hop_timeout
            return 3
        fi
        sleep 10
    done
    guard_run
    init_apt
    marker hop_unit_end
    t0="$(first_time "${HOP_UNIT_LOG}" START)"
    t1="$(first_time "${HOP_UNIT_LOG}" DONE)"
    hop_collect "${t0:-0}" "${t1:-${EPOCHREALTIME}}"
    marker hop_done
}

# hop_collect <start> <end>: what the unit left, read after it ended and before the reboot
hop_collect() {
    local t0="${1%.*}" t1="${2%.*}" f d
    units_dump hop
    containers_dump hop
    docker_dump hop
    dpkg_dump hop
    health_dump hop
    kernel_dump hop
    deps_dump hop
    df_dump hop
    apt_config_dump "${OUT}/apt-config-after.txt"
    db_dump hop
    cp "${OS_RELEASE}" "${OUT}/os-hop"
    rec unit-state systemctl show "${UNIT}.service" -p ActiveState -p Result -p ExecMainStatus
    # what systemd said about the units while dpkg ran (which services bounced), and when dpkg did what: the window of libc6 is read from it
    journalctl --no-pager -o short-iso-precise --since "@${t0}" --until "@$((t1 + 5))" 2>/dev/null |
        grep -E ' systemd\[1\]: (Stopping|Stopped|Starting|Started|Reloading|Reloaded|Reexecuting|Restarting) ' >"${OUT}/hop-journal.txt" || true
    d="$(date -d "@${t0}" '+%Y-%m-%d %H:%M:%S')"
    awk -v d="${d}" '($1 " " $2) >= d' "${DPKG_LOG}" >"${OUT}/dpkg-hop.log" 2>/dev/null || true
    cp "${APT_LOG_DIR}/history.log" "${OUT}/apt-history.log" 2>/dev/null || true
    if [ "${FAMILY}" = ubuntu ]; then
        for f in "${DISTUP_LOG_DIR}"/*; do
            case "${f}" in *.tar.gz) continue ;; esac
            [ -f "${f}" ] && cp "${f}" "${OUT}/dist-upgrade-${f##*/}"
        done
        rec release-upgrades-after cat /etc/update-manager/release-upgrades
        rec pro-status-after pro status --format json
        rec sources-dir-after sh -c 'ls -la /etc/apt/sources.list.d/; for f in /etc/apt/sources.list.d/*; do [ -f "$f" ] && { echo "==> $f <=="; cat "$f"; }; done'
    fi
}

# ---- the unit ---------------------------------------------------------------------------------------------

umark() { printf '%s %s\n' "${EPOCHREALTIME}" "$*" >>"${HOP_UNIT_LOG}"; }

# unit_facts <start|end>: the unit's own pid and invocation, and the versions of what its hop replaces (systemd, dbus, libc6): the unit is the same
# process at the end if it survived their upgrade
unit_facts() {
    {
        echo "pid $$"
        echo "invocation $(systemctl show -p InvocationID --value "${UNIT}.service" 2>/dev/null)"
        echo "manager $(systemctl show --property=Version --value 2>/dev/null)"
        echo "systemd $(dpkg-query -W -f='${Version}' systemd 2>/dev/null)"
        echo "dbus $(dpkg-query -W -f='${Version}' dbus 2>/dev/null)"
        echo "libc6 $(dpkg-query -W -f='${Version}' libc6 2>/dev/null)"
        echo "tty $(tty 2>&1 || true)"
    } >"${OUT}/unit-$1.txt" 2>&1 || true
}

# run_step <name> <command...>: its output to step-<name>.log and its status to step-<name>.rc, with the time it started and ended in the unit's log
run_step() {
    local name="$1" rc=0
    shift
    umark "STEP_START ${name}"
    "$@" >"${OUT}/step-${name}.log" 2>&1 || rc=$?
    echo "${rc}" >"${OUT}/step-${name}.rc"
    umark "STEP_END ${name} ${rc}"
    df -P -m / /boot 2>/dev/null | awk -v n="${name}" 'NR > 1 {printf "%s %s %s\n", n, $6, $4}' >>"${OUT}/df-steps.txt" || true
}

# pause_sources [list]: the sources that are not the distribution's are renamed (osresearch-ubuntu.md, section 7a; os-upgrade-design.md, section 5: the apt source
# of Docker is paused during the hop). The names go to $OUT/<list> (sources-paused.txt by default).
pause_sources() {
    local f list="${OUT}/${1:-sources-paused.txt}"
    : >"${list}"
    for f in "${APT_DIR}/sources.list.d"/*.list "${APT_DIR}/sources.list.d"/*.sources; do
        [ -f "${f}" ] || continue
        if python3 "${SOURCES_PY}" foreign "${FAMILY}" "${f}"; then
            mv "${f}" "${f}.casaos-paused"
            echo "${f}" >>"${list}"
        fi
    done
}

# resume_sources: what pause_sources renamed gets its name back (the refresh pauses Docker's source for its own apt, and gives it back for the baseline)
resume_sources() {
    local f
    for f in "${APT_DIR}/sources.list.d"/*.casaos-paused; do
        if [ -f "${f}" ]; then mv "${f}" "${f%.casaos-paused}"; fi
    done
}

hop_unit_debian() {
    local from="${HOP_FROM}" to="${TARGET_CODENAME}" f dest pass
    local apt_hop=(apt-get -y -q -o Dpkg::Use-Pty=0 -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
    # the sources, atomically per file, after a copy of /etc/apt (osresearch-debian.md, step 11)
    umark SRC_START
    mkdir -p "${BACKUP_DIR}"
    cp -a "${APT_DIR}" "${BACKUP_DIR}/apt-orig"
    apt_config_dump "${OUT}/sources-before.txt"
    pause_sources
    for f in "${APT_DIR}/sources.list" "${APT_DIR}/sources.list.d"/*; do
        [ -f "${f}" ] || continue
        case "${f}" in *.casaos-paused) continue ;; esac
        dest="${f}.osm-new"
        python3 "${SOURCES_PY}" rewrite "${from}" "${to}" "${f}" >"${dest}" && mv "${dest}" "${f}"
    done
    apt_config_dump "${OUT}/sources-after.txt"
    umark SRC_END
    # a rewrite that left no line of the target release (a source in a form the tool does not read, a mirror it does not know) is no hop: nothing is run against the old release's names
    if ! grep -rqs -- "${to}" "${APT_DIR}/sources.list" "${APT_DIR}/sources.list.d"; then
        umark "ABORTED no source names ${to} after the rewrite"
        return 1
    fi
    # apt-get update, then the minimal upgrade and the full one, each downloaded first (osresearch-debian.md, steps 12, 14, 16)
    run_step update "${apt_hop[@]}" update --error-on=any
    run_step minimal_dl "${apt_hop[@]}" --download-only upgrade
    run_step minimal "${apt_hop[@]}" upgrade
    run_step full_dl "${apt_hop[@]}" --download-only dist-upgrade
    run_step full "${apt_hop[@]}" dist-upgrade
    if [ "$(cat "${OUT}/step-full.rc")" != 0 ]; then
        # the failure ladder of the notes, once (osresearch-debian.md, step 16): configure what is half done, repair, again
        run_step recover_configure dpkg --configure -a --force-confdef --force-confold
        run_step recover_fix "${apt_hop[@]}" -f install
        run_step recover_full "${apt_hop[@]}" dist-upgrade
    fi
    # converge: the simulation says there is nothing left (osresearch-debian.md, step 17), two catch-up passes at the most
    for pass in 1 2 3; do
        run_step "converge${pass}" "${apt_hop[@]}" -s dist-upgrade
        if converged "${OUT}/step-converge${pass}.log"; then break; fi
        if [ "${pass}" -lt 3 ]; then run_step "catchup${pass}" "${apt_hop[@]}" dist-upgrade; fi
    done
}

hop_unit_ubuntu() {
    local sample_pid n
    local held=(osm-orphan-held)
    umark SRC_START
    mkdir -p "${BACKUP_DIR}"
    cp -a "${APT_DIR}" "${BACKUP_DIR}/apt-orig"
    apt_config_dump "${OUT}/sources-before.txt"
    pause_sources
    apt_config_dump "${OUT}/sources-after-pause.txt"
    umark SRC_END
    run_step update apt-get -y -q -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3 update
    # the launcher exits 1 when any installed package is upgradable and not phased (osresearch-ubuntu.md, section 3): what is, written down before it is asked
    rec upgradable-unit sh -c 'LC_ALL=C apt list --upgradable 2>/dev/null'
    # the installer's dependencies are marked as installed by hand, so that "obsolete" is not their name (osresearch-ubuntu.md, section 12, step 3)
    for n in "${CASA_DEPS[@]}"; do if installed "${n}"; then apt-mark manual "${n}" >/dev/null; fi; done
    if [ "${UBUNTU_VARIANT}" = hold ]; then
        for n in docker-ce docker-ce-cli containerd.io; do if installed "${n}"; then held+=("${n}"); fi; done
    fi
    if [ "${#held[@]}" -gt 0 ]; then apt-mark hold "${held[@]}" >/dev/null; fi
    printf 'variant %s\nheld %s\n' "${UBUNTU_VARIANT}" "${held[*]}" >"${OUT}/controls.txt"
    # the drop-in that keeps the tool from answering "remove" (osresearch-ubuntu.md, section 5, Overrides): is it read?
    if [ "${UBUNTU_VARIANT}" = keep ]; then
        mkdir -p "${UM_DIR}/release-upgrades.d"
        printf '[Distro]\nRemoveObsoletes=no\n' >"${UM_DIR}/release-upgrades.d/10-os-upgrade-measure.cfg"
    fi
    # Prompt=lts: the tool's own default, written down (osresearch-ubuntu.md, section 2)
    if [ -f "${UM_DIR}/release-upgrades" ]; then
        sed -i -E 's/^Prompt=.*/Prompt=lts/' "${UM_DIR}/release-upgrades"
    else
        mkdir -p "${UM_DIR}"
        printf '[DEFAULT]\nPrompt=lts\n' >"${UM_DIR}/release-upgrades"
    fi
    # dpkg's questions about a file the owner edited: keep it, otherwise the default (osresearch-ubuntu.md, section 5, Config files: the tool does it by itself, slowly)
    printf 'Dpkg::Options {\n"--force-confdef";\n"--force-confold";\n};\n' >"${APT_DIR}/apt.conf.d/99os-upgrade-measure"
    rec ubuntu-check-unit env LC_ALL=C do-release-upgrade -c
    (
        while :; do
            scr=0
            s1022=0
            if pgrep -x screen >/dev/null 2>&1; then scr=1; fi
            if [ -e /var/run/release-upgrader-sshd.pid ]; then s1022=1; fi
            if [ "$(ss -Hltn 2>/dev/null | awk '$4 ~ /:1022$/ {f = 1} END {print f + 0}')" = 1 ]; then s1022=1; fi
            printf '%s screen=%s sshd1022=%s\n' "${EPOCHREALTIME}" "${scr}" "${s1022}" >>"${OUT}/hop-sample.log"
            sleep 20
        done
    ) &
    sample_pid=$!
    # the tool, as the design says to run it: the non-interactive view, no -d, no -p, no --allow-third-party (osresearch-ubuntu.md, section 12, step 7)
    run_step upgrader do-release-upgrade -f DistUpgradeViewNonInteractive
    umark "UPGRADER_EXIT $(cat "${OUT}/step-upgrader.rc")"
    kill "${sample_pid}" 2>/dev/null || true
    rm -f "${APT_DIR}/apt.conf.d/99os-upgrade-measure"
    rm -f "${UM_DIR}/release-upgrades.d/10-os-upgrade-measure.cfg"
}

hop_unit() {
    set -uo pipefail
    umark START
    unit_facts start
    local rc=0
    if [ "${FAMILY}" = debian ]; then hop_unit_debian || rc=$?; else hop_unit_ubuntu || rc=$?; fi
    unit_facts end
    if [ "${rc}" -eq 0 ]; then umark DONE; else umark "ENDED ${rc}"; fi
}

# ---- after the reboot -----------------------------------------------------------------------------------------

# phase_after: nothing is started by hand before the first look. The core, the gateway and AppManagement are Restart=always units that systemd starts at boot; if they
# are not up, or come up with another InvocationID after this phase touched them, the report says so. This phase never starts, restarts or enables a unit of ReCasaOS.
phase_after() {
    if [ -e "${OUT}/hop-skipped" ]; then
        phase_begin after
        marker after_skipped
        return 0
    fi
    phase_begin after
    guard_run
    init_apt
    units_dump boot
    containers_dump boot
    rec uptime-since uptime -s
    rec analyze systemd-analyze time
    rec sysstate-wait timeout 600 systemctl is-system-running --wait
    cp "${OS_RELEASE}" "${OUT}/os-after"
    wait_docker 300 || log "docker does not answer within 300 s of the boot"
    settle settle-after
    marker after_settled
    http_code "${CASA_PORT}" / >"${OUT}/gateway-after.code"
    http_code "${CASA_PORT}" /v1/sys/version/current >"${OUT}/core-after.code"
    http_code "${CASA_PORT}" /v1/sys/utilization "$(internal_header)" >"${OUT}/internal-after.code"
    http_code "${CASA_PORT}" /v2/app_management/categories "$(jwt_header jwt)" >"${OUT}/am-after.code"
    # the token of the baseline, and a new one: is a session that was open before the hop still a session?
    http_code "${CASA_PORT}" /v1/sys/utilization "$(jwt_header jwt-before)" >"${OUT}/jwt-after.code"
    if login jwt-after; then
        http_code "${CASA_PORT}" /v1/sys/utilization "$(jwt_header jwt-after)" >"${OUT}/jwt-fresh.code"
    else
        echo 000 >"${OUT}/jwt-fresh.code"
    fi
    cp "${AUTH_DIR}/jwt-after" "${AUTH_DIR}/jwt" 2>/dev/null || true
    units_dump after
    containers_dump after
    docker_dump after
    dpkg_dump after
    health_dump after
    kernel_dump after
    deps_dump after
    df_dump after
    db_dump after
    rec testparm-after testparm -s
    rec apt-update-after "${APT[@]}" update
    rec apt-converge-after apt-get -s dist-upgrade
    if [ "${FAMILY}" = ubuntu ]; then
        rec sources-dir-end sh -c 'ls -la /etc/apt/sources.list.d/; for f in /etc/apt/sources.list.d/*; do [ -f "$f" ] && { echo "==> $f <=="; cat "$f"; }; done'
    fi
    marker after_done
    log "after: done"
}

# ---- the runner's side ----------------------------------------------------------------------------------

# These run on the CI runner (the workflow sources this file): the guest has to be waited for while it reboots, and one ssh call cannot do that.

runner_guest() { # <phase> [argument]: a phase of this script, in the guest, as root; its status is ssh's
    ssh -F ssh_config vm "sudo env OUT=/home/casa/osm-out bash os-upgrade-measure.sh $*"
}

runner_boot_id() { ssh -F ssh_config -o ConnectTimeout=5 vm 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true; }

# runner_wait <boot id it was on>: until the guest answers ssh with another boot id (20 minutes at the most)
runner_wait() {
    local new i
    for i in $(seq 1 120); do
        new="$(runner_boot_id)"
        if [ -n "${new}" ] && [ "${new}" != "$1" ]; then return 0; fi
        sleep 10
    done
    echo "the guest did not come back with another boot within 20 minutes" >&2
    return 1
}

# runner_reboot <pre|post>: the guest asks for its own reboot (status 7: there is nothing to reboot, the hop was skipped), and the runner waits for the next boot
runner_reboot() {
    local old rc=0
    old="$(runner_boot_id)"
    runner_guest reboot "$1" || rc=$?
    case "${rc}" in
    7) echo "the hop was skipped: no reboot"; return 0 ;;
    0 | 255) ;;
    *) return "${rc}" ;;
    esac
    runner_wait "${old}"
}

# runner_refresh: the refresh phase of the old release, and the reboot it asks for when it needs one (a new kernel, a reboot-required file); the phase writes its
# answer in refresh-reboot.txt, and a phase that aborted left none (status 2: the step ends there, and the report says why)
runner_refresh() {
    local rc=0
    runner_guest refresh
    ssh -F ssh_config vm 'grep -q "^yes" /home/casa/osm-out/refresh-reboot.txt' || rc=$?
    case "${rc}" in
    0) runner_reboot refresh ;;
    1) echo "the refresh needs no reboot" ;;
    *) return "${rc}" ;;
    esac
}

# runner_inhibit <reboot|force>: the phase waits 40 s after the attempt. A guest that went down under it is waited for (ssh ends with 255); one that did not is still there.
runner_inhibit() {
    local old rc=0 new
    old="$(runner_boot_id)"
    runner_guest inhibit "$1" || rc=$?
    case "${rc}" in
    0) return 0 ;;
    255) runner_wait "${old}" ;;
    *) return "${rc}" ;;
    esac
}

# runner_hop: starts the unit, then waits for it. Each wait is one ssh call that returns when the unit has ended; sshd restarts during the hop (glibc's postinst), a
# call that is cut (255) is made again, and the guest's own limit (status 3) is the end of the wait.
runner_hop() {
    local rc=0 i
    runner_guest hop
    for i in $(seq 1 600); do
        rc=0
        runner_guest hop-wait || rc=$?
        case "${rc}" in
        0) return 0 ;;
        255) sleep 15 ;;
        *) return "${rc}" ;;
        esac
    done
    return 1
}

# ---- the run ----------------------------------------------------------------------------------------------

main() {
    local phase="${1:-}"
    case "${phase}" in
    prepare)
        phase_prepare
        return
        ;;
    inhibit | reboot | refresh | baseline | probe | hop | hop-wait | after | poller | hop-unit) ;;
    *)
        echo "usage: $0 prepare | inhibit reboot|force | reboot pre|refresh|post | refresh | baseline | probe | hop | hop-wait | after" >&2
        exit 2
        ;;
    esac
    OUT="${OUT:-${PWD}/osm-out}"
    # a unit may say another locale (the Ubuntu tool wants UTF-8); the clock the markers read is the shell's, and `.` in both
    export LC_ALL="${LC_ALL:-C}"
    load_config
    case "${phase}" in
    inhibit) phase_inhibit "${2:-}" ;;
    reboot) phase_reboot "${2:-}" ;;
    refresh) phase_refresh ;;
    baseline) phase_baseline ;;
    probe) phase_probe ;;
    hop) phase_hop ;;
    hop-wait) phase_hop_wait ;;
    after) phase_after ;;
    poller) poller ;;
    hop-unit) hop_unit ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then main "$@"; fi
