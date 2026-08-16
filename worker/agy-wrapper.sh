#!/bin/sh
set -eu

runtime_directory="${XDG_RUNTIME_DIR:-/tmp/runtime-$(id -u)}"
mkdir -p "$runtime_directory"
chmod 700 "$runtime_directory"
export XDG_RUNTIME_DIR="$runtime_directory"
mkdir -p "$HOME/.cache" 2>/dev/null || true

exec dbus-run-session -- sh -c '
  eval "$(gnome-keyring-daemon --start --components=secrets)"
  exec /usr/local/libexec/agy "$@"
' sh "$@"
