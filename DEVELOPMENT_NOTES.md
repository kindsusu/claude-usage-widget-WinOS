# 개발 노트 — Claude Usage Widget

제작 과정에서 실제로 부딪힌 문제와 해결책, 그리고 **되돌아가지 말아야 할 폐기된 접근**을 기록한다. 같은 실수를 반복하지 않기 위한 문서.

---

## 1. 데이터 소스 — 어떻게 실시간 사용량을 얻는가

### 폐기한 접근 (다시 시도 금지)
- **ccusage USD 추정**: `ccusage`가 뱉는 비용(USD)으로 한도를 역산 → Anthropic의 실제 한도 단위(메시지/가중치 기반)와 무관해서 claude.ai 화면과 최대 40%p까지 어긋남. 근본적으로 불가능.
- **Claude Code statusLine 후킹**: `settings.json`의 statusLine에 스크립트를 물려 stdin JSON을 가로채는 방식. 두 가지로 실패 —
  1. statusLine은 **Git Bash로 실행**되어 `C:\Users\...` 백슬래시가 escape 문자로 깨짐. `.bat`은 bash가 실행 못 함. POSIX 경로(`/c/Users/...`)로 우회해야 겨우 됨.
  2. **Claude Desktop(GUI)에서는 statusLine 자체가 발동 안 함**. cmd의 CLI 세션에서만 트리거됨.

### 확정한 방식
```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <~/.claude/.credentials.json 의 claudeAiOauth.accessToken>
anthropic-beta: oauth-2025-04-20
```
- claude.ai/settings/usage와 **동일한 데이터**. 비공식이지만 안정적.
- 응답: `five_hour.utilization`, `seven_day.utilization`, `extra_usage.is_enabled`, 각 `resets_at`.
- 나중에 API가 모델별 스코프를 `limits[]` 배열로 옮김 → `weekly_scoped` 항목의 `display_name`("Fable" 등)으로 모델별 주간 한도를 읽음.

---

## 2. OAuth 토큰 — 자동 갱신

- **access token**은 ~8시간. 만료 시 **refresh token**으로 직접 갱신:
  `POST https://platform.claude.com/v1/oauth/token`,
  `client_id = 9d1c250a-e61b-44d9-88ed-5944d1962f5e` (공개, 모든 설치 공통).
- **핵심 함정 — refresh token 로테이션**: 응답에 새 refresh token이 오면 **반드시 저장**해야 함. 안 하면 다음 갱신이 `400 invalid_grant`. 커뮤니티 #1 버그.
- **동시 쓰기 위험**: Claude Code가 자체적으로 토큰을 갱신하며 파일을 다시 씀. 갱신 시 **락 안에서 파일을 재읽기**해서 이미 갱신됐으면 그것을 사용.
- **usage rate limit과 별개 엔드포인트** — 자동 갱신이 429에 기여하지 않음.
- refresh token까지 만료(수개월~1년, 드묾)되면 로그인 다이얼로그를 띄워 `claude auth login`(← `claude login` 아님, 서브커맨드 정확히) 실행. 컴퓨터를 며칠 꺼놨다 켜도 refresh token만 살아있으면 자동 복구.

---

## 3. Rate limit (429) — 정책 규명

- 응답에 `Retry-After` 헤더(관측값 38분, ~3600초)가 옴. `X-RateLimit-*`는 없음.
- **sliding window**로 확인됨: 호출할 때마다 unlock 시각이 `지금 + Retry-After`로 밀림. 한번 막히면 **호출을 자제해야** 풀림.
- 위젯 대응: 기본 폴링 주기를 넉넉히(수 분), 표시는 **절대 시각**("resets HH:MM")으로 — 카운트다운은 새로고침마다 값이 바뀌어 혼란.

---

## 4. 미니 모드 (아이폰 배터리 스트립) — 가장 오래 고생한 부분

### tkinter의 근본 한계
- **픽셀 알파(반투명) 불가**. 캡슐 배경을 `stipple="gray50"`로 반투명 흉내 → "모자이크"로 보여 폐기. 진짜 반투명은 `UpdateLayeredWindow` 전면 재작성만이 유일 — 위젯 규모 대비 과함, **재시도 금지**.

### 떠다니는 글자(S/W/F 라벨)의 프린지 지옥
- 배경 없는 투명 스트립 위 글자를 `create_text`로 그리면, **ClearType 안티앨리어싱이 투명 키색과 섞여** 가장자리에 테두리가 생김.
  - 키가 검정(`#010203`)이면 → 밝은 배경에서 **검은 테두리**.
  - 키가 흰색(`#fdfdfb`)이면 → 어두운 배경에서 **흰 테두리**.
  - 어떤 키색을 골라도 반대 밝기 배경에서 반드시 노출됨 = 구조적 문제.
- **최종 해결 (절대 되돌리지 말 것)**: 라벨을 **PIL로 렌더한 하드엣지 이미지**로 교체. 알파를 0/255로 임계값 처리 → 반투명 가장자리 픽셀이 **아예 없어** 어떤 배경에서도 프린지 불가능. `make_label_image()` + 캐시.
- 폐기: 글자 뒤 칩(배경 사각형), 글자를 배터리 안에 넣기, 캡슐 — 사용자가 순차 거부. 최종은 **배경 없는 플로팅 + 하드엣지 라벨**.

