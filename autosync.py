#!/usr/bin/env python
from __future__ import print_function
from __future__ import unicode_literals
import logging
logging.basicConfig(
    format="%(levelname)-8s [%(name)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)
import os
from os.path import dirname
import sys
import re
import json
from pprint import pprint
from time import time, gmtime, strftime, sleep
from argparse import ArgumentParser
from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileModifiedEvent,
    FileDeletedEvent,
    FileCreatedEvent
)


CONFIG_NAME = 'autosync.json'
LOCK_NAME = 'autosync.lock'
DELAY = 1.0  # seconds
CWD = os.getcwd()

config = {}
handlers = {}
cmds = [
    'rsync -vrzu --delete --include-from "{lock_file}" --exclude "*" {local}/ {remote}/',
    'rm -f "{lock_file}"'
]

def ts():
    return strftime("%Y-%m-%d %H:%M:%S", gmtime())

class Job(object):
    def __init__(self):
        pass

    def __repr__(self):
        return "<Job: %s>" % self.name

class Handler(FileSystemEventHandler):
    changed_dirs = []

    def __init__(self, observer, name, local, remote, ignore=[]):
        log.debug("(%s) Creating handler", name)
        super(Handler, self).__init__()
        self.name = name
        self.last_change = None
        self.remote = remote

        self.local_real = abspath(local)
        log.info("(%s) Local dir: %s", name, self.local_real)

        self.lock_file = os.path.join(self.local_real, LOCK_NAME)
        if os.path.exists(self.lock_file):
            log.debug("(%s) Removing old lock file: %s", name, self.lock_file)
            os.remove(self.lock_file)

        log.info("(%s) Remote dir: %s", name, remote)

        # build a regular expression for the ignore list
        log.info("(%s) Ignore list: %s", name, ignore)
        ignore_list = global_ignore + ignore
        for i, path in enumerate(ignore_list):
            if path.startswith('/'):
                path = '^' + path.lstrip('/')
            if not path.endswith('*'):
                path = path + '$'
            path = path.replace('.', '\.')
            path = path.replace('*', '.*')
            ignore_list[i] = path
        log.debug("(%s) Regexified ignore list: %s", name, ignore_list)
        self.ignore_re = re.compile('|'.join(ignore_list), re.IGNORECASE)

        log.debug("(%s) Adding handler to observer", name)
        observer.schedule(self, self.local_real, recursive=True)

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent):# we don't care about dir mutations
            self.handle_event(event)

    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent):# we don't care about dir mutations
            self.handle_event(event)

    def on_deleted(self, event):
        if isinstance(event, FileDeletedEvent):  # we don't care about dir mutations
            self.handle_event(event)

    def handle_event(self, event):
        if os.path.basename(event.src_path) == LOCK_NAME: return
        abs_path = abspath(event.src_path)
        abs_dir = dirname(abs_path)
        rel_path = relpath(abs_path, self.local_real)
        rel_dir = dirname(rel_path)
        if rel_dir == '.': rel_dir = ''

        if self.ignore_re.match(rel_path):
            log.debug("(%s) Ignored file changed: %s", self.name, abs_path)
            return

        """log.debug(
            "(%s) New event:-\nAP: %s\nAD: %s\nRP: %s\nRD: %s",
            self.name, abs_path, abs_dir, rel_path, rel_dir
        )"""

        self.last_change = time()

        # check if we're already going to be syncing this subdir
        if rel_dir in self.changed_dirs:
            #log.debug("(%s) Dir already in sync queue: %s", self.name, rel_dir)
            return

        log.debug("(%s) Dir added to sync queue: %s", self.name, rel_dir)
        self.changed_dirs.append(rel_dir)

def tick():
    for name, handler in handlers.items():
        if handler.last_change is None:
            continue

        remaining_time = handler.last_change + DELAY - time()
        if remaining_time > 0:
            #log.debug("(%s) Delay not yet elapsed (%s to go)", name, remaining_time)
            continue

        if os.path.exists(handler.lock_file):
            log.info("Waiting for previous sync to finish...")
            continue

        # lock in the list of dirs to sync
        dirs = list(handler.changed_dirs)
        handler.changed_dirs = []
        log.debug("Dirs to sync: %s", dirs)

        # write the list of dirs to include to the lock file
        with open(handler.lock_file, 'wb') as f:
            for d in dirs:
                # write all parents, with no trailing slashes
                head = d
                while True:
                    head, tail = os.path.split(head)
                    if head == '': break
                    f.write('%s\n' % head)
                    if tail == '': break

                # write the dir itself and tell rsync to include all files in it
                f.write('{0}{1}***\n'.format(d, os.sep))

        with open(handler.lock_file, 'rb') as f:
            contents = f.read()
            log.debug("(%s) rsync include list:\n%s", name, contents)


        cmdstr = ' && '.join(cmds).format(
            lock_file=handler.lock_file,
            local=handler.local_real,
            remote=handler.remote
        )

        log.info("(%s) %s", name, cmdstr)
        os.popen(cmdstr)
        log.debug("(%s) Resetting change time", name)
        handler.last_change = None

def abspath(path, resolve_links=False):
    path = os.path.expanduser(path)
    path = os.path.realpath(path) if resolve_links else os.path.abspath(path)
    return path.replace('\\', '/')

def relpath(path, start):
    path = path.replace('\\', '/')
    start = start.replace('\\', '/')
    return os.path.relpath(path, start).replace('\\', '/')

def load_config():
    """Look for a config file in the CWD and parse it."""
    global config, global_ignore

    config_path = os.path.join(CWD, CONFIG_NAME)
    if os.path.exists(config_path):
        log.info("Loading config from %s", config_path)
        with open(config_path, 'r') as config_file:
            config = json.load(config_file)
        log.debug("Config dict: %s", config)

        # get the global ignore list
        global_ignore = config.get('ignore', [])
        log.info("Global ignore list: %s", global_ignore)
    else:
        log.warning("No config file found in %s", CWD)
        sys.exit("Create %s first" % CONFIG_NAME)

def create_handlers():
    """Instantiates an event handler for each job defined in the config."""
    global handlers, observer

    jobs = config.get('jobs', [])
    job_count = len(jobs)
    if job_count:
        observer = Observer()
        log.debug("Created observer %s", observer)
        log.debug("Setting up %s jobs (%s)", job_count, ', '.join(jobs.keys()))
        handlers = {}
        for name, data in jobs.items():
            handler = Handler(observer, name, **data)
            handlers[name] = handler
        del jobs, job_count
        observer.start()
        log.info("Started watching directories")
    else:
        sys.exit("No jobs defined in config file")

if __name__ == '__main__':
    #TODO args
    # Set up and parse arguments
    #ap = ArgumentParser()
    #ap.add_argument("config", help="The config file to use.")
    #args = ap.parse_args()

    load_config()
    create_handlers()
    try:
        while True:
            sleep(1)
            tick()
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
