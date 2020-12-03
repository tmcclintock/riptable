__all__ = [
    # misc riptable utility funcs
    'get_default_value', 'merge_prebinned', 'alignmk', 'normalize_keys',
    'bytes_to_str', 'findTrueWidth', 'ischararray', 'islogical', 'mbget', 'str_to_bytes', 'to_str', 'describe',
    'crc_match',
    # h5 -> riptable
    'load_h5'
]

from collections.abc import Iterable
import keyword
from math import modf
import os
from typing import TYPE_CHECKING, Callable, Optional, List, Sequence, Union
import warnings

import numpy as np
import riptide_cpp as rc

from .rt_enum import TypeRegister, INVALID_DICT, NumpyCharTypes
from .rt_numpy import arange, bool_to_fancy, crc32c, get_common_dtype, tile, empty

# Type-checking-only imports.
if TYPE_CHECKING:
    import re
    from .rt_dataset import Dataset
    from .rt_struct import Struct


#-----------------------------------------------------------------------------------------
def load_h5(
    filepath: Union[str, os.PathLike], name: str = '/',
    columns: Union[Sequence[str], 're.Pattern', Callable[..., Sequence[str]]] = '',
    format=None, fixblocks: bool = False, drop_short: bool = False,
    verbose=0, **kwargs
) -> Union['Dataset', 'Struct']:
    """
    Load from h5 file and flip hdf5.io objects to riptable structures.

    In some h5 files, the arrays are saved as rows in "blocks". If `fixblocks` is ``True``,
    this routine will transpose the rows in the blocks.

    Parameters
    ----------
    filepath : str or os.PathLike
        The path to the HDF5 file to load.
    name : str
        Set to table name, defaults to '/'.
    columns : sequence of str or re.Pattern or callable, defaults to ''
        Return the given subset of columns, or those matching regex.
        If a function is passed, it will be called with column names, dtypes and shapes,
        and should return a subset of column names.
        Passing an empty string (the default) loads all columns.
    format : hdf5.Format
        TODO, defaults to hdf5.Format.NDARRAY
    fixblocks : bool
        True will transpose the rows when the H5 file are as ???, defaults to False.
    drop_short : bool
        Set to True to drop short rows and never return a Struct, defaults to False.
    verbose
        TODO

    Returns
    -------
    Dataset or Struct
        A `Dataset` or `Struct` with all workspace contents.

    Notes
    -----
    block<#>_items is a list of column names (bytes)
    block<#>_values is a numpy array of numpy array (rows)
    columns (for riptable) can be generated by zipping names from the list with transposed columns

    axis0 appears to be all column names - not sure what to do with this
    also what is axis1? should it get added like the other columns?
    """
    import hdf5
    if format is None:
        format = hdf5.Format.NDARRAY

    if verbose > 0: print(f'starting h5 load {filepath}')
    # TEMP: Until hdf5.load() implements support for path-like objects, force conversion to str.
    filepath = os.fspath(filepath)
    ws = hdf5.load(filepath, name=name, columns=columns, format=format, **kwargs)
    if verbose > 0: print(f'finished h5 load {filepath}')

    if isinstance(ws, dict):
        if verbose > 0: print(f'h5 file loaded into dictionary. Possibly returning Dataset from dictionary, otherwise Struct.')
        return _possibly_create_dataset(ws)

    ws = h5io_to_struct(ws)

    if fixblocks:
        ws = ws[0]
        final_dict = {}
        for k, v in ws.items():
            if k.endswith('_items'):
                names = v.astype('U')
                rows = ws[k[:-5]+'values']
                t_dict = dict(zip(names, rows.transpose()))
                for t_k, t_v in t_dict.items():
                    final_dict[t_k] = t_v
        ws = TypeRegister.Struct(final_dict)

    if drop_short:
        # try to make a dataset
        rownum_set = {len(ws[c]) for c in ws}
        maxrow = max(rownum_set)
        print("drop short was set! max was ", maxrow)
        final_dict = {}

        # build a new dictionary with only columns of the max length
        for k, v in ws.items():
            if len(v) == maxrow:
                final_dict[k] = v
            else:
                warnings.warn(f"load_h5: drop_short, dropping col {k!r} with len {len(v)} vs {maxrow}")

        ws = TypeRegister.Dataset(final_dict)

    return ws


