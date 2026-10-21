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

# (nothing of the CASAOS_MODULE hooks that its caller has exported reaches the checks below)
for hook in $(env | sed -n 's/^\(CASAOS_MODULE[A-Za-z0-9_]*\)=.*/\1/p'); do unset "${hook}"; done

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
grep -Fq 'Run_Module_Assistant "${CASA_BIN}" module "${verb}" "${name}" --yes </dev/null' "${INSTALL_SH}" ||
    fail "a module job is not 'casaos module <verb> <name> --yes' with no input, run through Run_Module_Assistant"
grep -Fxq '    if ! Run_Module_Job install "${name}"; then' "${INSTALL_SH}" || fail "the module install is not a job of the verb install"
grep -Fxq '    if ! Run_Module_Job update "${name}"; then' "${INSTALL_SH}" || fail "the module update is not a job of the verb update"
grep -Fq 'Run_Module_Assistant "${CASA_BIN}" module list </dev/null >' "${INSTALL_SH}" ||
    fail "the list of the modules is not 'casaos module list' with no input, run through Run_Module_Assistant"
# (the assistant is run in two places, both through Run_Module_Assistant: the job, and the list)
[ "$(grep -c '"${CASA_BIN}" module ' "${INSTALL_SH}")" -eq 2 ] && [ "$(grep -c 'Run_Module_Assistant "${CASA_BIN}" module ' "${INSTALL_SH}")" -eq 2 ] ||
    fail "the assistant is run in another place than the job and the list, or not through Run_Module_Assistant"
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
[ "$(grep -c '^Update_Installed_Modules$' "${INSTALL_SH}")" -eq 1 ] && [ "$(line_of '^Update_Installed_Modules$')" = "$(($(line_of '^Install_Modules$') + 1))" ] ||
    fail "Update_Installed_Modules is not called once, on the line after Install_Modules"
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

FUNCTIONS="${WORK}/functions.sh"
: >"${FUNCTIONS}"
for f in usage Valid_Module_Name Version_Parts Version_At_Least Run_Module_Assistant Add_Requested_Module Validate_Module_Request Remove_Module_Catalogue Place_Module_Catalogue Install_Module_Catalogue Run_Module_Job Install_One_Module Install_Modules Update_One_Module Running_Under_Update_Unit Hand_Off_Module_Updates Update_Installed_Modules; do
    body="$(extract "${f}")"
    [ -n "${body}" ] || fail "install.sh defines no ${f}()"
    printf '%s\n' "${body}" >>"${FUNCTIONS}"
    # these run under dash on the CI guests' side of the proof and under bash on a box: no bashism
    if printf '%s\n' "${body}" | grep -nE '\[\[|\(\(|<<<|\$\{[A-Za-z_]+\[|\$\{![A-Za-z]|=~|^ *function ' >/dev/null; then
        fail "${f}() uses a construct that only bash has"
    fi
    eval "${body}"
done
# the functions as the guests' shell reads them (dash is the shell of Debian and Ubuntu's sh)
sh -n "${FUNCTIONS}" || fail "the functions do not parse for sh"
if command -v dash >/dev/null 2>&1; then dash -n "${FUNCTIONS}" || fail "the functions do not parse for dash"; fi
bash --posix -n "${FUNCTIONS}" || fail "the functions do not parse for bash in POSIX mode"
pass "the module functions of install.sh parse for sh, dash and POSIX bash"

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
NO_MODULE_UPDATE=0
# the oldest core that has `casaos module update` is written in install.sh itself, once, and is v0.4.78
[ "$(grep -c '^readonly CASAOS_MODULE_UPDATE_MIN_CORE_TAG="v0.4.78"$' "${INSTALL_SH}")" -eq 1 ] && [ "$(grep -c 'CASAOS_MODULE_UPDATE_MIN_CORE_TAG=' "${INSTALL_SH}")" -eq 1 ] ||
    fail "install.sh does not say once that v0.4.78 is the oldest core that has 'casaos module update'"
CASAOS_MODULE_UPDATE_MIN_CORE_TAG=v0.4.78
CASAOS_UPDATE_LOG=/nonexistent/upgrade.log
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
SUDOCALLS="${WORK}/sudocalls"
UNPRIV="${WORK}/unprivileged"
ENVSEEN="${WORK}/envseen"
SERVED="${WORK}/served"
MODES="${WORK}/modes"
TMPDIR="${WORK}/tmp"
mkdir -p "${WORK}/bin" "${SERVED}" "${MODES}" "${TMPDIR}"
CASA_MODULE_CATALOGUE_DIR="${FAKE}/usr/share/casaos/modules"
CASA_BIN="${WORK}/bin/casaos"
export CALLS VERCALLS SUDOCALLS UNPRIV ENVSEEN WORK SERVED MODES TMPDIR

cat >"${WORK}/bin/wget" <<'STUB'
#!/bin/sh
# stand-in for wget: records the call and serves a file of $SERVED by its name
echo "wget $*" >>"${CALLS}"
[ "${VIA_SUDO:-}" = 1 ] || echo "wget $*" >>"${UNPRIV}"
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
[ "${VIA_SUDO:-}" = 1 ] || echo "casaos $*" >>"${UNPRIV}"
env | grep '^CASAOS_MODULE' >>"${ENVSEEN}" || true
echo "KEEP_ME=${KEEP_ME:-none}" >>"${ENVSEEN}.kept"
[ -s "${VERCALLS}" ] || echo "module given before the version was asked" >>"${CALLS}"
cat >"${WORK}/stdin-got"
# `module list` says what $WORK/list.out holds (a list with no module when there is no such file), with the status in $WORK/list.status
if [ "${2:-}" = list ]; then
    if [ -f "${WORK}/list.out" ]; then cat "${WORK}/list.out"; else echo '{"catalogue":{"ok":true},"modules":[]}'; fi
    echo "a line of the assistant's on its error output" >&2
    exit "$(cat "${WORK}/list.status" 2>/dev/null || echo 0)"
fi
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
uptodate | downgrade | busy)
    code="${mode}"
    [ "${mode}" != uptodate ] || code=up_to_date
    echo "refused: ${code}"
    echo "CASAOS_MODULE_RESULT - ${name} refused - ${code} - ${T}"
    exit 2
    ;;
notdropped)
    step done done
    echo "updated: ${name} v0.2.1 -> v0.2.2 is installed, but the backup folder /var/lib/casaos/modules/tmp/backup-${name} could not be dropped"
    echo "CASAOS_MODULE_RESULT - ${name} failed - backup_not_dropped - ${T}"
    exit 3
    ;;
left)
    all_steps_to swap
    step swap begin
    step swap failed
    echo "failed at step swap: file_exists"
    echo "rolled back, but these files were left as they are, and the old version is not whole: /etc/x"
    echo "CASAOS_MODULE_RESULT - ${name} failed swap file_exists rolled_back_with_left ${T}"
    exit 3
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
status7)
    step download begin
    exit 7
    ;;
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
[ "${VIA_SUDO:-}" = 1 ] || echo "install $*" >>"${UNPRIV}"
case "${INSTALL_SAYS:-}:${1:-}" in fail:* | copy:-m) exit 1 ;; esac
exec "${REAL_INSTALL}" "$@"
STUB
cat >"${WORK}/bin/mv" <<'STUB'
#!/bin/sh
echo "mv $*" >>"${CALLS}"
[ "${VIA_SUDO:-}" = 1 ] || echo "mv $*" >>"${UNPRIV}"
[ "${MV_SAYS:-}" != fail ] || exit 1
# a rename that says it is done, and leaves a directory where the file goes (as if one had been made since it was looked at)
if [ "${MV_SAYS:-}" = lie ]; then rm -f "$2" && mkdir -p "$3" && exit 0; fi
exec "${REAL_MV}" "$@"
STUB
cat >"${WORK}/bin/sudo" <<'STUB'
#!/bin/sh
# stand-in for sudo: forbidden unless the case says SUDO_OK=1; then the command is recorded and run as
# it is, with VIA_SUDO=1 in its environment so that the stand-ins can tell they were run through it
if [ "${SUDO_OK:-}" != 1 ]; then
    echo "FORBIDDEN $0 $*" >>"${CALLS}"
    exit 99
fi
echo "sudo $*" >>"${SUDOCALLS}"
# (SUDO_RM_FAILS=1: an rm through it fails, and removes nothing)
if [ "${1:-}" = rm ] && [ "${SUDO_RM_FAILS:-}" = 1 ]; then exit 1; fi
VIA_SUDO=1
export VIA_SUDO
exec "$@"
STUB
for tool in systemctl apt-get curl docker; do
    cat >"${WORK}/bin/${tool}" <<'STUB'
#!/bin/sh
echo "FORBIDDEN $0 $*" >>"${CALLS}"
exit 99
STUB
done
cat >"${WORK}/bin/systemd-run" <<'STUB'
#!/bin/sh
# stand-in for systemd-run: records the call, and each argument in a file of its own: $WORK/sdrun/<n>.raw as it was given, and $WORK/sdrun/<n>
# as systemd makes it the word of the unit's command ("${NAME}" and a word that is "$NAME" are replaced by the variable, of which the unit has none
# here, "$$" is "$", "%%" is "%" and "%<letter>" a specifier); $WORK/sdrun/changed is there when a word was not left as it was. Ends with $SDRUN_STATUS
echo "systemd-run $*" >>"${CALLS}"
rm -rf "${WORK}/sdrun"
mkdir -p "${WORK}/sdrun"
n=0
for a in "$@"; do
    n=$((n + 1))
    printf '%s' "${a}" >"${WORK}/sdrun/${n}.raw"
    printf '%s' "${a}" | sed -e 's/^\$[A-Za-z_][A-Za-z0-9_]*$//' -e 's/\${[^}]*}//g' -e 's/\$\$/$/g' -e 's/%%/%/g' -e 's/%[A-Za-z]/<specifier>/g' >"${WORK}/sdrun/${n}"
    cmp -s "${WORK}/sdrun/${n}" "${WORK}/sdrun/${n}.raw" || echo "${n}" >>"${WORK}/sdrun/changed"
