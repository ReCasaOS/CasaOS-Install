#!/bin/sh
#
# Checks what casaos-uninstall does about the optional modules, without removing
# anything and without a network: the functions that answer are copied out of
# casaos-uninstall as it stands and run in this shell (run it with dash: that is
# the shell of the guests the CI proves) against stand-ins for the core's
# `casaos` (its version, and its `module remove` assistant) and a fake root, the
# uninstaller's own Show replaced by a recorder.
#
#   sh scripts/test-uninstall-modules.sh
#
# UNINSTALL_SH_UNDER_TEST=<path> checks another copy of casaos-uninstall (a
# mutant, to see that each check below can say no). The release workflow runs
# this before anything is built.

set -eu

# (nothing of the CASAOS_MODULE hooks that its caller has exported reaches the checks below)
for hook in $(env | sed -n 's/^\(CASAOS_MODULE[A-Za-z0-9_]*\)=.*/\1/p'); do unset "${hook}"; done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap '"${REAL_RM:-rm}" -rf "${WORK}"' EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() { echo "ok: $*"; }

# the scripts as the shell reads them: a Windows checkout has CRLF line endings
UNINSTALL_SH="${WORK}/casaos-uninstall"
tr -d '\r' <"${UNINSTALL_SH_UNDER_TEST:-${ROOT}/casaos-uninstall}" >"${UNINSTALL_SH}"
INSTALL_SH="${WORK}/install.sh"
tr -d '\r' <"${ROOT}/install.sh" >"${INSTALL_SH}"
bash -n "${UNINSTALL_SH}" || fail "casaos-uninstall does not parse"

line_of() { grep -n "$1" "${UNINSTALL_SH}" | head -n 1 | cut -d: -f1; }

# ---- what the uninstaller says is where the pieces are -----------------------

[ "$(grep -c '^readonly CASAOS_MODULES_MIN_CORE_TAG="__CASAOS_MODULES_MIN_CORE_TAG__"$' "${UNINSTALL_SH}")" -eq 1 ] ||
    fail "casaos-uninstall does not carry the oldest core that has 'module' as one placeholder for the release build to fill"
[ "$(grep -c '__CASAOS_MODULES_MIN_CORE_TAG__' "${UNINSTALL_SH}")" -eq 1 ] ||
    fail "the placeholder of the oldest core that has 'module' is named somewhere else than its assignment"
eval "$(grep -E '^readonly (CASA_MODULE_BIN|CASA_MODULE_MANIFESTS)=' "${UNINSTALL_SH}" | sed 's/^readonly //')"
[ "${CASA_MODULE_BIN}" = /usr/bin/casaos ] || fail "the assistant is run as ${CASA_MODULE_BIN}, not as /usr/bin/casaos"
[ "${CASA_MODULE_MANIFESTS}" = /etc/casaos/modules ] || fail "the modules are looked for in ${CASA_MODULE_MANIFESTS}, not in /etc/casaos/modules"
grep -Fq 'Run_Module_Assistant "${CASA_MODULE_BIN}" module remove "${name}" --yes </dev/null' "${UNINSTALL_SH}" ||
    fail "the module remove is not '/usr/bin/casaos module remove <name> --yes' with no input, run through Run_Module_Assistant"
[ "$(grep -c '"${CASA_MODULE_BIN}" module ' "${UNINSTALL_SH}")" -eq 1 ] || fail "the assistant is run in more than one place, or not through Run_Module_Assistant"
[ "$(grep -cE 'apt-get|dpkg|virsh' "${UNINSTALL_SH}")" -eq 0 ] || fail "the uninstaller names a command that purges: what it prints, it prints from the assistant"
pass "the placeholder, the manifests directory and the assistant are where the uninstall needs them"

