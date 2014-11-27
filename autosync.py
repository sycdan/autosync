#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import print_function
from __future__ import unicode_literals
from multiprocessing import Process
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
DELAY = 1.5  # seconds

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
    return path.replace('\\', '/')

def reslash(path):
    return path.replace('/', os.sep)

class Job(FileSystemEventHandler):
    changed_dirs = []

    def __repr__(self):
        return "<Job: {0}>".format(self.name)

    def __init__(self, observer, name, local, remote, ignore=[]):
        log.debug("Initializing job: %s", name)
        super(Job, self).__init__()
        self.name = name
        self.last_change = None
        self.remote = remote

        self.local_rel = ".{0}{1}".format(os.sep, reslash(local))
        self.local_abs = os.path.abspath(local)

        log.debug("%s Local rel: %s; Local abs: %s; Remote: %s", self, self.local_rel, self.local_abs, remote)

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
        ignore_list = global_ignore + ignore
        for i, path in enumerate(ignore_list):
            if path.startswith('/'):
                path = '^' + path.lstrip('/')
            if not path.endswith('*'):
                path = path + '$'
            path = path.replace('.', '\.')
            path = path.replace('*', '.*')
            ignore_list[i] = path
        log.debug("%s Regexified ignore list: %s", self, ignore_list)
        self.ignore_re = re.compile('|'.join(ignore_list), re.IGNORECASE)

        log.debug("%s Adding handler to observer", self)
        observer.schedule(self, self.local_abs, recursive=True)

        log.debug("%s Watching queue: %s", self, self.work_dir)
        self.queue_watcher = Process(target=self.watch_queue)
        self.queue_watcher.start()

        log.info("%s %s --> %s (ignoring %s)",
            self, self.local_abs, remote, 'only globals' if not ignore else ', '.join(ignore))

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
        abs_path = abspath(event.src_path)
        abs_dir = os.path.dirname(abs_path)
        rel_path = relpath(abs_path, self.local_abs)
        rel_dir = os.path.dirname(rel_path)
        if rel_dir == '': rel_dir = '.'

        if self.ignore_re.match(rel_path):
            log.debug("(%s) Ignored file changed: %s", self.name, abs_path)
            return

        self.last_change = time()

        log.debug(
            "(%s) New event:-\nAP: %s\nAD: %s\nRP: %s\nRD: %s",
            self.name, abs_path, abs_dir, rel_path, rel_dir
        )

        # check if we're already going to be syncing this dir
        if rel_dir in self.changed_dirs:
            return

        log.debug("(%s) Dir added to sync queue: %s", self.name, rel_dir)
        self.changed_dirs.append(rel_dir)

    def initial_sync(self):
        os.walk()
        exit()
        pass

    def write_queue(self):
        """Write the list of dirs to include in a sync to a file in the queue dir."""

        # pop elements off the list of changed dirs until it's empty, in case more are added while we're working
        dirs = []
        while self.changed_dirs:
            path = self.changed_dirs.pop()
            if path not in dirs:
                dirs.append(path)
        self.last_change = 0
        log.info("%s Synchronizing changes in %s", self, ', '.join(dirs))

        lines = []
        for d in dirs:
            d = '' if d == '.' else d.strip(os.sep)
            lines.append(d + '/*')  # include all files in this dir
            # include the dir itself, and all its parents, with no trailing slashes (this in an rsync requirement)
            while True:
                if d == '': break
                lines.append(d)
                d, _ = os.path.split(d)
        lines = sorted(lines)
        log.debug("%s Lines to write to queue file: %s", self, lines)

        # determine the name of the queue file by getting the last item in the queue, if any,
        # and converting the filename back to a number
        num = 0
        queue_files = self.queue_files()
        if len(queue_files):
            num = int(os.path.splitext(os.path.basename(queue_files[-1]))[0])
        num += 1
        queue_file = os.path.join(self.work_dir, "{0}".format(num))
        log.debug("%s Writing to queue file: %s", self, queue_file)
        with open(queue_file, 'wb') as f:
            f.write('\n'.join(lines).encode('utf-8'))

    def queue_files(self):
        """
        Returns the files in the queue dir as a sorted list.
        Files are named as integers, with the lowest number being the highest priority.
        """
        ret = []
        files = sorted(os.listdir(self.work_dir))
        for file in files:
            path = os.path.join(self.work_dir, file)

            # skip dirs
            if not os.path.isfile(path):
                continue

            # check it's a queue file valid name (can be cast to int)
            try:
                num = int(file)
            except ValueError:
                continue

            ret.append(path)
        return ret

    def watch_queue(self):
        """
        Monitors the queue dir for lists of dirs to sync, and calls rsync when necessary.
        Started as a subprocess when the job is created.
        """
        try:
            while True:
                sleep(0.5)
                try:  # get the first item in the queue
                    queue_file = self.queue_files().pop(0)
                except IndexError:  # the queue was empty
                    continue
                args = [
                   'rsync',
                   #'-a',  # archive mode; equals -rlptgoD (no -H,-A,-X)
                   '-r',  # recurse into directories
                   '-z',  # compress file data during the transfer
                   '-q',  # suppress non-error messages
                   '--delete',  # delete extraneous files from destination dirs
                   '--include-from={0}'.format(queue_file),
                   '--exclude=*',
                   '{0}/'.format(fslash(self.local_rel)),
                   '{0}/'.format(self.remote)
                ]
                log.debug("%s Running command: %s", self, ' '.join(args))
                start_time = time()
                subprocess.call(args)
                log.info("%s Sync done in %d seconds", self, time() - start_time)

                log.debug("%s Removing queue file: %s", self, queue_file)
                os.remove(queue_file)
        except KeyboardInterrupt:
            log.debug("%s Stopped watching queue", self)

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
            job.initial_sync()
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
            # check each job to see if it's ready to sync
            for _, job in jobs.items():
                if job.last_change and time() > job.last_change + DELAY:
                    job.write_queue()
    except KeyboardInterrupt:
        observer.stop()
        log.info("Normal shutdown")
    observer.join()
