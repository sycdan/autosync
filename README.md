autosync
========

Automatically rsync changed dirs to a remote server.

### Requirements
Your system must have `rsync` available on the commandline.  On Windows, this can be accomplished by using [MinGW +  MSYS](http://www.mingw.org/wiki/MSYS).

Run `pip install -r requirements.txt` to install the required Python packages

### Setup
Copy autosync.json to the level above your project directories, and add an entry to the `jobs` dictionary for each one you want to keep in sync.

### Running
Navigate to the location of the config file and run `python /path/to/autosync.py`.

### Caveats
In the config file, only use / as the directory separator.  Internally, \s are replaced with / for corss-platform consistency, as Windows understands them just fine.
