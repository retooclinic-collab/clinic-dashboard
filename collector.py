# -*- coding: utf-8 -*-
"""
청담리투의원 카드 결제내역 자동 수집기
- CODEF에서 5개 카드(KB/현대/삼성/롯데/하나) 승인내역 수집
- 가맹점/업종 기반 지출 분류
- Firebase Firestore(card_expenses)에 중복 없이 저장(upsert)
환경변수(깃허브 시크릿)로 모든 설정 주입. 코드에 비밀정보 없음.
"""
import os, json, time, datetime, hashlib, sys
from easycodefpy import Codef, ServiceType
from easycodefpy.util import encrypt_rsa
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- 설정(환경변수) ----------
ENV          = os.environ.get("CODEF_ENV", "demo").lower()   # demo / api(정식)
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
ORG_NAME = {"0301":"KB국민","0302":"현대","0303":"삼성","0305":"IBK기업(비씨)","0311":"롯데","0313":"하나"}
MAX_MONTHS = {"0301":12,"0303":12,"0313":18,"0311":6,"0302":3,"0305":9}
PATH = "/v1/kr/card/p/account/approval-list"

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

def fetch_org(codef, org):
    today = datetime.date.today()
    max_m = MAX_MONTHS.get(org, 6)
    hd_conf = (org == "0302" and HD_CONFIRMED == "1")
    if hd_conf: max_m = 24            # 현대 결제확정(부가세포함) 모드 = 최근 2년 조회 가능
    mst = "3" if hd_conf else "1"     # 3=가맹점+부가세(결제확정), 1=실시간승인
    back = min(LOOKBACK, int(max_m * 30))
    start = today - datetime.timedelta(days=back)
    rows, ws = [], start
    while ws < today:
        we = min(ws + datetime.timedelta(days=60), today)
        p = {"organization":org,"connectedId":CONNECTED_ID,"birthDate":BIRTHDATE,
             "startDate":ws.strftime("%Y%m%d"),"endDate":we.strftime("%Y%m%d"),
             "orderBy":"0","inquiryType":"1","memberStoreInfoType":mst,"cardNo":"","cardPassword":""}
        if org == "0302":
            p["cardNo"] = HD_CARDNO
            p["cardPassword"] = encrypt_rsa(HD_CARDPW, PUBLIC_KEY) if HD_CARDPW else ""
        r = json.loads(codef.request_product(PATH, SVC, p))
        code = (r.get("result") or {}).get("code")
        if code != "CF-00000":
            print(f"  [{ORG_NAME.get(org,org)}] {ws}~{we} {code}: {(r.get('result') or {}).get('message')}", flush=True)
        d = r.get("data"); d = [d] if isinstance(d, dict) else (d or [])
        rows += d
        ws = we + datetime.timedelta(days=1)
        time.sleep(1)
    return rows

def to_doc(org, t):
    date = t.get("resUsedDate","")
    tm   = t.get("resUsedTime","")
    merch= str(t.get("resMemberStoreName","")).strip().rstrip("\\")
    amt  = int(t.get("resUsedAmount","0") or 0)
    appr = t.get("resApprovalNo","")
    key  = f"{org}|{date}|{tm}|{amt}|{merch}|{appr}"
    did  = hashlib.sha1(key.encode("utf-8")).hexdigest()
    cancel = {"0":"정상","1":"취소","2":"부분취소","3":"거절"}.get(t.get("resCancelYN","0"),"")
    return did, {
        "card": ORG_NAME.get(org, org), "org": org,
        "date": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date)==8 else date,
        "month": f"{date[:4]}-{date[4:6]}" if len(date)>=6 else "",
        "time": tm, "merchant": merch, "upjong": t.get("resMemberStoreType",""),
        "amount": amt, "category": categorize(merch, t.get("resMemberStoreType","")),
        "paymentType": {"1":"일시불","2":"할부","3":"기타"}.get(t.get("resPaymentType",""),""),
        "installment": t.get("resInstallmentMonth",""),
        "cancelYN": cancel, "approvalNo": appr,
        "corpNo": t.get("resMemberStoreCorpNo",""),
        "cardNoMasked": t.get("resCardNo",""),
        "currency": t.get("resAccountCurrency","KRW"),
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }

def main():
    db = init_db()
    codef = make_codef()
    col = db.collection(COLLECTION)
    total, written = 0, 0
    batch = db.batch(); n = 0
    for org in ORG_NAME:
        print("수집:", ORG_NAME[org], flush=True)
        for t in fetch_org(codef, org):
            did, doc = to_doc(org, t)
            batch.set(col.document(did), doc, merge=True)
            total += 1; n += 1
            if n >= 400:
                batch.commit(); batch = db.batch(); n = 0
    if n: batch.commit()
    written = total
    # 실행 로그 기록
    db.collection("card_sync_log").add({
        "ranAt": firestore.SERVER_TIMESTAMP, "env": ENV,
        "lookbackDays": LOOKBACK, "upserted": written,
    })
    print(f"\n완료: {written}건 upsert (collection={COLLECTION})", flush=True)

if __name__ == "__main__":
    main()
