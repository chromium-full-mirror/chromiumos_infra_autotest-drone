# Copyright 2021 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import logging
import os
import time

from autotest_lib.client.common_lib import error
from autotest_lib.client.common_lib import ui_utils
from autotest_lib.client.common_lib.cros import chrome
from autotest_lib.client.cros.audio import alsa_utils
from autotest_lib.client.cros.audio import audio_test_data
from autotest_lib.client.cros.audio import cras_utils
from autotest_lib.client.cros.graphics import graphics_utils
from autotest_lib.client.cros.input_playback import input_playback
from autotest_lib.client.cros.multimedia import facade_resource
from autotest_lib.client.cros.power import power_test


class audio_AudioPlaybackStress(power_test.power_Test):
    """ Playing multiple local WAV audio files for longer duration without
    crash and verifying that the audio is routing through INTERNAL SPEAKER.
    """

    version = 1
    _PLAYBACK_LOCATION = '/home/chronos/user/MyFiles/Downloads'
    _FILES_APP_NAME = 'files'

    # Timeout delay for clicking ui elements in seconds
    _WAIT_DELAY = 3

    # Play audio for 60 seconds
    _PLAY_TIME = 60

    # Names
    _DOWNLOADS = 'Downloads'
    _MY_FILES = '/^Files - My files$/i'
    _FILES_WINDOW_DOWNLOADS = '/^Files - Downloads$/i'
    _AUDIO_PLAYER = '/^audio player$/i'
    _WAV_FILE = '/.wav/i'
    _NEXT = '/^next$/i'
    _PLAY = '/^play$/i'
    _PAUSE = '/^pause$/i'

    # Roles
    _TREEE_ITEM = 'treeItem'
    _WINDOW = 'window'
    _LISTBOX_OPTION = 'listBoxOption'
    _BUTTON = 'button'

    def cleanup(self):
        if len(self.audio_files) != 0:
            for audio_file in self.audio_files:
                audio_file.delete()
        if self.ui:
            logging.info(self.ui.get_name_role_list())
        if not self.success:
            graphics_utils.take_screenshot(os.path.join(self.debugdir),
                                           'chrome')
        if self.app:
            self.app.close_app(self._FILES_APP_NAME)
        if self.cr:
            self.cr.close()
        super(audio_AudioPlaybackStress, self).cleanup()

    def generate_audio_files(self):
        """ Audio files with constant sampling rates that needs to be generated """
        sampling_rates = [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000]
        for sampling_rate in sampling_rates:
            mono_audio_file = audio_test_data.GenerateAudioTestData(
                    path=os.path.join(self._PLAYBACK_LOCATION,
                                      'audio_%s_mono_440_16.wav' %
                                      sampling_rate),
                    data_format=dict(
                    file_type='wav', sample_format='S16_LE', channel=1,
                    rate=sampling_rate),
                    duration_secs=70,
                    frequencies=[440, 440],
                    volume_scale=1)
            self.audio_files.append(mono_audio_file)
            stereo_audio_file = audio_test_data.GenerateAudioTestData(
                    path=os.path.join(self._PLAYBACK_LOCATION,
                                      'audio_%s_stereo_440_16.wav'
                                      % sampling_rate),
                    data_format=dict(
                    file_type='wav', sample_format='S16_LE', channel=2,
                    rate=sampling_rate),
                    duration_secs=70,
                    frequencies=[440, 440],
                    volume_scale=1)
            self.audio_files.append(stereo_audio_file)

    def initialize_test(self):
        self.success = False
        self.cr = chrome.Chrome(autotest_ext=True, disable_default_apps=False)
        self.audio_files = []
        self.generate_audio_files()
        self.ui = ui_utils.UI_Handler()
        self.ui.start_ui_root(self.cr)
        self._player = input_playback.InputPlayback()
        self._player.emulate(input_type='keyboard')
        self._player.find_connected_inputs()
        self.app = facade_resource.Application(self.cr)
        self.app.launch_app(self._FILES_APP_NAME)

        def _open_my_files():
            logging.info("Clicking on the My Files window")
            self.ui.wait_for_ui_obj(name=self._MY_FILES, isRegex=True,
                                    role=self._WINDOW)
            self.app.click_on(self.ui, self._MY_FILES, isRegex=True,
                        role=self._WINDOW)
            self.ui.wait_for_ui_obj(name=self._DOWNLOADS, isRegex=False,
                                    role=self._TREEE_ITEM)
        def _open_downloads_folder():
            logging.info("Selecting %s folder", self._DOWNLOADS)
            self.app.click_on(self.ui, self._DOWNLOADS, isRegex=False,
                        role=self._TREEE_ITEM)
            if self.ui.did_obj_not_load(self._FILES_WINDOW_DOWNLOADS,
                                isRegex=True, timeout=self._WAIT_DELAY):
                raise error.TestFail("Failed to open %s directory" %
                                    self._DOWNLOADS)
        def _play_wav_file():
            self.app.click_on(self.ui, self._WAV_FILE, isRegex=True,
                          role=self._LISTBOX_OPTION)
            self._player.blocking_playback_of_default_file(
                input_type='keyboard', filename='keyboard_enter')
            self.ui.wait_for_ui_obj(self._NEXT, isRegex=True, role=self._BUTTON)
            if self.ui.did_obj_not_load(self._AUDIO_PLAYER, isRegex=True,
                                   timeout=self._WAIT_DELAY):
                raise error.TestFail("Audio Player is not opened")
            self.previous_song = self.ui.doCommand_on_obj(self._AUDIO_PLAYER,
                                                self.ui._GET_ON_SCREEN_ITEMS,
                                                isRegex=True,
                                                role=self._WINDOW)[0]
        _open_my_files()
        _open_downloads_folder()
        _play_wav_file()

    def run_once(self, test_duration=10):
        """ Play the local audio files from eMMC for longer duration.

        @param test_duration: Duration for playing local audio files in seconds.
        """

        self.initialize_test()
        start_time = time.time()
        audio_not_changed = 0
        device_name = cras_utils.get_selected_output_device_name()
        device_type = cras_utils.get_selected_output_device_type()
        logging.info("Avoid display blanking")
        self.app.evaluate_javascript("""chrome.power.requestKeepAwake("display")""")

        self.start_measurements()
        while (time.time() - start_time) <= test_duration:
            logging.info("Time duration: %s" % (time.time() - start_time))
            if self.ui.did_obj_not_load(self._AUDIO_PLAYER, isRegex=True):
                raise error.TestFail("Not on audio player")
            logging.debug("Previous song is %s", self.previous_song)
            if self.ui.did_obj_not_load(self._PAUSE, isRegex=True,
                                   timeout=self._WAIT_DELAY):
                raise error.TestFail("%s is not playing" % self.previous_song)
            alsa_utils.check_audio_stream_at_selected_device(device_name,
                                                             device_type)
            logging.debug('Playing next song')
            self.ui.doDefault_on_obj(self._NEXT, isRegex=True,
                                role=self._BUTTON)
            time.sleep(self._PLAY_TIME)
            song = self.ui.doCommand_on_obj(self._AUDIO_PLAYER,
                                       self.ui._GET_ON_SCREEN_ITEMS,
                                       isRegex=True,
                                       role=self._WINDOW)[0]
            logging.debug("Current song is %s", song)
            if self.previous_song == song:
                audio_not_changed += 1
                logging.info(
                    'audio file is not changed at %s. previous_song is %s,'
                    ' current song is %s' % (time.time(), self.previous_song, song))
            self.previous_song = song

        if audio_not_changed != 0:
            raise error.TestFail("%d audio(s) failed to change during execution"
                                 % audio_not_changed)
        self.success = True
