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

import logging
import subprocess
from queue import (
    Empty,
    Queue,
)
from typing import (
    IO,
    List,
    Optional,
    Union,
)

from .runnerthreads import (
    BlockingOSReaderThread,
    BlockingOSWriterThread,
    IOState,
    ReadThread,
    WriteThread,
)


lgr = logging.getLogger("datalad.runner.yieldingrunner")

STDIN_FILENO = 0
STDOUT_FILENO = 1
STDERR_FILENO = 2


def yielding_run_command(cmd: Union[str, List],
                         stdin: Optional[Union[str, bytes, IO, Queue]] = None,
                         catch_stdout: bool = False,
                         catch_stderr: bool = False,
                         timeout: Optional[float] = None,
                         **kwargs):
    """
    Run a command in a subprocess

    This is a naive implementation that uses sub`process.Popen`
    and threads to read from sub-proccess' stdout and stderr and
    put it into a queue from which the main-thread reads.
    Upon receiving data from the queue, the main thread
    will delegate data handling to a protocol_class instance

    Parameters
    ----------
    cmd : list or str
      Command to be executed, passed to `subprocess.Popen`. If cmd
      is a str, `subprocess.Popen will be called with `shell=True`.
    stdin : file-like, subprocess.PIPE, str, bytes, Queue, or None
      Passed to the subprocess as its standard input. In the case of a str
      or bytes objects, the subprocess stdin is set to subprocess.PIPE
      and the given input is written to it after the process has started.
    kwargs : Pass to `subprocess.Popen`, will typically be parameters
       supported by `subprocess.Popen`. Note that `bufsize`, `stdin`,
       `stdout`, `stderr`, and `shell` will be overwritten by
       `run_command`.

    Returns
    -------
    :Returns: int return code of the process
    """

    if isinstance(stdin, (IO, type(None))):
        # indicate that we will not write anything to stdin, that
        # means the user can pass None, or he can pass a
        # file-like and write to it from a different thread.
        write_stdin = False  # the caller will write to the parameter

    elif isinstance(stdin, (str, bytes, Queue)):
        # establish infrastructure to write to the process
        write_stdin = True
        if not isinstance(stdin, Queue):
            # if input is already provided, enqueue it.
            stdin_queue = Queue()
            stdin_queue.put(stdin)
            stdin_queue.put(None)
        else:
            stdin_queue = stdin
    else:
        raise ValueError(f"unsupported stdin type: {stdin}")

    kwargs = {
        **kwargs,
        **dict(
            bufsize=0,
            stdin=subprocess.PIPE if write_stdin else stdin,
            stdout=subprocess.PIPE if catch_stdout else None,
            stderr=subprocess.PIPE if catch_stderr else None,
            shell=True if isinstance(cmd, str) else False
        )
    }

    process = subprocess.Popen(cmd, **kwargs)
    process_stdin_fileno = process.stdin.fileno() if write_stdin else None
    process_stdout_fileno = process.stdout.fileno() if catch_stdout else None
    process_stderr_fileno = process.stderr.fileno() if catch_stderr else None

    # Map the pipe file numbers to stdout and stderr file number, because
    # the latter are hardcoded in the protocol code
    fileno_mapping = {
        process_stdin_fileno: STDIN_FILENO,
        process_stdout_fileno: STDOUT_FILENO,
        process_stderr_fileno: STDERR_FILENO
    }

    if catch_stdout or catch_stderr or write_stdin:

        output_queue = Queue()
        active_file_numbers = set()
        active_threads = set()

        if catch_stderr:
            active_file_numbers.add(process_stderr_fileno)
            stderr_reader_thread = BlockingOSReaderThread(process.stderr)
            stderr_enqueueing_thread = ReadThread(
                identifier=process_stderr_fileno,
                source_blocking_queue=stderr_reader_thread.queue,
                destination_queue=output_queue,
                signal_queue=output_queue,
                timeout=timeout)
            active_threads.add(stderr_reader_thread)
            active_threads.add(stderr_enqueueing_thread)
            stderr_reader_thread.start()
            stderr_enqueueing_thread.start()

        if catch_stdout:
            active_file_numbers.add(process_stdout_fileno)
            stdout_reader_thread = BlockingOSReaderThread(process.stdout)
            stdout_enqueueing_thread = ReadThread(
                identifier=process_stdout_fileno,
                source_blocking_queue=stdout_reader_thread.queue,
                destination_queue=output_queue,
                signal_queue=output_queue,
                timeout=timeout)
            active_threads.add(stdout_reader_thread)
            active_threads.add(stdout_enqueueing_thread)
            stdout_reader_thread.start()
            stdout_enqueueing_thread.start()

        if write_stdin:
            active_file_numbers.add(process_stdin_fileno)
            stdin_writer_thread = BlockingOSWriterThread(process.stdin)
            stdin_enqueueing_thread = WriteThread(
                identifier=process_stdin_fileno,
                source_queue=stdin_queue,
                destination_blocking_queue=stdin_writer_thread.queue,
                signal_queue=output_queue,
                timeout=timeout)
            active_threads.add(stdin_writer_thread)
            active_threads.add(stdin_enqueueing_thread)
            stdin_writer_thread.start()
            stdin_enqueueing_thread.start()

        while active_file_numbers:

            active_threads = set([
                thread
                for thread in active_threads
                if thread.is_alive()
            ])

            process_exited = process.poll() is not None

            if not active_threads and output_queue.empty():
                lgr.log(5, "All threads exited and output queue is empty, exiting runner.")
                break
            elif not active_file_numbers and output_queue.empty():
                lgr.log(5, "No active queue filling threads and output queue is empty, exiting runner.")
                break
            elif process_exited and output_queue.empty():
                lgr.log(5, "Process exited and output queue is empty, exiting runner.")
                break

            while True:
                try:
                    file_number, state, data = output_queue.get(timeout=timeout)
                except Empty:
                    lgr.warning(f"TIMEOUT on output queue")
                    continue

                if state == IOState.ok:
                    break

                # Handle timeouts
                if state == IOState.timeout:
                    lgr.warning(f"TIMEOUT on {fileno_mapping[file_number]}")
                    if process.poll() is not None:
                        lgr.warning(f"PROCESS exited with {process.poll()}")

            if write_stdin and file_number == process_stdin_fileno:
                # If we receive anything on the signal queue from
                # the writer thread, it should be `None`,
                # indicating that all data was written.
                assert data is None, \
                    f"expected None-data from writer thread, got {data}"
                if process_stdin_fileno in active_file_numbers:
                    active_file_numbers.remove(process_stdin_fileno)
            else:
                if data is None:
                    if file_number in active_file_numbers:
                        active_file_numbers.remove(file_number)
                else:
                    yield fileno_mapping[file_number], data

    print("YR: waiting for process")
    process.wait()

    for fd in (process.stdin, process.stdout, process.stderr):
        if fd is not None:
            fd.close()

    return process.returncode
