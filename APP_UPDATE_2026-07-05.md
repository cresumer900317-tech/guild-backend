# 길드라운지 앱 업데이트 가이드 (2026-07-05 개편 반영)

백엔드는 배포 완료 — 앱(`guild-app-54`)은 클라이언트만 수정하면 됨.
이전 가이드(`APP_UPDATE_2026-06-29.md`)의 후속.

## 1. 길드 건강도 성장축 가동 (앱에 건강도 화면 있으면)
`GET /api/guild-health` 응답에 성장 필드가 추가됨:
```jsonc
{
  // ...기존 필드...
  "growthRatio": 0.9032,      // 주간(+1% 이상) 전투력 성장 멤버 비율 (0~1, null=이력 없음)
  "growthMedianPct": 31.0,    // 멤버 주간 성장률 중앙값 (%)
  "growthSampled": 27,        // 두 시점 모두 데이터 있던 멤버 수
  "growthBaseDate": "2026-06-29"  // 비교 기준일 (~7일 전)
}
```
점수 계산(웹 ranking.js와 동일):
```ts
const growth = g.growthRatio != null ? g.growthRatio * 100 : null;
// growth·balance·activity 모두 있으면 4축: 성장0.30 · 활동0.25 · 깊이0.25 · 균형0.20
// growth가 null이면 기존 3축 가중치(깊이0.42·활동0.33·균형0.25)로 폴백
```
축 표시 순서: 성장(초록 #16a34a) → 활동 → 깊이 → 균형.

## 2. 제거된 엔드포인트 (앱은 안 쓰므로 무영향, 참고만)
- `GET /api/rivals` (구 경쟁길드 비교, 싸이월드/리안 하드코딩) — 삭제됨
- `POST /api/rivals/crawl`·`/api/rivals/snapshot` — 삭제됨
- `GET/POST/DELETE /api/contributions` (공헌도) — 삭제됨
- ⚠️ `/api/rival-picks`(멤버 1:1 라이벌)와 `/api/weekly`(앱 랭킹 주간탭)는 **그대로 살아있음**

## 3. 신규 푸시 (백엔드 only — 앱 수정 불필요)
- **좋아요 알림**: 내 글에 좋아요 → `❤️ 좋아요` 푸시, data `{type, id}` → 기존 routeFromNotification이 /post로 딥링크 (댓글 알림과 동일 메커니즘, 이미 작동)
- **크롤 실패 운영진 알림**: 크롤 연속 3~4회 실패 시 admin/superadmin에게 ⚠️ 푸시

## 4. 글 작성 API 인증 필수화 (앱은 이미 토큰 보내므로 무영향)
`POST /api/tips`·`POST /api/free`가 이제 `Authorization: Bearer` 필수 + author는 토큰 기준 강제.
앱 lib/api.ts가 토큰 자동 첨부하므로 변경 불필요. (401 뜨면 로그인 만료 케이스만 처리)

## 5. 웹 변경 참고 (컨셉 통일)
- 웹 브랜드 통일: "메이플키우기 라운지 · 스카니아11 서버" — 앱 내 표기도 동일 컨벤션 권장
- 웹 홈·푸터에 App Store 링크 추가됨 (웹→앱 유입 동선)
- 공지·팁 읽기 비로그인 공개됨 (웹만, 앱은 로그인 게이트 유지해도 무방)
