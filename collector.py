# -*- coding: utf-8 -*-
"""
청담리투의원 카드 결제내역 자동 수집기
- CODEF에서 7개 카드(KB/현대/삼성/롯데/하나/IBK비씨/신한) 승인내역 수집
- 가맹점/업종 기반 지출 분류
- Firebase Firestore(card_expenses)에 중복 없이 저장(upsert)
환경변수(깃허브 시크릿)로 모든 설정 주입. 코드에 비밀정보 없음.
"""
import os, json, time, datetime, hashlib, sys, signal
from easycodefpy import Codef, ServiceType
from easycodefpy.util import encrypt_rsa
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- 설정(환경변수) ----------
ENV = "api"  # 2026-08 정식전환 강제 (yml env 무시)
CLIENT_ID    = os.environ["CODEF_CLIENT_ID"]
CLIENT_SECRET= os.environ["CODEF_CLIENT_SECRET"]
PUBLIC_KEY   = os.environ["CODEF_PUBLIC_KEY"]
CONNECTED_ID = os.environ["CODEF_CONNECTED_ID"]
BIRTHDATE    = os.environ.get("CODEF_BIRTHDATE", "")
HD_CARDNO    = os.environ.get("HD_CARDNO", "")   # 현대카드 카드번호
HD_CARDPW    = os.environ.get("HD_CARDPW", "")   # 현대카드 비번 4자리
LOOKBACK     = int(os.environ.get("LOOKBACK_DAYS", "14"))  # 매일 실행이면 14일 겹쳐 수집(누락방지)
HD_CONFIRMED = os.environ.get("HD_CONFIRMED", "0")  # "1"이면 현대카드를 결제확정(부가세포함) 모드로 최근 2년 조회
COLLECTION   = os.environ.get("FIRESTORE_COLLECTION", "card_expenses")

SVC = ServiceType.PRODUCT if ENV in ("api","prod","product") else ServiceType.DEMO
ORG_NAME = {"0301":"KB국민","0302":"현대","0303":"삼성","0305":"IBK기업(비씨)","0311":"롯데","0313":"하나","0306":"신한"}
MAX_MONTHS = {"0301":12,"0303":12,"0313":18,"0311":6,"0302":3,"0305":9,"0306":12}
PATH = "/v1/kr/card/p/account/approval-list"
CARD_CONF = {}   # Firestore codef_private/<org> {cardNo, cardPassword} — 2026-10 하나카드가 cardNo 필수로 바뀜(CF-12401 reqCardNo). 클라이언트 규칙상 접근 불가(관리자 SDK만)
ORG_ERRORS = {}  # 2026-10-19 기관별 오류 기록 → card_sync_log.errors (하나 CF-12401이 2주 넘게 조용히 묻혔던 것 재발 방지)

