#!/usr/bin/bash
#
#       ReCasaOS Installer __CASAOS_RELEASE_TAG__
#   GitHub: https://github.com/ReCasaOS/CasaOS-Install
#   Issues: https://github.com/ReCasaOS/CasaOS-Install/issues
#   Upstream: https://github.com/IceWhaleTech/CasaOS
#   Requires: bash, mv, rm, tr, grep, sed, curl/wget, tar, smartmontools, parted, ntfs-3g, net-tools
#
#   This script installs CasaOS to your system.
#   Usage:
#
#   	$ curl -fsSL https://github.com/ReCasaOS/CasaOS-Install/releases/latest/download/install.sh | sudo bash
#
#   In automated environments, you may want to run as root.
#   If using curl, we recommend using the -fsSL flags.
#
#   This only work on  Linux systems. Please
#   open an issue if you notice any bugs.
#
clear
echo -e "\e[0m\c"

# shellcheck disable=SC2016
echo '
  _____       _____                 ____   _____
 |  __ \     / ____|               / __ \ / ____|
 | |__) |___| |     __ _ ___  __ _| |  | | (___
 |  _  // _ \ |    / _` / __|/ _` | |  | |\___ \
 | | \ \  __/ |___| (_| \__ \ (_| | |__| |____) |
 |_|  \_\___|\_____\__,_|___/\__,_|\____/|_____/

   --- built on CasaOS by IceWhale ---
'
export PATH=/usr/sbin:$PATH
export DEBIAN_FRONTEND=noninteractive

set -e

###############################################################################
# GOLBALS                                                                     #
###############################################################################

((EUID)) && sudo_cmd="sudo"

# shellcheck source=/dev/null
source /etc/os-release

# SYSTEM REQUIREMENTS
readonly MINIMUM_DISK_SIZE_GB="5"
readonly MINIMUM_MEMORY="400"
readonly MINIMUM_DOCKER_VERSION="20"
readonly CASA_DEPANDS_PACKAGE=('wget' 'curl' 'smartmontools' 'parted' 'ntfs-3g' 'net-tools' 'udevil' 'samba' 'cifs-utils' 'mergerfs' 'unzip' 'bzip2' 'git')
readonly CASA_DEPANDS_COMMAND=('wget' 'curl' 'smartctl' 'parted' 'ntfs-3g' 'netstat' 'udevil' 'smbd' 'mount.cifs' 'mount.mergerfs' 'unzip' 'bzip2' 'git')

# SYSTEM INFO
PHYSICAL_MEMORY=$(LC_ALL=C free -m | awk '/Mem:/ { print $2 }')
readonly PHYSICAL_MEMORY

FREE_DISK_BYTES=$(LC_ALL=C df -P / | tail -n 1 | awk '{print $4}')
readonly FREE_DISK_BYTES

readonly FREE_DISK_GB=$((FREE_DISK_BYTES / 1024 / 1024))

LSB_DIST=$( ([ -n "${ID_LIKE}" ] && echo "${ID_LIKE}") || ([ -n "${ID}" ] && echo "${ID}"))
readonly LSB_DIST

DIST=$(echo "${ID}")
readonly DIST

UNAME_M="$(uname -m)"
readonly UNAME_M

UNAME_U="$(uname -s)"
readonly UNAME_U

readonly CASA_CONF_PATH=/etc/casaos/gateway.ini
readonly CASA_UNINSTALL_PATH=/usr/bin/casaos-uninstall
# The core's state: the overlay's fork-release marker, and the markers of the
# anonymous statistics (see Write_Telemetry_Markers).
readonly CASA_STATE_DIR=/var/lib/casaos
readonly CASAOS_APP_MANAGEMENT_TAG="__CASAOS_APP_MANAGEMENT_TAG__"
readonly CASAOS_INSTALL_RELEASE_TAG="__CASAOS_RELEASE_TAG__"
readonly CASAOS_RELEASE_BASE_URL="https://github.com/ReCasaOS/CasaOS-Install/releases/download/${CASAOS_INSTALL_RELEASE_TAG}"
readonly CASAOS_INSTALLER_SELF_URL="${CASAOS_RELEASE_BASE_URL}/install.sh"
readonly CASA_UNINSTALL_URL="${CASAOS_RELEASE_BASE_URL}/casaos-uninstall"
# Component releases this installer was cut against. Every double-underscore
# marker below is
# written by scripts/build-release-bundle.sh from release/components.env and
# from the checksums.txt each component publishes, or, for the dashboard and
# the App Store seed, whose releases publish none, from the package itself;
# nothing here is typed by hand. The script as committed will refuse to run
# until it has been filled.
readonly CASAOS_TAG="__CASAOS_TAG__"
readonly CASAOS_GATEWAY_TAG="__CASAOS_GATEWAY_TAG__"
readonly CASAOS_USER_SERVICE_TAG="__CASAOS_USER_SERVICE_TAG__"
readonly CASAOS_MESSAGE_BUS_TAG="__CASAOS_MESSAGE_BUS_TAG__"
readonly CASAOS_LOCAL_STORAGE_TAG="__CASAOS_LOCAL_STORAGE_TAG__"
readonly CASAOS_UI_TAG="__CASAOS_UI_TAG__"
readonly CASAOS_APPSTORE_TAG="__CASAOS_APPSTORE_TAG__"
readonly RCLONE_TAG="__RCLONE_TAG__"
readonly RESTIC_TAG="__RESTIC_TAG__"
readonly CASAOS_UPDATE_LOG="/var/log/casaos/upgrade.log"
readonly CASAOS_COMPAT_OVERLAY_FILE="linux-zz-casaos-compat-overlay-${CASAOS_INSTALL_RELEASE_TAG}.tar.gz"
readonly CASAOS_CORE_PACKAGE_FILE_PREFIX="casaos-${CASAOS_TAG}"
readonly CASAOS_APP_MANAGEMENT_SHA256_AMD64="__CASAOS_APP_MANAGEMENT_SHA256_AMD64__"
readonly CASAOS_APP_MANAGEMENT_SHA256_ARM64="__CASAOS_APP_MANAGEMENT_SHA256_ARM64__"
readonly CASAOS_APP_MANAGEMENT_SHA256_ARM7="__CASAOS_APP_MANAGEMENT_SHA256_ARM7__"
readonly CASAOS_CORE_SHA256_AMD64="__CASAOS_CORE_SHA256_AMD64__"
readonly CASAOS_CORE_SHA256_ARM64="__CASAOS_CORE_SHA256_ARM64__"
readonly CASAOS_CORE_SHA256_ARM7="__CASAOS_CORE_SHA256_ARM7__"
readonly CASAOS_GATEWAY_SHA256_AMD64="__CASAOS_GATEWAY_SHA256_AMD64__"
readonly CASAOS_GATEWAY_SHA256_ARM64="__CASAOS_GATEWAY_SHA256_ARM64__"
readonly CASAOS_GATEWAY_SHA256_ARM7="__CASAOS_GATEWAY_SHA256_ARM7__"
readonly CASAOS_MESSAGE_BUS_SHA256_AMD64="__CASAOS_MESSAGE_BUS_SHA256_AMD64__"
readonly CASAOS_MESSAGE_BUS_SHA256_ARM64="__CASAOS_MESSAGE_BUS_SHA256_ARM64__"
readonly CASAOS_MESSAGE_BUS_SHA256_ARM7="__CASAOS_MESSAGE_BUS_SHA256_ARM7__"
readonly CASAOS_USER_SERVICE_SHA256_AMD64="__CASAOS_USER_SERVICE_SHA256_AMD64__"
readonly CASAOS_USER_SERVICE_SHA256_ARM64="__CASAOS_USER_SERVICE_SHA256_ARM64__"
readonly CASAOS_USER_SERVICE_SHA256_ARM7="__CASAOS_USER_SERVICE_SHA256_ARM7__"
readonly CASAOS_LOCAL_STORAGE_SHA256_AMD64="__CASAOS_LOCAL_STORAGE_SHA256_AMD64__"
readonly CASAOS_LOCAL_STORAGE_SHA256_ARM64="__CASAOS_LOCAL_STORAGE_SHA256_ARM64__"
readonly CASAOS_LOCAL_STORAGE_SHA256_ARM7="__CASAOS_LOCAL_STORAGE_SHA256_ARM7__"
readonly CASAOS_UI_SHA256="__CASAOS_UI_SHA256__"
readonly RCLONE_SHA256_AMD64="__RCLONE_SHA256_AMD64__"
readonly RCLONE_SHA256_ARM64="__RCLONE_SHA256_ARM64__"
readonly RCLONE_SHA256_ARM7="__RCLONE_SHA256_ARM7__"
readonly RESTIC_SHA256_AMD64="__RESTIC_SHA256_AMD64__"
readonly RESTIC_SHA256_ARM64="__RESTIC_SHA256_ARM64__"
readonly RESTIC_SHA256_ARM7="__RESTIC_SHA256_ARM7__"
readonly CASAOS_APPSTORE_SHA256="__CASAOS_APPSTORE_SHA256__"
readonly CASAOS_COMPAT_OVERLAY_SHA256="__CASAOS_COMPAT_OVERLAY_SHA256__"
readonly CASAOS_UNINSTALL_SHA256="__CASAOS_UNINSTALL_SHA256__"
# empty when this release carries no module catalogue
readonly CASAOS_MODULE_CATALOGUE_SHA256="__CASAOS_MODULE_CATALOGUE_SHA256__"
# the oldest core that has `casaos module`, written by the release build, which also
# refuses a catalogue for a release whose pinned core is older: an older core given
# `module` does not know it and starts as a server
readonly CASAOS_MODULES_MIN_CORE_TAG="__CASAOS_MODULES_MIN_CORE_TAG__"
# Not a marker: the oldest core that has `casaos module update`, and the `update` field of
# each installed module in `casaos module list`. It is a tag of the core that exists and
# will not change (both came in v0.4.78), so it is written here; a core that reports an
# older version is never asked to update a module.
readonly CASAOS_MODULE_UPDATE_MIN_CORE_TAG="v0.4.78"

# REQUIREMENTS CONF PATH
# Udevil
readonly UDEVIL_CONF_PATH=/etc/udevil/udevil.conf
readonly DEVMON_CONF_PATH=/etc/conf.d/devmon

# COLORS - none when the output is not a terminal, such as the in-app update log
if [[ -t 1 ]]; then
    readonly COLOUR_RESET='\e[0m'
    readonly aCOLOUR=(
        '\e[38;5;154m' # green  	| Lines, bullets and separators
        '\e[1m'        # Bold white	| Main descriptions
        '\e[90m'       # Grey		| Credits
        '\e[91m'       # Red		| Update notifications Alert
        '\e[33m'       # Yellow		| Emphasis
    )
    readonly WGET_PROGRESS='--show-progress'
    readonly LINE_BREAK=''
else
    readonly COLOUR_RESET=''
    readonly aCOLOUR=('' '' '' '' '')
    readonly WGET_PROGRESS=''
    # Two trailing spaces are a Markdown hard line break: dashboards before
    # v0.4.34 render this log as Markdown and would run the lines together.
    readonly LINE_BREAK='  '
fi

readonly GREEN_LINE=" ${aCOLOUR[0]}─────────────────────────────────────────────────────$COLOUR_RESET"
readonly GREEN_BULLET=" ${aCOLOUR[0]}-$COLOUR_RESET"
readonly GREEN_SEPARATOR="${aCOLOUR[0]}:$COLOUR_RESET"

# CASAOS VARIABLES
TARGET_ARCH=""
CASAOS_APP_MANAGEMENT_SHA256=""
CASAOS_CORE_SHA256=""
TMP_ROOT=/tmp/casaos-installer
REGION="UNKNOWN"
# Where the App Store catalogue is fetched from. Set it on the install line to use a
# mirror -- curl <install.sh> | sudo CASA_DOWNLOAD_DOMAIN=https://mirror/ bash -- rather
# than being geo-located: this script used to curl a geo-IP service to guess it.
CASA_DOWNLOAD_DOMAIN="${CASA_DOWNLOAD_DOMAIN:-https://github.com/}"
# Anonymous statistics are turned off by --no-telemetry, or by
# RECASAOS_TELEMETRY=0 on the install line -- curl <install.sh> | sudo
# RECASAOS_TELEMETRY=0 bash. Without either, the owner's earlier choice stands.
NO_TELEMETRY=0
if [[ "${RECASAOS_TELEMETRY:-}" == "0" ]]; then
    NO_TELEMETRY=1
fi
# The optional modules that are installed are updated to the versions of this release's
# catalogue at the end of every run (Update_Installed_Modules). --no-module-update, or
# RECASAOS_MODULE_UPDATE=0 on the install line, leaves them at the versions they have.
NO_MODULE_UPDATE=0
if [[ "${RECASAOS_MODULE_UPDATE:-}" == "0" ]]; then
    NO_MODULE_UPDATE=1
fi
# Debian 11 is out of support and its security repository lists packages it no
# longer serves. --use-debian-archive, or RECASAOS_DEBIAN_ARCHIVE=1 on the install
# line, lets the installer point apt at archive.debian.org for that repository
# without asking; without either it asks, when it has a terminal to ask on, and
# otherwise only says what to run (see Package_Install_Failed).
DEBIAN_ARCHIVE=0
if [[ "${RECASAOS_DEBIAN_ARCHIVE:-}" == "1" ]]; then
    DEBIAN_ARCHIVE=1
fi
APT_SOURCES_LIST=/etc/apt/sources.list
# Optional modules (docs/modules.md). The catalogue of this release is placed in
# CASA_MODULE_CATALOGUE_DIR; --module adds a name to MODULES_REQUESTED (names
# separated by a space, each one checked by Valid_Module_Name); the modules are
# installed with the core's own assistant once ReCasaOS runs.
CASA_MODULE_CATALOGUE_DIR=/usr/share/casaos/modules
CASA_BIN=/usr/bin/casaos
# where the hand-off of the module updates puts its script (Hand_Off_Module_Updates)
CASA_RUN_DIR=/run
MODULES_REQUESTED=""
MODULE_CATALOGUE_READY=0
# 1 once this run has put the core of this release in place: only that core is given `module`
CORE_INSTALLED_NOW=0
# How long refreshing apt's package lists may take. A mirror that accepts the
# connection and then says nothing used to hold the whole install for as long as
# whoever ran it was willing to wait (a CI job's limit was forty minutes, twice).
APT_UPDATE_TIMEOUT=300
STOPPED_CASA_SERVICES=()
INSTALL_COMPLETED=0
# fork-release as it was before the copy onto / replaced it (see onExit)
PREVIOUS_FORK_RELEASE=""
FORK_RELEASE_REPLACED=0

trap 'onCtrlC' INT
trap 'onExit $?' EXIT
onCtrlC() {
    echo -e "${COLOUR_RESET}"
    exit 130
}

onExit() {
    local exit_code="$1"
    local service

    if ((exit_code == 0)); then
        return
    fi

    set +e
    # The copy onto / wrote this release's tag into fork-release, but the run
    # did not get through the services' check: the previous tag goes back, or
    # none when there was none, before any service restarts. The update is
    # still offered, and the core counts an automatic one as failed.
    if ((INSTALL_COMPLETED == 0 && FORK_RELEASE_REPLACED)); then
        if [[ -n "${PREVIOUS_FORK_RELEASE}" ]]; then
            printf '%s\n' "${PREVIOUS_FORK_RELEASE}" | ${sudo_cmd} tee "${CASA_STATE_DIR}/fork-release" >/dev/null
        else
            ${sudo_cmd} rm -f "${CASA_STATE_DIR}/fork-release"
        fi
    fi

    if ((INSTALL_COMPLETED == 0 && ${#STOPPED_CASA_SERVICES[@]} > 0)); then
        echo "CasaOS installation failed; restarting services that were stopped."
        for service in "${STOPPED_CASA_SERVICES[@]}"; do
            ${sudo_cmd} systemctl start "${service}" >/dev/null 2>&1 || true
        done
    fi

    # The dashboard's update dialog waits for this line in its log.
    if ! [[ -t 1 ]]; then
        echo "CasaOS upgrade failed"
    fi
}

###############################################################################
# Helpers                                                                     #
###############################################################################

#######################################
# Custom printing function
# Globals:
#   None
# Arguments:
#   $1 0:OK   1:FAILED  2:INFO  3:NOTICE
#   message
# Returns:
#   None
#######################################

Show() {
    # OK
    if (($1 == 0)); then
        echo -e "${aCOLOUR[2]}[$COLOUR_RESET${aCOLOUR[0]}  OK  $COLOUR_RESET${aCOLOUR[2]}]$COLOUR_RESET $2${LINE_BREAK}"
    # FAILED
    elif (($1 == 1)); then
        echo -e "${aCOLOUR[2]}[$COLOUR_RESET${aCOLOUR[3]}FAILED$COLOUR_RESET${aCOLOUR[2]}]$COLOUR_RESET $2${LINE_BREAK}"
        exit 1
    # INFO
    elif (($1 == 2)); then
        echo -e "${aCOLOUR[2]}[$COLOUR_RESET${aCOLOUR[0]} INFO $COLOUR_RESET${aCOLOUR[2]}]$COLOUR_RESET $2${LINE_BREAK}"
    # NOTICE
    elif (($1 == 3)); then
        echo -e "${aCOLOUR[2]}[$COLOUR_RESET${aCOLOUR[4]}NOTICE$COLOUR_RESET${aCOLOUR[2]}]$COLOUR_RESET $2${LINE_BREAK}"
    fi
}

Running_Under_CasaOS_Service() {
    [[ -r /proc/$$/cgroup ]] && grep -Eq '(^|/)casaos\.service($|/)' /proc/$$/cgroup
}

Detach_From_CasaOS_Service() {
    local detached_command
    local release_unit
    local update_unit

    if [[ "${CASAOS_INSTALLER_DETACHED:-0}" == "1" ]]; then
        return 0
    fi
    Running_Under_CasaOS_Service || return 0

    command -v systemd-run >/dev/null 2>&1 || Show 1 "systemd-run is required for in-app updates"
    ${sudo_cmd} install -d -m 0755 "$(dirname "${CASAOS_UPDATE_LOG}")" || Show 1 "Failed to prepare CasaOS update log directory"
    ${sudo_cmd} touch "${CASAOS_UPDATE_LOG}" || Show 1 "Failed to prepare CasaOS update log"

    release_unit=${CASAOS_INSTALL_RELEASE_TAG//./-}
    update_unit="casaos-update-${release_unit}-$(date +%s)-$$"
    printf -v detached_command 'exec >> %q 2>&1; curl -fsSL %q | /bin/bash' \
        "${CASAOS_UPDATE_LOG}" \
        "${CASAOS_INSTALLER_SELF_URL}"

    # The detached run gets no arguments: an opt-out goes through the environment.
    local opt_out_env=()
    if ((NO_TELEMETRY)); then opt_out_env+=(--setenv=RECASAOS_TELEMETRY=0); fi
    if ((NO_MODULE_UPDATE)); then opt_out_env+=(--setenv=RECASAOS_MODULE_UPDATE=0); fi

    # ... and so it takes no --module either: say so rather than drop the modules in silence.
    if [[ -n "${MODULES_REQUESTED}" ]]; then
        local module_name module_commands=""
        for module_name in ${MODULES_REQUESTED}; do
            module_commands+=" sudo casaos module install ${module_name};"
        done
        Show 3 "The update runs detached and takes no options, so it will NOT install the modules asked for with --module (${MODULES_REQUESTED}). Once it has finished, install them from the Modules page of the dashboard, or run:${module_commands%;}"
    fi

    Show 2 "Handing the update to ${update_unit}.service..."
    if ! ${sudo_cmd} systemd-run \
        --quiet \
        --collect \
        --unit="${update_unit}" \
        --property=Type=exec \
        --setenv=CASAOS_INSTALLER_DETACHED=1 \
        "${opt_out_env[@]}" \
        /bin/bash -o pipefail -c "${detached_command}"; then
        Show 1 "Failed to detach the in-app update from casaos.service"
    fi

    exit 0
}

Warn() {
    echo -e "${aCOLOUR[3]}$1$COLOUR_RESET"
}

GreyStart() {
    echo -e "${aCOLOUR[2]}\c"
}

ColorReset() {
    echo -e "$COLOUR_RESET\c"
}

# Clear Terminal
Clear_Term() {

    # Without an input terminal, there is no point in doing this.
    [[ -t 0 ]] || return

    # Printing terminal height - 1 newlines seems to be the fastest method that is compatible with all terminal types.
    lines=$(tput lines) i newlines
    local lines

    for ((i = 1; i < ${lines% *}; i++)); do newlines+='\n'; done
    echo -ne "\e[0m$newlines\e[H"

}

# Check file exists
exist_file() {
    if [ -e "$1" ]; then
        return 1
    else
        return 2
    fi
}

###############################################################################
# FUNCTIONS                                                                   #
###############################################################################



# 1 Check Arch
Check_Arch() {
    case $UNAME_M in
    *aarch64*)
        TARGET_ARCH="arm64"
        RCLONE_ARCH="arm64"
        RCLONE_SHA256="${RCLONE_SHA256_ARM64}"
        RESTIC_ARCH="arm64"
        RESTIC_SHA256="${RESTIC_SHA256_ARM64}"
        CASAOS_APP_MANAGEMENT_SHA256="${CASAOS_APP_MANAGEMENT_SHA256_ARM64}"
        CASAOS_CORE_SHA256="${CASAOS_CORE_SHA256_ARM64}"
        CASAOS_GATEWAY_SHA256="${CASAOS_GATEWAY_SHA256_ARM64}"
        CASAOS_MESSAGE_BUS_SHA256="${CASAOS_MESSAGE_BUS_SHA256_ARM64}"
        CASAOS_USER_SERVICE_SHA256="${CASAOS_USER_SERVICE_SHA256_ARM64}"
        CASAOS_LOCAL_STORAGE_SHA256="${CASAOS_LOCAL_STORAGE_SHA256_ARM64}"
        ;;
    *64*)
        TARGET_ARCH="amd64"
        RCLONE_ARCH="amd64"
        RCLONE_SHA256="${RCLONE_SHA256_AMD64}"
        RESTIC_ARCH="amd64"
        RESTIC_SHA256="${RESTIC_SHA256_AMD64}"
        CASAOS_APP_MANAGEMENT_SHA256="${CASAOS_APP_MANAGEMENT_SHA256_AMD64}"
        CASAOS_CORE_SHA256="${CASAOS_CORE_SHA256_AMD64}"
        CASAOS_GATEWAY_SHA256="${CASAOS_GATEWAY_SHA256_AMD64}"
        CASAOS_MESSAGE_BUS_SHA256="${CASAOS_MESSAGE_BUS_SHA256_AMD64}"
        CASAOS_USER_SERVICE_SHA256="${CASAOS_USER_SERVICE_SHA256_AMD64}"
        CASAOS_LOCAL_STORAGE_SHA256="${CASAOS_LOCAL_STORAGE_SHA256_AMD64}"
        ;;
    *armv7*)
        TARGET_ARCH="arm-7"
        RCLONE_ARCH="arm-v7"
        RCLONE_SHA256="${RCLONE_SHA256_ARM7}"
        RESTIC_ARCH="arm"
        RESTIC_SHA256="${RESTIC_SHA256_ARM7}"
        CASAOS_APP_MANAGEMENT_SHA256="${CASAOS_APP_MANAGEMENT_SHA256_ARM7}"
        CASAOS_CORE_SHA256="${CASAOS_CORE_SHA256_ARM7}"
        CASAOS_GATEWAY_SHA256="${CASAOS_GATEWAY_SHA256_ARM7}"
        CASAOS_MESSAGE_BUS_SHA256="${CASAOS_MESSAGE_BUS_SHA256_ARM7}"
        CASAOS_USER_SERVICE_SHA256="${CASAOS_USER_SERVICE_SHA256_ARM7}"
        CASAOS_LOCAL_STORAGE_SHA256="${CASAOS_LOCAL_STORAGE_SHA256_ARM7}"
        ;;
    *)
        Show 1 "Aborted, unsupported or unknown architecture: $UNAME_M"
        exit 1
        ;;
    esac
    Show 0 "Your hardware architecture is : $UNAME_M"
    CASA_PACKAGES=(
        "https://github.com/ReCasaOS/CasaOS-Gateway/releases/download/${CASAOS_GATEWAY_TAG}/linux-${TARGET_ARCH}-casaos-gateway-${CASAOS_GATEWAY_TAG}.tar.gz"
"https://github.com/ReCasaOS/CasaOS-MessageBus/releases/download/${CASAOS_MESSAGE_BUS_TAG}/linux-${TARGET_ARCH}-casaos-message-bus-${CASAOS_MESSAGE_BUS_TAG}.tar.gz"
"https://github.com/ReCasaOS/CasaOS-UserService/releases/download/${CASAOS_USER_SERVICE_TAG}/linux-${TARGET_ARCH}-casaos-user-service-${CASAOS_USER_SERVICE_TAG}.tar.gz"
"https://github.com/ReCasaOS/CasaOS-LocalStorage/releases/download/${CASAOS_LOCAL_STORAGE_TAG}/linux-${TARGET_ARCH}-casaos-local-storage-${CASAOS_LOCAL_STORAGE_TAG}.tar.gz"
"https://github.com/ReCasaOS/CasaOS-AppManagement/releases/download/${CASAOS_APP_MANAGEMENT_TAG}/linux-${TARGET_ARCH}-casaos-app-management-${CASAOS_APP_MANAGEMENT_TAG}.tar.gz"
"https://github.com/ReCasaOS/CasaOS/releases/download/${CASAOS_TAG}/linux-${TARGET_ARCH}-${CASAOS_CORE_PACKAGE_FILE_PREFIX}.tar.gz"
"https://github.com/ReCasaOS/CasaOS-UI/releases/download/${CASAOS_UI_TAG}/linux-all-casaos-${CASAOS_UI_TAG}.tar.gz"
"${CASAOS_RELEASE_BASE_URL}/linux-all-appstore-${CASAOS_APPSTORE_TAG}.tar.gz"
"${CASAOS_RELEASE_BASE_URL}/${CASAOS_COMPAT_OVERLAY_FILE}"
    )
}

# PACKAGE LIST OF CASAOS (make sure the services are in the right order)
CASA_SERVICES=(
    "casaos-gateway.service"
"casaos-message-bus.service"
"casaos-user-service.service"
"casaos-local-storage.service"
"casaos-app-management.service"
"rclone.service"
"casaos.service"  # must be the last one so update from UI can work
)

# 2 Check Distribution
Check_Distribution() {
    sType=0
    notice=""
    case $LSB_DIST in
    *debian*) ;;

    *ubuntu*) ;;

    *raspbian*) ;;

    *openwrt*)
        Show 1 "Aborted, OpenWrt cannot be installed using this script."
        exit 1
        ;;
    *alpine*)
        Show 1 "Aborted, Alpine installation is not yet supported."
        exit 1
        ;;
    *trisquel*) ;;

    *)
        sType=3
        notice="We have not tested it on this system and it may fail to install."
        ;;
    esac
    Show ${sType} "Your Linux Distribution is : ${DIST} ${notice}"

    if [[ ${sType} == 1 ]]; then
        select yn in "Yes" "No"; do
            case $yn in
            [yY][eE][sS] | [yY])
                Show 0 "Distribution check has been ignored."
                break
                ;;
            [nN][oO] | [nN])
                Show 1 "Already exited the installation."
                exit 1
                ;;
            esac
        done < /dev/tty # < /dev/tty is used to read the input from the terminal
    fi
}

