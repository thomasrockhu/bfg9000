from rule import Rule

# This is probably platform-specific, not toolchain-specific.
def target_name(rule, kind=None):
    if isinstance(rule, Rule):
        kind = rule.kind
        name = rule.name
    else:
        name = rule

    if kind == 'library':
        return 'lib{}.so'.format(name)
    elif kind == 'object_file':
        return '{}.o'.format(name)
    else:
        return name

def lib_link_name(rule):
    if isinstance(rule, Rule):
        return rule.name
    else:
        return rule

def link_libs(iterable):
    return ' '.join(('-l' + lib_link_name(i) for i in iterable))

def command_name(lang):
    return 'g++' if lang == 'c++' else 'gcc'

def compile_command(cmd, input, output, dep):
    return '{cmd} -MMD -MF {dep} -c {input} -o {output}'.format(
        cmd=cmd, input=input, output=output, dep=dep
    )

def link_command(cmd, mode, input, libs, output, prevars=None, postvars=None):
    result = cmd
    if mode == 'library':
        result += ' -shared'
    if prevars:
        result += ' ' + prevars
    result += ' ' + input
    if libs:
        result += ' ' + link_libs(libs)
    if postvars:
        result += ' ' + postvars
    result += ' -o ' + output
    return result
