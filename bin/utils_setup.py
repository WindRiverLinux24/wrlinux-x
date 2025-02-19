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

import os
import sys
import subprocess

# Setup-specific modules
import logger_setup

logger = logger_setup.setup_logging()

def run_cmd(cmd, environment=None, cwd=None, log=1, expected_ret=0, err=b'GitError', err2=b'error', err3=b'fatal', stderr=None, stdout=None):
    err_msg = []

    logger.debug('Running cmd: "%s"' % repr(cmd))
    if cwd:
        logger.debug('From %s' % cwd)

    # log 0 - send stdout to Exception, not logged stderr
    # log 1 - send output to plain
    # log 2 - send output to debug
    if log == 1 or log == 2:
        if stderr is None:
            stderr = subprocess.STDOUT

        ret = subprocess.Popen(cmd, env=environment, cwd=cwd, stderr=stderr, stdout=subprocess.PIPE)
        while True:
            output = ret.stdout.readline()
            if not output and ret.poll() is not None:
                break
            if output:
                output = output.strip()
                if len(err_msg) > 0 or output.startswith(err) or output.startswith(err2) or output.startswith(err3):
                    err_msg.append("%s" % output.decode('utf-8'))
                if log == 1:
                    logger.plain("%s" % output.decode('utf-8'))
                elif log == 2:
                    logger.debug("%s" % output.decode('utf-8'))
    else:
        logger.debug('stderr not logged for this command (%s)' % (cmd))
        ret = subprocess.Popen(cmd, env=environment, cwd=cwd, stderr=None, stdout=subprocess.PIPE, text=True)

    ret.wait()
    if ret.returncode != expected_ret:
        if stderr != subprocess.DEVNULL:
            if environment:
                for key in environment.keys():
                    logger.to_file('%20s = %s' % (key, repr(environment[key])))
            if log != 2:
                logger.critical('cmd "%s" returned %d' % (' '.join(cmd), ret.returncode))
            else:
                logger.debug('cmd "%s" returned %d' % (' '.join(cmd), ret.returncode))

        if log == 0:
            # Remove '\n' in the tail of each line
            err_msg += [line[:-1] for line in ret.stdout.readlines()]

        msg = ''
        if cwd:
            msg += cwd + ': '
        msg += " ".join(cmd) + '\n'
        msg += '\n'.join(err_msg)
        msg += '\n'
        raise Exception(msg)
    logger.debug('Finished running cmd: "%s"' % repr(cmd))

def query_input(question, interactive):
    client = os.environ.get('GIT_ASKPASS', None)
    if not client:
        client = os.environ.get('SSH_ASKPASS', None)
    if not client:
        if interactive:
            client = "[internal]"
        else:
            raise Exception('Unable to get authentication via ASKPASS.')

    cmd = [ client, question ]
    logger.debug("cmd: %s " % (cmd))

    if cmd[0] == "[internal]":
        import getpass
        retval = getpass.getpass(cmd[1])
    else:
        environ = os.environ.copy()

        # We do NOT want to inherit python home from the environment
        # See Issue: LIN1018-2934
        #   python3 wrapper from the buildtools sets this, which causes host
        #   python tools to fail
        if 'PYTHONHOME' in environ:
            del environ['PYTHONHOME']

        ret = subprocess.Popen(cmd, env=environ, close_fds=True, stdout=subprocess.PIPE)
        retval = ""
        while True:
            lin = ret.stdout.readline()
            if not lin and ret.poll() is not None:
                break
            retval += lin.decode('utf-8')
        ret.wait()
        if ret.returncode != 0:
            raise Exception('return code != 0 from %s.' % cmd)
        retval = retval.rstrip('\n')

    return retval


def fetch_url(url=None, auth=False, debuglevel=0, interactive=0):
    assert url is not None

    import urllib
    from urllib.request import urlopen, Request
    from urllib.parse import urlparse

    if auth:
        logger.debug("Configuring authentication for %s..." % url)

        up = urlparse(url)

        uname = query_input("Username for '%s://%s': " % (up.scheme, up.netloc), interactive)
        passwd = query_input("Password for '%s://%s@%s': " % (up.scheme, uname, up.netloc), interactive)

        # This is a security leak, as the username/password could be logged.
        # Only enable this during development.
        #logger.debug("%s: u:'%s' p:'%s'" % ( url, uname, passwd ))

        password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, "%s://%s" % (up.scheme, up.netloc), uname, passwd)
        handler = urllib.request.HTTPBasicAuthHandler(password_mgr)
        opener = urllib.request.build_opener(handler, urllib.request.HTTPSHandler(debuglevel=debuglevel))
    else:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(debuglevel=debuglevel))

    urllib.request.install_opener(opener)

    logger.debug("Fetching %s (%s)..." % (url, ["without authentication", "with authentication"][auth]))

    try:
        res = urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0 (Wind River Linux/setup.sh)'}, unverifiable=True))
    except urllib.error.HTTPError as e:
        logger.debug("HTTP Error: %s: %s" % (e.code, e.reason))
        logger.debug(" Requested: %s" % (url))
        logger.debug(" Actual:    %s" % (e.geturl()))
        if auth:
            logger.debug(" Authentication enabled.  Using username '%s'." % uname)
        if not auth and e.code == 401:
            logger.debug("Retrying with authentication...")
            res = fetch_url(url, auth=True, debuglevel=debuglevel, interactive=interactive)
            logger.debug("...retrying with authentication successful, continuing.")
        elif e.code == 404:
            logger.debug("Request not found.")
            raise e
        else:
            logger.debug("Headers:\n%s" % (e.headers))
            raise e
    except OSError as e:
        error = 0
        reason = ""

        # Process base OSError first...
        if hasattr(e, 'errno'):
            error = e.errno
            reason = e.strerror

        # Process gaierror (socket error) subclass if available.
        if hasattr(e, 'reason') and hasattr(e.reason, 'errno') and hasattr(e.reason, 'strerror'):
            error = e.reason.errno
            reason = e.reason.strerror
            if error == -2:
                raise e

        if error and error != 0:
            logger.critical("Unable to fetch %s due to exception: [Error %s] %s" % (url, error, reason))
        else:
            logger.critical("Unable to fetch %s due to OSError exception: %s" % (url, e))
        sys.exit(1)
    except Exception as e:
        logger.critical('Unable to fetch entitlement: %s (%s)' % (type(e), e))
        sys.exit(1)
    finally:
        logger.debug("...fetching %s (%s), done." % (url, ["without authentication", "with authentication"][auth]))

    return res

def is_dl_layer(layername):
    if '-dl-' in layername or layername.endswith('-dl'):
        return True
    else:
        return False

def create_symlinks(srclist, destdir):
    import glob
    saved_cwd = os.getcwd()
    destdir_abspath = os.path.abspath(destdir)
    try:
        os.chdir(destdir)
        for wildcard in srclist:
            files = glob.glob(wildcard)
            for f in files:
                # Skip the one in destdir itself
                f_abspath = os.path.abspath(f)
                if f_abspath.startswith(destdir_abspath):
                    logger.debug('Skipping %s' % f_abspath)
                    continue
                dst = os.path.basename(f)
                if not os.path.exists(dst):
                    os.symlink(f, dst)
    except Exception as e:
        raise
    finally:
        os.chdir(saved_cwd)