# ---------- 분류 규칙 ----------
VENDOR = {
 "의료재료/약품/장비":["케어캠프","지오영","파마리서치","파마","휴메딕스","휴젤","멀츠","merz","애브비","abbvie",
   "갈더마","galderma","엘러간","allergan","바임","vaim","소프웨이브","sofwave","루트로닉","lutronic","클래시스","classys",
   "인터케어","제테마","종근당","대웅","도프","미라","mira","bbl","에스엠에이","sma","제이시스","jeisys","원텍","이루다",
   "하이로닉","큐레이","의료기","메디","medi","약품","비엠아이","bmi","한독","휴온스","동국제약",
   "엘앤씨바이오","l&c","lnc","바이오메드","jsk","덱스레보","dexlevo","코델","레이저옵텍","옵텍","optech",
   "이트리얼","제약","바이오","스킨부스터","엑소좀","리쥬란","톡신","필러"],
 "세금/공과/수수료":["국세","지방세","세무","4대보험","건강보험","국민연금","수수료","공과금","한국전력","한전"],
 "보험":["손해보험","화재보험","현대해상","삼성화재","db손해","kb손해","메리츠","한화손해","흥국","롯데손해","다이렉트"],
 "채용/HR":["사람인","잡코리아","인크루트","피플앤잡","원티드"],
 "임대/관리비":["임대","관리비","부동산","빌딩"],
 "통신/IT":["kt","skt","lg유플","유플러스","통신","인터넷","아이디어스","구독","세스코"],
 "광고/마케팅":["네이버","카카오","메타","google","facebook","instagram","당근","강남언니","바비톡","굿닥","광고"],
 "교통/주유/출장":["택시","주유","gs칼텍스","에쓰오일","s-oil","sk에너지","현대오일","고속버스","코레일","ktx","주차","하이패스","항공","비엣젯","대한항공","아시아나","bsp"],
 "식대/카페/접대":["식당","한식","카페","커피","스타벅스","베이커리","제과","김밥","배달","요기요","쿠팡이츠"],
 "비품/사무/마트":["다이소","오피스","문구","쿠팡","11번가","지마켓","옥션","이마트","마트","정수기","비데"],
 "백화점/명품(확인)":["백화점","명품관","갤러리아","신세계","현대백화점","롯데백화점"],
}
UPJONG = {"의약품판매":"의료재료/약품/장비","기타약품.의료기":"의료재료/약품/장비","의료기기 및 용품":"의료재료/약품/장비",
 "화장품":"의료재료/약품/장비","화장품점":"의료재료/약품/장비","기계.장비(기타)":"의료재료/약품/장비",
 "정수기.비데":"비품/사무/마트","정수기판매점":"비품/사무/마트","대형할인매장":"비품/사무/마트",
 "택시":"교통/주유/출장","고속버스":"교통/주유/출장","항공사":"교통/주유/출장","공과금":"세금/공과/수수료",
 "손해보험(본점,지점)":"보험","손해보험":"보험","생명보험":"보험",
 "일반한식":"식대/카페/접대","제과점":"식대/카페/접대","편의점":"식대/카페/접대","백화점":"백화점/명품(확인)"}

def categorize(name, upjong):
    s = str(name).lower()
    for cat, kws in VENDOR.items():
        for kw in kws:
            if kw.lower() in s:
                return cat
    return UPJONG.get(str(upjong), "미분류")

# ---------- Firestore 초기화 ----------
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

def card_no(org, no):
    """2026-10-12 CODEF 공지: 하나카드(0313) 아이디 로그인 cardNo = 카드번호 앞 4 + 뒤 4 (8자리, 예 12341234). 16자리 전체를 넣으면 CF-12401 reqCardNo 지속."""
    no = "".join(ch for ch in str(no or "") if ch.isdigit())
    return no[:4] + no[-4:] if org == "0313" and len(no) > 8 else no

def conf_probe(org):
    return bool((CARD_CONF.get(org) or {}).get("probe"))

def fetch_org(codef, org):
    today = datetime.date.today()
    max_m = MAX_MONTHS.get(org, 6)
    hd_conf = (org == "0302" and HD_CONFIRMED == "1")
    if hd_conf: max_m = 24            # 현대 결제확정(부가세포함) 모드 = 최근 2년 조회 가능
    mst = "3" if hd_conf else "1"     # 3=가맹점+부가세(결제확정), 1=실시간승인
    back = min(LOOKBACK, int(max_m * 30))
    start = today - datetime.timedelta(days=back)
    # 2026-10-19 카드번호 필수 카드사(하나): codef_private/<org>.cards = [{cardNo, cardPassword}] 카드별로 따로 조회
    conf = CARD_CONF.get(org, {})
    cards = conf.get("cards") or ([conf] if conf.get("cardNo") else [None])
    rows = []
    for card in cards:
        rows += _fetch_card(codef, org, start, today, mst, card)
    return rows

