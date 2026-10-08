"""Jeff MCP Proxy -- stable host-side entrypoint.

Claude Code points at this file in .mcp.json. It never changes.
On each tool call, reads .jeff-state.json to find the active chip
and routes to it dynamically. Swap chips without restarting.
"""

import json
import os
import sqlite3
import sys
import threading

JEFF_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(JEFF_DIR, ".jeff-state.json")
SPECTRAL_DIR = os.path.join(JEFF_DIR, "spectral")

sys.path.insert(0, SPECTRAL_DIR)

from mcp.server.fastmcp import FastMCP
import spectral

mcp = FastMCP("jeff")


# ============================================================
# RESILIENCE LAYER
# ============================================================

class VolumeGoneError(Exception):
    """Raised when a volume/chip path is no longer reachable."""
    pass


def _safe_read(path):
    """Read a file, raising VolumeGoneError if the volume vanished."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        raise VolumeGoneError("Path not found: %s" % path)
    except PermissionError:
        raise VolumeGoneError("Permission denied: %s" % path)
    except OSError as exc:
        raise VolumeGoneError("I/O error on %s: %s" % (path, exc))


def _safe_json(path):
    """Read and parse a JSON file, raising VolumeGoneError if unreachable."""
    raw = _safe_read(path)
    return json.loads(raw)


def _safe_listdir(path):
    """List a directory, raising VolumeGoneError if gone."""
    try:
        return sorted(os.listdir(path))
    except FileNotFoundError:
        raise VolumeGoneError("Directory not found: %s" % path)
    except PermissionError:
        raise VolumeGoneError("Permission denied: %s" % path)
    except OSError as exc:
        raise VolumeGoneError("I/O error on %s: %s" % (path, exc))


def _safe_db_query(db_path, query, params=()):
    """Run a read query against a sqlite db. Returns (rows, error).

    On success: (list_of_dicts, None)
    On failure: ([], error_string)
    """
    if not db_path or not os.path.isfile(db_path):
        return [], "Database not found: %s" % db_path
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        conn.close()
        return rows, None
    except sqlite3.OperationalError as exc:
        return [], "Database error: %s" % exc
    except OSError as exc:
        return [], "Volume gone during query: %s" % exc


def _safe_db_execute(db_path, query, params=()):
    """Run a write query against a sqlite db. Returns result dict."""
    if not db_path or not os.path.isfile(db_path):
        return {"error": "Database not found: %s" % db_path}
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(query, params)
        conn.commit()
        result = {"rows_affected": cur.rowcount, "lastrowid": cur.lastrowid}
        conn.close()
        return result
    except sqlite3.OperationalError as exc:
        return {"error": "Database error: %s" % exc}
    except OSError as exc:
        return {"error": "Volume gone during write: %s" % exc}


def _volume_alive(path):
    """Quick liveness check -- can we stat the path?"""
    try:
        os.stat(path)
        return True
    except (FileNotFoundError, PermissionError, OSError):
        return False


def _scan_chip_surface(vol_path):
    """Scan a chip's full surface and return a structured manifest.

    Reports everything on the chip -- not just vaults. Datasets, MCP service,
    models, schemas, docs, sparseimage -- the whole picture.
    """
    surface = {
        "path": vol_path,
        "alive": _volume_alive(vol_path),
        "vaults": [],
        "datasets": [],
        "models": [],
        "docs": [],
        "schemas": [],
        "has_mcp": False,
        "has_heartbeat": False,
        "has_sparseimage": False,
        "sparseimage_mb": 0,
        "model_name": None,
        "label": None,
        "device_id": None,
    }

    if not surface["alive"]:
        return surface

    try:
        entries = _safe_listdir(vol_path)
    except VolumeGoneError:
        surface["alive"] = False
        return surface

    # Heartbeat
    hb_path = os.path.join(vol_path, "heartbeat.json")
    if os.path.isfile(hb_path):
        surface["has_heartbeat"] = True
        try:
            hb = _safe_json(hb_path)
            surface["label"] = hb.get("label")
            surface["device_id"] = hb.get("device_id")
            surface["model_name"] = hb.get("model")
        except VolumeGoneError:
            pass

    # MCP service
    mcp_dir = os.path.join(vol_path, "mcp")
    if os.path.isdir(mcp_dir):
        surface["has_mcp"] = True

    # Datasets
    ds_dir = os.path.join(vol_path, "datasets")
    if os.path.isdir(ds_dir):
        try:
            for f in _safe_listdir(ds_dir):
                if f.endswith(".json") or f.endswith(".md"):
                    surface["datasets"].append(f)
        except VolumeGoneError:
            pass

    # Models
    models_dir = os.path.join(vol_path, "models")
    if os.path.isdir(models_dir):
        try:
            for f in _safe_listdir(models_dir):
                full = os.path.join(models_dir, f)
                if os.path.isfile(full):
                    size_mb = os.path.getsize(full) / (1024 * 1024)
                    surface["models"].append({"name": f, "size_mb": round(size_mb, 1)})
        except VolumeGoneError:
            pass

    # Docs
    jeff_docs = os.path.join(vol_path, ".jeff", "docs")
    if os.path.isdir(jeff_docs):
        try:
            for f in _safe_listdir(jeff_docs):
                if f.endswith(".md"):
                    surface["docs"].append(f)
        except VolumeGoneError:
            pass

    # Schemas
    for f in entries:
        if f.endswith(".sql"):
            surface["schemas"].append(f)

    # Sparseimage
    si_path = os.path.join(vol_path, "vault.sparseimage")
    if os.path.isfile(si_path):
        surface["has_sparseimage"] = True
        try:
            surface["sparseimage_mb"] = round(os.path.getsize(si_path) / (1024 * 1024), 1)
        except OSError:
            pass

    # Vaults (vault-* dirs)
    for f in entries:
        vault_dir = os.path.join(vol_path, f)
        if os.path.isdir(vault_dir) and f.startswith("vault-"):
            vault_name = f.replace("vault-", "")
            db = os.path.join(vault_dir, "vault.db")
            has_db = os.path.isfile(db)
            has_cuesheet = os.path.isfile(os.path.join(vault_dir, "cue-sheet.yaml"))
            surface["vaults"].append({
                "name": vault_name,
                "has_db": has_db,
                "has_cuesheet": has_cuesheet,
            })

    # Search index
    idx = os.path.join(vol_path, ".jeff", "index", "search.json")
    surface["has_search_index"] = os.path.isfile(idx)

    return surface


def _auto_deactivate(vol_name):
    """Remove a volume from the active list when it vanishes."""
    if not os.path.isfile(VOLUMES_ACTIVE_FILE):
        return
    try:
        with open(VOLUMES_ACTIVE_FILE) as f:
            active = json.load(f)
        if vol_name in active:
            active.remove(vol_name)
            with open(VOLUMES_ACTIVE_FILE, "w") as f:
                json.dump(sorted(set(active)), f, indent=2)
                f.write("\n")
    except Exception:
        pass

# cue-vox stream notification (fire-and-forget)
CUE_VOX_PORT = os.environ.get("CUE_VOX_PORT", "3000")
CUE_VOX_CHIP_OP_URL = "http://localhost:%s/api/chip/op" % CUE_VOX_PORT


def _notify_stream(op, summary="", chip_data=None):
    """POST chip op to cue-vox stream. Non-blocking, best-effort."""
    state = _active_state()
    label = state.get("label", "?") if state else "?"
    root = "#888888"
    if state:
        hb_path = os.path.join(state.get("volume_path", ""), "heartbeat.json")
        if os.path.isfile(hb_path):
            with open(hb_path) as f:
                hb = json.load(f)
            root = hb.get("tool_chain", {}).get("root", root)

    payload = {
        "op": op,
        "label": label,
        "root_color": root,
        "summary": summary,
        "chip_data": chip_data or {},
    }

    def _post():
        try:
            import urllib.request
            req = urllib.request.Request(
                CUE_VOX_CHIP_OP_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass  # best-effort, cue-vox may not be running

    threading.Thread(target=_post, daemon=True).start()


# ============================================================
# STATE HELPERS
# ============================================================

def _active_state():
    """Read current chip state. Returns dict or None."""
    if not os.path.isfile(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        state = json.load(f)
    volume = state.get("volume_path", "")
    if not volume or not os.path.isdir(volume):
        return None
    return state


def _require_chip():
    """Return active state or raise with a clear message."""
    state = _active_state()
    if state is None:
        return None, "No chip active. Insert a chip and run: jeff activate /Volumes/NAME"
    return state, None


# ============================================================
# CHIP TOOLS
# ============================================================

@mcp.tool()
def chip_status():
    """Chip identity, mode, and capabilities."""
    state = _active_state()

    # Active volumes (always available, even without a chip inserted)
    vaults = _discover_active_vaults()
    active_volumes = sorted(set(v["volume"] for v in vaults))
    vault_count = len(vaults)

    result = {
        "active_volumes": active_volumes,
        "vault_count": vault_count,
    }

    if state:
        # A removable chip is inserted and active
        volume = state["volume_path"]
        hb_path = os.path.join(volume, "heartbeat.json")
        hb = {}
        try:
            if os.path.isfile(hb_path):
                hb = _safe_json(hb_path)
        except VolumeGoneError:
            result["chip"] = {"error": "Chip ejected (was %s)" % state.get("label", "?")}
            return json.dumps(result, indent=2)

        chain = hb.get("tool_chain", {})
        root = chain.get("root", "#000000")

        surface = _scan_chip_surface(volume)

        result["chip"] = {
            "label": state.get("label", "?"),
            "device_id": state.get("device_id", "?"),
            "mode": state.get("mode", "?"),
            "root_color": root,
            "band": spectral.resolve(root).get("band", "?") if root != "#000000" else "unknown",
            "model": state.get("model", ""),
            "volume": volume,
            "vault_mounted": bool(state.get("vault_mount")),
            "mount_count": hb.get("mount_count", 0),
        }
        result["surface"] = {
            "datasets": surface["datasets"],
            "models": surface["models"],
            "docs": surface["docs"],
            "schemas": surface["schemas"],
            "has_mcp": surface["has_mcp"],
            "has_sparseimage": surface["has_sparseimage"],
            "sparseimage_mb": surface["sparseimage_mb"],
            "has_search_index": surface["has_search_index"],
            "vaults_on_surface": surface["vaults"],
        }
    else:
        result["chip"] = None

    _notify_stream("status", "status checked", result)
    return json.dumps(result, indent=2)


@mcp.tool()
def chip_read_card(filename: str = ""):
    """Read a file from the card surface (no vault needed).

    Args:
        filename: File to read. Empty to list all files on the card.
    """
    state, err = _require_chip()
    if err:
        return json.dumps({"error": err})

    _notify_stream("read_card", "read: %s" % (filename or "(list)"))

    volume = state["volume_path"]

    try:
        if not filename:
            files = []
            for f in _safe_listdir(volume):
                if f.startswith(".") and f != ".jeff":
                    continue
                full = os.path.join(volume, f)
                if os.path.isfile(full):
                    files.append({"name": f, "size_bytes": os.path.getsize(full)})
                elif os.path.isdir(full):
                    files.append({"name": f + "/", "type": "directory"})
            return json.dumps({"volume": volume, "files": files}, indent=2)

        requested = os.path.normpath(os.path.join(volume, filename))
        if not requested.startswith(volume):
            return json.dumps({"error": "Path traversal denied"})

        if os.path.isdir(requested):
            entries = []
            for f in _safe_listdir(requested):
                full = os.path.join(requested, f)
                if os.path.isfile(full):
                    entries.append({"name": f, "size_bytes": os.path.getsize(full)})
                elif os.path.isdir(full):
                    entries.append({"name": f + "/", "type": "directory"})
            return json.dumps({"path": filename, "files": entries}, indent=2)

        if not os.path.isfile(requested):
            return json.dumps({"error": "File not found: %s" % filename})

        content = _safe_read(requested)
        return json.dumps({"file": filename, "content": content}, indent=2)

    except VolumeGoneError as exc:
        return json.dumps({"error": str(exc), "hint": "Chip may have been ejected"})


@mcp.tool()
def chip_tool_chain():
    """Show this chip's Merkle-VRGB tool chain and root color."""
    state, err = _require_chip()
    if err:
        return json.dumps({"error": err})

    _notify_stream("tool_chain", "tool chain verified")

    hb_path = os.path.join(state["volume_path"], "heartbeat.json")
    if os.path.isfile(hb_path):
        with open(hb_path) as f:
            hb = json.load(f)
        chain = hb.get("tool_chain", {})
        if chain:
            chain["spectral"] = spectral.resolve(chain.get("root", "#000000"))
            return json.dumps(chain, indent=2)
    return json.dumps({"error": "No tool chain found"})


