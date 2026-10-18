#!/bin/sh
#
# Checks what install.sh does about the optional modules, without installing
# anything and without a network: the functions that answer are copied out of
# install.sh as it stands and run in this shell (run it with dash: that is the
# shell of the guests the CI proves) against stand-ins for wget and for the
# core's `casaos module` assistant, a fake root for the catalogue, and the
# installer's own Show replaced by a recorder.
#
#   sh scripts/test-install-modules.sh
#
# INSTALL_SH_UNDER_TEST=<path> checks another copy of install.sh (a mutant, to
# see that each check below can say no). The release workflow runs this before
# anything is built.

set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() { echo "ok: $*"; }

# install.sh as the shell reads it: a Windows checkout has CRLF line endings.
INSTALL_SH="${WORK}/install.sh"
tr -d '\r' <"${INSTALL_SH_UNDER_TEST:-${ROOT}/install.sh}" >"${INSTALL_SH}.tmp"
mv "${INSTALL_SH}.tmp" "${INSTALL_SH}"
bash -n "${INSTALL_SH}" || fail "install.sh does not parse"

# ---- what the installer says is where the pieces are --------------------------

[ "$(grep -c '^readonly CASAOS_MODULE_CATALOGUE_SHA256="__CASAOS_MODULE_CATALOGUE_SHA256__"$' "${INSTALL_SH}")" -eq 1 ] ||
    fail "install.sh does not carry the digest of the catalogue as one placeholder for the release build to fill"
[ "$(grep -c '__CASAOS_MODULE_CATALOGUE_SHA256__' "${INSTALL_SH}")" -eq 1 ] ||
    fail "the placeholder of the catalogue digest is named somewhere else than its assignment"
eval "$(grep -E '^(CASA_MODULE_CATALOGUE_DIR|CASA_BIN)=' "${INSTALL_SH}")"
[ "${CASA_MODULE_CATALOGUE_DIR}" = /usr/share/casaos/modules ] || fail "the catalogue is placed in ${CASA_MODULE_CATALOGUE_DIR}, not in /usr/share/casaos/modules"
[ "${CASA_BIN}" = /usr/bin/casaos ] || fail "the assistant is run as ${CASA_BIN}, not as /usr/bin/casaos"
grep -Fq 'module install "${name}" --yes </dev/null' "${INSTALL_SH}" ||
    fail "the module install is not 'casaos module install <name> --yes' with no input"
pass "the placeholder, the catalogue directory and the assistant are where the install needs them"

# the oldest core that has `module` is written by the build, once; the flag that says
# this run put the core in place starts at 0 and is set once, right after the files of
# the release are copied onto the machine
[ "$(grep -c '^readonly CASAOS_MODULES_MIN_CORE_TAG="__CASAOS_MODULES_MIN_CORE_TAG__"$' "${INSTALL_SH}")" -eq 1 ] ||
    fail "install.sh does not carry the oldest core that has 'module' as one placeholder for the release build to fill"
[ "$(grep -c '__CASAOS_MODULES_MIN_CORE_TAG__' "${INSTALL_SH}")" -eq 1 ] ||
    fail "the placeholder of the oldest core that has 'module' is named somewhere else than its assignment"
[ "$(grep -c '^CORE_INSTALLED_NOW=0$' "${INSTALL_SH}")" -eq 1 ] || fail "CORE_INSTALLED_NOW does not start at 0, once"
[ "$(grep -c 'CORE_INSTALLED_NOW=1' "${INSTALL_SH}")" -eq 1 ] || fail "CORE_INSTALLED_NOW is not set to 1 in exactly one place"
n_copy="$(grep -nF 'cp -rf "${SYSROOT_DIR}"/* / ||' "${INSTALL_SH}" | head -n 1 | cut -d: -f1)"
n_flag="$(grep -n 'CORE_INSTALLED_NOW=1' "${INSTALL_SH}" | head -n 1 | cut -d: -f1)"
[ -n "${n_copy}" ] && [ "${n_flag}" = "$((n_copy + 1))" ] ||
    fail "CORE_INSTALLED_NOW is not set to 1 by the line that follows the copy of the release onto the machine"
pass "the oldest core that has 'module' is one placeholder, and the flag of the core put in place is set where the core is"

# the order of the calls in the install: the catalogue with the rest of the
# install (after rclone, before the services are started), the options checked
# before the first thing that changes the machine, the modules after the
# services run and before the banner
line_of() { grep -n "$1" "${INSTALL_SH}" | head -n 1 | cut -d: -f1; }
n_rclone="$(line_of '^    Install_Rclone$')"
n_catalogue="$(line_of '^    Install_Module_Catalogue$')"
n_starting="$(line_of 'Show 2 "Starting ${SERVICE}..."')"
[ -n "${n_rclone}" ] && [ -n "${n_catalogue}" ] && [ -n "${n_starting}" ] && [ "${n_rclone}" -lt "${n_catalogue}" ] && [ "${n_catalogue}" -lt "${n_starting}" ] ||
    fail "Install_Module_Catalogue is not called once, between Install_Rclone and the start of the services"
[ "$(grep -c '^ *Install_Module_Catalogue$' "${INSTALL_SH}")" -eq 1 ] || fail "Install_Module_Catalogue is not called exactly once"
[ "$(grep -c '^Install_Modules$' "${INSTALL_SH}")" -eq 1 ] || fail "Install_Modules is not called exactly once"
[ "$(grep -c '^Validate_Module_Request$' "${INSTALL_SH}")" -eq 1 ] || fail "Validate_Module_Request is not called exactly once"
n_done="$(grep -n '^done$' "${INSTALL_SH}" | head -n 1 | cut -d: -f1)"
n_validate="$(line_of '^Validate_Module_Request$')"
n_detach="$(line_of '^Detach_From_CasaOS_Service$')"
[ -n "${n_done}" ] && [ "${n_validate}" = "$((n_done + 1))" ] && [ "${n_validate}" -lt "${n_detach}" ] ||
    fail "Validate_Module_Request does not follow the options at once, before anything is changed"