# 3 Check OS
Check_OS() {
    if [[ $UNAME_U == *Linux* ]]; then
        Show 0 "Your System is : $UNAME_U"
    else
        Show 1 "This script is only for Linux."
        exit 1
    fi
}

# 4 Check Memory
Check_Memory() {
    if [[ "${PHYSICAL_MEMORY}" -lt "${MINIMUM_MEMORY}" ]]; then
        Show 1 "requires atleast 400MB physical memory."
        exit 1
    fi
    Show 0 "Memory capacity check passed."
}

# 5 Check Disk
Check_Disk() {
    if [[ "${FREE_DISK_GB}" -lt "${MINIMUM_DISK_SIZE_GB}" ]]; then
        echo -e "${aCOLOUR[4]}Recommended free disk space is greater than ${MINIMUM_DISK_SIZE_GB}GB, Current free disk space is ${aCOLOUR[3]}${FREE_DISK_GB}GB${COLOUR_RESET}${aCOLOUR[4]}.\nContinue installation?${COLOUR_RESET}"
        select yn in "Yes" "No"; do
            case $yn in
            [yY][eE][sS] | [yY])
                Show 0 "Disk capacity check has been ignored."
                break
                ;;
            [nN][oO] | [nN])
                Show 1 "Already exited the installation."
                exit 1
                ;;
            esac
        done < /dev/tty  # < /dev/tty is used to read the input from the terminal
    else
        Show 0 "Disk capacity check passed."
    fi
}