# ============================================================
# SPECTRAL BINDING (host-side, no chip needed)
# ============================================================

@mcp.tool()
def chip_resolve_hex(hex_color: str):
    """Resolve a hex color to its spectral band, hue, and position."""
    return json.dumps(spectral.resolve(hex_color), indent=2)


@mcp.tool()
def chip_resolve_deep(hex_color: str, depth: int = 3):
    """Resolve a hex color through multiple zoom levels."""
    return json.dumps(spectral.resolve_deep(hex_color, depth), indent=2)


@mcp.tool()
def chip_midpoint(hex_a: str, hex_b: str):
    """Find the midpoint between two hex colors in HSL space."""
    return json.dumps(spectral.midpoint(hex_a, hex_b), indent=2)


@mcp.tool()
def chip_split_band(hex_a: str, hex_b: str, n: int = 2):
    """Split the range between two hex colors into n equal parts."""
    points = spectral.split_band(hex_a, hex_b, n)
    result = [{"hex": p, "hue": spectral.resolve(p)["hue"], "band": spectral.resolve(p)["band"]} for p in points]
    return json.dumps(result, indent=2)


@mcp.tool()
def chip_distance(hex_a: str, hex_b: str):
    """Shortest arc hue distance between two hex colors."""
    return json.dumps({"hex_a": hex_a, "hex_b": hex_b, "distance_degrees": spectral.hue_distance(hex_a, hex_b)}, indent=2)