# the order of the calls: the modules come off before ReCasaOS does, and what stays is said last
n_images="$(line_of '^Remove_Images$')"
n_modules="$(line_of '^Remove_Modules$')"
n_casaos="$(line_of '^Uninstall_Casaos$')"
n_notice="$(line_of '^Modules_Left_Notice$')"
[ -n "${n_images}" ] && [ -n "${n_modules}" ] && [ -n "${n_casaos}" ] && [ -n "${n_notice}" ] &&
    [ "${n_images}" -lt "${n_modules}" ] && [ "${n_modules}" -lt "${n_casaos}" ] && [ "${n_casaos}" -lt "${n_notice}" ] ||
    fail "the calls are not Remove_Images, Remove_Modules, Uninstall_Casaos, Modules_Left_Notice, in that order"
for f in Remove_Modules Modules_Left_Notice Uninstall_Casaos; do
    [ "$(grep -c "^${f}\$" "${UNINSTALL_SH}")" -eq 1 ] || fail "${f} is not called exactly once"
done
pass "the modules are removed before ReCasaOS, and what stays is said after it"

# ---- the functions, as casaos-uninstall has them -----------------------------

extract() { sed -n "/^$2() {\$/,/^}\$/p" "$1"; }

for f in Valid_Module_Name Version_Parts Version_At_Least Run_Module_Assistant; do
    [ -n "$(extract "${UNINSTALL_SH}" "${f}")" ] || fail "casaos-uninstall defines no ${f}()"
    [ "$(extract "${UNINSTALL_SH}" "${f}")" = "$(extract "${INSTALL_SH}" "${f}")" ] ||
        fail "${f}() is not the text that install.sh has: the two scripts must judge a name and a version in the same way"
done
pass "the name and the version are judged in the same words as install.sh judges them"

FUNCTIONS="${WORK}/functions.sh"
: >"${FUNCTIONS}"
for f in Valid_Module_Name Version_Parts Version_At_Least Run_Module_Assistant Remove_One_Module Remove_Modules Modules_Left_Notice; do
    body="$(extract "${UNINSTALL_SH}" "${f}")"
    [ -n "${body}" ] || fail "casaos-uninstall defines no ${f}()"
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

# what those functions take from the uninstaller's globals and helpers
sudo_cmd=""
CASAOS_MODULES_MIN_CORE_TAG=v0.4.75
MODULES_REMOVED=""
MODULES_LEFT=""
Show() { echo "[$1] $2"; }

# a fake root, a TMPDIR to see what is left behind, and stand-ins first in PATH
FAKE="${WORK}/root"
CALLS="${WORK}/calls"
VERCALLS="${WORK}/vercalls"
RMCALLS="${WORK}/rmcalls"
SUDOCALLS="${WORK}/sudocalls"
ENVSEEN="${WORK}/envseen"
MODES="${WORK}/modes"
TMPDIR="${WORK}/tmp"
mkdir -p "${WORK}/bin" "${MODES}" "${TMPDIR}"
CASA_MODULE_MANIFESTS="${FAKE}/etc/casaos/modules"
CASA_MODULE_BIN="${WORK}/bin/casaos"
export CALLS VERCALLS RMCALLS SUDOCALLS ENVSEEN WORK MODES TMPDIR

cat >"${WORK}/bin/casaos" <<'STUB'
#!/bin/sh
# stand-in for the core. `-v` is asked of every core: it says what $WORK/version.out holds
# (v0.4.75 when there is no such file) and ends with the status in $WORK/version.status.
# `module remove` is the module assistant: the call is recorded, with what it was given on
# its input, and it says what the mode of that module (a file of $MODES) says, in the words
# the core's own remove uses. A core older than the assistant would take `module` for the
# start of a server; it is recorded all the same.
if [ "${1:-}" = "-v" ]; then
    echo "casaos -v" >>"${VERCALLS}"
    cat >"${WORK}/v-stdin-got"
    if [ -f "${WORK}/version.out" ]; then cat "${WORK}/version.out"; else echo v0.4.75; fi
    exit "$(cat "${WORK}/version.status" 2>/dev/null || echo 0)"
