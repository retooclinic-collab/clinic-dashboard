# -*- coding: utf-8 -*-
r"""
청담리투의원 — "PayPal 결제수령 메일 → 슬랙 자동알림"
=====================================================
목적: 해외 환자가 페이팔로 예약금($25 등)을 내면 PayPal이 retooclinic@gmail.com 으로
      "결제대금 수령 출처: <이메일>" 메일을 보낸다. 그 메일을 Gmail API로 읽어
      입금알림과 같은 슬랙 채널(상담팀 + #00-데스크-daily)에 통지한다.

왜 메일인가 (2026-09-14 원장 결정):
  - 페이팔 잔액은 국내통장으로 바로 안 빠지므로 CODEF 통장폴링엔 안 잡힌다.
  - 페이팔 웹훅(paypal_alert.py)은 개발자약관 동의·앱 발급이 걸려 보류 → 메일이 준비물 최소.
  - deposit-alert.yml 의 3분 루프 뒤에 한 줄 얹어 같은 주기·같은 웹훅·같은 Firestore를 쓴다.

알림 내용: 메일 본문 문장 그대로 + 구매자 정보 / 거래 ID / 배송 정보 / 내역·총계.
  ※ 결제자 이름·주소가 슬랙에 뜬다(환자 개인정보) → 비공개 채널에만 보낼 것.

중복방지: Firestore `paypal_mail_state/main` {sent_ids:[gmail message id...]}.
  - 상태 문서가 아예 없는 첫 실행은 **자동 시드**(조회된 메일을 전부 '보낸 것'으로만 표시, 전송 안 함).
    → 배포 첫 사이클에 과거 메일이 우르르 나가는 사고를 막는다.
  - 상태 읽기 실패(할당량 등)는 발송 포기(deposit_slack_notifier 의 2026-09-03 교훈 그대로).

환경변수:
  GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN : gmail.readonly OAuth
      (발급기: E:\_클로드작업기록\gws계정\gmail_oauth_token.py → gmail_token.json)
  FIREBASE_SA_JSON        : 서비스계정 JSON(문자열) — Firestore
  SLACK_WEBHOOK_URLS / SLACK_WEBHOOK_URL : 입금알림과 동일
  PAYPAL_MAIL_LOOKBACK    : Gmail 검색 범위(일, 기본 2). 루프가 밤에 멈춰도 다음날 아침 잡힘.
모드:
  --dry-run : Gmail만 읽어 파싱 결과·슬랙 문안을 출력(Firestore·슬랙 안 씀). 로컬 검증용.
  (기본)    : 신규 메일만 슬랙 전송 + 상태 저장.
"""
import os, sys, re, json, base64, html, urllib.request, urllib.parse
from html.parser import HTMLParser

LOOKBACK   = int(os.environ.get("PAYPAL_MAIL_LOOKBACK", "2"))
STATE_DOC  = os.environ.get("PAYPAL_STATE_DOC", "paypal_mail_state/main")
GMAIL_Q    = f'from:service@intl.paypal.com subject:"결제대금 수령" newer_than:{LOOKBACK}d'
GMAIL_API  = "https://gmail.googleapis.com/gmail/v1/users/me/"

# ── 발송 채널 (deposit_slack_notifier 와 동일 규칙) ─────────────────────────
def _webhook_list():
    raw = os.environ.get("SLACK_WEBHOOK_URLS", "") + "," + os.environ.get("SLACK_WEBHOOK_URL", "")
    seen, out = set(), []
    for u in raw.replace(chr(10), ",").split(","):
        u = u.strip()
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out
SLACK_WEBHOOKS = _webhook_list()

# ── Gmail ────────────────────────────────────────────────────────────────
def _http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def gmail_token():
    body = urllib.parse.urlencode({
        "client_id": os.environ["GMAIL_CLIENT_ID"], "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"], "grant_type": "refresh_token"}).encode()
    return _http("https://oauth2.googleapis.com/token", body,
                 {"Content-Type": "application/x-www-form-urlencoded"})["access_token"]

def gmail_list(tok):
    url = GMAIL_API + "messages?" + urllib.parse.urlencode({"q": GMAIL_Q, "maxResults": 50})
    d = _http(url, headers={"Authorization": "Bearer " + tok})
    return [m["id"] for m in d.get("messages", [])]

def gmail_get(tok, mid):
    return _http(GMAIL_API + f"messages/{mid}?format=full", headers={"Authorization": "Bearer " + tok})

