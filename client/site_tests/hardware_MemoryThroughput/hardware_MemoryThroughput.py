# Lint as: python2, python3
# Copyright 2009 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import logging

from autotest_lib.client.bin import test
from autotest_lib.client.common_lib import error, utils
from autotest_lib.client.cros import service_stopper


class hardware_MemoryThroughput(test.test):
    """Autotest for measuring memory throughput.

    This uses mem_bw with different parameters to measure memory and cache
    throughput.

    Private Attributes:
      _results: dict containing keyvals with throughput measurements
      _services: service_stopper.ServiceStopper object
    """
    version = 1

    def _run_benchmarks(self, test, warmup, num_iterations, parallel, sizes,
                        criteria):
        """Run the benchmark.

        This runs the bw_mem benchmark from lmbench 3 and fills out results.
        Args:
          test: string containing either rd, rdwr, cp, bzero, or bcopy
          warmup:  integer amount of time to spend warming up in microseconds.
          num_iterations: integer number of times to run the benchmark on each
            size.
          parallel: integer number of instances to run in parallel
          sizes: list of integer sizes in bytes to run
          criteria: the test fails if the throughput is less than the criteria
            in MB/s.
        """
        r = {}

        for size in sizes:
            cmd = 'bw_mem -P %d -W %d -N %d %d %s 2>&1' % (
                    parallel, warmup, num_iterations, size, test)
            logging.debug('cmd: %s', cmd)
            out = utils.system_output(cmd)
            logging.debug('Output: %s', out)

            lines = out.splitlines()
            if len(lines) != 1:
                raise error.TestFail('invalid amount of output from bw_mem')

            s = lines[0].split()
            if len(s) == 2:
                bw_mb_per_sec = float(s[1])
                if bw_mb_per_sec <= 0:
                    raise error.TestFail('invalid throughput %f' %
                                         bw_mb_per_sec)
                key = ('MB_per_second_' + test + '-' + str(parallel) +
                       '-thread-' + str(size / 1024) + 'KB')
                self._results[key] = bw_mb_per_sec
                if bw_mb_per_sec < criteria:
                    raise error.TestFail(
                            'The memory throughput %f (MB/s) is less than the '
                            'criteria %d (MB/s)' % (bw_mb_per_sec, criteria))
            else:
                raise error.TestFail('invalid output line %s' % lines[0])

    def initialize(self):
        super(hardware_MemoryThroughput, self).initialize()
        self._results = {}
        stop = ['ui']
        stop.extend(service_stopper.ServiceStopper.POWER_DRAW_SERVICES)
        self._services = service_stopper.ServiceStopper(stop)
        self._services.stop_services()

    def run_once(self,
                 test='rd',
                 warmup=100,
                 num_iterations=20,
                 parallel=1,
                 sizes=[64 * 1024 * 1024],
                 criteria=0):
        self._run_benchmarks(test, warmup, num_iterations, parallel, sizes,
                             criteria)
        self.write_perf_keyval(self._results)

    def cleanup(self):
        self._services.restore_services()
        super(hardware_MemoryThroughput, self).cleanup()