fi
echo "casaos $*" >>"${CALLS}"
env | grep '^CASAOS_MODULE' >>"${ENVSEEN}" || true
echo "KEEP_ME=${KEEP_ME:-none}" >>"${ENVSEEN}.kept"
[ -s "${VERCALLS}" ] || echo "module given before the version was asked" >>"${CALLS}"
cat >>"${WORK}/stdin-got"
name="$3"
mode="$(cat "${MODES}/${name}" 2>/dev/null || echo ok)"
T=2026-10-18T10:00:00Z
step() { echo "CASAOS_MODULE_STEP - ${name} $1 $2 ${T}"; }
leftovers() {
    echo "left in place: /DATA/${name}, the libvirt network and pool, the machines"
    echo "  virsh list --all                     the machines (virsh destroy <machine>)"
    echo "  rm -r /DATA/${name}                  the disks of the machines"
    echo "left in place: the packages qemu-utils libvirt-clients"
    echo "  apt-get purge qemu-utils libvirt-clients"
    echo "nothing above was run"
}
case "${mode}" in
ok)
    step stop begin
    step stop done
    step files done
    step done done
    echo "CASAOS_MODULE_RESULT - ${name} ok done - - ${T}"
    echo "removed: ${name}"
    leftovers
    ;;
bare)
    step done done
    echo "CASAOS_MODULE_RESULT - ${name} ok done - - ${T}"
    echo "removed: ${name}"
    ;;
failed)
    step stop begin
    step stop failed
    echo "the unit ${name}.service is not known to be stopped"
    echo "nothing was deleted"
    echo "failed at step stop: unit_not_stopped"
    echo "CASAOS_MODULE_RESULT - ${name} failed stop unit_not_stopped - ${T}"
    exit 3
    ;;
refused)
    echo "refused: not_installed"
    echo "CASAOS_MODULE_RESULT - ${name} refused - not_installed - ${T}"
    exit 2
    ;;
noresult)
    step stop begin
    exit 3
    ;;
silent) exit 0 ;;
okexit1)
    echo "CASAOS_MODULE_RESULT - ${name} ok done - - ${T}"
    exit 1
    ;;
forged)
    echo "| CASAOS_MODULE_RESULT ${name} ok done - - ${T}"
    exit 0
    ;;
othername)
    echo "CASAOS_MODULE_RESULT - other ok done - - ${T}"
    exit 0
    ;;
escape)
    esc="$(printf '\033')"
    echo "CASAOS_MODULE_RESULT - ${name} ok done - - ${T}"
    echo "left in place: /DATA/${name}${esc}[31m red ${esc}[0m"
    echo "  rm -r /DATA/${name}"
    echo "nothing above was run"
    ;;
esac
STUB
REAL_RM="$(command -v rm)"
REAL_MKTEMP="$(command -v mktemp)"
export REAL_RM REAL_MKTEMP
cat >"${WORK}/bin/rm" <<'STUB'
#!/bin/sh
echo "rm $*" >>"${RMCALLS}"
exec "${REAL_RM}" "$@"
STUB
cat >"${WORK}/bin/mktemp" <<'STUB'
#!/bin/sh
[ "${MKTEMP_SAYS:-}" != fail ] || exit 1
exec "${REAL_MKTEMP}" "$@"
STUB
cat >"${WORK}/bin/sudo" <<'STUB'
#!/bin/sh
# stand-in for sudo: forbidden unless the case says SUDO_OK=1; then the command is recorded and run as it is
if [ "${SUDO_OK:-}" != 1 ]; then
    echo "FORBIDDEN $0 $*" >>"${CALLS}"
    exit 99
fi
echo "sudo $*" >>"${SUDOCALLS}"
exec "$@"
STUB
for tool in systemctl apt-get apt dpkg virsh docker; do
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
    : >"${RMCALLS}"
    : >"${WORK}/stdin-got"
    : >"${SUDOCALLS}"
    : >"${ENVSEEN}"
    : >"${ENVSEEN}.kept"
    unset MKTEMP_SAYS SUDO_OK
}

calls() { sort "${CALLS}" | tr '\n' '|'; }

