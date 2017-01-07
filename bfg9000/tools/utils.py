import os
import subprocess
import warnings
from six.moves import zip

from .. import shell
from ..file_types import Package
from ..iterutils import listify
from ..path import Path, which


class Command(object):
    def __init__(self, env, command):
        self.env = env
        self.command = command

    def run(self, *args, **kwargs):
        env = kwargs.pop('env', self.env.variables)
        # XXX: Use shell mode so that the (user-defined) command can have
        # multiple arguments defined in it?
        return shell.execute(
            self(self.command, *args, **kwargs),
            env=env, quiet=True
        )


class SimpleCommand(Command):
    def __init__(self, env, var, default, kind='executable'):
        command = check_which(env.getvar(var, default), env.variables, kind)
        Command.__init__(self, env, command)


class SystemPackage(Package):
    def __init__(self, includes=None, lib_dirs=None, libraries=None,
                 version=None):
        self.includes = includes or []
        self.lib_dirs = lib_dirs or []
        self.all_libs = libraries or []
        self.version = version

    def cflags(self, compiler, output):
        return compiler.args(self, output, pkg=True)

    def ldflags(self, linker, output):
        return linker.args(self, output, pkg=True)

    def ldlibs(self, linker, output):
        return linker.libs(self, output, pkg=True)


def check_which(names, env=os.environ, kind='executable'):
    names = listify(names)
    try:
        return which(names, env, first_word=True)
    except IOError:
        warnings.warn("unable to find {kind}{filler} {names}".format(
            kind=kind, filler='; tried' if len(names) > 1 else '',
            names=', '.join("'{}'".format(i) for i in names)
        ))

        # Assume the first name is the best choice.
        return names[0]


def darwin_install_name(library):
    return os.path.join('@rpath', library.path.basename())
