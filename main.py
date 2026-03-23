"""
연세대학교 마일리지 수강신청 분석기
사용법: python main.py
"""

import os
import sys
import json
import requests
import webbrowser
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "api"))
from scraper import scrape

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

API_KEY = os.environ.get("OPENROUTER_API_KEY", "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")
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openai/gpt-4o-mini"

SMT_NAME = {"10": "1학기", "11": "여름학기", "20": "2학기", "21": "겨울학기"}


# ==================== 사용자 입력 ====================

def collect_user_profile():
    print("\n[내 정보 입력]")
    hy = input("  학년 (1/2/3/4): ").strip() or "3"
    dsstd = input("  특수교육대상자 여부 (Y/N) [N]: ").strip().upper() or "N"
    grdt = input("  졸업신청 여부 (Y/N) [N]: ").strip().upper() or "N"
    tt_rto = input("  총 이수비율 (0.0~1.0) [0.75]: ").strip() or "0.75"
    jstbf_rto = input("  직전학기 이수비율 (0.0~1.0) [1.0]: ").strip() or "1.0"
    apply_cnt = input("  총 신청 과목수 [6]: ").strip() or "6"
    budget = input("  총 보유 마일리지 [72]: ").strip() or "72"
    return {
        "hy": hy,
        "dsstdYn": dsstd,
        "grdtnAplyYn": grdt,
        "ttCmpsjCdtRto": float(tt_rto),
        "jstbfSmtCmpsjCdtRto": float(jstbf_rto),
        "aplySubjcCnt": int(apply_cnt),
        "budget": int(budget),
    }


def collect_courses():
    courses = []
    print("\n[과목 입력] 최대 6개, 빈 입력 시 종료")
    for i in range(6):
        raw = input(f"  과목 {i+1} 학정번호 (예: 2026-1-HUM2038-01, 빈칸=종료): ").strip()
        if not raw:
            break
        priority = input(f"    우선순위 (1~6) [{i+1}]: ").strip() or str(i + 1)
        is_major = input(f"    전공자 여부 (Y/N) [N]: ").strip().upper() or "N"
        courses.append({"raw": raw, "priority": int(priority), "isMajor": is_major})
    return courses


# ==================== 확률 계산 ====================

def priority_key(r, user):
    """낮을수록 우선순위 높음"""
    flag = lambda v: 0 if v == "Y" else 1
    return [
        -(r.get("mlgVal") or 0),
        flag(r.get("dsstdYn", "N")),
        flag((r.get("mjsbjYn") or "N")[0]),
        -min(r.get("aplySubjcCnt") or 0, 6),
        flag(r.get("grdtnAplyYn", "N")),
        flag(r.get("fratlcYn", "N")),
        -min(r.get("ttCmpsjCdtRto") or 0, 1.0),
        -min(r.get("jstbfSmtCmpsjCdtRto") or 0, 1.0),
    ]


def calc_acceptance(course_data, mileage, is_major, user):
    import re

    profile_key = priority_key({
        "mlgVal": mileage,
        "mjsbjYn": is_major,
        "dsstdYn": user["dsstdYn"],
        "aplySubjcCnt": user["aplySubjcCnt"],
        "grdtnAplyYn": user["grdtnAplyYn"],
        "fratlcYn": "N",
        "ttCmpsjCdtRto": user["ttCmpsjCdtRto"],
        "jstbfSmtCmpsjCdtRto": user["jstbfSmtCmpsjCdtRto"],
    }, user)

    admitted = 0
    semesters = course_data["semesters"]

    for sem in semesters:
        # 수강여부Y인 실제 합격자만 표본으로 사용
        y_ranks = [r for r in sem["ranks"] if r.get("mlgAppcsPrcesDivNm") == "Y"]
        if not y_ranks:
            continue

        capacity = sem["summary"]["atnlcPercpCnt"]
        mjr_str = sem["summary"].get("mjrprPercpCnt", "0(N)")
        m = re.match(r"(\d+)\(Y\)", str(mjr_str))
        major_quota = int(m.group(1)) if m else 0

        if major_quota > 0:
            if is_major == "Y":
                y_major = sorted(
                    [r for r in y_ranks if (r.get("mjsbjYn") or "N")[0] == "Y"],
                    key=lambda r: priority_key(r, user)
                )
                if len(y_major) < major_quota:
                    # 전공 쿼터가 다 안 찼으면 합격
                    admitted += 1
                else:
                    # 전공 쿼터 내 가장 낮은 합격자(컷오프)보다 우선순위가 높으면 합격
                    cutoff = priority_key(y_major[major_quota - 1], user)
                    if profile_key <= cutoff:
                        admitted += 1
            else:
                y_major = [r for r in y_ranks if (r.get("mjsbjYn") or "N")[0] == "Y"]
                y_general = sorted(
                    [r for r in y_ranks if (r.get("mjsbjYn") or "N")[0] != "Y"],
                    key=lambda r: priority_key(r, user)
                )
                actual_major = min(len(y_major), major_quota)
                gen_cap = capacity - actual_major
                if len(y_general) < gen_cap:
                    admitted += 1
                else:
                    cutoff = priority_key(y_general[gen_cap - 1], user)
                    if profile_key <= cutoff:
                        admitted += 1
        else:
            y_sorted = sorted(y_ranks, key=lambda r: priority_key(r, user))
            if len(y_sorted) < capacity:
                admitted += 1
            else:
                cutoff = priority_key(y_sorted[capacity - 1], user)
                if profile_key <= cutoff:
                    admitted += 1

    return admitted / len(semesters) if semesters else 0