manifest() { # <names...>: the manifests of installed modules
    mkdir -p "${CASA_MODULE_MANIFESTS}"
    for n in "$@"; do printf '{}\n' >"${CASA_MODULE_MANIFESTS}/${n}.json"; done
}

say_version() { # <what `casaos -v` prints, exactly> [its status]
    printf '%s' "$1" >"${WORK}/version.out"
    printf '%s' "${2:-0}" >"${WORK}/version.status"
}

# uninstall_part: what the uninstall does about the modules, in the order it does it, under set -e, with
# what is left of the terminal on its input; the lines of the rest of the uninstall are one line
uninstall_part() {
    status=0
    out="$(
        exec 2>&1
        set -e
        MODULES_REMOVED="" MODULES_LEFT=""
        printf 'REST OF THE INPUT\n' | {
            Remove_Modules
            echo "the uninstall goes on"
            Modules_Left_Notice
            read -r left || left="(nothing left)"
            echo "input: ${left}"
        }
    )" || status=$?
}
summary() { printf '%s\n' "${out}" | grep '^\[[0-3]\]' || true; }

no_leftover() { # <what>
    [ -z "$(ls -A "${TMPDIR}")" ] || fail "$1: a temporary directory was left: $(ls -A "${TMPDIR}")"
}

# nothing purges: every rm the functions made was the one of their own temporary directory
purged_nothing() { # <what>
    if grep -q FORBIDDEN "${CALLS}"; then fail "$1: something that purges was run: $(calls)"; fi
    if grep -v "^rm -rf ${TMPDIR}/" "${RMCALLS}" | grep -q .; then fail "$1: something was removed that is not a temporary directory: $(cat "${RMCALLS}")"; fi
}

# ---- the versions, in the copy the uninstaller has ---------------------------

at_least() { # <what> <yes|no> <version> <minimum>
    if Version_At_Least "$3" "$4" >/dev/null 2>&1; then got=yes; else got=no; fi
    [ "${got}" = "$2" ] || fail "$1: '$3' against '$4': wanted $2, got ${got}"
}

at_least "the minimum itself" yes v0.4.75 v0.4.75
at_least "a later minor, of two digits" yes v0.10.0 v0.4.75
at_least "a later patch, of three digits" yes v0.4.100 v0.4.75
at_least "a later major" yes v1.0.0 v0.4.75
at_least "the patch before" no v0.4.74 v0.4.75
at_least "one digit is a number, not a text" no v0.4.9 v0.4.75
at_least "an older minor, whatever its patch" no v0.3.999 v0.4.75
for bad in "" v v0.4 0.4.75 v0.4.75-rc1 v0.0.0-dev "v0.4.75 " "v0.4.75
v0.4.75" v0.4.075 v0.4.75.1 '$(id)'; do
    at_least "a text that is not a version, as the version" no "${bad}" v0.4.75
    at_least "a text that is not a version, as the minimum" no v9.9.9 "${bad}"
done
at_least "the unfilled placeholder as the minimum" no v9.9.9 __CASAOS_MODULES_MIN_CORE_TAG__
pass "a version is three numbers, compared as numbers, and anything else is no version"

# ---- no module installed, nothing said ---------------------------------------