#-----------------------------------------------------------------------------------------
def _possibly_create_dataset(itemdict):
    """
    Useful for iterating through dicts/other structures with items().

    Try to create a Dataset if all items are numpy arrays of the same length, otherwise throw everything into a Struct.
    Also used by load_h5, as h5 files may contain single datasets.

    Parameters
    ----------
    itemdict : dict
        TODO describe this parameter

    Returns
    -------
    Dataset or Struct
    """
    if isinstance(itemdict, np.ndarray):
        return itemdict

    try:
        result = TypeRegister.Dataset(itemdict)
    except:
        result = TypeRegister.Struct(itemdict)
    return result

#-----------------------------------------------------------------------------------------
def _possibly_escape_colname(parent_name, container, name):
    """
    If loading from h5, column names may need to change to valid riptable column names.
    Will warn the user with column name + container name if column name was changed.
    """

    # escape leading underscores
    old = name
    while name.startswith('_'):
        name = name[1:]
    if name in keyword.kwlist:
        name = name + '_'
        while(name in container):
            name = name + '_'
        warnings.warn(f"changed name {old} to {name} in {parent_name}")
    # capitalize names of existing attributes in dataset
    elif name in dir(TypeRegister.Dataset):
        old = name
        name = name.capitalize()
        warnings.warn(f"changed name {old} to {name} in {parent_name}")

    return name

#-----------------------------------------------------------------------------------------
def _possibly_convert_rec_array(item):
    """
    h5 often loads data into a numpy record array (void type). Flip these before converting to a dataset.
    """
    if item.dtype.char == 'V':
        warnings.warn(f"Converting numpy record array. Performance may suffer.")
        # flip row-major to column-major
        d={}
        if True:
            offsets=[]
            arrays=np.empty(len(item.dtype.fields), dtype='O')
            arrlen = len(item)
            count =0
            for name, v in item.dtype.fields.items():
                offsets.append(v[1])
                arr= empty(arrlen, dtype=v[0])
                arrays[count] = arr
                count += 1
                # build dict of names and new arrays
                d[name] = arr

            # Call new routine to convert
            rc.RecordArrayToColMajor(item, np.asarray(offsets, dtype=np.int64), arrays);

        else:
            # old way
            for name in item.dtype.names:
                d[name] = item[:][name].copy()
        return d
    return item

#-----------------------------------------------------------------------------------------
def h5io_to_struct(io):
    """
    Utility for crawling/flipping hdf5.io objects to Dataset/Struct.
    Will convert row-major numpy structured arrays to column-major.

    So far I've encountered hdf5.io. and hdf5.io.labels. It's a massive module.
    If anyone has use cases please send them my way, thanks - Sam Kachel
    """
    if isinstance(io, np.ndarray):
        if io.dtype.char == 'V':
            io = _possibly_convert_rec_array(io)
        else:
            print('Loaded a single numpy array from h5. Returning struct of single array.')
        return _possibly_create_dataset(io)
    if io.__module__ != 'hdf5.io':
        raise TypeError(f"This routine attempts to interpret H5 data from classes in the hdf5.io module. Got {io.__module__} module instead.")
    itemdict = {}
    for itemname in dir(io):
        if not itemname.startswith('_'):
            item = getattr(io, itemname)
            # need to check for record array
            if isinstance(item, np.ndarray):
                item = _possibly_convert_rec_array(item)
                item = _possibly_create_dataset(item)
                itemdict[itemname] = item

            # crawl python dictionary
            elif isinstance(item, dict):
                itemdict[itemname] = {}
                # this should use recursion too - how far do hdf5.io objects go?
                # only save arrays for now
                for d_name, d_item in item.items():
                    if isinstance(d_item, np.ndarray):
                        # some h5 has columns that start with _ or same as Python keywords
                        finalname = _possibly_escape_colname(itemname, item, d_name)
                        itemdict[itemname][finalname] = d_item

                itemdict[itemname] = _possibly_create_dataset(itemdict[itemname])

            # crawl the h5io object
            elif item.__module__ == 'hdf5.io':
                if item.__class__.__name__ == 'Categorical':
                    print(f"FOUND A CATEGORICAL: {itemname}\nPlease let Sam Kachel know where this file is.")
                itemdict[itemname] = h5io_to_struct(item)

            else:
                itemdict[itemname] = item

    result = _possibly_create_dataset(itemdict)

    return result