done
exit "${SDRUN_STATUS:-0}"
STUB
chmod +x "${WORK}"/bin/*
PATH="${WORK}/bin:${PATH}"
export PATH

fresh() {
    rm -rf "${FAKE}" "${TMPDIR}" "${MODES}" "${WORK}/stdin-got" "${WORK}/version.out" "${WORK}/version.status" "${WORK}/v-stdin-got" "${WORK}/list.out" "${WORK}/list.status" "${WORK}/sdrun"
    mkdir -p "${FAKE}" "${TMPDIR}" "${MODES}"
    : >"${CALLS}"
    : >"${VERCALLS}"
    unset WGET_SAYS INSTALL_SAYS MV_SAYS SUDO_OK SDRUN_STATUS SUDO_RM_FAILS
    : >"${SUDOCALLS}"
    : >"${UNPRIV}"
    : >"${ENVSEEN}"
    : >"${ENVSEEN}.kept"
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
    --no-module-update      Do not update the installed optional modules to the versions of this
                            release's catalogue (same as RECASAOS_MODULE_UPDATE=0). Without it, a
                            module whose update fails keeps running at its old version
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
        MODULES_REQUESTED="" NO_TELEMETRY=0 DEBIAN_ARCHIVE=0 BUILD_DIR="" NO_MODULE_UPDATE=0
        CASAOS_MODULE_CATALOGUE_SHA256="${CATALOGUE_SHA}"
        parse_options "$@"
        echo "REQUESTED=[${MODULES_REQUESTED}] TELEMETRY=${NO_TELEMETRY} ARCHIVE=${DEBIAN_ARCHIVE} BUILD=[${BUILD_DIR}] MODULE_UPDATE_OFF=${NO_MODULE_UPDATE}"
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

parses "" --no-telemetry
case "${out}" in *" MODULE_UPDATE_OFF=0") ;; *) fail "the update of the modules is turned off without being asked: ${out}" ;; esac
parses "" --no-module-update
case "${out}" in *" MODULE_UPDATE_OFF=1") ;; *) fail "--no-module-update does not turn the update of the modules off: ${out}" ;; esac
parses "virt" --module virt --no-module-update --no-telemetry -p /x
case "${out}" in *"TELEMETRY=1 ARCHIVE=0 BUILD=[/x] MODULE_UPDATE_OFF=1") ;; *) fail "--no-module-update beside the other options: ${out}" ;; esac
# RECASAOS_MODULE_UPDATE=0 on the install line is the same; any other value, or none, is not (install.sh's own lines, run by bash)
module_update_off() { # [the value of RECASAOS_MODULE_UPDATE; none: not set]
    (
        unset RECASAOS_MODULE_UPDATE
        if [ $# -gt 0 ]; then RECASAOS_MODULE_UPDATE="$1" && export RECASAOS_MODULE_UPDATE; fi
        bash -c "$(sed -n '/^NO_MODULE_UPDATE=0$/,/^fi$/p' "${INSTALL_SH}")"'
echo "${NO_MODULE_UPDATE}"'
    )
}
[ "$(sed -n '/^NO_MODULE_UPDATE=0$/,/^fi$/p' "${INSTALL_SH}" | wc -l | tr -d ' ')" -eq 4 ] || fail "install.sh does not read RECASAOS_MODULE_UPDATE in the four lines from NO_MODULE_UPDATE=0"
[ "$(module_update_off)" = 0 ] || fail "no RECASAOS_MODULE_UPDATE turned the update of the modules off"
[ "$(module_update_off 0)" = 1 ] || fail "RECASAOS_MODULE_UPDATE=0 does not turn the update of the modules off"
for v in 1 "" no false 00 " 0"; do
    [ "$(module_update_off "${v}")" = 0 ] || fail "RECASAOS_MODULE_UPDATE='${v}' turned the update of the modules off"
done
pass "--no-module-update and RECASAOS_MODULE_UPDATE=0 turn the update of the modules off, and nothing else does"

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
for bad in --help --bogus -x --module-x --modules --Module --no-telemetry=1 --use-debian-archive=1 -module -m --no-module-update=1 --no-module-updates --module-update; do
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

# a symbolic link; a Windows checkout needs MSYS=winsymlinks:nativestrict, and the right to make them, for a real one
link() { # <target> <link>
    ln -s "$1" "$2" 2>/dev/null && [ -L "$2" ] || fail "no symbolic link can be made here (on Windows: MSYS=winsymlinks:nativestrict, with the right to make them)"
}

no_leftover() { # <what>: no temporary directory, and no staged copy of the catalogue beside its place
    [ -z "$(ls -A "${TMPDIR}")" ] || fail "$1: a temporary directory was left: $(ls -A "${TMPDIR}")"
    [ -z "$(ls -A "${CASA_MODULE_CATALOGUE_DIR}" 2>/dev/null | grep '^\.catalogue\.json\.' || true)" ] || fail "$1: a staged copy of the catalogue was left"
}

earlier() { mkdir -p "${CASA_MODULE_CATALOGUE_DIR}" && printf 'earlier\n' >"${installed_file}"; }

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
staged="$(awk '$1 == "install" && $2 == "-m" && $3 == "0644" { print $5 }' "${CALLS}")"
case "${staged}" in "${CASA_MODULE_CATALOGUE_DIR}"/.catalogue.json.??????) ;; *) fail "good catalogue: the file is not put down 0644 under a name of its own beside its place: $(calls)" ;; esac
case "$(awk '$1 == "install" && $2 == "-m" { print $4 }' "${CALLS}")" in "${TMPDIR}"/*/catalogue.json) ;; *) fail "good catalogue: what is put down is not the download: $(calls)" ;; esac
[ "$(awk '$1 == "mv" { print $2 " " $3 " " $4 }' "${CALLS}")" = "-f ${staged} ${installed_file}" ] || fail "good catalogue: the staged copy is not renamed onto its place: $(calls)"
pass "the catalogue whose digest is right is put in place"

# every update: a newer catalogue replaces the one there
write_catalogue 2
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
catalogue_step
cmp -s "${CATALOGUE_FILE}" "${installed_file}" && case "${out}" in *"READY=1"*) true ;; *) false ;; esac || fail "an update did not replace the catalogue: ${out}"
no_leftover "update"
pass "an update replaces it"

# a file that is not the one the installer carries the digest of: the catalogue step stops, the catalogue of the earlier
# release goes (one rule, as for a release that has none), the install goes on
tamper() { # <what> <digest the installer carries> <reason said>
    what="$1"
    CASAOS_MODULE_CATALOGUE_SHA256="$2"
    had=no
    if [ -e "${installed_file}" ] || [ -L "${installed_file}" ]; then had=yes; fi
    catalogue_step
    [ "${status}" -eq 0 ] || fail "${what}: the install ended (status ${status}): ${out}"
    case "${out}" in *"READY=0"*"the install goes on"*) ;; *) fail "${what}: ${out}" ;; esac
    case "${out}" in *"$3"*"ReCasaOS itself is installed and is not affected"*) ;; *) fail "${what}: not said: ${out}" ;; esac
    [ ! -e "${installed_file}" ] && [ ! -L "${installed_file}" ] || fail "${what}: the catalogue of an earlier release is still in place"
    case "${had}:${out}" in
    yes:*"The catalogue of an earlier release was removed"*) ;;
    no:*"was removed"*) fail "${what}: a removal is said when there was nothing to remove: ${out}" ;;
    no:*) ;;
    *) fail "${what}: the removal of the earlier catalogue is not said: ${out}" ;;
    esac
    case "${out}" in *"Module catalogue installed"*) fail "${what}: said to be installed" ;; esac
    # a file that is not the one the installer has the digest of never gets as far as the directory of the catalogue
    case "$3" in
    *"expects"* | *"downloaded"*) ! grep -Eq '^(mv|install) ' "${CALLS}" || fail "${what}: the file went on towards its place after it was refused: $(calls)" ;;
    esac
    no_leftover "${what}"
}
fresh
write_catalogue 3
good="$(sha_of "${CATALOGUE_FILE}")"
printf 'tampered\n' >"${CATALOGUE_FILE}"
tamper "a tampered file, nothing in place before" "${good}" "is not the file this installer expects"
[ ! -e "${installed_file}" ] || fail "a tampered file was put in place"
earlier
tamper "a tampered file, an earlier catalogue in place" "${good}" "is not the file this installer expects"
: >"${CATALOGUE_FILE}"
earlier
tamper "an empty file" "${good}" "is not the file this installer expects"
rm -f "${CATALOGUE_FILE}"
earlier
tamper "a file that is not published" "${good}" "could not be downloaded"
write_catalogue 3
WGET_SAYS=fail
export WGET_SAYS
earlier
tamper "a download that fails" "${good}" "could not be downloaded"
unset WGET_SAYS
write_catalogue 3
earlier
tamper "a digest of another file" "$(printf 'x' | sha256sum | cut -d' ' -f1)" "is not the file this installer expects"
write_catalogue 3
earlier
MV_SAYS=fail
export MV_SAYS
tamper "a rename that fails" "${good}" "could not be put in place"
unset MV_SAYS
write_catalogue 3
earlier
INSTALL_SAYS=copy
export INSTALL_SAYS
tamper "a copy that fails" "${good}" "could not be put in place"
unset INSTALL_SAYS
write_catalogue 3
earlier
INSTALL_SAYS=fail
export INSTALL_SAYS
tamper "an install that fails altogether" "${good}" "could not be put in place"
unset INSTALL_SAYS
pass "a tampered or missing catalogue stops the catalogue and nothing else, and the earlier release's catalogue goes"

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
fresh
write_catalogue 3
earlier
CASAOS_MODULE_CATALOGUE_SHA256="abc"
catalogue_step
[ ! -e "${installed_file}" ] && case "${out}" in *"The catalogue of an earlier release was removed"*) true ;; *) false ;; esac || fail "a digest that is not a digest: the earlier catalogue stays: ${out}"
pass "a digest that is not 64 lower case hexadecimal digits downloads nothing"

