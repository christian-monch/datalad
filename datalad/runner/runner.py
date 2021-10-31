# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Base DataLad command execution runner
"""

import logging

from .coreprotocols import NoCapture
from .exception import CommandError
from .nonasyncrunner import run_command
from .protocol import GeneratorMixIn


lgr = logging.getLogger('datalad.runner.runner')


class WitlessRunner(object):
    """Minimal Runner with support for online command output processing

    It aims to be as simple as possible, providing only essential
    functionality.
    """
    __slots__ = ['cwd', 'env']

    def __init__(self, cwd=None, env=None):
        """
        Parameters
        ----------
        cwd : path-like, optional
          If given, commands are executed with this path as PWD,
          the PWD of the parent process is used otherwise.
        env : dict, optional
          Environment to be used for command execution. If `cwd`
          was given, 'PWD' in the environment is set to its value.
          This must be a complete environment definition, no values
          from the current environment will be inherited.
        """
        self.env = env
        # stringify to support Path instances on PY35
        self.cwd = str(cwd) if cwd is not None else None

    def _get_adjusted_env(self, env=None, cwd=None, copy=True):
        """Return an adjusted copy of an execution environment

        Or return an unaltered copy of the environment, if no adjustments
        need to be made.
        """
        if copy:
            env = env.copy() if env else None
        if cwd and env is not None:
            # if CWD was provided, we must not make it conflict with
            # a potential PWD setting
            env['PWD'] = cwd
        return env

    def run(self, cmd, protocol=None, stdin=None, cwd=None, env=None, timeout=None, **kwargs):
        """Execute a command and communicate with it.

        Parameters
        ----------
        cmd : list or str
          Sequence of program arguments. Passing a single string causes
          execution via the platform shell.
        protocol : WitlessProtocol, optional
          Protocol class handling interaction with the running process
          (e.g. output capture). A number of pre-crafted classes are
          provided (e.g `KillOutput`, `NoCapture`, `GitProgress`).
          If the protocol has the GeneratorMixIn-mixin, the run-method
          will return an iterator and can therefore be used in a for-clause.
        stdin : file-like, string, bytes, Queue, or None
          If stdin is a file-like, it will be directly used as stdin for the
          subprocess. The caller is resonsible for writing to it and closing it.
          If stdin is a string or bytes, those will be fed to stdin of the
          subprocess. If all data is written, stdin will be closed.
          If stdin is a Queue, all elements (bytes) put into the Queue will
          be passed to stdin until None is read from the queue. If None is read,
          stdin of the subprocess is closed.
        cwd : path-like, optional
          If given, commands are executed with this path as PWD,
          the PWD of the parent process is used otherwise. Overrides
          any `cwd` given to the constructor.
        env : dict, optional
          Environment to be used for command execution. If `cwd`
          was given, 'PWD' in the environment is set to its value.
          This must be a complete environment definition, no values
          from the current environment will be inherited. Overrides
          any `env` given to the constructor.
        timeout:
          None or the seconds after which a timeout callback is
          invoked, if no progress was made in communicating with
          the sub-process
        kwargs :
          Passed to the Protocol class constructor.

        Returns
        -------
        Union[dict, Generator]

            If the protocol does not have a GeneratorMixIn-mixin, the
            result will be a dictionary.
            At minimum there will be keys 'stdout', 'stderr' with
            unicode strings of the cumulative standard output and error
            of the process as values.

            If the protocol has a GeneratorMixIn-mixin, a Generator will be
            returned. This allows to use this function in constructs like:

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

        Raises
        ------
        CommandError
          On execution failure (non-zero exit code) this exception is
          raised which provides the command (cmd), stdout, stderr,
          exit code (status), and a message identifying the failed
          command, as properties.
        FileNotFoundError
          When a given executable does not exist.
        """
        if protocol is None:
            # by default let all subprocess stream pass through
            protocol = NoCapture

        cwd = cwd or self.cwd
        env = self._get_adjusted_env(
            env or self.env,
            cwd=cwd,
        )

        lgr.debug('Run %r (cwd=%s)', cmd, cwd)
        results_or_iterator = run_command(
            cmd,
            protocol,
            stdin,
            protocol_kwargs=kwargs,
            cwd=cwd,
            env=env,
            timeout=timeout
        )

        if issubclass(protocol, GeneratorMixIn):
            return results_or_iterator
        else:
            results = results_or_iterator

        # log before any exception is raised
        lgr.debug("Finished %r with status %s", cmd, results['code'])

        # make it such that we always blow if a protocol did not report
        # a return code at all
        if results.get('code', True) not in [0, None]:
            # the runner has a better idea, doc string warns Protocol
            # implementations not to return these
            results.pop('cmd', None)
            results.pop('cwd', None)
            raise CommandError(
                # whatever the results were, we carry them forward
                cmd=cmd,
                cwd=self.cwd,
                **results,
            )
        # denoise, must be zero at this point
        results.pop('code', None)
        return results
