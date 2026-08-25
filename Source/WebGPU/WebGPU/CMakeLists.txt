# FIXME: Use the standard WEBKIT_FRAMEWORK family of macros to describe WebGPU.

list(APPEND WebGPU_UNIFIED_SOURCE_LIST_FILES
    "Sources.txt"
)

set(WGSL_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../WGSL)

if (SWIFT_REQUIRED)
    set(WebGPU_SWIFT_INTEROP_SOURCES
        Buffer.mm
        CommandEncoder.mm
        Queue.mm
    )
    foreach (_f IN LISTS WebGPU_SWIFT_INTEROP_SOURCES)
        cmake_path(GET _f FILENAME _fn)
        list(APPEND WebGPU_UNIFIED_SOURCE_EXCLUDES "(^|/)${_fn}( |$)")
    endforeach ()
endif ()

WEBKIT_COMPUTE_SOURCES(WebGPU)

if (DEFINED WebGPU_SWIFT_INTEROP_SOURCES)
    list(REMOVE_ITEM WebGPU_SOURCES ${WebGPU_SWIFT_INTEROP_SOURCES})
endif ()

# WebGPU compiles uniformly with -fobjc-arc, so the bundler's ARC .mm bundles
# (WebGPU_ARC_SOURCES) compile directly into the library alongside WebGPU_SOURCES.
add_library(WebGPU SHARED ${WebGPU_SOURCES} ${WebGPU_ARC_SOURCES})

WEBKIT_ADD_TARGET_UNSAFE_BUFFER_WARNINGS(WebGPU)

WEBKIT_ADD_PREFIX_HEADER(WebGPU WebGPUPrefix.h PREFIX_LANGUAGES OBJCXX)

set_target_properties(WebGPU PROPERTIES
    FRAMEWORK TRUE
    OUTPUT_NAME WebGPU
)

if (WebGPU_INSTALL_NAME_DIR)
    set_target_properties(WebGPU PROPERTIES
        INSTALL_NAME_DIR "${WebGPU_INSTALL_NAME_DIR}"
    )
endif ()

if (WEBKIT_SDK_IS_IOS_FAMILY)
    set(MACOSX_FRAMEWORK_IDENTIFIER com.apple.WebGPU)
    set(BUNDLE_VERSION "${MACOSX_FRAMEWORK_BUNDLE_VERSION}")
    set(SHORT_VERSION_STRING "${WEBKIT_MAC_VERSION}")
    set(PRODUCT_NAME "WebGPU")
    set(PRODUCT_BUNDLE_IDENTIFIER "com.apple.WebGPU")
    configure_file(${CMAKE_SOURCE_DIR}/Source/JavaScriptCore/Info.plist
        ${CMAKE_CURRENT_BINARY_DIR}/WebGPU-Info.plist)
endif ()

find_library(COREFOUNDATION_FRAMEWORK CoreFoundation)
find_library(COREGRAPHICS_FRAMEWORK CoreGraphics)
find_library(COREVIDEO_FRAMEWORK CoreVideo)
find_library(FOUNDATION_FRAMEWORK Foundation)
find_library(IOSURFACE_FRAMEWORK IOSurface)
find_library(METAL_FRAMEWORK Metal)
find_library(QUARTZCORE_FRAMEWORK QuartzCore)

target_link_libraries(WebGPU PRIVATE
    ${COREFOUNDATION_FRAMEWORK}
    ${COREGRAPHICS_FRAMEWORK}
    ${COREVIDEO_FRAMEWORK}
    ${FOUNDATION_FRAMEWORK}
    ${IOSURFACE_FRAMEWORK}
    ${METAL_FRAMEWORK}
    ${QUARTZCORE_FRAMEWORK}
    ICU::uc
    JavaScriptCore
    WGSLCore
    INTERFACE WebKitAdditions
)

target_include_directories(WebGPU PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/..
    ${WGSL_DIR}
    ${WGSL_DIR}/AST
    ${WGSL_DIR}/Metal
    ${CMAKE_CURRENT_BINARY_DIR}/../WGSL
    ${WTF_FRAMEWORK_HEADERS_DIR}
    ${bmalloc_FRAMEWORK_HEADERS_DIR}
)

target_compile_definitions(WebGPU PRIVATE
    WGPU_SHARED_LIBRARY
    WGPU_IMPLEMENTATION
    __WEBGPU__
)

if (SWIFT_REQUIRED)
    webkit_target_compile_definitions(WebGPU PRIVATE ENABLE_WEBGPU_SWIFT=1)
endif ()

