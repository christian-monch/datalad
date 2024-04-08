"""Helper that extend protocols and runner with new functionality"""
from __future__ import annotations

from .coreprotocols import WitlessProtocol


def wrap_logger(klass: type[WitlessProtocol],
                stdout_io,
                stderr_io
                ) -> type[WitlessProtocol]:
    """
    This function modifies the class given in ``klass`` in such a way that it
    copies all data received in ``pipe_data_received`` to ``stdout_io`` or
    ``stderr_io`` depending on the file descriptor, and then passes it on to
    the original ``pipe_data_received`` method.

    Parameters
    ----------
    klass: type[WitlessProtocol]
        The class to modify
    stdout_io:
        The io object to write stdout data to, it has to support the method
        ``write``, which is called with ``bytes`` data.
    stderr_io
        The io object to write stderr data to, it has to support the method
        ``write``, which is called with ``bytes`` data.

    Returns
    -------
    type[WitlessProtocol]
         The modified class
    """
    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if fd == 1:
            stdout_io.write(data)
        elif fd == 2:
            stderr_io.write(data)
        original_pipe_data_received(self, fd, data)

    original_pipe_data_received = klass.pipe_data_received
    klass.pipe_data_received = pipe_data_received
    return klass


class WrapLogger:
    """A decorator for classes that allows logging of stdout and stderr data"""
    def __init__(self, stdout_io, stderr_io):
        self.stdout_io = stdout_io
        self.stderr_io = stderr_io

    def __call__(self, klass):
        def pipe_data_received(obj, fd: int, data: bytes) -> None:
            if fd == 1:
                self.stdout_io.write(data)
            elif fd == 2:
                self.stderr_io.write(data)
            original_pipe_data_received(obj, fd, data)

        original_pipe_data_received = klass.pipe_data_received
        klass.pipe_data_received = pipe_data_received
        return klass
