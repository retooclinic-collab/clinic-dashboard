# -*- coding: utf-8 -*-
"""
청담리투의원 은행 입출금내역 자동 수집기
- CODEF에서 하나(0081)·부산(0032) 수시입출금 거래내역 수집
- 입금/출금 + 적요·상대방 키워드 기반 분류
- Firebase Firestore(bank_transactions)에 중복 없이 저장(upsert)
환경변수(깃허브 시크릿)로 모든 설정 주입. 코드에 비밀정보 없음.
※ 청담리투는 기업/법인 계좌 → BANK_SCOPE 기본 "b"(법인). 개인계좌면 "p".
"""
import os, json, time, datetime, hashlib
from easycodefpy import Codef, ServiceType
from easycodefpy.util import encrypt_rsa
import firebase_admin
from firebase_admin import credentials, firestore

ENV = "api"  # 2026-08 정식전환 강제 (yml env 무시)
CLIENT_ID    = os.environ["CODEF_CLIENT_ID"]
CLIENT_SECRET= os.environ["CODEF_CLIENT_SECRET"]
PUBLIC_KEY   = os.environ["CODEF_PUBLIC_KEY"]
CONNECTED_ID = os.environ["CODEF_CONNECTED_ID"]
BIRTHDATE    = os.environ.get("CODEF_BIRTHDATE", "")
LOOKBACK     = int(os.environ.get("LOOKBACK_DAYS", "14"))
DEBUG        = os.environ.get("BANK_DEBUG", "0") == "1"
COLLECTION   = os.environ.get("FIRESTORE_COLLECTION", "bank_transactions")

SVC = ServiceType.PRODUCT if ENV in ("api","prod","product") else ServiceType.DEMO
ORG_NAME   = {"0081":"하나", "0032":"부산"}
MAX_MONTHS = {"0081":18, "0032":12}
ACCTPW_KEY = {"0081":"BANK_HANA_ACCTPW", "0032":"BANK_BUSAN_ACCTPW"}
# 수시입출 아닌 계좌(퇴직연금/ISA/신탁/펀드/정기/외화 등)는 조회 불가 → 호출 낭비 방지 위해 스킵
SKIP_ACCT = ("퇴직연금","irp","ira","isa","신탁","펀드","적금","정기예금","정기적금","밀리언달러","빌리언달러","외화","닥터클럽","플래티늄","청약")

# 스코프: b=법인/기업뱅킹(기본), p=개인. (청담리투는 기업계좌)
SCOPE = os.environ.get("BANK_SCOPE", "b")
PATH_ACCOUNTS = f"/v1/kr/bank/{SCOPE}/account/account-list"
PATH_TRANS    = f"/v1/kr/bank/{SCOPE}/account/transaction-list"
ORG_ONLY    = os.environ.get("BANK_ORG_ONLY", "")     # 지정 시 해당 은행코드만 수집
ACCT_SUFFIX = os.environ.get("BANK_ACCT_SUFFIX", "")  # 지정 시 계좌번호 끝자리 일치만
_LIMIT_HIT = []  # CF-00012(일일 호출한도) 도달 여부 기록

