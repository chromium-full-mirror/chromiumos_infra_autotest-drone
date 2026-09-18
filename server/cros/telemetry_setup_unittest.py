#!/usr/bin/python3
# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Unit tests for server/cros/telemetry_setup.py."""

import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import requests

import common
from autotest_lib.server.cros import telemetry_setup

_BUILD = 'eve-release/R130-16000.0.0'
_DEVSERVER_URL = 'http://devserver:8082'

# stderr of 'curl --fail --silent --show-error' for various HTTP statuses.
_CURL_404 = b'curl: (22) The requested URL returned error: 404'
_CURL_404_OLD = b'curl: (22) The requested URL returned error: 404 Not Found'
_CURL_503 = b'curl: (22) The requested URL returned error: 503'


def _url(filename, build=_BUILD):
    """Returns the devserver URL TelemetrySetup uses for a file."""
    return '%s/static/%s/autotest/packages/%s' % (_DEVSERVER_URL, build,
                                                  filename)


class _FakeDevserver:
    """Stands in for the dev_server.ImageServer that resolve() returns."""
    hostname = 'devserver'

    def url(self):
        """Returns the devserver URL."""
        return _DEVSERVER_URL


class _FailingBody(io.BytesIO):
    """A response body that raises once its data has been read.

    This is how a transfer that stalls or breaks part way looks to requests.
    """

    def __init__(self, data, error):
        super().__init__(data)
        self._error = error

    def read(self, *args):
        data = super().read(*args)
        if not data:
            raise self._error
        return data


def _response(url, status, body=b''):
    """Returns a requests.Response like the one requests.get produces."""
    response = requests.models.Response()
    response.url = url
    response.status_code = status
    response.reason = 'Reason'
    response.raw = body if isinstance(body, io.IOBase) else io.BytesIO(body)
    return response


class _FakeHttp:
    """Stands in for requests.get, serving files by name.

    Each name maps to a response body, to an HTTP status, or to an exception
    to raise. Names that are not listed get a 404.
    """

    def __init__(self, files):
        self.files = files
        self.requested = []

    def __call__(self, url, **kwargs):
        name = url.rsplit('/', 1)[-1]
        self.requested.append(name)
        served = self.files.get(name, 404)
        if isinstance(served, Exception):
            raise served
        if isinstance(served, int):
            return _response(url, served)
        return _response(url, 200, served)


class _FakeSsh:
    """Stands in for subprocess.Popen running 'ssh <devserver> curl <url>'.

    Each name maps to an (exit status, stdout, stderr) tuple. Names that are
    not listed fail the way curl does for a 404.
    """

    def __init__(self, files):
        self.files = files
        self.requested = []
        self.commands = []
        self.processes = []
        self.stdins = []

    def __call__(self, cmd, stdin=None, stdout=None, stderr=None):
        self.commands.append(cmd)
        self.stdins.append(stdin)
        name = cmd[-1].strip("'").rsplit('/', 1)[-1]
        self.requested.append(name)
        status, out, err = self.files.get(name, (22, b'', _CURL_404))
        stdout.write(out)
        process = mock.Mock(returncode=status)
        process.communicate.return_value = (None, err)
        self.processes.append(process)
        return process


