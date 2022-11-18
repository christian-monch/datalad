from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Generator,
)

from datalad.runner.coreprotocols import StdOutCapture
from datalad.runner.protocol import GeneratorMixIn
from datalad.runner.nonasyncrunner import ThreadedRunner


class _SshCatProtocol(StdOutCapture, GeneratorMixIn):
    def __init__(self, done_future=None, encoding=None):
        StdOutCapture.__init__(self, done_future, encoding)
        GeneratorMixIn.__init__(self, )

    def pipe_data_received(self, fd: int, data: bytes):
        assert fd == 1
        self.send_result(data)


class SshCat:
    def __init__(self, host: str, filename: str | Path, *additional_ssh_args):
        self.host = host
        self.filename = str(filename)
        self.ssh_args: list[str] = list(additional_ssh_args)

    def run(self) -> Any | Generator:
        return ThreadedRunner(
            cmd=['ssh'] + self.ssh_args + ['-e', 'none', self.host, 'cat',  self.filename],
            protocol_class=_SshCatProtocol,
            stdin=None,
        ).run()