# Generate a cmakeconfig.h for the WebGPU target, carrying the coverage feature defines.
#
# This file is deliberately not ${CMAKE_BINARY_DIR}/cmakeconfig.h, and its directory is
# placed ahead of the build root on WebGPU's include path, so WebGPU's translation units see
# none of the ENABLE_/USE_/HAVE_ values the rest of the build sees -- 39 of them differ, and
# five differ in value. That divergence is pre-existing and is left alone here.
#
# ENABLE_LLVM_COVERAGE cannot be left out of it, though. Instance.mm is the one translation
# unit that defines WebGPU.framework's __llvm_profile_filename, under
# #if ENABLE(LLVM_COVERAGE); with an empty cmakeconfig.h that conditional is 0, the framework
# links against the profile runtime's own default.profraw instead, and its counters are
# written to whatever the GPU process's working directory happens to be -- where the sandbox
# denies the write. Measured on a coverage build of WebGPU.framework before this change:
# 2,514 __profc_ symbols, nine __llvm_* sections, and no
# /private/tmp/WebKitCoverage/WebGPU_%4m%c.profraw string anywhere in the image. That is the
# same silent failure that reported 45,005 instrumented WebGPU lines as 0.00% on the Xcode
# build, where the cause was a missing per-framework define rather than a shadowed one.
#
# Emitted with their real values rather than only when set, so that the
# LLVM_PROFILE_GENERATION/LLVM_COVERAGE #error in Instance.mm still fires here.
set(_webgpu_cmakeconfig "")
foreach (_feature IN ITEMS ENABLE_LLVM_COVERAGE ENABLE_LLVM_PROFILE_GENERATION)
    if (${_feature})
        string(APPEND _webgpu_cmakeconfig "#define ${_feature} 1\n")
    else ()
        string(APPEND _webgpu_cmakeconfig "#define ${_feature} 0\n")
    endif ()
endforeach ()
file(CONFIGURE OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/cmakeconfig.h
    CONTENT "${_webgpu_cmakeconfig}" @ONLY)
unset(_webgpu_cmakeconfig)
unset(_feature)
target_include_directories(WebGPU PRIVATE ${CMAKE_CURRENT_BINARY_DIR})

target_compile_options(WebGPU PRIVATE
    $<$<NOT:$<COMPILE_LANGUAGE:Swift>>:-ObjC++>
    $<$<NOT:$<COMPILE_LANGUAGE:Swift>>:-std=c++2b>
    $<$<NOT:$<COMPILE_LANGUAGE:Swift>>:-fobjc-arc>
)

if (WEBKIT_PRIVATE_FRAMEWORKS_COMPILE_FLAG)
    target_compile_options(WebGPU PRIVATE ${WEBKIT_PRIVATE_FRAMEWORKS_COMPILE_FLAG})
endif ()

set(WebGPU_FRAMEWORK_HEADERS_DIR "${CMAKE_BINARY_DIR}/WebGPU/Headers")
file(MAKE_DIRECTORY ${WebGPU_FRAMEWORK_HEADERS_DIR}/WebGPU)
file(COPY
    ${CMAKE_CURRENT_SOURCE_DIR}/WebGPU.h
    ${CMAKE_CURRENT_SOURCE_DIR}/WebGPUExt.h
    DESTINATION ${WebGPU_FRAMEWORK_HEADERS_DIR}/WebGPU)

set(WebGPU_PRIVATE_FRAMEWORK_HEADERS_DIR "${CMAKE_BINARY_DIR}/WebGPU/PrivateHeaders")
file(MAKE_DIRECTORY ${WebGPU_PRIVATE_FRAMEWORK_HEADERS_DIR}/WebGPU)
file(COPY
    ${CMAKE_CURRENT_SOURCE_DIR}/WebGPU.h
    ${CMAKE_CURRENT_SOURCE_DIR}/WebGPUExt.h
    ${CMAKE_CURRENT_SOURCE_DIR}/CxxBridgingPublic.h
    ${CMAKE_CURRENT_SOURCE_DIR}/WGPUBufferImpl.h
    ${CMAKE_CURRENT_SOURCE_DIR}/WGPUQuerySetImpl.h
    ${CMAKE_CURRENT_SOURCE_DIR}/WGPUTextureImpl.h
    ${CMAKE_CURRENT_SOURCE_DIR}/WGPUTextureViewImpl.h
    ${CMAKE_CURRENT_SOURCE_DIR}/WorkAround173516139.h
    DESTINATION ${WebGPU_PRIVATE_FRAMEWORK_HEADERS_DIR}/WebGPU)

target_include_directories(WebGPU PUBLIC ${WebGPU_FRAMEWORK_HEADERS_DIR})
target_include_directories(WebGPU PRIVATE ${WebGPU_PRIVATE_FRAMEWORK_HEADERS_DIR})

