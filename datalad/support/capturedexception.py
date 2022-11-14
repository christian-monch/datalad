import logging
import traceback
from pathlib import Path


lgr = logging.getLogger('datalad.support.exceptions')


class CapturedException:
    """This class represents information about an occurred exception (including
    its traceback), while not holding any references to the actual exception
    object or its traceback, frame references, etc.

    Just keep the textual information for logging or whatever other kind of
    reporting.
    """

    def __init__(self, exc, limit=None, capture_locals=False,
                 level=8, logger=None):
        """Capture an exception and its traceback for logging.

        Clears the exception's traceback frame references afterwards.

        Parameters
        ----------
        exc: Exception
        limit: int
          Note, that this is limiting the capturing of the exception's
          traceback depth. Formatting for output comes with it's own limit.
        capture_locals: bool
          Whether or not to capture the local context of traceback frames.
        """
        # Note, that with lookup_lines=False the lookup is deferred,
        # not disabled. Unclear to me ATM, whether that means to keep frame
        # references around, but prob. not. TODO: Test that.
        self.tb = traceback.TracebackException.from_exception(
            exc,
            limit=limit,
            lookup_lines=True,
            capture_locals=capture_locals
        )
        traceback.clear_frames(exc.__traceback__)

        # log the captured exception
        logger = logger or lgr
        logger.log(level, "%r", self)

    def format_oneline_tb(self, limit=None, include_str=True):
        """Format an exception traceback as a one-line summary

        Returns a string of the form [filename:contextname:linenumber, ...].
        If include_str is True (default), this is prepended with the string
        representation of the exception.
        """
        return format_oneline_tb(
            self, self.tb, limit=limit, include_str=include_str)

    def format_standard(self):
        """Returns python's standard formatted traceback output

        Returns
        -------
        str
        """
        # TODO: Intended for introducing a decent debug mode later when this
        #       can be used from within log formatter / result renderer.
        #       For now: a one-liner is free
        return ''.join(self.tb.format())

    def format_short(self):
        """Returns a short representation of the original exception

        Form: ExceptionName(exception message)

        Returns
        -------
        str
        """
        return self.name + '(' + self.message + ')'

    @property
    def message(self):
        """Returns only the message of the original exception

        Returns
        -------
        str
        """
        return str(self.tb)

    @property
    def name(self):
        """Returns the class name of the original exception

        Returns
        -------
        str
        """
        return self.tb.exc_type.__qualname__

    def __str__(self):
        return self.format_short()

    def __repr__(self):
        return self.format_oneline_tb(limit=None, include_str=True)


def format_oneline_tb(exc, tb=None, limit=None, include_str=True):
    """Format an exception traceback as a one-line summary

    Parameters
    ----------
    exc: Exception
    tb: TracebackException, optional
      If not given, it is generated from the given exception.
    limit: int, optional
      Traceback depth limit. If not given, the config setting
      'datalad.exc.str.tblimit' will be used, or all entries
      are reported.
    include_str: bool
      If set, is True (default), the return value is prepended with a string
    representation of the exception.

    Returns
    -------
    str
      Of format [filename:contextname:linenumber, ...].
    """

    # Note: No import at module level, since ConfigManager imports
    # dochelpers -> circular import when creating datalad.cfg instance at
    # startup.
    from datalad import cfg

    if include_str:
        # try exc message else exception type
        leading = exc.message or exc.name
        out = "{} ".format(leading)
    else:
        out = ""

    if tb is None:
        tb = traceback.TracebackException.from_exception(
            exc,
            limit=limit,
            lookup_lines=True,
            capture_locals=False,
        )

    entries = []
    entries.extend(tb.stack)
    if tb.__cause__:
        entries.extend(tb.__cause__.stack)
    elif tb.__context__ and not tb.__suppress_context__:
        entries.extend(tb.__context__.stack)

    if limit is None:
        limit = int(cfg.obtain('datalad.exc.str.tblimit',
                               default=len(entries)))
    if entries:
        tb_str = "[%s]" % (','.join(
            "{}:{}:{}".format(
                Path(frame_summary.filename).name,
                frame_summary.name,
                frame_summary.lineno)
            for frame_summary in entries[-limit:])
        )
        out += "{}".format(tb_str)

    return out