# Check Port Use
Check_Port() {
    TCPListeningnum=$(${sudo_cmd} netstat -an | grep ":$1 " | awk '$1 == "tcp" && $NF == "LISTEN" {print $0}' | wc -l)
    UDPListeningnum=$(${sudo_cmd} netstat -an | grep ":$1 " | awk '$1 == "udp" && $NF == "0.0.0.0:*" {print $0}' | wc -l)
    ((Listeningnum = TCPListeningnum + UDPListeningnum))
    if [[ $Listeningnum == 0 ]]; then
        echo "0"
    else
        echo "1"
    fi
}

# Get an available port
Get_Port() {
    CurrentPort=$(${sudo_cmd} cat ${CASA_CONF_PATH} | grep HttpPort | awk '{print $3}')
    if [[ $CurrentPort == "$Port" ]]; then
        for PORT in {80..65536}; do
            if [[ $(Check_Port "$PORT") == 0 ]]; then
                Port=$PORT
                break
            fi
        done
    else
        Port=$CurrentPort
    fi
}

# Update package

# Refreshing the package lists is best effort.
#
# Every repository this host happens to have configured gets a vote here, and this
# script runs under `set -e`: one of them being unreachable, or serving a Release
# file whose Valid-Until has passed because the mirror stopped updating, used to end
# the whole upgrade with `CasaOS upgrade failed` and nothing else touched. None of
# those repositories belong to CasaOS. The packages needed below are already present
# on any host that is upgrading rather than installing, and Install_Depends says so
# plainly, naming the package, if one really is missing.
Update_Package_Resource() {
    Show 2 "Updating package manager..."
    GreyStart
    local refreshed=0
    if [ -x "$(command -v apk)" ]; then
        ${sudo_cmd} apk update || refreshed=$?
    elif [ -x "$(command -v apt-get)" ]; then
        # bounded where timeout exists; apt-get answers its TERM and leaves its lists
        # as they were, and what follows is the same as for any refresh that failed
        if [ -x "$(command -v timeout)" ]; then
            ${sudo_cmd} timeout "${APT_UPDATE_TIMEOUT}" apt-get update -qq || refreshed=$?
        else
            ${sudo_cmd} apt-get update -qq || refreshed=$?
        fi
    elif [ -x "$(command -v dnf)" ]; then
        ${sudo_cmd} dnf check-update || refreshed=$?
    elif [ -x "$(command -v zypper)" ]; then
        ${sudo_cmd} zypper update || refreshed=$?
    elif [ -x "$(command -v yum)" ]; then
        ${sudo_cmd} yum update || refreshed=$?
    fi
    ColorReset

    if ((refreshed == 124)); then
        Show 3 "apt did not finish refreshing the package lists in ${APT_UPDATE_TIMEOUT} seconds (a mirror that does not answer). Continuing with the lists this host already has."
        return 0
    fi
    if ((refreshed != 0)); then
        Show 3 "Could not refresh the package lists (exit ${refreshed}). Continuing with the lists this host already has; a repository of its own being stale is not a reason to stop upgrading CasaOS."
        return 0
    fi

    Show 0 "Update package manager complete."
}

# A dependency apt could not install. One cause is not the host's to mend by
# retrying: a repository that still publishes its index but no longer serves the
# files it lists, which apt reports as "404 Not Found" and which is what a release
# out of support looks like. Debian 11 is that today (its security repository
# lists ntfs-3g 2017.3.23AR.3-4+deb11u5 and has deleted it), and the fix is known:
# take that repository from archive.debian.org. It is the owner's sources, so the
# installer changes them only for that cause, seen in apt's own words, and only
# when told to or when the owner says yes on the terminal. Any other failure, on
# any system, gets the explanation and no change.

# Whether an answer typed at the prompt is a yes.
Answer_Is_Yes() {
    [[ "$1" == [yY] || "$1" == [yY][eE][sS] ]]
}

# Whether the owner agrees to that change: the option or the variable said so in
# advance, or they answer yes. A run with no terminal to ask on (the in-app update,
# output going to a log) never asks, and so never changes the sources.
Debian_Archive_Agreed() {
    local package="$1" answer=""
    if ((DEBIAN_ARCHIVE)); then return 0; fi
    # a terminal that can be written to and read from, not only a stdout that is one
    [[ -t 1 ]] && { : </dev/tty; } 2>/dev/null || return 1
    {
        echo -e "Debian 11 is out of support: its security repository lists packages it no longer serves, so \e[33m$package \e[0mcannot be downloaded (apt said 404 Not Found)."
        echo "Point apt at Debian's archive for that repository now? The first copy of ${APT_SOURCES_LIST} is kept as ${APT_SOURCES_LIST}.recasaos.bak."
        printf 'Change it? [y/N] '
    } >/dev/tty
    read -r -t 120 answer </dev/tty || return 1
    Answer_Is_Yes "${answer}"
}

# Take the security repository from the archive, keeping the first copy of the
# file. False when the file has no such repository line to move, or a step fails;
# true when the sources were moved, whether or not every repository then refreshed:
# the retry of the install is what decides.
Use_Debian_Archive() {
    grep -Eq '^[[:space:]]*deb(-src)?[[:space:]].*//(deb|security)\.debian\.org/debian-security' "${APT_SOURCES_LIST}" 2>/dev/null || return 1
    [[ -e "${APT_SOURCES_LIST}.recasaos.bak" ]] || ${sudo_cmd} cp -p "${APT_SOURCES_LIST}" "${APT_SOURCES_LIST}.recasaos.bak" || return 1
    ${sudo_cmd} sed -i -E 's#//(deb|security)\.debian\.org/debian-security#//archive.debian.org/debian-security#' "${APT_SOURCES_LIST}" || return 1
    Show 2 "apt now takes Debian's security updates from archive.debian.org; the first copy of the file is ${APT_SOURCES_LIST}.recasaos.bak."
    ${sudo_cmd} apt-get update -qq || Show 3 "Some repositories could not be refreshed; trying the install anyway."
    return 0
}

