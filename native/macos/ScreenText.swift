// buddy-screen-text: the text in an image, read on-device by the Vision framework.
//
// Usage: buddy-screen-text <image>
// Prints one JSON object per line of text: {"t": text, "x": left, "y": top, "w": width, "h": height},
// in pixels with the origin at the top-left. Buddy uses it to read the name on the tile a meeting
// app outlines while someone is talking. Nothing leaves the machine.

import Foundation
import ImageIO
import Vision

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: buddy-screen-text <image>\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    FileHandle.standardError.write("cannot read \(url.path)\n".data(using: .utf8)!)
    exit(1)
}
let width = Double(image.width)
let height = Double(image.height)
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false  // names are not dictionary words
do {
    try VNImageRequestHandler(cgImage: image).perform([request])
} catch {
    FileHandle.standardError.write("text recognition failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}
for observation in request.results ?? [] {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let box = observation.boundingBox  // normalised, origin bottom-left
    let item: [String: Any] = [
        "t": candidate.string,
        "x": box.minX * width,
        "y": (1 - box.maxY) * height,
        "w": box.width * width,
        "h": box.height * height,
    ]
    if let data = try? JSONSerialization.data(withJSONObject: item), let line = String(data: data, encoding: .utf8) {
        print(line)
    }
}
