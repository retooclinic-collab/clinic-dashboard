# -*- coding: utf-8 -*-
"""
청담리투의원 은행 입출금내역 자동 수집기
- CODEF에서 하나(0081)·부산(0032) 수시입출금 거래내역 수집
- 입금/출금 + 적요·상대방 키워드 기반 분류
- Firebase Firestore(bank_transactions)에 중복 없이 저장(upsert)
환경변수(깃허브 시크릿)로 모든 설정 주입. 코드에 비밀정보 없음.

수집 흐름: 보유계좌 조회(account-list) → 각 계좌 거래내역(transaction-list) → 분류 → upsert
"""
import os, json, time, datetime, hashlib
from easycodefpy import Codef, ServiceType
from easycodefpy.util import encrypt_rsa
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- 설정(환경변수) ----------
ENV          = os.environ.get("CODEF_ENV", "demo").lower()
CLIENT_ID    = os.environ["CODEF_CLIENT_ID"]
CLIENT_SECRET= os.environ["CODEF_CLIENT_SECRET"]
PUBLIC_KEY   = os.environ["CODEF_PUBLIC_KEY"]
CONNECTED_ID = os.environ["CODEF_CONNECTED_ID"]
BIRTHDATE    = os.environ.get("CODEF_BIRTHDATE", "")
LOOKBACK     = int(os.environ.get("LOOKBACK_DAYS", "14"))   # 매일 14일 겹쳐 수집(누락방지), 백필=600
DEBUG        = os.environ.get("BANK_DEBUG", "0") == "1"     # 1이면 원본 응답 1건 덤프(필드명 확인용)
COLLECTION   = os.environ.get("FIRESTORE_COLLECTION", "bank_transactions")

SVC = ServiceType.PRODUCT if ENV in ("api","prod","product") else ServiceType.DEMO
ORG_NAME   = {"0081":"하나", "0032":"부산"}
# 은행별 조회기간 한도(개월): 하나18, 부산12 (보수적). 백필 시 LOOKBACK과 min 적용.
MAX_MONTHS = {"0081":18, "0032":12}
# 계좌 비밀번호(거래내역 조회 시 일부 은행 필요) — 시크릿명: BANK_<코드>_ACCTPW
ACCTPW_KEY = {"0081":"BANK_HANA_ACCTPW", "0032":"BANK_BUSAN_ACCTPW"}

PATH_ACCOUNTS = "/v1/kr/bank/p/account/account-list"
PATH_TRANS    = "/v1/kr/bank/p/account/transaction-list"

# ---------- 분류 규칙 ----------
# 출금(지출) 거래: 적요/상대방에 아래 키워드 → 카테고리
OUT_VENDOR = {
 "카드대금출금":["kb국민카드","국민카드","현대카드","삼성카드","롯데카드","하나카드","비씨","bc카드","신한카드","우리카드","카드대금","카드결제"],
 "급여/4대보험":["급여","상여","월급","4대보험","건강보험","국민연금","고용보험","산재","원천세","갑근세","퇴직"],
 "임대/관리비":["임대","월세","관리비","빌딩","부동산","임차"],
 "세금/공과/수수료":["국세","지방세","세무","부가세","법인세","수수료","이체수수료","공과금","전기","수도","가스","한국전력","한전"],
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

# ---------- Firestore ----------
def init_db():
    sa_json = os.environ.get("FIREBASE_SA_JSON")
    if sa_json:
        cred = credentials.Certificate(json.loads(sa_json))
    else:
        cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    firebase_admin.initialize_app(cred)
    return firestore.client()

# ---------- CODEF ----------
def make_codef():
    c = Codef()
    if SVC == ServiceType.PRODUCT:
        c.set_client_info(CLIENT_ID, CLIENT_SECRET)
    else:
        c.set_demo_client_info(CLIENT_ID, CLIENT_SECRET)
    c.public_key = PUBLIC_KEY
    return c

def _g(d, *keys, default=""):
    """여러 후보 키 중 처음 값 있는 것 반환 (은행별 필드명 차이 흡수)"""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default

def list_accounts(codef, org):
    """은행 보유계좌 목록 (계좌번호 자동 확보)"""
    p = {"organization":org, "connectedId":CONNECTED_ID, "birthDate":BIRTHDATE}
    r = json.loads(codef.request_product(PATH_ACCOUNTS, SVC, p))
    code = (r.get("result") or {}).get("code")
    if code != "CF-00000":
        print(f"  [{ORG_NAME.get(org,org)}] 계좌목록 {code}: {(r.get('result') or {}).get('message')}", flush=True)
    data = r.get("data")
    if DEBUG:
        print("  [DEBUG account-list]", json.dumps(data, ensure_ascii=False)[:800], flush=True)
    # data가 dict({resDepositTrust:[...]}) 또는 list 형태
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
        if no:
            out.append({"no":str(no).strip(),
                        "name":_g(a,"resAccountName","resAccountNickName","resAccountDeposit"),
                        "balance":_g(a,"resAccountBalance","resAccountBalanceAmt", default="0")})
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
    # 적요/상대방: 은행별로 desc 슬롯이 달라 전부 모아 보관 + 합쳐서 분류용 텍스트 생성
    descs = [str(_g(t, k, default="")).strip() for k in
             ("resAccountDesc1","resAccountDesc2","resAccountDesc3","resAccountDesc4")]
    descs = [d for d in descs if d]
    desc_join = " / ".join(descs)
    counterparty = descs[0] if descs else ""
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
        "trNo": appr,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }

def main():
    db = init_db()
    codef = make_codef()
    col = db.collection(COLLECTION)
    total = 0
    batch = db.batch(); n = 0
    for org in ORG_NAME:
        print("수집:", ORG_NAME[org], flush=True)
        acctpw_raw = os.environ.get(ACCTPW_KEY.get(org, ""), "")
        acctpw_enc = encrypt_rsa(acctpw_raw, PUBLIC_KEY) if acctpw_raw else ""
        accts = list_accounts(codef, org)
        if not accts:
            print(f"  [{ORG_NAME[org]}] 보유계좌 없음/조회실패 — 건너뜀", flush=True)
            continue
        for a in accts:
            print(f"  계좌 {a['no']} ({a['name']})", flush=True)
            for t in fetch_trans(codef, org, a["no"], acctpw_enc):
                did, doc = to_doc(org, a["no"], a["name"], t)
                batch.set(col.document(did), doc, merge=True)
                total += 1; n += 1
                if n >= 400:
                    batch.commit(); batch = db.batch(); n = 0
    if n: batch.commit()
    db.collection("bank_sync_log").add({
        "ranAt": firestore.SERVER_TIMESTAMP, "env": ENV,
        "lookbackDays": LOOKBACK, "upserted": total,
    })
    print(f"\n완료: {total}건 upsert (collection={COLLECTION})", flush=True)

if __name__ == "__main__":
    main()
