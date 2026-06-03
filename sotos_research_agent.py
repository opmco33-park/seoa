#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sotos / NSD1 연구 추적 에이전트  (v2.0)
======================================
딸의 유전질환(Sotos syndrome, 원인 유전자 NSD1) 관련 논문·임상시험을 자동 수집하여
(1) 모든 항목을 쉬운 한국어로 분석하고, (2) 흩어진 발견을 주제별로 묶은 '종합 분석'을
만들어, (3) 항상 최신 상태인 '살아있는 단일 대시보드'(docs/index.html)로 발행합니다.

GitHub Actions에 얹으면 PC가 꺼져 있어도 매일 자동으로 갱신됩니다.

[중요 - 의학적 면책]
검색·정리 보조 도구이며 진단·치료 권고를 하지 않습니다. 모든 요약·분석은 부정확할 수
있고, 반드시 원문과 따님의 담당 의료진을 통해 검증해야 합니다.

[실행 모드]
  python sotos_research_agent.py --count     # 전체 건수만 확인 (비용 0)
  python sotos_research_agent.py --demo       # 네트워크 없이 샘플 대시보드 생성
  python sotos_research_agent.py --backfill    # (최초 1회) 과거 20년치 전부 분석 + 종합
  python sotos_research_agent.py               # (평소) 최근 신규만 분석 + 갱신  ← Actions가 매일 실행

[권장 흐름]  ① 집 PC에서 --backfill 1회 → ② data/ , docs/ 를 GitHub에 push
            → ③ Actions가 매일 평소 모드로 자동 증분 갱신