# a release with no catalogue: nothing is downloaded, the earlier release's file goes
fresh
CASAOS_MODULE_CATALOGUE_SHA256=""
catalogue_step
[ "${status}" -eq 0 ] && case "${out}" in *"READY=0"*"the install goes on"*) true ;; *) false ;; esac || fail "no catalogue in the release: ${status}: ${out}"
no_calls "a release with no catalogue"
earlier
catalogue_step
[ "${status}" -eq 0 ] && [ ! -e "${installed_file}" ] || fail "no catalogue in the release: the file of an earlier release is still there"
case "${out}" in *"the one of an earlier release was removed"*) ;; *) fail "no catalogue: removal not said: ${out}" ;; esac
[ ! -s "${CALLS}" ] || fail "no catalogue: something was run"
# a link where the file was, dangling or not: the link goes, and what it points at stays
fresh
mkdir -p "${CASA_MODULE_CATALOGUE_DIR}"
link "${FAKE}/nowhere" "${installed_file}"
catalogue_step
[ "${status}" -eq 0 ] && [ ! -L "${installed_file}" ] && [ ! -e "${installed_file}" ] || fail "no catalogue: a dangling link is still where the catalogue was: ${out}"
case "${out}" in *"the one of an earlier release was removed"*) ;; *) fail "no catalogue: the removal of the dangling link is not said: ${out}" ;; esac
fresh
mkdir -p "${CASA_MODULE_CATALOGUE_DIR}"
printf 'precious\n' >"${FAKE}/precious"
link "${FAKE}/precious" "${installed_file}"
catalogue_step
[ "${status}" -eq 0 ] && [ ! -L "${installed_file}" ] && [ "$(cat "${FAKE}/precious")" = precious ] || fail "no catalogue: a link to a file was followed, or is still there: ${out}"
# a directory that is a link is not gone into
fresh
mkdir -p "${FAKE}/elsewhere" "${FAKE}/usr/share/casaos"
printf 'theirs\n' >"${FAKE}/elsewhere/catalogue.json"
link "${FAKE}/elsewhere" "${CASA_MODULE_CATALOGUE_DIR}"
catalogue_step
[ "${status}" -eq 0 ] && [ "$(cat "${FAKE}/elsewhere/catalogue.json")" = theirs ] || fail "no catalogue: a file behind a link to the directory was removed: ${out}"
case "${out}" in *"was removed"*) fail "no catalogue: a removal is said behind a link to the directory: ${out}" ;; esac
pass "a release with no catalogue places none and leaves none of an earlier release, a link included, and goes through none"

# a catalogue that cannot be put in place (the directory cannot be made)
fresh
write_catalogue 4
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
mkdir -p "${FAKE}/usr/share/casaos" && printf 'a file where the directory should be\n' >"${FAKE}/usr/share/casaos/modules"
catalogue_step
[ "${status}" -eq 0 ] && case "${out}" in *"could not be put in place"*"READY=0"*"the install goes on"*) true ;; *) false ;; esac || fail "an impossible place: ${status}: ${out}"
no_leftover "an impossible place"
pass "a catalogue that cannot be put in place stops the catalogue and nothing else"

# what is in the way is not a plain file: a directory, or a link, where the file goes; a link where its directory is
in_the_way() { # <what>: refused, nothing moved inside, nothing followed, nothing left
    [ "${status}" -eq 0 ] || fail "$1: the install ended (status ${status}): ${out}"
    case "${out}" in *"could not be put in place: something that is not a plain file is where it goes"*"READY=0"*"the install goes on"*) ;; *) fail "$1: not refused as it should be: ${out}" ;; esac
    case "${out}" in *"Module catalogue installed"*) fail "$1: said to be installed" ;; esac
    ! grep -q '^mv ' "${CALLS}" || fail "$1: a rename was made: $(calls)"
    no_leftover "$1"
}
fresh
write_catalogue 6
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
mkdir -p "${installed_file}"
catalogue_step
in_the_way "a directory where the file goes"
[ -d "${installed_file}" ] && [ -z "$(ls -A "${installed_file}")" ] || fail "a directory where the file goes: the catalogue was moved into it, or the directory is gone"
case "${out}" in *"was removed"*) fail "a directory where the file goes: its removal is said" ;; esac
fresh
write_catalogue 6
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
mkdir -p "${CASA_MODULE_CATALOGUE_DIR}"
printf 'precious\n' >"${FAKE}/precious"
link "${FAKE}/precious" "${installed_file}"
catalogue_step
in_the_way "a link to a file where the file goes"
[ "$(cat "${FAKE}/precious")" = precious ] || fail "a link where the file goes: what it points at was written through"
[ ! -L "${installed_file}" ] || fail "a link where the file goes: it is still there, a refresh that failed leaves no catalogue of an earlier release"
fresh
write_catalogue 6
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
mkdir -p "${CASA_MODULE_CATALOGUE_DIR}"
link "${FAKE}/nowhere" "${installed_file}"
catalogue_step
in_the_way "a dangling link where the file goes"
[ ! -e "${FAKE}/nowhere" ] || fail "a dangling link where the file goes: the file it names was made"
fresh
write_catalogue 6
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
mkdir -p "${FAKE}/elsewhere" "${FAKE}/usr/share/casaos"
printf 'theirs\n' >"${FAKE}/elsewhere/catalogue.json"
link "${FAKE}/elsewhere" "${CASA_MODULE_CATALOGUE_DIR}"
catalogue_step
in_the_way "a link where the directory is"
[ "$(cat "${FAKE}/elsewhere/catalogue.json")" = theirs ] && [ "$(ls -A "${FAKE}/elsewhere")" = catalogue.json ] || fail "a link where the directory is: something was written, or removed, behind it"
case "${out}" in *"was removed"*) fail "a link where the directory is: a removal is said" ;; esac
! grep -q '^install ' "${CALLS}" || fail "a link where the directory is: something was installed: $(calls)"
pass "a directory or a link where the catalogue goes, or a link where its directory is, is refused, and nothing is moved into it or written through it"

# what the rename leaves is looked at too: a rename that says it is done and leaves no plain file is not a catalogue in place
fresh
write_catalogue 6
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
MV_SAYS=lie
export MV_SAYS
catalogue_step
unset MV_SAYS
[ "${status}" -eq 0 ] || fail "a rename that leaves a directory: the install ended (status ${status}): ${out}"
case "${out}" in *"could not be put in place: something that is not a plain file is where it goes"*"READY=0"*"the install goes on"*) ;; *) fail "a rename that leaves a directory: not refused: ${out}" ;; esac
case "${out}" in *"Module catalogue installed"*) fail "a rename that leaves a directory: said to be installed" ;; esac
no_leftover "a rename that leaves a directory"
pass "a rename that leaves no plain file where the catalogue goes is not a catalogue in place"

# with sudo: every call that changes the machine goes through it
sudo_run() { # <what>: the stand-ins that ran without sudo are none, and the calls through it are these (the temporary names made one)
    [ ! -s "${UNPRIV}" ] || fail "$1: something ran without sudo: $(tr '\n' '|' <"${UNPRIV}")"
    got="$(sed "s|${TMPDIR}/[^/ ]*|<work>|g; s|${CASA_MODULE_CATALOGUE_DIR}|<dir>|g; s|\.catalogue\.json\.[A-Za-z0-9]\{6\}|.catalogue.json.<s>|g" "${SUDOCALLS}")"
    [ "${got}" = "$2" ] || fail "$1: the calls through sudo are not these:
$(printf '%s' "$2")
got:
${got}"
}
fresh
write_catalogue 7
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
sudo_cmd=sudo
SUDO_OK=1
export SUDO_OK
catalogue_step
sudo_cmd=""
unset SUDO_OK
case "${out}" in *"READY=1"*) ;; *) fail "with sudo: the catalogue was not placed: ${out}" ;; esac
cmp -s "${CATALOGUE_FILE}" "${installed_file}" || fail "with sudo: the file in place is not the one served"
sudo_run "the catalogue" "sudo wget -t 3 -q -O <work>/catalogue.json ${CASAOS_RELEASE_BASE_URL}/modules-catalogue-v0.0.0.json
sudo sha256sum --check --status -
sudo install -d -m 0755 <dir>
sudo mktemp <dir>/.catalogue.json.<s>
sudo install -m 0644 <work>/catalogue.json <dir>/.catalogue.json.<s>
sudo mv -f <dir>/.catalogue.json.<s> <dir>/catalogue.json
sudo rm -rf <work>"
# and the removal of the catalogue of an earlier release
fresh
earlier
CASAOS_MODULE_CATALOGUE_SHA256=""
sudo_cmd=sudo
SUDO_OK=1
export SUDO_OK
catalogue_step
sudo_cmd=""
unset SUDO_OK
[ ! -e "${installed_file}" ] || fail "with sudo: the earlier catalogue is still there"
sudo_run "the removal" "sudo rm -f <dir>/catalogue.json"
pass "with sudo, every call that changes the machine goes through it"

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

