"""Live link to Unity MCB: streams the edited target meshes so Unity shows them at once (nothing saved there).

The wire protocol is specified with Unity MCB's BlenderLiveProtocol (Editor/Services/BlenderLive). In short: TCP to
127.0.0.1 on the port of the session's live.json (or the Magic Sync payload), little-endian frames
"MCBL", version, type, token, payload. Blender sends Hello, a Rest frame per target mesh (the positions Unity's mesh was
made from), Positions about 200 ms after the last edit, Topology when vertices were added or removed; Unity answers with
Status frames and asks for an export with Commit.
"""
import array
import itertools
import json
import os
import select
import socket
import struct
import time
import zlib

import bmesh
import bpy
from bpy.app.handlers import persistent

from . import exporter
from .sync import ADDON_VERSION, get_settings, get_sync_session

LIVE_PROTOCOL_VERSION = 1
MAGIC = 0x4C42434D
HEADER = struct.Struct("<IBBHI")
HELLO, REST, POSITIONS, TOPOLOGY, STATUS, COMMIT, BYE = range(1, 8)
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
SEND_DELAY_SECONDS = 0.2
CONNECTED_TICK_SECONDS = 0.05
IDLE_TICK_SECONDS = 0.5
RECONNECT_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)
CONNECT_TIMEOUT_SECONDS = 3.0


class _Link:
    """The one live connection of this Blender (module state: it is not saved with the file)."""

    def __init__(self):
        self.sock = None
        self.connected = False
        self.connect_started = 0.0
        self.session_key = None
        self.token = b""
        self.port = 0
        self.inbuf = bytearray()
        self.outbuf = bytearray()
        self.failures = 0
        self.next_attempt = 0.0
        self.state = "off"
        self.rest = {}          # mesh id -> positions Unity's mesh was made from
        self.sent = {}          # mesh id -> crc of the positions Unity has (rest or last Positions)
        self.topology = set()   # mesh ids whose vertex count differs from their rest
        self.edited = set()     # mesh ids whose positions differ from their rest
        self.pending = {}       # object name -> time to send its positions
        self.status = {}        # mesh id -> {"live": bool, "message": str} from Unity
        self.commit_requested = False


_link = _Link()


def link_state():
    """(state, port, {mesh id: status}) for the panel."""
    return _link.state, _link.port, dict(_link.status)


def mesh_id(obj):
    """The id Unity resolves: the renderer Unity knows the mesh as, else the object (FBX node) name."""
    value = obj.get(exporter.TARGET_RENDERER_PROP, "")
    return value if isinstance(value, str) and value else obj.name


def base_positions(obj):
    """The base shape in the mesh's local coordinates (what the FBX export writes)."""
    data = obj.data
    positions = array.array("f", bytes(12 * len(data.vertices)))
    data.vertices.foreach_get("co", positions)
    return positions


def edited_positions(obj):
    """The layer being edited: the active shape key in Edit or Sculpt mode on a non-reference key, else the base shape."""
    data = obj.data
    if obj.mode == "EDIT":
        # The edit mesh holds the shape being edited (the active key's, or the base one).
        edit_mesh = bmesh.from_edit_mesh(data)
        return array.array("f", itertools.chain.from_iterable(vertex.co for vertex in edit_mesh.verts))
    keys = data.shape_keys
    index = obj.active_shape_key_index
    if obj.mode == "SCULPT" and keys is not None and 0 < index < len(keys.key_blocks):
        block = keys.key_blocks[index]
        if block != keys.reference_key:
            positions = array.array("f", bytes(12 * len(block.data)))
            block.data.foreach_get("co", positions)
            return positions
    return base_positions(obj)


def _crc(positions):
    return (len(positions), zlib.crc32(positions))


def _frame(frame_type, payload=b""):
    return HEADER.pack(MAGIC, LIVE_PROTOCOL_VERSION, frame_type, len(_link.token), len(payload)) + _link.token + payload


