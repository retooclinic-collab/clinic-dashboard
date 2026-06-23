# -*- coding: utf-8 -*-
"""
청담리투의원 은행계좌 CODEF 등록기 (1회용)
- 기존 connectedId(dWaQN8n...)에 하나(0081)·부산(0032) 은행계좌를 add_account(businessType=BK)로 추가
- 아이디/비밀번호 로그인 방식 (loginType="1")
- 모든 비밀정보는 환경변수(깃허브 시크릿)로 주입. 코드/로그에 평문 비번 없음.

필요 시크릿:
  CODEF_CLIENT_ID, CODEF_CLIENT_SECRET, CODEF_PUBLIC_KEY, CODEF_CONNECTED_ID, CODEF_BIRTHDATE
  BANK_HANA_ID,  BANK_HANA_PW      (하나은행 인터넷뱅킹 아이디/비번)
  BANK_BUSAN_ID, BANK_BUSAN_PW     (부산은행 인터넷뱅킹 아이디/비번)
사용:
  - GitHub Actions의 bank-register 워크플로(workflow_dispatch)로 1회 실행.
  - 이미 등록돼 있으면 건너뜀(중복 등록 방지).
"""
import os, json
from easycodefpy import Codef, ServiceType
from easycodefpy.util import encrypt_rsa

ENV          = os.environ.get("CODEF_ENV", "demo").lower()
CLIENT_ID    = os.environ["CODEF_CLIENT_ID"]
CLIENT_SECRET= os.environ["CODEF_CLIENT_SECRET"]
PUBLIC_KEY   = os.environ["CODEF_PUBLIC_KEY"]
CONNECTED_ID = os.environ["CODEF_CONNECTED_ID"]
BIRTHDATE    = os.environ.get("CODEF_BIRTHDATE", "")

SVC = ServiceType.PRODUCT if ENV in ("api","prod","product") else ServiceType.DEMO

# 등록할 은행: (기관코드, 표시이름, ID시크릿, PW시크릿)
BANKS = [
    ("0081", "하나",  "BANK_HANA_ID",  "BANK_HANA_PW"),
    ("0032", "부산",  "BANK_BUSAN_ID", "BANK_BUSAN_PW"),
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
    """현재 connectedId에 등록된 기관코드 목록"""
    r = json.loads(codef.get_account_list(SVC, {"connectedId": CONNECTED_ID}))
    al = (r.get("data") or {}).get("accountList") or (r.get("data") or {}).get("accounts") or []
    return [a.get("organization") for a in al if isinstance(a, dict)]

def main():
    codef = make_codef()
    have = set(registered_orgs(codef))
    print("이미 등록된 기관:", sorted(have), flush=True)

    for org, name, id_key, pw_key in BANKS:
        if org in have:
            print(f"[{name}({org})] 이미 등록됨 — 건너뜀", flush=True)
            continue
        bid = os.environ.get(id_key, "").strip()
        bpw = os.environ.get(pw_key, "")
        if not bid or not bpw:
            print(f"[{name}({org})] 시크릿 {id_key}/{pw_key} 누락 — 건너뜀", flush=True)
            continue
        param = {"connectedId": CONNECTED_ID, "accountList": [{
            "countryCode": "KR",
            "businessType": "BK",
            "clientType":  "P",
            "organization": org,
            "loginType":   "1",          # 1=아이디/비밀번호
            "id":          bid,
            "password":    encrypt_rsa(bpw, PUBLIC_KEY),
            "birthDate":   BIRTHDATE,
        }]}
        r = json.loads(codef.add_account(SVC, param))
        result = r.get("result") or {}
        data   = r.get("data") or {}
        success = data.get("successList") or []
        fail    = data.get("failList") or data.get("errorList") or []
        print(f"[{name}({org})] code={result.get('code')} msg={result.get('message')}", flush=True)
        if success:
            print(f"  ✅ 등록 성공", flush=True)
        elif fail:
            for f in fail:
                print(f"  ❌ 실패: {json.dumps(f, ensure_ascii=False)}", flush=True)
        else:
            print(f"  ⚠️ 응답 확인 필요: {json.dumps(r, ensure_ascii=False)[:600]}", flush=True)

    print("\n최종 등록 기관:", sorted(set(registered_orgs(codef))), flush=True)

if __name__ == "__main__":
    main()
