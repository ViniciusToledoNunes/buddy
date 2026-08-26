import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

final class AudioOutput: NSObject, SCStreamOutput, SCStreamDelegate {
    private let selectedType: SCStreamOutputType

    init(selectedType: SCStreamOutputType) {
        self.selectedType = selectedType
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == selectedType, sampleBuffer.isValid, let block = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        var lengthAtOffset = 0
        var totalLength = 0
        var pointer: UnsafeMutablePointer<Int8>?
        let status = CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: &lengthAtOffset, totalLengthOut: &totalLength, dataPointerOut: &pointer)
        guard status == kCMBlockBufferNoErr, let pointer, totalLength > 0 else { return }
        FileHandle.standardOutput.write(Data(bytes: pointer, count: totalLength))
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

        let output = AudioOutput(selectedType: outputType)
        let stream = SCStream(filter: filter, configuration: configuration, delegate: output)
        try stream.addStreamOutput(output, type: outputType, sampleHandlerQueue: DispatchQueue(label: "meeting.audio"))
        try await stream.startCapture()
        dispatchMain()
    }
}
