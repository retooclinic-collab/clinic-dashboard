# -*- coding: utf-8 -*-
"""
IBK 기업카드(BC카드 0305) CODEF 재시도 스크립트 (1회용, 로컬 실행)
- 배경: CF-12701은 CODEF가 2026-06-23 패치 완료. 현재는 "ID/PW 불일치" 오류만 남음.
- 이 스크립트: 0305 계정을 (이미 있으면) 비밀번호 갱신 / (없으면) 신규 등록 → 최근 7일 승인내역 테스트 조회.

실행: GitHub Actions 'register-card-0305' 워크플로 (workflow_dispatch 수동 실행)
필요 시크릿: 기존 CODEF_* 5종 재사용 + 신규 BC_ID / BC_PW (bccard.com 웹 로그인 계정 — 2026-07-16 로그인 확인됨)
※ 데모키 만료(2026-07-22) 전 실행 권장.
"""
import os, json, datetime
from easycodefpy import Codef, ServiceType
from easycodefpy.util import encrypt_rsa

ENV = "api"  # 정식 강제
CLIENT_ID     = os.environ["CODEF_CLIENT_ID"]
CLIENT_SECRET = os.environ["CODEF_CLIENT_SECRET"]
PUBLIC_KEY    = os.environ["CODEF_PUBLIC_KEY"]
CONNECTED_ID  = os.environ["CODEF_CONNECTED_ID"]
BIRTHDATE     = os.environ.get("CODEF_BIRTHDATE", "")
BC_ID         = os.environ["BC_ID"]
BC_PW         = os.environ["BC_PW"]

ORG = "0305"  # BC카드
SVC = ServiceType.PRODUCT if ENV in ("api", "prod", "product") else ServiceType.DEMO

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
    al = (r.get("data") or {}).get("accountList") or []
    return [(a.get("organization"), a.get("businessType")) for a in al if isinstance(a, dict)]

def account_item():
    return {
        "countryCode": "KR",
        "businessType": "CD",
        "clientType": "P",
        "organization": ORG,
        "loginType": "1",                       # 아이디 로그인
        "id": BC_ID,
        "password": encrypt_rsa(BC_PW, PUBLIC_KEY),
        "birthDate": BIRTHDATE,
    }

def main():
    codef = make_codef()
    orgs = registered_orgs(codef)
    print("등록된 기관:", orgs, flush=True)
    has_0305 = any(o == ORG and b == "CD" for o, b in orgs)

    param = {"connectedId": CONNECTED_ID, "accountList": [account_item()]}
    if has_0305:
        print("0305 이미 등록됨 → 계정정보 갱신(update)", flush=True)
        r = json.loads(codef.update_account(SVC, param))
    else:
        print("0305 미등록 → 신규 등록(add)", flush=True)
        r = json.loads(codef.add_account(SVC, param))
    res = r.get("result") or {}
    print(f"등록/갱신 결과: code={res.get('code')} msg={res.get('message')}", flush=True)
    data = r.get("data") or {}
    for f in (data.get("errorList") or data.get("failList") or []):
        print("  실패상세:", json.dumps(f, ensure_ascii=False), flush=True)
    if res.get("code") not in ("CF-00000",):
        print("→ 등록/갱신 실패. paybooc 로그인 가능 여부부터 다시 확인하세요.", flush=True)
        return

    # 최근 7일 승인내역 테스트
    today = datetime.date.today()
    start = today - datetime.timedelta(days=7)
    q = {
        "connectedId": CONNECTED_ID,
        "organization": ORG,
        "birthDate": BIRTHDATE,
        "startDate": start.strftime("%Y%m%d"),
        "endDate": today.strftime("%Y%m%d"),
        "orderBy": "0",
        "inquiryType": "1",
        "memberStoreInfoType": "1",
    }
    r2 = json.loads(codef.request_product("/v1/kr/card/p/account/approval-list", SVC, q))
    res2 = r2.get("result") or {}
    rows = (r2.get("data") or [])
    if isinstance(rows, dict):
        rows = rows.get("resApprovalList") or []
    print(f"승인내역 테스트: code={res2.get('code')} msg={res2.get('message')} 건수={len(rows)}", flush=True)
    if res2.get("code") == "CF-00000":
        print("✅ 성공! collector.py에 0305 추가하고 백필 실행하면 됩니다.", flush=True)
    else:
        print("extraMessage:", res2.get("extraMessage"), flush=True)
        print("transactionId:", res2.get("transactionId"), flush=True)

if __name__ == "__main__":
    main()