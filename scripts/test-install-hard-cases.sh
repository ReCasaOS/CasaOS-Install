#!/usr/bin/env bash
#
# Checks four places where the installer or the uninstaller met a box that is
# not the usual one and stopped, or went on when it should not have:
#
#   - a box whose only network card is Wi-Fi (wlo1): the closing banner;
#   - an update with no terminal to ask on (the one started from the dashboard)
#     on a disk with little room left;
#   - an rclone that cannot be downloaded;
#   - an uninstall that removes the containers of a box that has more than one.
#
# The functions are copied out of install.sh and casaos-uninstall as they stand
# and run against stand-ins: nothing is installed, removed or downloaded. Each
# runs in a script of its own, under `set -e` as its script is: an install that
# stops on a command that failed is what these checks look for, and `set -e`
# does nothing inside a function called on the left of `||`.
#
#   bash scripts/test-install-hard-cases.sh
#
# The release workflow runs this before anything is built.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() { echo "ok: $*"; }

# the scripts as bash reads them: a Windows checkout has CRLF line endings
INSTALL_SH="${WORK}/install.sh"
UNINSTALL_SH="${WORK}/casaos-uninstall"
tr -d '\r' <"${ROOT}/install.sh" >"${INSTALL_SH}"
tr -d '\r' <"${ROOT}/casaos-uninstall" >"${UNINSTALL_SH}"
bash -n "${INSTALL_SH}" || fail "install.sh does not parse"
bash -n "${UNINSTALL_SH}" || fail "casaos-uninstall does not parse"

# a function as its script has it
take() { # <script> <function>
    sed -n "/^$2() {\$/,/^}\$/p" "$1"
}

GOES_ON="the script goes on"

###############################################################################
# The closing banner on a box whose only card is Wi-Fi                        #
###############################################################################

cat >"${WORK}/banner.sh" <<EOF
set -e
sudo_cmd=""
GREEN_BULLET="*"
Get_Gateway_Port() { echo 80; }
ls() {
    case "\$1" in
    /sys/class/net/) printf '%s' "\${EVERY_CARD}" ;;
    /sys/devices/virtual/net/) printf '%s' "\${VIRTUAL_CARDS}" ;;
    esac
}
ifconfig() {
    case "\$1" in
    lo) echo "        inet 127.0.0.1  netmask 255.0.0.0" ;;
    *) echo "        inet 192.168.1.20  netmask 255.255.255.0  broadcast 192.168.1.255" ;;
    esac
}
$(take "${INSTALL_SH}" Get_IPs)
Get_IPs
echo "${GOES_ON}"
EOF

# what the end of an install prints on a box with these cards
banner() { # <every card, one per line> <the ones that are not hardware>
    EVERY_CARD="$1" VIRTUAL_CARDS="$2" bash "${WORK}/banner.sh" 2>&1 || true
}

got="$(banner $'docker0\nlo\nveth1a2b\nwlo1\n' $'docker0\nlo\nveth1a2b\n')"
[[ "${got}" == $'* http://192.168.1.20 (wlo1)\n'"${GOES_ON}" ]] || fail "the banner of a box with wlo1 alone: '${got}'"
pass "a card whose name holds the name of a virtual one (wlo1 and lo) is shown"

got="$(banner $'docker0\neno1\nlo\n' $'docker0\nlo\n')"
[[ "${got}" == $'* http://192.168.1.20 (eno1)\n'"${GOES_ON}" ]] || fail "the banner of a box with eno1: '${got}'"
pass "a wired card is shown, the virtual ones are not"

got="$(banner $'docker0\nlo\n' $'docker0\nlo\n')"
[[ "${got}" == "${GOES_ON}" ]] || fail "the banner of a box with no hardware card: '${got}'"
pass "a box with no hardware card ends its install, with no address to show"

###############################################################################
# Little room on the disk, and no terminal to ask on                          #
###############################################################################

command -v setsid >/dev/null || fail "setsid is needed to run a script with no terminal"

