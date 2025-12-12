#!/bin/bash

SCRIPTPATH=$(dirname -- "$(readlink -f -- "$0")")
KSPATH=$(dirname "$SCRIPTPATH")
KSENV="${HOTKEYCOMPANION_VENV:-${HOME}/.HotkeyCompanion-env}"