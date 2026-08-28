import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";

import type { SuseoroApi, User } from "../api/client";
import { AppShell } from "../components/AppShell";
import { LoginScreen } from "../features/auth/LoginScreen";
import { WorklistScreen } from "../features/workspaces/WorklistScreen";
import { WorkroomScreen } from "../features/workspaces/WorkroomScreen";

interface AppProps {
  api: SuseoroApi;
}

export function App({ api }: AppProps) {
  const [user, setUser] = useState<User | null>(null);
  const [sessionChecked, setSessionChecked] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    let active = true;
    void api
      .getCurrentUser()
      .then((current) => {
        if (active) setUser(current);
      })
      .catch(() => undefined)
      .finally(() => {
        if (active) setSessionChecked(true);
      });
    return () => {
      active = false;
    };
  }, [api]);

  async function logout() {
    try {
      await api.logout();
    } finally {
      setUser(null);
      void navigate("/login", { replace: true });
    }
  }

  const signedOut = sessionChecked ? (
    <Navigate replace to="/login" />
  ) : (
    <p className="loading-state" role="status">
      업무를 불러오고 있습니다…
    </p>
  );

  return (
    <AppShell user={user} onLogout={() => void logout()}>
      <Routes>
        <Route
          path="/login"
          element={
            user ? (
              <Navigate replace to="/workspaces" />
            ) : (
              <LoginScreen
                api={api}
                onAuthenticated={(authenticated) => {
                  setUser(authenticated);
                  void navigate("/workspaces", { replace: true });
                }}
              />
            )
          }
        />
        <Route
          path="/workspaces"
          element={user ? <WorklistScreen api={api} user={user} /> : signedOut}
        />
        <Route
          path="/workspaces/:workspaceId"
          element={user ? <WorkroomScreen api={api} user={user} /> : signedOut}
        />
        <Route
          path="*"
          element={<Navigate replace to={user ? "/workspaces" : "/login"} />}
        />
      </Routes>
    </AppShell>
  );
}
