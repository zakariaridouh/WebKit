/*
 * Copyright (C) 2020 Apple Inc. All rights reserved.
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

#pragma once

#include <WebCore/FontBaseline.h>
#include <WebCore/InlineIteratorLineBoxPath.h>
#include <WebCore/RenderBlockFlow.h>

namespace WebCore {

class LineSelection;

namespace InlineIterator {

class LineBoxIterator;
class PathIterator;
class LeafBoxIterator;

struct EndLineBoxIterator { };

class LineBox {
public:
    LineBox(LineBoxIteratorPath&&);

    float logicalTop() const;
    float logicalBottom() const;
    float logicalHeight() const { return logicalBottom() - logicalTop(); }
    float logicalWidth() const;

    float contentLogicalTop() const;
    float contentLogicalBottom() const;
    float contentLogicalLeft() const;
    float contentLogicalRight() const;
    float contentLogicalWidth() const;
    float contentLogicalHeight() const;

    float contentLogicalTopAdjustedForPrecedingLineBox() const;
    float contentLogicalBottomAdjustedForFollowingLineBox() const;

    float inkOverflowLogicalTop() const;
    float inkOverflowLogicalBottom() const;
    float scrollableOverflowTop() const;
    float scrollableOverflowBottom() const;

    const Style::ComputedStyle& style() const LIFETIME_BOUND { return isFirst() ? formattingContextRoot().firstLineStyle() : formattingContextRoot().style(); }

    bool hasEllipsis() const;
    enum AdjustedForSelection : bool { No, Yes };
    FloatRect ellipsisVisualRect(AdjustedForSelection = AdjustedForSelection::No) const;
    TextRun ellipsisText() const;
    RenderObject::HighlightState ellipsisSelectionState() const;

    const RenderBlockFlow& formattingContextRoot() const;

    bool isHorizontal() const;
    FontBaseline baselineType() const;

    bool isFirst() const;
    bool isFirstAfterPageBreak() const;

    bool hasBlockContent() const;
    LeafBoxIterator blockLevelBox() const;
    inline bool hasContentfulInFlowBox() const;

    // Text-relative left/right
    LeafBoxIterator lineLeftmostLeafBox() const;
    LeafBoxIterator lineRightmostLeafBox() const;
    // Coordinate-relative left/right
    inline LeafBoxIterator logicalLeftmostLeafBox() const;
    inline LeafBoxIterator logicalRightmostLeafBox() const;

    LineBoxIterator next() const;
    LineBoxIterator previous() const;

    inline size_t lineIndex() const; // Defined in InlineIteratorLineBoxInlines.h

private:
    friend class LineBoxIterator;

    LineBoxIteratorPath m_path;
};

class LineBoxIterator {
public:
    LineBoxIterator() : m_lineBox(LineBoxIteratorPath { }) { };
    LineBoxIterator(LineBoxIteratorPath&&);
    LineBoxIterator(const LineBox&);

    LineBoxIterator& operator++() { return traverseNext(); }
    LineBoxIterator& operator--() { return traversePrevious(); }
    WEBCORE_EXPORT LineBoxIterator& traverseNext();
    LineBoxIterator& traversePrevious();

    WEBCORE_EXPORT explicit operator bool() const;

    bool operator==(const LineBoxIterator&) const;
    bool operator==(EndLineBoxIterator) const { return atEnd(); }

    const LineBox& operator*() const LIFETIME_BOUND { return m_lineBox; }
    const LineBox* operator->() const LIFETIME_BOUND { return &m_lineBox; }

    bool atEnd() const;

private:
    LineBox m_lineBox;
};

WEBCORE_EXPORT LineBoxIterator firstLineBoxFor(const RenderBlockFlow&);
LineBoxIterator lastLineBoxFor(const RenderBlockFlow&);
LineBoxIterator lineBoxFor(const LayoutIntegration::InlineContent&, size_t lineIndex);

LeafBoxIterator closestBoxForHorizontalPosition(const LineBox&, float horizontalPosition, bool editableOnly = false);

inline float previousLineBoxContentBottomOrBorderAndPadding(const LineBox&);
inline float contentStartInBlockDirection(const LineBox&);

// -----------------------------------------------

inline LineBox::LineBox(LineBoxIteratorPath&& path)
    : m_path(WTF::move(path))
{
}

inline float LineBox::contentLogicalTop() const
{
    return m_path.contentLogicalTop();
}

inline float LineBox::contentLogicalBottom() const
{
    return m_path.contentLogicalBottom();
}

inline float LineBox::contentLogicalTopAdjustedForPrecedingLineBox() const
{
    return m_path.contentLogicalTopAdjustedForPrecedingLineBox();
}

inline float LineBox::contentLogicalBottomAdjustedForFollowingLineBox() const
{
    return m_path.contentLogicalBottomAdjustedForFollowingLineBox();
}

inline float LineBox::logicalTop() const
{
    return m_path.logicalTop();
}

inline float LineBox::logicalBottom() const
{
    return m_path.logicalBottom();
}

inline float LineBox::logicalWidth() const
{
    return m_path.logicalWidth();
}

inline float LineBox::inkOverflowLogicalTop() const
{
    return m_path.inkOverflowLogicalTop();
}

inline float LineBox::inkOverflowLogicalBottom() const
{
    return m_path.inkOverflowLogicalBottom();
}

inline float LineBox::scrollableOverflowTop() const
{
    return m_path.scrollableOverflowTop();
}

inline float LineBox::scrollableOverflowBottom() const
{
    return m_path.scrollableOverflowBottom();
}

inline bool LineBox::hasEllipsis() const
{
    return m_path.hasEllipsis();
}

inline FloatRect LineBox::ellipsisVisualRect(AdjustedForSelection adjustedForSelection) const
{
    ASSERT(hasEllipsis());

    auto visualRect = m_path.ellipsisVisualRectIgnoringBlockDirection();

    // FIXME: Add pixel snapping here.
    if (adjustedForSelection == AdjustedForSelection::No) {
        formattingContextRoot().flipForWritingMode(visualRect);
        return visualRect;
    }
    auto selectionTop = formattingContextRoot().adjustEnclosingTopForPrecedingBlock(LayoutUnit { contentLogicalTopAdjustedForPrecedingLineBox() });
    auto selectionBottom = contentLogicalBottomAdjustedForFollowingLineBox();

    visualRect.setY(selectionTop);
    visualRect.setHeight(selectionBottom - selectionTop);
    formattingContextRoot().flipForWritingMode(visualRect);
    return visualRect;
}

inline TextRun LineBox::ellipsisText() const
{
    ASSERT(hasEllipsis());

    return m_path.ellipsisText();
}

inline float LineBox::contentLogicalLeft() const
{
    return m_path.contentLogicalLeft();
}

inline float LineBox::contentLogicalRight() const
{
    return m_path.contentLogicalRight();
}

inline float LineBox::contentLogicalWidth() const
{
    return contentLogicalRight() - contentLogicalLeft();
}

inline float LineBox::contentLogicalHeight() const
{
    return contentLogicalBottom() - contentLogicalTop();
}

inline bool LineBox::isHorizontal() const
{
    return m_path.isHorizontal();
}

inline FontBaseline LineBox::baselineType() const
{
    return m_path.baselineType();
}

inline const RenderBlockFlow& LineBox::formattingContextRoot() const
{
    return m_path.formattingContextRoot();
}

inline bool LineBox::isFirstAfterPageBreak() const
{
    return m_path.isFirstAfterPageBreak();
}

inline bool LineBox::isFirst() const
{
    return !previous();
}

inline bool LineBox::hasBlockContent() const
{
    return m_path.hasBlockLevelBox();
}

inline bool LineBox::hasContentfulInFlowBox() const
{
    return m_path.hasContentfulInFlowBox();
}

}
}

