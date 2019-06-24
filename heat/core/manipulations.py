import operator

import numpy as np
import torch
from mpi4py import MPI

from . import dndarray
from . import factories
from . import stride_tricks
from . import types

__all__ = [
    'expand_dims',
    'sort',
    'squeeze',
    'unique'
]


def expand_dims(a, axis):
    """
    Expand the shape of an array.

    Insert a new axis that will appear at the axis position in the expanded array shape.

    Parameters
    ----------
    a : ht.DNDarray
        Input array to be expanded.
    axis : int
        Position in the expanded axes where the new axis is placed.

    Returns
    -------
    res : ht.DNDarray
        Output array. The number of dimensions is one greater than that of the input array.

    Raises
    ------
    ValueError
        If the axis is not in range of the axes.

    Examples
    --------
    >>> x = ht.array([1,2])
    >>> x.shape
    (2,)

    >>> y = ht.expand_dims(x, axis=0)
    >>> y
    array([[1, 2]])
    >>> y.shape
    (1, 2)

    y = ht.expand_dims(x, axis=1)
    >>> y
    array([[1],
           [2]])
    >>> y.shape
    (2, 1)
    """
    # ensure type consistency
    if not isinstance(a, dndarray.DNDarray):
        raise TypeError('expected ht.DNDarray, but was {}'.format(type(a)))

    # sanitize axis, introduce arbitrary dummy dimension to model expansion
    axis = stride_tricks.sanitize_axis(a.shape + (1,), axis)

    return dndarray.DNDarray(
        a._DNDarray__array.unsqueeze(dim=axis), a.shape[:axis] + (1,) + a.shape[axis:],
        a.dtype,
        a.split if a.split is None or a.split < axis else a.split + 1,
        a.device,
        a.comm
    )