@mcp.tool()
def chip_constellation(hex_colors: str):
    """Group hex colors by spectral band proximity.

    Args:
        hex_colors: Comma-separated hex colors.
    """
    colors = [c.strip() for c in hex_colors.split(",") if c.strip()]
    return json.dumps({
        "groups": spectral.constellation(colors),
        "summary": spectral.constellation_summary(colors),
    }, indent=2)


@mcp.tool()
def chip_registry():
    """Show the Level 0 spectral band registry."""
    return json.dumps(spectral.registry(), indent=2)


# ============================================================
# MODEL INFERENCE (delegates to chip)
# ============================================================

@mcp.tool()
def chip_query(prompt: str, system: str = "", max_tokens: int = 2048):
    """Ask the chip model a question."""
    state, err = _require_chip()
    if err:
        return json.dumps({"error": err})

    truncated = prompt[:60] + ("..." if len(prompt) > 60 else "")
    _notify_stream("query", "query: \"%s\"" % truncated)

    volume = state["volume_path"]
    if not _volume_alive(volume):
        return json.dumps({"error": "Chip ejected (was %s)" % state.get("label", "?")})

    mcp_dir = os.path.join(volume, "mcp")
    sys.path.insert(0, mcp_dir)

    try:
        import inference
        return inference.generate(
            prompt=prompt,
            system=system or "You are Jeff, a helpful assistant on a Booster Chip.",
            max_tokens=max_tokens,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        sys.path.remove(mcp_dir) if mcp_dir in sys.path else None


@mcp.tool()
def engine_run(model_ref: str, prompt: str, system: str = "", max_tokens: int = 512):
    """Run a Jeff-hosted model by reference, gated by provenance + weight hash.

    ONE parameterized endpoint for every hosted model: linking a new model adds
    a resolvable ref, never a new tool, so the MCP surface never changes (and no
    restart is needed when models come and go). On every call the model's
    provenance chain, Ed25519 signature, and weight hash are re-verified; a
    tampered, unlinked, or unsigned model returns a PROVENANCE REJECT in the
    receipt and never reaches the runtime.

    Args:
        model_ref: hosted model reference, e.g. "engine/qwen2.5-7b"
        prompt: the user prompt
        system: optional system preamble
        max_tokens: generation budget

    Returns a JSON receipt: {ok, text, byline, reason, verification}.
    """
    cue_lib = os.path.join(JEFF_DIR, "..", "..", "cue-mem", "lib")
    sys.path.insert(0, cue_lib)
    try:
        import model_host
        receipt = model_host.run(model_ref, prompt, system=system, max_tokens=max_tokens)
        return json.dumps(receipt, indent=2)
    except Exception as exc:
        return json.dumps({"ok": False, "text": None, "reason": "engine_run error: %s" % exc})
    finally:
        sys.path.remove(cue_lib) if cue_lib in sys.path else None


@mcp.tool()
def engine_sync():
    """Register the models on mounted, self-describing cards into the MCP system.

    Scans mounted volumes for a card manifest (models/manifest.json) and links
    each model (signed, provenance-gated). Call this after activating a card —
    its models then become available via engine_list / engine_run. Idempotent.
    """
    cue_lib = os.path.join(JEFF_DIR, "..", "..", "cue-mem", "lib")
    sys.path.insert(0, cue_lib)
    try:
        import model_host
        return json.dumps({"registered": model_host.sync_mounted_cards()}, indent=2)
    except Exception as exc:
        return json.dumps({"error": "engine_sync error: %s" % exc})
    finally:
        sys.path.remove(cue_lib) if cue_lib in sys.path else None


@mcp.tool()
def engine_list():
    """List the hosted models available to the MCP system, with gate status.

    Each entry is a model you can run via engine_run(model_ref, ...). Auto-syncs
    mounted cards first, so activating a card and calling this surfaces its
    models without a separate step.
    """
    cue_lib = os.path.join(JEFF_DIR, "..", "..", "cue-mem", "lib")
    sys.path.insert(0, cue_lib)
    try:
        import model_host
        model_host.sync_mounted_cards()  # pick up freshly-activated cards
        return json.dumps({"engines": model_host.available()}, indent=2)
    except Exception as exc:
        return json.dumps({"error": "engine_list error: %s" % exc})
    finally:
        sys.path.remove(cue_lib) if cue_lib in sys.path else None


@mcp.tool()
def jeff_readiness():
    """Jeff's standing assessment of the model cascade -- a curated situation report.

    Returns every model Jeff can reach, ranked best-first (runnable first, then by
    capability), the always-on C2D2 floor, the configured default, and the single
    best currently-runnable engine. Use this to curate the cascade and pick the
    strongest model on hand. (Frontier/Claude -- the Computer -- sits above this
    as an external tier Jeff does not host.)
    """
    cue_lib = os.path.join(JEFF_DIR, "..", "..", "cue-mem", "lib")
    sys.path.insert(0, cue_lib)
    try:
        import model_host
        model_host.sync_mounted_cards()  # current picture incl. freshly-mounted cards
        report = model_host.readiness()
        # Toolchains are the other half of "what can the substrate do right now":
        # the model cascade answers WHO reasons, the toolchains answer WHAT the
        # floor can reach without escalating.
        try:
            report["toolchains"] = _toolchain_summary()
        except Exception as exc:
            report["toolchains"] = {"error": str(exc)}
        return json.dumps(report, indent=2)
    except Exception as exc:
        return json.dumps({"error": "jeff_readiness error: %s" % exc})
    finally:
        sys.path.remove(cue_lib) if cue_lib in sys.path else None


def _toolchain_summary(probe=True):
    """Load the toolchain registry (core/jeff/toolchains.py)."""
    sys.path.insert(0, JEFF_DIR)
    try:
        import toolchains
        return toolchains.summary(probe=probe)
    finally:
        sys.path.remove(JEFF_DIR) if JEFF_DIR in sys.path else None


@mcp.tool()
def toolchain_list():
    """List the toolchains Jeff owns the connection to -- the capability shelves
    the system maintains (e.g. the C2D2 read shelf), with their tools, triage
    capability, endpoint, and live status. Jeff is the doorway; the reasoning
    inside each toolchain stays with the toolchain. This is what the Jeff panel
    renders and what surfaces (cue-vox, ninja) read to route C2D2-first.
    """
    try:
        return json.dumps({"toolchains": _toolchain_summary()}, indent=2)
    except Exception as exc:
        return json.dumps({"error": "toolchain_list error: %s" % exc})


@mcp.tool()
def make_list():
    """List the live Figma Make design slots Jeff can see -- each registered project's
    make-integration/registry.json, aggregated through the make-roots pointer. Returns per
    slot: name, port, URL, title, live up/down, and its pages (each a mock, with a thumbnail
    path). Jeff only holds pointers; the designs live in their own project dirs.
    """
    added = JEFF_DIR not in sys.path
    if added:
        sys.path.insert(0, JEFF_DIR)
    try:
        import make as _make
        return json.dumps({"slots": _make.aggregate()}, indent=2, default=str)
    except Exception as exc:
        return json.dumps({"error": "make_list error: %s" % exc})
    finally:
        if added and JEFF_DIR in sys.path:
            sys.path.remove(JEFF_DIR)


@mcp.tool()
def make_open(slot: str, page: str = "/"):
    """Resolve a Make slot + page to its live local URL (to drill into that mock) and its
    thumbnail path.

    Args:
        slot: A slot name from make_list (e.g. "ticketflow").
        page: A page path (e.g. "/learn-more") or its label; defaults to the home page "/".
    """
    added = JEFF_DIR not in sys.path
    if added:
        sys.path.insert(0, JEFF_DIR)
    try:
        import make as _make
        for s in _make.aggregate():
            if s["slot"] != slot:
                continue
            want = page.strip().lower()
            match = None
            for p in s["pages"]:
                if want in ("", "/") and p["path"] == "/":
                    match = p
                    break
                if p["path"].lower() == want or (p["label"] or "").lower() == want:
                    match = p
                    break
            if match is None:
                match = s["pages"][0] if s["pages"] else None
            if match is None:
                return json.dumps({"error": "slot '%s' has no pages" % slot})
            return json.dumps({
                "slot": slot, "page": match["label"], "path": match["path"],
                "url": s["url"].rstrip("/") + match["path"],
                "live": s["live"], "thumb": match["thumb"],
            }, default=str)
        return json.dumps({"error": "no such slot: %s" % slot})
    except Exception as exc:
        return json.dumps({"error": "make_open error: %s" % exc})
    finally:
        if added and JEFF_DIR in sys.path:
            sys.path.remove(JEFF_DIR)


@mcp.tool()
def chip_search(query: str, top_k: int = 5):
    """Search the chip's knowledge base.

    Args:
        query: What to search for in natural language.
        top_k: Number of results to return (default 5).
    """
    state, err = _require_chip()
    if err:
        return json.dumps({"error": err})

    truncated_q = query[:60] + ("..." if len(query) > 60 else "")
    _notify_stream("search", "search: \"%s\"" % truncated_q)

    volume = state["volume_path"]
    if not _volume_alive(volume):
        return json.dumps({"error": "Chip ejected (was %s)" % state.get("label", "?")})

    mcp_dir = os.path.join(volume, "mcp")
    index_path = os.path.join(volume, ".jeff", "index", "search.json")

    if not os.path.isfile(index_path):
        return json.dumps({"error": "No search index. Run: python3 build_index.py %s" % volume})

    sys.path.insert(0, mcp_dir)
    try:
        import inference
        from build_index import search, cosine_similarity

        query_embedding = inference.embed(query)
        if not query_embedding:
            return json.dumps({"error": "Embedding failed -- is Ollama running with nomic-embed-text?"})

        results = search(index_path, query_embedding, top_k=top_k)
        return json.dumps({"query": query, "results": results}, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        sys.path.remove(mcp_dir) if mcp_dir in sys.path else None


# ============================================================
# SEMANTIC SEARCH (host-side, C2D2 vector layer)
# ============================================================

MAESTRO_ROOT = os.path.dirname(os.path.dirname(JEFF_DIR))
C2D2_DIR = os.path.join(MAESTRO_ROOT, "tools", "c2d2")
C2D2_FALLBACK_VECTORS = os.path.join(C2D2_DIR, ".vectors")


def _resolve_sidecar(volume_path, name):
    """Find a .vectors/ sidecar for a volume. Returns (path, location) or (None, None)."""
    on_volume = os.path.join(volume_path, ".vectors", "%s.npz" % name)
    if os.path.isfile(on_volume):
        return on_volume, "volume"
    fallback = os.path.join(C2D2_FALLBACK_VECTORS, "%s.npz" % name)
    if os.path.isfile(fallback):
        return fallback, "fallback"
    return None, None


@mcp.tool()
def chip_search_semantic(query: str, volume: str = "", top_k: int = 5):
    """Semantic search over a chip's unencrypted surface via the C2D2 vector layer.

    Loads a per-volume .vectors/ sidecar built by:
        python3 core/c2d2/cli.py index-chip --volume <path>

    Args:
        query: Natural-language search query.
        volume: Volume name ("gray", "yellow") OR absolute path.
                If empty, uses the active chip.
        top_k: Number of results to return (default 5).
    """
    # Resolve volume path
    if volume:
        volume_path = _resolve_chip(volume) if not volume.startswith("/") else volume
        if not volume_path:
            return json.dumps({"error": "Volume not found: %s" % volume})
    else:
        state = _active_state()
        if state is None:
            return json.dumps({"error": "No active chip and no volume specified"})
        volume_path = state["volume_path"]

    if not _volume_alive(volume_path):
        return json.dumps({"error": "Volume gone: %s" % volume_path})

    # Sidecar lookup
    name = os.path.basename(os.path.normpath(volume_path)).lower()
    sidecar, location = _resolve_sidecar(volume_path, name)
    if not sidecar:
        return json.dumps({
            "error": "No sidecar for %s. Build one: python3 core/c2d2/cli.py index-chip --volume %s"
                     % (name, volume_path)
        })

    # Search via C2D2 vecstore
    sys.path.insert(0, C2D2_DIR)
    try:
        import vecstore
        vs = vecstore.VecStore(sidecar)
        if not vs.load():
            if vs.load_status == "corrupt":
                return json.dumps({
                    "error": "Sidecar is corrupt: %s -- rebuild: "
                             "python3 core/c2d2/cli.py index-chip --volume %s"
                             % (sidecar, volume_path)
                })
            return json.dumps({"error": "Sidecar failed to load: %s" % sidecar})
        status = vs.status()
        results = vs.search(query, top_k=top_k)
    except vecstore.VecStoreError as exc:
        # Surfaceable degradation (embed down, dimension mismatch) -- announce,
        # don't bury in a generic error. The mode line reads this.
        return json.dumps({"error": str(exc), "degraded": True})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        if C2D2_DIR in sys.path:
            sys.path.remove(C2D2_DIR)

    return json.dumps({
        "query": query,
        "volume": volume_path,
        "sidecar": sidecar,
        "sidecar_location": location,
        "built_at": status.get("built_at"),
        "age": status.get("age"),
        "stale": status.get("stale"),
        "embed_model": status.get("embed_model"),
        "results": results,
    }, indent=2)


# ============================================================
# BACKUP (host-side, manages content file backups)
# ============================================================

sys.path.insert(0, JEFF_DIR)
import backup as _backup


def _resolve_chip(chip_name):
    """Resolve chip name to directory path.

    Checks in order:
      1. Staging dir in maestro (chip-blue/, chip-yellow/, etc.)
      2. Active chip via jeff state
      3. Physical chip at /Volumes/ by label match
    """
    # Staging dir
    chip_dir = os.path.join(MAESTRO_ROOT, "chip-%s" % chip_name.lower())
    if os.path.isdir(chip_dir):
        return chip_dir
    # Active chip
    state = _active_state()
    if state and state.get("label", "").lower() == chip_name.lower():
        return state["volume_path"]
    # Physical chip at /Volumes/
    candidate = os.path.join("/Volumes", chip_name.upper())
    if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "heartbeat.json")):
        return candidate
    return None


