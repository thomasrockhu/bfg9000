import re
from collections import namedtuple
from enum import Enum
from six import iteritems, string_types
from six.moves import cStringIO as StringIO

from ... import path
from ... import safe_str
from ... import iterutils
from ...platforms import platform_name
from ...shell import posix as pshell

__all__ = ['Call', 'Entity', 'Function', 'Makefile', 'Pattern', 'Section',
           'Syntax', 'Writer', 'Variable', 'var', 'qvar', 'silent',
           'path_vars']

Rule = namedtuple('Rule', ['targets', 'deps', 'order_only', 'recipe',
                           'variables', 'phony'])
Include = namedtuple('Include', ['name', 'optional'])

Syntax = Enum('Syntax', ['target', 'dependency', 'function', 'shell', 'clean'])
Section = Enum('Section', ['path', 'command', 'flags', 'other'])


class Writer(object):
    # Don't escape ":" if we're using Windows paths.
    __extra_escapes = '' if platform_name() == 'windows' else ':'
    __target_ex = re.compile(r'(\\*)([#?*\[\]~\s%{}])'.format(__extra_escapes))
    __dep_ex = re.compile(r'(\\*)([#?*\[\]~\s|%{}])'.format(__extra_escapes))

    def __init__(self, stream):
        self.stream = stream

    @classmethod
    def escape_str(cls, string, syntax):
        def repl(match):
            return match.group(1) * 2 + '\\' + match.group(2)

        if '\n' in string:
            raise ValueError('illegal newline')
        result = string.replace('$', '$$')

        if syntax == Syntax.target:
            return cls.__target_ex.sub(repl, result)
        elif syntax == Syntax.dependency:
            return cls.__dep_ex.sub(repl, result)
        elif syntax == Syntax.function:
            return result.replace(',', '$,')
        elif syntax in [Syntax.shell, Syntax.clean]:
            return result
        else:
            raise ValueError("unknown syntax '{}'".format(syntax))

    def write_literal(self, string):
        self.stream.write(string)

    def write(self, thing, syntax, shell_quote=pshell.quote_info):
        thing = safe_str.safe_str(thing)
        shelly = syntax in [Syntax.function, Syntax.shell]
        escaped = False

        if isinstance(thing, safe_str.escaped_str):
            self.write_literal(thing.string)
            escaped = True
        elif isinstance(thing, string_types):
            if shelly and shell_quote:
                thing, escaped = shell_quote(thing)
            self.write_literal(self.escape_str(thing, syntax))
        elif isinstance(thing, safe_str.jbos):
            for i in thing.bits:
                escaped |= self.write(i, syntax, shell_quote)
        elif isinstance(thing, path.Path):
            out = Writer(StringIO())
            thing = thing.realize(path_vars, shelly)
            escaped = out.write(thing, syntax, pshell.escape)

            thing = out.stream.getvalue()
            if shelly and escaped:
                thing = pshell.quote_escaped(thing)
            self.write_literal(thing)
        else:
            raise TypeError(type(thing))

        return escaped

    def write_each(self, things, syntax, delim=safe_str.escaped_str(' '),
                   prefix=None, suffix=None, shell_quote=pshell.quote_info):
        for i in iterutils.tween(things, delim, prefix, suffix):
            self.write(i, syntax, shell_quote)

    def write_shell(self, thing, syntax=Syntax.shell):
        if iterutils.isiterable(thing):
            self.write_each(thing, syntax)
        else:
            self.write(thing, syntax, shell_quote=None)


class Pattern(object):
    def __init__(self, path):
        if len(re.findall(r'([^\\]|^)(\\\\)*%', path)) != 1:
            raise ValueError('exactly one % required')
        self.path = path

    def use(self):
        bits = re.split(r'%', self.path)
        return safe_str.join(bits, safe_str.escaped_str('%'))

    def _safe_str(self):
        return self.use()

    def __str__(self):
        raise NotImplementedError()

    def __repr__(self):
        return repr(self.use())

    def __hash__(self):
        return hash(self.path)

    def __eq__(self, rhs):
        return self.path == rhs.path

    def __add__(self, rhs):
        return self.use() + rhs

    def __radd__(self, lhs):
        return lhs + self.use()


class Entity(object):
    def __init__(self, name):
        self.name = name

    def use(self):
        raise NotImplementedError()

    def _safe_str(self):
        return self.use()

    def __str__(self):
        raise NotImplementedError()

    def __repr__(self):
        return repr(self.use())

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, rhs):
        return self.name == rhs.name

    def __add__(self, rhs):
        return self.use() + rhs

    def __radd__(self, lhs):
        return lhs + self.use()


class Variable(Entity):
    def __init__(self, name, quoted=False):
        Entity.__init__(self, re.sub(r'[\s:#=]', '_', name))
        self.quoted = quoted

    def use(self):
        fmt = '${}' if len(self.name) == 1 else '$({})'
        if self.quoted:
            fmt = pshell.quote_escaped(fmt)
        return safe_str.escaped_str(fmt.format(self.name))


def var(v, quoted=False):
    return v if isinstance(v, Variable) else Variable(v, quoted)


def qvar(v):
    return var(v, True)


