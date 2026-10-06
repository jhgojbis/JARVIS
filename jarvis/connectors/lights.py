"""IKEA lights via the Trådfri Gateway's local CoAP/DTLS API (port 5684), spoken through libcoap's `coap-client` (brew install libcoap)."""
from __future__ import annotations
import json, os, shutil, subprocess, threading, time
from pathlib import Path
from ..config import Config

IDENTITY = "jarvis"
COAP = shutil.which("coap-client") or "/opt/homebrew/bin/coap-client"   # launchd has a short PATH
LOCK = threading.Lock()                       # the gateway serves one DTLS session at a time
TTL = 300                                     # seconds the room/lamp layout is remembered
_cache: dict = {"at": 0.0, "lights": []}
OPENSSL_CONF = str(Path(__file__).with_name("tradfri-openssl.cnf"))


def _coap(method: str, host: str, path: str, user: str, key: str, body: dict | None = None) -> object:
    cmd = [COAP, "-m", method, "-u", user, "-k", key, "-B", "6"]
    if body is not None:
        cmd += ["-e", json.dumps(body)]
    for attempt in (1, 2):                        # the gateway sometimes drops a handshake; once more is enough
        with LOCK:
            r = subprocess.run(cmd + [f"coaps://{host}:5684/{path}"], capture_output=True, text=True, timeout=12,
                               env=dict(os.environ, OPENSSL_CONF=OPENSSL_CONF))
        out = r.stdout.strip()
        if not r.returncode and (out.startswith(("{", "[")) or (method != "get" and not out.startswith(("Oct", "ERR")))):
            return json.loads(out) if out.startswith(("{", "[")) else out
    raise RuntimeError(f"coap {method} {path} failed: {(r.stderr or out)[-200:]}")


def _get(cfg: Config, path: str):
    return _coap("get", cfg["lights"]["host"], path, IDENTITY, Config.env("TRADFRI_KEY"))


def pair(host: str, security_code: str) -> str:
    """One-time: trades the Security Code from the underside of the gateway for a personal key."""
    r = _coap("post", host, "15011/9063", "Client_identity", security_code, {"9090": IDENTITY})
    if not isinstance(r, dict) or "9091" not in r:
        raise RuntimeError(f"the gateway did not return a key: {r!r}")
    return r["9091"]


def devices(cfg: Config, fresh: bool = False) -> list[dict]:
    """Every light, reduced to what Jarvis needs: id, name, room (the gateway's group), on/off, brightness (0-100).
    Reading the gateway takes about a second per lamp, so the result is remembered for TTL seconds."""
    if not fresh and _cache["lights"] and time.time() - _cache["at"] < TTL:
        return _cache["lights"]
    _cache.update(at=time.time(), lights=_read(cfg))
    return _cache["lights"]


def _read(cfg: Config) -> list[dict]:
    room_of = {}
    for gid in _get(cfg, "15004"):
        g = _get(cfg, f"15004/{gid}")
        if g.get("9001") == "SuperGroup":                    # the gateway's built-in "everything" group, not a room
            continue
        for d in g.get("9018", {}).get("15002", {}).get("9003", []):
            room_of[d] = g.get("9001", "")
    out = []
    for did in _get(cfg, "15001"):
        d = _get(cfg, f"15001/{did}")
        if "3311" not in d:                                  # remotes, repeaters and blinds are not lights
            continue
        a = d["3311"][0]
        out.append({"id": did, "name": d.get("9001") or str(did), "room": room_of.get(did, ""), "on": bool(a.get("5850")),
                    "level": round(a.get("5851", 0) * 100 / 254) if "5851" in a else None, "dimmable": "5851" in a, "online": bool(d.get("9019", 1))})
    return out


def pick(lights: list[dict], target: str) -> list[dict]:
    """Lights whose room or name matches `target` ('all'/'everything'/empty = every light)."""
    t = (target or "").strip().lower()
    if t in ("", "all", "everything", "everywhere", "alla"):
        return lights
    return [l for l in lights if t == l["room"].lower() or t == l["name"].lower()] or \
           [l for l in lights if t in l["room"].lower() or t in l["name"].lower()]


def set_lights(cfg: Config, target: str, on: bool | None = None, brightness: int | None = None) -> list[str]:
    """Switch and/or dim the matching lights; returns their names. Raises LookupError when nothing matches."""
    hit = pick(devices(cfg), target)
    if not hit:
        raise LookupError(f"no light matches {target!r}")
    hit = [l for l in hit if l["online"]] or hit         # an unreachable lamp cannot be switched: skip it unless nothing else matches
    done, error = [], None
    for l in hit:
        attrs = {}
        if on is not None:
            attrs["5850"] = int(on)
        if brightness is not None and l["dimmable"]:
            attrs["5851"] = round(max(1, min(100, brightness)) * 254 / 100)
            attrs.setdefault("5850", 1)
        if not attrs:
            continue
        try:                                             # one stubborn lamp must not stop the others
            _coap("put", cfg["lights"]["host"], f"15001/{l['id']}", IDENTITY, Config.env("TRADFRI_KEY"), {"3311": [attrs]})
        except RuntimeError as e:
            error = e
            continue
        l["on"] = bool(attrs.get("5850", l["on"]))       # keep the remembered state in step
        if "5851" in attrs:
            l["level"] = round(attrs["5851"] * 100 / 254)
        done.append(l["name"])
    if error and not done:
        raise error
    return done


def status_text(cfg: Config) -> str:
    rooms: dict[str, list[str]] = {}
    for l in devices(cfg):
        rooms.setdefault(l["room"] or "no room", []).append(f"{l['name']} ({('on, ' + str(l['level']) + '%' if l['on'] and l['level'] else 'on') if l['on'] else 'off'}{'' if l['online'] else ', offline'})")
    return "; ".join(f"{r}: {', '.join(v)}" for r, v in rooms.items()) or "no lights found"