@mcp.tool()
def chip_backup(chip_name: str):
    """Back up a chip's unencrypted surface to local volume and/or backup drive.

    Creates a timestamped zip per the chip's heartbeat backup config.
    Writes to ~/.jeff-backups (on-machine, not iCloud-synced) and any mounted
    backup-volume flash drive. FIFO rotates oldest backups.

    Args:
        chip_name: Chip color (yellow, red, blue) or label.
    """
    chip_dir = _resolve_chip(chip_name)
    if not chip_dir:
        return json.dumps({"error": "Chip not found: %s" % chip_name})
    if not _volume_alive(chip_dir):
        return json.dumps({"error": "Chip not reachable: %s" % chip_dir})

    _notify_stream("backup", "backing up %s" % chip_name)
    result = _backup.backup(chip_name.lower(), chip_dir)
    _notify_stream("backup_done", result.get("message", "backup complete"))
    return json.dumps(result, indent=2)


@mcp.tool()
def chip_backup_status(chip_name: str):
    """Show backup history across all tiers (local + backup volumes).

    Args:
        chip_name: Chip color (yellow, red, blue) or label.
    """
    return json.dumps(_backup.status(chip_name.lower()), indent=2)


@mcp.tool()
def chip_backup_rotate(chip_name: str, max_backups: int = 5):
    """Rotate old backups, keeping only the N most recent.

    Blobs are shared and never deleted during rotation -- only manifests
    are pruned. This is safe even with cross-chip deduplication.

    Args:
        chip_name: Chip color (yellow, red, blue) or label.
        max_backups: Maximum number of backup manifests to retain (default 5).
    """
    result = _backup.rotate(chip_name.lower(), max_backups)
    return json.dumps(result, indent=2)


