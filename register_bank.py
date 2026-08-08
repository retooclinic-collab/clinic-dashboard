# -*- coding: utf-8 -*-
"""
청담리투의원 은행계좌 CODEF 등록기 (1회용) — 공동인증서 방식
- 기존 connectedId에 하나(0081)·부산(0032) 기업뱅킹 계좌를 add_account로 추가
- 개인사업자 기업뱅킹 → clientType "B"(기본), loginType "0"(공동인증서)
- ★ CODEF 공식 인증서 필드명: derFile / keyFile / password(RSA암호화)
- 인증서 파일(.der, .key)은 base64로 시크릿에 저장 → 여기서 그대로 사용
- 모든 비밀정보는 환경변수(깃허브 시크릿)로 주입. 코드/로그에 평문 없음.

필요 시크릿:
  CODEF_CLIENT_ID, CODEF_CLIENT_SECRET, CODEF_PUBLIC_KEY, CODEF_CONNECTED_ID, CODEF_BIRTHDATE
  공유 인증서(한 인증서로 두 은행):
    BANK_CERT     = signCert.der 를 base64 인코딩한 문자열 (→ derFile)
    BANK_KEY      = signPri.key  를 base64 인코딩한 문자열 (→ keyFile)
    BANK_CERTPW   = 인증서 암호(평문) — RSA 암호화는 코드가 처리 (→ password)
  (은행별로 다르면 BANK_HANA_CERT/KEY/CERTPW, BANK_BUSAN_CERT/KEY/CERTPW)
환경변수: BANK_CLIENT_TYPE(기본 B), BANK_LOGIN_TYPE(기본 0=인증서)
"""
import os, json
from easycodefpy import Codef, ServiceType
from easycodefpy.util import encrypt_rsa

ENV = "api"  # 2026-08 정식전환 강제 (yml env 무시)
CLIENT_ID    = os.environ["CODEF_CLIENT_ID"]
CLIENT_SECRET= os.environ["CODEF_CLIENT_SECRET"]
PUBLIC_KEY   = os.environ["CODEF_PUBLIC_KEY"]
CONNECTED_ID = os.environ["CODEF_CONNECTED_ID"]
BIRTHDATE    = os.environ.get("CODEF_BIRTHDATE", "")
CLIENT_TYPE  = os.environ.get("BANK_CLIENT_TYPE", "B")
LOGIN_TYPE   = os.environ.get("BANK_LOGIN_TYPE", "0")
DEBUG        = os.environ.get("BANK_DEBUG", "0") == "1"

SVC = ServiceType.PRODUCT if ENV in ("api","prod","product") else ServiceType.DEMO

BANKS = [
    ("0081", "하나",  "HANA"),
    ("0032", "부산",  "BUSAN"),
]

def make_codef():
    c = Codef()
    if SVC == ServiceType.PRODUCT:
        c.set_client_info(CLIENT_ID, CLIENT_SECRET)
    else:
        c.set_demo_client_info(CLIENT_ID, CLIENT_SECRET)
    c.public_key = PUBLIC_KEY
    return c

def registered_orgs(codef):
    r = json.loads(codef.get_account_list(SVC, {"connectedId": CONNECTED_ID}))
    al = (r.get("data") or {}).get("accountList") or (r.get("data") or {}).get("accounts") or []
    return [a.get("organization") for a in al if isinstance(a, dict)]

def cert_material(name):
    cert = (os.environ.get(f"BANK_{name}_CERT")   or os.environ.get("BANK_CERT","")).strip()
    key  = (os.environ.get(f"BANK_{name}_KEY")    or os.environ.get("BANK_KEY","")).strip()
    cpw  =  os.environ.get(f"BANK_{name}_CERTPW") or os.environ.get("BANK_CERTPW","")
    bid  =  os.environ.get(f"BANK_{name}_ID","").strip()
    return cert, key, cpw, bid

def main():
    codef = make_codef()
    print(f"clientType={CLIENT_TYPE} loginType={LOGIN_TYPE} (0=인증서)", flush=True)
    have = set(registered_orgs(codef))
    print("이미 등록된 기관:", sorted(have), flush=True)

    for org, name, key_prefix in BANKS:
        cert, key, cpw, bid = cert_material(key_prefix)
        if LOGIN_TYPE == "0" and (not cert or not key or not cpw):
            print(f"[{name}({org})] 인증서 시크릿 누락(BANK_{key_prefix}_CERT/KEY/CERTPW 또는 BANK_CERT/KEY/CERTPW) — 건너뜀", flush=True)
            continue
        if DEBUG:
            print(f"  [DEBUG {name}] derFile={len(cert)}B keyFile={len(key)}B certpw={'set' if cpw else 'EMPTY'}", flush=True)
        item = {
            "countryCode": "KR",
            "businessType": "BK",
            "clientType":  CLIENT_TYPE,
            "organization": org,
            "loginType":   LOGIN_TYPE,
        }
        if LOGIN_TYPE == "0":
            item["derFile"]  = cert                              # ★ CODEF 정식 필드명
            item["keyFile"]  = key
            item["password"] = encrypt_rsa(cpw, PUBLIC_KEY)      # ★ 인증서 암호 = password
        if bid:
            item["id"] = bid
        param = {"connectedId": CONNECTED_ID, "accountList": [item]}
        r = json.loads(codef.add_account(SVC, param))
        result = r.get("result") or {}
        data   = r.get("data") or {}
        success = data.get("successList") or []
        fail    = data.get("failList") or data.get("errorList") or []
        print(f"[{name}({org})] code={result.get('code')} msg={result.get('message')}", flush=True)
        if success:
            print("  OK 등록 성공", flush=True)
        elif fail:
            for f in fail:
                print(f"  실패: {json.dumps(f, ensure_ascii=False)}", flush=True)
        else:
            print(f"  응답: {json.dumps(r, ensure_ascii=False)[:500]}", flush=True)

    print("최종 등록 기관:", sorted(set(registered_orgs(codef))), flush=True)

if __name__ == "__main__":
    main()
