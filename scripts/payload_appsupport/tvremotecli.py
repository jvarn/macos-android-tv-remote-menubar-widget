"""Non-interactive CLI for controlling an Android TV with improved TCL compatibility."""

import argparse
import asyncio
import logging
import sys
import os
import socket

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOSTNAME = socket.gethostname()
TV_HOST = "192.168.10.143"
CERTFILE = os.path.join(SCRIPT_DIR, "cert.pem")
KEYFILE = os.path.join(SCRIPT_DIR, "key.pem")
CLIENT_NAME = f"tvcontrol-helper-{HOSTNAME}"

_LOGGER = logging.getLogger(__name__)

# Mapping remain the same
VOLUME_MAP = {"up": "VOLUME_UP", "down": "VOLUME_DOWN", "mute": "MUTE", "volumemute": "VOLUME_MUTE"}
DPAD_MAP = {"up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT", "right": "DPAD_RIGHT", 
            "center": "DPAD_CENTER", "a": "BUTTON_A", "b": "BUTTON_B", "x": "BUTTON_X", "y": "BUTTON_Y"}
MEDIA_MAP = {"play-pause": "MEDIA_PLAY_PAUSE", "play": "MEDIA_PLAY", "pause": "MEDIA_PAUSE", 
             "next": "MEDIA_NEXT", "prev": "MEDIA_PREVIOUS", "stop": "MEDIA_STOP", 
             "rewind": "MEDIA_REWIND", "ff": "MEDIA_FAST_FORWARD"}

# Updated TCL Specific Input URIs based on your dumpsys logs
INPUT_MAP = {
    "hdmi1": "content://android.media.tv/passthrough/com.tcl.tvpassthrough%2F.TvPassThroughService%2FHW1413744128",
    "hdmi2": "content://android.media.tv/passthrough/com.tcl.tvpassthrough%2F.TvPassThroughService%2FHW1413744384",
    "hdmi3": "content://android.media.tv/passthrough/com.tcl.tvpassthrough%2F.TvPassThroughService%2FHW1413744640",
    "hdmi4": "content://android.media.tv/passthrough/com.tcl.tvpassthrough%2F.TvPassThroughService%2FHW1413745664",
    "av": "content://android.media.tv/passthrough/com.tcl.tvpassthrough%2F.TvPassThroughService%2FHW1413743104",
}

async def _pair(remote: AndroidTVRemote) -> None:
    name, mac = await remote.async_get_name_and_mac()
    print(f"Pairing with {remote.host} ({name} - {mac})...")
    await remote.async_start_pairing()
    while True:
        pairing_code = input("Enter pairing code shown on TV: ")
        try:
            return await remote.async_finish_pairing(pairing_code)
        except InvalidAuth:
            print("Invalid pairing code, try again.")
        except ConnectionClosed:
            print("Connection closed, restarting pairing...")
            return await _pair(remote)

async def _main() -> None:
    parser = argparse.ArgumentParser(description="Control an Android TV")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--volume", choices=VOLUME_MAP.keys())
    group.add_argument("--dpad", choices=DPAD_MAP.keys())
    group.add_argument("--media", choices=MEDIA_MAP.keys())
    group.add_argument("--input", choices=INPUT_MAP.keys(), help="Switch to a specific HDMI or AV input")
    group.add_argument("--key", metavar="KEYCODE")
    group.add_argument("--app", metavar="PACKAGE_OR_URL")
    group.add_argument("--text", metavar="TEXT")
    group.add_argument("--power", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    remote = AndroidTVRemote(CLIENT_NAME, CERTFILE, KEYFILE, TV_HOST, enable_voice=False)

    if await remote.async_generate_cert_if_missing():
        print("Generated new certificate — pairing required.")
        await _pair(remote)

    print(f"Connecting to {TV_HOST}...")
    try:
        await remote.async_connect()
    except InvalidAuth:
        print("Authentication failed — re-pairing.")
        await _pair(remote)
        await remote.async_connect()
    except Exception as e:
        print(f"Connection error: {e}")
        sys.exit(1)

    # TCL COMPATIBILITY HANDSHAKE
    ready_event = asyncio.Event()
    def on_ready(_):
        ready_event.set()

    remote.add_current_app_updated_callback(on_ready)
    remote.add_volume_info_updated_callback(on_ready)
    
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=2.5)
    except asyncio.TimeoutError:
        pass
    
    await asyncio.sleep(0.5)

    print("Connected and Ready. Sending command...")

    try:
        if args.volume:
            remote.send_key_command(VOLUME_MAP[args.volume])
        elif args.dpad:
            remote.send_key_command(DPAD_MAP[args.dpad])
        elif args.media:
            remote.send_key_command(MEDIA_MAP[args.media])
        elif args.input:
            # We use the launch_app command with the special URI for the input
            remote.send_launch_app_command(INPUT_MAP[args.input])
        elif args.power:
            remote.send_key_command("POWER")
        elif args.key:
            remote.send_key_command(args.key)
        elif args.app:
            remote.send_launch_app_command(args.app)
        elif args.text:
            remote.send_text(args.text)
            
        await asyncio.sleep(1.0)
        
    except Exception as e:
        print(f"Error sending command: {e}")
    finally:
        remote.disconnect()
        await asyncio.sleep(0.2)
        print("Done.")

if __name__ == "__main__":
    asyncio.run(_main())