fresh
uninstall_part
[ "${status}" -eq 0 ] && [ "${out}" = "the uninstall goes on
input: REST OF THE INPUT" ] || fail "no manifests directory: something happened: ${status}: ${out}"
[ ! -s "${CALLS}" ] && [ ! -s "${VERCALLS}" ] || fail "no manifests directory: the core was run: $(calls)"
fresh
mkdir -p "${CASA_MODULE_MANIFESTS}"
uninstall_part
[ "${status}" -eq 0 ] && [ "${out}" = "the uninstall goes on
input: REST OF THE INPUT" ] && [ ! -s "${CALLS}" ] && [ ! -s "${VERCALLS}" ] || fail "an empty manifests directory: something happened: ${out}"
# what is in the directory and is not the manifest of a module: not given to the assistant
fresh
manifest Bad "a.b" "-rf" "x y" UP 1a a_b "" abcdefghijklmnopqrstuvwxyz0123456 'a;id' '$(id)'
mkdir -p "${CASA_MODULE_MANIFESTS}/dir.json"
printf 'text\n' >"${CASA_MODULE_MANIFESTS}/README.txt"
printf 'text\n' >"${CASA_MODULE_MANIFESTS}/alpha.json.bak"
printf 'text\n' >"${CASA_MODULE_MANIFESTS}/alpha.conf"
uninstall_part
[ "${status}" -eq 0 ] && [ "${out}" = "the uninstall goes on
input: REST OF THE INPUT" ] && [ ! -s "${CALLS}" ] && [ ! -s "${VERCALLS}" ] || fail "files that are not the manifests of modules were taken for them: ${out} / $(calls)"
pass "with no module installed the uninstall says nothing about modules and runs nothing"

# ---- modules installed: removed, with the assistant ---------------------------

fresh
manifest alpha beta Bad "x y"
uninstall_part
[ "${status}" -eq 0 ] || fail "two modules: status ${status}: ${out}"
[ "$(calls)" = "casaos module remove alpha --yes|casaos module remove beta --yes|" ] || fail "two modules: the assistant is not run as 'casaos module remove <name> --yes', once for each: $(calls)"
[ "$(cat "${VERCALLS}")" = "casaos -v" ] || fail "two modules: the version was not asked exactly once"
[ ! -s "${WORK}/stdin-got" ] && [ ! -s "${WORK}/v-stdin-got" ] || fail "two modules: the core was given input"
case "${out}" in *"input: REST OF THE INPUT"*) ;; *) fail "two modules: the input of the uninstall was consumed: ${out}" ;; esac
for m in alpha beta; do
    case "${out}" in *"CASAOS_MODULE_STEP - ${m} stop done 2026"*) ;; *) fail "two modules: the lines of the remove of ${m} are not shown: ${out}" ;; esac
    case "$(summary)" in *"[0] The module ${m} is removed."*) ;; *) fail "two modules: ${m} not said removed: $(summary)" ;; esac
done
case "$(summary)" in *"[3] The module"*) fail "two modules: a notice about a module that went well: $(summary)" ;; esac
no_leftover "two modules"
purged_nothing "two modules"
# what stays, said last, with the commands printed and none run
case "${out}" in *"the uninstall goes on"*"[3] Left on this machine, on purpose, by the removal of the modules (alpha beta): the packages they needed, and their data (for example /DATA/VM, the disks of the virtual machines). Nothing of it was purged."*"did not run any of them."*) ;; *) fail "two modules: what stays is not said, last: ${out}" ;; esac
tail_of_out="$(printf '%s\n' "${out}" | sed -n '/^the uninstall goes on$/,$p')"
for want in "  alpha:" "    left in place: /DATA/alpha, the libvirt network and pool, the machines" "      rm -r /DATA/alpha" "  beta:" "      rm -r /DATA/beta" "      apt-get purge qemu-utils libvirt-clients" "    nothing above was run"; do
    case "${tail_of_out}" in *"${want}"*) ;; *) fail "two modules: the closing words do not hold '${want}': ${tail_of_out}" ;; esac
done
pass "each module is removed with 'casaos module remove <name> --yes', and what stays is said last, its commands printed and none run"

