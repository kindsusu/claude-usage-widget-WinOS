# Pixel Pet SVG Spec

> 다른 Claude 세션에 이 파일 통째로 던지면 펫 SVG들 만들어줄 거예요.

## 목표

Claude Usage Widget(https://github.com/kindsusu/claude-usage-widget)의 펫을 SVG로 다시 만들기. 각 부위(꼬리·귀·날개 등)를 별도 `<g>` element로 분리해서, CSS 키프레임으로 부위별 애니메이션 가능하게.

현재 PNG 펫은 한 덩어리라 전체 움직임(bounce·sway·float 등)만 가능. SVG로 가면 꼬리 흔들기, 귀 까딱, 날개 파닥 같은 부위별 모션 가능.

---

## 만들어야 할 펫 21마리

| # | 펫 (영문) | 한글 | 전체 움직임 | 부위 움직임 | 권장 메인 색상 |
|---|---|---|---|---|---|
| 1 | cat | 고양이 | bounce (통통) | tail wag, ear wiggle | #f4a261 (오렌지 줄무늬) |
| 2 | dog | 강아지 | bounce | tail wag | #c19a6b (베이지) |
| 3 | fox | 여우 | slow bounce | tail wag | #e76f51 (다홍) |
| 4 | bear | 곰 | slow breathe | eye blink | #6f4e37 (갈색) |
| 5 | rabbit | 토끼 | short jump | ear wiggle | #f4f4f4 (흰색) |
| 6 | penguin | 펭귄 | sway (좌우 뒤뚱) | eye blink | #2b2d42 (검정) |
| 7 | pig | 돼지 | weak bounce | nose twitch | #ffb3ba (분홍) |
| 8 | chick | 병아리 | fast bounce | eye blink | #ffd60a (노랑) |
| 9 | panda | 팬더 | slow sway | eye blink | #ffffff/#000 |
| 10 | frog | 개구리 | squish + jump | eye blink | #95d5b2 (연두) |
| 11 | ghost | 유령 | float | body wobble | #ffffff (반투명 80%) |
| 12 | unicorn | 유니콘 | bounce | horn shine, mane wave | #c8b6ff (라벤더) |
| 13 | dragon | 드래곤 | bounce | wing flap, tail wag | #80b918 (녹색) |
| 14 | slime | 슬라임 | squish | full body squish | #00b4d8 (청록) |
| 15 | robot | 로봇 | tick (딱딱) | antenna blink | #adb5bd (회색) |
| 16 | alien | 외계인 | slow float | antenna wiggle | #38b000 (네온그린) |
| 17 | dino | 공룡 | bounce | tail wag | #7209b7 (보라) |
| 18 | bat | 박쥐 | hover | wing flap | #240046 (진보라) |
| 19 | monster | 괴물 | bounce | mouth open/close | #ff006e (마젠타) |
| 20 | devil | 악마 | sway | tail wag | #d00000 (빨강) |
| 21 | claudecode | Claude Code 마스코트 | bounce | (현재 SVG 유지) | #D97757 |

---

## SVG 구조 규칙 (필수)

### viewBox + 크기

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="200" height="200" class="pixel-cat">
  ...
</svg>
```

- `viewBox`: **반드시 `0 0 200 200`** (모든 펫 동일, 애니메이션 transform-origin 통일용)
- `width/height`: `200`
- root `<svg>`에 `class="pixel-{이름}"` 부착 (예: `pixel-cat`, `pixel-dog`)

### 부위 element

각 부위는 **`<g class="{부위명}">`** 로 감싸기. 클래스 이름은 아래 표 그대로:

| 부위 클래스 | 사용 펫 |
|---|---|
| `body` | 모든 펫 (공통, 몸통) |
| `head` | 대부분 (몸통과 분리 가능한 경우만) |
| `tail` | cat, dog, fox, devil, dinosaur, dragon |
| `ears` | cat, rabbit, unicorn |
| `wings` | bat, dragon |
| `eyes` | 모든 펫 (눈 깜빡임용) |
| `mouth` | monster (입 열고 닫기) |
| `antenna` | robot, alien |
| `nose` | pig |
| `horn` | unicorn |
| `mane` | unicorn |

### transform-origin 가이드

CSS 애니메이션의 `transform-origin`은 viewBox 좌표 기준:
- `tail`: 꼬리가 몸통에 붙는 지점 (보통 `120px 100px` 근처)
- `ears`: 귀 뿌리 (`80px 60px`)
- `wings`: 어깨 (`80px 90px` / `120px 90px`)
- `eyes`: 눈 중앙
- `mouth`: 위턱과 아래턱 경계

### 픽셀 아트 느낌

- `shape-rendering="crispEdges"` 속성을 root `<svg>` 또는 각 `<rect>`에 추가
- 부위는 `<rect>` 사각형 모자이크로 (anti-alias 없는 픽셀 룩)
- 둥근 곡선 지양 → `<path>` 보다 `<rect>` 위주

---

## CSS 애니메이션 (재사용 가능)

별도 `pets.css` 파일로 분리하거나 각 SVG의 `<defs><style>` 안에 인라인.

```css
/* 꼬리 흔들기 */
.pixel-cat .tail,
.pixel-dog .tail,
.pixel-fox .tail,
.pixel-devil .tail,
.pixel-dinosaur .tail {
  animation: tail-wag 0.8s steps(2, end) infinite;
  transform-origin: 120px 100px;
}
@keyframes tail-wag {
  0%, 100% { transform: translateX(0); }
  50%      { transform: translateX(6px); }
}

/* 귀 까딱 */
.pixel-cat .ears,
.pixel-rabbit .ears,
.pixel-unicorn .ears {
  animation: ear-wiggle 1.5s steps(2, end) infinite;
  transform-origin: 80px 60px;
}
@keyframes ear-wiggle {
  0%, 100% { transform: translateY(0); }
  50%      { transform: translateY(-3px); }
}

/* 날개 파닥 */
.pixel-bat .wings,
.pixel-dragon .wings {
  animation: wing-flap 0.7s steps(2, end) infinite;
  transform-origin: 80px 90px;
}
@keyframes wing-flap {
  0%, 100% { transform: scaleY(1); }
  50%      { transform: scaleY(0.85); }
}

/* 눈 깜빡임 */
.pixel-bear .eyes,
.pixel-penguin .eyes,
.pixel-chick .eyes,
.pixel-panda .eyes,
.pixel-frog .eyes {
  animation: blink 4s steps(1, end) infinite;
  transform-origin: center;
}
@keyframes blink {
  0%, 96%, 100% { transform: scaleY(1); }
  98%           { transform: scaleY(0.1); }
}

/* 전체 통통 (몸 전체 bounce) */
.pixel-cat, .pixel-dog, .pixel-unicorn, .pixel-dragon,
.pixel-dinosaur, .pixel-monster, .pixel-chick {
  animation: body-bounce 1.2s ease-in-out infinite;
  transform-origin: center bottom;
}
@keyframes body-bounce {
  0%, 100% { transform: translateY(0); }
  50%      { transform: translateY(-4px); }
}

/* 좌우 뒤뚱 */
.pixel-penguin, .pixel-devil, .pixel-panda {
  animation: body-sway 2s ease-in-out infinite;
  transform-origin: center bottom;
}
@keyframes body-sway {
  0%, 100% { transform: rotate(0deg); }
  25%      { transform: rotate(3deg); }
  75%      { transform: rotate(-3deg); }
}

/* 둥둥 떠다님 */
.pixel-ghost, .pixel-bat, .pixel-alien {
  animation: float 3s ease-in-out infinite;
}
@keyframes float {
  0%, 100% { transform: translateY(0); }
  50%      { transform: translateY(-6px); }
}

/* 슬라임 / 개구리 / 돼지 squish */
.pixel-slime, .pixel-frog, .pixel-pig {
  animation: squish 1.5s ease-in-out infinite;
  transform-origin: center bottom;
}
@keyframes squish {
  0%, 100% { transform: scaleY(1) scaleX(1); }
  50%      { transform: scaleY(0.85) scaleX(1.1); }
}

/* 로봇 딱딱한 틱 */
.pixel-robot {
  animation: tick 0.6s steps(2, end) infinite;
}
@keyframes tick {
  0%, 50% { transform: translateX(0); }
  51%, 100% { transform: translateX(1px); }
}
```

---

## 산출물

1. **`pets/cat.svg`** ~ **`pets/devil.svg`** (20개) + 기존 `pets/claudecode.svg`
2. **`pets/pets.css`** — 위 CSS 통합본
3. **`pets/preview.html`** — 모든 펫을 한 페이지에 보여주는 미리보기 (격자 레이아웃, 각 펫 옆에 이름 라벨)

`preview.html` 템플릿:

```html
<!DOCTYPE html>
<html>
<head>
  <link rel="stylesheet" href="pets.css">
  <style>
    body { background: #f4f3ee; font-family: system-ui; padding: 20px; }
    .grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 20px; max-width: 900px; }
    .cell { text-align: center; }
    .cell img { width: 100px; height: 100px; image-rendering: pixelated; }
    .cell .name { margin-top: 6px; font-size: 13px; color: #333; }
  </style>
</head>
<body>
  <div class="grid">
    <div class="cell"><img src="cat.svg" alt="cat"><div class="name">cat</div></div>
    <!-- ... 21개 반복 ... -->
  </div>
</body>
</html>
```

---

## 픽셀 아트 디자인 팁

- viewBox `0 0 200 200` 안에서 캐릭터는 대략 `40,40 ~ 160,160` 범위에 배치 (외곽 20% 여백)
- 한 픽셀 단위 = viewBox 8~10 단위 (즉 200×200 안에 20×20 픽셀 그리드 정도)
- 색 팔레트는 펫당 3~5색 (메인색·진한그림자·하이라이트·눈색·강조색)
- 검정 윤곽선보다 어두운 톤의 그림자 색이 픽셀 아트답고 부드러움

---

## 통합 (나중에 본인이 직접)

위젯에 SVG 펫 통합은 별도 작업. 두 가지 옵션:

### Option A: 정적 PNG로 변환 (가장 쉬움, 애니메이션 손실)
- 각 SVG의 첫 프레임만 PNG로 export
- 현재 위젯 그대로 사용 가능
- 부위 애니메이션은 위젯에서 못 보지만 캐릭터 모양은 더 다양

### Option B: 애니메이션 프레임 sprite sheet
- 각 펫의 0%/25%/50%/75% 프레임을 PNG로 export (4프레임 × 21펫 = 84 PNG)
- 위젯에서 프레임 cycling
- 부위 애니메이션 살아남음

### Option C: 위젯을 Electron / pywebview 기반으로 변경
- HTML+CSS로 렌더링 → 원본 SVG 애니메이션 그대로
- 가장 깔끔하지만 위젯 전면 재작성

본인이 SVG 받은 후 어떤 옵션이 좋은지 정해서 작업하시면 됩니다.
