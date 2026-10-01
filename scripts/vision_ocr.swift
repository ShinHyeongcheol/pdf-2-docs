// Local macOS OCR: this helper makes no network or provider calls.
import Foundation
import Vision

struct OCRLine: Codable {
    let text: String
    let confidence: Float
    let x0: Double
    let y0: Double
    let x1: Double
    let y1: Double
}

struct OCRPage: Codable {
    let engine: String
    let languages: [String]
    let lines: [OCRLine]
}

do {
    guard CommandLine.arguments.count == 3 else {
        throw NSError(domain: "local-ocr", code: 1, userInfo: [NSLocalizedDescriptionKey: "Usage: vision-ocr IMAGE OUTPUT_JSON"])
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.revision = VNRecognizeTextRequestRevision3
    let languages = ["ko-KR", "en-US"]
    let supported = try request.supportedRecognitionLanguages()
    guard languages.allSatisfy({ supported.contains($0) }) else {
        throw NSError(domain: "local-ocr", code: 2, userInfo: [NSLocalizedDescriptionKey: "Required OCR languages unavailable on this Mac"])
    }
    request.recognitionLanguages = languages
    let handler = VNImageRequestHandler(url: URL(fileURLWithPath: CommandLine.arguments[1]), options: [:])
    try handler.perform([request])
    let lines = (request.results ?? []).compactMap { observation -> OCRLine? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return OCRLine(text: candidate.string, confidence: candidate.confidence,
                       x0: box.minX, y0: 1 - box.maxY, x1: box.maxX, y1: 1 - box.minY)
    }.sorted { left, right in
        if abs(left.y0 - right.y0) > 0.005 { return left.y0 < right.y0 }
        return left.x0 < right.x0
    }
    let result = OCRPage(engine: "apple-vision-r3", languages: languages, lines: lines)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    try encoder.encode(result).write(to: URL(fileURLWithPath: CommandLine.arguments[2]), options: .atomic)
    print("lines=\(lines.count)")
} catch {
    fputs("OCR failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}