# The assistant is given none of the CASAOS_MODULE variables of this environment (the test hooks of the core: a catalogue and
# an archive of the caller's own, in place of the pinned ones), and the uninstaller runs as root with whatever its caller
# exported. Nothing else of the environment is taken away.
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
manifest alpha
hooks_on
uninstall_part
hooks_off
[ "${status}" -eq 0 ] && [ "$(calls)" = "casaos module remove alpha --yes|" ] || fail "with the hooks exported the assistant was not run as it is: ${status}: $(calls)"
[ ! -s "${ENVSEEN}" ] || fail "the assistant was given variables of the hooks: $(tr '\n' ' ' <"${ENVSEEN}")"
[ "$(cat "${ENVSEEN}.kept")" = "KEEP_ME=kept" ] || fail "the rest of the environment was not left as it was: $(cat "${ENVSEEN}.kept")"
case "$(summary)" in *"[0] The module alpha is removed."*) ;; *) fail "with the hooks exported the module was not removed: $(summary)" ;; esac
# (and the stand-in does see a variable when it is not scrubbed: the check above can say no)
fresh
KEEP_ME=kept CASAOS_MODULE_TEST=1 "${CASA_MODULE_BIN}" module remove alpha --yes </dev/null >/dev/null
[ "$(cat "${ENVSEEN}")" = "CASAOS_MODULE_TEST=1" ] || fail "the stand-in does not record the variables it is given"

# with sudo: the assistant is run through it, still with none of the variables
fresh
manifest alpha
hooks_on
sudo_cmd=sudo
SUDO_OK=1
export SUDO_OK
uninstall_part
sudo_cmd=""
unset SUDO_OK
hooks_off
[ "${status}" -eq 0 ] || fail "with sudo: status ${status}: ${out}"
case "$(summary)" in *"[0] The module alpha is removed."*) ;; *) fail "with sudo: not removed: $(summary)" ;; esac
[ "$(wc -l <"${SUDOCALLS}" | tr -d ' ')" -eq 1 ] || fail "with sudo: not the assistant alone: $(tr '\n' '|' <"${SUDOCALLS}")"
line="$(sed -n 1p "${SUDOCALLS}")"
case "${line}" in "sudo env "*" ${CASA_MODULE_BIN} module remove alpha --yes") ;; *) fail "with sudo: the assistant is not run as 'sudo env ... casaos module remove alpha --yes': ${line}" ;; esac
for v in ${HOOKS}; do
    case "${line}" in *" -u ${v} "*) ;; *) fail "with sudo: ${v} is not taken out of the environment of the assistant: ${line}" ;; esac
done
case "${line}" in *KEEP_ME*) fail "with sudo: a variable that is not of the hooks is taken out: ${line}" ;; esac
[ ! -s "${ENVSEEN}" ] || fail "with sudo: the assistant was given variables of the hooks: $(tr '\n' ' ' <"${ENVSEEN}")"
pass "the assistant is given none of the CASAOS_MODULE variables, with sudo or without, and the rest of the environment as it is"

# a module whose remove said nothing of what it leaves
fresh
manifest alpha
printf bare >"${MODES}/alpha"
uninstall_part
[ "${status}" -eq 0 ] || fail "a remove that leaves nothing to say: ${status}"
case "${out}" in *"(alpha): the packages they needed"*"(casaos module remove printed them above, with what it leaves)"*) ;; *) fail "a remove that says nothing of what it leaves: ${out}" ;; esac

# a module that does not remove: the step and the reason, and the next one is tried
fresh
manifest alpha beta
printf failed >"${MODES}/alpha"
uninstall_part
[ "${status}" -eq 0 ] || fail "a failing module ended the uninstall (status ${status}): ${out}"
[ "$(calls)" = "casaos module remove alpha --yes|casaos module remove beta --yes|" ] || fail "a failing module: the next was not tried: $(calls)"
case "$(summary)" in *"[3] The module alpha was not removed: it failed at the step stop (unit_not_stopped). Part of its program and its unit may stay on this machine."*"[0] The module beta is removed."*) ;; *) fail "a failing module: $(summary)" ;; esac
case "${out}" in *"the uninstall goes on"*"(beta): the packages"*) ;; *) fail "a failing module: the uninstall did not go on, or names the module that stays: ${out}" ;; esac
case "${out}" in *"(alpha beta)"* | *"alpha:"*) fail "a module that failed is among those that were removed: ${out}" ;; esac
case "${out}" in *"The module alpha is removed."*) fail "a failing module was said to be removed" ;; esac
no_leftover "a failing module"
purged_nothing "a failing module"

