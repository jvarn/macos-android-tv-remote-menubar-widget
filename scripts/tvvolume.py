"""macOS menu bar widget for controlling Android TV volume."""

import asyncio
import json
import logging
import os
import threading

os.environ["__CFBundleIdentifier"] = "com.local.tvvolume"

import AppKit  # noqa: E402
import Foundation  # noqa: E402
import objc  # noqa: E402

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth  # noqa: E402

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(message)s",
    filename="/tmp/tvvolume.log",
    filemode="w",
)
_log = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/Library/Application Support/TVVolume")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CERTFILE = os.path.join(CONFIG_DIR, "cert.pem")
KEYFILE = os.path.join(CONFIG_DIR, "key.pem")
CLIENT_NAME = "tvvolume"
VOLUME_COMMAND_DELAY = 0.15


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

        # Pairing callbacks (all dispatched to main thread)
        self.on_pairing_needed = None    # (name: str) -> None
        self.on_pairing_started = None   # () -> None
        self.on_pairing_finished = None  # () -> None
        self.on_pairing_error = None     # (msg: str) -> None

    def start(self, host):
        self._host = host
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def restart(self, host):
        """Shut down the current loop and start fresh with a new host."""
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
                return  # cancelled
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
                _log.warning("Connection closed during pairing, restarting pairing")
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

    def set_volume(self, target):
        if self._loop:
            self._loop.call_soon_threadsafe(self._schedule_volume, target)

    def send_volume_step(self, direction):
        if self._loop:
            self._loop.call_soon_threadsafe(self._do_step, direction)

    def _do_step(self, direction):
        if self._remote:
            try:
                self._remote.send_key_command(direction)
            except ConnectionClosed:
                pass

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
# Pairing window  (first-run setup + re-pair flow)
# ---------------------------------------------------------------------------

class PairingController(AppKit.NSObject):
    """Multi-step window that guides through host entry and pairing code entry."""

    def init(self):
        self = objc.super(PairingController, self).init()
        if self is None:
            return None
        self._window = None
        self._status_label = None
        self._input_field = None
        self._action_button = None
        self._step = None
        self.on_host_entered = None   # (host: str) -> None
        self.on_code_entered = None   # (code: str) -> None
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
        self._window.setTitle_("TVVolume Setup")
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
        self._status_label.setStringValue_(f"Connecting to {name}…\nCheck your TV — a pairing code will appear.")
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
        self._step = "code"  # still in code-entry mode
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
    """Small window for changing the TV IP and triggering re-pair."""

    def init(self):
        self = objc.super(SettingsWindow, self).init()
        if self is None:
            return None
        self._window = None
        self._ip_field = None
        self.on_save = None    # (host: str) -> None
        self.on_repair = None  # () -> None
        return self

    def _build_window(self):
        if self._window:
            return
        w, h = 320, 120
        self._window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, w, h),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self._window.setTitle_("TVVolume Settings")
        self._window.center()
        self._window.setLevel_(AppKit.NSFloatingWindowLevel)
        self._window.setReleasedWhenClosed_(False)

        content = self._window.contentView()

        label = AppKit.NSTextField.labelWithString_("TV IP Address:")
        label.setFrame_(Foundation.NSMakeRect(20, 80, 120, 20))
        content.addSubview_(label)

        self._ip_field = AppKit.NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(20, 55, 280, 22)
        )
        content.addSubview_(self._ip_field)

        save_btn = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(210, 12, 90, 32)
        )
        save_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        save_btn.setTitle_("Save")
        save_btn.setTarget_(self)
        save_btn.setAction_("saveClicked:")
        save_btn.setKeyEquivalent_("\r")
        content.addSubview_(save_btn)

        repair_btn = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(20, 12, 90, 32)
        )
        repair_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        repair_btn.setTitle_("Re-pair…")
        repair_btn.setTarget_(self)
        repair_btn.setAction_("repairClicked:")
        content.addSubview_(repair_btn)

    def show(self, current_host=""):
        self._build_window()
        self._ip_field.setStringValue_(current_host or "")
        self._ip_field.setPlaceholderString_("192.168.1.x")
        self._window.makeKeyAndOrderFront_(None)
        self._window.makeFirstResponder_(self._ip_field)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def close(self):
        if self._window:
            self._window.orderOut_(None)

    @objc.typedSelector(b"v@:@")
    def saveClicked_(self, sender):
        host = self._ip_field.stringValue().strip()
        if host and self.on_save:
            self.on_save(host)
            self.close()

    @objc.typedSelector(b"v@:@")
    def repairClicked_(self, sender):
        self.close()
        if self.on_repair:
            self.on_repair()


