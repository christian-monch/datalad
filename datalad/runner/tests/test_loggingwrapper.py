from __future__ import annotations

from io import BytesIO
from typing import (
    Any,
    Optional,
)

from datalad.runner.coreprotocols import StdOutErrCapture
from datalad.runner.runnertools import (
    WrapLogger,
    wrap_logger,
)


class TestProtocol(StdOutErrCapture):

    __test__ = False  # class is not a class of tests

    def __init__(self,
                 output_list: list,
                 done_future: Any = None,
                 encoding: Optional[str] = None,
                 ) -> None:

        StdOutErrCapture.__init__(
            self,
            done_future=done_future,
            encoding=encoding)
        self.output_list = output_list

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        self.output_list.append((fd, data))


def test_wrapping_function_on_class() -> None:
    output_list = []
    stdout_io = BytesIO()
    stderr_io = BytesIO()

    wrap_logger(TestProtocol, stdout_io, stderr_io)
    _test_protocol_instance(TestProtocol(
        output_list=output_list),
        output_list,
        stdout_io,
        stderr_io
    )


def test_decorator():
    output = []
    stdout_io = BytesIO()
    stderr_io = BytesIO()

    @WrapLogger(stdout_io, stderr_io)
    class TestProtocol2(StdOutErrCapture):
        def pipe_data_received(self, fd: int, data: bytes) -> None:
            output.append((fd, data))

    _test_protocol_instance(TestProtocol2(), output, stdout_io, stderr_io)


def _test_protocol_instance(protocol_instance, output, stdout_io, stderr_io):
    protocol_instance.pipe_data_received(1, b"stdout")
    protocol_instance.pipe_data_received(2, b"stderr")

    assert output == [(1, b"stdout"), (2, b"stderr")]
    assert stdout_io.getvalue() == b"stdout"
    assert stderr_io.getvalue() == b"stderr"
