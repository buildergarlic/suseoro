import { Modal } from "./Modal";

export const DEVELOPER_SPONSOR_URL = "https://github.com/sponsors/buildergarlic";

export function SupportDialog({ onClose }: { onClose: () => void }) {
  return <Modal title="개발자 후원" description="수서로를 함께 가꾸어 주세요." onClose={onClose} className="support-modal">
    <div className="modal-body support-body">
      <div className="support-illustration" aria-hidden="true">
        <svg viewBox="0 0 120 96" fill="none"><path d="M17 69V25c16-8 30-7 43 1 13-8 27-9 43-1v44c-17-6-29-5-43 3-14-8-26-9-43-3Z" fill="#fffef9" stroke="currentColor" strokeWidth="3" /><path d="M60 27v44M29 39c8-2 14-1 21 2m-21 8c8-2 14-1 21 2m20-10c7-3 13-4 21-2m-21 12c7-3 13-4 21-2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" opacity=".55" /><path d="M60 89S43 79 43 69c0-9 12-13 17-4 5-9 17-5 17 4 0 10-17 20-17 20Z" fill="#bc7961" stroke="#f7f7f1" strokeWidth="3" /></svg>
      </div>
      <p className="support-lead">사서 선생님의 시간을 아끼는 도구로<br />꾸준히 다듬어 가겠습니다.</p>
      <p>수서로는 빌더갈릭이 만드는 무료 오픈소스 프로그램입니다. 자발적인 후원은 오류 수정, 추천도서 파일 읽기 개선, 사용설명서 정비와 새 버전 배포를 이어가는 데 힘이 됩니다.</p>
      <div className="support-choice"><strong>후원은 자유롭게 선택해 주세요.</strong><p>후원하지 않아도 수서로의 모든 기능을 사용할 수 있습니다.</p></div>
      <a className="button primary support-action" href={DEVELOPER_SPONSOR_URL} target="_blank" rel="noopener noreferrer">GitHub에서 후원하기 <span aria-hidden="true">↗</span></a>
      <p className="support-caption">브라우저에서 빌더갈릭의 후원 페이지가 열립니다.<br />금액과 후원 방식은 그곳에서 직접 선택합니다.</p>
    </div>
    <footer className="modal-footer"><span className="spacer" /><button className="button secondary" onClick={onClose}>목록으로 돌아가기</button></footer>
  </Modal>;
}
