/*
 * Copyright (C) 2026 Apple Inc. All rights reserved.
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

#include "config.h"
#include "RuntimeQuirkTable.h"

namespace WebCore {

bool RuntimeQuirk::appliesTo(const URLMatchContext& topContext, const URLMatchContext& documentContext, IsTopDocument isTopDocument) const
{
    bool isEmbedded = !embeddedMatches.isEmpty();
    if (isEmbedded) {
        if (isTopDocument == IsTopDocument::Yes || !anyPatternMatches(embeddedMatches, documentContext))
            return false;
        if (!matches.isEmpty() && !anyPatternMatches(matches, topContext))
            return false;
    } else if (!anyPatternMatches(matches, topContext))
        return false;

    auto& subject = isEmbedded ? documentContext : topContext;
    if (anyPatternMatches(excludeMatches, subject))
        return false;

    if (!queryContains.isNull() && !subject.url().query().contains(queryContains))
        return false;

    if (!fragmentContains.isNull() && !subject.url().fragmentIdentifier().contains(fragmentContains))
        return false;

    return !environment || evaluateURLEnvironment(*environment);
}

void RuntimeQuirk::apply(QuirksData& quirksData) const
{
    for (auto& behavior : behaviors)
        quirksData.addBehavior(behavior);
}

} // namespace WebCore