"""

import os
import sys
import json
import time
import html
import datetime as dt
from pathlib import Path

try:
    import requests
except ImportError:
    print("[설치 필요] 터미널에서:  pip install requests")
    sys.exit(1)

import xml.etree.ElementTree as ET


# ==========================================================================
# 1. 설정 (CONFIG)  -- 여기만 고치면 됩니다
# ==========================================================================
CONFIG = {
    # --- 추적 대상 ---
    "pubmed_query": '("Sotos syndrome"[Title/Abstract] OR "NSD1"[Title/Abstract])',
    "ctgov_condition": "Sotos syndrome",
    "ctgov_term": "NSD1",

    # --- 수집 범위 ---
    "lookback_days": 30,            # 평소(증분) 모드: 최근 30일이면 매일 돌릴 때 충분
    "backfill_lookback_days": 7300,  # --backfill 모드: ≈20년 전체
    "pubmed_max": 2000,
    "ctgov_max": 200,

    # --- AI 분석 (Anthropic) ---
    "use_ai_summary": True,
    # 평소 모드의 1회 요약 상한(비용 안전장치). --backfill은 이 값을 무시하고 전부 처리.
    "ai_summary_max_per_run": 50,
    "anthropic_model": "claude-haiku-4-5-20251001",  # 티어 허용 시 상위 모델로 품질↑
    "anthropic_max_tokens": 1024,

    # --- 종합 분석(synthesis) ---
    "synthesis_enabled": True,
    "synthesis_batch_size": 40,     # 한 묶음에 넣을 항목 수 (맵-리듀스)
    "synthesis_max_tokens": 2048,

    # --- NCBI 예의(권장) ---
    "ncbi_tool": "sotos_research_agent",
    "ncbi_email": "",
    "ncbi_api_key": "",

    # --- 사이트 ---
    "site_title": "Sotos / NSD1 연구 추적",
    "data_dir": "data",
    "docs_dir": "docs",   # GitHub Pages 발행 폴더 (index.html)
}

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / CONFIG["data_dir"]
DOCS_DIR = BASE / CONFIG["docs_dir"]
SEEN_PATH = DATA_DIR / "seen.json"
KB_PATH = DATA_DIR / "knowledge_base.jsonl"
SYNTH_PATH = DATA_DIR / "synthesis.json"
INDEX_PATH = DOCS_DIR / "index.html"


# ==========================================================================
# 2. 유틸
# ==========================================================================
def log(msg: str):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_seen() -> dict:
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    return {"pmids": [], "ncts": []}


def save_seen(seen: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def append_kb(items: list):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with KB_PATH.open("a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def load_kb() -> list:
    """누적 지식베이스 전체를 읽어 대시보드 렌더링에 사용."""
    if not KB_PATH.exists():
        return []
    items = []
    for line in KB_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return items


# ==========================================================================
# 3. 건수 조회 (수집·AI 없이)
# ==========================================================================
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CTGOV = "https://clinicaltrials.gov/api/v2/studies"


def _ncbi_common_params() -> dict:
    p = {"tool": CONFIG["ncbi_tool"]}
    if CONFIG["ncbi_email"]:
        p["email"] = CONFIG["ncbi_email"]
    if CONFIG["ncbi_api_key"]:
        p["api_key"] = CONFIG["ncbi_api_key"]
    return p


def count_pubmed() -> int:
    params = {"db": "pubmed", "term": CONFIG["pubmed_query"], "retmax": 0,
              "retmode": "json", "datetype": "pdat",
              "reldate": CONFIG["backfill_lookback_days"], **_ncbi_common_params()}
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    return int(r.json().get("esearchresult", {}).get("count", 0))


def count_trials() -> int:
    params = {"query.cond": CONFIG["ctgov_condition"], "query.term": CONFIG["ctgov_term"],
              "countTotal": "true", "pageSize": 1, "format": "json"}
    r = requests.get(CTGOV, params=params, timeout=30)
    r.raise_for_status()
    return int(r.json().get("totalCount", 0))


# ==========================================================================
# 4. 데이터 수집
# ==========================================================================
def fetch_pubmed(lookback_days: int) -> list:
    search_params = {"db": "pubmed", "term": CONFIG["pubmed_query"],
                     "retmax": CONFIG["pubmed_max"], "retmode": "json",
                     "datetype": "pdat", "reldate": lookback_days,
                     "sort": "pub_date", **_ncbi_common_params()}
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=search_params, timeout=30)
    r.raise_for_status()
    idlist = r.json().get("esearchresult", {}).get("idlist", [])
    log(f"PubMed 검색 결과: {len(idlist)}건")
    if not idlist:
        return []

    items = []
    for start in range(0, len(idlist), 200):     # id가 많으면 200개씩 나눠 받기
        chunk = idlist[start:start + 200]
        time.sleep(0.4)
        fp = {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml", **_ncbi_common_params()}
        r = requests.get(f"{EUTILS}/efetch.fcgi", params=fp, timeout=60)
        r.raise_for_status()
        items.extend(_parse_pubmed_xml(r.text))
        log(f"  PubMed 상세 수집: {min(start + 200, len(idlist))}/{len(idlist)}")
    return items


def _parse_pubmed_xml(xml_text: str) -> list:
    items = []
    root = ET.fromstring(xml_text)
    for art in root.findall(".//PubmedArticle"):
        pmid = art.findtext(".//PMID", default="")
        title = art.findtext(".//ArticleTitle", default="(제목 없음)")
        abstract = " ".join(t.text or "" for t in art.findall(".//AbstractText")).strip()
        journal = art.findtext(".//Journal/Title", default="")
        year = art.findtext(".//PubDate/Year", default="") or art.findtext(".//PubDate/MedlineDate", default="")
        all_authors = art.findall(".//Author")
        authors = []
        for a in all_authors[:3]:
            ln = a.findtext("LastName", default="")
            init = a.findtext("Initials", default="")
            if ln:
                authors.append(f"{ln} {init}".strip())
        author_str = ", ".join(authors) + (" et al." if len(all_authors) > 3 else "")
        items.append({
            "type": "pubmed", "id": pmid, "title": title,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "date": year,
            "meta": {"journal": journal, "authors": author_str},
            "raw_text": abstract or "(이 논문은 초록이 제공되지 않습니다.)", "ai": None,
        })
    return items


def fetch_trials(lookback_days: int) -> list:
    params = {"query.cond": CONFIG["ctgov_condition"], "query.term": CONFIG["ctgov_term"],
              "pageSize": CONFIG["ctgov_max"], "format": "json"}
    r = requests.get(CTGOV, params=params, timeout=30)
    r.raise_for_status()
    studies = r.json().get("studies", [])
    log(f"ClinicalTrials.gov 검색 결과: {len(studies)}건")
    cutoff = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    items = []
    for s in studies:
        ps = s.get("protocolSection", {})
        ident, status = ps.get("identificationModule", {}), ps.get("statusModule", {})
        desc, design = ps.get("descriptionModule", {}), ps.get("designModule", {})
        cond = ps.get("conditionsModule", {})
        nct = ident.get("nctId", "")
        last_update = status.get("lastUpdatePostDateStruct", {}).get("date", "")
        if last_update and last_update < cutoff:
            continue
        items.append({
            "type": "trial", "id": nct, "title": ident.get("briefTitle", "(제목 없음)"),
            "url": f"https://clinicaltrials.gov/study/{nct}", "date": last_update,
            "meta": {"status": status.get("overallStatus", ""),
                     "phase": ", ".join(design.get("phases", []) or ["N/A"]),
                     "conditions": ", ".join(cond.get("conditions", []))},
            "raw_text": desc.get("briefSummary", "(요약 정보 없음)"), "ai": None,
        })
    return items


# ==========================================================================
# 5. AI 분석 (항목별)  -- 가드레일 내장
# ==========================================================================
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

SUMMARY_SYSTEM = """당신은 희귀 유전질환(Sotos 증후군 / NSD1 유전자) 아이를 둔 보호자가
의학 연구 자료를 이해하도록 돕는 '요약 보조자'입니다. 규칙을 반드시 지키세요.
1. 진단·치료 권고·복용 지시 금지. 당신은 의료진이 아닙니다.
2. 제공된 원문에 실제로 있는 내용만. 추측·창작 금지.
3. 연구 단계를 구분: 리뷰/관찰연구/전임상/초기임상/후기임상/사례보고/기타.
4. 정보가 부족하면 솔직히 "원문에 정보 부족"이라고 표기.
5. 한국어. 아래 JSON '하나만' 출력(설명·마크다운·코드펜스 금지).
{
  "summary_3lines": "핵심을 3문장 이내로",
  "study_stage": "리뷰|관찰연구|전임상|초기임상|후기임상|사례보고|기타 중 하나",
  "relevance": "Sotos/NSD1 보호자에게 왜 중요한지 1~2문장(없으면 '직접 관련성 낮음')",
  "questions_for_doctor": ["담당 의료진에게 물어볼 질문 1~3개"]
}"""


def _call_anthropic(api_key, system, user, max_tokens):
    body = {"model": CONFIG["anthropic_model"], "max_tokens": max_tokens,
            "system": system, "messages": [{"role": "user", "content": user}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"status {r.status_code}: {r.text[:200]}")
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _parse_json_loose(text: str):
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def ai_summarize(item: dict, api_key: str) -> dict | None:
    user = (f"제목: {item['title']}\n종류: {'논문' if item['type']=='pubmed' else '임상시험'}\n"
            f"원문:\n{item['raw_text'][:6000]}")
    try:
        return _parse_json_loose(_call_anthropic(api_key, SUMMARY_SYSTEM, user,
                                                 CONFIG["anthropic_max_tokens"]))
    except Exception as e:
        log(f"  ! 항목 요약 실패({e}) - 원문만: {item['id']}")
        return None


# ==========================================================================
# 6. 종합 분석 (synthesis)  -- 맵-리듀스 + 가드레일
# ==========================================================================
SYNTH_MAP_SYSTEM = """Sotos/NSD1 논문·임상시험 요약 묶음을 받아, 이 묶음에서 드러나는
연구 주제와 핵심 발견을 한국어로 간결히 정리하세요. 진단·치료 권고 금지, 원문 범위 내에서만.
형식: 주제별로 'ㆍ주제: 핵심 발견(연구 단계)' 한 줄씩. 다른 군더더기 없이."""

SYNTH_REDUCE_SYSTEM = """당신은 Sotos/NSD1 연구 동향을 보호자에게 정리해 주는 분석가입니다.
여러 묶음의 주제 메모를 종합해, 전체 그림을 한국어로 정리하세요.
규칙: 진단·치료 권고 금지. 근거 수준(확립/임상/전임상/초기)을 구분해 표기. 과장 금지.
아래 JSON '하나만' 출력(코드펜스 금지):
{
  "overview": "전체 연구 지형을 2~3문장으로",
  "themes": [{"title": "주제명", "detail": "핵심 발견과 근거 수준을 2~4문장으로"}],
  "recent_developments": "최근 새롭거나 주목할 흐름 2~3문장(없으면 '특이 동향 없음')",
  "questions_for_doctor": ["보호자가 의료진과 논의할 큰 그림 질문 3~5개"]
}"""


def synthesize(items_with_ai: list, api_key: str) -> dict | None:
    """AI 요약이 있는 항목들을 맵-리듀스로 종합 분석."""
    usable = [i for i in items_with_ai if i.get("ai")]
    if not usable:
        return None
    bs = CONFIG["synthesis_batch_size"]
    batch_notes = []
    log(f"종합 분석: {len(usable)}건을 {bs}건씩 묶어 처리…")
    for start in range(0, len(usable), bs):
        batch = usable[start:start + bs]
        lines = []
        for it in batch:
            ai = it["ai"]
            lines.append(f"[{it['type']}] {it['title']} / 단계:{ai.get('study_stage','')} / "
                         f"{ai.get('summary_3lines','')}")
        user = "다음 요약 묶음을 정리:\n" + "\n".join(lines)
        try:
            note = _call_anthropic(api_key, SYNTH_MAP_SYSTEM, user, CONFIG["synthesis_max_tokens"])
            batch_notes.append(note.strip())
            log(f"  묶음 {start // bs + 1} 처리 완료")
        except Exception as e:
            log(f"  ! 묶음 {start // bs + 1} 실패({e})")
        time.sleep(0.5)

    if not batch_notes:
        return None
    reduce_user = "다음은 여러 묶음의 주제 메모입니다. 전체를 종합하세요:\n\n" + "\n\n".join(batch_notes)
    try:
        result = _parse_json_loose(_call_anthropic(api_key, SYNTH_REDUCE_SYSTEM, reduce_user,
                                                   CONFIG["synthesis_max_tokens"]))
    except Exception as e:
        log(f"  ! 종합 단계 실패({e})")
        return None
    result["generated_at"] = dt.datetime.now().isoformat()
    result["based_on"] = len(usable)
    return result


# ==========================================================================
# 7. 대시보드 렌더링 (살아있는 단일 페이지)  -- 데이터와 분리
# ==========================================================================
def render_dashboard(all_items: list, synth: dict | None, run_time: dt.datetime) -> str:
    pubmed_n = sum(1 for i in all_items if i["type"] == "pubmed")
    trial_n = sum(1 for i in all_items if i["type"] == "trial")

    # 클라이언트 검색/필터용 데이터 (필요한 필드만 추려서 embed)
    slim = []
    for it in all_items:
        ai = it.get("ai") or {}
        slim.append({
            "type": it["type"], "id": it["id"], "title": it.get("title", ""),
            "url": it.get("url", ""), "date": str(it.get("date", "")),
            "meta": it.get("meta", {}),
            "summary": ai.get("summary_3lines", ""),
            "stage": ai.get("study_stage", ""),
            "relevance": ai.get("relevance", ""),
            "questions": ai.get("questions_for_doctor", []),
            "has_ai": bool(it.get("ai")),
        })
    data_json = json.dumps(slim, ensure_ascii=False).replace("</", "<\\/")

    def esc(x):
        return html.escape(str(x or ""))

    # 종합 분석 섹션 (서버 측 정적 렌더)
    if synth:
        themes = "".join(
            f'<div class="theme"><h4>{esc(t.get("title"))}</h4><p>{esc(t.get("detail"))}</p></div>'
            for t in synth.get("themes", []))
        q = "".join(f"<li>{esc(x)}</li>" for x in synth.get("questions_for_doctor", []))
        synth_html = f"""
        <section class="synthesis">
          <div class="synth-head">종합 분석 <span class="synth-meta">{esc(synth.get('based_on'))}건 기반 · {esc((synth.get('generated_at') or '')[:10])}</span></div>
          <p class="synth-overview">{esc(synth.get('overview'))}</p>
          <div class="themes">{themes}</div>
          <p class="synth-recent"><b>최근 동향.</b> {esc(synth.get('recent_developments'))}</p>
          <div class="callout"><div class="callout-title">의료진과 논의할 큰 그림 질문</div><ul>{q}</ul></div>
        </section>"""
    else:
        synth_html = '<section class="synthesis"><p class="muted">종합 분석은 항목이 모이면 생성됩니다.</p></section>'

    css = """
    :root{--bg:#faf8f4;--ink:#1c1a17;--muted:#6b6457;--line:#e6e0d6;--card:#fff;
      --accent:#7c5c3b;--soft:#f3ece1;--warn-bg:#fdf3f0;--warn-ink:#8a3b2c;--warn-line:#f0cdc3;
      --pub:#4a6b7c;--trial:#6b7c4a;}
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--ink);font-family:'Noto Sans KR',sans-serif;line-height:1.7}
    .wrap{max-width:900px;margin:0 auto;padding:40px 22px 90px}
    h1{font-family:'Lora',serif;font-size:clamp(1.5rem,4vw,2.2rem);margin:0 0 4px}
    .sub{color:var(--muted);margin:0 0 20px;font-size:.9rem}
    .disclaimer{background:var(--warn-bg);border:1px solid var(--warn-line);color:var(--warn-ink);
      border-radius:12px;padding:13px 17px;font-size:.88rem;margin-bottom:26px}
    .synthesis{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:24px 26px;margin-bottom:30px}
    .synth-head{font-family:'Lora',serif;font-size:1.4rem;margin-bottom:10px}
    .synth-meta{font-family:'Noto Sans KR';font-size:.72rem;color:var(--muted);font-weight:400}
    .synth-overview{font-size:1rem;margin:0 0 16px}
    .themes{display:grid;gap:12px;margin-bottom:16px}
    .theme{background:var(--soft);border-radius:10px;padding:12px 16px}
    .theme h4{margin:0 0 4px;font-size:.95rem;color:var(--accent)}
    .theme p{margin:0;font-size:.9rem}
    .synth-recent{font-size:.92rem;margin:0 0 16px}
    .callout{background:var(--soft);border-left:3px solid var(--accent);border-radius:0 8px 8px 0;padding:12px 16px}
    .callout-title{font-weight:700;font-size:.85rem;color:var(--accent);margin-bottom:4px}
    .callout ul{margin:0;padding-left:18px;font-size:.9rem}
    .controls{display:flex;gap:10px;flex-wrap:wrap;margin:24px 0 18px;align-items:center}
    .controls input,.controls select{font-family:inherit;font-size:.9rem;padding:8px 12px;
      border:1px solid var(--line);border-radius:9px;background:#fff}
    .controls input{flex:1;min-width:200px}
    .countbar{font-size:.82rem;color:var(--muted);margin-bottom:14px}
    .item{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:18px 20px;margin-bottom:14px}
    .item-head{display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap}
    .src{font-size:.68rem;font-weight:700;letter-spacing:.03em;padding:3px 8px;border-radius:6px;color:#fff}
    .src-pubmed{background:var(--pub)}.src-trial{background:var(--trial)}
    .badge{font-size:.7rem;background:var(--soft);color:var(--accent);padding:3px 8px;border-radius:6px}
    .item h3{font-size:1rem;margin:3px 0 5px;line-height:1.45}
    .item h3 a{color:var(--ink);text-decoration:none}.item h3 a:hover{color:var(--accent);text-decoration:underline}
    .meta{color:var(--muted);font-size:.8rem;margin:0 0 9px}
    .summary{margin:0 0 7px;font-size:.94rem}.relevance{font-size:.88rem;color:#3a352d;margin:0 0 9px}
    .qbox{background:var(--soft);border-radius:8px;padding:9px 13px;font-size:.86rem}
    .qbox ul{margin:4px 0 0;padding-left:17px}
    .muted{color:var(--muted)}
    footer{margin-top:46px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:.78rem}
    """

    js = """
    const ITEMS = __DATA__;
    const listEl = document.getElementById('list');
    const countEl = document.getElementById('countbar');
    const qEl = document.getElementById('q');
    const typeEl = document.getElementById('type');
    function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
    function card(it){
      const src = it.type==='pubmed' ? 'PubMed' : 'ClinicalTrials.gov';
      const meta = it.type==='pubmed'
        ? `${esc(it.meta.journal||'')} · ${esc(it.meta.authors||'')} · ${esc(it.date)}`
        : `상태:${esc(it.meta.status||'')} · 단계:${esc(it.meta.phase||'')} · 갱신:${esc(it.date)}`;
      const badge = it.stage ? `<span class="badge">${esc(it.stage)}</span>` : '';
      let body;
      if(it.has_ai){
        const qs = (it.questions||[]).map(q=>`<li>${esc(q)}</li>`).join('');
        body = `<p class="summary">${esc(it.summary)}</p>`+
               (it.relevance?`<p class="relevance"><b>왜 중요한가:</b> ${esc(it.relevance)}</p>`:'')+
               (qs?`<div class="qbox"><b>의료진 질문</b><ul>${qs}</ul></div>`:'');
      } else {
        body = `<p class="summary muted">AI 분석 대기 중 — 원문 링크 참고</p>`;
      }
      return `<article class="item"><div class="item-head"><span class="src src-${it.type}">${src}</span>${badge}</div>`+
        `<h3><a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title)}</a></h3>`+
        `<p class="meta">${meta}</p>${body}</article>`;
    }
    function render(){
      const q = qEl.value.trim().toLowerCase();
      const t = typeEl.value;
      let rows = ITEMS.filter(it=>{
        if(t!=='all' && it.type!==t) return false;
        if(!q) return true;
        return (it.title+' '+it.summary+' '+it.relevance).toLowerCase().includes(q);
      });
      rows.sort((a,b)=> String(b.date).localeCompare(String(a.date)));
      countEl.textContent = `${rows.length}건 표시 (전체 ${ITEMS.length}건)`;
      listEl.innerHTML = rows.map(card).join('') || '<p class="muted">조건에 맞는 항목이 없습니다.</p>';
    }
    qEl.addEventListener('input', render);
    typeEl.addEventListener('change', render);
    render();
    """
    js = js.replace("__DATA__", data_json)

    return (
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(CONFIG['site_title'])}</title>"
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link href='https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&family=Lora:wght@500;600&display=swap' rel='stylesheet'>"
        f"<style>{css}</style></head><body><div class='wrap'>"
        f"<h1>{esc(CONFIG['site_title'])}</h1>"
        f"<p class='sub'>마지막 갱신 {run_time:%Y-%m-%d %H:%M} · 논문 {pubmed_n} · 임상시험 {trial_n}</p>"
        "<div class='disclaimer'><b>읽기 전에.</b> 검색·정리를 돕는 도구가 만든 자료입니다. "
        "요약·분석은 부정확할 수 있으며 진단·치료 판단이 아닙니다. <b>모든 내용은 원문과 담당 의료진을 통해 확인</b>하세요.</div>"
        f"{synth_html}"
        "<div class='controls'><input id='q' placeholder='검색어 (제목·요약 내)'>"
        "<select id='type'><option value='all'>전체</option>"
        "<option value='pubmed'>논문</option><option value='trial'>임상시험</option></select></div>"
        "<div class='countbar' id='countbar'></div><div id='list'></div>"
        "<footer>누적 기록: data/knowledge_base.jsonl · 검색식·주기는 스크립트 CONFIG에서 수정 · v2.0</footer>"
        f"<script>{js}</script></div></body></html>"
    )


# ==========================================================================
# 8. main()
# ==========================================================================
DEMO_ITEMS = [
    {"type": "pubmed", "id": "DEMO1", "title": "Sample: NSD1 splice variant in familial Sotos syndrome",
     "url": "https://pubmed.ncbi.nlm.nih.gov/", "date": "2026",
     "meta": {"journal": "Demo Journal", "authors": "Hong GD et al."},
     "raw_text": "Sample abstract for offline preview.",
     "ai": {"summary_3lines": "샘플 요약입니다.", "study_stage": "사례보고",
            "relevance": "오프라인 미리보기용 예시입니다.",
            "questions_for_doctor": ["이 변이 유형이 우리 아이와 관련 있나요?"]}},
    {"type": "trial", "id": "NCTDEMO", "title": "Sample: Growth patterns in Sotos syndrome",
     "url": "https://clinicaltrials.gov/", "date": "2026-05-01",
     "meta": {"status": "RECRUITING", "phase": "N/A", "conditions": "Sotos Syndrome"},
     "raw_text": "Sample summary.",
     "ai": {"summary_3lines": "샘플 임상시험 요약.", "study_stage": "관찰연구",
            "relevance": "예시.", "questions_for_doctor": ["참여 조건이 궁금합니다."]}},
]
DEMO_SYNTH = {"overview": "이것은 오프라인 미리보기용 샘플 종합 분석입니다.",
              "themes": [{"title": "유전형-표현형", "detail": "샘플 주제 설명."},
                         {"title": "성장 관리", "detail": "샘플 주제 설명."}],
              "recent_developments": "샘플 동향.", "questions_for_doctor": ["샘플 질문 1", "샘플 질문 2"],
              "generated_at": dt.datetime.now().isoformat(), "based_on": 2}


def main():
    run_time = dt.datetime.now()

    # ----- 건수만 -----
    if "--count" in sys.argv:
        log("건수만 확인 (수집·요약·비용 없음)…")
        try: n_pub = count_pubmed()
        except Exception as e: n_pub = None; log(f"  PubMed 실패: {e}")
        try: n_trial = count_trials()
        except Exception as e: n_trial = None; log(f"  ClinicalTrials 실패: {e}")
        print("\n===== 전체 건수 (최근 20년) =====")
        print(f"  PubMed 논문    : {n_pub if n_pub is not None else '실패'} 건")
        print(f"  ClinicalTrials : {n_trial if n_trial is not None else '실패'} 건")
        if n_pub is not None and n_trial is not None:
            print(f"  합계           : 약 {n_pub + n_trial} 건")
        return

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    # ----- 데모 -----
    if "--demo" in sys.argv:
        log("DEMO 모드: 샘플로 대시보드만 생성합니다.")
        INDEX_PATH.write_text(render_dashboard(DEMO_ITEMS, DEMO_SYNTH, run_time), encoding="utf-8")
        log(f"대시보드 생성 → {INDEX_PATH}")
        return

    backfill = "--backfill" in sys.argv
    lookback = CONFIG["backfill_lookback_days"] if backfill else CONFIG["lookback_days"]
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    use_ai = CONFIG["use_ai_summary"] and bool(api_key)
    if backfill:
        log("BACKFILL 모드: 과거 20년치 전체를 분석합니다 (시간·비용이 한 번에 발생).")

    # --- 수집 ---
    seen = load_seen()
    log("PubMed 수집 중…")
    pubmed = fetch_pubmed(lookback)
    log("ClinicalTrials.gov 수집 중…")
    trials = fetch_trials(lookback)
    new_pubmed = [i for i in pubmed if i["id"] not in set(seen["pmids"])]
    new_trials = [i for i in trials if i["id"] not in set(seen["ncts"])]
    all_new = new_pubmed + new_trials
    log(f"신규 항목: 논문 {len(new_pubmed)} · 임상시험 {len(new_trials)}")

    # --- 처리 대상 결정 (백필=전부 / 평소=상한) ---
    cap = 0 if backfill else CONFIG.get("ai_summary_max_per_run", 0)
    if use_ai and cap and len(all_new) > cap:
        to_process = all_new[:cap]
        log(f"신규 {len(all_new)}건 중 이번엔 {cap}건만 분석(나머지는 다음 실행 때).")
    else:
        to_process = all_new

    # --- 항목별 AI 분석 ---
    if use_ai and to_process:
        log(f"AI 분석 중… ({len(to_process)}건)")
        for idx, it in enumerate(to_process, 1):
            it["ai"] = ai_summarize(it, api_key)
            if idx % 25 == 0:
                log(f"  분석 진행: {idx}/{len(to_process)}")
            time.sleep(0.4)
    elif CONFIG["use_ai_summary"] and not api_key:
        log("! ANTHROPIC_API_KEY 미설정 → AI 분석 건너뜀(원문만, 비용 0).")

    # --- 상태/지식베이스 갱신 (처리한 것만 seen) ---
    processed = {i["id"] for i in to_process}
    seen["pmids"].extend(i["id"] for i in new_pubmed if i["id"] in processed)
    seen["ncts"].extend(i["id"] for i in new_trials if i["id"] in processed)
    save_seen(seen)
    if to_process:
        for it in to_process:
            it["fetched_at"] = run_time.isoformat()
        append_kb(to_process)

    # --- 전체 지식베이스 로드 ---
    all_items = load_kb()

    # --- 종합 분석: 신규가 있었거나 / 백필이거나 / 아직 종합이 없으면 재생성 ---
    synth = None
    if SYNTH_PATH.exists():
        try: synth = json.loads(SYNTH_PATH.read_text(encoding="utf-8"))
        except Exception: synth = None
    need_synth = CONFIG["synthesis_enabled"] and use_ai and (backfill or to_process or synth is None)
    if need_synth and any(i.get("ai") for i in all_items):
        new_synth = synthesize(all_items, api_key)
        if new_synth:
            synth = new_synth
            SYNTH_PATH.write_text(json.dumps(synth, ensure_ascii=False, indent=2), encoding="utf-8")
            log("종합 분석 갱신 완료.")

    # --- 대시보드 발행 ---
    INDEX_PATH.write_text(render_dashboard(all_items, synth, run_time), encoding="utf-8")
    log(f"대시보드 발행 → {INDEX_PATH}  (전체 {len(all_items)}건)")


if __name__ == "__main__":
    main()
