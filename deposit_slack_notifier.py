# -*- coding: utf-8 -*-
"""
청담리투의원 — "카드사 외 입금(고객 현금이체) → 슬랙 자동알림"
================================================================
목적: 병원 입금통장(하나 …23207)에 들어온 입금 중, 카드사·공단·이자·대표자전거래를
      제외한 나머지(=고객 현금이체 후보)를 상담팀 슬랙에 자동 통지.

설계(원장 확정 2026-08-11):
  - 방식: 자동 상시 폴링(운영은 GitHub Actions 헤드리스, 로그인 없음)
  - 대상: 하나 …23207 1개 계좌
  - 알림내용: 입금자명 + 금액 + 시간
  - 판정 = "제외리스트" 방식(기본 통과, 아래 4종만 제외):
      ① 카드사입금(카드매출입금)  ② 공단/보험(보험청구입금)  ③ 이자수입
      ④ 대표 자전거래(cfSelf: 김재림 / 제이알케이 5천만↑ / 청담리투의원 타행송금)
    → 나머지 입금은 사람이름이든 회사명이든 전부 통과(회사명으로 넣는 고객 포함).

두 가지 모드:
  --dry-run : 슬랙 안 쏨. 최근 N일 입금건을 판정해서 "보낼/제외" 목록만 출력(검토용).
  (기본)    : 신규 입금만 슬랙 웹훅으로 전송 + 상태파일에 마지막 처리지점 저장(중복방지).

데이터 소스: Firestore `bank_transactions` (collector_bank.py가 매일 적재).
  ※ 준실시간이 필요하면 별도 CODEF 빠른폴링으로 이 컬렉션을 자주 갱신(추후).

환경변수:
  FIREBASE_SA_JSON        : 서비스계정 JSON(문자열) — Firestore 읽기
  또는 GOOGLE_APPLICATION_CREDENTIALS : SA JSON 파일경로
  SLACK_WEBHOOK_URLS      : 발송할 Incoming Webhook URL 목록(쉼표/줄바꿈 구분)
                            예) 상담팀 + 00-데스크-daily 두 채널 동시발송
  SLACK_WEBHOOK_URL       : 단일 웹훅(하위호환). URLS와 같이 써도 됨(중복 자동제거)
  TARGET_ACCT_SUFFIX      : 대상계좌 끝자리(기본 "23207")
  LOOKBACK_DAYS           : --dry-run/--seed 의 조회 일수(기본 14).
                            ※ 운영 폴링은 이 값과 무관하게 **오늘 하루치만** 읽는다(할당량 보호).
  STATE_PATH              : 중복방지 상태파일(기본 E:\...\deposit_notifier_state.json)
"""
import os, sys, json, datetime, urllib.request

TARGET_SUFFIX = os.environ.get("TARGET_ACCT_SUFFIX", "23207")
LOOKBACK      = int(os.environ.get("LOOKBACK_DAYS", "14"))
STATE_PATH    = os.environ.get(
    "STATE_PATH",
    r"E:\Claude\Projects\클로드 Cowork\card-automation\deposit_notifier_state.json")
# ── 발송 채널 ─────────────────────────────────────────────────────────────
# 여러 채널 동시발송. 채널마다 Incoming Webhook URL이 따로 발급되므로 목록으로 받는다.
def _webhook_list():
    raw = os.environ.get("SLACK_WEBHOOK_URLS", "") + "," + os.environ.get("SLACK_WEBHOOK_URL", "")
    seen, out = set(), []
    for u in raw.replace(chr(10), ",").split(","):
        u = u.strip()
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out

SLACK_WEBHOOKS = _webhook_list()
COLLECTION    = os.environ.get("FIRESTORE_COLLECTION", "bank_transactions")

# ── 입금자명 추출기 (★ collector counterparty 신뢰 안 함) ────────────────────
# 하나 저축예금 적요 4칸 구조: [상대/라벨 │ 거래종류/계좌 │ 실제이름/기관 │ 은행/부서]
# 토스(비바리퍼블리카) 등 중계 라벨이 1번칸이면 실제 송금인은 3번칸에 있음 → 칸 전체 스캔.
_LABELS = {"타행이체", "대체", "자동이체", "이체", "입금이체", "출금", "입금",
           "펌뱅킹", "전자금융", "저축예금", "매출대금", "cms", "cbs", "카드대금"}
_PLATFORM = {"(주)비바리퍼블리카", "비바리퍼블리카", "비바리퍼블리카(주)"}  # 결제중계 → 이름 아님
_BANK_HINT = ("은행", "뱅킹", "저축은행", "카카오뱅크", "케이뱅크", "토스뱅크")
_SETTLE_HINT = ("자금결제부", "결제부", "센터", "지점", "여의도", "플랫폼", "마케팅팀", "정산")

