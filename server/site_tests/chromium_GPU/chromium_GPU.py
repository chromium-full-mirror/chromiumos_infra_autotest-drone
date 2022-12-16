# Lint as: python2, python3
# Copyright (c) 2022 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import logging
import os
import sys
import shutil
import tempfile

from autotest_lib.client.common_lib import error
from autotest_lib.client.common_lib import utils
from autotest_lib.client.common_lib.cros import tpm_utils
from autotest_lib.server import test
from autotest_lib.server.cros import chrome_sideloader

MAX_GPU_TELEMETRY_TIMEOUT_SEC = 3600


class chromium_GPU(test.test):
    """Run GPU integration tests for the Chrome built by browser infra."""
    version = 1

    # The path where TLS provisioned the lacros image.
    CHROME_PROVISION = '/var/lib/imageloader/lacros'

    # The path where we install chromium/src. In this experimental
    # stage, we may use existing lacros image, which built at src.
    CHROME_BUILD = '/usr/local/lacros-build'

    # GPU integration tests are calling telemetry to manipulate the DUT.
    # In telemetry the lacros chrome must be stored at below path.
    # See go/lacros_browser_backend.
    LACROS_MOUNT_POINT = '/usr/local/lacros-chrome'
    CHROME_MOUNT_POINT = '/opt/google/chrome'

    def initialize(self, host=None, args=None):
        self.host = host
        assert self.host.path_exists(self.CHROME_PROVISION), (
                'lacros artifact'
                'is not provisioned by CTP. Please check the CTP request.')

        self.args_dict = utils.args_to_dict(args)
        self.test_args = chrome_sideloader.get_test_args(
                self.args_dict, 'test_args').split(' ')

        chrome_sideloader.setup_host(
                self.host,
                self.CHROME_BUILD,
                self.CHROME_MOUNT_POINT
                if self.is_cros_chrome else self.LACROS_MOUNT_POINT,
                is_cros_chrome=self.is_cros_chrome,
        )

        if not self.args_dict.get('run_private_tests',
                                  True) in [False, 'False']:
            tpm_utils.ClearTPMOwnerRequest(self.host, wait_for_ready=True)

        # Chromium GPU tests have its own server side packages and can be
        # invoked directly on Drone server.
        # We copy it from the DUT, because TLS can not provision server
        # packages. Ideally, this should be built into the test driver
        # docker, if we move to CFT(go/cros-cft-site).
        self.server_pkg = tempfile.mkdtemp()
        self.host.get_file('{}/'.format(self.CHROME_BUILD),
                           '{}/'.format(self.server_pkg),
                           preserve_perm=False,
                           preserve_symlinks=True)
        # The test script needs to know it is running in Skylab environment.
        os.environ['RUNNING_IN_SKYLAB'] = '1'

    @property
    def is_cros_chrome(self):
        return any(['--browser=cros-chrome' in x for x in self.test_args])

    def run_once(self):
        """Run a GPU integration test."""
        vpython3_spec = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), '.vpython3')
        cmd = [
                # On Drone server vpython is in the PATH, so this must
                # run out of SSP.
                'vpython',
                '-vpython-spec',
                vpython3_spec,
                os.path.join(self.server_pkg, 'testing', 'scripts',
                             'run_gpu_integration_test_as_googletest.py'),
                os.path.join(self.server_pkg, 'content', 'test', 'gpu',
                             'run_gpu_integration_test.py'),
                # Generate a gtest-ish result file. This is mostly for reference now,
                # because GPU tests do not upload it to RDB.
                '--isolated-script-test-output={}'.format(
                        os.path.join(self.resultsdir, 'output.json')),
                '--chromium-output-directory={}'.format(
                        os.path.join(self.server_pkg, 'out', 'Release')),
                '--remote={}'.format(self.host.hostname),
        ]
        # Pass the test arguments from the browser test owners.
        # Note utils.run() quotes all members in the cmd list. We have
        # to split our test_args, or it will feed to
        # run_gpu_integration_test.py as a single string. The same
        # reason we have to pass extra-browser-args separately.
        cmd.extend(self.test_args)
        # Autotest does not recognize args with '-'.
        cmd.append('--extra-browser-args="{}"'.format(
                chrome_sideloader.get_test_args(self.args_dict,
                                                'extra_browser_args')))

        logging.debug('Running: %s', cmd)
        exit_code = 0
        try:
            result = utils.run(cmd,
                               stdout_tee=sys.stdout,
                               stderr_tee=sys.stderr,
                               timeout=MAX_GPU_TELEMETRY_TIMEOUT_SEC)
            exit_code = result.exit_status
        except error.CmdError as e:
            logging.debug('Error occurred executing GPU integration tests.')
            exit_code = e.result_obj.exit_status

        if exit_code:
            raise error.TestFail('Chromium GPU Integration Test'
                                 ' failed to run.')

    def cleanup(self):
        chrome_sideloader.cleanup_host(
                self.host,
                self.CHROME_BUILD,
                self.CHROME_MOUNT_POINT
                if self.is_cros_chrome else self.LACROS_MOUNT_POINT,
                is_cros_chrome=self.is_cros_chrome)
        shutil.rmtree(self.server_pkg)