def probe_card(codef, org, card):
    """codef_private/<org>.probe=true면 7일 창으로 파라미터 조합을 시험해 첫 성공 조합을 card에 반영(비밀값 출력 없음)."""
    t = datetime.date.today(); s = t - datetime.timedelta(days=7)
    pw = card.get("cardPassword") or ""
    variants = [("8+pw/0", card_no(org, card["cardNo"]), pw, "0"), ("8+pw/1", card_no(org, card["cardNo"]), pw, "1"),
                ("plain+pw/0", card["cardNo"], pw, "0"), ("plain+pw/1", card["cardNo"], pw, "1"),
                ("rsa+pw/0", encrypt_rsa(card["cardNo"], PUBLIC_KEY), pw, "0"), ("none/1", "", "", "1")]
    for name, no, cpw, inq in variants:
        p = {"organization":org,"connectedId":CONNECTED_ID,"birthDate":BIRTHDATE,"startDate":s.strftime("%Y%m%d"),
             "endDate":t.strftime("%Y%m%d"),"orderBy":"0","inquiryType":inq,"memberStoreInfoType":"1",
             "cardNo":no,"cardPassword":encrypt_rsa(cpw, PUBLIC_KEY) if cpw else ""}
        try:
            res = codef_call(codef, PATH, p).get("result") or {}
        except Exception as e:
            res = {"code": "EXC", "message": repr(e)[:80]}
        print(f"  probe {ORG_NAME.get(org,org)}…{card['cardNo'][-4:]} {name}: {res.get('code')} {res.get('message')} | {res.get('extraMessage')}", flush=True)
        if res.get("code") == "CF-00000":
            card["_variant"] = (name, no, cpw, inq); return
        time.sleep(1)

def _fetch_card(codef, org, start, today, mst, card):
    if card and conf_probe(org) and "_variant" not in card:
        probe_card(codef, org, card)
    rows, ws = [], start
    while ws < today:
        we = min(ws + datetime.timedelta(days=60), today)
        p = {"organization":org,"connectedId":CONNECTED_ID,"birthDate":BIRTHDATE,
             "startDate":ws.strftime("%Y%m%d"),"endDate":we.strftime("%Y%m%d"),
             "orderBy":"0","inquiryType":"1","memberStoreInfoType":mst,"cardNo":"","cardPassword":""}
        cf = CARD_CONF.get(org) or {}
        rrn = BIRTHDATE[2:] + str(cf.get("rrnBack") or "")   # 주민번호 = 시크릿 생년월일 6자리 + 비공개 설정 뒤 7자리
        for k, v in (cf.get("params") or {}).items():
            p[k] = {"$BIRTH8": BIRTHDATE, "$BIRTH6": BIRTHDATE[2:], "$RRN": rrn,
                    "$RRN_RSA": encrypt_rsa(rrn, PUBLIC_KEY),
                    "$ID7": rrn[:7], "$ID7_RSA": encrypt_rsa(rrn[:7], PUBLIC_KEY)}.get(v, v)   # 2026-10-19 카드사별 추가 파라미터(롯데 패치 후 userName 필수 CF-12851) · 2026-10-20 헥토 회신: 카드(개인) 승인내역 identity = 생년월일 6 + 뒷자리 첫째 1
        if org == "0302":
            p["cardNo"] = HD_CARDNO
            p["cardPassword"] = encrypt_rsa(HD_CARDPW, PUBLIC_KEY) if HD_CARDPW else ""
        elif card and card.get("_variant"):
            _, p["cardNo"], cpw, p["inquiryType"] = card["_variant"]
            p["cardPassword"] = encrypt_rsa(cpw, PUBLIC_KEY) if cpw else ""
        elif card:
            p["cardNo"] = card_no(org, card["cardNo"])
            p["inquiryType"] = card.get("inquiryType", "0")   # 0=개별카드 조회(카드번호 지정)
            pw = card.get("cardPassword") or ""
            p["cardPassword"] = encrypt_rsa(pw, PUBLIC_KEY) if pw else ""
        r = None
        for attempt in range(3):   # CODEF 연결 끊김(RemoteDisconnected) 등 일시 오류 재시도
            try:
                r = codef_call(codef, PATH, p); break
            except Exception as e:
                print(f"  [{ORG_NAME.get(org,org)}] {ws}~{we} 요청 실패({attempt+1}/3): {repr(e)[:120]}", flush=True)
                time.sleep(10 * (attempt + 1))
        if r is None:
            ws = we + datetime.timedelta(days=1); continue
        code = (r.get("result") or {}).get("code")
        if code == "CF-00016":   # 앞 요청(타임아웃 난 것)이 CODEF에서 아직 처리 중 → 잠시 후 1회 재요청
            time.sleep(45)
            try: r = codef_call(codef, PATH, p); code = (r.get("result") or {}).get("code")
            except Exception as e: print("  CF-00016 재요청 실패:", repr(e)[:80], flush=True)
        if code == "CF-03002":   # 2026-10-20 롯데: 휴대폰 문자 추가인증(2-way)
            r = two_way(codef, org, p, r); code = (r.get("result") or {}).get("code")
        if code != "CF-00000":
            res = r.get('result') or {}
            print(f"  [{ORG_NAME.get(org,org)}{'…'+card['cardNo'][-4:] if card else ''}] {ws}~{we} {code}: {res.get('message')} | extra: {res.get('extraMessage')} | tx: {res.get('transactionId')}", flush=True)
            ORG_ERRORS[ORG_NAME.get(org, org) + (f"(…{card['cardNo'][-4:]})" if card else "")] = f"{code}: {res.get('message')} {res.get('extraMessage') or ''}".strip()
        d = r.get("data"); d = [d] if isinstance(d, dict) else (d or [])
        # 2026-10-19 CODEF 공지: 조회 결과 없음이면 data={"commSession":...} 객체로 옴 → 결제 행이 아니므로 버림
        d = [x for x in d if isinstance(x, dict) and (x.get("resApprovalNo") or x.get("resUsedDate"))]
        rows += d
        ws = we + datetime.timedelta(days=1)
        time.sleep(1)
    return rows

