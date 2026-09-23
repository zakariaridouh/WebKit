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
#error "LLVM_COVERAGE has no iOS-device support: nothing retrieves a profile from a device, and continuous mode maps the profile before the container-temp sandbox extension is consumed. Tools/CodeCoverage/iOSCoverage.md records what a device needs. The simulator is supported by the branch below."
#else
// The directory is the same one macOS uses, and that is deliberate rather than an oversight:
// every iOS-family simulator can write it and the host can read it back. Measured with
// instrumented binaries built against iphonesimulator27.5 and run on an iPhone 17 Pro
// simulator (iOS 27.5):
//
//   * /private/tmp inside a CoreSimulator runtime *is* the host's /private/tmp. An app
//     installed with `simctl install` and launched with `simctl launch`, carrying exactly this
//     baked `<Product>_%8m%c.profraw` pattern, wrote a 49,152-byte .profraw that the host then
//     read at the same path and llvm-cov reported from. So the harness needs no path
//     translation and no `simctl get_app_container` lookup: prepare_coverage_profile_directory()
//     and collect_coverage_profiles() work on a simulator run unchanged.
//   * No sandbox profile is applied to a simulator process. sandbox_check(getpid(), NULL, 0)
//     returned 0 in that app, and WebKit applies none of its own on the iOS family --
//     AuxiliaryProcessMac.mm's sandbox code is PLATFORM(MAC) || PLATFORM(MACCATALYST),
//     GPUProcessIOS.mm's initializeSandbox() is empty, and the one iOS-family path that would
//     apply a compiled profile is behind ENABLE(SIMULATOR_SANDBOX), which is defined nowhere.
//     The nine iOS sandbox profiles carry the matching file-write allowance anyway, so that
//     turning that on later cannot silently discard profiles.
//
// %c survives SIGKILL in the simulator too, which is the only reason it is there: a spawned
// instrumented process was SIGKILLed after executing two of three functions and never running
// an atexit handler, and the recovered profile reported those two covered and the third at 0%.
//
// %8m pools writes into eight per-module files rather than one per process, which
// is what keeps total profile volume bounded across a parallel test run; %c is
// continuous mode, so profiles survive the SIGKILL the test harness uses to stop
// web processes. See Tools/Scripts/generate-coverage-report.
//
// Eight rather than four because the pool is what every instrumented process
// contends for: a %Nm process merges into its slot under a file lock at startup
// and at exit, and a layout run launches tens of thousands of them against a
// handful of slots. The cost is linear in N -- each slot holds a full copy of the
// module's counter, data and name sections, measured at 142 MB per slot across the
// five frameworks (WebCore alone is 111 MB of it), so four slots produced the 633 MB
// a measured full run collected and eight produce about 1.27 GB. Sixteen would be
// 2.3 GB, which is past what Tools/CodeCoverage/Followups.md records as the volume
// this has been measured to sustain. Raise N only with a volume measurement in hand,
// and change it in every site listed there: the pool size is baked into each
// framework separately and they must agree.
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