class _Text(HTMLParser):
    """HTML → 줄 단위 텍스트. 블록 요소 경계마다 줄바꿈."""
    _BLOCK = {"p", "div", "tr", "br", "li", "h1", "h2", "h3", "table", "td", "th"}
    def __init__(self):
        super().__init__(); self.out = []; self._skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"): self._skip += 1
        if tag in self._BLOCK: self.out.append("\n")
    def handle_endtag(self, tag):
        if tag in ("style", "script"): self._skip -= 1
        if tag in self._BLOCK: self.out.append("\n")
    def handle_data(self, data):
        if not self._skip: self.out.append(data)
    def text(self):
        s = html.unescape("".join(self.out))
        lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in s.splitlines()]
        return "\n".join(ln for ln in lines if ln)

def _walk(part):
    yield part
    for p in part.get("parts", []) or []:
        yield from _walk(p)

def body_text(msg):
    """text/plain 우선, 없으면 HTML을 텍스트로. PayPal 메일은 HTML 단일 파트."""
    plain, htm = None, None
    for p in _walk(msg["payload"]):
        data = (p.get("body") or {}).get("data")
        if not data: continue
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        mt = p.get("mimeType", "")
        if mt == "text/plain" and plain is None: plain = raw
        elif mt == "text/html" and htm is None: htm = raw
    if plain and "결제대금" in plain:
        return "\n".join(ln.strip() for ln in plain.splitlines() if ln.strip())
    t = _Text(); t.feed(htm or plain or ""); return t.text()

def header(msg, name):
    for h in msg["payload"].get("headers", []):
        if h["name"].lower() == name.lower(): return h["value"]
    return ""

# ── 파싱 ─────────────────────────────────────────────────────────────────
# 본문은 "라벨 줄 → 값 줄(들)" 구조. 값은 다음 라벨이 나올 때까지의 줄 전부(배송지는 5~6줄).
LABELS = ("거래 ID", "거래 날짜", "구매자 정보", "구매자의 안내", "배송 정보", "배송 방법",
          "설명", "단가", "수량", "금액", "보험:", "총계:", "도움말 및 문의하기")

def blocks(text):
    out, cur = {}, None
    for ln in text.splitlines():
        key = next((l for l in LABELS if ln.startswith(l)), None)
        if key:
            cur = key; out.setdefault(cur, [])
            rest = ln[len(key):].strip(" :")
            if rest: out[cur].append(rest)          # "거래 ID 5PW..." 처럼 한 줄에 붙은 경우
        elif cur:
            out[cur].append(ln)
    return {k: [x.replace("​", "").strip() for x in v if x.strip()] for k, v in out.items()}   # 제로폭 공백 제거

def parse(msg):
    text = body_text(msg)
    sent = re.search(r"\(?[^\s()]+@[^\s()]+\)?\s*님으로부터\s*[^\n]*?결제대금을 받았습니다\.?", text)
    sentence = sent.group(0).strip() if sent else header(msg, "Subject")   # 형식 바뀌어도 제목은 남긴다
    amt = re.search(r"([$€£¥]?\s?[\d,]+\.\d{2}\s*[A-Z]{3})", sentence)
    b = blocks(text)
    tx = next((x for x in b.get("거래 ID", []) if re.fullmatch(r"[A-Z0-9]{10,}", x)), "")
    note = " ".join(b.get("구매자의 안내", []))
    # 내역: 표 머리글이 "설명/단가/수량/금액" 네 줄이라 실제 행은 마지막 머리글 "금액" 블록에 붙는다
    #       = [상품명, "상품: #"(옵션), 단가, 수량, 금액] → 금액/수량 줄을 걸러내면 첫 줄이 상품명
    desc = [x for x in (b.get("금액") or b.get("설명") or []) if not re.match(r"^(상품:|[$€£¥]|[\d,]+$)", x)]
    total = next((x for x in b.get("총계:", []) if re.search(r"\d", x)), "")
    return {
        "sentence": sentence, "amount": (amt.group(1).strip() if amt else ""),
        "tx_id": tx, "date": " ".join(b.get("거래 날짜", [])),
        "buyer": " ".join(b.get("구매자 정보", [])),
        "note": "" if note in ("", "제공된 것 없음") else note,
        "ship": " ".join(b.get("배송 정보", [])),
        "ship_method": " ".join(b.get("배송 방법", [])),
        "item": desc[0] if desc else "", "total": total,
        "subject": header(msg, "Subject"), "received": header(msg, "Date"),
    }