def findTrueWidth(string):
    """
    Find the length of a byte string without trailing zeros. Useful for optimizing string matching functions.

    Parameters
    ----------
    string : a byte string as an array of int8
        A byte string as an array of int8

    Returns
    -------
    int
        Number of bytes in string.

    Examples
    --------
    >>> a = np.chararray(1, itemsize=5)
    >>> a[0] = b'abc'
    >>> findTrueWidth(np.frombuffer(a,dtype=np.int8))
    3
    """
    # Find the length of a byte string (without the trailing zeros)
    # the input must be dtype=np.int8, use np.frombuffer()
    width = string.shape[0]
    trailing = 0
    for i in reversed(range(width)):
        if string[i]:
            break
        else:
            trailing+=1
    return width-trailing

def merge_prebinned(key1: np.ndarray, key2: np.ndarray, val1, val2, totalUniqueSize):
    """
    merge_prebinned
    TODO: Improve docs when working properly

    Parameters
    ----------
    key1: a numpy array already binned (like a categorical)
    key2: a numpy array already binned
    val1: int32/64  or float32/64
    val2: int32/64  or float32/64

    Notes
    -----
    `key1` and `key2` must be same dtype
    `val1` and `val2` must be same dtype
    """
    if not isinstance(val1, np.ndarray):
        raise TypeError("val1 must be ndarray")

    if not isinstance(val2, np.ndarray):
        raise TypeError("val2 must be ndarray")

    return rc.MergeBinnedAndSorted(key1, key2, val1, val2, totalUniqueSize)

#------------------------------------------------------------------------------------------------------
def normalize_keys(key1, key2, verbose =False):
    """
    Helper function to make two different lists of keys the same itemsize. Handles categoricals.

    Parameters
    ----------
    key1 : a numpy array or a list/tuple of numpy arrays
    key2 : a numpy array or a list/tuple of numpy arrays

    Returns
    -------
    Two lists of arrays that are aligned (same itemsize)
        If the keys were passed in as single arrays they will be returned as a list of 1 array
        Integers, Float, String may be upcast if necessary.
        Categoricals may be aligned if necessary.

    Examples
    --------
    >>> c1 = rt.Cat(['A','B','C'])
    >>> c2 = rt.Cat(rt.arange(3) + 1, ['A','B','C'])
    >>> [d1], [d2] = rt.normalize_keys(c1, c2)

    Notes
    -----
    TODO: integer, float and string upcasting can be done while rotating.
    """
    def check_key(key):
        if not isinstance(key, (TypeRegister.Struct, dict)):
            if not isinstance(key, (tuple, list)):
                if isinstance(key, np.ndarray):
                    # if just pass in a single array, put in a list
                    key = [key]
                else:
                    # Try to convert to a numpy array
                    key = [np.asanyarray(key)]
            else:
                # possible multi key path
                # check first value to see if scalar - if it is assume user passed in a list of scalars
                if np.isscalar(key[0]):
                    key = [np.asanyarray(key)]
            return key
        else:
            # extract the value in the dictlike object
            return list(key.values())

    def possibly_convert(arr, common_dtype):
        # upcast if need to
        if arr.dtype.num != common_dtype.num:
            try:
                # perform a safe conversion understanding sentinels
                arr = TypeRegister.MathLedger._AS_FA_TYPE(arr, common_dtype.num)
            except Exception:
                # try numpy conversion
                arr = arr.astype(common_dtype)

        elif arr.itemsize != common_dtype.itemsize:
            # make strings sizes the same
            arr = arr.astype(common_dtype)
        return arr

    key1 = check_key(key1)
    key2 = check_key(key2)

    if verbose: print("check_key keys", key1, key2)

    arrays1 = []
    arrays2 = []

    # convert to common dtype
    for arr1, arr2 in zip(key1, key2):

        # if either one is Categorical or both are, make sure they are aligned
        if isinstance(arr1,TypeRegister.Categorical) or isinstance(arr2,TypeRegister.Categorical):
            arr1, arr2 = TypeRegister.Categorical.align([arr1, arr2])
            # even if categoricals were aligned we might have int16 vs int32 (so fall thru to check)

        # possibly convert common numpy dtypes
        common_dtype = get_common_dtype(arr1, arr2)

        # possibly upcast while appending to new list
        arrays1.append(possibly_convert(arr1, common_dtype))
        arrays2.append(possibly_convert(arr2, common_dtype))

    return arrays1, arrays2


