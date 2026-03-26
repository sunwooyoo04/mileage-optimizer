import urllib.parse
from playwright.sync_api import sync_playwright, Page

BASE_URL = "https://underwood1.yonsei.ac.kr"
INIT_URL = f"{BASE_URL}/com/lgin/SsoCtr/initExtPageWork.do?link=handbList&locale=ko"

SMT_INPUT = {"1": "10", "여름": "11", "2": "20", "겨울": "21"}

# JS: 과목번호 입력창 중심 좌표 반환 (빈 값의 spellcheck 텍스트 입력)
_JS_COURSE_INPUT = """() => {
    const el = Array.from(document.querySelectorAll("input.cl-text"))
        .find(el => el.value === "" && el.getAttribute("spellcheck") === "true");
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return {x: b.x + b.width / 2, y: b.y + b.height / 2};
}"""

# JS: 가장 마지막에 보이는 '조회' 링크 중심 좌표 반환
_JS_SEARCH_BTN = """() => {
    let last = null;
    for (const el of document.querySelectorAll("a.cl-text-wrapper")) {
        const b = el.getBoundingClientRect();
        if (b.width > 0 && b.height > 0 && el.innerText.trim() === "\uC870\uD68C") last = el;
    }
    if (!last) return null;
    const b = last.getBoundingClientRect();
    return {x: b.x + b.width / 2, y: b.y + b.height / 2};
}"""


def parse_course_number(raw: str):
    parts = raw.strip().split("-")
    if len(parts) < 3:
        raise ValueError(f"올바른 형식이 아닙니다: {raw}  (예: 2026-1-HUM2038-01)")
    year = parts[0]
    smt = parts[1]
    code = parts[2]
    section = parts[3] if len(parts) >= 4 else "01"
    smt_div = SMT_INPUT.get(smt)
    if smt_div is None:
        raise ValueError(f"알 수 없는 학기: {smt}  (1 / 2 / 여름 / 겨울 중 하나)")
    return year, smt_div, code, section