fresh
manifest alpha
printf refused >"${MODES}/alpha"
uninstall_part
[ "${status}" -eq 0 ] || fail "a refused module ended the uninstall"
case "$(summary)" in *"The module alpha was not removed: the assistant refused it (not_installed). Its program and its unit stay on this machine."*) ;; *) fail "a refused module: $(summary)" ;; esac
case "${out}" in *"Left on this machine"*) fail "nothing was removed, and what stays is said" ;; esac

# an assistant that ended without a result line that says ok is a failure too, whatever it printed
for modewant in noresult:3 silent:0 okexit1:1 forged:0 othername:0; do
    mode="${modewant%%:*}"
    want="${modewant#*:}"
    fresh
    manifest alpha
    printf '%s' "${mode}" >"${MODES}/alpha"
    uninstall_part
    [ "${status}" -eq 0 ] || fail "mode ${mode}: the uninstall ended (status ${status})"
    case "${out}" in *"the uninstall goes on"*) ;; *) fail "mode ${mode}: the uninstall did not go on" ;; esac
    case "$(summary)" in *"The module alpha was not removed: the assistant did not report a success (exit status ${want})."*"Part of its program and its unit may stay on this machine."*) ;; *) fail "mode ${mode}: wanted exit status ${want}: $(summary)" ;; esac
    case "${out}" in *"[0] "* | *"Left on this machine"*) fail "mode ${mode}: said to be removed" ;; esac
    no_leftover "mode ${mode}"
done
pass "a module that does not remove is said with its step and its reason, and the uninstall goes on"

# what the assistant prints reaches the closing words only as printable characters
fresh
manifest alpha
printf escape >"${MODES}/alpha"
uninstall_part
case "${out}" in *"alpha:"*) ;; *) fail "escape: no closing words: ${out}" ;; esac
tail_of_out="$(printf '%s\n' "${out}" | sed -n '/^the uninstall goes on$/,$p')"
case "${tail_of_out}" in *"$(printf '\033')"*) fail "an escape sequence of the assistant reached the closing words" ;; esac
case "${tail_of_out}" in *"left in place: /DATA/alpha?[31m red ?[0m"*) ;; *) fail "the closing words do not show the line as printable characters: ${tail_of_out}" ;; esac

# no temporary directory: the module is not removed, and the uninstall goes on
fresh
manifest alpha
MKTEMP_SAYS=fail
export MKTEMP_SAYS
uninstall_part
unset MKTEMP_SAYS
[ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] || fail "no temporary directory: ${status}: $(calls)"
case "$(summary)" in *"The module alpha was not removed: no temporary directory could be made."*) ;; *) fail "no temporary directory: not said: $(summary)" ;; esac
case "${out}" in *"the uninstall goes on"*) ;; *) fail "no temporary directory: the uninstall did not go on" ;; esac
pass "an escape sequence is shown as printable characters, and a missing temporary directory stops that module only"

# ---- the core that is given `module` -----------------------------------------

core_runs() { # <what> <what -v prints> [its status]
    fresh
    manifest alpha
    say_version "$2" "${3:-0}"
    uninstall_part
    [ "${status}" -eq 0 ] || fail "$1: status ${status}: ${out}"
    [ "$(calls)" = "casaos module remove alpha --yes|" ] || fail "$1: the assistant was not run, once: $(calls)"
    [ "$(cat "${VERCALLS}")" = "casaos -v" ] || fail "$1: the version was not asked exactly once"
}