#------------------------------------------------------------------------------------------------------
def alignmk(key1, key2, time1, time2, direction:str='backward', allow_exact_matches:bool=True, verbose:bool=False):
    """
    Core routine for merge_asof.
    Takes a key1 on the left and a key2 on the right (multikey is allowed).
    When going forward, it will check if time1 <= time2
        if so
            it will hash on key1 and return the last row number for key2 or INVALID
            it will increment the index into time1
        else
            it will return the last row number from key2
            it will increment the index into time2

    When going backward, it will start on the last time, it will check if time1 >= time2
        if so
            it will hash on key1 and return the last row number for key2 or INVALID
            it will decrement the index into time1
        else
            it will return the last row number from key2
            it will decrement the index into time2

    Parameters
    ----------
    key1: a numpy array or a list/tuple of numpy arrays
    key2: a numpy array or a list/tuple of numpy arrays
    time1: a monotonic integer array often indicating time, must be same length as key1
    time2: a monotonic integer array often indicating time, must be same length as key2
    direction : {'backward', 'forward', 'nearest'}
        The alignment direction.
    allow_exact_matches : bool
    verbose : bool
        When True, enables more-verbose logging output. Defaults to False.

    Returns
    -------
    Fancy index the same length as key1/time1 (may have invalids)
    use the return index to pull from right hand side, for example key2[return]
    to populate a dataset with length key1

    Examples
    --------
    >>> time1=rt.FA([0, 1, 4, 6, 8, 9, 11, 16, 19, 20, 22, 27])
    >>> time2=rt.FA([4, 5, 7, 8, 10, 12, 15, 16, 24])
    >>> alignmk(rt.ones(time1.shape), rt.ones(time2.shape), time1, time2, direction='backward')
    FastArray([-2147483648, -2147483648, 0, 1, 3, 3, 4, 7, 7, 7, 7, 8])
    >>> alignmk(rt.ones(time1.shape), rt.ones(time2.shape), time1, time2, direction='forward')
    FastArray([0, 0, 0, 2, 3, 4, 5, 7, 8, 8, 8, -2147483648])
    """
    key1, key2 = normalize_keys(key1, key2, verbose=verbose)

    if not isinstance(time1, np.ndarray):
        raise TypeError(f"time1 must be a numpy array not {time1}")

    if not isinstance(time2, np.ndarray):
        raise TypeError(f"time2 must be a numpy array not {time2}")

    if verbose: print("alignmk keys", key1, key2, time1, time2, direction, allow_exact_matches)

    if direction == 'nearest':
        # This logic isn't fully implemented and working yet; don't allow it to be used until it is.
        raise NotImplementedError("The 'nearest' direction is not yet supported by alignmk.")

        #backward= rc.MultiKeyAlign32((key1,), (key2,), time1, time2, False, allow_exact_matches)
        #forward= rc.MultiKeyAlign32((key1,), (key2,), time1, time2, True, allow_exact_matches)
        #if verbose: print("forward", forward, 'backward', backward)
        # TODO combine forward and backward
        #return forward
    else:
        if direction == 'backward':
            isForward = False
        elif direction == 'forward':
            isForward = True
        else:
            raise ValueError("unsupported direction in alignmk")

        #TODO: Update C++ code to take a list
        result= rc.MultiKeyAlign32((key1,), (key2,), time1, time2, isForward, allow_exact_matches)
    if verbose: print("result", result)
    return result

#------------------------------------------------------------------------------------------------------
def _mbget_2dims(arr, idx):
    """
    2-dimensional arrays are flattened, and index is repeated/expanded before going through
    the normal mbget routine.
    """
    orig_dtype = arr.dtype

    nrows = len(idx)
    ncols = arr.shape[1]
    final_shape = (nrows, ncols)

    # expand index array
    # possible optimization: multiply on the smaller one first?
    expanded_idx = np.repeat(idx, ncols) * ncols
    expanded_idx += tile(arange(ncols), nrows)

    # in as fortran
    restore_fortran = np.isfortran(arr)

    # flips to C-contiguous
    arr = arr.ravel()

    # send 1-dim raveled through
    result = mbget(arr, expanded_idx)

    result = result.reshape(final_shape)
    # performance warning: array gets copied during ravel, and copied back here if in fortran layout
    if restore_fortran:
        result = np.asfortranarray(result)

    return result.view(TypeRegister.FastArray)

