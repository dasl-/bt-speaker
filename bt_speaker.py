#!/usr/bin/python3

from gi.repository import GLib
from bt_manager.audio import SBCAudioSink
from bt_manager.media import BTMedia
from bt_manager.device import BTDevice
from bt_manager.agent import BTAgent, BTAgentManager
from bt_manager.adapter import BTAdapter
from bt_manager.serviceuuids import SERVICES
from bt_manager.uuid import BTUUID

import dbus
import dbus.mainloop.glib
import signal
import subprocess
import alsaaudio
import math
import configparser
import io
import os
import re

SCRIPT_PATH = os.path.dirname(os.path.realpath(__file__))

config = configparser.ConfigParser()
config.read(SCRIPT_PATH + '/config.ini.default')
config.read('/etc/bt_speaker/config.ini')

class PipedSBCAudioSinkWithAlsaVolumeControl(SBCAudioSink):
    """
    An audiosink that pipes the decoded output to a command via stdin.
    The class also sets the volume of an alsadevice
    """
    def __init__(self):
        SBCAudioSink.__init__(self, path='/endpoint/a2dpsink')
        self.startup()

    def startup(self):
        # Start process
        self.process = subprocess.Popen(
            config.get('bt_speaker', 'play_command'),
            shell=True,
            bufsize=2560,
            stdin=subprocess.PIPE
        )

        if config.getboolean('alsa', 'enabled'):
            # Use first available if no mixer is set
            # control = config.get('alsa', 'mixer') or alsaaudio.mixers()[0]
            # print("Using mixer %s" % control)
            self.__volume_controller = VolumeController()

            # Hook into alsa service for volume control
            # self.alsamixer = alsaaudio.Mixer(
            #     control=control,
            #     id=int(config.get('alsa', 'id')),
            #     cardindex=int(config.get('alsa', 'cardindex'))
            # )

    def raw_audio(self, data):
        # pipe to the play command
        try:
            self.process.stdin.write(data)
        except:
            # try to restart process on failure
            self.startup()
            self.process.stdin.write(data)

    def volume(self, new_volume):
        if not config.getboolean('alsa', 'enabled'):
            return

        # normalize volume
        volume = float(new_volume) / 127.0

        print("Volume changed to %i%%" % (volume * 100.0), flush = True)

        self.__volume_controller.set_vol_pct(volume * 100)

        # it looks like the value passed to alsamixer sets the volume by 'power level'
        # to adjust to the (human) perceived volume, we have to square the volume
        # @todo check if this only applies to the raspberry pi or in general (or if i got it wrong)
        # volume = math.pow(volume, 1.0/3.0)

        # alsamixer takes a percent value as integer from 0-100
        # self.alsamixer.setvolume(int(volume * 100.0))

class AutoAcceptSingleAudioAgent(BTAgent):
    """
    Accepts one client unconditionally and hides the device once connected.
    As long as the client is connected no other devices may connect.
    This 'first comes first served' is not necessarily the 'bluetooth way' of
    connecting devices but the easiest to implement.
    """
    def __init__(self, connect_callback, disconnect_callback, track_callback):
        BTAgent.__init__(self, default_pin_code=config.get('bluez', 'pin_code') or '0000', cb_notify_on_authorize=self.auto_accept_one)
        self.adapter = BTAdapter(config.get('bluez', 'device_path'))
        self.adapter.set_property('Discoverable', config.getboolean('bluez', 'discoverable'))
        self.allowed_uuids = [ SERVICES["AdvancedAudioDistribution"].uuid, SERVICES["AVRemoteControl"].uuid ]
        self.connected = None
        self.tracked_devices =  []
        self.connect_callback = connect_callback
        self.disconnect_callback = disconnect_callback
        self.track_callback = track_callback
        self.update_discoverable()

    def update_discoverable(self):
        if not config.getboolean('bluez', 'discoverable'):
            return

        if bool(self.connected):
            pass # Keep it discoverable
            # print("Hiding adapter from all devices.")
            # self.adapter.set_property('Discoverable', False)
        else:
            print("Showing adapter to all devices.", flush = True)
            self.adapter.set_property('Discoverable', True)

    def auto_accept_one(self, method, device, uuid):
        if not BTUUID(uuid).uuid in self.allowed_uuids: return False
        if self.connected and self.connected != device:
            # print("Rejecting device, because another one is already connected. connected_device=%s, device=%s" % (self.connected, device))
            # return False
            print(f"Device disconnected because a new one is connecting: old device ={self.connected} , new device: {device}, new uuid: {uuid}", flush = True)
            old_device_addr = self.connected.split('/dev_')[1].replace('_', ':')
            cmd = f'bluetoothctl disconnect {old_device_addr}'
            output = (subprocess
                .check_output(cmd, shell = True, executable = '/bin/bash')
                .decode("utf-8"))
            print(f"Ran {cmd}. Output: {output}")

            self.connected = None
            self.update_discoverable()
            self.disconnect_callback()

        # track connection state of the device (is there a better way?)
        if not device in self.tracked_devices:
            self.tracked_devices.append(device)
            self.adapter._bus.add_signal_receiver(self._track_connection_state,
                                                  path=device,
                                                  signal_name='PropertiesChanged',
                                                  dbus_interface='org.freedesktop.DBus.Properties',
                                                  path_keyword='device')
            self.adapter._bus.add_signal_receiver(self._watch_track,
                                                  path=device + '/player0',
                                                  signal_name='PropertiesChanged',
                                                  dbus_interface='org.freedesktop.DBus.Properties',
                                                  path_keyword='device')

        return True

    def _watch_track(self, addr, properties, signature, device):
        if not 'Track' in properties: return
        self.track_callback(properties['Track'])

    def _track_connection_state(self, addr, properties, signature, device):
        if self.connected and self.connected != device: return
        if not 'Connected' in properties: return

        if not self.connected and bool(properties['Connected']):
            print("Device connected. device=%s" % device, flush = True)
            self.connected = device
            self.update_discoverable()
            self.connect_callback()

        elif self.connected and not bool(properties['Connected']):
            print("Device disconnected. device=%s" % device, flush = True)
            self.connected = None
            self.update_discoverable()
            self.disconnect_callback()


