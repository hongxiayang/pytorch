from __future__ import annotations

from typing import Any, Union, Sequence, Optional, Callable, Dict, Tuple, List
from enum import Enum

import torch

# nvFuser imports are conditional on CUDA being available
if torch.cuda.is_available():
    from torch._C._nvfuser import DataType  # type: ignore[import]

    _torch_dtype_to_nvfuser_dtype_map = {
        torch.cdouble: DataType.ComplexDouble,
        torch.cfloat: DataType.ComplexFloat,
        torch.double: DataType.Double,
        torch.float: DataType.Float,
        torch.half: DataType.Half,
        torch.bfloat16: DataType.BFloat16,
        torch.long: DataType.Int,
        torch.int: DataType.Int32,
        torch.bool: DataType.Bool,
    }
else:
    _torch_dtype_to_nvfuser_dtype_map = {}


def getnvFuserDtype(dtype: torch.dtype):
    """
    Translates from torch.dtype to nvFuser's DataType enum
    """
    return _torch_dtype_to_nvfuser_dtype_map[dtype]


ShapeType = Union[torch.Size, List[int], Tuple[int, ...]]
StrideType = Union[List[int], Tuple[int, ...]]
DimsType = Union[int, List[int], Tuple[int, ...]]
DimsSequenceType = Union[List[int], Tuple[int, ...]]
NumberType = Union[bool, int, float, complex]
Number = (bool, int, float, complex)


class TensorMeta(torch.Tensor):
    """
    Model tensor metadata.  Not a stock meta tensor because device is modeled
    as the original device (not meta device), also we have different behavior
    for some high level Python bindings
    """

    # Note: this will be an fx Node if it's ever
    # populated, but some Meta-internal jobs don't include fx
    node: Optional[Any]
    tname: str

    @staticmethod
    def __new__(
        cls,
        tensorlike: Optional[Union[TensorMeta, NumberType, torch.Tensor]] = None,
        *,
        shape: Optional[ShapeType] = None,
        strides: Optional[StrideType] = None,
        dtype: Optional[torch.dtype] = None,
        device: Optional[torch.device] = None,
    ):

        if isinstance(tensorlike, Number):
            assert not shape and (shape is None or isinstance(shape, Sequence))
            assert not strides and (strides is None or isinstance(strides, Sequence))
            inferred_shape: Tuple[int, ...] = ()
            inferred_strides: Tuple[int, ...] = ()
            inferred_dtype = type_to_dtype(type(tensorlike))
            inferred_device = torch.device("cpu")
            # TODO: This looks wrong, a number that is wrapped into a tensor
            # needs to behave differently than a scalar tensor for type
            # promotion purposes
        elif tensorlike is not None:
            assert isinstance(tensorlike, (TensorMeta, torch.Tensor))
            inferred_shape = tuple(tensorlike.shape)
            inferred_strides = tuple(tensorlike.stride())
            inferred_dtype = tensorlike.dtype
            inferred_device = tensorlike.device
        else:
            # If no tensorlike "example" is given then all metadata
            # must be provided explicitly
            assert shape is not None
            assert strides is not None
            assert dtype is not None
            assert device is not None

        shape = inferred_shape if shape is None else tuple(shape)
        strides = inferred_strides if strides is None else tuple(strides)
        dtype = inferred_dtype if dtype is None else dtype
        device = inferred_device if device is None else device

        r = torch.Tensor._make_wrapper_subclass(  # type: ignore[attr-defined]
            cls,
            shape,
            strides=strides,
            storage_offset=0,  # TODO: this is inaccurate
            dtype=dtype,
            device=device,
            requires_grad=False,
        )

        r.tname = ""
        r.node = None
        return r

    @classmethod
    def __torch_function__(
        cls,
        func: Callable,
        types: Sequence,
        args: Sequence[Any] = (),
        kwargs: Optional[Dict] = None,
    ):
        if kwargs is None:
            kwargs = {}

        if func in {
            torch.Tensor.ndim.__get__,  # type: ignore[attr-defined]
            torch.Tensor.numel,
            torch.Tensor.stride,
            torch.Tensor.dtype.__get__,  # type: ignore[attr-defined]
            torch.Tensor.shape.__get__,  # type: ignore[attr-defined]
            torch.Tensor.device.__get__,  # type: ignore[attr-defined]
        }:
            return super().__torch_function__(func, types, args, kwargs)

        if not hasattr(func, "meta"):
            raise ValueError(f"Callable {func} has no meta function!")

        return func.meta(*args, **kwargs)  # type: ignore[attr-defined]

    @classmethod
    def __torch_dispatch__(
        cls,
        func,
        types,
        args=(),
        kwargs=None,
    ):
        raise RuntimeError("this should be unreachable")

    # TODO: fx uses dunder repr to print objects in code
    def __repr__(self):
        return self.tname
        # return f"TensorMeta(dtype={self.dtype}, device={self.device}, shape={self.shape}, strides={self.stride()})"

    def __format__(self, format_spec):
        return self.tname


