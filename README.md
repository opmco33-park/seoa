# Sotos / NSD1 연구 추적 에이전트 — 설치·운영 가이드

딸의 유전질환(Sotos 증후군 / NSD1) 관련 논문·임상시험을 **매일 자동으로 수집·한국어 분석**하고,
**항상 최신 상태인 대시보드 한 페이지**로 보여주는 시스템입니다.

> ⚠️ **의학적 면책**: 이 도구는 검색·정리 보조용입니다. 요약·분석은 부정확할 수 있으며 진단·치료
> 판단이 아닙니다. 모든 내용은 원문과 담당 의료진을 통해 확인하세요.

> 🔒 **꼭 지킬 두 가지**
> 1. 무료 GitHub Pages를 쓰려면 저장소가 **공개(public)** 여야 합니다. 여기 쌓이는 건 공개된
>    논문·임상 데이터뿐입니다. **따님의 개인정보·의료기록은 절대 이 저장소에 올리지 마세요.**
> 2. **API 키는 코드에 넣지 말고** GitHub의 암호화된 **Secret**에만 넣으세요(아래 4단계).

---

## A. 먼저 집 PC에서 한 번 — 과거 20년치 백필

자동화 전에, 과거 20년치를 한 번에 분석해 기초 데이터를 만듭니다. (시간·비용이 한 번에 발생)

```bash
pip install requests
# (Windows PowerShell)  $env:ANTHROPIC_API_KEY="sk-본인키"
# (Mac/Linux)           export ANTHROPIC_API_KEY="sk-본인키"

python sotos_research_agent.py --backfill      # Mac은 python3
```

끝나면 `data/`(누적 기록)와 `docs/index.html`(대시보드)이 생깁니다.
`docs/index.html`을 더블클릭해 결과를 먼저 확인하세요.

> 비용이 부담되면 키 없이 `python sotos_research_agent.py --backfill` 로 원문만 무료 수집한 뒤,
> 이후 자동 실행에서 신규 항목만 AI 분석되게 할 수도 있습니다.

---

## B. GitHub에 올려 자동화하기

### 1단계 — 공개 저장소 만들기
GitHub에서 **New repository** → 이름(예: `sotos-research`) → **Public** 선택 → Create.

### 2단계 — 파일 올리기
다음을 저장소에 넣습니다(웹에서 드래그 업로드 또는 git push):
- `sotos_research_agent.py`  (루트)
- `.github/workflows/research.yml`  (← `research.yml`을 이 경로에 두기)
- `README.md`
- A단계에서 생성된 `data/` 폴더와 `docs/` 폴더 (백필 결과)

### 3단계 — GitHub Pages 켜기
저장소 **Settings → Pages → Build and deployment → Source: Deploy from a branch**
→ Branch: **main** / 폴더: **/docs** → Save.
잠시 뒤 `https://<아이디>.github.io/<저장소이름>/` 에서 대시보드가 열립니다. 이 주소를 북마크하세요(휴대폰에서도 열람).

### 4단계 — API 키를 Secret으로 등록
저장소 **Settings → Secrets and variables → Actions → New repository secret**
- Name: `ANTHROPIC_API_KEY`
- Secret: 본인 키(`sk-...`)
→ Add secret. (코드·로그에 노출되지 않습니다.)

### 5단계 — 첫 자동 실행 테스트
저장소 **Actions 탭 → "Sotos Research Agent" → Run workflow** 버튼으로 수동 실행해 봅니다.
초록 체크가 뜨면 성공. 이후에는 **매일 한국시간 아침 6시쯤 자동 실행**되어, 새 논문/임상이 있으면
대시보드가 갱신됩니다.

---

## C. 평소 운영
- 손댈 것 없습니다. 매일 자동으로 돌고, 새 항목이 있을 때만 분석·종합이 갱신됩니다.
- 검색어·실행주기·분석량을 바꾸려면 `sotos_research_agent.py` 상단 `CONFIG`만 수정.
- 비공개로 두고 싶다면: GitHub Pages 대신 집 PC에서 주기 실행(작업 스케줄러/cron)하고
  `docs/index.html`을 로컬로 여는 방식으로 쓸 수 있습니다(공개 불필요).

## 실행 모드 요약
| 명령 | 용도 |
|---|---|
| `--count` | 전체 건수만 확인 (비용 0) |
| `--demo` | 네트워크 없이 샘플 대시보드 |
| `--backfill` | 과거 20년치 전부 분석 (최초 1회) |
| (옵션 없음) | 최근 신규만 분석·갱신 (Actions가 매일 실행) |