OUT_VENDOR = {
 # ★에이전시수수료 = 환자유치 에이전시 지급수수료(출금). 반드시 최상단 — '의료재료'(메디치골드의 '메디' 겹침)·
 #   '거래처이체'(주식회사)·'기타지출'보다 먼저 선매칭해야 오분류 방지.
 #   경로↔수취인 매핑 확정 (2026-08-12, 세션 a3f889 + 원장정정): 아이메이=중따, 비주얼제이=중마,
 #   메디치골드=중강, JIN TAIGEN=편화, HONGKONG TAOXI=다우시, 목금미여=미원, 박선하(비요네즈)=일본통, HAN RUNQI=ky여행사(추정)
 #   2026-09-01 동기화: clinic-final.html CF_AGENCY에만 있던 비요네즈·에스큐뷰티·HAN DAN·신화월드투어 추가(양쪽 목록 드리프트 해소).
 "에이전시수수료":["아이메이","비주얼제이","메디치골드","jin taigen","taoxi","목금미여","박선하","han runqi","비요네즈","에스큐뷰티","han dan","신화월드투어"],
 "카드대금출금":["kb국민카드","국민카드","현대카드","삼성카드","롯데카드","하나카드","비씨","bc카드","신한카드","우리카드","카드대금","카드결제"],
 # ★HU YAXIN = 직원 호아신 월급여(수수료 아님, 인건비). 2026-08-12 원장확정.
 "급여/4대보험":["급여","상여","월급","4대보험","건강보험","국민연금","고용보험","산재","원천세","갑근세","퇴직","hu yaxin"],
 "임대/관리비":["임대","월세","관리비","빌딩","부동산","임차"],
 # ★ 은행 적요는 축약형이 많다 — '서울특징김재림'(서울시 지방소득세 특별징수) · '김재림서울주민'(주민세)은
 #   '지방소득세'·'주민세' 통으로는 안 잡혀 기타지출로 샴다(2026-08 오분류). 축약표기 키워드 추가 (2026-09-01).
 #   '주민'단독은 사람이름(박주민 등) 오탐지 위험 → '서울주민/시주민/구주민'로만 좁혔다.
 "세금/공과/수수료":["국세","지방세","세무","부가세","법인세","종소","종합소득세","지방소득","국고","주민세","재산세","취득세","원천세","공과금",
   "특징","특별징수","서울주민","시주민","구주민","위택스","wetax",
   "전기","수도","가스","한국전력","한전"],
 "통신/IT":["kt","skt","lg유플","유플러스","통신","인터넷","호스팅","세스코"],
 "광고/마케팅":["네이버","카카오","메타","구글","google","당근","강남언니","바비톡","굿닥","광고","마케팅"],
 "의료재료/약품/장비":["케어캠프","지오영","파마리서치","파마","휴메딕스","휴젤","멀츠","merz","애브비","갈더마","엘러간",
   "바임","소프웨이브","루트로닉","클래시스","제테마","종근당","대웅","덱스레보","엘앤씨바이오","의료기","메디","제약","바이오"],
 "거래처이체":["상사","유통","무역","컴퍼니","주식회사","(주)","㈜"],
}
IN_VENDOR = {
 "카드매출입금":["카드","나이스","nice","ksnet","케이에스넷","koces","kovan","koviin","smartro","스마트로","다날","kg이니시스","토스페이먼츠","페이먼츠","밴","van","pg"],
 "보험청구입금":["심사평가","건강보험공단","공단","보험금","실손","손해보험","화재해상","db손보","현대해상","삼성화재","메리츠"],
 "이자수입":["이자","예금이자"],
}

def categorize(inout, desc):
    s = str(desc).lower()
    table = IN_VENDOR if inout == "입금" else OUT_VENDOR
    for cat, kws in table.items():
        for kw in kws:
            if kw.lower() in s:
                return cat
    return ("기타수입" if inout == "입금" else "기타지출")

# 벤더 메모(서브태그): 큰 카테고리 안에서 세부 채널/거래처를 식별. 필요시 계속 추가.
# 예) 아이메이 = 중국(따) 유치 에이전시 → 메모 "중따". 다른 에이전시/유치경로도 여기 추가.
VENDOR_MEMO = {
 "아이메이": "중따",
 "비주얼제이": "중마",
 "메디치골드": "중강",
 "jin taigen": "편화",
 "taoxi": "다우시",
 "목금미여": "미원",
 "박선하": "일본통",
 "han runqi": "ky여행사",
}
def vendor_memo(desc):
    s = str(desc).lower()
    for kw, memo in VENDOR_MEMO.items():
        if kw.lower() in s:
            return memo
    return ""

def init_db():
    sa_json = os.environ.get("FIREBASE_SA_JSON")
    if sa_json:
        cred = credentials.Certificate(json.loads(sa_json))
    else:
        cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    firebase_admin.initialize_app(cred)
    return firestore.client()

def make_codef():
    c = Codef()
    if SVC == ServiceType.PRODUCT:
        c.set_client_info(CLIENT_ID, CLIENT_SECRET)
    else:
        c.set_demo_client_info(CLIENT_ID, CLIENT_SECRET)
    c.public_key = PUBLIC_KEY
    return c