TensorLikeType = Union[torch.Tensor, TensorMeta]
TensorLike = (torch.Tensor, TensorMeta)
TensorSequenceType = Union[List[TensorLikeType], Tuple[TensorLikeType, ...]]


# TODO: look at using torch.testing.assert_close instead with an option
#   to just compare metadata
def compare_tensor_meta(a: TensorLikeType, b: TensorLikeType):
    """
    Checks that two tensor likes have the same shape,
    dtype, and device.

    In the future this will validate additional metadata, like
    strides.
    """
    assert isinstance(a, TensorLike)
    assert isinstance(b, TensorLike)

    for x, y in zip(a.shape, b.shape):
        if x != y:
            msg = "Shapes {0} and {1} are not equal!".format(a.shape, b.shape)
            raise AssertionError(msg)

    if a.dtype != b.dtype:
        msg = "Dtypes {0} and {1} are not equal!".format(a.dtype, b.dtype)
        raise AssertionError(msg)

    if a.device != b.device:
        msg = "Devices {0} and {1} are not equal!".format(a.device, b.device)
        raise AssertionError(msg)


#
# Common helper functions
#


def validate_dim_length(length: int):
    """
    Validates that an object represents a valid
    dimension length.
    """

    assert isinstance(length, int)
    assert length >= 0


def validate_shape(shape: Sequence):
    """
    Validates that a sequence represents a valid shape.
    """

    assert isinstance(shape, Sequence)
    for l in shape:
        validate_dim_length(l)


def validate_idx(shape: Sequence, idx: int):
    """
    Validates that idx is a valid idx for the given shape.
    0 and -1 is a valid index for an empty shape
    """

    assert isinstance(idx, int)
    ndim = len(shape) if len(shape) else 1
    assert idx >= 0 and idx < ndim


def validate_exclusive_idx(shape: Sequence, ex_idx: int):
    """
    Validates that ex_idx is a valid exclusive index
    for the given shape.
    """

    assert isinstance(ex_idx, int)
    assert ex_idx > 0 and ex_idx <= len(shape)


# "Wraps" a dim (up to one time) for the given rank, allowing
# dims to be specified using negative indices
def canonicalize_dim(rank: int, idx: int) -> int:
    # TODO: add a comment for why this is
    _rank = rank if rank != 0 else 1

    if idx >= 0 and idx < _rank:
        return idx

    if idx < 0:
        _idx = idx + _rank
    else:
        _idx = idx

    if _idx < 0 or _idx > _rank:
        msg = "Received out of bounds index {0} for tensor of rank {1}!".format(
            idx, rank
        )
        raise ValueError(msg)

    return _idx


# Takes a dimension or sequence of dimensions and "wraps" them,
# mapping negative offsets to positive ones
def canonicalize_dims(rank: int, indices: DimsType) -> DimsType:
    if isinstance(indices, int):
        return canonicalize_dim(rank, indices)

    return tuple(canonicalize_dim(rank, x) for x in indices)