# The assistant is given none of the CASAOS_MODULE variables of this environment: they are the test hooks of the core
# (a catalogue and an archive of the caller's own, read in place of the pinned ones; the nonce of the core's own jobs),
# and the installer runs as root, with whatever its caller exported (a direct run, sudo -E, a CI job). Nothing else of the
# environment is taken away.
HOOKS="CASAOS_MODULE_TEST CASAOS_MODULE_CATALOGUE CASAOS_MODULE_ARCHIVE_FILE CASAOS_MODULE_NONCE CASAOS_MODULE_FUTURE_HOOK CASAOS_MODULE_lower CASAOS_MODULECMD_REAL_APT CASAOS_MODULE"
hooks_on() {
    CASAOS_MODULE_TEST=1
    CASAOS_MODULE_CATALOGUE=/evil/catalogue.json
    CASAOS_MODULE_ARCHIVE_FILE=/evil/archive.tar.gz
    CASAOS_MODULE_NONCE=0123456789abcdef0123456789abcdef
    CASAOS_MODULE_FUTURE_HOOK=1
    CASAOS_MODULE_lower=1
    CASAOS_MODULECMD_REAL_APT=/evil/apt
    CASAOS_MODULE=1
    KEEP_ME=kept
    export ${HOOKS} KEEP_ME
}
hooks_off() {
    unset ${HOOKS} KEEP_ME
}
fresh
hooks_on
modules_step virt ok
hooks_off
[ "${status}" -eq 0 ] && [ "$(calls)" = "casaos module install virt --yes|" ] || fail "with the hooks exported the assistant was not run as it is: ${status}: $(calls)"
[ ! -s "${ENVSEEN}" ] || fail "the assistant was given variables of the hooks: $(tr '\n' ' ' <"${ENVSEEN}")"
[ "$(cat "${ENVSEEN}.kept")" = "KEEP_ME=kept" ] || fail "the rest of the environment was not left as it was: $(cat "${ENVSEEN}.kept")"
case "${out}" in *"[0] The module virt is installed."*) ;; *) fail "with the hooks exported the module was not installed: ${out}" ;; esac
# (and the stand-in does see a variable when it is not scrubbed: the check above can say no)
fresh
KEEP_ME=kept CASAOS_MODULE_TEST=1 "${CASA_BIN}" module install virt --yes </dev/null >/dev/null
[ "$(cat "${ENVSEEN}")" = "CASAOS_MODULE_TEST=1" ] || fail "the stand-in does not record the variables it is given"
pass "the assistant is given none of the CASAOS_MODULE variables, and the rest of the environment as it is"

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

# an assistant that ended without a result line is a failure too, whatever it printed; the status it ended with is said as it was
for modewant in noresult:3 silent:0 okexit1:1 forged:0 othername:0 status7:7; do
    mode="${modewant%%:*}"
    want="${modewant#*:}"
    fresh
    modules_step virt "${mode}"
    [ "${status}" -eq 0 ] || fail "mode ${mode}: the install ended (status ${status})"
    case "${out}" in *"the install goes on"*) ;; *) fail "mode ${mode}: the install did not go on" ;; esac
    case "$(summary)" in *"The module virt was not installed: the assistant did not report a success (exit status ${want})."*"sudo casaos module remove virt"*) ;; *) fail "mode ${mode}: wanted exit status ${want}: $(summary)" ;; esac
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

# with sudo: the assistant is run through it, still with none of the variables, and the temporary directory goes through it too
fresh
hooks_on
sudo_cmd=sudo
SUDO_OK=1
export SUDO_OK
modules_step virt ok
sudo_cmd=""
unset SUDO_OK
hooks_off
[ "${status}" -eq 0 ] || fail "with sudo: status ${status}: ${out}"
case "${out}" in *"[0] The module virt is installed."*) ;; *) fail "with sudo: not installed: ${out}" ;; esac
[ ! -s "${UNPRIV}" ] || fail "with sudo: something ran without it: $(tr '\n' '|' <"${UNPRIV}")"
[ ! -s "${ENVSEEN}" ] || fail "with sudo: the assistant was given variables of the hooks: $(tr '\n' ' ' <"${ENVSEEN}")"
[ "$(wc -l <"${SUDOCALLS}" | tr -d ' ')" -eq 2 ] || fail "with sudo: not the assistant and the removal of the temporary directory: $(tr '\n' '|' <"${SUDOCALLS}")"
line="$(sed -n 1p "${SUDOCALLS}")"
case "${line}" in "sudo env "*" ${CASA_BIN} module install virt --yes") ;; *) fail "with sudo: the assistant is not run as 'sudo env ... casaos module install virt --yes': ${line}" ;; esac
for v in ${HOOKS}; do
    case "${line}" in *" -u ${v} "*) ;; *) fail "with sudo: ${v} is not taken out of the environment of the assistant: ${line}" ;; esac
done
case "${line}" in *KEEP_ME*) fail "with sudo: a variable that is not of the hooks is taken out: ${line}" ;; esac
case "$(sed -n 2p "${SUDOCALLS}")" in "sudo rm -rf ${TMPDIR}/"*) ;; *) fail "with sudo: the temporary directory is not removed through it: $(tr '\n' '|' <"${SUDOCALLS}")" ;; esac
pass "with sudo, the assistant and the removal of the temporary directory go through it, and the hooks are still taken out"

# ---- the update of the installed modules -------------------------------------

CASAOS_UPDATE_LOG="${FAKE}/var/log/casaos/upgrade.log"
CASA_RUN_DIR="${FAKE}/run"

# entry <name> <none|same|newer> [description]: a module of the list as `casaos module list` prints it (json.Marshal: no blank, the keys in
# the order of the core's ListModule); none: not installed; same: installed at the catalogue's tag; newer: the catalogue has a later one
entry() {
    case "$2" in
    none) printf '{"name":"%s","title":"T","description":"%s","tier":"core","tag":"v0.2.2","catalogue":true,"installed":null,"system":{"supported":true,"reasons":[]}}' "$1" "${3:-d}" ;;
    same) printf '{"name":"%s","title":"T","description":"%s","tier":"core","tag":"v0.2.2","catalogue":true,"installed":{"version":"v0.2.2"},"system":{"supported":true,"reasons":[]},"update":{"available":false,"installed":"v0.2.2","latest":"v0.2.2"}}' "$1" "${3:-d}" ;;
    newer) printf '{"name":"%s","title":"T","description":"%s","tier":"core","tag":"v0.2.2","catalogue":true,"installed":{"version":"v0.2.1"},"system":{"supported":true,"reasons":[]},"update":{"available":true,"installed":"v0.2.1","latest":"v0.2.2"}}' "$1" "${3:-d}" ;;
    esac
}
list_says() { printf '{"catalogue":{"ok":true},"modules":[%s]}\n' "$1" >"${WORK}/list.out"; }
ufresh() { fresh && say_version v0.4.78; } # a core that has `casaos module update`

# update_step: Update_Installed_Modules as the install runs it, with what is left of a script piped into bash on its input
update_step() {
    status=0
    out="$(
        exec 2>&1
        set -e
        MODULE_CATALOGUE_READY="${READY:-1}"
        CASAOS_MODULE_CATALOGUE_SHA256="${MODSHA-${CATALOGUE_SHA}}"
        printf 'REST OF THE SCRIPT\n' | {
            Update_Installed_Modules
            read -r left || left="(nothing left)"
            echo "the install goes on: ${left}"
        }
    )" || status=$?
    case "${out}" in *"the install goes on: REST OF THE SCRIPT"*) ;; *) fail "the update of the modules ended the install, or consumed the script's own input (status ${status}): ${out}" ;; esac
    [ "${status}" -eq 0 ] || fail "the update of the modules changed the status of the install (${status}): ${out}"
    # (unless the case makes the removals fail)
    [ "${SUDO_RM_FAILS:-}" = 1 ] || [ -z "$(ls -A "${TMPDIR}")" ] || fail "the update of the modules left a temporary directory: $(ls -A "${TMPDIR}")"
}

# whether the run is casaos-update.service: install.sh's own test, on a cgroup file of the case's
CGROUP="${WORK}/cgroup"
eval "$(extract Running_Under_Update_Unit | sed 's|/proc/\$\$/cgroup|'"${CGROUP}"'|g')"
under_unit() { # <yes|no> <the cgroup file's text; none: no file>
    rm -f "${CGROUP}"
    [ "$2" = none ] || printf '%s\n' "$2" >"${CGROUP}"
    if Running_Under_Update_Unit; then got=yes; else got=no; fi
    [ "${got}" = "$1" ] || fail "a run whose cgroup says '$2' is taken for casaos-update.service: ${got}, wanted $1"
}
under_unit yes "0::/system.slice/casaos-update.service"
under_unit yes "1:name=systemd:/system.slice/casaos-update.service
0::/system.slice/casaos-update.service/x"
under_unit no "0::/system.slice/casaos.service"
under_unit no "0::/system.slice/casaos-update-v0-5-44-1729500000-42.service"
under_unit no "0::/system.slice/xcasaos-update.service"
under_unit no "0::/user.slice/user-0.slice/session-1.scope"
under_unit no none
Running_Under_Update_Unit() { [ "${UNDER_UPDATE_UNIT:-0}" = 1 ]; }
pass "the update the core started is told by its cgroup, casaos-update.service and no other unit"

