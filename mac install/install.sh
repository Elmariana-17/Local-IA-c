#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/com.localai.bridge.plist"
PYTHON=$(which python3)

echo "[1/3] Creando entorno virtual..."
cd "$PROJECT_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

echo "[2/3] Generando archivo launchd plist..."
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.localai.bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_DIR/.venv/bin/python</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>core.main:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8001</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/localai-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/localai-bridge.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "[3/3] Cargando servicio..."
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "   Instalación completada. API en http://127.0.0.1:8001"
echo "   Asegúrate de que mlx_lm.server esté en ejecución: mlx_lm.server --model <modelo> --port 8080"