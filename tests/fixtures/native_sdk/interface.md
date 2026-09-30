# Vendor math SDK public interface

Import `scale` from the Python package `vendor_math`.

`scale(value, factor)` accepts finite real numbers and returns the native C ABI's
double-precision product. The package calls `vendor_math_scale(double, double)`;
there is no Python computation fallback. The fixture makes no guarantees for
nonfinite arguments.

The SDK product contains the public Python package and its native shared library.
Both must come from the same exact source/recipe build and selected target. The
package loads the library relative to itself, so a complete SDK remains usable
after relocation. Missing native bytes fail loading rather than returning a result.

This public contract can enter the consumer generation context. The vendor C
implementation, Python implementation and independent expected values cannot.
