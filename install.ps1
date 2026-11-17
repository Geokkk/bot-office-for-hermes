# Bot Office for Hermes — installer (Windows)
# Copies the plugin into $HERMES_HOME\plugins\bot-office and registers
# "bot-office" in config.yaml plugins.enabled (the dashboard does not
# auto-mount new plugins, so this step is required). Existing installs
# and config.yaml are backed up first. Safe to re-run.
$ErrorActionPreference = "Stop"

$HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $HOME ".hermes" }
$SrcDir = Join-Path $PSScriptRoot "bot-office"
$PluginsDir = Join-Path $HermesHome "plugins"
$Dst = Join-Path $PluginsDir "bot-office"
$Stamp = Get-Date -Format "yyyyMMddHHmmss"

if (-not (Test-Path (Join-Path $SrcDir "dashboard"))) {
    Write-Error "bot-office\dashboard not found next to this script."
    exit 1
}
New-Item -ItemType Directory -Force -Path $PluginsDir | Out-Null

if (Test-Path $Dst) {
    $Bak = Join-Path $PluginsDir "bot-office-backup-$Stamp"
    Copy-Item -Recurse -Force $Dst $Bak
    Write-Host "Existing install backed up to $Bak"
    Remove-Item -Recurse -Force $Dst
}
Copy-Item -Recurse -Force $SrcDir $Dst
Write-Host "Plugin copied to $Dst"

# --- register in config.yaml plugins.enabled -------------------------------
$Cfg = Join-Path $HermesHome "config.yaml"
if (-not (Test-Path $Cfg)) {
    Write-Host "warning: $Cfg not found. After Hermes creates it, add this and restart the dashboard:"
    Write-Host "  plugins:"
    Write-Host "    enabled:"
    Write-Host "      - bot-office"
} else {
    $Py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Py) { $Py = Get-Command py -ErrorAction SilentlyContinue }
    if ($Py) {
        $Snippet = @'
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
else:
    print("PyYAML not available. Add 'bot-office' under plugins.enabled in config.yaml manually:")
    print("  plugins:")
    print("    enabled:")
    print("      - bot-office")
'@
        $Tmp = Join-Path $env:TEMP "bot-office-register.py"
        Set-Content -Path $Tmp -Value $Snippet -Encoding UTF8
        & $Py.Source $Tmp $Cfg $Stamp
        Remove-Item $Tmp -Force
    } else {
        Write-Host "Python not found. Add 'bot-office' under plugins.enabled in config.yaml manually:"
        Write-Host "  plugins:"
        Write-Host "    enabled:"
        Write-Host "      - bot-office"
    }
}

Write-Host ""
Write-Host "Done. Restart the Hermes dashboard, then open it and look for the 'Bot Office' tab."
