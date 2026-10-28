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

# 2026-10-28 JRK(법인) 확장 — 새 인증서 connectedId 전환 + 외화계좌·타행 수집 (통합관제-006).
#   JRK 모드 = 컬렉션이 jrk_* 일 때만. 병원(bank_transactions) 동작은 그대로.
#   설정은 Firestore codef_private/jrk_bank_conf (클라이언트 접근 불가, 관리자 SDK만):
#     connectedId = 새 인증서로 만든 connectedId (있으면 시크릿 JRK_CONNECTED_ID 대신 사용)
#     orgs        = 수집할 은행코드 목록 (yml의 BANK_ORG_ONLY=0081 대신)
#   인증서 등록은 codef_private/jrk_bank_cert {derFile,keyFile,password[,probeOrgs]} — 로컬 스크립트가 넣고 수집기가 쓰고 즉시 삭제.
#   외화 계좌: 기존 필드(amount·balanceAfter)는 항상 원화(외화는 거래일 ECB 환율 근사 환산), 외화 원값은 *Foreign·currency 필드.
JRK_MODE = COLLECTION.startswith("jrk_")
PATH_FX_TRANS = f"/v1/kr/bank/{SCOPE}/exchange/transaction-list"
BANK_NAMES = {"0002":"산업","0003":"기업","0004":"국민","0007":"수협","0011":"농협","0020":"우리","0023":"SC제일",
              "0027":"씨티","0031":"대구","0032":"부산","0034":"광주","0035":"제주","0037":"전북","0039":"경남",
              "0045":"새마을","0048":"신협","0071":"우체국","0081":"하나","0088":"신한","0089":"케이"}
_FX_CACHE = {}

