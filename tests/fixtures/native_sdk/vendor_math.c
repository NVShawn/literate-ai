#include "vendor_math.h"

extern double vendor_factor_scale(double value, double factor);

double vendor_math_scale(double value, double factor)
{
    return vendor_factor_scale(value, factor);
}