def _g(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default

def list_accounts(codef, org):
    p = {"organization":org, "connectedId":CONNECTED_ID, "birthDate":BIRTHDATE}
    r = json.loads(codef.request_product(PATH_ACCOUNTS, SVC, p))
    code = (r.get("result") or {}).get("code")
    if code != "CF-00000":
        print(f"  [{ORG_NAME.get(org,org)}] 계좌목록 {code}: {(r.get('result') or {}).get('message')}", flush=True)
    data = r.get("data")
    if DEBUG:
        print("  [DEBUG account-list]", json.dumps(data, ensure_ascii=False)[:800], flush=True)
    accts = []
    if isinstance(data, dict):
        for key in ("resDepositTrust","resForeignCurrency","resLoan","resFund","accountList","list"):
            v = data.get(key)
            if isinstance(v, list):
                accts += v
        if not accts and data.get("resAccount"):
            accts = [data]
    elif isinstance(data, list):
        accts = data
    out = []
    for a in accts:
        no = _g(a, "resAccount", "resAccountNo", "resAccountDisplay")
        nm = str(_g(a,"resAccountName","resAccountNickName","resAccountDeposit"))
        if no and ACCT_SUFFIX and not str(no).endswith(ACCT_SUFFIX):
            continue
        if no and not any(k in nm.lower() for k in SKIP_ACCT):
            out.append({"no":str(no).strip(), "name":nm,
                        "balance":_g(a,"resAccountBalance","resAccountBalanceAmt", default="0")})
        elif no:
            print(f"    (스킵: {no[-4:]} {nm} — 수시입출 아님)", flush=True)
    return out

def fetch_trans(codef, org, account_no, acctpw_enc):
    today = datetime.date.today()
    max_m = MAX_MONTHS.get(org, 12)
    back  = min(LOOKBACK, int(max_m*30))
    start = today - datetime.timedelta(days=back)
    rows, ws = [], start
    while ws < today:
        we = min(ws + datetime.timedelta(days=90), today)
        p = {"organization":org, "connectedId":CONNECTED_ID, "birthDate":BIRTHDATE,
             "account":account_no, "startDate":ws.strftime("%Y%m%d"), "endDate":we.strftime("%Y%m%d"),
             "orderBy":"0", "inquiryType":"0"}
        if acctpw_enc:
            p["accountPassword"] = acctpw_enc
        r = json.loads(codef.request_product(PATH_TRANS, SVC, p))
        code = (r.get("result") or {}).get("code")
        if code != "CF-00000":
            print(f"  [{ORG_NAME.get(org,org)} {account_no[-4:] if account_no else ''}] {ws}~{we} {code}: {(r.get('result') or {}).get('message')}", flush=True)
            if code == "CF-00012": _LIMIT_HIT.append(1)
        data = r.get("data") or {}
        if DEBUG and rows == []:
            print("  [DEBUG transaction-list]", json.dumps(data, ensure_ascii=False)[:1000], flush=True)
        hist = []
        if isinstance(data, dict):
            hist = data.get("resTrHistoryList") or data.get("resAccountTrHistoryList") or data.get("resHistoryList") or []
            bal  = _g(data, "resAccountBalance", "resAccountBalanceAmt", default="")
        elif isinstance(data, list):
            hist = data; bal = ""
        for h in hist:
            h["_balanceNow"] = bal
        rows += hist
        ws = we + datetime.timedelta(days=1)
        time.sleep(1)
    return rows

def to_doc(org, account_no, account_name, t):
    date = str(_g(t, "resAccountTrDate", "resTrDate", "resAccountTransDate"))
    tm   = str(_g(t, "resAccountTrTime", "resTrTime", "resAccountTransTime"))
    out_amt = _g(t, "resAccountOut", "resAccountWithdrawal", "resOut", default="0")
    in_amt  = _g(t, "resAccountIn",  "resAccountDeposit",    "resIn",  default="0")
    def _num(x):
        try: return int(str(x).replace(",","").strip() or "0")
        except: return 0
    out_n, in_n = _num(out_amt), _num(in_amt)
    inout = "입금" if in_n > 0 else ("출금" if out_n > 0 else "기타")
    amount = in_n if in_n > 0 else out_n
    bal_after = _num(_g(t, "resAfterTranBalance", "resAccountBalance", "resAfterBalance", default="0"))
    descs = [str(_g(t, k, default="")).strip() for k in
             ("resAccountDesc1","resAccountDesc2","resAccountDesc3","resAccountDesc4")]
    descs = [d for d in descs if d]
    desc_join = " / ".join(descs)
    # 상대방명: 은행 거래구분 라벨/결제중계(토스=비바리퍼블리카)/전표번호는 건너뛰고
    #           뒷단의 '실제 상대방'을 상대방명으로 사용 (입금·출금 공통).
    _SKIP = ("대체","타행이체","자동이체","이체","입금이체","출금","입금",
             "펌뱅킹","전자금융","저축예금","매출대금","카드대금")
    def _acctish(_s): return sum(_c.isdigit() for _c in _s) >= 5
    counterparty = ""
    for _d in descs:
        _dl = _d.lower().strip()
        if not _d: continue
        if _dl in _SKIP: continue
        if "cbs" in _dl: continue
        if "비바리퍼블리카" in _d: continue     # 결제중계사(토스) — 실제 송금인은 다른 칸
        if _acctish(_d): continue               # 계좌/전표번호류
        counterparty = _d; break
    if not counterparty and descs: counterparty = descs[0]
    appr = str(_g(t, "resAccountTrNo", "resTrNo", default=""))
    key  = f"{org}|{account_no}|{date}|{tm}|{amount}|{inout}|{bal_after}|{desc_join}|{appr}"
    did  = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return did, {
        "account": f"{ORG_NAME.get(org, org)} {account_name}".strip(),
        "bank": ORG_NAME.get(org, org), "org": org,
        "accountNoMasked": account_no,
        "date": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date)==8 else date,
        "month": f"{date[:4]}-{date[4:6]}" if len(date)>=6 else "",
        "time": tm, "inout": inout, "amount": amount,
        "balanceAfter": bal_after,
        "desc": desc_join, "descParts": descs, "counterparty": counterparty,
        "category": categorize(inout, desc_join),
        "vendorMemo": vendor_memo(desc_join),
        "trNo": appr,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }

