#!/bin/sh
# One-step POSIX installer for herdr-bot (compatible alias: hq).
set -eu

ASSUME_YES=0
PYTHON_ARG=""

usage() {
    cat <<'EOF'
Usage: ./install.sh [--yes] [--python <path>]

Options:
  --yes, -y         Non-interactive mode (auto-confirm optional installs)
  --python <path>   Explicit Python 3.10+ interpreter path
  --help, -h        Show this help message
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --yes|-y)
            ASSUME_YES=1
            shift
            ;;
        --python)
            if [ "$#" -lt 2 ]; then
                echo "Error: --python requires a path argument." >&2
                exit 1
            fi
            PYTHON_ARG="$2"
            shift 2
            ;;
        --python=*)
            PYTHON_ARG="${1#*=}"
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"

find_python() {
    if [ -n "$PYTHON_ARG" ]; then
        if [ ! -x "$PYTHON_ARG" ]; then
            echo "Error: Python interpreter is not executable: $PYTHON_ARG" >&2
            exit 1
        fi
        printf '%s\n' "$PYTHON_ARG"
        return
    fi
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return
    fi
    echo "Error: python3 (>= 3.10) not found. Pass --python /path/to/python3." >&2
    exit 1
}

PYTHON_BIN="$(find_python)"

if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Error: herdr-bot requires Python >= 3.10 (got: $("$PYTHON_BIN" --version 2>&1))." >&2
    exit 1
fi
echo "[ok] Python: $PYTHON_BIN"

# 1. Check coding agent CLI (`agy`)
if command -v agy >/dev/null 2>&1; then
    echo "[ok] agy: $(command -v agy)"
elif [ -x "${BIN_DIR}/agy" ]; then
    echo "[ok] agy: ${BIN_DIR}/agy"
else
    echo "[warn] agy CLI not found in PATH."
    echo "       Install your coding agent CLI (`agy`) and ensure it is in PATH,"
    echo "       or specify --agy-bin during 'herdr-bot setup'."
fi

# 2. Check Herdr terminal multiplexer (`herdr`)
install_herdr() {
    echo "[info] Installing Herdr via official installer (https://herdr.dev/install.sh) ..."
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL https://herdr.dev/install.sh | sh
    else
        echo "[warn] curl is not installed; install Herdr manually from https://github.com/herdrdev/herdr"
    fi
}

if command -v herdr >/dev/null 2>&1; then
    echo "[ok] herdr: $(command -v herdr)"
elif [ -x "${BIN_DIR}/herdr" ]; then
    echo "[ok] herdr: ${BIN_DIR}/herdr"
else
    if [ "$ASSUME_YES" -eq 1 ]; then
        install_herdr
    elif [ -t 0 ]; then
        printf "Herdr ('herdr') not found. Install now via https://herdr.dev/install.sh? [Y/n] "
        read -r ans || ans="n"
        case "$ans" in
            ""|[Yy]*)
                install_herdr
                ;;
            *)
                echo "[info] Skipping Herdr installation (tmux fallback remains available)."
                ;;
        esac
    else
        echo "[warn] herdr not found. Install anytime with: curl -fsSL https://herdr.dev/install.sh | sh"
    fi
fi

# 3. Install Python dependencies (pyyaml + optional lark-oapi)
pip_install_pkg() {
    pkg_spec="$1"
    if "$PYTHON_BIN" -m pip install "$pkg_spec"; then
        return 0
    fi
    "$PYTHON_BIN" -m pip install --user "$pkg_spec"
}

if "$PYTHON_BIN" -c "import yaml" >/dev/null 2>&1; then
    echo "[ok] Python dependency 'pyyaml' already available"
else
    echo "[info] Installing required dependency: pyyaml>=6.0 ..."
    pip_install_pkg "pyyaml>=6.0"
fi

if "$PYTHON_BIN" -c "import lark_oapi" >/dev/null 2>&1; then
    echo "[ok] Optional dependency 'lark-oapi' (Feishu WebSocket) already available"
else
    if [ "$ASSUME_YES" -eq 1 ]; then
        echo "[info] Installing optional Feishu SDK: lark-oapi>=1.4 ..."
        pip_install_pkg "lark-oapi>=1.4" || echo "[warn] Optional lark-oapi install skipped."
    elif [ -t 0 ]; then
        printf "Install optional Feishu/Lark WebSocket SDK (lark-oapi>=1.4)? [Y/n] "
        read -r fs_ans || fs_ans="n"
        case "$fs_ans" in
            ""|[Yy]*)
                pip_install_pkg "lark-oapi>=1.4"
                ;;
            *)
                echo "[info] Skipped lark-oapi (install later with: $PYTHON_BIN -m pip install 'lark-oapi>=1.4')."
                ;;
        esac
    fi
fi

# 4. Symlink CLI entrypoints into ~/.local/bin
mkdir -p "$BIN_DIR"
chmod +x "$REPO_ROOT/bin/herdr-bot" "$REPO_ROOT/bin/hq" "$REPO_ROOT/bin/hq-agent-hook" "$REPO_ROOT/bin/hq-notify"
ln -sf "$REPO_ROOT/bin/herdr-bot" "$BIN_DIR/herdr-bot"
ln -sf "$REPO_ROOT/bin/hq" "$BIN_DIR/hq"
ln -sf "$REPO_ROOT/bin/hq-agent-hook" "$BIN_DIR/hq-agent-hook"
ln -sf "$REPO_ROOT/bin/hq-notify" "$BIN_DIR/hq-notify"
echo "[ok] Linked herdr-bot & hq -> $BIN_DIR"

case ":${PATH:-}:" in
    *":${BIN_DIR}:"*)
        ;;
    *)
        echo "[note] Add $BIN_DIR to your PATH if not already configured:"
        echo "       export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

echo ""
echo "Installation complete! Run the interactive onboarding wizard next:"
if [ -n "$PYTHON_ARG" ]; then
    echo "  HQ_PYTHON=\"$PYTHON_BIN\" herdr-bot setup"
else
    echo "  herdr-bot setup"
fi
