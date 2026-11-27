import AppKit
// Regenerate: swift native/macos/AppIcon.swift native/macos/Buddy.png && sips -z 1024 1024 native/macos/Buddy.png

// Buddy's app icon: a friendly robot puppy with a shield tag on its collar, on a calm blue
// rounded square (macOS icon grid, 1024 pt). Drawn here, no third-party art.
func rgb(_ hex: Int, _ alpha: Double = 1) -> NSColor {
    NSColor(srgbRed: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255, alpha: alpha)
}

func rotated(_ path: NSBezierPath, by degrees: Double, around center: NSPoint) -> NSBezierPath {
    let t = NSAffineTransform()
    t.translateX(by: center.x, yBy: center.y)
    t.rotate(byDegrees: degrees)
    t.translateX(by: -center.x, yBy: -center.y)
    path.transform(using: t as AffineTransform)
    return path
}

let size = 1024.0
let image = NSImage(size: NSSize(width: size, height: size))
image.lockFocus()

// Tile.
let tile = NSBezierPath(roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824), xRadius: 185, yRadius: 185)
NSGradient(starting: rgb(0x6C9BFF), ending: rgb(0x2D4BB8))!.draw(in: tile, angle: -90)

// Floppy ears, behind the head.
let earColor = rgb(0x4A5A86)
for (x, angle) in [(318.0, -34.0), (706.0, 34.0)] {
    // Hung from the top of the head and swung outward, like a puppy's.
    let ear = NSBezierPath(roundedRect: NSRect(x: x - 72, y: 400, width: 144, height: 300), xRadius: 72, yRadius: 72)
    earColor.setFill()
    rotated(ear, by: angle, around: NSPoint(x: x, y: 660)).fill()
}

// Antenna with a warm light.
rgb(0xDCE3F2).setStroke()
let stalk = NSBezierPath()
stalk.move(to: NSPoint(x: 512, y: 660))
stalk.line(to: NSPoint(x: 512, y: 752))
stalk.lineWidth = 22
stalk.lineCapStyle = .round
stalk.stroke()
rgb(0xFFE9A8, 0.45).setFill()
NSBezierPath(ovalIn: NSRect(x: 512 - 52, y: 770 - 52, width: 104, height: 104)).fill()
rgb(0xFFC15A).setFill()
NSBezierPath(ovalIn: NSRect(x: 512 - 32, y: 770 - 32, width: 64, height: 64)).fill()

// Head.
let head = NSBezierPath(roundedRect: NSRect(x: 262, y: 290, width: 500, height: 390), xRadius: 175, yRadius: 175)
NSGradient(starting: rgb(0xFAFBFF), ending: rgb(0xD5DCEC))!.draw(in: head, angle: -90)

// Visor with two round, friendly eyes.
let visor = NSBezierPath(roundedRect: NSRect(x: 318, y: 455, width: 388, height: 140), xRadius: 70, yRadius: 70)
rgb(0x1C2645).setFill()
visor.fill()
for cx in [430.0, 594.0] {
    rgb(0x72E6FF, 0.30).setFill()
    NSBezierPath(ovalIn: NSRect(x: cx - 50, y: 525 - 50, width: 100, height: 100)).fill()
    rgb(0x72E6FF).setFill()
    NSBezierPath(ovalIn: NSRect(x: cx - 36, y: 525 - 36, width: 72, height: 72)).fill()
    NSColor.white.setFill()
    NSBezierPath(ovalIn: NSRect(x: cx - 4, y: 538, width: 20, height: 20)).fill()
}

// Snout: nose, smile and a little tongue.
let tongue = NSBezierPath(roundedRect: NSRect(x: 490, y: 318, width: 50, height: 58), xRadius: 25, yRadius: 25)
rgb(0xFF7F9C).setFill()
tongue.fill()
rgb(0x1C2645).setStroke()
let smile = NSBezierPath()
smile.move(to: NSPoint(x: 452, y: 384))
smile.curve(to: NSPoint(x: 512, y: 384), controlPoint1: NSPoint(x: 466, y: 352), controlPoint2: NSPoint(x: 498, y: 352))
smile.curve(to: NSPoint(x: 572, y: 384), controlPoint1: NSPoint(x: 526, y: 352), controlPoint2: NSPoint(x: 558, y: 352))
smile.lineWidth = 16
smile.lineCapStyle = .round
smile.stroke()
rgb(0x1C2645).setFill()
NSBezierPath(roundedRect: NSRect(x: 478, y: 392, width: 68, height: 44), xRadius: 22, yRadius: 22).fill()

// Collar with a shield tag: Buddy looks after you.
let collar = NSBezierPath(roundedRect: NSRect(x: 352, y: 246, width: 320, height: 52), xRadius: 26, yRadius: 26)
rgb(0xFF9F43).setFill()
collar.fill()
let shield = NSBezierPath()
shield.move(to: NSPoint(x: 512 - 52, y: 262))
shield.line(to: NSPoint(x: 512 + 52, y: 262))
shield.line(to: NSPoint(x: 512 + 52, y: 210))
shield.curve(to: NSPoint(x: 512, y: 140), controlPoint1: NSPoint(x: 512 + 52, y: 176), controlPoint2: NSPoint(x: 540, y: 152))
shield.curve(to: NSPoint(x: 512 - 52, y: 210), controlPoint1: NSPoint(x: 484, y: 152), controlPoint2: NSPoint(x: 512 - 52, y: 176))
shield.close()
rgb(0xFFD36B).setFill()
shield.fill()
let check = NSBezierPath()
check.move(to: NSPoint(x: 490, y: 212))
check.line(to: NSPoint(x: 507, y: 194))
check.line(to: NSPoint(x: 536, y: 232))
check.lineWidth = 12
check.lineCapStyle = .round
check.lineJoinStyle = .round
rgb(0x8A5A00).setStroke()
check.stroke()

image.unlockFocus()
let rep = NSBitmapImageRep(data: image.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