def two_way(codef, org, p, r):
    """2026-10-20 롯데(0311)가 조회 때 휴대폰 문자 추가인증(CF-03002)을 요구.
    codef_private/<org>.twoWayOnce=true일 때만: 대기 상태를 codef_private/<org>_2way에 쓰고, 클로드가 원장 카톡으로 받은 인증번호를
    그 문서 authNo에 넣으면(최대 270초 대기) 2차 요청. 평소 실행(플래그 없음)은 기다리지 않고 오류로 남긴다. 인증번호는 쓰고 바로 지움."""
    d = r.get("data") or {}
    print(f"  [{ORG_NAME.get(org,org)}] 추가인증 요구: method={d.get('method')} extraInfo키={sorted((d.get('extraInfo') or {}).keys())} "
          f"키={sorted(k for k in d if k not in ('jti','jobIndex','threadIndex','twoWayTimestamp'))}", flush=True)
    if not (CARD_CONF.get(org) or {}).get("twoWayOnce") or DB is None:
        return r
    ref = DB.collection("codef_private").document(f"{org}_2way")
    ref.set({"status": "waiting", "method": d.get("method"), "at": firestore.SERVER_TIMESTAMP})
    DB.collection("codef_private").document(org).update({"twoWayOnce": firestore.DELETE_FIELD})
    code = None
    for _ in range(54):
        time.sleep(5)
        code = (ref.get().to_dict() or {}).get("authNo")
        if code: break
    ref.update({"authNo": firestore.DELETE_FIELD, "status": "got" if code else "timeout"})
    if not code:
        print("  추가인증 번호 대기 시간 초과", flush=True); return r
    q = dict(p); q[d.get("method") or "smsAuthNo"] = str(code)
    q["is2Way"] = True
    q["twoWayInfo"] = {k: d.get(k) for k in ("jobIndex", "threadIndex", "jti", "twoWayTimestamp")}
    try:
        r2 = json.loads(codef.request_certification(PATH, SVC, q))
    except Exception as e:
        print("  추가인증 요청 실패:", repr(e)[:120], flush=True); return r
    res = r2.get("result") or {}
    print(f"  추가인증 결과: {res.get('code')} {res.get('message')} | {res.get('extraMessage')}", flush=True)
    ref.update({"status": res.get("code")})
    return r2

class _Timeout(Exception): pass

def codef_call(codef, path, params, sec=240):
    """2026-10-19 CODEF 응답이 안 와 실행이 38분 멈춘 일 → 호출마다 240초 제한(신한은 90초 넘게 걸릴 때 있음)(리눅스 러너 SIGALRM)."""
    def _h(*_): raise _Timeout(f"CODEF {sec}s timeout")
    old = signal.signal(signal.SIGALRM, _h); signal.alarm(sec)
    try:
        return json.loads(codef.request_product(path, SVC, params))
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, old)

