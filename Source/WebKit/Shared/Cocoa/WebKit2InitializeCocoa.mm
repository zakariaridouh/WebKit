/*
 * Copyright (C) 2017 Apple Inc. All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY APPLE INC. AND ITS CONTRIBUTORS ``AS IS''
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
 * THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
 * PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL APPLE INC. OR ITS CONTRIBUTORS
 * BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
 * CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
 * SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 * ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF
 * THE POSSIBILITY OF SUCH DAMAGE.
 */

#import "config.h"
#import "WebKit2Initialize.h"

#import <JavaScriptCore/InitializeThreading.h>
#import <WebCore/CommonAtomStrings.h>
#import <WebCore/WebCoreJITOperations.h>
#import <mutex>
#import <wtf/MainThread.h>
#import <wtf/RefCounted.h>
#import <wtf/WorkQueue.h>
#import <wtf/cocoa/RuntimeApplicationChecksCocoa.h>

#if PLATFORM(IOS_FAMILY)
#import <WebCore/WebCoreThreadSystemInterface.h>
#endif

#if ENABLE(LLVM_PROFILE_GENERATION) && ENABLE(LLVM_COVERAGE)
#error "LLVM_PROFILE_GENERATION and LLVM_COVERAGE both define __llvm_profile_filename. Enable only one."
#endif

#if ENABLE(LLVM_PROFILE_GENERATION)
#if PLATFORM(IOS_FAMILY)
#import <wtf/LLVMProfilingUtils.h>
extern "C" char __llvm_profile_filename[] = "%t/WebKitPGO/WebKit_%m_pid%p%c.profraw";
#else
extern "C" char __llvm_profile_filename[] = "/private/tmp/WebKitPGO/WebKit_%m_pid%p%c.profraw";
#endif
#elif ENABLE(LLVM_COVERAGE)
#if PLATFORM(IOS_FAMILY) && !PLATFORM(IOS_FAMILY_SIMULATOR)
#error "LLVM_COVERAGE has no iOS-device support; see Tools/CodeCoverage/iOSCoverage.md. The simulator is supported."
#else
// Simulators share /private/tmp with the host, so they use the macOS directory. %c is continuous mode,
// so profiles survive the SIGKILL used to stop web processes. %8m pools writes into eight files per
// module. The pool size is baked into every framework and must match at each site: here, InitializeThreading.cpp,
// ScriptController.cpp, WebGPU/Instance.mm, WebKitLegacy/WebView.mm, CommonBase.xcconfig and WebKitMacros.cmake.
extern "C" char __llvm_profile_filename[] = "/private/tmp/WebKitCoverage/WebKit_%8m%c.profraw";
#endif
#endif

namespace WebKit {

static std::once_flag flag;

enum class WebKitProfileTag { };

static void runInitializationCode(void* = nullptr)
{
    RELEASE_ASSERT_WITH_MESSAGE([NSThread isMainThread], "InitializeWebKit2 should be called on the main thread");

    WTF::initializeMainThread();
    JSC::initialize();
    WebCore::initializeCommonAtomStrings();
#if PLATFORM(IOS_FAMILY)
    InitWebCoreThreadSystemInterface();
#endif

    WTF::RefCountDebuggerBase::enableThreadingChecksGlobally();

    WebCore::populateJITOperations();
}

void InitializeWebKit2()
{
    // Make sure the initialization code is run only once and on the main thread since things like initializeMainThread()
    // are only safe to call on the main thread.
    std::call_once(flag, [] {
        if ([NSThread isMainThread] || linkedOnOrAfterSDKWithBehavior(SDKAlignedBehavior::InitializeWebKit2MainThreadAssertion))
            runInitializationCode();
        else
            WorkQueue::mainSingleton().dispatchSync([] { runInitializationCode(); });
    });
}

}