# nothing to update: the list is asked for, once, and nothing else
nothing_to_update() { # <what>
    update_step
    [ "$(calls)" = "casaos module list|" ] || fail "$1: something else than the list was asked: $(calls)"
    [ "$(cat "${VERCALLS}")" = "casaos -v" ] || fail "$1: the version was not asked exactly once"
    [ ! -s "${WORK}/stdin-got" ] || fail "$1: the list was given input"
    case "$(summary)" in "[2] No installed module has a later version in the catalogue of this release.") ;; *) fail "$1: not said, or more said: $(summary)" ;; esac
}
ufresh
nothing_to_update "no module at all"
ufresh
list_says "$(entry virt none)"
nothing_to_update "a module that is not installed"
ufresh
list_says "$(entry virt same)"
nothing_to_update "a module at the catalogue's version"
ufresh
list_says "$(entry virt none),$(entry other same)"
nothing_to_update "one not installed, one at the catalogue's version"
ufresh
printf '{"catalogue":{"ok":false,"error":"invalid","detail":"x"},"modules":[]}\n' >"${WORK}/list.out"
nothing_to_update "a catalogue that the assistant does not take"
pass "with no installed module that has a later version, the list is read and nothing is updated"

# one module with a later version: `casaos module update <name> --yes`, once, with no input and none of the CASAOS_MODULE variables
ufresh
list_says "$(entry virt newer)"
hooks_on
update_step
hooks_off
[ "$(calls)" = "casaos module list|casaos module update virt --yes|" ] || fail "one module with a later version: not the list then 'casaos module update virt --yes', once: $(calls)"
[ "$(cat "${VERCALLS}")" = "casaos -v" ] || fail "one module: the version was not asked exactly once"
[ ! -s "${WORK}/stdin-got" ] || fail "one module: the update was given input"
[ ! -s "${ENVSEEN}" ] || fail "one module: the assistant was given variables of the hooks: $(tr '\n' ' ' <"${ENVSEEN}")"
[ "$(cat "${ENVSEEN}.kept")" = "KEEP_ME=kept
KEEP_ME=kept" ] || fail "one module: the rest of the environment was not left as it was: $(cat "${ENVSEEN}.kept")"
case "${out}" in *"CASAOS_MODULE_STEP - virt done done 2026"*) ;; *) fail "one module: the assistant's lines are not shown: ${out}" ;; esac
case "$(summary)" in *"[2] Updating the module virt..."*"[0] The module virt is updated."*"[0] Modules updated to the versions of this release: virt."*) ;; *) fail "one module: not said: $(summary)" ;; esac
case "$(summary)" in *"NOT updated"*) fail "one module: said not to be updated: $(summary)" ;; esac
pass "a module with a later version is updated with 'casaos module update <name> --yes', once, no input, no hook variables"

# two: the first fails and is rolled back, the second is updated all the same; the install is not failed
ufresh
list_says "$(entry alpha newer),$(entry beta newer)"
printf failed >"${MODES}/alpha"
printf ok >"${MODES}/beta"
update_step
[ "$(calls)" = "casaos module list|casaos module update alpha --yes|casaos module update beta --yes|" ] || fail "two modules: not each in turn: $(calls)"
case "$(summary)" in *"[3] The module alpha was not updated: it failed at the step download (download_failed)."*"[3] Its old version was put back and keeps running."*"[3] Retry from the Modules window of the dashboard, or with: sudo casaos module update alpha"*"[0] The module beta is updated."*"[0] Modules updated to the versions of this release: beta."*"[3] Modules NOT updated, each at its old version unless its lines above say otherwise: alpha. Retry from the Modules window of the dashboard, or with: sudo casaos module update <name>"*) ;; *) fail "two modules, the first failing: $(summary)" ;; esac
case "${out}" in *"failed at step download: download_failed"*"rolled back"*) ;; *) fail "two modules: the assistant's own sentences are not shown: ${out}" ;; esac
pass "a module whose update fails is said, with its old version put back, and the next one is updated"

# each answer of the assistant, said as what it is
answer() { # <mode> <what is said> [counted as not updated: yes|no]
    ufresh
    list_says "$(entry virt newer)"
    printf '%s' "$1" >"${MODES}/virt"
    update_step
    [ "$(calls)" = "casaos module list|casaos module update virt --yes|" ] || fail "mode $1: $(calls)"
    case "$(summary)" in *"$2"*) ;; *) fail "mode $1: '$2' is not said: $(summary)" ;; esac
    case "$(summary)" in *"is updated."* | *"Modules updated"*) fail "mode $1: said to be updated: $(summary)" ;; esac
    case "${3:-yes}:$(summary)" in
    yes:*"[3] Modules NOT updated, each at its old version unless its lines above say otherwise: virt."*) ;;
    no:*"NOT updated"* | no:*"Retry"*) fail "mode $1: nothing to do is said as a failure: $(summary)" ;;
    no:*) ;;
    *) fail "mode $1: not in the summary of what was not updated: $(summary)" ;;
    esac
}
answer uptodate "[2] The module virt has nothing to update (up_to_date)." no
answer downgrade "[2] The module virt has nothing to update (downgrade)." no
# failed - backup_not_dropped -: the new version is installed, only the backup of the old one is left: updated, with what to do
ufresh
list_says "$(entry virt newer)"
printf notdropped >"${MODES}/virt"
update_step
case "$(summary)" in *"[3] The module virt is updated, but the backup of its old version could not be dropped: delete it by hand as the lines above say, or the next install, update or remove of the module puts the old version back."*"[0] Modules updated to the versions of this release: virt."*) ;; *) fail "backup_not_dropped: not said as an update that is installed: $(summary)" ;; esac
case "$(summary)" in *"NOT updated"* | *"was not updated"*) fail "backup_not_dropped: said not to be updated: $(summary)" ;; esac
answer busy "[3] The module virt was not updated: the assistant refused it (busy). Nothing was changed: it keeps its old version. Retry from the Modules window of the dashboard, or with: sudo casaos module update virt"
answer incomplete "[3] The module virt was not updated: it failed at the step unpack (unpack_failed).
[3] Its old version could not all be put back: the lines above say what is left and how to finish.
[3] Retry from the Modules window"
case "$(summary)" in *"keeps running"*) fail "an incomplete rollback is said to keep the old version running" ;; esac
answer left "[3] The module virt was not updated: it failed at the step swap (file_exists).
[3] Its old version was put back, except files that were found changed and left as they were: the lines above name them."
case "$(summary)" in *"keeps running"*) fail "a rollback that left files is said to keep the old version running" ;; esac
for modewant in noresult:3 silent:0 okexit1:1 forged:0 othername:0 status7:7; do
    answer "${modewant%%:*}" "[3] The module virt was not updated: the assistant did not report a success (exit status ${modewant#*:}). Retry from the Modules window of the dashboard, or with: sudo casaos module update virt"
done
answer escape "it failed at the step dl??31mx (cd??0m_x)."
case "$(summary)" in *"$(printf '\033')"*) fail "an escape sequence of the assistant's result line reached the summary" ;; esac
pass "nothing to update, a refusal, a failure and its rollback, and an end with no result are each said as what they are"

# the opt-out: nothing is asked of the core, not even its version
ufresh
list_says "$(entry virt newer)"
NO_MODULE_UPDATE=1
update_step
NO_MODULE_UPDATE=0
[ ! -s "${CALLS}" ] && [ ! -s "${VERCALLS}" ] || fail "the opt-out: the core was asked something: $(calls) / $(cat "${VERCALLS}")"
case "$(summary)" in "[2] The installed modules were not updated (--no-module-update or RECASAOS_MODULE_UPDATE=0): each keeps the version it has, and the Modules window offers the update.") ;; *) fail "the opt-out: not said: $(summary)" ;; esac
# and the option, through install.sh's own parsing, ahead of the step
ufresh
list_says "$(entry virt newer)"
status=0
out="$(
    exec 2>&1
    set -e
    NO_MODULE_UPDATE=0 MODULES_REQUESTED=""
    CASAOS_MODULE_CATALOGUE_SHA256="${CATALOGUE_SHA}"
    MODULE_CATALOGUE_READY=1
    parse_options --no-module-update
    Update_Installed_Modules
    echo finished
)" || status=$?
[ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] && [ ! -s "${VERCALLS}" ] && case "${out}" in *"were not updated (--no-module-update"*finished) true ;; *) false ;; esac ||
    fail "--no-module-update: the modules were updated, or the install did not finish: ${status}: $(calls): ${out}"
pass "--no-module-update and RECASAOS_MODULE_UPDATE=0 update no module and ask the core nothing"

# no catalogue in place, a core this run did not put in place, no core: nothing asked
not_asked() { # <what> <what is said>
    update_step
    [ ! -s "${CALLS}" ] && [ ! -s "${VERCALLS}" ] || fail "$1: the core was asked something: $(calls) / $(cat "${VERCALLS}")"
    case "$(summary)" in "$2") ;; *) fail "$1: not said, or more said: $(summary)" ;; esac
}
ufresh
list_says "$(entry virt newer)"
READY=0
not_asked "a catalogue that could not be installed" "[3] No module was updated: the module catalogue could not be installed by this run (see above), so each module keeps the version it has. ReCasaOS itself is installed and is not affected."
unset READY
MODSHA=""
not_asked "a release with no catalogue" "[2] No module was updated: this release carries no module catalogue, so it has no version to update one to."
unset MODSHA
CORE_INSTALLED_NOW=0
not_asked "a core this run did not put in place" "[3] No module was updated: this run did not put the core of this release in place, so ${CASA_BIN} is not known to have 'casaos module update'. ReCasaOS itself is installed and is not affected."
CORE_INSTALLED_NOW=1
CASA_BIN_SAVED="${CASA_BIN}"
CASA_BIN="${WORK}/bin/not-there"
not_asked "no core" "[3] No module was updated: ${WORK}/bin/not-there is not there. ReCasaOS itself is installed and is not affected."
CASA_BIN="${CASA_BIN_SAVED}"
pass "no catalogue in place, a core this run did not put in place, or no core: no module is updated and the core is asked nothing"