def build_curve(course_data, is_major, user):
    curve = {}
    max_mlg = min(36, max((s["summary"].get("maxMlg") or 36) for s in course_data["semesters"]))
    for m in range(1, 37):
        curve[m] = calc_acceptance(course_data, m, is_major, user)
    return curve, max_mlg


def optimize(course_list, budget):
    n = len(course_list)
    if n == 0:
        return []
    weights = [7 - c["priority"] for c in course_list]
    max_per = [c["maxMlg"] for c in course_list]
    alloc = [1] * n
    remaining = budget - n

    while remaining > 0:
        best_gain, best_idx = -float("inf"), -1
        for i in range(n):
            if alloc[i] >= max_per[i]:
                continue
            gain = weights[i] * ((course_list[i]["curve"].get(alloc[i] + 1) or 0) - (course_list[i]["curve"].get(alloc[i]) or 0))
            if gain > best_gain:
                best_gain, best_idx = gain, i
        if best_idx == -1:
            break
        alloc[best_idx] += 1
        remaining -= 1

    return alloc


def find_min_safe(curve, threshold=0.5):
    for m in range(1, 37):
        if (curve.get(m) or 0) >= threshold:
            return m
    return 36


# ==================== LLM ====================

def call_llm(course_results, user):
    summary = []
    for c in course_results:
        summary.append({
            "priority": c["priority"],
            "courseName": c["courseName"],
            "subjtNb": c["subjtNb"],
            "isMajor": c["isMajor"],
            "recommendedMileage": c["recommendedMileage"],
            "acceptanceProb": round(c["acceptanceProb"] * 100),
            "minSafe": c["minSafe"],
            "probAt10": round((c["curve"].get(10) or 0) * 100),
            "probAt15": round((c["curve"].get(15) or 0) * 100),
            "probAt20": round((c["curve"].get(20) or 0) * 100),
            "probAt25": round((c["curve"].get(25) or 0) * 100),
            "probAt30": round((c["curve"].get(30) or 0) * 100),
            "historicalSemesters": len(c["semesters"]),
        })

    prompt = f"""당신은 연세대학교 수강신청 마일리지 전략 전문가입니다.

학생 정보:
- 학년: {user['hy']}학년
- 특수교육대상자: {user['dsstdYn']}
- 졸업신청: {user['grdtnAplyYn']}
- 총 이수비율: {user['ttCmpsjCdtRto']}
- 직전학기 이수비율: {user['jstbfSmtCmpsjCdtRto']}
- 총 신청과목수: {user['aplySubjcCnt']}
- 총 마일리지: {user['budget']}점

과목별 분석 결과 (수강여부Y인 실제 합격자 데이터 기반):
{json.dumps(summary, ensure_ascii=False, indent=2)}

다음 JSON 형식으로만 응답하세요:
{{
  "overall_strategy": "전체 전략 요약 (3~5문장)",
  "course_advice": [
    {{"courseName": "과목명", "advice": "이 과목에 대한 조언 (2~3문장)"}}
  ],
  "warnings": ["경고사항1", "경고사항2"],
  "conservative_alloc": [
    {{"courseName": "과목명", "mileage": 숫자, "reason": "이유"}}
  ]
}}"""

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "HTTP-Referer": "http://localhost",
        "X-OpenRouter-Title": "Mileage Optimizer",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "연세대학교 마일리지 수강신청 전략 전문가입니다. JSON만 반환하세요."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1500,
    }
    resp = requests.post(ENDPOINT, headers=headers, data=json.dumps(payload))
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    # JSON 추출
    import re
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    return None


