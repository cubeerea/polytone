#!/bin/sh
# Build the capture probe app bundle: build.sh <out-dir> → <out-dir>/CaptureProbe.app
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:?usage: build.sh <out-dir>}
app="$out/CaptureProbe.app"
mkdir -p "$app/Contents/MacOS"
cat > "$app/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>dev.polytone.captureprobe</string>
  <key>CFBundleName</key><string>CaptureProbe</string>
  <key>CFBundleExecutable</key><string>CaptureProbe</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
EOF
swiftc -O -o "$app/Contents/MacOS/CaptureProbe" "$here/main.swift" -framework AppKit -framework WebKit
echo "$app"
