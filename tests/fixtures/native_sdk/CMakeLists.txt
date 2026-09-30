cmake_minimum_required(VERSION 3.20)
project(vendor_math VERSION 1.0.0 LANGUAGES C)

add_library(vendor_math SHARED vendor_math.c)
add_library(vendor_factor SHARED vendor_factor.c)
target_link_libraries(vendor_math PRIVATE vendor_factor)
target_include_directories(vendor_math PUBLIC include)
set_target_properties(vendor_math vendor_factor PROPERTIES
    PREFIX ""
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/sdk/python/vendor_math/lib"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/sdk/python/vendor_math/lib")
foreach(configuration DEBUG RELEASE RELWITHDEBINFO MINSIZEREL)
    set_target_properties(vendor_math vendor_factor PROPERTIES
        LIBRARY_OUTPUT_DIRECTORY_${configuration} "${CMAKE_BINARY_DIR}/sdk/python/vendor_math/lib"
        RUNTIME_OUTPUT_DIRECTORY_${configuration} "${CMAKE_BINARY_DIR}/sdk/python/vendor_math/lib")
endforeach()
if(APPLE)
    set_target_properties(vendor_math vendor_factor PROPERTIES
        BUILD_WITH_INSTALL_RPATH TRUE INSTALL_RPATH "@loader_path")
elseif(UNIX)
    set_target_properties(vendor_math vendor_factor PROPERTIES
        BUILD_WITH_INSTALL_RPATH TRUE INSTALL_RPATH "$ORIGIN")
endif()
configure_file(python/vendor_math/__init__.py
    "${CMAKE_BINARY_DIR}/sdk/python/vendor_math/__init__.py" COPYONLY)
configure_file(LICENSE "${CMAKE_BINARY_DIR}/sdk/LICENSE" COPYONLY)