Package_Install_Failed() {
    local package="$1" why="" cause_is_404=0 tried=0
    ColorReset
    if [[ "${ID:-}" == debian && "${VERSION_ID:-}" == 11 ]]; then
        # look at what apt says for this package, not at the system's age: a lock,
        # a full disk or a network failure is not this and must not move the sources
        why="$(${sudo_cmd} apt-get -y -qq --download-only install "$package" --no-upgrade 2>&1)" || true
        if grep -Eq '404 +Not Found' <<<"${why}"; then cause_is_404=1; fi
    fi
    if ((cause_is_404)); then
        if Debian_Archive_Agreed "$package" && Use_Debian_Archive; then
            if ${sudo_cmd} apt-get -y -qq install "$package" --no-upgrade; then
                return 0
            fi
            tried=1
        fi
        if ((tried)); then
            Show 3 "apt now takes Debian's security updates from archive.debian.org, but \e[33m$package \e[0mstill could not be installed: its message is above."
        else
            Show 3 "Debian 11 is out of support. Its security repository still lists packages it no longer serves, so apt cannot download \e[33m$package \e[0m(the 404 Not Found above)."
            Show 3 "Point apt at Debian's archive for that repository, keeping a copy of the file, then run this installer again:"
            echo "           sudo sed -i.recasaos.bak -E 's#//(deb|security)\.debian\.org/debian-security#//archive.debian.org/debian-security#' /etc/apt/sources.list${LINE_BREAK}"
            echo "           sudo apt-get update${LINE_BREAK}"
            Show 3 "Or run it again with the option and it does that for you: curl -fsSL https://github.com/ReCasaOS/CasaOS-Install/releases/latest/download/install.sh | sudo bash -s -- --use-debian-archive. Or move this machine to Debian 12 or newer, which is still supported."
        fi
    else
        Show 3 "apt could not install \e[33m$package \e[0m: its message is above. If it says 404 Not Found, a repository of this system lists packages it no longer serves, which is what a release out of support looks like. Move to a supported release, or point apt at the distribution's archive, then run this installer again."
    fi
    Show 1 "Dependency \e[33m$package \e[0mcould not be installed."
}

