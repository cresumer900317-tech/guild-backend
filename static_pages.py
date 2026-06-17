"""앱스토어 제출용 정적 페이지(개인정보처리방침·지원). main.py에서 HTMLResponse로 서빙."""

_STYLE = """
<style>
  body{max-width:720px;margin:0 auto;padding:28px 20px 64px;
    font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo',sans-serif;
    color:#1a1a1a;line-height:1.7;font-size:16px;}
  h1{font-size:24px;margin:0 0 4px;} h2{font-size:18px;margin:28px 0 8px;}
  .sub{color:#6e727a;font-size:14px;margin-bottom:24px;}
  ul{padding-left:20px;} li{margin:4px 0;} a{color:#5b8def;}
  table{border-collapse:collapse;width:100%;margin:8px 0;font-size:15px;}
  td,th{border:1px solid #ececef;padding:8px 10px;text-align:left;vertical-align:top;}
  th{background:#f5f6f8;}
</style>
"""

PRIVACY_HTML = f"""<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>길드라운지 개인정보처리방침</title>{_STYLE}</head><body>
<h1>개인정보처리방침</h1>
<p class="sub">앱 이름: 길드라운지 · 시행일자: 2026년 6월 18일</p>

<p>길드라운지(이하 "서비스")는 이용자의 개인정보를 중요하게 생각하며, 아래와 같이 수집·이용·보관합니다.
본 서비스는 특정 게임 커뮤니티(길드) 구성원을 위한 비공개 그룹 앱입니다.</p>

<h2>1. 수집하는 개인정보 항목</h2>
<table>
<tr><th>구분</th><th>항목</th></tr>
<tr><td>회원가입</td><td>캐릭터명(닉네임), 비밀번호(암호화 저장), 이메일, 생년월일</td></tr>
<tr><td>서비스 이용</td><td>푸시 알림 토큰(기기 식별자), 기기 운영체제 종류</td></tr>
</table>

<h2>2. 수집·이용 목적</h2>
<ul>
<li>회원 식별 및 로그인 인증</li>
<li>커뮤니티(길드) 구성원 확인</li>
<li>콘텐츠 일정 알림(푸시) 발송</li>
<li>랭킹·게시판 등 서비스 제공 및 문의 응대</li>
</ul>

<h2>3. 보유 및 이용 기간</h2>
<ul>
<li>회원 탈퇴 시 보유 정보를 <b>지체 없이 파기</b>합니다. (앱 내 [프로필 → 회원 탈퇴] 또는 운영진 요청)</li>
<li>관계 법령에 따라 보존이 필요한 경우 해당 기간 동안 보관 후 파기합니다.</li>
</ul>

<h2>4. 제3자 제공</h2>
<p>서비스는 이용자의 개인정보를 외부에 제공하지 않습니다.</p>

<h2>5. 처리 위탁 (인프라)</h2>
<p>서비스 운영을 위해 아래 사업자의 인프라를 이용합니다.</p>
<ul>
<li>Supabase — 데이터베이스 저장</li>
<li>Railway — 서버 호스팅</li>
<li>Expo (Expo Push) — 푸시 알림 발송</li>
</ul>

<h2>6. 이용자의 권리</h2>
<p>이용자는 언제든지 본인의 개인정보를 열람·수정할 수 있으며, 앱 내 <b>[프로필 → 회원 탈퇴]</b>를 통해
계정과 개인정보 삭제를 직접 요청할 수 있습니다.</p>

<h2>7. 만 14세 미만 아동</h2>
<p>본 서비스는 만 14세 이상 이용을 권장하며, 생년월일은 회원 식별 및 연령 확인 목적으로만 사용됩니다.</p>

<h2>8. 개인정보 보호 문의</h2>
<p>개인정보 관련 문의: <a href="mailto:qkqhrnfl@icloud.com">qkqhrnfl@icloud.com</a></p>
</body></html>"""

SUPPORT_HTML = f"""<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>길드라운지 지원</title>{_STYLE}</head><body>
<h1>길드라운지 지원</h1>
<p class="sub">문의 및 고객지원</p>
<p>길드라운지 이용 중 문의·오류 제보·계정 관련 도움이 필요하시면 아래로 연락해 주세요.</p>
<h2>문의</h2>
<ul>
<li>이메일: <a href="mailto:qkqhrnfl@icloud.com">qkqhrnfl@icloud.com</a></li>
</ul>
<h2>자주 묻는 질문</h2>
<ul>
<li><b>가입이 안 돼요</b> — 길드에 등록된 캐릭터명으로만 가입할 수 있으며, 가입 후 운영진 승인이 필요합니다.</li>
<li><b>알림이 안 와요</b> — [프로필 → 푸시 알림]이 켜져 있는지, 기기 설정에서 알림이 허용됐는지 확인해 주세요.</li>
<li><b>탈퇴하고 싶어요</b> — [프로필 → 회원 탈퇴]에서 직접 처리할 수 있습니다.</li>
</ul>
</body></html>"""

TERMS_HTML = f"""<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>길드라운지 이용약관</title>{_STYLE}</head><body>
<h1>이용약관</h1>
<p class="sub">앱 이름: 길드라운지 · 시행일자: 2026년 6월 18일</p>

<h2>제1조 (목적)</h2>
<p>본 약관은 길드라운지(이하 "서비스")의 이용 조건 및 회원과 서비스 간의 권리·의무를 규정합니다.</p>

<h2>제2조 (이용 자격)</h2>
<p>서비스는 길드에 등록된 구성원만 가입할 수 있으며, 가입 후 운영진의 승인을 거쳐 이용할 수 있습니다.</p>

<h2>제3조 (금지 행위 · 콘텐츠 정책)</h2>
<p><b>서비스는 부적절한 콘텐츠와 이용자 괴롭힘에 대해 무관용 원칙을 적용합니다.</b> 회원은 다음 행위를 해서는 안 됩니다.</p>
<ul>
<li>욕설·비방·차별·혐오·성적·폭력적 표현 등 타인에게 불쾌감을 주는 콘텐츠 게시</li>
<li>타인 괴롭힘, 사칭, 개인정보 무단 게시</li>
<li>불법 정보, 스팸, 광고성 게시물 등록</li>
</ul>
<p>위반 시 해당 게시물은 사전 통지 없이 삭제될 수 있으며, 이용자는 이용 제한 또는 강제 탈퇴될 수 있습니다.</p>

<h2>제4조 (신고 및 조치)</h2>
<p>회원은 부적절한 게시물·댓글 또는 이용자를 앱 내 <b>신고</b> 기능으로 신고할 수 있고, 다른 이용자를 <b>차단</b>할 수 있습니다.
운영진은 신고 접수 후 <b>24시간 이내</b>에 검토하여 게시물 삭제·이용 제한 등 필요한 조치를 합니다.</p>

<h2>제5조 (계정 및 탈퇴)</h2>
<p>회원은 앱 내 [프로필 → 회원 탈퇴]를 통해 언제든지 계정과 개인정보 삭제를 요청할 수 있습니다.</p>

<h2>제6조 (면책)</h2>
<p>서비스가 제공하는 게임 관련 수치·순위 등 정보는 외부 데이터에 기반하며 정확성을 보장하지 않습니다.
회원이 게시한 콘텐츠에 대한 책임은 해당 회원에게 있습니다.</p>

<h2>문의</h2>
<p><a href="mailto:qkqhrnfl@icloud.com">qkqhrnfl@icloud.com</a></p>
</body></html>"""
