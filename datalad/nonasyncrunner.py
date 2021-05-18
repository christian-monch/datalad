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
import threading
import time
from typing import Any


logger = logging.getLogger("datalad.runner")

STDIN_FILENO = 0
STDOUT_FILENO = 1
STDERR_FILENO = 2


class ReaderThread(threading.Thread):
    def __init__(self, file, q, command=""):
        super().__init__(daemon=True)
        self.file = file
        self.queue = q
        self.command = command
        self.quit = False

    def __str__(self):
        return f"ReaderThread({self.file}, {self.queue}, {self.command})"

    def request_exit(self):
        """
        Request the thread to exit. This is not guaranteed to
        have any effect, because the thread might be waiting in
        os.read() or queue.put(). We are closing the file that read
        is reading from here, but the queue has to be emptied in
        another thread in order to ensure thread-exiting.
        """
        self.quit = True
        self.file.close()

    def run(self):
        logger.debug(f"{self} started")

        while not self.quit:

            data = os.read(self.file.fileno(), 1024)
            if data == b"":
                logger.debug(f"{self} EOF")
                self.queue.put((self.file.fileno(), None, time.time()))
                break

            self.queue.put((self.file.fileno(), data, time.time()))

        logger.debug(f"{self} exiting")


class WriterThread(threading.Thread):
    def __init__(self, file, q, command=""):
        super().__init__(daemon=True)
        self.file = file
        self.queue = q
        self.command = command
        self.quit = False

    def __str__(self):
        return f"WriterThread({self.file}, {self.queue}, {self.command})"

    def run(self):
        logger.debug(f"{self} started")
        while not self.quit:

            data = self.queue.get()
            if data is None:
                logger.debug(f"{self} EOF")
                break
            try:
                os.write(self.file.fileno(), data)
            except BrokenPipeError:
                logger.debug(f"{self} broken pipe")
                break

        logger.debug(f"{self} exiting")


def run_command(cmd,
                protocol_class,
                stdin,
                protocol_kwargs=None,
                **kwargs) -> Any:

    catch_stdout = protocol_class.proc_out is not None
    catch_stderr = protocol_class.proc_err is not None
    write_stdin = protocol_class.proc_in is not None

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
    process_stdout_fileno = process.stdout.fileno() if catch_stdout else None
    process_stderr_fileno = process.stderr.fileno() if catch_stderr else None
    protocol = protocol_class(**protocol_kwargs)

    # We pass process as transport-argument. It does not have the same
    # semantics as the asyncio-signature, but since it is only used in
    # WitlessProtocol, we can change it there.
    protocol.connection_made(process)

    # Map the pipe file numbers to stdout and stderr file number, because
    # the latter are hardcoded in the code that subclasses asyncioprotocol code
    fileno_mapping = {
        process_stdout_fileno: STDOUT_FILENO,
        process_stderr_fileno: STDERR_FILENO
    }

    if catch_stdout or catch_stderr:

        output_queue = queue.Queue()
        input_queue = queue.Queue()

        active_file_numbers = set()
        if catch_stderr:
            stderr_reader_thread = ReaderThread(process.stderr, output_queue, cmd)
            stderr_reader_thread.start()
            active_file_numbers.add(process.stderr.fileno())
        if catch_stdout:
            stdout_reader_thread = ReaderThread(process.stdout, output_queue, cmd)
            stdout_reader_thread.start()
            active_file_numbers.add(process.stdout.fileno())
        if write_stdin:
            stdin_writer_thread = WriterThread(process.stdin, input_queue, cmd)
            stdin_writer_thread.start()
            active_file_numbers.add(process.stdin.fileno())

        while True:
            if write_stdin and stdin_writer_thread.is_alive():
                write_data = protocol.provide_pipe_data(STDIN_FILENO)
                if write_data is not None:
                    input_queue.put(write_data)

            file_number, data, time_stamp = output_queue.get()
            if isinstance(data, bytes):
                protocol.pipe_data_received(fileno_mapping[file_number], data)
            else:
                protocol.pipe_connection_lost(fileno_mapping[file_number], data)
                active_file_numbers.remove(file_number)

                if write_stdin:
                    if active_file_numbers == {process.stdin.fileno()}:
                        # Let writer thread terminate
                        input_queue.put(None)
                        break
                else:
                    if not active_file_numbers:
                        break

    process.wait()
    result = protocol._prepare_result()
    protocol.process_exited()
    protocol.connection_lost(None)  # TODO: check exception

    return result
