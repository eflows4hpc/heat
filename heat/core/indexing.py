import torch
import numpy as np

from . import dndarray
from . import factories
from . import operations
from . import stride_tricks
from . import types

__all__ = [
    'nonzero',
    'where'
]


def nonzero(a):
    """
    Return the indices of the elements that are non-zero. (uses torch.nonzero)

    Returns a tuple of arrays, one for each dimension of a, containing the indices of the non-zero elements in that dimension.
    The values in a are always tested and returned in row-major, C-style order. The corresponding non-zero values can be obtained with: a[nonzero(a)].
    **NOTE** this array will be unbalanced by default

    Parameters
    ----------
    a: ht.DNDarray
    all_nodes: boolean
        if True: distrubted the results to all processes

    :param a:
    :return:
    """

    if a.split is None:
        # if there is no split then just return the values from torch
        return operations.__local_op(torch.nonzero, a, out=None)
    else:
        # a is split
        lcl_nonzero = torch.nonzero(a._DNDarray__array)
        _, _, slices = a.comm.chunk(a.shape, a.split)
        lcl_nonzero[..., a.split] += slices[a.split].start
        # print(lcl_nonzero)

        return factories.array(lcl_nonzero, is_split=0, dtype=types.int)


def where(op, x=None, y=None):
    """
    mirror of the numpy where function
    :param op:
    :param x:
    :param y:
    :return:
    """
    '''
    where works by taking just a nonzero call for only op
    if x AND y then you broadcast x in the case that its true and y in the case that its not
    '''
    print(op.shape)
    # if (x and y is None) or (y and x is None):
    #     raise ValueError("either both or neither x and y should be given")
    #
    # indices = nonzero(op)
    # if x is None and y is None:
    #     return indices