if (APPLE)
    file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/WebGPU/Modules")
    configure_file(${CMAKE_CURRENT_SOURCE_DIR}/WebGPU.modulemap
        ${CMAKE_BINARY_DIR}/WebGPU/Modules/module.modulemap COPYONLY)
    configure_file(${CMAKE_CURRENT_SOURCE_DIR}/WebGPU_Private.modulemap
        ${CMAKE_BINARY_DIR}/WebGPU/Modules/module.private.modulemap COPYONLY)

    set(_webgpu_fw "${CMAKE_LIBRARY_OUTPUT_DIRECTORY}/WebGPU.framework")
    if (WEBKIT_SDK_IS_IOS_FAMILY)
        file(MAKE_DIRECTORY "${_webgpu_fw}")

        if (SWIFT_REQUIRED)
            set(_webgpu_swiftmodule_dir "${CMAKE_BINARY_DIR}/WebGPU/Modules/WebGPU.swiftmodule")
            file(MAKE_DIRECTORY ${_webgpu_swiftmodule_dir})
            set(_webgpu_swift_output "${CMAKE_CURRENT_BINARY_DIR}")
            set(WebGPU_POST_BUILD_COMMAND
                COMMAND ${CMAKE_COMMAND} -E copy_if_different
                    "${_webgpu_swift_output}/WebGPU.swiftmodule"
                    "${_webgpu_swiftmodule_dir}/${WEBKIT_SWIFT_MODULE_TRIPLE}.swiftmodule"
                COMMAND ${CMAKE_COMMAND} -E copy_if_different
                    "${_webgpu_swift_output}/WebGPU.swiftdoc"
                    "${_webgpu_swiftmodule_dir}/${WEBKIT_SWIFT_MODULE_TRIPLE}.swiftdoc"
                COMMAND ${CMAKE_COMMAND} -E copy_if_different
                    "${_webgpu_swift_output}/WebGPU.abi.json"
                    "${_webgpu_swiftmodule_dir}/${WEBKIT_SWIFT_MODULE_TRIPLE}.abi.json"
            )
        endif ()

        # Strip Swift compiler artifacts and lift dSYM out of the framework
        # BEFORE codesigning on iOS. Doing this in a separate POST_BUILD chain
        # afterward would modify the framework after the seal and break
        # code-sign verification at sim runtime.
        set(_webgpu_ios_strip_artifacts "")
        if (WEBKIT_SDK_IS_IOS_FAMILY)
            set(_webgpu_ios_strip_artifacts
                COMMAND ${CMAKE_COMMAND} -E rm -f
                    ${_webgpu_fw}/WebGPU.emit-module.d
                    ${_webgpu_fw}/WebGPU.swiftdeps
                COMMAND ${CMAKE_COMMAND}
                    -DSRC=${_webgpu_fw}/WebGPU.dSYM
                    -DDST=${CMAKE_LIBRARY_OUTPUT_DIRECTORY}/WebGPU.framework.dSYM
                    -P ${WEBKIT_DIR}/MoveDirectory.cmake
            )
        endif ()

        add_custom_command(TARGET WebGPU POST_BUILD
            ${WebGPU_POST_BUILD_COMMAND}
            COMMAND ${CMAKE_COMMAND} -E copy_directory
                ${WebGPU_PRIVATE_FRAMEWORK_HEADERS_DIR}/WebGPU
                ${_webgpu_fw}/PrivateHeaders
            COMMAND ${CMAKE_COMMAND} -E copy_directory
                ${CMAKE_BINARY_DIR}/WebGPU/Modules
                ${_webgpu_fw}/Modules
            COMMAND ${CMAKE_COMMAND} -E copy
                ${CMAKE_CURRENT_BINARY_DIR}/WebGPU-Info.plist
                ${_webgpu_fw}/Info.plist
            ${_webgpu_ios_strip_artifacts}
            COMMAND codesign --force --sign - ${_webgpu_fw}
            COMMENT "Populating WebGPU.framework PrivateHeaders/, Modules/, and codesigning")
    else ()
        file(MAKE_DIRECTORY "${_webgpu_fw}/Versions/A")
        if (NOT EXISTS "${_webgpu_fw}/Versions/Current")
            file(CREATE_LINK "A" "${_webgpu_fw}/Versions/Current" SYMBOLIC)
        endif ()
        if (NOT EXISTS "${_webgpu_fw}/Versions/A/PrivateHeaders")
            file(CREATE_LINK "${WebGPU_PRIVATE_FRAMEWORK_HEADERS_DIR}/WebGPU"
                             "${_webgpu_fw}/Versions/A/PrivateHeaders" SYMBOLIC)
        endif ()
        if (NOT EXISTS "${_webgpu_fw}/Versions/A/Modules")
            file(CREATE_LINK "${CMAKE_BINARY_DIR}/WebGPU/Modules"
                             "${_webgpu_fw}/Versions/A/Modules" SYMBOLIC)
        endif ()
        if (NOT EXISTS "${_webgpu_fw}/PrivateHeaders")
            file(CREATE_LINK "Versions/Current/PrivateHeaders"
                             "${_webgpu_fw}/PrivateHeaders" SYMBOLIC)
        endif ()
        if (NOT EXISTS "${_webgpu_fw}/Modules")
            file(CREATE_LINK "Versions/Current/Modules"
                             "${_webgpu_fw}/Modules" SYMBOLIC)
        endif ()
    endif ()
    unset(_webgpu_fw)