#------------------------------------------------------------------------------------------------------
def mbget(aValues: np.ndarray, aIndex: np.ndarray, d: Optional[Union[int, float, bytes]]=None) -> np.ndarray:
    """
    Provides fancy-indexing functionality similar to `np.take`, but where out-of-bounds indices 'retrieve' a
    default value instead of e.g. raising an exception.

    It returns an array the same size as the `aIndex` array, with `aValues` in place of the indices and
    delimiter values (use `d` to customize) for invalid indices.

    Parameters
    ----------
    aValues : np.ndarray
        A single dimension of array values (strings only accepted as chararray).
    aIndex : np.ndarray
        A single dimension array of int64 indices.
    d
        An optional argument for a custom default for string operations to use when the index
        is out of range. (currently always uses the default)
        d is character byte ``b''`` when `aValues` is a chararray
        ``np.nan`` when aValues are floats,
        ``INVALID_POINTER_32`` or ``INVALID_POINTER_64`` when aValues are ints.

    Returns
    -------
    vout : np.ndarray
        An array of values in `aValues` that have been looked up according to the indices in `aIndex`.
        The array will have the same shape as `aIndex`, and the same dtype and class as `aValues`.

    Raises
    ------
    KeyError
        When the dtype for `aValues` is not int32,int64,float32,float64 and `aValues` is not a chararray.

    Notes
    -----
    Tests Performed:
        Large aValues size (28 million)
        Large aValues typesize (50 for chararray)
        Large aIndex size (28 million)
        All indices valid for aIndex in aValues.
        No indices valid for aIndex in aValues.
        Empty input arrays.
        Invalid types for aValues array.
        Invalid types for aIndex array (not int64 or int32)

    The return array vout is the same size as the p array. Suppose we have a position i. If the index stored at
    position i of p is a valid index for array v, vout at position i will contain the value of v at that index.
    If the index stored at position i of p is an invalid index, vout at position i will contain the default or
    custom delimiter value (d).

    Match:
    4 is at position 2 of the p array.
    4 is a valid index in array v (within range).
    50 is at position 4 of the v array.
    Therefore, position 2 of the result vout will contain 50.

    Miss:
    -7 is at position 1 of the p array.
    -7 is an invalid index in array v (out of range).
    Therefore, position 1 of the result vout will contain the delimiter.

    Edge Case Tests:
        (TODO)

    Examples
    --------
    Start with two arrays:

    >>> v = np.array([10, 20, 30, 40, 50, 60, 70])          #MATLab: v = [10 20 30 40 50 60 70];
    >>> p = np.array([0, -7, 4, 3, 7, 1, 2])                #MATLab: p = [1 -6 5 4 8 2 3];
    >>> vout = mbget(v,p)                                   #MATLab: vout = mbget(v,p);
    >>> print(vout)                                         #MATLab: vout
    [10  -2147483648  50  40 -2147483648  20  30]    #MATLab: [10.00  NaN  50.00  40.00  NaN  20.00  30.00]
    """
    # make sure a aValues and aIndex are both numpy arrays
    if isinstance(aValues, (list, tuple)):
        aValues = TypeRegister.FastArray(aValues)

    if isinstance(aIndex, (list, tuple)):
        aIndex = TypeRegister.FastArray(aIndex)

    # If one or both of the inputs is still not an array,
    # we can't proceed so raise an error.
    if not isinstance(aValues, np.ndarray) or not isinstance(aIndex, np.ndarray):
        raise TypeError(f"Values and index must be numpy arrays. Got {type(aValues)} {type(aIndex)}")

    elif aValues.dtype.char == 'O':
        raise TypeError(f"mbget does not support object types")

    elif aIndex.dtype.char not in NumpyCharTypes.AllInteger:
        raise TypeError(f"indices provided to mbget must be an integer type not {aIndex.dtype}")

    # mbget supports both 1D and 2D arrays.
    if aValues.ndim == 1:
        # TODO: probably need special code or parameter to set custom default value for NAN_TIME
        result = TypeRegister.MathLedger._MBGET(aValues, aIndex, d)
        result = TypeRegister.newclassfrominstance(result, aValues)
        return result

    elif aValues.ndim == 2:
        return _mbget_2dims(aValues, aIndex)

    else:
        raise ValueError("mbget does not support arrays of more than 2 dimensions.")

