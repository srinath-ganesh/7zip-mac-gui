#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export SEVEN_ZZ="${SEVEN_ZZ:-/Applications/7z2600-mac/7zz}"
if [[ ! -f "$SEVEN_ZZ" ]]; then
  echo "error: 7zz not found at: $SEVEN_ZZ" >&2
  echo "Set SEVEN_ZZ to your 7zz path, or install 7-Zip to that location." >&2
  exit 1
fi

echo "Building the application with PyInstaller..."
python3 -m PyInstaller --noconfirm --clean "7Zip-Master-GUI.spec"

echo "Creating Beautiful DMG..."

# 1. Clean up old DMGs if they exist so create-dmg doesn't fail
rm -f ./dist/7Zip-Master-GUI.dmg

# 2. Create a temporary staging folder
mkdir -p ./dist/dmg_stage
cp -R ./dist/7Zip-Master-GUI.app ./dist/dmg_stage/

# 3. Use create-dmg to build the styled disk image
create-dmg \
  --volname "7Zip-Master" \
  --background "dmg_background.png" \
  --window-pos 200 120 \
  --window-size 600 400 \
  --icon-size 110 \
  --icon "7Zip-Master-GUI.app" 150 190 \
  --hide-extension "7Zip-Master-GUI.app" \
  --app-drop-link 450 190 \
  "./dist/7Zip-Master-GUI.dmg" \
  "./dist/dmg_stage/"

# 4. Clean up the temporary folder
rm -rf ./dist/dmg_stage

echo ""
echo "Build complete! You can find your styled 7Zip-Master-GUI.dmg in the dist/ folder."