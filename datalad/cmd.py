# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""
Class the starts a subprocess and keeps it around to communicate with it
via stdin. For each instruction send over stdin, a response is read and
returned. The response structure is determined by "output_proc"

"""

import logging
import os
import queue
import sys
import warnings
from subprocess import TimeoutExpired
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
    STDOUT_FILENO,
)
from datalad.runner.protocol import GeneratorMixIn
from datalad.runner.runner import WitlessRunner
from datalad.runner.utils import LineSplitter
from datalad.utils import (
    auto_repr,
    ensure_unicode,
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


def readline_rstripped(stdout):
    warnings.warn("the function `readline_rstripped()` is deprecated "
                  "and will be removed in a future release",
                  DeprecationWarning)
    return _readline_rstripped(stdout)


def _readline_rstripped(stdout):
    """Internal helper for BatchedCommand"""
    return stdout.readline().rstrip()


class BatchedCommandProtocol(GeneratorMixIn, StdOutErrCapture):
    def __init__(self,
                 batched_command: "BatchedCommand",
                 done_future: Any = None,
                 encoding: Optional[str] = None,
                 output_proc: Callable = None,
                 ):
        GeneratorMixIn.__init__(self)
        StdOutErrCapture.__init__(self, done_future, encoding)
        self.batched_command = batched_command
        self.output_proc = output_proc
        self.line_splitter = LineSplitter()

    def pipe_data_received(self, fd: int, data: bytes):
        if fd == STDERR_FILENO:
            self.send_result((fd, data))
        elif fd == STDOUT_FILENO:
            for line in self.line_splitter.process(data.decode(self.encoding)):
                self.send_result((fd, line))
        else:
            raise ValueError(f"unknown file descriptor: {fd}")

    def pipe_connection_lost(self, fd: int, exc: Optional[Exception]):
        if fd == STDOUT_FILENO:
            remaining_line = self.line_splitter.finish_processing()
            if remaining_line is not None:
                lgr.warning(f"unterminated line: {remaining_line}")
                self.send_result((fd, remaining_line))

    def timeout(self, fd: Optional[int]) -> bool:
        timeout_error = self.batched_command.get_timeout_exception(fd)
        if timeout_error:
            raise timeout_error
        self.send_result(("timeout", fd))
        return False


class ReadlineEmulator:
    """
    This class implements readline() on the basis of an instance of
    BatchedCommand. Its purpose is to emulate stdout's for output_procs,
    This allows us to provide a BatchedCommand API that is identical
    to the old version, but with an implementation that is based on the
    threaded runner.
    """
    def __init__(self,
                 batched_command: "BatchedCommand"):
        self.batched_command = batched_command

    def readline(self):
        """
        Read from the stdout provider until we have a line or None (which
        indicates some error).
        """
        return self.batched_command.get_one_line()


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
                 output_proc: Callable = None,
                 timeout: Optional[float] = None,
                 exception_on_timeout: bool = False,
                 ):

        command = cmd
        self.command = [command] if not isinstance(command, List) else command
        self.path = path
        self.output_proc = output_proc
        self.timeout = timeout
        self.exception_on_timeout = exception_on_timeout

        self.stdin_queue = None
        self.stderr_output = b""
        self.runner = None
        self.generator = None
        self.encoding = None
        self.wait_timed_out = None
        self.return_code = None
        self._abandon_cache = None

    def _initialize(self):

        lgr.debug("Starting new runner for %s", repr(self))
        lgr.log(5, "Command: %s", self.command)

        self.stdin_queue = queue.Queue()
        self.stderr_output = b""
        self.wait_timed_out = None
        self.return_code = None

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
            # This mimics the behavior of the old implementation w.r.t
            # timeouts when waiting for the closing process
            timeout=self.timeout or 11.0,
            # Keyword arguments for the protocol
            batched_command=self,
            output_proc=self.output_proc,
        )
        self.encoding = self.generator.runner.protocol.encoding

    def __call__(self,
                 cmds: Union[str, Tuple, List]):
        """
        Send commands to the subprocess and return the results. We expect one
        result per command, how the result is structured is determined by
        output_proc. If output_proc returns not-None, the output is
        considered to be a result.

        If the subprocess does not exist yet it is started before the first
        command is sent.

        TODO: If the subprocess exits between commands, it is restarted.

        Parameters
        ----------
        cmds : str or tuple or list of (str or tuple)
            instructions for the subprocess

        Returns
        -------
        str or list
            Output received from process. Either a string, or a list of strings,
            if cmds was a list.
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

                # Get the response from the generator. We only consider
                # data received on stdout as a response.
                if self.output_proc:
                    # If we have an output procedure, let the output procedure
                    # decide about the nature of the response
                    result = self.output_proc(ReadlineEmulator(self))
                else:
                    result = self.get_one_line()
                    if result is not None:
                        result = result.rstrip()

                output.append(result)

        except CommandError as command_error:
            lgr.error(f"{self}: command error: {command_error}")
            self.runner = None

        except StopIteration:
            self.runner = None

        return output if input_multiple else output[0] if output else None

    def proc1(self,
              single_command: str):
        """
        Simulate the old interface. This method is used only once in
        AnnexRepo.get_metadata()
        """
        assert isinstance(single_command, str)
        return self(single_command)

    def get_one_line(self) -> Optional[str]:
        """
        Get a single stdout line from the generator.

        If timeout was specified, and exception_on_timeout is False,
        and if a timeout occurs, return None. Otherwise return the
        str that was read from the generator.
        """

        # Implementation remarks:
        # 1. We known that BatchedCommandProtocol only returns complete lines on
        #    stdout, that makes this code simple.
        # 2. stderr is handled transparently within this method,
        #    by adding all stderr-content to an internal buffer.
        while True:
            source, data = self.generator.send(None)
            if source == STDERR_FILENO:
                self.stderr_output += data
            elif source == STDOUT_FILENO:
                return data
            elif source == "timeout":
                # TODO: we should restart the subprocess on timeout, otherwise
                #  we might end up with results from a previous instruction,
                #  when handling multiple instructions at once. Until this is
                #  done properly, communication timeouts are ignored in order
                #  to avoid errors.
                pass
            else:
                raise ValueError(f"{self}: unknown source: {source}")

    def close(self, return_stderr=False):
        """
        Close communication and wait for process to terminate. If the "timeout"
        parameter to the constructor was not None, and if the configuration
        setting "datalad.runtime.stalled-external" is set to "abandon",
        the method will return latest after "timeout" seconds. If the subprocess
        did not exit within this time, the attribute "wait_timed_out" will
        be set to "True".

        Parameters
        ----------
        return_stderr: bool
          if set to "True", the call will return all collected stderr content
          as string. In addition, if return_stderr is True and the log level
          is 5 or lower, and the configuration setting "datalad.log.outputs"
          evaluates to "True", the content of stderr will be logged.

        Returns
        -------
        str, optional
          stderr output if return_stderr is True, None otherwise
        """

        if self.runner:

            abandon = self._get_abandon()

            # Close stdin to let the process know that we want to end
            # communication. We also close stdout and stderr to inform
            # the generator that we do not care about them anymore, because
            # otherwise the generator would wait for output from them.
            # (The threads would create timeouts, but at this point
            # returning them in the generator does not work (TODO: why?)
            self.generator.runner.close_process_stdin_stdout_stderr()

            # Process all remaining messages until the subprocess exits.
            remaining = []
            timeout = False
            try:
                for source, data in self.generator:
                    if source == STDERR_FILENO:
                        self.stderr_output += data
                    elif source == STDOUT_FILENO:
                        remaining.append(data)
                    elif source == "timeout":
                        if data is None and abandon is True:
                            timeout = True
                            break
                    else:
                        raise ValueError(f"{self}: unknown source: {source}")
                self.return_code = self.generator.return_code

            except CommandError as command_error:
                lgr.error(f"{self} subprocess failed with {command_error}")
                self.return_code = command_error.code

            if remaining:
                lgr.warning(f"{self}: remaining content: {remaining}")

            self.wait_timed_out = timeout is True
            if self.wait_timed_out:
                lgr.debug(
                    f"{self}: timeout while waiting for subprocess to exit")
                lgr.warning(
                    f"Batched process ({self.generator.runner.process.pid}) "
                    f"did not finish, abandoning it without killing it")

        result = self.get_requested_error_output(return_stderr)
        self.runner = None
        self.stderr_output = b""
        return result

    def get_requested_error_output(self, return_stderr: bool):
        if not self.runner:
            return None

        stderr_content = ensure_unicode(self.stderr_output)
        if lgr.isEnabledFor(5):
            from . import cfg
            if cfg.getbool("datalad.log", "outputs", default=False):
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

    def get_timeout_exception(self,
                              fd: Optional[int]
                              ) -> Optional[TimeoutExpired]:
        """
        Get a process timeout exception if timeout exceptions should
        be generated for a process that continues longer than timeout
        seconds after self.close() was initiated.
        """
        if self.timeout is None \
                or fd is not None \
                or self.exception_on_timeout is False\
                or self._get_abandon() == "wait":
            return None
        return TimeoutExpired(
            cmd=self.command,
            timeout=self.timeout or 11.0,
            stderr=self.stderr_output)

    def _get_abandon(self):
        if self._abandon_cache is None:
            from . import cfg
            cfg_var = "datalad.runtime.stalled-external"
            cfg_val = cfg.obtain(cfg_var)
            if cfg_val not in ("wait", "abandon"):
                raise ValueError(f"Unexpected value: {cfg_var}={cfg_val!r}")
            self._abandon_cache = cfg_val == "abandon"
        return self._abandon_cache
