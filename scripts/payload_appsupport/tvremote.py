"""macOS menu bar remote for controlling an Android TV."""

import asyncio
import json
import logging
import os
import subprocess
import threading

import AppKit  # noqa: E402
import Foundation  # noqa: E402
import objc  # noqa: E402

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth  # noqa: E402

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(message)s",
    filename="/tmp/tvremote.log",
    filemode="w",
)
_log = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/Library/Application Support/TVRemote")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CERTFILE = os.path.join(CONFIG_DIR, "cert.pem")
KEYFILE = os.path.join(CONFIG_DIR, "key.pem")
CLIENT_NAME = "tvremote"
VOLUME_COMMAND_DELAY = 0.15
_DPAD_H = 3 * 26 + 2 * 2  # 3 rows of 26px buttons + 2px gaps = 82px

# Default HDMI input URIs — TCL-specific. Override via "inputs" key in config.json.
DEFAULT_INPUT_MAP = {
    "hdmi1": "content://android.media.tv/passthrough/com.tcl.tvpassthrough%2F.TvPassThroughService%2FHW1413744128",
    "hdmi2": "content://android.media.tv/passthrough/com.tcl.tvpassthrough%2F.TvPassThroughService%2FHW1413744384",
    "hdmi3": "content://android.media.tv/passthrough/com.tcl.tvpassthrough%2F.TvPassThroughService%2FHW1413744640",
    "hdmi4": "content://android.media.tv/passthrough/com.tcl.tvpassthrough%2F.TvPassThroughService%2FHW1413745664",
}

# Optional panel sections in display order
SECTION_DEFS = [
    ("media",      "Media Controls"),
    ("dpad",       "D-Pad"),
    ("navigation", "Navigation"),
    ("inputs",     "Inputs (HDMI 1–4)"),
    ("power",      "Power"),
]

# Button tag → (command_type, value)
#   "key" → send_key_command(value)
#   "app" → send_launch_app_command(input_map[value])
_TAG_CMD = {
    10: ("key", "MUTE"),
    20: ("key", "MEDIA_PREVIOUS"),
    21: ("key", "MEDIA_REWIND"),
    22: ("key", "MEDIA_PLAY_PAUSE"),
    23: ("key", "MEDIA_FAST_FORWARD"),
    24: ("key", "MEDIA_NEXT"),
    25: ("key", "MEDIA_STOP"),
    60: ("key", "DPAD_UP"),
    61: ("key", "DPAD_DOWN"),
    62: ("key", "DPAD_LEFT"),
    63: ("key", "DPAD_RIGHT"),
    64: ("key", "DPAD_CENTER"),
    30: ("key", "HOME"),
    31: ("key", "BACK"),
    32: ("key", "MENU"),
    33: ("key", "SEARCH"),
    34: ("key", "SETTINGS"),
    40: ("app", "hdmi1"),
    41: ("app", "hdmi2"),
    42: ("app", "hdmi3"),
    43: ("app", "hdmi4"),
    50: ("key", "POWER"),
}


# Derived from __file__ at import time; launcher sets CWD = Resources dir.
_RESOURCES_DIR = os.path.dirname(os.path.abspath(__file__))

LAUNCH_AGENT_LABEL = "com.local.tvremote"
LAUNCH_AGENT_PLIST = os.path.expanduser(
    f"~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.plist"
)