def _is_accountish(s):
    return sum(c.isdigit() for c in s) >= 5           # 계좌/전표번호류

def _looks_name(s):
    s = (s or "").strip()
    if not s or s.lower() in _LABELS or s in _PLATFORM:
        return False
    if _is_accountish(s):
        return False
    if any(h in s for h in _BANK_HINT):
        return False
    if any(h in s for h in _SETTLE_HINT):
        return False
    return True

def best_name(parts):
    """적요칸들 중 실제 사람/회사 이름으로 보이는 첫 토큰. 없으면 '' (=정산성)."""
    for p in (parts or []):
        if _looks_name(p):
            return p.strip()
    return ""

# ── 제외 판정 ─────────────────────────────────────────────────────────────
# collector_bank.py 의 IN_VENDOR 분류와 일치. category 값으로 1차 제외.
EXCLUDE_CATEGORIES = {"카드매출입금", "보험청구입금", "이자수입"}
# 확실한 공과금/환급/기관성 입금 = 제외 (이름이 있어도). 세금환급·4대보험환급 등.
INSTITUTIONAL_HINTS = ("공단", "건강보험", "국민연금", "고용보험", "산재보험", "근로복지",
                       "국고", "국세", "지방세", "세무서", "환급", "연금공단")

def is_self_transfer(counterparty: str, desc: str, amount: int) -> bool:
    """대표 자금이동(자전거래) = clinic-final.html cfSelf 규칙과 동일."""
    cp = f"{counterparty} {desc}"
    if "김재림" in cp:
        return True
    if "제이알케이" in cp and amount >= 50_000_000:
        return True
    if "청담리투의원" in counterparty and "타행송금" in desc:
        return True
    return False

def decision(row: dict):
    """(send: bool, reason: str) 반환."""
    if row["inout"] != "입금":
        return False, "출금/기타"
    cat = row.get("category", "")
    if cat in EXCLUDE_CATEGORIES:
        return False, {"카드매출입금": "카드사입금", "보험청구입금": "공단/보험",
                       "이자수입": "이자"}[cat]
    if is_self_transfer(row.get("counterparty", ""), row.get("desc", ""), row.get("amount", 0)):
        return False, "대표자전거래"
    blob = f"{row.get('counterparty','')} {row.get('desc','')}"
    if any(h in blob for h in INSTITUTIONAL_HINTS):
        return False, "공과금/환급"           # 세금환급·4대보험환급·공단 등
    if not row.get("name"):
        return False, "정산성(이름없음)"     # 매출대금/삼성센터 등 카드·PG정산(=카드입금 동급)
    return True, "고객이체(후보)"

# ── Firestore ────────────────────────────────────────────────────────────
def init_db():
    import firebase_admin
    from firebase_admin import credentials, firestore
    sa = os.environ.get("FIREBASE_SA_JSON")
    if sa:
        cred = credentials.Certificate(json.loads(sa))
    else:
        cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()

def kst_today():
    """러너는 UTC다. 진료시간(KST 09~22시)엔 UTC 날짜와 같지만, 헷갈리지 않게 명시적으로 계산한다."""
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date()

