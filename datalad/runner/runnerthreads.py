import logging
import os
import threading
import time
from abc import (
    abstractmethod,
    ABCMeta,
)
from enum import Enum
from queue import (
    Full,
    Queue,
)
from typing import (
    Any,
    IO,
    List,
    Optional,
    Union,
)


lgr = logging.getLogger("datalad.runner.runnerthreads")


def _try_close(file: IO):
    try:
        file.close()
    except OSError:
        pass


class IOState(Enum):
    ok = "ok"
    timeout = "timeout"


class ExitingThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.exit_requested = False

    def request_exit(self):
        """
        Request the thread to exit. This is not guaranteed to
        have any effect, because the instance has to check for
        self.exit_requested and act accordingly. It might not
        do that.
        """
        self.exit_requested = True


class TimeoutThread(ExitingThread):
    def __init__(self,
                 identifier: Any,
                 timeout: float,
                 signal_queues: List[Queue]):

        assert timeout is not None

        ExitingThread.__init__(self)
        self.identifier = identifier
        self.timeout = timeout
        self.signal_queues = signal_queues
        self.reset_time = 0

    def signal_timeout(self):
        for signal_queue in self.signal_queues:
            try:
                signal_queue.put(
                    (self.identifier, IOState.timeout, None),
                    block=True,
                    timeout=.1
                )
            except Full:
                lgr.debug(
                    f"timeout while trying to signal "
                    f'{(self.identifier, IOState.timeout, None)}')

    def reset(self,
              time_stamp: Optional[float] = None):
        self.reset_time = time_stamp or time.time()

    def run(self):

        lgr.log(5, "%s (%s) started", self, self.identifier)

        # Copy data from source queue to destination queue
        # until exit is requested. If timeouts arise, signal
        # them to the receiver via the signal queue.
        self.reset()
        while not self.exit_requested:

            time_to_wait = (self.reset_time + self.timeout) - time.time()
            if time_to_wait > 0:
                time.sleep(time_to_wait)
                if time.time() - self.reset_time > self.timeout:
                    self.signal_timeout()
                    self.reset()
            else:
                self.signal_timeout()
                self.reset()

        lgr.log(
            5,
            "%s exiting (exit_requested: %s, last data: %s)",
            self,
            self.exit_requested)


class TransportThread(ExitingThread, metaclass=ABCMeta):
    def __init__(self,
                 identifier: str,
                 user_info: Any,
                 signal_queues: List[Queue],
                 timeout_thread: Optional[TimeoutThread] = None
                 ):

        super().__init__()
        self.identifier = identifier
        self.user_info = user_info
        self.signal_queues = signal_queues
        self.timeout_thread = timeout_thread

    def __repr__(self):
        return f"Thread<(user_info: {self.user_info}, cmd:{self.identifier})"

    def __str__(self):
        return self.__repr__()

    def signal(self,
               state: IOState,
               data: Union[bytes, None]):
        for queue in self.signal_queues:
            # Ensure that self.signal() will never block.
            # TODO: separate the timeout and EOF signal paths?
            try:
                queue.put((self.user_info, state, data), block=True, timeout=.1)
            except Full:
                lgr.debug(
                    f"timeout while trying to signal "
                    f"{(self.user_info, state, data)}")

    @abstractmethod
    def read(self) -> Union[bytes, None]:
        """
        Read data from source return None, if source is close,
        or destination close is required.
        """
        raise NotImplementedError

    @abstractmethod
    def write(self,
              data: Union[bytes, None]):
        """
        Write given data to destination, return True if data is
        written successfully, False otherwise.
        """
        raise NotImplementedError

    def run(self):

        lgr.log(5, "%s (%s) started", self, self.identifier)

        # Copy data from source queue to destination queue
        # until exit is requested. If timeouts arise, signal
        # them to the receiver via the signal queue.
        data = b""
        while not self.exit_requested:

            data = self.read()
            # If the source sends None-data it wants
            # us to exit the thread. Signal this to
            # the downstream queues (which might or might
            # not be contain the output queue),
            # and exit the thread.
            if data is None:
                break

            if self.timeout_thread:
                self.timeout_thread.reset()

            if self.exit_requested:
                break

            succeeded = self.write(data)
            if not succeeded:
                break

            if self.timeout_thread:
                self.timeout_thread.reset()

        self.signal(IOState.ok, None)
        lgr.log(
            5,
            "%s exiting (exit_requested: %s, last data: %s)",
            self,
            self.exit_requested, data)


class ReadThread(TransportThread):
    def __init__(self,
                 identifier: str,
                 user_info: Any,
                 source: IO,
                 destination_queue: Queue,
                 signal_queues: List[Queue],
                 length: int = 1024,
                 timeout_thread: Optional[TimeoutThread] = None,
                 ):

        super().__init__(identifier, user_info, signal_queues, timeout_thread)
        self.source = source
        self.destination_queue = destination_queue
        self.length = length

    def read(self) -> Union[bytes, None]:
        try:
            data = os.read(self.source.fileno(), self.length)
        except (ValueError, OSError):
            # The destination was most likely closed, nevertheless,
            # try to close it and indicate EOF.
            _try_close(self.source)
            return None
        return data or None

    def write(self,
              data: Union[bytes, None]) -> bool:

        # We write to an unlimited queue, no need for timeout checking.
        self.destination_queue.put((self.user_info, IOState.ok, data))
        return True


class WriteThread(TransportThread):
    def __init__(self,
                 identifier: str,
                 user_info: Any,
                 source_queue: Queue,
                 destination: IO,
                 signal_queues: List[Queue],
                 timeout_thread: Optional[TimeoutThread] = None,
                 ):

        super().__init__(identifier, user_info, signal_queues, timeout_thread)
        self.source_queue = source_queue
        self.destination = destination

    def read(self) -> Union[bytes, None]:
        data = self.source_queue.get()
        if data is None:
            # Close stdin file descriptor here, since we know that no more
            # data will be sent to stdin.
            _try_close(self.destination)
        return data

    def write(self,
              data: bytes) -> bool:
        try:
            written = 0
            while written < len(data) and not self.exit_requested:
                written += os.write(
                    self.destination.fileno(),
                    data[written:])
                if self.timeout_thread:
                    self.timeout_thread.reset()
                if self.exit_requested:
                    return written == len(data)
        except (BrokenPipeError, OSError, ValueError):
            # The destination was most likely closed, nevertheless,
            # try to close it and indicate EOF.
            _try_close(self.destination)
            return False
        return True