# a core older than the first that has `casaos module update` (v0.4.75 has `module` and no update), or whose version is not one: never given `module`
too_old() { # <what> <what -v prints> [its status]
    ufresh
    say_version "$2" "${3:-0}"
    list_says "$(entry virt newer)"
    update_step
    [ ! -s "${CALLS}" ] || fail "$1: the core was given 'module': $(calls)"
    [ "$(cat "${VERCALLS}")" = "casaos -v" ] || fail "$1: the version was not asked exactly once"
    case "$(summary)" in "[3] No module was updated: ${CASA_BIN} did not report a version of v0.4.78 or later, the first that has 'casaos module update' (it reported '"*"'). ReCasaOS itself is installed and is not affected.") ;; *) fail "$1: not said: $(summary)" ;; esac
    case "$(summary)" in *"$(printf '\033')"*) fail "$1: an escape sequence reached the screen" ;; esac
}
too_old "the core that brought module, without update" v0.4.75
too_old "the patch before update" v0.4.77
too_old "an older minor" v0.3.999
too_old "a private build" v0.0.0-dev
too_old "a tag with a suffix" v0.4.78-rc1
too_old "a core that says nothing" ''
too_old "a word" hello
too_old "an escape sequence" "$(printf '\033[31mv0.4.77')"
too_old "the right version, with a status that is not 0" v0.4.78 1
for v in v0.4.78 v0.4.79 v0.4.100 v0.5.0 v1.0.0; do
    ufresh
    say_version "${v}"
    list_says "$(entry virt newer)"
    update_step
    [ "$(calls)" = "casaos module list|casaos module update virt --yes|" ] || fail "a core of ${v}: the module was not updated: $(calls)"
done
for badmin in __CASAOS_MODULES_MIN_CORE_TAG__ "" v0.4; do
    ufresh
    say_version v9.9.9
    list_says "$(entry virt newer)"
    CASAOS_MODULES_MIN_CORE_TAG="${badmin}"
    update_step
    CASAOS_MODULES_MIN_CORE_TAG=v0.4.75
    [ ! -s "${CALLS}" ] || fail "a minimum of '${badmin}' for 'module': the core was given 'module': $(calls)"
done
pass "a core older than v0.4.78, or whose version is not one, or an installer built with no minimum, updates no module"

# a list that is not one: nothing is updated, and that is said
bad_list() { # <what> [the status of the list]
    printf '%s' "${2:-0}" >"${WORK}/list.status"
    update_step
    [ "$(calls)" = "casaos module list|" ] || fail "$1: something else than the list was asked: $(calls)"
    case "$(summary)" in "[3] No module was updated: 'casaos module list' did not give the list of the modules (exit status ${2:-0}). Each module keeps the version it has, and the Modules window offers the update.") ;; *) fail "$1: not said: $(summary)" ;; esac
}
ufresh
: >"${WORK}/list.out"
bad_list "an empty list"
ufresh
printf 'hello\n"name":"virt","update":{"available":true,\n' >"${WORK}/list.out"
bad_list "a text that is not a list"
ufresh
list_says "$(entry virt newer)"
bad_list "a list with a status that is not 0" 1
ufresh
list_says "$(entry virt newer)"
bad_list "a list with a status of 3" 3
pass "a list that is empty, is not a list, or ends with a status that is not 0, updates no module"

# what a string of the list says is never a key: a description that holds the words of the keys, escaped as JSON escapes them
ufresh
list_says "$(entry virt same 'x\"update\":{\"available\":true,\"installed\":\"v0.0.1\"}'),$(entry alpha newer 'see \"name\":\"beta\",')"
update_step
[ "$(calls)" = "casaos module list|casaos module update alpha --yes|" ] || fail "the words of a string were taken for keys: $(calls)"
# blanks between the keys, the colons and the values are taken as the list's words too
ufresh
printf '{"catalogue": {"ok": true}, "modules": [{"name": "virt", "installed": {"version": "v0.2.1"}, "update": {"available": true, "installed": "v0.2.1", "latest": "v0.2.2"}}, {"name": "other", "update": {"available": false}}]}\n' >"${WORK}/list.out"
update_step
[ "$(calls)" = "casaos module list|casaos module update virt --yes|" ] || fail "a list with blanks: $(calls)"
# a long list: 20000 modules that are not installed, and one at the end with a later version
ufresh
awk 'BEGIN {
    printf "{\"catalogue\":{\"ok\":true},\"modules\":["
    for (i = 0; i < 20000; i++) printf "{\"name\":\"m%d\",\"title\":\"T\",\"description\":\"%0100d\",\"tier\":\"core\",\"tag\":\"v0.2.2\",\"catalogue\":true,\"installed\":null,\"system\":{\"supported\":true,\"reasons\":[]}},", i, 0
}' >"${WORK}/list.out"
printf '%s]}\n' "$(entry last newer)" >>"${WORK}/list.out"
[ "$(wc -c <"${WORK}/list.out")" -gt 4000000 ] || fail "the long list is not long"
update_step
[ "$(calls)" = "casaos module list|casaos module update last --yes|" ] || fail "a long list: $(calls)"
pass "a string is never read as a key, blanks are allowed, and a long list is read whole"

# the names: only one that is a module name is ever given to the assistant, and each once
ufresh
hostile=""
for bad in Virt 1virt -virt virt_x virt.x 'a;id' '$(id)' '`id`' '../x' 'a b' ' virt' 'virt ' 'a{b' 'a}b' abcdefghijklmnopqrstuvwxyz0123456 'a\"b' 'vért' '' 'a%sb' '-' "$(printf 'a\033[31m')" "$(printf 'virt\tx')"; do
    hostile="${hostile}$(entry "${bad}" newer),"
done
list_says "${hostile}$(entry good newer),$(entry good newer),$(entry abcdefghijklmnopqrstuvwxyz012345 newer)"
update_step
[ "$(calls)" = "casaos module list|casaos module update good --yes|casaos module update abcdefghijklmnopqrstuvwxyz012345 --yes|" ] || fail "a name that is not a module name was given to the assistant, or one was given twice: $(calls)"
case "${out}" in *"$(printf '\033')"*) fail "an escape sequence of a name of the list reached the screen" ;; esac
pass "of the names of the list, only module names reach the assistant, each once"

# with sudo: the list, the update and the removal of the temporary directories go through it, with none of the variables of the hooks
ufresh
list_says "$(entry virt newer)"
hooks_on
sudo_cmd=sudo
SUDO_OK=1
export SUDO_OK
update_step
sudo_cmd=""
unset SUDO_OK
hooks_off
case "$(summary)" in *"[0] The module virt is updated."*) ;; *) fail "with sudo: not updated: ${out}" ;; esac
[ ! -s "${UNPRIV}" ] || fail "with sudo: something ran without it: $(tr '\n' '|' <"${UNPRIV}")"
[ ! -s "${ENVSEEN}" ] || fail "with sudo: the assistant was given variables of the hooks: $(tr '\n' ' ' <"${ENVSEEN}")"
[ "$(wc -l <"${SUDOCALLS}" | tr -d ' ')" -eq 4 ] || fail "with sudo: not the list, a removal, the update and a removal: $(tr '\n' '|' <"${SUDOCALLS}")"
case "$(sed -n 1p "${SUDOCALLS}")" in "sudo env"*" -u CASAOS_MODULE_TEST "*" ${CASA_BIN} module list") ;; *) fail "with sudo: the list is not 'sudo env -u ... casaos module list': $(sed -n 1p "${SUDOCALLS}")" ;; esac
case "$(sed -n 3p "${SUDOCALLS}")" in "sudo env"*" -u CASAOS_MODULE_TEST "*" ${CASA_BIN} module update virt --yes") ;; *) fail "with sudo: the update is not 'sudo env -u ... casaos module update virt --yes': $(sed -n 3p "${SUDOCALLS}")" ;; esac
for n in 2 4; do
    case "$(sed -n "${n}p" "${SUDOCALLS}")" in "sudo rm -rf ${TMPDIR}/"*) ;; *) fail "with sudo: a temporary directory is not removed through it: $(tr '\n' '|' <"${SUDOCALLS}")" ;; esac
done
pass "with sudo, the list, the update and the removals go through it, and the hooks are still taken out"

