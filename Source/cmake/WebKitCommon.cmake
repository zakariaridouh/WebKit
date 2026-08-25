# -----------------------------------------------------------------------------
# This file is included individually from various subdirectories (JSC, WTF,
# WebCore, WebKit) in order to allow scripts to build only part of WebKit.
# We want to run this file only once.
# -----------------------------------------------------------------------------
if (NOT HAS_RUN_WEBKIT_COMMON)
    set(HAS_RUN_WEBKIT_COMMON TRUE)

    # Preset values are not replayed on auto-reconfigure; if CMake's "compiler
    # changed" path wipes the cache, these silently revert. Stamp them outside
    # the cache and refuse to proceed if any go missing.
    set(WEBKIT_IDENTITY_VARS CMAKE_BUILD_TYPE PORT DEVELOPER_MODE ENABLE_SANITIZERS ENABLE_COVERAGE WEBKIT_SDK_NAME CMAKE_OSX_SYSROOT)
    set(_config_stamp "${CMAKE_BINARY_DIR}/.webkit-config-stamp")
    if (EXISTS "${_config_stamp}")
        file(STRINGS "${_config_stamp}" _stamp_lines)
        foreach (_line IN LISTS _stamp_lines)
            if (_line MATCHES "^([^=]+)=(.*)$")
                set(_var "${CMAKE_MATCH_1}")
                set(_prev "${CMAKE_MATCH_2}")
                if (NOT DEFINED CACHE{${_var}})
                    message(FATAL_ERROR
                        "${_var} is not in the CMake cache, but this build directory was "
                        "previously configured with ${_var}='${_prev}'. The cache was "
                        "probably wiped by an auto-reconfigure (\"You have changed "
                        "variables that require your cache to be deleted\"). Re-run "
                        "'cmake --preset <name>' to restore your configuration, or "
                        "delete the build directory.")
                elseif (NOT "$CACHE{${_var}}" STREQUAL "${_prev}")
                    message(FATAL_ERROR
                        "${_var} changed from '${_prev}' to '$CACHE{${_var}}'. This build "
                        "directory's identity variables must not change after the first "
                        "configure. Delete the build directory and reconfigure.")
                endif ()
            endif ()
        endforeach ()
        unset(_stamp_lines)
    endif ()

    if (NOT CMAKE_BUILD_TYPE)
        message(WARNING "No CMAKE_BUILD_TYPE value specified, defaulting to RelWithDebInfo.")
        set(CMAKE_BUILD_TYPE "RelWithDebInfo" CACHE STRING "Choose the type of build." FORCE)
    else ()
        message(STATUS "The CMake build type is: ${CMAKE_BUILD_TYPE}")
    endif ()

    # Exporting compile commands is available for Ninja and Makefile generators
    # See https://cmake.org/cmake/help/latest/variable/CMAKE_EXPORT_COMPILE_COMMANDS.html
    if (DEVELOPER_MODE AND (CMAKE_GENERATOR MATCHES "Makefile" OR CMAKE_GENERATOR MATCHES "Ninja"))
        set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
    endif ()

    option(ENABLE_JAVASCRIPTCORE "Enable building JavaScriptCore" ON)
    option(ENABLE_WEBCORE "Enable building JavaScriptCore" ON)
    option(ENABLE_WEBKIT "Enable building WebKit" ON)

    if (NOT ENABLE_JAVASCRIPTCORE)
        set(ENABLE_WEBCORE OFF)
    endif ()

    if (NOT ENABLE_WEBCORE)
        set(ENABLE_WEBKIT OFF)
    endif ()

    if (NOT DEFINED ENABLE_TOOLS AND EXISTS "${CMAKE_SOURCE_DIR}/Tools")
        set(ENABLE_TOOLS ON)
    endif ()

    if (NOT DEFINED ENABLE_WEBINSPECTORUI)
        set(ENABLE_WEBINSPECTORUI ON)
    endif ()

    # -----------------------------------------------------------------------------
    # Determine which port will be built
    # -----------------------------------------------------------------------------
    set(ALL_PORTS
        Cocoa
        GTK
        IOS
        JSCOnly
        Mac
        PlayStation
        WPE
        Win
    )
    set(PORT "NOPORT" CACHE STRING "choose which WebKit port to build (one of ${ALL_PORTS})")

    list(FIND ALL_PORTS ${PORT} RET)
    if (${RET} EQUAL -1)
        if (APPLE AND PORT STREQUAL "NOPORT")
            set(PORT "Cocoa" CACHE STRING "choose which WebKit port to build (one of ${ALL_PORTS})" FORCE)
        else ()
            message(FATAL_ERROR "Please choose which WebKit port to build (one of ${ALL_PORTS})")
        endif ()
    endif ()

    # Mac and IOS are aliases for the Cocoa port; the target platform is selected
    # by the SDK (CMAKE_OSX_SYSROOT / WEBKIT_SDK_NAME), not by the port name.
    if (PORT STREQUAL "Mac" OR PORT STREQUAL "IOS")
        set(PORT "Cocoa" CACHE STRING "choose which WebKit port to build (one of ${ALL_PORTS})" FORCE)
    endif ()

    string(TOLOWER ${PORT} WEBKIT_PORT_DIR)

    # -----------------------------------------------------------------------------
    # Check the CMake generator.
    # -----------------------------------------------------------------------------
    # The GTK and WPE ports only support the Ninja generator.
    # Ninja has its own dependency graph, used for dependencies between targets
    if (PORT STREQUAL "GTK" OR PORT STREQUAL "WPE")
        if (NOT CMAKE_GENERATOR MATCHES "Ninja")
            message(FATAL_ERROR "The ${PORT} port requires the Ninja generator, but this build "
                "directory was configured with the \"${CMAKE_GENERATOR}\" generator.\n"
                "Re-run CMake with -GNinja or export CMAKE_GENERATOR=Ninja\n")
        endif ()
    endif ()

    set(_stamp_content "")
    foreach (_var IN LISTS WEBKIT_IDENTITY_VARS)
        if (DEFINED CACHE{${_var}})
            string(APPEND _stamp_content "${_var}=$CACHE{${_var}}\n")
        endif ()
    endforeach ()
    file(WRITE "${_config_stamp}" "${_stamp_content}")
    unset(_stamp_content)
    unset(_config_stamp)

    # -----------------------------------------------------------------------------
    # Determine the compiler
    # -----------------------------------------------------------------------------
    if (${CMAKE_CXX_COMPILER_ID} STREQUAL "Clang" OR ${CMAKE_CXX_COMPILER_ID} STREQUAL "AppleClang")
        set(COMPILER_IS_CLANG ON)
    endif ()

    if (${CMAKE_CXX_COMPILER_ID} STREQUAL "GNU")
        if (${CMAKE_CXX_COMPILER_VERSION} VERSION_LESS "13.1.0")
            message(FATAL_ERROR "GCC 13.1 or newer is required to build WebKit. Use a newer GCC version or Clang.")
        endif ()
    endif ()

    if (${CMAKE_CXX_COMPILER_ID} STREQUAL "QCC")
        set(COMPILER_IS_QCC ON)
        set(COMPILER_IS_GCC_OR_CLANG ON)
    endif ()

    if (CMAKE_COMPILER_IS_GNUCXX OR COMPILER_IS_CLANG)
        set(COMPILER_IS_GCC_OR_CLANG ON)
    endif ()

    if (MSVC AND COMPILER_IS_CLANG)
        set(COMPILER_IS_CLANG_CL ON)
    endif ()

    # -----------------------------------------------------------------------------
    # Determine the target processor
    # -----------------------------------------------------------------------------
    # Use MSVC_CXX_ARCHITECTURE_ID instead of CMAKE_SYSTEM_PROCESSOR when defined,
    # since the later one just resolves to the host processor on Windows.
    if (MSVC_CXX_ARCHITECTURE_ID)
        string(TOLOWER ${MSVC_CXX_ARCHITECTURE_ID} LOWERCASE_CMAKE_SYSTEM_PROCESSOR)
    else ()
        string(TOLOWER ${CMAKE_SYSTEM_PROCESSOR} LOWERCASE_CMAKE_SYSTEM_PROCESSOR)
    endif ()
    if (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "^(arm|aarch32|cortex-(a(5|7|8|9|1[2-7]|32)|m[0-9]|r[0-9]([^0-9]|$)))"
            AND NOT LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm64)")
        set(WTF_CPU_ARM 1)
        set(CMAKE_SYSTEM_PROCESSOR "armv7l" CACHE INTERNAL "" FORCE)
    elseif (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm64|cortex-(a|x|c))")
        if (FORCE_32BIT)
            set(WTF_CPU_ARM 1)
            set(CMAKE_SYSTEM_PROCESSOR "armv7l" CACHE INTERNAL "" FORCE)
        else ()
            set(WTF_CPU_ARM64 1)
        endif ()
    elseif (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "^mips64")
        set(WTF_CPU_MIPS64 1)
    elseif (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "^mips")
        set(WTF_CPU_MIPS 1)
    elseif (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "(x64|x86_64|amd64)")
        # FORCE_32BIT is set in the build script when --32-bit is passed
        # on a Linux/intel 64bit host. This allows us to produce 32bit
        # binaries without setting the build up as a crosscompilation,
        # which is the only way to modify CMAKE_SYSTEM_PROCESSOR.
        if (FORCE_32BIT)
            set(WTF_CPU_X86 1)
        else ()
            set(WTF_CPU_X86_64 1)
        endif ()
    elseif (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "(i[3-6]86|x86)")
        set(WTF_CPU_X86 1)
    elseif (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "(ppc|powerpc)")
        set(WTF_CPU_PPC 1)
    elseif (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "ppc64")
        set(WTF_CPU_PPC64 1)
    elseif (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "ppc64le")
        set(WTF_CPU_PPC64LE 1)
    elseif (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "^riscv64")
        set(WTF_CPU_RISCV64 1)
    elseif (LOWERCASE_CMAKE_SYSTEM_PROCESSOR MATCHES "^loongarch64")
        set(WTF_CPU_LOONGARCH64 1)
    else ()
        set(WTF_CPU_UNKNOWN 1)
    endif ()

    # -----------------------------------------------------------------------------
    # Determine the operating system
    # -----------------------------------------------------------------------------
    if (UNIX)
        if (APPLE)
            set(WTF_OS_MACOS 1)
        elseif (CMAKE_SYSTEM_NAME MATCHES "Linux")
            set(WTF_OS_LINUX 1)
        else ()
            set(WTF_OS_UNIX 1)
        endif ()
    elseif (CMAKE_SYSTEM_NAME MATCHES "Windows")
        set(WTF_OS_WINDOWS 1)
    elseif (CMAKE_SYSTEM_NAME MATCHES "Fuchsia")
        set(WTF_OS_FUCHSIA 1)
    else ()
        message(FATAL_ERROR "Unknown OS '${CMAKE_SYSTEM_NAME}'")
    endif ()

    # -----------------------------------------------------------------------------
    # Default library types
    # -----------------------------------------------------------------------------
    # By default, only the highest-level libraries, WebKitLegacy and WebKit, are
    # shared, because properly building shared libraries that depend on each other
    # can be tricky. Override these in Options*.cmake for your port as needed.
    set(bmalloc_LIBRARY_TYPE STATIC)
    set(WTF_LIBRARY_TYPE STATIC)
    set(JavaScriptCore_LIBRARY_TYPE STATIC)
    set(PAL_LIBRARY_TYPE STATIC)
    set(WebCore_LIBRARY_TYPE STATIC)
    set(WebKitLegacy_LIBRARY_TYPE SHARED)
    set(WebKit_LIBRARY_TYPE SHARED)
    set(WebCoreTestSupport_LIBRARY_TYPE STATIC)

    set(CMAKE_POSITION_INDEPENDENT_CODE True)

    # -----------------------------------------------------------------------------
    # Install JavaScript shell
    # -----------------------------------------------------------------------------
    option(SHOULD_INSTALL_JS_SHELL "generate an installation rule to install the built JavaScript shell")

    # -----------------------------------------------------------------------------
    # Default output directories, which can be overwritten by ports
    #------------------------------------------------------------------------------
    set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

    # -----------------------------------------------------------------------------
    # Find common packages (used by all ports)
    # -----------------------------------------------------------------------------
    if (WIN32)
        list(APPEND CMAKE_PROGRAM_PATH $ENV{SystemDrive}/cygwin/bin)
    endif ()

    # TODO Enforce version requirement for perl
    find_package(Perl 5.10.0 REQUIRED)
    find_package(PerlModules COMPONENTS English FindBin JSON::PP REQUIRED)

    # This module looks preferably for version 3 of Python. If not found, version 2 is searched.
    find_package(Python COMPONENTS Interpreter REQUIRED)
    # Set the variable with uppercase name to keep compatibility with code and users expecting it.
    set(PYTHON_EXECUTABLE ${Python_EXECUTABLE} CACHE FILEPATH "Path to the Python interpreter")

    # We only need the Ruby interpreter (to run .rb generators), not the dev
    # package, so find the executable and query its version directly instead of
    # paying find_package(Ruby)'s config probing (a dozen ruby subprocesses).
    message(CHECK_START "Ruby interpreter executable")
    # Honor a pinned interpreter passed as -DRUBY_EXECUTABLE=... (the legacy name
    # find_package(Ruby) accepted; some bots use it to avoid a broken PATH ruby).
    if (RUBY_EXECUTABLE AND NOT Ruby_EXECUTABLE)
        set(Ruby_EXECUTABLE "${RUBY_EXECUTABLE}" CACHE FILEPATH "Path to the Ruby interpreter")
    endif ()
    find_program(Ruby_EXECUTABLE NAMES ruby)
    if (Ruby_EXECUTABLE)
        execute_process(
            COMMAND "${Ruby_EXECUTABLE}" -e "print RUBY_VERSION"
            OUTPUT_VARIABLE Ruby_VERSION
            OUTPUT_STRIP_TRAILING_WHITESPACE
            RESULT_VARIABLE _ruby_version_result)
        if (NOT _ruby_version_result EQUAL 0)
            set(Ruby_VERSION "")
        endif ()
        unset(_ruby_version_result)
    endif ()
    if (Ruby_EXECUTABLE AND Ruby_VERSION)
        if (Ruby_VERSION VERSION_LESS 2.5)
            message(CHECK_FAIL "${Ruby_EXECUTABLE} (version: ${Ruby_VERSION}, minimum required 2.5)")
            set(Ruby_EXECUTABLE NOTFOUND)
        else ()
            message(CHECK_PASS "${Ruby_EXECUTABLE} (version: ${Ruby_VERSION})")
        endif ()
    else ()
        message(CHECK_FAIL "not found")
    endif ()
    if (NOT Ruby_EXECUTABLE)
        message(FATAL_ERROR "Ruby 2.5 or higher is required.")
    endif ()

    # -----------------------------------------------------------------------------
    # Helper macros and feature defines
    # -----------------------------------------------------------------------------

    # To prevent multiple inclusion, most modules should be included once here.
    include(CheckCCompilerFlag)
    include(CheckCSourceCompiles)
    include(CheckCXXCompilerFlag)
    include(CheckCXXSourceCompiles)
    include(CheckFunctionExists)
    include(CheckIncludeFile)
    include(CheckSymbolExists)
    include(CheckStructHasMember)
    include(CheckTypeSize)
    include(CMakeDependentOption)
    include(CMakeParseArguments)
    include(CMakePushCheckState)
    include(ProcessorCount)

    include(WebKitPackaging)
    include(WebKitHeaderMap)
    include(WebKitMacros)
    include(WebKitSwiftPrewarm)
    include(WebKitFS)
    include(WebKitCCache)
    include(WebKitCompilerFlags)
    include(WebKitStaticAnalysis)
    include(WebKitFeatures)
    include(WebKitEntitlements)

    if (USE_APPLE_INTERNAL_SDK)
        list(APPEND CMAKE_MODULE_PATH "${CMAKE_SOURCE_DIR}/../Internal/WebKit/WebKitAdditions/CMake")
        include(WebKitAdditions)
    endif ()

    include(OptionsCommon)
    include(Options${PORT})

    # This has to come after Options${PORT} to see any ENABLE_THREAD_SAFETY_WARNING.
    if (ENABLE_THREAD_SAFETY_WARNING)
        WEBKIT_PREPEND_GLOBAL_CXX_FLAGS(-Wthread-safety)
    endif ()

    # Check gperf after including OptionsXXX.cmake since gperf is required only when ENABLE_WEBCORE is true,
    # and ENABLE_WEBCORE is configured in OptionsXXX.cmake.
    if (ENABLE_WEBCORE)
        # TODO Enforce version requirement for gperf
        find_package(Gperf 3.0.1 REQUIRED)
    endif ()

    # -------------------------------------------------------------------------
    # PGO (Profile-Guided Optimization) support
    # -------------------------------------------------------------------------
    if (ENABLE_LLVM_PROFILE_GENERATION AND NOT COMPILER_IS_CLANG)
        message(FATAL_ERROR "ENABLE_LLVM_PROFILE_GENERATION requires Clang.")
    endif ()

    if (USE_PGO_PROFILE AND NOT COMPILER_IS_CLANG)
        message(FATAL_ERROR "USE_PGO_PROFILE requires Clang.")
    endif ()

    # Phase 1: Profile Generation - build instrumented binary that writes .profraw files
    if (ENABLE_LLVM_PROFILE_GENERATION AND COMPILER_IS_CLANG AND NOT MSVC)
        include(CheckCXXSourceCompiles)
        cmake_push_check_state()
        set(CMAKE_REQUIRED_FLAGS "-fprofile-generate")
        set(CMAKE_REQUIRED_LINK_OPTIONS "-fprofile-generate")
        check_cxx_source_compiles("int main() { return 0; }" HAVE_CLANG_PROFILE_RUNTIME)
        cmake_pop_check_state()

        if (NOT HAVE_CLANG_PROFILE_RUNTIME)
            message(FATAL_ERROR
                "ENABLE_LLVM_PROFILE_GENERATION requires the Clang profile runtime (libclang_rt.profile).\n"
                "Install it or disable PGO with: -DENABLE_LLVM_PROFILE_GENERATION=OFF")
        endif ()

        set(PGO_PROFILE_DIR "" CACHE PATH "Runtime directory for PGO profile output. Leave empty for clang default.")

        if (PGO_PROFILE_DIR)
            set(PGO_COMPILE_OPTIONS "-fprofile-generate=${PGO_PROFILE_DIR}" CACHE INTERNAL "")
        else ()
            set(PGO_COMPILE_OPTIONS "-fprofile-generate" CACHE INTERNAL "")
        endif ()

        # LTO builds error out on duplicate __llvm_profile_filename definitions.
        set(PGO_LINK_FLAGS "${PGO_COMPILE_OPTIONS}")
        if (LD_SUPPORTS_ALLOW_MULTIPLE_DEFINITION)
            add_link_options("LINKER:--allow-multiple-definition")
        endif ()
        string(PREPEND CMAKE_EXE_LINKER_FLAGS "${PGO_LINK_FLAGS} ")
        string(PREPEND CMAKE_SHARED_LINKER_FLAGS "${PGO_LINK_FLAGS} ")
        string(PREPEND CMAKE_MODULE_LINKER_FLAGS "${PGO_LINK_FLAGS} ")

        if (PGO_PROFILE_DIR)
            message(STATUS "PGO profile generation enabled. Profile output: ${PGO_PROFILE_DIR}")
        else ()
            message(STATUS "PGO profile generation enabled. Using clang default profile output.")
        endif ()
        message(STATUS "  Override at runtime with: LLVM_PROFILE_FILE=/your/path/%p_%m.profraw")
    endif ()

    # Phase 2: Profile Use - build optimized binary using collected profile data.
    # This is the generic single-profile path for most ports. On Apple internal SDK
    # builds, the WebKitAdditions overlay applies per-framework profiles instead,
    # so skip there.
    if (USE_PGO_PROFILE AND COMPILER_IS_CLANG AND NOT MSVC AND NOT USE_APPLE_INTERNAL_SDK)
        set(PGO_PROFILE_PATH "" CACHE FILEPATH "Path to merged .profdata file for PGO")
        if (NOT PGO_PROFILE_PATH)
            message(FATAL_ERROR "USE_PGO_PROFILE is ON but PGO_PROFILE_PATH is not set")
        endif ()
        if (NOT EXISTS "${PGO_PROFILE_PATH}")
            message(FATAL_ERROR "PGO_PROFILE_PATH does not exist: ${PGO_PROFILE_PATH}")
        endif ()

        # profile counter mismatches (e.g., source changed since profiling) trigger backend-plugin warnings,
        # which would break the build with our usage of -Werror in CI
        WEBKIT_PREPEND_GLOBAL_COMPILER_FLAGS("-fprofile-use=${PGO_PROFILE_PATH}" "-Wno-error=backend-plugin")
        string(PREPEND CMAKE_EXE_LINKER_FLAGS "-fprofile-use=${PGO_PROFILE_PATH} ")
        string(PREPEND CMAKE_SHARED_LINKER_FLAGS "-fprofile-use=${PGO_PROFILE_PATH} ")
        string(PREPEND CMAKE_MODULE_LINKER_FLAGS "-fprofile-use=${PGO_PROFILE_PATH} ")

        message(STATUS "PGO profile use enabled with: ${PGO_PROFILE_PATH}")
    endif ()

    # -------------------------------------------------------------------------
    # LLVM source-based code coverage support
    # -------------------------------------------------------------------------
    # Two things about the shape of this block are deliberate.
    #
    # The flags are global (CMAKE_<LANG>_FLAGS) rather than per-target the way the PGO
    # flags above are. Precompiled header reuse -- target_precompile_headers(REUSE_FROM),
    # see WEBKIT_REUSE_PREFIX_HEADER in WebKitMacros.cmake -- requires the producing and
    # consuming targets to compile with exactly equal flags, so instrumenting only the
    # library targets would invalidate every shared PCH and give up the reuse.
    #
    # Source/ThirdParty is therefore kept out of the instrumentation by source path with
    # -fprofile-list rather than by target. That is strictly better than a per-target
    # exclusion anyway: it also drops third-party inline functions that reach a WebKit
    # translation unit through a header, and it works inside unified-source bundles.
    if (ENABLE_COVERAGE AND NOT COMPILER_IS_CLANG)
        message(FATAL_ERROR "ENABLE_COVERAGE requires Clang.")
    endif ()

    # Refuse rather than no-op: an instrumented-looking build that produced no profiles
    # is the exact failure this support exists to stop happening.
    if (ENABLE_COVERAGE AND MSVC)
        message(FATAL_ERROR
            "ENABLE_COVERAGE is not implemented for the MSVC-compatible clang-cl driver, "
            "which spells these flags differently and has no libclang_rt.profile here.")
    endif ()

    if (ENABLE_COVERAGE AND ENABLE_SANITIZERS)
        message(FATAL_ERROR
            "ENABLE_COVERAGE cannot be combined with ENABLE_SANITIZERS=${ENABLE_SANITIZERS}. "
            "Configure a separate build directory for each.")
    endif ()

    # Coverage under LTO is deliberately not refused. A refusal was added here and then
    # measured away: it assumed ENABLE_LLVM_COVERAGE's per-framework __llvm_profile_filename
    # definitions collide under LTO, and on this port they cannot, because each image has
    # exactly one -- checked across all five frameworks, with none of libWTF.a, libbmalloc.a,
    # libPAL.a or libWGSLCore.a contributing another. A duplicate needs two strong definitions
    # in one link and fails the same way with or without LTO. Measured: ENABLE_COVERAGE=ON with
    # LTO_MODE=thin links JavaScriptCore.framework in 2:36, no duplicate symbols, 33,120
    # __profc_ symbols, baked profile path intact.

    if (ENABLE_COVERAGE)
        # CMAKE_CURRENT_LIST_DIR, not CMAKE_SOURCE_DIR: this file is also included
        # directly from Source/JavaScriptCore and friends for partial builds, where the
        # top-level source directory is not the checkout root.
        set(COVERAGE_EXCLUSIONS_FILE "${CMAKE_CURRENT_LIST_DIR}/CoverageExclusions.txt"
            CACHE FILEPATH "Clang -fprofile-list file naming the sources to leave uninstrumented")
        if (NOT EXISTS "${COVERAGE_EXCLUSIONS_FILE}")
            message(FATAL_ERROR "COVERAGE_EXCLUSIONS_FILE does not exist: ${COVERAGE_EXCLUSIONS_FILE}")
        endif ()

        # Deliberately the bare -fprofile-instr-generate, with no =<path>. A path here
        # would appear in every compile command and so in the ccache hash, for no
        # benefit: where the profiles land is a run-time decision (LLVM_PROFILE_FILE, or
        # the baked-in default that ENABLE_LLVM_COVERAGE provides).
        set(COVERAGE_COMPILE_FLAGS "-fprofile-instr-generate -fcoverage-mapping")

        cmake_push_check_state()
        set(CMAKE_REQUIRED_FLAGS "${COVERAGE_COMPILE_FLAGS}")
        set(CMAKE_REQUIRED_LINK_OPTIONS "-fprofile-instr-generate")
        check_cxx_source_compiles("int main() { return 0; }" HAVE_CLANG_COVERAGE_RUNTIME)
        cmake_pop_check_state()

        if (NOT HAVE_CLANG_COVERAGE_RUNTIME)
            message(FATAL_ERROR
                "ENABLE_COVERAGE requires the Clang profile runtime (libclang_rt.profile).\n"
                "Install it or disable coverage with: -DENABLE_COVERAGE=OFF")
        endif ()

        # Probe -fprofile-list separately, and by compiling with the real exclusions
        # file: Clang treats a malformed special-case list as a hard error, so a
        # successful compile checks both that the flag exists and that the file parses.
        # (An unrecognised prefix inside the file is silently ignored, so this cannot
        # catch a typo in "source:" -- that fails open, instrumenting more, not less.)
        cmake_push_check_state()
        set(CMAKE_REQUIRED_FLAGS "${COVERAGE_COMPILE_FLAGS} -fprofile-list=${COVERAGE_EXCLUSIONS_FILE}")
        set(CMAKE_REQUIRED_LINK_OPTIONS "-fprofile-instr-generate")
        check_cxx_source_compiles("int main() { return 0; }" HAVE_CLANG_PROFILE_LIST)
        cmake_pop_check_state()

        if (HAVE_CLANG_PROFILE_LIST)
            string(APPEND COVERAGE_COMPILE_FLAGS " -fprofile-list=${COVERAGE_EXCLUSIONS_FILE}")
        else ()
            message(WARNING
                "This Clang does not accept -fprofile-list=${COVERAGE_EXCLUSIONS_FILE}, so "
                "Source/ThirdParty will be instrumented as well. Expect a much larger build "
                "and much slower tests.")
        endif ()

        set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} ${COVERAGE_COMPILE_FLAGS}")
        set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${COVERAGE_COMPILE_FLAGS}")
        # Apple ports also enable OBJC/OBJCXX, so .m/.mm sources need the same flags.
        # On ports where those languages are not enabled, setting these is harmless.
        set(CMAKE_OBJC_FLAGS "${CMAKE_OBJC_FLAGS} ${COVERAGE_COMPILE_FLAGS}")
        set(CMAKE_OBJCXX_FLAGS "${CMAKE_OBJCXX_FLAGS} ${COVERAGE_COMPILE_FLAGS}")

        # -fprofile-instr-generate at link time is what pulls in libclang_rt.profile.
        set(COVERAGE_LINK_FLAGS "-fprofile-instr-generate")

        # Continuous mode -- the "%c" in the baked profile path -- mmaps the counters, bitmap
        # and data sections, so the profiling runtime requires each of them to start on a page
        # boundary and refuses at process start otherwise. It checks them in that order, so
        # they surfaced one at a time in a WebKitTestRunner run against this build:
        #
        #   LLVM Profile Error: Counters section not page-aligned (start = 0x..., pagesz = 16384)
        #   LLVM Profile Error: Bitmap section not page-aligned   (start = 0x..., pagesz = 16384)
        #   LLVM Profile Error: Data section not page-aligned     (start = 0x..., pagesz = 16384)
        #
        # Twice each, from WebKit.framework and WebGPU.framework -- the two Swift-linked
        # frameworks, whose whole __llvm_prf_* group lands off a page boundary here
        # (WebKit's data section at 0x21230d0, WebGPU's at 0x30d310) while the clang-linked
        # frameworks' happen to be aligned. The Xcode build's are all aligned, which is why it
        # has never needed more than the counters rename; that is section-layout luck rather
        # than a guarantee.
        #
        # The counters section is renamed as well as aligned, matching the Xcode build
        # (CommonBase.xcconfig, WK_COMMON_OTHER_LDFLAGS_INSTRUMENTATION_YES, for
        # rdar://151083138), which moves the section the runtime mmaps into its own segment.
        # The other two are aligned in place: renaming them to a shared segment made things
        # worse, because the bitmap then followed the non-empty counters and lost its
        # alignment again.
        #
        # 4000 is hex, so 16 KB -- the arm64 page size. Mach-O only.
        set(_coverage_macho_section_flags "")
        if (APPLE)
            list(APPEND _coverage_macho_section_flags
                "-Wl,-rename_section,__DATA,__llvm_prf_cnts,__MMAP_DATA,__llvm_prf_cnts"
                "-Wl,-sectalign,__MMAP_DATA,__llvm_prf_cnts,4000"
                "-Wl,-sectalign,__DATA,__llvm_prf_bits,4000"
                "-Wl,-sectalign,__DATA,__llvm_prf_data,4000")
            foreach (_flag IN LISTS _coverage_macho_section_flags)
                string(APPEND COVERAGE_LINK_FLAGS " ${_flag}")
            endforeach ()
            # CMake omits CMAKE_EXE_LINKER_FLAGS from Swift executable link lines, so the
            # TestWebKitAPI binaries need these named for their link language specifically --
            # the same reason the profile runtime is named that way below. They only started
            # needing them once WEBKIT_EXECUTABLE began baking a profile path into every
            # executable, because that is what selects continuous mode: before, a Swift-linked
            # test binary wrote a non-continuous default.profraw and no alignment was
            # required. Measured on an API-test run without this: "Counters section not
            # page-aligned (start = 0x105482988)".
            #
            # Spelled -Xlinker for Swift rather than reusing the -Wl, forms above, because
            # swiftc rejects -Wl, outright with "error: unknown argument" -- which is how this
            # was found: TestWTF became a Swift-linked executable and its link failed on the
            # rename.
            #
            # SHELL: is what makes it survive. Without it add_link_options() treats each token
            # as an option and de-duplicates them, so four groups that repeat -Xlinker,
            # -sectalign, 4000 and __llvm_prf_cnts collapse into
            # "-Xlinker -rename_section __DATA __llvm_prf_cnts __MMAP_DATA -sectalign 4000
            # __llvm_prf_bits __llvm_prf_data" -- every repeat dropped, and ld handed arguments
            # belonging to a different flag. The -Wl, forms above are immune only because each
            # is a single token with commas inside it, which is also why this went unnoticed
            # while the wrapper was translating them.
            add_link_options(
                "$<$<LINK_LANGUAGE:Swift>:SHELL:-Xlinker -rename_section -Xlinker __DATA -Xlinker __llvm_prf_cnts -Xlinker __MMAP_DATA -Xlinker __llvm_prf_cnts>"
                "$<$<LINK_LANGUAGE:Swift>:SHELL:-Xlinker -sectalign -Xlinker __MMAP_DATA -Xlinker __llvm_prf_cnts -Xlinker 4000>"
                "$<$<LINK_LANGUAGE:Swift>:SHELL:-Xlinker -sectalign -Xlinker __DATA -Xlinker __llvm_prf_bits -Xlinker 4000>"
                "$<$<LINK_LANGUAGE:Swift>:SHELL:-Xlinker -sectalign -Xlinker __DATA -Xlinker __llvm_prf_data -Xlinker 4000>")
            unset(_flag)
        endif ()
        unset(_coverage_macho_section_flags)

        # ENABLE_LLVM_COVERAGE defines __llvm_profile_filename in one translation unit
        # per framework. Ports that link JavaScriptCore and WebCore into a single image
        # (WPE builds both as OBJECT libraries) then get two definitions of it. The
        # values are identical, so demote the clash the way the PGO path above does.
        if (ENABLE_LLVM_COVERAGE AND LD_SUPPORTS_ALLOW_MULTIPLE_DEFINITION)
            string(PREPEND COVERAGE_LINK_FLAGS "-Wl,--allow-multiple-definition ")
        endif ()

        string(PREPEND CMAKE_EXE_LINKER_FLAGS "${COVERAGE_LINK_FLAGS} ")
        string(PREPEND CMAKE_SHARED_LINKER_FLAGS "${COVERAGE_LINK_FLAGS} ")
        string(PREPEND CMAKE_MODULE_LINKER_FLAGS "${COVERAGE_LINK_FLAGS} ")

        # Swift-linked targets -- the TestWebKitAPI binaries -- are linked by swiftc, not
        # by the C/C++ driver, so none of CMAKE_<TYPE>_LINKER_FLAGS above reaches them and
        # nothing pulls in the profile runtime that their instrumented C++ objects
        # reference. Name the runtime archive for those link lines specifically. Asking
        # the compiler for it (rather than -fprofile-instr-generate, which swiftc would
        # reject) keeps this to a link input and leaves Swift codegen alone; Swift sources
        # themselves stay uninstrumented, which would take -profile-generate.
        set(_coverage_profile_runtime "")
        foreach (_candidate IN ITEMS
                 libclang_rt.profile_osx.a
                 libclang_rt.profile-${CMAKE_SYSTEM_PROCESSOR}.a
                 libclang_rt.profile.a)
            execute_process(COMMAND ${CMAKE_CXX_COMPILER} -print-file-name=${_candidate}
                OUTPUT_VARIABLE _candidate_path OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
            if (_candidate_path AND EXISTS "${_candidate_path}")
                set(_coverage_profile_runtime "${_candidate_path}")
                break ()
            endif ()
        endforeach ()

        if (_coverage_profile_runtime)
            add_link_options("$<$<LINK_LANGUAGE:Swift>:-Xlinker>"
                             "$<$<LINK_LANGUAGE:Swift>:${_coverage_profile_runtime}>")
        else ()
            message(WARNING
                "Could not locate libclang_rt.profile for ${CMAKE_CXX_COMPILER}. Targets "
                "linked by swiftc will fail with undefined __llvm_profile_* symbols.")
        endif ()
        unset(_candidate)
        unset(_candidate_path)
        unset(_coverage_profile_runtime)

        # Reconfigure when the exclusions change, so an edited list can't be quietly
        # ignored. This does not rebuild objects on its own, and ccache cannot help either
        # way: it refuses to cache any compile carrying -fprofile-list at all. Measured with
        # ccache 4.13.6, which logs "Unknown profiling option: -fprofile-list=..." and
        # "Result: unsupported_compiler_option", then execs the compiler -- 100% uncacheable
        # calls, zero hits, zero misses. So every instrumented compile is a real compile, and
        # there is no cache key for the exclusions file to participate in.
        set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${COVERAGE_EXCLUSIONS_FILE}")

        message(STATUS "LLVM source-based code coverage enabled.")
        message(STATUS "  Uninstrumented sources: ${COVERAGE_EXCLUSIONS_FILE}")
        if (ENABLE_LLVM_COVERAGE)
            message(STATUS "  Profile output: /private/tmp/WebKitCoverage/<Framework>_%4m%c.profraw")
        else ()
            message(STATUS "  Profile output: clang's default.profraw")
        endif ()
        message(STATUS "  Override at runtime with: LLVM_PROFILE_FILE=/your/path/%p_%m.profraw")
        message(STATUS "  Turn the collected profiles into a report with Tools/Scripts/generate-coverage-report")
    endif ()

    # -----------------------------------------------------------------------------
    # Generate a usable compile_commands.json when using unified builds
    # -----------------------------------------------------------------------------
    # Ideally this would be done during configure or generation, but CMake doesn't
    # produce compile_commands.json until generation has ended. Having a build
    # target that processes it is the next best thing we can do.
    if (CMAKE_EXPORT_COMPILE_COMMANDS AND ENABLE_UNIFIED_BUILDS)
        # compile_commands.json
        add_custom_command(
            OUTPUT  ${CMAKE_BINARY_DIR}/DeveloperTools/compile_commands.json
            DEPENDS ${CMAKE_SOURCE_DIR}/Tools/Scripts/rewrite-compile-commands
                    ${CMAKE_BINARY_DIR}/compile_commands.json
            COMMAND ${Python_EXECUTABLE}
                    ${CMAKE_SOURCE_DIR}/Tools/Scripts/rewrite-compile-commands
                    ${CMAKE_BINARY_DIR}/compile_commands.json
                    ${CMAKE_BINARY_DIR}/DeveloperTools/compile_commands.json
                    ${CMAKE_SOURCE_DIR}
                    ${CMAKE_BINARY_DIR}
            VERBATIM
        )
        add_custom_target(RewriteCompileCommands
            ALL
            DEPENDS ${CMAKE_BINARY_DIR}/DeveloperTools/compile_commands.json
        )
    elseif (CMAKE_EXPORT_COMPILE_COMMANDS)
        # Create a simple symlink in DeveloperTools/compile_commands.json so that
        # it can be relied upon regardless of unified or non-unified builds.
        file(MAKE_DIRECTORY ${CMAKE_BINARY_DIR}/DeveloperTools)
        set(COMPILE_COMMANDS_PATH "../compile_commands.json")
        cmake_path(NATIVE_PATH COMPILE_COMMANDS_PATH COMPILE_COMMANDS_PATH)
        file(CREATE_LINK
            ${COMPILE_COMMANDS_PATH}
            ${CMAKE_BINARY_DIR}/DeveloperTools/compile_commands.json
            SYMBOLIC
        )
    endif ()

    # CLANGD_AUTO_SETUP defaults to ON if ENABLE_DEVELOPER_MODE or if building inside <SOURCE>/WebKitBuild
    set(webkitbuild_dir ${CMAKE_SOURCE_DIR}/WebKitBuild)
    cmake_path(IS_PREFIX webkitbuild_dir ${CMAKE_BINARY_DIR} NORMALIZE building_inside_WebKitBuild)
    if (building_inside_WebKitBuild OR ENABLE_DEVELOPER_MODE)
        set(clangd_auto_setup_default ON)
    else ()
        set(clangd_auto_setup_default OFF)
    endif ()
    cmake_dependent_option(CLANGD_AUTO_SETUP
        "Install a .clangd configuration file and a compile_commands.json symlink
         in the root of the source tree to have out-of-the-box code completion
         in editors."
        ${clangd_auto_setup_default}
        CMAKE_EXPORT_COMPILE_COMMANDS
        OFF
    )

    if (CLANGD_AUTO_SETUP)
        # update-compile-commands-symlink.conf
        add_custom_command(
            OUTPUT ${CMAKE_SOURCE_DIR}/update-compile-commands-symlink.conf
            DEPENDS ${TOOLS_DIR}/clangd/update-compile-commands-symlink-conf
                    ${TOOLS_DIR}/clangd/update-compile-commands-symlink.conf.example
            COMMAND ${Python_EXECUTABLE}
                    ${TOOLS_DIR}/clangd/update-compile-commands-symlink-conf
                    ${TOOLS_DIR}/clangd/update-compile-commands-symlink.conf.example
                    ${CMAKE_SOURCE_DIR}/update-compile-commands-symlink.conf
            VERBATIM
        )
        # compile_commands.json
        set(_compile_commands_symlink_stamp ${CMAKE_BINARY_DIR}/DeveloperTools/compile_commands_symlink.stamp)
        add_custom_command(
            OUTPUT ${_compile_commands_symlink_stamp}
            DEPENDS ${CMAKE_BINARY_DIR}/DeveloperTools/compile_commands.json
                    ${CMAKE_SOURCE_DIR}/update-compile-commands-symlink.conf
            COMMAND ${Python_EXECUTABLE}
                    ${TOOLS_DIR}/clangd/update-compile-commands-symlink
                    ${CMAKE_SOURCE_DIR}/compile_commands.json
                    ${CMAKE_SOURCE_DIR}/update-compile-commands-symlink.conf
            COMMAND ${CMAKE_COMMAND} -E touch ${_compile_commands_symlink_stamp}
            VERBATIM
        )
        add_custom_target(UpdateCompileCommandsSymlink ALL
            DEPENDS ${_compile_commands_symlink_stamp}
        )
        # .clangd
        add_custom_command(
            OUTPUT ${CMAKE_SOURCE_DIR}/.clangd
            DEPENDS ${TOOLS_DIR}/clangd/update-clangd-config
                    ${TOOLS_DIR}/clangd/clangd-config.yaml.tpl
            COMMAND ${Python_EXECUTABLE}
                    ${TOOLS_DIR}/clangd/update-clangd-config
                    ${TOOLS_DIR}/clangd/clangd-config.yaml.tpl
                    ${CMAKE_SOURCE_DIR}/.clangd
            VERBATIM
        )
        add_custom_target(UpdateClangdConf
            ALL
            DEPENDS ${CMAKE_SOURCE_DIR}/.clangd
        )
    endif ()

    # -----------------------------------------------------------------------------
    # Job pool to avoid running too many memory hungry processes
    # -----------------------------------------------------------------------------
    if (DEFINED ENV{WEBKIT_NINJA_LINK_MAX})
        list(APPEND WK_POOLS "link_pool_jobs=$ENV{WEBKIT_NINJA_LINK_MAX}")
    elseif (${CMAKE_BUILD_TYPE} STREQUAL "Release" OR ${CMAKE_BUILD_TYPE} STREQUAL "MinSizeRel")
        list(APPEND WK_POOLS link_pool_jobs=4)
    else ()
        list(APPEND WK_POOLS link_pool_jobs=2)
    endif ()
    set(CMAKE_JOB_POOL_LINK link_pool_jobs)
    if (DEFINED ENV{WEBKIT_NINJA_COMPILE_MAX})
        list(APPEND WK_POOLS "compile_pool_jobs=$ENV{WEBKIT_NINJA_COMPILE_MAX}")
        set(CMAKE_JOB_POOL_COMPILE compile_pool_jobs)
    endif ()
    set_property(GLOBAL PROPERTY JOB_POOLS ${WK_POOLS})

    # -----------------------------------------------------------------------------
    # Create derived sources directories
    # -----------------------------------------------------------------------------

    file(MAKE_DIRECTORY ${WTF_DERIVED_SOURCES_DIR})
    file(MAKE_DIRECTORY ${JavaScriptCore_DERIVED_SOURCES_DIR})

    if (ENABLE_WEBCORE)
        file(MAKE_DIRECTORY ${PAL_DERIVED_SOURCES_DIR})
        file(MAKE_DIRECTORY ${WebCore_DERIVED_SOURCES_DIR})
    endif ()

    if (ENABLE_WEBKIT)
        file(MAKE_DIRECTORY ${WebKit_DERIVED_SOURCES_DIR})
    endif ()

    if (ENABLE_WEBKIT_LEGACY)
        file(MAKE_DIRECTORY ${WebKitLegacy_DERIVED_SOURCES_DIR})
    endif ()

    if (ENABLE_WEBDRIVER)
        file(MAKE_DIRECTORY ${WebDriver_DERIVED_SOURCES_DIR})
    endif ()

    # -----------------------------------------------------------------------------
    # config.h
    # -----------------------------------------------------------------------------
    CREATE_CONFIGURATION_HEADER()
endif ()
