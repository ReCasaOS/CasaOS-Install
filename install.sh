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
readonly CASA_DEPANDS_PACKAGE=('wget' 'curl' 'smartmontools' 'parted' 'ntfs-3g' 'net-tools' 'udevil' 'samba' 'cifs-utils' 'mergerfs' 'unzip' 'git')
readonly CASA_DEPANDS_COMMAND=('wget' 'curl' 'smartctl' 'parted' 'ntfs-3g' 'netstat' 'udevil' 'smbd' 'mount.cifs' 'mount.mergerfs' 'unzip' 'git')

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
readonly CASAOS_APPSTORE_SHA256="__CASAOS_APPSTORE_SHA256__"
readonly CASAOS_COMPAT_OVERLAY_SHA256="__CASAOS_COMPAT_OVERLAY_SHA256__"
readonly CASAOS_UNINSTALL_SHA256="__CASAOS_UNINSTALL_SHA256__"

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
STOPPED_CASA_SERVICES=()
INSTALL_COMPLETED=0

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

    Show 2 "Handing the update to ${update_unit}.service..."
    if ! ${sudo_cmd} systemd-run \
        --quiet \
        --collect \
        --unit="${update_unit}" \
        --property=Type=exec \
        --setenv=CASAOS_INSTALLER_DETACHED=1 \
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
        ${sudo_cmd} apt-get update -qq || refreshed=$?
    elif [ -x "$(command -v dnf)" ]; then
        ${sudo_cmd} dnf check-update || refreshed=$?
    elif [ -x "$(command -v zypper)" ]; then
        ${sudo_cmd} zypper update || refreshed=$?
    elif [ -x "$(command -v yum)" ]; then
        ${sudo_cmd} yum update || refreshed=$?
    fi
    ColorReset

    if ((refreshed != 0)); then
        Show 3 "Could not refresh the package lists (exit ${refreshed}). Continuing with the lists this host already has; a repository of its own being stale is not a reason to stop upgrading CasaOS."
        return 0
    fi

    Show 0 "Update package manager complete."
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
                ${sudo_cmd} apt-get -y -qq install "$packagesNeeded" --no-upgrade
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
        Docker_Version=$(${sudo_cmd} docker version --format '{{.Server.Version}}')
        if [[ $? -ne 0 ]]; then
            Install_Docker
        elif [[ ${Docker_Version:0:2} -lt "${MINIMUM_DOCKER_VERSION}" ]]; then
            Show 1 "Recommended minimum Docker version is \e[33m${MINIMUM_DOCKER_VERSION}.xx.xx\e[0m,\Current Docker version is \e[33m${Docker_Version}\e[0m,\nPlease uninstall current Docker and rerun the CasaOS installation script."
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
        Docker_Version=$(${sudo_cmd} docker version --format '{{.Server.Version}}')
        if [[ $? -ne 0 ]]; then
            Install_Docker
        elif [[ ${Docker_Version:0:2} -lt "${MINIMUM_DOCKER_VERSION}" ]]; then
            Show 1 "Recommended minimum Docker version is \e[33m${MINIMUM_DOCKER_VERSION}.xx.xx\e[0m,\Current Docker version is \e[33m${Docker_Version}\e[0m,\nPlease uninstall current Docker and rerun the CasaOS installation script."
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

    ${sudo_cmd} cp -rf "${SYSROOT_DIR}"/* / || Show 1 "Failed to install CasaOS"
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

# Get the physical NIC IP
Get_IPs() {
    PORT=$(${sudo_cmd} cat ${CASA_CONF_PATH} | grep port | sed 's/port=//')
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

###############################################################################
# Main                                                                        #
###############################################################################

#Usage
usage() {
    cat <<-EOF
		Usage: install.sh [options]
		Valid options are:
		    -p <build_dir>          Specify build directory (Local install)
		    -h                      Show this help message and exit
	EOF
    exit "$1"
}

while getopts ":p:h" arg; do
    case "$arg" in
    p)
        BUILD_DIR=$OPTARG
        ;;
    h)
        usage 0
        ;;
    *)
        usage 1
        ;;
    esac
done

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

# Step 10: Clear Term and Show Welcome Banner
Welcome_Banner
