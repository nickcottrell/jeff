#!/usr/bin/env python3
"""Jeff's toolchain registry.

Jeff owns the connection to the toolchains the system maintains: it discovers
them, tracks their status, and exposes them through its panel and APIs. The
reasoning inside a toolchain (e.g. C2D2's triage + shelf) stays with the
toolchain; Jeff is the doorway, not the room.

The list is the live MCP network, not a hand-kept roster. The source of truth
is .mcp.json -- every MCP peer Jeff sits beside is a toolchain, discovered and
probed the same way mcp-doctor connects to it (initialize + tools/list over a
real JSON-RPC handshake). Add a server to the network and it shows up here; no
second file to maintain. Jeff itself is the doorway, so it is left off its own
list.

A server may ship a toolchain.json beside its script (e.g.
core/c2d2/toolchain.json next to core/c2d2/server.py). That manifest enriches
the row -- title, tier, triage command, capabilities -- but it is optional.
Peers without one still appear, as a bare live line.

Usage:
    toolchains.py            human-readable registry (for the Jeff panel)
    toolchains.py --json     machine-readable (for Jeff's APIs / surfaces)
    toolchains.py --no-probe skip the live handshake (manifest fields only)
    toolchains.py --compact  one line per toolchain, never probes
"""

import importlib.util
import json
import os
import re
import sys

_JEFF_DIR = os.path.dirname(os.path.abspath(__file__))
_CORE_DIR = os.path.dirname(_JEFF_DIR)
_MAESTRO_ROOT = os.path.dirname(_CORE_DIR)
_MCP_CONFIG = os.path.join(_MAESTRO_ROOT, ".mcp.json")


def _read_network():
    """The live MCP network: .mcp.json peers, minus Jeff (the doorway)."""
    try:
        with open(_MCP_CONFIG) as f:
            servers = json.load(f).get("mcpServers", {})
    except Exception:
        return []
    out = []
    for name, cfg in servers.items():
        args = cfg.get("args", [])
        script = args[0] if args else ""
        if name == "jeff" or script.endswith("mcp_proxy.py"):
            continue  # Jeff is the doorway, not an item in its own list.
        out.append({
            "server": name,
            "type": cfg.get("type", "stdio"),
            "command": cfg.get("command", ""),
            "args": args,
            "script": script,
        })
    return out


def _find_manifest(script):
    """A toolchain.json beside the server's script enriches its row (optional)."""
    if not script:
        return None
    path = os.path.join(_MAESTRO_ROOT, os.path.dirname(script), "toolchain.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            tc = json.load(f)
        tc["_manifest"] = os.path.relpath(path, _MAESTRO_ROOT)
        return tc
    except Exception as exc:
        return {"error": "bad manifest: %s" % exc}


def _tool_count_from_manifest(tc):
    """Fallback tool count from a declared tools_manifest, when not probing live."""
    rel = tc.get("tools_manifest")
    if not rel:
        return None
    path = os.path.join(_MAESTRO_ROOT, rel)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return sum(1 for line in f if re.match(r"\s+-\s+name:", line))
    except Exception:
        return None


_doctor = None


def _handshake(command, args):
    """Reuse mcp-doctor's proven initialize + tools/list probe (one copy, shared)."""
    global _doctor
    if _doctor is None:
        path = os.path.join(_CORE_DIR, "mcp-doctor.py")
        spec = importlib.util.spec_from_file_location("mcp_doctor", path)
        _doctor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_doctor)
    return _doctor.handshake(command, args)


def summary(probe=True):
    """The registry: one dict per live toolchain, with manifest enrichment."""
    out = []
    for srv in _read_network():
        man = _find_manifest(srv["script"]) or {}
        if man.get("error"):
            out.append({"name": srv["server"], "server": srv["server"],
                        "error": man["error"]})
            continue
        tri = man.get("triage") or {}
        triage_cmd = ([tri["command"]] + tri.get("args", [])) if tri.get("command") else None
        status, tools = "unprobed", None
        if probe:
            h = _handshake(srv["command"], srv["args"])
            status = "up" if h.get("ok") else "down"
            tools = h.get("tools")
        if tools is None:
            tools = _tool_count_from_manifest(man)
        ep = man.get("endpoint", {}) or {}
        out.append({
            "name": man.get("name", srv["server"]),
            "server": srv["server"],
            "title": man.get("title", ""),
            "kind": man.get("kind", ""),
            "tier": man.get("tier", ""),
            "summary": man.get("summary", ""),
            "tools": tools,
            "capabilities": man.get("capabilities", []),
            "triage": bool(man.get("triage")),
            "triage_cmd": triage_cmd,
            "endpoint": ("%s %s %s" % (
                ep.get("type", srv["type"]), srv["command"],
                " ".join(srv["args"]))).strip(),
            "status": status,
            "manifest": man.get("_manifest", ""),
        })
    return out


def _print_human(rows):
    if not rows:
        print("No toolchains on the network.")
        return
    dot = {"up": "●", "down": "○", "unknown": "◌", "unprobed": "●"}
    print("Toolchains on the network: %d\n" % len(rows))
    for r in rows:
        if r.get("error"):
            print("  ○ %s — %s" % (r["name"], r["error"]))
            continue
        label = r["title"] or r["server"]
        tags = ", ".join(t for t in (r.get("tier"), r.get("kind")) if t)
        head = "  %s %s — %s" % (dot.get(r["status"], "◌"), r["name"], label)
        print(head + ("  [%s]" % tags if tags else ""))
        bits = ["status: %s" % r["status"]]
        if r.get("tools") is not None:
            bits.append("%d tools" % r["tools"])
        bits.append("triage: %s" % ("yes" if r["triage"] else "no"))
        print("    %s" % " · ".join(bits))
        print("    endpoint: %s" % r["endpoint"])
        if r.get("summary"):
            print("    %s" % r["summary"])
        print()


def _print_compact(rows):
    """One line per toolchain -- for the always-visible panel header."""
    if not rows:
        print("  (none on the network)")
        return
    dot = {"up": "●", "down": "○", "unknown": "◌", "unprobed": "●"}
    for r in rows:
        if r.get("error"):
            print("  ○ %s — %s" % (r["name"], r["error"]))
            continue
        bits = []
        if r.get("tools") is not None:
            bits.append("%d tools" % r["tools"])
        if r.get("triage"):
            bits.append("triage")
        tier = ("  [%s]" % r["tier"]) if r.get("tier") else ""
        line = "  %s %s" % (dot.get(r["status"], "●"), r["name"])
        if bits:
            line += "  %s" % " · ".join(bits)
        print(line + tier)


def main():
    if "--compact" in sys.argv:
        # No probe: the panel redraws every keypress, keep it instant.
        _print_compact(summary(probe=False))
        return
    rows = summary(probe="--no-probe" not in sys.argv)
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=2))
    else:
        _print_human(rows)


if __name__ == "__main__":
    main()
