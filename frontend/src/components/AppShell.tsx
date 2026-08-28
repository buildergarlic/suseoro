import type { ReactNode } from "react";
import { useRef } from "react";
import { Link } from "react-router-dom";

import type { User } from "../api/client";

interface AppShellProps {
  user: User | null;
  children: ReactNode;
  onLogout?: () => void;
}

export function AppShell({ user, children, onLogout }: AppShellProps) {
  const mainRef = useRef<HTMLElement>(null);

  return (
    <div className="app-shell">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          mainRef.current?.focus();
        }}
      >
        본문으로 건너뛰기
      </a>
      <header className="site-header">
        <Link className="brand" to={user ? "/workspaces" : "/login"} aria-label="수서로 홈">
          <span aria-hidden="true" className="brand-mark">
            ㅅ
          </span>
          <span>수서로</span>
        </Link>
        {user ? (
          <div className="user-summary">
            <span>
              <strong>{user.display_name}</strong>님
            </span>
            <button className="button button-quiet" type="button" onClick={onLogout}>
              로그아웃
            </button>
          </div>
        ) : null}
      </header>
      <main id="main-content" ref={mainRef} tabIndex={-1}>
        {children}
      </main>
      <footer className="site-footer">학교도서관의 차분한 수서 일을 돕습니다.</footer>
    </div>
  );
}