def sort(a, axis=None, descending=False, out=None):
    # default: using last axis
    if axis is None:
        axis = len(a.shape) - 1

    stride_tricks.sanitize_axis(a.shape, axis)

    if a.split is None or axis != a.split:
        # sorting is not affected by split -> we can just sort along the axis
        partial, index = torch.sort(a._DNDarray__array, dim=axis, descending=descending)

    else:
        # sorting is affected by split, processes need to communicate results
        # transpose so we can work along the 0 axis
        transposed = a._DNDarray__array.transpose(axis, 0)
        print("transposed", transposed)
        local_sorted, _ = torch.sort(transposed, dim=0, descending=descending)
        print("local_sorted", local_sorted)

        size = a.comm.Get_size()
        rank = a.comm.Get_rank()
        counts, _, _ = a.comm.counts_displs_shape(a.gshape, axis=axis)

        length = local_sorted.size()[0]
        print("length", length, 'counts', counts)

        # Separate the sorted tensor into size + 1 equal length partitions
        partitions = [x * length // (size + 1) for x in range(1, size + 1)]
        print("partitions", partitions)
        local_pivots = local_sorted[partitions] if counts[rank] else torch.empty(
            (0, ) + local_sorted.size()[1:], dtype=local_sorted.dtype)
        print("local_pivots", local_pivots)

        # Only processes with elements should share their pivots
        gather_counts = [int(x > 0) * size for x in counts]
        gather_displs = (0, ) + tuple(np.cumsum(gather_counts[:-1]))
        print('gather_counts', gather_counts, 'gather_displs', gather_displs)

        pivot_dim = list(transposed.size())
        pivot_dim[0] = size * sum([1 for x in counts if x > 0])
        print("pivot_dim", pivot_dim)

        # share the local pivots with root process
        pivot_buffer = torch.empty(pivot_dim, dtype=a.dtype.torch_type())
        a.comm.Gatherv(local_pivots, (pivot_buffer, gather_counts, gather_displs), root=0)
        print("Gathered pivot_buffer", pivot_buffer)

        pivot_dim[0] = size - 1
        global_pivots = torch.empty(pivot_dim, dtype=a.dtype.torch_type())

        # root process creates new pivots and shares them with other processes
        if rank is 0:
            sorted_pivots, _ = torch.sort(pivot_buffer, descending=descending, dim=0)
            print('sorted_pivots', sorted_pivots)
            length = sorted_pivots.size()[0]
            global_partitions = [x * length // size for x in range(1, size)]
            print("global_partitions", global_partitions)
            global_pivots = sorted_pivots[global_partitions]
        print("global_pivots", global_pivots)

        a.comm.Bcast(global_pivots, root=0)

        print("Bcas global_pivots", global_pivots)

        # Create matrix that holds information which process gets how many values at which position
        zeroes_dim = (size, ) + transposed.size()[1:]
        print('zeros_dim', zeroes_dim)
        partition_matrix = torch.zeros(zeroes_dim, dtype=torch.int64)

        # Create matrix that holds information, which value is shipped to which process
        index_matrix = torch.empty_like(local_sorted, dtype=torch.int64)

        # Iterate along the split axis which is now 0 due to transpose
        for i, x in enumerate(local_sorted):
            # print('x', x)
            # Enumerate over all elements with correct index
            for idx, val in np.ndenumerate(x.numpy()):
                # print('index', idx, 'val', val)
                op_func = operator.gt if descending else operator.lt
                # Calculate position where element must be sent to
                cur = next(i for i in range(len(global_pivots) + 1)
                           if (i == len(global_pivots) or op_func(val, global_pivots[i][idx])))

                # print('cur', cur)
                partition_matrix[cur][idx] += 1
                index_matrix[i][idx] = cur
        print('partition_matrix', partition_matrix)
        # Tested with 2-4 processes to this point

        print('index_matrix', index_matrix)

        # Share and sum the local partition_matrix
        a.comm.Allreduce(MPI.IN_PLACE, partition_matrix, op=MPI.SUM)
        print('reduced partition_matrix', partition_matrix)

        shape = (size, ) + transposed.size()[1:]
        send_recv_matrix = torch.zeros(shape, dtype=partition_matrix.dtype)
        # recv_matrix = torch.empty(shape, dtype=partition_matrix.dtype)

        for idx, val in np.ndenumerate(index_matrix.numpy()):
            pos = (val, ) + idx[1:]
            send_recv_matrix[pos] += 1

        print('rank', rank, 'send_matrix', send_recv_matrix)
        a.comm.Alltoall(MPI.IN_PLACE, send_recv_matrix)

        print('recv_matrix', send_recv_matrix)
        shape = (partition_matrix[rank].max(), ) + transposed.size()[1:]
        print('shape', shape)

        # create matrix whose elements are ranks of processes where the value will come from
        recv_indices = torch.empty(shape, dtype=local_sorted.dtype)
        fill_level = torch.zeros(shape[1:], dtype=torch.int32)

        for i, x in enumerate(send_recv_matrix):
            for idx, val in np.ndenumerate(x.numpy()):
                for k in range(val):
                    recv_indices[fill_level[idx]][idx] = i
                    fill_level[idx] += 1

        print('recv_indices', recv_indices)

        for idx, val in np.ndenumerate(index_matrix.numpy()):
            send_buf = torch.tensor(local_sorted[idx])
            # Add tag to identify correct value we want to receive later
            tag = int(''.join([str(el) for el in idx[1:]]))
            a.comm.Send(send_buf, dest=val, tag=tag)

        recv_amount = sum(send_recv_matrix)
        print('recv_amount', recv_amount)
        fill_level = torch.zeros(shape[1:], dtype=torch.int32)
        local_result = torch.empty(shape, dtype=local_sorted.dtype)

        for idx, val in np.ndenumerate(recv_amount.numpy()):
            for i in range(val):
                source = recv_indices[fill_level[idx]][idx]
                tag = int(''.join([str(el) for el in idx]))
                recv_buf = torch.empty(1, dtype=local_sorted.dtype)
                a.comm.Recv(recv_buf, source=source, tag=tag)
                local_result[fill_level[idx]][idx] = recv_buf
                fill_level[idx] += 1

        print('local_result', local_result)

        # Create a matrix which holds information about the 'unbalancedness' of the local result
        problem_idx = torch.zeros((size, ) + local_result.shape[1:], dtype=partition_matrix.dtype)
        for i, x in enumerate(partition_matrix):
            for idx, val in np.ndenumerate(x.numpy()):
                problem_idx[i][idx] = x[idx] - counts[i]
        print('problem_index', problem_idx)

        # create final result tensor by iteratively redistributing with the neighbour processes
        partial = torch.empty(transposed.size(), dtype=a.dtype.torch_type())
        copy_size = min(a.lshape[axis], partition_matrix[rank].max())
        partial[0: copy_size] = local_result[0: copy_size]
        print('partial', partial)
        for i in range(size):
            # start with lowest rank and populate to the highest
            for idx, val in np.ndenumerate(problem_idx[i].numpy()):
                while val != 0:
                    # print('current', i, 'val', val, 'idx', idx)
                    if val < 0:
                        receiver = i
                        sender = next(ind + i + 1 for ind, pr in enumerate(partition_matrix[i + 1:]) if pr[idx] > 0)
                        # print('Sender', sender, local_result.shape)
                        receiver_idx = (val, ) + idx

                        if rank == sender:
                            end = partition_matrix[sender][idx]
                            enumerate_index = [slice(None)] + [slice(ind, ind + 1) for ind in idx]
                            # print('end', end, local_result[0: end])
                            values = local_result[0: end][enumerate_index]
                            sender_idx = (values.argmax() if descending else values.argmin(), ) + idx

                            # print('Sender', sender, 'Sender_idx', sender_idx, 'receiver', receiver, 'receiver_idx', receiver_idx)
                            send_buf = torch.tensor(local_result[sender_idx])
                            # print('send_buf', send_buf)
                            a.comm.Send(send_buf, dest=receiver)
                            # Swap last element along axis at the now freed location
                            last_idx = (a.lshape[axis] + problem_idx[sender][idx] - 1, ) + sender_idx[1:]
                            # print('last_index', last_idx, local_result[last_idx])
                            local_result[sender_idx] = local_result[last_idx]
                            # print('local_result', local_result)
                            if sender_idx[0] < partial.shape[0]:
                                partial[sender_idx] = local_result[last_idx]
                        if rank == receiver:
                            recv_buf = torch.empty(1, dtype=local_result.dtype)
                            a.comm.Recv(recv_buf, source=sender)
                            # print('Received', recv_buf)
                            partial[receiver_idx] = recv_buf
                            # print('partial', partial)

                        val += 1
                        problem_idx[receiver][idx] += 1
                        partition_matrix[receiver][idx] += 1
                        problem_idx[sender][idx] -= 1
                        partition_matrix[receiver][idx] -= 1

                        # print('problem_idx', problem_idx)

                    if val > 0:
                        sender = i
                        receiver = next(ind + i + 1 for ind, pr in enumerate(partition_matrix[i + 1:]) if pr[idx] > 0)
                        # print('sender', sender, 'receiver', receiver)
                        if rank == sender:
                            end = partition_matrix[sender][idx]
                            enumerate_index = [slice(None)] + [slice(ind, ind + 1) for ind in idx]
                            values = local_result[0: end][enumerate_index]
                            sender_idx = (values.argmin() if descending else values.argmax(), ) + idx

                            send_buf = torch.tensor(local_result[sender_idx])
                            a.comm.Send(send_buf, dest=receiver)
                        if rank == receiver:
                            recv_buf = torch.empty(1, dtype=local_result.dtype)
                            a.comm.Recv(recv_buf, source=sender)
                            # print('recv_buf', recv_buf)
                            recv_index = (partition_matrix[receiver][idx], ) + idx
                            # print('receive_index', recv_index)
                            if recv_index[axis] < partial.shape[axis]:
                                partial[recv_index] = recv_buf
                            if recv_index[axis] < local_result.shape[axis]:
                                local_result[recv_index] = recv_buf
                            else:
                                new_shape = list(local_result.shape)
                                new_shape[0] = new_shape[0] + val
                                tmp = torch.empty(new_shape, dtype=local_result.dtype)
                                tmp[0: local_result.shape[axis]] = local_result
                                local_result = tmp
                                local_result[recv_index] = recv_buf
                            # print('partial', partial, 'local_result', local_result)
                        val -= 1
                        problem_idx[receiver][idx] += 1
                        partition_matrix[receiver][idx] += 1
                        problem_idx[sender][idx] -= 1
                        partition_matrix[receiver][idx] -= 1
        partial, _ = partial.sort(dim=0, descending=descending)
        partial = partial.transpose(0, axis)

    if out is not None:
        out._DNDarray__array = partial
    else:
        return dndarray.DNDarray(
            partial,
            a.gshape,
            a.dtype,
            a.split,
            a.device,
            a.comm
        )


def squeeze(x, axis=None):
    """
    Remove single-dimensional entries from the shape of a tensor.

    Parameters:
    -----------
    x : ht.DNDarray
        Input data.

    axis : None or int or tuple of ints, optional
           Selects a subset of the single-dimensional entries in the shape.
           If axis is None, all single-dimensional entries will be removed from the shape.
           If an axis is selected with shape entry greater than one, a ValueError is raised.


    Returns:
    --------
    squeezed : ht.DNDarray
               The input tensor, but with all or a subset of the dimensions of length 1 removed.


    Examples:
    >>> import heat as ht
    >>> import torch
    >>> torch.manual_seed(1)
    <torch._C.Generator object at 0x115704ad0>
    >>> a = ht.random.randn(1,3,1,5)
    >>> a
    tensor([[[[ 0.2673, -0.4212, -0.5107, -1.5727, -0.1232]],

            [[ 3.5870, -1.8313,  1.5987, -1.2770,  0.3255]],

            [[-0.4791,  1.3790,  2.5286,  0.4107, -0.9880]]]])
    >>> a.shape
    (1, 3, 1, 5)
    >>> ht.squeeze(a).shape
    (3, 5)
    >>> ht.squeeze(a)
    tensor([[ 0.2673, -0.4212, -0.5107, -1.5727, -0.1232],
            [ 3.5870, -1.8313,  1.5987, -1.2770,  0.3255],
            [-0.4791,  1.3790,  2.5286,  0.4107, -0.9880]])
    >>> ht.squeeze(a,axis=0).shape
    (3, 1, 5)
    >>> ht.squeeze(a,axis=-2).shape
    (1, 3, 5)
    >>> ht.squeeze(a,axis=1).shape
    Traceback (most recent call last):
    ...
    ValueError: Dimension along axis 1 is not 1 for shape (1, 3, 1, 5)
    """

    # Sanitize input
    if not isinstance(x, dndarray.DNDarray):
        raise TypeError('expected x to be a ht.DNDarray, but was {}'.format(type(x)))
    # Sanitize axis
    axis = stride_tricks.sanitize_axis(x.shape, axis)
    if axis is not None:
        if isinstance(axis, int):
            dim_is_one = (x.shape[axis] == 1)
        if isinstance(axis, tuple):
            dim_is_one = bool(factories.array(list(x.shape[dim] == 1 for dim in axis)).all()._DNDarray__array)
        if not dim_is_one:
            raise ValueError('Dimension along axis {} is not 1 for shape {}'.format(axis, x.shape))

    # Local squeeze
    if axis is None:
        axis = tuple(i for i, dim in enumerate(x.shape) if dim == 1)
    if isinstance(axis, int):
        axis = (axis,)
    out_lshape = tuple(x.lshape[dim] for dim in range(len(x.lshape)) if not dim in axis)
    x_lsqueezed = x._DNDarray__array.reshape(out_lshape)

    # Calculate split axis according to squeezed shape
    if x.split is not None:
        split = x.split - len(list(dim for dim in axis if dim < x.split))
    else:
        split = x.split

    # Distributed squeeze
    if x.split is not None:
        if x.comm.is_distributed():
            if x.split in axis:
                raise ValueError('Cannot split AND squeeze along same axis. Split is {}, axis is {} for shape {}'.format(
                    x.split, axis, x.shape))
            out_gshape = tuple(x.gshape[dim] for dim in range(len(x.gshape)) if not dim in axis)
            x_gsqueezed = factories.empty(out_gshape, dtype=x.dtype)
            loffset = factories.zeros(1, dtype=types.int64)
            loffset.__setitem__(0, x.comm.chunk(x.gshape, x.split)[0])
            displs = factories.zeros(x.comm.size, dtype=types.int64)
            x.comm.Allgather(loffset, displs)

            # TODO: address uneven distribution of dimensions (Allgatherv). Issue #273, #233
            x.comm.Allgather(x_lsqueezed, x_gsqueezed)  # works with evenly distributed dimensions only
            return dndarray.DNDarray(
                x_gsqueezed,
                out_gshape,
                x_lsqueezed.dtype,
                split=split,
                device=x.device,
                comm=x.comm)

    return dndarray.DNDarray(
        x_lsqueezed,
        out_lshape,
        x.dtype,
        split=split,
        device=x.device,
        comm=x.comm)


def unique(a, sorted=False, return_inverse=False, axis=None):
    """
    Finds and returns the unique elements of an array.

    Works most effective if axis != a.split.

    Parameters
    ----------
    a : ht.DNDarray
        Input array where unique elements should be found.
    sorted : bool
        Whether the found elements should be sorted before returning as output.
    return_inverse:
        Whether to also return the indices for where elements in the original input ended up in the returned
        unique list.
    axis : int
        Axis along which unique elements should be found. Default to None, which will return a one dimensional list of
        unique values.

    Returns
    -------
    res : ht.DNDarray
        Output array. The unique elements. Elements are distributed the same way as the input tensor.
    inverse_indices : torch.tensor (optional)
        If return_inverse is True, this tensor will hold the list of inverse indices

    Examples
    --------
    >>> x = ht.array([[3, 2], [1, 3]])
    >>> ht.unique(x, sorted=True)
    array([1, 2, 3])

    >>> ht.unique(x, sorted=True, axis=0)
    array([[1, 3],
           [2, 3]])

    >>> ht.unique(x, sorted=True, axis=1)
    array([[2, 3],
           [3, 1]])
    """
    if a.split is None:
        # Trivial case, result can just be forwarded
        return torch.unique(a._DNDarray__array, sorted=sorted, return_inverse=return_inverse, dim=axis)

    local_data = a._DNDarray__array
    unique_axis = None
    inverse_indices = None

    if axis is not None:
        # transpose so we can work along the 0 axis
        local_data = local_data.transpose(0, axis)
        unique_axis = 0

    # Calculate the unique on the local values
    if a.lshape[a.split] == 0:
        # Passing an empty vector to torch throws exception
        if axis is None:
            res_shape = [0]
            inv_shape = list(a.gshape)
            inv_shape[a.split] = 0
        else:
            res_shape = list(local_data.shape)
            res_shape[0] = 0
            inv_shape = [0]
        lres = torch.empty(res_shape, dtype=a.dtype.torch_type())
        inverse_pos = torch.empty(inv_shape, dtype=torch.int64)

    else:
        lres, inverse_pos = torch.unique(local_data, sorted=sorted, return_inverse=True, dim=unique_axis)

    # Share and gather the results with the other processes
    uniques = torch.tensor([lres.shape[0]]).to(torch.int32)
    uniques_buf = torch.empty((a.comm.Get_size(), ), dtype=torch.int32)
    a.comm.Allgather(uniques, uniques_buf)

    split = None
    is_split = None

    if axis is None or axis == a.split:
        # Local results can now just be added together
        if axis is None:
            # One dimensional vectors can't be distributed -> no split
            output_dim = [uniques_buf.sum().item()]
            recv_axis = 0
        else:
            output_dim = list(lres.shape)
            output_dim[0] = uniques_buf.sum().item()
            recv_axis = a.split

            # Result will be split along the same axis as a
            split = a.split

        # Gather all unique vectors
        counts = list(uniques_buf.tolist())
        displs = list([0] + uniques_buf.cumsum(0).tolist()[:-1])
        gres_buf = torch.empty(output_dim, dtype=a.dtype.torch_type())
        a.comm.Allgatherv(lres, (gres_buf, counts, displs,), axis=recv_axis, recv_axis=0)

        if return_inverse:
            # Prepare some information to generated the inverse indices list
            avg_len = a.gshape[a.split] // a.comm.Get_size()
            rem = a.gshape[a.split] % a.comm.Get_size()

            # Share the local reverse indices with other processes
            counts = [avg_len] * a.comm.Get_size()
            add_vec = [1] * rem + [0] * (a.comm.Get_size() - rem)
            inverse_counts = [sum(x) for x in zip(counts, add_vec)]
            inverse_displs = [0] + list(np.cumsum(inverse_counts[:-1]))
            inverse_dim = list(inverse_pos.shape)
            inverse_dim[a.split] = a.gshape[a.split]
            inverse_buf = torch.empty(inverse_dim, dtype=inverse_pos.dtype)

            # Transpose data and buffer so we can use Allgatherv along axis=0 (axis=1 does not work properly yet)
            inverse_pos = inverse_pos.transpose(0, a.split)
            inverse_buf = inverse_buf.transpose(0, a.split)
            a.comm.Allgatherv(inverse_pos, (inverse_buf, inverse_counts, inverse_displs), axis=0)
            inverse_buf = inverse_buf.transpose(0, a.split)

        # Run unique a second time
        gres = torch.unique(gres_buf, sorted=sorted, return_inverse=return_inverse, dim=unique_axis)
        if return_inverse:
            # Use the previously gathered information to generate global inverse_indices
            g_inverse = gres[1]
            gres = gres[0]
            if axis is None:
                # Calculate how many elements we have in each layer along the split axis
                elements_per_layer = 1
                for num, val in enumerate(a.gshape):
                    if not num == a.split:
                        elements_per_layer *= val

                # Create the displacements for the flattened inverse indices array
                local_elements = [displ * elements_per_layer for displ in inverse_displs][1:] + [float('inf')]

                # Flatten the inverse indices array every element can be updated to represent a global index
                transposed = inverse_buf.transpose(0, a.split)
                transposed_shape = transposed.shape
                flatten_inverse = transposed.flatten()

                # Update the index elements iteratively
                cur_displ = 0
                inverse_indices = [0] * len(flatten_inverse)
                for num in range(len(inverse_indices)):
                    if num >= local_elements[cur_displ]:
                        cur_displ += 1
                    index = flatten_inverse[num] + displs[cur_displ]
                    inverse_indices[num] = g_inverse[index].tolist()

                # Convert the flattened array back to the correct global shape of a
                inverse_indices = torch.tensor(inverse_indices).reshape(transposed_shape)
                inverse_indices = inverse_indices.transpose(0, a.split)

            else:
                inverse_indices = torch.zeros_like(inverse_buf)
                steps = displs + [None]

                # Algorithm that creates the correct list for the reverse_indices
                for i in range(len(steps) - 1):
                    begin = steps[i]
                    end = steps[i + 1]
                    for num, x in enumerate(inverse_buf[begin: end]):
                        inverse_indices[begin + num] = g_inverse[begin + x]

    else:
        max_uniques, max_pos = uniques_buf.max(0)

        # find indices of vectors
        if a.comm.Get_rank() == max_pos.item():
            # Get the indices of the vectors we need from each process
            indices = []
            found = []
            pos_list = inverse_pos.tolist()
            for p in pos_list:
                if p not in found:
                    found += [p]
                    indices += [pos_list.index(p)]
                if len(indices) is max_uniques.item():
                    break
            indices = torch.tensor(indices, dtype=a.dtype.torch_type())
        else:
            indices = torch.empty((max_uniques.item(),), dtype=a.dtype.torch_type())

        a.comm.Bcast(indices, root=max_pos)
        gres = local_data[indices.tolist()]

        is_split = a.split
        inverse_indices = indices

    if axis is not None:
        # transpose matrix back
        gres = gres.transpose(0, axis)
    result = factories.array(gres, dtype=a.dtype, device=a.device, comm=a.comm, is_split=is_split)

    if split is not None:
        result.resplit(a.split)

    return_value = result
    if return_inverse:
        return_value = [return_value, inverse_indices]

    return return_value