def is_valid_permutation(rank: int, perm: DimsSequenceType) -> bool:
    """
    Validates that perm is a permutation of length rank.
    """

    if not isinstance(perm, Sequence):
        return False

    if not (tuple(sorted(perm)) == tuple(range(0, rank))):
        return False

    return True


def is_same_shape(a: Sequence, b: Sequence) -> bool:
    """
    Compares two shapes a and b, returning True if they are the same
    (their ranks and corresponding lengths match) and False otherwise.
    """

    return tuple(a) == tuple(b)


def is_cpu_scalar_tensor(a: Any) -> bool:
    return isinstance(a, TensorLike) and a.ndim == 0 and a.device.type == "cpu"


def check_same_device(*args, allow_cpu_scalar_tensors):
    """
    Checks that all Tensors in args have the same device.

    Raises a RuntimeError when:
      - args contains an object whose type is not Tensor or Number
      - two Tensor objects in args have different devices, unless one is a CPU scalar tensor and allow_cpu_scalar_tensors is True
    """
    # Short-circuits if all (one or fewer) arguments are trivially on the same device
    if len(args) <= 1:
        return

    # Note: cannot initialize device to the first arg's device (it may not have one)
    device = None
    for arg in args:
        if isinstance(arg, Number):
            continue
        elif isinstance(arg, TensorLike):
            if allow_cpu_scalar_tensors and is_cpu_scalar_tensor(arg):
                continue

            if device is None:
                device = arg.device

            if device != arg.device:
                msg = (
                    "Tensor on device "
                    + str(arg.device)
                    + " is not on the expected device "
                    + str(device)
                    + "!"
                )
                raise RuntimeError(msg)
        else:
            msg = (
                "Unexpected type when checking for same device, " + str(type(arg)) + "!"
            )
            raise RuntimeError(msg)


# Asserts if any of the following are true:
#   - a non-scalar or non-Tensor is given
#   - the shape of any tensors is distinct
def check_same_shape(*args, allow_cpu_scalar_tensors):
    """
    Checks that all Tensors in args have the same shape.

    Raises a RuntimeError when:
      - args contains an object whose type is not Tensor or Number
      - two Tensor objects in args have different devices
    """
    shape = None

    for arg in args:
        if isinstance(arg, Number):
            continue
        elif isinstance(arg, TensorLike):
            if allow_cpu_scalar_tensors and is_cpu_scalar_tensor(arg):
                continue

            if shape is None:
                shape = arg.shape

            if not is_same_shape(shape, arg.shape):
                msg = "Shape {0} is not the expected shape {1}!".format(
                    arg.shape, shape
                )
                raise RuntimeError(msg)
        else:
            msg = (
                "Unexpected type when checking for same shape, " + str(type(arg)) + "!"
            )
            raise RuntimeError(msg)


_integer_dtypes = (torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64)
_float_dtypes = (torch.float16, torch.bfloat16, torch.float32, torch.float64)
_complex_dtypes = (torch.complex32, torch.complex64, torch.complex128)


def is_boolean_dtype(dtype: torch.dtype) -> bool:
    return dtype is torch.bool


def is_integer_dtype(dtype: torch.dtype) -> bool:
    return dtype in _integer_dtypes


def is_float_dtype(dtype: torch.dtype) -> bool:
    return dtype in _float_dtypes


def is_complex_dtype(dtype: torch.dtype) -> bool:
    return dtype in _complex_dtypes


_complex_to_real_dtype_map = {
    torch.complex128: torch.float64,
    torch.complex64: torch.float32,
    torch.complex32: torch.float16,
}

_real_to_complex_dtype_map = {
    torch.float16: torch.complex32,
    torch.bfloat16: torch.complex64,
    torch.float32: torch.complex64,
    torch.float64: torch.complex128,
}


def corresponding_real_dtype(dtype: torch.dtype) -> torch.dtype:
    return _complex_to_real_dtype_map[dtype]


def corresponding_complex_dtype(dtype: torch.dtype) -> torch.dtype:
    return _real_to_complex_dtype_map[dtype]


