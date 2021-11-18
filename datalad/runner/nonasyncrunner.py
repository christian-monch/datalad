# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""
Thread based subprocess execution with stdout and stderr passed to protocol objects
"""

import enum
import logging
import subprocess
from collections import deque
from collections.abc import Generator
from queue import (
    Empty,
    Queue,
)
from typing import (
    Any,
    Dict,
    IO,
    List,
    Optional,
    Type,
    Union,
)

from .exception import CommandError
from .protocol import (
    GeneratorMixIn,
    WitlessProtocol,
)
from .runnerthreads import (
    _try_close,
    IOState,
    ReadThread,
    TimeoutThread,
    WriteThread,
)


lgr = logging.getLogger("datalad.runner.nonasyncrunner")

STDIN_FILENO = 0
STDOUT_FILENO = 1
STDERR_FILENO = 2


class _ResultGenerator(Generator):
    """
    Generator returned by run_command if the protocol class
    is a subclass of `datalad.runner.protocol.GeneratorMixIn`
    """
    class GeneratorState(enum.Enum):
        process_running = 0
        process_exited = 1
        connection_lost = 2
        waiting_for_process = 3

    def __init__(self,
                 runner: "ThreadedRunner",
                 result_queue: deque
                 ):

        super().__init__()
        self.runner = runner
        self.result_queue = result_queue
        self.return_code = None
        self.state = self.GeneratorState.process_running
        self.all_closed = False

    def _check_result(self):
        if self.runner.exception_on_error is True:
            if self.return_code != 0:
                protocol = self.runner.protocol
                stderr = protocol.fd_infos[protocol.stderr_fileno][1]
                if stderr is not None:
                    stderr = stderr.decode()
                raise CommandError(cmd=self.runner.cmd,
                                   code=self.return_code,
                                   stderr=stderr)

    def send(self, _):
        runner = self.runner
        if self.state == self.GeneratorState.process_running:
            # If the result queue is empty and no more process interaction
            # files are monitored, progress to next state
            if len(self.result_queue) == 0 and not runner.active_file_numbers:
                self.state = self.GeneratorState.waiting_for_process
            else:
                # If we have no results in the queue, but still monitored
                # file numbers or elements in the output queue, wait on
                # the threaded runner queue.
                while len(self.result_queue) == 0 \
                        and (runner.active_file_numbers
                             or runner.output_queue.empty() is False):
                    runner.process_queue()
                if len(self.result_queue) > 0:
                    return self.result_queue.popleft()
                else:
                    self.state = self.GeneratorState.waiting_for_process

        if self.state == self.GeneratorState.waiting_for_process:
            if len(self.result_queue) > 0:
                return self.result_queue.popleft()

            while runner.wait_for_process():
                if len(self.result_queue) > 0:
                    return self.result_queue.popleft()

            self.return_code = runner.process.poll()
            runner.protocol.process_exited()
            self.state = self.GeneratorState.process_exited
            self._check_result()

        if self.state == self.GeneratorState.process_exited:
            if len(self.result_queue) > 0:
                return self.result_queue.popleft()
            else:
                # TODO: check exception
                runner.protocol.connection_lost(None)
                runner.close_process_stdin_stdout_stderr()
                runner.wait_for_threads()
                self.state = self.GeneratorState.connection_lost

        if self.state == self.GeneratorState.connection_lost:
            # Get all results that were enqueued in
            # state: GeneratorState.process_exited.
            if len(self.result_queue) > 0:
                return self.result_queue.popleft()
            raise StopIteration(self.return_code)

    def throw(self, exception_type, value=None, trace_back=None):
        return Generator.throw(self, exception_type, value, trace_back)

    def poll_process(self) -> Optional[int]:
        return self.runner.process.poll()

    def would_wait(self):
        return self.runner.output_queue.emtpy()

    def terminate_process(self,
                  waiting_time=None):

        self.runner.close_process_stdin_stdout_stderr()
        if waiting_time is not None:
            try:
                self.runner.process.wait(waiting_time)
            except subprocess.TimeoutExpired:
                pass
        self.runner.process.terminate()
        self.runner.process.wait()
        return self.poll_process()


class ThreadedRunner:
    """
    A class the contains a naive implementation for concurrent sub-process
    execution. It uses `subprocess.Popen` and threads to read from stdout and
    stderr of the subprocess, and to write to stdin of the subprocess.

    All read data and timeouts are passed to a protocol instance, which can
    create the final result.
    """
    # Interval in seconds after which we check that a subprocess
    # is still running.
    process_check_interval = 0.2

    def __init__(self,
                 cmd: Union[str, List],
                 protocol_class: Type[WitlessProtocol],
                 stdin: Any,
                 protocol_kwargs: Optional[Dict] = None,
                 timeout: Optional[float] = None,
                 exception_on_error: bool = True,
                 **popen_kwargs
                 ):
        """
        Parameters
        ----------
        cmd : list or str
            Command to be executed, passed to `subprocess.Popen`. If cmd
            is a str, `subprocess.Popen will be called with `shell=True`.

        protocol : WitlessProtocol class or subclass which will be
            instantiated for managing communication with the subprocess.

            If the protocol is a subclass of
            `datalad.runner.protocol.GeneratorMixIn`, this function will
            return a `Generator` which yields whatever the protocol callback
            fed into `GeneratorMixIn.send_result()`.

            If the protocol is not a subclass of
            `datalad.runner.protocol.GeneratorMixIn`, the function will return
            the result created by the protocol method `_generate_result`.

        stdin : file-like, string, bytes, Queue, or None
            If stdin is a file-like, it will be directly used as stdin for the
            subprocess. The caller is responsible for writing to it and closing
            it. If stdin is a string or bytes, those will be fed to stdin of the
            subprocess. If all data is written, stdin will be closed.
            If stdin is a Queue, all elements (bytes) put into the Queue will
            be passed to stdin until None is read from the queue. If None is
            read, stdin of the subprocess is closed.
            If stdin is None, nothing will be sent to stdin of the subprocess.
            More precisely, `subprocess.Popen` will be called with `stdin=None`.

        protocol_kwargs : dict, optional
            Passed to the protocol class constructor.

        timeout : float, optional
            If a non-`None` timeout is specified, the `timeout`-method of
            the protocol will be called if:

            - stdin-write, stdout-read, or stderr-read time out. In this case
              the file descriptor will be given as argument to the
              timeout-method. If the timeout-method return `True`, the file
              descriptor will be closed.

            - process.wait() timeout: if waiting for process completion after
              stdin, stderr, and stdout takes longer than `timeout` seconds,
              the timeout-method will be called with the argument `None`. If
              it returns `True`, the process will be terminated.

        exception_on_error : bool, optional
            This argument is only interpreted if the protocol is a subclass
            of `GeneratorMixIn`. If it is `True` (default), a
            `CommandErrorException` is raised by the generator if the
            sub process exited with a return code not equal to zero. If the
            parameter is `False`, no exception is raised. In both cases the
            return code can be read from the attribute `return_code` of
            the generator.

        popen_kwargs : dict
            Passed to `subprocess.Popen`, will typically be parameters
            supported by `subprocess.Popen`. Note that `bufsize`, `stdin`,
            `stdout`, `stderr`, and `shell` will be overwritten internally.
        """

        self.cmd = cmd
        self.protocol_class = protocol_class
        self.stdin = stdin
        self.protocol_kwargs = protocol_kwargs or {}
        self.timeout = timeout
        self.exception_on_error = exception_on_error
        self.popen_kwargs = popen_kwargs

        self.catch_stdout = self.protocol_class.proc_out is not None
        self.catch_stderr = self.protocol_class.proc_err is not None
        self.generator = self.protocol_class.generator is not None

        self.write_stdin = False
        self.stdin_queue = None
        self.protocol = None
        self.process = None
        self.process_stdin_fileno = None
        self.process_stdout_fileno = None
        self.process_stderr_fileno = None
        self.stderr_enqueueing_thread = None
        self.stdout_enqueueing_thread = None
        self.stdin_enqueueing_thread = None
        self.stderr_timeout_thread = None
        self.stdout_timeout_thread = None
        self.stdin_timeout_thread = None
        self.process_running = False
        self.fileno_mapping = None
        self.fileno_to_file = None
        self.file_to_fileno = None
        self.output_queue = None
        self.active_file_numbers = None

    def run(self) -> Union[Any, Generator]:
        """
        Run the command as specified in __init__.

        Returns
        -------
        Any
            If the protocol is not a subclass of `GeneratorMixIn`, the
            result of protocol._prepare_result will be returned.

        Generator
            If the protocol is a subclass of `GeneratorMixIn`, a Generator
            will be returned. This allows to use this method in constructs
            like:

                for protocol_output in runner.run():
                    ...

            Where the iterator yields whatever protocol.pipe_data_received
            sends into the generator.
            If all output was yielded and the process has terminated, the
            generator will raise StopIteration(return_code), where
            return_code is the return code of the process. The return code
            of the process will also be stored in the "return_code"-attribute
            of the runner. So you could write:

               gen = runner.run()
               for file_descriptor, data in gen:
                   ...

               # get the return code of the process
               result = gen.return_code
        """
        if isinstance(self.stdin, (int, IO, type(None))):
            # indicate that we will not write anything to stdin, that
            # means the user can pass None, or he can pass a
            # file-like and write to it from a different thread.
            self.write_stdin = False  # the caller will write to the parameter

        elif isinstance(self.stdin, (str, bytes)):
            # Establish a queue to write to the process and
            # enqueue the input that is already provided.
            self.write_stdin = True
            self.stdin_queue = Queue()
            self.stdin_queue.put(self.stdin)
            self.stdin_queue.put(None)
        elif isinstance(self.stdin, Queue):
            # Establish a queue to write to the process.
            self.write_stdin = True
            self.stdin_queue = self.stdin
        else:
            # indicate that we will not write anything to stdin, that
            # means the user can pass None, or he can pass a
            # file-like and write to it from a different thread.
            lgr.warning(f"Unknown instance class: {type(self.stdin)}, "
                        f"assuming file-like input: {self.stdin}")
            # We assume that the caller will write to the given
            # file descriptor.
            self.write_stdin = False

        self.protocol = self.protocol_class(**self.protocol_kwargs)

        kwargs = {
            **self.popen_kwargs,
            **dict(
                bufsize=0,
                stdin=subprocess.PIPE if self.write_stdin else self.stdin,
                stdout=subprocess.PIPE if self.catch_stdout else None,
                stderr=subprocess.PIPE if self.catch_stderr else None,
                shell=True if isinstance(self.cmd, str) else False
            )
        }
        self.process = subprocess.Popen(self.cmd, **kwargs)
        self.process_running = True

        self.process_stdin_fileno = self.process.stdin.fileno() if self.write_stdin else None
        self.process_stdout_fileno = self.process.stdout.fileno() if self.catch_stdout else None
        self.process_stderr_fileno = self.process.stderr.fileno() if self.catch_stderr else None

        # We pass process as transport-argument. It does not have the same
        # semantics as the asyncio-signature, but since it is only used in
        # WitlessProtocol, all necessary changes can be made there.
        self.protocol.connection_made(self.process)

        # Map the pipe file numbers to stdout and stderr file number, because
        # the latter are hardcoded in the protocol code
        self.fileno_mapping = {
            self.process_stdout_fileno: STDOUT_FILENO,
            self.process_stderr_fileno: STDERR_FILENO,
            self.process_stdin_fileno: STDIN_FILENO,
        }

        self.fileno_to_file = {
            self.process_stdout_fileno: self.process.stdout,
            self.process_stderr_fileno: self.process.stderr,
            self.process_stdin_fileno: self.process.stdin
        }

        self.file_to_fileno = {
            f: f.fileno()
            for f in (
                self.process.stdout,
                self.process.stderr,
                self.process.stdin
            ) if f is not None
        }

        self.active_file_numbers = set()
        self.output_queue = Queue()

        cmd_string = self.cmd if isinstance(self.cmd, str) else " ".join(self.cmd)

        if self.catch_stderr:
            if self.timeout is not None:
                self.stderr_timeout_thread = TimeoutThread(
                    self.process_stderr_fileno,
                    self.timeout,
                    [self.output_queue])
                self.stderr_timeout_thread.start()
            self.active_file_numbers.add(self.process_stderr_fileno)
            self.stderr_enqueueing_thread = ReadThread(
                identifier="STDERR: " + cmd_string[:20],
                user_info=self.process_stderr_fileno,
                source=self.process.stderr,
                destination_queue=self.output_queue,
                signal_queues=[self.output_queue],
                timeout_thread=self.stderr_timeout_thread)
            self.stderr_enqueueing_thread.start()

        if self.catch_stdout:
            if self.timeout is not None:
                self.stdout_timeout_thread = TimeoutThread(
                    self.process_stdout_fileno,
                    self.timeout,
                    [self.output_queue])
                self.stdout_timeout_thread.start()
            self.active_file_numbers.add(self.process_stdout_fileno)
            self.stdout_enqueueing_thread = ReadThread(
                identifier="STDOUT: " + cmd_string[:20],
                user_info=self.process_stdout_fileno,
                source=self.process.stdout,
                destination_queue=self.output_queue,
                signal_queues=[self.output_queue],
                timeout_thread=self.stdout_timeout_thread)
            self.stdout_enqueueing_thread.start()

        if self.write_stdin:
            if self.timeout is not None:
                self.stdin_timeout_thread = TimeoutThread(
                    self.process_stdin_fileno,
                    self.timeout,
                    [self.output_queue])
                self.stdin_timeout_thread.start()
            self.active_file_numbers.add(self.process_stdin_fileno)
            self.stdin_enqueueing_thread = WriteThread(
                identifier="STDIN: " + cmd_string[:20],
                user_info=self.process_stdin_fileno,
                source_queue=self.stdin_queue,
                destination=self.process.stdin,
                signal_queues=[self.output_queue],
                timeout_thread=self.stdin_timeout_thread)
            self.stdin_enqueueing_thread.start()

        if issubclass(self.protocol_class, GeneratorMixIn):
            assert isinstance(self.protocol, GeneratorMixIn)
            return _ResultGenerator(self, self.protocol.result_queue)

        # Process internal messages until no more active file descriptors
        # are present. This works because active file numbers are only
        # removed when an EOF is received in `self.process_queue`.
        while self.active_file_numbers or self.output_queue.empty() is False:
            self.process_queue()

        # Close communication channels to subprocess (if they were
        # not closed before) and wait for the process to exit. If
        # a timeout was specified and is triggered, call the timeout
        # handler in the protocol with `None`, and ---if the handler
        # returns `True`--- terminate the process.
        self.close_process_stdin_stdout_stderr()
        while self.wait_for_process():
            pass

        result = self.protocol._prepare_result()
        self.protocol.process_exited()
        # TODO: check exception
        self.protocol.connection_lost(None)
        self.close_process_stdin_stdout_stderr()
        self.wait_for_threads()
        return result

    def process_queue(self):
        """
        Get a single event from the queue or handle a timeout. This method
        might modify the set of active file numbers if a file-closed event
        is read from the output queue, or if a timeout-callback return True.
        """
        data = None
        while True:
            # We do not need a user provided timeout here. If
            # self.timeout is None, no timeouts are reported anyway.
            # If self.timeout is not None, and any enqueuing (stdin)
            # or de-queuing (stdout, stderr) operation takes longer than
            # self.timeout, we will get a queue entry for that.
            # We still use a "system"-timeout, i.e.
            # `ThreadedRunner.process_check_interval`, to check whether the
            # process is still running.
            try:
                file_number, state, data = self.output_queue.get(
                    timeout=ThreadedRunner.process_check_interval)
            except Empty:
                self.check_process_state()
                continue

            if state == IOState.ok:
                break

            # Handle timeouts
            if state == IOState.timeout:
                # If the timeout handler returns True, remove
                # the file number that caused the timeout from
                # active files and return.
                if self.protocol.timeout(self.fileno_mapping[file_number]) is True:
                    if file_number in self.active_file_numbers:
                        self.active_file_numbers.remove(file_number)
                    return

        # No timeout occurred, we have proper data or EOF indicators.
        if self.write_stdin and file_number == self.process_stdin_fileno:
            # The only data-signal we expect from stdin thread
            # is None, indicating that the thread ended
            assert data is None
            self.protocol.pipe_connection_lost(
                self.fileno_mapping[self.process_stdin_fileno],
                None)  # TODO: check exception
            self.active_file_numbers.remove(self.process_stdin_fileno)

        elif self.catch_stderr or self.catch_stdout:
            if data is None:
                # Received an EOF for stdout or stderr.
                # TODO: check exception
                self.protocol.pipe_connection_lost(
                    self.fileno_mapping[file_number],
                    None)
                if file_number in self.active_file_numbers:
                    self.active_file_numbers.remove(file_number)
                _try_close(self.fileno_to_file[file_number])
            else:
                # Call the protocol handler for data
                assert isinstance(data, bytes)
                self.protocol.pipe_data_received(
                    self.fileno_mapping[file_number],
                    data)

    def drain_queue(self):
        """
        Process until the queue is empty. This only yields reliable results
        if the threads are not writing to the queue anymore
        """
        while not self.output_queue.empty():
            self.process_queue()

    def check_process_state(self):
        """
        Check whether the process is still running. If we see
        the process exit for the first time, close stdin, stdout,
        and stderr. This will eventually lead to EOF-signaling
        and removal of the file descriptors from the set of
        active file descriptors
        """
        if self.process_running is False:
            return
        self.process_running = self.process.poll() is None
        if self.process_running is False:
            self.close_process_stdin_stdout_stderr()

    def close_process_stdin_stdout_stderr(self):
        self.close_process_stdin()
        self.close_process_stdout_stderr()

    def close_process_stdout_stderr(self):
        """
        Close stdout and stderr of the process. The file descriptors are
        immediately closed and removed from the set of monitored descriptors.
        """
        for file_object in (self.process.stdout,
                            self.process.stderr):
            if file_object is not None:
                file_number = self.file_to_fileno.get(file_object, None)
                if file_number is not None:
                    if file_number in self.active_file_numbers:
                        self.active_file_numbers.remove(file_number)
                _try_close(file_object)

    def close_process_stdin(self):
        # This is handled slightly differently from stdout and
        # stderr, because we want the thread to exit and it might
        # be waiting on an input queue. That is why we enqueue
        # None to the input queue, if it exists.
        if self.stdin_queue:
            self.stdin_queue.put(None)

    def wait_for_process(self) -> bool:
        """
        Wait for a process to exit. Handle timeout and
        call the process timeout handler, if necessary.
        Terminate the process if the timeout handler
        requests that.

        Return:
            `True` if we should continue to wait
            `False`, if the wait is completed
        """
        try:
            self.process.wait(timeout=self.timeout)
            return False
        except subprocess.TimeoutExpired:
            if self.protocol.timeout(None) is True:
                self.close_process_stdin_stdout_stderr()
                self.process.terminate()
                self.process.wait()
                return False
            return True

    def wait_for_threads(self):
        for thread in (self.stderr_enqueueing_thread,
                       self.stdout_enqueueing_thread,
                       self.stdin_enqueueing_thread,
                       self.stderr_timeout_thread,
                       self.stdout_timeout_thread,
                       self.stdin_timeout_thread):
            if thread is not None:
                thread.request_exit()
                #thread.join()


def run_command(cmd: Union[str, List],
                protocol: Type[WitlessProtocol],
                stdin: Any,
                protocol_kwargs: Optional[Dict] = None,
                timeout: Optional[float] = None,
                exception_on_error: bool = True,
                **popen_kwargs) -> Union[Any, Generator]:
    """
    Run a command in a subprocess

    this function delegates the execution to an instance of
    `ThreadedRunner`, please see `ThreadedRunner.__init__()` for a
    documentation of the parameters, and `ThreadedRunner.run()` for a
    documentation of the return values.
    """
    runner = ThreadedRunner(
        cmd=cmd,
        protocol_class=protocol,
        stdin=stdin,
        protocol_kwargs=protocol_kwargs,
        timeout=timeout,
        exception_on_error=exception_on_error,
        **popen_kwargs,
    )

    return runner.run()
