import { type FormEvent, useEffect, useState } from "react";

import type { SuseoroApi, User } from "../../api/client";

interface LoginScreenProps {
  api: SuseoroApi;
  onAuthenticated: (user: User) => void;
}

function errorMessage(error: unknown): string {
  if (
    typeof error === "object" &&
    error !== null &&
    "detail" in error &&
    typeof error.detail === "object" &&
    error.detail !== null &&
    "message" in error.detail &&
    typeof error.detail.message === "string"
  ) {
    return error.detail.message;
  }
  if (error instanceof Error) return error.message;
  return "로그인하지 못했습니다. 잠시 후 다시 시도해 주세요.";
}

export function LoginScreen({ api, onAuthenticated }: LoginScreenProps) {
  const [schoolId, setSchoolId] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    document.title = "로그인 · 수서로";
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const authenticated = await api.login({
        school_id: schoolId.trim(),
        username: username.trim(),
        password,
      });
      onAuthenticated(authenticated);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="login-shell" aria-labelledby="login-title">
      <div className="login-introduction">
        <p className="eyebrow">학교도서관 수서 도우미</p>
        <h1 id="login-title">로그인</h1>
        <p className="lede">
          추천자료를 넣으면 우리 도서관에 없는 책부터 차근차근 찾아드려요.
        </p>
      </div>
      <form className="login-card" onSubmit={(event) => void submit(event)}>
        {error ? <p role="alert" className="message message-error">{error}</p> : null}
        <label>
          <span>학교 코드</span>
          <input
            autoComplete="organization"
            required
            value={schoolId}
            onChange={(event) => setSchoolId(event.currentTarget.value)}
          />
        </label>
        <label>
          <span>아이디</span>
          <input
            autoComplete="username"
            required
            value={username}
            onChange={(event) => setUsername(event.currentTarget.value)}
          />
        </label>
        <label>
          <span>비밀번호</span>
          <input
            autoComplete="current-password"
            required
            type="password"
            value={password}
            onChange={(event) => setPassword(event.currentTarget.value)}
          />
        </label>
        <button className="button button-primary button-wide" disabled={submitting} type="submit">
          {submitting ? "확인하고 있어요…" : "로그인"}
        </button>
      </form>
    </section>
  );
}