# Install depends package
Install_Depends() {
    for ((i = 0; i < ${#CASA_DEPANDS_COMMAND[@]}; i++)); do
        cmd=${CASA_DEPANDS_COMMAND[i]}
        if [[ ! -x $(${sudo_cmd} which "$cmd") ]]; then
            packagesNeeded=${CASA_DEPANDS_PACKAGE[i]}
            Show 2 "Install the necessary dependencies: \e[33m$packagesNeeded \e[0m"
            GreyStart
            if [ -x "$(command -v apk)" ]; then
                ${sudo_cmd} apk add --no-cache "$packagesNeeded"
            elif [ -x "$(command -v apt-get)" ]; then
                ${sudo_cmd} apt-get -y -qq install "$packagesNeeded" --no-upgrade || Package_Install_Failed "$packagesNeeded"
            elif [ -x "$(command -v dnf)" ]; then
                ${sudo_cmd} dnf install "$packagesNeeded"
            elif [ -x "$(command -v zypper)" ]; then
                ${sudo_cmd} zypper install "$packagesNeeded"
            elif [ -x "$(command -v yum)" ]; then
                ${sudo_cmd} yum install -y "$packagesNeeded"
            elif [ -x "$(command -v pacman)" ]; then
                ${sudo_cmd} pacman -S "$packagesNeeded"
            elif [ -x "$(command -v paru)" ]; then
                ${sudo_cmd} paru -S "$packagesNeeded"
            else
                Show 1 "Package manager not found. You must manually install: \e[33m$packagesNeeded \e[0m"
            fi
            ColorReset
        fi
    done
}

Check_Dependency_Installation() {
    for ((i = 0; i < ${#CASA_DEPANDS_COMMAND[@]}; i++)); do
        cmd=${CASA_DEPANDS_COMMAND[i]}
        if [[ ! -x $(${sudo_cmd} which "$cmd") ]]; then
            packagesNeeded=${CASA_DEPANDS_PACKAGE[i]}
            Show 1 "Dependency \e[33m$packagesNeeded \e[0m installation failed, please try again manually!"
            exit 1
        fi
    done
}

Install_Optional_SMB_Discovery_Package() {
    local package_name="$1"
    local command_name="$2"
    local install_status=0

    if [[ -x "$(command -v "${command_name}" 2>/dev/null)" ]]; then
        return 0
    fi

    Show 2 "Install optional SMB discovery dependency: \e[33m${package_name}\e[0m"
    GreyStart
    if command -v apk >/dev/null 2>&1; then
        ${sudo_cmd} apk add --no-cache "${package_name}" || install_status=$?
    elif command -v apt-get >/dev/null 2>&1; then
        ${sudo_cmd} apt-get -y -qq install "${package_name}" --no-upgrade || install_status=$?
    elif command -v dnf >/dev/null 2>&1; then
        ${sudo_cmd} dnf install -y "${package_name}" || install_status=$?
    elif command -v zypper >/dev/null 2>&1; then
        ${sudo_cmd} zypper --non-interactive install "${package_name}" || install_status=$?
    elif command -v yum >/dev/null 2>&1; then
        ${sudo_cmd} yum install -y "${package_name}" || install_status=$?
    elif command -v pacman >/dev/null 2>&1; then
        ${sudo_cmd} pacman --noconfirm --needed -S "${package_name}" || install_status=$?
    elif command -v paru >/dev/null 2>&1; then
        ${sudo_cmd} paru --noconfirm --needed -S "${package_name}" || install_status=$?
    else
        install_status=1
    fi
    ColorReset

    if ((install_status != 0)) || [[ ! -x "$(command -v "${command_name}" 2>/dev/null)" ]]; then
        Show 3 "Optional SMB discovery dependency ${package_name} is unavailable; continuing without ${command_name}."
    else
        Show 0 "Optional SMB discovery dependency ${package_name} installed."
    fi
}

Install_SMB_Discovery_Depends() {
    local avahi_package
    local wsdd_package="wsdd"

    if command -v apt-get >/dev/null 2>&1; then
        avahi_package="avahi-daemon"
    elif command -v apk >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1 || \
        command -v zypper >/dev/null 2>&1 || command -v yum >/dev/null 2>&1 || \
        command -v pacman >/dev/null 2>&1 || command -v paru >/dev/null 2>&1; then
        avahi_package="avahi"
    else
        Show 3 "No supported package manager found for optional SMB discovery dependencies."
        return 0
    fi

    Install_Optional_SMB_Discovery_Package "${avahi_package}" "avahi-daemon"
    Install_Optional_SMB_Discovery_Package "${wsdd_package}" "wsdd"
}

# Check Docker running
Check_Docker_Running() {
    for ((i = 1; i <= 3; i++)); do
        sleep 3
        if [[ ! $(${sudo_cmd} systemctl is-active docker) == "active" ]]; then
            Show 1 "Docker is not running, try to start"
            ${sudo_cmd} systemctl start docker
        else
            break
        fi
    done
}

#Check Docker Installed and version
Check_Docker_Install() {
    if [[ -x "$(command -v docker)" ]]; then
        # Under set -e a failing command substitution ends the script on its own line,
        # so the question is asked in an if: a docker on the PATH whose daemon does not
        # answer is said plainly, and nothing is reinstalled over it.
        if ! Docker_Version=$(${sudo_cmd} docker version --format '{{.Server.Version}}' 2>/dev/null); then
            Show 1 "Docker is installed but does not answer. Start it (sudo systemctl start docker) and run this installer again."
        elif [[ ${Docker_Version%%.*} -lt "${MINIMUM_DOCKER_VERSION}" ]]; then
            Show 1 "Recommended minimum Docker version is \e[33m${MINIMUM_DOCKER_VERSION}.xx.xx\e[0m,\nCurrent Docker version is \e[33m${Docker_Version}\e[0m,\nPlease uninstall current Docker and rerun the CasaOS installation script."
            exit 1
        else
            Show 0 "Current Docker version is ${Docker_Version}."
        fi
    else
        Install_Docker
    fi
}

# Check Docker installed
Check_Docker_Install_Final() {
    if [[ -x "$(command -v docker)" ]]; then
        # asked in an if for the reason given at Check_Docker_Install; and a Docker
        # that does not answer straight after it was installed is said, not installed
        # again by the function that installs it
        if ! Docker_Version=$(${sudo_cmd} docker version --format '{{.Server.Version}}' 2>/dev/null); then
            Show 1 "Docker was installed but does not answer. See 'sudo systemctl status docker', then run this installer again."
        elif [[ ${Docker_Version%%.*} -lt "${MINIMUM_DOCKER_VERSION}" ]]; then
            Show 1 "Recommended minimum Docker version is \e[33m${MINIMUM_DOCKER_VERSION}.xx.xx\e[0m,\nCurrent Docker version is \e[33m${Docker_Version}\e[0m,\nPlease uninstall current Docker and rerun the CasaOS installation script."
            exit 1
        else
            Show 0 "Current Docker version is ${Docker_Version}."
            Check_Docker_Running
        fi
    else
        Show 1 "Installation failed, please run 'curl -fsSL https://get.docker.com | bash' and rerun the CasaOS installation script."
        exit 1
    fi
}

#Install Docker
Install_Docker() {
    Show 2 "Install the necessary dependencies: \e[33mDocker \e[0m"
    if [[ ! -d "${PREFIX}/etc/apt/sources.list.d" ]]; then
        ${sudo_cmd} mkdir -p "${PREFIX}/etc/apt/sources.list.d"
    fi
    GreyStart
    if [[ "${REGION}" = "China" ]] || [[ "${REGION}" = "CN" ]]; then
        ${sudo_cmd} curl -fsSL https://play.cuse.eu.org/get_docker.sh | bash -s docker --mirror Aliyun
    else
        ${sudo_cmd} curl -fsSL https://get.docker.com | bash
    fi
    ColorReset
    if [[ $? -ne 0 ]]; then
        Show 1 "Installation failed, please try again."
        exit 1
    else
        Check_Docker_Install_Final
    fi
}

###############################################################################
# Rclone & other components                                                  #
###############################################################################

# Install rclone at the pinned version, from its own release, verified.
#
# This used to run rclone.org's install script, which installs whatever is
# current that day, while Install_Rclone below compared the installed version
# against a fixed "v1.61.1" that the script never installed. So a fresh box got
# the current release, and every upgrade after that found a version other than
# the one it named, deleted the binary out from under the running daemon, and
# downloaded the current one again. The pin is real now, and the archive is
# checked against a digest written in at release time like every other package.
Install_rclone_from_source() {
  local archive="rclone-${RCLONE_TAG}-linux-${RCLONE_ARCH}.zip"
  local url="${CASA_DOWNLOAD_DOMAIN}rclone/rclone/releases/download/${RCLONE_TAG}/${archive}"
  local unpack
  unpack="$(mktemp -d)" || Show 1 "Failed to create a temporary directory for rclone"

  Show 2 "Downloading rclone ${RCLONE_TAG}..."
  GreyStart
  ${sudo_cmd} wget -t 3 -q ${WGET_PROGRESS} -O "${unpack}/${archive}" "${url}" || Show 1 "Failed to download rclone"
  ColorReset

  pushd "${unpack}" >/dev/null
  Verify_Fork_Package "${archive}" "${RCLONE_SHA256}"
  ${sudo_cmd} unzip -q -o -j "${archive}" "rclone-${RCLONE_TAG}-linux-${RCLONE_ARCH}/rclone" -d . || Show 1 "Failed to extract rclone"
  ${sudo_cmd} install -m 0755 -o root -g root rclone "${PREFIX}/usr/bin/rclone" || Show 1 "Failed to install rclone"
  popd >/dev/null
  ${sudo_cmd} rm -rf "${unpack}"

  Show 0 "rclone ${RCLONE_TAG} installed."
}

Install_Rclone() {
  Show 2 "Install the necessary dependencies: Rclone"
  if [[ -x "$(command -v rclone)" ]]; then
    version=$(rclone --version 2>>errors | head -n 1)
    target_version="rclone ${RCLONE_TAG}"
    rclone1="${PREFIX}/usr/share/man/man1/rclone.1.gz"
    if [ "$version" != "$target_version" ]; then
      Show 3 "Will change rclone from $version to $target_version."
      rclone_path=$(command -v rclone)
      ${sudo_cmd} rm -rf "${rclone_path}"
      if [[ -f "$rclone1" ]]; then
        ${sudo_cmd} rm -rf "$rclone1"
      fi
      Install_rclone_from_source
    else
      Show 2 "Target version already installed."
    fi
  else
    Install_rclone_from_source
  fi
  ${sudo_cmd} systemctl enable rclone || Show 3 "Service rclone does not exist."
}

# Install restic at the pinned version, from its own release, verified.
#
# restic is what a backup destination is switched to when it should keep a
# repository instead of stamped copies. It is optional: a box that cannot get it
# still installs, and the switch answers that restic is not installed. A digest
# that does not match is not optional, and ends the install like any package's.
Install_restic_from_source() {
  local version="${RESTIC_TAG#v}"
  local archive="restic_${version}_linux_${RESTIC_ARCH}.bz2"
  local url="${CASA_DOWNLOAD_DOMAIN}restic/restic/releases/download/${RESTIC_TAG}/${archive}"
  local unpack
  unpack="$(mktemp -d)" || {
    Show 3 "Failed to create a temporary directory for restic; skipping it."
    return 0
  }

  Show 2 "Downloading restic ${RESTIC_TAG}..."
  GreyStart
  if ! ${sudo_cmd} wget -t 3 -q ${WGET_PROGRESS} -O "${unpack}/${archive}" "${url}"; then
    ColorReset
    ${sudo_cmd} rm -rf "${unpack}"
    Show 3 "Failed to download restic; skipping it."
    return 0
  fi
  ColorReset

  pushd "${unpack}" >/dev/null
  Verify_Fork_Package "${archive}" "${RESTIC_SHA256}"
  if ${sudo_cmd} bunzip2 -f "${archive}" && ${sudo_cmd} install -m 0755 -o root -g root "restic_${version}_linux_${RESTIC_ARCH}" "${PREFIX}/usr/bin/restic"; then
    Show 0 "restic ${RESTIC_TAG} installed."
  else
    Show 3 "Failed to install restic; skipping it."
  fi
  popd >/dev/null
  ${sudo_cmd} rm -rf "${unpack}"
}

Install_Restic() {
  Show 2 "Install the optional backup tool: restic"
  if [[ -x "$(command -v restic)" ]] && [[ "$(restic version 2>>errors | head -n 1)" == "restic ${RESTIC_TAG#v} "* ]]; then
    Show 2 "Target version already installed."
    return 0
  fi

  Install_restic_from_source
}

#Configuration Addons
Configuration_Addons() {
    Show 2 "Configuration CasaOS Addons"
    #Remove old udev rules
    if [[ -f "${PREFIX}/etc/udev/rules.d/11-usb-mount.rules" ]]; then
        ${sudo_cmd} rm -rf "${PREFIX}/etc/udev/rules.d/11-usb-mount.rules"
    fi

    if [[ -f "${PREFIX}/etc/systemd/system/usb-mount@.service" ]]; then
        ${sudo_cmd} rm -rf "${PREFIX}/etc/systemd/system/usb-mount@.service"
    fi

    #Udevil
    if [[ -f $PREFIX${UDEVIL_CONF_PATH} ]]; then

        # GreyStart
        # Add a devmon user
        USERNAME=devmon
        id ${USERNAME} &>/dev/null || {
            ${sudo_cmd} useradd -M -u 300 ${USERNAME}
            ${sudo_cmd} usermod -L ${USERNAME}
        }

        ${sudo_cmd} sed -i '/exfat/s/, nonempty//g' "$PREFIX"${UDEVIL_CONF_PATH}
        ${sudo_cmd} sed -i '/default_options/s/, noexec//g' "$PREFIX"${UDEVIL_CONF_PATH}
        ${sudo_cmd} sed -i '/^ARGS/cARGS="--mount-options nosuid,nodev,noatime --ignore-label EFI"' "$PREFIX"${DEVMON_CONF_PATH}

        # Add and start Devmon service
        GreyStart
        ${sudo_cmd} systemctl enable devmon@devmon
        ${sudo_cmd} systemctl start devmon@devmon
        ColorReset
        # ColorReset
    fi
}

# Verify a downloaded package before extracting it.
Verify_Fork_Package() {
    local package_file="$1"
    local expected_sha256="$2"

    command -v sha256sum >/dev/null 2>&1 || Show 1 "sha256sum is required to verify CasaOS fork packages"

    if ! printf '%s  %s\n' "${expected_sha256}" "${package_file}" | ${sudo_cmd} sha256sum --check --status -; then
        Show 1 "Checksum verification failed for ${package_file}"
    fi

    Show 0 "Verified ${package_file}"
}

# What this run replaces, for the core's version_changed statistic: the tag the
# previous ReCasaOS release left in fork-release, "upstream" when a casaos
# binary is there without one (a box coming from IceWhale's CasaOS), "new"
# otherwise. Builtins only.
Previous_Release() {
    local previous=""

    if [[ -f "${CASA_STATE_DIR}/fork-release" ]]; then
        read -r previous <"${CASA_STATE_DIR}/fork-release" || true
        previous="${previous//[[:space:]]/}"
    fi

    if [[ -n "${previous}" ]]; then
        echo "${previous}"
    elif command -v casaos >/dev/null 2>&1; then
        echo upstream
    else
        echo new
    fi
}

# The markers of the anonymous statistics (README.md, "Anonymous statistics"),
# written with the services stopped and before the release overlay replaces
# fork-release. upgraded-from, on every run, names what this run replaces: the
# core sends it once as version_changed, or deletes it unsent when statistics
# are off. The dashboard's update button runs this same path.
Write_Telemetry_Markers() {
    local previous
    previous="$(Previous_Release)"
    Show 2 "Previous release: ${previous}"

    ${sudo_cmd} mkdir -p "${CASA_STATE_DIR}" || Show 1 "Failed to create ${CASA_STATE_DIR}"
    ${sudo_cmd} install -m 0600 /dev/null "${CASA_STATE_DIR}/upgraded-from" || Show 1 "Failed to write ${CASA_STATE_DIR}/upgraded-from"
    printf '%s\n' "${previous}" | ${sudo_cmd} tee "${CASA_STATE_DIR}/upgraded-from" >/dev/null || Show 1 "Failed to write ${CASA_STATE_DIR}/upgraded-from"

    # telemetry-off only when asked; the core folds it into telemetry.json at
    # its next start. Without the flag nothing here touches the owner's choice.
    if ((NO_TELEMETRY)); then
        ${sudo_cmd} install -m 0600 /dev/null "${CASA_STATE_DIR}/telemetry-off" || Show 1 "Failed to turn anonymous statistics off"
        Show 0 "Anonymous statistics turned off."
    fi
}

# Download And Install CasaOS
DownloadAndInstallCasaOS() {
    # With -p <build_dir> the packages are neither downloaded nor verified:
    # this block is skipped and the preset directory is used as is; the
    # uninstall script below is still downloaded and verified.
    if [ -z "${BUILD_DIR}" ]; then
        ${sudo_cmd} rm -rf ${TMP_ROOT}
        mkdir -p ${TMP_ROOT} || Show 1 "Failed to create temporary directory"
        TMP_DIR=$(${sudo_cmd} mktemp -d -p ${TMP_ROOT} || Show 1 "Failed to create temporary directory")

        pushd "${TMP_DIR}"

        for PACKAGE in "${CASA_PACKAGES[@]}"; do
            Show 2 "Downloading ${PACKAGE}..."
            GreyStart
            ${sudo_cmd} wget -t 3 -q ${WGET_PROGRESS} -c "${PACKAGE}" || Show 1 "Failed to download package"
            ColorReset
        done

        Verify_Fork_Package \
            "linux-${TARGET_ARCH}-casaos-gateway-${CASAOS_GATEWAY_TAG}.tar.gz" \
            "${CASAOS_GATEWAY_SHA256}"
        Verify_Fork_Package \
            "linux-${TARGET_ARCH}-casaos-message-bus-${CASAOS_MESSAGE_BUS_TAG}.tar.gz" \
            "${CASAOS_MESSAGE_BUS_SHA256}"
        Verify_Fork_Package \
            "linux-${TARGET_ARCH}-casaos-user-service-${CASAOS_USER_SERVICE_TAG}.tar.gz" \
            "${CASAOS_USER_SERVICE_SHA256}"
        Verify_Fork_Package \
            "linux-${TARGET_ARCH}-casaos-local-storage-${CASAOS_LOCAL_STORAGE_TAG}.tar.gz" \
            "${CASAOS_LOCAL_STORAGE_SHA256}"
        Verify_Fork_Package \
            "linux-${TARGET_ARCH}-casaos-app-management-${CASAOS_APP_MANAGEMENT_TAG}.tar.gz" \
            "${CASAOS_APP_MANAGEMENT_SHA256}"
        Verify_Fork_Package \
            "linux-${TARGET_ARCH}-${CASAOS_CORE_PACKAGE_FILE_PREFIX}.tar.gz" \
            "${CASAOS_CORE_SHA256}"
        Verify_Fork_Package \
            "linux-all-casaos-${CASAOS_UI_TAG}.tar.gz" \
            "${CASAOS_UI_SHA256}"
        Verify_Fork_Package \
            "linux-all-appstore-${CASAOS_APPSTORE_TAG}.tar.gz" \
            "${CASAOS_APPSTORE_SHA256}"
        Verify_Fork_Package \
            "${CASAOS_COMPAT_OVERLAY_FILE}" \
            "${CASAOS_COMPAT_OVERLAY_SHA256}"

        for PACKAGE_FILE in linux-*.tar.gz; do
            Show 2 "Extracting ${PACKAGE_FILE}..."
            GreyStart
            ${sudo_cmd} tar zxf "${PACKAGE_FILE}" || Show 1 "Failed to extract package"
            ColorReset
        done

        BUILD_DIR=$(${sudo_cmd} realpath -e "${TMP_DIR}"/build || Show 1 "Failed to find build directory")

        popd
    fi

    for SERVICE in "${CASA_SERVICES[@]}"; do
        if ${sudo_cmd} systemctl --quiet is-active "${SERVICE}"; then
            Show 2 "Stopping ${SERVICE}..."
            GreyStart
            ${sudo_cmd} systemctl stop "${SERVICE}" || Show 3 "Service ${SERVICE} does not exist."
            STOPPED_CASA_SERVICES+=("${SERVICE}")
            ColorReset
        fi
    done

    # the message bus listened on /tmp/message-bus.sock until v0.4.99; its socket
    # is in the runtime path now, and the old one would only linger
    ${sudo_cmd} rm -f /tmp/message-bus.sock

    Write_Telemetry_Markers

    MIGRATION_SCRIPT_DIR=$(realpath -e "${BUILD_DIR}"/scripts/migration/script.d || Show 1 "Failed to find migration script directory")

    for MIGRATION_SCRIPT in "${MIGRATION_SCRIPT_DIR}"/*.sh; do
        Show 2 "Running ${MIGRATION_SCRIPT}..."

        ${sudo_cmd} bash "${MIGRATION_SCRIPT}" || Show 1 "Failed to run migration script"

    done


    Show 2 "Installing CasaOS..."
    SYSROOT_DIR=$(realpath -e "${BUILD_DIR}"/sysroot || Show 1 "Failed to find sysroot directory")

    # Generate manifest for uninstallation
    MANIFEST_FILE=${BUILD_DIR}/sysroot/var/lib/casaos/manifest
    ${sudo_cmd} touch "${MANIFEST_FILE}" || Show 1 "Failed to create manifest file"

    GreyStart
    find "${SYSROOT_DIR}" -type f | ${sudo_cmd} cut -c ${#SYSROOT_DIR}- | ${sudo_cmd} cut -c 2- | ${sudo_cmd} tee "${MANIFEST_FILE}" >/dev/null || Show 1 "Failed to create manifest file"

    # The copy carries the overlay's fork-release: from here on, a failure
    # puts the previous one back (onExit)
    PREVIOUS_FORK_RELEASE="$(cat "${CASA_STATE_DIR}/fork-release" 2>/dev/null || true)"
    FORK_RELEASE_REPLACED=1
    ${sudo_cmd} cp -rf "${SYSROOT_DIR}"/* / || Show 1 "Failed to install CasaOS"
    CORE_INSTALLED_NOW=1
    ColorReset

    SETUP_SCRIPT_DIR=$(realpath -e "${BUILD_DIR}"/scripts/setup/script.d || Show 1 "Failed to find setup script directory")

    for SETUP_SCRIPT in "${SETUP_SCRIPT_DIR}"/*.sh; do
        Show 2 "Running ${SETUP_SCRIPT}..."
        GreyStart
        ${sudo_cmd} bash "${SETUP_SCRIPT}" || Show 1 "Failed to run setup script"
        ColorReset
    done

    UI_EVENTS_REG_SCRIPT=/etc/casaos/start.d/register-ui-events.sh
    if [[ -f ${UI_EVENTS_REG_SCRIPT} ]]; then
        ${sudo_cmd} chmod +x $UI_EVENTS_REG_SCRIPT
    fi

    # Modify app store configuration
    sed -i "s#https://github.com/IceWhaleTech/_appstore/#${CASA_DOWNLOAD_DOMAIN}IceWhaleTech/_appstore/#g" "$PREFIX/etc/casaos/app-management.conf"

    #Download Uninstall Script
    if [[ -f $PREFIX/tmp/casaos-uninstall ]]; then
        ${sudo_cmd} rm -rf "$PREFIX/tmp/casaos-uninstall"
    fi
    ${sudo_cmd} curl -fsSL "$CASA_UNINSTALL_URL" >"$PREFIX/tmp/casaos-uninstall"
    Verify_Fork_Package "$PREFIX/tmp/casaos-uninstall" "${CASAOS_UNINSTALL_SHA256}"
    ${sudo_cmd} cp -rf "$PREFIX/tmp/casaos-uninstall" $CASA_UNINSTALL_PATH || {
        Show 1 "Download uninstall script failed, Please check if your internet connection is working and retry."
        exit 1
    }

    ${sudo_cmd} chmod +x $CASA_UNINSTALL_PATH

    Install_Rclone

    Install_Restic

    Install_Module_Catalogue

    for SERVICE in "${CASA_SERVICES[@]}"; do
        Show 2 "Starting ${SERVICE}..."
        GreyStart
        # A first start can fail once with everything else starting in the same
        # second (seen on the amd64 leg of the v0.4.83 install check: exit 127 from
        # app-management, clean on the next run). The unit restarts it on its own;
        # give it that chance before calling the install failed.
        if ! ${sudo_cmd} systemctl start "${SERVICE}"; then
            Show 3 "Service ${SERVICE} did not start the first time, trying again in 5 seconds..."
            sleep 5
            ${sudo_cmd} systemctl start "${SERVICE}" || Show 3 "Service ${SERVICE} does not exist."
        fi
        ColorReset
    done
}

Clean_Temp_Files() {
    Show 2 "Clean temporary files..."
    ${sudo_cmd} rm -rf "${TMP_DIR}" || Show 1 "Failed to clean temporary files"
}

Check_Service_status() {
    for SERVICE in "${CASA_SERVICES[@]}"; do
        Show 2 "Checking ${SERVICE}..."
        if [[ $(${sudo_cmd} systemctl is-active "${SERVICE}") == "active" ]]; then
            Show 0 "${SERVICE} is running."
        else
            Show 1 "${SERVICE} is not running, Please reinstall."
            exit 1
        fi
    done
}

# The port the gateway listens on, from its ini file: the key `port` alone. The
# file also has `httpsport` and may have other keys that end in "port", which a
# plain grep for "port" picks up too (and which made the address shown at the
# end of an install read "http://192.168.1.15:https 80"). 80 when the file does
# not say a number.
Get_Gateway_Port() {
    local lines port
    lines=$(${sudo_cmd} cat "${CASA_CONF_PATH}" 2>/dev/null | sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*//p') || lines=""
    port="${lines%%$'\n'*}"
    port="${port//[[:space:]]/}"
    if [[ ! "${port}" =~ ^[0-9]+$ ]]; then
        port=80
    fi
    echo "${port}"
}

# Get the physical NIC IP
Get_IPs() {
    PORT=$(Get_Gateway_Port)
    ALL_NIC=$($sudo_cmd ls /sys/class/net/ | grep -v "$(ls /sys/devices/virtual/net/)")
    for NIC in ${ALL_NIC}; do
        IP=$($sudo_cmd ifconfig "${NIC}" | grep inet | grep -v 127.0.0.1 | grep -v inet6 | awk '{print $2}' | sed -e 's/addr://g')
        if [[ -n $IP ]]; then
            if [[ "$PORT" -eq "80" ]]; then
                echo -e "${GREEN_BULLET} http://$IP (${NIC})"
            else
                echo -e "${GREEN_BULLET} http://$IP:$PORT (${NIC})"
            fi
        fi
    done
}

# Show Welcome Banner
Welcome_Banner() {
    CASA_TAG="$(casaos -v), distribution ${CASAOS_INSTALL_RELEASE_TAG}"

    echo -e "${GREEN_LINE}${aCOLOUR[1]}"
    echo -e " ReCasaOS ${CASA_TAG}${COLOUR_RESET} is running at${COLOUR_RESET}${GREEN_SEPARATOR}"
    echo -e "${GREEN_LINE}"
    Get_IPs
    echo -e " Open your browser and visit the above address."
    echo -e "${GREEN_LINE}"
    echo -e ""
    echo -e " ${aCOLOUR[2]}Distribution    : https://github.com/ReCasaOS/CasaOS-Install"
    echo -e " ${aCOLOUR[2]}Upstream        : https://github.com/IceWhaleTech/CasaOS"
    echo -e " ${aCOLOUR[2]}Special Thanks  : Sabitech  Cp0204"
    echo -e ""
    echo -e " ${COLOUR_RESET}${aCOLOUR[1]}Uninstall       ${COLOUR_RESET}: casaos-uninstall"
    echo -e "${COLOUR_RESET}"
}

# The closing block of every run: whether this box sends anonymous statistics,
# what they contain, and how to turn them off. Off when this run turned them
# off, when that request still waits for the core, or when the core has them off.
Telemetry_Notice() {
    echo -e "${GREEN_LINE}"
    if ((NO_TELEMETRY)) || [[ -e "${CASA_STATE_DIR}/telemetry-off" ]] ||
        ${sudo_cmd} grep -Eqs '"enabled"[[:space:]]*:[[:space:]]*false' "${CASA_STATE_DIR}/telemetry.json"; then
        echo -e " ${aCOLOUR[1]}Anonymous statistics are off.${COLOUR_RESET}"
        echo -e " Upgrades keep them off; the dashboard's settings can turn them on."
    else
        echo -e " ${aCOLOUR[1]}Anonymous statistics are on.${COLOUR_RESET}"
        echo -e " Once a day this box tells the ReCasaOS maintainers which release it runs"
        echo -e " and on what hardware, through PostHog (EU), which keeps the country of"
        echo -e " the connection, never its address. No apps, files, accounts or names."
        echo -e " Turn them off in the dashboard's settings (Anonymous usage statistics),"
        echo -e " or run the installer again with --no-telemetry or RECASAOS_TELEMETRY=0."
    fi
    echo -e " What they contain: https://github.com/ReCasaOS/CasaOS-Install#anonymous-statistics"
    echo -e "${GREEN_LINE}"
    echo -e "${COLOUR_RESET}"
}

###############################################################################
# Modules                                                                     #
###############################################################################

# Whether a text is the name of a module: a lower case letter, then lower case
# letters, digits or hyphens, 32 characters at most. The letters are listed, not
# written as ranges, so that no locale reads more into them than they say.
Valid_Module_Name() {
    case "$1" in
    "" | [!abcdefghijklmnopqrstuvwxyz]* | *[!abcdefghijklmnopqrstuvwxyz0123456789-]*) return 1 ;;
    esac
    [ "${#1}" -le 32 ]
}

# The three numbers of a version tag, "v<major>.<minor>.<patch>" and nothing else (each
# of up to six digits and without a leading zero), on one line; status 1 and nothing
# printed for any other text.
Version_Parts() {
    local rest="${1#v}" a b c part
    [ "${rest}" != "$1" ] || return 1
    case "${rest}" in
    *[!0123456789.]* | .* | *. | *..* | *.*.*.*) return 1 ;;
    *.*.*) ;;
    *) return 1 ;;
    esac
    a="${rest%%.*}"
    b="${rest#*.}"
    c="${b#*.}"
    b="${b%%.*}"
    for part in "${a}" "${b}" "${c}"; do
        case "${part}" in
        0 | [123456789]*) [ "${#part}" -le 6 ] || return 1 ;;
        *) return 1 ;;
        esac
    done
    echo "${a} ${b} ${c}"
}

