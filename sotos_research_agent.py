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
    # --- 수집 범위 (전역) ---
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
    "site_title": "서아 통합 연구·케어",
    "data_dir": "data",
    "docs_dir": "docs",   # GitHub Pages 발행 폴더 (index.html)

    # --- 국내 실용자료 수집 (robots.txt 준수) ---
    "crawler_user_agent": "seoa-care-agent",
    # 신뢰하는 '공식 공개' 페이지/피드만 넣으세요. kind: "page"(정보 페이지) | "rss"(피드)
    # 아래는 예시이며 자유롭게 교체/추가하세요. (각 URL은 robots.txt 허용 시에만 수집됩니다.)
    "resource_sources": [
        {"id": "snuh_sotos", "label": "서울대병원 희귀질환센터", "title": "소토스 증후군 정보",
         "kind": "page",
         "url": "https://raredisease.snuh.org/rare-disease-info/congenital-malformation/소토스-증후군/"},
        {"id": "nrc_child", "label": "국립재활원 재활정보포털", "title": "장애아동 질병과 재활치료",
         "kind": "page",
         "url": "https://www.nrc.go.kr/portal/html/content.do?depth=dc&menu_cd=06_02_02"},
    ],

    # --- 공유 캘린더 (Firebase) ---
    # Firebase 콘솔 > 프로젝트 설정 > '웹 앱' 의 firebaseConfig 값을 그대로 채우세요.
    # 웹 config는 공개돼도 안전합니다(보안은 Realtime DB '보안 규칙' + 로그인으로 합니다).
    # 비워두면 캘린더 탭에 설정 안내가 표시되고, 나머지 기능은 정상 동작합니다.
    "firebase_config": {
        # "apiKey": "...",
        # "authDomain": "your-project.firebaseapp.com",
        # "databaseURL": "https://your-project-default-rtdb.firebaseio.com",
        # "projectId": "your-project",
        # "appId": "...",
    },
    "calendar_categories": ["인지", "운동", "자조", "언어", "병원", "기타"],
}

# 연구 '영역(domain)'별 검색 스트림. 영역을 추가/수정하려면 여기만 고치면 됩니다.
#  - lookback_days: 평소(증분) 수집 범위 / backfill_lookback_days: 최초 전체 수집 범위
#  - 발달치료는 연구량이 매우 많으므로 범위를 좁히고(3년) 질의를 소아·실용 중심으로 한정
STREAMS = [
    {
        "id": "sotos", "label": "Sotos 연구",
        "pubmed_query": '("Sotos syndrome"[Title/Abstract] OR "NSD1"[Title/Abstract])',
        "ctgov_condition": "Sotos syndrome", "ctgov_term": "NSD1",
        "lookback_days": 30, "backfill_lookback_days": 7300,   # ≈20년
    },
    {
        "id": "therapy", "label": "발달치료 연구",
        "pubmed_query": ('(("developmental delay"[Title/Abstract] OR "developmental disability"[Title/Abstract] '
                         'OR "intellectual disability"[Title/Abstract]) '
                         'AND ("occupational therapy"[Title/Abstract] OR "self-care"[Title/Abstract] '
                         'OR "activities of daily living"[Title/Abstract] OR "early intervention"[Title/Abstract] '
                         'OR "cognitive intervention"[Title/Abstract] OR "motor intervention"[Title/Abstract]) '
                         'AND ("child"[Title/Abstract] OR "children"[Title/Abstract] OR "pediatric"[Title/Abstract]))'),
        "ctgov_condition": "developmental delay", "ctgov_term": "rehabilitation",
        "lookback_days": 30, "backfill_lookback_days": 1095,    # ≈3년 (양이 많아 범위 한정)
    },
]
# 대시보드 영역 라벨 (id -> 표시명)
DOMAIN_LABELS = {s["id"]: s["label"] for s in STREAMS}

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