# removals of the temporary directories that fail (through sudo): the install goes on all the same, for a list that is one and one that is not. This is
# run as install.sh runs it, by bash with set -e at its top and the step a command of its own: in the subshells above, a part of "out=$(...) || ...",
# bash does not stop on an error whatever set -e says, so they cannot tell
sete_step() { # Update_Installed_Modules as install.sh runs it, through sudo
    {
        echo 'set -e'
        cat "${FUNCTIONS}"
        echo 'Running_Under_Update_Unit() { return 1; }'
        echo 'GreyStart() { :; }; ColorReset() { :; }; Show() { echo "[$1] $2"; }'
        for v in CASA_BIN CASA_RUN_DIR CASAOS_UPDATE_LOG CATALOGUE_SHA CASAOS_MODULES_MIN_CORE_TAG CASAOS_MODULE_UPDATE_MIN_CORE_TAG; do
            eval "printf '%s=%s\n' \"\${v}\" \"'\${${v}}'\""
        done
        echo 'sudo_cmd=sudo NO_MODULE_UPDATE=0 MODULE_CATALOGUE_READY=1 CORE_INSTALLED_NOW=1 CASAOS_MODULE_CATALOGUE_SHA256="${CATALOGUE_SHA}"'
        echo 'Update_Installed_Modules'
        echo 'echo "the install goes on"'
    } >"${WORK}/sete.bash"
    status=0
    out="$(bash "${WORK}/sete.bash" </dev/null 2>&1)" || status=$?
}
# (and that run does stop on an error: the check can say no)
printf 'set -e\nfalse\necho "the install goes on"\n' >"${WORK}/sete-false.bash"
[ "$(bash "${WORK}/sete-false.bash" 2>&1 || true)" = "" ] || fail "bash with set -e at its top does not stop on an error here"
for listcase in good bad; do
    ufresh
    if [ "${listcase}" = good ]; then list_says "$(entry virt newer)"; else : >"${WORK}/list.out"; fi
    SUDO_OK=1
    SUDO_RM_FAILS=1
    export SUDO_OK SUDO_RM_FAILS
    sete_step
    unset SUDO_OK SUDO_RM_FAILS
    [ "${status}" -eq 0 ] && case "${out}" in *"the install goes on") true ;; *) false ;; esac || fail "removals that fail (${listcase} list): the install ended (status ${status}): ${out}"
    grep -q '^sudo rm -rf ' "${SUDOCALLS}" || fail "removals that fail (${listcase} list): none was tried: $(tr '\n' '|' <"${SUDOCALLS}")"
    case "${listcase}:$(summary)" in good:*"[0] The module virt is updated."* | bad:*"did not give the list of the modules"*) ;; *) fail "removals that fail (${listcase} list): $(summary)" ;; esac
done
pass "a removal of a temporary directory that fails does not end the install, run with set -e as install.sh runs"