@mcp.tool()
def chip_backup_list(chip_name: str, manifest: str = ""):
    """List files in a backup manifest.

    Shows each file, its hash, and whether the blob is available.
    Defaults to the latest backup if no manifest specified.

    Args:
        chip_name: Chip color (yellow, red, blue) or label.
        manifest: Specific manifest filename (default: latest).
    """
    result = _backup.restore_list(chip_name.lower(), manifest or None)
    return json.dumps(result, indent=2)


# ============================================================
# DYNAMIC VAULT ROUTING
# ============================================================

VOLUMES_ACTIVE_FILE = os.path.join(JEFF_DIR, ".jeff-volumes-active.json")
VOLUMES_FILE = os.path.join(JEFF_DIR, "volumes.json")


def _load_active_volumes():
    """Load active volume names."""
    if not os.path.isfile(VOLUMES_ACTIVE_FILE):
        return []
    with open(VOLUMES_ACTIVE_FILE) as f:
        return json.load(f)


def _load_registry():
    """Load volume registry, including physical chips at /Volumes/."""
    if not os.path.isfile(VOLUMES_FILE):
        registry = []
    else:
        with open(VOLUMES_FILE) as f:
            registry = json.load(f)

    # Scan /Volumes/ for physical chips
    volumes_dir = "/Volumes"
    if os.path.isdir(volumes_dir):
        known_paths = {v["path"] for v in registry}
        for name in sorted(os.listdir(volumes_dir)):
            vol = os.path.join(volumes_dir, name)
            hb = os.path.join(vol, "heartbeat.json")
            if not os.path.isfile(hb) or name == "Macintosh HD":
                continue
            if vol in known_paths:
                continue
            try:
                with open(hb) as f:
                    chip = json.load(f)
                label = chip.get("label", name)
                registry.append({
                    "name": "sd:%s" % label.lower(),
                    "type": "chip",
                    "path": vol,
                    "physical": True,
                    "label": label,
                })
            except Exception:
                continue

    return registry