# ==================== HTML 생성 ====================

def render_html(course_results, user, ai_result):
    def prob_color(p):
        if p >= 70: return "#27ae60"
        if p >= 40: return "#f39c12"
        return "#e74c3c"

    def prob_badge(p):
        if p >= 70: return "badge-low"
        if p >= 40: return "badge-medium"
        return "badge-high"

    def prob_label(p):
        if p >= 70: return "안전"
        if p >= 40: return "중간"
        return "위험"

    # 결과 테이블 rows
    table_rows = ""
    for c in course_results:
        p = round(c["acceptanceProb"] * 100)
        table_rows += f"""
        <tr>
          <td>{c['priority']}순위</td>
          <td><strong>{c['courseName']}</strong><br><small style="color:#666">{c['subjtNb']} | {c['professor']} | {'전공' if c['isMajor']=='Y' else '비전공'}</small></td>
          <td style="text-align:center"><strong style="font-size:1.1em;color:#1a3a6b">{c['recommendedMileage']}</strong>점</td>
          <td>
            <div style="display:flex;align-items:center;gap:8px">
              <div style="background:#e9ecef;border-radius:4px;height:8px;width:80px;overflow:hidden">
                <div style="background:{prob_color(p)};height:100%;width:{p}%;border-radius:4px"></div>
              </div>
              <span class="badge {prob_badge(p)}">{p}% {prob_label(p)}</span>
            </div>
          </td>
          <td style="text-align:center">{c['minSafe']}점</td>
        </tr>"""

    # 차트 bars
    charts = ""
    for c in course_results:
        bars = ""
        max_v = max((c["curve"].get(m) or 0) for m in range(1, 37)) or 1
        for m in range(1, 37):
            v = c["curve"].get(m) or 0
            h = max(2, round(v / max_v * 80))
            color = prob_color(round(v * 100))
            bars += f'<div class="chart-bar-wrap"><div class="chart-bar" style="height:{h}px;background:{color}" data-tip="{m}점:{round(v*100)}%"></div><div class="chart-bar-label">{m if m%5==0 else ""}</div></div>'
        charts += f"""
        <div class="chart-course">
          <h4>{c['courseName']} ({c['subjtNb']})</h4>
          <div class="chart-bars">{bars}</div>
          <div class="chart-legend">
            <span><span class="legend-dot" style="background:#27ae60"></span> 70%+ 안전</span>
            <span><span class="legend-dot" style="background:#f39c12"></span> 40~70% 중간</span>
            <span><span class="legend-dot" style="background:#e74c3c"></span> 40% 미만 위험</span>
          </div>
        </div>"""

    # AI 결과
    ai_html = ""
    if ai_result:
        ai_html += f"""
        <div class="section">
          <div class="section-title"><span class="num">6</span> AI 전략 요약</div>
          <div class="ai-box">{ai_result.get('overall_strategy','').replace(chr(10),'<br>')}</div>
        </div>"""

        if ai_result.get("course_advice"):
            cards = "".join(f'<div class="course-analysis-item"><h4>{a["courseName"]}</h4><p>{a["advice"]}</p></div>' for a in ai_result["course_advice"])
            ai_html += f"""
        <div class="section">
          <div class="section-title"><span class="num">7</span> 과목별 AI 분석</div>
          <div class="course-analysis-grid">{cards}</div>
        </div>"""

        if ai_result.get("warnings"):
            warnings = "".join(f"<li>{w}</li>" for w in ai_result["warnings"])
            ai_html += f"""
        <div class="section">
          <div class="section-title"><span class="num">8</span> 경고 사항</div>
          <ul class="warning-list">{warnings}</ul>
        </div>"""

        if ai_result.get("conservative_alloc"):
            pills = "".join(f'<div class="alt-course-pill">{a["courseName"]} {a["mileage"]}점</div>' for a in ai_result["conservative_alloc"])
            reasons = "".join(f'<li style="font-size:0.85rem;color:#555;margin-bottom:4px">{a["courseName"]}: {a["reason"]}</li>' for a in ai_result["conservative_alloc"])
            ai_html += f"""
        <div class="section">
          <div class="section-title"><span class="num">9</span> 보수적 대안 배분</div>
          <div class="alt-box">
            <div class="alt-course-list" style="margin-bottom:12px">{pills}</div>
            <ul style="list-style:none">{reasons}</ul>
          </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>마일리지 분석 결과</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--navy:#1a3a6b;--navy-light:#2a5298;--accent:#4a90d9;--accent-light:#e8f2fc;--green:#27ae60;--yellow:#f39c12;--red:#e74c3c;--gray-200:#e9ecef;--gray-700:#495057;--gray-900:#212529;--white:#ffffff;--shadow:0 2px 12px rgba(26,58,107,.10);--radius:12px;--radius-sm:8px}}
body{{font-family:'Apple SD Gothic Neo','Malgun Gothic','맑은 고딕',sans-serif;background:linear-gradient(135deg,#f0f4fb,#e8f0fc);color:var(--gray-900);min-height:100vh;padding-bottom:60px}}
.header{{background:linear-gradient(135deg,#0f2244,#1a3a6b,#2a5298);color:#fff;padding:32px 0 24px;text-align:center;box-shadow:0 4px 24px rgba(26,58,107,.25)}}
.header h1{{font-size:1.8rem;font-weight:700}}
.header p{{margin-top:8px;font-size:0.9rem;opacity:.82}}
.badge-row{{display:flex;gap:12px;justify-content:center;margin-top:14px;flex-wrap:wrap}}
.info-badge{{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.3);border-radius:20px;padding:4px 14px;font-size:0.78rem}}
.container{{max-width:960px;margin:0 auto;padding:0 20px}}
.section{{background:#fff;border-radius:var(--radius);box-shadow:var(--shadow);padding:28px 32px;margin-top:24px}}
.section-title{{font-size:1.05rem;font-weight:700;color:var(--navy);margin-bottom:20px;padding-bottom:12px;border-bottom:2px solid var(--accent-light);display:flex;align-items:center;gap:10px}}
.num{{background:var(--navy);color:#fff;width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.8rem;font-weight:700;flex-shrink:0}}
.result-table{{width:100%;border-collapse:collapse;font-size:.9rem}}
.result-table th{{background:var(--navy);color:#fff;padding:11px 14px;text-align:left;font-weight:600;font-size:.82rem}}
.result-table th:first-child{{border-radius:8px 0 0 0}}.result-table th:last-child{{border-radius:0 8px 0 0}}
.result-table td{{padding:11px 14px;border-bottom:1px solid #e9ecef;vertical-align:middle}}
.result-table tr:last-child td{{border-bottom:none}}
.result-table tr:hover td{{background:var(--accent-light)}}
.badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.77rem;font-weight:700}}
.badge-low{{background:#d4edda;color:#155724}}
.badge-medium{{background:#fff3cd;color:#856404}}
.badge-high{{background:#f8d7da;color:#721c24}}
.ai-box{{background:linear-gradient(135deg,var(--accent-light),#f0f7ff);border:1px solid rgba(74,144,217,.25);border-radius:var(--radius);padding:20px 24px;font-size:.92rem;line-height:1.75}}
.course-analysis-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-top:8px}}
.course-analysis-item{{background:#fff;border:1px solid #e9ecef;border-radius:var(--radius-sm);padding:16px}}
.course-analysis-item h4{{font-size:.88rem;font-weight:700;color:var(--navy);margin-bottom:8px}}
.course-analysis-item p{{font-size:.83rem;line-height:1.65;color:var(--gray-700)}}
.warning-list{{list-style:none;margin-top:8px}}
.warning-list li{{padding:10px 14px;background:#fff8e1;border-left:4px solid var(--yellow);border-radius:0 6px 6px 0;font-size:.87rem;margin-bottom:8px;color:#5a3e00}}
.alt-box{{background:#f8fff8;border:1px solid #c3e6cb;border-radius:var(--radius);padding:18px 22px}}
.alt-course-list{{display:flex;flex-wrap:wrap;gap:10px}}
.alt-course-pill{{background:#fff;border:1px solid var(--green);border-radius:20px;padding:6px 14px;font-size:.83rem;color:var(--green);font-weight:600}}
.chart-course{{margin-bottom:24px}}
.chart-course h4{{font-size:.88rem;font-weight:700;color:var(--navy);margin-bottom:10px}}
.chart-bars{{display:flex;align-items:flex-end;gap:2px;height:90px}}
.chart-bar-wrap{{display:flex;flex-direction:column;align-items:center;flex:1;height:100%;justify-content:flex-end}}
.chart-bar{{width:100%;border-radius:3px 3px 0 0;min-height:2px;cursor:pointer;position:relative}}
.chart-bar:hover::after{{content:attr(data-tip);position:absolute;bottom:105%;left:50%;transform:translateX(-50%);background:#0f2244;color:#fff;padding:4px 8px;border-radius:4px;font-size:.72rem;white-space:nowrap;z-index:10;pointer-events:none}}
.chart-bar-label{{font-size:.6rem;color:#adb5bd;margin-top:3px}}
.chart-legend{{display:flex;gap:14px;margin-top:8px;font-size:.78rem;color:var(--gray-700);align-items:center}}
.legend-dot{{width:10px;height:10px;border-radius:50%;display:inline-block}}
</style>
</head>
<body>
<div class="header">
  <div class="container">
    <h1>마일리지 분석 결과</h1>
    <p>연세대학교 수강신청 마일리지 최적화 분석기</p>
    <div class="badge-row">
      <span class="info-badge">{user['hy']}학년</span>
      <span class="info-badge">마일리지 {user['budget']}점</span>
      <span class="info-badge">신청과목 {user['aplySubjcCnt']}개</span>
      <span class="info-badge">이수비율 {round(user['ttCmpsjCdtRto']*100)}%</span>
      {'<span class="info-badge">특수교육</span>' if user['dsstdYn']=='Y' else ''}
      {'<span class="info-badge">졸업예정</span>' if user['grdtnAplyYn']=='Y' else ''}
    </div>
  </div>
</div>

<div class="container">
  <div class="section">
    <div class="section-title"><span class="num">5</span> 최적 마일리지 배분 결과</div>
    <div style="overflow-x:auto">
      <table class="result-table">
        <thead>
          <tr>
            <th>우선순위</th>
            <th>과목명</th>
            <th>추천 마일리지</th>
            <th>합격확률</th>
            <th>최소 안전 마일리지</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </div>

  {ai_html}

  <div class="section">
    <div class="section-title"><span class="num">{'10' if ai_result else '6'}</span> 확률 시각화</div>
    {charts}
  </div>
</div>
</body>
</html>"""
    return html


