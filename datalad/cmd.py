# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""
Wrapper for command and function calls, allowing for dry runs and output handling

"""

import logging
import os
import queue
import subprocess
import sys
import tempfile
import warnings
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Tuple,
    Union,
)

# start of legacy import block
# to avoid breakage of code written before datalad.runner
from datalad.runner.coreprotocols import (
    KillOutput,
    NoCapture,
    StdErrCapture,
    StdOutCapture,
    StdOutErrCapture,
)
from datalad.runner.gitrunner import (
    GIT_SSH_COMMAND,
    GitRunnerBase,
    GitWitlessRunner,
)
from datalad.runner.runner import WitlessRunner
from datalad.runner.protocol import WitlessProtocol
from datalad.runner.nonasyncrunner import run_command
from datalad.support.exceptions import CommandError
# end of legacy import block

from datalad.runner.coreprotocols import StdOutErrCapture
from datalad.runner.nonasyncrunner import (
    STDERR_FILENO,
    STDIN_FILENO,
    STDOUT_FILENO,
)
from datalad.runner.protocol import GeneratorMixIn
from datalad.runner.runner import WitlessRunner
from datalad.runner.utils import LineSplitter
from datalad.utils import (
    auto_repr,
    ensure_unicode,
    try_multiple,
    unlink,
)


lgr = logging.getLogger('datalad.cmd')

# TODO unused?
# In python3 to split byte stream on newline, it must be bytes
linesep_bytes = os.linesep.encode()

# TODO unused?
_TEMP_std = sys.stdout, sys.stderr

# TODO unused?
# To be used in the temp file name to distinguish the ones we create
# in Runner so we take care about their removal, in contrast to those
# which might be created outside and passed into Runner
_MAGICAL_OUTPUT_MARKER = "_runneroutput_"


def readline_rstripped(line: str):
    warnings.warn("the function `readline_rstripped()` is deprecated "
                  "and will be removed in a future release",
                  DeprecationWarning)
    return _readline_rstripped(line)


def _readline_rstripped(line):
    """Internal helper for BatchedCommand"""
    return line.rstrip()


class BatchedCommandProtocol(GeneratorMixIn, StdOutErrCapture):
    def __init__(self,
                 done_future: Any = None,
                 encoding: Optional[str] = None,
                 output_proc: Callable = None,
                 ):
        GeneratorMixIn.__init__(self)
        StdOutErrCapture.__init__(self, done_future, encoding)
        self.output_proc = output_proc
        self.line_splitter = LineSplitter()

    def _process_line(self, fd: int, line: str):
        if self.output_proc:
            result = self.output_proc(line)
            if result is not None:
                self.send_result((fd, result))
        else:
            self.send_result((fd, line.rstrip()))

    def pipe_data_received(self, fd: int, data: bytes):
        if fd == STDERR_FILENO:
            self.send_result((fd, data))
        elif fd == STDOUT_FILENO:
            for line in self.line_splitter.process(data.decode(self.encoding)):
                self._process_line(fd, line)
        else:
            raise ValueError(f"unknown file descriptor: {fd}")

    def pipe_connection_lost(self, fd: int, exc: Optional[Exception]):
        if fd == STDOUT_FILENO:
            remaining_line = self.line_splitter.finish_processing()
            if remaining_line is not None:
                lgr.warning(f"unterminated line: {remaining_line}")
                self._process_line(fd, remaining_line)


class SafeDelCloseMixin(object):
    """A helper class to use where __del__ would call .close() which might
    fail if "too late in GC game"
    """
    def __del__(self):
        try:
            self.close()
        except TypeError:
            if os.fdopen is None or lgr.debug is None:
                # if we are late in the game and things already gc'ed in py3,
                # it is Ok
                return
            raise


@auto_repr
class BatchedCommand(SafeDelCloseMixin):
    """
    Container for a running subprocess. Supports communication with the
    subprocess via stdin and stdout.
    """

    def __init__(self,
                 cmd: Union[str, Tuple, List],
                 path: Optional[str] = None,
                 output_proc: Callable = None
                 ):

        command = cmd
        self.command = [command] if not isinstance(command, List) else command
        self.path = path
        self.output_proc = output_proc
        self.stdin_queue = None
        self.stderr_output = b""
        self.runner = None
        self.generator = None
        self.encoding = None

    def _initialize(self):

        lgr.debug("Starting new runner for %s", repr(self))
        lgr.log(5, "Command: %s", self.command)

        self.stdin_queue = queue.Queue()
        self.runner = WitlessRunner(
            cwd=self.path,
            env=GitRunnerBase.get_git_environ_adjusted()
        )
        self.generator = self.runner.run(
            cmd=self.command,
            protocol=BatchedCommandProtocol,
            stdin=self.stdin_queue,
            cwd=self.path,
            env=GitRunnerBase.get_git_environ_adjusted(),
            output_proc=self.output_proc,
        )
        self.encoding = self.generator.runner.protocol.encoding

    def _check_process(self, restart=False):
        """Check if the process was terminated and restart if restart

        Returns
        -------
        bool
          True if process was alive.
        str
          stderr if any recorded if was terminated
        """
        process = self._process
        ret = True
        ret_stderr = None
        if process and process.poll():
            lgr.warning("Process %s was terminated with returncode %s" % (process, process.returncode))
            ret_stderr = self.close(return_stderr=True)
            ret = False
        if self._process is None and restart:
            lgr.warning("Restarting the process due to previous failure")
            self._initialize()
        return ret, ret_stderr

    def __call__(self,
                 cmds: Union[str, Tuple, List]):
        """

        Parameters
        ----------
        cmds : str or tuple or list of (str or tuple)
            instructions for the subprocess

        Returns
        -------
        str or list
          Output received from process.  list in case if cmds was a list
        """
        instructions = cmds
        if not self.runner:
            self._initialize()

        input_multiple = isinstance(instructions, list)
        if not input_multiple:
            instructions = [instructions]

        output = []
        try:
            # This code assumes that each processing instruction is
            # a single line and leads to a response that triggers a
            # `send_result` in the protocol.
            for instruction in instructions:

                # Send instruction to subprocess
                if not isinstance(instruction, str):
                    instruction = ' '.join(instruction)
                self.stdin_queue.put((instruction + "\n").encode())

                # Get the response from the generator. The protocol is
                # responsible for sending a response on stdout
                # (and not blocking us)
                while True:
                    source, data = self.generator.send(None)
                    if source == STDERR_FILENO:
                        self.stderr_output += data
                    elif source == STDOUT_FILENO:
                        output.append(data)
                        break
                    else:
                        raise ValueError(f"{self}: unknown source: {source}")

        except CommandError as command_error:
            lgr.error(f"{self}: command error: {command_error}")
            self.runner = None

        except StopIteration:
            self.runner = None

        return output if input_multiple else output[0] if output else None

    def close(self, return_stderr=False):
        """
        Close communication and wait for process to terminate

        Returns
        -------
        str, optional
          stderr output if return_stderr and stderr file was there.
          None otherwise
        """

        if self.runner:

            # Request closing of stdin by enqueueing None
            self.stdin_queue.put(None)

            # Process all remaining messages until the subprocess exits.
            remaining = []
            try:
                remaining.extend(self.generator)
            except CommandError as command_error:
                lgr.error(f"{self} subprocess failed with {command_error}")
            if remaining:
                lgr.warning(f"{self}: remaining content: {remaining}")
            self.runner = None

        return self.get_requested_error_output(return_stderr)

    def get_requested_error_output(self, return_stderr: bool):
        if not self.runner:
            return None

        stderr_content = ensure_unicode(self.stderr_output)
        if lgr.isEnabledFor(5):
            from . import cfg
            if cfg.getbool('datalad.log', 'outputs', default=False):
                stderr_lines = stderr_content.splitlines()
                lgr.log(
                    5,
                    "stderr of %s had %d lines:",
                    self.generator.runner.process.pid,
                    len(stderr_lines))
                for line in stderr_lines:
                    lgr.log(5, "| " + line)
        if return_stderr:
            return stderr_content
        return None

        ret = None
        process = self._process
        if self._stderr_out:
            # close possibly still open fd
            lgr.debug(
                "Closing stderr of %s", process)
            os.fdopen(self._stderr_out).close()
            self._stderr_out = None
        if process:
            lgr.debug(
                "Closing stdin of %s and waiting process to finish", process)
            process.stdin.close()
            process.stdout.close()
            from . import cfg
            cfg_var = 'datalad.runtime.stalled-external'
            cfg_val = cfg.obtain(cfg_var)
            if cfg_val == 'wait':
                process.wait()
            elif cfg_val == 'abandon':
                # try waiting for the annex process to finish 3 times for 3 sec
                # with 1s pause in between
                try:
                    try_multiple(
                        # ntrials
                        3,
                        # exception to catch
                        subprocess.TimeoutExpired,
                        # base waiting period
                        1.0,
                        # function to run
                        process.wait,
                        timeout=3.0,
                    )
                except subprocess.TimeoutExpired:
                    lgr.warning(
                        "Batched process %s did not finish, abandoning it without killing it",
                        process)
            else:
                raise ValueError(f"Unexpected {cfg_var}={cfg_val!r}")
            self._process = None
            lgr.debug("Process %s has finished", process)