def _chip_vault_mount_path(vol_path):
    """Return the mounted sparseimage path for a chip, or None."""
    # Check .chip-mount.json
    state_file = os.path.join(MAESTRO_ROOT, ".chip-mount.json")
    if os.path.isfile(state_file):
        try:
            with open(state_file) as f:
                state = json.load(f)
            if state.get("volume_path") == vol_path and state.get("vault_mount"):
                if os.path.isdir(state["vault_mount"]):
                    return state["vault_mount"]
        except Exception:
            pass

    # Fallback: check heartbeat for vault_volume_name
    hb_path = os.path.join(vol_path, "heartbeat.json")
    if os.path.isfile(hb_path):
        try:
            with open(hb_path) as f:
                hb = json.load(f)
            vol_name = hb.get("vault_volume_name", "")
            if vol_name:
                mount = os.path.join("/Volumes", vol_name)
                if os.path.isdir(mount):
                    return mount
        except Exception:
            pass

    return None


def _discover_active_vaults():
    """Discover all vaults across active volumes. Returns list of vault dicts."""
    active = set(_load_active_volumes())
    registry = _load_registry()
    vaults = []

    for vol in registry:
        if vol["name"] not in active:
            continue
        vol_path = vol["path"] if os.path.isabs(vol["path"]) else os.path.join(MAESTRO_ROOT, vol["path"])

        # Liveness check -- if volume vanished, auto-deactivate and skip
        if not _volume_alive(vol_path):
            _auto_deactivate(vol["name"])
            continue

        if vol["type"] == "local":
            # Local volume IS a vault
            db = os.path.join(vol_path, "vault.db")
            vaults.append({
                "vault": vol["name"],
                "volume": vol["name"],
                "type": "local",
                "path": vol_path,
                "db": db if os.path.isfile(db) else None,
                "env": vol.get("env", {}),
            })
        else:
            # Chip -- discover vault-* subdirs
            # Check card surface first, then mounted sparseimage
            scan_path = vol_path
            try:
                surface_entries = _safe_listdir(vol_path)
            except VolumeGoneError:
                _auto_deactivate(vol["name"])
                continue

            surface_vaults = [d for d in surface_entries
                             if os.path.isdir(os.path.join(vol_path, d))
                             and d.startswith("vault-")]

            if not surface_vaults:
                # No vaults on surface -- check mounted sparseimage
                mount_path = _chip_vault_mount_path(vol_path)
                if mount_path:
                    scan_path = mount_path

            try:
                scan_entries = _safe_listdir(scan_path)
            except VolumeGoneError:
                _auto_deactivate(vol["name"])
                continue

            for d in scan_entries:
                vault_dir = os.path.join(scan_path, d)
                if not os.path.isdir(vault_dir) or not d.startswith("vault-"):
                    continue
                db = os.path.join(vault_dir, "vault.db")
                vault_name = d.replace("vault-", "")
                vaults.append({
                    "vault": vault_name,
                    "volume": vol["name"],
                    "type": "chip",
                    "path": vault_dir,
                    "db": db if os.path.isfile(db) else None,
                })

            # If still no vaults found, use heartbeat manifest for metadata
            if not any(v["volume"] == vol["name"] for v in vaults):
                hb_path = os.path.join(vol_path, "heartbeat.json")
                if os.path.isfile(hb_path):
                    try:
                        hb = _safe_json(hb_path)
                        for hb_vault in hb.get("vaults", []):
                            vault_name = hb_vault.get("name", "")
                            if vault_name:
                                vaults.append({
                                    "vault": vault_name,
                                    "volume": vol["name"],
                                    "type": "chip",
                                    "path": "",
                                    "db": None,
                                    "sealed": True,
                                    "has_cuesheet": bool(hb_vault.get("cuesheet")),
                                })
                    except VolumeGoneError:
                        _auto_deactivate(vol["name"])
                        continue

    return vaults


def _find_vault(vault_name):
    """Find a specific vault by name across active volumes."""
    for v in _discover_active_vaults():
        if v["vault"] == vault_name:
            return v
    return None


def _vault_db_query(vault, query, params=()):
    """Run a query against a vault's database. Returns (rows, error)."""
    return _safe_db_query(vault.get("db"), query, params)


def _vault_db_execute(vault, query, params=()):
    """Execute a write query against a vault's database."""
    return _safe_db_execute(vault.get("db"), query, params)


@mcp.tool()
def chip_discover():
    """Discover all active vaults across all activated volumes.

    Call this first to see what vaults are available. Each vault
    can be queried via vault_query(vault=name, operation=...).
    """
    vaults = _discover_active_vaults()
    result = []
    for v in vaults:
        info = {
            "vault": v["vault"],
            "volume": v["volume"],
            "type": v["type"],
        }
        if v.get("db"):
            rows, err = _safe_db_query(v["db"],
                "SELECT COUNT(DISTINCT slug) as entries, COUNT(*) as images FROM vault_images")
            if rows and not err:
                info["entries"] = rows[0]["entries"]
                info["images"] = rows[0]["images"]
            else:
                info["entries"] = 0
                info["images"] = 0
                if err:
                    info["db_error"] = err
        else:
            info["entries"] = 0
            info["images"] = 0

        # Check for cue-sheet
        if v.get("sealed"):
            info["has_cuesheet"] = v.get("has_cuesheet", False)
            info["sealed"] = True
        else:
            info["has_cuesheet"] = os.path.isfile(os.path.join(v["path"], "cue-sheet.yaml"))
        result.append(info)

    # Scan physical chip surfaces for full manifest
    surfaces = {}
    registry = _load_registry()
    active_names = set(_load_active_volumes())
    for vol in registry:
        if vol["name"] not in active_names:
            continue
        if not vol.get("physical"):
            continue
        vol_path = vol["path"] if os.path.isabs(vol["path"]) else os.path.join(MAESTRO_ROOT, vol["path"])
        surface = _scan_chip_surface(vol_path)
        surfaces[vol["name"]] = {
            "datasets": surface["datasets"],
            "models": surface["models"],
            "docs": surface["docs"],
            "schemas": surface["schemas"],
            "has_mcp": surface["has_mcp"],
            "has_sparseimage": surface["has_sparseimage"],
            "has_search_index": surface["has_search_index"],
            "model_name": surface["model_name"],
        }

    output = {
        "active_volumes": sorted(set(v["volume"] for v in vaults)),
        "vaults": result,
        "operations": [
            "status", "search", "get", "list_images",
            "search_images", "filter",
            "gallery", "list_galleries", "get_gallery",
            "annotate", "get_annotations",
            "ingest",
        ],
    }
    if surfaces:
        output["chip_surfaces"] = surfaces

    return json.dumps(output, indent=2)