### 잔량 블록 상하 여백 비대칭
- 원인 1: smooth 폴리곤의 둥근 모서리가 채움을 안쪽으로 굽힘 → 각진 `create_rectangle`으로 교체.
- 원인 2: **Tk `create_rectangle`의 y2/x2가 배타적(exclusive)** — 채움이 아래쪽 1px 짧음. `+1` 보정으로 상하 대칭 달성. bbox 검사는 근사값이라 이 1px를 못 잡음 → **실화면 ImageGrab 픽셀 측정으로만** 검증.

### 확정 스펙 (변경 시 사용자 명시 지시 필요 — [[feedback-no-unsolicited-design-changes]])
- 배터리 = **잔량(100−사용량)**, 잔량 ≤20% → 빨강. S=연두 W=파랑 F(Fable)=보라.
- 라벨 잉크 가장자리가 배터리 body에서 정확히 S(5) (`body_x - S(5) + 2`, +2는 이미지 패드 상쇄).
- 투명 키 = `#fdfdfb` 양 테마 고정 (정확 일치 픽셀만 투명 → 다크 fg와 충돌 없음).

---

## 5. Windows 창 동작

- **작업표시줄 버튼 방지**: `overrideredirect(True)`만으론 불충분 — withdraw/deiconify(트레이 숨김/복원) 후 Windows가 `WS_EX_APPWINDOW`를 다시 붙임. `WS_EX_TOOLWINDOW` 강제 + `APPWINDOW` 제거를 **시작·복원·미니전환 후마다** 재적용.

### 표시 상태 모델

- 데스크톱 위젯은 `desktop_mode` 하나로 `normal` / `mini` / `hidden` 중 정확히 하나만 저장한다.
- 작업표시줄 위젯은 `taskbar_visible`로 별도 관리하며 데스크톱 상태와 독립적이다.
- 예전 설정의 `minimized`는 최초 로드 시 `desktop_mode`로 옮기고, 구버전 호환을 위해 이후에도 미러링한다.
- 작업표시줄을 왼쪽 클릭해 숨긴 뒤 다시 클릭하면 `desktop_restore_mode`에 저장된 마지막 일반/미니 모드로 복원한다.
- **smart topmost**: 전경 창이 관심 대상일 때만 위로.
  - **자기 자신은 PID(`os.getpid()`)로 식별** — 프로세스명 `pythonw.exe`로 매칭하면 다른 pythonw 앱(코덱스 위젯 등)까지 딸려 올라옴.
  - **터미널 호스트 감지**: cmd/PowerShell/Windows Terminal이 전경이고 그 **자식 프로세스에 claude가 있으면** claude 활성으로 간주 (CreateToolhelp32Snapshot 프로세스 트리). CLI를 터미널에서 돌릴 때도 위로.
  - z-order는 **전이(transition)일 때만** SetWindowPos 호출 (캐시). 매번 호출하면 메뉴 위로 위젯이 재상승하는 버그.
- **오프스크린 복구**: 저장된 x/y가 화면(가상 데스크탑 포함) 밖이면 시작 시 안쪽으로 클램프. 듀얼 모니터는 `winfo_vrootwidth()`로 인식.

---

## 6. 자잘하지만 물렸던 것들

- **`ImageTk.PhotoImage`는 `tk.Tk()` 생성 후에** 호출해야 함. 이전에 만들면 숨은 Tcl 인터프리터에 이미지가 묶여 진짜 root의 Label에 안 뜸 (펫이 첫 실행 때 안 보이던 원인).
- **실행.bat**: `start "" pythonw`가 App Paths 레지스트리 미등록 시 실패 → `where`/표준경로/재귀검색 폴백.
- **stdout 인코딩**: 콘솔 print에 한글·`·` 넣으면 cp949 에러. 파일 IO는 `encoding="utf-8"` 명시.

---

## 7. 작업 방식 (이 프로젝트에서 효과적이었던 것)

- **역할 분리**: 계획·설계·검수·지시 = 상위 모델이 직접, 코딩 = Opus 에이전트 위임. [[model-role-split]]
- **검증은 실화면으로**: canvas `bbox`는 근사값이라 픽셀 문제를 놓침. **PIL ImageGrab 실제 캡처**로 눈으로 확인해야 함. 특히 라벨 프린지·여백은 정적 검사로 안 잡힘.
- **에이전트 재개(SendMessage)의 함정**: 세션 한도 등으로 중단된 에이전트를 재개하면 **이전 pending 지시가 함께 실행**될 수 있음 (확정한 디자인이 뒤집힌 사고 발생). 재개 시 "이전 지시 무시" 명시하거나 파일 상태를 먼저 확인.
- **동시 에이전트 = 같은 파일 편집 위험**: 병렬로 띄운 두 에이전트가 같은 파일을 편집하면 중복 정의/충돌. 스폰 전 작업 범위를 겹치지 않게 나눌 것.
