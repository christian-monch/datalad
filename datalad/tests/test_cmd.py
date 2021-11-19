# emacs: -*- mode: python-mode; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil; coding: utf-8 -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Test WitlessRunner
"""
import sys
import unittest.mock
from subprocess import TimeoutExpired

from datalad.cmd import (
    readline_rstripped,
    BatchedCommand,
)
from datalad.tests.utils import (
    assert_equal,
    assert_is_none,
    assert_is_not_none,
    assert_raises,
    assert_true,
)


def test_readline_rstripped_deprecation():
    with unittest.mock.patch("datalad.cmd.warnings.warn") as warn_mock:
        class StdoutMock:
            def readline(self):
                return "abc\n"
        readline_rstripped(StdoutMock())
        warn_mock.assert_called_once()


def test_batched_command():
    bc = BatchedCommand(cmd=[sys.executable, "-i", "-u", "-q", "-"])
    response = bc("print('a')")
    assert_equal(response, "a")
    response = bc("print(2 + 1)")
    assert_equal(response, "3")
    stderr = bc.close(return_stderr=True)
    assert_is_not_none(stderr)


def test_batched_close_abandon():
    # Expect a timeout if the process runs longer than timeout and the config
    # for "datalad.runtime.stalled-external" is "abandon".
    bc = BatchedCommand(
        cmd=[sys.executable, "-i", "-u", "-q", "-"],
        timeout=.5)
    # Send at least one instruction to start the subprocess
    response = bc("import time; print('a')")
    assert_equal(response, "a")
    bc.stdin_queue.put("time.sleep(2); exit(1)\n".encode())
    with unittest.mock.patch("datalad.cfg") as cfg_mock:
        cfg_mock.configure_mock(**{"obtain.return_value": "abandon"})
        bc.close(return_stderr=False)
        assert_true(bc.wait_timed_out is True)
        assert_is_none(bc.return_code)


def test_batched_close_timeout_exception():
    # Expect a timeout if the process runs longer than timeout and the config
    # for "datalad.runtime.stalled-external" is "abandon".
    bc = BatchedCommand(
        cmd=[sys.executable, "-i", "-u", "-q", "-"],
        timeout=.5,
        exception_on_timeout=True)

    # Send at least one instruction to start the subprocess
    response = bc("import time; print('a')")
    assert_equal(response, "a")
    bc.stdin_queue.put("time.sleep(2); exit(1)\n".encode())
    with unittest.mock.patch("datalad.cfg") as cfg_mock:
        cfg_mock.configure_mock(**{"obtain.return_value": "abandon"})
        assert_raises(TimeoutExpired, bc.close)


def test_batched_close_wait():
    # Expect a long wait and no timeout if the process runs longer than timeout
    # and the config for "datalad.runtime.stalled-external" has its default
    # value.
    bc = BatchedCommand(
        cmd=[sys.executable, "-i", "-u", "-q", "-"],
        timeout=.5)
    # Send at least one instruction to start the subprocess
    response = bc("import time; print('a')")
    assert_equal(response, "a")
    bc.stdin_queue.put("time.sleep(2); exit(2)\n".encode())
    bc.close(return_stderr=False)
    assert_true(bc.wait_timed_out is False)
    assert_equal(bc.return_code, 2)


def test_batched_close_ok():
    # Expect a long wait and no timeout if the process runs longer than timeout
    # seconds and the config for "datalad.runtime.stalled-external" has its
    # default value.
    bc = BatchedCommand(
        cmd=[sys.executable, "-i", "-u", "-q", "-"],
        timeout=2)
    # Send at least one instruction to start the subprocess
    response = bc("import time; print('a')")
    assert_equal(response, "a")
    bc.stdin_queue.put("time.sleep(.5); exit(3)\n".encode())
    bc.close(return_stderr=False)
    assert_true(bc.wait_timed_out is False)
    assert_equal(bc.return_code, 3)