def main():
    db = init_db()
    codef = make_codef()
    col = db.collection(COLLECTION)
    total = 0
    stats = {}   # 계좌명 -> {min, max, cnt}
    batch = db.batch(); n = 0
    for org in ([ORG_ONLY] if ORG_ONLY else ORG_NAME):
        print("수집:", ORG_NAME[org], flush=True)
        acctpw_raw = os.environ.get(ACCTPW_KEY.get(org, ""), "")
        acctpw_enc = encrypt_rsa(acctpw_raw, PUBLIC_KEY) if acctpw_raw else ""
        accts = list_accounts(codef, org)
        if not accts:
            print(f"  [{ORG_NAME[org]}] 보유계좌 없음/조회실패 — 건너뜀", flush=True)
            continue
        for a in accts:
            print(f"  계좌 {a['no']} ({a['name']})", flush=True)
            acnt = 0
            for t in fetch_trans(codef, org, a["no"], acctpw_enc):
                did, doc = to_doc(org, a["no"], a["name"], t)
                batch.set(col.document(did), doc, merge=True)
                total += 1; n += 1; acnt += 1
                d = doc.get("date", "")
                if d:
                    nm = doc.get("account", "")
                    st = stats.setdefault(nm, {"min": d, "max": d, "cnt": 0})
                    if d < st["min"]: st["min"] = d
                    if d > st["max"]: st["max"] = d
                    st["cnt"] += 1
                if n >= 400:
                    batch.commit(); batch = db.batch(); n = 0
            print(f"    -> {acnt}건 수집", flush=True)
    if n: batch.commit()
    db.collection("bank_sync_log").add({
        "ranAt": firestore.SERVER_TIMESTAMP, "env": ENV,
        "lookbackDays": LOOKBACK, "upserted": total, "limitHit": bool(_LIMIT_HIT),
    })
    print(f"\n완료: {total}건 upsert (collection={COLLECTION})", flush=True)
    print("── 계좌별 수집범위(가장 과거 ~ 최근) ──", flush=True)
    for nm in sorted(stats):
        st = stats[nm]
        print(f"  {nm}: {st['min']} ~ {st['max']}  ({st['cnt']}건)", flush=True)
    if _LIMIT_HIT:
        print("\n⚠️ CODEF 일일 호출한도(100)에 도달 — 일부 과거기간이 빠졌을 수 있습니다.", flush=True)
        print("   내일(한도 리셋 후) lookback 600 으로 한 번 더 실행하면 나머지가 채워집니다. (이미 있는 건 중복 안 쌓임)", flush=True)
    else:
        print(f"\n✅ 한도 여유 — 요청 기간({LOOKBACK}일) 전체 조회 완료. 추가 실행 불필요.", flush=True)

if __name__ == "__main__":
    _lb = os.environ.get("LOOKBACK_DAYS")
    if _lb == "9999":
        import deposit_slack_notifier as _dn; _dn.cli_dry_run()  # 수집 안 함, 입금 dry-run만
    elif _lb == "7777":
        import deposit_slack_notifier as _dn; _dn.cli_seed()      # 시드(발송처리만, 전송 안 함)
    else:
        main()