def api_post(page: Page, endpoint: str, params: dict, menu_id: str, pgm_id: str) -> dict:
    form = {"_menuId": menu_id, "_menuNm": "", "_pgmId": pgm_id}
    form.update(params)
    resp = page.request.post(
        BASE_URL + endpoint,
        form=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return resp.json()


def scrape(raw: str) -> dict:
    year, smt_div, code, section = parse_course_number(raw)

    course_info = {}
    mileage_summary = {}
    mileage_ranks = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1200})

        handb_rows = []
        menu_id = ""
        pgm_id = ""

        def on_request(request):
            nonlocal menu_id, pgm_id
            if request.method == "POST" and "sles" in request.url and not menu_id:
                pd = request.post_data or ""
                for part in pd.split("&"):
                    if part.startswith("_menuId="):
                        menu_id = urllib.parse.unquote(part[len("_menuId="):])
                    elif part.startswith("_pgmId="):
                        pgm_id = urllib.parse.unquote(part[len("_pgmId="):])

        def on_handb(response):
            if "findAtnlcHandbList" in response.url:
                try:
                    handb_rows.extend(response.json().get("dsSles251", []))
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_handb)

        page.goto(INIT_URL, wait_until="networkidle", timeout=30000)

        # 과목번호 입력창을 CSS/HTML로 동적 탐색
        input_pos = page.evaluate(_JS_COURSE_INPUT)
        if not input_pos:
            raise RuntimeError("과목번호 입력창을 찾을 수 없습니다.")
        page.mouse.click(input_pos["x"], input_pos["y"])
        page.wait_for_timeout(300)
        page.keyboard.press("Control+A")
        page.keyboard.type(code, delay=50)
        page.wait_for_timeout(300)

        # 조회 버튼을 CSS/HTML로 동적 탐색
        search_pos = page.evaluate(_JS_SEARCH_BTN)
        if not search_pos:
            raise RuntimeError("조회 버튼을 찾을 수 없습니다.")
        page.mouse.click(search_pos["x"], search_pos["y"])
        page.wait_for_timeout(3000)

        for r in handb_rows:
            if r.get("syy") == year and r.get("smtDivCd") == smt_div and r.get("corseDvclsNo") == section:
                course_info = r
                break
        if not course_info and handb_rows:
            course_info = handb_rows[0]

        def post(endpoint, params):
            return api_post(page, endpoint, params, menu_id, pgm_id)

        smt_list_data = post("/sch/sles/SlessyCtr/findMlgSyySmtDivCdList.do", {
            "@d1#syy": "", "@d1#smtDivCd": "",
            "@d1#sysinstDivCd": "H1", "@d1#subjtnb": code,
            "@d1#corseDvclsNo": section, "@d1#prctsCorseDvclsNo": "00",
            "@d1#syySmtDivCd": "",
            "@d#": "@d1#", "@d1#": "dmCond", "@d1#tp": "dm",
        })
        available_smts = smt_list_data.get("dsSyySmtDivCd", [])

        for smt_info in available_smts:
            s_year = smt_info["syy"]
            s_smt = smt_info["smtDivCd"]
            s_code = smt_info["code"]
            s_name = smt_info.get("fullNm", f"{s_year}-{s_smt}")

            summary_data = post("/sch/sles/SlessyCtr/findMlgAppcsResltList.do", {
                "@d1#syy": s_year, "@d1#smtDivCd": s_smt,
                "@d1#sysinstDivCd": "H1", "@d1#subjtnb": code,
                "@d1#corseDvclsNo": section, "@d1#prctsCorseDvclsNo": "00",
                "@d1#syySmtDivCd": s_code,
                "@d#": "@d1#", "@d1#": "dmCond", "@d1#tp": "dm",
            })
            rows_s = summary_data.get("dsSles251", [])
            if rows_s:
                mileage_summary[s_name] = rows_s[0]

            rank_data = post("/sch/sles/SlessyCtr/findMlgRankResltList.do", {
                "@d1#sysinstDivCd": "H1", "@d1#syy": s_year, "@d1#smtDivCd": s_smt,
                "@d1#stuno": "", "@d1#subjtnb": code, "@d1#appcsSchdlCd": "",
                "@d1#corseDvclsNo": section, "@d1#prctsCorseDvclsNo": "00",
                "@d#": "@d1#", "@d1#": "dmCond", "@d1#tp": "dm",
            })
            mileage_ranks[s_name] = rank_data.get("dsSles440", [])

        browser.close()

    if not mileage_summary:
        raise ValueError("마일리지 데이터가 없습니다. 학정번호를 확인해주세요.")

    semesters = []
    for s_name, summary in mileage_summary.items():
        ranks = mileage_ranks.get(s_name, [])
        semesters.append({
            "name": s_name,
            "summary": {
                "atnlcPercpCnt": summary.get("atnlcPercpCnt", 0),
                "cnt": summary.get("cnt", 0),
                "minMlg": summary.get("minMlg", 0),
                "avgMlg": summary.get("avgMlg", 0),
                "maxMlg": summary.get("maxMlg", 36),
                "mjrprPercpCnt": summary.get("mjrprPercpCnt", "0(N)"),
            },
            "ranks": [
                {
                    "mlgVal": r.get("mlgVal"),
                    "hy": r.get("hy"),
                    "mjsbjYn": r.get("mjsbjYn", "N"),
                    "dsstdYn": r.get("dsstdYn", "N"),
                    "aplySubjcCnt": r.get("aplySubjcCnt"),
                    "grdtnAplyYn": r.get("grdtnAplyYn", "N"),
                    "fratlcYn": r.get("fratlcYn", "N"),
                    "ttCmpsjCdtRto": r.get("ttCmpsjCdtRto"),
                    "jstbfSmtCmpsjCdtRto": r.get("jstbfSmtCmpsjCdtRto"),
                    "mlgAppcsPrcesDivNm": r.get("mlgAppcsPrcesDivNm", "N"),
                }
                for r in ranks
            ]
        })

    return {
        "course_info": {
            "subjtNb": course_info.get("subjtnb", code),
            "subjtNm": course_info.get("subjtNm", ""),
            "corseDvclsNo": course_info.get("corseDvclsNo", section),
            "cgprfNm": course_info.get("cgprfNm", ""),
            "cdt": course_info.get("cdt", ""),
        },
        "semesters": semesters,
    }
