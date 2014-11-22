autosync
========

Automatically mirror changes in local directories to a remote server.

### Requirements
Your system must have `rsync` available on the commandline.  On Windows, this can be accomplished by using [MSYS + MinGW](http://www.mingw.org/wiki/MSYS) or [Git for Windows](http://git-scm.com/download/win) + [Grsync](http://grsync-win.sourceforge.net/).

Run `pip install -r requirements.txt` to install the required Python packages

### Setup
Copy the autosync.json config file to the level above your project directories, and add an entry to the `jobs` dictionary for each one you want to keep in sync.

### Running
Navigate to the location of the config file and run `python /path/to/autosync.py`.

### Caveats
In the config file, only use / as the directory separator.  Internally, \ is replaced with / for cross-platform consistency, as Windows understands them just fine.