def _num(v):
    """'1,234' / '74.23' / '' 모두 안전하게 숫자로."""
    try:
        return float(str(v if v is not None else "0").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0

def to_doc(org, t):
    date = t.get("resUsedDate","")
    tm   = t.get("resUsedTime","")
    merch= str(t.get("resMemberStoreName","")).strip().rstrip("\\")
    # 2026-10-18 해외승인(통화≠KRW)은 금액이 '74.23' 같은 소수로 와서 int() 크래시 → 수집 전체 중단되던 버그 수정.
    amt_raw = _num(t.get("resUsedAmount","0"))
    cur  = str(t.get("resAccountCurrency") or "KRW").strip() or "KRW"
    foreign = cur not in ("KRW", "원")
    amt  = int(round(amt_raw))
    appr = t.get("resApprovalNo","")
    key  = f"{org}|{date}|{tm}|{amt}|{merch}|{appr}"
    did  = hashlib.sha1(key.encode("utf-8")).hexdigest()
    cancel = {"0":"정상","1":"취소","2":"부분취소","3":"거절"}.get(t.get("resCancelYN","0"),"")
    return did, {
        "card": ORG_NAME.get(org, org), "org": org,
        "date": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date)==8 else date,
        "month": f"{date[:4]}-{date[4:6]}" if len(date)>=6 else "",
        "time": tm, "merchant": merch, "upjong": t.get("resMemberStoreType",""),
        "amount": amt, "amountRaw": amt_raw,
        "category": ("해외결제(확인)" if foreign else categorize(merch, t.get("resMemberStoreType",""))),
        "paymentType": {"1":"일시불","2":"할부","3":"기타"}.get(t.get("resPaymentType",""),""),
        "installment": t.get("resInstallmentMonth",""),
        "cancelYN": cancel, "approvalNo": appr,
        "corpNo": t.get("resMemberStoreCorpNo",""),
        "cardNoMasked": t.get("resCardNo",""),
        "currency": cur, "foreign": foreign,
        # 해외승인은 원화 청구액·환율 등 카드사별 추가 필드를 그대로 보존(나중에 원화 확정용)
        "raw": ({k: str(v) for k, v in t.items() if v not in ("", None)} if foreign else None),
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }


def load_id_map(col):
    """★2026-09-29 중복적재 방지: 이미 저장된 결제의 문서ID를 재사용하기 위한 지도.
    문서ID 해시에 가맹점명이 들어가서, 카드사가 가맹점명을 나중에 바꿔 보내면 같은 결제가 새 문서로 또 저장됐다(승인번호 동일 2문서).
    키 = (카드사·날짜·시각·금액·승인번호·취소구분). 승인번호가 다르면 다른 결제이므로 같은 금액 2회 결제는 그대로 각각 저장된다.
    조회 실패 시 예전 동작(해시ID)으로 계속 진행 — 수집이 멈추면 안 된다."""
    m = {}
    try:
        from google.cloud.firestore_v1 import FieldFilter
        since = (datetime.date.today() - datetime.timedelta(days=LOOKBACK + 3)).isoformat()
        q = col.where(filter=FieldFilter("date", ">=", since)).select(["org", "date", "time", "amount", "approvalNo", "cancelYN"])
        for d in q.stream():
            x = d.to_dict() or {}
            ap = str(x.get("approvalNo") or "").strip()
            if not ap:
                continue
            k = (x.get("org"), x.get("date"), str(x.get("time") or ""), int(x.get("amount") or 0), ap, x.get("cancelYN"))
            m.setdefault(k, d.id)
    except Exception as e:
        print("[경고] 기존 문서 조회 실패 — 문서ID 재사용 없이 진행:", e, flush=True)
    return m