# Gets and sets alsa volume
class VolumeController:

    __GLOBAL_MIN_VOL_VAL = None
    __GLOBAL_MAX_VOL_VAL = None

    # gets a perceptual loudness %
    # returns a float in the range [0, 100]
    def get_vol_pct(self):
        vol_val = self.get_vol_val()
        if vol_val <= VolumeController.__get_global_min_vol_val():
            return 0

        if VolumeController.__should_adjust_volume_logarithmically():
            # Assume that the volume value is a value in millibels if we are adjusting volume logarithmically.
            # This might be a poor assumption if it's only true on the RPI internal soundcard...
            mb_level = vol_val

            # convert from decibel attenuation amount to perceptual loudness %
            # see: http://www.sengpielaudio.com/calculator-levelchange.htm
            db_level = mb_level / 100
            vol_pct = 100 * math.pow(2, (db_level / 10))
        else:
            vol_pct = 100 * vol_val / VolumeController.__get_limited_max_vol_val()

        vol_pct = max(0, vol_pct)
        vol_pct = min(100, vol_pct)
        return vol_pct

    # takes a perceptual loudness %.
    # vol_pct should be a float in the range [0, 100]
    def set_vol_pct(self, vol_pct):
        vol_pct = max(0, vol_pct)
        vol_pct = min(100, vol_pct)

        if VolumeController.__should_adjust_volume_logarithmically():
            # Assume that the volume value is a value in millibels if we are adjusting volume logarithmically.
            # This might be a poor assumption if it's only true on the RPI internal soundcard...
            mb_level = VolumeController.pct_to_millibels(vol_pct)
            vol_val = mb_level
        else:
            vol_val = vol_pct * VolumeController.__get_limited_max_vol_val() / 100

        vol_val = round(vol_val)
        subprocess.check_output(
            ('amixer', '-c', str(0), 'cset', f'numid={1}', '--', str(vol_val))
        )

    # increments volume percentage by the specified increment. The increment should be a float in the range [0, 100]
    # Returns the new volume percent, which will be a float in the range [0, 100]
    def increment_vol_pct(self, inc = 1):
        old_vol_pct = self.get_vol_pct()
        new_vol_pct = old_vol_pct + inc
        new_vol_pct = max(0, new_vol_pct)
        new_vol_pct = min(100, new_vol_pct)
        self.set_vol_pct(new_vol_pct)
        return new_vol_pct

    @staticmethod
    def __get_global_min_vol_val():
        if VolumeController.__GLOBAL_MIN_VOL_VAL is not None:
            return VolumeController.__GLOBAL_MIN_VOL_VAL
        else:
            VolumeController.__init_global_min_and_max_vol_vals()
            return VolumeController.__GLOBAL_MIN_VOL_VAL

    @staticmethod
    def __get_global_max_vol_val():
        if VolumeController.__GLOBAL_MAX_VOL_VAL is not None:
            return VolumeController.__GLOBAL_MAX_VOL_VAL
        else:
            VolumeController.__init_global_min_and_max_vol_vals()
            return VolumeController.__GLOBAL_MAX_VOL_VAL

    @staticmethod
    def __init_global_min_and_max_vol_vals():
        res = subprocess.check_output(
            ('amixer', '-c', str(0), 'cget', f'numid={1}'),
        ).decode("utf-8")
        m = re.search(r",min=(-?\d+),max=(-?\d+)", res, re.MULTILINE)

        if m is None:
            # use the defaults for the raspberry pi built in headphone jack:
            # amixer output: ; type=INTEGER,access=rw---R--,values=1,min=-10239,max=400,step=0
            # These values are in millibels.
            VolumeController.__GLOBAL_MIN_VOL_VAL = -10239
            VolumeController.__GLOBAL_MAX_VOL_VAL = 400
        else:
            VolumeController.__GLOBAL_MIN_VOL_VAL = int(m.group(1))
            VolumeController.__GLOBAL_MAX_VOL_VAL = int(m.group(2))

    @staticmethod
    def __should_adjust_volume_logarithmically():
        if VolumeController.__is_internal_soundcard_being_used():
            # Assume we're using the raspberry pi internal soundcard's headphone jack
            return True

        return False

    @staticmethod
    def __get_limited_max_vol_val():
        if VolumeController.__is_internal_soundcard_being_used():
            # Assume we're using the raspberry pi internal soundcard's headphone jack
            # Anything higher than 0 dB may result in clipping.
            return 0

        return VolumeController.__get_global_max_vol_val()

    # Attempt to autodetect if the default soundcard is being used, based on config.json values.
    @staticmethod
    def __is_internal_soundcard_being_used():
        return 0 == 0 and 1 == 1

    # Return volume value. Returns an integer in the range
    # [VolumeController.__get_global_min_vol_val(), VolumeController.__get_limited_max_vol_val()]
    def get_vol_val(self):
        res = subprocess.check_output(
            ('amixer', '-c', str(0), 'cget', f'numid={1}')
        ).decode("utf-8")
        m = re.search(r" values=(-?\d+)", res, re.MULTILINE)
        if m is None:
            return VolumeController.__get_global_min_vol_val()

        vol_val = int(m.group(1))
        vol_val = max(VolumeController.__get_global_min_vol_val(), vol_val)
        vol_val = min(VolumeController.__get_limited_max_vol_val(), vol_val)
        return vol_val

    # Map the volume from [0, 100] to [0, 1]
    @staticmethod
    def normalize_vol_pct(vol_pct):
        vol_pct_normalized = vol_pct / 100
        vol_pct_normalized = max(0, vol_pct_normalized)
        vol_pct_normalized = min(1, vol_pct_normalized)
        return vol_pct_normalized

    # input: [0, 100]
    # output: [VolumeController.__get_global_min_vol_val(), VolumeController.__get_limited_max_vol_val()]
    @staticmethod
    def pct_to_millibels(vol_pct):
        if (vol_pct <= 0):
            mb_level = VolumeController.__get_global_min_vol_val()
        else:
            # get the decibel adjustment required for the human perceived loudness %.
            # see: http://www.sengpielaudio.com/calculator-levelchange.htm
            mb_level = 1000 * math.log(vol_pct / 100, 2)

        mb_level = max(VolumeController.__get_global_min_vol_val(), mb_level)
        mb_level = min(VolumeController.__get_limited_max_vol_val(), mb_level)
        return mb_level

