/*
 * Copyright (C) 2025 Apple Inc. All rights reserved.
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
 * THIS SOFTWARE IS PROVIDED BY APPLE INC. ``AS IS'' AND ANY
 * EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
 * PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL APPLE INC. OR
 * CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
 * PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
 * PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
 * OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "config.h"
#include "WasmBreakpointManager.h"

#if ENABLE(WEBASSEMBLY_DEBUGGER)

WTF_ALLOW_UNSAFE_BUFFER_USAGE_BEGIN

#include "Options.h"
#include <wtf/DataLog.h>
#include <wtf/TZoneMallocInlines.h>

namespace JSC {
namespace Wasm {

WTF_MAKE_TZONE_ALLOCATED_IMPL(BreakpointManager);

BreakpointManager::~BreakpointManager()
{
    clearAllBreakpoints();
}

bool BreakpointManager::hasOneTimeBreakpoints()
{
    Locker locker { m_lock };
    return !m_oneTimeBreakpoints.isEmpty();
}

Breakpoint& BreakpointManager::ensurePatched(const ModuleInformation& owner, uint8_t* pc)
{
    RELEASE_ASSERT(pc);
    if (auto it = m_breakpoints.find(pc); it != m_breakpoints.end()) {
        dataLogLnIf(Options::verboseWasmDebugger(), "[BreakpointManager] Reusing the patch at ", RawPointer(pc));
        return it->value;
    }

    Ref<Breakpoint> breakpoint = Breakpoint::create(owner, pc);
    breakpoint->patchBreakpoint();
    dataLogLnIf(Options::verboseWasmDebugger(), "[BreakpointManager] Patched ", breakpoint);
    return m_breakpoints.set(pc, WTF::move(breakpoint)).iterator->value;
}

void BreakpointManager::releasePatchIfUnused(uint8_t* pc)
{
    auto it = m_breakpoints.find(pc);
    RELEASE_ASSERT(it != m_breakpoints.end());
    if (it->value->hasSite || m_oneTimeBreakpoints.contains(pc))
        return;

    dataLogLnIf(Options::verboseWasmDebugger(), "[BreakpointManager] Restoring ", it->value);
    it->value->restorePatch();
    m_breakpoints.remove(it);
}

void BreakpointManager::setStepBreakpoint(const ModuleInformation& owner, uint8_t* pc)
{
    Locker locker { m_lock };
    ensurePatched(owner, pc);
    m_oneTimeBreakpoints.add(pc);
}

void BreakpointManager::setBreakpointAt(VirtualAddress address, const ModuleInformation& owner, uint8_t* pc)
{
    Locker locker { m_lock };
    // Re-arming an existing site is a no-op.
    auto result = m_addressToPC.add(address, pc);
    if (!result.isNewEntry) {
        RELEASE_ASSERT(result.iterator->value == pc);
        return;
    }
    ensurePatched(owner, pc).hasSite = true;
}

bool BreakpointManager::removeBreakpointAt(VirtualAddress address)
{
    Locker locker { m_lock };
    uint8_t* pc = m_addressToPC.take(address);
    if (!pc)
        return false;

    auto it = m_breakpoints.find(pc);
    RELEASE_ASSERT(it != m_breakpoints.end());
    it->value->hasSite = false;
    releasePatchIfUnused(pc);
    return true;
}

std::optional<BreakpointManager::TrapAction> BreakpointManager::trapActionFor(uint8_t* pc)
{
    Locker locker { m_lock };
    auto it = m_breakpoints.find(pc);
    if (it == m_breakpoints.end())
        return std::nullopt;

    // A breakpoint site takes precedence over a step.
    Breakpoint::Type stopType = it->value->hasSite ? Breakpoint::Type::Regular : Breakpoint::Type::Step;
    return TrapAction { static_cast<OpType>(it->value->originalBytecode), stopType };
}

void BreakpointManager::clearAllOneTimeBreakpoints()
{
    Locker locker { m_lock };
    // Cleared first so releasePatchIfUnused can free unreferenced patches.
    auto steppedPCs = std::exchange(m_oneTimeBreakpoints, { });
    for (uint8_t* pc : steppedPCs)
        releasePatchIfUnused(pc);
    dataLogLnIf(Options::verboseWasmDebugger(), "[BreakpointManager] Cleared all one-time breakpoints");
}

void BreakpointManager::clearAllBreakpoints()
{
    Locker locker { m_lock };
    for (auto& [_, breakpoint] : m_breakpoints)
        breakpoint->restorePatch();
    m_breakpoints.clear();
    m_oneTimeBreakpoints.clear();
    m_addressToPC.clear();
}

} // namespace Wasm
} // namespace JSC

WTF_ALLOW_UNSAFE_BUFFER_USAGE_END

#endif // ENABLE(WEBASSEMBLY_DEBUGGER)