def dtype_to_type(dtype: torch.dtype) -> type:
    """
    Computes the corresponding Python type (AKA "type kind") for the
    given dtype.
    """
    assert isinstance(dtype, torch.dtype)

    if dtype is torch.bool:
        return bool
    if dtype in _integer_dtypes:
        return int
    if dtype in _float_dtypes:
        return float
    if dtype in _complex_dtypes:
        return complex

    raise ValueError("Invalid dtype!")


_type_to_dtype_map = {
    bool: torch.bool,
    int: torch.int64,
    float: torch.float64,
    complex: torch.complex128,
}


def type_to_dtype(typ: type) -> torch.dtype:
    """
    Computes the corresponding dtype for a Number type.
    """
    return _type_to_dtype_map[typ]


_ordered_types = (bool, int, float, complex)


def get_higher_type(a: type, b: type) -> type:
    """
    Returns the higher of the two given Number types.

    The types are ordered bool -> int -> float -> complex.
    """
    # Type checking
    assert a in _ordered_types
    assert b in _ordered_types

    if a is b:
        return a

    for typ in _ordered_types:
        if a is typ:
            return b
        if b is typ:
            return a

    raise ValueError("Unknown Python scalar type!")


# Returns the higher of two torch datatypes a and b or, if the two
#   are not ordered relative to each other, the next
#   higher datatype
def get_higher_dtype(
    a: Optional[Union[torch.dtype, TensorLikeType, NumberType]],
    b: Optional[Union[torch.dtype, TensorLikeType, NumberType]],
) -> Optional[torch.dtype]:
    """
    Computes the "lowest" datatype that is weakly
    "higher" than both a and b.
    """

    # Type checking
    assert a is None or isinstance(a, (torch.dtype, TensorLike, Number))
    assert b is None or isinstance(b, (torch.dtype, TensorLike, Number))

    def _extract_dtype(
        x: Optional[Union[torch.dtype, TensorLikeType, NumberType]]
    ) -> Optional[torch.dtype]:
        if x is None:
            return None
        if isinstance(x, torch.dtype):
            return x
        if isinstance(x, TensorLike):
            return x.dtype
        if isinstance(x, Number):
            return type_to_dtype(type(x))

        raise RuntimeError("Unexpected type given to _extract_dtype!")

    a, b = _extract_dtype(a), _extract_dtype(b)

    if a is b:
        return a

    if a is None:
        return b

    if b is None:
        return a

    ordered_datatypes = (
        (torch.bool,),
        (torch.uint8, torch.int8),
        (torch.int16,),
        (torch.int32,),
        (torch.int64,),
        (torch.float16, torch.bfloat16),
        (torch.float32,),
        (torch.float64,),
        (torch.complex32,),
        (torch.complex64,),
        (torch.complex128,),
    )

    for idx, dtypes in enumerate(ordered_datatypes):
        if a in dtypes and b in dtypes:
            return ordered_datatypes[idx + 1][0]
        if a in dtypes:
            return b
        if b in dtypes:
            return a

    raise RuntimeError("Unexpected termination!")


def is_weakly_lesser_type(a: type, b: type) -> bool:
    """
    Compares two types, a and b, returning True if a is weakly "less" than b.

    The comparison is determined by the following type ordering: bool, int, float, complex.
    """
    ordered_types = (
        bool,
        int,
        float,
        complex,
    )

    assert a in ordered_types
    assert b in ordered_types

    for typ in ordered_types:
        if a == typ:
            return True
        if b == typ:
            return False

    raise RuntimeError("Unexpected termination!")


def can_safe_cast_to(*, cast_to: torch.dtype, cast_from: torch.dtype) -> bool:
    for fn in (is_complex_dtype, is_float_dtype, is_integer_dtype, is_boolean_dtype):
        if fn(cast_to):
            return True
        if fn(cast_from):
            return False

    raise ValueError("Received unknown dtypes {0}, {1}!".format(cast_to, cast_from))


