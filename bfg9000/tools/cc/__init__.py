import os
import re
from itertools import chain

from .. import mopack, pkg_config
from ... import log, options as opts, shell
from .compiler import CcCompiler, CcPchCompiler
from .linker import CcExecutableLinker, CcSharedLibraryLinker
from .rc import CcRcBuilder  # noqa: F401
from ..ar import ArLinker
from ..common import Builder, check_which
from ..ld import LdLinker
from ...exceptions import PackageResolutionError
from ...file_types import (HeaderDirectory, Library, SharedLibrary,
                           StaticLibrary)
from ...iterutils import uniques
from ...languages import known_formats
from ...packages import CommonPackage, Framework, PackageKind
from ...path import abspath, exists, Root
from ...platforms import parse_triplet
from ...versioning import detect_version


class CcBuilder(Builder):
    def __init__(self, env, langinfo, command, version_output):
        brand, version, target_flags = self._parse_brand(env, command,
                                                         version_output)
        super().__init__(langinfo.name, brand, version)
        self.object_format = env.target_platform.object_format

        name = langinfo.var('compiler').lower()
        ldinfo = known_formats['native']['dynamic']
        arinfo = known_formats['native']['static']

        # Try to infer the appropriate -fuse-ld option from the LD environment
        # variable.
        link_command = command[:]
        ld_command = env.getvar(ldinfo.var('linker'))
        if ld_command:
            tail = os.path.splitext(ld_command)[1][1:]
            if tail in ['bfd', 'gold']:
                log.info('setting `-fuse-ld={}` for `{}`'
                         .format(tail, shell.join(command)))
                link_command.append('-fuse-ld={}'.format(tail))

        cflags_name = langinfo.var('flags').lower()
        cflags = (target_flags +
                  shell.split(env.getvar('CPPFLAGS', '')) +
                  shell.split(env.getvar(langinfo.var('flags'), '')))

        ldflags_name = ldinfo.var('flags').lower()
        ldflags = (target_flags +
                   shell.split(env.getvar(ldinfo.var('flags'), '')))
        ldlibs_name = ldinfo.var('libs').lower()
        ldlibs = shell.split(env.getvar(ldinfo.var('libs'), ''))

        ar_name = arinfo.var('linker').lower()
        ar_command = check_which(env.getvar(arinfo.var('linker'), 'ar'),
                                 env.variables, kind='static linker')
        arflags_name = arinfo.var('flags').lower()
        arflags = shell.split(env.getvar(arinfo.var('flags'), 'cr'))

        # macOS's ld doesn't support --version, but we can still try it out and
        # grab the command line.
        ld_command = None
        try:
            stdout, stderr = env.execute(
                command + ldflags + ['-v', '-Wl,--version'],
                stdout=shell.Mode.pipe, stderr=shell.Mode.pipe,
                returncode='any'
            )

            for line in stderr.split('\n'):
                if '--version' in line:
                    ld_command = shell.split(line)[0:1]
                    if os.path.basename(ld_command[0]) != 'collect2':
                        break
        except (OSError, shell.CalledProcessError):
            pass

        compile_kwargs = {'command': (name, command),
                          'flags': (cflags_name, cflags)}
        self.compiler = CcCompiler(self, env, **compile_kwargs)
        try:
            self.pch_compiler = CcPchCompiler(self, env, **compile_kwargs)
        except ValueError:
            self.pch_compiler = None

        link_kwargs = {'command': (name, link_command),
                       'flags': (ldflags_name, ldflags),
                       'libs': (ldlibs_name, ldlibs)}
        self._linkers = {
            'executable': CcExecutableLinker(self, env, **link_kwargs),
            'shared_library': CcSharedLibraryLinker(self, env, **link_kwargs),
            'static_library': ArLinker(
                self, env, command=(ar_name, ar_command),
                flags=(arflags_name, arflags)
            ),
        }
        if ld_command:
            self._linkers['raw'] = LdLinker(self, env, ld_command, stdout)

        self.packages = CcPackageResolver(self, env, command, ldflags)
        self.runner = None

    @classmethod
    def _parse_brand(cls, env, command, version_output):
        target_flags = []
        if 'Free Software Foundation' in version_output:
            brand = 'gcc'
            version = detect_version(version_output)
            if env.is_cross:
                triplet = parse_triplet(env.execute(
                    command + ['-dumpmachine'],
                    stdout=shell.Mode.pipe, stderr=shell.Mode.devnull
                ).rstrip())
                target_flags = cls._gcc_arch_flags(
                    env.target_platform.arch, triplet.arch
                )
        elif 'clang' in version_output:
            brand = 'clang'
            version = detect_version(version_output)
            if env.is_cross:
                target_flags = ['-target', env.target_platform.triplet]
        else:
            brand = 'unknown'
            version = None

        return brand, version, target_flags

    @staticmethod
    def _gcc_arch_flags(arch, native_arch):
        if arch == native_arch:
            return []
        elif arch == 'x86_64':
            return ['-m64']
        elif re.match(r'i.86$', arch):
            return ['-m32'] if not re.match(r'i.86$', native_arch) else []
        return []

    @staticmethod
    def check_command(env, command):
        return env.execute(command + ['--version'], stdout=shell.Mode.pipe,
                           stderr=shell.Mode.devnull)

    @property
    def flavor(self):
        return 'cc'

    @property
    def family(self):
        return 'native'

    @property
    def auto_link(self):
        return False

    @property
    def can_dual_link(self):
        return True

    def linker(self, mode):
        return self._linkers[mode]


