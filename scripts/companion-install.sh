#!/bin/bash

### credits to th33xitus for the script base
clear
set -e

SCRIPTPATH=$(dirname -- "$(readlink -f -- "$0")")
HCPATH=$(dirname "$SCRIPTPATH")
HCENV="${HOTKEYCOMPANION_VENV:-${HOME}/.HotkeyCompanion-env}"
HCCONFIGPATH="/home/$(whoami)/printer_data/config/hotkey-companion.cfg"

PACKAGES="python3-venv python3-dev"

### set color variables
green=$(echo -en "\e[92m")
yellow=$(echo -en "\e[93m")
red=$(echo -en "\e[91m")
cyan=$(echo -en "\e[96m")
default=$(echo -en "\e[39m")

warn_msg(){
  echo -e "${red}<!!!!> $1${default}"
}

status_msg(){
  echo; echo -e "${yellow}###### $1${default}"
}

ok_msg(){
  echo -e "${green}>>>>>> $1${default}"
}

title_msg(){
  echo -e "${cyan}$1${default}"
}

get_date(){
  current_date=$(date +"%y%m%d-%H%M")
}

print_unkown_cmd(){
  ERROR_MSG="Invalid command!"
}

print_msg(){
  if [[ "$ERROR_MSG" != "" ]]; then
    echo -e "${red}"
    echo -e "#########################################################"
    echo -e " $ERROR_MSG "
    echo -e "#########################################################"
    echo -e "${default}"
  fi
  if [ "$CONFIRM_MSG" != "" ]; then
    echo -e "${green}"
    echo -e "#########################################################"
    echo -e " $CONFIRM_MSG "
    echo -e "#########################################################"
    echo -e "${default}"
  fi
}

clear_msg(){
  unset CONFIRM_MSG
  unset ERROR_MSG
}

install_packages()
{
    status_msg "Update package data"
    sudo apt update

    status_msg "Checking for broken packages..."
    if dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package}\n' | grep -E "^.[^nci]"; then
        warn_msg "Detected broken packages. Attempting to fix"
        sudo apt -f install
        if dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package}\n' | grep -E "^.[^nci]"; then
            warn_msg "Unable to fix broken packages. These must be fixed before Hotkey Companion can be installed"
            exit 1
        fi
    else
        ok_msg "No broken packages"
    fi

    status_msg "Installing Hotkey Companion dependencies"
    sudo apt install -y $PACKAGES
    echo "$_"
}

check_requirements()
{
    VERSION="3,8"
    status_msg "Checking Python version > "$VERSION
    python3 --version
    if ! python3 -c 'import sys; exit(1) if sys.version_info <= ('$VERSION') else exit(0)'; then
        warn_msg 'Not supported'
        exit 1
    fi
}

create_virtualenv()
{
    if [ "${HCENV}" = "/" ]; then
        warn_msg "Failed to resolve venv location. Aborting."
        exit 1
    fi

    if [ -d "$HCENV" ]; then
        status_msg "Removing old virtual environment"
        rm -rf "${HCENV}"
    fi

    status_msg "Creating virtual environment"
    python3 -m venv "${HCENV}"

    if ! . "${HCENV}/bin/activate"; then
        warn_msg "Could not activate the environment, try deleting ${HCENV} and retry"
        exit 1
    fi

    if [[ "$(uname -m)" =~ armv[67]l ]]; then
        status_msg "Using armv[67]l! Adding piwheels.org as extra index..."
        pip --disable-pip-version-check install --extra-index-url https://www.piwheels.org/simple -r ${HCPATH}/scripts/companion-requirements.txt
    else
        pip --disable-pip-version-check install -r ${HCPATH}/scripts/companion-requirements.txt
    fi
    if [ $? -gt 0 ]; then
        warn_msg "Error: pip install exited with status code $?"
        status_msg "Trying again with new tools..."
        sudo apt install -y build-essential cmake libsystemd-dev
        if [[ "$(uname -m)" =~ armv[67]l ]]; then
            status_msg "Adding piwheels.org as extra index..."
            pip install --extra-index-url https://www.piwheels.org/simple --upgrade pip setuptools
            pip install --extra-index-url https://www.piwheels.org/simple -r ${HCPATH}/scripts/companion-requirements.txt --prefer-binary
        else
            pip install --upgrade pip setuptools
            pip install -r ${HCPATH}/scripts/companion-requirements.txt --prefer-binary
        fi
        if [ $? -gt 0 ]; then
            warn_msg "Unable to install dependencies, aborting install."
            deactivate
            exit 1
        fi
    fi
    deactivate
    ok_msg "Virtual environment created"
}

copy_config_if_missing() {
    local SRC_CFG="${SCRIPTPATH}/hotkey-companion.cfg"

    status_msg "Checking Hotkey Companion config"

    if [ -f "${HCCONFIGPATH}" ]; then
        ok_msg "Config already exists at ${HCCONFIGPATH} (skipping copy)"
        return 0
    fi

    if [ ! -f "${SRC_CFG}" ]; then
        warn_msg "Config not found next to script: ${SRC_CFG}"
        exit 1
    fi

    status_msg "Copying default config to ${HCCONFIGPATH}"
    sudo mkdir -p "$(dirname "${HCCONFIGPATH}")"
    sudo cp -n "${SRC_CFG}" "${HCCONFIGPATH}"
    sudo chown "${USER}:${USER}" "${HCCONFIGPATH}" 2>/dev/null || true
    ok_msg "Config copied to ${HCCONFIGPATH}"
}


install_systemd_service()
{
    status_msg "Installing Hotkey Companion unit file"

    SERVICE=$(cat "$SCRIPTPATH"/HotkeyCompanion.service)
    SERVICE=${SERVICE//HC_USER/$USER}
    SERVICE=${SERVICE//HC_ENV/$HCENV}
    SERVICE=${SERVICE//HC_DIR/$HCPATH}
    SERVICE=${SERVICE//HC_CONFIG_PATH/$HCCONFIGPATH}

    echo "$SERVICE" | sudo tee /etc/systemd/system/HotkeyCompanion.service > /dev/null
    sudo systemctl unmask HotkeyCompanion.service
    sudo systemctl daemon-reload
    sudo systemctl enable HotkeyCompanion
    sudo systemctl set-default multi-user.target
    sudo adduser "$USER" tty
}

start_HotkeyCompanion()
{
    status_msg "Starting service..."
    sudo systemctl restart HotkeyCompanion
}

# Script start
if [ "$EUID" == 0 ]
    then warn_msg "Please do not run this script as root"
    exit 1
fi
check_requirements

install_packages
create_virtualenv
copy_config_if_missing
install_systemd_service
start_HotkeyCompanion