# the run the core started (casaos-update.service): the assistant would refuse the update there (busy), so it is handed to a unit of its own
handed_off() { # Update_Installed_Modules inside casaos-update.service, with a directory for the script
    mkdir -p "${CASA_RUN_DIR}"
    UNDER_UPDATE_UNIT=1
    update_step
    unset UNDER_UPDATE_UNIT
}
sdrun_words() { # <first> <last>: the words given to systemd-run, as systemd makes them, joined by |
    words=""
    n="$1"
    while [ "${n}" -le "$2" ]; do
        words="${words}$(cat "${WORK}/sdrun/${n}")|"
        n=$((n + 1))
    done
    printf '%s' "${words}"
}
ufresh
list_says "$(entry virt newer),$(entry beta newer),$(entry other same)"
handed_off
! grep -q '^casaos module update' "${CALLS}" || fail "the hand-off: a module was updated while casaos-update.service runs: $(calls)"
[ "$(grep -c '^systemd-run ' "${CALLS}")" -eq 1 ] || fail "the hand-off: not one unit: $(calls)"
# systemd takes ${NAME}, $NAME, $$ and % in the words of a unit's command for its own: no word given to it holds one
[ ! -e "${WORK}/sdrun/changed" ] || fail "the hand-off: systemd would change the words $(tr '\n' ' ' <"${WORK}/sdrun/changed")of the unit's command"
! grep -l '[$%]' "${WORK}"/sdrun/*.raw >/dev/null 2>&1 || fail "the hand-off: a word given to systemd-run holds a \$ or a %"
[ "$(sdrun_words 1 7)" = "--quiet|--collect|--unit=casaos-update-modules|--property=Type=exec|--property=KillMode=mixed|--property=TimeoutStopSec=420|/bin/sh|" ] ||
    fail "the hand-off: the unit is not started as it should be: $(sdrun_words 1 7)"
SCRIPT_FILE="$(cat "${WORK}/sdrun/8")"
case "${SCRIPT_FILE}" in "${CASA_RUN_DIR}"/casaos-update-modules.??????/run.sh) ;; *) fail "the hand-off: the script is not a file of a directory of its own in ${CASA_RUN_DIR}: ${SCRIPT_FILE}" ;; esac
[ -f "${SCRIPT_FILE}" ] && [ "$(ls -A "${SCRIPT_FILE%/*}")" = run.sh ] || fail "the hand-off: the script is not there, alone in its directory"
# (mktemp -d makes it 0700; a Windows checkout's file system does not keep the mode)
[ "$(uname -s)" != Linux ] || [ "$(stat -c %a "${SCRIPT_FILE%/*}")" = 700 ] || fail "the hand-off: the directory of the script is $(stat -c %a "${SCRIPT_FILE%/*}"), not 700"
[ "$(sdrun_words 9 12)" = "${CASAOS_UPDATE_LOG}|${CASA_BIN}|virt|beta|" ] && [ ! -e "${WORK}/sdrun/13" ] ||
    fail "the hand-off: the unit is not given the log, the core and the names, and nothing else: $(sdrun_words 9 13)"
! grep -q -e '--setenv' "${WORK}"/sdrun/* || fail "the hand-off: the unit is given variables"
case "$(summary)" in "[2] The modules virt beta will be updated once this update has finished, by casaos-update-modules.service: what they say is added to ${CASAOS_UPDATE_LOG}, and the Modules window shows the version each one has.") ;; *) fail "the hand-off: not said: $(summary)" ;; esac
cp "${SCRIPT_FILE}" "${WORK}/run.sh.saved"
# what the unit runs: it waits while casaos-update.service runs, then updates each module in a unit of its own, with no input, in the log, whatever one
# of them does, and takes its script away
mkdir -p "${WORK}/handoff-bin"
cat >"${WORK}/handoff-bin/systemctl" <<'STUB'
#!/bin/sh
# answers ACTIVE_WORD (active) for the first ACTIVE_FOR questions, then inactive
echo "systemctl $*" >>"${WORK}/systemctl.calls"
n="$(wc -l <"${WORK}/systemctl.calls")"
if [ "${n}" -le "${ACTIVE_FOR:-0}" ]; then echo "${ACTIVE_WORD:-active}"; else echo inactive; fi
STUB
cat >"${WORK}/handoff-bin/sleep" <<'STUB'
#!/bin/sh
echo "sleep $*" >>"${WORK}/sleep.calls"
STUB
cat >"${WORK}/handoff-bin/systemd-run" <<'STUB'
#!/bin/sh
# stand-in for the systemd-run of the unit's script: its words, one call a line, and a word that holds a $ or a % in inner.bad; then what follows
# "--" is run as --wait --pipe has it run: to its end, with this input and output, and its status is this one's
printf '%s|' "$@" >>"${WORK}/inner.calls"
echo >>"${WORK}/inner.calls"
for a in "$@"; do case "${a}" in *'$'* | *%*) echo "${a}" >>"${WORK}/inner.bad" ;; esac; done
while [ $# -gt 0 ] && [ "$1" != -- ]; do shift; done
shift
exec "$@"
STUB
chmod +x "${WORK}/handoff-bin/systemctl" "${WORK}/handoff-bin/sleep" "${WORK}/handoff-bin/systemd-run"
handoff_runs() { # <questions answered active> <the word> <names>...
    ACTIVE_FOR="$1" ACTIVE_WORD="$2"
    shift 2
    rm -rf "${WORK}/systemctl.calls" "${WORK}/sleep.calls" "${WORK}/inner.calls" "${WORK}/inner.bad" "${CASAOS_UPDATE_LOG}" "${WORK}/stdin-got" "${WORK}/rd"
    : >"${CALLS}"
    mkdir -p "$(dirname "${CASAOS_UPDATE_LOG}")" "${WORK}/rd"
    cp "${WORK}/run.sh.saved" "${WORK}/rd/run.sh"
    (
        export ACTIVE_FOR ACTIVE_WORD
        PATH="${WORK}/handoff-bin:${PATH}"
        export PATH
        printf 'not for the unit\n' | sh "${WORK}/rd/run.sh" "${CASAOS_UPDATE_LOG}" "${CASA_BIN}" "$@"
    ) >"${WORK}/handoff.out" 2>&1
    [ ! -s "${WORK}/handoff.out" ] || fail "the unit wrote elsewhere than in the log: $(cat "${WORK}/handoff.out")"
    [ ! -s "${WORK}/stdin-got" ] || fail "the unit gave the assistant input"
    [ ! -e "${WORK}/rd" ] || fail "the unit did not take its script and its directory away: $(ls -A "${WORK}/rd")"
    [ ! -e "${WORK}/inner.bad" ] || fail "a word given to systemd-run by the unit holds a \$ or a %: $(cat "${WORK}/inner.bad")"
}
inner_words() { sed 's/-[0-9][0-9]*|--property=Type=exec/-<time>|--property=Type=exec/' "${WORK}/inner.calls"; }
printf failed >"${MODES}/virt"
printf ok >"${MODES}/beta"
handoff_runs 2 active virt beta
[ "$(wc -l <"${WORK}/systemctl.calls" | tr -d ' ')" -eq 3 ] && [ "$(sort -u "${WORK}/systemctl.calls")" = "systemctl show casaos-update.service --property=ActiveState --value" ] ||
    fail "the unit did not ask casaos-update.service until it was gone: $(cat "${WORK}/systemctl.calls")"
[ "$(cat "${WORK}/sleep.calls")" = "sleep 2
sleep 2" ] || fail "the unit did not wait two seconds between its questions: $(cat "${WORK}/sleep.calls")"
# each module in a unit of its own, whose stop asks the assistant alone (KillMode=mixed) and gives it the time of its rollback, waited for
[ "$(inner_words)" = "--wait|--pipe|--collect|--unit=casaos-update-modules-virt-<time>|--property=Type=exec|--property=KillMode=mixed|--property=TimeoutStopSec=420|--|${CASA_BIN}|module|update|virt|--yes|
--wait|--pipe|--collect|--unit=casaos-update-modules-beta-<time>|--property=Type=exec|--property=KillMode=mixed|--property=TimeoutStopSec=420|--|${CASA_BIN}|module|update|beta|--yes|" ] ||
    fail "the unit does not update each module in a unit of its own as it should: $(cat "${WORK}/inner.calls")"
[ "$(calls)" = "casaos module update virt --yes|casaos module update beta --yes|" ] || fail "the unit did not update each module in turn: $(calls)"
case "$(cat "${CASAOS_UPDATE_LOG}")" in *"Updating the module virt..."*"failed at step download: download_failed"*"rolled back"*"The update of the module virt ended with exit status 3: the lines above say whether it is updated, and if not whether its old version was put back. To try again: the Modules window of the dashboard, or sudo casaos module update virt"*"Updating the module beta..."*"CASAOS_MODULE_RESULT - beta ok done - - "*) ;; *) fail "the log of the unit: $(cat "${CASAOS_UPDATE_LOG}")" ;; esac
handoff_runs 1 deactivating virt
[ "$(wc -l <"${WORK}/systemctl.calls" | tr -d ' ')" -eq 2 ] || fail "the unit did not wait for a casaos-update.service that is stopping"
handoff_runs 0 active virt
[ "$(wc -l <"${WORK}/systemctl.calls" | tr -d ' ')" -eq 1 ] && [ ! -e "${WORK}/sleep.calls" ] || fail "the unit waited for a casaos-update.service that is gone"
# bounded: a casaos-update.service that never ends is waited for 300 times 2 seconds, and the assistant is then asked all the same (it says busy itself)
handoff_runs 100000 active virt
[ "$(wc -l <"${WORK}/systemctl.calls" | tr -d ' ')" -eq 300 ] && [ "$(wc -l <"${WORK}/sleep.calls" | tr -d ' ')" -eq 300 ] || fail "the unit's wait is not 300 questions"
[ "$(calls)" = "casaos module update virt --yes|" ] || fail "after its wait the unit did not go on: $(calls)"
# a unit that cannot be started, no directory for the script, or a word that systemd would change: said, no module is updated, nothing is left
handoff_refused() { # <what>
    ! grep -q '^casaos module update' "${CALLS}" || fail "$1: a module was updated: $(calls)"
    case "$(summary)" in "[3] The modules virt were not updated: their update could not be handed to a unit of its own, and the assistant refuses one while this update runs. Each keeps its old version. Once this update has finished, update them from the Modules window of the dashboard, or with: sudo casaos module update <name>") ;; *) fail "$1: not said: $(summary)" ;; esac
    [ -z "$(ls -A "${CASA_RUN_DIR}" 2>/dev/null)" ] || fail "$1: a script was left: $(ls -A "${CASA_RUN_DIR}")"
}
ufresh
list_says "$(entry virt newer)"
SDRUN_STATUS=1
export SDRUN_STATUS
handed_off
unset SDRUN_STATUS
handoff_refused "a unit that cannot be started"
ufresh
list_says "$(entry virt newer)"
UNDER_UPDATE_UNIT=1
update_step
unset UNDER_UPDATE_UNIT
handoff_refused "no directory for the script"
! grep -q '^systemd-run ' "${CALLS}" || fail "no directory for the script: systemd-run was run"
for odd in 'ca$saos' 'ca%nsaos' 'ca${X}saos' 'ca$$saos'; do
    ufresh
    cp "${WORK}/bin/casaos" "${WORK}/bin/${odd}"
    CASA_BIN_SAVED="${CASA_BIN}"
    CASA_BIN="${WORK}/bin/${odd}"
    list_says "$(entry virt newer)"
    handed_off
    CASA_BIN="${CASA_BIN_SAVED}"
    rm -f "${WORK}/bin/${odd}"
    handoff_refused "a core at '${odd}'"
    ! grep -q '^systemd-run ' "${CALLS}" || fail "a core at '${odd}': a word with a \$ or a % was given to systemd-run"
done
pass "inside casaos-update.service the updates go to a unit of their own, from a script file and no word systemd would change, which waits for it, at most 10 minutes, and updates each module in a unit of its own"

# the whole sequence of the end of the install, under set -e: a module that fails its update does not change its status
ufresh
write_catalogue 8
CASAOS_MODULE_CATALOGUE_SHA256="$(sha_of "${CATALOGUE_FILE}")"
list_says "$(entry virt newer)"
printf failed >"${MODES}/virt"
status=0
out="$(
    exec 2>&1
    set -e
    MODULES_REQUESTED=""
    Install_Module_Catalogue
    Install_Modules
    Update_Installed_Modules
    echo finished
)" || status=$?
[ "${status}" -eq 0 ] && case "${out}" in *"The module virt was not updated"*finished) true ;; *) false ;; esac || fail "a failed update ended the install: ${status}: ${out}"
pass "catalogue, modules, then updates, and the install finishes whatever an update did"

# ---- the detached run --------------------------------------------------------

# An update that runs inside casaos.service hands itself to a transient unit that takes no options: the modules asked for
# with --module cannot reach it, and that is said, with the way to install them afterwards, instead of dropped in silence
# (Detach_From_CasaOS_Service is bash: the harness runs it under bash, against a stand-in for systemd-run)
detach() { # <the modules asked for> [NO_TELEMETRY] [NO_MODULE_UPDATE]
    fresh
    status=0
    out="$(bash "${WORK}/detach.bash" "$1" "${2:-0}" "${3:-0}" 2>&1)" || status=$?
}
extract Detach_From_CasaOS_Service >"${WORK}/detach.fn"
[ -s "${WORK}/detach.fn" ] || fail "install.sh defines no Detach_From_CasaOS_Service()"
{
    echo 'set -e'
    echo 'sudo_cmd=""'
    echo 'Show() { echo "[$1] $2"; if [ "$1" -eq 1 ]; then exit 1; fi; }'
    echo 'Running_Under_CasaOS_Service() { return 0; }'
    echo "CASAOS_UPDATE_LOG='${FAKE}/var/log/casaos/upgrade.log'"
    echo 'CASAOS_INSTALL_RELEASE_TAG=v0.0.0'
    echo 'CASAOS_INSTALLER_SELF_URL=https://example.invalid/install.sh'
    cat "${WORK}/detach.fn"
    echo 'MODULES_REQUESTED="$1" NO_TELEMETRY="$2" NO_MODULE_UPDATE="$3"'
    echo 'Detach_From_CasaOS_Service'
} >"${WORK}/detach.bash"
bash -n "${WORK}/detach.bash" || fail "the detach function does not parse"

detach "virt other"
[ "${status}" -eq 0 ] || fail "the detached update ended with ${status}: ${out}"
[ "$(grep -c '^systemd-run ' "${CALLS}")" -eq 1 ] || fail "the update was not handed to a unit, once: $(calls)"
grep '^systemd-run ' "${CALLS}" | grep -q -e '--module' && fail "the detached unit was given --module: $(calls)"
case "${out}" in *"[3] The update runs detached and takes no options, so it will NOT install the modules asked for with --module (virt other). Once it has finished, install them from the Modules page of the dashboard, or run: sudo casaos module install virt; sudo casaos module install other"*"Handing the update to "*) ;; *) fail "the modules that cannot be installed by the detached update are not said, before the hand-off, with the way to install them: ${out}" ;; esac
detach "virt"
case "${out}" in *"(virt). Once it has finished"*"run: sudo casaos module install virt"*) ;; *) fail "one module: not said: ${out}" ;; esac
case "${out}" in *"install virt;"*) fail "one module: a stray separator: ${out}" ;; esac
detach ""
[ "${status}" -eq 0 ] && [ "$(grep -c '^systemd-run ' "${CALLS}")" -eq 1 ] || fail "no module: the update was not handed over: ${status}: $(calls)"
case "${out}" in *"[3] "*) fail "no module asked for and something is said: ${out}" ;; esac
case "${out}" in *"[2] Handing the update to casaos-update-"*) ;; *) fail "no module: the hand-off is not said: ${out}" ;; esac
pass "the detached update says that it does not install the modules asked for, and how to install them"

# the detached run takes no options: the opt-outs reach it as variables of its unit, and only when asked
sdrun_args() { i=1; while [ -e "${WORK}/sdrun/${i}" ]; do cat "${WORK}/sdrun/${i}"; echo; i=$((i + 1)); done; }
detach "" 0 1
[ "${status}" -eq 0 ] && [ "$(sdrun_args | grep -c '^--setenv=RECASAOS_MODULE_UPDATE=0$')" -eq 1 ] || fail "--no-module-update does not reach the detached update: $(sdrun_args | tr '\n' ' ')"
sdrun_args | grep -q '^--setenv=RECASAOS_TELEMETRY' && fail "the detached update was given RECASAOS_TELEMETRY without being asked"
detach "" 1 1
[ "$(sdrun_args | grep -c '^--setenv=RECASAOS_MODULE_UPDATE=0$')" -eq 1 ] && [ "$(sdrun_args | grep -c '^--setenv=RECASAOS_TELEMETRY=0$')" -eq 1 ] || fail "both opt-outs do not reach the detached update: $(sdrun_args | tr '\n' ' ')"
detach "" 0 0
[ "$(sdrun_args | grep -c '^--setenv=')" -eq 1 ] && [ "$(sdrun_args | grep -c '^--setenv=CASAOS_INSTALLER_DETACHED=1$')" -eq 1 ] || fail "the detached update was given an opt-out without being asked: $(sdrun_args | tr '\n' ' ')"
pass "--no-module-update and --no-telemetry reach the detached update as variables, and only when asked"

echo "all module checks passed"
