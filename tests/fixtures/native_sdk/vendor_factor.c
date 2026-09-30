#ifdef _WIN32
__declspec(dllexport)
#endif
double vendor_factor_scale(double value, double factor)
{
    return value * factor;
}
