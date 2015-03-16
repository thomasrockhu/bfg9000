import cStringIO
import os
import re
from collections import OrderedDict
from itertools import chain

import cc_toolchain
import node
from languages import ext2lang, lang

__rule_handlers__ = {}
def rule_handler(rule_name):
    def decorator(fn):
        __rule_handlers__[rule_name] = fn
        return fn
    return decorator

def rule_name(name):
    if name == 'c++':
        return 'cxx'
    return name

def use_var(name):
    return '$({})'.format(name)

class MakeWriter(object):
    def __init__(self):
        self._var_table = set()
        self._variables = []
        self._includes = []
        self._rules = []

    def escape_var_name(self, name):
        return re.sub('/', '_', name)

    def unique_var_name(self, name):
        name = self.escape_var_name(name)
        if name in self._var_table:
            i = 2
            fmt = name + '_{}'
            while True:
                name = fmt.format(i)
                if name not in self._var_table:
                    break
                i += 1
            self._var_table.add(name)
        return name

    def variable(self, name, value, make_unique=False):
        if make_unique:
            name = self.unique_var_name(name)
        else:
            name = self.escape_var_name(name)
            if self.has_variable(name):
                raise RuntimeError('variable "{}" already exists'.format(name))

        out = cStringIO.StringIO()
        self._write_variable(out, name, value)
        self._var_table.add(name)
        self._variables.append(out.getvalue())
        return name

    def has_variable(self, name):
        return name in self._var_table

    def _write_variable(self, out, name, value):
        out.write('{name} := {value}\n'.format(
            name=name, value=value
        ))

    def include(self, name, optional=False):
        self._includes.append('{opt}include {name}\n'.format(
            name=name, opt='-' if optional else ''
        ))

    def rule(self, target, deps, recipe, variables=None, phony=False):
        out = cStringIO.StringIO()
        if variables:
            for k, v in variables.iteritems():
                self._write_variable(out, k, v)
        if phony:
            out.write('.PHONY: {}\n'.format(target))
        out.write('{target}:{deps}\n'.format(
            target=target,
            deps=''.join((' ' + i for i in deps))
        ))
        for cmd in recipe:
            out.write('\t{}\n'.format(cmd))
        out.write('\n')
        self._rules.append(out.getvalue())

    def write(self, out):
        for v in self._variables:
            out.write(v)
        if self._variables:
            out.write('\n')

        for r in self._rules:
            out.write(r)

        for i in self._includes:
            out.write(i)
        if self._includes:
            out.write('\n')


def write(path, targets):
    writer = MakeWriter()
    for rule in targets:
        __rule_handlers__[rule.kind](writer, rule)
    with open(os.path.join(path, 'Makefile'), 'w') as out:
        writer.write(out)

def cmd_var(writer, lang):
    var = rule_name(lang).upper()
    if not writer.has_variable(var):
        writer.variable(var, cc_toolchain.command_name(lang))
    return var

__seen_compile_rules__ = set() # TODO: put this somewhere else (on the writer?)

@rule_handler('object_file')
def emit_object_file(writer, rule):
    base, ext = os.path.splitext(cc_toolchain.target_name(rule['file']))

    def compile_recipe(lang):
        return [
            cc_toolchain.compile_command(
                cmd=use_var(cmd_var(writer, lang)), input='$<',
                output='$@', dep='$*.d'
            ),
            "@sed -e 's/.*://' -e 's/\\$$//' < $*.d | fmt -1 | \\",
            "  sed -e 's/^ *//' -e 's/$$/:/' >> $*.d"
        ]

    if ext2lang[ext] == rule['lang']:
        if ext not in __seen_compile_rules__:
            __seen_compile_rules__.add(ext)
            writer.rule(
                target=cc_toolchain.target_name(node.Node('%', 'object_file')),
                deps=['%' + ext], recipe=compile_recipe(rule['lang'])
            )
    else:
        writer.rule(target=cc_toolchain.target_name(rule),
                    deps=[rule['file'].name],
                    recipe=compile_recipe(rule['lang']))

    writer.include(base + '.d', True)

def emit_link(writer, rule, var_prefix):
    variables = {}
    lib_deps = [i for i in rule['libs'] if i.kind != 'external_library']

    if len(rule.deps) == 0 and len(lib_deps) == 0:
        inputs = ['$^']
        deps = [cc_toolchain.target_name(i) for i in rule['files']]
    else:
        if len(rule['files']) > 1:
            var_name = writer.unique_var_name('{}{}_OBJS'.format(
                var_prefix, rule.name.upper()
            ))
            variables[var_name] = ' '.join(
                (cc_toolchain.target_name(i) for i in rule['files'])
            )
            inputs = [use_var(var_name)]
        else:
            inputs = [cc_toolchain.target_name(rule['files'][0])]

        deps = chain(inputs, (cc_toolchain.target_name(i) for i in
                              chain(rule.deps, lib_deps)))

    cmd = cmd_var(writer, lang(rule['files']))
    writer.rule(
        target=cc_toolchain.target_name(rule),
        deps=deps,
        recipe=[cc_toolchain.link_command(
            cmd=use_var(cmd), mode=rule.kind, input=inputs,
            libs=rule['libs'], output='$@'
        )],
        variables=variables
    )

@rule_handler('executable')
def emit_executable(writer, rule):
    emit_link(writer, rule, '')

@rule_handler('library')
def emit_library(writer, rule):
    emit_link(writer, rule, 'LIB')

@rule_handler('target')
def emit_target(writer, rule):
    writer.rule(
        target=cc_toolchain.target_name(rule),
        deps=(cc_toolchain.target_name(i) for i in rule.deps),
        recipe=[],
        phony=True
    )

@rule_handler('command')
def emit_command(writer, rule):
    writer.rule(
        target=cc_toolchain.target_name(rule),
        deps=(cc_toolchain.target_name(i) for i in rule.deps),
        recipe=rule['cmd'],
        phony=True
    )