n_completed="$(line_of '^INSTALL_COMPLETED=1$')"
n_modules="$(line_of '^Install_Modules$')"
n_banner="$(line_of '^Welcome_Banner$')"
[ -n "${n_completed}" ] && [ -n "${n_modules}" ] && [ -n "${n_banner}" ] && [ "${n_completed}" -lt "${n_modules}" ] && [ "${n_modules}" -lt "${n_banner}" ] ||
    fail "Install_Modules is not called once ReCasaOS is installed and its services run, before the banner"
pass "the calls are where the install needs them"

# ---- the functions, as install.sh has them ------------------------------------

extract() { sed -n "/^$1() {\$/,/^}\$/p" "${INSTALL_SH}"; }

for f in usage Valid_Module_Name Version_Parts Version_At_Least Add_Requested_Module Validate_Module_Request Install_Module_Catalogue Install_One_Module Install_Modules; do
    body="$(extract "${f}")"
    [ -n "${body}" ] || fail "install.sh defines no ${f}()"
    # these run under dash on the CI guests' side of the proof and under bash on a box: no bashism
    if printf '%s\n' "${body}" | grep -nE '\[\[|\(\(|<<<|\$\{[A-Za-z_]+\[|\$\{![A-Za-z]|=~|^ *function ' >/dev/null; then
        fail "${f}() uses a construct that only bash has"
    fi
    eval "${body}"
done

loop="$(sed -n '/^while getopts/,/^done$/p' "${INSTALL_SH}")"
[ -n "${loop}" ] || fail "install.sh has no option loop"
eval "parse_options() {
OPTIND=1
${loop}
Validate_Module_Request
}"

# what those functions take from install.sh's globals and helpers
sudo_cmd=""
CASAOS_INSTALL_RELEASE_TAG=v0.0.0
CASAOS_RELEASE_BASE_URL=https://example.invalid/releases/download/v0.0.0
NO_TELEMETRY=0
DEBIAN_ARCHIVE=0
BUILD_DIR=""
MODULES_REQUESTED=""
MODULE_CATALOGUE_READY=0
CASAOS_MODULES_MIN_CORE_TAG=v0.4.75
CORE_INSTALLED_NOW=1
GreyStart() { :; }
ColorReset() { :; }
Show() {
    echo "[$1] $2"
    if [ "$1" -eq 1 ]; then exit 1; fi
}

# a fake root, a TMPDIR to see what is left behind, and stand-ins first in PATH
FAKE="${WORK}/root"
CALLS="${WORK}/calls"
VERCALLS="${WORK}/vercalls"
SERVED="${WORK}/served"
MODES="${WORK}/modes"
TMPDIR="${WORK}/tmp"
mkdir -p "${WORK}/bin" "${SERVED}" "${MODES}" "${TMPDIR}"
CASA_MODULE_CATALOGUE_DIR="${FAKE}/usr/share/casaos/modules"
CASA_BIN="${WORK}/bin/casaos"
export CALLS VERCALLS WORK SERVED MODES TMPDIR

cat >"${WORK}/bin/wget" <<'STUB'
#!/bin/sh
# stand-in for wget: records the call and serves a file of $SERVED by its name
echo "wget $*" >>"${CALLS}"
out=""
url=""
while [ $# -gt 0 ]; do
    case "$1" in
    -O) out="$2"; shift ;;
    -t) shift ;;
    -*) ;;
    *) url="$1" ;;
    esac
    shift
done
[ "${WGET_SAYS:-}" != fail ] || exit 4
[ -f "${SERVED}/${url##*/}" ] || exit 8
cp "${SERVED}/${url##*/}" "${out}"
STUB
cat >"${WORK}/bin/casaos" <<'STUB'
#!/bin/sh
# stand-in for the core. `-v` is asked of every core: it says what $WORK/version.out holds
# (v0.4.75 when there is no such file) and ends with the status in $WORK/version.status.
# `module` is the module assistant: the call is recorded, with what it was given on its
# input, and it says what the mode of that module (a file of $MODES) says. A core older
# than the assistant would take `module` for the start of a server; it is recorded all the same.
if [ "${1:-}" = "-v" ]; then
    echo "casaos -v" >>"${VERCALLS}"
    cat >"${WORK}/v-stdin-got"
    if [ -f "${WORK}/version.out" ]; then cat "${WORK}/version.out"; else echo v0.4.75; fi
    exit "$(cat "${WORK}/version.status" 2>/dev/null || echo 0)"
fi
echo "casaos $*" >>"${CALLS}"
[ -s "${VERCALLS}" ] || echo "module given before the version was asked" >>"${CALLS}"
cat >"${WORK}/stdin-got"
name="$3"
mode="$(cat "${MODES}/${name}" 2>/dev/null || echo ok)"
T=2026-10-18T10:00:00Z
step() { echo "CASAOS_MODULE_STEP - ${name} $1 $2 ${T}"; }
all_steps_to() { # <the step to stop before>
    for s in packages download verify unpack units manifest start route; do
        [ "$s" != "$1" ] || return 0
        step "$s" begin
        step "$s" done
    done
}
case "${mode}" in
ok)
    all_steps_to none
    step done done
    echo "installed: ${name} v0.1.1"
    echo "CASAOS_MODULE_RESULT - ${name} ok done - - ${T}"
    ;;
failed)
    all_steps_to download
    step download begin
    echo "download: try 1 of 3 failed"
    step download failed
    echo "failed at step download: download_failed"
    echo "rolled back"
    echo "kept_packages: qemu-utils libvirt-clients"
    echo "CASAOS_MODULE_RESULT - ${name} failed download download_failed rolled_back ${T}"
    exit 3
    ;;