def _mesh_payload(identifier, positions):
    name = identifier.encode("utf-8")
    return struct.pack("<H", len(name)) + name + struct.pack("<I", len(positions) // 3) + positions.tobytes()


def _topology_payload(identifier, vertex_count):
    name = identifier.encode("utf-8")
    return struct.pack("<H", len(name)) + name + struct.pack("<I", vertex_count)


def _queue(frame_type, payload=b""):
    _link.outbuf += _frame(frame_type, payload)


def _settings():
    return get_settings(bpy.context)


def _endpoint(session):
    """Unity's live port: live.json in the inbox (rewritten after Unity's script reloads), else the sync payload's."""
    inbox = session.get("inboxPath") or ""
    try:
        with open(os.path.join(inbox, "live.json"), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if (data.get("kind") == "orbiters.mcb.blenderLive" and data.get("protocolVersion") == LIVE_PROTOCOL_VERSION
                and data.get("sessionId") == session.get("sessionId") and int(data.get("port", 0)) > 0):
            return "127.0.0.1", int(data["port"])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    live = session.get("live") if isinstance(session.get("live"), dict) else {}
    if live.get("protocolVersion") == LIVE_PROTOCOL_VERSION and int(live.get("port") or 0) > 0:
        return "127.0.0.1", int(live["port"])
    return None


def target_meshes(context, session):
    meshes, _missing = exporter.find_export_meshes(context, session)
    return meshes


def close(reason="off", send_bye=True):
    if _link.sock is not None:
        if send_bye and _link.connected:
            try:
                _link.sock.setblocking(True)
                _link.sock.settimeout(0.2)
                _link.sock.sendall(_frame(BYE))
            except OSError:
                pass
        try:
            _link.sock.close()
        except OSError:
            pass
    _link.sock = None
    _link.connected = False
    _link.inbuf.clear()
    _link.outbuf.clear()
    _link.state = reason
    _link.status.clear()
    _link.commit_requested = False


def _forget_session():
    close(send_bye=True)
    _link.session_key = None
    _link.rest.clear()
    _link.sent.clear()
    _link.topology.clear()
    _link.edited.clear()
    _link.pending.clear()
    _link.failures = 0
    _link.next_attempt = 0.0


def _start_connect(session):
    endpoint = _endpoint(session)
    if endpoint is None:
        _link.state = "no live port from Unity"
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.connect_ex(endpoint)
    _link.sock = sock
    _link.port = endpoint[1]
    _link.connect_started = time.monotonic()
    _link.state = "connecting"


def _redraw():
    window_manager = getattr(bpy.context, "window_manager", None)
    for window in getattr(window_manager, "windows", None) or []:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _connect_failed(reason="waiting for Unity"):
    close(reason, send_bye=False)
    _redraw()
    delay = RECONNECT_DELAYS[min(_link.failures, len(RECONNECT_DELAYS) - 1)]
    _link.failures += 1
    _link.next_attempt = time.monotonic() + delay


def _finish_connect(context, session):
    _, writable, errored = select.select([], [_link.sock], [_link.sock], 0)
    if errored or (writable and _link.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) != 0):
        _connect_failed()
        return
    if not writable:
        if time.monotonic() - _link.connect_started > CONNECT_TIMEOUT_SECONDS:
            _connect_failed()
        return
    _link.connected = True
    _link.failures = 0
    _link.state = "connected"
    _redraw()
    hello = {"sessionId": session.get("sessionId", ""), "blender": bpy.app.version_string, "addon": ADDON_VERSION}
    _queue(HELLO, json.dumps(hello).encode("utf-8"))
    for obj in target_meshes(context, session):
        _send_rest(obj, keep_existing=True)
        _send_positions(obj)


def _send_rest(obj, keep_existing=False):
    identifier = mesh_id(obj)
    if not keep_existing or identifier not in _link.rest:
        _link.rest[identifier] = base_positions(obj)
    rest = _link.rest[identifier]
    _link.topology.discard(identifier)
    _link.edited.discard(identifier)
    _link.sent[identifier] = _crc(rest)
    _queue(REST, _mesh_payload(identifier, rest))


def _send_positions(obj):
    identifier = mesh_id(obj)
    rest = _link.rest.get(identifier)
    if rest is None or identifier in _link.topology:
        return
    positions = edited_positions(obj)
    if len(positions) != len(rest):
        _link.topology.add(identifier)
        _link.edited.add(identifier)
        _queue(TOPOLOGY, _topology_payload(identifier, len(positions) // 3))
        return
    crc = _crc(positions)
    if _link.sent.get(identifier) == crc:
        return
    _link.sent[identifier] = crc
    if crc == _crc(rest):
        _link.edited.discard(identifier)
    else:
        _link.edited.add(identifier)
    _queue(POSITIONS, _mesh_payload(identifier, positions))


def _flush():
    while _link.outbuf:
        try:
            sent = _link.sock.send(_link.outbuf)
        except BlockingIOError:
            return
        except OSError:
            _connect_failed()
            return
        del _link.outbuf[:sent]


def _receive():
    while True:
        readable, _, _ = select.select([_link.sock], [], [], 0)
        if not readable:
            break
        try:
            data = _link.sock.recv(65536)
        except BlockingIOError:
            break
        except OSError:
            data = b""
        if not data:
            _connect_failed()
            return
        _link.inbuf += data

    while len(_link.inbuf) >= HEADER.size:
        magic, version, frame_type, token_length, payload_length = HEADER.unpack_from(_link.inbuf)
        if magic != MAGIC or version != LIVE_PROTOCOL_VERSION or payload_length > MAX_PAYLOAD_BYTES:
            _connect_failed()
            return
        end = HEADER.size + token_length + payload_length
        if len(_link.inbuf) < end:
            return
        token = bytes(_link.inbuf[HEADER.size:HEADER.size + token_length])
        payload = bytes(_link.inbuf[HEADER.size + token_length:end])
        del _link.inbuf[:end]
        if token != _link.token:
            _connect_failed("Unity answered with another session's token")
            return
        _on_frame(frame_type, payload)


def _on_frame(frame_type, payload):
    if frame_type == STATUS:
        try:
            status = json.loads(payload.decode("utf-8"))
            _link.status[status["meshId"]] = {"live": bool(status.get("live")), "message": str(status.get("message", ""))}
        except (ValueError, KeyError, TypeError):
            return
        _redraw()
    elif frame_type == COMMIT:
        _link.commit_requested = True
    elif frame_type == BYE:
        _connect_failed("Unity closed the live link; reconnecting")


def _commit(context, settings):
    """Unity's Commit: the usual export of the meshes edited since their rest (all target meshes when none was)."""
    session = get_sync_session(settings)
    names = [obj.name for obj in target_meshes(context, session) if mesh_id(obj) in _link.edited]
    ok, message = exporter._send_to_unity(context, requested_mesh_names=names or None)
    settings.last_status = ("Committed from Unity: " if ok else "Commit from Unity failed: ") + message
    print("[MCB] " + settings.last_status)


def _on_export(context, mesh_objects):
    """After any export: those meshes are what Unity will show, so they are the new rest."""
    if not _link.connected:
        for obj in mesh_objects:
            _link.rest.pop(mesh_id(obj), None)
        return
    for obj in mesh_objects:
        _send_rest(obj)


def tick():
    """Connects, reads Unity's frames and sends due positions. Runs on Blender's main thread (a timer)."""
    context = bpy.context
    settings = _settings()
    session = get_sync_session(settings) if settings is not None else None
    if settings is None or session is None or not settings.live_preview:
        if _link.session_key is not None or _link.sock is not None:
            _forget_session()
        _link.state = "off"
        return IDLE_TICK_SECONDS

    key = (session.get("sessionId", ""), session.get("token", ""))
    if key != _link.session_key:
        _forget_session()
        _link.session_key = key
        _link.token = key[1].encode("utf-8")

    if _link.sock is None:
        if time.monotonic() >= _link.next_attempt:
            _start_connect(session)
        return CONNECTED_TICK_SECONDS if _link.sock is not None else IDLE_TICK_SECONDS
    if not _link.connected:
        _finish_connect(context, session)
        if _link.connected:
            _flush()
        return CONNECTED_TICK_SECONDS

    _receive()
    if not _link.connected:
        return IDLE_TICK_SECONDS
    if _link.commit_requested:
        _link.commit_requested = False
        _commit(context, settings)
    if _link.pending and not exporter.is_exporting():
        now = time.monotonic()
        due = [name for name, when in _link.pending.items() if when <= now]
        if due:
            targets = {obj.name: obj for obj in target_meshes(context, session)}
            for name in due:
                del _link.pending[name]
                if name in targets:
                    _send_positions(targets[name])
    _flush()
    return CONNECTED_TICK_SECONDS


def _tick_timer():
    try:
        return tick()
    except Exception as exc:  # A timer that raises is dropped by Blender: keep the link alive.
        print("[MCB] Live link error: " + str(exc))
        return IDLE_TICK_SECONDS


@persistent
def _depsgraph_update_handler(scene, depsgraph):
    if not _link.connected or exporter.is_exporting() or scene is None:
        return
    due = time.monotonic() + SEND_DELAY_SECONDS
    for name in exporter.updated_mesh_names(scene, depsgraph):
        _link.pending[name] = due


def register():
    if _depsgraph_update_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_update_handler)
    if _on_export not in exporter.EXPORT_LISTENERS:
        exporter.EXPORT_LISTENERS.append(_on_export)
    if not bpy.app.timers.is_registered(_tick_timer):
        bpy.app.timers.register(_tick_timer, first_interval=IDLE_TICK_SECONDS, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick_timer):
        bpy.app.timers.unregister(_tick_timer)
    if _on_export in exporter.EXPORT_LISTENERS:
        exporter.EXPORT_LISTENERS.remove(_on_export)
    if _depsgraph_update_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_update_handler)
    _forget_session()