def setup_bt():
    # register sink and media endpoint
    sink = PipedSBCAudioSinkWithAlsaVolumeControl()
    media = BTMedia(config.get('bluez', 'device_path'))
    media.register_endpoint(sink._path, sink.get_properties())

    def startup():
        command = config.get('bt_speaker', 'startup_command')
        if not command: return
        subprocess.Popen(command, shell=True).communicate()

    def connect():
        command = config.get('bt_speaker', 'connect_command')
        if not command: return
        subprocess.Popen(command, shell=True).communicate()

    def disconnect():
        sink.close_transport()
        command = config.get('bt_speaker', 'disconnect_command')
        if not command: return
        subprocess.Popen(command, shell=True).communicate()

    def track(data):
        command = config.get('bt_speaker', 'track_command')
        if not command: return
        env = dict()
        for key in data:
            if type(data[key]) == dbus.String:
                env[key.upper()] = data[key].encode("utf-8")
        # dirty hack to prevent unnecessary double execution
        if str(env) == track.last: return
        track.last = str(env)
        subprocess.Popen(command, shell=True, env=env).communicate()

    track.last = None

    # setup bluetooth agent (that manages connections of devices)
    agent = AutoAcceptSingleAudioAgent(connect, disconnect, track)
    manager = BTAgentManager()
    manager.register_agent(agent._path, "NoInputNoOutput")
    manager.request_default_agent(agent._path)

    startup()

def run():
    # Initialize the DBus SystemBus
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    # Mainloop for communication
    mainloop = GLib.MainLoop()

    # catch SIGTERM
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, lambda signal: mainloop.quit(), None)

    # setup bluetooth configuration
    setup_bt()

    # Run
    mainloop.run()

if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        print('KeyboardInterrupt', flush = True)
    except Exception as e:
        print(e.message, flush = True)