class Function(Entity):
    def __init__(self, name, *args, **kwargs):
        Entity.__init__(self, name)
        self.args = args
        self.quoted = kwargs.get('quoted', False)

    def use(self):
        esc = safe_str.escaped_str
        kwargs = {'shell_quote': None} if self.quoted else {}

        out = Writer(StringIO())
        prefix = '$(' + self.name + ' '
        for i in iterutils.tween(self.args, esc(','), esc(prefix), esc(')')):
            out.write_each(iterutils.iterate(i), Syntax.function, **kwargs)
        result = out.stream.getvalue()

        if self.quoted:
            result = pshell.quote_escaped(result)
        return safe_str.escaped_str(result)


class Call(Function):
    def __init__(self, func, *args):
        Function.__init__(self, 'call', var(func).name, *args)


def silent(command):
    if isinstance(command, list):
        return ['@' + command[0]] + command[1:]
    else:
        return '@' + command


path_vars = {
    path.Root.srcdir:   Variable('srcdir'),
    path.Root.builddir: None,
}
path_vars.update({i: Variable(i.name) for i in path.InstallRoot})


class Makefile(object):
    def __init__(self):
        self._var_table = set()
        self._global_variables = {i: [] for i in Section}
        self._target_variables = []
        self._defines = []

        self._rules = []
        self._targets = set()
        self._includes = []

    def variable(self, name, value, section=Section.other, exist_ok=False):
        name, exists = self._unique_var(name, exist_ok)
        if not exists:
            self._global_variables[section].append((name, value))
        return name

    def target_variable(self, name, value, exist_ok=False):
        name, exists = self._unique_var(name, exist_ok)
        if not exists:
            self._target_variables.append((name, value))
        return name

    def define(self, name, value, exist_ok=False):
        name, exists = self._unique_var(name, exist_ok)
        if not exists:
            self._defines.append((name, value))
        return name

    def has_variable(self, name):
        return var(name) in self._var_table

    def _unique_var(self, name, exist_ok):
        name = var(name)
        exists = self.has_variable(name)
        if exists and not exist_ok:
            raise ValueError("variable {!r} already exists".format(name))
        self._var_table.add(name)
        return name, exists

    def include(self, name, optional=False):
        self._includes.append(Include(name, optional))

    def rule(self, target, deps=None, order_only=None, recipe=None,
             variables=None, phony=False):
        variables = {var(k): v for k, v in iteritems(variables or {})}

        targets = iterutils.listify(target)
        if len(targets) == 0:
            raise ValueError('must have at least one target')
        for i in targets:
            if self.has_rule(i):
                raise ValueError("rule for '{}' already exists".format(i))
            self._targets.add(i)
        self._rules.append(Rule(
            targets, iterutils.listify(deps), iterutils.listify(order_only),
            recipe, variables, phony
        ))

    def has_rule(self, name):
        return name in self._targets

    def _write_variable(self, out, name, value, syntax=Syntax.shell,
                        target=None):
        if target:
            out.write(target, Syntax.target)
            out.write_literal(': ')
        out.write_literal(name.name + ' := ')
        out.write_shell(value, syntax)
        out.write_literal('\n')

    def _write_define(self, out, name, value):
        out.write_literal('define ' + name.name + '\n')
        for line in value:
            out.write_shell(line)
            out.write_literal('\n')
        out.write_literal('endef\n\n')

    def _write_rule(self, out, rule):
        if rule.variables:
            for target in rule.targets:
                for name, value in iteritems(rule.variables):
                    self._write_variable(out, name, value, target=target)

        if rule.phony:
            out.write_literal('.PHONY: ')
            out.write_each(rule.targets, Syntax.dependency)
            out.write_literal('\n')

        out.write_each(rule.targets, Syntax.target)
        out.write_literal(':')

        esc = safe_str.escaped_str
        out.write_each(rule.deps, Syntax.dependency, prefix=esc(' '))
        out.write_each(rule.order_only, Syntax.dependency, prefix=esc(' | '))

        if isinstance(rule.recipe, Entity):
            out.write_literal(' ; ')
            out.write_shell(rule.recipe)
        elif rule.recipe is not None:
            for cmd in rule.recipe:
                out.write_literal('\n\t')
                out.write_shell(cmd)
        out.write_literal('\n\n')

    def write(self, out):
        out = Writer(out)

        # Don't let make use built-in suffix rules.
        out.write_literal('.SUFFIXES:\n')

        # Necessary for escaping commas in function calls.
        self._write_variable(out, Variable(','), ',')
        out.write_literal('\n')

        for section in Section:
            # The built-in paths don't need shell quoting because they're used
            # by other paths, which *are* quoted.
            syntax = Syntax.clean if section == Section.path else Syntax.shell
            for name, value in self._global_variables[section]:
                self._write_variable(out, name, value, syntax)
            if self._global_variables[section]:
                out.write_literal('\n')

        target = Pattern('%')
        for name, value in self._target_variables:
            self._write_variable(out, name, value, target=target)
        if self._target_variables:
            out.write_literal('\n')

        for name, value in self._defines:
            self._write_define(out, name, value)

        for r in self._rules:
            self._write_rule(out, r)

        for i in self._includes:
            out.write_literal(('-' if i.optional else '') + 'include ')
            out.write(i.name, Syntax.target)
            out.write_literal('\n')