@mcp.tool()
def vault_query(vault: str, operation: str, slug: str = "", query: str = "",
                filename: str = "", body: str = "", title: str = "",
                slugs: str = "", tag: str = "", era: str = "",
                limit: int = 50):
    """Query any active vault dynamically. Call chip_discover first to see available vaults.

    Args:
        vault: Vault name (e.g. "hot", "cold", "rose", "finbot", "geo")
        operation: One of: status, search, get, list_images, search_images,
                   filter, gallery, list_galleries, get_gallery,
                   annotate, get_annotations, ingest
        slug: Entry slug (for get, annotate, get_annotations, gallery)
        query: Search query string (for search, search_images, gallery)
        filename: Filename (for annotate, get_annotations)
        body: Annotation body (for annotate)
        title: Gallery title (for gallery create)
        slugs: Comma-separated slugs (for gallery create)
        tag: Tag filter (for filter)
        era: Era filter (for filter)
        limit: Result limit (default 50)
    """
    v = _find_vault(vault)
    if not v:
        available = [x["vault"] for x in _discover_active_vaults()]
        return json.dumps({
            "error": "Vault '%s' not found or not active" % vault,
            "available_vaults": available,
        })

    from datetime import datetime, timezone
    utc_now = datetime.now(timezone.utc).isoformat()

    if operation == "status":
        entries = 0
        images = 0
        db_err = None
        if v.get("db"):
            rows, db_err = _vault_db_query(v,
                "SELECT COUNT(DISTINCT slug) as entries, COUNT(*) as images FROM vault_images")
            if rows and not db_err:
                entries = rows[0]["entries"]
                images = rows[0]["images"]
        result = {
            "vault": vault,
            "volume": v["volume"],
            "type": v["type"],
            "entries": entries,
            "images": images,
        }
        if db_err:
            result["db_error"] = db_err
        return json.dumps(result)

    elif operation == "search":
        pattern = "%%%s%%" % (query or "")
        rows, err = _vault_db_query(v,
            "SELECT DISTINCT slug FROM vault_images WHERE slug LIKE ? ORDER BY slug LIMIT ?",
            (pattern, int(limit)))
        if err:
            return json.dumps({"error": err})
        return json.dumps(rows)

    elif operation == "get":
        if not slug:
            return json.dumps({"error": "slug required for get"})
        rows, err = _vault_db_query(v,
            "SELECT slug, filename, port FROM vault_images WHERE slug = ? ORDER BY filename",
            (slug,))
        if err:
            return json.dumps({"error": err})
        return json.dumps(rows)

    elif operation == "list_images":
        rows, err = _vault_db_query(v,
            "SELECT slug, filename, port FROM vault_images ORDER BY slug, filename LIMIT ?",
            (int(limit),))
        if err:
            return json.dumps({"error": err})
        return json.dumps(rows)

    elif operation == "search_images":
        pattern = "%%%s%%" % (query or "")
        rows, err = _vault_db_query(v,
            "SELECT slug, filename, port FROM vault_images "
            "WHERE slug LIKE ? OR filename LIKE ? ORDER BY slug, filename LIMIT ?",
            (pattern, pattern, int(limit)))
        if err:
            return json.dumps({"error": err})
        return json.dumps(rows)

    elif operation == "filter":
        rows, err = _vault_db_query(v,
            "SELECT DISTINCT slug FROM vault_images ORDER BY slug LIMIT ?",
            (int(limit),))
        if err:
            return json.dumps({"error": err})
        return json.dumps(rows)

    elif operation == "list_galleries":
        rows, err = _vault_db_query(v,
            "SELECT slug, title, created_at FROM galleries ORDER BY created_at DESC LIMIT ?",
            (int(limit),))
        if err:
            return json.dumps({"error": err})
        return json.dumps(rows)

    elif operation == "get_gallery":
        if not slug:
            return json.dumps({"error": "slug required for get_gallery"})
        rows, err = _vault_db_query(v,
            "SELECT slug, title, images, created_at FROM galleries WHERE slug = ? LIMIT 1",
            (slug,))
        if err:
            return json.dumps({"error": err})
        if not rows:
            return json.dumps({})
        gallery = rows[0]
        raw = gallery.get("images")
        if isinstance(raw, str):
            try:
                gallery["images"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                gallery["images"] = []
        return json.dumps(gallery)

    elif operation == "gallery":
        if not title:
            return json.dumps({"error": "title required to create a gallery"})
        gal_slug = "gallery-%d" % int(datetime.now(timezone.utc).timestamp())
        images = []
        if slugs:
            for s in slugs.split(","):
                s = s.strip()
                imgs, err = _vault_db_query(v,
                    "SELECT slug, filename, port FROM vault_images WHERE slug = ? ORDER BY filename",
                    (s,))
                if not err:
                    images.extend(imgs)
        elif query:
            pattern = "%%%s%%" % query
            imgs, err = _vault_db_query(v,
                "SELECT slug, filename, port FROM vault_images "
                "WHERE slug LIKE ? OR filename LIKE ? ORDER BY slug, filename LIMIT 50",
                (pattern, pattern))
            if not err:
                images = imgs
        result = _vault_db_execute(v,
            "INSERT OR REPLACE INTO galleries (slug, title, images, created_at) VALUES (?, ?, ?, ?)",
            (gal_slug, title, json.dumps(images), utc_now))
        if result.get("error"):
            return json.dumps(result)
        return json.dumps({"slug": gal_slug, "title": title, "images": images, "created_at": utc_now})

    elif operation == "annotate":
        if not slug or not filename or not body:
            return json.dumps({"error": "slug, filename, and body required for annotate"})
        result = _vault_db_execute(v,
            "INSERT INTO vault_annotations (slug, filename, content, source, created_at) "
            "VALUES (?, ?, ?, 'mcp', ?)",
            (slug, filename, body, utc_now))
        if result.get("error"):
            return json.dumps(result)
        return json.dumps({"slug": slug, "filename": filename, "body": body, "id": result.get("lastrowid")})

    elif operation == "get_annotations":
        q = "SELECT id, slug, filename, content as body, pinned FROM vault_annotations"
        params_list = []
        filters = []
        if slug:
            filters.append("slug = ?")
            params_list.append(slug)
        if filename:
            filters.append("filename = ?")
            params_list.append(filename)
        if filters:
            q += " WHERE " + " AND ".join(filters)
        q += " ORDER BY id DESC"
        rows, err = _vault_db_query(v, q, tuple(params_list))
        if err:
            return json.dumps({"error": err})
        return json.dumps(rows)

    elif operation == "ingest":
        # Only for local vaults with ingest.py
        ingest_script = os.path.join(v["path"], "app", "ingest.py")
        if not os.path.isfile(ingest_script):
            return json.dumps({"error": "No ingest script for vault %s" % vault})
        import subprocess
        cmd = [sys.executable, ingest_script]
        if slug:
            cmd.append(slug)
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=v["path"])
        return json.dumps({"status": "ok" if result.returncode == 0 else "error",
                          "output": result.stdout.strip(),
                          "error": result.stderr.strip() if result.returncode != 0 else ""})

    else:
        return json.dumps({"error": "Unknown operation: %s" % operation,
                          "valid": ["status", "search", "get", "list_images", "search_images",
                                   "filter", "gallery", "list_galleries", "get_gallery",
                                   "annotate", "get_annotations", "ingest"]})


