from . import tool
from .utils import SimpleCommand

from ..iterutils import iterate


@tool('doppel')
class Doppel(SimpleCommand):
    rule_name = command_var = 'doppel'

    def __init__(self, env):
        SimpleCommand.__init__(self, env, 'DOPPEL', 'doppel')

    @property
    def data_args(self):
        return ['-m', '644']

    def _call(self, cmd, mode, src, dst, directory=None, format=None,
              dest_prefix=None):
        if mode == 'onto':
            return [cmd, '-p', src, dst]

        elif mode == 'into':
            result = [cmd, '-ipN']
            if directory:
                result.extend(['-C', directory])
            result.extend(iterate(src))
            result.append(dst)
            return result

        elif mode == 'archive':
            result = [cmd, '-ipN', '-f', format]
            if directory:
                result.extend(['-C', directory])
            if dest_prefix:
                result.extend(['-P', dest_prefix])
            result.extend(iterate(src))
            result.append(dst)
            return result

        else:
            raise ValueError("unknown mode '{}'".format(mode))