cat >"${WORK}/disk.sh" <<EOF
set -e
Show() {
    echo "show \$1: \$2"
    [[ \$1 != 1 ]] || exit 1
}
aCOLOUR=("" "" "" "" "")
COLOUR_RESET=""
FREE_DISK_GB="\${FREE}"
MINIMUM_DISK_SIZE_GB=5
$(take "${INSTALL_SH}" Can_Ask)
$(take "${INSTALL_SH}" Check_Disk)
Check_Disk
echo "${GOES_ON}"
EOF

# a session of its own has no terminal: opening /dev/tty fails, as it does under the dashboard's update
no_terminal() { # <GB free>
    FREE="$1" setsid -w bash "${WORK}/disk.sh" </dev/null 2>&1 || true
}

got="$(no_terminal 3)"
grep -qx "${GOES_ON}" <<<"${got}" || fail "an update with no terminal stops on a disk with little room: ${got}"
grep -q "^show 3: .*3GB" <<<"${got}" || fail "an update with no terminal does not say how little room there is: ${got}"
pass "with no terminal, little room on the disk is said and the install goes on"

# FREE_DISK_GB is rounded down: 0 is less than 1GB, where an update would stop halfway with its services down
got="$(no_terminal 0)"
grep -qx "${GOES_ON}" <<<"${got}" && fail "an update with no terminal goes on with less than 1GB free: ${got}"
grep -q "^show 1: .*1GB" <<<"${got}" || fail "an update that stops for lack of room does not say why: ${got}"
pass "with no terminal and less than 1GB free, the install stops before it changes anything"

take "${INSTALL_SH}" Check_Disk | grep -q 'select yn in "Yes" "No"' || fail "Check_Disk no longer asks on a terminal"
pass "with a terminal, the question is still asked"

###############################################################################
# An rclone that cannot be downloaded                                         #
###############################################################################

# a box whose rclone, v1.60.0, is at \${OLD}; the download of the pinned one works when \${WORKS} is yes
cat >"${WORK}/rclone.sh" <<EOF
set -e
rclone_saying() { # <path> <version>
    mkdir -p "\$(dirname "\$1")"
    printf '#!/bin/sh\necho "rclone %s"\n' "\$2" >"\$1"
    chmod +x "\$1"
}
rm -rf "${WORK}/box"
mkdir -p "${WORK}/box/root/usr/bin"
cd "${WORK}/box"
# \${LINKED}: the usual place is also reached through a link, as /bin is on a merged /usr
[[ "\${LINKED:-no}" != yes ]] || ln -s root/usr/bin "${WORK}/box/bin"
rclone_saying "\${OLD}" v1.60.0

sudo_cmd=""
PREFIX="${WORK}/box/root"
RCLONE_TAG=v1.75.1
Show() { [[ \$1 != 1 ]] || exit 1; }
systemctl() { :; }
Install_rclone_from_source() {
    [[ "\${WORKS}" == yes ]] || Show 1 "Failed to download rclone"
    rm -f "\${PREFIX}/usr/bin/rclone" # as install(1) does: a new file, not the old one written over
    rclone_saying "\${PREFIX}/usr/bin/rclone" "\${RCLONE_TAG}"
}
PATH="${WORK}/box/bin:${WORK}/box/elsewhere/bin:\${PREFIX}/usr/bin:\${PATH}"
$(take "${INSTALL_SH}" Install_Rclone)
Install_Rclone
echo "${GOES_ON}"
EOF

change_rclone() { # <old path> <the download works: yes|no>
    OLD="$1" WORKS="$2" bash "${WORK}/rclone.sh" 2>&1 || true
}

elsewhere="${WORK}/box/elsewhere/bin/rclone"
pinned="${WORK}/box/root/usr/bin/rclone"

got="$(change_rclone "${elsewhere}" no)"
grep -qx "${GOES_ON}" <<<"${got}" && fail "an rclone that cannot be downloaded does not end the install"
[[ "$("${elsewhere}" 2>/dev/null)" == "rclone v1.60.0" ]] || fail "the download failed and the rclone the box had is gone"
pass "a download that fails ends the install and leaves the box the rclone it had"

got="$(change_rclone "${elsewhere}" yes)"
grep -qx "${GOES_ON}" <<<"${got}" || fail "Install_Rclone failed with a download that works: ${got}"
[[ ! -e "${elsewhere}" ]] || fail "the old rclone is still first in the PATH, before the new one"
[[ "$("${pinned}")" == "rclone v1.75.1" ]] || fail "the pinned rclone is not installed"
pass "the old rclone, somewhere else in the PATH, is removed once the new one is there"

