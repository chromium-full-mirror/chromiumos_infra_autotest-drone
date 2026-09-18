# -*- coding: utf-8 -*-
# Copyright 2021 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""A class that sets up the environment for telemetry testing."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from autotest_lib.client.common_lib.cros import dev_server

import contextlib
import errno
import fcntl
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile

import requests

_READ_BUFFER_SIZE_BYTES = 1024 * 1024  # 1 MB

# Applies to connecting and to each wait for data, including the wait for the
# response headers, not to the whole download. A timeout of either kind, like
# a failed connection, sends this and any remaining downloads over SSH.
_HTTP_TIMEOUT_SECONDS = 60

# Bounds for the SSH fallback, which would otherwise wait forever on an
# unresponsive devserver. ConnectTimeout covers connecting and the handshake,
# ServerAlive* notices a connection that dies silently, and curl's --max-time
# runs on the devserver once curl starts; the transfer limit is generous
# because the dependency tarballs are large. The local timeout is the backstop
# for anything else, such as a remote login that hangs before curl starts.
_SSH_CONNECT_TIMEOUT_SECONDS = 60
_SSH_SERVER_ALIVE_INTERVAL_SECONDS = 30
_SSH_SERVER_ALIVE_COUNT_MAX = 4
_SSH_CURL_MAX_TIME_SECONDS = 30 * 60
_SSH_LOCAL_TIMEOUT_SECONDS = (_SSH_CONNECT_TIMEOUT_SECONDS +
                              _SSH_CURL_MAX_TIME_SECONDS + 60)

# curl exits with this for any HTTP status of 400 or above when --fail is
# passed, so on its own it cannot tell a missing file from, say, a 503.
_CURL_HTTP_ERROR_EXIT_CODE = 22

# What 'curl --fail --show-error' prints on stderr for a 404. Older versions of
# curl append the reason phrase ('404 Not Found').
_CURL_HTTP_404_RE = re.compile(r'returned error: 404\b')

_ZSTD_TARBALL_SUFFIX = '.tar.zst'
_BZIP2_TARBALL_SUFFIX = '.tar.bz2'


def _has_zstd():
    """Returns whether the zstd binary that tar needs for .tar.zst exists."""
    return shutil.which('zstd') is not None


def _output_text(output):
    """Returns subprocess output as text, for use in messages.

    Args:
      output: Output captured from a subprocess, as bytes.
    """
    return output.decode('utf-8', 'replace').strip()


