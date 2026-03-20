<<<<<<< HEAD
# macOS Android TV Remote Menubar Widget
This app uses [androidtvremote2](https://github.com/tronikos/androidtvremote2) and a custom Python script to create a remote control menu bar widget for Android TVs. This has only been tested on a *TCL Percee TV* running Android 8. If you have a different model of Android television, you may need to modify `tvremote.py`.

## Installation
Download the installer from the [releases page](https://github.com/jvarn/macos-android-tv-remote-menubar-widget/releases/latest).
Run the installer.

### First Run
1. Open AndroidTVRemote from /Applications.
2. When the new TV icon appears in your menu bar, right click and choose Settings.
3. Click on "Re-pair...".
4. Enter your TV's IP address and then enter the pairing code shown on your TV.
5. Enter your TV's IP address to connect and enable the remote control.

## Make It Yourself (Simple)
The only part that contains a pre-compiled binary is the Automator app at `payload/AndroidTVRemote.app`. If you prefer not to trust it, the you can make this part yourself using Automator on macOS.

### Create Automator App
1. Create new Application in Automator.
2. Add "Run Shell Script".
3. Set Pass input to "as arguments" and Shell to "/bin/bash".
4. Paste the following script:
```sh
	APP_PATH="$HOME/Library/Application Support/AndroidTVRemote"
	PYTHON_EXECUTABLE="$APP_PATH/.venv/bin/python3"
	PYTHON_SCRIPT="$APP_PATH/tvremote.py"
	
	cd "$APP_PATH"
	"$PYTHON_EXECUTABLE" "tvremote.py" > /dev/null 2>&1 &
```
5. Save the app as "AndroidTVRemote".
6. Place "AndroidTVRemote.app" into the `payload` folder.

### Pre Install
1. You may need to install `portaudio` using [homebrew](https://brew.sh).
2. Run `./createinstaller.sh`.
3. Run the installer from `built/AndroidTVRemote_Installer.pkg`.

## Make It Yourself (Complete)

If you want to build the whole thing yourself from scratch, then follow the instructions below.

1. Install dependencies – adapted from: [installation instructions](https://github.com/tronikos/androidtvremote2#development-environment).
	```sh
		brew install portaudio python3
	
		mkdir "~/Library/Application Support/AndroidTVRemote"
		cd "~/Library/Application Support/AndroidTVRemote"
		
		git clone https://github.com/tronikos/androidtvremote2.git
		
		python3 -m venv .venv
		source .venv/bin/activate
		
		python -m pip install --upgrade pip
		python -m pip install -e ./androidtvremote2
		python -m pip install grpcio-tools mypy-protobuf
		python -m grpc_tools.protoc androidtvremote2/src/androidtvremote2/*.proto --python_out=androidtvremote2/src/androidtvremote2 --mypy_out=androidtvremote2/src/androidtvremote2 -Iandroidtvremote2/src/androidtvremote2
		python -m pip install pre-commit
		pre-commit install
		pre-commit run --all-files
		
		python -m pip install -e "./androidtvremote2[test]"
		pytest
		
		python -m pip install build
		python -m build ./androidtvremote2
		
		curl -LJO https://raw.githubusercontent.com/jvarn/macos-android-tv-remote-menubar-widget/refs/heads/main/scripts/tvremote.py
	```
2. Create a new Application in Automator.
3. Add "Run Shell Script".
4. Set Pass input to "as arguments" and Shell to "/bin/bash".
5. Paste the following script:
```sh
	APP_PATH="$HOME/Library/Application Support/AndroidTVRemote"
	PYTHON_EXECUTABLE="$APP_PATH/.venv/bin/python3"
	PYTHON_SCRIPT="$APP_PATH/tvremote.py"
	
	cd "$APP_PATH"
	"$PYTHON_EXECUTABLE" "tvremote.py" > /dev/null 2>&1 &
```
7. Save the app as "AndroidTVRemote" and close Automator.
8. Create your own app icon or use the one provided; name it `AppIcon.icns` and place it in `AndroidTVRemote.app/Contents/Resources/`.
9. Modify the strings in your "AndroidTVRemote" app bundle:
	```sh
		plutil -replace CFBundleIdentifier -string "com.jvarn.AndroidTVRemote" payload/AndroidTVRemote.app/Contents/Info.plist
		plutil -replace CFBundleIconFile -string "AppIcon" payload/AndroidTVRemote.app/Contents/Info.plist
		plutil -replace CFBundleIconName -string "AppIcon" payload/AndroidTVRemote.app/Contents/Info.plist
	```
9. Move or copy the app into your Applications folder.