got="$(change_rclone "${pinned}" yes)"
grep -qx "${GOES_ON}" <<<"${got}" || fail "Install_Rclone failed on an rclone at the usual place: ${got}"
[[ "$("${pinned}" 2>/dev/null)" == "rclone v1.75.1" ]] || fail "the new rclone was removed with the old one, at the same place"
pass "an old rclone at the usual place is replaced, and the new one stays"

got="$(LINKED=yes change_rclone "${pinned}" yes)"
grep -qx "${GOES_ON}" <<<"${got}" || fail "Install_Rclone failed on an rclone found through a link: ${got}"
[[ "$("${pinned}" 2>/dev/null)" == "rclone v1.75.1" ]] || fail "the new rclone was removed: the old one was found through a link to the same folder"
pass "an old rclone found through a link to the usual place is replaced, and the new one stays"

###############################################################################
# Removing the containers and the images of a box that has several            #
###############################################################################

# a docker that lists two containers and three image lines (one image twice), wants one id per
# argument as the real one does, and refuses to remove while ${WORK}/docker-refuses is there
mkdir -p "${WORK}/bin"
cat >"${WORK}/bin/docker" <<EOF
#!/usr/bin/env bash
case "\$1 \${2:-}" in
"ps -aq") printf 'aaa111\nbbb222\n' ;;
"images -q") printf 'img1\nimg2\nimg1\n' ;;
*)
    for argument in "\$@"; do
        case "\${argument}" in
        *\$'\n'*)
            echo "docker: several ids in one argument" >&2
            exit 1
            ;;
        esac
    done
    echo "\$*" >>"${WORK}/docker.log"
    [[ ! -e "${WORK}/docker-refuses" ]] || exit 1
    ;;
esac
EOF
chmod +x "${WORK}/bin/docker"

# the two functions on that box, with everything asked for; says what is left to do with AppData
cat >"${WORK}/remove.sh" <<EOF
set -e
sudo_cmd=""
UNINSTALL_ALL_CONTAINER=true
REMOVE_IMAGES="all"
REMOVE_APP_DATA=true
Show() { echo "show \$1: \$2"; }
PATH="${WORK}/bin:\${PATH}"
$(take "${UNINSTALL_SH}" Unistall_Container)
$(take "${UNINSTALL_SH}" Remove_Images)
Unistall_Container
Remove_Images
echo "appdata=\${REMOVE_APP_DATA}"
EOF

remove_all() {
    : >"${WORK}/docker.log"
    bash "${WORK}/remove.sh" 2>&1 || true
}

got="$(remove_all)"
grep -qx "rm -f aaa111 bbb222" "${WORK}/docker.log" || fail "the two containers are not removed, each as its own argument: $(cat "${WORK}/docker.log") / ${got}"
[[ "$(head -n 1 "${WORK}/docker.log")" == "stop aaa111 bbb222" ]] || fail "the containers are not stopped before they are removed (rm -f alone kills them): $(cat "${WORK}/docker.log")"
grep -qx "rmi -f img1 img2" "${WORK}/docker.log" || fail "the two images are not removed, each once and as its own argument: $(cat "${WORK}/docker.log") / ${got}"
grep -q "^show 1:" <<<"${got}" && fail "a failure is shown when everything was removed: ${got}"
grep -qx "appdata=true" <<<"${got}" || fail "AppData is kept though its containers are gone: ${got}"
pass "two containers and two images are removed, and AppData after them"

touch "${WORK}/docker-refuses"
got="$(remove_all)"
rm -f "${WORK}/docker-refuses"
grep -q "^show 1: Failed to delete all containers" <<<"${got}" || fail "containers that are left are not said: ${got}"
grep -qx "appdata=false" <<<"${got}" || fail "AppData is removed under containers that are still there: ${got}"
grep -q "^rmi" "${WORK}/docker.log" && fail "the images are removed under containers that are still there: $(cat "${WORK}/docker.log")"
pass "containers that cannot be removed keep their AppData and their images"

echo "all hard-case checks passed"
