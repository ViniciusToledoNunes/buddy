import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

/// Streaming box-filter decimator. ScreenCaptureKit ignores the requested sample rate for the
/// microphone stream and delivers the device rate (48 kHz), while Buddy reads the pipe at the
/// configured rate; without this the speech plays at double speed and ASR hears nothing.
final class Resampler {
    private let ratio: Double
    private var carry: [Float] = []
    private var position = 0.0

    init(inputRate: Double, outputRate: Double) {
        ratio = inputRate / outputRate
    }

    func process(_ input: [Float]) -> [Float] {
        let width = max(1, Int(ratio.rounded()))
        let samples = carry + input
        var output: [Float] = []
        var cursor = position
        while cursor + Double(width) <= Double(samples.count) {
            let start = Int(cursor)
            var sum: Float = 0
            for offset in 0..<width { sum += samples[start + offset] }
            output.append(sum / Float(width))
            cursor += ratio
        }
        let consumed = min(Int(cursor), samples.count)
        carry = Array(samples[consumed...])
        position = cursor - Double(consumed)
        return output
    }
}

final class AudioOutput: NSObject, SCStreamOutput, SCStreamDelegate {
    private let selectedType: SCStreamOutputType
    private let targetRate: Double
    private var resampler: Resampler?
    private var rateChecked = false

    init(selectedType: SCStreamOutputType, targetRate: Int) {
        self.selectedType = selectedType
        self.targetRate = Double(targetRate)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == selectedType, sampleBuffer.isValid, let block = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        if !rateChecked {
            rateChecked = true
            if let format = CMSampleBufferGetFormatDescription(sampleBuffer),
               let description = CMAudioFormatDescriptionGetStreamBasicDescription(format)?.pointee,
               description.mSampleRate > 0, abs(description.mSampleRate - targetRate) > 1 {
                resampler = Resampler(inputRate: description.mSampleRate, outputRate: targetRate)
            }
        }
        var lengthAtOffset = 0
        var totalLength = 0
        var pointer: UnsafeMutablePointer<Int8>?
        let status = CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: &lengthAtOffset, totalLengthOut: &totalLength, dataPointerOut: &pointer)
        guard status == kCMBlockBufferNoErr, let pointer, totalLength > 0 else { return }
        guard let resampler else {
            FileHandle.standardOutput.write(Data(bytes: pointer, count: totalLength))
            return
        }
        let count = totalLength / MemoryLayout<Float>.size
        let input = pointer.withMemoryRebound(to: Float.self, capacity: count) {
            Array(UnsafeBufferPointer(start: $0, count: count))
        }
        let output = resampler.process(input)
        if !output.isEmpty { output.withUnsafeBytes { FileHandle.standardOutput.write(Data($0)) } }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write(Data("capture stopped: \(error)\n".utf8))
        exit(2)
    }
}

struct Options {
    var source = "system"
    var rate = 24_000
    var channels = 1
    var device = "default"
    var list = false
}

func options() -> Options {
    var value = Options()
    var index = 1
    let args = CommandLine.arguments
    while index < args.count {
        switch args[index] {
        case "--list": value.list = true
        case "--source": index += 1; value.source = args[index]
        case "--rate": index += 1; value.rate = Int(args[index]) ?? value.rate
        case "--channels": index += 1; value.channels = Int(args[index]) ?? value.channels
        case "--device": index += 1; value.device = args[index]
        default: break
        }
        index += 1
    }
    return value
}

@main
struct MeetingAudioCapture {
    static func main() async throws {
        let opts = options()
        if opts.list {
            let microphones: [[String: Any]] = AVCaptureDevice.devices(for: .audio).map {
                ["name": $0.localizedName, "id": $0.uniqueID, "loopback": false]
            }
            let payload: [String: Any] = [
                "backend": "screencapturekit", "default_speaker": "macOS system audio",
                "default_microphone": "macOS default microphone", "speakers": [], "microphones": microphones,
            ]
            FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: payload))
            return
        }

        guard #available(macOS 15.0, *) else {
            FileHandle.standardError.write(Data("macOS 15 or newer is required\n".utf8))
            exit(1)
        }
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else { throw NSError(domain: "MeetingAudioCapture", code: 1) }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = 2
        configuration.height = 2
        configuration.sampleRate = opts.rate
        configuration.channelCount = opts.channels
        configuration.excludesCurrentProcessAudio = true

        let outputType: SCStreamOutputType
        if opts.source == "microphone" {
            configuration.capturesAudio = false
            configuration.captureMicrophone = true
            if opts.device != "default" { configuration.microphoneCaptureDeviceID = opts.device }
            outputType = .microphone
        } else {
            configuration.capturesAudio = true
            configuration.captureMicrophone = false
            outputType = .audio
        }

        let output = AudioOutput(selectedType: outputType, targetRate: opts.rate)
        let stream = SCStream(filter: filter, configuration: configuration, delegate: output)
        try stream.addStreamOutput(output, type: outputType, sampleHandlerQueue: DispatchQueue(label: "meeting.audio"))
        try await stream.startCapture()
        // dispatchMain() traps when called from the async main task (macOS 26). Suspending keeps
        // the process alive; sample callbacks arrive on the "meeting.audio" queue.
        while true { try await Task.sleep(nanoseconds: 3_600_000_000_000) }
    }
}