def check_same_dtype(*args):
    """
    Checks that all Tensors in args have the same device and that all Numbers have the
    same corresponding Python type.

    Raises a RuntimeError when:
      - args contains an object whose type is not Tensor or Number
      - two Tensors objects in args have different dtypes
      - two Number objects in args have different types
      - there are Tensors and Numbers in args, and one of those Tensors corresponding
          Python types is different from the type of one of those Numbers
    """
    full_dtype = None
    scalar_type = None

    for arg in args:
        if isinstance(arg, Number):
            # Scalar type checking is disabled (and may be removed in the future)
            continue
            # if scalar_type is None:
            #     scalar_type = type(arg)

            # if scalar_type is not type(arg):
            #     msg = (
            #         "Scalar of type "
            #         + str(type(arg))
            #         + " is not the expected type of "
            #         + str(scalar_type)
            #         + "!"
            #     )
            #     raise RuntimeError(msg)
        elif isinstance(arg, TensorLike):
            if full_dtype is None:
                full_dtype = arg.dtype
            if scalar_type is None:
                scalar_type = dtype_to_type(arg.dtype)

            if full_dtype is not arg.dtype:
                msg = (
                    "Tensor with dtype "
                    + str(arg.dtype)
                    + " is not the expected dtype of "
                    + str(full_dtype)
                    + "!"
                )
                raise RuntimeError(msg)

            arg_type = dtype_to_type(arg.dtype)
            if arg_type is not scalar_type:
                msg = (
                    "Tensor with corresponding Python type "
                    + str(arg_type)
                    + " is not the expected type of "
                    + str(scalar_type)
                    + "!"
                )
                raise RuntimeError(msg)
        else:
            msg = (
                "Unexpected type when checking for same dtype, " + str(type(arg)) + "!"
            )
            raise RuntimeError(msg)


# Maps datatypes to their computation types for elementwise operations
_computation_dtype_map = {
    torch.bfloat16: torch.float32,
    torch.float16: torch.float32,
    torch.complex32: torch.complex64,
}


def _get_computation_dtype(dtype: torch.dtype) -> torch.dtype:
    return _computation_dtype_map.get(dtype, dtype)


class ELEMENTWISE_TYPE_PROMOTION_KIND(Enum):
    DEFAULT = (0,)
    INT_TO_FLOAT = (1,)
    ALWAYS_BOOL = (2,)
    OP_MATH = (3,)
    COMPLEX_TO_FLOAT = (4,)
    BOOL_TO_LONG = (5,)


