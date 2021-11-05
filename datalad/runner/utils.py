# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Utilities required by runner-related functionality

All runner-related code imports from here, so this is a comprehensive declaration
of utility dependencies.
"""
import enum
from typing import (
    List,
    Optional,
    Tuple,
)

from datalad.dochelpers import borrowdoc
from datalad.utils import (
    auto_repr,
    ensure_unicode,
    generate_file_chunks,
    join_cmdline,
    try_multiple,
    unlink,
)


class LineEndMarker(enum.Enum):
    LF = 0
    CRLF = 1
    unterminated = None


class LineSplitter:

    class State(enum.Enum):
        plain = 0
        carriage_return = 1

    def __init__(self):
        self.state = LineSplitter.State.plain
        self.current_line = None

    def process(self, data: str) -> List[str]:

        assert isinstance(data, str), f"data ({data}) is not of type str"

        detected_lines = []
        if data == "":
            self.current_line = ""
        else:
            for character in data:
                self._process_character(character, detected_lines)
        return detected_lines

    def finish_processing(self) -> Optional[Tuple[str, LineEndMarker]]:
        if self.current_line is not None:
            return self.current_line, LineEndMarker.unterminated
        return None

    def _process_character(self,
                           character: str,
                           detected_lines: List[Tuple[str, LineEndMarker]]):

        if self.state == self.State.plain:
            if character == "\n":
                self._add_character("")
                detected_lines.append((self.current_line, LineEndMarker.LF))
                self.current_line = None
            elif character == "\r":
                self.state = self.State.carriage_return
            else:
                self._add_character(character)

        elif self.state == self.State.carriage_return:
            if character == "\n":
                self._add_character("")
                detected_lines.append((self.current_line, LineEndMarker.CRLF))
                self.current_line = None
            else:
                self._add_character("\r")
                self._add_character(character)
            self.state = self.State.plain
        else:
            raise ValueError(f"Unknown state: {self.state}")

    def _add_character(self, character: str):
        if self.current_line is None:
            self.current_line = character
        else:
            self.current_line += character
