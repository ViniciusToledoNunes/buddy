// Fits square icon art (e.g. a 1254 px render with black corners) into the macOS icon grid:
// swift native/macos/FitIcon.swift <art.png> native/macos/Buddy.png
import AppKit

// Fit a full-bleed square icon artwork into the macOS icon grid: the art scaled to the
// 824 pt tile at (100, 100) of a 1024 canvas, clipped to the tile's rounded corners, the
// rest transparent. Usage: fit_icon <in.png> <out.png> [measure]
let args = CommandLine.arguments
let src = NSImage(contentsOfFile: args[1])!
let rep = NSBitmapImageRep(data: src.tiffRepresentation!)!
let w = rep.pixelsWide, h = rep.pixelsHigh

// The tile: the first non-black pixels along the middle row and column.
func bright(_ x: Int, _ y: Int) -> Bool {
    let c = rep.colorAt(x: x, y: y)!.usingColorSpace(.sRGB)!
    return c.redComponent + c.greenComponent + c.blueComponent > 0.25
}
var left = 0; while left < w / 2 && !bright(left, h / 2) { left += 1 }
var right = w - 1; while right > w / 2 && !bright(right, h / 2) { right -= 1 }
var top = 0; while top < h / 2 && !bright(w / 2, top) { top += 1 }
var bottom = h - 1; while bottom > h / 2 && !bright(w / 2, bottom) { bottom -= 1 }
print("pixels \(w)x\(h) tile x \(left)...\(right) y \(top)...\(bottom)")
if args.count > 3 { exit(0) }

// Trim a few pixels inside the art's own edge so no dark rim survives.
let trim = 6
let crop = NSRect(x: left + trim, y: h - 1 - bottom + trim,
                  width: right - left - 2 * trim, height: bottom - top - 2 * trim)
let size = 1024.0, tile = NSRect(x: 100, y: 100, width: 824, height: 824)
let out = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1024, pixelsHigh: 1024, bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: out)
NSColor.clear.set()
NSRect(x: 0, y: 0, width: size, height: size).fill()
NSBezierPath(roundedRect: tile, xRadius: 185, yRadius: 185).addClip()
let cg = rep.cgImage!.cropping(to: CGRect(x: crop.minX, y: Double(h) - crop.maxY, width: crop.width, height: crop.height))!
NSImage(cgImage: cg, size: crop.size).draw(in: tile)
NSGraphicsContext.restoreGraphicsState()
try! out.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: args[2]))
print("wrote \(args[2])")