def write_kb(items: list):
    """지식베이스 전체를 다시 씀 (주제 꼬리표 갱신 등 기존 항목 수정용)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with KB_PATH.open("w", encoding="utf-8") as f:
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


def count_pubmed(query: str, lookback_days: int) -> int:
    params = {"db": "pubmed", "term": query, "retmax": 0,
              "retmode": "json", "datetype": "pdat",
              "reldate": lookback_days, **_ncbi_common_params()}
    r = requests.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    return int(r.json().get("esearchresult", {}).get("count", 0))


def count_trials(cond: str, term: str) -> int:
    params = {"query.cond": cond, "query.term": term,
              "countTotal": "true", "pageSize": 1, "format": "json"}
    r = requests.get(CTGOV, params=params, timeout=30)
    r.raise_for_status()
    return int(r.json().get("totalCount", 0))


# ==========================================================================
# 4. 데이터 수집
# ==========================================================================
def fetch_pubmed(query: str, lookback_days: int) -> list:
    search_params = {"db": "pubmed", "term": query,
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


def fetch_trials(cond: str, term: str, lookback_days: int) -> list:
    params = {"query.cond": cond, "query.term": term,
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

# 주제 분류 체계 (CONFIG처럼 자유롭게 수정 가능). 탭 표시 순서이기도 함.
TOPICS = ["유전·진단", "성장·발달", "신경·인지·행동", "종양·감시",
          "합병증·동반질환", "치료·관리", "기전·기초연구", "기타"]
_TOPICS_STR = ", ".join(TOPICS)

SUMMARY_SYSTEM = """당신은 희귀 유전질환(Sotos 증후군 / NSD1 유전자) 아이를 둔 보호자가
의학 연구 자료를 이해하도록 돕는 '요약 보조자'입니다. 규칙을 반드시 지키세요.
1. 진단·치료 권고·복용 지시 금지. 당신은 의료진이 아닙니다.
2. 제공된 원문에 실제로 있는 내용만. 추측·창작 금지.
3. 연구 단계를 구분: 리뷰/관찰연구/전임상/초기임상/후기임상/사례보고/기타.
4. 정보가 부족하면 솔직히 "원문에 정보 부족"이라고 표기.
5. 주제는 다음 중 가장 알맞은 하나만: """ + _TOPICS_STR + """.
6. 한국어. 아래 JSON '하나만' 출력(설명·마크다운·코드펜스 금지).
{
  "summary_3lines": "핵심을 3문장 이내로",
  "study_stage": "리뷰|관찰연구|전임상|초기임상|후기임상|사례보고|기타 중 하나",
  "topic": "위 주제 목록 중 하나",
  "relevance": "Sotos/NSD1 보호자에게 왜 중요한지 1~2문장(없으면 '직접 관련성 낮음')",
  "questions_for_doctor": ["담당 의료진에게 물어볼 질문 1~3개"]
}"""

CLASSIFY_SYSTEM = """다음 Sotos/NSD1 논문·임상 목록을 각각 가장 알맞은 주제 하나로 분류하세요.
허용 주제(이 중 하나만): """ + _TOPICS_STR + """.
규칙: 각 항목 머리의 id에 주제를 매칭. 판단이 어려우면 '기타'. 주제명은 한국어 그대로.
출력은 JSON 객체 하나만(코드펜스·설명 금지): {"항목id": "주제", ...}"""


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


def ai_classify_batch(batch: list, api_key: str) -> dict:
    """여러 항목을 한 번에 주제 분류. {id: topic} 반환. 비용 절감 위해 묶음 처리."""
    lines = []
    for it in batch:
        ai = it.get("ai") or {}
        lines.append(f"[{it['id']}] {it.get('title','')} — {ai.get('summary_3lines','')[:200]}")
    user = "분류할 목록:\n" + "\n".join(lines)
    try:
        result = _parse_json_loose(_call_anthropic(api_key, CLASSIFY_SYSTEM, user, 1024))
        return result if isinstance(result, dict) else {}
    except Exception as e:
        log(f"  ! 묶음 분류 실패({e})")
        return {}


# ==========================================================================
# 5-2. 국내 실용자료 수집 (robots.txt 준수 · 페이지/RSS)
# ==========================================================================
import urllib.robotparser
from urllib.parse import urlparse
from html.parser import HTMLParser

RESOURCES_PATH = DATA_DIR / "resources.jsonl"


class _TextExtractor(HTMLParser):
    """HTML에서 본문 텍스트만 추출 (script/style 제외). bs4 등 외부 의존성 없이 stdlib만 사용."""
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def robots_allowed(url: str) -> bool:
    """robots.txt에서 허용되는지 확인. 읽을 수 없으면 표준 해석상 허용으로 간주."""
    try:
        p = urlparse(url)
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{p.scheme}://{p.netloc}/robots.txt")
        rp.read()
        return rp.can_fetch(CONFIG.get("crawler_user_agent", "*"), url)
    except Exception:
        return True


def fetch_text(url: str) -> str:
    headers = {"User-Agent": CONFIG.get("crawler_user_agent", "seoa-care-agent")}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    ext = _TextExtractor()
    ext.feed(r.text)
    return " ".join(" ".join(ext.parts).split())[:6000]


RESOURCE_SUMMARY_SYSTEM = """당신은 발달지연 아동(Sotos 증후군)을 돌보는 보호자를 돕는 보조자입니다.
국내 공식 사이트 글에서 '보호자에게 실용적으로 도움되는 내용'을 한국어로 정리하세요.
규칙: 진단·치료 권고 금지. 글에 있는 내용만. 메뉴·광고·머리말 등 잡음 제외.
아래 JSON 하나만 출력(코드펜스 금지):
{
  "summary_3lines": "핵심 내용 3문장 이내",
  "relevance": "발달치료(인지·운동·자조 등)에 어떻게 도움되는지 1~2문장(관련 낮으면 '관련성 낮음')",
  "tips": ["일상에서 적용해볼 포인트 1~3개(글에 근거)"]
}"""


def ai_summarize_resource(title: str, text: str, api_key: str) -> dict | None:
    try:
        return _parse_json_loose(_call_anthropic(
            api_key, RESOURCE_SUMMARY_SYSTEM, f"제목: {title}\n본문:\n{text}",
            CONFIG["anthropic_max_tokens"]))
    except Exception as e:
        log(f"  ! 자료 요약 실패({e})")
        return None


def _parse_rss(xml_text: str) -> list:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out
    for item in root.iter():
        tag = item.tag.lower().split("}")[-1]
        if tag in ("item", "entry"):
            rec = {"title": "", "link": "", "date": ""}
            for ch in item:
                t = ch.tag.lower().split("}")[-1]
                if t == "title":
                    rec["title"] = (ch.text or "").strip()
                elif t == "link":
                    rec["link"] = (ch.text or "").strip() or ch.attrib.get("href", "")
                elif t in ("pubdate", "updated", "published", "date"):
                    rec["date"] = rec["date"] or (ch.text or "").strip()
            if rec["link"]:
                out.append(rec)
    return out


def load_resources() -> list:
    if not RESOURCES_PATH.exists():
        return []
    items = []
    for line in RESOURCES_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return items


def append_resources(items: list):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with RESOURCES_PATH.open("a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def collect_resources(api_key: str, seen_urls: set) -> list:
    """resource_sources를 순회하며 robots 허용 시 수집·요약. 신규 자료 리스트 반환."""
    new = []
    for src in CONFIG.get("resource_sources", []):
        url = src["url"]
        try:
            if not robots_allowed(url):
                log(f"  [국내자료] robots.txt 비허용 → 건너뜀: {src['label']}")
                continue
            if src.get("kind") == "rss":
                r = requests.get(url, headers={"User-Agent": CONFIG.get("crawler_user_agent", "seoa-care-agent")}, timeout=30)
                r.raise_for_status()
                for e in _parse_rss(r.text):
                    if e["link"] in seen_urls or not robots_allowed(e["link"]):
                        continue
                    try:
                        text = fetch_text(e["link"])
                    except Exception:
                        text = e["title"]
                    ai = ai_summarize_resource(e["title"], text, api_key) if api_key else None
                    new.append({"source": src["label"], "title": e["title"] or src["label"],
                                "url": e["link"], "date": (e["date"] or "")[:10], "ai": ai})
                    seen_urls.add(e["link"]); time.sleep(0.5)
            else:  # page
                if url in seen_urls:
                    continue
                text = fetch_text(url)
                ai = ai_summarize_resource(src.get("title", src["label"]), text, api_key) if api_key else None
                new.append({"source": src["label"], "title": src.get("title", src["label"]),
                            "url": url, "date": dt.date.today().isoformat(), "ai": ai})
                seen_urls.add(url); time.sleep(0.5)
        except Exception as e:
            log(f"  ! [국내자료] 수집 실패({src['label']}): {e}")
    return new


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
def render_dashboard(all_items: list, synth: dict | None, run_time: dt.datetime, resources: list | None = None) -> str:
    resources = resources or []
    pubmed_n = sum(1 for i in all_items if i["type"] == "pubmed")
    trial_n = sum(1 for i in all_items if i["type"] == "trial")

    # 클라이언트 검색/필터용 데이터 (필요한 필드만 추려서 embed)
    slim = []
    for it in all_items:
        ai = it.get("ai") or {}
        slim.append({
            "type": it["type"], "id": it["id"], "title": it.get("title", ""),
            "url": it.get("url", ""), "date": str(it.get("date", "")),
            "domain": it.get("domain", "sotos"),
            "meta": it.get("meta", {}),
            "summary": ai.get("summary_3lines", ""),
            "stage": ai.get("study_stage", ""),
            "topic": ai.get("topic", ""),
            "relevance": ai.get("relevance", ""),
            "questions": ai.get("questions_for_doctor", []),
            "has_ai": bool(it.get("ai")),
        })
    data_json = json.dumps(slim, ensure_ascii=False).replace("</", "<\\/")

    # 국내 실용자료 데이터
    res_slim = []
    for r in resources:
        ai = r.get("ai") or {}
        res_slim.append({
            "source": r.get("source", ""), "title": r.get("title", ""),
            "url": r.get("url", ""), "date": str(r.get("date", "")),
            "summary": ai.get("summary_3lines", ""),
            "relevance": ai.get("relevance", ""),
            "tips": ai.get("tips", []), "has_ai": bool(r.get("ai")),
        })
    res_json = json.dumps(res_slim, ensure_ascii=False).replace("</", "<\\/")

    # 캘린더(Firebase) 설정
    fb_cfg = {k: v for k, v in CONFIG.get("firebase_config", {}).items() if v}
    fb_json = json.dumps(fb_cfg, ensure_ascii=False) if fb_cfg else "null"
    cats_json = json.dumps(CONFIG.get("calendar_categories", []), ensure_ascii=False)
    fb_enabled = bool(fb_cfg)

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
    .badge-topic{background:#eef2f4;color:var(--pub)}
    .item h3{font-size:1rem;margin:3px 0 5px;line-height:1.45}
    .item h3 a{color:var(--ink);text-decoration:none}.item h3 a:hover{color:var(--accent);text-decoration:underline}
    .meta{color:var(--muted);font-size:.8rem;margin:0 0 9px}
    .summary{margin:0 0 7px;font-size:.94rem}.relevance{font-size:.88rem;color:#3a352d;margin:0 0 9px}
    .qbox{background:var(--soft);border-radius:8px;padding:9px 13px;font-size:.86rem}
    .qbox ul{margin:4px 0 0;padding-left:17px}
    .muted{color:var(--muted)}
    .sections{display:flex;gap:4px;border-bottom:2px solid var(--line);margin:6px 0 8px}
    .secbtn{font-family:'Lora',serif;font-size:1.05rem;padding:9px 18px;border:none;background:none;cursor:pointer;color:var(--muted);border-bottom:3px solid transparent;margin-bottom:-2px}
    .secbtn.active{color:var(--accent);border-bottom-color:var(--accent)}
    .secbtn .n{font-family:'Noto Sans KR';font-size:.72rem;opacity:.7}
    .res-intro{color:var(--muted);font-size:.86rem;margin:14px 0 16px}
    .src-res{background:#8a6d3b}
    .cal-setup,.cal-login{background:#fff;border:1px solid var(--line);border-radius:12px;padding:22px;margin-top:18px;max-width:420px}
    .cal-login input{display:block;width:100%;box-sizing:border-box;margin:8px 0;padding:10px 12px;border:1px solid var(--line);border-radius:8px;font-family:inherit;font-size:.95rem}
    .cal-btn{font-family:inherit;font-weight:600;padding:9px 18px;border:none;border-radius:8px;background:var(--accent);color:#fff;cursor:pointer}
    .cal-link{font-family:inherit;border:none;background:none;color:var(--accent);cursor:pointer;font-size:.9rem}
    .cal-err{color:#e05c5c;font-size:.85rem;margin-top:8px;min-height:1em}
    .cal-bar{display:flex;justify-content:space-between;align-items:center;margin:16px 0 8px;font-size:.86rem}
    .cal-form{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 14px}
    .cal-form input,.cal-form select{padding:9px 11px;border:1px solid var(--line);border-radius:8px;font-family:inherit;font-size:.9rem}
    .cal-form #cal-title{flex:1;min-width:140px}.cal-form #cal-memo{flex:1;min-width:140px}
    .cal-legend{display:flex;flex-wrap:wrap;gap:12px;margin:6px 0 12px;font-size:.8rem;color:var(--muted)}
    .cal-lg{display:inline-flex;align-items:center;gap:5px}.cal-lg i{width:11px;height:11px;border-radius:3px;display:inline-block}
    .cal-nav{display:flex;justify-content:space-between;align-items:center;margin:6px 0 10px;font-family:'Lora',serif;font-size:1.1rem}
    .cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}
    .cal-dow{text-align:center;font-size:.78rem;color:var(--muted);padding:4px 0;font-weight:500}
    .cal-cell{min-height:84px;border:1px solid var(--line);border-radius:8px;padding:5px;background:#fff;overflow:hidden}
    .cal-empty{background:transparent;border:none}
    .cal-today{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
    .cal-d{font-size:.78rem;color:var(--muted);margin-bottom:3px}
    .cal-ev{display:flex;justify-content:space-between;align-items:center;gap:3px;color:#fff;font-size:.72rem;border-radius:5px;padding:2px 5px;margin-bottom:3px;line-height:1.25}
    .cal-ev span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .cal-del{border:none;background:rgba(0,0,0,.18);color:#fff;border-radius:4px;cursor:pointer;font-size:.7rem;line-height:1;padding:1px 4px}
    @media(max-width:640px){.cal-cell{min-height:62px}.cal-ev{font-size:.66rem}}
    .domains{display:flex;gap:7px;flex-wrap:wrap;margin:18px 0 4px}
    .dombtn{font-family:inherit;font-size:.86rem;font-weight:500;padding:8px 15px;border:1px solid var(--line);background:#fff;border-radius:9px;cursor:pointer;color:var(--ink)}
    .dombtn:hover{border-color:var(--accent)}
    .dombtn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
    .dombtn .n{opacity:.65;font-size:.78rem;margin-left:2px}
    .axis{display:flex;margin:10px 0 6px}
    .axbtn{font-family:inherit;font-size:.82rem;padding:6px 16px;border:1px solid var(--line);background:#fff;cursor:pointer;color:var(--muted)}
    .axbtn:first-child{border-radius:9px 0 0 9px}
    .axbtn:last-child{border-radius:0 9px 9px 0;border-left:none}
    .axbtn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
    .tabs{display:flex;gap:7px;flex-wrap:wrap;margin:6px 0 12px}
    .tab{font-family:inherit;font-size:.84rem;padding:7px 13px;border:1px solid var(--line);background:#fff;border-radius:20px;cursor:pointer;color:var(--ink)}
    .tab:hover{border-color:var(--accent)}
    .tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
    .tab .n{opacity:.65;font-size:.76rem;margin-left:2px}
    .more-btn{display:block;width:100%;margin-top:10px;padding:12px;border:1px dashed var(--line);background:#fff;border-radius:10px;cursor:pointer;font-family:inherit;font-size:.9rem;color:var(--accent)}
    .more-btn:hover{border-color:var(--accent);background:var(--soft)}
    footer{margin-top:46px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:.78rem}
    """

    js = """
    const ITEMS = __DATA__;
    const DOMAINS = __DOMAINS__;
    const RESOURCES = __RESOURCES__;
    const FB = __FIREBASE__;
    const CATS = __CATS__;
    const PAGE = 30;
    const TOPIC_ORDER=['유전·진단','성장·발달','신경·인지·행동','종양·감시','합병증·동반질환','치료·관리','기전·기초연구','기타','미분류'];
    const STAGE_ORDER=['리뷰','관찰연구','초기임상','후기임상','전임상','사례보고','기타','미분석'];
    let domainF='all', axis='topic', tabVal='전체', typeF='all', yearF='all', shown=PAGE;
    const listEl=document.getElementById('list');
    const countEl=document.getElementById('countbar');
    const tabsEl=document.getElementById('tabs');
    const axisEl=document.getElementById('axis');
    const domainsEl=document.getElementById('domains');
    const qEl=document.getElementById('q');
    const typeEl=document.getElementById('type');
    const yearEl=document.getElementById('year');
    const moreEl=document.getElementById('more');
    function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
    function domainOf(it){return it.domain||'sotos';}
    function inDomain(it){return domainF==='all'||domainOf(it)===domainF;}
    function stageOf(it){return it.has_ai && it.stage ? it.stage : '미분석';}
    function topicOf(it){return it.has_ai && it.topic ? it.topic : '미분류';}
    function keyOf(it){return axis==='topic' ? topicOf(it) : stageOf(it);}
    function orderFor(){return axis==='topic' ? TOPIC_ORDER : STAGE_ORDER;}
    function yearOf(it){return String(it.date||'').slice(0,4);}
    const years=[...new Set(ITEMS.map(yearOf).filter(Boolean))].sort().reverse();
    yearEl.innerHTML="<option value='all'>전체 연도</option>"+years.map(y=>`<option value="${y}">${y}</option>`).join('');
    function buildAxis(){
      axisEl.innerHTML=
        `<button class="axbtn${axis==='topic'?' active':''}" data-a="topic">주제별</button>`+
        `<button class="axbtn${axis==='stage'?' active':''}" data-a="stage">단계별</button>`;
      axisEl.querySelectorAll('.axbtn').forEach(b=>b.onclick=()=>{
        axis=b.dataset.a; tabVal='전체'; shown=PAGE; buildAxis(); buildTabs(); render();});
    }
    function buildDomains(){
      const counts={}; ITEMS.forEach(it=>{const d=domainOf(it);counts[d]=(counts[d]||0)+1;});
      let h=`<button class="dombtn${domainF==='all'?' active':''}" data-d="all">전체 영역 <span class="n">${ITEMS.length}</span></button>`;
      h+=Object.keys(DOMAINS).map(d=>`<button class="dombtn${domainF===d?' active':''}" data-d="${esc(d)}">${esc(DOMAINS[d])} <span class="n">${counts[d]||0}</span></button>`).join('');
      domainsEl.innerHTML=h;
      domainsEl.querySelectorAll('.dombtn').forEach(b=>b.onclick=()=>{
        domainF=b.dataset.d; tabVal='전체'; shown=PAGE; buildDomains(); buildTabs(); render();});
    }
    function buildTabs(){
      const pool=ITEMS.filter(inDomain);
      const counts={}; pool.forEach(it=>{const k=keyOf(it);counts[k]=(counts[k]||0)+1;});
      const order=orderFor();
      const keys=Object.keys(counts).sort((a,b)=>{
        const ia=order.indexOf(a),ib=order.indexOf(b);return (ia<0?99:ia)-(ib<0?99:ib);});
      let h=`<button class="tab${tabVal==='전체'?' active':''}" data-v="전체">전체 <span class="n">${pool.length}</span></button>`;
      h+=keys.map(k=>`<button class="tab${tabVal===k?' active':''}" data-v="${esc(k)}">${esc(k)} <span class="n">${counts[k]}</span></button>`).join('');
      tabsEl.innerHTML=h;
      tabsEl.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{tabVal=b.dataset.v;shown=PAGE;buildTabs();render();});
    }
    function card(it){
      const src = it.type==='pubmed' ? 'PubMed' : 'ClinicalTrials.gov';
      const meta = it.type==='pubmed'
        ? `${esc(it.meta.journal||'')} · ${esc(it.meta.authors||'')} · ${esc(it.date)}`
        : `상태:${esc(it.meta.status||'')} · 단계:${esc(it.meta.phase||'')} · 갱신:${esc(it.date)}`;
      const badge = (it.stage?`<span class="badge">${esc(it.stage)}</span>`:'')
                  + (it.topic?`<span class="badge badge-topic">${esc(it.topic)}</span>`:'');
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
    function filtered(){
      const q=qEl.value.trim().toLowerCase();
      return ITEMS.filter(it=>{
        if(!inDomain(it)) return false;
        if(tabVal!=='전체' && keyOf(it)!==tabVal) return false;
        if(typeF!=='all' && it.type!==typeF) return false;
        if(yearF!=='all' && yearOf(it)!==yearF) return false;
        if(q && !((it.title+' '+it.summary+' '+it.relevance).toLowerCase().includes(q))) return false;
        return true;
      }).sort((a,b)=>String(b.date).localeCompare(String(a.date)));
    }
    function render(){
      const rows=filtered();
      const page=rows.slice(0,shown);
      countEl.textContent=`${rows.length}건 중 ${page.length}건 표시`;
      listEl.innerHTML=page.map(card).join('')||'<p class="muted">조건에 맞는 항목이 없습니다.</p>';
      if(rows.length>shown){moreEl.style.display='block';moreEl.textContent=`더 보기 (남은 ${rows.length-shown}건)`;}
      else{moreEl.style.display='none';}
    }
    qEl.addEventListener('input',()=>{shown=PAGE;render();});
    typeEl.addEventListener('change',()=>{typeF=typeEl.value;shown=PAGE;render();});
    yearEl.addEventListener('change',()=>{yearF=yearEl.value;shown=PAGE;render();});
    moreEl.addEventListener('click',()=>{shown+=PAGE;render();});
    // ----- 상단 섹션 전환 (연구 / 국내 실용자료 / 캘린더) -----
    const sectionsEl=document.getElementById('sections');
    const SECS=[['research','연구',''],['resources','국내 실용자료',RESOURCES.length],['calendar','캘린더','']];
    let calInited=false;
    function showSection(key){
      sectionsEl.querySelectorAll('.secbtn').forEach(x=>x.classList.toggle('active',x.dataset.s===key));
      SECS.forEach(([k])=>{const el=document.getElementById('sec-'+k); if(el) el.style.display=(k===key?'block':'none');});
      if(key==='calendar' && !calInited){calInited=true; initCalendar();}
    }
    function buildSections(){
      sectionsEl.innerHTML=SECS.map(([k,label,n])=>
        `<button class="secbtn${k==='research'?' active':''}" data-s="${k}">${esc(label)}${n!==''?` <span class="n">${n}</span>`:''}</button>`).join('');
      sectionsEl.querySelectorAll('.secbtn').forEach(b=>b.onclick=()=>showSection(b.dataset.s));
    }
    function renderResources(){
      const el=document.getElementById('reslist');
      if(!RESOURCES.length){el.innerHTML='<p class="muted">아직 수집된 국내 자료가 없습니다. CONFIG의 resource_sources에 신뢰하는 공식 페이지를 추가하세요.</p>';return;}
      el.innerHTML=RESOURCES.map(r=>{
        const tips=(r.tips||[]).map(t=>`<li>${esc(t)}</li>`).join('');
        const body=r.has_ai
          ? `<p class="summary">${esc(r.summary)}</p>`+
            (r.relevance?`<p class="relevance"><b>도움 포인트:</b> ${esc(r.relevance)}</p>`:'')+
            (tips?`<div class="qbox"><b>활용 팁</b><ul>${tips}</ul></div>`:'')
          : `<p class="summary muted">정리 대기 중 — 원문 링크 참고</p>`;
        return `<article class="item"><div class="item-head"><span class="src src-res">${esc(r.source)}</span>`+
          (r.date?`<span class="badge">${esc(r.date)}</span>`:'')+`</div>`+
          `<h3><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a></h3>${body}</article>`;
      }).join('');
    }
    // ----- 공유 캘린더 (Firebase) -----
    const CATCOLOR={'인지':'#5b8def','운동':'#2bb673','자조':'#e8923b','언어':'#9b6fd6','병원':'#e05c5c','기타':'#8a8f99'};
    let fbAuth=null, fbDB=null, calUser=null, calEvents={}, calMonth=new Date();
    function calEl(id){return document.getElementById(id);}
    function initCalendar(){
      const setup=calEl('cal-setup'), login=calEl('cal-login'), app=calEl('cal-app');
      if(!FB){ setup.style.display='block'; login.style.display='none'; app.style.display='none'; return; }
      setup.style.display='none';
      try{
        if(!firebase.apps.length) firebase.initializeApp(FB);
        fbAuth=firebase.auth(); fbDB=firebase.database();
      }catch(e){ setup.style.display='block'; setup.innerHTML='<p class="muted">Firebase 초기화 오류: '+esc(e.message)+'</p>'; return; }
      fbAuth.onAuthStateChanged(u=>{
        calUser=u;
        if(u){ login.style.display='none'; app.style.display='block'; calEl('cal-who').textContent=u.email; subscribeCal(); buildCatOptions(); }
        else { login.style.display='block'; app.style.display='none'; }
      });
      calEl('cal-login-btn').onclick=()=>{
        const em=calEl('cal-email').value.trim(), pw=calEl('cal-pw').value;
        calEl('cal-err').textContent='';
        fbAuth.signInWithEmailAndPassword(em,pw).catch(e=>{calEl('cal-err').textContent='로그인 실패: '+e.message;});
      };
      calEl('cal-pw').addEventListener('keydown',e=>{if(e.key==='Enter')calEl('cal-login-btn').click();});
      calEl('cal-logout').onclick=()=>fbAuth.signOut();
      calEl('cal-prev').onclick=()=>{calMonth.setMonth(calMonth.getMonth()-1);renderCal();};
      calEl('cal-next').onclick=()=>{calMonth.setMonth(calMonth.getMonth()+1);renderCal();};
      calEl('cal-add').onclick=addEventFromForm;
    }
    function buildCatOptions(){
      calEl('cal-cat').innerHTML=CATS.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');
    }
    function subscribeCal(){
      fbDB.ref('calendar/events').on('value',snap=>{calEvents=snap.val()||{};renderCal();},
        err=>{calEl('cal-app').insertAdjacentHTML('afterbegin','<p class="muted">읽기 권한 오류(보안 규칙 확인): '+esc(err.message)+'</p>');});
    }
    function addEventFromForm(){
      const date=calEl('cal-date').value, time=calEl('cal-time').value, title=calEl('cal-title').value.trim(), cat=calEl('cal-cat').value, memo=calEl('cal-memo').value.trim();
      if(!date||!title){alert('날짜와 제목은 필수입니다.');return;}
      fbDB.ref('calendar/events').push({date,time,title,cat,memo,by:calUser.email,ts:Date.now()})
        .then(()=>{calEl('cal-title').value='';calEl('cal-memo').value='';})
        .catch(e=>alert('저장 실패(권한 확인): '+e.message));
    }
    function delEvent(id){ if(confirm('이 일정을 삭제할까요?')) fbDB.ref('calendar/events/'+id).remove().catch(e=>alert('삭제 실패: '+e.message)); }
    window.__delEvent=delEvent;
    function ymd(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
    function renderCal(){
      const y=calMonth.getFullYear(), m=calMonth.getMonth();
      calEl('cal-label').textContent=`${y}년 ${m+1}월`;
      const byDate={};
      Object.keys(calEvents).forEach(id=>{const e=calEvents[id]; (byDate[e.date]=byDate[e.date]||[]).push({id,...e});});
      const first=new Date(y,m,1), start=first.getDay(), days=new Date(y,m+1,0).getDate(), today=ymd(new Date());
      let cells='';
      for(let i=0;i<start;i++) cells+='<div class="cal-cell cal-empty"></div>';
      for(let d=1;d<=days;d++){
        const ds=`${y}-${String(m+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
        const evs=(byDate[ds]||[]).sort((a,b)=>(a.time||'').localeCompare(b.time||''));
        const chips=evs.map(e=>`<div class="cal-ev" style="background:${CATCOLOR[e.cat]||'#8a8f99'}" title="${esc(e.title)} ${esc(e.memo||'')} (${esc(e.by||'')})">`+
          `<span>${e.time?esc(e.time)+' ':''}${esc(e.title)}</span>`+
          `<button class="cal-del" onclick="__delEvent('${e.id}')">×</button></div>`).join('');
        cells+=`<div class="cal-cell${ds===today?' cal-today':''}"><div class="cal-d">${d}</div>${chips}</div>`;
      }
      calEl('cal-grid').innerHTML=
        '<div class="cal-dow">일</div><div class="cal-dow">월</div><div class="cal-dow">화</div><div class="cal-dow">수</div><div class="cal-dow">목</div><div class="cal-dow">금</div><div class="cal-dow">토</div>'+cells;
      // 카테고리 범례
      calEl('cal-legend').innerHTML=CATS.map(c=>`<span class="cal-lg"><i style="background:${CATCOLOR[c]||'#8a8f99'}"></i>${esc(c)}</span>`).join('');
    }
    buildSections();
    renderResources();
    buildDomains();
    buildAxis();
    buildTabs();
    render();
    """
    js = (js.replace("__DATA__", data_json)
            .replace("__DOMAINS__", json.dumps(DOMAIN_LABELS, ensure_ascii=False))
            .replace("__RESOURCES__", res_json)
            .replace("__FIREBASE__", fb_json)
            .replace("__CATS__", cats_json))

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
        "<div class='sections' id='sections'></div>"
        "<div id='sec-research'>"
        f"{synth_html}"
        "<div class='domains' id='domains'></div>"
        "<div class='axis' id='axis'></div>"
        "<div class='tabs' id='tabs'></div>"
        "<div class='controls'><input id='q' placeholder='검색어 (제목·요약 내)'>"
        "<select id='type'><option value='all'>전체 종류</option>"
        "<option value='pubmed'>논문</option><option value='trial'>임상시험</option></select>"
        "<select id='year'></select></div>"
        "<div class='countbar' id='countbar'></div><div id='list'></div>"
        "<button class='more-btn' id='more' style='display:none'></button>"
        "</div>"
        "<div id='sec-resources' style='display:none'>"
        "<p class='res-intro'>국내 공식 사이트에서 robots.txt를 지키며 모은 실용 자료입니다. 원문 확인은 각 링크에서.</p>"
        "<div id='reslist'></div></div>"
        "<div id='sec-calendar' style='display:none'>"
        "<div id='cal-setup' style='display:none' class='cal-setup'>"
        "<b>공유 캘린더를 쓰려면 Firebase 설정이 필요합니다.</b><br>"
        "스크립트 CONFIG의 <code>firebase_config</code>에 Firebase 웹 앱 설정을 넣고, Realtime Database 보안 규칙으로 두 분만 접근하도록 잠그세요. 자세한 절차는 대화의 안내를 참고하세요.</div>"
        "<div id='cal-login' style='display:none' class='cal-login'>"
        "<h3>로그인</h3><p class='muted'>두 분만 접근할 수 있는 비공개 일정입니다.</p>"
        "<input id='cal-email' type='email' placeholder='이메일' autocomplete='username'>"
        "<input id='cal-pw' type='password' placeholder='비밀번호' autocomplete='current-password'>"
        "<button id='cal-login-btn' class='cal-btn'>로그인</button>"
        "<div id='cal-err' class='cal-err'></div></div>"
        "<div id='cal-app' style='display:none'>"
        "<div class='cal-bar'><span class='muted'>로그인: <b id='cal-who'></b></span>"
        "<button id='cal-logout' class='cal-link'>로그아웃</button></div>"
        "<div class='cal-form'>"
        "<input id='cal-date' type='date'><input id='cal-time' type='time'>"
        "<select id='cal-cat'></select>"
        "<input id='cal-title' placeholder='일정 (예: 인지치료)'>"
        "<input id='cal-memo' placeholder='메모 (선택)'>"
        "<button id='cal-add' class='cal-btn'>추가</button></div>"
        "<div id='cal-legend' class='cal-legend'></div>"
        "<div class='cal-nav'><button id='cal-prev' class='cal-link'>‹ 이전</button>"
        "<b id='cal-label'></b><button id='cal-next' class='cal-link'>다음 ›</button></div>"
        "<div id='cal-grid' class='cal-grid'></div></div>"
        "</div>"
        "<footer>누적 기록: data/knowledge_base.jsonl · data/resources.jsonl · 설정은 스크립트 CONFIG · v3.0</footer>"
        + (("<script src='https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js'></script>"
            "<script src='https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js'></script>"
            "<script src='https://www.gstatic.com/firebasejs/10.12.2/firebase-database-compat.js'></script>") if fb_enabled else "")
        + f"<script>{js}</script></div></body></html>"
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
            "questions_for_doctor": ["이 변이 유형이 우리 아이와 관련 있나요?"], "topic": "유전·진단"}, "domain": "sotos"},
    {"type": "trial", "id": "NCTDEMO", "title": "Sample: Growth patterns in Sotos syndrome",
     "url": "https://clinicaltrials.gov/", "date": "2026-05-01",
     "meta": {"status": "RECRUITING", "phase": "N/A", "conditions": "Sotos Syndrome"},
     "raw_text": "Sample summary.",
     "ai": {"summary_3lines": "샘플 임상시험 요약.", "study_stage": "관찰연구",
            "relevance": "예시.", "questions_for_doctor": ["참여 조건이 궁금합니다."], "topic": "성장·발달"}, "domain": "sotos"},
    {"type": "pubmed", "id": "DEMO3", "title": "Sample: Occupational therapy for self-care in developmental delay",
     "url": "https://pubmed.ncbi.nlm.nih.gov/", "date": "2025",
     "meta": {"journal": "Demo Peds Journal", "authors": "Kim SA et al."},
     "raw_text": "Sample therapy abstract.",
     "ai": {"summary_3lines": "샘플 발달치료 요약.", "study_stage": "관찰연구",
            "relevance": "자조기술 훈련 관련 예시.", "questions_for_doctor": ["가정에서 적용할 수 있나요?"], "topic": "치료·관리"}, "domain": "therapy"},
]
DEMO_SYNTH = {"overview": "이것은 오프라인 미리보기용 샘플 종합 분석입니다.",
              "themes": [{"title": "유전형-표현형", "detail": "샘플 주제 설명."},
                         {"title": "성장 관리", "detail": "샘플 주제 설명."}],
              "recent_developments": "샘플 동향.", "questions_for_doctor": ["샘플 질문 1", "샘플 질문 2"],
              "generated_at": dt.datetime.now().isoformat(), "based_on": 2}