incomplete)
    all_steps_to unpack
    step unpack begin
    step unpack failed
    echo "failed at step unpack: unpack_failed"
    echo "rollback incomplete: 1 things could not be undone; casaos module remove ${name} finishes it"
    echo "kept_packages: none"
    echo "CASAOS_MODULE_RESULT - ${name} failed unpack unpack_failed rollback_incomplete ${T}"
    exit 3
    ;;
refused)
    echo "refused: already_installed"
    echo "CASAOS_MODULE_RESULT - ${name} refused - already_installed - ${T}"
    exit 2
    ;;
noresult)
    all_steps_to download
    step download begin
    exit 3
    ;;
okexit1)
    step done done
    echo "CASAOS_MODULE_RESULT - ${name} ok done - - ${T}"
    exit 1
    ;;
silent) exit 0 ;;
forged)
    step download begin
    echo "| CASAOS_MODULE_RESULT ${name} ok done - - ${T}"
    exit 0
    ;;
twice)
    echo "CASAOS_MODULE_RESULT - ${name} failed download download_failed rolled_back ${T}"
    echo "CASAOS_MODULE_RESULT - ${name} ok done - - ${T}"
    exit 0
    ;;
othername)
    echo "CASAOS_MODULE_RESULT - other ok done - - ${T}"
    exit 0
    ;;
escape)
    esc="$(printf '\033')"
    echo "CASAOS_MODULE_RESULT - ${name} failed dl${esc}[31mx cd${esc}[0m_x rolled_back ${T}"
    exit 3
    ;;
esac
STUB
REAL_INSTALL="$(command -v install)"
REAL_MV="$(command -v mv)"
export REAL_INSTALL REAL_MV
cat >"${WORK}/bin/install" <<'STUB'
#!/bin/sh
echo "install $*" >>"${CALLS}"
exec "${REAL_INSTALL}" "$@"
STUB
cat >"${WORK}/bin/mv" <<'STUB'
#!/bin/sh
echo "mv $*" >>"${CALLS}"
[ "${MV_SAYS:-}" != fail ] || exit 1
exec "${REAL_MV}" "$@"
STUB
for tool in sudo systemctl apt-get curl docker; do
    cat >"${WORK}/bin/${tool}" <<'STUB'