# ---------------------------------------------------------------------------
# Volume panel
# ---------------------------------------------------------------------------

class VolumePanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return True


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class VolumeApp(AppKit.NSObject):
    def init(self):
        self = objc.super(VolumeApp, self).init()
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
    # Setup
    # ------------------------------------------------------------------

    def setup(self):
        _log.debug("setup: start")
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

        self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        button = self.status_item.button()

        sf_image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "speaker.wave.2.fill", "TV Volume"
        )
        if sf_image:
            config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
                14, AppKit.NSFontWeightRegular, 1
            )
            button.setImage_(sf_image.imageWithSymbolConfiguration_(config))
        else:
            button.setTitle_("\U0001F50A")

        button.setTarget_(self)
        button.setAction_("togglePanel:")
        button.sendActionOn_(AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseDown)

        self._build_panel()

        self._event_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskLeftMouseDown | AppKit.NSEventMaskRightMouseDown,
            self._global_click,
        )

        # Defer bridge start until the run loop is active so windows display correctly.
        self.performSelector_withObject_afterDelay_("_startBridge:", None, 0.0)
        _log.debug("setup: done")

    @objc.typedSelector(b"v@:@")
    def _startBridge_(self, _):
        host = self._config.get("tv_host")
        if not host:
            self._pairing_ctrl.show_host_entry()
        else:
            self.bridge.start(host)

    # ------------------------------------------------------------------
    # Volume panel construction
    # ------------------------------------------------------------------

    def _build_panel(self):
        width, height = 300, 50
        self._panel = VolumePanel.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, width, height),
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

        self._panel.contentView().setWantsLayer_(True)
        self._panel.contentView().layer().setCornerRadius_(10)
        self._panel.contentView().layer().setMasksToBounds_(True)

        content = self._panel.contentView()

        minus = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(8, 11, 28, 28))
        minus.setBezelStyle_(AppKit.NSBezelStyleCircular)
        minus.setTitle_("-")
        minus.setTarget_(self)
        minus.setAction_("volumeDown:")
        content.addSubview_(minus)

        self._slider = AppKit.NSSlider.alloc().initWithFrame_(
            Foundation.NSMakeRect(40, 13, 180, 24)
        )
        self._slider.setMinValue_(0)
        self._slider.setMaxValue_(100)
        self._slider.setIntValue_(0)
        self._slider.setContinuous_(False)
        self._slider.setTarget_(self)
        self._slider.setAction_("sliderMoved:")
        content.addSubview_(self._slider)

        plus = AppKit.NSButton.alloc().initWithFrame_(Foundation.NSMakeRect(224, 11, 28, 28))
        plus.setBezelStyle_(AppKit.NSBezelStyleCircular)
        plus.setTitle_("+")
        plus.setTarget_(self)
        plus.setAction_("volumeUp:")
        content.addSubview_(plus)

        self._label = AppKit.NSTextField.labelWithString_("--")
        self._label.setFrame_(Foundation.NSMakeRect(256, 15, 35, 20))
        self._label.setAlignment_(AppKit.NSTextAlignmentCenter)
        self._label.setFont_(
            AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(12, 0.0)
        )
        content.addSubview_(self._label)

    # ------------------------------------------------------------------
    # Status bar button actions
    # ------------------------------------------------------------------

    @objc.typedSelector(b"v@:@")
    def togglePanel_(self, sender):
        event = AppKit.NSApp.currentEvent()
        if event and event.type() == AppKit.NSEventTypeRightMouseDown:
            menu = AppKit.NSMenu.alloc().init()
            settings_item = menu.addItemWithTitle_action_keyEquivalent_("Settings…", "showSettings:", "")
            settings_item.setTarget_(self)
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            quit_item = menu.addItemWithTitle_action_keyEquivalent_("Quit TVVolume", "quitApp:", "")
            quit_item.setTarget_(self)
            self.status_item.popUpStatusItemMenu_(menu)
            return

        if self._panel.isVisible():
            self._panel.orderOut_(None)
        else:
            button = self.status_item.button()
            button_rect = button.window().convertRectToScreen_(button.frame())
            panel_width = self._panel.frame().size.width
            x = button_rect.origin.x + button_rect.size.width / 2 - panel_width / 2
            y = button_rect.origin.y - self._panel.frame().size.height - 4
            self._panel.setFrameOrigin_(Foundation.NSMakePoint(x, y))

            info = self.bridge.volume_info
            if info:
                self._update_slider(info)

            self._panel.makeKeyAndOrderFront_(None)

    def _global_click(self, event):
        if self._panel.isVisible():
            click_loc = Foundation.NSEvent.mouseLocation()
            if not Foundation.NSPointInRect(click_loc, self._panel.frame()):
                self._panel.orderOut_(None)

    @objc.typedSelector(b"v@:@")
    def showSettings_(self, sender):
        self._settings_win.show(current_host=self._config.get("tv_host", ""))

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
        self.bridge.send_volume_step("VOLUME_UP")

    @objc.typedSelector(b"v@:@")
    def volumeDown_(self, sender):
        self.bridge.send_volume_step("VOLUME_DOWN")

    def _update_slider(self, volume_info):
        self._updating_from_tv = True
        self._slider.setMaxValue_(volume_info["max"])
        self._slider.setIntValue_(volume_info["level"])
        self._label.setStringValue_(str(volume_info["level"]))
        self._updating_from_tv = False

    # ------------------------------------------------------------------
    # Bridge callbacks
    # ------------------------------------------------------------------

    def _on_volume_changed(self, volume_info):
        if self._panel and self._panel.isVisible():
            self._update_slider(volume_info)

    def _on_connection_changed(self, available):
        button = self.status_item.button()
        name = "speaker.wave.2.fill" if available else "speaker.slash.fill"
        desc = "TV Volume" if available else "TV Disconnected"
        sf_image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, desc)
        if sf_image:
            config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
                14, AppKit.NSFontWeightRegular, 1
            )
            button.setImage_(sf_image.imageWithSymbolConfiguration_(config))

    def _on_pairing_needed(self, name):
        self._pairing_ctrl.show_connecting(name)

    def _on_pairing_started(self):
        self._pairing_ctrl.show_code_entry()

    def _on_pairing_finished(self):
        self._pairing_ctrl.close()

    def _on_pairing_error(self, msg):
        self._pairing_ctrl.show_error(msg)

    # ------------------------------------------------------------------
    # Pairing controller callbacks
    # ------------------------------------------------------------------

    def _on_host_entered(self, host):
        self._config["tv_host"] = host
        save_config(self._config)
        self.bridge.start(host)

    def _on_code_entered(self, code):
        self.bridge.submit_pairing_code(code)

    # ------------------------------------------------------------------
    # Settings window callbacks
    # ------------------------------------------------------------------

    def _on_settings_save(self, host):
        if host == self._config.get("tv_host"):
            return
        self._config["tv_host"] = host
        save_config(self._config)
        self.bridge.restart(host)

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
        _log.debug("run: start")
        self.setup()
        _log.debug("run: entering event loop")
        AppKit.NSApp.run()


if __name__ == "__main__":
    app = VolumeApp.alloc().init()
    app.run()
