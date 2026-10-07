/*
 * Copyright (C) 2026 Apple Inc. All rights reserved.
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Library General Public
 * License as published by the Free Software Foundation; either
 * version 2 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Library General Public License for more details.
 *
 * You should have received a copy of the GNU Library General Public License
 * along with this library; see the file COPYING.LIB.  If not, write to
 * the Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor,
 * Boston, MA 02110-1301, USA.
 */

#include "config.h"
#include "SVGImageIntrinsicSizing.h"

#include "CachedImage.h"
#include "Image.h"
#include "LayoutSize.h"
#include "ObjectSizeNegotiation.h"
#include "RenderElement.h"
#include "SVGImageElement.h"
#include "SVGImageElementSizing.h"
#include "SVGLengthContext.h"
#include "StyleComputedStyle+GettersInlines.h"
#include "StyleImage.h"

namespace WebCore {

SVGImageIntrinsicSizing resolveSVGImageIntrinsicSizing(CachedImage& cachedImage, float usedZoom)
{
    using HasRatio = SVGImageIntrinsicSizing::HasRatio;

    RefPtr image = cachedImage.image();

    // Raster (non-SVG) sources: the intrinsic size *is* the ratio.
    if (!image || !image->isSVGImage()) {
        auto naturalDimensions = cachedImage.naturalDimensions();
        auto size = naturalDimensions.width && naturalDimensions.height ? FloatSize { *naturalDimensions.width, *naturalDimensions.height } : FloatSize { };
        size.scale(usedZoom);
        return { size, size, size.isEmpty() ? HasRatio::No : HasRatio::Yes };
    }

    auto naturalDimensions = image->naturalDimensions();

    auto concreteObjectSize = SVGImageElementSizing { }.resolve(naturalDimensions);

    return {
        concreteObjectSize.size(),
        naturalDimensions.aspectRatio.value_or(FloatSize { }),
        naturalDimensions.aspectRatio ? HasRatio::Yes : HasRatio::No
    };
}

FloatRect calculateSVGImageObjectBoundingBox(const SVGImageElement& imageElement, const Style::ComputedStyle& style, CachedImage* cachedImage)
{
    SVGImageIntrinsicSizing sizing;
    if (RefPtr protectedCachedImage = cachedImage)
        sizing = resolveSVGImageIntrinsicSizing(*protectedCachedImage, style.usedZoom());

    SVGLengthContext lengthContext(&imageElement);

    auto& width = style.width();
    auto& height = style.height();
    auto usedZoom = style.usedZoomForLength();
    bool hasRatio = sizing.hasRatio == SVGImageIntrinsicSizing::HasRatio::Yes;

    float concreteWidth;
    if (!width.isAuto())
        concreteWidth = lengthContext.valueForLength(width, usedZoom, SVGLengthMode::Width);
    else if (!height.isAuto() && hasRatio)
        concreteWidth = lengthContext.valueForLength(height, usedZoom, SVGLengthMode::Height) * sizing.ratio.width() / sizing.ratio.height();
    else
        concreteWidth = sizing.size.width();

    float concreteHeight;
    if (!height.isAuto())
        concreteHeight = lengthContext.valueForLength(height, usedZoom, SVGLengthMode::Height);
    else if (!width.isAuto() && hasRatio)
        concreteHeight = lengthContext.valueForLength(width, usedZoom, SVGLengthMode::Width) * sizing.ratio.height() / sizing.ratio.width();
    else
        concreteHeight = sizing.size.height();

    return { imageElement.x().value(lengthContext), imageElement.y().value(lengthContext), concreteWidth, concreteHeight };
}

NaturalDimensions svgImageNaturalDimensions(const Style::Image& styleImage, const RenderElement& renderer)
{
    if (styleImage.errorOccurred()) {
        if (RefPtr cachedImage = styleImage.cachedImage()) {
            if (RefPtr image = cachedImage->image())
                return image->naturalDimensions(renderer.imageOrientation());
        }
    }
    return styleImage.naturalDimensions(renderer, SVGImageElementSizing { });
}

FloatSize svgImageRenderingSize(const Style::Image& styleImage, const RenderElement& renderer, FloatSize containerSize)
{
    if (styleImage.drawsSVGImage())
        return containerSize;
    auto naturalDimensions = svgImageNaturalDimensions(styleImage, renderer);
    return { naturalDimensions.width.value_or(0), naturalDimensions.height.value_or(0) };
}

IntSize svgImageSizeForPreserveAspectRatioNone(const CachedImage& cachedImage, float usedZoom)
{
    if (!cachedImage.hasImage() || cachedImage.errorOccurred())
        return { };

    auto naturalDimensions = protect(cachedImage.image())->naturalDimensions();
    auto size = [&] -> FloatSize {
        if (naturalDimensions.width && naturalDimensions.height)
            return { *naturalDimensions.width, *naturalDimensions.height };
        if (naturalDimensions.aspectRatio)
            return *naturalDimensions.aspectRatio;
        return ObjectSizeNegotiation::defaultObjectSize;
    }();
    size.scale(usedZoom);

    // Don't let a dimension of 1 or more shrink below 1 when zoomed.
    LayoutSize layoutSize { size };
    if (!layoutSize.isEmpty() && usedZoom != 1)
        layoutSize.clampToMinimumSize({ layoutSize.width() > 0 ? 1 : 0, layoutSize.height() > 0 ? 1 : 0 });
    return roundedIntSize(layoutSize);
}

} // namespace WebCore
