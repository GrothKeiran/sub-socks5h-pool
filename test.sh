#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .cache/tmp .cache/tests
export TMPDIR="$PWD/.cache/tmp"
export XDG_CACHE_HOME="$PWD/.cache"
export PYTHONPYCACHEPREFIX="$PWD/.cache/pycache"
exec python3 -W error::ResourceWarning -m unittest discover -s tests -v