DEMO_RESOURCES = [
    {"source": "국립재활원 재활정보포털", "title": "장애아동 질병과 재활치료", "url": "https://www.nrc.go.kr/",
     "date": "2026-06-01", "ai": {"summary_3lines": "샘플 자료 요약입니다.",
     "relevance": "자조·운동 훈련 관련 예시.", "tips": ["가정에서 일상생활동작 반복 연습"]}},
]


def main():
    run_time = dt.datetime.now()

    # ----- 건수만 -----
    if "--count" in sys.argv:
        log("건수만 확인 (수집·요약·비용 없음)…")
        total = 0
        print("\n===== 영역별 전체 건수 =====")
        for st in STREAMS:
            try: n_pub = count_pubmed(st["pubmed_query"], st["backfill_lookback_days"])
            except Exception as e: n_pub = None; log(f"  [{st['label']}] PubMed 실패: {e}")
            try: n_trial = count_trials(st["ctgov_condition"], st["ctgov_term"])
            except Exception as e: n_trial = None; log(f"  [{st['label']}] ClinicalTrials 실패: {e}")
            print(f"  [{st['label']}] 논문 {n_pub if n_pub is not None else '실패'} · "
                  f"임상 {n_trial if n_trial is not None else '실패'}")
            if isinstance(n_pub, int): total += n_pub
            if isinstance(n_trial, int): total += n_trial
        print(f"  합계: 약 {total} 건")
        return

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    # ----- 데모 -----
    if "--demo" in sys.argv:
        log("DEMO 모드: 샘플로 대시보드만 생성합니다.")
        INDEX_PATH.write_text(render_dashboard(DEMO_ITEMS, DEMO_SYNTH, run_time, DEMO_RESOURCES), encoding="utf-8")
        log(f"대시보드 생성 → {INDEX_PATH}")
        return

    # ----- 국내 실용자료만 (재)수집 -----
    if "--resources" in sys.argv:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        seen = load_seen()
        seen_urls = set(seen.get("resources", []))
        log("국내 실용자료 수집 중…")
        new_res = collect_resources(api_key, seen_urls)
        if new_res:
            append_resources(new_res)
            seen["resources"] = list(seen_urls)
            save_seen(seen)
        log(f"국내 실용자료 신규 {len(new_res)}건")
        synth = json.loads(SYNTH_PATH.read_text(encoding="utf-8")) if SYNTH_PATH.exists() else None
        INDEX_PATH.write_text(render_dashboard(load_kb(), synth, run_time, load_resources()), encoding="utf-8")
        log(f"대시보드 재발행 → {INDEX_PATH}")
        return

    # ----- 기존 항목 주제 분류 (일회성) -----
    if "--retag" in sys.argv:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log("! ANTHROPIC_API_KEY가 필요합니다 (--retag).")
            return
        items = load_kb()
        todo = [it for it in items if it.get("ai") and not (it["ai"] or {}).get("topic")]
        log(f"주제 분류 대상: {len(todo)}건 (이미 분류된 항목은 건너뜀)")
        id2topic = {}
        bs = 20
        for start in range(0, len(todo), bs):
            res = ai_classify_batch(todo[start:start + bs], api_key)
            for k, v in res.items():
                id2topic[str(k)] = v if v in TOPICS else "기타"
            log(f"  분류 진행: {min(start + bs, len(todo))}/{len(todo)}")
            time.sleep(0.5)
        applied = 0
        for it in items:
            t = id2topic.get(str(it["id"]))
            if t and it.get("ai"):
                it["ai"]["topic"] = t
                applied += 1
        write_kb(items)
        log(f"주제 분류 적용: {applied}건")
        synth = None
        if SYNTH_PATH.exists():
            try: synth = json.loads(SYNTH_PATH.read_text(encoding="utf-8"))
            except Exception: synth = None
        INDEX_PATH.write_text(render_dashboard(items, synth, run_time, load_resources()), encoding="utf-8")
        log(f"대시보드 재발행 → {INDEX_PATH}  (전체 {len(items)}건)")
        return

    backfill = "--backfill" in sys.argv
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    use_ai = CONFIG["use_ai_summary"] and bool(api_key)
    if backfill:
        log("BACKFILL 모드: 각 영역의 전체 범위를 분석합니다 (시간·비용이 한 번에 발생).")

    # --- 수집 (영역별 스트림 반복) ---
    seen = load_seen()
    seen_pmids, seen_ncts = set(seen["pmids"]), set(seen["ncts"])
    added = set()           # 이번 실행에서 이미 담은 id (영역 간 중복 방지)
    all_new = []
    for st in STREAMS:
        lb = st["backfill_lookback_days"] if backfill else st["lookback_days"]
        log(f"[{st['label']}] PubMed 수집 중…")
        for it in fetch_pubmed(st["pubmed_query"], lb):
            if it["id"] in seen_pmids or it["id"] in added:
                continue
            it["domain"] = st["id"]; all_new.append(it); added.add(it["id"])
        log(f"[{st['label']}] ClinicalTrials.gov 수집 중…")
        for it in fetch_trials(st["ctgov_condition"], st["ctgov_term"], lb):
            if it["id"] in seen_ncts or it["id"] in added:
                continue
            it["domain"] = st["id"]; all_new.append(it); added.add(it["id"])
    new_pubmed = [i for i in all_new if i["type"] == "pubmed"]
    new_trials = [i for i in all_new if i["type"] == "trial"]
    log(f"신규 항목: 논문 {len(new_pubmed)} · 임상시험 {len(new_trials)}")

    # --- 처리 대상 결정 (백필=전부 / 평소=상한) ---
    cap = 0 if backfill else CONFIG.get("ai_summary_max_per_run", 0)
    if use_ai and cap and len(all_new) > cap:
        to_process = all_new[:cap]
        log(f"신규 {len(all_new)}건 중 이번엔 {cap}건만 분석(나머지는 다음 실행 때).")
    else:
        to_process = all_new

    # --- 항목별 AI 분석 + 중간 저장 ---
    # 긴 백필 중 중단(절전·끊김 등)돼도 진행분이 보존되고, --backfill을 다시 실행하면
    # 이미 끝낸 항목은 건너뛰고 남은 것부터 이어서 처리합니다.
    def flush(batch):
        if not batch:
            return
        for it in batch:
            it["fetched_at"] = run_time.isoformat()
        append_kb(batch)
        seen["pmids"].extend(i["id"] for i in batch if i["type"] == "pubmed")
        seen["ncts"].extend(i["id"] for i in batch if i["type"] == "trial")
        save_seen(seen)

    if use_ai and to_process:
        log(f"AI 분석 중… ({len(to_process)}건)")
        pending = []
        for idx, it in enumerate(to_process, 1):
            it["ai"] = ai_summarize(it, api_key)
            pending.append(it)
            if idx % 25 == 0:                     # 25건마다 중간 저장
                flush(pending); pending = []
                log(f"  분석·저장 진행: {idx}/{len(to_process)}")
            time.sleep(0.4)
        flush(pending)                            # 남은 것 저장
    else:
        if CONFIG["use_ai_summary"] and not api_key:
            log("! ANTHROPIC_API_KEY 미설정 → AI 분석 건너뜀(원문만, 비용 0).")
        flush(to_process)                         # AI 없이도 수집분은 저장

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

    # --- 국내 실용자료 수집 (선택·격리: 실패해도 본 시스템에 영향 없음) ---
    if CONFIG.get("resource_sources"):
        try:
            seen_urls = set(seen.get("resources", []))
            log("국내 실용자료 수집 중…")
            new_res = collect_resources(api_key, seen_urls)
            if new_res:
                append_resources(new_res)
                seen["resources"] = list(seen_urls)
                save_seen(seen)
                log(f"국내 실용자료 신규 {len(new_res)}건")
        except Exception as e:
            log(f"! 국내 실용자료 단계 오류(무시): {e}")

    # --- 대시보드 발행 ---
    INDEX_PATH.write_text(render_dashboard(all_items, synth, run_time, load_resources()), encoding="utf-8")
    log(f"대시보드 발행 → {INDEX_PATH}  (연구 {len(all_items)}건)")


if __name__ == "__main__":
    main()
