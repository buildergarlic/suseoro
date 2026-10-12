# 업데이트 오류 수신 경로

Netlify 프로젝트 `suseoro-update-reports`의 정적 폼 `update-errors`를 사용합니다. 이 폴더의 `index.html`과 `_headers`만 배포합니다. Forms를 켠 뒤 배포하고, Forms → Submission notifications의 이메일 수신처를 `buildergarlic@gmail.com`으로 설정합니다. 발신자는 Netlify이며 PC에 이메일 비밀번호나 서버 인증키를 넣지 않습니다.

클라이언트는 앱 버전, 오류 단계·종류·코드, OS 정보, 임의 설치 식별자, 발생 시각만 전송합니다. 원본 desktop.log, 학교 이름, 도서 목록, 사용자 이름, 파일 경로, 인증키는 전송하지 않습니다. 동일 오류는 하루에 한 번 보고하고, 연결이 끊기면 최대 50건을 PC에 보관한 뒤 다시 시도합니다. 설정에서 보고를 끌 수 있습니다.

배포 후 시험 보고가 Netlify의 Verified submissions와 수신 메일에 나타나는지 확인합니다. Spam으로 분류된 경우 Netlify의 공식 Forms 문제 해결 안내에 따라 검토합니다. HTTP 접수 성공만으로 이메일 도착을 단정하지 않습니다.
