import os.path
import re
import warnings

from .hooks import builtin
from .find import find
from ..file_types import Executable, Package
from ..iterutils import iterate, listify
from ..objutils import objectify
from ..path import Path, Root, which
from ..tools.utils import SystemPackage
from ..versioning import check_version, SpecifierSet, Version


@builtin.globals('env')
@builtin.type(Package)
def package(env, name, lang='c', kind='any', version=None, header=None,
            header_only=False):
    if kind not in ('any', 'shared', 'static'):
        raise ValueError("kind must be one of 'any', 'shared', or 'static'")
    version = objectify(version or '', SpecifierSet)

    return env.builder(lang).packages.resolve(name, kind, version, header,
                                              header_only)


# XXX: Remove this after 0.3 is released.
@builtin.globals('builtins')
def system_package(builtins, name, lang='c', kind='any', header=None):
    warnings.warn('system_package is deprecated; please use package instead',
                  DeprecationWarning)
    return builtins['package'](name, lang=lang, kind=kind, header=header)


# XXX: Remove this after 0.3 is released.
@builtin.globals('builtins')
def pkgconfig_package(builtins, name, lang='c', version=None):
    warnings.warn('pkgconfig_package is deprecated; please use package ' +
                  'instead', DeprecationWarning)
    return builtins['package'](name, version=version)


@builtin.globals('env')
@builtin.type(Executable)
def system_executable(env, name, format=None):
    return Executable(
        Path(which(name, env.variables, resolve=True), Root.absolute),
        format or env.platform.object_format, external=True
    )


def _boost_version(header, required_version=None):
    version_hpp = header.path.append('boost').append('version.hpp')
    with open(version_hpp.string()) as f:
        for line in f:
            m = re.match(r'#\s*define\s+BOOST_LIB_VERSION\s+"([\d_]+)"', line)
            if m:
                version = Version(m.group(1).replace('_', '.'))
                check_version(version, required_version, 'Boost')
                return version
    raise IOError('unable to parse "boost/version.hpp"')


# XXX: This is a bit hacky, and we should try to make it work a little more
# like the package() function above.
@builtin.globals('env')
def boost_package(env, name=None, version=None):
    final_name = 'boost({})'.format(','.join(iterate(name)))
    version = objectify(version or '', SpecifierSet)
    pkg = env.builder('c++').packages
    version_hpp = 'boost/version.hpp'

    root = env.getvar('BOOST_ROOT')
    incdir = env.getvar('BOOST_INCLUDEDIR', os.path.join(root, 'include')
                        if root else None)
    libdir = env.getvar('BOOST_LIBRARYDIR', os.path.join(root, 'lib')
                        if root else None)

    if incdir:
        header = pkg.header(version_hpp, [incdir])
        boost_version = _boost_version(header, version)
    else:
        # On Windows, check the default install location, which is structured
        # differently from other install locations.
        if env.platform.name == 'windows':
            dirs = find(r'C:\Boost\include', 'boost-*', type='d', flat=True)
            if dirs:
                try:
                    header = pkg.header(version_hpp, [max(dirs)])
                    boost_version = _boost_version(header, version)
                    return SystemPackage(
                        final_name,
                        includes=[header],
                        lib_dirs=[r'C:\Boost\lib'],
                        version=boost_version
                    )
                except IOError:
                    pass

        header = pkg.header(version_hpp)
        boost_version = _boost_version(header, version)

    if env.platform.name == 'windows':
        if not env.builder('c++').auto_link:
            # XXX: Don't require auto-link.
            raise ValueError('Boost on Windows requires auto-link')
        return SystemPackage(
            final_name,
            includes=[header],
            lib_dirs=listify(libdir),
            version=boost_version
        )
    else:
        dirs = [libdir] if libdir else None
        return SystemPackage(
            final_name,
            includes=[header],
            libraries=[pkg.library('boost_' + i, search_dirs=dirs)
                       for i in iterate(name)],
            version=boost_version
        )