class _TestBase(unittest.TestCase):
    """Creates TelemetrySetup objects against a fake devserver."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='telemetry_setup_unittest_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for patcher in (
                mock.patch.object(telemetry_setup.dev_server.ImageServer,
                                  'resolve',
                                  return_value=_FakeDevserver()),
                mock.patch.object(telemetry_setup.TelemetrySetup,
                                  '_BASE_DIR_PATH',
                                  os.path.join(self.tmp, 'workdir')),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make(self, has_zstd=True):
        """Returns a TelemetrySetup, as if zstd was or was not installed."""
        patcher = mock.patch.object(telemetry_setup,
                                    '_has_zstd',
                                    return_value=has_zstd)
        self.has_zstd = patcher.start()
        self.addCleanup(patcher.stop)
        setup = telemetry_setup.TelemetrySetup(hostname='dut', build=_BUILD)
        self.addCleanup(shutil.rmtree, setup._setup_dir_path, True)
        return setup

    def _make_tarball(self, name, compress_args, filename='hello.txt'):
        """Creates a tarball holding test_src/<filename>.

        Args:
          name: File name of the tarball.
          compress_args: tar arguments that select the compression.
          filename: Name of the file inside test_src.

        Returns:
            The contents of the tarball.
        """
        payload = tempfile.mkdtemp(dir=self.tmp)
        os.makedirs(os.path.join(payload, 'test_src'))
        with open(os.path.join(payload, 'test_src', filename), 'w') as f:
            f.write('hello\n')
        path = os.path.join(self.tmp, name)
        subprocess.check_call(['tar'] + compress_args +
                              ['-cf', path, '-C', payload, 'test_src'])
        with open(path, 'rb') as f:
            return f.read()


class DownloadAndExtractTest(_TestBase):
    """Tests choosing between the compression formats of a dependency."""

    def _fetch(self, files, deps=('dep-telemetry_dep', ), has_zstd=True):
        """Fetches deps over HTTP without extracting anything.

        Returns:
            (setup, http, the tarballs that would have been extracted, the
            results of _DownloadAndExtract).
        """
        http = _FakeHttp(files)
        setup = self._make(has_zstd)
        extracted = []

        def fake_extract(tarball, _dest):
            self.assertTrue(os.path.isfile(tarball))
            extracted.append(os.path.basename(tarball))

        with mock.patch.object(telemetry_setup.requests, 'get', http), \
             mock.patch.object(setup, '_ExtractTarball',
                               side_effect=fake_extract):
            results = [setup._DownloadAndExtract(dep) for dep in deps]
        return setup, http, extracted, results

    def test_zstd_only(self):
        """A zstd-only build is satisfied by the first request."""
        setup, http, extracted, results = self._fetch(
                {'dep-telemetry_dep.tar.zst': b'z'})
        self.assertEqual(results, [True])
        self.assertEqual(extracted, ['dep-telemetry_dep.tar.zst'])
        self.assertEqual(http.requested, ['dep-telemetry_dep.tar.zst'])
        # The tarball is deleted once extracted.
        self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_bzip2_only(self):
        """A bzip2-only build falls back to .tar.bz2."""
        _, http, extracted, _ = self._fetch(
                {'dep-telemetry_dep.tar.bz2': b'b'})
        self.assertEqual(extracted, ['dep-telemetry_dep.tar.bz2'])
        self.assertEqual(
                http.requested,
                ['dep-telemetry_dep.tar.zst', 'dep-telemetry_dep.tar.bz2'])

    def test_both_formats_never_requests_bzip2(self):
        """A build that publishes both formats is read as zstd."""
        _, http, extracted, _ = self._fetch({
                'dep-telemetry_dep.tar.zst': b'z',
                'dep-telemetry_dep.tar.bz2': b'b',
        })
        self.assertEqual(extracted, ['dep-telemetry_dep.tar.zst'])
        self.assertEqual(http.requested, ['dep-telemetry_dep.tar.zst'])

    def test_neither_format(self):
        """A dependency published in neither format is skipped."""
        setup, http, extracted, results = self._fetch({})
        self.assertEqual(results, [False])
        self.assertEqual(extracted, [])
        self.assertEqual(
                http.requested,
                ['dep-telemetry_dep.tar.zst', 'dep-telemetry_dep.tar.bz2'])
        self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_later_dependencies_try_the_format_found_first(self):
        """Once a format is found, the next dependency tries it first."""
        _, http, extracted, _ = self._fetch(
                {
                        'dep-telemetry_dep.tar.bz2': b'b',
                        'dep-chrome_test.tar.bz2': b'b',
                },
                deps=('dep-telemetry_dep', 'dep-chrome_test'))
        self.assertEqual(
                extracted,
                ['dep-telemetry_dep.tar.bz2', 'dep-chrome_test.tar.bz2'])
        self.assertEqual(http.requested, [
                'dep-telemetry_dep.tar.zst',
                'dep-telemetry_dep.tar.bz2',
                'dep-chrome_test.tar.bz2',
        ])

    def test_skips_zstd_without_zstd_binary(self):
        """A drone without zstd never asks for a tarball it cannot extract."""
        _, http, extracted, _ = self._fetch(
                {
                        'dep-telemetry_dep.tar.zst': b'z',
                        'dep-telemetry_dep.tar.bz2': b'b',
                },
                has_zstd=False)
        self.assertEqual(extracted, ['dep-telemetry_dep.tar.bz2'])
        self.assertEqual(http.requested, ['dep-telemetry_dep.tar.bz2'])

    @unittest.skipUnless(shutil.which('bzip2'), 'bzip2 is not installed')
    def test_broken_zstd_falls_back_to_bzip2(self):
        """A .tar.zst that fails to extract falls back to the .tar.bz2."""
        telemetry_bz2 = self._make_tarball('d.tar.bz2', ['-j'])
        chrome_bz2 = self._make_tarball('c.tar.bz2', ['-j'],
                                        filename='chrome.txt')
        http = _FakeHttp({
                'dep-telemetry_dep.tar.zst': b'not zstd',
                'dep-telemetry_dep.tar.bz2': telemetry_bz2,
                'dep-chrome_test.tar.zst': b'not zstd',
                'dep-chrome_test.tar.bz2': chrome_bz2,
        })
        setup = self._make()
        # Setup() creates this before downloading anything.
        os.makedirs(setup._tlm_src_dir_path)
        with mock.patch.object(telemetry_setup.requests, 'get', http):
            self.assertTrue(setup._DownloadAndExtract('dep-telemetry_dep'))
            self.assertTrue(setup._DownloadAndExtract('dep-chrome_test'))
        test_src = os.path.join(setup._tlm_src_dir_path, 'test_src')
        self.assertEqual(sorted(os.listdir(test_src)),
                         ['chrome.txt', 'hello.txt'])
        # The second dependency goes straight to the format that worked.
        self.assertEqual(http.requested, [
                'dep-telemetry_dep.tar.zst',
                'dep-telemetry_dep.tar.bz2',
                'dep-chrome_test.tar.bz2',
        ])
        self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_broken_zstd_without_bzip2_raises(self):
        """A dependency that is found but cannot be extracted is fatal."""
        http = _FakeHttp({'dep-telemetry_dep.tar.zst': b'not zstd'})
        setup = self._make()
        with mock.patch.object(telemetry_setup.requests, 'get', http):
            with self.assertRaises(telemetry_setup.TelemetrySetupError):
                setup._DownloadAndExtract('dep-telemetry_dep')
        self.assertEqual(os.listdir(setup._setup_dir_path), [])


class HttpDownloadTest(_TestBase):
    """Tests downloading over HTTP."""

    def _download(self, get, filename='x.tar.zst', ssh=None):
        """Downloads filename with requests.get and ssh replaced."""
        setup = self._make()
        with mock.patch.object(telemetry_setup.requests, 'get', get), \
             mock.patch.object(telemetry_setup.subprocess, 'Popen',
                               ssh or _FakeSsh({})):
            path = setup._DownloadFilesFromDevserver(filename,
                                                     setup._setup_dir_path)
        return path, setup

    def test_success_writes_the_body_as_bytes(self):
        """The body is written byte for byte, with a timeout, streaming."""
        body = b'\x28\xb5\x2f\xfd\xff\x00' * 1000  # Not valid UTF-8.
        get = mock.Mock(side_effect=_FakeHttp({'x.tar.zst': body}))
        path, setup = self._download(get)
        self.assertEqual(path, os.path.join(setup._setup_dir_path,
                                            'x.tar.zst'))
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), body)
        get.assert_called_once_with(
                _url('x.tar.zst'),
                timeout=telemetry_setup._HTTP_TIMEOUT_SECONDS,
                stream=True)

    def test_404_returns_none_and_leaves_no_file(self):
        """A 404 means the build does not publish the file."""
        path, setup = self._download(_FakeHttp({}))
        self.assertIsNone(path)
        self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_500_raises(self):
        """Any other HTTP error is fatal."""
        with self.assertRaises(telemetry_setup.TelemetrySetupError):
            self._download(_FakeHttp({'x.tar.zst': 500}))

    def test_broken_transfer_is_replaced_over_ssh(self):
        """A transfer that breaks part way is retried over SSH."""
        body = _FailingBody(b'partial',
                            requests.exceptions.ChunkedEncodingError('reset'))
        ssh = _FakeSsh({'x.tar.zst': (0, b'complete', b'')})
        path, setup = self._download(_FakeHttp({'x.tar.zst': body}), ssh=ssh)
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), b'complete')
        self.assertTrue(setup._http_unreachable)

    def test_other_error_raises_and_removes_file(self):
        """Any other failure part way is fatal and leaves no file behind."""
        body = _FailingBody(b'partial',
                            requests.exceptions.ContentDecodingError('bad'))
        setup = self._make()
        with mock.patch.object(telemetry_setup.requests, 'get',
                               _FakeHttp({'x.tar.zst': body})):
            with self.assertRaises(telemetry_setup.TelemetrySetupError):
                setup._DownloadFilesFromDevserver('x.tar.zst',
                                                  setup._setup_dir_path)
        self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_read_timeout_falls_back_to_ssh(self):
        """A devserver that sends no headers in time is retried over SSH."""
        get = mock.Mock(side_effect=requests.exceptions.ReadTimeout('slow'))
        ssh = _FakeSsh({'x.tar.zst': (0, b'payload', b'')})
        path, setup = self._download(get, ssh=ssh)
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), b'payload')
        self.assertEqual(ssh.requested, ['x.tar.zst'])
        self.assertTrue(setup._http_unreachable)

    def test_stalled_transfer_is_replaced_over_ssh(self):
        """The SSH download replaces what a stalled transfer left behind."""
        body = _FailingBody(
                b'partial',
                requests.exceptions.ConnectionError('Read timed out.'))
        ssh = _FakeSsh({'x.tar.zst': (0, b'complete', b'')})
        path, _ = self._download(_FakeHttp({'x.tar.zst': body}), ssh=ssh)
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), b'complete')

    def test_connection_error_falls_back_to_ssh_once(self):
        """After one failed connection, everything else goes over SSH."""
        get = mock.Mock(side_effect=requests.exceptions.ConnectionError('no'))
        ssh = _FakeSsh({
                'dep-telemetry_dep.tar.bz2': (0, b'b', b''),
                'dep-chrome_test.tar.bz2': (0, b'b', b''),
        })
        setup = self._make()
        with mock.patch.object(telemetry_setup.requests, 'get', get), \
             mock.patch.object(telemetry_setup.subprocess, 'Popen', ssh), \
             mock.patch.object(setup, '_ExtractTarball'):
            self.assertTrue(setup._DownloadAndExtract('dep-telemetry_dep'))
            self.assertTrue(setup._DownloadAndExtract('dep-chrome_test'))
        self.assertEqual(get.call_count, 1)
        self.assertEqual(ssh.requested, [
                'dep-telemetry_dep.tar.zst',
                'dep-telemetry_dep.tar.bz2',
                'dep-chrome_test.tar.bz2',
        ])


class SshDownloadTest(_TestBase):
    """Tests downloading by running curl on the devserver over SSH."""

    def _download(self, result, url=None):
        """Downloads x.tar.zst over SSH, where curl ends with result."""
        ssh = _FakeSsh({'x.tar.zst': result})
        setup = self._make()
        dep_path = os.path.join(setup._setup_dir_path, 'x.tar.zst')
        with mock.patch.object(telemetry_setup.subprocess, 'Popen', ssh):
            path = setup._DownloadFilesFromDevserverViaSSH(
                    url or _url('x.tar.zst'), dep_path)
        return path, setup, ssh

    def test_success_returns_path(self):
        """A successful download returns the downloaded file."""
        path, setup, ssh = self._download((0, b'payload', b''))
        self.assertEqual(path, os.path.join(setup._setup_dir_path,
                                            'x.tar.zst'))
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), b'payload')
        self.assertEqual(ssh.commands[0][:-1], [
                'ssh',
                '-o',
                'BatchMode=yes',
                '-o',
                'ConnectTimeout=%d' %
                telemetry_setup._SSH_CONNECT_TIMEOUT_SECONDS,
                '-o',
                'ServerAliveInterval=%d' %
                telemetry_setup._SSH_SERVER_ALIVE_INTERVAL_SECONDS,
                '-o',
                'ServerAliveCountMax=%d' %
                telemetry_setup._SSH_SERVER_ALIVE_COUNT_MAX,
                'devserver',
                'curl',
                '--fail',
                '--silent',
                '--show-error',
                '--max-time',
                str(telemetry_setup._SSH_CURL_MAX_TIME_SECONDS),
        ])
        # ssh must not read the test's stdin, and is bounded locally too.
        self.assertEqual(ssh.stdins, [subprocess.DEVNULL])
        ssh.processes[0].communicate.assert_called_once_with(
                timeout=telemetry_setup._SSH_LOCAL_TIMEOUT_SECONDS)

    def test_local_timeout_kills_ssh(self):
        """An ssh that hangs past every remote bound is killed locally."""
        bin_dir = os.path.join(self.tmp, 'bin')
        os.makedirs(bin_dir)
        fake_ssh = os.path.join(bin_dir, 'ssh')
        with open(fake_ssh, 'w') as f:
            # Like a remote login that never gets as far as running curl. The
            # sleep is a child that keeps stderr open after ssh is killed.
            f.write('#!/bin/sh\nsleep 10\n')
        os.chmod(fake_ssh, 0o755)
        setup = self._make()
        path = os.environ['PATH']
        with mock.patch.dict(os.environ,
                             {'PATH': bin_dir + os.pathsep + path}), \
             mock.patch.object(telemetry_setup, '_SSH_LOCAL_TIMEOUT_SECONDS',
                               0.5):
            start = time.monotonic()
            with self.assertRaisesRegex(telemetry_setup.TelemetrySetupError,
                                        'timed out'):
                setup._DownloadFilesFromDevserverViaSSH(
                        _url('x.tar.zst'),
                        os.path.join(setup._setup_dir_path, 'x.tar.zst'))
        # Well under the 10 s the child keeps stderr open for.
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_ssh_that_cannot_start_raises_and_removes_file(self):
        """A missing ssh binary is a TelemetrySetupError, not an OSError."""
        setup = self._make()
        with mock.patch.object(telemetry_setup.subprocess,
                               'Popen',
                               side_effect=FileNotFoundError('ssh')):
            with self.assertRaises(telemetry_setup.TelemetrySetupError):
                setup._DownloadFilesFromDevserverViaSSH(
                        _url('x.tar.zst'),
                        os.path.join(setup._setup_dir_path, 'x.tar.zst'))
        self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_404_returns_none_and_removes_file(self):
        """A 404, in either curl's old or new wording, means unpublished."""
        for err in (_CURL_404, _CURL_404_OLD):
            with self.subTest(err=err):
                path, setup, _ = self._download((22, b'partial', err))
                self.assertIsNone(path)
                self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_other_http_error_raises_and_removes_file(self):
        """An HTTP error other than 404 is fatal, though curl also exits 22."""
        setup = self._make()
        ssh = _FakeSsh({'x.tar.zst': (22, b'partial', _CURL_503)})
        with mock.patch.object(telemetry_setup.subprocess, 'Popen', ssh):
            with self.assertRaises(telemetry_setup.TelemetrySetupError):
                setup._DownloadFilesFromDevserverViaSSH(
                        _url('x.tar.zst'),
                        os.path.join(setup._setup_dir_path, 'x.tar.zst'))
        self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_ssh_failure_raises_and_removes_file(self):
        """A failure of SSH itself is fatal."""
        setup = self._make()
        ssh = _FakeSsh({'x.tar.zst': (255, b'partial', b'Connection refused')})
        with mock.patch.object(telemetry_setup.subprocess, 'Popen', ssh):
            with self.assertRaises(telemetry_setup.TelemetrySetupError):
                setup._DownloadFilesFromDevserverViaSSH(
                        _url('x.tar.zst'),
                        os.path.join(setup._setup_dir_path, 'x.tar.zst'))
        self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_url_is_quoted_for_the_remote_shell(self):
        """The remote shell sees the URL as a single, inert word."""
        canary = os.path.join(self.tmp, 'injected')
        url = _url('x.tar.zst', build='eve-release/R1;touch %s;' % canary)
        _, _, ssh = self._download((0, b'payload', b''), url=url)
        cmd = ssh.commands[0]
        # ssh joins everything after the host into one string, which sshd
        # hands to the remote shell; model that with sh -c, where curl only
        # prints its arguments.
        remote = ' '.join(cmd[cmd.index('curl'):])
        argv = subprocess.check_output(
                ['sh', '-c', 'curl() { printf "%s\\n" "$@"; }; ' + remote],
                text=True).splitlines()
        self.assertEqual(argv[-1], url)
        self.assertFalse(os.path.exists(canary))