endif ()

if (TARGET WTF_CopyHeaders)
    add_dependencies(WebGPU WTF_CopyHeaders)
endif ()
if (TARGET bmalloc_CopyHeaders)
    add_dependencies(WebGPU bmalloc_CopyHeaders)
endif ()
add_dependencies(WebGPU wgsl-types)

if (SWIFT_REQUIRED)
    target_sources(WebGPU PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/Buffer.swift
        ${CMAKE_CURRENT_SOURCE_DIR}/CommandEncoder.swift
        ${CMAKE_CURRENT_SOURCE_DIR}/Queue.swift
        ${CMAKE_CURRENT_SOURCE_DIR}/StdLibExtras.swift
    )

    target_compile_options(WebGPU PRIVATE "$<$<COMPILE_LANGUAGE:Swift>:-I${CMAKE_CURRENT_SOURCE_DIR}/Internal>")
    target_compile_options(WebGPU PRIVATE
        "$<$<COMPILE_LANGUAGE:Swift>:SHELL:-enable-experimental-feature SafeInteropWrappers>"
        "$<$<COMPILE_LANGUAGE:Swift>:SHELL:-enable-experimental-feature LifetimeDependence>"
        "$<$<COMPILE_LANGUAGE:Swift>:SHELL:-enable-experimental-feature Lifetimes>"
        "$<$<COMPILE_LANGUAGE:Swift>:SHELL:-enable-experimental-feature ImportNonPublicCxxMembers>"
        "$<$<COMPILE_LANGUAGE:Swift>:-strict-memory-safety>"
        "$<$<COMPILE_LANGUAGE:Swift>:SHELL:-Werror StrictMemorySafety>"
        "$<$<COMPILE_LANGUAGE:Swift>:SHELL:-Werror ForeignReferenceType>"
        "$<$<COMPILE_LANGUAGE:Swift>:SHELL:-library-level spi>"
        "$<$<COMPILE_LANGUAGE:Swift>:-no-verify-emitted-module-interface>"
        "$<$<COMPILE_LANGUAGE:Swift>:SHELL:-Xfrontend -clang-header-expose-decls=has-expose-attr-or-stdlib>"
    )

    if (APPLE)
        # Swift compiles transitively load system frameworks whose PCM builds
        # need our project -Xcc flags. Explicit modules let libSwiftScan
        # pre-build PCMs with the right context.
        set(WebGPU_SWIFT_EXPLICIT_MODULE_BUILD TRUE)
    endif ()

    set(WebGPU_SWIFT_EMIT_CLANG_HEADER_MIN_ACCESS internal)

    WEBKIT_SETUP_SWIFT_AND_GENERATE_SWIFT_CPP_INTEROP_HEADER(WebGPU WebGPU
        "${WebGPU_FRAMEWORK_HEADERS_DIR}/WebGPU"
        "WebGPUSwift-Generated.h")

    WEBKIT_DEFINE_SUBTARGET(WebGPU_SwiftInterop WebGPU ${WebGPU_SWIFT_INTEROP_SOURCES})
    target_precompile_headers(WebGPU_SwiftInterop REUSE_FROM WebGPU)
    add_dependencies(WebGPU_SwiftInterop WTF_CopyHeaders bmalloc_CopyHeaders wgsl-types)
    set(_interop_objs "$<FILTER:$<TARGET_OBJECTS:WebGPU_SwiftInterop>,EXCLUDE,\\.(g|p)ch$>")
    set(_interop_rsp "${CMAKE_CURRENT_BINARY_DIR}/CMakeFiles/WebGPU_SwiftInterop.dir/objects.rsp")
    file(GENERATE OUTPUT "${_interop_rsp}" CONTENT "$<JOIN:${_interop_objs},\n>")
    target_link_options(WebGPU PRIVATE "@${_interop_rsp}")
    set_property(TARGET WebGPU APPEND PROPERTY LINK_DEPENDS "${_interop_objs};${_interop_rsp}")
endif ()

if (WEBKIT_SDK_IS_IOS_FAMILY)
    # Note: WebGPU.emit-module.d / WebGPU.swiftdeps removal and dSYM hoisting
    # is now done in the main POST_BUILD chain BEFORE codesigning, above.
    # See _webgpu_ios_strip_artifacts. Doing it in a separate POST_BUILD here
    # broke the codesign seal.
endif ()
