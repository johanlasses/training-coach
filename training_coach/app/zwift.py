"""Upload workouts into the athlete's Zwift account as custom workouts.

Zwift has no public API for this (the Training Connections API is partner-only), so this talks to the
same endpoints the game uses, as documented by zwift-offline and sauce4zwift:

- Login: Keycloak password grant at secure.zwift.com with the game's client id. Only the rotating
  refresh token is stored, never the password. A daily refresh keeps the session alive.
- Files: the game keeps custom workouts in its cloud "zfiles" store, folder ``customworkouts``,
  plus a ``workouts.files`` manifest listing them. Zwift downloads both on launch, so a new file
  needs an upload *and* a manifest entry. The workout shows up under Custom Workouts after the
  next Zwift start.
"""
from __future__ import annotations

import hashlib
import logging
import time
import xml.etree.ElementTree as ET

import httpx

from .db import DB

log = logging.getLogger(__name__)

TOKEN_URL = "https://secure.zwift.com/auth/realms/zwift/protocol/openid-connect/token"
API = "https://us-or-rly101.zwift.com/api"
CLIENT_ID = "Zwift Game Client"
COMPANION_CLIENT_ID = "Zwift_Mobile_Link"  # fallback if Zwift stops accepting the game's client for this grant
FOLDER = "customworkouts"
MANIFEST = "workouts.files"
PROTOBUF = "application/x-protobuf-lite"
HEADERS = {"Platform": "OSX", "Source": "Game Client",
           "User-Agent": "CNL/3.44.0 (Darwin Kernel 23.2.0) zwift/1.0.122968 game/1.54.0 curl/8.4.0"}


class ZwiftError(Exception):
    pass


class ZwiftClient:
    def __init__(self, db: DB, http: httpx.AsyncClient | None = None):
        self.db = db
        self.http = http or httpx.AsyncClient(timeout=30, headers=HEADERS)

    @property
    def configured(self) -> bool:
        """A refresh token that hasn't expired yet. Zwift may still revoke it; uploads report that."""
        tok = self.db.get("zwift_token") or {}
        return bool(tok.get("refresh_token")) and tok.get("refresh_expires_at", 0) > time.time()

    async def connect(self, username: str, password: str) -> dict:
        """Log in once, store tokens and the profile, return the profile. The password is not kept."""
        for client_id in (CLIENT_ID, COMPANION_CLIENT_ID):
            r = await self.http.post(TOKEN_URL, data={"client_id": client_id, "grant_type": "password",
                                                      "username": username.strip(), "password": password})
            error = _auth_error(r)
            if r.status_code < 400 or error not in ("unauthorized_client", "invalid_client"):
                break
            log.warning("Zwift login with client %r refused (%s), trying the next one", client_id, error)
        if r.status_code >= 400:
            log.warning("Zwift login failed: %s %s %s", r.status_code, error, r.text[:300])
            if error == "invalid_grant":
                raise ZwiftError("Zwift rejected the email or password")
            raise ZwiftError(f"Zwift login failed ({r.status_code}{': ' + error if error else ''})")
        self.db.set("zwift_client_id", client_id)
        self._store(r.json())
        me = await self._request("GET", "/profiles/me", headers={"Accept": "application/json"})
        profile = me.json()
        self.db.set("zwift_profile", {"id": profile.get("id"), "firstName": profile.get("firstName"),
                                      "lastName": profile.get("lastName")})
        return profile

    def disconnect(self) -> None:
        self.db.set("zwift_token", None)
        self.db.set("zwift_profile", None)

    def _store(self, data: dict) -> None:
        now = time.time()
        self.db.set("zwift_token", {
            "access_token": data["access_token"], "refresh_token": data["refresh_token"],
            "expires_at": now + data.get("expires_in", 0),
            "refresh_expires_at": now + data.get("refresh_expires_in", 0)})

    async def refresh(self) -> None:
        tok = self.db.get("zwift_token") or {}
        if not tok.get("refresh_token"):
            raise ZwiftError("Zwift is not connected — see Settings")
        client_id = self.db.get("zwift_client_id") or CLIENT_ID
        r = await self.http.post(TOKEN_URL, data={"client_id": client_id, "grant_type": "refresh_token",
                                                  "refresh_token": tok["refresh_token"]})
        if r.status_code in (400, 401):
            self.db.set("zwift_token", tok | {"refresh_expires_at": 0})  # dead session: show as disconnected
            raise ZwiftError("The Zwift login has expired — reconnect in Settings")
        r.raise_for_status()
        self._store(r.json())

    async def _token(self) -> str:
        tok = self.db.get("zwift_token") or {}
        if not (tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60):
            await self.refresh()
            tok = self.db.get("zwift_token")
        return tok["access_token"]

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._token()}", **kw.pop("headers", {})}
        r = await self.http.request(method, f"{API}{path}", headers=headers, follow_redirects=True, **kw)
        if r.status_code >= 400:
            raise ZwiftError(f"Zwift {method} {path} failed ({r.status_code})")
        return r

    async def _put_file(self, filename: str, content: bytes) -> None:
        await self._request("POST", "/zfiles", content=encode_zfile(FOLDER, filename, content),
                            headers={"Content-Type": f"{PROTOBUF}; version=2.0", "Accept": PROTOBUF})

    async def upload_workout(self, filename: str, zwo: str) -> None:
        """Store the .zwo in the athlete's custom workouts and list it in the manifest. Re-uploading the
        same filename replaces the workout."""
        content = zwo.encode("utf-8")
        await self._put_file(filename, content)
        listing = await self._request("GET", "/zfiles/list", params={"folder": FOLDER},
                                      headers={"Accept": PROTOBUF})
        entry = next((f for f in decode_zfiles(listing.content) if f.get("filename") == MANIFEST), None)
        old = (await self._request("GET", f"/zfiles/{entry['id']}/download")).content if entry else b""
        await self._put_file(MANIFEST, upsert_manifest(old, filename, content, int(time.time())))