class ExtractTarballTest(_TestBase):
    """Tests extracting a downloaded tarball."""

    def _extract(self, name, contents):
        """Extracts a tarball into a new directory and returns the directory."""
        tarball = os.path.join(self.tmp, 'download-' + name)
        with open(tarball, 'wb') as f:
            f.write(contents)
        dest = tempfile.mkdtemp(dir=self.tmp)
        self._make()._ExtractTarball(tarball, dest)
        return dest

    @unittest.skipUnless(shutil.which('zstd'), 'zstd is not installed')
    def test_zstd_round_trip(self):
        """A .tar.zst tarball is extracted."""
        dest = self._extract('d.tar.zst',
                             self._make_tarball('d.tar.zst', ['-I', 'zstd']))
        self.assertTrue(
                os.path.isfile(os.path.join(dest, 'test_src', 'hello.txt')))

    @unittest.skipUnless(shutil.which('bzip2'), 'bzip2 is not installed')
    def test_bzip2_round_trip(self):
        """A .tar.bz2 tarball is extracted."""
        dest = self._extract('d.tar.bz2',
                             self._make_tarball('d.tar.bz2', ['-j']))
        self.assertTrue(
                os.path.isfile(os.path.join(dest, 'test_src', 'hello.txt')))

    def test_tar_failure_raises_and_keeps_destination(self):
        """A failing tar raises, and leaves the locked directory alone."""
        tarball = os.path.join(self.tmp, 'd.tar.bz2')
        with open(tarball, 'wb') as f:
            f.write(b'not a tarball')
        dest = tempfile.mkdtemp(dir=self.tmp)
        with self.assertRaises(telemetry_setup.TelemetrySetupError):
            self._make()._ExtractTarball(tarball, dest)
        self.assertTrue(os.path.isdir(dest))

    def test_popen_failure_keeps_destination(self):
        """A tar that cannot even start leaves the locked directory alone."""
        tarball = os.path.join(self.tmp, 'd.tar.bz2')
        dest = tempfile.mkdtemp(dir=self.tmp)
        open(os.path.join(dest, '.lock'), 'w').close()
        setup = self._make()
        with mock.patch.object(telemetry_setup.subprocess,
                               'Popen',
                               side_effect=OSError('no tar')):
            with self.assertRaises(telemetry_setup.TelemetrySetupError):
                setup._ExtractTarball(tarball, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, '.lock')))

    @unittest.skipUnless(shutil.which('zstd'), 'zstd is not installed')
    def test_zstd_without_zstd_binary_raises(self):
        """Without zstd on PATH, extracting .tar.zst fails loudly."""
        contents = self._make_tarball('d.tar.zst', ['-I', 'zstd'])
        bin_dir = os.path.join(self.tmp, 'bin')
        os.makedirs(bin_dir)
        os.symlink(shutil.which('tar'), os.path.join(bin_dir, 'tar'))
        with mock.patch.dict(os.environ, {'PATH': bin_dir}):
            with self.assertRaises(telemetry_setup.TelemetrySetupError):
                self._extract('d.tar.zst', contents)


