/*
 * Copyright (C) 2019-2021 Apple Inc. All rights reserved.
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

#include <WebCore/InlineIteratorBoxPath.h>

namespace WebCore {

class RenderLineBreak;
class RenderObject;
class RenderSVGText;

namespace Style {
class ComputedStyle;
}

namespace InlineIterator {

class LineBoxIterator;
class BoxIterator;
class LeafBoxIterator;
class TextBoxIterator;

struct EndIterator { };

class Box {
public:
    Box(BoxPath&&);

    bool isText() const;
    bool isSVGText() const;
    bool isInlineBox() const;
    bool isRubyBase() const;
    bool isRootInlineBox() const;
    bool isLineBreak() const;
    bool isBlockLevelBox() const;
    bool isAtomicInlineBox() const;

    FloatRect visualRect() const;
    FloatRect visualRectIgnoringBlockDirection() const;
    // Visual in inline direction, logical for writing mode.
    inline FloatRect logicalRectIgnoringInlineDirection() const;

    inline float logicalTop() const;
    inline float logicalBottom() const;
    inline float logicalHeight() const;
    inline float logicalWidth() const;
    inline float logicalLeft() const;
    inline float logicalRight() const;

    // Return line-relative left/right coords (they are still considered logical values as there's no flip for writing mode).
    inline float logicalLeftIgnoringInlineDirection() const;
    inline float logicalRightIgnoringInlineDirection() const;

    inline bool isHorizontal() const;

    unsigned minimumCaretOffset() const;
    unsigned maximumCaretOffset() const;
    unsigned leftmostCaretOffset() const { return isLeftToRightDirection() ? minimumCaretOffset() : maximumCaretOffset(); }
    unsigned rightmostCaretOffset() const { return isLeftToRightDirection() ? maximumCaretOffset() : minimumCaretOffset(); }

    // isLeftToRightDirection() here is not the same as writingMode().isBidiLTR().
    unsigned char bidiLevel() const;
    TextDirection direction() const { return bidiLevel() % 2 ? TextDirection::RTL : TextDirection::LTR; }
    bool isLeftToRightDirection() const { return direction() == TextDirection::LTR; }
    bool isInlineFlipped() const { return !(isLeftToRightDirection() == writingMode().isLogicalLeftLineLeft()); }

    RenderObject::HighlightState selectionState() const;

    const RenderObject& renderer() const;
    const RenderBlockFlow& formattingContextRoot() const;
    CheckedRef<const Style::ComputedStyle> style() const;
    WritingMode writingMode() const { return style()->writingMode(); }

    // Text-relative left/right
    LeafBoxIterator nextLineRightwardOnLine() const;
    LeafBoxIterator nextLineLeftwardOnLine() const;
    LeafBoxIterator nextLineRightwardOnLineIgnoringLineBreak() const;
    LeafBoxIterator nextLineLeftwardOnLineIgnoringLineBreak() const;

    // Coordinate-relative left/right
    inline LeafBoxIterator nextLogicalRightwardOnLine() const;
    inline LeafBoxIterator nextLogicalLeftwardOnLine() const;
    inline LeafBoxIterator nextLogicalRightwardOnLineIgnoringLineBreak() const;
    inline LeafBoxIterator nextLogicalLeftwardOnLineIgnoringLineBreak() const;

    InlineBoxIterator parentInlineBox() const;

    LineBoxIterator lineBox() const;
    size_t lineIndex() const;

    const BoxPath& path() const LIFETIME_BOUND { return m_path; }

protected:
    friend class BoxIterator;
    friend class InlineBoxIterator;
    friend class LeafBoxIterator;
    friend class TextBoxIterator;

    BoxPath m_path;

private:
    bool hasRenderer() const;
};

class BoxIterator {
public:
    BoxIterator() : m_box(BoxPath { }) { };
    BoxIterator(BoxPath&&);
    BoxIterator(const Box&);

    explicit operator bool() const { return !atEnd(); }

    bool operator==(const BoxIterator&) const;
    bool operator==(EndIterator) const { return atEnd(); }

    const Box& operator*() const LIFETIME_BOUND { return m_box; }
    const Box* operator->() const LIFETIME_BOUND { return &m_box; }

    BoxIterator& traverseLineRightwardOnLine();
    BoxIterator& traverseLineRightwardOnLineSkippingChildren();
    BoxIterator& traverseLineLeftwardOnLine();

    BoxIterator& operator++() { return traverseLineRightwardOnLine(); }
    BoxIterator& operator--() { return traverseLineLeftwardOnLine(); }

    bool atEnd() const;

protected:
    Box m_box;
};

class LeafBoxIterator : public BoxIterator {
public:
    LeafBoxIterator() = default;
    LeafBoxIterator(BoxPath&&);
    LeafBoxIterator(const Box&);

    // Text-relative left/right
    LeafBoxIterator& traverseLineRightwardOnLine();
    LeafBoxIterator& traverseLineLeftwardOnLine();
    LeafBoxIterator& traverseLineRightwardOnLineIgnoringLineBreak();
    LeafBoxIterator& traverseLineLeftwardOnLineIgnoringLineBreak();

    // Coordinate-relative left/right
    inline LeafBoxIterator& traverseLogicalRightwardOnLine();
    inline LeafBoxIterator& traverseLogicalLeftwardOnLine();
    inline LeafBoxIterator& traverseLogicalRightwardOnLineIgnoringLineBreak();
    inline LeafBoxIterator& traverseLogicalLeftwardOnLineIgnoringLineBreak();

    LeafBoxIterator& operator++() { return traverseLineRightwardOnLine(); }
};

template<class IteratorType>
class BoxRange {
public:
    BoxRange(IteratorType begin)
        : m_begin(begin)
    {
    }

    IteratorType begin() const { return m_begin; }
    EndIterator end() const { return { }; }

private:
    IteratorType m_begin;
};

LeafBoxIterator boxFor(const RenderLineBreak&);
LeafBoxIterator boxFor(const RenderBox&);
LeafBoxIterator boxFor(const LayoutIntegration::InlineContent&, size_t boxIndex);

// -----------------------------------------------

inline Box::Box(BoxPath&& path)
    : m_path(WTF::move(path))
{
}

inline bool Box::isText() const
{
    return m_path.isText();
}

inline bool Box::isInlineBox() const
{
    return m_path.isInlineBox();
}

inline bool Box::isRootInlineBox() const
{
    return m_path.isRootInlineBox();
}

inline bool Box::isRubyBase() const
{
    return m_path.isRubyBase();
}

inline FloatRect Box::visualRectIgnoringBlockDirection() const
{
    return m_path.visualRectIgnoringBlockDirection();
}

inline bool Box::isLineBreak() const
{
    return m_path.isLineBreak();
}

inline bool Box::isBlockLevelBox() const
{
    return m_path.isBlockLevelBox();
}

inline bool Box::isAtomicInlineBox() const
{
    return m_path.isAtomicInlineBox();
}

inline unsigned Box::minimumCaretOffset() const
{
    return m_path.minimumCaretOffset();
}

inline unsigned Box::maximumCaretOffset() const
{
    return m_path.maximumCaretOffset();
}

inline unsigned char Box::bidiLevel() const
{
    return m_path.bidiLevel();
}

inline size_t Box::lineIndex() const
{
    return m_path.lineIndex();
}

inline const RenderObject& Box::renderer() const
{
    return m_path.renderer();
}

inline bool Box::hasRenderer() const
{
    return m_path.hasRenderer();
}

inline const RenderBlockFlow& Box::formattingContextRoot() const
{
    return m_path.formattingContextRoot();
}

inline CheckedRef<const Style::ComputedStyle> Box::style() const
{
    return m_path.style();
}

}
}