def _auth_error(r: httpx.Response) -> str:
    """Keycloak's OAuth error code (invalid_grant, unauthorized_client, …), or '' for non-JSON bodies."""
    try:
        return str(r.json().get("error", "")) if r.status_code >= 400 else ""
    except ValueError:
        return ""


# --- workouts.files manifest --------------------------------------------------
# <custom_file_directory><custom_file><name/><time/><guid/><checksum/><deleted/></custom_file>…
# <deleted_files/></custom_file_directory>. Checksum and guid as reverse-engineered by zwift-offline.

def checksum(content: bytes) -> int:
    return (-sum(content)) % 256


def file_guid(filename: str) -> int:
    return (int.from_bytes(hashlib.sha1(filename.encode()).digest()[:4], "big") & 0x7FFFFFFF) or 1


def upsert_manifest(old: bytes, filename: str, content: bytes, now: int) -> bytes:
    """Add or replace one entry, keeping every other entry (the athlete's own workouts) untouched."""
    if old.strip():
        try:
            root = ET.fromstring(old)
        except ET.ParseError as e:
            raise ZwiftError("Zwift's workout list is in an unexpected format; not touching it") from e
        if root.tag != "custom_file_directory":
            raise ZwiftError("Zwift's workout list is in an unexpected format; not touching it")
    else:
        root = ET.Element("custom_file_directory")
    for node in root.findall("custom_file"):
        if (node.findtext("name") or "").strip() == filename:
            root.remove(node)
    node = ET.Element("custom_file")
    for tag, value in (("name", filename), ("time", now), ("guid", file_guid(filename)),
                       ("checksum", checksum(content)), ("deleted", "false")):
        ET.SubElement(node, tag).text = str(value)
    deleted = root.find("deleted_files")
    if deleted is None:
        root.append(node)
        ET.SubElement(root, "deleted_files")
    else:
        root.insert(list(root).index(deleted), node)
    ET.indent(root, space="    ")
    return ET.tostring(root, encoding="utf-8") + b"\n"


# --- minimal protobuf for ZFileProto / ZFilesProto -----------------------------
# message ZFileProto { optional uint64 id = 1; required string folder = 2; required string filename = 3;
#                      optional bytes file = 4; optional uint64 timestamp = 5; }
# message ZFilesProto { repeated ZFileProto zfiles = 1; }

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num: int, data: bytes) -> bytes:
    return _varint(num << 3 | 2) + _varint(len(data)) + data


def encode_zfile(folder: str, filename: str, content: bytes) -> bytes:
    return _field(2, folder.encode()) + _field(3, filename.encode()) + _field(4, content)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i


def _fields(buf: bytes):
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(buf, i)
        elif wire == 2:
            size, i = _read_varint(buf, i)
            val, i = buf[i:i + size], i + size
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise ZwiftError(f"unexpected protobuf wire type {wire}")
        yield num, val


def decode_zfiles(buf: bytes) -> list[dict]:
    files = []
    for num, raw in _fields(buf):
        if num != 1:
            continue
        f: dict = {}
        for n, v in _fields(raw):
            name = {1: "id", 2: "folder", 3: "filename", 5: "timestamp"}.get(n)
            if name:
                f[name] = v.decode() if isinstance(v, bytes) else v
        files.append(f)
    return files