# TODO: document type promotion kinds
def elementwise_dtypes(
    *_args, type_promotion_kind: ELEMENTWISE_TYPE_PROMOTION_KIND
) -> Tuple[torch.dtype, torch.dtype]:
    """
    Computes the computation and result dtypes for elementwise type promotion
    on the given arguments and with the given elementwise type promotion kind.

    Note that not all inputs to an elementwise operation necessarily participate in type promotion.
    For example, the "alpha" parameter of torch.add does not participate in type promotion,
    although it is cast to the Python type corresponding to the computation dtype that
    the type promotion algorithm determines.

    Default elementwise type promotion, which all other type promotion kinds tweak (see below),
    first decides which of four ordered types to use:

    bool -> integer -> floating point -> complex

    The selected type is the "lowest" type in the above list such that all number arguments
    have a weakly "lower" type and all tensor arguments have a weakly lower corresponding
    type for their dtype.

    Once the type is determined, the particular result dtype is found. The dtypes are
    partially ordered as follows:

    bool -> uint8, int8 -> int16 -> int32 -> int64 ->
      float16, bfloat16 -> float32 -> float64 -> complex32 -> complex64 -> complex128

    The result dtype is selected by:
      - if no tensor's dtype has the same corresponding type as the one selected,
          then the result dtype is the (default) dtype corresponding to the selected type
          (for example, 1.5 + an integer tensor has a result dtype of the default floating point dtype)
      - if the result type is complex then the dtype is:
        -  the default complex dtype if there are no floating point or complex tensors
        -  if there are floating point or complex tensors with one or more dimensions, then
            the complex dtype corresponding to the highest corresponding complex dtype among those tensors
            (for example, double + cfloat -> cdouble)
        -  if there are only floating point or complex tensors with zero dimensions, then
            the complex dtype corresponding to the highest corresponding complex dtype among those tensors
      - if the first two cases do not apply, the result dtype is the highest dtype among
          all tensors with one or more dimensions of the output type, and if there are no such
          tensors then it's the highest dtype among all tensors with zero dimensions of the output type
          (for example, long + half -> half, even if the half tensor has zero dimensions)

    The "corresponding complex dtypes" are:
      float16    -> complex32
      bfloat16   -> complex64
      float32    -> complex64
      float64    -> complex128
      complex32  -> complex32
      complex64  -> complex64
      complex128 -> complex128

    The DEFAULT type promotion option computes per above, and uses the result dtype as the computation dtype.

    The OP_MATH, INT_TO_FLOAT, COMPLEX_TO_FLOAT and BOOL_TO_LONG type promotion options tweak the above slightly.
    OP_MATH determines a "computation dtype" from the result dtype, and the mapping is simple:

      float16   -> float32
      bfloat16  -> float32
      complex32 -> complex64

    INT_TO_FLOAT, COMPLEX_TO_FLOAT, and BOOL_TO_LONG compute the computation type in the same way, but INT_TO_FLOAT
    and BOOL_TO_LONG map the result dtype to another dtype first, and COMPLEX_TO_FLOAT maps its result dtype
    after the compuation dtype is determined, as follows:

      INT_TO_FLOAT  maps all boolean and integer result dtypes to the default floating point dtype
      COMPLEX_TO_FLOAT  maps complex result dtypes to their corresponding floating point dtype
      BOOL_TO_LONG maps the boolean result dtype to long

    The "corresponding floating point dtypes" are:
      complex32  -> float16
      complex64  -> float32
      complex128 -> float64

    The ALWAYS_BOOL type promotion option always maps the result dtype to bool.

    Example operators for each type promotion option:
      DEFAULT          : nextafter
      OP_MATH          : add
      INT_TO_FLOAT     : sin
      COMPLEX_TO_FLOAT : abs
      BOOL_TO_LONG     : pow
      ALWAYS_BOOL      : eq

    """

    args = tuple(x for x in _args if x is not None)

    highest_type: type = bool
    for x in args:
        if not isinstance(x, (Number, TensorLike)):
            msg = (
                "Unexpected type {0} when computing elementwise type promotion!".format(
                    str(type(x))
                )
            )
            raise ValueError(msg)

        if isinstance(x, Number):
            highest_type = get_higher_type(highest_type, type(x))
        else:
            # x is a TensorLike
            highest_type = get_higher_type(highest_type, dtype_to_type(x.dtype))

    result_dtype = None

    def _find_highest_dtype_filtered(
        args, filter, *, float_as_complex=False, all_tensors_equal=False
    ) -> Optional[torch.dtype]:
        zero_dim_tensor_dtype = None
        one_plus_dim_tensor_dtype = None
        for x in args:
            if isinstance(x, TensorLike) and filter(x.dtype):
                _dtype = x.dtype
                if float_as_complex and is_float_dtype(_dtype):
                    _dtype = corresponding_complex_dtype(_dtype)
                if x.ndim == 0 and not all_tensors_equal:
                    zero_dim_tensor_dtype = get_higher_dtype(
                        zero_dim_tensor_dtype, _dtype
                    )
                else:
                    # x.ndim > 0 or all_tensors_equal
                    one_plus_dim_tensor_dtype = get_higher_dtype(
                        one_plus_dim_tensor_dtype, _dtype
                    )

        # Prefers dtype of tensors with one or more dimensions
        if one_plus_dim_tensor_dtype is not None:
            return one_plus_dim_tensor_dtype

        return zero_dim_tensor_dtype

    if highest_type is float:
        result_dtype = _find_highest_dtype_filtered(args, is_float_dtype)
        result_dtype = (
            torch.get_default_dtype() if result_dtype is None else result_dtype
        )
    elif highest_type is complex:
        # NOTE: complex x float type promotion is incorrectly implemented in PyTorch today
        # it will treat zero dim and non-zero-dim float and complex tensors equally
        # unless there's a non-zero-dim complex tensor
        # the following captures this oddity
        has_one_plus_dim_complex_tensor = False
        for x in args:
            if isinstance(x, TensorLike) and x.ndim > 0 and is_complex_dtype(x.dtype):
                has_one_plus_dim_complex_tensor = True
                break

        if has_one_plus_dim_complex_tensor:
            result_dtype = _find_highest_dtype_filtered(
                args,
                lambda x: is_float_dtype(x) or is_complex_dtype(x),
                float_as_complex=True,
            )
        else:
            # no complex tensors of rank 1+
            # NOTE: bugged case where all tensors are equal
            result_dtype = _find_highest_dtype_filtered(
                args,
                lambda x: is_float_dtype(x) or is_complex_dtype(x),
                float_as_complex=True,
                all_tensors_equal=True,
            )

        if result_dtype is None:
            result_dtype = corresponding_complex_dtype(torch.get_default_dtype())
    elif highest_type is int:
        result_dtype = _find_highest_dtype_filtered(args, is_integer_dtype)
        result_dtype = torch.long if result_dtype is None else result_dtype
    else:
        # highest_type is bool
        result_dtype = torch.bool

    if type_promotion_kind is ELEMENTWISE_TYPE_PROMOTION_KIND.DEFAULT:
        return result_dtype, result_dtype
    elif type_promotion_kind is ELEMENTWISE_TYPE_PROMOTION_KIND.OP_MATH:
        return _get_computation_dtype(result_dtype), result_dtype
    elif type_promotion_kind is ELEMENTWISE_TYPE_PROMOTION_KIND.INT_TO_FLOAT:
        if is_integer_dtype(result_dtype) or is_boolean_dtype(result_dtype):
            result_dtype = torch.get_default_dtype()
        return _get_computation_dtype(result_dtype), result_dtype
    elif type_promotion_kind is ELEMENTWISE_TYPE_PROMOTION_KIND.COMPLEX_TO_FLOAT:
        if is_complex_dtype(result_dtype):
            # Note: computation still occurs in complex
            return _get_computation_dtype(result_dtype), corresponding_real_dtype(
                result_dtype
            )
        return _get_computation_dtype(result_dtype), result_dtype
    elif type_promotion_kind is ELEMENTWISE_TYPE_PROMOTION_KIND.BOOL_TO_LONG:
        if is_boolean_dtype(result_dtype):
            return torch.long, torch.long
        return result_dtype, result_dtype
    elif type_promotion_kind is ELEMENTWISE_TYPE_PROMOTION_KIND.ALWAYS_BOOL:
        return result_dtype, torch.bool
    else:
        raise ValueError(
            "Unknown type promotion kind {0}".format(str(type_promotion_kind))
        )