class SetupTest(_TestBase):
    """Tests the whole download and extraction."""

    def _setup(self, files, has_zstd=True):
        """Runs Setup() against a devserver that serves files over HTTP."""
        setup = self._make(has_zstd)
        with mock.patch.object(telemetry_setup.requests, 'get',
                               _FakeHttp(files)):
            return setup, setup.Setup()

    def _test_src(self, setup):
        """Returns the path of the test_src directory of setup."""
        return os.path.join(setup._tlm_src_dir_path, 'test_src')

    def test_raises_when_no_dependency_was_found(self):
        """A build without any dependency fails with a TelemetrySetupError."""
        with self.assertRaisesRegex(telemetry_setup.TelemetrySetupError,
                                    r'found as \.tar\.zst or \.tar\.bz2\.$'):
            self._setup({})

    def test_no_zstd_error_says_why(self):
        """The error for a zstd-only build on a zstd-less drone says why."""
        with self.assertRaisesRegex(
                telemetry_setup.TelemetrySetupError,
                r'found as \.tar\.bz2 \(\.tar\.zst was skipped because zstd '
                r'is not installed on this drone\)'):
            self._setup({'dep-telemetry_dep.tar.zst': b'z'}, has_zstd=False)

    def test_leftover_test_src_is_not_promoted(self):
        """A partial test_src from a failed run does not count as found."""
        setup = self._make()
        os.makedirs(os.path.join(self._test_src(setup), 'stale'))
        with mock.patch.object(telemetry_setup.requests, 'get', _FakeHttp({})):
            with self.assertRaises(telemetry_setup.TelemetrySetupError):
                setup.Setup()
        self.assertFalse(os.path.exists(self._test_src(setup)))
        self.assertFalse(
                os.path.exists(os.path.join(setup._tlm_src_dir_path, 'src')))

    @unittest.skipUnless(shutil.which('bzip2'), 'bzip2 is not installed')
    def test_failure_removes_partial_test_src(self):
        """A Setup() that fails part way leaves no partial tree behind."""
        setup = self._make()
        # The first dependency extracts; a later one fails with a 500.
        tarball = self._make_tarball('d.tar.bz2', ['-j'])
        http = _FakeHttp({
                'dep-telemetry_dep.tar.bz2': tarball,
                'dep-chrome_test.tar.zst': 500,
        })
        with mock.patch.object(telemetry_setup.requests, 'get', http):
            with self.assertRaises(telemetry_setup.TelemetrySetupError):
                setup.Setup()
        self.assertFalse(os.path.exists(self._test_src(setup)))

    @unittest.skipUnless(shutil.which('bzip2'), 'bzip2 is not installed')
    def test_move_failure_raises(self):
        """A failure to move test_src into src is a TelemetrySetupError."""
        setup = self._make()
        tarball = self._make_tarball('d.tar.bz2', ['-j'])
        http = _FakeHttp({'dep-telemetry_dep.tar.bz2': tarball})
        with mock.patch.object(telemetry_setup.requests, 'get', http), \
             mock.patch.object(telemetry_setup.shutil, 'move',
                               side_effect=OSError('read-only')):
            with self.assertRaises(telemetry_setup.TelemetrySetupError):
                setup.Setup()
        self.assertFalse(os.path.exists(self._test_src(setup)))

    @unittest.skipUnless(shutil.which('zstd'), 'zstd is not installed')
    def test_extracts_into_src(self):
        """The dependencies end up under src/, and the tarballs are gone."""
        tarball = self._make_tarball('d.tar.zst', ['-I', 'zstd'])
        setup, src = self._setup({'dep-telemetry_dep.tar.zst': tarball})
        self.assertTrue(os.path.isfile(os.path.join(src, 'hello.txt')))
        self.assertEqual(os.listdir(setup._setup_dir_path), [])

    def test_existing_src_needs_no_download(self):
        """With src already in place, nothing is downloaded or checked."""
        setup = self._make()
        os.makedirs(os.path.join(setup._tlm_src_dir_path, 'src'))
        get = mock.Mock()
        with mock.patch.object(telemetry_setup.requests, 'get', get):
            setup.Setup()
        get.assert_not_called()
        self.has_zstd.assert_not_called()


if __name__ == '__main__':
    unittest.main()