# Whether the version tag $1 is $2 or later: no for a text that is not a version tag,
# in either place.
Version_At_Least() {
    local got min
    got="$(Version_Parts "$1")" || return 1
    min="$(Version_Parts "$2")" || return 1
    # shellcheck disable=SC2086 # (six numbers, split on purpose)
    set -- ${got} ${min}
    [ "$1" -gt "$4" ] && return 0
    [ "$1" -lt "$4" ] && return 1
    [ "$2" -gt "$5" ] && return 0
    [ "$2" -lt "$5" ] && return 1
    [ "$3" -ge "$6" ]
}

# Runs the core's module assistant (the program and what it is asked, as the arguments)
# with none of the CASAOS_MODULE variables of this environment. The assistant has test
# hooks that it reads from there: a catalogue and an archive of the caller's own, read in
# place of the pinned ones, and the nonce of the core's own jobs. This script runs as root,
# and a root shell that has such variables (a direct run, sudo -E, a CI job) must not hand
# them on. Every variable that starts with CASAOS_MODULE is taken out, not only the ones the
# assistant reads today; the rest of the environment is left as it is.
Run_Module_Assistant() {
    local var
    for var in $(env | sed -n 's/^\(CASAOS_MODULE[A-Za-z0-9_]*\)=.*/\1/p'); do
        set -- -u "${var}" "$@"
    done
    ${sudo_cmd} env "$@"
}

# A name given to --module, added once to the list. A text that is not the name
# of a module ends the install here, before anything on this machine is touched.
Add_Requested_Module() {
    local name="$1" shown
    if ! Valid_Module_Name "${name}"; then
        shown="$(printf '%.40s' "${name}" | tr -c 'A-Za-z0-9._-' '?')"
        Show 1 "--module needs the name of a module: a lower case letter, then lower case letters, digits or hyphens, 32 characters at most (got '${shown}'). Nothing on this machine was changed."
        return 1
    fi
    case " ${MODULES_REQUESTED} " in
    *" ${name} "*) ;;
    *) MODULES_REQUESTED="${MODULES_REQUESTED:+${MODULES_REQUESTED} }${name}" ;;
    esac
}

# Checked once the options are read, still before anything is changed: a release
# that carries no module catalogue offers no module, so --module is refused.
Validate_Module_Request() {
    [ -n "${MODULES_REQUESTED}" ] || return 0
    if [ -z "${CASAOS_MODULE_CATALOGUE_SHA256}" ]; then
        Show 1 "--module cannot be used with this release: it carries no module catalogue, so it offers no module. Nothing on this machine was changed."
    fi
}

# Takes away the catalogue file that is in place. The status says whether there was one to
# take away. Nothing is touched behind a directory that is a link.
Remove_Module_Catalogue() {
    local file="${CASA_MODULE_CATALOGUE_DIR}/catalogue.json"

    [ ! -L "${CASA_MODULE_CATALOGUE_DIR}" ] || return 1
    [ -e "${file}" ] || [ -L "${file}" ] || return 1
    ${sudo_cmd} rm -f "${file}"
}