def wrap_device(d: Union[str, torch.device]) -> torch.device:
    """
    Wraps strings into torch.device objects.

    Given torch.device objects are returned unmodified.
    """

    assert isinstance(d, (str, torch.device))
    if isinstance(d, str):
        return torch.device(d)

    return d


def make_contiguous_strides_for(shape: Sequence) -> Tuple[int, ...]:
    validate_shape(shape)
    if not shape:
        return ()

    multiplier = 1
    strides = [multiplier]
    for l in reversed(shape[1:]):
        multiplier = l * multiplier
        strides.append(multiplier)

    return tuple(reversed(strides))


def compute_reduction_output_shape(
    shape: ShapeType, dimensions: Sequence
) -> Tuple[int, ...]:
    for idx in dimensions:
        validate_idx(shape, idx)

    new_shape = []
    for idx in range(len(shape)):
        if idx in dimensions:
            continue

        new_shape.append(shape[idx])

    return tuple(new_shape)


def reduction_dims(shape: ShapeType, dims: Optional[Sequence]) -> Tuple[int, ...]:
    if dims is None:
        return tuple(range(len(shape)))
    dims = tuple(canonicalize_dim(len(shape), idx) for idx in dims)
    if len(dims) != len(set(dims)):
        raise RuntimeError("duplicate value in the list of dims")
    return dims
