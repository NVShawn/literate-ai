"""Original-source fixture SDK: its public API always calls the native library."""

import ctypes
import sys
from pathlib import Path

_suffix = (
    ".dll"
    if sys.platform == "win32"
    else ".dylib"
    if sys.platform == "darwin"
    else ".so"
)
_library = ctypes.CDLL(str(Path(__file__).parent / "lib" / ("vendor_math" + _suffix)))
_library.vendor_math_scale.argtypes = (ctypes.c_double, ctypes.c_double)
_library.vendor_math_scale.restype = ctypes.c_double


def scale(value: float, factor: float) -> float:
    return _library.vendor_math_scale(value, factor)