OUT_VENDOR = {
 # ★에이전시수수료 = 환자유치 에이전시 지급수수료(출금). 반드시 최상단 — '의료재료'(메디치골드의 '메디' 겹침)·
 #   '거래처이체'(주식회사)·'기타지출'보다 먼저 선매칭해야 오분류 방지.
 #   경로↔수취인 매핑 확정 (2026-08-12, 세션 a3f889 + 원장정정): 아이메이=중따, 비주얼제이=중마,
 #   메디치골드=중강, JIN TAIGEN=편화, HONGKONG TAOXI=다우시, 목금미여=미원(2026-10~ 글로우업서울/이새롬), 박선하(비요네즈)=일본통, HAN RUNQI=ky여행사(추정)
 #   2026-09-01 동기화: clinic-final.html CF_AGENCY에만 있던 비요네즈·에스큐뷰티·HAN DAN·신화월드투어 추가(양쪽 목록 드리프트 해소).
 "에이전시수수료":["아이메이","비주얼제이","메디치골드","jin taigen","taoxi","목금미여","글로우업","이새롬","박선하","han runqi","비요네즈","에스큐뷰티","han dan","신화월드투어"],
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
# ※ 신화월드투어: 경로 매핑 보류(2026-09-01 원장 — 2025-11 이후 거래 없음). 재개 시 확인 후 추가할 것.
VENDOR_MEMO = {
 "아이메이": "중따",
 "비주얼제이": "중마",
 "에스큐뷰티": "중마",   # 2026-09-01 원장확정: 비주얼제이와 동일 경로(중마)
 "메디치골드": "중강",
 "jin taigen": "편화",
 "taoxi": "다우시",
 "목금미여": "미원",
 "글로우업": "미원",   # 2026-10-01 원장확정: 미원 상호변경 → 글로우업서울(우리 1005-304-799500, 통장명의 이새롬)
 "이새롬": "미원",
 "박선하": "일본통",
 "han runqi": "ky여행사",
 "han dan": "라미타",   # 2026-09-01 원장확정
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
    fx_nos = set()
    if JRK_MODE and isinstance(data, dict):   # 외화계좌는 resForeignCurrency에 통화와 함께 옴 → 외화 거래내역 API로 따로 수집
        for a in (data.get("resForeignCurrency") or []):
            no = str(_g(a, "resAccount", "resAccountNo", "resAccountDisplay")).strip()
            if not no: continue
            fx_nos.add(no)
            cur = str(_g(a, "resAccountCurrency", default="")).strip().upper()
            out.append({"no":no, "name":str(_g(a,"resAccountName","resAccountNickName","resAccountDeposit")),
                        "balance":_g(a,"resAccountBalance", default="0"), "fx":True,
                        "currencies":[c.strip() for c in cur.replace("/",",").split(",") if c.strip()] or ["USD"]})
    for a in accts:
        no = _g(a, "resAccount", "resAccountNo", "resAccountDisplay")
        nm = str(_g(a,"resAccountName","resAccountNickName","resAccountDeposit"))
        if str(no).strip() in fx_nos:
            continue
        if no and ACCT_SUFFIX and not str(no).endswith(ACCT_SUFFIX):
            continue
        if no and not any(k in nm.lower() for k in SKIP_ACCT):
            out.append({"no":str(no).strip(), "name":nm,
                        "balance":_g(a,"resAccountBalance","resAccountBalanceAmt", default="0")})
        elif no:
            print(f"    (스킵: {no[-4:]} {nm} — 수시입출 아님)", flush=True)
    return out

def fx_rate(cur, date_s):
    """거래일(YYYY-MM-DD) 1통화→원 ECB 환율(frankfurter, 휴일이면 직전 영업일). 실패 시 None."""
    if not cur or cur == "KRW": return 1.0
    k = (cur, date_s)
    if k not in _FX_CACHE:
        rate = None
        try:
            import requests
            r = requests.get(f"https://api.frankfurter.app/{date_s}", params={"from":cur, "to":"KRW"}, timeout=15)
            rate = float(r.json()["rates"]["KRW"])
        except Exception as e:
            print(f"    (환율 조회 실패 {cur} {date_s}: {repr(e)[:80]})", flush=True)
        _FX_CACHE[k] = rate
    return _FX_CACHE[k]

def fetch_fx_trans(codef, org, account_no, currency):
    """외화 거래내역(/bank/b/exchange/transaction-list). 하나는 조회기간 제한 없음 → 90일 창으로 나눠 조회."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=min(LOOKBACK, 540))
    rows, ws = [], start
    while ws < today:
        we = min(ws + datetime.timedelta(days=90), today)
        p = {"organization":org, "connectedId":CONNECTED_ID, "account":account_no,
             "startDate":ws.strftime("%Y%m%d"), "endDate":we.strftime("%Y%m%d"), "orderBy":"0", "currency":currency}
        r = json.loads(codef.request_product(PATH_FX_TRANS, SVC, p))
        code = (r.get("result") or {}).get("code")
        if code != "CF-00000":
            print(f"  [{ORG_NAME.get(org,org)} 외화 {account_no[-4:]} {currency}] {ws}~{we} {code}: {(r.get('result') or {}).get('message')}", flush=True)
            if code == "CF-00012": _LIMIT_HIT.append(1)
            if code == "CF-00401":   # 10-28 외화 거래내역 상품 권한 미개통(헥토 계약) — 나머지 기간 호출 생략
                print("    (외화 거래내역 상품 권한 없음 → 잔액만 jrk_bank_accounts에 기록)", flush=True); break
        data = r.get("data") or {}
        hist = (data.get("resTrHistoryList") or []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        acur = str(data.get("resAccountCurrency") or currency).upper() if isinstance(data, dict) else currency
        for h in hist:
            h["_currency"] = str(h.get("resAccountCurrency") or acur or currency).upper()
        rows += hist
        ws = we + datetime.timedelta(days=1)
        time.sleep(1)
    return rows

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
    cur = t.get("_currency") or "KRW"
    fx_extra = {}
    if cur != "KRW":   # 외화: 소수 금액 → 원값은 *Foreign, 기존 필드는 거래일 환율로 원화 근사
        def _f(x):
            try: return float(str(x).replace(",","").strip() or "0")
            except: return 0.0
        out_f, in_f = _f(out_amt), _f(in_amt)
        bal_f = _f(_g(t, "resAfterTranBalance", "resAccountBalance", "resAfterBalance", default="0"))
        rate = fx_rate(cur, f"{date[:4]}-{date[4:6]}-{date[6:8]}") if len(date) == 8 else None
        amt_f = in_f if in_f > 0 else out_f
        inout = "입금" if in_f > 0 else ("출금" if out_f > 0 else "기타")
        amount = int(round(amt_f * rate)) if rate else 0
        bal_after = int(round(bal_f * rate)) if rate else 0
        fx_extra = {"amountForeign": amt_f, "balanceAfterForeign": bal_f, "fxRate": rate, "fxSource": "ECB(frankfurter) 거래일 기준 근사"}
    else:
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
    if cur != "KRW":   # 환율 재조회로 원화값이 바뀌어도 같은 문서가 되도록 외화 원값으로 키
        key = f"{org}|{account_no}|{cur}|{date}|{tm}|{fx_extra['amountForeign']}|{inout}|{fx_extra['balanceAfterForeign']}|{desc_join}|{appr}"
    else:
        key = f"{org}|{account_no}|{date}|{tm}|{amount}|{inout}|{bal_after}|{desc_join}|{appr}"
    did  = hashlib.sha1(key.encode("utf-8")).hexdigest()
    extra = ({"currency": cur, "isForeign": cur != "KRW"} | fx_extra) if JRK_MODE else {}
    return did, extra | {
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

def _codef_call(fn, payload, label):
    try:
        r = json.loads(fn(SVC, payload))
    except Exception as e:
        print(f"  {label} 예외:", repr(e)[:150], flush=True); return {}, False, []
    res = r.get("result") or {}; dd = r.get("data") or {}
    fails = [{k: f.get(k) for k in ("organization","code","message","extraMessage")} for f in (dd.get("errorList") or []) if isinstance(f, dict)]
    print(f"  {label}: {res.get('code')} {res.get('message')} | 성공 {len(dd.get('successList') or [])} 실패:", fails, flush=True)
    return dd, (res.get("code") == "CF-00000" and not fails), fails

def jrk_relink_cert(db, codef):
    """codef_private/jrk_bank_cert {derFile,keyFile,password[,probeOrgs]}가 있으면 새 인증서로 '새 connectedId'를 만든다.
    기존 connectedId(시크릿)는 건드리지 않음 → 실패해도 기존 수집 유지. 하나(0081) 계좌목록이 새 connectedId로 읽히면
    jrk_bank_conf.connectedId로 전환. probeOrgs(타행)는 같은 인증서로 등록 시도 → 계좌가 나온 은행만 orgs에 추가.
    인증서·암호 사본은 성공·실패 무관 즉시 삭제. (비밀값·계좌번호 전체는 로그에 안 찍음 — 레포 public)"""
    ref = db.collection("codef_private").document("jrk_bank_cert")
    snap = ref.get()
    x = snap.to_dict() if snap.exists else None
    if not (x and x.get("derFile") and x.get("keyFile") and x.get("password")): return
    print("[JRK] 새 인증서로 connectedId 생성 시도", flush=True)
    def item(org):
        it = {"countryCode":"KR","businessType":"BK","clientType":"B","organization":org,"loginType":"0",
              "derFile":x["derFile"],"keyFile":x["keyFile"],"password":encrypt_rsa(x["password"], PUBLIC_KEY)}
        if BIRTHDATE: it["birthDate"] = BIRTHDATE
        return it
    report = {"triedAt": firestore.SERVER_TIMESTAMP, "orgs": {}}
    try:
        dd, ok, fails = _codef_call(codef.create_account, {"accountList":[item("0081")]}, "생성 하나(0081)")
        new_cid = dd.get("connectedId")
        report["orgs"]["0081"] = {"ok": ok, "fails": fails}
        if ok and new_cid:
            for org in (x.get("probeOrgs") or []):
                _, ok2, f2 = _codef_call(codef.add_account, {"connectedId":new_cid, "accountList":[item(org)]}, f"추가 {BANK_NAMES.get(org,org)}({org})")
                report["orgs"][org] = {"ok": ok2, "fails": f2}
            found, accts_all = [], {}
            for org in ["0081"] + [o for o in (x.get("probeOrgs") or []) if report["orgs"].get(o, {}).get("ok")]:
                r = json.loads(codef.request_product(PATH_ACCOUNTS, SVC, {"organization":org, "connectedId":new_cid, "birthDate":BIRTHDATE}))
                code = (r.get("result") or {}).get("code"); data = r.get("data") or {}
                lst = []
                if isinstance(data, dict):
                    for kind in ("resDepositTrust","resForeignCurrency","resFund","resLoan","resInsurance"):
                        for a in (data.get(kind) or []):
                            if isinstance(a, dict):
                                lst.append({"kind":kind, "no":str(_g(a,"resAccount","resAccountDisplay")), "name":str(_g(a,"resAccountName","resAccountNickName")),
                                            "currency":str(_g(a,"resAccountCurrency", default="")), "balance":str(_g(a,"resAccountBalance", default=""))})
                print(f"  계좌목록 {BANK_NAMES.get(org,org)}({org}) {code}: " + ", ".join(f"{a['kind']}:{a['name']}…{a['no'][-4:]} {a['currency']}" for a in lst), flush=True)
                accts_all[org] = {"code": code, "accounts": lst}
                if code == "CF-00000" and lst: found.append(org)
            report["accounts"] = accts_all
            if "0081" in found:
                db.collection("codef_private").document("jrk_bank_conf").set(
                    {"connectedId": new_cid, "orgs": found, "switchedAt": firestore.SERVER_TIMESTAMP, "certNote": "2026-10-22 발급 조회용 인증서"}, merge=True)
                report["switched"] = True
                print(f"[JRK] 새 connectedId로 전환 — 수집 은행 {found}", flush=True)
            else:
                report["switched"] = False
                print("[JRK] 새 connectedId로 하나 계좌목록 실패 → 전환 안 함(기존 connectedId 유지)", flush=True)
            report["newCidHead"] = new_cid[:6]
    finally:
        ref.set({"derFile": firestore.DELETE_FIELD, "keyFile": firestore.DELETE_FIELD, "password": firestore.DELETE_FIELD,
                 "probeOrgs": firestore.DELETE_FIELD, "lastReport": report}, merge=True)

def jrk_cleanup_old(db, codef, conf):
    """conf.cleanupOldCid=true면 옛 connectedId(시크릿)의 하나 계정(옛 범용 인증서) 삭제 — 옛 인증서가 CODEF에 남지 않게."""
    old = os.environ["CODEF_CONNECTED_ID"]
    if not conf.get("cleanupOldCid") or not conf.get("connectedId") or conf["connectedId"] == old: return
    _, ok, _ = _codef_call(codef.delete_account, {"connectedId": old, "accountList":[
        {"countryCode":"KR","businessType":"BK","clientType":"B","organization":"0081","loginType":"0"}]}, "옛 connectedId 하나 계정 삭제")
    db.collection("codef_private").document("jrk_bank_conf").set(
        {"cleanupOldCid": firestore.DELETE_FIELD, "oldCidCleanup": "ok" if ok else "fail", "oldCidCleanupAt": firestore.SERVER_TIMESTAMP}, merge=True)

def main():
    global CONNECTED_ID
    db = init_db()
    codef = make_codef()
    col = db.collection(COLLECTION)
    orgs = [ORG_ONLY] if ORG_ONLY else list(ORG_NAME)
    if JRK_MODE:
        try:
            jrk_relink_cert(db, codef)
            cs = db.collection("codef_private").document("jrk_bank_conf").get()
            conf = cs.to_dict() if cs.exists else {}
            if conf.get("connectedId"):
                CONNECTED_ID = conf["connectedId"]
                print(f"[JRK] connectedId = Firestore 설정({CONNECTED_ID[:6]}…)", flush=True)
            if conf.get("orgs"):
                orgs = list(conf["orgs"])
            jrk_cleanup_old(db, codef, conf)
        except Exception as e:
            print("[JRK] 설정/인증서 처리 실패(기존 방식으로 수집):", repr(e)[:150], flush=True)
        for o in orgs:
            ORG_NAME.setdefault(o, BANK_NAMES.get(o, o))
    total = 0
    stats = {}   # 계좌명 -> {min, max, cnt}
    batch = db.batch(); n = 0
    for org in orgs:
        print("수집:", ORG_NAME[org], flush=True)
        acctpw_raw = os.environ.get(ACCTPW_KEY.get(org, ""), "")
        acctpw_enc = encrypt_rsa(acctpw_raw, PUBLIC_KEY) if acctpw_raw else ""
        accts = list_accounts(codef, org)
        if not accts:
            print(f"  [{ORG_NAME[org]}] 보유계좌 없음/조회실패 — 건너뜀", flush=True)
            continue
        if JRK_MODE:   # 계좌별 현재잔액 스냅샷(외화는 거래내역 상품 권한 없어도 잔액은 계좌목록으로 옴, 10-28 CF-00401)
            try:
                for a in accts:
                    cur = (a.get("currencies") or ["KRW"])[0] if a.get("fx") else "KRW"
                    try: bal = float(str(a.get("balance") or "0").replace(",", ""))
                    except: bal = 0.0
                    rate = fx_rate(cur, datetime.date.today().strftime("%Y-%m-%d")) if cur != "KRW" else 1.0
                    db.collection("jrk_bank_accounts").document(f"{org}_{a['no'][-4:]}_{cur}").set({
                        "bank": ORG_NAME.get(org, org), "org": org, "accountNoMasked": a["no"], "name": a["name"], "currency": cur,
                        "isForeign": cur != "KRW", "balance": int(round(bal * rate)) if rate else 0,
                        "balanceForeign": bal if cur != "KRW" else None, "fxRate": rate if cur != "KRW" else None,
                        "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True)
            except Exception as e:
                print("  (잔액 스냅샷 실패:", repr(e)[:100], ")", flush=True)
        for a in accts:
            print(f"  계좌 …{a['no'][-4:]} ({a['name']}{' 외화 ' + '/'.join(a['currencies']) if a.get('fx') else ''})", flush=True)
            acnt = 0
            if a.get("fx"):
                trs = [t for c in a["currencies"] for t in fetch_fx_trans(codef, org, a["no"], c)]
            else:
                trs = fetch_trans(codef, org, a["no"], acctpw_enc)
            for t in trs:
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

def retag():
    """[재라벨 모드] CODEF 재조회 없이, Firestore에 이미 저장된 desc/inout 만으로
    category·vendorMemo 를 다시 계산해 갱신한다. (CODEF 일일 호출한도 소모 없음)

    실행:  collect_bank.yml -> Run workflow
             lookback_days = 5555   (재라벨 모드)
             debug         = 1      -> 드라이런(변경내역만 출력, 쓰기 없음)
                             0      -> 실제 적용
    ※ 분류키워드(OUT_VENDOR/IN_VENDOR/VENDOR_MEMO)를 고친 뒤 과거분에 소급 반영할 때 사용.
    """
    db = init_db()
    col = db.collection(COLLECTION)
    dry = DEBUG
    print("[재라벨] collection=%s / 모드=%s" % (COLLECTION, "드라이런(쓰기 없음)" if dry else "실제 적용"), flush=True)

    cat_moves, memo_moves, samples = {}, {}, []
    scanned = changed = 0
    batch = db.batch(); n = 0

    for snap in col.stream():
        d = snap.to_dict() or {}
        scanned += 1
        inout = d.get("inout", "")
        desc = d.get("desc", "")
        if not inout:
            continue
        old_cat, new_cat = d.get("category", ""), categorize(inout, desc)
        old_memo, new_memo = d.get("vendorMemo", ""), vendor_memo(desc)
        upd = {}
        if new_cat != old_cat:
            upd["category"] = new_cat
            k = "%s -> %s" % (old_cat or "(없음)", new_cat or "(없음)")
            cat_moves[k] = cat_moves.get(k, 0) + 1
        if new_memo != old_memo:
            upd["vendorMemo"] = new_memo
            k = "%s -> %s" % (old_memo or "(없음)", new_memo or "(없음)")
            memo_moves[k] = memo_moves.get(k, 0) + 1
        if not upd:
            continue
        changed += 1
        if len(samples) < 60:
            samples.append("  %s %14s원 %-28s [%s -> %s] memo[%s -> %s]" % (
                d.get("date", ""), format(int(d.get("amount") or 0), ","),
                (d.get("counterparty") or "")[:28],
                old_cat or "-", new_cat or "-", old_memo or "-", new_memo or "-"))
        if not dry:
            upd["retaggedAt"] = firestore.SERVER_TIMESTAMP
            batch.set(col.document(snap.id), upd, merge=True)
            n += 1
            if n >= 400:
                batch.commit(); batch = db.batch(); n = 0

    if not dry and n:
        batch.commit()

    print("", flush=True)
    print("── 카테고리 변경 요약 ──", flush=True)
    for k in sorted(cat_moves, key=lambda x: -cat_moves[x]):
        print("  %-46s %d건" % (k, cat_moves[k]), flush=True)
    if not cat_moves:
        print("  (변경 없음)", flush=True)
    print("", flush=True)
    print("── vendorMemo(내원경로) 변경 요약 ──", flush=True)
    for k in sorted(memo_moves, key=lambda x: -memo_moves[x]):
        print("  %-46s %d건" % (k, memo_moves[k]), flush=True)
    if not memo_moves:
        print("  (변경 없음)", flush=True)
    if samples:
        print("", flush=True)
        print("── 변경 예시(최대 60건) ──", flush=True)
        for line in samples:
            print(line, flush=True)

    print("", flush=True)
    print("스캔 %d건 / 변경대상 %d건 %s" % (
        scanned, changed, "(드라이런 — 아무것도 쓰지 않음)" if dry else "-> 적용완료"), flush=True)

    if not dry:
        db.collection("bank_sync_log").add({
            "ranAt": firestore.SERVER_TIMESTAMP, "env": ENV,
            "mode": "retag", "scanned": scanned, "retagged": changed,
        })


if __name__ == "__main__":
    _lb = os.environ.get("LOOKBACK_DAYS")
    if _lb == "9999":
        import deposit_slack_notifier as _dn; _dn.cli_dry_run()  # 수집 안 함, 입금 dry-run만
    elif _lb == "7777":
        import deposit_slack_notifier as _dn; _dn.cli_seed()      # 시드(발송처리만, 전송 안 함)
    elif _lb == "5555":
        retag()                                                   # 재라벨(CODEF 미사용). debug=1이면 드라이런
    else:
        main()