#------------------------------------------------------
def str_to_bytes(s):
    if isinstance(s, str):
        s = s.encode()
    return s

#------------------------------------------------------
def bytes_to_str(b):
    if isinstance(b,bytes):
        b = b.decode()
    return str(b)

#------------------------------------------------------
def to_str(s):
    if isinstance(s, bytes):
        return s.decode()
    if isinstance(s, str):
        return s
    return str(s)

#----------------------------------------------------------
# Checks for numpy array logical or just python bool
def islogical(a):
    if isinstance(a, np.ndarray): return a.dtype.char == '?'
    return isinstance(a,bool)

#----------------------------------------------------------
# similar to matlab ischar or iscellstr
# return True for both char and string arrays
def ischararray(a):
    if isinstance(a, np.ndarray): return a.dtype.char in 'SU'
    return False

#----------------------------------------------------------
# from sacore.core_utils import interpolate
# from apexqr_math.dataBox import dataBox
def interpolate(data, floatIndex):
   frac, whole = modf(floatIndex)
   whole = int(whole)

   if floatIndex < 0.0:
      raise ValueError("interpolate: cannot call with negative index: %r" % floatIndex)
   if floatIndex > len(data) - 1:
      return np.nan, whole
      #raise ValueError("interpolate: cannot call with index greater than length-1: %r > %r" %
      #                 (floatIndex, len(data) - 1))

   if not frac:
      return data[whole], whole

   return (1.0 - frac) * data[whole] + frac * data[whole + 1], whole
#----------------------------------------------------------
def quantile(arr: Optional[np.ndarray], q:List[float]=None):
    """
    Parameters
    ----------
    arr : data array, optional
        presumed computable, if None return headers instead
    q : list of float
        List of quantiles, defaults to ``[0.10, 0.25, 0.50, 0.75, 0.90]``.

    Returns
    -------
    array of floats, optionally return list of headers instead.
    """
    if q is None:
        q = [0.10, 0.25, 0.50, 0.75, 0.90]
    ivalid = arr.isnotnan()
    count = len(arr)
    cvalid = ivalid.sum()
    if cvalid == 0:
        retvals = [np.nan] * len(q)
    else:
        # sort will put nans at end
        # for signed integer sentinels, they will be in the beginning

        # make a fast copy first
        arr_sort = arr.copy()

        #inplace sort
        arr_sort.sort()

        if cvalid == count:
            # use entire array
            valid = arr_sort
            notvalid = 0
        else:
            # pick a subset removing the invalid
            notvalid = count - cvalid
            if arr_sort.dtype.char in NumpyCharTypes.Integer:
                valid = arr_sort[notvalid:-1]
            else:
                valid = arr_sort[0:cvalid]

        # this interpolate call is the same as numpy.percentile, which doesn't exist until a recent version of numpy
        # ## interpolate( data, pp / 100.0 * ( len( data ) - 1 ) ) == percentile( data, pp ) for pp \in [ 0, 100 ]
        # it also avoids multiple sorts...
        cvalidm1 =  cvalid -1

        # calculate the quantiles
        quantiles=[]
        for percent in q:
            interp, _ = interpolate(valid, percent * cvalidm1)
            quantiles.append(interp)

        retvals = quantiles
        # help recycler
        del arr_sort
    return TypeRegister.FastArray(retvals, dtype=np.float64)


