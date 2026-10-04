"""push —— Web Push 订阅与发送（#35，2026-10-05，手机收推送）

老板场景：在外面用手机收发消息，回话要能推到手机通知。本模块提供：
- POST /v1/push/subscribe（登录态：Bearer 或 session cookie）→ 存订阅
  （body：{"endpoint":..., "keys":{"p256dh":..., "auth":...}}）
- POST /v1/push/test（登录态）→ 给当前登录者推一条测试
- push_to_account(account_id, title, body, url=None) → 给该账号的所有订阅发推送。
  返回 {"sent": n, "failed": m}；优雅降级（返回 {"skipped": True, "reason": ...}，
  绝不抛错）：无订阅 / 环境变量没有 VAPID_PRIVATE_KEY+VAPID_PUBLIC_KEY /
  cryptography 未安装（真推需要它做 ECDH+AES-GCM+ES256）。

存储（attach 时幂等建表）：push_subscriptions(endpoint PRIMARY KEY, account_id,
keys TEXT(JSON), created_at)。

真推协议：RFC 8291（aes128gcm）+ VAPID（RFC 8292，ES256 JWT，aud=推送服务 origin，
sub 用环境变量 VAPID_SUBJECT 默认 mailto:warm@hvip.one，exp=12h）。

注入约定同其它模块：push.attach(db=, lock=, user_token=, q=, q1=, ex=, now_iso=,
tenancy=可选)；登录态解析复用 session.resolve_token(request)。main.py 挂载由 H 加。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

_S: Dict[str, object] = {}


def attach(**state) -> None:
    """main.py 启动时调用（约定同 api_v1.py）；幂等建表。"""
    _S.update(state)
    _ex(
        "CREATE TABLE IF NOT EXISTS push_subscriptions ("
        " endpoint TEXT PRIMARY KEY,"
        " account_id TEXT NOT NULL,"
        " keys TEXT NOT NULL,"
        " created_at TEXT NOT NULL)"
    )


def _q(sql: str, args=()):
    return _S["q"](sql, args)  # type: ignore[operator]


def _q1(sql: str, args=()):
    return _S["q1"](sql, args)  # type: ignore[operator]


def _ex(sql: str, args=()):
    return _S["ex"](sql, args)  # type: ignore[operator]


def _now_iso() -> str:
    return _S["now_iso"]() if "now_iso" in _S else datetime.now(timezone.utc).isoformat(timespec="seconds")  # type: ignore[operator]


def _resolve(request: Request) -> Dict[str, str]:
    """登录态：优先 session.resolve_token（Bearer + cookie），否则 401。"""
    import session
    who = session.resolve_token(request)
    if who is None:
        raise HTTPException(status_code=401, detail="未登录：Bearer token 或会话 cookie 都没有效")
    return who


# ---------------------------------------------------------------- 真推（RFC 8291 + VAPID）

def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _send_one(endpoint: str, keys: Dict[str, str], payload: bytes, ttl: int = 60) -> None:
    """对单个订阅发一条加密推送（RFC 8291 aes128gcm + VAPID JWT）。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    priv_b64 = os.environ["VAPID_PRIVATE_KEY"]
    pub_b64 = os.environ["VAPID_PUBLIC_KEY"]
    priv = serialization.load_pem_private_key(
        _b64url_decode(priv_b64) + b"\n", password=None)
    ua_pub = _b64url_decode(keys["p256dh"])
    ua_point = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    local_priv = ec.generate_private_key(ec.SECP256R1())
    local_pub = local_priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = local_priv.exchange(ec.ECDH(), ua_point)
    auth = _b64url_decode(keys["auth"])
    salt = os.urandom(16)

    def expand(info: bytes, n: int) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=n, salt=auth, info=info).derive(shared)

    ikm = expand(b"WebPush: info\x00" + ua_pub + local_pub, 32)
    cek = expand(b"Content-Encoding: aes128gcm\x00", 16)
    nonce = expand(b"Content-Encoding: nonce\x00", 12)
    rs = b"\x00\x00\x10\x00"                                    # record size 4096
    header = local_pub + salt + rs + b"\x00"                    # 无 keyid：idlen=0
    context = (b"P-256\x00" + b"\x00"                            # label + 无 keyid
               + b"\x00\x41" + ua_pub                            # 接收者公钥（65B）
               + b"\x00\x41" + local_pub                         # 发送者公钥（65B）
               + b"\x00\x00" + rs)                               # 内容头：rs
    ciphertext = AESGCM(cek).encrypt(nonce, payload, header + context)

    # VAPID JWT（ES256，claims: aud=origin, exp=+12h, sub）
    from urllib.parse import urlparse
    aud = f"{urlparse(endpoint).scheme}://{urlparse(endpoint).netloc}"
    header_jwt = _b64url_encode(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = {"aud": aud, "exp": int(time.time()) + 12 * 3600,
              "sub": os.environ.get("VAPID_SUBJECT", "mailto:warm@hvip.one")}
    payload_jwt = _b64url_encode(json.dumps(claims).encode())
    signing_input = f"{header_jwt}.{payload_jwt}".encode()
    der = local_priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = _b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    jwt = f"{header_jwt}.{payload_jwt}.{sig}"
    pub = _b64url_encode(
        local_priv.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

    req = urllib.request.Request(
        endpoint, data=header + ciphertext, method="POST",
        headers={"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                 "TTL": str(ttl), "Authorization": f"vapid t={jwt}, k={pub}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.status  # 2xx 即成功；非 2xx urlopen 会抛异常


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ---------------------------------------------------------------- 发送入口（降级不抛错）

def push_to_account(account_id: str, title: str, body: str, url: Optional[str] = None) -> Dict[str, object]:
    """给账号的所有订阅发推送。返回 {"sent":n,"failed":m}；
    无订阅/无 VAPID key/cryptography 未装 → {"skipped":True,"reason":...}，不抛错。"""
    subs = _q("SELECT endpoint, keys FROM push_subscriptions WHERE account_id = ?", (account_id,))
    if not subs:
        return {"skipped": True, "reason": "no subscriptions"}
    if not (os.environ.get("VAPID_PRIVATE_KEY") and os.environ.get("VAPID_PUBLIC_KEY")):
        return {"skipped": True, "reason": "no VAPID key（没配 VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY，降级不推）"}
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return {"skipped": True, "reason": "cryptography 未安装（真推需要它）"}
    payload = json.dumps({"title": title, "body": body, "url": url or "",
                          "ts": _now_iso()}, ensure_ascii=False).encode()
    sent = failed = 0
    for s in subs:
        try:
            keys = json.loads(s["keys"])
            _send_one(s["endpoint"], keys, payload)
            sent += 1
        except Exception as e:
            failed += 1
            _S.get("log", lambda *a: None)(f"push fail {s['endpoint']}: {e}")  # type: ignore[operator]
    return {"sent": sent, "failed": failed}


# ---------------------------------------------------------------- 路由

class SubscribeIn(BaseModel):
    endpoint: str
    keys: Dict[str, str]


@router.post("/v1/push/subscribe")
def v1_push_subscribe(body: SubscribeIn, request: Request) -> Dict[str, object]:  # type: ignore[assignment]
    """登录态存订阅（按 endpoint upsert）。"""
    who = _resolve(request)
    endpoint = (body.endpoint or "").strip()
    keys = body.keys or {}
    if not endpoint or "p256dh" not in keys or "auth" not in keys:
        raise HTTPException(status_code=400, detail="endpoint 与 keys.p256dh/auth 都必填")
    _ex(
        "INSERT INTO push_subscriptions (endpoint, account_id, keys, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(endpoint) DO UPDATE SET account_id=excluded.account_id, keys=excluded.keys",
        (endpoint, who["id"], json.dumps(keys, ensure_ascii=False), _now_iso()),
    )
    return {"ok": True, "endpoint": endpoint, "account_id": who["id"]}


@router.post("/v1/push/test")
def v1_push_test(request: Request) -> Dict[str, object]:  # type: ignore[assignment]
    """给当前登录者推一条测试（不抛错，降级返回 skipped）。"""
    who = _resolve(request)
    return push_to_account(who["id"], "温暖通信台", "push 测试：通了", None)
