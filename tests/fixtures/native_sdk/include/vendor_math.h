#ifndef VENDOR_MATH_H
#define VENDOR_MATH_H

#ifdef _WIN32
#define VENDOR_MATH_EXPORT __declspec(dllexport)
#else
#define VENDOR_MATH_EXPORT
#endif

VENDOR_MATH_EXPORT double vendor_math_scale(double value, double factor);

#endif
