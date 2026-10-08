#!/usr/bin/env python3
"""jeff/make.py -- Jeff's read-only view of live Figma Make design slots.

Reads a roots pointer (make-roots.json: a list of make-integration dirs, one per project)
and aggregates each root's registry.json -- the file that project's `./ops.sh make` writes.
The slots' code, designs, and thumbnails stay in their own project dirs; Jeff only holds the
pointer and reads. Shared by the Jeff TUI (list), the Jeff MCP tool (make_list), and the
broker /make gallery -- one reader, one contract.

CLI:  make.py list   -> Jeff-grammar rows for the TUI panel
      make.py json   -> machine-readable slots
"""
import json
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOTS_FILE = os.path.join(HERE, "make-roots.json")


def load_roots():
    """The pointer: a list of make-integration dirs. Nothing client-side is copied here."""
    try:
        with open(ROOTS_FILE, encoding="utf-8") as f:
            roots = json.load(f)
        return [os.path.expanduser(r) for r in roots if isinstance(r, str)]
    except (OSError, ValueError):
        return []


def _port_live(port):
    if not port:
        return False
    # Probe "localhost" (not just 127.0.0.1): Vite binds localhost, which can resolve to
    # IPv6 ::1 — create_connection tries every resolved address, so IPv4-only misses it.
    try:
        with socket.create_connection(("localhost", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def aggregate():
    """Flat list of slot dicts across all roots -- each with live status + absolute thumb paths."""
    out = []
    for root in load_roots():
        try:
            with open(os.path.join(root, "registry.json"), encoding="utf-8") as f:
                reg = json.load(f)
        except (OSError, ValueError):
            continue
        for name, slot in (reg.get("slots") or {}).items():
            pages = []
            for p in slot.get("pages", []):
                thumb_rel = p.get("thumb", "")
                pages.append({
                    "path": p.get("path", "/"),
                    "label": p.get("label", ""),
                    "thumb": os.path.join(root, thumb_rel) if thumb_rel else "",
                })
            out.append({
                "slot": name,
                "root": root,
                "port": slot.get("port"),
                "url": slot.get("url", ""),
                "title": slot.get("title", name),
                "permalink": slot.get("permalink", ""),
                "info": slot.get("info", ""),
                "front_port": slot.get("front_port"),
                "origin": slot.get("origin", "deployed"),
                "pages": pages,
                "updated": slot.get("updated", ""),
                "live": _port_live(slot.get("port")),
            })
    out.sort(key=lambda s: s["slot"])
    return out


def cmd_list():
    slots = aggregate()
    if not slots:
        print("  (none registered)")
        return
    for s in slots:
        n = len(s["pages"])
        # registered == connected, so always [+]; note a local proxy/port only if one is running
        status = "connected"
        if s.get("port"):
            status += " · :%s %s" % (s.get("front_port") or s["port"], "up" if s["live"] else "offline")
        # full slot name (no truncation) -- it's what `make disconnect <name>` needs
        print("  [+] %-30s %d page%s  %s" % (s["slot"], n, "" if n == 1 else "s", status))


def cmd_sync():
    """Refresh the cue-mem token for each CONNECTED prototype -- the one durable record Claude
    Code reads to understand the build without re-explanation. The token carries the linkage
    (back to Figma) + Figma's relayed diff. Disconnected prototypes are NOT deleted here: their
    tokens are left to cool naturally via the lifecycle. Minted via the official createtoken."""
    import subprocess
    import re as _re
    maestro = os.path.dirname(os.path.dirname(HERE))
    tokens_dir = os.path.join(maestro, ".claude", "tokens")
    createtoken = os.path.join(maestro, "cue-mem", "cli", "createtoken")
    refreshed = 0
    for s in aggregate():
        tid = "ctx_make_" + _re.sub(r"[^a-z0-9]+", "_", s["slot"].lower()).strip("_")
        link = s["permalink"] or s["url"] or (("http://localhost:%s/" % s["port"]) if s["port"] else "")
        parts = ["figma prototype %s" % s["slot"]]
        if link:
            parts.append(link)
        if s.get("info"):
            parts.append(s["info"])
        if s["url"] or s["port"]:
            parts.append("live" if s["live"] else "offline")
        # Overwrite (= refresh + re-heat) so a re-register with a new diff updates the record.
        subprocess.run([sys.executable, createtoken, "--token-id", tid,
                        "--value", " · ".join(parts), "--tokens-dir", tokens_dir],
                       check=False, capture_output=True)
        refreshed += 1
    print("tokens: %d refreshed (disconnected cool naturally)" % refreshed)


# ---- the local proxy: one stable front port per prototype -> its real port ----------
# Raw TCP relay (not HTTP), so Vite asset paths AND the HMR websocket pass through untouched.

def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle(client, real_port):
    import threading
    try:
        upstream = socket.create_connection(("127.0.0.1", real_port), timeout=5)
    except OSError:
        client.close()
        return
    threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
    threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


def _listener(front_port, real_port, stop_event):
    import threading
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", front_port))
    except OSError:
        print("proxy: :%d busy, skipped" % front_port, flush=True)
        return
    srv.listen(64)
    srv.settimeout(1.0)
    while not stop_event.is_set():
        try:
            client, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_handle, args=(client, real_port), daemon=True).start()
    srv.close()


def cmd_serve():
    """Always-on Make daemon (com.maestro.make, KeepAlive): forward each connected prototype's
    stable front port -> its real local port, reconciling as you connect/disconnect, and refresh
    the cue-mem tokens on a slower pulse. Dies cleanly on launchd stop (daemon threads)."""
    import threading
    import time
    reconcile_s = int(os.environ.get("MAKE_RECONCILE_INTERVAL", "10"))
    sync_s = int(os.environ.get("MAKE_SYNC_INTERVAL", "1800"))
    forwarders = {}  # front_port -> (stop_event, real_port)

    def reconcile():
        want = {}
        for s in aggregate():
            fp, rp = s.get("front_port"), s.get("port")
            if fp and rp:
                want[int(fp)] = int(rp)
        for fp, rp in want.items():
            cur = forwarders.get(fp)
            if cur and cur[1] == rp:
                continue
            if cur:
                cur[0].set()
            ev = threading.Event()
            threading.Thread(target=_listener, args=(fp, rp, ev), daemon=True).start()
            forwarders[fp] = (ev, rp)
            print("proxy: :%d -> :%d" % (fp, rp), flush=True)
        for fp in [p for p in forwarders if p not in want]:
            forwarders[fp][0].set()
            del forwarders[fp]
            print("proxy: :%d stopped" % fp, flush=True)

    print("make serve: proxy + token sync (reconcile %ds, sync %ds)" % (reconcile_s, sync_s), flush=True)
    last_sync = 0.0
    while True:
        try:
            reconcile()
            now = time.monotonic()
            if now - last_sync >= sync_s:
                cmd_sync()
                last_sync = now
        except Exception as exc:  # keep the daemon alive
            print("serve error: %s" % exc, flush=True)
        time.sleep(reconcile_s)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        cmd_list()
    elif cmd == "json":
        print(json.dumps(aggregate(), indent=2))
    elif cmd == "sync":
        cmd_sync()
    elif cmd == "serve":
        cmd_serve()
    else:
        print("usage: make.py list|json|sync|serve", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
