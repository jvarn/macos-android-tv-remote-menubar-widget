#!/bin/bash

plutil -replace CFBundleIdentifier -string "com.jvarn.TVVolumeApp" payload/TVVolumeApp.app/Contents/Info.plist
plutil -replace CFBundleIconFile -string "AppIcon" payload/TVVolumeApp.app/Contents/Info.plist
plutil -replace CFBundleIconName -string "AppIcon" payload/TVVolumeApp.app/Contents/Info.plist
cp ./payload/AppIcon.icns ./payload/TVVolumeApp.app/Contents/Resources/
mkdir -p ./built

if [ ! -d "scripts/androidtvremote2" ]; then
    echo "Fetching dependencies."
    git clone https://github.com/tronikos/androidtvremote2.git
    mv ./androidtvremote2 scripts/
fi

pkgbuild --root ./payload \
         --identifier "com.jvarn.TVVolumeApp" \
         --version "1.0.0" \
         --install-location "/Applications" \
         ./built/App_Component.pkg
pkgbuild --identifier "com.jvarn.TVVolumeAppSupport" \
         --version "1.0.0" \
         --scripts ./scripts \
         --nopayload \
         ./built/Support_Component.pkg
productbuild --package ./built/App_Component.pkg \
             --package ./built/Support_Component.pkg \
             ./built/TVVolumeApp_Installer.pkg

rm ./built/Support_Component.pkg && rm ./built/App_Component.pkg

echo "Done"