#!/bin/sh
# Builds ~/Applications/SE Bluetooth.app
set -e
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    echo "Python 3.10 or newer is required (found: $(python3 --version 2>&1)). Try: brew install python" >&2
    exit 1
fi
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/SE Bluetooth.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$SRC/bt.py" "$SRC/obex.py" "$SRC/se_app.py" "$SRC/media.py" "$SRC/se_ui.html" "$APP/Contents/Resources/"
python3 -m venv "$APP/Contents/Resources/venv"
"$APP/Contents/Resources/venv/bin/pip" install -q --disable-pip-version-check -r "$SRC/requirements.txt"
cat > "$APP/Contents/MacOS/SEBluetooth" <<'SH'
#!/bin/sh
R="$(cd "$(dirname "$0")/../Resources" && pwd)"
exec "$R/venv/bin/python" "$R/se_app.py"
SH
chmod +x "$APP/Contents/MacOS/SEBluetooth"
cat > "$APP/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>SE Bluetooth</string>
  <key>CFBundleDisplayName</key><string>SE Bluetooth</string>
  <key>CFBundleIdentifier</key><string>local.se.bluetooth</string>
  <key>CFBundleExecutable</key><string>SEBluetooth</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSBluetoothAlwaysUsageDescription</key><string>SE Bluetooth transfers files to and from your phone over Bluetooth.</string>
</dict></plist>
PL
echo "Done: $APP"