@contextlib.contextmanager
def lock_dir(dir_name):
    """Lock a directory exclusively by placing a file lock in it.

    Args:
      dir_name: the directory name to be locked.
    """
    lock_file = os.path.join(dir_name, '.lock')
    with open(lock_file, 'w+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class TelemetrySetupError(Exception):
    """Exception class used by this module."""
    pass


class TelemetrySetup(object):
    """Class that sets up the environment for telemetry testing."""

    # Relevant directory paths.
    _BASE_DIR_PATH = '/tmp/telemetry-workdir'
    _PARTIAL_DEPENDENCY_DIR_PATH = 'autotest/packages'

    # Relevant directory names.
    _TELEMETRY_SRC_DIR_NAME = 'telemetry_src'
    _TEST_SRC_DIR_NAME = 'test_src'
    _SRC_DIR_NAME = 'src'

    # Base names of the telemetry dependency tarballs, without a compression
    # suffix.
    _DEPENDENCIES = [
            'dep-telemetry_dep',
            'dep-page_cycler_dep',
            'dep-chrome_test',
            'dep-perf_data_dep',
    ]

    # Compression suffixes to try, in order of preference. Builds published
    # before the switch to zstd only carry the bzip2 variant, transitional
    # builds carry both, and a drone at HEAD has to be able to test all of
    # them. zstd goes first so that transitional builds already get its much
    # faster extraction; a .tar.zst that fails to extract falls back to the
    # .tar.bz2 next to it.
    #
    # Extracting .tar.zst relies on the writer (client/common_lib/packages.py
    # in chromiumos/third_party/autotest) keeping zstd's --long at 27 or
    # below: tar pipes into zstd, so the frame always declares the full
    # window, and 2**27 is the largest window zstd decodes without extra flags.
    #
    # TODO(b/563203733): Drop '.tar.bz2' once no supported (including LTS)
    # branch publishes it.
    _TARBALL_SUFFIXES = [
            _ZSTD_TARBALL_SUFFIX,
            _BZIP2_TARBALL_SUFFIX,
    ]

    # Partial devserver URLs.
    _STATIC_URL_TEMPLATE = '%s/static/%s/autotest/packages/%s'

    def __init__(self, hostname, build):
        """Initializes the TelemetrySetup class.

        Args:
        hostname: The host for which telemetry environment should be setup. This
            is important for devserver resolution.
        build: The build for which telemetry environment should be setup. It is
            typically in the format <board>/<version>.
        """
        self._build = build
        self._ds = dev_server.ImageServer.resolve(self._build,
                                                  hostname=hostname)
        self._setup_dir_path = tempfile.mkdtemp(prefix='telemetry-setupdir_')
        self._tmp_build_dir = os.path.join(self._BASE_DIR_PATH, self._build)
        self._tlm_src_dir_path = os.path.join(self._tmp_build_dir,
                                              self._TELEMETRY_SRC_DIR_NAME)
        # Set once an HTTP request to the devserver fails to connect or times
        # out, so that later downloads go straight to SSH instead of each
        # failing the same way first.
        self._http_unreachable = False
        # The suffixes this drone can extract, most promising first. Filled in
        # by _TarballSuffixes() when the first download is needed.
        self._tarball_suffixes = None

    def Setup(self):
        """Sets up the environment for telemetry testing.

        This method downloads the telemetry dependency tarballs and extracts
        them into a 'src' directory.

        Returns:
        Path to the src directory where the telemetry dependencies have been
            downloaded and extracted.

        Raises:
          TelemetrySetupError if none of the dependencies was found, or if a
              download, an extraction, or the move into 'src' fails.
        """
        src_folder = os.path.join(self._tlm_src_dir_path, self._SRC_DIR_NAME)
        test_src = os.path.join(self._tlm_src_dir_path,
                                self._TEST_SRC_DIR_NAME)
        self._MkDirP(self._tlm_src_dir_path)
        with lock_dir(self._tlm_src_dir_path):
            if not os.path.exists(src_folder):
                # A test_src left by an earlier Setup() that failed part way is
                # incomplete, so start from scratch rather than build on it.
                shutil.rmtree(test_src, ignore_errors=True)
                try:
                    # Download and extract the required dependency tarballs.
                    extracted = 0
                    for dep in self._DEPENDENCIES:
                        if self._DownloadAndExtract(dep):
                            extracted += 1
                    if not extracted:
                        raise TelemetrySetupError(
                                'Failure in telemetry setup for build %s. None '
                                'of the telemetry dependencies were found as '
                                '%s.' %
                                (self._build, self._DescribeTarballSuffixes()))

                    # By default all the tarballs extract to test_src but some
                    # parts of the telemetry code specifically hardcoded to
                    # exist inside of 'src'.
                    try:
                        shutil.move(test_src, src_folder)
                    except (shutil.Error, OSError) as e:
                        raise TelemetrySetupError(
                                'Failure in telemetry setup for build %s. '
                                'Appears that the test_src to src move '
                                'failed: %s' % (self._build, e))
                except Exception:
                    # Cleanup() never runs after a failed Setup(), so do not
                    # leave a partial tree behind.
                    shutil.rmtree(test_src, ignore_errors=True)
                    raise
        return src_folder

    def _TarballSuffixes(self):
        """Returns the compression suffixes to try, most promising first.

        Whether zstd is installed is only checked the first time, so that a
        Setup() that finds 'src' already in place does not check or warn.
        """
        if self._tarball_suffixes is None:
            self._tarball_suffixes = list(self._TARBALL_SUFFIXES)
            if not _has_zstd():
                self._tarball_suffixes.remove(_ZSTD_TARBALL_SUFFIX)
                logging.warning(
                        'zstd is not installed on this drone, so %s telemetry '
                        'dependencies cannot be extracted. Only trying %s.',
                        _ZSTD_TARBALL_SUFFIX,
                        ', '.join(self._tarball_suffixes))
        return self._tarball_suffixes

    def _DescribeTarballSuffixes(self):
        """Describes the suffixes tried, and why zstd was not, for messages."""
        suffixes = self._TarballSuffixes()
        description = ' or '.join(suffixes)
        if _ZSTD_TARBALL_SUFFIX not in suffixes:
            description += (
                    ' (%s was skipped because zstd is not installed on '
                    'this drone)' % _ZSTD_TARBALL_SUFFIX)
        return description

    def _DownloadAndExtract(self, dep_name):
        """Downloads a dependency tarball and extracts it, preferring zstd.

        A build publishes a dependency in one or both compression formats,
        depending on how old the build is, so each candidate is tried in turn
        until one is found. A tarball that is found but fails to extract
        falls back to the next format, so that a broken .tar.zst does not hide
        a good .tar.bz2.

        Args:
          dep_name: Base name of the dependency, without compression suffix.

        Returns:
            True if the dependency was extracted, or False if this build does
            not publish it in any format this drone can extract.

        Raises:
          TelemetrySetupError if a download fails, or if the dependency was
              found but none of its tarballs could be extracted.
        """
        candidates = list(self._TarballSuffixes())
        extract_error = None
        for suffix in candidates:
            dep_path = self._DownloadFilesFromDevserver(
                    dep_name + suffix, self._setup_dir_path)
            if not dep_path:
                continue
            try:
                self._ExtractTarball(dep_path, self._tlm_src_dir_path)
            except TelemetrySetupError as e:
                logging.warning('%s; falling back to the next format, if any.',
                                e)
                extract_error = e
                continue
            finally:
                # Nothing needs the tarball once it has been extracted, and
                # Cleanup() never runs after a failed Setup().
                self._RemoveQuietly(dep_path)
            # The dependencies of a build share their formats, so try this one
            # first for the rest of them.
            self._tarball_suffixes.remove(suffix)
            self._tarball_suffixes.insert(0, suffix)
            return True

        if extract_error:
            raise extract_error
        # Not an error on its own: a dependency can be absent because it is
        # new, because it was dropped, or because this build predates it.
        logging.warning('None of %s were found for build %s.',
                        ', '.join(dep_name + s for s in candidates),
                        self._build)
        return False

    def _DownloadFilesFromDevserver(self, filename, dest_path):
        """Downloads the given tarball from the devserver.

        Args:
          filename: Name of the tarball to be downloaded.
          dest_path: Full path to the directory where it should be downloaded.

        Returns:
            Full path to the downloaded file, or None if the devserver does not
            have the file.

        Raises:
          TelemetrySetupError when the download cannot be completed for any
              reason.
        """
        dep_path = os.path.join(dest_path, filename)
        url = (self._STATIC_URL_TEMPLATE %
               (self._ds.url(), self._build, filename))
        if self._http_unreachable:
            return self._DownloadFilesFromDevserverViaSSH(url, dep_path)
        try:
            # stream=True writes the tarball out as it arrives, rather than
            # buffering all of it in memory first.
            with requests.get(url, timeout=_HTTP_TIMEOUT_SECONDS,
                              stream=True) as resp:
                resp.raise_for_status()
                with open(dep_path, 'wb') as f:
                    for content in resp.iter_content(_READ_BUFFER_SIZE_BYTES):
                        f.write(content)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                # Routine: each dependency is probed once per compression
                # format, and a build need not publish all of them.
                logging.debug('The request %s returned 404 Not Found.', url)
                return None
            raise TelemetrySetupError(
                    'An error occurred while trying to complete %s: %s' %
                    (url, e))
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.Timeout) as e:
            # Covers a connection that fails or times out, a server that sends
            # no headers in time, and a transfer that stalls part way (which
            # requests reports as a ConnectionError) or breaks part way, for
            # example because the devserver restarted.
            logging.warning(
                    'The request %s to the devserver failed: %s. Downloading '
                    'this and any remaining dependencies over SSH instead.',
                    url, e)
            self._http_unreachable = True
            return self._DownloadFilesFromDevserverViaSSH(url, dep_path)
        except Exception as e:
            self._RemoveQuietly(dep_path)
            raise TelemetrySetupError(
                    'An error occurred while trying to complete %s: %s' %
                    (url, e))
        logging.info('Downloaded telemetry dependency %s from %s.', filename,
                     url)
        return dep_path

    def _DownloadFilesFromDevserverViaSSH(self, url, dep_path):
        """Downloads the file at the URL from the devserver by SSH-ing into it.

        Args:
          url: URL of the location of the tarball on the devserver.
          dep_path: Full path to the file where it will be downloaded.

        Returns:
            Full path to the downloaded file, or None if the devserver
            answered but does not have the file.

        Raises:
          TelemetrySetupError when the download cannot be completed for any
              reason.
        """
        # --fail keeps an HTTP error page out of a file that is about to be
        # handed to tar. --silent --show-error leaves only curl's error message
        # on stderr, which is the one place a 404 can be told apart from other
        # HTTP errors. The remote shell parses the command again and the URL
        # embeds the build label, so the URL is quoted. See the _SSH_*
        # constants for how long this can take.
        ssh_options = [
                'BatchMode=yes',
                'ConnectTimeout=%d' % _SSH_CONNECT_TIMEOUT_SECONDS,
                'ServerAliveInterval=%d' % _SSH_SERVER_ALIVE_INTERVAL_SECONDS,
                'ServerAliveCountMax=%d' % _SSH_SERVER_ALIVE_COUNT_MAX,
        ]
        cmd = ['ssh']
        for option in ssh_options:
            cmd += ['-o', option]
        cmd += [
                self._ds.hostname,
                'curl',
                '--fail',
                '--silent',
                '--show-error',
                '--max-time',
                str(_SSH_CURL_MAX_TIME_SECONDS),
                shlex.quote(url),
        ]
        try:
            with open(dep_path, 'wb') as f:
                proc = subprocess.Popen(cmd,
                                        stdin=subprocess.DEVNULL,
                                        stdout=f,
                                        stderr=subprocess.PIPE)
                try:
                    _, err = proc.communicate(
                            timeout=_SSH_LOCAL_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    # Reap ssh without waiting for stderr to close: a child of
                    # ssh, such as a ProxyCommand, may still hold it open.
                    proc.kill()
                    proc.stderr.close()
                    proc.wait()
                    raise
        except (OSError, subprocess.TimeoutExpired) as e:
            self._RemoveQuietly(dep_path)
            raise TelemetrySetupError('Could not run %s: %s' %
                                      (' '.join(cmd), e))
        err = _output_text(err)

        if (proc.returncode == _CURL_HTTP_ERROR_EXIT_CODE
                    and _CURL_HTTP_404_RE.search(err)):
            # Same meaning as a 404 over the regular path: this build does not
            # publish the file.
            logging.debug('The request %s returned 404 Not Found: %s', url,
                          err)
            self._RemoveQuietly(dep_path)
            return None

        if proc.returncode != 0:
            self._RemoveQuietly(dep_path)
            raise TelemetrySetupError(
                    'The command: %s finished with returncode %s and '
                    'errors as following: %s. The telemetry dependency '
                    'could not be downloaded.' %
                    (' '.join(cmd), proc.returncode, err))
        logging.info('Downloaded telemetry dependency %s from %s via SSH.',
                     os.path.basename(dep_path), url)
        return dep_path

    @staticmethod
    def _RemoveQuietly(path):
        """Deletes a file, ignoring failures.

        Args:
          path: Full path to the file to delete.
        """
        try:
            os.remove(path)
        except OSError as e:
            logging.debug('Could not remove %s: %s', path, e)

    def _ExtractTarball(self, tarball_path, dest_path):
        """Extracts the given tarball into the destination directory.

        dest_path is never removed here, even on failure: it holds the lock
        file that Setup() is holding. Setup() removes the partial tree instead.

        Args:
          tarball_path: Full path to the tarball to be extracted.
          dest_path: Full path to the directory where the tarball should be
              extracted.

        Raises:
          TelemetrySetupError if the method is unable to extract the tarball for
              any reason.
        """
        logging.info('Extracting telemetry dependency %s into %s.',
                     os.path.basename(tarball_path), dest_path)
        if tarball_path.endswith(_ZSTD_TARBALL_SUFFIX):
            # Name the decompressor: tar only detects zstd by itself from GNU
            # tar 1.31 on, whereas -I just needs the zstd binary.
            cmd = [
                    'tar', '-I', 'zstd', '-xf', tarball_path, '--directory',
                    dest_path
            ]
        else:
            cmd = ['tar', 'xf', tarball_path, '--directory', dest_path]
        try:
            proc = subprocess.Popen(cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            _, err = proc.communicate()
        except Exception as e:
            raise TelemetrySetupError(
                    'An exception occurred while trying to untar %s into %s: %s'
                    % (tarball_path, dest_path, str(e)))
        if proc.returncode != 0:
            raise TelemetrySetupError(
                    'tar exited with status %d while extracting %s into %s: %s'
                    % (proc.returncode, tarball_path, dest_path,
                       _output_text(err)))

    def _MkDirP(self, path):
        """Recursively creates the given directory.

        Args:
          path: Full path to the directory that needs to the created.

        Raises:
          TelemetrySetupError is the method is unable to create directories for
              any reason except OSError EEXIST which indicates that the
              directory already exists.
        """
        try:
            os.makedirs(path)
        except Exception as e:
            if not isinstance(e, OSError) or e.errno != errno.EEXIST:
                raise TelemetrySetupError(
                        'Could not create directory %s due to %s.' %
                        (path, str(e)))

    def Cleanup(self):
        """Cleans up telemetry setup and work environment."""
        try:
            shutil.rmtree(self._setup_dir_path)
        except Exception as e:
            logging.error('Something went wrong. Could not delete %s: %s',
                          self._setup_dir_path, e)
        try:
            shutil.rmtree(self._tlm_src_dir_path)
        except Exception as e:
            logging.error('Something went wrong. Could not delete %s: %s',
                          self._tlm_src_dir_path, e)
