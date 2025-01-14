# Copyright (C) 2016 Wind River Systems, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

# Note this class MUST run in both python2 and python3

import argparse
import sys
import os

from urllib.parse import urlparse

import logger_setup
logger = logger_setup.setup_logging()

class Argparse_Setup:
    def __init__(self, setup, parser=None):
        if not parser:
            parser = argparse.ArgumentParser(description='setup.py: Application to fetch & setup a distribution project.')
        self.layer_select = False
        self.parser = parser
        self.setup = setup
        # Each group's layername must have a group keyword, for example:
        # - meta-examplekey
        # - meta-examplekey-layer1
        # - meta-examplekey-layer2
        # The examplekey is the keyword for these layers. It will be used to
        # check against the layername from layerindex (not layer's directory
        # name). Any layername contains the keyword is treated as an extra
        # group layer, and they should only be enabled when
        # --use-layer-groups=keyword or --layers=layer is specified.
        # Add the new keyword here when there is a new extra group.
        self.extra_group_keys=['ccm']

    def evaluate_args(self, args):
        self.add_options()
        parsed_args = self.parser.parse_args(args)
        self.handle_setup_args(parsed_args, args)

    def handle_setup_args(self, parsed_args, args):
        # Parse setup options
        if (parsed_args.verbose):
            if self.setup:
                self.setup.set_debug()
            del parsed_args.verbose

        if (parsed_args.base_url):
            if self.setup:
                self.setup.set_base_url(parsed_args.base_url)
            del parsed_args.base_url

        if (parsed_args.base_branch):
            if self.setup:
                self.setup.set_base_branch(parsed_args.base_branch)
            del parsed_args.base_branch

        if parsed_args.no_prime:
            if self.setup:
                self.setup.set_no_prime(parsed_args.no_prime)
            del parsed_args.no_prime

        # Parse repo option
        if (parsed_args.repo_verbose):
            if self.setup:
                self.setup.set_repo_verbose(parsed_args.repo_verbose)
            del parsed_args.repo_verbose

        if (parsed_args.repo_jobs):
            if self.setup:
                self.setup.set_jobs(parsed_args.repo_jobs)
            del parsed_args.repo_jobs

        if (parsed_args.repo_depth):
            if self.setup:
                self.setup.set_depth(parsed_args.repo_depth)
            del parsed_args.repo_depth

        if (parsed_args.repo_retry_fetches):
            if self.setup:
                self.setup.set_retry_fetches(parsed_args.repo_retry_fetches)
            del parsed_args.repo_retry_fetches

        if (parsed_args.repo_force_sync):
            if self.setup:
                self.setup.set_force_sync(parsed_args.repo_force_sync)
            del parsed_args.repo_force_sync

        if (parsed_args.repo_url):
            if self.setup:
                self.setup.set_repo_url(parsed_args.repo_url)
            del parsed_args.repo_url

        if (parsed_args.repo_branch):
            if self.setup:
                self.setup.set_repo_rev(parsed_args.repo_branch)
            del parsed_args.repo_branch

        if parsed_args.repo_no_prune:
            if self.setup:
                self.setup.set_no_prune(parsed_args.repo_no_prune)
            del parsed_args.repo_no_prune

        # Look for list options
        if parsed_args.list_distros:
            if self.setup:
                self.setup.list_distros = parsed_args.list_distros

        if parsed_args.list_machines:
            if self.setup:
                self.setup.list_machines = parsed_args.list_machines

        if parsed_args.list_layers:
            if self.setup:
                self.setup.list_layers = True

        if parsed_args.list_recipes:
            if self.setup:
                self.setup.list_recipes = True

        if parsed_args.repo_no_fetch:
            if self.setup:
                self.setup.repo_no_fetch = True
            del parsed_args.repo_no_fetch


        if (parsed_args.list_distros or parsed_args.list_machines or parsed_args.list_layers or parsed_args.list_recipes):
            return

        # Parse layer selection options
        if parsed_args.distros:
            self.layer_select = True
            if self.setup:
                self.setup.distros = []
                for d in parsed_args.distros:
                    for distro in d.split(','):
                        self.setup.distros.append(distro)

        if parsed_args.machines:
            self.layer_select = True
            if self.setup:
                self.setup.machines = []
                for m in parsed_args.machines:
                    for machine in m.split(','):
                        self.setup.machines.append(machine)

        if parsed_args.layers:
            self.layer_select = True
            if self.setup:
                self.setup.layers = []
                for l in parsed_args.layers:
                    for layer in l.split(','):
                        if '://' in layer:
                            # check if layer has a valid url scheme
                            url = urlparse(layer)
                            if url.scheme and url.scheme != 'file':
                                remote_layer = {}
                                remote_layer['branch'] = 'master'
                                remote = layer.split('+')
                                remote_layer['url'] = remote[0]
                                remote_layer['path'] = 'layers/' + remote[0].split('/')[-1]
                                for arg in remote[1:]:
                                    if arg.startswith('branch='):
                                        remote_layer['branch'] = arg[7:]

                                self.setup.remote_layers.append(remote_layer)
                            else:
                                logger.warning("Skipping invalid remote url: %s" % (layer))
                        elif '/' in layer or os.path.exists(layer):
                            layer = os.path.realpath(layer)
                            if not os.path.isdir(layer):
                                logger.warning("Skipping invalid local layer %s" % (layer))
                            else:
                                self.setup.local_layers.append(layer)
                        else:
                            self.setup.layers.append(layer)

        if parsed_args.recipes:
            self.layer_select = True
            if self.setup:
                self.setup.recipes = []
                for r in parsed_args.recipes:
                    for recipe in r.split(','):
                        self.setup.recipes.append(recipe)

        if parsed_args.all_layers:
            self.layer_select = True
            if self.setup:
                self.setup.all_layers = parsed_args.all_layers

        if parsed_args.no_recommend:
            self.layer_select = True
            if self.setup:
                self.setup.no_recommend = parsed_args.no_recommend

        if (parsed_args.mirror):
            if self.layer_select is not True:
                print('ERROR: The --mirror option requires at least one Layer Section argument, see --help.')
                sys.exit(1)

            if self.setup:
                self.setup.mirror = parsed_args.mirror

        if parsed_args.mirror_as_premirrors:
            if self.setup:
                self.setup.mirror_as_premirrors = parsed_args.mirror_as_premirrors

        if parsed_args.use_layer_groups:
            if self.setup:
                self.setup.use_layer_groups = parsed_args.use_layer_groups

        if self.layer_select is not True:
            print('ERROR: You must include at least one Layer Selection argument, see --help.')
            sys.exit(1)

    def add_setup_options(self):
        # Setup options
        self.parser.add_argument('-v', '--verbose', help='Set the verbosity to debug', action="store_true")

        self.base_args = self.parser.add_argument_group('Base Settings')

        setup_base_url = ""
        if self.setup and self.setup.base_url:
            setup_base_url = '(default %s)' % (self.setup.base_url)
        self.base_args.add_argument('--base-url', metavar="URL", help='URL to fetch from %s' % (setup_base_url))

        setup_base_branch = ""
        if self.setup and self.setup.base_branch:
            setup_base_branch = '(default %s)' % (self.setup.base_branch)
        self.base_args.add_argument('--base-branch', metavar="BRANCH", help='Base branch identifier %s' % (setup_base_branch))

        self.parser.add_argument('--mirror', help='Do not construct a project, instead construct a mirror of the repositories that would have been used to construct a project (requires a Layer Selection argument)', action='store_true')
        self.parser.add_argument('-mp', '--mirror-as-premirrors', help="Make the dl layers as premirrors when --mirror is specified. Use the project mirror as PREMIRRORS during the build when --mirror is not specified", action='store_true', default=False)

        self.parser.add_argument('--no-prime', help='Control whether to download common objects before repo sync. Default: True', action="store_true")
 
        self.parser.add_argument('--use-buildtools-cert', help='Using SSL certificate included in buildtools', action="store_true")

    def add_repo_options(self):
        self.repo_args = self.parser.add_argument_group('repo Settings')
        # Repo options
        setup_jobs = ""
        self.repo_args.add_argument('-rv', '--repo-verbose', action='store_true', help='Disables use of --quiet with repo commands')
        if self.setup and self.setup.jobs:
            setup_jobs = '(default %s)' % (self.setup.jobs)
        self.repo_args.add_argument('-rj', '--repo-jobs', metavar='JOBS', type=int, help='Sets repo project to fetch simultaneously %s' % (setup_jobs))
        self.repo_args.add_argument('--repo-depth', metavar='DEPTH', type=int, help='Sets repo --depth; see repo init --help (note: if set, a value of >= 2 is required)')
        self.repo_args.add_argument('--repo-retry-fetches', metavar='RETRY', type=int, help='Set repo retry remote fetches times; see repo sync --help', default=5)
        self.repo_args.add_argument('--repo-force-sync', action='store_true', help='Sets repo --force-sync; see repo sync --help')
        self.repo_args.add_argument('--repo-no-fetch', help='Do all the setup but do not call repo sync', action="store_true")
        self.repo_args.add_argument('--repo-no-prune', help='When calling repo sync do not use --prune. May cause sync failures.', action="store_true")

        repo_url = ""
        if 'REPO_URL' in os.environ:
            repo_url = '(default %s)' % os.environ['REPO_URL']

        repo_rev = ""
        if 'REPO_REV' in os.environ:
            repo_rev = '(default %s)' % os.environ['REPO_REV']

        self.repo_args.add_argument('--repo-url', metavar="URL", help='Url for git-repo %s' % (repo_url))
        self.repo_args.add_argument('--repo-branch', metavar="REV", help='Url for git-repo %s' % (repo_rev))


    def add_list_options(self):
        self.list_args = self.parser.add_argument_group('Layer Listings')
        # List options
        self.list_args.add_argument('--list-distros',   metavar='all', nargs='?', const='default', help='List available distro values')
        self.list_args.add_argument('--list-machines',  metavar='all', nargs='?', const='default', help='List available machine values')
        self.list_args.add_argument('--list-layers',    action='store_true', help='List all available layers')
        self.list_args.add_argument('--list-recipes',   action='store_true', help='List all available recipes')

    def add_layer_options(self):
        self.layer_args = self.parser.add_argument_group('Layer Selection')

        # Layer selection and local.conf setup
        setup_distro = ""
        setup_distro_str = ""
        if self.setup and self.setup.distros:
            setup_distro = self.setup.distros[0]
            setup_distro_str = '(default %s)' % setup_distro
        self.layer_args.add_argument('--distros', metavar='DISTRO', help='Select layer(s) based on required distribution and set the default DISTRO= value %s' % setup_distro_str, nargs="+")

        setup_machine = ""
        setup_machine_str = ""
        if self.setup and self.setup.machines:
            setup_machine = self.setup.machines[0]
            setup_machine_str = '(default %s)' % setup_machine
        self.layer_args.add_argument('--machines', metavar='MACHINE', help='Select layer(s) based on required machine(s) and set the default MACHINE= value %s' % setup_machine_str, nargs='+')

        self.layer_args.add_argument('--layers', metavar='LAYER', help='Select layer(s) to include in the project and add to the default bblayers.conf. Can accept the name of a layer in the layerindex, a path to a layer on local storage or a remote url that will be cloned by git-repo. (<name>|<path>|<scheme>://<url>/<repo>(+branch=<branch>)) ', nargs='+')
        self.layer_args.add_argument('--recipes', metavar='RECIPE', help='Select layers(s) based on recipe(s)', nargs='+')
        self.layer_args.add_argument('--all-layers', help='Select all available layers', action='store_true')
        self.layer_args.add_argument('--no-recommend', help='Disable recommended layers during layer resolution', action='store_true')
        self.layer_args.add_argument('--use-layer-groups', metavar='EXTRA_GROUP', help="Specify extra layer groups to use. Make sure you have permissions to access these groups before you use it.", action='store', nargs='+', choices=self.extra_group_keys)

    def add_other_options(self):
        pass

    def add_options(self):
        self.add_setup_options()
        self.add_repo_options()
        self.add_list_options()
        self.add_layer_options()
        self.add_other_options()
