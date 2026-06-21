/* ============================================================
   청담리투 파싱규칙 추가분 (2026-06-22) — 후처리 패치 (자동연결)
   ------------------------------------------------------------
   적용: staff-portal.html 의 </body> 직전에 아래 한 줄만 추가
       <script src="./parse-rules-2026-06-patch.js"></script>
   (메인 스크립트 뒤에 로드되어야 함)

   동작: 파싱 결과 공유배열 window._importRows 에 4개 규칙을 후처리로 적용.
         _applyRegexFallbackAndRenderTable / confirmImport 진입 시 자동 실행.
         행마다 _xr2026 플래그로 멱등 처리(중복적용 방지).

   규칙:
     1) 환불후재결제        -> 단일 항목, 금액 그대로
     2) 지정비/원장님지정비용 -> 별도 라인 X, 시술항목에 비례배분
     3) VIP 회원권 신규구매   -> VIP회원권(0원) 추가 + 할인 정수 비례배분
     4) 미라젯 동반 쥬베룩    -> 쥬베룩볼륨 qty1
   ============================================================ */

(function (global) {
  'use strict';

  var round = Math.round;
  var r2 = function (n) { return Math.round(n * 100) / 100; };
  var SKIP = ['VIP회원권', 'VIP할인혜택', '적립금', '적립금차감', '예약금제외'];

  function T(item, price, qty) {
    return { item: item, price: price, qty: qty || 1, fromText: true };
  }
  function pool(items) {
    return items.filter(function (t) { return t.price > 0 && SKIP.indexOf(t.item) === -1; });
  }
  function distributeIntegerDeduction(items, D) {
    var p = pool(items);
    if (!p.length || !D) return;
    var base = p.reduce(function (a, t) { return a + t.price; }, 0);
    var largest = p[0];
    p.forEach(function (t) { if (t.price > largest.price) largest = t; });
    var used = 0;
    p.forEach(function (t) {
      if (t !== largest) { var cut = round(D * t.price / base); t.price = r2(t.price - cut); used += cut; }
    });
    largest.price = r2(largest.price - (D - used));
  }
  function distributeAddition(items, A) {
    var p = pool(items);
    if (!p.length || !A) return;
    var base = p.reduce(function (a, t) { return a + t.price; }, 0);
    var used = 0;
    for (var i = 0; i < p.length - 1; i++) {
      var addv = r2(A * p[i].price / base);
      p[i].price = r2(p[i].price + addv);
      used += addv;
    }
    p[p.length - 1].price = r2(p[p.length - 1].price + (A - used));
  }
  function reconcileDeductionLine(row) {
    var ded = row.treatments.find(function (t) { return t.item === '적립금차감'; });
    if (!ded) return;
    var others = row.treatments.reduce(function (a, t) { return t === ded ? a : a + t.price; }, 0);
    if (Math.abs(row.amount - 0) < 0.001) ded.price = r2(-others);
  }
  function isRowBalanced(row) {
    var s = row.treatments.reduce(function (a, t) { return a + t.price; }, 0);
    return Math.abs(r2(s) - row.amount) < 0.001;
  }

  function applyExtraParseRules2026(row) {
    if (!row || !row.paymentText) return row;
    if (row._xr2026) return row;
    row.treatments = row.treatments || [];
    var txt = row.paymentText;

    if (/환불\s*후\s*재결제/.test(txt)) {
      row.treatments = [T('환불후재결제', row.amount, 1)];
      row._xr2026 = true;
      return row;
    }
    if (row.treatments.some(function (t) { return /미라젯/.test(t.item); })) {
      row.treatments.forEach(function (t) {
        if (t.item === '쥬베룩' || t.item === '쥬베룩볼륨') { t.item = '쥬베룩볼륨'; t.qty = 1; }
      });
    }
    var feeLine = row.treatments.find(function (t) { return /지정비|지정비용/.test(t.item); });
    if (feeLine) {
      var feeAmt = Math.abs(feeLine.price);
      row.treatments = row.treatments.filter(function (t) { return t !== feeLine; });
      distributeAddition(row.treatments, feeAmt);
      reconcileDeductionLine(row);
    } else {
      var m = txt.match(/지정비[용]?\s*\d*%?\s*([\d.]+)/);
      if (m) {
        var amt = parseFloat(m[1]);
        var hasDeduction = row.treatments.some(function (t) { return t.item === '적립금차감'; });
        if (amt && (hasDeduction || !isRowBalanced(row))) {
          distributeAddition(row.treatments, amt);
          reconcileDeductionLine(row);
        }
      }
    }
    if (/VIP\s*(500|700|1000|1500)/i.test(txt)) {
      var discLine = row.treatments.find(function (t) { return t.item === 'VIP할인혜택'; });
      if (discLine) {
        var D = Math.abs(discLine.price);
        row.treatments = row.treatments.filter(function (t) { return t !== discLine; });
        distributeIntegerDeduction(row.treatments, D);
      }
      if (!row.treatments.some(function (t) { return t.item === 'VIP회원권'; })) {
        row.treatments.push(T('VIP회원권', 0, 1));
      }
    }
    row._xr2026 = true;
    return row;
  }

  global.applyExtraParseRules2026 = applyExtraParseRules2026;

  function postProcessImportRows() {
    if (Array.isArray(global._importRows)) {
      global._importRows = global._importRows.map(applyExtraParseRules2026);
    }
  }
  if (!global.__xr2026Wired) {
    global.__xr2026Wired = true;
    ['_applyRegexFallbackAndRenderTable', 'confirmImport'].forEach(function (fn) {
      var orig = global[fn];
      if (typeof orig !== 'function') return;
      global[fn] = function () {
        try { postProcessImportRows(); } catch (e) { console.warn('xr2026', e); }
        return orig.apply(this, arguments);
      };
    });
  }
})(typeof window !== 'undefined' ? window : this);