class CcPackageResolver:
    def __init__(self, builder, env, command, ldflags):
        self.builder = builder
        self.env = env

        self.include_dirs = [i for i in uniques(chain(
            self.builder.compiler.search_dirs(),
            self.env.host_platform.include_dirs
        )) if exists(i)]

        cc_lib_dirs = self.builder.linker('executable').search_dirs()
        try:
            sysroot = self.builder.linker('executable').sysroot()
            ld_lib_dirs = self.builder.linker('raw').search_dirs(sysroot, True)
        except (KeyError, OSError, shell.CalledProcessError):
            ld_lib_dirs = self.env.host_platform.lib_dirs

        self.lib_dirs = [i for i in uniques(chain(
            cc_lib_dirs, ld_lib_dirs, self.env.host_platform.lib_dirs
        )) if exists(i)]

    @property
    def lang(self):
        return self.builder.lang

    def header(self, name, search_dirs=None):
        if not search_dirs:
            search_dirs = self.include_dirs

        for base in search_dirs:
            if base.root != Root.absolute:
                raise ValueError('expected an absolute path')
            if exists(base.append(name)):
                return HeaderDirectory(base, None, system=True)

        raise PackageResolutionError("unable to find header '{}'".format(name))

    def library(self, name, kind=PackageKind.any, search_dirs=None):
        if not search_dirs:
            search_dirs = self.lib_dirs

        libnames = []
        if kind & PackageKind.shared:
            base = 'lib' + name + self.env.target_platform.shared_library_ext
            if self.env.target_platform.has_import_library:
                libnames.append((base + '.a', Library, {}))
            else:
                libnames.append((base, SharedLibrary, {}))
        if kind & PackageKind.static:
            libnames.append(('lib' + name + '.a', StaticLibrary,
                             {'lang': self.lang}))

        # XXX: Include Cygwin here too?
        if self.env.target_platform.family == 'windows':
            # We don't actually know what kind of library this is. It could be
            # a static library or an import library (which we classify as a
            # kind of shared lib).
            libnames.append((name + '.lib', Library, {}))

        for base in search_dirs:
            if base.root != Root.absolute:
                raise ValueError('expected an absolute path')
            for libname, libkind, extra_kwargs in libnames:
                fullpath = base.append(libname)
                if exists(fullpath):
                    return libkind(fullpath, format=self.builder.object_format,
                                   **extra_kwargs)

        raise PackageResolutionError("unable to find library '{}'"
                                     .format(name))

    def _resolve_path(self, name, submodules, format, kind, *, version=None,
                      get_version=None, usage={}):
        if usage.get('auto_link', False):  # pragma: no cover
            raise PackageResolutionError('package {!r} requires auto-link'
                                         .format(name))

        headers = usage.get('headers', [])
        libraries = mopack.to_frameworks(usage.get('libraries', []))
        include_path = [abspath(i) for i in usage.get('include_path', [])]
        library_path = [abspath(i) for i in usage.get('library_path', [])]

        compile_options = opts.option_list()
        link_options = opts.option_list()

        if headers:
            compile_options.extend(opts.include_dir(
                self.header(i, include_path)
            ) for i in headers)
        elif include_path:
            compile_options.extend(opts.include_dir(
                HeaderDirectory(i, None, system=True)
            ) for i in include_path)

        found_lib_path = None
        for i in libraries:
            if isinstance(i, Framework):
                link_options.append(opts.lib(i))
            elif i == 'pthread':
                compile_options.append(opts.pthread())
                link_options.append(opts.pthread())
            else:
                lib = self.library(i, kind, library_path)
                if not found_lib_path:
                    found_lib_path = lib.path.parent().string()
                link_options.append(opts.lib(lib))

        found_ver = None
        if get_version:
            header_dirs = [i.directory for i in compile_options
                           if isinstance(i, opts.include_dir)]
            found_ver = get_version(header_dirs, version)

        version_note = ' version {}'.format(found_ver) if found_ver else ''
        path_note = ' in {}'.format(found_lib_path) if found_lib_path else ''
        log.info('found package {!r}{} via path-search{}'
                 .format(name, version_note, path_note))
        return CommonPackage(
            name, submodules, format=format, version=found_ver,
            compile_options=compile_options, link_options=link_options
        )

    def resolve(self, name, submodules, version, kind, *, get_version=None):
        format = self.builder.object_format
        usage = mopack.try_usage(self.env, name, submodules)

        if usage['type'] == 'pkg-config':
            if len(usage['pcfiles']) != 1:
                raise PackageResolutionError('only one pkg-config file ' +
                                             'currently supported')
            return pkg_config.resolve(self.env, usage['pcfiles'][0], format,
                                      version, kind, usage['path'],
                                      usage['extra_args'])
        elif usage['type'] == 'path':
            return self._resolve_path(
                name, submodules, format, kind, version=version,
                get_version=get_version, usage=usage
            )
        elif usage['type'] == 'system':
            try:
                return pkg_config.resolve(self.env, name, format, version,
                                          kind)
            except (OSError, PackageResolutionError):
                return self._resolve_path(
                    name, submodules, format, kind, version=version,
                    get_version=get_version, usage=usage
                )
        else:
            raise PackageResolutionError('unsupported package usage {!r}'
                                         .format(usage['type']))
