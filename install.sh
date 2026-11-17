#!/usr/bin/env bash
# Bot Office for Hermes — installer (Linux / macOS)
# Copies the plugin into $HERMES_HOME/plugins/bot-office and registers
# "bot-office" in config.yaml plugins.enabled (the dashboard does not
# auto-mount new plugins, so this step is required). Existing installs
# and config.yaml are backed up first. Safe to re-run.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bot-office"
PLUGINS_DIR="$HERMES_HOME/plugins"
DST="$PLUGINS_DIR/bot-office"
STAMP="$(date +%Y%m%d%H%M%S)"

if [ ! -d "$SRC_DIR/dashboard" ]; then
  echo "error: bot-office/dashboard not found next to this script." >&2
  exit 1
fi
mkdir -p "$PLUGINS_DIR"

if [ -d "$DST" ]; then
  cp -a "$DST" "$PLUGINS_DIR/bot-office-backup-$STAMP"
  echo "Existing install backed up to $PLUGINS_DIR/bot-office-backup-$STAMP"
fi
rm -rf "$DST"
cp -r "$SRC_DIR" "$DST"
echo "Plugin copied to $DST"

# --- register in config.yaml plugins.enabled -------------------------------
CFG="$HERMES_HOME/config.yaml"
if [ ! -f "$CFG" ]; then
  echo "warning: $CFG not found. After Hermes creates it, add this and restart the dashboard:"
  echo "  plugins:"
  echo "    enabled:"
  echo "      - bot-office"
else
  python3 - "$CFG" "$STAMP" <<'PYEOF'
import sys, os, shutil

cfg_path, stamp = sys.argv[1], sys.argv[2]

def backup():
    bak = cfg_path + ".bak-bot-office-" + stamp
    shutil.copy(cfg_path, bak)
    return bak

try:
    import yaml
except ImportError:
    yaml = None

if yaml is not None:
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    plugins = cfg.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if "bot-office" in enabled:
        print("config.yaml: bot-office already in plugins.enabled")
    else:
        bak = backup()
        enabled.append("bot-office")
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        print("config.yaml: bot-office added to plugins.enabled (backup: %s)" % bak)
    sys.exit(0)

# Fallback without PyYAML: careful line-based insert into the enabled list.
lines = open(cfg_path, encoding="utf-8").read().splitlines()
out, in_plugins, in_enabled, inserted = [], False, False, False
item_indent = None
for i, line in enumerate(lines):
    out.append(line)
    stripped = line.strip()
    indent = len(line) - len(line.lstrip())
    if stripped.startswith("plugins:"):
        in_plugins = True
        continue
    if in_plugins and indent == 0 and stripped:
        in_plugins, in_enabled = False, False
    if in_plugins and stripped.startswith("enabled:"):
        in_enabled = True
        continue
    if in_enabled:
        if stripped.startswith("- "):
            item_indent = indent
            if stripped == "- bot-office":
                inserted = True  # already there; suppress duplicate insert
        elif stripped and item_indent is not None and not inserted:
            out.insert(len(out) - 1, " " * item_indent + "- bot-office")
            inserted = True
            in_enabled = False
if in_enabled and item_indent is not None and not inserted:
    out.append(" " * item_indent + "- bot-office")
    inserted = True

if inserted and any(l.strip() == "- bot-office" for l in out):
    bak = backup()
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("config.yaml: bot-office added to plugins.enabled (backup: %s)" % bak)
else:
    print("Could not edit config.yaml automatically. Add 'bot-office' under plugins.enabled manually:")
    print("  plugins:")
    print("    enabled:")
    print("      - bot-office")
PYEOF
fi

echo
echo "Done. Restart the Hermes dashboard to load Bot Office:"
echo "  systemctl restart hermes-dashboard   # if installed as a service"
echo "Then open the dashboard and look for the 'Bot Office' tab."