core_refused() { # <what> <what -v prints> [its status]: the core is never given `module`, and the uninstall goes on
    fresh
    manifest alpha beta
    say_version "$2" "${3:-0}"
    uninstall_part
    [ "${status}" -eq 0 ] || fail "$1: the uninstall ended (status ${status}): ${out}"
    [ ! -s "${CALLS}" ] || fail "$1: the core was given 'module' (an older one would have started as a server): $(calls)"
    [ "$(cat "${VERCALLS}")" = "casaos -v" ] || fail "$1: the version was not asked exactly once"
    [ ! -s "${WORK}/v-stdin-got" ] || fail "$1: -v was given input"
    case "$(summary)" in *"[3] The modules (alpha beta) were not removed: ${CASA_MODULE_BIN} did not report a version of v0.4.75 or later"*"Their programs and their units stay on this machine."*) ;; *) fail "$1: not said: $(summary)" ;; esac
    case "$(summary)" in *"$(printf '\033')"*) fail "$1: an escape sequence reached the screen" ;; esac
    [ "$(summary | wc -c)" -lt 600 ] || fail "$1: what the core printed reached the screen at length"
    case "${out}" in *"the uninstall goes on"*"input: REST OF THE INPUT") ;; *) fail "$1: the uninstall did not go on, or its input was consumed: ${out}" ;; esac
    case "${out}" in *"Left on this machine"*) fail "$1: nothing was removed, and what stays is said" ;; esac
    no_leftover "$1"
}

core_runs "the minimum" v0.4.75
core_runs "a later patch" v0.4.76
core_runs "a later minor, of two digits" v0.10.0
core_runs "a later major" v1.0.0
core_runs "a line end after the version" 'v0.4.75
'
core_refused "an older patch" v0.4.74
core_refused "an older patch of one digit" v0.4.9
core_refused "an older minor" v0.3.999
core_refused "a private build" v0.0.0-dev
core_refused "a core that says nothing" ''
core_refused "a word" hello
core_refused "two numbers" v0.4
core_refused "no v" 0.4.75
core_refused "a suffix" v0.4.75-rc1
core_refused "two lines" 'v0.4.75
v0.4.75'
core_refused "an escape sequence" "$(printf '\033[31mv0.4.74')"
core_refused "a long text" "$(printf 'v0.4.74%0500d' 0)"
core_refused "a version that is right, with a status that is not" v0.4.75 1
core_refused "no version, with a status of 127" '' 127
pass "a core whose version is older, or is not a version, is never given 'module'; the modules that stay are said"

# no core at all, or one that is not an executable
fresh
manifest alpha
CASA_MODULE_BIN_SAVED="${CASA_MODULE_BIN}"
CASA_MODULE_BIN="${WORK}/bin/not-there"
uninstall_part
[ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] && [ ! -s "${VERCALLS}" ] || fail "no core: something was run: $(calls)"
case "$(summary)" in *"[3] The modules (alpha) were not removed: ${CASA_MODULE_BIN} is not there to remove them. Their programs and their units stay on this machine."*) ;; *) fail "no core: not said: $(summary)" ;; esac
case "${out}" in *"the uninstall goes on"*"input: REST OF THE INPUT") ;; *) fail "no core: the uninstall did not go on" ;; esac
printf 'text\n' >"${WORK}/bin/not-executable"
CASA_MODULE_BIN="${WORK}/bin/not-executable"
uninstall_part
[ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] || fail "a core that is not an executable: something was run: $(calls)"
case "$(summary)" in *"is not there to remove them"*) ;; *) fail "a core that is not an executable: not said: $(summary)" ;; esac
CASA_MODULE_BIN="${CASA_MODULE_BIN_SAVED}"

# the minimum this uninstaller was built with is no version: no core is given `module`
for badmin in __CASAOS_MODULES_MIN_CORE_TAG__ "" v0.4 0.4.75 'v0.4.75-rc1'; do
    fresh
    manifest alpha
    say_version v9.9.9
    CASAOS_MODULES_MIN_CORE_TAG="${badmin}"
    uninstall_part
    CASAOS_MODULES_MIN_CORE_TAG=v0.4.75
    [ "${status}" -eq 0 ] && [ ! -s "${CALLS}" ] || fail "a minimum of '${badmin}': the core was given 'module': $(calls)"
    case "$(summary)" in *"The modules (alpha) were not removed: ${CASA_MODULE_BIN} did not report a version of "*) ;; *) fail "a minimum of '${badmin}': not said: $(summary)" ;; esac
done
pass "no core, a file that is no program, or an uninstaller that was not built with a minimum: no module is removed, and it is said"

echo "all uninstall module checks passed"