#----------------------------------------------------------
def describe_helper(arr: Optional[np.ndarray], q: Optional[List[float]] = None) -> Union[List[str], np.ndarray]:
    """
    pass in None to get labels
    otherwise returns an array matching the labels
    """
    if q is None:
        q = [0.10, 0.25, 0.50, 0.75, 0.90]
    preamble = 'Count Valid Nans Mean Std Min '
    body = ''
    for percent in q:
        body += 'P' + str(int(percent*100)) + ' '
    postamble = 'Max MeanM'

    # Do I want to allow for optionally adding more pctls?
    if arr is None:
        allstrings = preamble + body + postamble
        return allstrings.split()
    ivalid = arr.isnotnan()
    count = len(arr)
    cvalid = ivalid.sum()
    if cvalid == 0:
        # NOTE: The 6 must be increased if we change code below
        retvals = [count, cvalid] + [np.nan] * (6 + len(q))
    else:
        # sort will put nans at end
        # for signed integer sentinels, they will be in the beginning

        # make a fast copy first
        arr_sort = arr.copy()

        #inplace sort
        arr_sort.sort()

        if cvalid == count:
            # use entire array
            valid = arr_sort
            notvalid = 0
        else:
            # pick a subset removing the invalid
            notvalid = count - cvalid
            if arr_sort.dtype.char in NumpyCharTypes.Integer:
                valid = arr_sort[notvalid:-1]
            else:
                valid = arr_sort[0:cvalid]

        # this interpolate call is the same as numpy.percentile, which doesn't exist until a recent version of numpy
        # ## interpolate( data, pp / 100.0 * ( len( data ) - 1 ) ) == percentile( data, pp ) for pp \in [ 0, 100 ]
        # it also avoids multiple sorts...
        cvalidm1 =  cvalid -1
        d1, d1break = interpolate(valid, 0.10 * cvalidm1)
        d9, d9break = interpolate(valid, 0.90 *  cvalidm1)

        # calculate the quantiles
        quantiles=[]
        for percent in q:
            interp, _ = interpolate(valid, percent * cvalidm1)
            quantiles.append(interp)

        # get nice data in the middle 80%
        # the mean calculation includes the low which gets truncated
        # the high needs to move up if it was truncated
        frac, whole = modf(0.90 *  cvalidm1)
        if frac:
            d9break = d9break + 1

        # slices do not include last value
        d9break = d9break + 1
        if d9break > cvalid:
            d9break = cvalid

        validm = valid[d1break:d9break]

        m0 = validm.mean() if len(validm) > 0 else np.nan
        vmean = valid.mean()
        retvals = [count, cvalid, notvalid, vmean, valid.std(),
                   valid[0]]
        retvals += quantiles
        retvals += [valid[-1], m0]
        # help recycler
        del arr_sort
    return TypeRegister.FastArray(retvals, dtype=np.float64)

#--------------------------------------------------------------------------
def describe(arr, q: Optional[List[float]] = None, fill_value = None):
    """
    Similar to pandas describe; columns remain stable, with extra column (Stats) added for names.

    Parameters
    ----------
    arr : array, list-like, or Dataset
        The data to be described.
    q : list of float, optional
        List of quantiles, defaults to ``[0.10, 0.25, 0.50, 0.75, 0.90]``.
    fill_value : optional
        Place-holder value for non-computable columns.

    Returns
    -------
    Dataset

    Examples
    --------
    >>> describe(arange(100) %3)
    *Stats     Col0
    ------   ------
    Count    100.00
    Valid    100.00
    Nans       0.00
    Mean       0.99
    Std        0.82
    Min        0.00
    P10        0.00
    P25        0.00
    P50        1.00
    P75        2.00
    P90        2.00
    Max        2.00
    MeanM      0.99
    <BLANKLINE>
    [13 rows x 2 columns] total bytes: 169.0 B
    """
    if q is None:
        q = [0.10, 0.25, 0.50, 0.75, 0.90]
    if arr is None:
        # support for old code that might pass in None to get labels
        return describe_helper(None, q=q)

    # first call is to get labels we use
    labels = TypeRegister.FastArray(describe_helper(None, q=q))
    if not isinstance(fill_value, (list, np.ndarray, dict, type(None))):
        fill_value = [fill_value] * len(labels)

    if isinstance(arr, TypeRegister.Dataset):
        retval = arr.reduce(describe_helper, q=q, as_dataset=True, fill_value=fill_value)
    else:
        if not isinstance (arr, TypeRegister.FastArray):
            arr = TypeRegister.FastArray(arr)
        name = arr.get_name()
        if name is None: name = 'Col0'

        retval = TypeRegister.Dataset({name: describe_helper(arr)})

    retval.Stats = labels
    retval.col_move_to_front(['Stats'])
    retval.label_set_names(['Stats'])
    return retval