def resolve_id(idmap, did, doc):
    """같은 결제가 이미 있으면 그 문서ID를, 없으면 새 ID를 등록해 쓴다(같은 실행 안의 중복도 함께 흡수)."""
    ap = str(doc.get("approvalNo") or "").strip()
    if not ap:
        return did
    k = (doc.get("org"), doc.get("date"), str(doc.get("time") or ""), int(doc.get("amount") or 0), ap, doc.get("cancelYN"))
    return idmap.setdefault(k, did)

def relink_accounts(db, codef):
    """2026-10-19 하나카드가 계정 로그인에 cardNo 필수(CF-12401 reqCardNo)로 바뀜 → 원장이 로컬 스크립트로 넣은
    codef_private/<org>_login {id, password[, cardPassword]}로 CODEF 계정을 cardNo 포함해 갱신하고, 비밀번호는 즉시 삭제."""
    for d in db.collection("codef_private").stream():
        if d.id.endswith("_cert"):
            relink_cert(db, codef, d); continue
        if not d.id.endswith("_login"): continue
        org = d.id[:-6]; x = d.to_dict() or {}
        if not (x.get("id") and x.get("password")): continue
        cards = (CARD_CONF.get(org) or {}).get("cards") or []
        item = {"countryCode":"KR","businessType":"CD","clientType":"P","organization":org,"loginType":"1",
                "id":x["id"],"password":encrypt_rsa(x["password"], PUBLIC_KEY),"birthDate":BIRTHDATE}
        if cards:   # 로그인 카드 = 원장이 입력한 카드비번(앞 2자리)과 같은 비번을 가진 카드
            lc = next((c for c in cards if x.get("cardPassword") and c.get("cardPassword") == x["cardPassword"]), cards[0])
            item["cardNo"] = lc["cardNo"]   # 계정 등록은 13~16자리 전체(8자리면 CF-13104 (13-16))
        if x.get("cardPassword"): item["cardPassword"] = encrypt_rsa(x["cardPassword"], PUBLIC_KEY)
        # 2026-10-21 update_account는 CF-00000을 내고도 실제 반영 안 되는 경우가 있음(롯데에서 확인) → recreate=true면 삭제 후 새로 등록
        steps = [("삭제", codef.delete_account, {k: item[k] for k in ("countryCode","businessType","clientType","organization","loginType")}),
                 ("등록", codef.add_account, item)] if x.get("recreate") else [("갱신", codef.update_account, item)]
        for label, fn, acc in steps:
            try:
                r = json.loads(fn(SVC, {"connectedId": CONNECTED_ID, "accountList": [acc]}))
                res = r.get("result") or {}; dd = r.get("data") or {}
                print(f"계정 {label} {ORG_NAME.get(org,org)}: {res.get('code')} {res.get('message')} | 성공 {len(dd.get('successList') or [])} 실패:",
                      [ {k: f.get(k) for k in ("code","message","extraMessage")} for f in (dd.get("errorList") or dd.get("failList") or []) if isinstance(f, dict)], flush=True)
            except Exception as e:
                print(f"계정 {label} {org} 예외:", repr(e)[:150], flush=True)
        d.reference.update({"password": firestore.DELETE_FIELD, "cardPassword": firestore.DELETE_FIELD, "recreate": firestore.DELETE_FIELD,
                            "lastTriedAt": firestore.SERVER_TIMESTAMP})

