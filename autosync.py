#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import print_function
from __future__ import unicode_literals
from multiprocessing import Process, Pipe
import argparse
import logging
log = logging.getLogger(__name__)
import os
import sys
import re
import json
import subprocess
import shutil
from time import time, sleep
from argparse import ArgumentParser
from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileModifiedEvent,
    FileDeletedEvent,
    FileCreatedEvent
)


CWD = os.getcwd()
CONFIG_FILE = os.path.join(CWD, 'autosync.json')
WORK_DIR = os.path.join(CWD, '.autosync')
TIMEOUT = 3  # seconds

# TODO: fix the fact that logging must be set up here instead of at the bottom so it can be used by subprocesses
parser = argparse.ArgumentParser()
group = parser.add_mutually_exclusive_group()
group.add_argument('-v', '--verbose', help="increase verbosity (show debugging messages)",
    action='store_const', const=logging.DEBUG, dest='loglevel')
group.add_argument('-q', '--quiet', help="decrease verbosity (only show warnings)",
    action='store_const', const=logging.WARNING, dest='loglevel')
args = parser.parse_args()

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    level=args.loglevel or logging.INFO
)

def fslash(path):
    ret = path.replace('\\', '/')
    while '//' in ret:
        ret = ret.replace('//', '/')
    return ret

class Job(FileSystemEventHandler):
    changed_dirs = []

    def __repr__(self):
        return "<Job: {0}>".format(self.name)

    def __init__(self, observer, name, local, remote, ignore=[]):
        log.debug("Initializing job: %s", name)
        super(Job, self).__init__()
        self.name = name
        self.last_change = 0

        if os.path.isabs(local):
            log.critical("%s Local path must be relative to %s", self, CWD)
            sys.exit(1)

        self.local_rel = os.path.relpath(local, CWD)
        self.local_abs = os.path.join(CWD, local)
        if not os.path.exists(self.local_abs):
            log.critical("%s Local path does not exist: %s", self, self.local_abs)
            sys.exit(1)

        self.remote = remote

        log.debug("%s Local rel: %s; Local abs: %s; Remote: %s", self, self.local_rel, self.local_abs, self.remote)

        self.work_dir = os.path.join(WORK_DIR, name)
        if os.path.exists(self.work_dir):
            log.debug("%s Deleting old work directory: %s", self, self.work_dir)
            shutil.rmtree(self.work_dir)

        attempts = 0
        while True:
            if attempts >= 10:
                raise Exception("Couldn't create directory: {0}".format(self.work_dir))
            log.debug("%s Creating work directory: %s (%s failed attempts)", self, self.work_dir, attempts)
            try:
                os.makedirs(self.work_dir)
                break
            except Exception as e:
                attempts += 1
                log.warning(e)

        # build a regular expression for the ignore list
        ignore_regexes = []
        ignore_list = global_ignore + ignore
        for i, path in enumerate(ignore_list):
            path = fslash(path)  # ensure all slashes are forward, for easy matching
            path = path.replace('.', '\\.')  # match literal dots in the regex
            path = path.replace('*', '.*')  # turen glob wildcards into regex ones
            ignore_regexes.append('^' + path + '$')  # the file/dir itself
            ignore_regexes.append('^' + path + '/.*')  # any children, if it's a dir
        log.debug("%s Regexified ignore list: %s", self, ignore_regexes)
        self.ignore_re = re.compile('|'.join(ignore_regexes), re.IGNORECASE)

        # create an exclude file for rsync
        self.exclude_file = os.path.join(self.work_dir, 'exclude.txt')
        with open(self.exclude_file, 'wb') as f:
            for path in ignore_list:
                f.write('{0}{1}'.format(path, os.linesep).encode('utf-8'))

        log.debug("%s Adding handler to observer", self)
        observer.schedule(self, self.local_abs, recursive=True)

        log.info("%s %s --> %s (ignoring %s)",
            self, self.local_abs, remote, 'only globals' if not ignore else ', '.join(ignore))

        log.debug("%s Starting main loop", self)
        self.parent_pipe, child_pipe = Pipe()
        self.child_process = Process(target=self.loop, args=(child_pipe,))
        self.child_process.start()

    def on_any_event(self, event):
        """
        When a file changes, if it's not in the ignore list, alert the child process.
        """
        log.debug("%s %s", self, event)
        rel_path = fslash(os.path.relpath(event.src_path, self.local_abs))
        if self.ignore_re.match(rel_path):
            log.debug("(%s) Ignored file changed: %s", self.name, rel_path)
            return
        self.parent_pipe.send(time())

    def sync(self):
        """
        Actually call rsync.
        """
        log.info("%s Starting%s sync", self, ' initital' if self.last_change == -1 else '')
        self.last_change = 0
        args = [
           'rsync',
           #'-a',  # archive mode; equals -rlptgoD (no -H,-A,-X)
           '-r',  # recurse into directories
           '-z',  # compress file data during the transfer
           '-q',  # suppress non-error messages
           '--delete',  # delete extraneous files from destination dirs
           '--exclude-from={0}'.format(self.exclude_file),
           fslash(self.local_rel).rstrip('/') + '/',
           fslash(self.remote).rstrip('/') + '/',
           '--chmod=ugo=rwX'  # use remote default permissions for new files
        ]
        log.debug("%s Running command: %s", self, ' '.join(args))
        start_time = time()
        subprocess.call(args)
        log.info("%s Sync done in %d seconds", self, time() - start_time)

    def loop(self, pipe):
        """
        Wait for changes to be made by the parent process and start a sync when the timeout expires.
        """
        try:
            self.last_change = -1  # force initial sync
            while True:
                # check if a change was posted
                if pipe.poll(0.1):  # block for 100ms
                    self.last_change = pipe.recv()
                    log.debug("%s Change logged at %s", self, self.last_change)
                # check if the change timeout has expired, and sync if so
                if not self.last_change and time() > self.last_change + TIMEOUT:
                    self.sync()
        except KeyboardInterrupt:
            log.debug("%s Stopped watching for changes", self)

def load_config():
    """Look for a config file in the CWD and parse it."""
    global global_ignore

    if os.path.exists(CONFIG_FILE):
        log.info("Loading config from %s", CONFIG_FILE)
        with open(CONFIG_FILE, 'r') as config_file:
            config = json.load(config_file)
        log.debug("Config dict: %s", config)

        # get the global ignore list
        global_ignore = config.get('ignore', [])
        log.info("Global ignore list: %s", global_ignore)
        return config
    else:
        log.warning("No config file found in %s", CWD)
        sys.exit("Create %s first" % CONFIG_NAME)

def create_jobs(config):
    """Instantiates an event handler for each job defined in the config."""
    jobs = {}
    jobs_ = config.get('jobs', {})
    job_count = len(jobs_)
    if job_count:
        log.debug("Starting %s jobs (%s)", job_count, ', '.join(jobs_.keys()))
        for name, data in jobs_.items():
            job = Job(observer, name, **data)
            jobs[name] = job
        del jobs_, job_count
        try:
            observer.start()
        except FileNotFoundError as e:
            log.error(e.args[1])
            sys.exit(1)
        return jobs
    else:
        sys.exit("No jobs defined in config file")

if __name__ == '__main__':
    if not os.path.exists(WORK_DIR):
        os.makedirs(WORK_DIR)

    observer = Observer()
    config = load_config()
    jobs = create_jobs(config)
    try:
        while True:
            sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        log.info("Normal shutdown")
    observer.join()
