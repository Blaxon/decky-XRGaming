import asyncio
import decky
import os
import select
import subprocess
import sys
import threading
import time
from settings import SettingsManager

sys.path.insert(1, decky.DECKY_PLUGIN_DIR)
from PyXRLinuxDriverIPC.xrdriveripc import XRDriverIPC

INSTALLED_VERSION_SETTING_KEY = "installed_from_plugin_version"
DONT_SHOW_AGAIN_SETTING_KEY = "dont_show_again"
MANIFEST_CHECKSUM_KEY = "manifest_checksum"
MEASUREMENT_UNITS_SETTING_KEY = "measurement_units"
BREEZY_INSTALL_STARTED_AT_SETTING_KEY = "breezy_install_started_at"
BREEZY_INSTALL_TIMEOUT_SECONDS = 60

RECENTER_BUTTON_ENABLED_KEY = "recenter_button_enabled"
RECENTER_BUTTON_COMBO_KEY = "recenter_button_combo"
DEFAULT_RECENTER_BUTTON_COMBO = "l4+r4"
RECENTER_BUTTON_HIDRAW_DEVICE = "/dev/hidraw2"
RECENTER_BUTTON_COOLDOWN_SECONDS = 1.0

# (byte offset, bitmask) for each button within a 64-byte controller HID report.
# Byte offsets/masks verified against the Steam Deck's raw input report format.
BUTTON_BITS = {
    "a": (8, 0x80), "b": (8, 0x20), "x": (8, 0x40), "y": (8, 0x10),
    "l1": (8, 0x08), "r1": (8, 0x04), "l2": (8, 0x02), "r2": (8, 0x01),
    "dup": (9, 0x01), "dright": (9, 0x02), "dleft": (9, 0x04), "ddown": (9, 0x08),
    "select": (9, 0x10), "steam": (9, 0x20), "start": (9, 0x40), "l4": (9, 0x80),
    "r4": (10, 0x01), "lpadtouch": (10, 0x08), "rpadtouch": (10, 0x10), "lstick": (10, 0x40),
    "rstick": (11, 0x04),
    "l3": (13, 0x02), "r3": (13, 0x04), "lstouch": (13, 0x40), "rstouch": (13, 0x80),
    "quick": (14, 0x04)
}
# lpadpress/rpadpress require both the touch and press bits set together
BUTTON_NAMES = set(BUTTON_BITS) | {"lpadpress", "rpadpress"}


def _button_pressed(report, name):
    if name == "lpadpress":
        return (report[10] & 0x0a) == 0x0a
    if name == "rpadpress":
        return (report[10] & 0x14) == 0x14

    offset, mask = BUTTON_BITS[name]
    return bool(report[offset] & mask)


class RecenterButtonListener:
    """Watches the controller's hidraw device on a background thread and fires
    on_trigger() when every button in combo is pressed simultaneously
    (edge-triggered, rate-limited by cooldown_seconds)."""

    def __init__(self, combo, on_trigger, device_path=RECENTER_BUTTON_HIDRAW_DEVICE,
                 cooldown_seconds=RECENTER_BUTTON_COOLDOWN_SECONDS):
        self._combo_buttons = combo.split("+")
        self._on_trigger = on_trigger
        self._device_path = device_path
        self._cooldown_seconds = cooldown_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread.join(timeout=3)

    def _run(self):
        try:
            fd = os.open(self._device_path, os.O_RDONLY)
        except OSError as e:
            decky.logger.error(f"Error opening {self._device_path}: {e}")
            return

        prev_pressed = False
        last_triggered = 0.0
        try:
            while not self._stop_event.is_set():
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue

                report = os.read(fd, 64)
                if len(report) < 15:
                    continue

                pressed = all(_button_pressed(report, name) for name in self._combo_buttons)
                now = time.monotonic()
                if pressed and not prev_pressed and (now - last_triggered) >= self._cooldown_seconds:
                    last_triggered = now
                    try:
                        self._on_trigger()
                    except Exception as e:
                        decky.logger.error(f"Error running recenter button trigger: {e}")
                prev_pressed = pressed
        except OSError as e:
            decky.logger.error(f"Error reading {self._device_path}: {e}")
        finally:
            os.close(fd)


settings = SettingsManager(name="settings", settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR)
settings.read()

ipc = XRDriverIPC(logger = decky.logger,
                  config_home = os.path.join(decky.DECKY_USER_HOME, ".config"),
                  supported_output_modes = ['virtual_display', 'sideview'])

