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
  SLACK_WEBHOOK_URL       : 상담팀 채널 Incoming Webhook (운영모드에서만 필요)
  TARGET_ACCT_SUFFIX      : 대상계좌 끝자리(기본 "23207")
  LOOKBACK_DAYS           : 조회 일수(기본 14)
  STATE_PATH              : 중복방지 상태파일(기본 E:\...\deposit_notifier_state.json)
"""
import os, sys, json, datetime, urllib.request

TARGET_SUFFIX = os.environ.get("TARGET_ACCT_SUFFIX", "23207")
LOOKBACK      = int(os.environ.get("LOOKBACK_DAYS", "14"))
STATE_PATH    = os.environ.get(
    "STATE_PATH",
    r"E:\Claude\Projects\클로드 Cowork\card-automation\deposit_notifier_state.json")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
COLLECTION    = os.environ.get("FIRESTORE_COLLECTION", "bank_transactions")

# ── 제외 판정 ─────────────────────────────────────────────────────────────
# collector_bank.py 의 IN_VENDOR 분류와 일치. category 값으로 1차 제외.
EXCLUDE_CATEGORIES = {"카드매출입금", "보험청구입금", "이자수입"}

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

def fetch_deposits(db, days=None):
    if days is None:
        days = LOOKBACK
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    q = db.collection(COLLECTION).where("date", ">=", cutoff)
    rows = []
    for d in q.stream():
        r = d.to_dict() or {}
        acct = str(r.get("accountNoMasked", ""))
        if TARGET_SUFFIX and TARGET_SUFFIX not in acct:
            continue
        if r.get("inout") != "입금":
            continue
        rows.append({
            "id": d.id,
            "date": r.get("date", ""), "time": str(r.get("time", "")),
            "inout": r.get("inout", ""), "amount": int(r.get("amount", 0) or 0),
            "counterparty": r.get("counterparty", "") or r.get("desc", ""),
            "desc": r.get("desc", ""), "descParts": r.get("descParts", []),
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
    if not SLACK_WEBHOOK:
        raise RuntimeError("SLACK_WEBHOOK_URL 미설정 — 운영모드 불가")
    req = urllib.request.Request(
        SLACK_WEBHOOK, data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status

# ── 상태(중복방지) ────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent_ids": []}

def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

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
    """운영모드: 신규 입금만 슬랙 전송 + 중복방지 상태 저장."""
    db = init_db()
    rows = fetch_deposits(db)
    send_rows, _ = classify_rows(rows)
    st = load_state(); seen = set(st.get("sent_ids", []))
    new_sent = 0
    for r, why in send_rows:
        if r["id"] in seen:
            continue
        post_slack(slack_text(r)); seen.add(r["id"]); new_sent += 1
    st["sent_ids"] = list(seen)[-5000:]; save_state(st)
    print(f"전송 {new_sent}건 (신규만). 누적 상태 {len(seen)}건.")

def main():
    if "--dry-run" in sys.argv:
        cli_dry_run()
    else:
        cli_live()

if __name__ == "__main__":
    main()
