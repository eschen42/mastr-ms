import sys
import os
from distutils.core import setup
from esky import bdist_esky
from esky.bdist_esky import Executable

from src.version import VERSION

freeze_includes = ['encodings','encodings.*', 'calendar', 'tendo']

freeze_excludes = [#'_ssl',  # Exclude _ssl
                      'pyreadline', 'doctest',
                      'pickle', 'calendar']  # Exclude standard library

# GUI applications require a different base on Windows (the default is for a
# console application).
base = None
options = None
if sys.platform == "win32":
    base = "Win32GUI"
    import py2exe

    py2exe_options = dict(
        ascii=True,  # Exclude encodings
        excludes=freeze_excludes,
        dll_excludes=['MSVCP90.dll'],  # Exclude msvcr71
        includes=freeze_includes,
        compressed=True,  # Compress library.zip
    )

    bdist_esky_options_py2exe = dict(
        #includes = freeze_includes,
        freezer_options = py2exe_options,
        freezer_module = 'py2exe')

    options = {'py2exe': py2exe_options, 'bdist_esky' : bdist_esky_options_py2exe}

    # I can use this to fool py2exe! Docs -
    #http://www.py2exe.org/index.cgi/OverridingCriteraForIncludingDlls
    origIsSystemDLL = py2exe.build_exe.isSystemDLL

    def isSystemDLL(pathname):
        if os.path.basename(pathname).lower() in ("msvcp90.dll","gdiplus.dll", "msvcr71.dll"):
            return 0
        return origIsSystemDLL(pathname)

    py2exe.build_exe.isSystemDLL = isSystemDLL

    mdatasync_target = Executable(
        gui_only = True,
        description = "Application to sync data from clients to server",
        script = "main.py",
        dest_base = "mdatasync")

    mssimulator_target = Executable(
        gui_only = True,
        description = "A tool to generate sample mass spec files from worklists",
        script = "Simulator.py",
        dest_base = "simulator")

    scripts=[mdatasync_target, mssimulator_target]

setup(name='msDataSync',
      version=VERSION,
      description='msDataSync application',
      author='CCG/Brad Power',
      package_dir={"mdatasync_client": "src",
                   "mdatasync_client.test": "test"},
      packages=[
          "mdatasync_client",
          "mdatasync_client.test",
          "mdatasync_client.plogging",
          "esky",
          "httplib2",
          "yaphc",
          "tendo",
      ],
      entry_points={
          "gui_scripts": [
              "msdatasync=mdatasync_client.main:main",
              "simulator=mdatasync_client.Simulator:main",
          ]
      },
      options=options, # fixme: not recognized by distutils
      )