# ============================================================
# DYNAMIC NAMED-TOOL PROJECTION (tools-in-vaults)
# ============================================================

def _register_active_vault_tools():
    """Surface each active vault's OWN named tools on the jeff proxy.

    At startup, discover active local vaults that ship a tool CLI
    (app/mcp/server.py --list/--call), enumerate their declared tools, and
    register a forwarding wrapper for each via mcp.add_tool. The vault stays the
    single source of truth for its tools; the proxy is just the router.

    This is the first-class sibling of the generic vault_query router: instead of
    vault_query(vault="line-one", operation="intake"), the model sees the real
    named tool line_one_intake with its real parameters. Newly activated vaults
    surface on the next reconnect, since MCP clients load the tool list at
    connect time -- the honest cost of named (vs. one generic) tools.
    """
    import asyncio
    import inspect
    import subprocess

    json_py = {"string": str, "integer": int, "number": float, "boolean": bool}

    try:
        existing = {t.name for t in asyncio.run(mcp.list_tools())}
    except Exception:
        existing = set()

    for v in _discover_active_vaults():
        if v.get("type") != "local":
            continue
        server_py = os.path.join(v["path"], "app", "mcp", "server.py")
        if not os.path.isfile(server_py):
            continue

        # Opt-in guard: only probe vaults whose server.py declares the
        # --list/--call CLI. A legacy read-only vault (template server, no CLI)
        # would treat `--list` as an MCP launch and block on stdin -- so we skip
        # it here and let it keep routing through the generic vault_query. Keeps
        # proxy startup fast and never hangs on a vault that didn't opt in.
        try:
            with open(server_py, encoding="utf-8", errors="replace") as fh:
                if "--list" not in fh.read():
                    continue
        except OSError:
            continue

        try:
            listing = subprocess.run(
                [sys.executable, server_py, "--list"],
                capture_output=True, text=True, cwd=v["path"], timeout=10,
                stdin=subprocess.DEVNULL,
            )
            specs = json.loads(listing.stdout.strip() or "[]")
        except Exception as e:
            sys.stderr.write("jeff: tool listing failed for %s: %s\n" % (v["vault"], e))
            continue

        vslug = v["vault"].replace("-", "_")
        for spec in specs:
            tname = spec.get("name")
            if not tname:
                continue
            # On collision (e.g. every vault has vault_status), namespace it.
            reg_name = tname if tname not in existing else "%s_%s" % (vslug, tname)
            if reg_name in existing:
                continue

            # Mirror the vault tool's parameter schema onto the wrapper so the
            # proxy advertises real params, not an opaque kwargs blob.
            schema = spec.get("inputSchema") or {}
            props = schema.get("properties", {}) or {}
            required = set(schema.get("required", []) or [])
            req_params, opt_params = [], []
            for pname, pspec in props.items():
                ann = json_py.get((pspec or {}).get("type"), str)
                if pname in required:
                    req_params.append(inspect.Parameter(
                        pname, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann))
                else:
                    opt_params.append(inspect.Parameter(
                        pname, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        default=(pspec or {}).get("default", None), annotation=ann))

            def _make(_server_py, _cwd, _tname):
                def _forward(**kwargs):
                    res = subprocess.run(
                        [sys.executable, _server_py, "--call", _tname,
                         "--json", json.dumps(kwargs)],
                        capture_output=True, text=True, cwd=_cwd, timeout=60,
                        stdin=subprocess.DEVNULL,
                    )
                    out = res.stdout.strip()
                    if not out:
                        return json.dumps({"error": res.stderr.strip() or "forward failed"})
                    return out
                return _forward

            wrapper = _make(server_py, v["path"], tname)
            wrapper.__name__ = reg_name
            wrapper.__doc__ = spec.get("description") or ("%s · %s" % (v["vault"], tname))
            if req_params or opt_params:
                wrapper.__signature__ = inspect.Signature(req_params + opt_params)

            try:
                mcp.add_tool(wrapper, name=reg_name, description=wrapper.__doc__)
                existing.add(reg_name)
            except Exception as e:
                sys.stderr.write("jeff: could not register %s: %s\n" % (reg_name, e))


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    _register_active_vault_tools()
    mcp.run(transport="stdio")