def fetch_deposits(db, days=None):
    """운영(3분 폴링)에서는 **오늘 하루치만** 읽는다.

    ★ 2026-09-04 변경 — Firestore 일일 읽기 할당량 보호.
      종전엔 LOOKBACK_DAYS(운영 yml=2)로 3일치를 매 사이클 긁었다. 3~4분마다, 하루 200사이클이면
      같은 과거 행을 수백 번 다시 읽는 셈이다. 2026-09-03 12:53 실제로 할당량이 말라
      중복방지 상태를 못 읽고 중복 발송 + 이후 수집기 연쇄 크래시로 이어졌다.
      알림은 '오늘 들어온 입금'만 보면 되고 중복방지는 Firestore 상태가 따로 하므로 하루치로 충분하다.
      (놓친 과거분 점검이 필요하면 --dry-run 이 days 를 명시적으로 받는다.)
    """
    rows = []
    if days is None:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)   # KST
        if now.hour < 10:
            # 루프는 22시에 멈춘다. 어제 21:58(마지막 폴링) 이후 들어온 입금은 어제 못 잡았으므로
            # 하루 첫 시간대에만 어제분까지 훑는다. 나머지 시간은 오늘 하루치만(할당량 보호).
            cutoff = (now.date() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            q = db.collection(COLLECTION).where("date", ">=", cutoff)
        else:
            q = db.collection(COLLECTION).where("date", "==", now.date().strftime("%Y-%m-%d"))
    else:
        cutoff = (kst_today() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        q = db.collection(COLLECTION).where("date", ">=", cutoff)
    for d in q.stream():
        r = d.to_dict() or {}
        acct = str(r.get("accountNoMasked", ""))
        if TARGET_SUFFIX and TARGET_SUFFIX not in acct:
            continue
        if r.get("inout") != "입금":
            continue
        parts = r.get("descParts", []) or []
        name = best_name(parts)                       # ★ 적요 전체칸에서 실제 이름 복원
        rows.append({
            "id": d.id,
            "date": r.get("date", ""), "time": str(r.get("time", "")),
            "inout": r.get("inout", ""), "amount": int(r.get("amount", 0) or 0),
            "name": name,
            "counterparty": name or r.get("counterparty", "") or r.get("desc", ""),
            "desc": r.get("desc", ""), "descParts": parts,
            "category": r.get("category", ""),
            "account": r.get("account", ""), "acct": acct,
            "trNo": str(r.get("trNo", "")),
        })
    rows.sort(key=lambda x: (x["date"], x["time"]))
    return rows

# ── Slack ────────────────────────────────────────────────────────────────
def fmt_won(n): return f"{n:,}원"

def fmt_time(row):
    d, t = row["date"], row["time"]
    mmdd = f"{d[5:7]}/{d[8:10]}" if len(d) >= 10 else d
    hhmm = f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else t
    return f"{mmdd} {hhmm}".strip()

def slack_text(row):
    acct_tail = row["acct"][-5:] if row["acct"] else TARGET_SUFFIX
    return ("🤖 클로드 AI가 알려드립니다\n"
            "💰 입금 확인 (카드사 외)\n"
            f"• 입금자 : {row['counterparty']}\n"
            f"• 금액   : {fmt_won(row['amount'])}\n"
            f"• 시각   : {fmt_time(row)}\n"
            f"• 계좌   : 하나 …{acct_tail}")

def post_slack(text):
    """등록된 모든 채널에 발송. 일부 채널만 실패하면 경고만 남기고 성공 처리한다
    (한 채널 장애로 다른 채널까지 재발송/중복되는 것을 막기 위함). 전 채널 실패면 예외."""
    if not SLACK_WEBHOOKS:
        raise RuntimeError("SLACK_WEBHOOK_URLS / SLACK_WEBHOOK_URL 미설정 — 운영모드 불가")
    ok, errs = 0, []
    for url in SLACK_WEBHOOKS:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    ok += 1
                else:
                    errs.append(f"...{url[-10:]} HTTP {resp.status}")
        except Exception as e:                      # URL은 비밀 → 끝 10자만 로그
            errs.append(f"...{url[-10:]} {e}")
    if errs:
        print("slack warn:", "; ".join(errs), flush=True)
    if ok == 0:
        raise RuntimeError("슬랙 전 채널 발송 실패: " + "; ".join(errs))
    return ok

# ── 상태(중복방지) — Firestore 저장 (헤드리스 필수: 매 실행 초기화 방지) ──────────
STATE_DOC = os.environ.get("STATE_DOC", "deposit_alert_state/main")

class StateUnavailable(RuntimeError):
    """중복방지 상태를 못 읽음 → 발송을 포기한다(빈 상태로 진행 금지)."""

def load_state(db):
    """★ 읽기 실패는 치명적으로 다룬다 — 빈 상태로 진행하면 안 된다.

    2026-09-03 사고: Firestore 일일 할당량 초과(429 Quota exceeded)로 상태 읽기가 실패했는데
    종전 코드는 경고만 찍고 `{"sent_ids": []}` 로 진행했다. 그 결과
      ① 이미 보낸 2건(정진혁 1,000만 · 박시현 27.5만)을 12:53에 재발송 → 상담팀·데스크 중복 알림
      ② 이어진 save_state 가 sent_ids 를 그 2건으로 **덮어써** 중복방지 이력 29건이 통째로 소실
    못 읽었을 때 안전한 쪽은 "보내지 않는다"다. 한 사이클 빠져도 3~4분 뒤 다시 폴링한다.
    문서가 아예 없는 첫 실행(snap.exists=False)만 빈 상태로 시작한다.
    """
    col, doc = STATE_DOC.split("/", 1)
    try:
        snap = db.collection(col).document(doc).get()
    except Exception as e:
        raise StateUnavailable("상태 읽기 실패 — 이번 사이클 발송 건너뜀: %s" % e) from e
    if snap.exists:
        return snap.to_dict() or {"sent_ids": []}
    return {"sent_ids": []}          # 첫 실행(문서 없음)은 정상

def save_state(db, st):
    col, doc = STATE_DOC.split("/", 1)
    db.collection(col).document(doc).set({"sent_ids": st.get("sent_ids", [])[-8000:]})

# ── main ─────────────────────────────────────────────────────────────────
def classify_rows(rows):
    send_rows, skip_rows = [], []
    for r in rows:
        ok, why = decision(r)
        (send_rows if ok else skip_rows).append((r, why))
    return send_rows, skip_rows

def cli_dry_run(days=21):
    """슬랙 전송 없이 최근 days일 입금 판정 결과만 출력(검토용)."""
    db = init_db()
    rows = fetch_deposits(db, days=days)
    send_rows, skip_rows = classify_rows(rows)
    print(f"\n=== DRY-RUN — 하나 …{TARGET_SUFFIX} 최근 {days}일 입금 판정 ===")
    accts = sorted({r["account"] for r in rows})
    print(f"대상 계좌 표기: {accts or '(해당 입금 없음 — 계좌 끝자리/기간 확인 필요)'}")
    print(f"\n[✅ 슬랙 전송 대상 = 고객이체 후보] {len(send_rows)}건")
    for r, why in send_rows:
        print(f"  {fmt_time(r):>11} | {r['counterparty'][:16]:<16} | {fmt_won(r['amount']):>14} | {r['category']}")
    print(f"\n[❌ 제외] {len(skip_rows)}건")
    agg = {}
    for r, why in skip_rows:
        agg.setdefault(why, [0, 0]); agg[why][0] += 1; agg[why][1] += r["amount"]
    for why, (c, amt) in sorted(agg.items(), key=lambda x: -x[1][1]):
        print(f"  {why:<12} {c:>3}건 {fmt_won(amt):>16}")
    print("\n--- 전송 미리보기(첫 3건) ---")
    for r, why in send_rows[:3]:
        print(slack_text(r)); print("-")
    # ★ 적요 원본 4칸 전수 덤프 — 진짜 입금자명이 어느 칸에 있는지 진단용
    print("\n=== RAW 적요칸 덤프 (…{} 입금 전건) ===".format(TARGET_SUFFIX))
    print("  일시        | 금액           | category   | 추출된counterparty || 원본칸들(desc1│desc2│...)")
    for r in rows:
        parts = " │ ".join(str(p) for p in (r.get("descParts") or []))
        print(f"  {fmt_time(r):>11} | {fmt_won(r['amount']):>14} | {r['category']:<10} | {r['counterparty'][:14]:<14} || {parts}")
    print("\n(dry-run: 슬랙 전송 안 함)")

def cli_live():
    """운영모드: 신규 입금만 슬랙 전송 + 중복방지 상태 저장(Firestore)."""
    db = init_db()
    rows = fetch_deposits(db)
    send_rows, _ = classify_rows(rows)
    try:
        st = load_state(db)
    except StateUnavailable as e:
        # 발송 안 함. 상태 저장도 안 함(덮어쓰기로 이력 날리는 걸 막는다).
        print("SKIP:", e, flush=True)
        return
    seen = set(st.get("sent_ids", []))
    new_sent = 0
    try:
        for r, why in send_rows:
            if r["id"] in seen:
                continue
            post_slack(slack_text(r)); seen.add(r["id"]); new_sent += 1
    finally:
        # 중간에 실패해도 "이미 보낸 것"까지는 반드시 기록 — 안 그러면 다음 폴링에 중복 발송된다.
        # (3분 폴링이라 예전 10분 크론보다 중복 위험이 훨씬 크다)
        save_state(db, {"sent_ids": list(seen)})
    print(f"전송 {new_sent}건 (신규만). 누적 {len(seen)}건.")

def cli_seed(days=14):
    """최초 배포용: 기존 입금건을 '이미 발송'으로 표시만(전송 안 함). 첫 실행 폭주 방지."""
    db = init_db()
    rows = fetch_deposits(db, days=days)
    send_rows, _ = classify_rows(rows)
    st = load_state(db); seen = set(st.get("sent_ids", []))   # 시드는 수동 실행 → 실패 시 그대로 예외
    for r, _why in send_rows:
        seen.add(r["id"])
    save_state(db, {"sent_ids": list(seen)})
    print(f"시드 완료: 기존 {len(send_rows)}건 발송처리(전송 안 함). 누적 {len(seen)}건.")

def main():
    if "--dry-run" in sys.argv:
        cli_dry_run()
    elif "--seed" in sys.argv:
        cli_seed()
    else:
        cli_live()

if __name__ == "__main__":
    main()
