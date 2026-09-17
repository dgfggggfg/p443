# ┞━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ┃ 🦅 EAGLE Panel — Railway MINI  ·  تک‌فایل بک‌اند
# ┃ بخش ۱: اتصال به API ریلوی (کارت اعتبار ۵ دلاری)
# ┃ بخش ۲: رله‌ی XHTTP (packet-up / stream-up)
# ┃ بخش ۳: هسته‌ی پنل (API + رله‌ی VLESS-WS + صفحات)
# ┞━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
railway_api.py — اتصال به API عمومی ریلوی (GraphQL)
نمایش اعتبار حساب Railway (کارت «Railway Balance» داشبورد)

اسکیمای واقعی با introspection از backboard.railway.com/graphql/v2 استخراج شده:
  me { email name workspaces { id name plan createdAt
        customer { creditBalance currentUsage trialDaysRemaining
                   billingPeriod { start end } usageLimit { hardLimit softLimit } } } }
  workspaceUsageTotals(workspaceId, startDate, endDate, measurements) {
    measurement value }   → NETWORK_TX_GB / NETWORK_RX_GB / DISK_USAGE_GB
"""

import asyncio
import time
import logging
from datetime import datetime, timedelta

import httpx

rw_logger = logging.getLogger("railway-api")

RAILWAY_GQL = "https://backboard.railway.com/graphql/v2"

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}

IDENTITY_QUERY = """
query {
  me {
    id
    email
    name
    username
    createdAt
    workspaces {
      id
      name
      plan
      createdAt
      customer {
        creditBalance
        currentUsage
        appliedCredits
        remainingUsageCreditBalance
        trialDaysRemaining
        isTrialing
        hasExhaustedFreePlan
        state
        billingPeriod { start end }
        usageLimit { hardLimit softLimit }
      }
    }
  }
}
"""

USAGE_QUERY = """
query Usage($ws: String!, $start: DateTime!, $end: DateTime!) {
  workspaceUsageTotals(
    workspaceId: $ws
    startDate: $start
    endDate: $end
    measurements: [NETWORK_TX_GB, NETWORK_RX_GB, DISK_USAGE_GB]
  ) {
    measurement
    value
  }
}
"""

# کش کوتاه تا با هر رفرش داشبورد به API ریلوی فشار نیاید
_CACHE: dict = {}
CACHE_TTL = 60  # ثانیه

# قیمت تقریبی ترافیک ریلوی برای تخمین «ترافیکی که می‌خرد» (هر گیگ بین ۵ تا ۱۰ سنت)
PRICE_PER_GB_MIN = 0.10
PRICE_PER_GB_MAX = 0.05
TRIAL_DAYS = 30


async def _gql(token: str, query: str, variables: dict | None = None) -> dict:
    """اجرای یک درخواست GraphQL با توکن کاربر"""
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(
            RAILWAY_GQL,
            json={"query": query, "variables": variables or {}},
            headers=headers,
        )
        if resp.status_code == 401 or resp.status_code == 403:
            raise PermissionError("توکن ریلوی نامعتبر است یا منقضی شده")
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            msgs = "; ".join(e.get("message", "?") for e in data["errors"][:3])
            raise RuntimeError(f"خطای GraphQL: {msgs}")
        return data.get("data") or {}


def _pick_workspace(me: dict) -> dict:
    """انتخاب ورک‌اسپیس اصلی (اولین ورک‌اسپیس شخصی)"""
    ws_list = me.get("workspaces") or []
    if not ws_list:
        return {}
    for ws in ws_list:
        if ws.get("customer") and ws["customer"].get("isTrialing"):
            return ws
    return ws_list[0]


async def fetch_credit_summary(token: str) -> dict:
    """
    خلاصه‌ی اعتبار حساب ریلوی — دقیقاً همان داده‌های کارت Railway Balance:
      remaining_credit, days_remaining, spent_this_cycle, used_bytes,
      used_per_day, egress_bytes, account, plan, cycle_start/cycle_end
    """
    now = time.time()
    key = f"credit:{token[:12]}"
    hit = _CACHE.get(key)
    if hit and now - hit["t"] < CACHE_TTL:
        return hit["data"]

    try:
        me_data = await _gql(token, IDENTITY_QUERY)
        me = (me_data or {}).get("me") or {}
        if not me:
            raise RuntimeError("پاسخ خالی از ریلوی")

        ws = _pick_workspace(me)
        customer = (ws.get("customer") or {})
        ws_id = ws.get("id", "")

        # بازه‌ی سیکل جاری (اگر ریلوی نداد: از ابتدای ماه)
        cycle_start = datetime.now() - timedelta(days=30)
        cycle_end = datetime.now()
        bp = customer.get("billingPeriod") or {}
        try:
            if bp.get("start"):
                cycle_start = datetime.fromisoformat(str(bp["start"]).replace("Z", "+00:00"))
            if bp.get("end"):
                cycle_end = datetime.fromisoformat(str(bp["end"]).replace("Z", "+00:00"))
        except Exception:
            pass
        if not bp.get("start"):
            cycle_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        days_in_cycle = max(1, (cycle_end - cycle_start).days)
        days_elapsed = max(1, min(days_in_cycle, (datetime.now() - cycle_start).days + 1))

        # مصرف منابع سیکل جاری (بایت / گیگابایت)
        used_bytes = 0
        egress_bytes = 0
        try:
            usage = await _gql(token, USAGE_QUERY, {
                "ws": ws_id,
                "start": cycle_start.isoformat(),
                "end": datetime.now().isoformat(),
            })
            for row in (usage or {}).get("workspaceUsageTotals") or []:
                m = str(row.get("measurement", ""))
                v = float(row.get("value") or 0)
                if "NETWORK_TX" in m:
                    egress_bytes += v * 1024 ** 3
                elif "NETWORK_RX" in m:
                    egress_bytes += 0  # فقط خروجی (egress) شمرده می‌شود
                used_bytes += v * 1024 ** 3
        except Exception as e:
            rw_logger.warning(f"usage query failed: {e}")

        credit_balance = float(customer.get("creditBalance") or 0)
        current_usage = float(customer.get("currentUsage") or 0)
        trial_days = customer.get("trialDaysRemaining")
        is_trial = bool(customer.get("isTrialing"))

        if trial_days is not None:
            days_remaining = int(trial_days)
        else:
            days_remaining = max(0, days_in_cycle - days_elapsed)

        spent = current_usage if current_usage else 0.0
        spent_per_day = spent / days_elapsed
        used_per_day = used_bytes / days_elapsed if used_bytes else 0

        gb_can_buy_min = credit_balance / PRICE_PER_GB_MIN if PRICE_PER_GB_MIN else 0
        gb_can_buy_max = credit_balance / PRICE_PER_GB_MAX if PRICE_PER_GB_MAX else 0

        def _fmt_gb(b: float) -> str:
            return f"{b / 1024 ** 3:.2f} GB"

        result = {
            "ok": True,
            "account": me.get("email") or me.get("username") or "",
            "account_name": me.get("name") or "",
            "workspace": ws.get("name") or "",
            "plan": str(ws.get("plan") or "FREE"),
            "is_trial": is_trial,
            "state": str(customer.get("state") or "ACTIVE"),
            "exhausted": bool(customer.get("hasExhaustedFreePlan")),
            "remaining_credit": round(credit_balance, 2),
            "remaining_credit_fmt": f"${credit_balance:.2f}",
            "days_remaining": days_remaining,
            "spent_this_cycle": round(spent, 2),
            "spent_this_cycle_fmt": f"${spent:.2f}",
            "spent_per_day_fmt": f"${spent_per_day:.2f}/d",
            "cycle_start": cycle_start.isoformat(),
            "cycle_end": cycle_end.isoformat(),
            "used_bytes": int(used_bytes),
            "used_fmt": _fmt_gb(used_bytes),
            "used_per_day_fmt": f"{_fmt_gb(used_per_day)}/d" if used_bytes else "0 B/d",
            "egress_bytes": int(egress_bytes),
            "egress_fmt": f"{egress_bytes / 1024 ** 3:.2f} GB out",
            "traffic_buys": f"{gb_can_buy_min:.0f}-{max(gb_can_buy_min, gb_can_buy_max):.0f} GB"
                            if credit_balance > 0 else "0 GB",
            "hard_limit": (customer.get("usageLimit") or {}).get("hardLimit"),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }

        _CACHE[key] = {"t": now, "data": result}
        return result

    except PermissionError as e:
        return {"ok": False, "reason": "invalid_token", "error": str(e)}
    except Exception as e:
        rw_logger.error(f"railway credit failed: {e}")
        return {"ok": False, "reason": "api_error", "error": str(e)}


async def verify_token(token: str) -> dict:
    """فقط بررسی هویت حساب برای تأیید توکن"""
    try:
        data = await _gql(token, "query { me { email name username } }")
        me = (data or {}).get("me") or {}
        return {"ok": bool(me), "account": me.get("email", ""), "name": me.get("name", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── سازگاری با فراخوانی‌های پنل (main قبلاً get_credit_summary صدا می‌زد) ──
get_credit_summary = fetch_credit_summary



import asyncio
import secrets
import socket
import time
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse


xhttp_router = APIRouter()

XHTTP_BUF = 512 * 1024
DOWNLINK_QUEUE_MAX = 512
SESSION_IDLE_TIMEOUT = 30
REAPER_INTERVAL = 10
TCP_CONNECT_TIMEOUT = 10.0

# ── تنظیمات موتور تطبیقی ──────────────────────────────────────────────────────
SOCK_BUF_SIZE = 2 * 1024 * 1024     # SO_SNDBUF / SO_RCVBUF

# _AdaptiveFlow: بازه‌ی مجاز برای high-water تطبیقی (AIMD)
FLOW_MIN_HW = 256 * 1024
FLOW_MAX_HW = 16 * 1024 * 1024
FLOW_START_HW = 2 * 1024 * 1024
FLOW_FAST_DRAIN_MS = 2.0    # زیر این یعنی downstream خیلی سریعه → بافر مجاز رو زیاد کن
FLOW_SLOW_DRAIN_MS = 25.0   # بالای این یعنی backpressure واقعی → فوری نصفش کن

# _QuotaGate: بازه‌ی مجاز برای batch تطبیقی چک کوتا
QUOTA_MIN_BATCH = 32 * 1024
QUOTA_MAX_BATCH = 1 * 1024 * 1024
QUOTA_START_BATCH = 64 * 1024
QUOTA_CHECK_INTERVAL = 0.2  # سقف زمانی؛ حتی اگر batch پر نشده، بعد این مدت چک کن

PACKET_UP_HIGH_WATER = 2 * 1024 * 1024  # packet-up همون منطق ساده‌ی قبلی رو داره (تمرکز این راند فقط stream-up بود)

xhttp_sessions: dict = {}
XHTTP_LOCK = asyncio.Lock()

XHTTP_RESP_HEADERS = {
    "chrome": {
        "content-type": "application/grpc",
        "cache-control": "no-cache, no-store",
        "x-accel-buffering": "no",
        "server": "cloudflare",
    },
    "plain": {
        "content-type": "application/octet-stream",
        "cache-control": "no-store",
        "x-accel-buffering": "no",
    },
}
XHTTP_DEFAULT_FP = "chrome"


def _resp_headers(fp: str) -> dict:
    return dict(XHTTP_RESP_HEADERS.get(fp, XHTTP_RESP_HEADERS[XHTTP_DEFAULT_FP]))


def _tune_socket(writer: asyncio.StreamWriter):
    """TCP_NODELAY + بافرهای بزرگ‌تر سوکت برای کاهش سربار سیستم‌عامل روی ترافیک بالا."""
    sock = writer.transport.get_extra_info("socket")
    if not sock:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF_SIZE)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF_SIZE)
    except OSError:
        pass


class _QuotaGate:
    """
    نسخه‌ی تطبیقی: به‌جای await check_and_use() به‌ازای هر چانک، و به‌جای یک آستانه‌ی
    ثابت، نرخ واقعی ترافیک هر سشن رو با EWMA اندازه می‌گیره و اندازه‌ی batch رو زنده
    عوض می‌کنه:
      - سشن پرسرعت (دانلود حجیم) → batch بزرگ می‌شه → await های سنگین کمتر.
      - سشن کم‌ترافیک/تعاملی → batch کوچیک می‌مونه → کوتا دقیق‌تر و قطع سریع‌تر
        اگه کاربر تموم کرده باشه.
    داده هیچ‌وقت نگه داشته نمی‌شه، فقط لحظه‌ی چک‌کردنِ کوتا adaptive هست.
    """
    __slots__ = ("uuid", "pending", "last_check", "ok", "batch_bytes", "rate_ewma")

    def __init__(self, uuid: str):
        self.uuid = uuid
        self.pending = 0
        self.last_check = time.monotonic()
        self.ok = True
        self.batch_bytes = QUOTA_START_BATCH
        self.rate_ewma = 0.0

    async def add(self, nbytes: int) -> bool:
        if not self.ok:
            return False
        self.pending += nbytes
        now = time.monotonic()
        elapsed = now - self.last_check
        if self.pending >= self.batch_bytes or elapsed >= QUOTA_CHECK_INTERVAL:
            flush, self.pending = self.pending, 0
            if elapsed > 0:
                inst_rate = flush / elapsed
                self.rate_ewma = inst_rate if self.rate_ewma == 0 else (0.7 * self.rate_ewma + 0.3 * inst_rate)
                target = int(self.rate_ewma * QUOTA_CHECK_INTERVAL)
                self.batch_bytes = max(QUOTA_MIN_BATCH, min(QUOTA_MAX_BATCH, target or QUOTA_MIN_BATCH))
            self.last_check = now
            self.ok = await check_and_use(self.uuid, flush)
            return self.ok
        return True

    async def flush(self) -> bool:
        if self.pending:
            flush, self.pending = self.pending, 0
            self.ok = self.ok and await check_and_use(self.uuid, flush)
        return self.ok


class _AdaptiveFlow:
    """
    high-water تطبیقی برای drain(), رفتار شبیه AIMD در TCP congestion control:
      - هر بار drain() صدا زده می‌شه، مدت زمانش اندازه‌گیری می‌شه.
      - اگه سریع تموم بشه (لینک پایین‌دستی داره جواب می‌ده) → سقف بافر مجاز رو
        additive increase می‌کنیم؛ یعنی دفعه‌ی بعد دیرتر drain صدا زده می‌شه،
        پس syscall/context-switch کمتر می‌شه و throughput واقعی بالا می‌ره.
      - اگه drain کند بشه (backpressure واقعیه، صف داره جمع می‌شه) → سقف رو فوری
        نصف می‌کنیم (multiplicative decrease) تا بافربلوت/لتنسی رشد نکنه.
    هر سشن یک نمونه‌ی جدا از این داره، پس مسیرهای کند و سریع تداخلی با هم ندارن.
    """
    __slots__ = ("high_water", "last_drain_ms")

    def __init__(self):
        self.high_water = FLOW_START_HW
        self.last_drain_ms = 0.0

    def should_drain(self, buf_size: int) -> bool:
        return buf_size > self.high_water

    async def drain(self, writer: asyncio.StreamWriter):
        t0 = time.monotonic()
        await writer.drain()
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.last_drain_ms = elapsed_ms
        if elapsed_ms < FLOW_FAST_DRAIN_MS:
            self.high_water = min(FLOW_MAX_HW, int(self.high_water * 1.5) + 65536)
        elif elapsed_ms > FLOW_SLOW_DRAIN_MS:
            self.high_water = max(FLOW_MIN_HW, self.high_water // 2)


def _req_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "نامشخص"


async def _open_tcp_from_header(first_chunk: bytes):
    command, address, port, payload = await parse_vless_header(first_chunk)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(address, port), timeout=TCP_CONNECT_TIMEOUT
    )
    _tune_socket(writer)
    if payload:
        writer.write(payload)
        await writer.drain()
    return reader, writer, address, port


async def _check_link(uuid: str):
    async with LINKS_LOCK:
        link = LINKS.get(uuid)
    if not is_link_allowed(link):
        raise HTTPException(status_code=403, detail="not authorized")


async def _get_or_create_session(uuid: str, mode: str, session_id: str, ip: str = "نامشخص") -> dict:
    """Session بر اساس session_id که خودِ کلاینت در URL فرستاده، lazily ساخته می‌شه."""
    async with XHTTP_LOCK:
        sess = xhttp_sessions.get(session_id)
        if sess is not None:
            sess["last_seen"] = time.time()
            return sess
        conn_id = secrets.token_urlsafe(6)
        connections[conn_id] = {
            "uuid": uuid,
            "ip": ip,
            "connected_at": datetime.now().isoformat(),
            "bytes": 0,
            "transport": f"xhttp-{mode}",
        }
        sess = {
            "uuid": uuid, "mode": mode, "writer": None,
            "downlink_task": None, "uplink_task": None,
            "down_q": asyncio.Queue(maxsize=DOWNLINK_QUEUE_MAX),
            "last_seen": time.time(),
            "conn_id": conn_id, "tcp_open": False, "closed": False,
            "seq_buf": {}, "next_seq": 0,
            "gate": None,  # لازی ساخته می‌شه: _QuotaGate تطبیقی مخصوص stream-up
            "flow": None,  # لازی ساخته می‌شه: _AdaptiveFlow مخصوص stream-up
        }
        xhttp_sessions[session_id] = sess
        logger.info(f"new XHTTP[{mode}] session [{session_id[:8]}] uuid={uuid[:8]} ip={ip}")
        return sess


async def _teardown(session_id: str):
    async with XHTTP_LOCK:
        sess = xhttp_sessions.pop(session_id, None)
    if not sess:
        return
    sess["closed"] = True
    for t in ("uplink_task", "downlink_task"):
        task = sess.get(t)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    writer = sess.get("writer")
    if writer:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
    connections.pop(sess.get("conn_id"), None)
    dq = sess.get("down_q")
    if dq:
        try:
            dq.put_nowait(None)
        except Exception:
            pass
    logger.info(f"closed XHTTP[{sess.get('mode')}] [{session_id[:8]}] total={len(xhttp_sessions)}")


async def _reaper():
    while True:
        await asyncio.sleep(REAPER_INTERVAL)
        now = time.time()
        async with XHTTP_LOCK:
            stale = [sid for sid, s in xhttp_sessions.items()
                     if now - s["last_seen"] > SESSION_IDLE_TIMEOUT and not s.get("tcp_open")]
        for sid in stale:
            await _teardown(sid)


_reaper_started = False


def ensure_reaper():
    global _reaper_started
    if not _reaper_started:
        asyncio.create_task(_reaper())
        _reaper_started = True


async def _pump_tcp_to_queue(session_id: str, uuid: str, reader: asyncio.StreamReader, down_q: asyncio.Queue):
    first = True
    gate = _QuotaGate(uuid)  # دانلینک هم از همون گیت batched استفاده می‌کنه
    try:
        while True:
            data = await reader.read(XHTTP_BUF)
            if not data:
                break
            if not await gate.add(len(data)):
                break
            async with XHTTP_LOCK:
                sess = xhttp_sessions.get(session_id)
            if sess:
                c = connections.get(sess["conn_id"])
                if c:
                    c["bytes"] += len(data)
            payload = (b"\x00\x00" + data) if first else data
            first = False
            await down_q.put(payload)
    except (asyncio.CancelledError, Exception):
        pass
    finally:
        await gate.flush()
        await _teardown(session_id)


async def _open_tcp_for_session(session_id: str, uuid: str, sess: dict, first_chunk: bytes):
    """تونل TCP رو از روی هدر VLESS باز می‌کنه و پمپ دانلینک رو راه می‌اندازه."""
    reader, writer, address, port = await _open_tcp_from_header(first_chunk)
    logger.info(f"connect XHTTP[{sess['mode']}] [{session_id[:8]}] -> {address}:{port}")
    sess["writer"] = writer
    sess["tcp_open"] = True
    sess["downlink_task"] = asyncio.create_task(
        _pump_tcp_to_queue(session_id, uuid, reader, sess["down_q"])
    )
    asyncio.create_task(save_state())


def _downstream_gen(sess: dict):
    async def gen():
        try:
            while True:
                chunk = await sess["down_q"].get()
                if chunk is None:
                    break
                sess["last_seen"] = time.time()
                yield chunk
        finally:
            pass
    return gen()


# ══════════════════════════════ GET دانلینک (مشترک بین سه مد) ══════════════════════════════
@xhttp_router.get("/xhttp-siz10/{mode}/{uuid}/{session_id}")
async def xhttp_downlink(mode: str, uuid: str, session_id: str, request: Request):
    ensure_reaper()
    if mode not in ("packet-up", "stream-up"):
        raise HTTPException(status_code=404, detail="unknown mode")
    await _check_link(uuid)
    fp = request.query_params.get("fp", XHTTP_DEFAULT_FP)
    sess = await _get_or_create_session(uuid, mode, session_id, _req_client_ip(request))
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")

    headers = _resp_headers(fp)
    return StreamingResponse(_downstream_gen(sess), headers=headers, media_type=headers["content-type"])


# ══════════════════════════════ PACKET-UP (آپلینک با seq) ══════════════════════════════
@xhttp_router.post("/xhttp-siz10/packet-up/{uuid}/{session_id}/{seq}")
async def packet_up_upload(uuid: str, session_id: str, seq: int, request: Request):
    ensure_reaper()
    sess = await _get_or_create_session(uuid, "packet-up", session_id, _req_client_ip(request))
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")

    sess["last_seen"] = time.time()
    body = await request.body()
    if not body:
        return {"ok": True}

    if not await check_and_use(uuid, len(body)):
        await _teardown(session_id)
        raise HTTPException(status_code=403, detail="quota/disabled/unknown")

    stats["total_requests"] += 1
    connections[sess["conn_id"]]["bytes"] += len(body)

    try:
        if sess["writer"] is None:
            # اولین پکتی که حاوی هدر VLESS است، می‌تونه seq=0 نباشه اگر پکت‌ها
            # خارج از ترتیب برسن؛ بافر کوچیک برای سورت کردن seqهای زودرس.
            if seq != 0:
                sess["seq_buf"][seq] = body
                return {"ok": True, "buffered": True}
            await _open_tcp_for_session(session_id, uuid, sess, body)
            # هر پکت بافرشده‌ای که حالا نوبتش رسیده رو هم بفرست
            nxt = 1
            while nxt in sess["seq_buf"]:
                pending = sess["seq_buf"].pop(nxt)
                sess["writer"].write(pending)
                nxt += 1
            sess["next_seq"] = nxt
            return {"ok": True, "connected": True}

        if seq == sess["next_seq"]:
            sess["writer"].write(body)
            sess["next_seq"] += 1
            while sess["next_seq"] in sess["seq_buf"]:
                pending = sess["seq_buf"].pop(sess["next_seq"])
                sess["writer"].write(pending)
                sess["next_seq"] += 1
        else:
            sess["seq_buf"][seq] = body

        if sess["writer"].transport.get_write_buffer_size() > PACKET_UP_HIGH_WATER:
            await sess["writer"].drain()
    except Exception as exc:
        error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        await _teardown(session_id)
        raise HTTPException(status_code=502, detail="write failed")

    return {"ok": True}


# ══════════════════════════════ STREAM-UP (یک POST پیوسته) ══════════════════════════════
# موتور تطبیقی: _QuotaGate (batch کوتا بر اساس نرخ واقعی) + _AdaptiveFlow (AIMD روی
# high-water درین) + کش رفرنس‌ها داخل لوپ. هیچ داده‌ای بافر/coalesce نمی‌شه —
# هر بایت فوری write() می‌شه، فقط «کِی صبر کنیم برای drain» تطبیقیه.
@xhttp_router.post("/xhttp-siz10/stream-up/{uuid}/{session_id}")
async def stream_up_upload(uuid: str, session_id: str, request: Request):
    ensure_reaper()
    sess = await _get_or_create_session(uuid, "stream-up", session_id, _req_client_ip(request))
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")

    gate = sess.get("gate")
    if gate is None:
        gate = _QuotaGate(uuid)
        sess["gate"] = gate

    flow = sess.get("flow")
    if flow is None:
        flow = _AdaptiveFlow()
        sess["flow"] = flow

    conn = connections[sess["conn_id"]]   # یک بار لوک‌آپ، نه هر چانک
    writer = sess["writer"]               # ممکنه هنوز None باشه

    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            sess["last_seen"] = time.time()

            if not await gate.add(len(chunk)):
                raise HTTPException(status_code=403, detail="quota/disabled/unknown")

            stats["total_requests"] += 1
            conn["bytes"] += len(chunk)

            if writer is None:
                await _open_tcp_for_session(session_id, uuid, sess, chunk)
                writer = sess["writer"]
                continue

            writer.write(chunk)
            if flow.should_drain(writer.transport.get_write_buffer_size()):
                await flow.drain(writer)
    except HTTPException:
        await gate.flush()
        await _teardown(session_id)
        raise
    except Exception as exc:
        error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        await gate.flush()
        await _teardown(session_id)
        raise HTTPException(status_code=502, detail="stream error")

    await gate.flush()
    return {"ok": True}


import asyncio
import json
import os
import hashlib
import secrets
import time
import aiofiles
import psutil
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
from collections import deque, defaultdict
from pathlib import Path
import socket
import base64

from fastapi import FastAPI, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import httpx
import logging

# ─── تنظیمات ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("Persepolis-Gateway")

IRAN_TZ = ZoneInfo("Asia/Tehran")

# ─── کانفیگ ──────────────────────────────────────────────────────────────────
CONFIG = {
    "port": int(os.environ.get("PORT", 8000)),
    "secret": os.environ.get("SECRET_KEY", secrets.token_urlsafe(32)),
    "host": os.environ.get("RAILWAY_PUBLIC_DOMAIN", os.environ.get("RENDER_EXTERNAL_URL", "localhost")),
}

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "P443"
DEFAULT_ADMIN_PASSWORD = "P443"  # رمز پیش‌فرض نصب — بعد از تغییر رمز، راهنمای آبی از صفحه لاگین حذف می‌شود
PANEL_VERSION = "v10.9"  # 🎨 طراحی جدید صفحه ساب/اطلاعات (استایل PXpanel: حلقه مصرف، جزئیات فنی، دانلود برنامه‌ها)

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="🏛️ Persepolis Gateway v14", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── State ────────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_FILE = DATA_DIR / "persepolis_state.json"
SAVE_LOCK = asyncio.Lock()

# ─── Web (فایل‌های استاتیک UI) ──────────────────────────────────────────────
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_DIR_EARLY = WEB_DIR

# ─── In-Memory State ─────────────────────────────────────────────────────────
LINKS: dict = {}
LINKS_LOCK = asyncio.Lock()
SUBS: dict = {}
SUBS_LOCK = asyncio.Lock()
connections: dict = {}
stats = {
    "total_bytes": 0,
    "total_requests": 0,
    "total_errors": 0,
    "start_time": time.time(),
}
error_logs: deque = deque(maxlen=50)
activity_logs: deque = deque(maxlen=200)
hourly_traffic: dict = defaultdict(int)
hourly_traffic_history: dict = defaultdict(lambda: defaultdict(int))
# ⚡ سقف مصرف ۱۰۰ گیگابایت — دائمی، هیچ‌وقت ریست نمی‌شود؛ اخطار در ۹۹ گیگ
QUOTA_LIMIT_GB = 100
QUOTA_LIMIT_BYTES = QUOTA_LIMIT_GB * 1024 ** 3
QUOTA_WARN_BYTES = 99 * 1024 ** 3
quota_usage: dict = {"bytes": 0, "warned_99": False}
device_connections: dict = {}
DEVICE_CONNECTIONS_LOCK = asyncio.Lock()
http_client: httpx.AsyncClient | None = None
_tg_sess: dict = {}  # جلسات گفتگوی ربات تلگرام
_tg_offset = 0          # آفست long-polling ربات
_tg_poll_task: "asyncio.Task | None" = None  # تسک polling ربات

# ─── Auth ──────────────────────────────────────────────────────────────────────
SESSION_COOKIE = "persepolis_session"
SESSION_TTL = 60 * 60 * 24 * 7
SESSIONS: dict = {}
SESSIONS_LOCK = asyncio.Lock()

# ─── Settings ──────────────────────────────────────────────────────────────────
SETTINGS: dict = {
    "rgb_mode": False,
    "default_protocol": "vless-ws",
    "language": "fa",
    "theme": "dark",
}

# ─── پروتکل‌های پشتیبانی شده ──────────────────────────────────────────────────
PROTOCOLS = {
    "vless-ws": {"name": "VLESS-WS", "icon": "🚀", "type": "ws", "alpn": "h2,http/1.1"},
    "vless-grpc": {"name": "VLESS-gRPC", "icon": "⚡", "type": "grpc", "alpn": "h2"},
    "vless-xhttp": {"name": "VLESS-XHTTP", "icon": "🛡️", "type": "xhttp", "alpn": "h2,http/1.1"},
    "vless-http2": {"name": "VLESS-HTTP/2", "icon": "📶", "type": "tcp", "alpn": "h2"},
    "trojan-ws": {"name": "Trojan-WS", "icon": "🔒", "type": "ws", "alpn": "h2,http/1.1"},
    "shadowsocks": {"name": "Shadowsocks", "icon": "🌊", "type": "tcp", "alpn": ""},
}

DEFAULT_PROTOCOL = "vless-ws"
DEFAULT_PORT = 443

# ─── HTTP نسخه‌ها ────────────────────────────────────────────────────────────
HTTP_VERSIONS = {
    "h1": {"name": "HTTP/1.1", "alpn": "http/1.1"},
    "h2": {"name": "HTTP/2", "alpn": "h2"},
    "h3": {"name": "HTTP/3 (QUIC)", "alpn": "h3"},
    "auto": {"name": "Auto", "alpn": "h2,http/1.1"},
}

# ─── انگشت‌نگاری ──────────────────────────────────────────────────────────────
FINGERPRINTS = {
    "chrome": "🌐 Chrome",
    "firefox": "🦊 Firefox",
    "safari": "🧭 Safari",
    "edge": "🌊 Edge",
    "ios": "📱 iOS",
    "android": "🤖 Android",
    "safari_ios": "🍏 Safari iOS",
    "random": "🎲 Random",
    "none": "🚫 None"
}

# ─── Functions ─────────────────────────────────────────────────────────────────

def now_ir() -> datetime:
    return datetime.now(IRAN_TZ)

def generate_uuid() -> str:
    h = secrets.token_hex(16)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

def get_host() -> str:
    return os.environ.get("RAILWAY_PUBLIC_DOMAIN", os.environ.get("RENDER_EXTERNAL_URL", CONFIG["host"]))

def fmt_bytes(b: int) -> str:
    if not b or b == 0:
        return "0 B"
    if b < 1024:
        return f"{b} B"
    if b < 1024**2:
        return f"{b/1024:.1f} KB"
    if b < 1024**3:
        return f"{b/1024**2:.2f} MB"
    if b < 1024**4:
        return f"{b/1024**3:.2f} GB"
    return f"{b/1024**4:.2f} TB"

def fmt_bytes_short(b: int) -> tuple:
    if not b or b == 0:
        return ("0", "B")
    if b < 1024:
        return (str(b), "B")
    if b < 1024**2:
        return (f"{b/1024:.1f}", "KB")
    if b < 1024**3:
        return (f"{b/1024**2:.2f}", "MB")
    if b < 1024**4:
        return (f"{b/1024**3:.2f}", "GB")
    return (f"{b/1024**4:.2f}", "TB")

def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "نامشخص"

def uptime() -> str:
    secs = int(time.time() - stats["start_time"])
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def parse_size_to_bytes(value: float, unit: str) -> int:
    unit = unit.upper()
    if unit == "GB":
        return int(value * 1024 ** 3)
    if unit == "MB":
        return int(value * 1024 ** 2)
    if unit == "KB":
        return int(value * 1024)
    return int(value)

def is_link_expired(link: dict) -> bool:
    exp = link.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.now() > datetime.fromisoformat(exp)
    except Exception:
        return False

def is_link_allowed(link: dict | None) -> bool:
    if link is None:
        return False
    if not link.get("active", True):
        return False
    if is_link_expired(link):
        return False
    lb = link.get("limit_bytes", 0)
    if lb > 0 and link.get("used_bytes", 0) >= lb:
        return False
    return True

def generate_vless_link(uuid: str, host: str, remark: str = "", protocol: str = DEFAULT_PROTOCOL, 
                        fingerprint: str = "chrome", port: int = DEFAULT_PORT, 
                        sni: str = None, http_version: str = "h2", fake_port: bool = False) -> str:
    if not remark:
        remark = "تختجمشید"
    
    if not sni:
        sni = host
    
    if fake_port:
        port = 1
    
    proto_info = PROTOCOLS.get(protocol, PROTOCOLS["vless-ws"])
    proto_type = proto_info["type"]
    alpn = HTTP_VERSIONS.get(http_version, HTTP_VERSIONS["h2"])["alpn"]
    
    params = {
        "encryption": "none",
        "security": "tls",
        "sni": sni,
        "fp": fingerprint,
        "alpn": alpn,
    }
    
    if proto_type == "ws":
        params["type"] = "ws"
        params["host"] = host
        params["path"] = f"/{proto_type}/{uuid}"
        if http_version == "h3":
            params["quic"] = "true"
            
    elif proto_type == "grpc":
        params["type"] = "grpc"
        params["serviceName"] = f"{uuid}.service"
        if http_version == "h3":
            params["quic"] = "true"
            
    elif proto_type == "xhttp":
        mode = protocol.replace("vless-", "").replace("trojan-", "")
        params["type"] = "xhttp"
        params["mode"] = mode
        params["host"] = host
        params["path"] = f"/xhttp-siz10/{mode}/{uuid}"
        if http_version == "h3":
            params["quic"] = "true"
            
    elif proto_type == "tcp":
        params["type"] = "tcp"
        if protocol == "shadowsocks":
            params["type"] = "ss"
            params["method"] = "chacha20-ietf-poly1305"
            params["password"] = secrets.token_urlsafe(16)
    
    if protocol == "trojan-ws":
        params["type"] = "ws"
        params["host"] = host
        params["path"] = f"/trojan/{uuid}"
        params["password"] = secrets.token_urlsafe(16)
    
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    
    if protocol == "trojan-ws":
        return f"trojan://{params['password']}@{host}:{port}?{query}#{quote(remark)}"
    elif protocol == "shadowsocks":
        return f"ss://{quote(f'{params['method']}:{params['password']}')}@{host}:{port}#{quote(remark)}"
    else:
        return f"vless://{uuid}@{host}:{port}?{query}#{quote(remark)}"

def log_activity(kind: str, message: str, level: str = "info"):
    activity_logs.append({
        "kind": kind,
        "level": level,
        "message": message,
        "time": datetime.now().isoformat(),
    })

async def remove_device_connection(uuid: str, client_ip: str):
    async with DEVICE_CONNECTIONS_LOCK:
        if uuid in device_connections:
            if client_ip in device_connections[uuid]:
                device_connections[uuid].remove(client_ip)
                if not device_connections[uuid]:
                    del device_connections[uuid]

# ─── Session Functions ──────────────────────────────────────────────────────

async def create_session() -> str:
    token = secrets.token_urlsafe(32)
    async with SESSIONS_LOCK:
        SESSIONS[token] = time.time() + SESSION_TTL
    return token

async def get_session_info(token: str | None) -> dict | None:
    if not token:
        return None
    async with SESSIONS_LOCK:
        expiry = SESSIONS.get(token)
        if expiry is None:
            return None
        if expiry < time.time():
            SESSIONS.pop(token, None)
            return None
        return {"expiry": expiry}

async def is_valid_session(token: str | None) -> bool:
    return await get_session_info(token) is not None

async def destroy_session(token: str | None):
    if not token:
        return
    async with SESSIONS_LOCK:
        SESSIONS.pop(token, None)

async def require_auth(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not await is_valid_session(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    return token

# ─── State Persistence ──────────────────────────────────────────────────────

async def load_state():
    global LINKS, SUBS, SETTINGS, hourly_traffic_history, hourly_traffic, ADMIN_USERNAME, ADMIN_PASSWORD
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if DATA_FILE.exists():
            async with aiofiles.open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
            data = json.loads(raw)
            LINKS.update(data.get("links", {}))
            SUBS.update(data.get("subs", {}))
            if "settings" in data:
                SETTINGS.update(data["settings"])
                if SETTINGS.get("admin_username"):
                    ADMIN_USERNAME = SETTINGS["admin_username"]
                if SETTINGS.get("admin_password"):
                    ADMIN_PASSWORD = SETTINGS["admin_password"]
            if "hourly_traffic" in data:
                hourly_traffic = defaultdict(int, data["hourly_traffic"])
            if isinstance(data.get("quota_usage"), dict):
                quota_usage["bytes"] = int(data["quota_usage"].get("bytes", 0) or 0)
                quota_usage["warned_99"] = bool(data["quota_usage"].get("warned_99", False))
            elif isinstance(data.get("daily_traffic"), dict):
                # مهاجرت از نسخه‌ی قبلی (شمارنده‌ی روزانه) — مصرف قبلی حفظ می‌شود
                quota_usage["bytes"] = int(data["daily_traffic"].get("bytes", 0) or 0)
            if "hourly_traffic_history" in data:
                hist = data["hourly_traffic_history"]
                hourly_traffic_history = defaultdict(lambda: defaultdict(int))
                for day, hours in hist.items():
                    hourly_traffic_history[day] = defaultdict(int, hours)
            logger.info(f"📂 State loaded: {len(LINKS)} links, {len(SUBS)} subs")
    except Exception as e:
        logger.warning(f"Could not load state: {e}")

async def save_state():
    async with SAVE_LOCK:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            
            hist_dict = {}
            for day, hours in hourly_traffic_history.items():
                hist_dict[day] = dict(hours)
            
            data = {
                "links": dict(LINKS),
                "subs": dict(SUBS),
                "settings": SETTINGS,
                "hourly_traffic": dict(hourly_traffic),
                "hourly_traffic_history": hist_dict,
                "quota_usage": dict(quota_usage),
                "saved_at": datetime.now().isoformat(),
            }
            tmp = DATA_FILE.with_suffix(".tmp")
            async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(DATA_FILE)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")

# ─── Startup / Shutdown ─────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global http_client
    limits = httpx.Limits(max_connections=500, max_keepalive_connections=100)
    timeout = httpx.Timeout(30.0, connect=10.0)
    http_client = httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=True)
    await load_state()
    # 🤖 اگر ربات قبلاً فعال بوده، polling را دوباره شروع کن (webhook قبلی هم پاک می‌شود)
    _tg = SETTINGS.get("telegram") or {}
    if _tg.get("enabled") and _tg.get("token"):
        asyncio.create_task(_tg_safe_webhook_clear(_tg["token"]))
        _tg_start_poll()
    
    log_activity("system", "🏛️ Persepolis Gateway v14 راه‌اندازی شد", "ok")
    logger.info(f"🏛️ Persepolis Gateway v14 started on port {CONFIG['port']}")

@app.on_event("shutdown")
async def shutdown():
    await save_state()
    if http_client:
        await http_client.aclose()

# ─── API: Settings ─────────────────────────────────────────────────────────

@app.post("/api/settings/language")
async def set_language(request: Request, _=Depends(require_auth)):
    body = await request.json()
    lang = body.get("language", "fa")
    if lang in ["fa", "en"]:
        SETTINGS["language"] = lang
        await save_state()
        return {"ok": True, "language": lang}
    raise HTTPException(status_code=400, detail="زبان نامعتبر")

@app.get("/api/language")
async def get_language():
    return {"language": SETTINGS.get("language", "fa")}

@app.post("/api/settings/theme")
async def set_theme(request: Request, _=Depends(require_auth)):
    body = await request.json()
    theme = body.get("theme", "obsidian")
    if theme == "light":
        theme = "white"  # مهاجرت نام قدیم
    if theme == "dark":
        theme = "cosmic"  # مهاجرت نام قدیم (مثل UI)
    if theme in ["obsidian", "cosmic", "bumblebee", "white"]:
        SETTINGS["theme"] = theme
        await save_state()
        return {"ok": True, "theme": theme}
    raise HTTPException(status_code=400, detail="تم نامعتبر")

@app.get("/api/settings")
async def get_settings(_=Depends(require_auth)):
    s = dict(SETTINGS)
    tg = s.get("telegram")
    if tg:
        s["telegram"] = {k: tg.get(k) for k in ("chat_id", "bot_username", "enabled")}
    s.pop("admin_password", None)
    return s

@app.post("/api/settings/rgb")
async def toggle_rgb(request: Request, _=Depends(require_auth)):
    body = await request.json()
    SETTINGS["rgb_mode"] = bool(body.get("enabled", False))
    await save_state()
    return {"rgb_mode": SETTINGS["rgb_mode"]}

# ─── API: پینگ/نسخه (همگام با پنل کلادفلری) ─────────────────────────────────
@app.get("/api/ping")
async def api_ping():
    return {"ok": True, "v": PANEL_VERSION, "t": int(time.time() * 1000)}


@app.get("/api/stats")
async def api_stats_hourly(_=Depends(require_auth)):
    return {"hourly": dict(hourly_traffic)}


# ─── API: تغییر اعتبارنامه ادمین ─────────────────────────────────────────────
@app.post("/api/settings/credentials")
async def set_credentials(request: Request, _=Depends(require_auth)):
    global ADMIN_USERNAME, ADMIN_PASSWORD
    body = await request.json()
    username = (body.get("username") or "").strip()
    new_password = body.get("new_password") or ""
    current_password = body.get("current_password") or ""
    if current_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=400, detail="رمز فعلی اشتباه است")
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="نام کاربری حداقل ۳ کاراکتر")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="رمز جدید حداقل ۶ کاراکتر")
    ADMIN_USERNAME = username
    ADMIN_PASSWORD = new_password
    SETTINGS["admin_username"] = username
    SETTINGS["admin_password"] = new_password
    await save_state()
    log_activity("auth", "اعتبارنامه ادمین از تنظیمات تغییر کرد", "warn")
    return {"ok": True}


# ─── ربات تلگرام v10.7 — کنترل کامل پنل (رمز پنل، QR، گروه‌های اشتراک، مصرف ۱۰۰ گیگ) ──

def _tg_origin() -> str:
    host = get_host() or ""
    if not host:
        return "http://localhost"
    return host if host.startswith("http") else "https://" + host


def _tg_esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _tg_api(token: str, method: str, payload: dict, timeout: float = 10.0):
    client = http_client
    own = False
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
        own = True
    try:
        r = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=timeout)
        return r.json()
    finally:
        if own:
            await client.aclose()


async def _tg_photo(token: str, chat_id, caption: str, png: bytes):
    """ارسال عکس QR با multipart — بدون وابستگی جدید"""
    client = http_client
    own = False
    if client is None:
        client = httpx.AsyncClient(timeout=25.0)
        own = True
    try:
        r = await client.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": str(chat_id), "caption": caption[:1000]},
            files={"photo": ("qr.png", png, "image/png")},
            timeout=25.0,
        )
        return r.json()
    except Exception as e:
        logger.warning(f"TG photo error: {e}")
        return None
    finally:
        if own:
            await client.aclose()


async def _tg_send(token, chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        return await _tg_api(token, "sendMessage", payload)
    except Exception:
        return None


# ── ساخت PNG از ماتریس QR — بدون PIL (انکودر PNG استاندارد با zlib) ──
def _tg_qr_png(data: str) -> bytes:
    import struct as _struct
    import zlib as _zlib
    try:
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        m = qr.get_matrix()
    except Exception as e:
        logger.warning(f"QR build failed: {e}")
        return b""
    n = len(m)
    quiet, scale = 4, 8
    dim = (n + quiet * 2) * scale
    fg, bg = (10, 14, 42), (255, 255, 255)  # سرمه‌ای روی سفید — اسکن آسان
    rows = []
    for y in range(dim):
        my = y // scale - quiet
        row = bytearray()
        for x in range(dim):
            mx = x // scale - quiet
            on = (0 <= my < n and 0 <= mx < n and m[my][mx])
            row += bytes(fg if on else bg)
        rows.append(b"\x00" + bytes(row))

    def _chunk(typ: bytes, d: bytes) -> bytes:
        c = _struct.pack(">I", len(d)) + typ + d
        return c + _struct.pack(">I", _zlib.crc32(typ + d) & 0xFFFFFFFF)

    ihdr = _struct.pack(">IIBBBBB", dim, dim, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", _zlib.compress(b"".join(rows), 6))
            + _chunk(b"IEND", b""))


# ── ابزارهای نمایش ──
def _tg_bar(pct: float, n: int = 10) -> str:
    filled = max(0, min(n, round((pct / 100.0) * n)))
    return "▰" * filled + "▱" * (n - filled)


def _tg_def_hint() -> str:
    # پرانتز رمز پیش‌فرض فقط تا وقتی رمز پنل همان پیش‌فرض P443 است
    if ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD:
        return " (رمز پیش‌فرض: P443)"
    return ""


def _tg_authed_ids() -> list:
    return list(((SETTINGS.get("telegram") or {}).get("authed")) or [])


def _tg_is_authed(chat_id: str) -> bool:
    return chat_id in _tg_authed_ids()


def _tg_auth_add(chat_id: str):
    tg = SETTINGS.setdefault("telegram", {})
    ids = tg.setdefault("authed", [])
    if chat_id not in ids:
        ids.append(chat_id)
    asyncio.create_task(save_state())


def _tg_welcome() -> str:
    return ("🏰 <b>ربات کنترل پنل PERSEPOLIS</b> " + PANEL_VERSION + "\n\n"
            "سلام ادمین 👋 از اینجا پنل را کامل مدیریت کن:\n"
            "• 👥 ساخت / حذف / ویرایش کاربر + 📱 QR\n"
            "• 📁 گروه‌های اشتراک + صفحه عمومی\n"
            "• ♻️ ریست مصرف هر کاربر · 📊 مصرف از ۱۰۰ گیگ با نوار پرشونده\n"
            "• 🔑 تغییر رمز پنل (رمز ربات و پنل همیشه یکی است)")


def _tg_menu():
    return [
        [{"text": "📊 وضعیت پنل", "callback_data": "tg:status"}, {"text": "👥 کاربران", "callback_data": "tg:users"}],
        [{"text": "➕ ساخت کاربر", "callback_data": "tg:create"}, {"text": "📁 گروه‌های اشتراک", "callback_data": "tg:groups"}],
        [{"text": "🔑 تغییر رمز پنل", "callback_data": "tg:chpass"}, {"text": "ℹ️ نسخه و درباره", "callback_data": "tg:about"}],
    ]


def _tg_user_usage_block(l: dict) -> str:
    used = l.get("used_bytes") or 0
    lim = l.get("limit_bytes") or 0
    if lim > 0:
        pct = min(100.0, (used / lim) * 100.0)
        return (f"📊 مصرف: {fmt_bytes(used)} از {fmt_bytes(lim)} ({pct:.0f}٪)\n"
                + _tg_bar(pct))
    pct = min(100.0, (used / QUOTA_LIMIT_BYTES) * 100.0)
    return (f"📊 مصرف: {fmt_bytes(used)} از ∞ (نامحدود) — سهم از ۱۰۰ گیگ پنل: {pct:.0f}٪\n"
            + _tg_bar(pct))


def _tg_days_left(exp) -> str:
    if not exp:
        return "نامحدود ♾"
    try:
        d = (datetime.fromisoformat(str(exp)) - datetime.now()).days
        if d > 0:
            return f"{d} روز مانده ⏳"
        if d == 0:
            return "امروز منقضی می‌شود ⚠️"
        return "منقضی شده ⛔"
    except Exception:
        return "نامشخص"


def _tg_sub_out(sid: str, s: dict) -> dict | None:
    if not s.get("uuid_key"):
        return None
    exp = s.get("expires_at")
    expired = False
    if exp:
        try:
            expired = datetime.now() > datetime.fromisoformat(str(exp))
        except Exception:
            expired = False
    host = get_host()
    return {
        "sid": sid,
        "name": s.get("name") or "گروه",
        "uuid_key": s.get("uuid_key"),
        "expires_at": exp,
        "expired": expired,
        "link_ids": [x for x in (s.get("link_ids") or []) if x in LINKS],
        "page_url": f"https://{host}/group/{s.get('uuid_key')}",
        "sub_url": f"https://{host}/sub-group/{s.get('uuid_key')}",
    }


def _tg_group_line(g: dict) -> str:
    st = "⛔ منقضی" if g["expired"] else "✅ فعال"
    return (f"📁 <b>{_tg_esc(g['name'])}</b> {st}\n"
            f"⏳ انقضا: {_tg_days_left(g['expires_at'])}\n"
            f"👥 {len(g['link_ids'])} کانفیگ\n\n"
            f"🌐 صفحه گروه:\n<code>{g['page_url']}</code>\n"
            f"📡 ساب گروه:\n<code>{g['sub_url']}</code>")


async def _tg_handle(update: dict, origin: str):
    try:
        await _tg_handle_inner(update, origin)
    except Exception as e:
        logger.warning(f"TG handler error: {e}")


async def _tg_handle_inner(update: dict, origin: str):
    global ADMIN_USERNAME, ADMIN_PASSWORD
    tg = SETTINGS.get("telegram") or {}
    if not tg or not tg.get("enabled", True) or not tg.get("token"):
        return
    token = tg["token"]
    allowed = str(tg.get("chat_id", ""))
    chat_id = text = cb_id = data = from_id = None
    if update.get("callback_query"):
        cq = update["callback_query"]
        chat_id = str(cq["message"]["chat"]["id"])
        from_id = str(cq["from"]["id"])
        cb_id = cq.get("id")
        data = cq.get("data") or ""
    elif update.get("message", {}).get("text"):
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        from_id = str(msg["from"]["id"])
        text = str(msg.get("text") or "").strip()
    else:
        return
    if from_id != allowed or chat_id != allowed:
        return
    if cb_id:
        try:
            await _tg_api(token, "answerCallbackQuery", {"callback_query_id": cb_id})
        except Exception:
            pass

    sess = _tg_sess.get(chat_id)

    def finish():
        _tg_sess.pop(chat_id, None)

    # ══════════ 🔒 گیت رمز — ربات قبل از ورود رمز پنل را می‌پرسد ══════════
    if not _tg_is_authed(chat_id):
        if cb_id:
            await _tg_send(token, chat_id, "🔒 اول با رمز پنل وارد شو — /start را بزن" + _tg_def_hint() + ":")
            return
        if not text:
            return
        if text.startswith("/start"):
            await _tg_send(token, chat_id, "🏰 <b>ربات پنل PERSEPOLIS</b> " + PANEL_VERSION
                           + "\n\n🔒 رمز پنل را بفرست تا وارد شوی" + _tg_def_hint() + ":")
            return
        if text.startswith("/"):
            await _tg_send(token, chat_id, "🔒 اول رمز پنل را بفرست" + _tg_def_hint() + " — یا /start بزن:")
            return
        # هر متنی = تلاش برای واردشدن با رمز
        if text == ADMIN_PASSWORD:
            _tg_auth_add(chat_id)
            log_activity("auth", "ورود موفق به ربات تلگرام", "ok")
            await _tg_send(token, chat_id, "✅ <b>وارد شدی!</b> 🏰\n\n"
                           + "رمز ربات و پنل یکی است — از هر جا عوضش کنی همه‌جا عوض می‌شود 💎", _tg_menu())
        else:
            await _tg_send(token, chat_id, "❌ رمز اشتباه است — دوباره بفرست" + _tg_def_hint() + ":")
        return

    # ══════════ حالت‌های گفتگو (بعد از ورود) ══════════
    if sess and text and not text.startswith("/"):
        if time.time() - (sess.get("ts") or 0) > 600:
            finish()
        elif sess["a"] == "create":
            finish()
            label = text[:60] or "کاربر"
            uid = generate_uuid()
            async with LINKS_LOCK:
                LINKS[uid] = {"label": label, "limit_bytes": 0, "used_bytes": 0,
                              "created_at": now_ir().isoformat(), "active": True, "expires_at": None,
                              "note": "ساخته‌شده از ربات تلگرام", "is_default": False, "sub_id": None,
                              "protocol": DEFAULT_PROTOCOL, "http_version": "h2", "max_devices": 0,
                              "fingerprint": "chrome", "password_hash": ""}
            await save_state()
            log_activity("link", f"کانفیگ «{label}» از ربات تلگرام ساخته شد", "ok")
            await _tg_send(token, chat_id,
                           "✅ کاربر <b>" + _tg_esc(label) + "</b> ساخته شد (نامحدود) 🎉\n\n"
                           "📡 لینک ساب:\n<code>" + origin + "/sub/" + uid + "</code>",
                           [[{"text": "📱 QR کاربر", "callback_data": "tg:qr:" + uid},
                             {"text": "🔗 متن کانفیگ", "callback_data": "tg:cfg:" + uid}],
                            [{"text": "👥 کاربران", "callback_data": "tg:users"}]])
            return
        elif sess["a"] == "ren":
            finish()
            old = None
            new_label = None
            async with LINKS_LOCK:
                link = LINKS.get(sess["uid"])
                if link:
                    old = link.get("label")
                    link["label"] = text[:60] or old
                    new_label = link["label"]
            if old is not None:
                await save_state()
                log_activity("link", f"کانفیگ «{old}» از ربات به «{new_label}» تغییر نام یافت", "info")
                await _tg_send(token, chat_id, "✏️ نام به <b>" + _tg_esc(new_label) + "</b> تغییر کرد ✅", _tg_menu())
            else:
                await _tg_send(token, chat_id, "❌ کاربر یافت نشد.", _tg_menu())
            return
        elif sess["a"] == "chpass":
            finish()
            if len(text) < 6:
                await _tg_send(token, chat_id, "❌ رمز باید حداقل ۶ کاراکتر باشد — از منو دوباره شروع کن.", _tg_menu())
                return
            ADMIN_PASSWORD = text
            SETTINGS["admin_password"] = text
            if not SETTINGS.get("admin_username"):
                SETTINGS["admin_username"] = ADMIN_USERNAME
            await save_state()
            log_activity("auth", "رمز ادمین از ربات تلگرام تغییر کرد — پنل و ربات همه‌جا با رمز جدید", "warn")
            await _tg_send(token, chat_id,
                           "🔑 <b>رمز پنل تغییر کرد!</b> 💎\n\n"
                           "از این به بعد این رمز برای ورود پنل، ورود ربات و همه‌جا یکسان است — امنش نگه دار 🙏")
            return
        elif sess["a"] == "gname":
            finish()
            _tg_sess[chat_id] = {"a": "gexp", "gname": text[:60], "ts": time.time()}
            await _tg_send(token, chat_id,
                           "⏳ انقضای گروه «" + _tg_esc(text[:60]) + "» چند روز باشد؟\n\n"
                           "🔢 یک عدد بفرست (مثلاً ۳۰) — ۰ یعنی نامحدود ♾")
            return
        elif sess["a"] == "gexp":
            finish()
            try:
                days = max(0, int(text.strip()))
            except Exception:
                await _tg_send(token, chat_id, "❌ عدد نامعتبر — از منو «📁 گروه‌ها» را دوباره بزن.", _tg_menu())
                return
            gname = sess.get("gname") or "گروه"
            sid = secrets.token_hex(8)
            ukey = secrets.token_hex(10)
            expires_at = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
            async with SUBS_LOCK:
                SUBS[sid] = {"name": gname, "uuid_key": ukey, "link_ids": [], "password_hash": "",
                             "created_at": datetime.now().isoformat(), "expires_at": expires_at}
            await save_state()
            log_activity("sub", f"گروه اشتراک «{gname}» از ربات ساخته شد", "ok")
            g = _tg_sub_out(sid, SUBS[sid])
            await _tg_send(token, chat_id, "✅ <b>گروه ساخته شد!</b> 🎉\n\n" + _tg_group_line(g),
                           [[{"text": "➕ افزودن کانفیگ", "callback_data": "tg:gadd:" + sid},
                             {"text": "📱 QR صفحه", "callback_data": "tg:gqr:" + sid}],
                            [{"text": "📁 گروه‌ها", "callback_data": "tg:groups"}]])
            return

    # ══════════ دستورات متنی ══════════
    if text in ("/start", "/menu"):
        finish()
        await _tg_send(token, chat_id, _tg_welcome(), _tg_menu())
        return
    if text == "/cancel":
        finish()
        await _tg_send(token, chat_id, "✖️ لغو شد.", _tg_menu())
        return

    # ══════════ دکمه‌ها ══════════
    if data and data.startswith("tg:"):
        act = data[3:]
        if act in ("menu", "back"):
            finish()
            await _tg_send(token, chat_id, _tg_welcome(), _tg_menu())
            return
        if act in ("users",):
            finish()
            async with LINKS_LOCK:
                links = sorted(LINKS.items(), key=lambda kv: str(kv[1].get("created_at") or ""), reverse=True)
            if not links:
                await _tg_send(token, chat_id, "هنوز کاربری وجود ندارد — با دکمه‌ی «➕ ساخت کاربر» بساز.", _tg_menu())
                return
            kb = []
            for uid, l in links[:20]:
                st = "⛔" if l.get("active") is False else "✅"
                kb.append([{"text": st + " " + _tg_esc(l.get("label") or uid[:8]), "callback_data": "tg:view:" + uid}])
            kb.append([{"text": "➕ ساخت کاربر", "callback_data": "tg:create"}, {"text": "⬅️ منو", "callback_data": "tg:back"}])
            await _tg_send(token, chat_id, "👥 <b>" + str(len(links)) + " کاربر</b> — روی اسم بزن تا مدیریتش کنی:", kb)
            return
        if act.startswith("view:"):
            uid = act[5:]
            async with LINKS_LOCK:
                l = dict(LINKS[uid]) if uid in LINKS else None
            if not l:
                await _tg_send(token, chat_id, "❌ کاربر یافت نشد.", _tg_menu())
                return
            st = "⛔ غیرفعال" if l.get("active") is False else "✅ فعال"
            kb = [
                [{"text": "📱 QR کانفیگ", "callback_data": "tg:qr:" + uid}, {"text": "🔗 متن کانفیگ", "callback_data": "tg:cfg:" + uid}],
                [{"text": "✏️ تغییر نام", "callback_data": "tg:ren:" + uid}, {"text": "♻️ ریست مصرف", "callback_data": "tg:reset:" + uid}],
                [{"text": ("▶️ فعال‌سازی" if l.get("active") is False else "⏸ غیرفعال‌سازی"), "callback_data": "tg:tog:" + uid},
                 {"text": "📁 افزودن به گروه", "callback_data": "tg:gadd1:" + uid}],
                [{"text": "📡 ارسال لینک ساب", "callback_data": "tg:sub:" + uid}, {"text": "🗑 حذف", "callback_data": "tg:delq:" + uid}],
                [{"text": "⬅️ کاربران", "callback_data": "tg:users"}],
            ]
            await _tg_send(token, chat_id,
                           "👤 <b>" + _tg_esc(l.get("label") or "") + "</b>\n\n"
                           "وضعیت: " + st + "\n"
                           + _tg_user_usage_block(l) + "\n"
                           "⏳ انقضا: " + _tg_days_left(l.get("expires_at")) + "\n"
                           "📱 دستگاه‌ها: " + ("∞" if not l.get("max_devices") else str(l.get("max_devices"))) + "\n"
                           "🛫 پروتکل: " + PROTOCOLS.get(l.get("protocol", DEFAULT_PROTOCOL), PROTOCOLS["vless-ws"])["icon"]
                           + " " + PROTOCOLS.get(l.get("protocol", DEFAULT_PROTOCOL), PROTOCOLS["vless-ws"])["name"]
                           + " + " + HTTP_VERSIONS.get(l.get("http_version", "h2"), HTTP_VERSIONS["h2"])["name"] + "\n\n"
                           "📡 ساب:\n<code>" + origin + "/sub/" + uid + "</code>", kb)
            return
        if act.startswith("qr:"):
            uid = act[3:]
            async with LINKS_LOCK:
                l = dict(LINKS[uid]) if uid in LINKS else None
            if not l:
                await _tg_send(token, chat_id, "❌ کاربر یافت نشد.", _tg_menu())
                return
            png = _tg_qr_png(origin + "/sub/" + uid)
            if png:
                await _tg_photo(token, chat_id, "📱 QR کانفیگ «" + (l.get("label") or "") + "»\n📡 اسکن کن تا ساب اضافه شود 🚀", png)
            else:
                await _tg_send(token, chat_id, "📡 ساب «" + _tg_esc(l.get("label") or "") + "»:\n<code>" + origin + "/sub/" + uid + "</code>")
            return
        if act.startswith("cfg:"):
            uid = act[4:]
            async with LINKS_LOCK:
                l = dict(LINKS[uid]) if uid in LINKS else None
            if not l:
                await _tg_send(token, chat_id, "❌ کاربر یافت نشد.", _tg_menu())
                return
            host = get_host()
            link = generate_vless_link(uid, host, remark="🏛️ " + (l.get("label") or "کاربر"),
                                       protocol=l.get("protocol", DEFAULT_PROTOCOL),
                                       fingerprint=l.get("fingerprint", "chrome"),
                                       port=DEFAULT_PORT, http_version=l.get("http_version", "h2"), fake_port=False)
            await _tg_send(token, chat_id,
                           "🔗 کانفیگ اصلی «" + _tg_esc(l.get("label") or "") + "» — کپی کن:\n<code>" + link + "</code>")
            return
        if act.startswith("reset:"):
            uid = act[6:]
            lbl = None
            async with LINKS_LOCK:
                l = LINKS.get(uid)
                if l:
                    l["used_bytes"] = 0
                    l.pop("alert_80", None)
                    lbl = l.get("label")
            if lbl is not None:
                await save_state()
                log_activity("link", f"مصرف «{lbl}» از ربات ریست شد", "info")
                await _tg_send(token, chat_id,
                               "♻️ مصرف «" + _tg_esc(lbl) + "» صفر شد ✅\n"
                               "(مصرف کل ۱۰۰ گیگ پنل دست‌نخورده ماند — طبق تنظیم پنل)", _tg_menu())
            return
        if act.startswith("ren:"):
            uid = act[4:]
            async with LINKS_LOCK:
                exists = uid in LINKS
            if exists:
                _tg_sess[chat_id] = {"a": "ren", "uid": uid, "ts": time.time()}
                await _tg_send(token, chat_id, "✏️ اسم جدید کاربر را بفرست:")
            return
        if act.startswith("tog:"):
            uid = act[4:]
            label_now = None
            active_now = None
            async with LINKS_LOCK:
                l = LINKS.get(uid)
                if l:
                    l["active"] = (l.get("active") is False)
                    label_now = l.get("label")
                    active_now = l["active"]
            if label_now is not None:
                await save_state()
                log_activity("link", f"کانفیگ «{label_now}» از ربات {'فعال' if active_now else 'غیرفعال'} شد", "info")
                await _tg_send(token, chat_id, "✅ «" + _tg_esc(label_now) + "» " + ("فعال ⚡" if active_now else "غیرفعال ⏸") + " شد.", _tg_menu())
            return
        if act.startswith("sub:"):
            uid = act[4:]
            async with LINKS_LOCK:
                l = LINKS.get(uid)
                lbl = l.get("label") if l else None
            if l:
                await _tg_send(token, chat_id, "📡 ساب «" + _tg_esc(lbl) + "»:\n<code>" + origin + "/sub/" + uid + "</code>")
            return
        if act.startswith("delq:"):
            uid = act[5:]
            async with LINKS_LOCK:
                l = LINKS.get(uid)
                lbl = l.get("label") if l else None
            if l:
                await _tg_send(token, chat_id, "⚠️ «" + _tg_esc(lbl) + "» برای همیشه حذف شود؟",
                               [[{"text": "🗑 بله، حذف قطعی", "callback_data": "tg:dely:" + uid},
                                 {"text": "✖️ انصراف", "callback_data": "tg:back"}]])
            return
        if act.startswith("dely:"):
            uid = act[5:]
            async with LINKS_LOCK:
                l = LINKS.pop(uid, None)
                if l:
                    sid = l.get("sub_id")
                    if sid and sid in SUBS:
                        ids = SUBS[sid].get("link_ids") or []
                        if uid in ids:
                            ids.remove(uid)
                    for s in SUBS.values():
                        ids2 = s.get("link_ids") or []
                        if uid in ids2:
                            ids2.remove(uid)
            if l:
                await save_state()
                log_activity("link", f"کانفیگ «{l.get('label')}» از ربات تلگرام حذف شد", "err")
                await _tg_send(token, chat_id, "🗑 «" + _tg_esc(l.get("label")) + "» حذف شد.", _tg_menu())
            return
        if act == "create":
            _tg_sess[chat_id] = {"a": "create", "ts": time.time()}
            await _tg_send(token, chat_id, "➕ اسم کاربر جدید را بفرست:\n(نامحدود ساخته می‌شود — محدودکردن از پنل)")
            return
        if act == "chpass":
            _tg_sess[chat_id] = {"a": "chpass", "ts": time.time()}
            await _tg_send(token, chat_id,
                           "🔑 رمز جدید پنل را بفرست (حداقل ۶ کاراکتر):\n\n"
                           "⚠️ این رمز هم برای ورود پنل است، هم ورود ربات — همه‌جا یک رمز 💎")
            return
        if act == "status":
            async with LINKS_LOCK:
                links = list(LINKS.values())
            active = sum(1 for l in links if l.get("active") is not False)
            today = sum(hourly_traffic.values())
            used_total = quota_usage.get("bytes", 0)
            qpct = min(100.0, used_total / QUOTA_LIMIT_BYTES * 100.0)
            async with SUBS_LOCK:
                ngroups = len(SUBS)
            warn_line = ""
            if used_total >= QUOTA_LIMIT_BYTES:
                warn_line = "⛔ سقف ۱۰۰ گیگ پر شده!\n"
            elif used_total >= QUOTA_WARN_BYTES:
                warn_line = "⚠️ اخطار: مصرف به ۹۹ گیگ رسید!\n"
            await _tg_send(token, chat_id,
                           "📊 <b>وضعیت پنل P443</b>\n\n"
                           "👥 کاربران: " + str(len(links)) + " (" + str(active) + " فعال)\n"
                           "📈 ترافیک کل: " + fmt_bytes(stats["total_bytes"]) + "\n"
                           "🕒 امروز: " + fmt_bytes(today) + "\n"
                           "🔗 اتصالات زنده: " + str(len(connections)) + "\n"
                           "⏱ آپ‌تایم: " + uptime() + "\n"
                           "📁 گروه‌های اشتراک: " + str(ngroups) + "\n\n"
                           + warn_line
                           + "📊 مصرف کل پنل: " + fmt_bytes(used_total) + " از ۱۰۰ گیگ (" + f"{qpct:.0f}" + "٪)\n"
                           + _tg_bar(qpct) + "\n"
                           "(دائمی — هیچ‌وقت ریست نمی‌شود)\n\n"
                           "💎 نسخه پنل: <b>" + PANEL_VERSION + "</b>", _tg_menu())
            return
        if act == "about":
            await _tg_send(token, chat_id,
                           "💎 <b>P443 Panel " + PANEL_VERSION + "</b>\n"
                           "🏛 Persepolis VLESS-WS Panel\n\n"
                           "🤖 این ربات فقط برای ادمین پنل کار می‌کند.\n"
                           "🚫 فروش پنل یا کانفیگ‌ها ممنوع است — نشانه‌ی شخصیت و شرف توست 🙏", _tg_menu())
            return

        # ══════════ 📁 گروه‌های اشتراک ══════════
        if act == "groups":
            finish()
            async with SUBS_LOCK:
                subs = [s for s in (_tg_sub_out(sid, s) for sid, s in list(SUBS.items())) if s]
            if not subs:
                await _tg_send(token, chat_id,
                               "📁 هنوز گروهی نساخته‌ای!\n\nگروه اشتراک یعنی چند کانفیگ دور هم — بعد لینکش را به هرکی بدهی همه کانفیگ‌ها را با QR و صفحه‌ی خفن می‌بیند 🚀",
                               [[{"text": "➕ گروه جدید", "callback_data": "tg:gnew"}], [{"text": "⬅️ منو", "callback_data": "tg:back"}]])
                return
            kb = []
            for g in subs[:15]:
                st = " ⛔" if g["expired"] else ""
                kb.append([{"text": f"📁 {_tg_esc(g['name'])} ({len(g['link_ids'])}){st}", "callback_data": "tg:grp:" + g["sid"]}])
            kb.append([{"text": "➕ گروه جدید", "callback_data": "tg:gnew"}, {"text": "⬅️ منو", "callback_data": "tg:back"}])
            await _tg_send(token, chat_id, f"📁 <b>{len(subs)} گروه اشتراک</b> — روی اسم بزن:", kb)
            return
        if act == "gnew":
            _tg_sess[chat_id] = {"a": "gname", "ts": time.time()}
            await _tg_send(token, chat_id, "📁 اسم گروه اشتراک جدید را بفرست (مثلاً VIP):")
            return
        if act.startswith("grp:"):
            gid = act[4:]
            async with SUBS_LOCK:
                g = _tg_sub_out(gid, dict(SUBS.get(gid) or {})) if gid in SUBS else None
                members = []
                if g:
                    for m in g["link_ids"]:
                        mm = LINKS.get(m)
                        if mm:
                            members.append((mm.get("label") or m[:8], mm.get("active") is not False))
            if not g:
                await _tg_send(token, chat_id, "❌ گروه یافت نشد.", _tg_menu())
                return
            mtxt = "\n".join(("✅ " if a else "⛔ ") + _tg_esc(n) for n, a in members[:15]) or "— خالی —"
            kb = [
                [{"text": "➕ افزودن کانفیگ", "callback_data": "tg:gadd:" + gid},
                 {"text": "📱 QR صفحه", "callback_data": "tg:gqr:" + gid}],
                [{"text": "🌐 باز کردن صفحه گروه", "url": g["page_url"]}],
                [{"text": "🗑 حذف گروه", "callback_data": "tg:gdelq:" + gid}],
                [{"text": "⬅️ گروه‌ها", "callback_data": "tg:groups"}],
            ]
            await _tg_send(token, chat_id, _tg_group_line(g) + "\n👥 کانفیگ‌های این گروه:\n" + mtxt, kb)
            return
        if act.startswith("gqr:"):
            gid = act[4:]
            async with SUBS_LOCK:
                g = _tg_sub_out(gid, dict(SUBS.get(gid) or {})) if gid in SUBS else None
            if not g:
                await _tg_send(token, chat_id, "❌ گروه یافت نشد.", _tg_menu())
                return
            png = _tg_qr_png(g["page_url"])
            if png:
                await _tg_photo(token, chat_id, "📱 QR صفحه‌ی گروه «" + _tg_esc(g["name"]) + "»\n🌐 اسکن کن تا صفحه‌ی خفن گروه باز شود 🚀", png)
            else:
                await _tg_send(token, chat_id, "🌐 صفحه گروه:\n<code>" + g["page_url"] + "</code>")
            return
        if act.startswith("gadd:"):
            gid = act[5:]
            async with SUBS_LOCK:
                exists = gid in SUBS
                in_grp = set((SUBS.get(gid) or {}).get("link_ids") or [])
            if not exists:
                await _tg_send(token, chat_id, "❌ گروه یافت نشد.", _tg_menu())
                return
            async with LINKS_LOCK:
                links = sorted(LINKS.items(), key=lambda kv: str(kv[1].get("created_at") or ""), reverse=True)
            cands = [(u, l) for u, l in links if u not in in_grp][:15]
            if not cands:
                await _tg_send(token, chat_id, "همه‌ی کاربران در این گروه هستند یا کاربری وجود ندارد 🤷",
                               [[{"text": "⬅️ گروه", "callback_data": "tg:grp:" + gid}]])
                return
            kb = [[{"text": "👤 " + _tg_esc(l.get("label") or u[:8]), "callback_data": "tg:gput:" + gid + ":" + u}] for u, l in cands]
            kb.append([{"text": "⬅️ گروه", "callback_data": "tg:grp:" + gid}])
            await _tg_send(token, chat_id, "➕ کدام کانفیگ به گروه اضافه شود؟", kb)
            return
        if act.startswith("gput:"):
            rest = act[5:]
            gid, _, uid = rest.partition(":")
            ok = False
            gname = uid_lbl = None
            async with SUBS_LOCK:
                if gid in SUBS:
                    ids = SUBS[gid].setdefault("link_ids", [])
                    if uid in LINKS and uid not in ids:
                        ids.append(uid)
                        ok = True
                    elif uid in ids:
                        ok = True
                    gname = SUBS[gid].get("name")
            async with LINKS_LOCK:
                if uid in LINKS:
                    uid_lbl = LINKS[uid].get("label")
                    LINKS[uid]["sub_id"] = gid
            if ok:
                await save_state()
                log_activity("sub", f"«{uid_lbl}» به گروه «{gname}» اضافه شد", "ok")
                await _tg_send(token, chat_id, "✅ «" + _tg_esc(uid_lbl or uid[:8]) + "» به گروه «" + _tg_esc(gname or "") + "» اضافه شد 🎉",
                               [[{"text": "➕ یکی دیگر", "callback_data": "tg:gadd:" + gid},
                                 {"text": "📁 گروه", "callback_data": "tg:grp:" + gid}]])
            return
        if act.startswith("gdelq:"):
            gid = act[6:]
            async with SUBS_LOCK:
                g = SUBS.get(gid)
                gname = g.get("name") if g else None
            if gname is not None:
                await _tg_send(token, chat_id, "⚠️ گروه «" + _tg_esc(gname) + "» حذف شود؟\n(کانفیگ‌های داخلش سالم می‌مانند ✅)",
                               [[{"text": "🗑 بله، حذف گروه", "callback_data": "tg:gdely:" + gid},
                                 {"text": "✖️ انصراف", "callback_data": "tg:back"}]])
            return
        if act.startswith("gdely:"):
            gid = act[6:]
            async with SUBS_LOCK:
                g = SUBS.pop(gid, None)
            if g:
                async with LINKS_LOCK:
                    for u in g.get("link_ids") or []:
                        if u in LINKS and LINKS[u].get("sub_id") == gid:
                            LINKS[u]["sub_id"] = None
                await save_state()
                log_activity("sub", f"گروه «{g.get('name')}» از ربات حذف شد", "warn")
                await _tg_send(token, chat_id, "🗑 گروه «" + _tg_esc(g.get("name") or "") + "» حذف شد — کانفیگ‌ها سالم ماندند ✅", _tg_menu())
            return
        if act.startswith("gadd1:"):
            # افزودن کاربر خاص به گروه: اول انتخاب گروه
            uid = act[6:]
            async with LINKS_LOCK:
                exists = uid in LINKS
            if not exists:
                await _tg_send(token, chat_id, "❌ کاربر یافت نشد.", _tg_menu())
                return
            async with SUBS_LOCK:
                subs = [s for s in (_tg_sub_out(sid, s) for sid, s in list(SUBS.items())) if s]
            if not subs:
                await _tg_send(token, chat_id, "اول یک گروه بساز — «📁 گروه‌های اشتراک» ← «➕ گروه جدید»", _tg_menu())
                return
            kb = [[{"text": "📁 " + _tg_esc(g["name"]), "callback_data": "tg:gput:" + g["sid"] + ":" + uid}] for g in subs[:15]]
            kb.append([{"text": "⬅️ کاربر", "callback_data": "tg:view:" + uid}])
            await _tg_send(token, chat_id, "📁 به کدام گروه اضافه شود؟", kb)
            return


# ══════════ 🤖 Polling موتور ربات — بدون نیاز به وبهوک، روی Railway همیشه کار می‌کند ══════════
async def _tg_safe_webhook_clear(token: str):
    try:
        await _tg_api(token, "deleteWebhook", {"drop_pending_updates": False}, timeout=8.0)
    except Exception:
        pass


async def _tg_poll_loop():
    global _tg_offset
    logger.info("🤖 Telegram bot polling loop started")
    while True:
        tg = SETTINGS.get("telegram") or {}
        if not tg.get("enabled") or not tg.get("token"):
            await asyncio.sleep(3)
            continue
        try:
            r = await _tg_api(tg["token"], "getUpdates",
                              {"offset": _tg_offset, "timeout": 25,
                               "allowed_updates": ["message", "callback_query"]},
                              timeout=35.0)
            if isinstance(r, dict) and r.get("ok"):
                for u in r.get("result") or []:
                    try:
                        _tg_offset = max(_tg_offset, int(u.get("update_id", 0)) + 1)
                    except Exception:
                        pass
                    await _tg_handle(u, _tg_origin())
            else:
                if isinstance(r, dict) and r.get("error_code") == 409:
                    await _tg_safe_webhook_clear(tg["token"])
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"TG poll error: {e}")
            await asyncio.sleep(5)


def _tg_start_poll():
    global _tg_poll_task
    if _tg_poll_task is None or _tg_poll_task.done():
        try:
            _tg_poll_task = asyncio.create_task(_tg_poll_loop())
        except RuntimeError:
            pass


def _tg_stop_poll():
    global _tg_poll_task
    if _tg_poll_task and not _tg_poll_task.done():
        _tg_poll_task.cancel()
    _tg_poll_task = None


@app.post("/api/settings/telegram")
async def set_telegram(request: Request, _=Depends(require_auth)):
    global _tg_offset
    body = await request.json()
    token = str(body.get("token") or "").strip()
    chat_id = str(body.get("chat_id") or "").strip()
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{20,}", token):
        raise HTTPException(status_code=400, detail="فرمت توکن ربات اشتباه است (مثل 123456:ABC-DEF...)")
    if not re.fullmatch(r"\d{3,}", chat_id):
        raise HTTPException(status_code=400, detail="آیدی عددی تلگرام نامعتبر است (فقط رقم — از @userinfobot بگیر)")
    try:
        me = await _tg_api(token, "getMe", {})
        if not me or not me.get("ok"):
            raise HTTPException(status_code=400, detail="توکن ربات نامعتبر است — از @BotFather دوباره چک کن")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="اتصال به تلگرام ناموفق بود — دوباره امتحان کن")
    bot_username = (me.get("result") or {}).get("username", "")
    SETTINGS["telegram"] = {"token": token, "chat_id": chat_id, "bot_username": bot_username,
                            "enabled": True, "authed": []}
    await save_state()
    # حالت polling — وبهوک قدیمی باید پاک شود تا تداخل پیش نیاید
    await _tg_safe_webhook_clear(token)
    _tg_offset = 0
    _tg_start_poll()
    log_activity("auth", "ربات تلگرام @" + str(bot_username or "?") + " به پنل متصل شد (polling)", "ok")
    try:
        await _tg_send(token, chat_id,
                       "🏰 <b>ربات پنل PERSEPOLIS</b> " + PANEL_VERSION + " وصل شد! ✅\n\n"
                       "/start را بزن و با رمز پنل وارد شو" + _tg_def_hint() + " 💎")
    except Exception:
        pass
    return {"ok": True, "bot_username": bot_username, "webhook_set": True, "chat_id": chat_id, "saved": True}


@app.post("/api/settings/telegram/disable")
async def disable_telegram(_=Depends(require_auth)):
    tg = SETTINGS.get("telegram") or {}
    if tg.get("token"):
        try:
            await _tg_safe_webhook_clear(tg["token"])
        except Exception:
            pass
    _tg_stop_poll()
    SETTINGS.pop("telegram", None)
    _tg_sess.clear()
    await save_state()
    log_activity("auth", "ربات تلگرام از پنل قطع شد", "warn")
    return {"ok": True}


@app.get("/api/settings/telegram")
async def get_telegram(_=Depends(require_auth)):
    tg = SETTINGS.get("telegram")
    if not tg:
        return {"configured": False}
    return {"configured": True, "enabled": bool(tg.get("enabled")), "chat_id": tg.get("chat_id"),
            "bot_username": tg.get("bot_username", ""), "mode": "polling",
            "authed": len(tg.get("authed") or []),
            "token_masked": str(tg.get("token", "")).split(":")[0] + ":..."}


@app.post("/tg-webhook/{secret}")
async def tg_webhook(secret: str, request: Request):
    tg = SETTINGS.get("telegram") or {}
    if not tg or not tg.get("secret") or tg["secret"] != secret:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"detail": "bad request"}, status_code=400)
    asyncio.create_task(_tg_handle(update, _tg_origin()))
    return JSONResponse({"ok": True})

# ─── API: Dashboard Stats ──────────────────────────────────────────────────

@app.get("/api/dashboard/stats")
async def dashboard_stats(_=Depends(require_auth)):
    disk_usage = psutil.disk_usage('/')
    
    if len(hourly_traffic) > 0:
        last_hour = sum(list(hourly_traffic.values())[-6:])
        speed = last_hour / 21600
    else:
        speed = 0
    
    # ⚡ مصرف کل با سقف ۱۰۰ گیگ — دائمی، هیچ‌وقت ریست نمی‌شود؛ اخطار در ۹۹ گیگ
    used_total = quota_usage.get("bytes", 0)
    quota_obj = {
        "bytes": used_total,
        "bytes_fmt": fmt_bytes(used_total),
        "limit": QUOTA_LIMIT_BYTES,
        "limit_fmt": f"{QUOTA_LIMIT_GB} GB",
        "percent": min(100.0, (used_total / QUOTA_LIMIT_BYTES) * 100.0),
        "warn_99": used_total >= QUOTA_WARN_BYTES,
        "exhausted": used_total >= QUOTA_LIMIT_BYTES,
    }
    cpu_percent = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    return {
        "version": PANEL_VERSION,
        "cpu": {
            "percent": cpu_percent,
            "cores": psutil.cpu_count(logical=True) or 2,
        },
        "memory": {
            "percent": mem.percent,
            "used_fmt": fmt_bytes(mem.used),
            "total_fmt": fmt_bytes(mem.total),
        },
        "traffic": {
            "total": stats["total_bytes"],
            "total_fmt": fmt_bytes(stats["total_bytes"]),
            "today": sum(hourly_traffic.values()),
            "today_fmt": fmt_bytes(sum(hourly_traffic.values()))
        },
        "requests": stats["total_requests"],
        "uptime": uptime(),
        "disk": {
            "total": disk_usage.total,
            "used": disk_usage.used,
            "free": disk_usage.free,
            "total_fmt": fmt_bytes(disk_usage.total),
            "used_fmt": fmt_bytes(disk_usage.used),
            "free_fmt": fmt_bytes(disk_usage.free),
            "percent": disk_usage.percent
        },
        "connections": len(connections),
        "quota": quota_obj,
        "daily": quota_obj,  # نام قدیمی — برای سازگاری
        "speed": {
            "download": speed,
            "download_fmt": fmt_bytes(speed) + "/s" if speed > 0 else "0 B/s"
        },
        "links_count": len(LINKS),
        "active_links": sum(1 for l in LINKS.values() if is_link_allowed(l))
    }

# ─── API: Inbound ────────────────────────────────────────────────────────────

@app.get("/api/inbound")
async def get_inbound(_=Depends(require_auth)):
    return {
        "port": DEFAULT_PORT,
        "protocol": SETTINGS.get("default_protocol", "vless-ws"),
        "host": get_host(),
        "is_active": True
    }

# ─── API: Links ─────────────────────────────────────────────────────────────

@app.post("/api/links")
async def create_link(request: Request, _=Depends(require_auth)):
    body = await request.json()
    label = (body.get("label") or "لینک جدید").strip()[:60]
    lv = float(body.get("limit_value") or 0)
    lu = body.get("limit_unit") or "GB"
    limit_bytes = 0 if lv <= 0 else parse_size_to_bytes(lv, lu)
    exp_days = int(body.get("expires_days") or 0)
    
    expires_at = None
    if exp_days > 0:
        exp_date = datetime.now() + timedelta(days=exp_days)
        expires_at = exp_date.isoformat()
    
    note = (body.get("note") or "").strip()[:200]
    sub_id = body.get("sub_id") or None
    
    protocol = body.get("protocol", DEFAULT_PROTOCOL)
    if protocol not in PROTOCOLS:
        protocol = DEFAULT_PROTOCOL
    
    http_version = body.get("http_version", "h2")
    if http_version not in HTTP_VERSIONS:
        http_version = "h2"
    
    max_devices = int(body.get("max_devices", 0))
    fingerprint = body.get("fingerprint", "chrome")
    if fingerprint not in FINGERPRINTS:
        fingerprint = "chrome"
    config_password = body.get("password", "").strip()
    
    uid = generate_uuid()
    async with LINKS_LOCK:
        LINKS[uid] = {
            "label": label,
            "limit_bytes": limit_bytes,
            "used_bytes": 0,
            "created_at": datetime.now().isoformat(),
            "active": True,
            "expires_at": expires_at,
            "note": note,
            "is_default": False,
            "sub_id": sub_id,
            "protocol": protocol,
            "http_version": http_version,
            "max_devices": max_devices,
            "fingerprint": fingerprint,
            "password_hash": config_password,
        }

    if sub_id:
        async with SUBS_LOCK:
            if sub_id in SUBS:
                ids = SUBS[sub_id].setdefault("link_ids", [])
                if uid not in ids:
                    ids.append(uid)

    asyncio.create_task(save_state())
    
    proto_name = PROTOCOLS.get(protocol, PROTOCOLS["vless-ws"])["name"]
    http_name = HTTP_VERSIONS.get(http_version, HTTP_VERSIONS["h2"])["name"]
    log_activity("link", f"کانفیگ «{label}» ساخته شد با {proto_name} + {http_name}", "ok")
    
    host = get_host()
    remark = f"🏛️ {label}"
    main_link = generate_vless_link(uid, host, remark=remark, protocol=protocol, fingerprint=fingerprint, port=DEFAULT_PORT, http_version=http_version, fake_port=False)
    
    link_data = {
        "uuid": uid,
        **LINKS[uid],
        "has_password": bool(config_password),
        "vless_link": main_link,
        "sub_url": f"https://{host}/sub/{uid}",
        "warning_config": "",
    }
    
    return link_data

@app.get("/api/links")
async def list_links(_=Depends(require_auth)):
    host = get_host()
    async with LINKS_LOCK:
        snap = dict(LINKS)
    
    result = []
    for uid, d in snap.items():
        protocol = d.get("protocol", DEFAULT_PROTOCOL)
        http_version = d.get("http_version", "h2")
        fp = d.get("fingerprint", "chrome")
        label = d.get("label", "کاربر")
        remark = f"🏛️ {label}"
        
        last_connected = None
        for c in connections.values():
            if c.get("uuid") == uid:
                if not last_connected or c.get("connected_at") > last_connected:
                    last_connected = c.get("connected_at")
        
        active = d.get("active", True) and not is_link_expired(d)
        proto_info = PROTOCOLS.get(protocol, PROTOCOLS["vless-ws"])
        http_info = HTTP_VERSIONS.get(http_version, HTTP_VERSIONS["h2"])
        
        result.append({
            "uuid": uid,
            **d,
            "protocol": protocol,
            "protocol_name": proto_info["name"],
            "protocol_icon": proto_info["icon"],
            "http_version": http_version,
            "http_name": http_info["name"],
            "fingerprint": fp,
            "max_devices": d.get("max_devices", 0),
            "expired": is_link_expired(d),
            "has_password": bool(d.get("password_hash")),
            "last_connected_at": last_connected,
            "vless_link": generate_vless_link(uid, host, remark=remark, protocol=protocol, fingerprint=fp, port=DEFAULT_PORT, http_version=http_version, fake_port=False),
            "sub_url": f"https://{host}/sub/{uid}",
            "warning_config": "",
        })
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return {"links": result}

@app.patch("/api/links/{uid}")
async def update_link(uid: str, request: Request, _=Depends(require_auth)):
    body = await request.json()
    
    async with LINKS_LOCK:
        if uid not in LINKS:
            raise HTTPException(status_code=404, detail="link not found")
        link = LINKS[uid]
        
        if link.get("password_hash"):
            password = body.get("password", "").strip()
            if not password or password != link["password_hash"]:
                raise HTTPException(status_code=403, detail="رمز کانفیگ اشتباه است")
        
        old_sub = link.get("sub_id")
        
        if "active" in body:
            link["active"] = bool(body["active"])
        if "label" in body:
            link["label"] = str(body["label"])[:60]
        if "note" in body:
            link["note"] = str(body["note"])[:200]
        if "reset_usage" in body and body["reset_usage"]:
            link["used_bytes"] = 0
        if "limit_value" in body:
            lv = float(body.get("limit_value") or 0)
            lu = body.get("limit_unit") or "GB"
            link["limit_bytes"] = 0 if lv <= 0 else parse_size_to_bytes(lv, lu)
        if "expires_days" in body:
            ed = int(body["expires_days"] or 0)
            if ed > 0:
                exp_date = datetime.now() + timedelta(days=ed)
                link["expires_at"] = exp_date.isoformat()
            else:
                link["expires_at"] = None
        if "max_devices" in body:
            link["max_devices"] = int(body["max_devices"])
        if "fingerprint" in body and body["fingerprint"] in FINGERPRINTS:
            link["fingerprint"] = body["fingerprint"]
        if "protocol" in body and body["protocol"] in PROTOCOLS:
            link["protocol"] = body["protocol"]
        if "http_version" in body and body["http_version"] in HTTP_VERSIONS:
            link["http_version"] = body["http_version"]
        new_sub = body.get("sub_id", "UNCHANGED")
        if new_sub != "UNCHANGED":
            link["sub_id"] = new_sub or None

    if new_sub != "UNCHANGED":
        async with SUBS_LOCK:
            if old_sub and old_sub in SUBS:
                ids = SUBS[old_sub].get("link_ids", [])
                if uid in ids:
                    ids.remove(uid)
            if new_sub and new_sub in SUBS:
                ids = SUBS[new_sub].setdefault("link_ids", [])
                if uid not in ids:
                    ids.append(uid)

    asyncio.create_task(save_state())
    log_activity("link", f"کانفیگ «{link['label']}» ویرایش شد", "info")
    return {"ok": True}

@app.delete("/api/links/{uid}")
async def delete_link(uid: str, request: Request, _=Depends(require_auth)):
    body = await request.json()
    password = body.get("password", "").strip()
    
    async with LINKS_LOCK:
        if uid not in LINKS:
            raise HTTPException(status_code=404, detail="link not found")
        link = LINKS[uid]
        
        if link.get("password_hash"):
            if not password or password != link["password_hash"]:
                raise HTTPException(status_code=403, detail="رمز کانفیگ اشتباه است")
        
        label = link.get("label", uid)
        sub_id = link.get("sub_id")
        del LINKS[uid]
    
    if sub_id:
        async with SUBS_LOCK:
            if sub_id in SUBS:
                ids = SUBS[sub_id].get("link_ids", [])
                if uid in ids:
                    ids.remove(uid)
    
    asyncio.create_task(save_state())
    log_activity("link", f"کانفیگ «{label}» حذف شد", "err")
    
    return {"ok": True, "deleted": uid}

# ─── API: گروه‌های اشتراک (Subscription Groups) ─────────────────────────────
def _sub_public(sid: str, s: dict, known_uuids: set) -> dict:
    exp = s.get("expires_at")
    expired = False
    if exp:
        try:
            expired = datetime.now() > datetime.fromisoformat(str(exp))
        except Exception:
            expired = False
    host = get_host()
    ukey = s.get("uuid_key") or ""
    members = [m for m in (s.get("link_ids") or []) if m in known_uuids]
    return {
        "sid": sid,
        "name": s.get("name") or "گروه",
        "uuid_key": ukey,
        "created_at": s.get("created_at"),
        "expires_at": exp,
        "expired": expired,
        "days_left": _tg_days_left(exp),
        "members_count": len(members),
        "member_uuids": members,
        "page_url": f"https://{host}/group/{ukey}" if ukey else "",
        "sub_url": f"https://{host}/sub-group/{ukey}" if ukey else "",
    }


@app.get("/api/subs")
async def list_subs(_=Depends(require_auth)):
    async with LINKS_LOCK:
        known = set(LINKS.keys())
    async with SUBS_LOCK:
        snap = {k: dict(v) for k, v in SUBS.items()}
    subs = [_sub_public(sid, s, known) for sid, s in snap.items()]
    subs.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"subs": subs}


@app.post("/api/subs")
async def create_sub(request: Request, _=Depends(require_auth)):
    body = await request.json()
    name = (body.get("name") or "").strip()[:60] or "گروه اشتراک"
    try:
        days = max(0, int(body.get("expires_days") or 0))
    except (TypeError, ValueError):
        days = 0
    sid = secrets.token_hex(8)
    ukey = secrets.token_hex(10)
    expires_at = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
    added = []
    async with SUBS_LOCK:
        SUBS[sid] = {
            "name": name,
            "uuid_key": ukey,
            "link_ids": [],
            "password_hash": "",
            "created_at": datetime.now().isoformat(),
            "expires_at": expires_at,
        }
        # 🎯 v10.8 — انتخاب کانفیگ‌های عضو هنگام ساخت گروه (از بین کانفیگ‌های موجود)
        members = body.get("members")
        if isinstance(members, list):
            ids = SUBS[sid].setdefault("link_ids", [])
            for mu in members[:60]:
                mu = str(mu or "")
                if mu in LINKS and mu not in ids:
                    ids.append(mu)
                    LINKS[mu]["sub_id"] = sid
                    added.append(LINKS[mu].get("label") or mu[:8])
    asyncio.create_task(save_state())
    log_activity("sub", f"گروه اشتراک «{name}» ساخته شد" + (f" با انقضای {days} روز" if days else "") + (f" با {len(added)} کانفیگ عضو" if added else ""), "ok")
    return {"ok": True, "sid": sid, "uuid_key": ukey, "name": name, "expires_at": expires_at, "added": added}


@app.patch("/api/subs/{sid}")
async def update_sub(sid: str, request: Request, _=Depends(require_auth)):
    body = await request.json()
    async with SUBS_LOCK:
        if sid not in SUBS:
            raise HTTPException(status_code=404, detail="group not found")
        sub = SUBS[sid]
        if "name" in body:
            sub["name"] = (str(body["name"]) or "").strip()[:60] or sub["name"]
        if "expires_days" in body:
            try:
                ed = max(0, int(body["expires_days"] or 0))
            except (TypeError, ValueError):
                ed = 0
            sub["expires_at"] = (datetime.now() + timedelta(days=ed)).isoformat() if ed > 0 else None
        if "add_uuid" in body:
            au = str(body["add_uuid"] or "")
            if au in LINKS:
                ids = sub.setdefault("link_ids", [])
                if au not in ids:
                    ids.append(au)
                    LINKS[au]["sub_id"] = sid
        if "remove_uuid" in body:
            ru = str(body["remove_uuid"] or "")
            ids = sub.get("link_ids") or []
            if ru in ids:
                ids.remove(ru)
            if ru in LINKS and LINKS[ru].get("sub_id") == sid:
                LINKS[ru]["sub_id"] = None
    asyncio.create_task(save_state())
    log_activity("sub", f"گروه «{sub.get('name')}» ویرایش شد", "info")
    return {"ok": True}


@app.delete("/api/subs/{sid}")
async def delete_sub(sid: str, _=Depends(require_auth)):
    async with SUBS_LOCK:
        sub = SUBS.pop(sid, None)
    if not sub:
        raise HTTPException(status_code=404, detail="group not found")
    async with LINKS_LOCK:
        for u in sub.get("link_ids") or []:
            if u in LINKS and LINKS[u].get("sub_id") == sid:
                LINKS[u]["sub_id"] = None
    asyncio.create_task(save_state())
    log_activity("sub", f"گروه «{sub.get('name')}» حذف شد — کانفیگ‌ها سالم ماندند", "warn")
    return {"ok": True, "deleted": sid}

# ─── API: Stats & Connections ──────────────────────────────────────────────

@app.get("/stats")
async def get_stats(_=Depends(require_auth)):
    async with LINKS_LOCK:
        snap = dict(LINKS)
    
    top_user = None
    top_usage = 0
    for uid, link in snap.items():
        used = link.get("used_bytes", 0)
        if used > top_usage:
            top_usage = used
            top_user = {
                "uuid": uid,
                "label": link.get("label", "نامشخص"),
                "used_bytes": used,
                "used_fmt": fmt_bytes(used)
            }
    
    hourly_data = {}
    now = datetime.now()
    for i in range(30):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        if day in hourly_traffic_history:
            for hour, bytes_count in hourly_traffic_history[day].items():
                hourly_data[hour] = hourly_data.get(hour, 0) + bytes_count
    
    return {
        "active_connections": len(connections),
        "total_traffic_mb": round(stats["total_bytes"] / (1024 ** 2), 2),
        "total_requests": stats["total_requests"],
        "total_errors": stats["total_errors"],
        "uptime": uptime(),
        "timestamp": datetime.now().isoformat(),
        "hourly": dict(hourly_traffic),
        "hourly_history": hourly_data,
        "recent_errors": list(error_logs)[-10:],
        "links_count": len(snap),
        "active_links": sum(1 for l in snap.values() if is_link_allowed(l)),
        "expired_links": sum(1 for l in snap.values() if is_link_expired(l)),
        "subs_count": len(SUBS),
        "top_user": top_user,
    }

@app.get("/api/connections")
async def get_connections(_=Depends(require_auth)):
    async with LINKS_LOCK:
        snap = dict(LINKS)

    grouped: dict[str, dict] = {}
    for conn_id, c in connections.items():
        ip = c.get("ip", "نامشخص")
        link = snap.get(c.get("uuid"))
        label = link.get("label") if link else "نامشخص"
        g = grouped.get(ip)
        if g is None:
            g = {
                "ip": ip,
                "sessions": 0,
                "bytes": 0,
                "labels": set(),
                "transports": set(),
                "first_connected_at": c.get("connected_at"),
                "last_connected_at": c.get("connected_at"),
            }
            grouped[ip] = g
        g["sessions"] += 1
        g["bytes"] += c.get("bytes", 0)
        g["labels"].add(label)
        g["transports"].add(c.get("transport", "vless-ws"))
        ca = c.get("connected_at")
        if ca:
            if not g["first_connected_at"] or ca < g["first_connected_at"]:
                g["first_connected_at"] = ca
            if not g["last_connected_at"] or ca > g["last_connected_at"]:
                g["last_connected_at"] = ca

    result = []
    for ip, g in grouped.items():
        result.append({
            "ip": ip,
            "sessions": g["sessions"],
            "labels": sorted(g["labels"]),
            "label": " · ".join(sorted(g["labels"])) if g["labels"] else "نامشخص",
            "transports": sorted(g["transports"]),
            "bytes": g["bytes"],
            "bytes_fmt": fmt_bytes(g["bytes"]),
            "connected_at": g["first_connected_at"],
            "last_connected_at": g["last_connected_at"],
        })
    result.sort(key=lambda x: x.get("last_connected_at") or "", reverse=True)

    by_uuid: dict = {}
    for c in connections.values():
        u = c.get("uuid")
        if u:
            by_uuid[u] = by_uuid.get(u, 0) + 1

    return {
        "connections": result,
        "count": len(result),
        "raw_count": len(connections),
        "by_uuid": by_uuid,
    }

# ─── Auth Endpoints ────────────────────────────────────────────────────────

@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    ip = client_ip(request)
    username = body.get("username", "")
    password = body.get("password", "")
    remember = body.get("remember", False)
    
    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        log_activity("auth", f"تلاش ورود ناموفق از {ip}", "err")
        raise HTTPException(status_code=401, detail="یوزرنیم یا رمز عبور اشتباه است")
    
    token = await create_session()
    log_activity("auth", f"ورود موفق به پنل از {ip}", "ok")
    
    max_age = SESSION_TTL if remember else None
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="lax", path="/")
    return resp

@app.post("/api/logout")
async def api_logout(request: Request):
    await destroy_session(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

@app.get("/api/me")
async def api_me(request: Request):
    return {
        "authenticated": await is_valid_session(request.cookies.get(SESSION_COOKIE)),
        "default_password": ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD,
    }

@app.head("/api/me")
async def api_me_head(request: Request):
    return {"authenticated": await is_valid_session(request.cookies.get(SESSION_COOKIE))}

# ─── API: Activity Logs ───────────────────────────────────────────────────────

@app.get("/api/activity")
async def get_activity_logs(_=Depends(require_auth)):
    limit = 100
    logs = list(activity_logs)[-limit:]
    return {"logs": logs}

# ─── Backup ────────────────────────────────────────────────────────────────────

@app.get("/api/backup")
async def get_backup(_=Depends(require_auth)):
    async with LINKS_LOCK:
        links = dict(LINKS)
    async with SUBS_LOCK:
        subs = dict(SUBS)
    
    hist_dict = {}
    for day, hours in hourly_traffic_history.items():
        hist_dict[day] = dict(hours)
    
    return {
        "links": links,
        "subs": subs,
        "settings": SETTINGS,
        "hourly_traffic": dict(hourly_traffic),
        "hourly_traffic_history": hist_dict,
        "exported_at": datetime.now().isoformat(),
        "version": "14.0"
    }

@app.post("/api/backup/restore")
async def restore_backup(request: Request, _=Depends(require_auth)):
    global hourly_traffic_history
    try:
        body = await request.json()
        
        if "links" in body and isinstance(body["links"], dict):
            async with LINKS_LOCK:
                LINKS.clear()
                for uid, link_data in body["links"].items():
                    if not isinstance(link_data, dict):
                        continue
                    LINKS[uid] = link_data
        
        if "subs" in body and isinstance(body["subs"], dict):
            async with SUBS_LOCK:
                SUBS.clear()
                for sid, sub_data in body["subs"].items():
                    if not isinstance(sub_data, dict):
                        continue
                    SUBS[sid] = sub_data
        
        if "settings" in body and isinstance(body["settings"], dict):
            SETTINGS.update(body["settings"])
        
        if "hourly_traffic" in body:
            hourly_traffic.clear()
            hourly_traffic.update(body["hourly_traffic"])
        
        if "hourly_traffic_history" in body:
            hourly_traffic_history = defaultdict(lambda: defaultdict(int))
            for day, hours in body["hourly_traffic_history"].items():
                hourly_traffic_history[day] = defaultdict(int, hours)
        
        await save_state()
        log_activity("backup", "بکاپ بازیابی شد", "ok")
        return {"ok": True, "message": "بکاپ با موفقیت بازیابی شد"}
    except Exception as e:
        logger.error(f"Backup restore error: {e}")
        raise HTTPException(status_code=400, detail=f"خطا در بازیابی بکاپ: {str(e)}")

# ─── VLESS WebSocket Tunnel ────────────────────────────────────────────────

RELAY_BUF = 512 * 1024

def _ws_client_ip(ws: WebSocket) -> str:
    fwd = ws.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real_ip = ws.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return ws.client.host if ws.client else "نامشخص"

async def check_device_limit(uuid: str, client_ip: str) -> bool:
    async with LINKS_LOCK:
        link = LINKS.get(uuid)
        if not link:
            return False
        max_devices = link.get("max_devices", 0)
        if max_devices == 0:
            return True
    
    async with DEVICE_CONNECTIONS_LOCK:
        current_ips = device_connections.get(uuid, [])
        if client_ip in current_ips:
            return True
        if len(current_ips) >= max_devices:
            return False
        if uuid not in device_connections:
            device_connections[uuid] = []
        device_connections[uuid].append(client_ip)
        return True

async def parse_vless_header(chunk: bytes):
    if len(chunk) < 24:
        raise ValueError("chunk too small")
    pos = 1
    pos += 16
    addon_len = chunk[pos]
    pos += 1 + addon_len
    command = chunk[pos]
    pos += 1
    port = int.from_bytes(chunk[pos:pos+2], "big")
    pos += 2
    addr_type = chunk[pos]
    pos += 1
    if addr_type == 1:
        address = ".".join(str(b) for b in chunk[pos:pos+4])
        pos += 4
    elif addr_type == 2:
        dlen = chunk[pos]
        pos += 1
        address = chunk[pos:pos+dlen].decode("utf-8", errors="ignore")
        pos += dlen
    elif addr_type == 3:
        ab = chunk[pos:pos+16]
        pos += 16
        address = ":".join(f"{ab[i]:02x}{ab[i+1]:02x}" for i in range(0, 16, 2))
    else:
        raise ValueError(f"unknown addr type: {addr_type}")
    return command, address, port, chunk[pos:]

async def check_and_use(uid: str, n: int) -> bool:
    async with LINKS_LOCK:
        link = LINKS.get(uid)
        if link is None:
            return False
        if not is_link_allowed(link):
            return False
        link["used_bytes"] = link.get("used_bytes", 0) + n
        stats["total_bytes"] = stats.get("total_bytes", 0) + n
        
        now = datetime.now()
        day_key = now.strftime("%Y-%m-%d")
        hour_key = now.strftime("%H:00")
        hourly_traffic[hour_key] = hourly_traffic.get(hour_key, 0) + n
        hourly_traffic_history[day_key][hour_key] = hourly_traffic_history[day_key].get(hour_key, 0) + n
        quota_usage["bytes"] += n
        if not quota_usage.get("warned_99") and quota_usage["bytes"] >= QUOTA_WARN_BYTES:
            quota_usage["warned_99"] = True  # اخطار ۹۹ گیگ فقط یک‌بار در لاگ ثبت می‌شود
            log_activity("warning", "⚠️ مصرف کل به ۹۹ گیگابایت رسید — سقف ۱۰۰ گیگابایت نزدیک است", "warn")
        
        limit = link.get("limit_bytes", 0)
        used = link.get("used_bytes", 0)
        if limit > 0 and used / limit > 0.8 and not link.get("alert_80"):
            link["alert_80"] = True
            log_activity("warning", f"⚠️ مصرف کانفیگ {link.get('label')} به 80% رسید", "warn")
        
        return True

async def relay_ws_to_tcp(ws: WebSocket, writer: asyncio.StreamWriter, conn_id: str, uid: str):
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            if not data:
                continue
            if not await check_and_use(uid, len(data)):
                await ws.close(code=1008, reason="quota/disabled/unknown")
                break
            stats["total_requests"] = stats.get("total_requests", 0) + 1
            if conn_id in connections:
                connections[conn_id]["bytes"] = connections[conn_id].get("bytes", 0) + len(data)
            writer.write(data)
            if writer.transport.get_write_buffer_size() > RELAY_BUF:
                await writer.drain()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        try:
            writer.write_eof()
        except Exception:
            pass

async def relay_tcp_to_ws(ws: WebSocket, reader: asyncio.StreamReader, conn_id: str, uid: str):
    first = True
    try:
        while True:
            data = await reader.read(RELAY_BUF)
            if not data:
                break
            if not await check_and_use(uid, len(data)):
                await ws.close(code=1008, reason="quota/disabled/unknown")
                break
            if conn_id in connections:
                connections[conn_id]["bytes"] = connections[conn_id].get("bytes", 0) + len(data)
            payload = (b"\x00\x00" + data) if first else data
            first = False
            await ws.send_bytes(payload)
    except Exception:
        pass

@app.websocket("/ws/{uuid}")
async def websocket_tunnel(ws: WebSocket, uuid: str):
    await ws.accept()

    client_ip = _ws_client_ip(ws)
    
    async with LINKS_LOCK:
        link = LINKS.get(uuid)

    if not link:
        logger.warning(f"🚫 WS rejected uuid={uuid[:8]}… (user not found)")
        await ws.close(code=1008, reason="user not found")
        return

    if not is_link_allowed(link):
        logger.warning(f"🚫 WS rejected uuid={uuid[:8]}… (not allowed)")
        await ws.close(code=1008, reason="not authorized")
        return

    max_devices = link.get("max_devices", 0)
    if max_devices > 0:
        if not await check_device_limit(uuid, client_ip):
            logger.warning(f"🚫 Device limit exceeded for {uuid[:8]}… (max: {max_devices})")
            await ws.close(code=1008, reason="device limit exceeded")
            return

    conn_id = secrets.token_urlsafe(6)
    connections[conn_id] = {
        "uuid": uuid,
        "ip": client_ip,
        "transport": link.get("protocol", "vless-ws"),
        "connected_at": datetime.now().isoformat(),
        "bytes": 0,
    }
    
    logger.info(f"✅ WS [{conn_id}] uuid={uuid[:8]}… ip={client_ip} total={len(connections)}")
    log_activity("connection", f"اتصال جدید از {client_ip} (کانفیگ {link.get('label','?')})", "info")
    
    writer = None

    try:
        first_msg = await asyncio.wait_for(ws.receive(), timeout=15.0)
        if first_msg["type"] == "websocket.disconnect":
            return
        first_chunk = first_msg.get("bytes") or (first_msg.get("text") or "").encode()
        if not first_chunk:
            return

        command, address, port, payload = await parse_vless_header(first_chunk)

        if not await check_and_use(uuid, len(first_chunk)):
            await ws.close(code=1008, reason="quota/disabled")
            return

        stats["total_requests"] = stats.get("total_requests", 0) + 1
        if conn_id in connections:
            connections[conn_id]["bytes"] = connections[conn_id].get("bytes", 0) + len(first_chunk)
        logger.info(f"➡️  [{conn_id}] → {address}:{port}")

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port),
            timeout=10.0
        )
        sock = writer.transport.get_extra_info('socket')
        if sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024*1024)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024*1024)

        if payload:
            writer.write(payload)
            await writer.drain()

        done, pending = await asyncio.wait(
            {
                asyncio.create_task(relay_ws_to_tcp(ws, writer, conn_id, uuid)),
                asyncio.create_task(relay_tcp_to_ws(ws, reader, conn_id, uuid)),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        asyncio.create_task(save_state())

    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        stats["total_errors"] = stats.get("total_errors", 0) + 1
        error_logs.append({"error": "connection timeout", "time": datetime.now().isoformat()})
    except Exception as exc:
        stats["total_errors"] = stats.get("total_errors", 0) + 1
        error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        logger.error(f"WS error [{conn_id}]: {exc}")
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        connections.pop(conn_id, None)
        await remove_device_connection(uuid, client_ip)
        logger.info(f"🔌 WS closed [{conn_id}] total={len(connections)}")

# ─── ===== ساب‌لینک با طراحی جدید ===== ──────────────────────────────────

@app.get("/sub/{uuid}")
async def subscription_single(request: Request, uuid: str):
    import base64
    
    user_agent = request.headers.get("user-agent", "").lower()
    is_browser = any(b in user_agent for b in [
        "chrome", "firefox", "safari", "edge", "opera", "brave",
        "msie", "trident"
    ])
    
    async with LINKS_LOCK:
        link = LINKS.get(uuid)
    
    if not link:
        if is_browser:
            return FileResponse(WEB_DIR_EARLY / "404.html", status_code=404)
        else:
            raise HTTPException(status_code=404, detail="user not found")
    
    if not is_link_allowed(link):
        if is_browser:
            return FileResponse(WEB_DIR_EARLY / "403.html", status_code=403)
        else:
            raise HTTPException(status_code=403, detail="user disabled or expired")
    
    host = get_host()
    label = link.get("label", "کاربر")
    protocol = link.get("protocol", DEFAULT_PROTOCOL)
    http_version = link.get("http_version", "h2")
    fingerprint = link.get("fingerprint", "chrome")
    limit_bytes = link.get("limit_bytes", 0)
    used_bytes = link.get("used_bytes", 0)
    expires_at = link.get("expires_at")
    max_devices = link.get("max_devices", 0)
    
    percent = 0
    if limit_bytes > 0:
        percent = min(100, (used_bytes / limit_bytes) * 100)
    
    days_left = "نامحدود"
    if expires_at:
        try:
            clean_exp = expires_at.replace('Z', '').replace('+00:00', '')
            if 'T' in clean_exp:
                exp_date = datetime.fromisoformat(clean_exp)
            else:
                exp_date = datetime.fromisoformat(clean_exp)
            
            if exp_date.tzinfo is None:
                exp_date = exp_date.replace(tzinfo=IRAN_TZ)
            
            now = datetime.now(IRAN_TZ)
            days = (exp_date - now).days
            
            if days > 0:
                days_left = f"{days} روز"
            elif days == 0:
                days_left = "امروز"
            else:
                days_left = "منقضی"
        except:
            days_left = "نامشخص"
    
    remaining_bytes = limit_bytes - used_bytes if limit_bytes > 0 else 0
    remaining_val, remaining_unit = fmt_bytes_short(remaining_bytes) if remaining_bytes > 0 else ("∞", "")
    used_val, used_unit = fmt_bytes_short(used_bytes)
    limit_val, limit_unit = fmt_bytes_short(limit_bytes) if limit_bytes > 0 else ("∞", "")
    
    user_ip = "نامشخص"
    for c in connections.values():
        if c.get("uuid") == uuid:
            user_ip = c.get("ip", "نامشخص")
            break
    
    proto_icon = PROTOCOLS.get(protocol, PROTOCOLS["vless-ws"])["icon"]
    proto_name = PROTOCOLS.get(protocol, PROTOCOLS["vless-ws"])["name"]
    http_name = HTTP_VERSIONS.get(http_version, HTTP_VERSIONS["h2"])["name"]
    
    # ===== لینک اصلی =====
    main_remark = f"{proto_icon} {label} ({proto_name}+{http_name})"
    main_link = generate_vless_link(uuid, host, remark=main_remark, protocol=protocol, fingerprint=fingerprint, port=DEFAULT_PORT, http_version=http_version, fake_port=False)
    
    # ===== لینک‌های اضافی برای کلاینت =====
    time_remark = f"⏳ {days_left}"
    time_link = generate_vless_link(uuid, host, remark=time_remark, protocol=protocol, fingerprint=fingerprint, port=DEFAULT_PORT, http_version=http_version, fake_port=True)
    
    if limit_bytes > 0:
        volume_remark = f"📊 {remaining_val} {remaining_unit}" if remaining_bytes > 0 else "📊 0 B"
    else:
        volume_remark = "📊 ∞"
    volume_link = generate_vless_link(uuid, host, remark=volume_remark, protocol=protocol, fingerprint=fingerprint, port=DEFAULT_PORT, http_version=http_version, fake_port=True)
    
    # ===== اگر کلاینت (غیر مرورگر) =====
    if not is_browser:
        config_lines = [main_link, time_link, volume_link]
        content = "\n".join(config_lines)
        content_b64 = base64.b64encode(content.encode()).decode()
        
        return Response(
            content=content_b64,
            media_type="text/plain",
            headers={
                "Content-Disposition": f"attachment; filename=config_{uuid[:8]}.txt",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "profile-title": quote(f"{proto_icon} {label}"),
                "profile-update-interval": "1",
                "profile-web-page-url": f"https://{host}/info/{uuid}",
            }
        )
    
    # ===== اگر مرورگر: هدایت به صفحه‌ی اطلاعات =====
    return RedirectResponse(url=f"/info/{uuid}", status_code=307)
    return RedirectResponse(url=f"/info/{uuid}", status_code=307)




@app.get("/sub-all")
async def subscription_all(_=Depends(require_auth)):
    import base64
    host = get_host()
    async with LINKS_LOCK:
        lines = []
        for uid, d in LINKS.items():
            if is_link_allowed(d):
                fp = d.get("fingerprint", "chrome")
                protocol = d.get("protocol", DEFAULT_PROTOCOL)
                http_version = d.get("http_version", "h2")
                label = d.get("label", "کاربر")
                proto_icon = PROTOCOLS.get(protocol, PROTOCOLS["vless-ws"])["icon"]
                remark = f"{proto_icon} {label}"
                lines.append(generate_vless_link(uid, host, remark=remark, protocol=protocol, fingerprint=fp, port=DEFAULT_PORT, http_version=http_version, fake_port=False))
    content = base64.b64encode("\n".join(lines).encode()).decode()
    return Response(content=content, media_type="text/plain")


@app.get("/sub-group/{uuid_key}")
async def sub_group_subscription(uuid_key: str, request: Request):
    import base64
    async with SUBS_LOCK:
        sub = next((s for s in SUBS.values() if s.get("uuid_key") == uuid_key), None)
    if not sub:
        raise HTTPException(status_code=404, detail="not found")

    if sub.get("password_hash"):
        pw = request.query_params.get("pw", "")
        if pw != sub["password_hash"]:
            raise HTTPException(status_code=403, detail="wrong password")

    host = get_host()
    link_ids = sub.get("link_ids", [])
    async with LINKS_LOCK:
        lines = []
        for lid in link_ids:
            link = LINKS.get(lid)
            if link and is_link_allowed(link):
                fp = link.get("fingerprint", "chrome")
                protocol = link.get("protocol", DEFAULT_PROTOCOL)
                http_version = link.get("http_version", "h2")
                label = link.get("label", "کاربر")
                proto_icon = PROTOCOLS.get(protocol, PROTOCOLS["vless-ws"])["icon"]
                remark = f"{proto_icon} {label}"
                lines.append(generate_vless_link(lid, host, remark=remark, protocol=protocol, fingerprint=fp, port=DEFAULT_PORT, http_version=http_version, fake_port=False))

    content = base64.b64encode("\n".join(lines).encode()).decode()
    return Response(
        content=content,
        media_type="text/plain",
        headers={
            "profile-title": quote(sub["name"]),
            "profile-update-interval": "12",
        }
    )



async def _group_payload(uuid_key: str) -> dict:
    """ساخت داده‌ی کامل گروه اشتراک (مشترک بین صفحه و API)"""
    async with SUBS_LOCK:
        sub = next((dict(s) for s in SUBS.values() if s.get("uuid_key") == uuid_key), None)
    if not sub:
        raise HTTPException(status_code=404, detail="این لینک گروه اشتراک معتبر نیست یا گروه حذف شده است.")

    expired = False
    if sub.get("expires_at"):
        try:
            expired = datetime.now() > datetime.fromisoformat(str(sub["expires_at"]))
        except Exception:
            expired = False

    host = get_host()
    page_url = f"https://{host}/group/{uuid_key}"
    sub_url = f"https://{host}/sub-group/{uuid_key}"

    async with LINKS_LOCK:
        members = []
        for lid in sub.get("link_ids") or []:
            l = LINKS.get(lid)
            if not l:
                continue
            allowed = is_link_allowed(l)
            proto = l.get("protocol", DEFAULT_PROTOCOL)
            http_v = l.get("http_version", "h2")
            fp = l.get("fingerprint", "chrome")
            label = l.get("label", "کاربر")
            proto_icon = PROTOCOLS.get(proto, PROTOCOLS["vless-ws"])["icon"]
            proto_name = PROTOCOLS.get(proto, PROTOCOLS["vless-ws"])["name"]
            http_name = HTTP_VERSIONS.get(http_v, HTTP_VERSIONS["h2"])["name"]
            link = generate_vless_link(lid, host, remark=f"🏛️ {label}", protocol=proto,
                                       fingerprint=fp, port=DEFAULT_PORT, http_version=http_v, fake_port=False)
            members.append({
                "uuid": lid,
                "label": label,
                "vless_link": link,
                "allowed": allowed,
                "expired": is_link_expired(l),
                "inactive": l.get("active") is False,
                "days_left": _tg_days_left(l.get("expires_at")),
                "used_fmt": fmt_bytes(l.get("used_bytes") or 0),
                "limit_fmt": fmt_bytes(l["limit_bytes"]) if (l.get("limit_bytes") or 0) > 0 else "نامحدود",
                "proto_icon": proto_icon,
                "proto_name": proto_name,
                "http_name": http_name,
            })

    return {
        "name": sub.get("name") or "گروه اشتراک",
        "page_url": page_url,
        "sub_url": sub_url,
        "expires_at": sub.get("expires_at"),
        "expired": expired,
        "days_left": _tg_days_left(sub.get("expires_at")),
        "created_at": sub.get("created_at"),
        "version": PANEL_VERSION,
        "members": members,
    }


@app.get("/group/{uuid_key}")
async def group_public_page(uuid_key: str, request: Request):
    """مرورگر: صفحه‌ی گروه با QR و کپی (web/group.html)؛ کلاینت: ساب base64"""
    user_agent = request.headers.get("user-agent", "").lower()
    is_browser = any(b in user_agent for b in [
        "chrome", "firefox", "safari", "edge", "opera", "brave", "msie", "trident"
    ])

    async with SUBS_LOCK:
        exists = any(s.get("uuid_key") == uuid_key for s in SUBS.values())
    if not exists:
        if is_browser:
            return FileResponse(WEB_DIR / "404.html", status_code=404)
        raise HTTPException(status_code=404, detail="group not found")

    if not is_browser:
        data = await _group_payload(uuid_key)
        lines = [m["vless_link"] for m in data["members"] if m["allowed"] and not data["expired"]]
        content = base64.b64encode("\n".join(lines).encode()).decode()
        return Response(
            content=content,
            media_type="text/plain",
            headers={
                "profile-title": quote(str(data["name"])),
                "profile-update-interval": "12",
                "profile-web-page-url": data["page_url"],
            }
        )

    return FileResponse(WEB_DIR / "group.html")


@app.get("/api/group/{uuid_key}")
async def group_api(uuid_key: str):
    return JSONResponse(content=await _group_payload(uuid_key))


@app.get("/info/{uuid}")
async def info_page(uuid: str, request: Request):
    """صفحه‌ی عمومی اطلاعات کانفیگ (سبک PXpanel) — داده‌ها از /api/info/{uuid}"""
    return FileResponse(WEB_DIR / "sub.html")

@app.get("/api/info/{uuid}")
async def info_api(uuid: str, request: Request):
    async with LINKS_LOCK:
        link = LINKS.get(uuid)
    
    if not link:
        raise HTTPException(status_code=404, detail="لینک اطلاعات معتبر نیست یا کاربر حذف شده است.")
    
    if not is_link_allowed(link):
        raise HTTPException(status_code=403, detail="این کانفیگ فعال نیست یا تاریخ انقضای آن گذشته است.")
    
    host = get_host()
    label = link.get("label", "کاربر")
    protocol = link.get("protocol", DEFAULT_PROTOCOL)
    http_version = link.get("http_version", "h2")
    fingerprint = link.get("fingerprint", "chrome")
    limit_bytes = link.get("limit_bytes", 0)
    used_bytes = link.get("used_bytes", 0)
    expires_at = link.get("expires_at")
    max_devices = link.get("max_devices", 0)
    
    percent = 0
    if limit_bytes > 0:
        percent = min(100, (used_bytes / limit_bytes) * 100)
    
    days_left = "نامحدود"
    if expires_at:
        try:
            clean_exp = expires_at.replace('Z', '').replace('+00:00', '')
            if 'T' in clean_exp:
                exp_date = datetime.fromisoformat(clean_exp)
            else:
                exp_date = datetime.fromisoformat(clean_exp)
            
            if exp_date.tzinfo is None:
                exp_date = exp_date.replace(tzinfo=IRAN_TZ)
            
            now = datetime.now(IRAN_TZ)
            days = (exp_date - now).days
            
            if days > 0:
                days_left = f"{days} روز"
            elif days == 0:
                days_left = "امروز"
            else:
                days_left = "منقضی"
        except:
            days_left = "نامشخص"
    
    user_ip = "نامشخص"
    for c in connections.values():
        if c.get("uuid") == uuid:
            user_ip = c.get("ip", "نامشخص")
            break
    
    remaining_bytes = limit_bytes - used_bytes if limit_bytes > 0 else 0
    remaining_val, remaining_unit = fmt_bytes_short(remaining_bytes) if remaining_bytes > 0 else ("∞", "")
    
    proto_icon = PROTOCOLS.get(protocol, PROTOCOLS["vless-ws"])["icon"]
    proto_name = PROTOCOLS.get(protocol, PROTOCOLS["vless-ws"])["name"]
    http_name = HTTP_VERSIONS.get(http_version, HTTP_VERSIONS["h2"])["name"]
    
    main_remark = f"{proto_icon} {label} ({proto_name}+{http_name})"
    main_link = generate_vless_link(uuid, host, remark=main_remark, protocol=protocol, fingerprint=fingerprint, port=DEFAULT_PORT, http_version=http_version, fake_port=False)
    
    time_remark = f"⏳ {days_left}"
    time_link = generate_vless_link(uuid, host, remark=time_remark, protocol=protocol, fingerprint=fingerprint, port=DEFAULT_PORT, http_version=http_version, fake_port=True)
    
    if limit_bytes > 0:
        volume_remark = f"📊 {remaining_val} {remaining_unit}" if remaining_bytes > 0 else "📊 0 B"
    else:
        volume_remark = "📊 ∞"
    volume_link = generate_vless_link(uuid, host, remark=volume_remark, protocol=protocol, fingerprint=fingerprint, port=DEFAULT_PORT, http_version=http_version, fake_port=True)
    
    active_connections_list = []
    for c in connections.values():
        if c.get("uuid") == uuid:
            active_connections_list.append(c)
    active_connections_count = len(active_connections_list)
    
    last_connected = None
    for c in connections.values():
        if c.get("uuid") == uuid:
            if not last_connected or c.get("connected_at") > last_connected:
                last_connected = c.get("connected_at")
    
    used_val, used_unit = fmt_bytes_short(used_bytes)
    limit_val, limit_unit = fmt_bytes_short(limit_bytes) if limit_bytes > 0 else ("∞", "")
    
    link_data = {
        **link,
        "expired": is_link_expired(link),
        "active_connections": active_connections_count,
        "active_connections_list": active_connections_list,
        "last_connected_at": last_connected,
        "uuid": uuid,
        "vless_links": [main_link, time_link, volume_link],
        "vless_link": main_link,
        "sub_url": f"https://{host}/sub/{uuid}",
        "user_ip": user_ip,
        "percent": percent,
        "days_left": days_left,
        "used_fmt": fmt_bytes(used_bytes),
        "used_val": used_val,
        "used_unit": used_unit,
        "limit_fmt": fmt_bytes(limit_bytes) if limit_bytes > 0 else "نامحدود",
        "limit_val": limit_val,
        "limit_unit": limit_unit,
        "max_devices": max_devices,
        "time_link": time_link,
        "volume_link": volume_link,
        "main_link": main_link,
        "remaining_val": remaining_val,
        "remaining_unit": remaining_unit,
        "protocol_name": proto_name,
        "protocol_icon": proto_icon,
        "http_name": http_name,
        "fingerprint": fingerprint,
        "label": label,
        "panel_version": PANEL_VERSION,
    }
    
    return JSONResponse(content=link_data)


# ─── API: Railway (اعتبار حساب ریلوی) ────────────────────────────────────────

@app.post("/api/settings/railway")
async def set_railway_token(request: Request, _=Depends(require_auth)):
    body = await request.json()
    token = (body.get("token") or "").strip()
    if not token or len(token) < 10:
        raise HTTPException(status_code=400, detail="توکن ریلوی نامعتبر است")
    SETTINGS["railway_token"] = token
    await save_state()
    log_activity("system", "🔑 توکن ریلوی ثبت شد", "ok")
    info = await verify_token(token)
    if not info.get("ok"):
        return {"ok": True, "saved": True, "verified": False, "error": info.get("error", "")}
    return {"ok": True, "saved": True, "verified": True, "account": info.get("account", "")}

@app.delete("/api/settings/railway")
async def delete_railway_token(_=Depends(require_auth)):
    SETTINGS.pop("railway_token", None)
    SETTINGS.pop("railway_cache", None)
    await save_state()
    return {"ok": True}

@app.get("/api/railway/status")
async def railway_status(_=Depends(require_auth)):
    token = SETTINGS.get("railway_token")
    return {"has_token": bool(token), "account": SETTINGS.get("railway_account", "")}

@app.get("/api/railway/credit")
async def railway_credit(_=Depends(require_auth)):
    token = SETTINGS.get("railway_token")
    if not token:
        return {"ok": False, "reason": "no_token"}
    info = await get_credit_summary(token)
    if info.get("ok") and info.get("account"):
        SETTINGS["railway_account"] = info["account"]
    return info


# ─── Web (فایل‌های استاتیک — به‌جای pages.py) ─────────────────────────────

WEB_DIR = Path(__file__).resolve().parent / "web"

def _web_file(name: str) -> FileResponse:
    path = WEB_DIR / name
    if not path.is_file():
        path = WEB_DIR / "index.html"
    return FileResponse(path)

@app.get("/assets/{fname:path}")
async def web_assets(fname: str):
    """فایل‌های استاتیک UI (css/js) — با محافظت از path traversal"""
    root = (WEB_DIR / "assets").resolve()
    path = (root / fname).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        raise HTTPException(status_code=404)
    media = "text/css" if path.suffix == ".css" else "application/javascript" if path.suffix == ".js" else "application/octet-stream"
    return FileResponse(path, media_type=media)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/dashboard")
    return FileResponse(WEB_DIR / "index.html")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not await is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/login")
    return FileResponse(WEB_DIR / "index.html")

@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(WEB_DIR / "index.html")

@app.get("/favicon.ico")
async def favicon():
    path = WEB_DIR / "favicon.svg"
    if path.is_file():
        return FileResponse(path, media_type="image/svg+xml")
    raise HTTPException(status_code=404)

# ─── اتصال روت‌های XHTTP به اپ (در نسخه‌ی قبل جا مانده بود) ───
app.include_router(xhttp_router)

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=CONFIG["port"], log_level="info", workers=1)
