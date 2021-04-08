# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""High-level interface for creating a publication target on GitHub
"""

__docformat__ = 'restructuredtext'


import logging
import re

from os.path import relpath

from datalad.interface.base import (
    build_doc,
    Interface,
)
from datalad.interface.common_opts import (
    recursion_flag,
    recursion_limit,
    publish_depends,
)
from datalad.interface.results import get_status_dict
from datalad.interface.utils import eval_results
from datalad.support.param import Parameter
from datalad.support.constraints import (
    EnsureChoice,
    EnsureNone,
    EnsureStr,
)
from datalad.distribution.dataset import (
    datasetmethod,
    EnsureDataset,
    require_dataset,
)
from datalad.distribution.siblings import Siblings

lgr = logging.getLogger('datalad.distribution.create_sibling_github')


def normalize_reponame(path):
    """Turn name (e.g. path) into a Github compliant repository name
    """
    return re.sub(r'\s+', '_', re.sub(r'[/\\]+', '-', path))


@build_doc
class CreateSiblingGithub(Interface):
    """Create dataset sibling on GitHub.

    An existing GitHub project, or a project created via the GitHub website can
    be configured as a sibling with the :command:`siblings` command.
    Alternatively, this command can create a repository under a user's GitHub
    account, or any organization a user is a member of (given appropriate
    permissions). This is particularly helpful for recursive sibling creation
    for subdatasets. In such a case, a dataset hierarchy is represented as a
    flat list of GitHub repositories.

    GitHub cannot host dataset content (but LFS special remote could be used,
    http://handbook.datalad.org/r.html?LFS). However, in combination with
    other data sources (and siblings), publishing a dataset to GitHub can
    facilitate distribution and exchange, while still allowing any dataset
    consumer to obtain actual data content from alternative sources.

    For GitHub authentication a personal access token is needed.
    Such a token can be generated by visiting https://github.com/settings/tokens
    or navigating via GitHub Web UI through:
    Settings -> Developer settings -> Personal access tokens.
    We will first consult Git configuration *hub.oauthtoken* for tokens possibly
    available there, and then from the system credential store.

    If you provide [PY: `github_login` PY][CMD: --github-login NAME CMD],
    we will consider only tokens associated with that GitHub login from
    *hub.oauthtoken*, and store/check the token in credential store as associated
    with that specific login name.
    """
    _params_ = dict(
        dataset=Parameter(
            args=("--dataset", "-d",),
            doc="""specify the dataset to create the publication target for. If
                no dataset is given, an attempt is made to identify the dataset
                based on the current working directory""",
            constraints=EnsureDataset() | EnsureNone()),
        reponame=Parameter(
            args=('reponame',),
            metavar='REPONAME',
            doc="""GitHub repository name. When operating recursively,
            a suffix will be appended to this name for each subdataset""",
            constraints=EnsureStr()),
        recursive=recursion_flag,
        recursion_limit=recursion_limit,
        name=Parameter(
            args=('-s', '--name',),
            metavar='NAME',
            doc="""name to represent the GitHub repository in the local
            dataset installation""",
            constraints=EnsureStr()),
        existing=Parameter(
            args=("--existing",),
            constraints=EnsureChoice('skip', 'error', 'reconfigure', 'replace'),
            metavar='MODE',
            doc="""desired behavior when already existing or configured
            siblings are discovered. In this case, a dataset can be skipped
            ('skip'), the sibling configuration be updated ('reconfigure'),
            or process interrupts with error ('error'). DANGER ZONE: If 'replace'
            is used, an existing github repository will be irreversibly removed,
            re-initialized, and the sibling (re-)configured (thus implies 'reconfigure').
            `replace` could lead to data loss, so use with care.  To minimize
            possibility of data loss, in interactive mode DataLad will ask for
            confirmation, but it would raise an exception in non-interactive mode.
            """,),
        github_login=Parameter(
            args=('--github-login',),
            constraints=EnsureStr() | EnsureNone(),
            metavar='NAME',
            doc="""GitHub user name or access token"""),
        github_organization=Parameter(
            args=('--github-organization',),
            constraints=EnsureStr() | EnsureNone(),
            metavar='NAME',
            doc="""If provided, the repository will be created under this
            GitHub organization. The respective GitHub user needs appropriate
            permissions."""),
        access_protocol=Parameter(
            args=("--access-protocol",),
            constraints=EnsureChoice('https', 'ssh'),
            doc="""Which access protocol/URL to configure for the sibling"""),
        publish_depends=publish_depends,
        private=Parameter(
            args=("--private",),
            action="store_true",
            default=False,
            doc="""If this flag is set, the repository created on github
            will be marked as private and only visible to those granted 
            access or by membership of a team/organization/etc.
            """),
        dry_run=Parameter(
            args=("--dry-run",),
            action="store_true",
            doc="""If this flag is set, no repositories will be created.
            Instead tests for name collisions with existing projects will be
            performed, and would-be repository names are reported for all
            relevant datasets"""),
        dryrun=Parameter(
            args=("--dryrun",),
            action="store_true",
            doc="""Deprecated. Use the renamed
            [CMD: --dry-run CMD][PY: `dry_run` PY] parameter"""),
    )

    @staticmethod
    @datasetmethod(name='create_sibling_github')
    @eval_results
    def __call__(
            reponame,
            dataset=None,
            recursive=False,
            recursion_limit=None,
            name='github',
            existing='error',
            github_login=None,
            github_organization=None,
            access_protocol='https',
            publish_depends=None,
            private=False,
            dryrun=False,
            dry_run=False):
        # this is an absolute leaf package, import locally to avoid
        # unnecessary dependencies
        from datalad.support.github_ import _make_github_repos_

        if reponame != normalize_reponame(reponame):
            raise ValueError('Invalid name for a GitHub project: {}'.format(
                reponame))

        # what to operate on
        ds = require_dataset(
            dataset, check_installed=True, purpose='create GitHub sibling')

        res_kwargs = dict(
            action='create_sibling_github [dry-run]' if dryrun else
            'create_sibling_github',
            logger=lgr,
            refds=ds.path,
        )
        # gather datasets and essential info
        # dataset instance and mountpoint relative to the top
        toprocess = [ds]
        if recursive:
            for sub in ds.subdatasets(
                    fulfilled=None,  # we want to report on missing dataset in here
                    recursive=recursive,
                    recursion_limit=recursion_limit,
                    result_xfm='datasets'):
                if not sub.is_installed():
                    lgr.info('Ignoring unavailable subdataset %s', sub)
                    continue
                toprocess.append(sub)

        # check for existing remote configuration
        filtered = []
        for d in toprocess:
            if name in d.repo.get_remotes():
                yield get_status_dict(
                    ds=d,
                    status='error' if existing == 'error' else 'notneeded',
                    message=('already has a configured sibling "%s"', name),
                    **res_kwargs)
                continue
            gh_reponame = reponame if d == ds else \
                '{}-{}'.format(
                    reponame,
                    normalize_reponame(str(d.pathobj.relative_to(ds.pathobj))))
            filtered.append((d, gh_reponame))

        if not filtered:
            # all skipped
            return

        # actually make it happen on GitHub
        for res in _make_github_repos_(
                github_login, github_organization, filtered,
                existing, access_protocol, private, dryrun):
            # blend reported results with standard properties
            res = dict(
                res,
                **res_kwargs)
            if 'message' not in res:
                res['message'] = ("project at %s", res['url'])
            # report to caller
            yield get_status_dict(**res)
            if res['status'] not in ('ok', 'notneeded'):
                # something went wrong, do not proceed
                continue
            # lastly configure the local datasets
            if not dryrun:
                extra_remote_vars = {
                    # first make sure that annex doesn't touch this one
                    # but respect any existing config
                    'annex-ignore': 'true',
                    # first push should separately push active branch first
                    # to overcome github issue of choosing "default" branch
                    # alphabetically if its name does not match the default
                    # branch for the user (or organization) which now defaults
                    # to "main"
                    'datalad-push-default-first': 'true'
                }
                for var_name, var_value in extra_remote_vars.items():
                    var = 'remote.{}.{}'.format(name, var_name)
                    if var not in d.config:
                        d.config.add(var, var_value, where='local')
                yield from Siblings()(
                    'configure',
                    dataset=d,
                    name=name,
                    url=res['url'],
                    recursive=False,
                    # TODO fetch=True, maybe only if one existed already
                    publish_depends=publish_depends,
                    result_renderer='disabled')

        # TODO let submodule URLs point to GitHub (optional)