#----------------------------------------------------------
def is_list_like(obj):
    """
    Check if the object is list-like.

    Objects that are considered list-like are for example Python
    lists, tuples, sets, and NumPy arrays.
    Strings and bytes, however, are not considered list-like.

    Parameters
    ----------
    obj : The object to check.

    Returns
    -------
    is_list_like : bool
        Whether `obj` has list-like properties.

    Examples
    --------
    >>> is_list_like([1, 2, 3])
    True

    >>> is_list_like({1, 2, 3})
    True

    >>> is_list_like(datetime(2017, 1, 1))
    False

    >>> is_list_like("foo")
    False

    >>> is_list_like(1)
    False
    """
    return (isinstance(obj, Iterable) and
            not isinstance(obj, (str, bytes)))

#----------------------------------------------------------
def get_default_value(arr):
    t = type(arr)
    if isinstance(arr, np.ndarray):
        if isinstance(arr, TypeRegister.Categorical):
            return arr.invalid_category
        return INVALID_DICT[arr.dtype.num]
    return np.nan

#----------------------------------------------------------
def str_replace(arr, old, new, missing=''):
    """

    Parameters
    ----------
    arr : array of str
        Array of strings to be replaced.
    old : array-like of str
        Unique list/array of possible values to replace.
    new : array-like of str
        Unique list/array of replacement values
    missing : str
        String value to insert if array value is not found in list of uniques.

    Returns
    -------
    New string array with replaced strings.
    """

    for i in [arr, old, new]:
        if isinstance(i, np.ndarray):
            if i.dtype.char not in 'US':
                raise TypeError(f"str_replace input must be arrays of strings")
        else:
            raise TypeError(f"str_replace input must be numpy array")

    if len(old) == len(new):
        c = TypeRegister.Categorical(arr, old, invalid=missing)
        d = TypeRegister.Categorical(c._fa, new, invalid=missing)
        return d.expand_array
    else:
        raise ValueError(f"Lists of old uniques, new uniques must be the same length.")


# -------------------------------------------------------
def sample(obj, N: int=10, filter=None):
    """
    Select N random samples from Dataset or FastArray.

    Parameters
    ----------
    obj: Dataset or FastArray
    N : int
        Number of rows to sample.
    filter : array-like (bool or rownums), optional
        Filter for rows to sample.

    Returns
    -------
    sample : Subset of `obj`
    """
    if filter is None:
        M = obj.shape[0]
        N = min(N, obj.shape[0])
    else:
        if filter.dtype.char == '?':  # Bool
            M = bool_to_fancy(filter)
        else:
            M = filter
        N = min(N, M.shape[0])
    # np.random.choice accepts as M either an int, which implicitly means 1-M, or a list of numbers
    idx = np.random.choice(M, N, replace=False)
    idx.sort()
    if len(obj.shape) == 1:
        return obj[idx]
    else:
        return obj[idx, :]

# ------------------------------------------------------------
def crc_match(arrlist: List[np.ndarray]) -> bool:
    """
    Perform a CRC check on every array in list, returns True if they were all a match.

    Parameters
    ----------
    arrlist : list of numpy arrays

    Returns
    -------
    bool
        True if all arrays in `arrlist` are structurally equal; otherwise, False.

    See Also
    --------
    numpy.array_equal
    """
    # This function also compares the shapes of the arrays in addition to the CRC value.
    # This is necessary for correctness because this function is (essentially) implementing a structural
    # equality comparison for arrays; a CRC value may not be impacted by zeros in some cases, e.g.
    #   crc32c(FA([b'', b'abcdef'])) == crc32c(FA([b'abcdef']))
    # which will give an incorrect result (since the arrays actually aren't structurally equal).

    # TODO: Also need to consider strides, at least until the CRC implementation respects them.
    #       Even then, we may want to calculate the CRC over the whole memory for performance reasons
    #       then use the strides here to disambiguate the results.
    #       Also consider dtype -- even if underlying data is identical, different dtypes means the data
    #       will be interpreted differently so the arrays aren't a match. Checking for shape already partially
    #       accounts for this, but we need to check dtype explicitly to account for bool vs. int8 and signed vs. unsigned int.
    crcs = {(arr.shape, crc32c(arr)) for arr in arrlist}
    return len(crcs) == 1