# Puts the catalogue $1, a file whose SHA-256 digest has been checked, at its place, whole or
# not at all: copied under a name of its own beside the place, and renamed onto it. Status 2
# when something is in the way that is not a plain file (a directory or a link where the file
# goes, a link where its directory is), 1 when a command failed.
Place_Module_Catalogue() {
    local dir="${CASA_MODULE_CATALOGUE_DIR}" staged
    local file="${CASA_MODULE_CATALOGUE_DIR}/catalogue.json"

    [ ! -L "${dir}" ] || return 2
    { [ ! -d "${file}" ] && [ ! -L "${file}" ]; } || return 2
    ${sudo_cmd} install -d -m 0755 "${dir}" || return 1
    staged="$(${sudo_cmd} mktemp "${dir}/.catalogue.json.XXXXXX")" || return 1
    if ! ${sudo_cmd} install -m 0644 "$1" "${staged}" || ! ${sudo_cmd} mv -f "${staged}" "${file}"; then
        ${sudo_cmd} rm -f "${staged}"
        return 1
    fi
    { [ -f "${file}" ] && [ ! -L "${file}" ]; } || return 2
}

# Places the module catalogue of this release at CASA_MODULE_CATALOGUE_DIR/catalogue.json:
# downloaded from the release, compared with the digest this script carries, and only then
# put in place. Whatever is wrong with it stops the catalogue and nothing else: it is said,
# ReCasaOS goes on, and MODULE_CATALOGUE_READY tells Install_Modules whether this run may
# install any module. The file that is in place is the catalogue of an earlier release, which
# says nothing of the release it is for, so one rule covers a release that has no catalogue
# and a refresh that failed: the earlier file is removed (a module that is installed still
# shows as installed, no longer offered by a catalogue).
Install_Module_Catalogue() {
    local sum="${CASAOS_MODULE_CATALOGUE_SHA256}"
    local work reason="" placed

    MODULE_CATALOGUE_READY=0
    if [ -z "${sum}" ]; then
        if Remove_Module_Catalogue; then
            Show 2 "This release carries no module catalogue; the one of an earlier release was removed."
        fi
        return 0
    fi
    if [ "${#sum}" -ne 64 ] || [ -n "$(printf '%s' "${sum}" | tr -d '0123456789abcdef')" ]; then
        reason="was not installed: the digest this installer carries for it is not a SHA-256 digest"
    elif ! work="$(mktemp -d)"; then
        reason="was not installed: no temporary directory could be made"
    else
        Show 2 "Downloading the module catalogue..."
        GreyStart
        if ! ${sudo_cmd} wget -t 3 -q -O "${work}/catalogue.json" "${CASAOS_RELEASE_BASE_URL}/modules-catalogue-${CASAOS_INSTALL_RELEASE_TAG}.json"; then
            reason="could not be downloaded"
        elif ! printf '%s  %s\n' "${sum}" "${work}/catalogue.json" | ${sudo_cmd} sha256sum --check --status -; then
            reason="is not the file this installer expects (its SHA-256 digest differs)"
        else
            placed=0
            Place_Module_Catalogue "${work}/catalogue.json" || placed=$?
            case "${placed}" in
            0) ;;
            2) reason="could not be put in place: something that is not a plain file is where it goes" ;;
            *) reason="could not be put in place" ;;
            esac
        fi
        ColorReset
        ${sudo_cmd} rm -rf "${work}"
    fi

    if [ -n "${reason}" ]; then
        Show 3 "The module catalogue ${reason}. ReCasaOS itself is installed and is not affected; no module can be installed by this run. Run the installer again to try again."
        if Remove_Module_Catalogue; then
            Show 3 "The catalogue of an earlier release was removed, so that no module is offered from it; a module that is installed still shows as installed."
        fi
        return 0
    fi
    MODULE_CATALOGUE_READY=1
    Show 0 "Module catalogue installed."
}

# `casaos module <install|update> <name> --yes` with the core's assistant: its lines as
# they come (the steps and the sentences), then what its result line says
# (CASAOS_MODULE_RESULT <nonce> <name> <ok|refused|failed> <step> <code> <undo> <time>) in
# MODULE_KIND, MODULE_STEP, MODULE_CODE and MODULE_UNDO, and its exit status in
# MODULE_STATUS. Status 1, and nothing run, when no temporary directory could be made.
Run_Module_Job() {
    local verb="$1" name="$2" work line rname=""

    MODULE_STATUS=0 MODULE_KIND="" MODULE_STEP="" MODULE_CODE="" MODULE_UNDO=""
    work="$(mktemp -d)" || return 1
    GreyStart
    # no input, so that it cannot read the rest of a script piped into bash; the
    # status of the assistant is kept in a file, the pipe's being tee's
    { Run_Module_Assistant "${CASA_BIN}" module "${verb}" "${name}" --yes </dev/null 2>&1 || echo "$?" >"${work}/status"; } | tee "${work}/out" || true
    ColorReset
    if [ -s "${work}/status" ]; then
        read -r MODULE_STATUS <"${work}/status" || MODULE_STATUS=1
    fi
    # a run has one result line; were there more, the first would be final, as it is for the core
    line="$(grep '^CASAOS_MODULE_RESULT ' "${work}/out" 2>/dev/null | head -n 1)" || line=""
    ${sudo_cmd} rm -rf "${work}" || true
    if [ -n "${line}" ]; then
        read -r _ _ rname MODULE_KIND MODULE_STEP MODULE_CODE MODULE_UNDO _ <<RESULT_LINE
${line}
RESULT_LINE
        [ "${rname}" = "${name}" ] || MODULE_KIND=""
        MODULE_STEP="$(printf '%.40s' "${MODULE_STEP}" | tr -c 'a-z0-9_-' '?')"
        MODULE_CODE="$(printf '%.60s' "${MODULE_CODE}" | tr -c 'a-z0-9_-' '?')"
    fi
    return 0
}

# One module, installed, then, when the install did not end well, the step and the
# reason from its result line. Nothing here changes the exit status of this script.
Install_One_Module() {
    local name="$1"

    Show 2 "Installing the module ${name}..."
    if ! Run_Module_Job install "${name}"; then
        Show 3 "The module ${name} was not installed: no temporary directory could be made."
        return 0
    fi
    if [ "${MODULE_KIND}" = ok ] && [ "${MODULE_STATUS}" = 0 ]; then
        Show 0 "The module ${name} is installed."
    elif [ "${MODULE_KIND}" = refused ]; then
        Show 3 "The module ${name} was not installed: the assistant refused it (${MODULE_CODE}). Nothing was changed."
    else
        if [ "${MODULE_KIND}" = failed ]; then
            Show 3 "The module ${name} was not installed: it failed at the step ${MODULE_STEP} (${MODULE_CODE})."
            [ "${MODULE_UNDO}" != rollback_incomplete ] || Show 3 "What the install had done could not all be undone."
        else
            Show 3 "The module ${name} was not installed: the assistant did not report a success (exit status ${MODULE_STATUS})."
        fi
        Show 3 "To clean up what a failed install left, run: sudo casaos module remove ${name}   (the packages it installed stay)"
    fi
    return 0
}

# The modules asked for with --module, one after the other, once ReCasaOS is
# installed and its services run. A module that fails is said and the next one is
# tried: the exit status of this installer is the one ReCasaOS earned.
Install_Modules() {
    local name found shown

    [ -n "${MODULES_REQUESTED}" ] || return 0
    # (a catalogue digest that is not empty is what says this release was built with the
    # check of the core it pins against CASAOS_MODULES_MIN_CORE_TAG)
    if [ "${MODULE_CATALOGUE_READY}" != 1 ] || [ -z "${CASAOS_MODULE_CATALOGUE_SHA256}" ]; then
        Show 3 "No module was installed: the module catalogue could not be installed by this run (see above). ReCasaOS itself is installed and is not affected."
        return 0
    fi
    # `module` is run only by the core this run has put in place, and only once that core
    # says a version that has it: a core without it would start as a second server
    if [ "${CORE_INSTALLED_NOW}" != 1 ]; then
        Show 3 "No module was installed: this run did not put the core of this release in place, so ${CASA_BIN} is not known to have the module assistant. ReCasaOS itself is installed and is not affected."
        return 0
    fi
    if [ ! -x "${CASA_BIN}" ]; then
        Show 3 "No module was installed: ${CASA_BIN} is not there. ReCasaOS itself is installed and is not affected."
        return 0
    fi
    found="$("${CASA_BIN}" -v </dev/null 2>/dev/null)" || found=""
    if ! Version_At_Least "${found}" "${CASAOS_MODULES_MIN_CORE_TAG}"; then
        shown="$(printf '%.40s' "${found}" | tr -c 'A-Za-z0-9._-' '?')"
        Show 3 "No module was installed: ${CASA_BIN} did not report a version of ${CASAOS_MODULES_MIN_CORE_TAG} or later, the first that has the module assistant (it reported '${shown}'); an older one would start as a second server. ReCasaOS itself is installed and is not affected."
        return 0
    fi
    for name in ${MODULES_REQUESTED}; do
        Install_One_Module "${name}"
    done
}

# One installed module, updated to the version of the catalogue, and said: updated, nothing
# to update (the assistant refuses an update to the same version or an older one), or not
# updated, with the step that failed and whether the old version was put back. Adds the name
# to MODULES_UPDATED or MODULES_NOT_UPDATED. Nothing here changes the exit status of this script.
Update_One_Module() {
    local name="$1" retry

    retry="Retry from the Modules window of the dashboard, or with: sudo casaos module update ${name}"
    Show 2 "Updating the module ${name}..."
    if ! Run_Module_Job update "${name}"; then
        Show 3 "The module ${name} was not updated: no temporary directory could be made. It keeps its old version. ${retry}"
    elif [ "${MODULE_KIND}" = ok ] && [ "${MODULE_STATUS}" = 0 ]; then
        Show 0 "The module ${name} is updated."
        MODULES_UPDATED="${MODULES_UPDATED:+${MODULES_UPDATED} }${name}"
        return 0
    else
        case "${MODULE_KIND}:${MODULE_CODE}" in
        refused:up_to_date | refused:downgrade)
            Show 2 "The module ${name} has nothing to update (${MODULE_CODE})."
            return 0
            ;;
        refused:*)
            Show 3 "The module ${name} was not updated: the assistant refused it (${MODULE_CODE}). Nothing was changed: it keeps its old version. ${retry}"
            ;;
        failed:backup_not_dropped)
            # (the new version is installed; only the backup of the old one is still there)
            Show 3 "The module ${name} is updated, but the backup of its old version could not be dropped: delete it by hand as the lines above say, or the next install, update or remove of the module puts the old version back."
            MODULES_UPDATED="${MODULES_UPDATED:+${MODULES_UPDATED} }${name}"
            return 0
            ;;
        failed:*)
            Show 3 "The module ${name} was not updated: it failed at the step ${MODULE_STEP} (${MODULE_CODE})."
            case "${MODULE_UNDO}" in
            rolled_back) Show 3 "Its old version was put back and keeps running." ;;
            rolled_back_with_left) Show 3 "Its old version was put back, except files that were found changed and left as they were: the lines above name them." ;;
            rollback_incomplete) Show 3 "Its old version could not all be put back: the lines above say what is left and how to finish." ;;
            esac
            Show 3 "${retry}"
            ;;
        *)
            Show 3 "The module ${name} was not updated: the assistant did not report a success (exit status ${MODULE_STATUS}). ${retry}"
            ;;
        esac
    fi
    MODULES_NOT_UPDATED="${MODULES_NOT_UPDATED:+${MODULES_NOT_UPDATED} }${name}"
    return 0
}