def relink_cert(db, codef, d):
    """2026-10-20 헥토 회신: 롯데 개인 아이디 로그인은 조회마다 문자 추가인증 필수 → 무인 수집은 공동인증서 로그인으로.
    원장이 로컬 스크립트로 넣은 codef_private/<org>_cert {derFile, keyFile(base64), password}로 CODEF 계정을 인증서 방식으로 바꾸고,
    인증서·암호 사본은 즉시 삭제. 성공하면 아이디 로그인용 추가 파라미터(identity·telecom 등)는 params_idlogin으로 치워둔다."""
    org = d.id[:-5]; x = d.to_dict() or {}
    if not (x.get("derFile") and x.get("keyFile") and x.get("password")): return
    item = {"countryCode":"KR","businessType":"CD","clientType":"P","organization":org,"loginType":"0",
            "derFile":x["derFile"],"keyFile":x["keyFile"],"password":encrypt_rsa(x["password"], PUBLIC_KEY),"birthDate":BIRTHDATE}
    def _try(fn, label, acc=item):
        try:
            r = json.loads(fn(SVC, {"connectedId": CONNECTED_ID, "accountList": [acc]}))
        except Exception as e:
            print(f"인증서 {label} {ORG_NAME.get(org,org)} 예외:", repr(e)[:150], flush=True); return False
        res = r.get("result") or {}; dd = r.get("data") or {}
        fails = [{k: f.get(k) for k in ("code","message","extraMessage")} for f in (dd.get("errorList") or []) if isinstance(f, dict)]
        print(f"인증서 {label} {ORG_NAME.get(org,org)}: {res.get('code')} {res.get('message')} | 성공 {len(dd.get('successList') or [])} 실패:", fails, flush=True)
        return res.get("code") == "CF-00000" and not fails
    try:   # 2026-10-21 계정이 이미 지워진 기관은 update_account가 CF-00000을 내고도 아무것도 안 함 → 목록에 없으면 바로 등록
        al = (json.loads(codef.get_account_list(SVC, {"connectedId": CONNECTED_ID})).get("data") or {}).get("accountList") or []
        exists = any(isinstance(a, dict) and a.get("organization") == org for a in al)
    except Exception:
        exists = True
    ok = _try(codef.update_account, "갱신") if exists else _try(codef.add_account, "등록")
    if not ok and exists:   # 같은 기관 아이디 계정이 인증서로 안 바뀌면 지우고 새로 등록
        _try(codef.delete_account, "기존삭제", {k: item[k] for k in ("countryCode","businessType","clientType","organization")} | {"loginType":"1"})
        ok = _try(codef.add_account, "등록")
    d.reference.update({"derFile": firestore.DELETE_FIELD, "keyFile": firestore.DELETE_FIELD, "password": firestore.DELETE_FIELD,
                        "result": "ok" if ok else "fail", "lastTriedAt": firestore.SERVER_TIMESTAMP})
    if ok:
        conf = CARD_CONF.get(org) or {}
        if conf.get("params"):
            db.collection("codef_private").document(org).update({"params_idlogin": conf["params"], "params": firestore.DELETE_FIELD})
            conf.pop("params", None)

DB = None

def main():
    global DB
    db = DB = init_db()
    codef = make_codef()
    col = db.collection(COLLECTION)
    idmap = load_id_map(col)
    try:
        for d in db.collection("codef_private").stream():
            CARD_CONF[d.id] = d.to_dict() or {}
        print("카드번호 설정:", sorted(CARD_CONF), flush=True)
        relink_accounts(db, codef)
    except Exception as e:
        print("codef_private 조회 실패:", repr(e)[:120], flush=True)
    try:  # 등록 기관 목록(비밀값 없음) — 연결 끊김 진단용
        al = (json.loads(codef.get_account_list(SVC, {"connectedId": CONNECTED_ID})).get("data") or {}).get("accountList") or []
        print("등록 기관:", [(a.get("organization"), a.get("businessType"), a.get("loginType")) for a in al if isinstance(a, dict)], flush=True)
    except Exception as e:
        print("등록 기관 조회 실패:", repr(e)[:120], flush=True)
    total, written = 0, 0
    batch = db.batch(); n = 0
    for org in ORG_NAME:
        print("수집:", ORG_NAME[org], flush=True)
        for t in fetch_org(codef, org):
            did, doc = to_doc(org, t)
            did = resolve_id(idmap, did, doc)
            batch.set(col.document(did), doc, merge=True)
            total += 1; n += 1
            if n >= 400:
                batch.commit(); batch = db.batch(); n = 0
    if n: batch.commit()
    written = total
    # 실행 로그 기록
    db.collection("card_sync_log").add({
        "ranAt": firestore.SERVER_TIMESTAMP, "env": ENV,
        "lookbackDays": LOOKBACK, "upserted": written, "errors": ORG_ERRORS,
    })
    if ORG_ERRORS: print("기관 오류:", ORG_ERRORS, flush=True)
    print(f"\n완료: {written}건 upsert (collection={COLLECTION})", flush=True)

if __name__ == "__main__":
    main()