#!/bin/sh
echo "FORBIDDEN $0 $*" >>"${CALLS}"
exit 99
STUB
done
chmod +x "${WORK}"/bin/*
PATH="${WORK}/bin:${PATH}"
export PATH

fresh() {
    rm -rf "${FAKE}" "${TMPDIR}" "${MODES}" "${WORK}/stdin-got" "${WORK}/version.out" "${WORK}/version.status" "${WORK}/v-stdin-got"
    mkdir -p "${FAKE}" "${TMPDIR}" "${MODES}"
    : >"${CALLS}"
    : >"${VERCALLS}"
    unset WGET_SAYS
}

calls() { tr '\n' '|' <"${CALLS}"; }

no_calls() { # <what>: nothing was run, nothing was written
    [ ! -s "${CALLS}" ] || fail "$1: something was run ($(calls))"
    [ -z "$(ls -A "${FAKE}")" ] || fail "$1: something was written in the root"
}

# ---- the options -------------------------------------------------------------

EXPECTED_USAGE="$(
    cat <<'TEXT'
Usage: install.sh [options]
Valid options are:
    -p <build_dir>          Specify build directory (Local install)
    --no-telemetry          Turn anonymous statistics off (same as RECASAOS_TELEMETRY=0)
    --use-debian-archive    On Debian 11, when apt answers 404 Not Found for a package, take the
                            security repository from archive.debian.org and go on (same as
                            RECASAOS_DEBIAN_ARCHIVE=1); the first copy of sources.list is kept
    --module <name>         Once ReCasaOS runs, install this optional module (repeat the option
                            for more than one); the module catalogue of the release lists them
    -h                      Show this help message and exit

Anonymous statistics: https://github.com/ReCasaOS/CasaOS-Install#anonymous-statistics
TEXT
)"

CATALOGUE_SHA=0000000000000000000000000000000000000000000000000000000000000000

# parse <args...>: what parse_options leaves, in $out and $status, as the install runs it
parse() {
    status=0
    out="$(
        exec 2>&1
        set -e
        MODULES_REQUESTED="" NO_TELEMETRY=0 DEBIAN_ARCHIVE=0 BUILD_DIR=""
        CASAOS_MODULE_CATALOGUE_SHA256="${CATALOGUE_SHA}"
        parse_options "$@"
        echo "REQUESTED=[${MODULES_REQUESTED}] TELEMETRY=${NO_TELEMETRY} ARCHIVE=${DEBIAN_ARCHIVE} BUILD=[${BUILD_DIR}]"
    )" || status=$?
}

parses() { # <wanted REQUESTED> <args...>
    want="$1"
    shift
    fresh
    parse "$@"
    [ "${status}" -eq 0 ] || fail "'$*': status ${status}: ${out}"
    case "${out}" in
    "REQUESTED=[${want}] "*) ;;
    *) fail "'$*': wanted [${want}], got: ${out}" ;;
    esac
    no_calls "'$*'"
}

parses "" -p /x
parses "virt" --module virt
parses "virt other-1" --module virt --module other-1
parses "virt other-1" --module=virt --module=other-1
parses "virt" --module virt --module=virt --module virt
parses "virt" --module=virt --no-telemetry
parses "a b" --no-telemetry --module a --use-debian-archive --module=b -p /x
case "${out}" in *"TELEMETRY=1 ARCHIVE=1 BUILD=[/x]"*) ;; *) fail "the other options were lost beside --module: ${out}" ;; esac
parses "abcdefghijklmnopqrstuvwxyz012345" --module abcdefghijklmnopqrstuvwxyz012345
parses "a-b-" --module a-b-
parses "" -- --module virt
pass "--module, repeated, in both forms, beside the other options"

refuses() { # <what> <args...>: status 1, the sentence, nothing run, nothing written
    what="$1"
    shift
    fresh
    parse "$@"
    [ "${status}" -eq 1 ] || fail "${what}: status ${status}, wanted 1: ${out}"
    case "${out}" in "[1] --module needs the name of a module"*"Nothing on this machine was changed."*) ;; *) fail "${what}: no sentence, or not as a failure: ${out}" ;; esac
    case "${out}" in *REQUESTED=*) fail "${what}: the install went on: ${out}" ;; esac
    no_calls "${what}"
}

refuses "a capital" --module Virt
refuses "a capital inside" --module vIrt
refuses "all capitals" --module VIRT
refuses "a digit first" --module 1virt
refuses "a hyphen first" --module -virt
refuses "an underscore" --module virt_x
refuses "a dot" --module virt.x
refuses "a slash" --module virt/x
refuses "a space" --module "vi rt"
refuses "a semicolon" --module 'virt;id'
refuses "a command substitution" --module '$(id)'
refuses "a quote" --module "virt'"
refuses "a new line" --module "virt
x"
refuses "an accent" --module "vért"
refuses "a name of 33 characters" --module abcdefghijklmnopqrstuvwxyz0123456
refuses "an empty name" --module ""
refuses "an empty name after =" --module=
refuses "no name at all" --module
refuses "the next option as the name" --module --no-telemetry
refuses "the second of two" --module virt --module Other
case "${out}" in *"(got 'Other')"*) ;; *) fail "the name that was refused is not the one said: ${out}" ;; esac
refuses "a name of escapes" --module "$(printf '\033[31m')"
case "${out}" in *"$(printf '\033')"*) fail "an escape sequence of the argument reached the screen" ;; esac
pass "a name that is not a module name is refused, and nothing was run or written"

fresh
CASAOS_MODULE_CATALOGUE_SHA256=""
status=0
out="$( (
    exec 2>&1
    set -e
    MODULES_REQUESTED=""
    parse_options --module virt
    echo went-on
) )" || status=$?
[ "${status}" -eq 1 ] || fail "--module with no catalogue in the release: status ${status}: ${out}"
case "${out}" in "[1] --module cannot be used with this release: it carries no module catalogue"*"Nothing on this machine was changed."*) ;; *) fail "no catalogue: no sentence, or not as a failure: ${out}" ;; esac
case "${out}" in *went-on*) fail "no catalogue: the install went on" ;; esac
no_calls "no catalogue"
status=0
out="$( (
    exec 2>&1
    set -e
    MODULES_REQUESTED=""
    parse_options --no-telemetry
    echo went-on
) )" || status=$?
[ "${status}" -eq 0 ] && [ "${out}" = went-on ] || fail "no catalogue and no --module is not an install like any other: ${status}: ${out}"
pass "--module is refused when the release has no catalogue, and only then"

# the options and the usage as they were: -h, and the refusal of what is not an option (--help is one of those: it always was), are the old text and the
# old status, with the new option's lines in it
fresh
parse -h
[ "${status}" -eq 0 ] && [ "${out}" = "${EXPECTED_USAGE}" ] || fail "-h: status ${status}, text: ${out}"
for bad in --help --bogus -x --module-x --modules --Module --no-telemetry=1 --use-debian-archive=1 -module -m; do
    parse "${bad}" virt
    [ "${status}" -eq 1 ] && [ "${out}" = "${EXPECTED_USAGE}" ] || fail "'${bad}': status ${status}, text: ${out}"
done
parse --module virt --bogus
[ "${status}" -eq 1 ] && [ "${out}" = "${EXPECTED_USAGE}" ] || fail "an unknown option after --module: status ${status}, text: ${out}"
parse -p
[ "${status}" -eq 1 ] && [ "${out}" = "${EXPECTED_USAGE}" ] || fail "-p with no directory: status ${status}, text: ${out}"
pass "-h and the refusal of an option that is not one are the old text, the old status"

# ---- the versions ------------------------------------------------------------

at_least() { # <what> <yes|no> <version> <minimum>
    if Version_At_Least "$3" "$4" >/dev/null 2>&1; then got=yes; else got=no; fi
    [ "${got}" = "$2" ] || fail "$1: '$3' against '$4': wanted $2, got ${got}"
}

at_least "the minimum itself" yes v0.4.75 v0.4.75
at_least "the next patch" yes v0.4.76 v0.4.75
at_least "the next minor" yes v0.5.0 v0.4.75
at_least "the next major" yes v1.0.0 v0.4.75
at_least "two digits are a number, not a text (minor)" yes v0.10.0 v0.4.75
at_least "three digits are a number, not a text (patch)" yes v0.4.100 v0.4.75
at_least "a minimum of one digit" yes v0.4.75 v0.4.9
at_least "zero against zero" yes v0.0.0 v0.0.0
at_least "major of two digits" yes v10.0.0 v9.9.9
at_least "the patch before" no v0.4.74 v0.4.75
at_least "one digit is a number, not a text" no v0.4.9 v0.4.75
at_least "an older minor, whatever its patch" no v0.3.999 v0.4.75
at_least "an older patch of the same minor" no v0.4.0 v0.4.75
at_least "a core nobody tagged" no v0.0.0 v0.4.75
at_least "an older major of one digit" no v9.9.9 v10.0.0
for bad in "" v v. v0 v0.4 v0.4. v0.4.75. v.4.75 v0..75 0.4.75 V0.4.75 vv0.4.75 v0.4.75-rc1 v0.4.75+x v0.0.0-dev "v0.4.75 " " v0.4.75" \
    "v0.4.75
" "v0.4.75
v0.4.75" "x
v0.4.75" v0.4.075 v00.4.75 v0.4.0x v0.4.1234567 v0.4.75.1 v-1.4.75 "v0.4.7 5" '$(id)' 'v0.4.75;id' 'v0.4.75`id`' v0x4.75 "v0.4.٧٥" "v0.4.75$(printf '\r')"; do
    at_least "a text that is not a version, as the version" no "${bad}" v0.4.75
    at_least "a text that is not a version, as the minimum" no v9.9.9 "${bad}"
done
at_least "the unfilled placeholder as the minimum" no v9.9.9 __CASAOS_MODULES_MIN_CORE_TAG__
pass "a version is three numbers, compared as numbers, and anything else is no version"

# ---- the catalogue -----------------------------------------------------------

CATALOGUE_FILE="${SERVED}/modules-catalogue-v0.0.0.json"
write_catalogue() { printf '{"schema":1,"modules":[],"n":%s}\n' "$1" >"${CATALOGUE_FILE}"; }
sha_of() { sha256sum "$1" | cut -d' ' -f1; }

# catalogue_step: Install_Module_Catalogue as the install runs it (under set -e, and what comes after it runs)
catalogue_step() {
    status=0
    out="$(
        exec 2>&1
        set -e
        MODULE_CATALOGUE_READY=7
        Install_Module_Catalogue
        echo "READY=${MODULE_CATALOGUE_READY}"
        echo "the install goes on"
    )" || status=$?
}

installed_file="${CASA_MODULE_CATALOGUE_DIR}/catalogue.json"

no_leftover() { # <what>
    [ -z "$(ls -A "${TMPDIR}")" ] || fail "$1: a temporary directory was left: $(ls -A "${TMPDIR}")"
    [ ! -e "${installed_file}.new" ] || fail "$1: catalogue.json.new was left"
}

# the digest is right: the file is put in place, whole, 0644, and the install goes on
fresh
write_catalogue 1
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
catalogue_step
[ "${status}" -eq 0 ] || fail "good catalogue: status ${status}: ${out}"
case "${out}" in *"READY=1"*"the install goes on"*) ;; *) fail "good catalogue: ${out}" ;; esac
cmp -s "${CATALOGUE_FILE}" "${installed_file}" || fail "good catalogue: the file in place is not the one served"
[ "$(stat -c %a "${installed_file}")" = 644 ] || fail "good catalogue: mode $(stat -c %a "${installed_file}"), wanted 644"
[ "$(stat -c %a "${CASA_MODULE_CATALOGUE_DIR}")" = 755 ] || fail "good catalogue: the directory is $(stat -c %a "${CASA_MODULE_CATALOGUE_DIR}"), wanted 755"
no_leftover "good catalogue"
grep -Fq "wget -t 3 -q -O " "${CALLS}" && grep -Fq " ${CASAOS_RELEASE_BASE_URL}/modules-catalogue-v0.0.0.json" "${CALLS}" || fail "good catalogue: not downloaded from the release: $(calls)"
[ "$(grep -c '^wget' "${CALLS}")" -eq 1 ] || fail "good catalogue: more than one download"
grep -Fxq "install -d -m 0755 ${CASA_MODULE_CATALOGUE_DIR}" "${CALLS}" || fail "good catalogue: the directory is not made 0755 by install -d: $(calls)"
grep -Eq "^install -m 0644 ${TMPDIR}/[^ ]+/catalogue.json ${installed_file}.new\$" "${CALLS}" || fail "good catalogue: the file is not put down 0644 beside its place: $(calls)"
grep -Fxq "mv -f ${installed_file}.new ${installed_file}" "${CALLS}" || fail "good catalogue: the file is not renamed into place: $(calls)"
pass "the catalogue whose digest is right is put in place"

# every update: a newer catalogue replaces the one there
write_catalogue 2
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
catalogue_step
cmp -s "${CATALOGUE_FILE}" "${installed_file}" && case "${out}" in *"READY=1"*) true ;; *) false ;; esac || fail "an update did not replace the catalogue: ${out}"
no_leftover "update"
pass "an update replaces it"

# a file that is not the one the installer carries the digest of: the catalogue step stops, the earlier file is left alone, the install goes on
tamper() { # <what> <digest the installer carries> <reason said>
    what="$1"
    CASAOS_MODULE_CATALOGUE_SHA256="$2"
    before="$(cat "${installed_file}" 2>/dev/null || echo none)"
    catalogue_step
    [ "${status}" -eq 0 ] || fail "${what}: the install ended (status ${status}): ${out}"
    case "${out}" in *"READY=0"*"the install goes on"*) ;; *) fail "${what}: ${out}" ;; esac
    case "${out}" in *"$3"*"ReCasaOS itself is installed and is not affected"*) ;; *) fail "${what}: not said: ${out}" ;; esac
    [ "$(cat "${installed_file}" 2>/dev/null || echo none)" = "${before}" ] || fail "${what}: the file in place changed"
    no_leftover "${what}"
}
fresh
write_catalogue 3
good="$(sha_of "${CATALOGUE_FILE}")"
printf 'tampered\n' >"${CATALOGUE_FILE}"
tamper "a tampered file, nothing in place before" "${good}" "is not the file this installer expects"
[ ! -e "${installed_file}" ] || fail "a tampered file was put in place"
mkdir -p "${CASA_MODULE_CATALOGUE_DIR}" && printf 'earlier\n' >"${installed_file}"
tamper "a tampered file, an earlier catalogue in place" "${good}" "is not the file this installer expects"
: >"${CATALOGUE_FILE}"
tamper "an empty file" "${good}" "is not the file this installer expects"
rm -f "${CATALOGUE_FILE}"
tamper "a file that is not published" "${good}" "could not be downloaded"
write_catalogue 3
WGET_SAYS=fail
export WGET_SAYS
tamper "a download that fails" "${good}" "could not be downloaded"
unset WGET_SAYS
write_catalogue 3
tamper "a digest of another file" "$(printf 'x' | sha256sum | cut -d' ' -f1)" "is not the file this installer expects"
write_catalogue 3
printf 'earlier\n' >"${installed_file}"
MV_SAYS=fail
export MV_SAYS
tamper "a rename that fails" "${good}" "could not be put in place"
unset MV_SAYS
pass "a tampered or missing catalogue stops the catalogue and nothing else"

# a digest that is not a digest: nothing is downloaded
for bad in abc "$(printf '%064d' 0 | tr 0 A)" "$(printf '%063d' 0)" "$(printf '%065d' 0)" "__CASAOS_MODULE_CATALOGUE_SHA256__" "$(printf '%s' "${good}" | tr a-f A-F)" "${good} " ; do
    fresh
    write_catalogue 3
    CASAOS_MODULE_CATALOGUE_SHA256="${bad}"
    catalogue_step
    [ "${status}" -eq 0 ] || fail "digest '${bad}': the install ended: ${out}"
    case "${out}" in *"is not a SHA-256 digest"*"READY=0"*"the install goes on"*) ;; *) fail "digest '${bad}': ${out}" ;; esac
    no_calls "digest '${bad}'"
done
pass "a digest that is not 64 lower case hexadecimal digits downloads nothing"

# a release with no catalogue: nothing is downloaded, the earlier release's file goes
fresh
CASAOS_MODULE_CATALOGUE_SHA256=""
catalogue_step
[ "${status}" -eq 0 ] && case "${out}" in *"READY=0"*"the install goes on"*) true ;; *) false ;; esac || fail "no catalogue in the release: ${status}: ${out}"
no_calls "a release with no catalogue"
mkdir -p "${CASA_MODULE_CATALOGUE_DIR}" && printf 'earlier\n' >"${installed_file}"
catalogue_step
[ "${status}" -eq 0 ] && [ ! -e "${installed_file}" ] || fail "no catalogue in the release: the file of an earlier release is still there"
case "${out}" in *"the one of an earlier release was removed"*) ;; *) fail "no catalogue: removal not said: ${out}" ;; esac
[ ! -s "${CALLS}" ] || fail "no catalogue: something was run"
pass "a release with no catalogue places none and leaves none of an earlier release"

# a catalogue that cannot be put in place (the directory cannot be made)
fresh
write_catalogue 4
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
mkdir -p "${FAKE}/usr/share/casaos" && printf 'a file where the directory should be\n' >"${FAKE}/usr/share/casaos/modules"
catalogue_step
[ "${status}" -eq 0 ] && case "${out}" in *"could not be put in place"*"READY=0"*"the install goes on"*) true ;; *) false ;; esac || fail "an impossible place: ${status}: ${out}"
no_leftover "an impossible place"
pass "a catalogue that cannot be put in place stops the catalogue and nothing else"

# ---- the modules -------------------------------------------------------------

# modules_step <names> [mode of the first] [mode of the second]: Install_Modules as the install runs it, with
# what is left of a script piped into bash on its input
modules_step() {
    MODULES_REQUESTED="$1"
    [ -z "${2:-}" ] || printf '%s' "$2" >"${MODES}/${1%% *}"
    [ -z "${3:-}" ] || printf '%s' "$3" >"${MODES}/${1##* }"
    status=0
    out="$(
        exec 2>&1
        set -e
        MODULE_CATALOGUE_READY="${READY:-1}"
        CASAOS_MODULE_CATALOGUE_SHA256="${MODSHA-${CATALOGUE_SHA}}"
        printf 'REST OF THE SCRIPT\n' | {
            Install_Modules
            read -r left || left="(nothing left)"
            echo "the install goes on: ${left}"
        }
    )" || status=$?
}
summary() { printf '%s\n' "${out}" | grep '^\[[0-3]\]' || true; }

# a module that installs
fresh
modules_step virt ok
[ "${status}" -eq 0 ] || fail "a good module: status ${status}: ${out}"
[ "$(calls)" = "casaos module install virt --yes|" ] || fail "the assistant is not run as 'casaos module install virt --yes': $(calls)"
[ ! -s "${WORK}/stdin-got" ] || fail "the assistant was given input"
case "${out}" in *"the install goes on: REST OF THE SCRIPT"*) ;; *) fail "the script's own input was consumed: ${out}" ;; esac
for s in "packages begin" "download done" "verify done" "unpack done" "units done" "manifest done" "start done" "route done" "done done"; do
    case "${out}" in *"CASAOS_MODULE_STEP - virt ${s} 2026"*) ;; *) fail "a good module: the line of '${s}' is not shown: ${out}" ;; esac
done
case "${out}" in *"[0] The module virt is installed."*) ;; *) fail "a good module: not said: ${out}" ;; esac
[ -z "$(ls -A "${TMPDIR}")" ] || fail "a good module: a temporary directory was left"
pass "a module is installed with 'casaos module install <name> --yes', its steps shown, the input left alone"

# a module that fails: the step and the reason, the way to clean, and the install is not failed
fresh
modules_step virt failed
[ "${status}" -eq 0 ] || fail "a failing module ended the install (status ${status}): ${out}"
case "${out}" in *"the install goes on: REST OF THE SCRIPT"*) ;; *) fail "a failing module: the install did not go on: ${out}" ;; esac
case "${out}" in *"CASAOS_MODULE_STEP - virt download failed 2026"*"failed at step download: download_failed"*) ;; *) fail "a failing module: the assistant's lines are not shown: ${out}" ;; esac
case "$(summary)" in *"it failed at the step download (download_failed)"*"sudo casaos module remove virt"*) ;; *) fail "a failing module: the step and the reason are not said, nor the way to clean: $(summary)" ;; esac
case "$(summary)" in *"could not all be undone"*) fail "a rolled back install was said not to be" ;; esac
case "${out}" in *"is installed."*) fail "a failing module was said to be installed" ;; esac
[ -z "$(ls -A "${TMPDIR}")" ] || fail "a failing module: a temporary directory was left"
pass "a failing module says its step and its reason and does not fail the install"

fresh
modules_step virt incomplete
case "$(summary)" in *"failed at the step unpack (unpack_failed)"*"could not all be undone"*"sudo casaos module remove virt"*) ;; *) fail "a rollback that is incomplete is not said: $(summary)" ;; esac

fresh
modules_step virt refused
[ "${status}" -eq 0 ] || fail "a refused module ended the install"
case "$(summary)" in *"the assistant refused it (already_installed). Nothing was changed."*) ;; *) fail "a refused module: $(summary)" ;; esac
case "$(summary)" in *"module remove"*) fail "a refused module is told to be removed" ;; esac

# an assistant that ended without a result line is a failure too, whatever it printed
for mode in noresult silent okexit1 forged othername; do
    fresh
    modules_step virt "${mode}"
    [ "${status}" -eq 0 ] || fail "mode ${mode}: the install ended (status ${status})"
    case "${out}" in *"the install goes on"*) ;; *) fail "mode ${mode}: the install did not go on" ;; esac
    case "$(summary)" in *"The module virt was not installed: the assistant did not report a success (exit status"*"sudo casaos module remove virt"*) ;; *) fail "mode ${mode}: $(summary)" ;; esac
    case "${out}" in *"[0] "*) fail "mode ${mode}: said to be installed" ;; esac
done
pass "a refusal, a half rollback and an end with no result line are each said as what they are"

# what the assistant prints reaches the summary only as lower case words
fresh
modules_step virt escape
[ "${status}" -eq 0 ] || fail "escape: the install ended"
case "$(summary)" in *"$(printf '\033')"*) fail "an escape sequence of the assistant's line reached the summary" ;; esac
case "$(summary)" in *"failed at the step dl??31mx (cd??0m_x)"*) ;; *) fail "the step and the code of a result line are not shown as words: $(summary)" ;; esac

# the first result line is the one that counts
fresh
modules_step virt twice
case "$(summary)" in *"failed at the step download (download_failed)"*) ;; *) fail "of two result lines the first did not count: $(summary)" ;; esac
pass "the summary shows only the words of a result line, never the line"

# two modules: the second is tried after the first failed, in the order given
fresh
modules_step "alpha beta" failed ok
[ "${status}" -eq 0 ] || fail "two modules: status ${status}"
[ "$(calls)" = "casaos module install alpha --yes|casaos module install beta --yes|" ] || fail "two modules: not each one in turn: $(calls)"
case "$(summary)" in *"The module alpha was not installed"*"The module beta is installed."*) ;; *) fail "two modules: $(summary)" ;; esac
pass "a module that fails does not stop the next one"

# no catalogue was installed in this run, no assistant, nothing asked
fresh
READY=0
modules_step virt ok
unset READY
[ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] || fail "without a catalogue the assistant was run: $(calls)"
case "$(summary)" in *"No module was installed: the module catalogue could not be installed by this run"*) ;; *) fail "without a catalogue: not said: $(summary)" ;; esac
fresh
CASA_BIN_SAVED="${CASA_BIN}"
CASA_BIN="${WORK}/bin/not-there"
modules_step virt ok
CASA_BIN="${CASA_BIN_SAVED}"
[ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] || fail "without the assistant something was run: $(calls)"
case "$(summary)" in *"not-there is not there"*) ;; *) fail "without the assistant: not said: $(summary)" ;; esac
fresh
modules_step ""
[ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] && [ "${out}" = "the install goes on: REST OF THE SCRIPT" ] || fail "no module asked for and something happened: ${out}"
fresh
READY=0
modules_step ""
unset READY
[ "${status}" -eq 0 ] && [ "${out}" = "the install goes on: REST OF THE SCRIPT" ] || fail "no module asked for, no catalogue, and something is said: ${out}"
pass "no catalogue, no assistant or no module asked for: no assistant is run"

# ---- the core that is given `module` ----------------------------------------

say_version() { # <what `casaos -v` prints, exactly> [its status]
    printf '%s' "$1" >"${WORK}/version.out"
    printf '%s' "${2:-0}" >"${WORK}/version.status"
}

core_runs() { # <what> <what -v prints> [its status]: the assistant is run, once, after the version was asked, once
    fresh
    say_version "$2" "${3:-0}"
    modules_step virt ok
    [ "${status}" -eq 0 ] || fail "$1: status ${status}: ${out}"
    [ "$(calls)" = "casaos module install virt --yes|" ] || fail "$1: the assistant was not run, once: $(calls)"
    [ "$(cat "${VERCALLS}")" = "casaos -v" ] || fail "$1: the version was not asked exactly once"
    [ ! -s "${WORK}/v-stdin-got" ] || fail "$1: -v was given input"
    case "${out}" in *"the install goes on: REST OF THE SCRIPT"*) ;; *) fail "$1: the script's own input was consumed: ${out}" ;; esac
    case "${out}" in *"[0] The module virt is installed."*) ;; *) fail "$1: not installed: ${out}" ;; esac
}

core_refused() { # <what> <what -v prints> [its status]: the core is never given `module`, and the install goes on
    fresh
    say_version "$2" "${3:-0}"
    modules_step virt ok
    [ "${status}" -eq 0 ] || fail "$1: the install ended (status ${status}): ${out}"
    [ ! -s "${CALLS}" ] || fail "$1: the core was given 'module' (an older one would have started as a server): $(calls)"
    [ "$(cat "${VERCALLS}")" = "casaos -v" ] || fail "$1: the version was not asked exactly once"
    [ ! -s "${WORK}/v-stdin-got" ] || fail "$1: -v was given input"
    case "$(summary)" in *"[3] No module was installed: ${CASA_BIN} did not report a version of v0.4.75 or later"*"ReCasaOS itself is installed and is not affected."*) ;; *) fail "$1: not said: $(summary)" ;; esac
    case "$(summary)" in *"$(printf '\033')"*) fail "$1: an escape sequence reached the screen" ;; esac
    [ "$(summary | wc -c)" -lt 500 ] || fail "$1: what the core printed reached the screen at length"
    case "${out}" in *"the install goes on: REST OF THE SCRIPT"*) ;; *) fail "$1: the script's own input was consumed: ${out}" ;; esac
    case "${out}" in *"is installed."*) fail "$1: said to be installed" ;; esac
    [ -z "$(ls -A "${TMPDIR}")" ] || fail "$1: a temporary directory was left"
}

core_runs "the minimum" v0.4.75
core_runs "a later patch" v0.4.76
core_runs "a later minor, of two digits" v0.10.0
core_runs "a later patch, of three digits" v0.4.100
core_runs "a later major" v1.0.0
core_runs "a line end after the version" 'v0.4.75
'
pass "a core that says the minimum or a later version is given 'module', after its version was asked once"

core_refused "an older patch" v0.4.74
core_refused "an older patch of one digit" v0.4.9
core_refused "an older minor" v0.3.999
core_refused "a private build" v0.0.0-dev
core_refused "a tag with a suffix" v0.4.74-dirty
core_refused "a tag above the minimum with a suffix" v0.4.75-rc1
core_refused "a core that says nothing" ''
core_refused "a core that says nothing, with a new line" '
'
core_refused "a word" hello
core_refused "two numbers" v0.4
core_refused "no v" 0.4.75
core_refused "a capital V" V0.4.75
core_refused "a leading zero" v0.4.075
core_refused "four numbers" v0.4.75.1
core_refused "a space before" ' v0.4.75'
core_refused "a space after" 'v0.4.75 '
core_refused "two lines" 'v0.4.75
v0.4.75'
core_refused "a line before the version" 'starting
v0.4.75'
core_refused "a line after the version" 'v0.4.75
starting'
core_refused "a number of seven digits" v0.4.1234567
core_refused "an escape sequence" "$(printf '\033[31mv0.4.74')"
core_refused "a long text" "$(printf 'v0.4.74%0500d' 0)"
core_refused "a version that is right, with a status that is not" v0.4.75 1
core_refused "a version that is right, with a status of 127" v0.4.99 127
core_refused "no version, with a status of 1" '' 1
pass "a core whose version is older, or is not a version, or is not said as one, is never given 'module'"

# a core that cannot even be started is no core
fresh
printf '#!/bin/sh\nexit 127\n' >"${WORK}/bin/casaos-broken"
chmod +x "${WORK}/bin/casaos-broken"
CASA_BIN_SAVED="${CASA_BIN}"
CASA_BIN="${WORK}/bin/casaos-broken"
modules_step virt ok
CASA_BIN="${CASA_BIN_SAVED}"
[ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] || fail "a core that cannot start was given 'module': ${status}: $(calls)"
case "$(summary)" in *"did not report a version of v0.4.75 or later"*"(it reported '')"*) ;; *) fail "a core that cannot start: not said: $(summary)" ;; esac

# the minimum this installer was built with is no version: no core is given `module`
for badmin in __CASAOS_MODULES_MIN_CORE_TAG__ "" v0.4 0.4.75 'v0.4.75-rc1'; do
    fresh
    say_version v9.9.9
    CASAOS_MODULES_MIN_CORE_TAG="${badmin}"
    modules_step virt ok
    CASAOS_MODULES_MIN_CORE_TAG=v0.4.75
    [ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] || fail "a minimum of '${badmin}': the core was given 'module': $(calls)"
    case "$(summary)" in *"No module was installed: ${CASA_BIN} did not report a version of "*) ;; *) fail "a minimum of '${badmin}': not said: $(summary)" ;; esac
done
pass "a core that cannot be started, or an installer that was not built with a minimum, runs no module"

# the core was not put in place by this run: not given `module`, not even asked its version
fresh
say_version v9.9.9
CORE_INSTALLED_NOW=0
modules_step virt ok
CORE_INSTALLED_NOW=1
[ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] && [ ! -s "${VERCALLS}" ] || fail "a core this run did not put in place was asked something: $(calls) / $(cat "${VERCALLS}")"
case "$(summary)" in *"No module was installed: this run did not put the core of this release in place"*"ReCasaOS itself is installed and is not affected."*) ;; *) fail "a core not put in place by this run: not said: $(summary)" ;; esac

# a release built without a catalogue (an empty digest) is not a release built with the check of its core
fresh
say_version v9.9.9
MODSHA=""
modules_step virt ok
unset MODSHA
[ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] && [ ! -s "${VERCALLS}" ] || fail "an installer with no catalogue digest ran a module: $(calls)"
pass "a core that this run did not put in place, or an installer with no catalogue digest, runs no module"

# the whole sequence of the install around the modules, under set -e: a failing module does not change its status
fresh
write_catalogue 5
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
printf 'failed' >"${MODES}/virt"
status=0
out="$(
    exec 2>&1
    set -e
    MODULES_REQUESTED=virt
    Install_Module_Catalogue
    Install_Modules
    echo finished
)" || status=$?
[ "${status}" -eq 0 ] && case "${out}" in *finished) true ;; *) false ;; esac || fail "the install did not finish as it does without --module: ${status}: ${out}"
cmp -s "${CATALOGUE_FILE}" "${installed_file}" || fail "the catalogue was not placed for the module"
pass "catalogue then module, and the install finishes whatever the module did"

echo "all module checks passed"