class Plugin:
    def __init__(self):
        self.breezy_installed = False
        self._recenter_listener = None

    async def is_breezy_install_pending(self):
        started_at = settings.getSetting(BREEZY_INSTALL_STARTED_AT_SETTING_KEY)
        if started_at is None:
            return False

        try:
            started_at = float(started_at)
        except (TypeError, ValueError):
            settings.setSetting(BREEZY_INSTALL_STARTED_AT_SETTING_KEY, None)
            return False

        if time.time() - started_at > BREEZY_INSTALL_TIMEOUT_SECONDS:
            settings.setSetting(BREEZY_INSTALL_STARTED_AT_SETTING_KEY, None)
            return False

        return True

    def mark_breezy_install_started(self):
        settings.setSetting(BREEZY_INSTALL_STARTED_AT_SETTING_KEY, time.time())

    def clear_breezy_install_started(self):
        settings.setSetting(BREEZY_INSTALL_STARTED_AT_SETTING_KEY, None)
    
    async def retrieve_config(self):
        try:
            config = ipc.retrieve_config()
            measurement_units = settings.getSetting(MEASUREMENT_UNITS_SETTING_KEY)
            if measurement_units is not None:
                config['measurement_units'] = measurement_units
            return config
        except Exception as e:
            decky.logger.error(f"Error retrieving config {e}")
            return None
    
    async def write_config(self, config):
        try:
            config_copy = config.copy()
            if 'measurement_units' in config_copy:
                measurement_units = config_copy['measurement_units']
                del config_copy['measurement_units']
                settings.setSetting(MEASUREMENT_UNITS_SETTING_KEY, measurement_units)
            ipc.write_config(config_copy)

            return config
        except Exception as e:
            decky.logger.error(f"Error writing config {e}")
            return None

    async def write_control_flags(self, control_flags):
        ipc.write_control_flags(control_flags)

    async def retrieve_driver_state(self):
        return ipc.retrieve_driver_state()

    async def retrieve_dont_show_again_keys(self):
        return [key for key in settings.getSetting(DONT_SHOW_AGAIN_SETTING_KEY, "").split(",") if key]

    async def set_dont_show_again(self, key):
        try:
            dont_show_again_keys = await self.retrieve_dont_show_again_keys()
            dont_show_again_keys.append(key)
            settings.setSetting(DONT_SHOW_AGAIN_SETTING_KEY, ",".join(dont_show_again_keys))
            return True
        except Exception as e:
            decky.logger.error(f"Error setting dont_show_again {e}")
            return False

    async def reset_dont_show_again(self):
        try:
            settings.setSetting(DONT_SHOW_AGAIN_SETTING_KEY, "")
            return True
        except Exception as e:
            decky.logger.error(f"Error resetting dont_show_again {e}")
            return False

    async def is_breezy_installed_and_running(self):
        return self.breezy_installed

    async def is_driver_running(self):
        return ipc.is_driver_running(as_user=decky.DECKY_USER)

    async def force_reset_driver(self):
        return ipc.reset_driver(as_user=decky.DECKY_USER)

    async def get_recenter_button_config(self):
        return {
            "enabled": settings.getSetting(RECENTER_BUTTON_ENABLED_KEY, False),
            "combo": settings.getSetting(RECENTER_BUTTON_COMBO_KEY, DEFAULT_RECENTER_BUTTON_COMBO)
        }

    async def set_recenter_button_combo(self, combo):
        if not self._is_valid_combo(combo):
            decky.logger.error(f"Rejected invalid recenter button combo: {combo}")
            return False

        settings.setSetting(RECENTER_BUTTON_COMBO_KEY, combo)
        if settings.getSetting(RECENTER_BUTTON_ENABLED_KEY, False):
            self._restart_recenter_listener(combo)

        return True

    async def set_recenter_button_enabled(self, enabled):
        settings.setSetting(RECENTER_BUTTON_ENABLED_KEY, enabled)
        if enabled:
            combo = settings.getSetting(RECENTER_BUTTON_COMBO_KEY, DEFAULT_RECENTER_BUTTON_COMBO)
            self._restart_recenter_listener(combo)
        else:
            self._stop_recenter_listener()

        return True

    def _is_valid_combo(self, combo):
        return bool(combo) and all(button in BUTTON_NAMES for button in combo.split("+"))

    def _trigger_recenter(self):
        ipc.write_control_flags({"recenter_screen": True})

    def _restart_recenter_listener(self, combo):
        self._stop_recenter_listener()
        self._start_recenter_listener(combo)

    def _start_recenter_listener(self, combo):
        decky.logger.info(f"Starting recenter button listener for combo '{combo}'")
        self._recenter_listener = RecenterButtonListener(combo, self._trigger_recenter)
        self._recenter_listener.start()

    def _stop_recenter_listener(self):
        if self._recenter_listener is not None:
            self._recenter_listener.stop()
            self._recenter_listener = None

    async def check_breezy_installed(self):
        try:
            if not await self.is_driver_running():
                return False

            installed_from_plugin_version = settings.getSetting(INSTALLED_VERSION_SETTING_KEY)
            if not installed_from_plugin_version == decky.DECKY_PLUGIN_VERSION:
                decky.logger.info(f"Breezy plugin version {decky.DECKY_PLUGIN_VERSION} does not match installed version {installed_from_plugin_version}")
                return False

            if (await self.get_breezy_manifest_checksum()) != settings.getSetting(MANIFEST_CHECKSUM_KEY):
                decky.logger.info("Breezy manifest checksum does not match expected value")
                return False

            output = subprocess.check_output(['su', '-l', '-c', 'XDG_RUNTIME_DIR=/run/user/1000 ' + decky.DECKY_USER_HOME + '/.local/bin/breezy_vulkan_verify', decky.DECKY_USER], stderr=subprocess.STDOUT)
            self.breezy_installed = output.strip() == b"Verification succeeded"
            if not self.breezy_installed:
                decky.logger.error(f"Error verifying breezy installation {output}")
            
            return self.breezy_installed
        except subprocess.CalledProcessError as exc:
            decky.logger.error(f"Error checking driver installation {exc.output}")
            return False

    async def get_breezy_manifest_checksum(self):
        try:
            output = subprocess.check_output(["sha256sum", decky.DECKY_USER_HOME + "/.local/share/breezy_vulkan/manifest"], stderr=subprocess.STDOUT)

            # convert to a non-byte string, then split on spaces
            return output.strip().decode("utf-8").split(" ")[0]
        except subprocess.CalledProcessError as exc:
            decky.logger.error(f"Error getting breezy manifest checksum {exc.output}")
            return None
        
    async def install_breezy(self):
        self.loop.create_task(self._install_breezy())

        return True

    async def _install_breezy(self):
        decky.logger.info(f"Installing breezy for plugin version {decky.DECKY_PLUGIN_VERSION}")
        self.mark_breezy_install_started()

        # Set the USER environment variable for this command
        env_copy = os.environ.copy()
        del env_copy["LD_LIBRARY_PATH"]
        env_copy["USER"] = decky.DECKY_USER

        setup_script_path = os.path.dirname(__file__) + "/bin/breezy_vulkan_setup"
        binaries_dir = os.path.dirname(__file__) + "/bin/"

        if not os.path.isfile(setup_script_path):
            decky.logger.error(f"Breezy setup script not found at {setup_script_path}")
            self.clear_breezy_install_started()
            return False

        await self.write_control_flags({
            "request_features": ["sbs", "smooth_follow"]
        })

        attempt = 0
        while attempt < 3:
            try:
                subprocess.check_output([
                    setup_script_path,
                    "-v",
                    decky.DECKY_PLUGIN_VERSION.replace("-", "_"),
                    binaries_dir
                ], stderr=subprocess.STDOUT, env=env_copy)

                self.breezy_installed = await self.is_driver_running()
                if self.breezy_installed:
                    settings.setSetting(INSTALLED_VERSION_SETTING_KEY, decky.DECKY_PLUGIN_VERSION)
                    settings.setSetting(MANIFEST_CHECKSUM_KEY, await self.get_breezy_manifest_checksum())
                    self.clear_breezy_install_started()

                    decky.logger.info(f"Breezy install succeeded on attempt {attempt}")
                    
                    return True
            except FileNotFoundError as exc:
                # don't return, we still want to retry in case a file was still being downloaded
                decky.logger.error(f"Breezy install failed because a required file was missing: {exc}")
                time.sleep(4) # overall sleep of 5 seconds with the sleep below
            except subprocess.CalledProcessError as exc:
                decky.logger.error(f"Error running setup script: {exc.output}")

            attempt += 1
            time.sleep(1)

        return False

    async def request_token(self, email):
        return ipc.request_token(email)

    async def verify_token(self, token):
        return ipc.verify_token(token)
    
    # Asyncio-compatible long-running code, executed in a task when the plugin is loaded
    async def _main(self):
        self.loop = asyncio.get_event_loop()

        if settings.getSetting(RECENTER_BUTTON_ENABLED_KEY, False):
            combo = settings.getSetting(RECENTER_BUTTON_COMBO_KEY, DEFAULT_RECENTER_BUTTON_COMBO)
            self._start_recenter_listener(combo)

    # Function called first during the unload process, utilize this to handle your plugin being removed
    async def _unload(self):
        self._stop_recenter_listener()

    # Migrations that should be performed before entering `_main()`.
    async def _migration(self):
        pass

    async def _uninstall(self):
        decky.logger.info(f"Uninstalling breezy for plugin version {decky.DECKY_PLUGIN_VERSION}")

        self._stop_recenter_listener()

        # Set the USER environment variable for this command
        env_copy = os.environ.copy()
        del env_copy["LD_LIBRARY_PATH"]
        env_copy["USER"] = decky.DECKY_USER

        try:
            subprocess.check_output([decky.DECKY_USER_HOME + "/.local/bin/breezy_vulkan_uninstall"], stderr=subprocess.STDOUT, env=env_copy)
            subprocess.check_output([decky.DECKY_USER_HOME + "/.local/bin/xr_driver_uninstall"], stderr=subprocess.STDOUT, env=env_copy)
            settings.setSetting(INSTALLED_VERSION_SETTING_KEY, None)
            settings.setSetting(MANIFEST_CHECKSUM_KEY, None)
            self.clear_breezy_install_started()
            self.breezy_installed = False
            return True
        except subprocess.CalledProcessError as exc:
            decky.logger.error(f"Error running uninstall script {exc.output}")
            return False
