// buddy-mic-state: whether you are muted in the meeting app, read from its accessibility tree.
//
// Usage: buddy-mic-state [--once] [--interval SECONDS] [--rules FILE]
// Prints one JSON line whenever the state changes, and once at start:
//   {"state": "muted" | "open" | "none", "app": "Microsoft Teams", "label": "Unmute mic"}
// "none": no meeting app with a microphone button was found. With --once it prints the
// current state and exits.
//
// Meeting apps label their microphone button by what pressing it does: "Unmute mic" while
// you are muted, "Mute mic" while you are heard (Teams); "Turn on microphone" / "Turn off
// microphone" (Google Meet, in a Chromium browser). A rule names the apps, and the label
// prefixes that mean muted or open. Needs the Accessibility permission, granted to the app
// that starts Buddy (the terminal or the editor). Nothing is clicked or changed.

import ApplicationServices
import Cocoa
import Foundation

struct Rule: Codable {
    let name: String
    let bundles: [String]  // bundle id prefixes
    let muted: [String]  // label prefixes, case-insensitive
    let open: [String]
    let web: Bool?  // Chromium: ask it to expose the page's accessibility tree
}

let builtIn: [Rule] = [
    Rule(name: "Microsoft Teams", bundles: ["com.microsoft.teams2", "com.microsoft.teams"],
         muted: ["Unmute mic", "Unmute"], open: ["Mute mic", "Mute"], web: true),
    Rule(name: "Google Meet",
         bundles: ["com.google.Chrome", "com.microsoft.edgemac", "company.thebrowser.Browser", "com.brave.Browser"],
         muted: ["Turn on microphone"], open: ["Turn off microphone"], web: true),
    Rule(name: "Zoom", bundles: ["us.zoom.xos"], muted: ["Unmute audio", "Unmute my audio"],
         open: ["Mute audio", "Mute my audio"], web: false),
]

var once = false
var interval = 1.0
var rules = builtIn
var arguments = Array(CommandLine.arguments.dropFirst())
while !arguments.isEmpty {
    let argument = arguments.removeFirst()
    switch argument {
    case "--once": once = true
    case "--interval": interval = Double(arguments.isEmpty ? "1" : arguments.removeFirst()) ?? 1.0
    case "--rules":
        let path = arguments.isEmpty ? "" : arguments.removeFirst()
        guard let data = FileManager.default.contents(atPath: path),
              let loaded = try? JSONDecoder().decode([Rule].self, from: data) else {
            FileHandle.standardError.write("cannot read rules from \(path)\n".data(using: .utf8)!)
            exit(2)
        }
        rules = loaded
    default:
        FileHandle.standardError.write("usage: buddy-mic-state [--once] [--interval S] [--rules FILE]\n".data(using: .utf8)!)
        exit(2)
    }
}

if !AXIsProcessTrusted() {
    FileHandle.standardError.write("Accessibility permission is missing for the app that started Buddy\n".data(using: .utf8)!)
    print(#"{"state": "none", "app": null, "label": null, "error": "not trusted"}"#)
    exit(3)
}

func text(_ element: AXUIElement, _ name: String) -> String? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value as? String
}

func label(_ element: AXUIElement) -> String {
    for name in [kAXTitleAttribute, kAXDescriptionAttribute] {
        if let value = text(element, name), !value.isEmpty { return value }
    }
    return ""
}

func classify(_ value: String, _ rule: Rule) -> String? {
    let lowered = value.lowercased()
    // "Unmute" also contains "mute": the muted prefixes are tried first.
    if rule.muted.contains(where: { lowered.hasPrefix($0.lowercased()) }) { return "muted" }
    if rule.open.contains(where: { lowered.hasPrefix($0.lowercased()) }) { return "open" }
    return nil
}

func find(_ element: AXUIElement, _ rule: Rule, _ depth: Int, _ budget: inout Int) -> AXUIElement? {
    budget -= 1
    if budget < 0 || depth > 60 { return nil }
    if (text(element, kAXRoleAttribute) ?? "") == kAXButtonRole as String, classify(label(element), rule) != nil {
        return element
    }
    var children: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children) == .success,
          let list = children as? [AXUIElement] else { return nil }
    for child in list {
        if let found = find(child, rule, depth + 1, &budget) { return found }
    }
    return nil
}

// The button found last time, per process: reading one label each second costs nothing,
// walking a meeting window's ~2,000 elements does, so the walk is repeated only when the
// button is gone, and not more than every few seconds.
var cached: [pid_t: (rule: Rule, button: AXUIElement)] = [:]
var lastSearch: [pid_t: Date] = [:]

func current() -> (state: String, app: String?, label: String?) {
    var found: [(String, String, String)] = []
    for app in NSWorkspace.shared.runningApplications {
        let bundle = app.bundleIdentifier ?? ""
        guard let rule = rules.first(where: { rule in rule.bundles.contains(where: { bundle.hasPrefix($0) }) }) else { continue }
        let pid = app.processIdentifier
        if let hit = cached[pid] {
            let value = label(hit.button)
            if let state = classify(value, hit.rule) {
                found.append((state, hit.rule.name, value))
                continue
            }
            cached[pid] = nil
        }
        if let last = lastSearch[pid], Date().timeIntervalSince(last) < 5 { continue }
        lastSearch[pid] = Date()
        let root = AXUIElementCreateApplication(pid)
        if rule.web ?? false {
            AXUIElementSetAttributeValue(root, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        }
        var budget = 30000
        if let button = find(root, rule, 0, &budget), let state = classify(label(button), rule) {
            cached[pid] = (rule, button)
            found.append((state, rule.name, label(button)))
        }
    }
    // Two calls at once (a Teams call and a Meet tab): heard in either one counts as heard.
    if let heard = found.first(where: { $0.0 == "open" }) { return (heard.0, heard.1, heard.2) }
    if let muted = found.first { return (muted.0, muted.1, muted.2) }
    return ("none", nil, nil)
}

func emit(_ value: (state: String, app: String?, label: String?)) {
    let item: [String: Any] = ["state": value.state, "app": value.app ?? NSNull(), "label": value.label ?? NSNull()]
    if let data = try? JSONSerialization.data(withJSONObject: item), let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

var previous = ""
repeat {
    let value = current()
    let key = "\(value.state)|\(value.app ?? "")"
    if key != previous || once {
        emit(value)
        previous = key
    }
    if once { break }
    Thread.sleep(forTimeInterval: interval)
} while true