# Whether this run is the update that the core started in casaos-update.service: the
# dashboard's update button and the automatic updates (see Hand_Off_Module_Updates).
Running_Under_Update_Unit() {
    [ -r "/proc/$$/cgroup" ] && grep -Eq '(^|/)casaos-update\.service($|/)' "/proc/$$/cgroup"
}

# While casaos-update.service runs, the assistant refuses to change a module (busy: an update
# of ReCasaOS is at work), and this run is that unit. The updates go to a unit of their own,
# casaos-update-modules.service, which waits for casaos-update.service to end (10 minutes at
# most), then updates each module in a unit of its own, casaos-update-modules-<name>-<time>,
# with systemd-run --wait --pipe: a stop of that unit (a shutdown) asks the assistant alone to
# stop (KillMode=mixed, as the core's own module units), and gives it the 420 seconds of its
# rollback; apt and dpkg are left for the assistant to stop. Each line goes to the log of the
# update. The units have none of this environment, so none of the CASAOS_MODULE variables.
# systemd reads ${NAME}, $$ and % in the words of a unit's command as its own, so the script
# is a file in a root-only directory of CASA_RUN_DIR, which it removes, and no word given to
# systemd-run holds a $ or a %. The arguments are the names, each one that Valid_Module_Name
# passed.
Hand_Off_Module_Updates() {
    local dir="" plain=0
    # shellcheck disable=SC2016 # the unit's own script, given its values as arguments
    local script='trap '\''rm -f -- "$0"; rmdir -- "${0%/*}" 2>/dev/null'\'' EXIT
        trap '\''exit 143'\'' HUP INT TERM
        exec >>"$1" 2>&1
        bin="$2"
        shift 2
        n=0
        while [ "${n}" -lt 300 ]; do
            case "$(systemctl show casaos-update.service --property=ActiveState --value 2>/dev/null)" in
            active | activating | deactivating | reloading) n="$(expr "${n}" + 1)"; sleep 2 ;;
            *) break ;;
            esac
        done
        for name in "$@"; do
            echo "Updating the module ${name}..."
            systemd-run --wait --pipe --collect --unit="casaos-update-modules-${name}-$(date +%s)" --property=Type=exec --property=KillMode=mixed --property=TimeoutStopSec=420 -- "${bin}" module update "${name}" --yes </dev/null ||
                echo "The update of the module ${name} ended with exit status $?: the lines above say whether it is updated, and if not whether its old version was put back. To try again: the Modules window of the dashboard, or sudo casaos module update ${name}"
        done'

    case "${CASA_RUN_DIR}${CASAOS_UPDATE_LOG}${CASA_BIN}$*" in
    *'$'* | *'%'*) ;;
    *) plain=1 ;;
    esac
    if [ "${plain}" = 1 ] && command -v systemd-run >/dev/null 2>&1 &&
        dir="$(${sudo_cmd} mktemp -d "${CASA_RUN_DIR}/casaos-update-modules.XXXXXX")" &&
        printf '%s\n' "${script}" | ${sudo_cmd} tee "${dir}/run.sh" >/dev/null &&
        ${sudo_cmd} systemd-run --quiet --collect --unit=casaos-update-modules --property=Type=exec --property=KillMode=mixed --property=TimeoutStopSec=420 \
            /bin/sh "${dir}/run.sh" "${CASAOS_UPDATE_LOG}" "${CASA_BIN}" "$@"; then
        Show 2 "The modules $* will be updated once this update has finished, by casaos-update-modules.service: what they say is added to ${CASAOS_UPDATE_LOG}, and the Modules window shows the version each one has."
    else
        if [ -n "${dir}" ]; then ${sudo_cmd} rm -rf "${dir}" || true; fi
        Show 3 "The modules $* were not updated: their update could not be handed to a unit of its own, and the assistant refuses one while this update runs. Each keeps its old version. Once this update has finished, update them from the Modules window of the dashboard, or with: sudo casaos module update <name>"
    fi
    return 0
}

# The optional modules that are installed, each updated to the version of this release's
# catalogue, once ReCasaOS runs: the dashboard's update button, the automatic updates and a
# run of the installer by hand all end here, and a module left behind is one built for the
# core and the dashboard of an earlier release. The modules are those that the assistant's own
# list says have a later version in the catalogue (it says so only of a module that is
# installed); nothing is installed and nothing goes back to an earlier version (the assistant
# refuses that). A module whose update fails is said, the assistant puts its old version back,
# and the next one is tried: the exit status of this installer is the one ReCasaOS earned. Off
# with --no-module-update or RECASAOS_MODULE_UPDATE=0. The time of each update is bounded by the
# assistant's own limits on each of its steps (the download, apt, systemctl, the route, the
# rollback); no timeout is put around it, which would stop apt beside it.
Update_Installed_Modules() {
    local found shown work status=0 name names=""

    MODULES_UPDATED="" MODULES_NOT_UPDATED=""
    if [ "${NO_MODULE_UPDATE}" = 1 ]; then
        Show 2 "The installed modules were not updated (--no-module-update or RECASAOS_MODULE_UPDATE=0): each keeps the version it has, and the Modules window offers the update."
        return 0
    fi
    if [ -z "${CASAOS_MODULE_CATALOGUE_SHA256}" ]; then
        Show 2 "No module was updated: this release carries no module catalogue, so it has no version to update one to."
        return 0
    fi
    if [ "${MODULE_CATALOGUE_READY}" != 1 ]; then
        Show 3 "No module was updated: the module catalogue could not be installed by this run (see above), so each module keeps the version it has. ReCasaOS itself is installed and is not affected."
        return 0
    fi
    if [ "${CORE_INSTALLED_NOW}" != 1 ]; then
        Show 3 "No module was updated: this run did not put the core of this release in place, so ${CASA_BIN} is not known to have 'casaos module update'. ReCasaOS itself is installed and is not affected."
        return 0
    fi
    if [ ! -x "${CASA_BIN}" ]; then
        Show 3 "No module was updated: ${CASA_BIN} is not there. ReCasaOS itself is installed and is not affected."
        return 0
    fi
    found="$("${CASA_BIN}" -v </dev/null 2>/dev/null)" || found=""
    if ! Version_At_Least "${found}" "${CASAOS_MODULES_MIN_CORE_TAG}" || ! Version_At_Least "${found}" "${CASAOS_MODULE_UPDATE_MIN_CORE_TAG}"; then
        shown="$(printf '%.40s' "${found}" | tr -c 'A-Za-z0-9._-' '?')"
        Show 3 "No module was updated: ${CASA_BIN} did not report a version of ${CASAOS_MODULE_UPDATE_MIN_CORE_TAG} or later, the first that has 'casaos module update' (it reported '${shown}'). ReCasaOS itself is installed and is not affected."
        return 0
    fi

    work="$(mktemp -d)" || {
        Show 3 "No module was updated: no temporary directory could be made. Each module keeps the version it has."
        return 0
    }
    # The list is one JSON object on one line (docs/os-modules.md of the core): each module is
    # an object whose first key is "name" and whose last, for one installed at a version older
    # than the catalogue's, is "update":{"available":true,...}. Cut before each "{", a module
    # starts a line with its "name" and its update a line with "available" after a line that
    # ends with "update":. A quote inside a string is escaped, so no string can make such a
    # line; the name of an "available" is the "name" before it, and one that is not a module
    # name is dropped. (A line, not the whole list, is what grep and awk are given: a long line
    # makes grep -o slow beyond use.)
    Run_Module_Assistant "${CASA_BIN}" module list </dev/null >"${work}/list" 2>/dev/null || status=$?
    tr '{' '\n' <"${work}/list" >"${work}/lines" 2>/dev/null || true
    if [ "${status}" != 0 ] || ! grep -Eq '"modules": *\[' "${work}/lines" 2>/dev/null; then
        ${sudo_cmd} rm -rf "${work}" || true
        Show 3 "No module was updated: 'casaos module list' did not give the list of the modules (exit status ${status}). Each module keeps the version it has, and the Modules window offers the update."
        return 0
    fi
    # (a pipe and not a file of names: nothing here can end the install under set -e)
    names="$(awk '/^ *"name": *"/ { name = $0; sub(/^ *"name": *"/, "", name); i = index(name, "\""); name = i ? substr(name, 1, i - 1) : "" }
        /^ *"available": *true *[,}]/ && prev ~ /"update": *$/ { print name; name = "" }
        { prev = $0 }' "${work}/lines" 2>/dev/null | while IFS= read -r name; do
        if Valid_Module_Name "${name}"; then echo "${name}"; fi
    done | awk '!seen[$0]++' | tr '\n' ' ')" || names=""
    ${sudo_cmd} rm -rf "${work}" || true

    if [ -z "${names}" ]; then
        Show 2 "No installed module has a later version in the catalogue of this release."
        return 0
    fi
    if Running_Under_Update_Unit; then
        # shellcheck disable=SC2086 # (names, split on purpose)
        Hand_Off_Module_Updates ${names}
        return 0
    fi
    for name in ${names}; do
        Update_One_Module "${name}"
    done
    if [ -n "${MODULES_UPDATED}" ]; then
        Show 0 "Modules updated to the versions of this release: ${MODULES_UPDATED}."
    fi
    if [ -n "${MODULES_NOT_UPDATED}" ]; then
        Show 3 "Modules NOT updated, each at its old version unless its lines above say otherwise: ${MODULES_NOT_UPDATED}. Retry from the Modules window of the dashboard, or with: sudo casaos module update <name>"
    fi
}

###############################################################################
# Main                                                                        #
###############################################################################

#Usage
usage() {
    cat <<EOF
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
EOF
    exit "$1"
}

# "-:" makes getopts hand over a long option: --no-telemetry arrives as "-",
# with OPTARG=no-telemetry.
while getopts ":p:h-:" arg; do
    case "$arg" in
    p)
        BUILD_DIR=$OPTARG
        ;;
    h)
        usage 0
        ;;
    -)
        case "$OPTARG" in
        no-telemetry)
            NO_TELEMETRY=1
            ;;
        use-debian-archive)
            DEBIAN_ARCHIVE=1
            ;;
        no-module-update)
            NO_MODULE_UPDATE=1
            ;;
        module)
            # "--module name": getopts leaves the name as the next argument
            eval "module_name=\${${OPTIND}:-}"
            OPTIND=$((OPTIND + 1))
            Add_Requested_Module "${module_name}"
            ;;
        module=*)
            Add_Requested_Module "${OPTARG#module=}"
            ;;
        *)
            usage 1
            ;;
        esac
        ;;
    *)
        usage 1
        ;;
    esac
done
Validate_Module_Request

# Existing CasaOS releases start this installer inside casaos.service. Move the
# real update into its own systemd unit before the installer stops CasaOS.
Detach_From_CasaOS_Service

# Step 0 : Get Download Url Domain
# Step 1: Check ARCH
Check_Arch

# Step 2: Check OS
Check_OS

# Step 3: Check Distribution
Check_Distribution

# Step 4: Check System Required
Check_Memory
Check_Disk

# Step 5: Install Depends
Update_Package_Resource
Install_Depends
Check_Dependency_Installation
Install_SMB_Discovery_Depends

# Step 6: Check And Install Docker
Check_Docker_Install

# Step 7: Configuration Addon
Configuration_Addons

# Step 8: Download And Install CasaOS
DownloadAndInstallCasaOS

# Step 9: Check Service Status
Check_Service_status
INSTALL_COMPLETED=1

# The dashboard's update dialog waits for this line in its log.
if ! [[ -t 1 ]]; then
    Show 0 "CasaOS upgrade successfully"
fi

# The modules asked for with --module, then the installed modules updated to this
# release's catalogue; whatever happens to them, the exit status of this script
# stays the one of the install above
Install_Modules
Update_Installed_Modules

# Step 10: Clear Term and Show Welcome Banner
Welcome_Banner

# Step 11: Anonymous statistics, on or off, and how to turn them off
Telemetry_Notice