# ── Slack ────────────────────────────────────────────────────────────────
def slack_text(p):
    lines = ["🤖 클로드 AI가 알려드립니다", "💳 PayPal 결제 수령", p["sentence"], ""]
    def add(k, v):
        if v: lines.append(f"• {k} : {v}")
    add("거래 ID", p["tx_id"])
    add("거래일", p["date"])
    add("구매자", p["buyer"])
    add("구매자 안내", p["note"])
    add("배송지", p["ship"])
    add("내역", " / ".join(x for x in (p["item"], p["total"] or p["amount"]) if x))
    return "\n".join(lines)

def post_slack(text):
    if not SLACK_WEBHOOKS:
        raise RuntimeError("SLACK_WEBHOOK_URLS / SLACK_WEBHOOK_URL 미설정 — 운영모드 불가")
    ok, errs = 0, []
    for url in SLACK_WEBHOOKS:
        req = urllib.request.Request(url, data=json.dumps({"text": text}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300: ok += 1
                else: errs.append(f"...{url[-10:]} HTTP {resp.status}")
        except Exception as e:
            errs.append(f"...{url[-10:]} {e}")
    if errs: print("slack warn:", "; ".join(errs), flush=True)
    if ok == 0: raise RuntimeError("슬랙 전 채널 발송 실패: " + "; ".join(errs))
    return ok

# ── Firestore 상태 ───────────────────────────────────────────────────────
class StateUnavailable(RuntimeError): pass

def init_db():
    import firebase_admin
    from firebase_admin import credentials, firestore
    sa = os.environ.get("FIREBASE_SA_JSON")
    cred = credentials.Certificate(json.loads(sa)) if sa else \
           credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    if not firebase_admin._apps: firebase_admin.initialize_app(cred)
    return firestore.client()

def load_state(db):
    """반환 (state, exists). 읽기 실패는 예외 → 발송 포기."""
    col, doc = STATE_DOC.split("/", 1)
    try:
        snap = db.collection(col).document(doc).get()
    except Exception as e:
        raise StateUnavailable("상태 읽기 실패 — 이번 사이클 건너뜀: %s" % e) from e
    return ((snap.to_dict() or {}) if snap.exists else {}), snap.exists

def save_state(db, seen):
    col, doc = STATE_DOC.split("/", 1)
    db.collection(col).document(doc).set({"sent_ids": sorted(seen)[-4000:]})

# ── main ─────────────────────────────────────────────────────────────────
def cli_dry_run():
    tok = gmail_token(); ids = gmail_list(tok)
    print(f"Gmail 검색 [{GMAIL_Q}] → {len(ids)}건")
    for mid in ids:
        p = parse(gmail_get(tok, mid))
        print("─" * 60); print("id:", mid, "|", p["subject"], "|", p["received"])
        print(json.dumps({k: v for k, v in p.items() if k not in ("subject", "received")}, ensure_ascii=False, indent=1))
        print(">> 슬랙 문안:\n" + slack_text(p))
    print("(dry-run: 슬랙·Firestore 안 씀)")

def cli_live():
    tok = gmail_token(); ids = gmail_list(tok)
    if not ids:
        print("신규 없음(검색 0건)."); return
    db = init_db()
    try:
        st, exists = load_state(db)
    except StateUnavailable as e:
        print("SKIP:", e, flush=True); return
    seen = set(st.get("sent_ids", []))
    if not exists:                           # 첫 실행 = 자동 시드(전송 안 함)
        save_state(db, set(ids))
        print(f"첫 실행 — 기존 {len(ids)}건 발송처리만(전송 안 함)."); return
    new_ids = [m for m in ids if m not in seen]
    if not new_ids:
        print(f"신규 없음. 누적 {len(seen)}건."); return
    sent = 0
    try:
        for mid in new_ids:
            p = parse(gmail_get(tok, mid))
            post_slack(slack_text(p)); seen.add(mid); sent += 1
            print("전송:", p["sentence"][:60], p["tx_id"])
    finally:
        save_state(db, seen)                 # 중간 실패해도 보낸 것까진 기록(중복 방지)
    print(f"전송 {sent}건. 누적 {len(seen)}건.")

if __name__ == "__main__":
    cli_dry_run() if "--dry-run" in sys.argv else cli_live()
