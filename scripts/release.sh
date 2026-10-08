#!/bin/sh
set -eu
exec python3 "$(dirname "$0")/release-web.py" "$@"