# ==================== MAIN ====================

def main():
    print("=" * 55)
    print("  연세대학교 마일리지 수강신청 분석기")
    print("=" * 55)

    user = collect_user_profile()
    courses_input = collect_courses()

    if not courses_input:
        print("과목을 입력해야 합니다.")
        sys.exit(1)

    # 스크래핑
    course_results = []
    for ci in courses_input:
        print(f"\n[*] 스크래핑: {ci['raw']}")
        try:
            data = scrape(ci["raw"])
        except Exception as e:
            print(f"[!] 실패: {e}")
            continue

        print(f"[*] 확률 계산 중: {data['course_info']['subjtNm']}")
        curve, max_mlg = build_curve(data, ci["isMajor"], user)

        course_results.append({
            "priority": ci["priority"],
            "isMajor": ci["isMajor"],
            "courseName": data["course_info"]["subjtNm"],
            "subjtNb": data["course_info"]["subjtNb"],
            "professor": data["course_info"]["cgprfNm"],
            "curve": curve,
            "maxMlg": max_mlg,
            "semesters": data["semesters"],
        })

    if not course_results:
        print("데이터를 가져온 과목이 없습니다.")
        sys.exit(1)

    course_results.sort(key=lambda c: c["priority"])

    # 최적화
    print("\n[*] 마일리지 최적 배분 계산 중...")
    alloc = optimize(course_results, user["budget"])
    for i, c in enumerate(course_results):
        c["recommendedMileage"] = alloc[i]
        c["acceptanceProb"] = c["curve"].get(alloc[i]) or 0
        c["minSafe"] = find_min_safe(c["curve"])

    # LLM
    ai_result = None
    use_ai = input("\nAI 전략 분석을 실행할까요? (Y/N) [Y]: ").strip().upper() or "Y"
    if use_ai == "Y":
        print("[*] AI 전략 생성 중...")
        try:
            ai_result = call_llm(course_results, user)
        except Exception as e:
            print(f"[!] AI 호출 실패: {e}")

    # HTML 생성
    print("[*] HTML 생성 중...")
    html = render_html(course_results, user, ai_result)

    out_path = Path(__file__).parent / "result.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"[+] 저장 완료: {out_path}")

    webbrowser.open(str(out_path))
    print("[+] 브라우저에서 결과를 확인하세요.")


if __name__ == "__main__":
    main()