def _launch_agent_plist_content():
    python = os.path.join(_RESOURCES_DIR, ".venv", "bin", "python3")
    script = os.path.abspath(__file__)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{_RESOURCES_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>__CFBundleIdentifier</key>
        <string>{LAUNCH_AGENT_LABEL}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>StandardOutPath</key>
    <string>/tmp/tvremote.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/tvremote.log</string>
</dict>
</plist>
"""


def is_launch_agent_installed():
    return os.path.exists(LAUNCH_AGENT_PLIST)


def install_launch_agent():
    os.makedirs(os.path.dirname(LAUNCH_AGENT_PLIST), exist_ok=True)
    with open(LAUNCH_AGENT_PLIST, "w") as f:
        f.write(_launch_agent_plist_content())
    subprocess.run(["launchctl", "load", "-w", LAUNCH_AGENT_PLIST], capture_output=True)


def uninstall_launch_agent():
    subprocess.run(["launchctl", "unload", "-w", LAUNCH_AGENT_PLIST], capture_output=True)
    try:
        os.remove(LAUNCH_AGENT_PLIST)
    except FileNotFoundError:
        pass


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# ---------------------------------------------------------------------------
# Main-thread dispatch helper
# ---------------------------------------------------------------------------

_dispatch_lock = threading.Lock()
_dispatch_pending = []
_dispatcher_instance = None


class _Dispatcher(AppKit.NSObject):
    @objc.typedSelector(b"v@:@")
    def runPending_(self, _):
        with _dispatch_lock:
            fns = list(_dispatch_pending)
            _dispatch_pending.clear()
        for fn in fns:
            try:
                fn()
            except Exception:
                _log.exception("Error in dispatched callback")


def dispatch_to_main(fn):
    global _dispatcher_instance
    with _dispatch_lock:
        _dispatch_pending.append(fn)
        if _dispatcher_instance is None:
            _dispatcher_instance = _Dispatcher.alloc().init()
    _dispatcher_instance.performSelectorOnMainThread_withObject_waitUntilDone_(
        "runPending:", None, False
    )


# ---------------------------------------------------------------------------
# TV bridge
# ---------------------------------------------------------------------------

class TVBridge:
    """Bridges the async AndroidTVRemote library with the AppKit main thread."""

    def __init__(self):
        self._loop = None
        self._remote = None
        self._thread = None
        self._volume_task = None
        self._shutdown_event = None
        self._pairing_code_future = None
        self._host = None

        self.on_volume_changed = None
        self.on_connection_changed = None

        self.on_pairing_needed = None    # (name: str) -> None
        self.on_pairing_started = None   # () -> None
        self.on_pairing_finished = None  # () -> None
        self.on_pairing_error = None     # (msg: str) -> None

    def start(self, host):
        self._host = host
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def restart(self, host):
        if self._loop and self._shutdown_event:
            self._loop.call_soon_threadsafe(self._shutdown_event.set)
        self.start(host)

    def _run_loop(self):
        asyncio.run(self._async_main())

    async def _async_main(self):
        self._loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()

        self._remote = AndroidTVRemote(
            CLIENT_NAME, CERTFILE, KEYFILE, self._host, enable_voice=False
        )
        self._remote.add_volume_info_updated_callback(self._on_volume_info)
        self._remote.add_is_available_updated_callback(self._on_available)

        os.makedirs(CONFIG_DIR, exist_ok=True)
        new_certs = await self._remote.async_generate_cert_if_missing()
        if new_certs:
            _log.info("New certificates generated — pairing required")
            await self._do_pairing()
            if self._shutdown_event.is_set():
                return

        while not self._shutdown_event.is_set():
            try:
                await self._remote.async_connect()
                break
            except InvalidAuth:
                _log.warning("InvalidAuth — starting pairing flow")
                await self._do_pairing()
                if self._shutdown_event.is_set():
                    return
            except (CannotConnect, ConnectionClosed) as e:
                _log.error("Cannot connect: %s", e)
                if self.on_connection_changed:
                    dispatch_to_main(lambda: self.on_connection_changed(False))
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass

        if self._shutdown_event.is_set():
            return

        self._remote.keep_reconnecting()
        if self.on_connection_changed:
            dispatch_to_main(lambda: self.on_connection_changed(True))

        await self._shutdown_event.wait()
        self._remote.disconnect()

    async def _do_pairing(self):
        try:
            name, _mac = await self._remote.async_get_name_and_mac()
        except Exception as e:
            _log.error("async_get_name_and_mac failed: %s", e)
            msg = f"Could not reach TV: {e}"
            if self.on_pairing_error:
                dispatch_to_main(lambda: self.on_pairing_error(msg))
            return

        _log.info("Pairing with %s", name)
        if self.on_pairing_needed:
            n = name
            dispatch_to_main(lambda: self.on_pairing_needed(n))

        await self._remote.async_start_pairing()
        if self.on_pairing_started:
            dispatch_to_main(lambda: self.on_pairing_started())

        while True:
            self._pairing_code_future = self._loop.create_future()
            code = await self._pairing_code_future
            if code is None:
                return
            try:
                await self._remote.async_finish_pairing(code)
                if self.on_pairing_finished:
                    dispatch_to_main(lambda: self.on_pairing_finished())
                return
            except InvalidAuth:
                _log.warning("Invalid pairing code")
                if self.on_pairing_error:
                    dispatch_to_main(lambda: self.on_pairing_error("Invalid code — try again."))
            except ConnectionClosed:
                _log.warning("Connection closed during pairing, restarting")
                await self._remote.async_start_pairing()
                if self.on_pairing_started:
                    dispatch_to_main(lambda: self.on_pairing_started())

    def submit_pairing_code(self, code):
        if self._pairing_code_future and self._loop:
            fut = self._pairing_code_future
            self._loop.call_soon_threadsafe(
                lambda: fut.set_result(code) if not fut.done() else None
            )

    def _on_volume_info(self, volume_info):
        if self.on_volume_changed:
            info = dict(volume_info)
            dispatch_to_main(lambda: self.on_volume_changed(info))

    def _on_available(self, available):
        if self.on_connection_changed:
            dispatch_to_main(lambda: self.on_connection_changed(available))

    def send_key(self, keycode):
        if self._loop:
            self._loop.call_soon_threadsafe(self._do_key, keycode)

    def _do_key(self, keycode):
        if self._remote:
            try:
                self._remote.send_key_command(keycode)
            except ConnectionClosed:
                pass

    def send_app(self, uri):
        if self._loop:
            self._loop.call_soon_threadsafe(self._do_app, uri)

    def _do_app(self, uri):
        if self._remote:
            try:
                self._remote.send_launch_app_command(uri)
            except ConnectionClosed:
                pass

    def set_volume(self, target):
        if self._loop:
            self._loop.call_soon_threadsafe(self._schedule_volume, target)

    def _schedule_volume(self, target):
        if self._volume_task and not self._volume_task.done():
            self._volume_task.cancel()
        self._volume_task = asyncio.ensure_future(self._send_volume(target))

    async def _send_volume(self, target):
        if not self._remote or not self._remote.volume_info:
            return
        current = self._remote.volume_info["level"]
        delta = target - current
        if delta == 0:
            return
        command = "VOLUME_UP" if delta > 0 else "VOLUME_DOWN"
        for _ in range(abs(delta)):
            try:
                self._remote.send_key_command(command)
            except ConnectionClosed:
                break
            await asyncio.sleep(VOLUME_COMMAND_DELAY)

    @property
    def volume_info(self):
        return self._remote.volume_info if self._remote else None

    def shutdown(self):
        if self._loop and self._shutdown_event:
            self._loop.call_soon_threadsafe(self._shutdown_event.set)


# ---------------------------------------------------------------------------
# Pairing window
# ---------------------------------------------------------------------------

class PairingController(AppKit.NSObject):
    def init(self):
        self = objc.super(PairingController, self).init()
        if self is None:
            return None
        self._window = None
        self._status_label = None
        self._input_field = None
        self._action_button = None
        self._step = None
        self.on_host_entered = None
        self.on_code_entered = None
        return self

    def _build_window(self):
        if self._window:
            return
        w, h = 380, 150
        self._window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, w, h),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self._window.setTitle_("TVRemote Setup")
        self._window.center()
        self._window.setLevel_(AppKit.NSFloatingWindowLevel)
        self._window.setReleasedWhenClosed_(False)

        content = self._window.contentView()

        self._status_label = AppKit.NSTextField.labelWithString_("")
        self._status_label.setFrame_(Foundation.NSMakeRect(20, 100, 340, 34))
        self._status_label.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        self._status_label.setMaximumNumberOfLines_(2)
        content.addSubview_(self._status_label)

        self._input_field = AppKit.NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(20, 66, 340, 22)
        )
        content.addSubview_(self._input_field)

        self._action_button = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(270, 18, 90, 32)
        )
        self._action_button.setBezelStyle_(AppKit.NSBezelStyleRounded)
        self._action_button.setTarget_(self)
        self._action_button.setAction_("actionClicked:")
        self._action_button.setKeyEquivalent_("\r")
        content.addSubview_(self._action_button)

    def _present(self):
        self._window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def show_host_entry(self):
        self._build_window()
        self._step = "host"
        self._status_label.setStringValue_("Enter your Android TV's IP address:")
        self._input_field.setPlaceholderString_("192.168.1.x")
        self._input_field.setStringValue_("")
        self._input_field.setHidden_(False)
        self._input_field.setEnabled_(True)
        self._action_button.setTitle_("Connect")
        self._action_button.setEnabled_(True)
        self._present()
        self._window.makeFirstResponder_(self._input_field)

    def show_connecting(self, name):
        self._build_window()
        self._step = "connecting"
        self._status_label.setStringValue_(
            f"Connecting to {name}…\nCheck your TV — a pairing code will appear."
        )
        self._input_field.setHidden_(True)
        self._action_button.setTitle_("…")
        self._action_button.setEnabled_(False)
        self._present()

    def show_code_entry(self):
        self._build_window()
        self._step = "code"
        self._status_label.setStringValue_("Your TV is showing a pairing code.\nEnter it below:")
        self._input_field.setPlaceholderString_("123456")
        self._input_field.setStringValue_("")
        self._input_field.setHidden_(False)
        self._input_field.setEnabled_(True)
        self._action_button.setTitle_("Pair")
        self._action_button.setEnabled_(True)
        self._present()
        self._window.makeFirstResponder_(self._input_field)

    def show_error(self, message):
        self._build_window()
        self._step = "code"
        self._status_label.setStringValue_(message)
        self._input_field.setStringValue_("")
        self._input_field.setHidden_(False)
        self._input_field.setEnabled_(True)
        self._action_button.setTitle_("Pair")
        self._action_button.setEnabled_(True)
        self._present()
        self._window.makeFirstResponder_(self._input_field)

    def close(self):
        if self._window:
            self._window.orderOut_(None)

    @objc.typedSelector(b"v@:@")
    def actionClicked_(self, sender):
        if self._step == "host":
            host = self._input_field.stringValue().strip()
            if host and self.on_host_entered:
                self.on_host_entered(host)
        elif self._step == "code":
            code = self._input_field.stringValue().strip()
            if code and self.on_code_entered:
                self.on_code_entered(code)


# ---------------------------------------------------------------------------
# Settings window
# ---------------------------------------------------------------------------

class SettingsWindow(AppKit.NSObject):
    def init(self):
        self = objc.super(SettingsWindow, self).init()
        if self is None:
            return None
        self._window = None
        self._ip_field = None
        self._section_checks = {}
        self._launch_checkbox = None
        self.on_save = None    # (host: str, sections: dict, launch_at_login: bool) -> None
        self.on_repair = None  # () -> None
        return self

    def _build_window(self):
        if self._window:
            return
        # Layout (bottom-origin NSView coords, 300×316):
        #   y=274  "TV IP Address:" label
        #   y=248  IP text field
        #   y=220  "Launch at Login" checkbox
        #   y=214  separator line
        #   y=198  "Show Sections:" label
        #   y=172  Media checkbox          (i=0)
        #   y=148  D-Pad checkbox          (i=1)
        #   y=124  Navigation checkbox     (i=2)
        #   y=100  Inputs checkbox         (i=3)
        #   y= 76  Power checkbox          (i=4)
        #   y= 16  [Re-pair]  [Save]
        w, h = 300, 316
        self._window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, w, h),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self._window.setTitle_("TVRemote Settings")
        self._window.center()
        self._window.setLevel_(AppKit.NSFloatingWindowLevel)
        self._window.setReleasedWhenClosed_(False)

        content = self._window.contentView()

        # IP address
        ip_label = AppKit.NSTextField.labelWithString_("TV IP Address:")
        ip_label.setFrame_(Foundation.NSMakeRect(20, 274, 180, 18))
        content.addSubview_(ip_label)

        self._ip_field = AppKit.NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(20, 248, 260, 22)
        )
        content.addSubview_(self._ip_field)

        # Launch at login
        self._launch_checkbox = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(20, 220, 260, 20)
        )
        self._launch_checkbox.setButtonType_(AppKit.NSButtonTypeSwitch)
        self._launch_checkbox.setTitle_("Launch at Login")
        content.addSubview_(self._launch_checkbox)

        # Separator
        sep = AppKit.NSBox.alloc().initWithFrame_(Foundation.NSMakeRect(20, 214, 260, 1))
        sep.setBoxType_(AppKit.NSBoxSeparator)
        content.addSubview_(sep)

        # Section toggles
        sec_label = AppKit.NSTextField.labelWithString_("Show Sections:")
        sec_label.setFrame_(Foundation.NSMakeRect(20, 198, 200, 14))
        sec_label.setFont_(AppKit.NSFont.systemFontOfSize_weight_(11, AppKit.NSFontWeightSemibold))
        content.addSubview_(sec_label)

        for i, (key, display_name) in enumerate(SECTION_DEFS):
            cb = AppKit.NSButton.alloc().initWithFrame_(
                Foundation.NSMakeRect(20, 172 - i * 24, 260, 20)
            )
            cb.setButtonType_(AppKit.NSButtonTypeSwitch)
            cb.setTitle_(display_name)
            content.addSubview_(cb)
            self._section_checks[key] = cb

        # Buttons
        repair_btn = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(20, 16, 90, 32))
        repair_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        repair_btn.setTitle_("Re-pair…")
        repair_btn.setTarget_(self)
        repair_btn.setAction_("repairClicked:")
        content.addSubview_(repair_btn)

        save_btn = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(190, 16, 90, 32))
        save_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        save_btn.setTitle_("Save")
        save_btn.setTarget_(self)
        save_btn.setAction_("saveClicked:")
        save_btn.setKeyEquivalent_("\r")
        content.addSubview_(save_btn)

    def show(self, current_host="", enabled_sections=None):
        self._build_window()
        self._ip_field.setStringValue_(current_host or "")
        self._ip_field.setPlaceholderString_("192.168.1.x")

        self._launch_checkbox.setState_(
            AppKit.NSControlStateValueOn
            if is_launch_agent_installed()
            else AppKit.NSControlStateValueOff
        )

        if enabled_sections is None:
            enabled_sections = {}
        for key, cb in self._section_checks.items():
            cb.setState_(
                AppKit.NSControlStateValueOn
                if enabled_sections.get(key, True)
                else AppKit.NSControlStateValueOff
            )

        self._window.makeKeyAndOrderFront_(None)
        self._window.makeFirstResponder_(self._ip_field)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def close(self):
        if self._window:
            self._window.orderOut_(None)

    @objc.typedSelector(b"v@:@")
    def saveClicked_(self, sender):
        host = self._ip_field.stringValue().strip()
        sections = {
            key: cb.state() == AppKit.NSControlStateValueOn
            for key, cb in self._section_checks.items()
        }
        launch = self._launch_checkbox.state() == AppKit.NSControlStateValueOn
        if host and self.on_save:
            self.on_save(host, sections, launch)
            self.close()

    @objc.typedSelector(b"v@:@")
    def repairClicked_(self, sender):
        self.close()
        if self.on_repair:
            self.on_repair()


# ---------------------------------------------------------------------------
# Remote panel (NSPanel subclass)
# ---------------------------------------------------------------------------

class RemotePanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return True


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

# Panel layout constants
_W = 320          # panel width
_PAD = 8          # outer padding
_BTN_H = 28       # button height
_LABEL_H = 14     # section header label height
_GAP = 4          # inner gap


class TVRemoteApp(AppKit.NSObject):
    def init(self):
        self = objc.super(TVRemoteApp, self).init()
        if self is None:
            return None

        self._config = load_config()

        self.bridge = TVBridge()
        self.bridge.on_volume_changed = self._on_volume_changed
        self.bridge.on_connection_changed = self._on_connection_changed
        self.bridge.on_pairing_needed = self._on_pairing_needed
        self.bridge.on_pairing_started = self._on_pairing_started
        self.bridge.on_pairing_finished = self._on_pairing_finished
        self.bridge.on_pairing_error = self._on_pairing_error

        self.status_item = None
        self._panel = None
        self._slider = None
        self._label = None
        self._updating_from_tv = False
        self._event_monitor = None

        self._pairing_ctrl = PairingController.alloc().init()
        self._pairing_ctrl.on_host_entered = self._on_host_entered
        self._pairing_ctrl.on_code_entered = self._on_code_entered

        self._settings_win = SettingsWindow.alloc().init()
        self._settings_win.on_save = self._on_settings_save
        self._settings_win.on_repair = self._on_settings_repair

        return self

    # ------------------------------------------------------------------
    # App setup
    # ------------------------------------------------------------------

    def setup(self):
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

        self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        self._set_status_icon("tv.remote.fill", "TVRemote")

        button = self.status_item.button()
        button.setTarget_(self)
        button.setAction_("togglePanel:")
        button.sendActionOn_(AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseDown)

        self._rebuild_panel()

        self._event_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskLeftMouseDown | AppKit.NSEventMaskRightMouseDown,
            self._global_click,
        )

        self.performSelector_withObject_afterDelay_("_startBridge:", None, 0.0)

    def _set_status_icon(self, symbol_name, desc):
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol_name, desc)
        if img:
            cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
                14, AppKit.NSFontWeightRegular, 1
            )
            self.status_item.button().setImage_(img.imageWithSymbolConfiguration_(cfg))
        else:
            self.status_item.button().setTitle_("📺")

    @objc.typedSelector(b"v@:@")
    def _startBridge_(self, _):
        host = self._config.get("tv_host")
        if not host:
            self._pairing_ctrl.show_host_entry()
        else:
            self.bridge.start(host)

    # ------------------------------------------------------------------
    # Panel construction
    # ------------------------------------------------------------------

    def _get_enabled_sections(self):
        saved = self._config.get("sections", {})
        return {key: saved.get(key, True) for key, _ in SECTION_DEFS}

    def _rebuild_panel(self):
        was_visible = self._panel and self._panel.isVisible()
        if was_visible:
            self._panel.orderOut_(None)

        # No TV configured yet — show a minimal "Pair" panel.
        if not self._config.get("tv_host"):
            self._build_pair_panel()
            return

        enabled = self._get_enabled_sections()

        # Calculate panel height
        H = _PAD + _BTN_H + _PAD  # volume row: 44px
        for key, _ in SECTION_DEFS:
            if enabled.get(key, True):
                ch = _DPAD_H if key == "dpad" else _BTN_H
                H += _LABEL_H + _GAP + ch + _GAP

        self._panel = RemotePanel.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, _W, H),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self._panel.setLevel_(AppKit.NSStatusWindowLevel)
        self._panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorTransient
        )
        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(AppKit.NSColor.windowBackgroundColor())
        cv = self._panel.contentView()
        cv.setWantsLayer_(True)
        cv.layer().setCornerRadius_(10)
        cv.layer().setMasksToBounds_(True)

        content = self._panel.contentView()

        # Build top-down (NSView is bottom-origin, so high y = visual top)
        cursor = H

        cursor -= _PAD
        vol_y = cursor - _BTN_H
        self._add_volume_row(content, vol_y)
        cursor = vol_y - _PAD

        section_builders = {
            "media":      self._add_media_row,
            "dpad":       self._add_dpad_row,
            "navigation": self._add_navigation_row,
            "inputs":     self._add_inputs_row,
            "power":      self._add_power_row,
        }
        section_titles = {
            "media":      "MEDIA",
            "dpad":       "D-PAD",
            "navigation": "NAVIGATION",
            "inputs":     "INPUTS",
            "power":      "POWER",
        }
        for key, _ in SECTION_DEFS:
            if not enabled.get(key, True):
                continue
            cursor -= _LABEL_H
            self._add_section_label(content, cursor, section_titles[key])
            cursor -= _GAP
            ch = _DPAD_H if key == "dpad" else _BTN_H
            btn_y = cursor - ch
            section_builders[key](content, btn_y)
            cursor = btn_y - _GAP

    def _build_pair_panel(self):
        """Minimal panel shown before a TV has been configured."""
        H = 44  # single button row
        self._panel = RemotePanel.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, _W, H),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self._panel.setLevel_(AppKit.NSStatusWindowLevel)
        self._panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorTransient
        )
        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(AppKit.NSColor.windowBackgroundColor())
        cv = self._panel.contentView()
        cv.setWantsLayer_(True)
        cv.layer().setCornerRadius_(10)
        cv.layer().setMasksToBounds_(True)

        btn = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect((_W - 160) // 2, _PAD, 160, _BTN_H)
        )
        btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        btn.setTitle_("Pair with TV…")
        btn.setTarget_(self)
        btn.setAction_("pairButtonClicked:")
        btn.setKeyEquivalent_("\r")
        cv.addSubview_(btn)

    def _add_section_label(self, content, y, text):
        label = AppKit.NSTextField.labelWithString_(text)
        label.setFrame_(Foundation.NSMakeRect(_PAD, y, _W - _PAD * 2, _LABEL_H))
        label.setFont_(AppKit.NSFont.systemFontOfSize_weight_(9, AppKit.NSFontWeightSemibold))
        label.setTextColor_(AppKit.NSColor.tertiaryLabelColor())
        content.addSubview_(label)

    def _sym_btn(self, content, symbol, fallback, action, tag, x, y, w, h=_BTN_H):
        """Add a symbol button (falls back to text if symbol unavailable)."""
        btn = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(x, y, w, h))
        btn.setBezelStyle_(AppKit.NSBezelStyleTexturedRounded)
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, fallback)
        if img:
            btn.setImage_(img)
            btn.setTitle_("")
            btn.setImagePosition_(AppKit.NSImageOnly)
        else:
            btn.setTitle_(fallback)
        btn.setTarget_(self)
        btn.setAction_(action)
        btn.setTag_(tag)
        content.addSubview_(btn)
        return btn

    def _text_btn(self, content, title, action, tag, x, y, w, h=_BTN_H):
        """Add a text-label button."""
        btn = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(x, y, w, h))
        btn.setBezelStyle_(AppKit.NSBezelStyleTexturedRounded)
        btn.setTitle_(title)
        btn.setTarget_(self)
        btn.setAction_(action)
        btn.setTag_(tag)
        content.addSubview_(btn)
        return btn

    def _add_volume_row(self, content, y):
        # Positions: minus(8,24) slider(36,170) plus(210,24) label(238,30) mute(272,40)
        minus = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(8, y, 24, _BTN_H))
        minus.setBezelStyle_(AppKit.NSBezelStyleCircular)
        minus.setTitle_("-")
        minus.setTarget_(self)
        minus.setAction_("volumeDown:")
        content.addSubview_(minus)

        self._slider = AppKit.NSSlider.alloc().initWithFrame_(
            Foundation.NSMakeRect(36, y + 2, 170, _BTN_H - 4)
        )
        self._slider.setMinValue_(0)
        self._slider.setMaxValue_(100)
        self._slider.setIntValue_(0)
        self._slider.setContinuous_(False)
        self._slider.setTarget_(self)
        self._slider.setAction_("sliderMoved:")
        content.addSubview_(self._slider)

        plus = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(210, y, 24, _BTN_H))
        plus.setBezelStyle_(AppKit.NSBezelStyleCircular)
        plus.setTitle_("+")
        plus.setTarget_(self)
        plus.setAction_("volumeUp:")
        content.addSubview_(plus)

        self._label = AppKit.NSTextField.labelWithString_("--")
        self._label.setFrame_(Foundation.NSMakeRect(238, y + 4, 30, _BTN_H - 8))
        self._label.setAlignment_(AppKit.NSTextAlignmentCenter)
        self._label.setFont_(AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(12, 0.0))
        content.addSubview_(self._label)

        self._sym_btn(content, "speaker.slash.fill", "Mute", "remoteAction:", 10, 272, y, 40)

    def _add_media_row(self, content, y):
        # 6 buttons: |< << ▶ >> >| ■  (tag 20-25), width=47, gap=4, start=9
        specs = [
            (20, "backward.end.fill",  "|<"),
            (21, "backward.fill",       "<<"),
            (22, "play.fill",           "▶"),
            (23, "forward.fill",        ">>"),
            (24, "forward.end.fill",    ">|"),
            (25, "stop.fill",           "■"),
        ]
        x = 9
        for tag, sym, fb in specs:
            self._sym_btn(content, sym, fb, "remoteAction:", tag, x, y, 47)
            x += 51  # 47 + 4 gap

    def _add_dpad_row(self, content, y):
        # 3×3 cross: button 34×26, gap 2px, centered in panel
        bw, bh, gap = 34, 26, 2
        x0 = (_W - (3 * bw + 2 * gap)) // 2  # = 107
        col = [x0, x0 + bw + gap, x0 + 2 * (bw + gap)]
        row = [y, y + bh + gap, y + 2 * (bh + gap)]  # row[0]=bottom, row[2]=top (visual up)
        self._sym_btn(content, "chevron.up",    "Up",    "remoteAction:", 60, col[1], row[2], bw, bh)
        self._sym_btn(content, "chevron.left",  "Left",  "remoteAction:", 62, col[0], row[1], bw, bh)
        self._text_btn(content,                 "OK",    "remoteAction:", 64, col[1], row[1], bw, bh)
        self._sym_btn(content, "chevron.right", "Right", "remoteAction:", 63, col[2], row[1], bw, bh)
        self._sym_btn(content, "chevron.down",  "Down",  "remoteAction:", 61, col[1], row[0], bw, bh)

    def _add_navigation_row(self, content, y):
        # 5 buttons: Home Back Menu Search TVSettings (tag 30-34), width=57, gap=4, start=10
        specs = [
            (30, "house.fill",           "Home"),
            (31, "chevron.backward",     "Back"),
            (32, "line.3.horizontal",    "Menu"),
            (33, "magnifyingglass",      "Search"),
            (34, "gearshape",            "Settings"),
        ]
        x = 10
        for tag, sym, fb in specs:
            self._sym_btn(content, sym, fb, "remoteAction:", tag, x, y, 57)
            x += 61  # 57 + 4 gap

    def _add_inputs_row(self, content, y):
        # 4 text buttons: HDMI 1-4 (tag 40-43), width=72, gap=4, start=10
        specs = [(40, "HDMI 1"), (41, "HDMI 2"), (42, "HDMI 3"), (43, "HDMI 4")]
        x = 10
        for tag, title in specs:
            self._text_btn(content, title, "remoteAction:", tag, x, y, 72)
            x += 76  # 72 + 4 gap

    def _add_power_row(self, content, y):
        self._sym_btn(content, "power", "Power", "remoteAction:", 50, 110, y, 100)

    # ------------------------------------------------------------------
    # Status bar button / panel toggle
    # ------------------------------------------------------------------

    @objc.typedSelector(b"v@:@")
    def togglePanel_(self, sender):
        event = AppKit.NSApp.currentEvent()
        if event and event.type() == AppKit.NSEventTypeRightMouseDown:
            menu = AppKit.NSMenu.alloc().init()
            item = menu.addItemWithTitle_action_keyEquivalent_("Settings…", "showSettings:", "")
            item.setTarget_(self)
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            item = menu.addItemWithTitle_action_keyEquivalent_("Quit TVRemote", "quitApp:", "")
            item.setTarget_(self)
            self.status_item.popUpStatusItemMenu_(menu)
            return

        if self._panel.isVisible():
            self._panel.orderOut_(None)
        else:
            button = self.status_item.button()
            btn_rect = button.window().convertRectToScreen_(button.frame())
            pw = self._panel.frame().size.width
            x = btn_rect.origin.x + btn_rect.size.width / 2 - pw / 2
            y = btn_rect.origin.y - self._panel.frame().size.height - 4
            self._panel.setFrameOrigin_(Foundation.NSMakePoint(x, y))

            info = self.bridge.volume_info
            if info:
                self._update_slider(info)

            self._panel.makeKeyAndOrderFront_(None)

    def _global_click(self, event):
        if self._panel.isVisible():
            loc = Foundation.NSEvent.mouseLocation()
            if not Foundation.NSPointInRect(loc, self._panel.frame()):
                self._panel.orderOut_(None)

    @objc.typedSelector(b"v@:@")
    def showSettings_(self, sender):
        self._settings_win.show(
            current_host=self._config.get("tv_host", ""),
            enabled_sections=self._get_enabled_sections(),
        )

    @objc.typedSelector(b"v@:@")
    def pairButtonClicked_(self, sender):
        self._panel.orderOut_(None)
        self._pairing_ctrl.show_host_entry()

    @objc.typedSelector(b"v@:@")
    def quitApp_(self, sender):
        AppKit.NSApp.terminate_(None)

    # ------------------------------------------------------------------
    # Volume controls
    # ------------------------------------------------------------------

    @objc.typedSelector(b"v@:@")
    def sliderMoved_(self, sender):
        if self._updating_from_tv:
            return
        target = int(sender.intValue())
        self._label.setStringValue_(str(target))
        self.bridge.set_volume(target)

    @objc.typedSelector(b"v@:@")
    def volumeUp_(self, sender):
        self.bridge.send_key("VOLUME_UP")

    @objc.typedSelector(b"v@:@")
    def volumeDown_(self, sender):
        self.bridge.send_key("VOLUME_DOWN")

    def _update_slider(self, volume_info):
        self._updating_from_tv = True
        self._slider.setMaxValue_(volume_info["max"])
        self._slider.setIntValue_(volume_info["level"])
        self._label.setStringValue_(str(volume_info["level"]))
        self._updating_from_tv = False

    # ------------------------------------------------------------------
    # Generic remote button handler (tag-based dispatch)
    # ------------------------------------------------------------------

    @objc.typedSelector(b"v@:@")
    def remoteAction_(self, sender):
        tag = sender.tag()
        cmd = _TAG_CMD.get(tag)
        if not cmd:
            return
        cmd_type, value = cmd
        if cmd_type == "key":
            self.bridge.send_key(value)
        elif cmd_type == "app":
            inputs = self._config.get("inputs", DEFAULT_INPUT_MAP)
            self.bridge.send_app(inputs.get(value, DEFAULT_INPUT_MAP.get(value, "")))

    # ------------------------------------------------------------------
    # Bridge callbacks
    # ------------------------------------------------------------------

    def _on_volume_changed(self, volume_info):
        if self._panel and self._panel.isVisible():
            self._update_slider(volume_info)

    def _on_connection_changed(self, available):
        if available:
            self._set_status_icon("tv.remote.fill", "TVRemote")
        else:
            self._set_status_icon("tv.remote", "TVRemote (disconnected)")

    def _on_pairing_needed(self, name):
        self._pairing_ctrl.show_connecting(name)

    def _on_pairing_started(self):
        self._pairing_ctrl.show_code_entry()

    def _on_pairing_finished(self):
        self._pairing_ctrl.close()
        self._rebuild_panel()  # replace pair button with full controls

    def _on_pairing_error(self, msg):
        self._pairing_ctrl.show_error(msg)

    # ------------------------------------------------------------------
    # Pairing callbacks
    # ------------------------------------------------------------------

    def _on_host_entered(self, host):
        self._config["tv_host"] = host
        save_config(self._config)
        self.bridge.start(host)

    def _on_code_entered(self, code):
        self.bridge.submit_pairing_code(code)

    # ------------------------------------------------------------------
    # Settings callbacks
    # ------------------------------------------------------------------

    def _on_settings_save(self, host, sections, launch_at_login):
        host_changed = host != self._config.get("tv_host")
        sections_changed = sections != self._config.get("sections", {})

        self._config["tv_host"] = host
        self._config["sections"] = sections
        save_config(self._config)

        if host_changed:
            self.bridge.restart(host)

        if host_changed or sections_changed:
            self._rebuild_panel()

        # Launch agent install/uninstall
        currently_installed = is_launch_agent_installed()
        if launch_at_login and not currently_installed:
            install_launch_agent()
        elif not launch_at_login and currently_installed:
            uninstall_launch_agent()

    def _on_settings_repair(self):
        for path in (CERTFILE, KEYFILE):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        host = self._config.get("tv_host")
        if host:
            self.bridge.restart(host)
        else:
            self._pairing_ctrl.show_host_entry()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self):
        self.setup()
        AppKit.NSApp.run()


if __name__ == "__main__":
    app = TVRemoteApp.alloc().init()
    app.run()
