# 길드라운지 앱 업데이트 가이드 (2026-06-29 친구들.com 개편 반영)

웹(친구들.com = 메이플키우기 라운지)과 백엔드가 크게 바뀌었습니다. 앱(`guild-app-54`)은
같은 Railway API(`https://guild-backend-production-75a6.up.railway.app`)를 공유하므로,
아래 변경을 앱에도 반영하면 됩니다. **백엔드는 이미 배포 완료** — 앱은 클라이언트만 수정.

---

## 1. 리브랜딩: "스카니아 라운지" → "메이플키우기 라운지"
- 앱 내 브랜드명/타이틀/스플래시/스토어 카피에서 **"스카니아 라운지" → "메이플키우기 라운지"**
- 부제/서브타이틀은 **"스카니아11 서버"** 유지 (정확성)
- 예: 헤더 "메이플키우기 라운지", 부제 "스카니아11 서버 포털"

## 2. 게시판 리스트는 반드시 `summary=true` (성능 필수)
- `GET /api/tips` · `GET /api/free` 는 기본적으로 **본문(content) 전체**를 반환하는데,
  본문에 base64 이미지가 인라인되어 **리스트 응답이 7~8MB**까지 커짐.
- **리스트 화면에서는 반드시 `?summary=true`** 사용 → content 제외, 목록용 가벼운 필드만(수 KB).
  - `GET /api/tips?summary=true` → `id,title,author,author_guild,likes,views,created_at,category`
  - `GET /api/free?summary=true` → `id,title,author,author_guild,likes,views,created_at`
- **전체 본문은 상세 화면에서만**: `GET /api/tips/{id}`, `GET /api/free/{id}`
- ⚠️ 앱 리스트가 summary 없이 호출 중이면 즉시 `?summary=true` 추가 (웹은 이번에 다 전환함)

## 3. 신규 엔드포인트: `GET /api/guild-health` (길드 건강도)
스카니아11 서버 길드들의 **활력 통계**를 길드별로 집계해 반환 (상위 30, 캐시 5분, ~9KB).
앱에서 server-ranking 전체(6820명·1MB)를 받아 계산할 필요 없이 이것만 쓰면 됨.

응답(배열) 각 항목:
```jsonc
{
  "guildRank": 4, "guildName": "친구들", "level": 10, "members": 27,
  "power": 28989280476980000, "topPower": ..., "lowPower": ..., "avgMemberPower": ...,
  "memberSampled": 27,            // server-ranking에서 잡힌 멤버 수 (분포 산출 표본)
  "medianPower": 641000000000000, // 중앙값 전투력
  "effContributors": 6.1,         // 유효 기여자 수 (1/허핀달지수, "사실상 몇 명이 떠받치나")
  "activeRatio": 0.96             // 인기도 ≥50 멤버 비율 (활동)
}
```

### 건강도 점수 계산 (클라이언트, 웹 ranking.js와 동일 공식)
점수는 프론트에서 위 raw 통계로 계산합니다(튜닝 자유도 위해). TS로 옮기면:
```ts
const clamp = (v: number) => Math.max(0, Math.min(100, v));
// 깊이: 본대가 두꺼운가 (중앙값 전투력, 로그 절대앵커 — 1조=50점, ×10마다 +12.5)
const depth = (medianPower: number) =>
  medianPower > 0 ? clamp(50 + 12.5 * Math.log10(medianPower / 1e12)) : 0;
// 균형: 한두 명에 안 업혔는가 (유효기여자 1명→0, 10명→100)
const balance = (eff: number | null) =>
  eff != null ? clamp((eff - 1) / 9 * 100) : null;
// 활동: 활발한 멤버 비율 (인기도≥50)
const activity = (activeRatio: number | null) =>
  activeRatio != null ? activeRatio * 100 : null;

// 종합 (성장축 미가동 → 깊이0.42·활동0.33·균형0.25 비례 재분배)
function healthScore(g) {
  const d = depth(g.medianPower);
  const b = (g.memberSampled >= 3) ? balance(g.effContributors) : null;
  const a = activity(g.activeRatio);
  const parts = [[d, 0.42], [a, 0.33], [b, 0.25]].filter(p => p[0] != null);
  const wsum = parts.reduce((s, [, w]) => s + w, 0) || 1;
  return Math.round(parts.reduce((s, [v, w]) => s + v * w, 0) / wsum);
}
// 점수 색: ≥70 초록(#16a34a) · ≥55 황(#f59e0b) · ≥40 주황(#fb923c) · else 회(#94a3b8)
```
- 정렬: 건강도 점수 내림차순. (전투력 순위와 다름 — 리안 1위, 싸이월드는 전력1위지만 쏠림으로 중위)
- 표시 축: **활동 · 깊이 · 균형** (각 0~100). 카드 근거: 중앙값 전투력 · 유효 기여자 N명 · 활동 멤버 %
- **성장축은 아직 숨김** — 아래 4번 참고.

## 4. 길드 건강도 "성장축"은 아직 미가동
- 4번째 축 **성장(주간 전투력 성장)**은 이력 데이터가 충분히 안 쌓여(현재 며칠치) **미표시**.
- 앱도 성장축은 숨기거나 생략. **~2026-07 초** 백엔드에서 `server_ranking_history` 기반
  주간 성장률을 `/api/guild-health`에 `growth` 필드로 추가하면, 그때 4축으로 켤 예정.
- 가동 시 가중치: 성장30 · 활동25 · 깊이25 · 균형20.

## 5. 성능/캐싱 (앱 변경 불필요, 참고)
- `/api/server-ranking`(6820명), `/api/guild-health`, `/api/home-summary`, `/api/notices`,
  `/api/visitors/stats` 등은 **백엔드 메모리 캐시** 적용 → 더 빨라짐.
- ⚠️ 데이터가 크롤(수~12시간)때만 바뀌고 캐시 TTL 5~10분이라, **크롤 직후 최대 5~10분은 옛 값**일 수 있음(무해).

## 6. 프로필 성장 그래프 (앱에 동일 화면 있으면 참고)
- `GET /api/server-ranking/history?name={캐릭터명}` → 일별 `{date, serverRank, power, popularity, guild}`.
- 웹은 그래프에 **실제 값 라벨**(전투력 compact·순위 N위)을 시작·현재점에 표시하도록 개선함.
- ⚠️ 쿼리 파라미터명은 `name` (not `nickname`).

---

## 변경 커밋 (참고)
- backend: `8609411`(캐시 truncate fix) · `7f36ddb`(캐싱 + `/api/guild-health` 신설)
- web: `34c9e04`(리브랜딩+홈3단) · `541df36`(건강도 유효기여자) · `3bd06a3`(건강도 4축) ·
  `b90c059`(캡션정리) · `8dc1085`(프로필 그래프 값) · `3c66271`/`5b382cb`(팁·자유 summary)

## 친구패밀리 = 친구들·친구둘·친구삼·친구넷·친구닷 (메인=친구들, superadmin=친구닷)
