#!/bin/bash

plutil -replace CFBundleIdentifier -string "com.jvarn.AndroidTVRemote" payload/AndroidTVRemote.app/Contents/Info.plist
plutil -replace CFBundleIconFile -string "AppIcon" payload/AndroidTVRemote.app/Contents/Info.plist
plutil -replace CFBundleIconName -string "AppIcon" payload/AndroidTVRemote.app/Contents/Info.plist
cp ./icon/AppIcon.icns ./payload/AndroidTVRemote.app/Contents/Resources/

# Re-sign after modifying the bundle to restore a valid (ad-hoc) signature
codesign --force --deep --sign - payload/AndroidTVRemote.app

mkdir -p ./built

if [ ! -d "scripts/payload_appsupport/androidtvremote2" ]; then
    echo "Fetching dependencies."
    git clone https://github.com/tronikos/androidtvremote2.git
    mv ./androidtvremote2 scripts/payload_appsupport/
fi

pkgbuild --root ./payload \
         --identifier "com.jvarn.TVRemoteApp" \
         --version "1.0.0" \
         --install-location "/Applications" \
         ./built/App_Component.pkg
pkgbuild --identifier "com.jvarn.AndroidTVRemoteSupport" \
         --version "1.0.0" \
         --scripts ./scripts \
         --nopayload \
         ./built/Support_Component.pkg
productbuild --package ./built/App_Component.pkg \
             --package ./built/Support_Component.pkg \
             ./built/AndroidTVRemote_Installer.pkg

rm ./built/Support_Component.pkg && rm ./built/App_Component.pkg

echo "Done"