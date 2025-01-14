#!/usr/bin/env python3

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

# Please keep these sorted.
import errno
import logging
import os
import shutil
import subprocess
import sys
import time
import re
import json

from urllib.parse import urlparse

import utils_setup

# Setup-specific modules
import logger_setup
from argparse_wrl import Argparse_Wrl

from layer_index import Layer_Index

import settings
import sanity
import filecmp

import xml.etree.ElementTree as ET

logger = logger_setup.setup_logging()

# Redirect stdout and stderr to the custom logger.  This allows us to use
# python modules that may output only via stdout/stderr.
isatty = sys.stdout.isatty()
sys.stdout = logger_setup.LoggerOut(logger.info, isatty)
sys.stderr = logger_setup.LoggerOut(logger.error, isatty)

# Last shared object between linux-yocto and linux-yocto-dev currently is...
KERNEL_MERGE_BASE="v5.13"

class Setup():

    tool_list = ['repo', 'git']

    default_xml = 'default.xml'
    default_repo_quiet = '--quiet'

    class_config_dir = 'config'
    class_log_dir = 'log'

    check_repo_install_dir = '.repo/repo/.git'
    check_repo_sync_file = '.repo/projects/'

    replacement = {}

    BINTOOLS_SSL_DIR="/bin/buildtools/sysroots/x86_64-wrlinuxsdk-linux/usr/share/ca-certificates/mozilla"
    BINTOOLS_SSL_CERT= "/bin/buildtools/sysroots/x86_64-wrlinuxsdk-linux/etc/ssl/certs/ca-certificates.crt"

    def __init__(self):
        # Set various default values
        # Default -j for repo init
        self.jobs = str(settings.REPO_JOBS)

        # Pull in the defaults from the environment (set by setup.sh)
        self.base_url = os.getenv('OE_BASEURL')
        self.base_branch = os.getenv('OE_BASEBRANCH')
        self.buildtools_branch = os.getenv('OE_BUILDTOOLS_BRANCH')
        self.buildtools_remote = os.getenv('OE_BUILDTOOLS_REMOTE')
        self.another_buildtools_remote = os.getenv('OE_ANOTHER_BUILDTOOLS_REMOTE')

        # Real project or a mirror?
        self.mirror = False

        self.mirror_index_path = None

        # Make/Use the project mirror as PREMIRRORS for do_fetch
        self.mirror_as_premirrors = False

        # Default configuration
        self.distros = [ settings.DEFAULT_DISTRO ]
        self.machines = [ settings.DEFAULT_MACHINE ]
        self.layers = []
        # The dl layers specified by --layers
        self.specified_dl_layers = set()
        self.recipes = []
        self.wrtemplates = []

        self.processed_dl_layers = set()

        self.all_layers = False
        self.dl_layers = -1
        self.local_layers = []
        self.remote_layers = []

        # The extra layer groups enabled by user
        self.use_layer_groups = []

        self.no_recommend = False

        self.no_network = False
        self.allowed_network = None

        self.remotes = {}
        self.requiredlayers = []
        self.recommendedlayers = []

        # Default quiet:
        self.quiet = self.default_repo_quiet
        self.repo_verbose = False

        # Default depth
        self.depth = None

        # Default retry_fetches
        self.retry_fetches = None

        # Default to NOT force-sync
        self.force_sync = None

        self.repo_url = None
        if 'REPO_URL' in os.environ:
            self.repo_url = os.environ['REPO_URL']

        self.repo_rev = None
        if 'REPO_REV' in os.environ:
            self.repo_rev = os.environ['REPO_REV']

        self.debug_lvl = 0

        self.repo_no_fetch = False

        # Default to always prune. This prevents sync failures when upstream
        # moves tags or deletes branch foo and creates foo/bar
        self.prune = True

        self.prime = True

        # Set the install_dir
        # Use the path from this file.  Note bin has to be dropped.
        self.install_dir = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + '/../')

        # Default location for the related XML files
        self.xml_dir = os.path.join(self.install_dir, 'data/xml')

        # wrtemplates config file
        self.wrtemplates_cfg = os.path.join(self.install_dir, 'data/json/wrtemplates.json')
        self.wrtemplates_cfg_dict = {}

        # dl layer recommends from json
        self.dl_layer_recommends = os.path.join(self.install_dir, 'data/json/dl_layer_recommends.json')
        self.dl_layer_recommends_dict = {}

        # Set the directory where we're running.
        self.project_dir = os.getcwd()

        self.conf_dir = os.path.join(self.project_dir, self.class_config_dir)

        # Save current base_branch to compare with next one
        self.saved_base_branch = os.path.join(self.conf_dir, 'saved_base_branch')

        # Environment setup
        self.env = os.environ.copy()

        # We do NOT want to inherit python home from the environment
        # See Issue: LIN1018-2934
        #   python3 wrapper from the buildtools sets this, which causes host
        #   python tools to fail
        if 'PYTHONHOME' in self.env:
            del self.env['PYTHONHOME']

        self.setup_env()

        # Config flags
        self.list_distros = False
        self.list_machines = None
        self.list_layers = False
        self.list_recipes = False
        self.list_wrtemplates = False

        self.premirrors_dl = os.path.join(self.project_dir, 'premirrors-dl')
        self.premirrors_dl_downloads = os.path.join(self.premirrors_dl, 'downloads')

        # The bare repo
        self.manifest_dir_bare = os.path.join(self.project_dir, '.repo/manifests.git')
        # The cloned repo
        self.manifest_dir = os.path.join(self.project_dir, '.repo/manifests')

        # The xml lines which will be written to default.xml
        self.xml_lines_out = []

    def exit(self, ret=0):
        logger.debug("setup.py finished (ret=%s)" % (ret))
        sys.exit(ret)

    def start_file_logging(self):
        log_dir = os.path.join(self.conf_dir, self.class_log_dir)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        log_file = '%s/%s.log' % (log_dir, time.strftime('%Y%m%d%H%M%S', time.localtime()))
        logger_setup.setup_logging_file(log_file)

        # Create symlink setup-latest.log
        loglink = os.path.join(os.path.dirname(log_file), 'setup-latest.log')
        try:
            os.unlink(loglink)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        os.symlink(os.path.basename(log_file), loglink)

    def main(self, orig_args):
        parser = Argparse_Wrl(self)
        # We want to default to help mode lacking any args.
        if not orig_args or not orig_args[1:]:
            orig_args.append('--help')
        parser.evaluate_args(orig_args[1:])
        self.setup_args = " ".join(orig_args[1:])
        self.extra_group_keys = parser.extra_group_keys

        self.start_file_logging()

        logger.debug('REPO_URL = %s' % self.repo_url)
        logger.debug('REPO_BRANCH = %s' % self.repo_rev)

        if not self.base_url:
            logger.error('Unable to determine base url, you may need to specify --base-url=')

        if not self.base_branch:
            logger.error('Unable to determine base branch, you may need to specify --base-branch=')

        # Check for require host tools for real project
        if not self.mirror:
            sanity.check_hosttools(self.tool_list)

        if not self.base_url or not self.base_branch:
            self.exit(1)

        # Check for all the tools and create a dictionary of the path
        # This shouldn't fail, because sanity.check_hosttools already checked for these...
        self.tools = {i : self.get_path(i) for i in self.tool_list}
        if None in self.tools.values():
            sys.exit(1)

        self.load_layer_index()

        if len(self.index.index) == 0:
            logger.critical('No indexes could be loaded.  This could be due to an invalid branch or tag.  Exiting...')
            sys.exit(1)

        if self.list_distros:
            compat = self.list_distros
            if compat == 'default':
                compat = settings.DEFAULT_LAYER_COMPAT_TAG
            self.index.list_distros(self.base_branch, compat)

        if self.list_machines:
            compat = self.list_machines
            if compat == 'default':
                compat = settings.DEFAULT_LAYER_COMPAT_TAG
            self.index.list_machines(self.base_branch, compat)

        if self.list_layers:
            self.index.list_layers(self.base_branch)

        if self.list_recipes:
            self.index.list_recipes(self.base_branch)

        if self.list_wrtemplates:
            compat = self.list_wrtemplates
            if compat == 'default':
                compat = settings.DEFAULT_LAYER_COMPAT_TAG
            self.index.list_wrtemplates(self.base_branch, compat)

        if self.list_distros or self.list_machines or self.list_layers or self.list_recipes or self.list_wrtemplates:
            sys.exit(0)

        logger.debug('setup.py started')
        logger.debug('Calling setup main with arguments %s' % str(orig_args))

        # Log debug which may have been missed due to log level.
        logger.debug("PATH=%s" % self.env["PATH"])

        # Check saved_base_branch vs current base_branch
        self.check_base_branch()

        logger.debug("Tools are:")
        for key in self.tools:
            logger.debug("%s -> %s" % (key, self.tools[key]))

        logger.plain('Setting distro to "%s"' % (",".join(self.distros)))
        logger.plain('Setting machine to "%s"' % (",".join(self.machines)))
        if self.layers != []:
            logger.plain('Setting layers to "%s"' % (",".join(self.layers)))
        if self.recipes != []:
            logger.plain('Setting recipes to "%s"' % (",".join(self.recipes)))
        if self.wrtemplates != []:
            logger.plain('Setting templates to "%s"' % (",".join(self.wrtemplates)))

        self.process_layers()

        self.project_setup()

        self.__prep_replacements()

        if self.mirror:
            if self.dl_layers not in (0, -1):
                logger.warning("clone-depth is ignored since --mirror is used")

            # Setup an index for others to use if we're mirroring...
            self.update_mirror()
            self.update_mirror_index()

        self.update_manifest()

        # We only want to do this if we're not mirroring, this must be run
        # after update_manifest() to make sure dl layers are added to 'LAYERS'
        if self.mirror != True:
            self.update_project()

        self.check_default_xml()

        self.update_gitignore()

        self.commit_files()

        self.check_project_path()

        self.repo_sync()

        if self.mirror != True:
            self.update_symlinks()

        if self.mirror_as_premirrors:
            if self.mirror:
                self.make_mirror_as_premirrors()
            else:
                self.use_mirror_as_premirrors()

        self.exit(0)

    def check_project_path(self):
        project_dir_last = ""
        if os.path.exists(self.manifest_dir_bare):
            cmd = [self.tools['git'], 'config', '--get', 'remote.origin.url']
            try:
                p = subprocess.run(cmd, check=True, cwd=self.manifest_dir_bare, stdout=subprocess.PIPE)
                project_dir_last = p.stdout.decode('utf-8').strip()
            except Exception as e:
                logger.warning('Failed to run "%s": %s' % (' '.join(cmd), e))

        logger.debug('last project dir: %s' % project_dir_last)
        if project_dir_last and project_dir_last != self.project_dir:
            logger.info('project dir has been changed from %s to %s' % (project_dir_last, self.project_dir))
            logger.info('Updating config files for new project dir...')
            cmd = [self.tools['git'], 'config', 'remote.origin.url', self.project_dir]
            subprocess.run(cmd, cwd=self.manifest_dir_bare)

    def load_mirror_index(self, remote_mirror, folder=""):
        # See if there is a mirror index available from the BASE_URL
        mirror_index = os.path.join(self.conf_dir, 'mirror-index')
        try:
            cmd = [self.tools['git'], 'ls-remote', remote_mirror, self.base_branch]
            utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=self.project_dir, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except:
            try:
                remote_mirror += "/.git"
                cmd = [self.tools['git'], 'ls-remote', remote_mirror, self.base_branch]
                utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=self.project_dir, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            except:
                # No mirror, return
                return None

        logger.plain('Loading the mirror index from %s (%s)...' % (remote_mirror, self.base_branch))
        # This MIGHT be a valid mirror..
        if not os.path.exists(mirror_index):
            os.makedirs(mirror_index)
            cmd = [self.tools['git'], 'init' ]
            utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=mirror_index)

        try:
            # We don't know if we're fetching a branch or tag, if it's a tag we have to do this
            # in two steps anyway, so always go to 'FETCH_HEAD' and then branch it.
            cmd = [self.tools['git'], 'fetch', '-n', '-u', remote_mirror, self.base_branch]
            utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=mirror_index)
            cmd = [self.tools['git'], 'checkout', '-B', folder + self.base_branch, 'FETCH_HEAD']
            utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=mirror_index)
        except:
            # Could not fetch, return
            return None

        logger.debug('Found mirrored index.')
        cmd = [self.tools['git'], 'checkout', folder + self.base_branch ]
        utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=mirror_index)
        cmd = [self.tools['git'], 'reset', '--hard' ]
        utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=mirror_index)

        return mirror_index

    def check_base_branch(self):
        logger.debug('Checking saved_base_branch vs current base_branch')

        sync_file = os.path.join(self.project_dir, self.check_repo_sync_file)
        if not os.path.exists(sync_file):
            logger.debug("Skip the check since %s doesn't exist" % sync_file)
            return

        if not os.path.exists(self.saved_base_branch):
            logger.debug("Skip the check since %s doesn't exist" % self.saved_base_branch)
            return

        with open(self.saved_base_branch, 'r') as f:
            saved_base_branch = f.read().rstrip('\n')
        if saved_base_branch == self.base_branch:
            logger.debug('saved_base_branch and current base_branch are the same')
            return

        repo = self.tools['repo']
        cmd = [repo, 'status']
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, env=self.env, cwd=self.project_dir) as proc:
            output = proc.stdout.read().decode('utf-8')
            if re.search(r'^project\s+layers/.*\s+branch', output, flags=re.M):
                logger.error("Found checked out branches, here is the output from 'repo status':\n%s" % output)
                logger.error('Switching base branch with checked out projects is not allowed,')
                logger.error('but you are switching from "%s" to "%s".'% (saved_base_branch, self.base_branch))
                logger.error('Consider using either of the following ways to fix the problem:')
                logger.error('- Run "git checkout m/master" in the projects')
                logger.error('- Use "repo abandon [--all | <branchname>] [<project>...]" to abandon the branch\n')
                self.exit(1)

    def load_layer_index(self):
        # Load Layer_Index

        if not (self.base_branch == "master" or self.base_branch == "master-wr"):
            from windshare import Windshare
            ws = Windshare(debug=self.debug_lvl)

            # Determine if this is a windshare install
            (ws_base_url, ws_base_folder, ws_entitlement_url) = ws.get_windshare_urls(self.base_url)
            if ws_base_url and ws_base_url != "" and ws.load_folders(ws_entitlement_url):
                logger.plain('Detected Windshare configuration.  Processing entitlements and indexes.')

                for folder in ws.folders:
                    self.mirror_index_path = ws.load_mirror_index(self, ws_base_url, folder)

                ws.write_local_mirror_index(self, self.mirror_index_path)

                # We need to adjust the base_url so everything works properly...
                self.base_url = ws_base_url

                # Adjust the location of the buildtools (was based on the original base_url)
                if self.buildtools_remote:
                    self.buildtools_remote = ws_base_folder + '/' + self.buildtools_remote
                if self.another_buildtools_remote:
                    self.another_buildtools_remote = ws_base_folder + '/' + self.another_buildtools_remote
            else:
                logger.debug('No Windshare configuration detected.')
        else:
            logger.debug('Windshare configuration disabled, building %s.' % self.base_branch)

        # Check if we have a mirror-index, and load it if we do...
        if not self.mirror_index_path:
            self.mirror_index_path = self.load_mirror_index(self.base_url + '/mirror-index')

            # Is this a tag? if so... we only pull from mirror-indexes
            if not self.mirror_index_path and self.base_branch.startswith('refs/tags/'):
                logger.error("An install from a repository tag (%s) can only be installed with a corresponding layerindex snapshot." % self.base_branch)
                logger.error("Unable to find %s" % self.base_url + '/mirror-index')
                sys.exit(1)

        # Mirror also has a copy of the associated XML bits
        if self.mirror_index_path:
            self.xml_dir = os.path.join(self.mirror_index_path, 'xml')

        # Setup replace strings as late as possible.  The various self.* values
        # may be modified prior to this place.
        replace = []
        replace = replace + settings.REPLACE
        replace = replace + [
                   ( '#INSTALL_DIR#', self.install_dir ),
                   ( '#BASE_URL#', self.base_url ),
                   ( '#BASE_BRANCH#', self.base_branch ),
                  ]

        self.index = Layer_Index(indexcfg=settings.INDEXES, base_branch=self.base_branch, replace=replace, mirror=self.mirror_index_path)

        # Is this a Wind River tag? if so... we need to modify the 'branches' entries to be the same as the tag
        if self.base_branch.startswith('refs/tags/vWRLINUX'):
            if '_RCPL' in self.base_branch or '_UPDATE' in self.base_branch:
                # vWRLINUX_<something>_UPDATExxxx
                # vWRLINUX_<something>_RCPLxxxx
                new_base_branch = self.base_branch[11:]
            else:
                # vWRLINUX_<something>_10.19.25.0
                new_base_branch_pos = self.base_branch.find('10.')
                new_base_branch = self.base_branch[:(new_base_branch_pos-1)][11:]

            logger.debug('Detected a WR tag, replacing %s with %s...' % (self.base_branch[11:], new_base_branch))
            for lindex in self.index.index:
                for branch in lindex['branches']:
                    if branch and branch['bitbake_branch'] == new_base_branch:
                        branch['bitbake_branch'] = self.base_branch
                    if branch and branch['name'] == new_base_branch:
                        branch['name'] = self.base_branch

    def is_group_layer(self, layer_name):
        """
        Is it an extra group layer?
        """
        for group_key in self.extra_group_keys:
            if '-%s-' % group_key in layer_name or layer_name.endswith('-%s' % group_key):
                return True

        return False

    def is_enabled_group(self, layer_name):
        """
        Is it an extra group layer which is enabled by user?
        """
        if layer_name in self.layers:
            return True

        for group_key in self.extra_group_keys:
            if group_key in self.use_layer_groups:
                return True

        return False

    def process_layers(self):
        from collections import deque

        # We allow duplicates in the queue, they're filtered later
        # Queue of required objects
        requiredQueue = deque([])
        # Queue of recommended objects
        recommendedQueue = deque([])

        logger.debug('Starting')
        # if this switches to false, we have to exit at the end of this function
        allfound = True

        # It all starts with BASE_LAYERS, so always include this. (only from index 0)
        lindex = self.index.index[0]
        branchid = self.index.getBranchId(lindex, self.get_branch(lindex=lindex))
        if branchid:
            for lname in settings.BASE_LAYERS.split():
                base_layerBranch = self.index.getLayerBranch(lindex, branchid, name=lname)

                if not base_layerBranch or not branchid:
                    raise Exception('Unable to find base layer: %s in the Layer_Index' % (lname))

                for lb in base_layerBranch:
                    requiredQueue.append( (lindex, lb) )

        # process the configuration arguments (find the layers we need for the project)
        # if an item is 'layer:item', then the 'foo' part must match a layer name.
        def procConfig(layer=None, distro=None, machine=None, recipe=None, wrtemplate=None):
            item = ["", layer][layer != None]
            item = item + ["", distro][distro != None]
            item = item + ["", machine][machine != None]
            item = item + ["", recipe][recipe != None]
            item = item + ["", wrtemplate][wrtemplate != None]

            type = ["", 'layer'][layer != None]
            type = type + ["", 'distro'][distro != None]
            type = type + ["", 'machine'][machine != None]
            type = type + ["", 'recipe'][recipe != None]
            type = type + ["", 'template'][wrtemplate != None]

            if (':' in item):
                # User told us which layer, so ignore the other bits -- they can be used later...
                layer = item.split(':')[0]
                distro = None
                machine = None
                recipe = None
                wrtemplate = None

            if layer and utils_setup.is_dl_layer(layer):
                xmlfile = os.path.join(self.xml_dir, '%s.xml' % layer)
                if os.path.exists(xmlfile):
                    self.specified_dl_layers.add(layer)
                    return True
            # TODO: We do not actually verify the item we asked for (if a layer was specified) is available
            found = False
            for lindex in self.index.index:
                branchid = self.index.getBranchId(lindex, self.get_branch(lindex=lindex))
                if not branchid:
                    continue
                for layerBranch in self.index.getLayerBranch(lindex, branchid, name=layer, distro=distro, machine=machine, recipe=recipe, wrtemplate=wrtemplate) or []:
                    requiredQueue.append( (lindex, layerBranch) )
                    found = True
                if found:
                    break

            if not found:
                return False

            return True

        for l in self.layers:
            if not procConfig(layer=l):
                allfound = False

        for l in self.distros:
            if not procConfig(distro=l):
                allfound = False

        for l in self.machines:
            if not procConfig(machine=l):
                if self.local_layers or self.remote_layers:
                    logger.info("Machine %s not found but validation disabled due to local or remote layers specified.", l)
                else:
                    allfound = False
            else:
                if l not in self.index.get_machines(self.base_branch, settings.DEFAULT_LAYER_COMPAT_TAG):
                    logger.critical('Unsupported machine: %s' % l)
                    self.exit(1)

        for l in self.recipes:
            if not procConfig(recipe=l):
                allfound = False

        if self.wrtemplates != []:
            with open(self.wrtemplates_cfg, 'r') as fl:
                self.wrtemplates_cfg_dict = json.load(fl)

        def handle_wrtemplates_dependenies(wrtemplate, wrtemplates_cfg_dict):
            allfound = True
            for template in wrtemplates_cfg_dict["wrtemplates"]:
                if template["name"] == wrtemplate:
                    if "layer_dependencies" in template:
                        for d in template["layer_dependencies"]:
                            logger.debug("Checking depend layer %s for template %s" % (d, wrtemplate))
                            if not procConfig(layer=d):
                                allfound = False
                    if "require" in template:
                        for r in template["require"]:
                            logger.debug("Checking require template %s for template %s" % (r, wrtemplate))
                            if not handle_wrtemplates_dependenies(r, wrtemplates_cfg_dict):
                                allfound = False
                    break

            return allfound

        for l in self.wrtemplates:
            if not procConfig(wrtemplate=l):
                allfound = False
            if not handle_wrtemplates_dependenies(l, self.wrtemplates_cfg_dict):
                allfound = False

        if not allfound:
            logger.critical('Please correct the missing items, exiting.')
            self.exit(1)

        # Add all layers -- if necessary
        if self.all_layers == True:
            for lindex in self.index.index:
                branchid = self.index.getBranchId(lindex, self.get_branch(lindex=lindex))
                if not branchid:
                    continue
                for l in lindex['layerItems']:
                    l_name = l['name']
                    if self.is_group_layer(l_name) and not self.is_enabled_group(l_name):
                        logger.info('Skipping extra group layer %s' % l_name)
                        continue

                    for layerBranch in self.index.getLayerBranch(lindex, branchid, layerItem=l) or []:
                        # Skip the download layer which will be handled by process_dl_layer()
                        if not utils_setup.is_dl_layer(l_name):
                            requiredQueue.append( (lindex, layerBranch) )

        # Compute requires and recommended layers...

        # List of 'collection' and layer 'name'.  This list is used to avoid
        # including duplicates.  Collection is matched first, then name -- as not
        # all layer indexes may contain 'collection'
        depCacheCol = []
        depCacheName = []

        def checkCache(lindex, layerBranch, addCache=False):
            (collection, name, vcs_url) = self.index.getLayerInfo(lindex, layerBranch=layerBranch)

            if collection in depCacheCol or name in depCacheName:
                return True

            if addCache:
                if collection:
                    depCacheCol.append(collection)
                if name:
                    depCacheName.append(name)
            return False

        def resolveIndexOrder(lindex, layerBranch, Queue):
            # We want to recompute the dependency in INDEXES order...
            (collection, name, vcs_url) = self.index.getLayerInfo(lindex, layerBranch)
            found = False
            for pindex in self.index.index:
                # We already know it'll be in this index, so we just use it as-is...
                if pindex == lindex:
                    break

                # Look for the collection (or name if no collection) in the indexes in
                # priority order...
                pbranchid = self.index.getBranchId(pindex, self.get_branch(lindex=pindex))
                if collection:
                    new_layerBranches = self.index.getLayerBranch(pindex, pbranchid, collection=collection)
                    if new_layerBranches and new_layerBranches != []:
                        for lb in new_layerBranches:
                            logger.info('Resolving dependency %s from %s to %s from %s' % (name, lindex['CFG']['DESCRIPTION'], name, pindex['CFG']['DESCRIPTION']))
                            Queue.append( (pindex, lb) )
                        lindex = None
                        layerBranch = None
                        break

                if name:
                    new_layerBranches = self.index.getLayerBranch(pindex, pbranchid, name=name)
                    if new_layerBranches and new_layerBranches != []:
                        for lb in new_layerBranches:
                            logger.info('Resolving dependency %s from %s to %s from %s' % (name, lindex['CFG']['DESCRIPTION'], name, pindex['CFG']['DESCRIPTION']))
                            Queue.append( (pindex, lb) )
                        lindex = None
                        layerBranch = None
                        break

            return (lindex, layerBranch)

        while requiredQueue:
            (lindex, layerBranch) = requiredQueue.popleft()

            (lindex, layerBranch) = resolveIndexOrder(lindex, layerBranch, requiredQueue)

            if not lindex or not layerBranch:
                continue

            if not checkCache(lindex, layerBranch, True):
                self.requiredlayers.append( (lindex, layerBranch) )
                (required, recommended) = self.index.getDependencies(lindex, layerBranch)
                for dep in required:
                    requiredQueue.append( (lindex, dep) )

                for dep in recommended:
                    recommendedQueue.append( (lindex, dep) )

        while recommendedQueue:
            (lindex, layerBranch) = recommendedQueue.popleft()

            (lindex, layerBranch) = resolveIndexOrder(lindex, layerBranch, recommendedQueue)

            if not lindex or not layerBranch:
                continue

            # Skip the download layer which will be handled by process_dl_layer()
            layers = self.index.find_layer(lindex, id=layerBranch['layer'])
            if layers and utils_setup.is_dl_layer(layers[0]['name']):
                continue

            if not checkCache(lindex, layerBranch, True):
                self.recommendedlayers.append( (lindex, layerBranch) )
                (required, recommended) = self.index.getDependencies(lindex, layerBranch)
                for dep in required + recommended:
                    recommendedQueue.append( (lindex, dep) )

        unexpected_groups = []
        for (lindex, layerBranch) in self.requiredlayers + self.recommendedlayers:
            for layer in self.index.find_layer(lindex, id=layerBranch['layer']):
                l_name = layer['name']
                if self.is_group_layer(l_name) and not self.is_enabled_group(l_name):
                    if not l_name in unexpected_groups:
                        unexpected_groups.append(l_name)
        unexpected_groups.sort()

        if unexpected_groups:
            logger.error('The following layer(s) need --use-layer-group=<group> or --layers=<layer>:\n%s' % '\n'.join(unexpected_groups))
            logger.error('Try to rerun with one of the options?')
            self.exit(1)

        # Also compute the various remotes
        self.remotes['base'] = self.base_url

        def process_remote(lindex, layerBranch):
            for layer in self.index.find_layer(lindex, id=layerBranch['layer']):
                add_remote_entry(layer['vcs_url'])

            for remote_layer in self.remote_layers:
                add_remote_entry(remote_layer.get('url'))

        def add_remote_entry(vcs_url):
            found = False
            for remote in self.remotes:
                if vcs_url.startswith(self.remotes[remote]):
                    found = True
                    break
            if not found:
                url = urlparse(vcs_url)
                if not url.scheme:
                    self.remotes['local'] = '/'
                    found = True

                if not found:
                    for (remoteurl, remotename) in settings.REMOTES:
                        if vcs_url.startswith(remoteurl):
                            self.remotes[remotename] = remoteurl
                            found = True
                            break

                if not found:
                    self.remotes[url.scheme + '_' + url.netloc.translate(str.maketrans('/:', '__'))] = url.scheme + '://' + url.netloc

        for (lindex, layerBranch) in self.requiredlayers + self.recommendedlayers:
            process_remote(lindex, layerBranch)

        def display_layer(lindex, layerBranch):
            branchid = self.index.getBranchId(lindex, self.get_branch(lindex=lindex))

            for layer in self.index.find_layer(lindex, id=layerBranch['layer']):
                vcs_url = layer['vcs_url']

                path = 'layers/' + "".join(vcs_url.split('/')[-1:])

                if (layer['name'] == 'openembedded-core'):
                    bitbakeBranch = self.index.getBranch(lindex, layerBranch['branch'])['bitbake_branch']
                    logger.debug('bitbake: %s %s %s' % ( settings.BITBAKE, path + '/bitbake', bitbakeBranch ))

                actual_branch = layerBranch['actual_branch'] or self.index.getBranch(lindex, branchid)['name']
                logger.debug('%s: %s %s %s' % (layer['name'], vcs_url, path, actual_branch ))


        logger.debug('Computed required layers:')
        for (lindex, layerBranch) in self.requiredlayers:
            display_layer(lindex, layerBranch)

        logger.debug('Computed recommended layers:%s' % (["", ' (skipping)'][self.no_recommend == True]))
        for (lindex, layerBranch) in self.recommendedlayers:
            display_layer(lindex, layerBranch)

        # Recommends are disabled, filter it...
        if self.no_recommend == True:
            if self.dl_layers != -1:
                newRecommendedlayers = []
                for (lindex, layerBranch) in self.recommendedlayers:
                    layers = self.index.find_layer(lindex, id=layerBranch['layer'])
                    if layers and ('-dl-' in layers[0]['name'] or layers[0]['name'].endswith('-dl')):
                        newRecommendedlayers.append( (lindex, layerBranch) )
                self.recommendedlayers = newRecommendedlayers
            else:
                self.recommendedlayers = []

        # if any of the remote layers have the same path as one of the
        # layerindex layers override the vcs_url and
        # actual_branch. Keep a list of remote layers that were not
        # overrides so the final list of remote layers will have them
        # removed. Cannot modify a list while iterating over it
        additional_remote_layers = []
        for remote_layer in self.remote_layers:
            found = False
            for (lindex, layerBranch) in self.requiredlayers + self.recommendedlayers:
                for layer in self.index.find_layer(lindex, id=layerBranch['layer']):
                    vcs_url = layer['vcs_url']
                    path = 'layers/' + "".join(vcs_url.split('/')[-1:])
                    if remote_layer.get('path') == path:
                        layer['vcs_url'] = remote_layer.get('url')
                        layerBranch['actual_branch'] = remote_layer.get('branch')
                        found = True
                        break
                if found:
                    break

            if not found and remote_layer not in additional_remote_layers:
                additional_remote_layers.append(remote_layer)

        # remote layers with the overrides removed
        self.remote_layers = additional_remote_layers

        logger.debug('Done')

    def project_setup(self):
        logger.debug('Starting')
        self.__setup_local_layer()

        if self.mirror != True:
            # We need to make sure the environment-setup link is always current
            for (dirpath, dirnames, filenames) in os.walk(os.path.join(self.project_dir, 'bin/buildtools')):
                for filename in filenames:
                    if filename.startswith('environment-setup-'):
                        src = os.path.relpath(os.path.join(dirpath, filename), self.project_dir)
                        dst = os.path.join(self.project_dir, filename)
                        if os.path.islink(dst):
                            os.unlink(dst)
                        os.symlink(src, dst)

        logger.debug('Done')

    def update_project(self):
        logger.debug('Starting')
        if not os.path.exists(self.project_dir + '/.templateconf'):
            tmplconf = open(self.project_dir + '/.templateconf', 'w')
            tmplconf.write('# Project template settings\n')
            tmplconf.write('TEMPLATECONF=${TEMPLATECONF:-$OEROOT/config}\n')
            tmplconf.close()

        self.copySample(self.install_dir + '/data/samples/README.sample', self.project_dir + '/README')
        self.copySample(self.install_dir + '/data/samples/bblayers.conf.sample', self.project_dir + '/config/bblayers.conf.sample')
        self.copySample(self.install_dir + '/data/samples/conf-notes.sample', self.project_dir + '/config/conf-notes.txt')
        self.copySample(self.install_dir + '/data/samples/local.conf.sample', self.project_dir + '/config/local.conf.sample')

        if os.path.exists(self.install_dir + '/data/samples/site.conf.sample'):
            self.copySample(self.install_dir + '/data/samples/site.conf.sample', self.project_dir + '/config/site.conf.sample')
        with open(self.saved_base_branch, 'w') as f:
            f.write('%s\n' % self.base_branch)

    def update_mirror(self):
        self.copySample(self.install_dir + '/data/samples/README-MIRROR.sample', self.project_dir + '/README')

    def __prep_replacements(self):
        self.replacement['layers'] = []
        self.replacement['machines'] = {}
        self.replacement['distros'] = {}

        def addLayer(lindex, layerBranch):
            branchid = self.index.getBranchId(lindex, self.get_branch(lindex=lindex))

            paths = []
            for layer in self.index.find_layer(lindex, id=layerBranch['layer']):
                vcs_url = layer['vcs_url']

                path = 'layers/' + "".join(vcs_url.split('/')[-1:])
                if layerBranch['vcs_subdir']:
                    path += '/' + layerBranch['vcs_subdir']

                paths.append(path)

            return paths

        # Add layers to 'LAYERS'
        for (lindex, layerBranch) in self.requiredlayers + self.recommendedlayers:
            self.replacement['layers'] = self.replacement['layers'] + addLayer(lindex, layerBranch)

        # Add machines to 'MACHINES'
        for (lindex, layerBranch) in self.requiredlayers + self.recommendedlayers:
            for machine in lindex['machines']:
                if machine['layerbranch'] == layerBranch['id']:
                    desc = machine['description'] or machine['name']
                    self.replacement['machines'][machine['name']] = desc

        # Add distro to 'DISTROS'
        for (lindex, layerBranch) in self.requiredlayers + self.recommendedlayers:
            for distro in lindex['distros']:
                if distro['layerbranch'] == layerBranch['id']:
                    desc = distro['description'] or distro['name']
                    self.replacement['distros'][distro['name']] = desc



    def copySample(self, src, dst):
        src = open(src, 'r')
        dst = open(dst, 'w')

        wrlinux_src_dl_added = False
        for line in src:
            if '####LAYERS####' in line:
                for l in self.replacement['layers']:
                    # Check and replace dl layers with wrlinux-src-dl
                    layername = os.path.basename(l)
                    if utils_setup.is_dl_layer(layername):
                        if not wrlinux_src_dl_added:
                            dst.write(line.replace('####LAYERS####', '##OEROOT##/%s' % ('layers/wrlinux-src-dl')))
                            wrlinux_src_dl_added = True
                    else:
                        dst.write(line.replace('####LAYERS####', '##OEROOT##/%s' % (l)))
                for rl in self.remote_layers:
                    dst.write(line.replace('####LAYERS####', '##OEROOT##/%s' % (rl.get('path'))))
                for ll in self.local_layers:
                    dst.write(line.replace('####LAYERS####', '%s' % (ll)))
                continue
            if '####SETUP_ARGS####' in line:
                dst.write(line.replace('####SETUP_ARGS####', self.setup_args))
                continue
            if '####MACHINES####' in line:
                for (name, desc) in sorted(self.replacement['machines'].items(), key=lambda t: t[0]):
                    dst.write('# %s\n' % desc.strip())
                    dst.write(line.replace('####MACHINES####', name))
                continue
            if '####DEFAULTMACHINE####' in line:
                name = self.machines[0]
                if ':' in name:
                    name = ':'.join(name.split(':')[1:])
                dst.write(line.replace('####DEFAULTMACHINE####', name))
                continue
            if '####DISTROS####' in line:
                for (name, desc) in sorted(self.replacement['distros'].items(), key=lambda t: t[0]):
                    dst.write('# %s\n' % desc.strip())
                    dst.write(line.replace('####DISTROS####', name))
                continue
            if '####DEFAULTDISTRO####' in line:
                name = self.distros[0]
                if ':' in name:
                    name = ':'.join(name.split(':')[1:])
                dst.write(line.replace('####DEFAULTDISTRO####', name))
                continue
            if '####DEFAULTWRTEMPLATE####' in line:
                dst.write(line.replace('####DEFAULTWRTEMPLATE####', ' '.join(self.wrtemplates)))
                continue

            if '####BB_NO_NETWORK####' in line:
                # Enable network download when --dl-layers is not specified
                if self.dl_layers == -1:
                    bb_no_network = "0"
                else:
                    bb_no_network = "1"
                logger.plain('Setting BB_NO_NETWORK to "%s"' % bb_no_network)
                dst.write(line.replace('####BB_NO_NETWORK####', bb_no_network))
                continue

            dst.write(line)

        src.close()
        dst.close()

    def get_dl_layers_from_json(self, layername):
        if not self.dl_layer_recommends_dict:
            with open(self.dl_layer_recommends, 'r') as f:
                self.dl_layer_recommends_dict = json.load(f)

        if layername in self.dl_layer_recommends_dict.keys():
            return self.dl_layer_recommends_dict[layername]

        return []

    def update_mirror_index(self):
        logger.debug('Starting')
        path = os.path.join(self.project_dir, 'mirror-index')

        logger.plain('Exporting mirror-index %s...' % (path))
        if not os.path.exists(path):
            cmd = [self.tools['git'], 'init', path]
            utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=self.project_dir)

        try:
            cmd = [self.tools['git'], 'checkout', '-b', self.base_branch]
            utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=path)
        except:
            # if we failed, then simply try to switch branches
            cmd = [self.tools['git'], 'checkout', self.base_branch]
            utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=path)

        # Make sure the directory is empty, use -f to ignore failures
        for (dirpath, dirnames, filenames) in os.walk(path):
            if dirpath.endswith('/.git') or path + '/.git' in dirpath:
                continue
            for filename in filenames:
                os.remove(os.path.join(dirpath, filename))

        # Construct a list of all layers we've downloaded, by url, including sublayers not activated
        url_cache = {}
        for (lindex, layerBranch) in self.requiredlayers + self.recommendedlayers:
            for layer in self.index.find_layer(lindex, id=layerBranch['layer']):
                vcs_url = layer['vcs_url']
                if not vcs_url in url_cache:
                    url_cache[vcs_url] = []
                url_cache[vcs_url].append((lindex, layerBranch['branch']))

        # Serialize the information for each of the layers (and their sublayers)
        extra_dl_xmls = set()
        for vcs_url in url_cache:
            for (lindex, branchid) in url_cache[vcs_url]:
                for layer in lindex['layerItems']:
                    if layer['vcs_url'] in url_cache:
                        for lb in self.index.getLayerBranch(lindex, branchid=branchid, layerItem=layer):
                            self.index.serialize_index(lindex, os.path.join(path, lindex['CFG']['DESCRIPTION']), split=True, layerBranches=[lb], IncludeCFG=True, mirror=True, base_url=self.base_url)
                        name = layer['name']
                        destdir = os.path.join(path, 'xml')
                        srcfile = os.path.join(self.xml_dir, '%s.inc' % (name))
                        if os.path.exists(srcfile):
                            os.makedirs(destdir, exist_ok=True)
                            shutil.copy(srcfile, destdir)
                        srcfile = os.path.join(self.xml_dir, '%s.xml' % (name))
                        if os.path.exists(srcfile):
                            os.makedirs(destdir, exist_ok=True)
                            shutil.copy(srcfile, destdir)

                        # Check whether need copy dl xml files from data/xml/ to mirror-index/xml/
                        if self.dl_layers != -1:
                            # For mapping layer to layer-dl.xml
                            srcfile_dl = os.path.join(self.xml_dir, '%s-dl.xml' % (os.path.basename(layer['vcs_url'])))
                            if os.path.exists(srcfile_dl):
                                extra_dl_xmls.add(srcfile_dl)

                            # For real dl layers which are not in layerindex
                            for dl_name in self.get_dl_layers_from_json(name):
                                srcfile_dl = os.path.join(self.xml_dir, '%s.xml' % dl_name)
                                if os.path.exists(srcfile_dl):
                                    extra_dl_xmls.add(srcfile_dl)

                        # Special processing for the openembedded-core layer
                        if name == 'openembedded-core':
                            srcfile = os.path.join(self.xml_dir, 'bitbake.inc')
                            if os.path.exists(srcfile):
                                os.makedirs(destdir, exist_ok=True)
                                shutil.copy(srcfile, destdir)
                            srcfile = os.path.join(self.xml_dir, 'bitbake.xml')
                            if os.path.exists(srcfile):
                                os.makedirs(destdir, exist_ok=True)
                                shutil.copy(srcfile, destdir)

        if extra_dl_xmls:
            destdir = os.path.join(path, 'xml')
            for srcfile_dl in extra_dl_xmls:
                shutil.copy(srcfile_dl, destdir)

        # git add file.
        cmd = [self.tools['git'], 'add', '-A', '.']
        utils_setup.run_cmd(cmd, environment=self.env, cwd=path)

        try:
            cmd = [self.tools['git'], 'diff-index', '--quiet', 'HEAD', '--']
            utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=path, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except:
            logger.debug('Updating mirror-index')
            cmd = [self.tools['git'], 'commit', '-m', 'Updated index - %s' % (self.setup_args)]
            utils_setup.run_cmd(cmd, environment=self.env, cwd=path)
        logger.debug('Done')


    def update_manifest(self):
        logger.debug('Starting')

        fxml = open(os.path.join(self.project_dir, self.default_xml), 'w+')
        fxml.write('<manifest>\n')

        remote = 'base'
        fxml.write('    <remote  name="%s" fetch="%s"/>\n' % (remote, self.remotes[remote]))
        fxml.write('    <default revision="%s" remote="%s" sync-j="%s"/>\n' % (self.base_branch, remote, self.jobs))

        for remote in sorted(self.remotes):
            if remote == 'base':
                continue
            fxml.write('    <remote  name="%s" fetch="%s"/>\n' % (remote, self.remotes[remote]))

        def add_xml_tag(name, url, remote, path, revision, close=True):
            xml_line = '    <project name="%s" remote="%s" path="%s"' % (url, remote, path)
            if revision != self.base_branch:
                xml_line += ' revision="%s"' % revision

            # Full clone if self.dl_layers is 0
            if (not self.dl_layers in (-1, 0)) and utils_setup.is_dl_layer(name):
                xml_line += ' clone-depth="%d"' % self.dl_layers

            if close:
                xml_line += '/>\n'
            return xml_line

        def inc_xml(name):
            # incfile is included inline and has to work as elements of the 'project'
            ret = ''
            incfile = os.path.join(self.xml_dir, '%s.inc' % (name))
            logger.debug('Looking for %s' % (incfile))
            if os.path.exists(incfile):
                fbase = open(incfile, 'r')
                for line in fbase:
                    ret += line
                fbase.close()
            return ret

        def add_xml(name, url):
            # xmlfile is included after the entry and is completely standalone
            xmlfile_list = [os.path.join(self.xml_dir, '%s.xml' % (name))]

            # If a recipe layer only uses git based sources, then the download
            # layer (-dl) is empty which contains no useful data, such a layer
            # doesn't have to be in layerindex, just check and add the git
            # sources here.
            if self.dl_layers != -1 and not utils_setup.is_dl_layer(name):
                xmlfile_dl = os.path.join(self.xml_dir, '%s-dl.xml' % (os.path.basename(url)))
                xmlfile_list.append(xmlfile_dl)
            for xmlfile in xmlfile_list:
                logger.debug('Looking for %s' % (xmlfile))
                if os.path.exists(xmlfile):
                    fbase = open(xmlfile, 'r')
                    for line in fbase:
                        if self.mirror != True and not self.dl_layers in (-1, 0):
                            et = ET.fromstring(line)
                            path = os.path.basename(et.attrib['path'])
                            if utils_setup.is_dl_layer(path):
                                et.attrib['clone-depth'] = '%d' %  self.dl_layers
                                line = ET.tostring(et, encoding='unicode')
                                # Remove an extra space and append '\n'
                                line = '    %s' % line.replace(' />',  '/>\n')
                        self.xml_lines_out.append(line)
                    fbase.close()

        if self.mirror == True and self.buildtools_branch:
            for bt in (self.buildtools_remote, self.another_buildtools_remote):
                if bt:
                    self.xml_lines_out.append(add_xml_tag('buildtools', bt, 'base', bt, self.buildtools_branch))

        def process_specified_dl_layers(specified_dl_layers):
            """
            Process dl layers specified by --layers
            """
            if not specified_dl_layers:
                return
            for sdl in specified_dl_layers:
                xmlfile = os.path.join(self.xml_dir, '%s.xml' % sdl)
                with open(xmlfile) as f:
                    self.xml_lines_out += f.readlines()

        def process_xml_layers(allLayers):
            def get_cache_entry(name, remote, path, revision):
                return {
                       'name' : name,
                       'remote' : remote,
                       'path' : path,
                       'revision' : revision,
                    }

            # Add recommended dl layers from json file when needed
            def process_dl_layer(layername, url, remote):
                for dl_name in self.get_dl_layers_from_json(layername):
                    if dl_name in self.processed_dl_layers:
                        continue
                    else:
                        self.processed_dl_layers.add(dl_name)

                    path = 'layers/' + dl_name
                    entry = get_cache_entry(dl_name, remote, path, self.base_branch)

                    url_dl = url.replace(os.path.basename(url), dl_name)

                    # The dl layer should starts with 'layers/' except mirrored index.
                    if not self.index.m_index and not url_dl.startswith('layers/'):
                        url_dl = 'layers/%s' % url_dl

                    if url_dl not in cache:
                        cache[url_dl] = []

                    cache[url_dl].append(entry)
                    self.replacement['layers'].append(path)

            def process_xml_layer(lindex, layerBranch):
                branchid = self.index.getBranchId(lindex, self.get_branch(lindex=lindex))

                for layer in self.index.find_layer(lindex, id=layerBranch['layer']):
                    revision = layerBranch['actual_branch'] or self.index.getBranch(lindex, branchid)['name']

                    vcs_url = layer['vcs_url']

                    for remote in self.remotes:
                        if vcs_url.startswith(self.remotes[remote]):
                            break

                    url = vcs_url[len(self.remotes[remote]):]
                    url = url.strip('/')

                    path = 'layers/' + "".join(url.split('/')[-1:])

                    layername = layer['name']
                    entry = get_cache_entry(layername, remote, path, revision,)

                    if url not in cache:
                        cache[url] = []

                    if entry['name'] == 'openembedded-core':
                        bitbakeurl = '/'.join(url.split('/')[:-1] + [ settings.BITBAKE ])
                        bitbakeBranch = self.index.getBranch(lindex, layerBranch['branch'])['bitbake_branch']
                        bitbake_entry = get_cache_entry('bitbake', remote, path + '/bitbake', bitbakeBranch)
                        if bitbakeurl not in cache:
                            cache[bitbakeurl] = []
                        cache[bitbakeurl].append(bitbake_entry)

                    cache[url].append(entry)

                    # Add recommended dl layers from json file when needed
                    if self.dl_layers != -1:
                         process_dl_layer(layername, url, remote)

            # We need to construct a list of layers with same urls...
            cache = {}

            for (lindex, layerBranch) in allLayers:
                process_xml_layer(lindex, layerBranch)

            # oe-core is special, it might be either of:
            # 1. BASE_URL/oe-core
            # 2. BASE_URL/subset/oe-core
            # And there isn't a way to know from client, which makes its dl
            # layers may lost a 'layers/' prefix for case 1#, so adjust its dl
            # layers from layer wrlinux.
            if 'layers/wrlinux' in cache:
                for url, entry in cache.copy().items():
                    url_bn = os.path.basename(url)
                    if not utils_setup.is_dl_layer(url_bn):
                        continue
                    if not url.startswith('layers/'):
                        url_dl = 'layers/%s' % url
                        del(cache[url])
                        cache[url_dl] = entry

            from collections import OrderedDict

            for url in OrderedDict(sorted(cache.items(), key=lambda t: t[0])):
                name = cache[url][0]['name']
                remote = cache[url][0]['remote']
                path = cache[url][0]['path']
                revision = cache[url][0]['revision']

                xml_line = add_xml_tag(name, url, remote, path, revision, False)
                xml_line_inc = ''
                for entry in cache[url]:
                    xml_line_inc += inc_xml(entry['name'])

                if xml_line_inc:
                    xml_line += '>\n'
                    xml_line += xml_line_inc
                    xml_line += '    </project>\n'
                else:
                    xml_line += '/>\n'
                self.xml_lines_out.append(xml_line)

                for entry in cache[url]:
                    add_xml(entry['name'], url)

        allLayers = self.requiredlayers + self.recommendedlayers

        process_xml_layers(allLayers)

        process_specified_dl_layers(self.specified_dl_layers - self.processed_dl_layers)

        # process the remote layers
        for remote_layer in self.remote_layers:
            vcs_url = remote_layer.get('url')
            for remote in self.remotes:
                if vcs_url.startswith(self.remotes[remote]):
                    break

            url = vcs_url[len(self.remotes[remote]):]
            url = url.strip('/')
            name = "".join(url.split('/')[-1:])
            path = remote_layer.get('path')
            revision = remote_layer.get('branch')

            self.xml_lines_out.append(add_xml_tag(name, url, remote, path, revision))

        # Remove duplicates
        self.xml_lines_out = list(set(self.xml_lines_out))
        self.xml_lines_out.sort(key=self.get_xml_path)

        # Make the larger one download earlier
        large_lines = []
        prioritized_lines = []
        for line in self.xml_lines_out[:]:
            root = ET.fromstring(line)
            name = os.path.basename(root.attrib['name'])
            if name.startswith('linux-yocto'):
                prioritized_lines.append(line)
                self.xml_lines_out.remove(line)
            elif utils_setup.is_dl_layer(name):
                large_lines.append(line)
                self.xml_lines_out.remove(line)

        prioritized_lines += large_lines
        prioritized_lines += self.xml_lines_out

        fxml.write(''.join(prioritized_lines))
        fxml.write('</manifest>\n')
        fxml.close()

        logger.debug('Done')

    def check_default_xml(self):
        """
        * Check for duplicated basename in default.xml, e.g.:
          - path="/foo1/bar"
          - path="/foo2/bar"
          This doesn't work for flatten mirrors.

        * Check whether 'name' is endded with '.git'
        """

        logger.debug('Starting checking duplicated path in xml')
        default_xml_dict = {}
        tree = ET.parse(self.default_xml)
        root = tree.getroot()
        for project in root.iter('project'):
            path = project.attrib['path']
            if path:
                basename = os.path.basename(path)
                # Handle foo_repo and foo_repo.git as the same repo
                if basename.endswith('.git'):
                    basename = basename[:-4]
                if basename in default_xml_dict:
                    default_xml_dict[basename].append(path)
                else:
                    default_xml_dict[basename] = [path]

            # The 'name' cannot be ended with '.git'
            name = project.attrib['name']
            if name and name.endswith('.git'):
                name_no_git = name[:-4]
                logger.warning("%s cannot be ended with '.git', suggest %s" %(name, name_no_git))

        for basename, paths in default_xml_dict.items():
            if len(paths) > 1:
                logger.warning("Found duplicated basename in %s's attribute 'path':" % self.default_xml)
                for path in paths:
                    logger.warning(path)

        # Check blank line
        lineno = 1
        with open(os.path.join(self.project_dir, self.default_xml)) as f:
            for line in f.readlines():
                if not line.strip():
                    logger.warning('Found blank line in %s: line %s' % (self.default_xml, lineno))
                lineno += 1

        logger.debug('Done')

    def get_git_premirrors_from_mirror_index(self, url, protocol):
        """
        Generate PREMIRRORS conf for git from mirror-index.
        """
        premirrors_conf_git = []
        for xml in os.listdir(self.xml_dir):
            xml_file = os.path.join(self.xml_dir, xml)
            if not (xml.endswith('.xml') and '-dl' in xml):
                logger.debug('Skipping %s' % xml_file)
                continue

            # Parse xml file and save the name, these xml files are just xml
            # sections, not integrated xml, so can't use ET to parse them.
            logger.debug('Parsing %s' % xml_file)
            with open(xml_file) as x:
                for line in x.readlines():
                    if not 'name=' in line:
                        continue
                    # Part of the parsing errors may not matter since we only
                    # need their parent dirs.
                    try:
                        name = line.split('name=')[1].split()[0].strip('"')
                    except Exception as esc:
                        logger.warning('Failed to get name from %s:%s' % (xml_file, line))
                    if name:
                        conf = "    git://.*/.* git://%s%s/%s/MIRRORNAME;protocol=%s \\n \\" % \
                                (url.netloc, url.path, os.path.dirname(name), protocol)
                    if not conf in premirrors_conf_git:
                        premirrors_conf_git.append(conf)
                        premirrors_conf_git.append(conf.replace('git://', 'gitsm://'))

        # Make the subdirs in front to have a higher priority in PREMIRRORS, e.g.:
        # external/yocto
        # external/qt5
        # [snip]
        # external
        if not premirrors_conf_git:
            logger.warning('Failed get figure out dirs from %s' % self.xml_dir)
        else:
            premirrors_conf_git.reverse()

        return premirrors_conf_git

    def make_mirror_as_premirrors(self):
        """
        * dl*/downlaods: Use "git clone --local --branch <revision>" to clone dl layers into
                         premirrors-dl, and copy (link) dl*/downloads/* into premirrors-dl/downloads,
                         the premirrors-dl/downloads will be used as PREMIRRORS by client.

        * git repo sources: No action is needed since they are already bare repos.
        """

        logger.info('Making project mirror as PREMIRRORS...')
        premirrors_dict = {}
        tree = ET.parse(self.default_xml)
        root = tree.getroot()
        for project in root.iter('project'):
            # Only need the dl layers
            if 'bare' in project.attrib and project.attrib['bare'] == "True":
                continue
            name = project.attrib['name']
            if name and (name.endswith('-dl') or '-dl-' in name):
                try:
                    path = project.attrib['path']
                    if path and path.startswith('layers/'):
                        if 'revision' in project.attrib:
                            revision = project.attrib['revision'].replace('refs/tags/', '')
                        else:
                            revision = self.base_branch
                        premirrors_dict[name] = revision
                except Exception as esc:
                    logger.warning('%s: Failed to find revision or path: %s' % (name, esc))

        if premirrors_dict:
             if not os.path.exists(self.premirrors_dl):
                os.mkdir(self.premirrors_dl)
        else:
            logger.warning("mirror-as-premirrors: No dl layers found!")
            return

        for name, revision in premirrors_dict.items():
            src = os.path.join(self.project_dir, name)
            dst = os.path.join(self.premirrors_dl, os.path.basename(name))
            # Run the git reset and pull in the existed repo
            dst_git = os.path.join(dst, '.git')
            need_clone = True
            if os.path.exists(dst_git):
                logger.debug('Making %s as a PREMIRROR' % src)
                try:
                    for cmd in ([self.tools['git'], 'fetch', '-q', 'origin', revision], \
                                    [self.tools['git'], 'checkout', '-q', 'FETCH_HEAD']):
                        utils_setup.run_cmd(cmd, environment=self.env, cwd=dst)
                    need_clone = False
                except Exception as esc:
                    logger.warning('%s: Failed to update it: %s' % (dst, esc))
                    logger.warning('%s: Removing it...' % dst)
                    shutil.rmtree(dst)
            if need_clone:
                # There are a lot of messages when run "git clone --branch <tag>"
                # which rush the screen, so use "git clone -nq" to make it
                # quiet, and "git checkout <branch/tag>" to checkout the files.
                cmd = [self.tools['git'], 'clone', '--local', '-nq', src, dst]
                utils_setup.run_cmd(cmd, environment=self.env)
                cmd = [self.tools['git'], 'checkout', '-q', revision]
                utils_setup.run_cmd(cmd, environment=self.env, cwd=dst)
        # Create a clean premirrors-dl/downloads as PREMIRRORS
        if os.path.exists(self.premirrors_dl_downloads):
            shutil.rmtree(self.premirrors_dl_downloads)
        os.mkdir(self.premirrors_dl_downloads)
        utils_setup.create_symlinks(('../*-dl*/downloads/*', '../*gitshallow-dl*/git*.tar.gz'), self.premirrors_dl_downloads)
        logger.info('The PREMIRROR files are prepared in %s' % self.premirrors_dl_downloads)

    def use_mirror_as_premirrors(self):
        """
        Use mirror as PREMIRRORS if the project is cloned from a mirror
        """

        logger.info('Using mirror as PREMIRRORS...')

        if not self.mirror_index_path:
            logger.warning("This project isn't setup from a project mirror, skipping mirror-as-premirrors")
            return

        premirrors_output = os.path.join(self.conf_dir, 'mirror-as-premirrors.conf')

        enable_network = False
        url = urlparse(self.base_url)
        if url.scheme and url.scheme != 'file':
            protocol = url.scheme
            enable_network = True
        else:
            protocol = 'file'

        premirrors_conf_git = self.get_git_premirrors_from_mirror_index(url, protocol)

        premirrors_conf_downloads = []
        downloads_via_git = ""
        if protocol == 'git':
            downloads_via_git = "The protocol git:// is invalid for premirrors-dl/downloads"
            logger.warning('mirror-as-mirrorrs: %s' % downloads_via_git)
        else:
            premirrors_conf_downloads.append("    .*://.*/.* %s://%s%s/premirrors-dl/downloads/ \\n \\" % \
                (protocol, url.netloc, url.path))

        with open(premirrors_output, 'w') as f:
            f.write('# Use project mirror as PREMIRRORS for the build\n')
            if premirrors_conf_git:
                f.write('PREMIRRORS:append = " \\\n')
                f.write('%s\n"\n' % '\n'.join(premirrors_conf_git))
            if premirrors_conf_downloads:
                f.write('PREMIRRORS:append = " \\\n')
                f.write('%s\n"\n' % '\n'.join(premirrors_conf_downloads))
            if enable_network:
                # The network is for PREMIRRORS only
                f.write('\n# The network is for PREMIRRORS only\n')
                f.write('BB_NO_NETWORK = "0"\n')
                f.write('BB_FETCH_PREMIRRORONLY = "1"\n')

        local_conf_sample = os.path.join(self.project_dir, 'config/local.conf.sample')
        with open(local_conf_sample, 'a+') as f:
            f.seek(0)
            line = 'require ##OEROOT##/config/mirror-as-premirrors.conf\n'
            if not line in f.readlines():
                f.write('\n# Use project mirror as PREMIRRORS for the build\n')
                f.write(line)

    def update_gitignore(self):
        logger.debug('Starting')

        ign_list = [
                    '.repo*',
                    '*.pyc',
                    '*.pyo',
                    '*.swp',
                    '*.orig',
                    '*.rej',
                    '*~',
                    '/bin',
                    '/environment-setup-*',
                    '/layers/*',
                    '!layers/local',
                    os.path.basename(self.install_dir),
                    ]

        tree = ET.parse(os.path.join(self.project_dir, 'default.xml'))
        root = tree.getroot()
        for linkfile in root.iter('linkfile'):
            dest = linkfile.attrib['dest']
            if not '/' in dest:
                ign_list.append(dest)

        with open(os.path.join(self.project_dir, '.gitignore'), 'a+') as f:
            f.seek(0)
            existed = f.readlines()
            for l in ign_list:
                item = '%s\n' % l
                if item not in existed:
                    f.write(item)

        logger.debug('Done')

    def commit_files(self):
        logger.debug('Starting')

        # List of all files that may change due to config
        filelist = [
            'README',
            'default.xml',
            '.gitignore',
            '.gitconfig',
            'config/index-cache',
            ]

        # If we are mirroring, skip all of these...
        if self.mirror != True:
            filelist.append('layers/local')
            filelist.append('.templateconf')
            filelist.append('config/bblayers.conf.sample')
            filelist.append('config/conf-notes.txt')
            filelist.append('config/local.conf.sample')
            filelist.append('config/saved_base_branch')

            if os.path.exists('config/site.conf.sample'):
                filelist.append('config/site.conf.sample')

        # Add log dir if it contains files
        if os.listdir('config/log'):
            filelist.append('config/log')

        # git init
        if not os.path.exists(self.project_dir + '/.git'):
            cmd = [self.tools['git'], 'init', self.project_dir]
            if self.quiet == self.default_repo_quiet:
                cmd.append(self.quiet)
            utils_setup.run_cmd(cmd, environment=self.env, cwd=self.conf_dir)

        # git add manifest. (Since these files are new, always try to add them)
        cmd = [self.tools['git'], 'add', '--'] + filelist
        utils_setup.run_cmd(cmd, environment=self.env, cwd=self.project_dir)

        try:
            cmd = [self.tools['git'], 'diff-index', '--quiet', 'HEAD', '--'] + filelist
            utils_setup.run_cmd(cmd, log=2, environment=self.env, cwd=self.project_dir, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except:
            logger.plain('Updated project configuration')
            # Command failed -- so self.default_xml changed...
            cmd = [self.tools['git'], 'commit', '-q', '-m', 'Configuration change - %s' % (self.setup_args), '--'] + filelist
            utils_setup.run_cmd(cmd, environment=self.env, cwd=self.project_dir)

        logger.debug('Done')

    # Sort by path like repo does
    def get_xml_path(self, xml_line):
        root = ET.fromstring(xml_line)
        return root.attrib['path']

    def remove_obsoleted_dl_repos(self, old_xml, new_xml):
        need_remove = set()
        with open(old_xml) as f:
            old = f.readlines()
        with open(new_xml) as f:
            new = f.readlines()
        for line in old:
            if not line in new:
                line = line.strip()
                # Check '/>' to skip old style default.xml.
                if '<project name=' in line and line.endswith('/>'):
                    need_remove.add(line)
        for line in need_remove:
            path = self.get_xml_path(line)
            # Remove the clean local dl layer to replace with new one.
            # Only remove it when it is clean, otherwise, report errors to
            # ask user to run with '--repo-force-sync'
            if os.path.exists(path) and utils_setup.is_dl_layer(path):
                cmd = [self.tools['git'], 'status', '--porcelain']
                dirty = subprocess.check_output(cmd, cwd=path)
                cmd = [self.tools['git'], 'diff-index', 'm/master']
                commit = subprocess.check_output(cmd, cwd=path)
                if not (dirty or commit):
                    path_git = os.path.join(path, '.git')
                    path_git_real = os.path.realpath(path_git)
                    logger.info('Removing obsoleted clean dl layer %s' % path)
                    shutil.rmtree(path)
                    if os.path.exists(path_git_real):
                        logger.info('Removing obsoleted clean dl layer %s' % path_git_real)
                        shutil.rmtree(path_git_real)

    def repo_sync(self):
        if self.repo_no_fetch:
            logger.info('Skipping repo sync')
            return

        logger.debug('Starting')

        def checkout_to_repo_branch():
            # Checkout to specific branch
            if self.repo_rev:
                cmd = [self.tools['git'], 'checkout', '-B', self.repo_rev, 'origin/%s' % self.repo_rev]
                utils_setup.run_cmd(cmd, environment=self.env, cwd=os.path.dirname(self.repo_dir))

        self.repo_dir = os.path.join(self.project_dir, self.check_repo_install_dir)
        if os.path.exists(self.repo_dir):
            default_xml_manifest = os.path.join(self.manifest_dir, 'default.xml')
            # Re-init when needed
            if os.path.exists(default_xml_manifest):
                if not filecmp.cmp(self.default_xml, default_xml_manifest):
                    if not self.mirror:
                        self.remove_obsoleted_dl_repos(default_xml_manifest, self.default_xml)
                    self.call_repo_init(first_init=False)
            else:
                self.call_repo_init(first_init=False)

            checkout_to_repo_branch()
            cmd = ['-j', self.jobs]
            self.call_repo_sync(cmd)
        else:
            # repo init
            self.call_repo_init()

            checkout_to_repo_branch()

            # repo sync
            cmd = ['-j', self.jobs]
            self.call_initial_repo_sync(cmd)

        logger.debug('Done')

    def update_symlinks(self):
        """
        Create symlinks in wrlinux-src-dl/downloads/* for the files from
        gitshallow-dl/*  and ../*-dl*/downloads/ so that there will be only one
        PREMIRRORs.
        """
        if self.dl_layers == -1:
            return

        logger.plain('Updating symlinks...')

        wrlinux_src_dl_downloads = os.path.join(self.project_dir, 'layers/wrlinux-src-dl/downloads')
        if not os.path.exists(wrlinux_src_dl_downloads):
            logger.warning('Failed to find %s' % wrlinux_src_dl_downloads)
            return

        # Remove invalid symlinks
        for sym in os.listdir(wrlinux_src_dl_downloads):
            sym_path  = os.path.join(wrlinux_src_dl_downloads, sym)
            if not os.path.exists(sym_path):
                logger.debug('Removing invalid symlink %s' % sym)
                os.unlink(sym_path)

        # Create new symlinks
        utils_setup.create_symlinks(('../*-gitshallow-dl/git*.tar*', '../../*dl/downloads/*'), wrlinux_src_dl_downloads)
        logger.info('Done')

    def __check_and_update_layerseries_compat(self, project_local_layer_path, data_local_layer_path):
        project_layer_conf = os.path.join(project_local_layer_path, 'conf/layer.conf')
        data_layer_conf = os.path.join(data_local_layer_path, 'conf/layer.conf')
        project_layerseries_compat_line = ""
        data_layerseries_compat_line = ""
        update_local_conf = False
        with open(data_layer_conf, 'r') as f:
            for line in f.readlines():
                if 'LAYERSERIES_COMPAT_local' in line:
                    data_layerseries_compat_line = line
                    break

        with open(project_layer_conf, 'r') as f:
            lines = f.readlines()
            newlines = []
            for line in lines:
                if 'LAYERSERIES_COMPAT_local' in line:
                    project_layerseries_compat_line = line
                    if project_layerseries_compat_line != data_layerseries_compat_line:
                        newlines.append(data_layerseries_compat_line)
                        update_local_conf = True
                    else:
                        newlines.append(project_layerseries_compat_line)
                else:
                    newlines.append(line)

        if update_local_conf:
            logger.info("Update LAYERSERIES_COMPAT_local for local layer")
            with open(project_layer_conf, 'w') as f:
                for line in newlines:
                    f.write(line)

    def __set_recipelists_layers(self):
        """Set RECIPE_LIST_LAYERS in wrlinux-recipelists.inc of layer local"""
        logger.debug('Checking RECIPE_LIST_LAYERS in local layer')

        local_layer_conf = os.path.join(self.project_dir, 'layers/local', 'conf/wrlinux-recipelists.inc')
        index_layers = self.index.get_index_layers(self.base_branch)
        collections = []
        for _, layers in index_layers.items():
            for layer in layers:
                col = layer['collection']
                if col:
                    collections.append(layer['collection'])
                else:
                    logger.warning('%s: BBFILE_COLLECTIONS is None' % layer['name'])

        existed = False
        lines = []
        if os.path.isfile(local_layer_conf):
            with open(local_layer_conf, 'r') as f:
                for line in f:
                    if re.match(r'RECIPE_LIST_LAYERS\s*=', line):
                        lines.append("RECIPE_LIST_LAYERS = '%s'" % ' '.join(sorted(collections)))
                        existed = True
                    else:
                        lines.append(line)
        else:
            lines.append('# DO NOT EDIT THIS FILE!')
            lines.append('\n# This file is automatically generated by setup.py\n')

        if not existed:
            lines.append('\n# Set RECIPE_LIST_LAYERS with all known layers')
            lines.append("\nRECIPE_LIST_LAYERS = '%s'" % ' '.join(sorted(collections)))

        with open(local_layer_conf, 'w') as f:
            f.writelines(lines)

        require_recipelists = 'require conf/wrlinux-recipelists.inc\n'
        require_desc = '\n# WR layers are added to recipe lists used during allowed list checking\n'
        require_desc += '# With the list below included it is not needed to enable customer packages\n'
        require_desc += '# or remove customer layers from allowed list checking\n'
        require_desc += '# RECOMMENED NOT TO REMOVE\n'
        with open(os.path.join(self.project_dir + '/layers/local/conf/layer.conf'), 'a+') as f:
            f.seek(0)
            if require_recipelists not in f.readlines():
                f.write(require_desc + require_recipelists)

    def __setup_local_layer(self):
        """Setup the local layer in /layers/local - if required."""
        logger.debug('Checking local layer')

        if self.mirror is True:
            return

        project_local_layer_path = os.path.join(self.project_dir,'layers/local')
        data_local_layer_path = os.path.join(self.install_dir, 'data/local_layer')
        if os.path.exists(project_local_layer_path):
            # update LAYERSERIES_COMPAT_local if necessary
            self.__check_and_update_layerseries_compat(project_local_layer_path, data_local_layer_path)
            self.__set_recipelists_layers()
            return

        logger.debug('Starting local layer')

        if not os.path.exists(os.path.join(self.project_dir, 'layers')):
            os.makedirs(os.path.join(self.project_dir, 'layers'))

        if not os.path.exists(os.path.join(self.project_dir, 'layers/local')):
            shutil.copytree(os.path.join(self.install_dir, 'data/local_layer'), os.path.join(self.project_dir, 'layers/local'))
            # make sure the local layer is writeable
            try:
                cmd = ['chmod', '-R', '+w', '%s' % (project_local_layer_path)]
                utils_setup.run_cmd(cmd, environment=self.env)
            except Exception as e:
                raise

        self.__set_recipelists_layers()

        logger.debug('Done')

    def setup_env(self):
        self.set_ssl_cert()
        self.set_repo_git_env()
        self.add_bin_path()

    def add_bin_path(self):
        self.env["PATH"] = self.install_dir + "/bin:" + self.env["PATH"]

    def set_repo_git_env(self):
        # Set HOME to install_dir to use install_dir/.gitconfig settings.  Otherwise the user will
        # be prompted for information.
        self.env["HOME"] = self.project_dir

    def set_ssl_cert(self):
        fn = self.project_dir + self.BINTOOLS_SSL_CERT
        dn = self.project_dir + self.BINTOOLS_SSL_DIR
        if os.path.exists(fn) and os.path.exists(dn):
            self.env["GIT_SSL_CAINFO"] = fn
            self.env["CURL_CA_BUNDLE"] = fn
            self.env["SSL_CERT_FILE"] = fn
            self.env["SSL_CERT_DIR"] = dn
            os.environ["SSL_CERT_FILE"] = fn
            os.environ["SSL_CERT_DIR"] = dn

    def call_repo_init(self, first_init=True):
        logger.debug('Starting')
        repo = self.tools['repo']
        directory = os.path.join(self.project_dir, self.check_repo_install_dir)

        cmd = [repo, 'init', '--no-clone-bundle', '-m', self.default_xml, \
                '-u',  self.project_dir, '--no-repo-verify']

        if self.mirror == True and first_init:
            cmd.append('--mirror')

        if self.depth:
            cmd.append(self.depth)
        if self.repo_url:
            cmd.extend(['--repo-url', self.repo_url])
        if self.repo_rev:
            cmd.extend(['--repo-branch', self.repo_rev])
        logger.debug("cmd: %s" % cmd)
        try:
            utils_setup.run_cmd(cmd, environment=self.env)
        except Exception as e:
            raise
        logger.debug('Done')

    # This only exists to check if we have fully sync'ed the project
    # Updating should use call_repo_sync
    def call_initial_repo_sync(self, args):
        logger.debug('Starting')
        sync_file= os.path.join(self.project_dir, self.check_repo_sync_file)
        local_only = 0
        orig_args = list(args)
        if os.path.exists(sync_file):
            logger.info('Detected repo sync already run since %s exists' % sync_file)
            logger.info('Only running local update.')
            args.append('--local-only')
            local_only = 1
        try:
            self.prime_kernel_repo(args)
            self.call_repo_sync(args)
        except Exception as e:
            if not local_only:
                raise
            else:
                logger.info('Using --local-only failed.  Trying full sync.')
                try:
                    self.call_repo_sync(orig_args)
                except Exception as e2:
                    raise

        logger.debug('Done')

    def parse_manifest_for_kernel(self):
        import xml.etree.ElementTree as ET

        linux_yocto_path = ""
        linux_yocto_dev_path = ""
        tree = ET.parse(os.path.join(self.project_dir, 'default.xml'))
        root = tree.getroot()
        for project in root.iter('project'):
            name = project.attrib['name']
            if name.endswith('linux-yocto'):
                linux_yocto_path = name
            if name.endswith('linux-yocto-dev'):
                linux_yocto_dev_path = name

        return (linux_yocto_path, linux_yocto_dev_path)

    # If manifest contains linux-yocto and linux-yocto-dev sync linux-yocto first
    # and copy it to linux-yocto-dev to reduce download
    def prime_kernel_repo(self, args):
        if not self.prime:
            logger.debug('Priming is disabled')
            return

        # do not prime if REPO_MIRROR_LOCATION is set
        if os.getenv('REPO_MIRROR_LOCATION'):
            logger.debug('Skipping priming because REPO_MIRROR_LOCATION is set')
            return

        (linux_yocto_path, linux_yocto_dev_path) = self.parse_manifest_for_kernel()

        # only prime the download if there is a linux-yocto-dev repo
        if linux_yocto_dev_path:
            # in mirror mode the repos are bare clone in the project dir
            prime_dir = self.project_dir
            if self.mirror == False:
                # in non-mirror mode the objects are located in the .repo directory
                prime_dir = os.path.join(self.project_dir, '.repo/project-objects')
                os.mkdir(prime_dir)

            logger.info('Preloading common git objects...')
            subdir = os.path.dirname(linux_yocto_path)
            if subdir:
                os.mkdir(os.path.join(prime_dir, subdir))

            cmd = [self.tools['git'], 'clone', '--bare', '--single-branch', '--branch',
                   KERNEL_MERGE_BASE, '%s/%s' % (self.base_url, linux_yocto_path) ]
            utils_setup.run_cmd(cmd, environment=self.env,
                                cwd=os.path.join(prime_dir, subdir))

            # remove origin so there isn't any conflict with repo
            cmd = [self.tools['git'], 'remote', 'remove', 'origin']
            utils_setup.run_cmd(cmd, environment=self.env,
                                cwd=os.path.join(prime_dir, linux_yocto_path + '.git'))

            # the objects are stored in bare repos so they end with .git
            shutil.copytree(os.path.join(prime_dir, linux_yocto_path + '.git'),
                            os.path.join(prime_dir, linux_yocto_dev_path + '.git'),
                            copy_function=os.link)


    def call_repo_sync(self, args):
        logger.debug('Starting')
        repo = self.tools['repo']
        cmd = [repo, 'sync']
        # disable use of /clone.bundle on HTTP/HTTPS
        cmd.append('--no-clone-bundle')
        if self.retry_fetches:
            cmd.append(self.retry_fetches)
        if self.prune:
            cmd.append('--prune')
        if self.force_sync:
            cmd.append(self.force_sync)
        log_it = 0
        cmd.extend(args)
        utils_setup.run_cmd(cmd, environment=self.env, log=log_it)
        logger.debug('Done')

    def get_branch(self, lindex=None):
        if lindex:
            return self.index.getIndexBranch(default=self.base_branch, lindex=lindex)
        return self.base_branch

    def get_layer_by_name(self, name):
        """
        Return lindex and layerBranch for layer 'name'
        """
        for lindex in self.index.index:
            for l in lindex['layerItems']:
                if name == l['name']:
                    branchid = self.index.getBranchId(lindex, self.get_branch(lindex=lindex))
                    if not branchid:
                        continue
                    for layerBranch in self.index.getLayerBranch(lindex, branchid, layerItem=l) or []:
                        return (lindex, layerBranch)
        logger.error("Failed to find lindex and layerBranch for %s" % name)
        logger.error("Check %s to fix it" % self.dl_layer_recommends)
        return ()

    def get_path(self, tool):
        cmd = self.which(tool)
        if (not cmd):
            logger.critical('Cannot find %s in path!' % tool)
            logger.critical('Path was: %s' % os.environ['PATH'])
        return cmd

    # Helpers: Set_*, which..
    def set_repo_verbose(self, verbose):
        self.repo_verbose = verbose

    def set_jobs(self, jobs):
        logger.debug('Setting jobs to %s' % jobs)
        self.jobs = str(jobs)

    def set_depth(self, depth):
        if depth <= 1:
            logger.info('repo depth %s is invalid, setting to 2' % depth)
            depth = 2
        logger.debug('Setting depth to %s' % depth)
        self.depth = '--depth=%s' % depth

    def set_retry_fetches(self, retry_fetches):
        logger.debug('Setting retry_fetches to %s' % retry_fetches)
        self.retry_fetches = '--retry-fetches=%s' % retry_fetches

    def set_force_sync(self, sync):
        logger.debug('Setting force-sync to %s' % sync)
        if sync is True:
            self.force_sync = '--force-sync'

    def set_repo_url(self, url):
        logger.debug('Setting repo-url to %s' % url)
        self.repo_url = url

    def set_repo_rev(self, rev):
        logger.debug('Setting repo-rev to %s' % rev)
        self.repo_rev = rev

    def set_debug(self):
        self.debug_lvl += 1
        self.set_debug_env()
        self.quiet = None
        logger.setLevel(logging.DEBUG)
        logger.debug('logging level set to DEBUG')

    def set_base_url(self, url):
        logger.debug('Setting base-url to %s' % url)
        self.base_url = url

    def set_base_branch(self, branch):
        logger.debug('Setting base-branch to %s' % branch)
        self.base_branch = branch

    def set_debug_env(self):
        self.env["REPO_CURL_VERBOSE"] = '1'

    def set_no_prime(self, no_prime):
        logger.debug('Setting priming to %s' % (not no_prime))
        self.prime = not no_prime

    def set_no_prune(self, no_prune):
        logger.debug('Setting repo sync prune behavior to %s' % (not no_prune))
        self.prune = not no_prune

    def touch(self, fn):
        logger.debug("Creating %s" % fn)
        open(fn, 'a').close()

    ''' When this is python3.3, use built in version'''
    def which(self, program):
        path=self.env["PATH"]
        for path in path.split(os.path.pathsep):
            fullpath=os.path.join(path, program)
            if os.path.exists(fullpath) and os.access(fullpath,os.X_OK):
                return fullpath
        return None

if __name__ == '__main__':
    try:
        x = Setup()
        x.main(sys.argv)
    except KeyboardInterrupt:
        logger.warning("Aborted by user, will terminate this setup.")
        sys.exit(1)
