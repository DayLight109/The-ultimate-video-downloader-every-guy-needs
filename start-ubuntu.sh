#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export AVTOOL_ROOT="$PWD"
mode="${1:-web}"
case "$mode" in setup|web|crawler|import|doctor) ;; *) echo 'Usage: ./start-ubuntu.sh [setup|web|crawler|import|doctor] [arguments]'; exit 2;; esac
if [[ $# -gt 0 ]]; then shift; fi
command -v python3 >/dev/null || { echo 'Install Python 3.10+ and python3-venv.'; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || { echo 'Ubuntu: sudo apt install python3-venv'; exit 1; }
fi
if ! .venv/bin/python -c 'import yt_dlp.version; assert yt_dlp.version.__version__ == "2026.08.19"' 2>/dev/null; then
  .venv/bin/python -m pip install -r requirements.txt
fi
if [[ ! -f web/out/index.html ]]; then
  command -v npm >/dev/null || { echo 'Install Node.js 22.12+ to build the Web UI locally. Source archives do not include built assets.'; exit 1; }
  node -e 'const [m,n]=process.versions.node.split(".").map(Number); if(m<22||(m===22&&n<12))process.exit(1)'
  npm ci --ignore-scripts
  npm run build
fi
if [[ "$mode" == setup ]]; then
  exec .venv/bin/python run.py doctor
fi
exec .venv/bin/python run.py "$mode" "$@"
